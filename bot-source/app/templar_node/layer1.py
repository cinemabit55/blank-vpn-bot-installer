"""Layer 1 local bootstrap package orchestration."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.templar_node.fake_env import FakeEnvironmentError, FakeEnvironmentStore
from app.templar_node.render import render_bundle, render_ufw_plan, write_bundle
from app.templar_node.schemas import CertificateMode, NodeConfig, RealityStrategy, WarpMode
from app.templar_node.secrets import LocalSecretStore, SecretStoreError
from app.templar_node.state import LAYER1_STEPS, NodeStateStore, utc_now_iso


class Layer1Error(RuntimeError):
    """Raised when Layer 1 cannot prepare a safe bootstrap package."""


ProgressReporter = Callable[[str], None]
SSH_CONNECT_OPTIONS = (
    '-o',
    'ConnectTimeout=20',
    '-o',
    'ConnectionAttempts=1',
    '-o',
    'ServerAliveInterval=15',
    '-o',
    'ServerAliveCountMax=2',
)


@dataclass(frozen=True)
class Layer1Result:
    internal_name: str
    output_dir: Path
    written_paths: tuple[Path, ...]
    state_path: Path
    last_completed_step: str

    def to_lines(self) -> list[str]:
        lines = [
            f'Layer 1 local bootstrap package: {self.internal_name}',
            f'Output dir: {self.output_dir}',
            f'Artifacts: {len(self.written_paths)}',
        ]
        lines.extend(str(path) for path in self.written_paths)
        lines.append(f'State: {self.state_path}')
        lines.append(f'Last checkpoint: {self.last_completed_step}')
        return lines


@dataclass(frozen=True)
class Layer1SshBootstrapOptions:
    root_password_ref: str | None = None
    root_private_key_ref: str | None = None
    admin_public_key_ref: str = 'secrets/ssh-admin-public-key'
    admin_private_key_ref: str | None = 'secrets/ssh-admin-private-key'
    dns_api_token_ref: str | None = None
    acme_email: str | None = None
    issue_certificates: bool = True
    harden_ssh: bool = True
    start_services: bool = True
    progress: ProgressReporter | None = None


class Layer1SshClient(Protocol):
    def preflight(self) -> str:
        """Verify root SSH access and return a short remote identity string."""

    def upload_dir(self, local_dir: Path, remote_dir: str) -> None:
        """Upload the rendered bootstrap bundle to a temporary remote directory."""

    def run_root(self, script: str) -> str:
        """Run a shell script as root on the remote host."""

    def verify_admin_key(self, admin_user: str, private_key: str, *, port: int | None = None) -> None:
        """Verify that the newly installed admin SSH key can log in."""


def run_layer1_local_bootstrap(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore,
    state_store: NodeStateStore,
    output_dir: str | Path,
    env_store: FakeEnvironmentStore | None = None,
    progress: ProgressReporter | None = None,
) -> Layer1Result:
    _progress(progress, f'{config.display.internal_name}: checking RemnaWave Node SECRET_KEY')
    secret_check = secret_store.check_ref(config.remnanode.secret_key_ref)
    if not secret_check.ok:
        raise Layer1Error(
            f'missing or unsafe RemnaWave Node SECRET_KEY at {config.remnanode.secret_key_ref}; '
            'run pre-bootstrap first',
        )

    node_output_dir = Path(output_dir).expanduser().resolve() / config.display.internal_name
    _progress(progress, f'{config.display.internal_name}: rendering bootstrap bundle')
    written = tuple(write_bundle(render_bundle(config, secret_store=secret_store), node_output_dir))
    state = state_store.load_or_init(config)
    for checkpoint in LAYER1_STEPS:
        state.mark_checkpoint(checkpoint)
    if env_store is not None:
        _progress(progress, f'{config.display.internal_name}: marking fake node online')
        _mark_fake_node_online(config, env_store)
    _progress(progress, f'{config.display.internal_name}: saving Layer 1 state')
    state_path = state_store.save(state)
    return Layer1Result(
        internal_name=config.display.internal_name,
        output_dir=node_output_dir,
        written_paths=written,
        state_path=state_path,
        last_completed_step=state.last_completed_step or '',
    )


def run_layer1_ssh_bootstrap(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore,
    state_store: NodeStateStore,
    output_dir: str | Path,
    options: Layer1SshBootstrapOptions,
    ssh_client: Layer1SshClient | None = None,
) -> Layer1Result:
    """Run the first live SSH bootstrap layer against a fresh VPS.

    Fresh VPS bootstrap uses the root password via ``sshpass -f`` so the
    password is not placed in argv or logs. Key-only fresh VPSes can use a
    root private key. Existing hardened nodes can omit root credentials and run
    the same root scripts through the admin SSH key.
    """

    _progress(options.progress, f'{config.display.internal_name}: checking SSH/bootstrap secrets')
    _require_ok_secret(secret_store, config.remnanode.secret_key_ref, 'RemnaWave Node SECRET_KEY')
    if options.root_password_ref:
        _require_ok_secret(secret_store, options.root_password_ref, 'root SSH password')
    if options.root_private_key_ref:
        _require_ok_secret(secret_store, options.root_private_key_ref, 'root SSH private key')
    if ssh_client is None and not any((options.root_password_ref, options.root_private_key_ref, options.admin_private_key_ref)):
        raise Layer1Error('SSH bootstrap requires root_password_ref, root_private_key_ref or --admin-private-key-ref')
    dns_api_token = _read_dns_api_token(secret_store, config, options)
    admin_public_key = _read_admin_public_key(secret_store, options.admin_public_key_ref)
    admin_private_key = None
    needs_admin_private_key = options.harden_ssh or (ssh_client is None and not options.root_password_ref and not options.root_private_key_ref)
    if needs_admin_private_key:
        if options.admin_private_key_ref is None:
            raise Layer1Error('SSH hardening/admin-key bootstrap requires --admin-private-key-ref')
        admin_private_key = _read_admin_private_key(secret_store, options.admin_private_key_ref)
    if options.harden_ssh and config.main_server.ipv4 not in config.ssh.admin_allowlist:
        raise Layer1Error(
            f'main server {config.main_server.ipv4} must be present in ssh.admin_allowlist before applying UFW',
        )

    node_output_dir = Path(output_dir).expanduser().resolve() / config.display.internal_name
    _progress(options.progress, f'{config.display.internal_name}: rendering bootstrap bundle')
    written = tuple(write_bundle(render_bundle(config, secret_store=secret_store), node_output_dir))
    public_key_path = node_output_dir / 'opt/templar-node/admin_authorized_key.pub'
    public_key_path.parent.mkdir(parents=True, exist_ok=True)
    public_key_path.write_text(admin_public_key.rstrip() + '\n', encoding='utf-8')
    public_key_path.chmod(0o644)
    acme_token_path = _write_acme_token_file(node_output_dir, dns_api_token) if dns_api_token is not None else None

    if ssh_client is not None:
        client = ssh_client
    elif options.root_password_ref:
        client = SshpassRootClient(
            host=config.public_ipv4,
            root_password_path=secret_store.path_for_ref(options.root_password_ref),
        )
    elif options.root_private_key_ref:
        client = RootKeyClient(
            host=config.public_ipv4,
            private_key=_read_private_key(secret_store, options.root_private_key_ref, 'root SSH private key'),
        )
    else:
        assert admin_private_key is not None
        client = AdminKeySudoClient(
            host=config.public_ipv4,
            admin_user=config.ssh.admin_user,
            private_key=admin_private_key,
            port=config.ssh.port,
        )
    remote_tmp = f'/var/lib/templar-node-bootstrap/{config.display.internal_name}'

    try:
        _progress(options.progress, f'{config.display.internal_name}: SSH preflight to {config.public_ipv4}')
        client.preflight()
        _progress(options.progress, f'{config.display.internal_name}: preparing remote bootstrap directory')
        client.run_root(_remote_pre_hardening_script(remote_tmp))
        _progress(options.progress, f'{config.display.internal_name}: uploading bootstrap bundle')
        client.upload_dir(node_output_dir, remote_tmp)
        _remove_local_acme_token(acme_token_path)
        _progress(
            options.progress,
            f'{config.display.internal_name}: installing packages, Docker, Caddy and certificates on VPS; this can take 10-20 minutes',
        )
        client.run_root(_remote_install_script(config, remote_tmp, options=options))
        if options.harden_ssh:
            assert admin_private_key is not None
            _progress(options.progress, f'{config.display.internal_name}: verifying admin SSH key')
            client.verify_admin_key(config.ssh.admin_user, admin_private_key)
            _progress(options.progress, f'{config.display.internal_name}: applying SSH hardening and UFW')
            client.run_root(_remote_hardening_script(config))
            _progress(options.progress, f'{config.display.internal_name}: verifying hardened SSH port {config.ssh.port}')
            client.verify_admin_key(config.ssh.admin_user, admin_private_key, port=config.ssh.port)
    except (OSError, subprocess.SubprocessError, SecretStoreError) as exc:
        _remove_local_acme_token(acme_token_path)
        raise Layer1Error(str(exc)) from exc

    state = state_store.load_or_init(config)
    for checkpoint in LAYER1_STEPS:
        state.mark_checkpoint(checkpoint)
    state.update_discovered(
        {
            'bootstrap_adapter': 'ssh',
            'ssh_client': 'root_password' if options.root_password_ref else 'root_key' if options.root_private_key_ref else 'admin_key',
            'ssh_admin_user': config.ssh.admin_user,
            'ssh_hardened': options.harden_ssh,
            'certificate_issue': 'external_acme_dns01' if _should_issue_external_acme(config, options) else 'skipped',
        },
    )
    state_path = state_store.save(state)
    return Layer1Result(
        internal_name=config.display.internal_name,
        output_dir=node_output_dir,
        written_paths=written,
        state_path=state_path,
        last_completed_step=state.last_completed_step or '',
    )


class SshpassRootClient:
    def __init__(self, *, host: str, root_password_path: Path, port: int = 22):
        self.host = host
        self.root_password_path = root_password_path
        self.port = port
        _require_tool('sshpass')
        _require_tool('ssh')
        _require_tool('scp')

    def preflight(self) -> str:
        return self.run_root('set -e; hostname; id -u; . /etc/os-release; echo "$ID $VERSION_ID"')

    def upload_dir(self, local_dir: Path, remote_dir: str) -> None:
        self.run_root(f'install -d -m 0700 {sh_quote(remote_dir)}')
        args = [
            'sshpass',
            '-f',
            str(self.root_password_path),
            'scp',
            '-P',
            str(self.port),
            *_ssh_connect_options(),
            '-o',
            'StrictHostKeyChecking=accept-new',
            '-o',
            'UserKnownHostsFile=/root/.ssh/known_hosts',
            '-r',
            f'{local_dir}/.',
            f'root@{self.host}:{remote_dir}/',
        ]
        _run(args)

    def run_root(self, script: str) -> str:
        args = [
            'sshpass',
            '-f',
            str(self.root_password_path),
            'ssh',
            '-p',
            str(self.port),
            *_ssh_connect_options(),
            '-o',
            'StrictHostKeyChecking=accept-new',
            '-o',
            'UserKnownHostsFile=/root/.ssh/known_hosts',
            'root@' + self.host,
            'bash -s',
        ]
        return _run(args, input_text=script).stdout

    def verify_admin_key(self, admin_user: str, private_key: str, *, port: int | None = None) -> None:
        verify_port = port or self.port
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False) as key_file:
            key_file.write(private_key.rstrip() + '\n')
            key_path = Path(key_file.name)
        try:
            key_path.chmod(0o600)
            args = [
                'ssh',
                '-i',
                str(key_path),
                '-p',
                str(verify_port),
                *_ssh_connect_options(),
                '-o',
                'BatchMode=yes',
                '-o',
                'IdentitiesOnly=yes',
                '-o',
                'StrictHostKeyChecking=accept-new',
                '-o',
                'UserKnownHostsFile=/root/.ssh/known_hosts',
                f'{admin_user}@{self.host}',
                'true',
            ]
            _run(args)
        finally:
            key_path.unlink(missing_ok=True)


class RootKeyClient:
    def __init__(self, *, host: str, private_key: str, port: int = 22):
        self.host = host
        self.private_key = private_key
        self.port = port
        _require_tool('ssh')
        _require_tool('scp')

    def preflight(self) -> str:
        return self.run_root('set -e; hostname; id -u; . /etc/os-release; echo "$ID $VERSION_ID"')

    def upload_dir(self, local_dir: Path, remote_dir: str) -> None:
        self.run_root(f'rm -rf {sh_quote(remote_dir)} && install -d -m 0700 {sh_quote(remote_dir)}')
        key_path = _write_temp_private_key(self.private_key)
        try:
            args = [
                'scp',
                '-i',
                str(key_path),
                '-P',
                str(self.port),
                *_ssh_connect_options(),
                '-o',
                'BatchMode=yes',
                '-o',
                'IdentitiesOnly=yes',
                '-o',
                'StrictHostKeyChecking=accept-new',
                '-o',
                'UserKnownHostsFile=/root/.ssh/known_hosts',
                '-r',
                f'{local_dir}/.',
                f'root@{self.host}:{remote_dir}/',
            ]
            _run(args)
        finally:
            key_path.unlink(missing_ok=True)

    def run_root(self, script: str) -> str:
        key_path = _write_temp_private_key(self.private_key)
        try:
            args = [
                'ssh',
                '-i',
                str(key_path),
                '-p',
                str(self.port),
                *_ssh_connect_options(),
                '-o',
                'BatchMode=yes',
                '-o',
                'IdentitiesOnly=yes',
                '-o',
                'StrictHostKeyChecking=accept-new',
                '-o',
                'UserKnownHostsFile=/root/.ssh/known_hosts',
                'root@' + self.host,
                'bash -s',
            ]
            return _run(args, input_text=script).stdout
        finally:
            key_path.unlink(missing_ok=True)

    def verify_admin_key(self, admin_user: str, private_key: str, *, port: int | None = None) -> None:
        verify_port = port or self.port
        key_path = _write_temp_private_key(private_key)
        try:
            args = [
                'ssh',
                '-i',
                str(key_path),
                '-p',
                str(verify_port),
                *_ssh_connect_options(),
                '-o',
                'BatchMode=yes',
                '-o',
                'IdentitiesOnly=yes',
                '-o',
                'StrictHostKeyChecking=accept-new',
                '-o',
                'UserKnownHostsFile=/root/.ssh/known_hosts',
                f'{admin_user}@{self.host}',
                'true',
            ]
            _run(args)
        finally:
            key_path.unlink(missing_ok=True)


class AdminKeySudoClient:
    def __init__(self, *, host: str, admin_user: str, private_key: str, port: int = 22):
        self.host = host
        self.admin_user = admin_user
        self.private_key = private_key
        self.port = port
        _require_tool('ssh')
        _require_tool('scp')

    def preflight(self) -> str:
        return self.run_root('set -e; hostname; id -u; . /etc/os-release; echo "$ID $VERSION_ID"')

    def upload_dir(self, local_dir: Path, remote_dir: str) -> None:
        self.run_root(
            f'rm -rf {sh_quote(remote_dir)} && '
            f'install -d -m 0770 -o {sh_quote(self.admin_user)} -g {sh_quote(self.admin_user)} {sh_quote(remote_dir)}',
        )
        key_path = _write_temp_private_key(self.private_key)
        try:
            args = [
                'scp',
                '-i',
                str(key_path),
                '-P',
                str(self.port),
                *_ssh_connect_options(),
                '-o',
                'BatchMode=yes',
                '-o',
                'IdentitiesOnly=yes',
                '-o',
                'StrictHostKeyChecking=accept-new',
                '-o',
                'UserKnownHostsFile=/root/.ssh/known_hosts',
                '-r',
                f'{local_dir}/.',
                f'{self.admin_user}@{self.host}:{remote_dir}/',
            ]
            _run(args)
        finally:
            key_path.unlink(missing_ok=True)

    def run_root(self, script: str) -> str:
        key_path = _write_temp_private_key(self.private_key)
        try:
            args = [
                'ssh',
                '-i',
                str(key_path),
                '-p',
                str(self.port),
                *_ssh_connect_options(),
                '-o',
                'BatchMode=yes',
                '-o',
                'IdentitiesOnly=yes',
                '-o',
                'StrictHostKeyChecking=accept-new',
                '-o',
                'UserKnownHostsFile=/root/.ssh/known_hosts',
                f'{self.admin_user}@{self.host}',
                'sudo -n bash -s',
            ]
            return _run(args, input_text=script).stdout
        finally:
            key_path.unlink(missing_ok=True)

    def verify_admin_key(self, admin_user: str, private_key: str, *, port: int | None = None) -> None:
        verify_port = port or self.port
        key_path = _write_temp_private_key(private_key)
        try:
            args = [
                'ssh',
                '-i',
                str(key_path),
                '-p',
                str(verify_port),
                *_ssh_connect_options(),
                '-o',
                'BatchMode=yes',
                '-o',
                'IdentitiesOnly=yes',
                '-o',
                'StrictHostKeyChecking=accept-new',
                '-o',
                'UserKnownHostsFile=/root/.ssh/known_hosts',
                f'{admin_user}@{self.host}',
                'true',
            ]
            _run(args)
        finally:
            key_path.unlink(missing_ok=True)


def _progress(progress: ProgressReporter | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _mark_fake_node_online(config: NodeConfig, env_store: FakeEnvironmentStore) -> None:
    try:
        environment = env_store.load()
        node = environment['remnawave']['nodes'].get(config.display.internal_name)
        if node is None:
            raise Layer1Error(f'fake RemnaWave Node {config.display.internal_name!r} is missing; run pre-bootstrap first')
        node['online'] = True
        node['online_checked_at'] = utc_now_iso()
        env_store.save(environment)
    except FakeEnvironmentError as exc:
        raise Layer1Error(str(exc)) from exc


def _require_ok_secret(secret_store: LocalSecretStore, ref: str, label: str) -> None:
    check = secret_store.check_ref(ref)
    if not check.ok:
        raise Layer1Error(f'missing or unsafe {label} at {ref}')


def _write_temp_private_key(private_key: str) -> Path:
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False) as key_file:
        key_file.write(private_key.rstrip() + '\n')
        key_path = Path(key_file.name)
    key_path.chmod(0o600)
    return key_path


def _ssh_connect_options() -> list[str]:
    return list(SSH_CONNECT_OPTIONS)


def _read_admin_public_key(secret_store: LocalSecretStore, ref: str) -> str:
    _require_ok_secret(secret_store, ref, 'admin SSH public key')
    value = secret_store.read_text(ref)
    if not value.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-sha2-')):
        raise Layer1Error(f'admin SSH public key {ref} has unsupported format')
    return value


def _read_private_key(secret_store: LocalSecretStore, ref: str, label: str) -> str:
    _require_ok_secret(secret_store, ref, label)
    value = secret_store.read_text(ref)
    if 'PRIVATE KEY' not in value:
        raise Layer1Error(f'{label} {ref} has unsupported format')
    return value


def _read_admin_private_key(secret_store: LocalSecretStore, ref: str) -> str:
    return _read_private_key(secret_store, ref, 'admin SSH private key')


def _read_dns_api_token(
    secret_store: LocalSecretStore,
    config: NodeConfig,
    options: Layer1SshBootstrapOptions,
) -> str | None:
    if not _should_issue_external_acme(config, options):
        return None
    ref = options.dns_api_token_ref or config.site.dns_api_token_ref
    if ref is None:
        raise Layer1Error('external ACME DNS-01 certificate issue requires --dns-api-token-ref or site.dns_api_token_ref')
    _require_ok_secret(secret_store, ref, 'Cloudflare DNS API token')
    token = secret_store.read_text(ref).strip()
    if not token:
        raise Layer1Error(f'Cloudflare DNS API token at {ref} is empty')
    return token


def _write_acme_token_file(node_output_dir: Path, token: str) -> Path:
    acme_dir = node_output_dir / 'opt/templar-node/acme'
    acme_dir.mkdir(parents=True, exist_ok=True)
    acme_dir.chmod(0o700)
    token_path = acme_dir / 'cloudflare-token'
    token_path.write_text(token.rstrip() + '\n', encoding='utf-8')
    token_path.chmod(0o600)
    return token_path


def _remove_local_acme_token(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise Layer1Error(f'cannot remove temporary ACME token file {path}: {exc}') from exc


def _should_issue_external_acme(config: NodeConfig, options: Layer1SshBootstrapOptions) -> bool:
    return (
        options.start_services
        and options.issue_certificates
        and config.reality.strategy == RealityStrategy.LOCAL_DECOY_SITE
        and config.site.certificate_mode == CertificateMode.FILE
        and config.site.certificate_source == 'external_acme_dns01'
    )


def _remote_pre_hardening_script(remote_tmp: str) -> str:
    return f"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
install -d -m 0700 {sh_quote(remote_tmp)}
"""


def _remote_install_script(config: NodeConfig, remote_tmp: str, *, options: Layer1SshBootstrapOptions) -> str:
    start_services_flag = '1' if options.start_services else '0'
    warp_native_flag = '1' if config.warp.mode == WarpMode.XRAY_NATIVE else '0'
    certificate_script = _remote_certificate_script(config, options=options)
    return f"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
TMP_DIR={sh_quote(remote_tmp)}
ADMIN_USER={sh_quote(config.ssh.admin_user)}
START_SERVICES={start_services_flag}
WARP_NATIVE={warp_native_flag}

. /etc/os-release
echo "templar-node os=$ID version=$VERSION_ID"

apt_lock_busy() {{
  if command -v fuser >/dev/null 2>&1; then
    fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock >/dev/null 2>&1
    return $?
  fi
  pgrep -x apt >/dev/null 2>&1 && return 0
  pgrep -x apt-get >/dev/null 2>&1 && return 0
  pgrep -x dpkg >/dev/null 2>&1 && return 0
  return 1
}}

wait_for_apt() {{
  local timeout="${{1:-600}}"
  local waited=0
  while apt_lock_busy; do
    if [ "$waited" -ge "$timeout" ]; then
      echo "templar-node apt/dpkg lock is still busy after $timeout seconds" >&2
      command -v fuser >/dev/null 2>&1 && fuser -v /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock || true
      ps -eo pid,comm,args | grep -E 'apt|dpkg|unattended' | grep -v grep || true
      exit 1
    fi
    echo 'templar-node apt/dpkg lock is busy; waiting 10s'
    sleep 10
    waited=$((waited + 10))
  done
}}

install_caddy() {{
  if command -v caddy >/dev/null 2>&1; then
    return 0
  fi
  wait_for_apt 600
  if apt-get install -y caddy; then
    return 0
  fi
  echo 'templar-node caddy package is absent in default apt sources; adding official Caddy repo'
  wait_for_apt 600
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg.tmp
  mv -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg.tmp /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  chmod 0644 /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list
  wait_for_apt 600
  apt-get update
  wait_for_apt 600
  apt-get install -y caddy
}}

wait_for_apt 600
apt-get update
wait_for_apt 600
apt-get install -y ca-certificates curl gnupg ufw jq git rsync logrotate docker.io
install_caddy
if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
  wait_for_apt 600
  apt-get install -y docker-compose-v2
elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
  wait_for_apt 600
  apt-get install -y docker-compose-plugin
fi

systemctl enable --now docker
docker compose version >/dev/null

if [ "$WARP_NATIVE" = "1" ]; then
  install -d -m 0755 /dev/net
  if [ ! -c /dev/net/tun ]; then
    mknod /dev/net/tun c 10 200
  fi
  chmod 0666 /dev/net/tun
fi

if ! id -u "$ADMIN_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$ADMIN_USER"
fi
usermod -aG sudo "$ADMIN_USER"
usermod -aG docker "$ADMIN_USER" || true
printf '%s\\n' "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$ADMIN_USER"
chmod 0440 "/etc/sudoers.d/90-$ADMIN_USER"

install -d -m 0700 -o "$ADMIN_USER" -g "$ADMIN_USER" "/home/$ADMIN_USER/.ssh"
install -m 0600 -o "$ADMIN_USER" -g "$ADMIN_USER" "$TMP_DIR/opt/templar-node/admin_authorized_key.pub" "/home/$ADMIN_USER/.ssh/authorized_keys"

install -d -m 0755 /opt/remnanode /opt/templar-node /etc/caddy /etc/caddy/certs /opt/node-site/public
if getent passwd caddy >/dev/null 2>&1; then
  install -d -m 0750 -o caddy -g caddy /var/log/caddy
  touch /var/log/caddy/node-decoy.log
  chown caddy:caddy /var/log/caddy/node-decoy.log
  chmod 0640 /var/log/caddy/node-decoy.log
else
  install -d -m 0755 /var/log/caddy
fi
install -m 0600 "$TMP_DIR/opt/remnanode/.env" /opt/remnanode/.env
install -m 0644 "$TMP_DIR/opt/remnanode/docker-compose.yml" /opt/remnanode/docker-compose.yml
install -m 0644 "$TMP_DIR/opt/templar-node/ufw-plan.txt" /opt/templar-node/ufw-plan.txt
install -m 0755 "$TMP_DIR/opt/templar-node/network-tuning.sh" /opt/templar-node/network-tuning.sh
install -m 0644 "$TMP_DIR/opt/templar-node/xray-snippets.json" /opt/templar-node/xray-snippets.json
install -m 0644 "$TMP_DIR/etc/caddy/Caddyfile" /etc/caddy/Caddyfile
install -m 0644 "$TMP_DIR/etc/systemd/system/templar-node-network-tuning.service" /etc/systemd/system/templar-node-network-tuning.service
systemctl daemon-reload
systemctl enable --now templar-node-network-tuning.service
if [ -d "$TMP_DIR/opt/node-site/public" ]; then
  rsync -a --delete "$TMP_DIR/opt/node-site/public/" /opt/node-site/public/
  find /opt/node-site/public -type d -exec chmod 0755 {{}} \\;
  find /opt/node-site/public -type f -exec chmod 0644 {{}} \\;
fi

{certificate_script}

if [ "$START_SERVICES" = "1" ]; then
  (cd /opt/remnanode && docker compose up -d)
  if grep -q 'remote_dest REALITY strategy' /etc/caddy/Caddyfile; then
    echo 'templar-node caddy skipped: remote_dest strategy'
  elif grep -q '/etc/caddy/certs/fullchain.pem' /etc/caddy/Caddyfile && [ ! -s /etc/caddy/certs/fullchain.pem ]; then
    echo 'templar-node caddy skipped: certificate files are not installed yet'
  else
    caddy validate --config /etc/caddy/Caddyfile
    systemctl enable --now caddy
    if ! timeout 30 systemctl reload caddy; then
      echo 'templar-node caddy reload failed or timed out; restarting caddy'
      timeout 60 systemctl restart caddy
    fi
  fi
fi
"""


def _remote_certificate_script(config: NodeConfig, *, options: Layer1SshBootstrapOptions) -> str:
    if not _should_issue_external_acme(config, options):
        return "echo 'templar-node certificate issue skipped'"
    domain = sh_quote(config.domain)
    email = sh_quote(options.acme_email or config.site.contact_email)
    return f"""ACME_DOMAIN={domain}
ACME_EMAIL={email}
ACME_TOKEN_FILE="$TMP_DIR/opt/templar-node/acme/cloudflare-token"
if [ "$START_SERVICES" = "1" ]; then
  if [ ! -s "$ACME_TOKEN_FILE" ]; then
    echo 'templar-node certificate issue failed: missing Cloudflare DNS token file' >&2
    exit 1
  fi
  wait_for_apt 600
  apt-get install -y certbot python3-certbot-dns-cloudflare
  install -d -m 0700 /etc/letsencrypt /etc/letsencrypt/renewal-hooks/deploy
  if getent group caddy >/dev/null 2>&1; then
    install -d -m 0750 -o root -g caddy /etc/caddy/certs
  else
    install -d -m 0700 /etc/caddy/certs
  fi
  umask 077
  printf 'dns_cloudflare_api_token = %s\\n' "$(tr -d '\\r\\n' < "$ACME_TOKEN_FILE")" > /etc/letsencrypt/cloudflare.ini
  chmod 0600 /etc/letsencrypt/cloudflare.ini
  rm -f "$ACME_TOKEN_FILE"
  if [ ! -s "/etc/letsencrypt/live/$ACME_DOMAIN/fullchain.pem" ] || [ ! -s "/etc/letsencrypt/live/$ACME_DOMAIN/privkey.pem" ]; then
    certbot certonly \\
      --non-interactive \\
      --agree-tos \\
      --keep-until-expiring \\
      --email "$ACME_EMAIL" \\
      --dns-cloudflare \\
      --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \\
      --dns-cloudflare-propagation-seconds 60 \\
      -d "$ACME_DOMAIN"
  fi
  install -m 0644 "/etc/letsencrypt/live/$ACME_DOMAIN/fullchain.pem" /etc/caddy/certs/fullchain.pem
  if getent group caddy >/dev/null 2>&1; then
    install -m 0640 -o root -g caddy "/etc/letsencrypt/live/$ACME_DOMAIN/privkey.pem" /etc/caddy/certs/privkey.pem
  else
    install -m 0600 "/etc/letsencrypt/live/$ACME_DOMAIN/privkey.pem" /etc/caddy/certs/privkey.pem
  fi
  cat > /etc/letsencrypt/renewal-hooks/deploy/templar-caddy.sh <<HOOK
#!/usr/bin/env bash
set -euo pipefail
DOMAIN="$ACME_DOMAIN"
if getent group caddy >/dev/null 2>&1; then
  install -d -m 0750 -o root -g caddy /etc/caddy/certs
else
  install -d -m 0700 /etc/caddy/certs
fi
install -m 0644 "/etc/letsencrypt/live/\\$DOMAIN/fullchain.pem" /etc/caddy/certs/fullchain.pem
if getent group caddy >/dev/null 2>&1; then
  install -m 0640 -o root -g caddy "/etc/letsencrypt/live/\\$DOMAIN/privkey.pem" /etc/caddy/certs/privkey.pem
else
  install -m 0600 "/etc/letsencrypt/live/\\$DOMAIN/privkey.pem" /etc/caddy/certs/privkey.pem
fi
if systemctl is-active --quiet caddy; then
  caddy validate --config /etc/caddy/Caddyfile
  if ! timeout 30 systemctl reload caddy; then
    echo 'templar-node caddy reload failed or timed out; restarting caddy'
    timeout 60 systemctl restart caddy
  fi
fi
HOOK
  chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/templar-caddy.sh
  systemctl enable --now certbot.timer || true
fi"""


def _remote_hardening_script(config: NodeConfig) -> str:
    ufw_commands = '\n'.join(_force_ufw_command(line) for line in render_ufw_plan(config).splitlines() if line and not line.startswith('#'))
    return f"""set -euo pipefail
ADMIN_USER={sh_quote(config.ssh.admin_user)}
SSH_PORT={int(config.ssh.port)}
{ufw_commands}
if [ -x /opt/templar-node/network-tuning.sh ]; then
  /opt/templar-node/network-tuning.sh
fi
cat > /etc/ssh/sshd_config.d/99-templar-node.conf <<EOF
Port $SSH_PORT
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
AllowUsers $ADMIN_USER root
EOF
sshd -t
systemctl reload ssh || systemctl reload sshd
"""


def _force_ufw_command(command: str) -> str:
    if command == 'ufw enable':
        return 'ufw --force enable'
    return command


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise Layer1Error(f'{name} is required for SSH bootstrap; install it on the control-plane server')


def _run(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - argv is built as a list, secrets are read from files, no shell is used.
            args,
            input=input_text,
            text=True,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or '').strip()
        stdout = (exc.stdout or '').strip()
        detail = stderr or stdout or f'exit code {exc.returncode}'
        raise Layer1Error(f'command failed: {args[0]}: {detail}') from exc


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"

#!/usr/bin/env bash
set -Eeuo pipefail

INSTALLER_REPO_URL="${INSTALLER_REPO_URL:-https://github.com/cinemabit55/blank-vpn-bot-installer.git}"
INSTALLER_REF="${INSTALLER_REF:-main}"
INSTALLER_DIR="${INSTALLER_DIR:-/opt/blank-vpn-bot-installer}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() {
  printf '[status] %s\n' "$*"
}

git_auth_args=()
if [[ -n "${INSTALLER_GITHUB_TOKEN:-}" ]]; then
  git_auth_args=(-c "http.extraHeader=Authorization: Bearer ${INSTALLER_GITHUB_TOKEN}")
fi

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

if [[ "${EUID}" -ne 0 ]]; then
  die "Run as root: sudo bash scripts/install_blank_vpn_bot.sh"
fi

log "Installing base packages"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates curl git jq openssl python3 python3-venv rsync tar ufw
  apt-get install -y --no-install-recommends python3-pil || true
  if ! command -v docker >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends docker.io
  fi
  if ! docker compose version >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends docker-compose-v2 || \
      apt-get install -y --no-install-recommends docker-compose-plugin || true
  fi
else
  log "apt-get is not available; assuming packages are already installed"
fi

command -v git >/dev/null 2>&1 || die "git is required"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "python3 is required"
command -v docker >/dev/null 2>&1 || die "docker is required"
docker compose version >/dev/null 2>&1 || die "docker compose plugin is required"

if [[ -d "$INSTALLER_DIR/.git" ]]; then
  log "Updating installer repo: $INSTALLER_DIR"
  git "${git_auth_args[@]}" -C "$INSTALLER_DIR" fetch --depth=1 origin "$INSTALLER_REF"
  git -C "$INSTALLER_DIR" checkout -q FETCH_HEAD
else
  log "Cloning installer repo: $INSTALLER_REPO_URL"
  rm -rf "$INSTALLER_DIR"
  git "${git_auth_args[@]}" clone --depth=1 --branch "$INSTALLER_REF" "$INSTALLER_REPO_URL" "$INSTALLER_DIR"
fi

log "Starting Python installer"
if [[ ! -t 0 && -r /dev/tty ]]; then
  exec "$PYTHON_BIN" "$INSTALLER_DIR/blank_vpn_bot_installer/installer.py" "$@" < /dev/tty
fi
exec "$PYTHON_BIN" "$INSTALLER_DIR/blank_vpn_bot_installer/installer.py" "$@"

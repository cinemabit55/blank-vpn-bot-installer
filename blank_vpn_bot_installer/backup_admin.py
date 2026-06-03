#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_BOT_DIR = Path("/opt/bedolaga")
DEFAULT_REMNAWAVE_DIR = Path("/opt/remnawave")
DEFAULT_CADDY_DIR = Path("/opt/caddy-remnawave")
DEFAULT_CABINET_DIR = Path("/opt/cabinet")
DEFAULT_STATE_DIR = Path("/opt/blank-vpn-bot-installer")
DEFAULT_OUTPUT_DIR = DEFAULT_STATE_DIR / "backups"


@dataclass(frozen=True)
class BackupPaths:
    bot_dir: Path = DEFAULT_BOT_DIR
    remnawave_dir: Path = DEFAULT_REMNAWAVE_DIR
    caddy_dir: Path = DEFAULT_CADDY_DIR
    cabinet_dir: Path = DEFAULT_CABINET_DIR
    state_dir: Path = DEFAULT_STATE_DIR
    output_dir: Path = DEFAULT_OUTPUT_DIR


def status(message: str) -> None:
    print(f"[status] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[warn] {message}", flush=True)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def copy_if_exists(source: Path, staging_root: Path, arcname: str) -> bool:
    if not source.exists():
        return False
    target = staging_root / arcname
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)
    return True


def run_dump(args: list[str], *, cwd: Path, output_path: Path) -> bool:
    status(f"dump {' '.join(args)}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("w", encoding="utf-8") as handle:
            subprocess.run(args, cwd=str(cwd), check=True, text=True, stdout=handle)
    except (OSError, subprocess.CalledProcessError) as exc:
        warn(f"database dump skipped/failed: {exc}")
        if output_path.exists():
            output_path.unlink()
        return False
    return True


def dump_databases(paths: BackupPaths, staging_root: Path, *, skip_db: bool) -> None:
    if skip_db:
        status("database dumps skipped by request")
        return
    if shutil.which("docker") is None:
        warn("docker is not available; database dumps skipped")
        return

    bot_env = parse_env(paths.bot_dir / ".env")
    if (paths.bot_dir / "docker-compose.yml").exists() and bot_env:
        run_dump(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "-e",
                f"PGPASSWORD={bot_env.get('POSTGRES_PASSWORD', '')}",
                "postgres",
                "pg_dump",
                "-U",
                bot_env.get("POSTGRES_USER", "remnawave_user"),
                "-d",
                bot_env.get("POSTGRES_DB", "remnawave_bot"),
                "--clean",
                "--if-exists",
            ],
            cwd=paths.bot_dir,
            output_path=staging_root / "db" / "bot.sql",
        )

    panel_env = parse_env(paths.remnawave_dir / ".env")
    if (paths.remnawave_dir / "docker-compose.yml").exists() and panel_env:
        run_dump(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "-e",
                f"PGPASSWORD={panel_env.get('POSTGRES_PASSWORD', '')}",
                "remnawave-db",
                "pg_dump",
                "-U",
                panel_env.get("POSTGRES_USER", "postgres"),
                "-d",
                panel_env.get("POSTGRES_DB", "postgres"),
                "--clean",
                "--if-exists",
            ],
            cwd=paths.remnawave_dir,
            output_path=staging_root / "db" / "remnawave.sql",
        )


def collect_static_files(paths: BackupPaths, staging_root: Path, *, include_runtime: bool) -> int:
    items = [
        (paths.bot_dir / ".env", "bedolaga/.env"),
        (paths.bot_dir / "docker-compose.yml", "bedolaga/docker-compose.yml"),
        (paths.bot_dir / "vpn_logo.png", "bedolaga/vpn_logo.png"),
        (paths.remnawave_dir / ".env", "remnawave/.env"),
        (paths.remnawave_dir / ".env.subscription", "remnawave/.env.subscription"),
        (paths.remnawave_dir / "docker-compose.yml", "remnawave/docker-compose.yml"),
        (paths.caddy_dir / ".env", "caddy-remnawave/.env"),
        (paths.caddy_dir / "Caddyfile", "caddy-remnawave/Caddyfile"),
        (paths.caddy_dir / "docker-compose.yml", "caddy-remnawave/docker-compose.yml"),
        (paths.cabinet_dir / ".env", "cabinet/.env"),
        (paths.cabinet_dir / "docker-compose.yml", "cabinet/docker-compose.yml"),
        (paths.state_dir / "install-summary.txt", "installer/install-summary.txt"),
        (paths.state_dir / "state.json", "installer/state.json"),
        (paths.state_dir / "answers.last.json", "installer/answers.last.json"),
    ]
    if include_runtime:
        items.extend(
            [
                (paths.bot_dir / "data", "bedolaga/data"),
                (paths.bot_dir / "uploads", "bedolaga/uploads"),
                (paths.bot_dir / "app" / "assets" / "banners", "bedolaga/app/assets/banners"),
            ]
        )
    copied = 0
    for source, arcname in items:
        if copy_if_exists(source, staging_root, arcname):
            copied += 1
    return copied


def create_archive(staging_root: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(staging_root, arcname=staging_root.name)
    archive_path.chmod(0o600)


def create_backup(paths: BackupPaths, *, include_runtime: bool = False, skip_db: bool = False) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    staging_root = paths.output_dir / f".blank-vpn-backup-{timestamp}.tmp"
    archive_path = paths.output_dir / f"blank-vpn-backup-{timestamp}.tar.gz"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True)

    try:
        copied = collect_static_files(paths, staging_root, include_runtime=include_runtime)
        status(f"copied {copied} static item(s)")
        dump_databases(paths, staging_root, skip_db=skip_db)
        create_archive(staging_root, archive_path)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return archive_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a backup archive for a blank VPN bot install")
    parser.add_argument("--bot-dir", type=Path, default=DEFAULT_BOT_DIR)
    parser.add_argument("--remnawave-dir", type=Path, default=DEFAULT_REMNAWAVE_DIR)
    parser.add_argument("--caddy-dir", type=Path, default=DEFAULT_CADDY_DIR)
    parser.add_argument("--cabinet-dir", type=Path, default=DEFAULT_CABINET_DIR)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--include-runtime", action="store_true", help="also include bot data, uploads, and banners")
    parser.add_argument("--skip-db", action="store_true", help="do not try to run pg_dump through Docker")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = BackupPaths(
        bot_dir=args.bot_dir.expanduser().resolve(),
        remnawave_dir=args.remnawave_dir.expanduser().resolve(),
        caddy_dir=args.caddy_dir.expanduser().resolve(),
        cabinet_dir=args.cabinet_dir.expanduser().resolve(),
        state_dir=args.state_dir.expanduser().resolve(),
        output_dir=args.output_dir.expanduser().resolve(),
    )
    archive_path = create_backup(paths, include_runtime=args.include_runtime, skip_db=args.skip_db)
    print()
    print(f"Backup archive: {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

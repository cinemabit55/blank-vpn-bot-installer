#!/usr/bin/env python3
"""Manage Telegram bot banner assets on an installed Bedolaga/Templar server."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - exercised only on incomplete installs
    Image = None
    ImageOps = None

if Image is not None:
    Image.MAX_IMAGE_PIXELS = 60_000_000


BANNERS_RELATIVE_DIR = Path('app/assets/banners')
BANNER_CACHE_RELATIVE_PATH = Path('data/banner_file_ids.json')
BANNER_BACKUP_RELATIVE_DIR = Path('data/banner_backups')
DEFAULT_MAX_DIMENSION = 1600
DEFAULT_JPEG_QUALITY = 88

BANNER_SLOTS: dict[str, dict[str, str]] = {
    'main_menu': {'ru': 'main_menu_ru.jpg', 'en': 'main_menu_en.jpg', 'fallback': 'main_menu.jpg'},
    'profile': {'ru': 'profile_ru.jpg', 'en': 'profile_en.jpg', 'fallback': 'profile.jpg'},
    'referral': {'ru': 'referral_ru.jpg', 'en': 'referral_en.jpg', 'fallback': 'referral.jpg'},
    'support': {'ru': 'support_ru.jpg', 'en': 'support_en.jpg', 'fallback': 'support.jpg'},
    'download': {'ru': 'download_ru.jpg', 'en': 'download_en.jpg', 'fallback': 'download.jpg'},
    'about': {'ru': 'about_ru.jpg', 'en': 'about_en.jpg', 'fallback': 'about.jpg'},
    'resources': {'ru': 'resources_ru.jpg', 'en': 'resources_en.jpg', 'fallback': 'resources.jpg'},
    'welcome': {'ru': 'welcome.jpg', 'en': 'welcome.jpg', 'fallback': 'welcome.jpg'},
}

BANNER_SLOT_LABELS = {
    'main_menu': 'main menu',
    'profile': 'profile / purchase',
    'referral': 'referral',
    'support': 'support',
    'download': 'apps/download',
    'about': 'about',
    'resources': 'resources',
    'welcome': 'welcome/start',
}

LANGUAGE_LABELS = {
    'ru': 'Russian variant',
    'en': 'English variant',
    'fallback': 'fallback variant',
    'all': 'all variants',
}


@dataclass(frozen=True)
class BannerUpdateResult:
    slot: str
    language: str
    written_files: tuple[Path, ...]
    backup_files: tuple[Path, ...]
    cleared_cache_keys: tuple[str, ...]


@dataclass(frozen=True)
class BannerResetResult:
    slot: str
    language: str
    removed_files: tuple[Path, ...]
    backup_files: tuple[Path, ...]
    cleared_cache_keys: tuple[str, ...]


def status(message: str) -> None:
    print(f'[status] {message}', flush=True)


def resolve_repo_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()

    env_dir = os.getenv('TEMPLAR_BOT_REPO_DIR')
    candidates = [
        Path(env_dir).expanduser() if env_dir else None,
        Path.cwd(),
        Path(__file__).resolve().parent.parent,
        Path('/opt/bedolaga'),
    ]
    for candidate in candidates:
        if candidate and (candidate / BANNERS_RELATIVE_DIR).exists():
            return candidate.resolve()
    return Path('/opt/bedolaga')


def banners_dir(repo_dir: Path) -> Path:
    return repo_dir / BANNERS_RELATIVE_DIR


def cache_path(repo_dir: Path) -> Path:
    return repo_dir / BANNER_CACHE_RELATIVE_PATH


def backup_dir(repo_dir: Path, slot: str) -> Path:
    return repo_dir / BANNER_BACKUP_RELATIVE_DIR / slot


def unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def validate_slot(slot: str) -> str:
    normalized = slot.strip()
    if normalized not in BANNER_SLOTS:
        available = ', '.join(BANNER_SLOTS)
        raise ValueError(f"Unknown banner slot '{slot}'. Available: {available}")
    return normalized


def validate_language(language: str) -> str:
    normalized = language.strip().lower()
    if normalized not in LANGUAGE_LABELS:
        available = ', '.join(LANGUAGE_LABELS)
        raise ValueError(f"Unknown banner language '{language}'. Available: {available}")
    return normalized


def target_filenames(slot: str, language: str) -> tuple[str, ...]:
    slot = validate_slot(slot)
    language = validate_language(language)
    variants = BANNER_SLOTS[slot]
    if language == 'all':
        return tuple(unique([variants['ru'], variants['en'], variants['fallback']]))
    return (variants[language],)


def cache_keys_for(slot: str, language: str) -> tuple[str, ...]:
    slot = validate_slot(slot)
    language = validate_language(language)
    if slot == 'welcome' or language in {'fallback', 'all'}:
        return (f'{slot}:ru', f'{slot}:en')
    return (f'{slot}:{language}',)


def load_cache(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'Banner file_id cache is not valid JSON: {path}') from exc
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value}


def clear_cache_keys(repo_dir: Path, keys: Iterable[str]) -> tuple[str, ...]:
    path = cache_path(repo_dir)
    keys_to_clear = tuple(keys)
    if not path.exists():
        return ()

    cache = load_cache(path)
    removed = tuple(key for key in keys_to_clear if key in cache)
    if not removed:
        return ()

    for key in removed:
        cache.pop(key, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix('.tmp')
    if cache:
        tmp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp_path.replace(path)
    else:
        path.unlink()
        if tmp_path.exists():
            tmp_path.unlink()
    return removed


def copy_or_convert_to_jpeg(source_path: Path, destination_path: Path, max_dimension: int, quality: int) -> None:
    tmp_path = destination_path.with_name(f'.{destination_path.name}.tmp')
    if tmp_path.exists():
        tmp_path.unlink()

    if Image is None or ImageOps is None:
        if source_path.suffix.lower() not in {'.jpg', '.jpeg'}:
            raise RuntimeError('Pillow is not installed, so only .jpg/.jpeg banner files can be copied')
        shutil.copy2(source_path, tmp_path)
        tmp_path.replace(destination_path)
        return

    with Image.open(source_path) as raw_image:
        image = ImageOps.exif_transpose(raw_image)
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        if image.mode in {'RGBA', 'LA'} or 'transparency' in image.info:
            rgba = image.convert('RGBA')
            background = Image.new('RGB', rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel('A'))
            image = background
        elif image.mode != 'RGB':
            image = image.convert('RGB')
        image.save(tmp_path, format='JPEG', quality=quality, optimize=True, progressive=True)

    tmp_path.replace(destination_path)


def backup_existing_file(repo_dir: Path, slot: str, destination_path: Path, timestamp: str) -> Path | None:
    if not destination_path.exists():
        return None
    destination_backup_dir = backup_dir(repo_dir, slot)
    destination_backup_dir.mkdir(parents=True, exist_ok=True)
    target = destination_backup_dir / f'{destination_path.name}.{timestamp}.bak'
    shutil.copy2(destination_path, target)
    return target


def set_banner(
    repo_dir: Path,
    slot: str,
    language: str,
    source: Path,
    *,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> BannerUpdateResult:
    slot = validate_slot(slot)
    language = validate_language(language)
    source_path = source.expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f'Banner source file does not exist: {source_path}')

    target_dir = banners_dir(repo_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    written: list[Path] = []
    backups: list[Path] = []

    for file_name in target_filenames(slot, language):
        destination_path = target_dir / file_name
        backup = backup_existing_file(repo_dir, slot, destination_path, timestamp)
        if backup is not None:
            backups.append(backup)
        copy_or_convert_to_jpeg(source_path, destination_path, max_dimension, quality)
        destination_path.chmod(0o644)
        written.append(destination_path)

    removed_cache_keys = clear_cache_keys(repo_dir, cache_keys_for(slot, language))
    return BannerUpdateResult(
        slot=slot,
        language=language,
        written_files=tuple(written),
        backup_files=tuple(backups),
        cleared_cache_keys=removed_cache_keys,
    )


def reset_banner(repo_dir: Path, slot: str, language: str) -> BannerResetResult:
    slot = validate_slot(slot)
    language = validate_language(language)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    removed: list[Path] = []
    backups: list[Path] = []

    for file_name in target_filenames(slot, language):
        destination_path = banners_dir(repo_dir) / file_name
        if not destination_path.exists():
            continue
        backup = backup_existing_file(repo_dir, slot, destination_path, timestamp)
        if backup is not None:
            backups.append(backup)
        destination_path.unlink()
        removed.append(destination_path)

    removed_cache_keys = clear_cache_keys(repo_dir, cache_keys_for(slot, language))
    return BannerResetResult(
        slot=slot,
        language=language,
        removed_files=tuple(removed),
        backup_files=tuple(backups),
        cleared_cache_keys=removed_cache_keys,
    )


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f'{size_bytes} B'
    if size_bytes < 1024 * 1024:
        return f'{size_bytes / 1024:.1f} KB'
    return f'{size_bytes / (1024 * 1024):.1f} MB'


def print_banner_list(repo_dir: Path) -> None:
    print(f'Banner directory: {banners_dir(repo_dir)}')
    print(f'Telegram file_id cache: {cache_path(repo_dir)}')
    for slot, variants in BANNER_SLOTS.items():
        print(f'\n{slot} ({BANNER_SLOT_LABELS.get(slot, slot)})')
        printed: set[str] = set()
        for language in ('ru', 'en', 'fallback'):
            file_name = variants[language]
            if file_name in printed:
                continue
            printed.add(file_name)
            path = banners_dir(repo_dir) / file_name
            if not path.exists():
                print(f'  {language:<8} missing  {file_name}')
                continue
            stat = path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            print(f'  {language:<8} present  {file_name}  {format_size(stat.st_size)}  {mtime}')


def prompt_choice(title: str, values: list[tuple[str, str]]) -> str:
    print(title)
    for index, (value, label) in enumerate(values, start=1):
        print(f'{index}. {value} - {label}')

    while True:
        answer = input(f'Choose [1-{len(values)}]: ').strip()
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(values):
                return values[index - 1][0]
        for value, _label in values:
            if answer == value:
                return value
        print('Invalid choice, try again.')


def prompt_set_args(slot: str | None, language: str | None, source: str | None) -> tuple[str, str, Path]:
    selected_slot = slot or prompt_choice(
        'Select banner slot',
        [(value, BANNER_SLOT_LABELS.get(value, value)) for value in BANNER_SLOTS],
    )
    selected_language = language or prompt_choice(
        'Select banner language',
        [(value, LANGUAGE_LABELS[value]) for value in ('ru', 'en', 'fallback', 'all')],
    )
    selected_source = source
    while not selected_source:
        selected_source = input('Image file path on this server: ').strip()
    return selected_slot, selected_language, Path(selected_source)


def prompt_reset_args(slot: str | None, language: str | None) -> tuple[str, str]:
    selected_slot = slot or prompt_choice(
        'Select banner slot to reset',
        [(value, BANNER_SLOT_LABELS.get(value, value)) for value in BANNER_SLOTS],
    )
    selected_language = language or prompt_choice(
        'Select banner language to reset',
        [(value, LANGUAGE_LABELS[value]) for value in ('ru', 'en', 'fallback', 'all')],
    )
    return selected_slot, selected_language


def confirm_reset(slot: str, language: str, *, yes: bool) -> bool:
    if yes:
        return True
    files = ', '.join(target_filenames(slot, language))
    answer = input(f'Remove banner file(s) {files} after backup? [y/N]: ').strip().lower()
    return answer in {'y', 'yes'}


def print_update_result(result: BannerUpdateResult) -> None:
    status(f'banner updated: {result.slot} {result.language}')
    for path in result.written_files:
        print(f'UPDATED {path}')
    for path in result.backup_files:
        print(f'BACKUP  {path}')
    if result.cleared_cache_keys:
        print(f"CLEARED Telegram file_id cache keys: {', '.join(result.cleared_cache_keys)}")
    else:
        print('Telegram file_id cache did not contain matching keys')
    print('Bot restart is not required; the next screen render will upload the new banner.')


def print_reset_result(result: BannerResetResult) -> None:
    status(f'banner reset: {result.slot} {result.language}')
    for path in result.removed_files:
        print(f'REMOVED {path}')
    for path in result.backup_files:
        print(f'BACKUP  {path}')
    if not result.removed_files:
        print('No banner files were present for this selection.')
    if result.cleared_cache_keys:
        print(f"CLEARED Telegram file_id cache keys: {', '.join(result.cleared_cache_keys)}")


def print_commands() -> None:
    print(
        """Templar bot banner commands:
  add_banner                  interactive banner replacement
  set_banner SLOT LANG FILE   non-interactive banner replacement
  list_banners                show installed banner files
  reset_banner                remove selected banner file(s) after backup

Slots: main_menu, profile, referral, support, download, about, resources, welcome
Languages: ru, en, fallback, all
Examples:
  add_banner
  set_banner main_menu ru /root/banner.png
  set_banner profile all /root/profile.webp
  list_banners
"""
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Manage bot banner images')
    parser.add_argument('--repo-dir', help='Installed bot repo directory, default: auto or /opt/bedolaga')

    subparsers = parser.add_subparsers(dest='command')

    set_parser = subparsers.add_parser('set', help='replace a banner')
    set_parser.add_argument('slot', nargs='?')
    set_parser.add_argument('language', nargs='?')
    set_parser.add_argument('file', nargs='?')
    set_parser.add_argument('--max-dimension', type=int, default=DEFAULT_MAX_DIMENSION)
    set_parser.add_argument('--quality', type=int, default=DEFAULT_JPEG_QUALITY)

    reset_parser = subparsers.add_parser('reset', help='remove selected banner files after backup')
    reset_parser.add_argument('slot', nargs='?')
    reset_parser.add_argument('language', nargs='?')
    reset_parser.add_argument('--yes', action='store_true', help='do not ask for reset confirmation')

    subparsers.add_parser('list', help='list banner files')
    subparsers.add_parser('commands', help='show shell aliases')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or 'set'
    repo_dir = resolve_repo_dir(args.repo_dir)

    try:
        if command == 'commands':
            print_commands()
            return 0
        if command == 'list':
            print_banner_list(repo_dir)
            return 0
        if command == 'set':
            slot, language, source = prompt_set_args(args.slot, args.language, args.file)
            status(f'using bot repo: {repo_dir}')
            result = set_banner(
                repo_dir,
                slot,
                language,
                source,
                max_dimension=args.max_dimension,
                quality=args.quality,
            )
            print_update_result(result)
            return 0
        if command == 'reset':
            slot, language = prompt_reset_args(args.slot, args.language)
            if not confirm_reset(slot, language, yes=args.yes):
                print('Cancelled.')
                return 1
            status(f'using bot repo: {repo_dir}')
            result = reset_banner(repo_dir, slot, language)
            print_reset_result(result)
            return 0
    except (FileNotFoundError, RuntimeError, ValueError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1

    parser.error(f'Unknown command: {command}')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


BannerLog = Callable[[str], None]

BANNERS_RELATIVE_DIR = Path("app/assets/banners")
CANVAS_SIZE = (1200, 675)
JPEG_QUALITY = 88

BANNER_SLOTS: dict[str, dict[str, str]] = {
    "main_menu": {"ru": "main_menu_ru.jpg", "en": "main_menu_en.jpg", "fallback": "main_menu.jpg"},
    "profile": {"ru": "profile_ru.jpg", "en": "profile_en.jpg", "fallback": "profile.jpg"},
    "referral": {"ru": "referral_ru.jpg", "en": "referral_en.jpg", "fallback": "referral.jpg"},
    "support": {"ru": "support_ru.jpg", "en": "support_en.jpg", "fallback": "support.jpg"},
    "download": {"ru": "download_ru.jpg", "en": "download_en.jpg", "fallback": "download.jpg"},
    "about": {"ru": "about_ru.jpg", "en": "about_en.jpg", "fallback": "about.jpg"},
    "resources": {"ru": "resources_ru.jpg", "en": "resources_en.jpg", "fallback": "resources.jpg"},
    "welcome": {"ru": "welcome.jpg", "en": "welcome.jpg", "fallback": "welcome.jpg"},
}

SLOT_TITLES = {
    "main_menu": "Main menu",
    "profile": "Profile",
    "referral": "Referral",
    "support": "Support",
    "download": "Apps",
    "about": "About",
    "resources": "Resources",
    "welcome": "Welcome",
}

SLOT_SUBTITLES = {
    "main_menu": "Choose an action",
    "profile": "Subscription and balance",
    "referral": "Invite and earn",
    "support": "Help center",
    "download": "Install and connect",
    "about": "Service information",
    "resources": "Useful links",
    "welcome": "Start using the service",
}

PALETTE = (
    ((14, 23, 37), (37, 99, 235), (20, 184, 166)),
    ((17, 24, 39), (124, 58, 237), (34, 197, 94)),
    ((15, 23, 42), (245, 158, 11), (59, 130, 246)),
    ((24, 24, 27), (16, 185, 129), (99, 102, 241)),
)


def banner_targets() -> list[tuple[str, str, str]]:
    targets: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for slot, variants in BANNER_SLOTS.items():
        for language, filename in variants.items():
            if filename in seen:
                continue
            seen.add(filename)
            targets.append((slot, language, filename))
    return targets


def install_default_banners(
    bot_dir: Path,
    project_name: str,
    *,
    dry_run: bool = False,
    status: BannerLog = print,
    warn: BannerLog = print,
) -> int:
    targets = banner_targets()
    destination_dir = bot_dir / BANNERS_RELATIVE_DIR
    status(f"default banner pack: {len(targets)} image(s) -> {destination_dir}")
    if dry_run:
        return len(targets)

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        warn("Pillow is not installed; default banner pack was skipped. Install python3-pil or use add_banner later.")
        return 0

    destination_dir.mkdir(parents=True, exist_ok=True)
    for index, (slot, language, filename) in enumerate(targets):
        path = destination_dir / filename
        render_banner(
            path,
            project_name=project_name,
            slot=slot,
            language=language,
            image_module=Image,
            draw_module=ImageDraw,
            font_module=ImageFont,
            palette=PALETTE[index % len(PALETTE)],
        )
    return len(targets)


def render_banner(
    path: Path,
    *,
    project_name: str,
    slot: str,
    language: str,
    image_module,
    draw_module,
    font_module,
    palette: tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]],
) -> None:
    bg, accent, accent_2 = palette
    image = image_module.new("RGB", CANVAS_SIZE, bg)
    draw = draw_module.Draw(image)
    width, height = CANVAS_SIZE

    for offset in range(-260, width, 220):
        color = blend(bg, accent, 0.18 if offset % 2 else 0.26)
        draw.polygon(
            [(offset, 0), (offset + 180, 0), (offset + 420, height), (offset + 240, height)],
            fill=color,
        )
    draw.rounded_rectangle((72, 78, width - 72, height - 78), radius=34, outline=blend(bg, (255, 255, 255), 0.18), width=2)
    draw.rounded_rectangle((92, 98, 210, 216), radius=28, fill=accent)
    draw.ellipse((128, 134, 174, 180), fill=blend(accent, (255, 255, 255), 0.78))
    draw.rounded_rectangle((712, 420, 1098, 506), radius=26, fill=blend(bg, accent_2, 0.54))

    title_font = load_font(font_module, 82, bold=True)
    subtitle_font = load_font(font_module, 34, bold=False)
    small_font = load_font(font_module, 26, bold=False)
    label_font = load_font(font_module, 24, bold=True)

    project = project_name.strip() or "VPN Service"
    title = SLOT_TITLES.get(slot, slot.replace("_", " ").title())
    subtitle = SLOT_SUBTITLES.get(slot, "Ready")
    language_label = {"ru": "RU", "en": "EN", "fallback": "Default"}.get(language, language.upper())

    draw.text((250, 120), project, font=small_font, fill=blend((255, 255, 255), bg, 0.25))
    draw.text((96, 282), title, font=title_font, fill=(255, 255, 255))
    draw.text((100, 386), subtitle, font=subtitle_font, fill=blend((255, 255, 255), bg, 0.18))
    draw.text((742, 442), "Ready to configure", font=label_font, fill=(255, 255, 255))
    draw.text((96, height - 142), language_label, font=label_font, fill=blend((255, 255, 255), bg, 0.35))

    image.save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)


def load_font(font_module, size: int, *, bold: bool):
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    )
    preferred = candidates[0] if bold else candidates[1]
    for path in (preferred, *candidates):
        try:
            return font_module.truetype(path, size)
        except OSError:
            continue
    return font_module.load_default()


def blend(left: tuple[int, int, int], right: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(round(l * (1 - amount) + r * amount) for l, r in zip(left, right))

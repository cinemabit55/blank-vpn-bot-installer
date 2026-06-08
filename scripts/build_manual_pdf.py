#!/usr/bin/env python3
from __future__ import annotations

import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import ListFlowable, ListItem, Paragraph, Preformatted, SimpleDocTemplate, Spacer


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "operator-manual.md"
TARGET = ROOT / "docs" / "operator-manual.pdf"

FONT_CANDIDATES = [
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
    Path(
        "/Users/ilyakiselev/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/"
        "libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/fonts/truetype/DejaVuSans.ttf"
    ),
]
FONT_BOLD_CANDIDATES = [
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    Path(
        "/Users/ilyakiselev/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/"
        "libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/fonts/truetype/DejaVuSans-Bold.ttf"
    ),
]
FONT_MONO_CANDIDATES = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    Path(
        "/Users/ilyakiselev/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/"
        "libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/fonts/truetype/DejaVuSansMono.ttf"
    ),
]


def first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def register_fonts() -> tuple[str, str, str]:
    regular = first_existing(FONT_CANDIDATES)
    bold = first_existing(FONT_BOLD_CANDIDATES)
    mono = first_existing(FONT_MONO_CANDIDATES)
    if not regular:
        return "Helvetica", "Helvetica-Bold", "Courier"

    pdfmetrics.registerFont(TTFont("ManualRegular", str(regular)))
    if bold:
        pdfmetrics.registerFont(TTFont("ManualBold", str(bold)))
        bold_name = "ManualBold"
    else:
        bold_name = "ManualRegular"
    if mono:
        pdfmetrics.registerFont(TTFont("ManualMono", str(mono)))
        mono_name = "ManualMono"
    else:
        mono_name = "Courier"
    pdfmetrics.registerFontFamily(
        "ManualRegular",
        normal="ManualRegular",
        bold=bold_name,
        italic="ManualRegular",
        boldItalic=bold_name,
    )
    return "ManualRegular", bold_name, mono_name


def inline_markup(text: str, mono_font: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" color="#2563eb">\1</a>',
        escaped,
    )

    def repl(match: re.Match[str]) -> str:
        content = match.group(1)
        return f'<font name="{mono_font}">{content}</font>'

    return re.sub(r"`([^`]+)`", repl, escaped)


def flush_paragraph(buffer: list[str], story: list, body: ParagraphStyle, mono_font: str) -> None:
    if not buffer:
        return
    text = " ".join(part.strip() for part in buffer if part.strip())
    if text:
        story.append(Paragraph(inline_markup(text, mono_font), body))
        story.append(Spacer(1, 3 * mm))
    buffer.clear()


def flush_list(
    items: list[str],
    story: list,
    bullet: ParagraphStyle,
    mono_font: str,
    *,
    ordered: bool = False,
    start: int = 1,
) -> None:
    if not items:
        return
    flowable_items = [
        ListItem(Paragraph(inline_markup(item, mono_font), bullet), leftIndent=4 * mm)
        for item in items
    ]
    list_options = {
        "bulletType": "1" if ordered else "bullet",
        "leftIndent": 6 * mm,
    }
    if ordered:
        list_options["start"] = str(start)
    story.append(ListFlowable(flowable_items, **list_options))
    story.append(Spacer(1, 3 * mm))
    items.clear()


def build_story(markdown: str, styles: dict[str, ParagraphStyle], mono_font: str) -> list:
    story: list = []
    paragraph_buffer: list[str] = []
    list_items: list[str] = []
    list_ordered: bool | None = None
    list_start = 1
    code_buffer: list[str] = []
    in_code = False

    def flush_pending_list() -> None:
        nonlocal list_ordered, list_start
        flush_list(
            list_items,
            story,
            styles["bullet"],
            mono_font,
            ordered=bool(list_ordered),
            start=list_start,
        )
        list_ordered = None
        list_start = 1

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()

        if line.startswith("```"):
            if in_code:
                story.append(Preformatted("\n".join(code_buffer), styles["code"]))
                story.append(Spacer(1, 4 * mm))
                code_buffer.clear()
                in_code = False
            else:
                flush_paragraph(paragraph_buffer, story, styles["body"], mono_font)
                flush_pending_list()
                in_code = True
            continue

        if in_code:
            code_buffer.append(line)
            continue

        if not line.strip():
            flush_paragraph(paragraph_buffer, story, styles["body"], mono_font)
            flush_pending_list()
            continue

        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            flush_paragraph(paragraph_buffer, story, styles["body"], mono_font)
            flush_pending_list()
            level = len(heading.group(1))
            style_name = "title" if level == 1 else "heading2" if level == 2 else "heading3"
            story.append(Paragraph(inline_markup(heading.group(2), mono_font), styles[style_name]))
            story.append(Spacer(1, 3 * mm))
            continue

        bullet = re.match(r"^-\s+(.+)$", line)
        if bullet:
            flush_paragraph(paragraph_buffer, story, styles["body"], mono_font)
            if list_ordered is True:
                flush_pending_list()
            list_ordered = False
            list_start = 1
            list_items.append(bullet.group(1))
            continue

        ordered = re.match(r"^(\d+)\.\s+(.+)$", line)
        if ordered:
            flush_paragraph(paragraph_buffer, story, styles["body"], mono_font)
            if list_ordered is False:
                flush_pending_list()
            if list_ordered is not True:
                list_start = int(ordered.group(1))
            list_ordered = True
            list_items.append(ordered.group(2))
            continue

        flush_pending_list()
        paragraph_buffer.append(line)

    flush_paragraph(paragraph_buffer, story, styles["body"], mono_font)
    flush_pending_list()
    return story


def draw_page_number(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#6b7280"))
    canvas.drawCentredString(A4[0] / 2, 8 * mm, str(doc.page))
    canvas.restoreState()


def main() -> int:
    font_regular, font_bold, font_mono = register_fonts()
    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle(
            "ManualTitle",
            parent=base["Title"],
            fontName=font_bold,
            fontSize=20,
            leading=25,
            spaceAfter=6,
        ),
        "heading2": ParagraphStyle(
            "ManualHeading2",
            parent=base["Heading2"],
            fontName=font_bold,
            fontSize=14,
            leading=18,
            textColor=colors.HexColor("#1f2937"),
        ),
        "heading3": ParagraphStyle(
            "ManualHeading3",
            parent=base["Heading3"],
            fontName=font_bold,
            fontSize=12,
            leading=15,
        ),
        "body": ParagraphStyle(
            "ManualBody",
            parent=base["BodyText"],
            fontName=font_regular,
            fontSize=9.5,
            leading=13,
        ),
        "bullet": ParagraphStyle(
            "ManualBullet",
            parent=base["BodyText"],
            fontName=font_regular,
            fontSize=9.5,
            leading=13,
        ),
        "code": ParagraphStyle(
            "ManualCode",
            parent=base["Code"],
            fontName=font_mono,
            fontSize=8,
            leading=10,
            backColor=colors.HexColor("#f3f4f6"),
            borderColor=colors.HexColor("#d1d5db"),
            borderWidth=0.3,
            borderPadding=4,
        ),
    }
    doc = SimpleDocTemplate(
        str(TARGET),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title="Blank VPN Bot Installer Operator Manual",
    )
    story = build_story(SOURCE.read_text(encoding="utf-8"), styles, font_mono)
    doc.build(story, onFirstPage=draw_page_number, onLaterPages=draw_page_number)
    print(TARGET)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

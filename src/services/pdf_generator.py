from __future__ import annotations

from datetime import datetime
from pathlib import Path

import fitz

from src.config import (
    COMPANY_ADDRESS,
    COMPANY_DIRECTOR,
    COMPANY_INN,
    COMPANY_KPP,
    COMPANY_NAME,
    COMPANY_OGRN,
    DELIVERY_DAYS,
    DELIVERY_TERMS,
    KP_TEMPLATES_DIR,
    KP_VAT_LABEL,
    PAYMENT_TERMS,
)
from src.services.excel_generator import _money
from src.services.markup_settings import get_markup_percent
from src.services.models import MatchResult, MatchStatus, ProposalSummary


def ensure_kp_stamp_image() -> Path:
    stamp_path = KP_TEMPLATES_DIR / "kp_stamp.png"
    if stamp_path.exists():
        return stamp_path

    from PIL import Image, ImageDraw, ImageFont

    size = 240
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    blue = (0, 82, 165, 255)
    draw.ellipse([8, 8, size - 8, size - 8], outline=blue, width=7)
    draw.ellipse([18, 18, size - 18, size - 18], outline=(0, 82, 165, 180), width=2)

    lines = ["ООО", "«УЧТЕНДЕР»"]
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 22)
    except OSError:
        font = ImageFont.load_default()

    y = 78
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        draw.text(((size - text_w) / 2, y), line, fill=blue, font=font)
        y += 34

    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(stamp_path, format="PNG")
    return stamp_path


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


class PdfGenerator:
    def generate(
        self,
        results: list[MatchResult],
        summary: ProposalSummary,
        output_path: Path,
        request_number: str = "б/н",
    ) -> Path:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        margin_x = 42
        y = 48
        today = datetime.now().strftime("%d.%m.%Y")

        def write_line(text: str, *, size: float = 11, bold: bool = False, center: bool = False) -> None:
            nonlocal y
            font = "hebo" if bold else "helv"
            page.insert_text(
                fitz.Point(margin_x if not center else 297, y),
                text,
                fontsize=size,
                fontname=font,
                color=(0, 0, 0),
            )
            y += size + 6

        write_line("КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ", size=16, bold=True, center=True)
        write_line(f"на запрос {request_number} от {today} г.", size=11, center=True)

        legal = [COMPANY_NAME]
        if COMPANY_INN:
            legal.append(f"ИНН {COMPANY_INN}")
        if COMPANY_KPP:
            legal.append(f"КПП {COMPANY_KPP}")
        if COMPANY_OGRN:
            legal.append(f"ОГРН {COMPANY_OGRN}")
        write_line(". ".join(legal), size=10)
        if COMPANY_ADDRESS:
            write_line(COMPANY_ADDRESS, size=10)

        y += 8
        headers = ["№", "Наименование", "Ед.", "Кол-во", f"Цена, {KP_VAT_LABEL}", "Сумма"]
        col_x = [margin_x, margin_x + 24, margin_x + 250, margin_x + 280, margin_x + 330, margin_x + 410]
        header_y = y
        for idx, header in enumerate(headers):
            page.insert_text(fitz.Point(col_x[idx], header_y), header, fontsize=9, fontname="hebo")
        y += 18
        page.draw_line(fitz.Point(margin_x, y - 4), fitz.Point(553, y - 4), width=0.8)

        for result in results:
            qty = result.tz_item.quantity
            name = result.tz_item.name
            if result.status == MatchStatus.SIMILAR:
                name = f"{name} *"
            elif result.status == MatchStatus.NOT_FOUND:
                name = f"{name} (не подобрано)"

            unit_price = _money(result.unit_price) if result.unit_price is not None else None
            line_total = _money(result.total_price) if result.total_price is not None else None
            row_values = [
                str(result.tz_item.number),
                name[:90],
                result.tz_item.unit,
                str(qty).replace(".", ","),
                _fmt_money(unit_price),
                _fmt_money(line_total),
            ]
            for idx, value in enumerate(row_values):
                page.insert_text(fitz.Point(col_x[idx], y), value, fontsize=9, fontname="helv")
            y += 14
            if y > 760:
                page = doc.new_page(width=595, height=842)
                y = 48

        y += 6
        page.draw_line(fitz.Point(margin_x, y), fitz.Point(553, y), width=0.8)
        y += 12
        write_line(f"Всего: {_fmt_money(summary.total_price)}", size=11, bold=True)
        y += 8
        write_line(f"Условия оплаты: {PAYMENT_TERMS}", size=10)
        write_line(f"Срок поставки: {DELIVERY_DAYS}", size=10)
        write_line(f"Доставка: {DELIVERY_TERMS}", size=10)
        y += 10
        write_line(
            f"Наценка: {get_markup_percent()}% (каталог/прайс); интернет −5%",
            size=9,
        )
        y += 18
        write_line(COMPANY_DIRECTOR, size=11)

        stamp_path = ensure_kp_stamp_image()
        stamp_size = 110
        stamp_x = 330
        stamp_y = y + 8
        page.insert_image(
            fitz.Rect(stamp_x, stamp_y, stamp_x + stamp_size, stamp_y + stamp_size),
            filename=str(stamp_path),
        )

        y = stamp_y + stamp_size + 16
        write_line("* — позиции, требующие проверки менеджером", size=9)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(output_path)
        doc.close()
        return output_path

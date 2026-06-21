from __future__ import annotations

import logging
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
    KP_STAMP_PATH,
    KP_VAT_LABEL,
    PAYMENT_TERMS,
)
from src.services.excel_generator import _money
from src.services.markup_settings import get_markup_percent
from src.services.models import MatchResult, MatchStatus, ProposalSummary

logger = logging.getLogger(__name__)


def resolve_kp_stamp_image() -> Path | None:
    stamp_path = KP_STAMP_PATH
    if not stamp_path.exists():
        logger.warning("KP stamp image not found: %s", stamp_path)
        return None
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

        stamp_path = resolve_kp_stamp_image()
        if stamp_path:
            from PIL import Image

            with Image.open(stamp_path) as stamp_image:
                stamp_w, stamp_h = stamp_image.size
            display_width = 150
            display_height = max(80, int(display_width * stamp_h / max(stamp_w, 1)))
            stamp_x = 305
            stamp_y = y + 6
            page.insert_image(
                fitz.Rect(
                    stamp_x,
                    stamp_y,
                    stamp_x + display_width,
                    stamp_y + display_height,
                ),
                filename=str(stamp_path),
            )
            y = stamp_y + display_height + 12

        write_line("* — позиции, требующие проверки менеджером", size=9)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(output_path)
        doc.close()
        return output_path

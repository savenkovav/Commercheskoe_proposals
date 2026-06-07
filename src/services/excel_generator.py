from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from src.config import (
    COMPANY_ADDRESS,
    COMPANY_INN,
    COMPANY_KPP,
    COMPANY_NAME,
    COMPANY_OGRN,
    DELIVERY_DAYS,
    DELIVERY_TERMS,
    PAYMENT_TERMS,
)
from src.services.markup_settings import get_markup_percent
from src.services.models import MatchResult, MatchStatus, ProposalSummary

STATUS_LABELS = {
    MatchStatus.EXACT: "Точное совпадение",
    MatchStatus.SIMILAR: "Похожее (проверить)",
    MatchStatus.NOT_FOUND: "Не найдено",
}

STATUS_COLORS = {
    MatchStatus.EXACT: "C6EFCE",
    MatchStatus.SIMILAR: "FFEB9C",
    MatchStatus.NOT_FOUND: "FFC7CE",
}

SOURCE_LABELS = {
    "catalog": "Каталог",
    "registry": "Реестр остатков",
    "price_list": "Прайс поставщика",
    "web": "Интернет (оценка AI)",
    "ai": "AI-подбор",
    "none": "—",
}


def _money(value: float | None) -> float:
    return round(float(value or 0), 2)


def _thin_border() -> Border:
    side = Side(style="thin", color="CCCCCC")
    return Border(left=side, right=side, top=side, bottom=side)


class ExcelGenerator:
    def generate(
        self,
        results: list[MatchResult],
        summary: ProposalSummary,
        output_path: Path,
        request_number: str = "б/н",
    ) -> Path:
        wb = Workbook()

        self._build_kp_sheet(wb.active, results, request_number)
        self._build_detail_sheet(wb.create_sheet("Детализация"), results, summary)
        self._build_summary_sheet(wb.create_sheet("Сводка"), results, summary)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(output_path)
        return output_path

    def _build_kp_sheet(self, ws, results: list[MatchResult], request_number: str) -> None:
        ws.title = "КП"
        today = datetime.now().strftime("%d.%m.%Y")

        ws.merge_cells("A1:F1")
        ws["A1"] = "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ"
        ws["A1"].font = Font(bold=True, size=14)
        ws["A1"].alignment = Alignment(horizontal="center")

        ws.merge_cells("A2:F2")
        ws["A2"] = f"на запрос {request_number} от {today} г."
        ws["A2"].alignment = Alignment(horizontal="center")

        ws.merge_cells("A3:F3")
        ws["A3"] = (
            f"{COMPANY_NAME}. ИНН {COMPANY_INN}. КПП {COMPANY_KPP}. ОГРН {COMPANY_OGRN}"
        )

        ws.merge_cells("A4:F4")
        ws["A4"] = COMPANY_ADDRESS

        headers = [
            "№ п/п",
            "Наименование товара",
            "Ед. изм.",
            "Количество",
            "Цена, включая НДС 5%",
            "Стоимость, включая НДС 5%",
        ]
        header_row = 6
        for col, header in enumerate(headers, start=1):
            cell = ws.cell(row=header_row, column=col, value=header)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
            cell.fill = PatternFill("solid", fgColor="D9E1F2")
            cell.border = _thin_border()

        total_price = 0.0
        for idx, result in enumerate(results, start=1):
            row = header_row + idx
            qty = result.tz_item.quantity
            unit_price = _money(result.unit_price)
            line_total = _money(result.total_price)
            total_price += line_total

            name = result.matched_name or result.tz_item.name
            if result.status == MatchStatus.SIMILAR:
                name = f"{name} *"
            elif result.status == MatchStatus.NOT_FOUND:
                name = f"{result.tz_item.name} (не подобрано)"

            values = [
                idx,
                name,
                result.tz_item.unit,
                qty,
                unit_price if unit_price else "—",
                line_total if line_total else "—",
            ]
            for col, value in enumerate(values, start=1):
                cell = ws.cell(row=row, column=col, value=value)
                cell.border = _thin_border()
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                if col in (5, 6) and isinstance(value, (int, float)):
                    cell.number_format = '#,##0.00'

        total_row = header_row + len(results) + 1
        ws.cell(row=total_row, column=5, value="Всего:").font = Font(bold=True)
        total_cell = ws.cell(row=total_row, column=6, value=_money(total_price))
        total_cell.font = Font(bold=True)
        total_cell.number_format = '#,##0.00'

        note_row = total_row + 2
        ws.merge_cells(f"A{note_row}:F{note_row}")
        ws[f"A{note_row}"] = "* — позиции, требующие проверки менеджером"

        terms_row = note_row + 2
        ws[f"A{terms_row}"] = f"Условия оплаты: {PAYMENT_TERMS}"
        ws[f"A{terms_row + 1}"] = f"Срок поставки: {DELIVERY_DAYS}"
        ws[f"A{terms_row + 2}"] = f"Доставка: {DELIVERY_TERMS}"

        widths = [8, 55, 10, 12, 22, 22]
        for idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = width

    def _build_detail_sheet(
        self, ws, results: list[MatchResult], summary: ProposalSummary
    ) -> None:
        headers = [
            "№",
            "Запрос заказчика",
            "Найденная позиция",
            "Статус",
            "Источник",
            "Совпадение %",
            "Кол-во",
            "Себест. ед.",
            "Цена баз.",
            f"Наценка {get_markup_percent()}%",
            "Цена ед.",
            "Сумма",
            "Примечание",
            "Детали источника",
            "Альтернативы",
        ]

        for col, header in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9E1F2")
            cell.border = _thin_border()
            cell.alignment = Alignment(wrap_text=True, horizontal="center")

        for idx, result in enumerate(results, start=1):
            row = idx + 1
            markup_amount = None
            if result.unit_base_price is not None and result.unit_price is not None:
                markup_amount = _money(result.unit_price - result.unit_base_price)

            values = [
                result.tz_item.number,
                result.tz_item.name,
                result.matched_name or "—",
                STATUS_LABELS[result.status],
                SOURCE_LABELS.get(result.source.value, result.source.value),
                round(result.match_score, 1),
                result.tz_item.quantity,
                result.unit_cost,
                result.unit_base_price,
                markup_amount,
                result.unit_price,
                result.total_price,
                result.notes,
                result.source_detail,
                "; ".join(result.alternatives[:3]),
            ]

            for col, value in enumerate(values, start=1):
                cell = ws.cell(row=row, column=col, value=value)
                cell.border = _thin_border()
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                if col in (8, 9, 10, 11, 12) and isinstance(value, (int, float)):
                    cell.number_format = '#,##0.00'

            status_cell = ws.cell(row=row, column=4)
            status_cell.fill = PatternFill(
                "solid", fgColor=STATUS_COLORS[result.status]
            )

        for col in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(col)].width = 18
        ws.column_dimensions["B"].width = 35
        ws.column_dimensions["C"].width = 35
        ws.column_dimensions["M"].width = 40

    def _build_summary_sheet(
        self, ws, results: list[MatchResult], summary: ProposalSummary
    ) -> None:
        ws["A1"] = "Сводка обработки ТЗ"
        ws["A1"].font = Font(bold=True, size=13)

        rows = [
            ("Всего позиций в ТЗ", summary.total_items),
            ("Точных совпадений", summary.exact_count),
            ("Похожих (требуют проверки)", summary.similar_count),
            ("Не найдено", summary.not_found_count),
            ("", ""),
            ("Итого себестоимость", _money(summary.total_cost)),
            ("Итого цена без наценки", _money(summary.total_base_price)),
            (f"Наценка {get_markup_percent()}%", ""),
            ("Итого цена КП", _money(summary.total_price)),
            ("", ""),
            ("Время обработки, сек", round(summary.processing_seconds, 2)),
        ]

        for idx, (label, value) in enumerate(rows, start=3):
            ws.cell(row=idx, column=1, value=label)
            cell = ws.cell(row=idx, column=2, value=value)
            if isinstance(value, float) and label.startswith("Итого"):
                cell.number_format = '#,##0.00'

        ws.column_dimensions["A"].width = 35
        ws.column_dimensions["B"].width = 20

        ws["A15"] = "Позиции без подбора:"
        ws["A15"].font = Font(bold=True)
        not_found = [r for r in results if r.status == MatchStatus.NOT_FOUND]
        if not_found:
            for idx, result in enumerate(not_found, start=16):
                ws.cell(row=idx, column=1, value=f"{result.tz_item.number}. {result.tz_item.name}")
        else:
            ws["A16"] = "— все позиции подобраны"

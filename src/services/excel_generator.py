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
from src.services.kp_preferences import KpPreferences, competitor_link_urls
from src.services.markup_settings import get_markup_percent
from src.services.models import KitComponentLine, MatchResult, MatchStatus, ProposalSummary

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


def _write_hyperlink(ws, row: int, col: int, url: str | None) -> None:
    cell = ws.cell(row=row, column=col, value=url or "—")
    cell.border = _thin_border()
    cell.alignment = Alignment(wrap_text=True, vertical="top")
    if url:
        cell.hyperlink = url
        cell.font = Font(color="0563C1", underline="single")
        cell.value = url


def _specs_preview(text: str, limit: int = 500) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


class ExcelGenerator:
    def generate(
        self,
        results: list[MatchResult],
        summary: ProposalSummary,
        output_path: Path,
        request_number: str = "б/н",
        preferences: KpPreferences | None = None,
    ) -> Path:
        wb = Workbook()

        self._build_kp_sheet(wb.active, results, request_number, preferences)
        self._build_detail_sheet(
            wb.create_sheet("Детализация"), results, summary, preferences
        )
        self._build_kit_sheet(wb.create_sheet("Состав комплектов"), results)
        self._build_comparison_sheet(wb.create_sheet("Сравнение"), results)
        self._build_summary_sheet(wb.create_sheet("Сводка"), results, summary)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(output_path)
        return output_path

    def _build_kp_sheet(
        self,
        ws,
        results: list[MatchResult],
        request_number: str,
        preferences: KpPreferences | None = None,
    ) -> None:
        ws.title = "КП"
        today = datetime.now().strftime("%d.%m.%Y")
        markup = get_markup_percent()
        last_col = 10

        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
        ws["A1"] = "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ"
        ws["A1"].font = Font(bold=True, size=14)
        ws["A1"].alignment = Alignment(horizontal="center")

        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
        ws["A2"] = f"на запрос {request_number} от {today} г."
        ws["A2"].alignment = Alignment(horizontal="center")

        ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=last_col)
        ws["A3"] = (
            f"{COMPANY_NAME}. ИНН {COMPANY_INN}. КПП {COMPANY_KPP}. ОГРН {COMPANY_OGRN}"
        )

        ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=last_col)
        ws["A4"] = COMPANY_ADDRESS

        headers = [
            "№",
            "Наименование товара",
            "Характеристики (из ТЗ)",
            "Ед. изм.",
            "Количество",
            f"Цена (+{markup}%)",
            "Стоимость",
            "Ссылка 1",
            "Ссылка 2",
            "Ссылка 3",
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

            name = result.tz_item.name
            if result.status == MatchStatus.SIMILAR:
                name = f"{name} *"
            elif result.status == MatchStatus.NOT_FOUND:
                name = f"{name} (не подобрано)"

            values = [
                result.tz_item.number,
                name,
                _specs_preview(result.tz_item.specifications),
                result.tz_item.unit,
                qty,
                unit_price if unit_price else "—",
                line_total if line_total else "—",
            ]
            for col, value in enumerate(values, start=1):
                cell = ws.cell(row=row, column=col, value=value)
                cell.border = _thin_border()
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                if col in (6, 7) and isinstance(value, (int, float)):
                    cell.number_format = '#,##0.00'

            links = competitor_link_urls(
                result.comparison,
                result.tz_item.name,
                preferences,
                limit=3,
            )
            for offset, url in enumerate(links):
                _write_hyperlink(ws, row, 8 + offset, url)
            for col in range(8 + len(links), 11):
                cell = ws.cell(row=row, column=col, value="—")
                cell.border = _thin_border()

        total_row = header_row + len(results) + 1
        ws.cell(row=total_row, column=6, value="Всего:").font = Font(bold=True)
        total_cell = ws.cell(row=total_row, column=7, value=_money(total_price))
        total_cell.font = Font(bold=True)
        total_cell.number_format = '#,##0.00'

        note_row = total_row + 2
        ws.merge_cells(
            start_row=note_row,
            start_column=1,
            end_row=note_row,
            end_column=last_col,
        )
        ws[f"A{note_row}"] = "* — позиции, требующие проверки менеджером"

        terms_row = note_row + 2
        ws[f"A{terms_row}"] = f"Условия оплаты: {PAYMENT_TERMS}"
        ws[f"A{terms_row + 1}"] = f"Срок поставки: {DELIVERY_DAYS}"
        ws[f"A{terms_row + 2}"] = f"Доставка: {DELIVERY_TERMS}"

        widths = [6, 36, 42, 10, 12, 16, 16, 28, 28, 28]
        for col_idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(col_idx)].width = width

    def _build_detail_sheet(
        self,
        ws,
        results: list[MatchResult],
        summary: ProposalSummary,
        preferences: KpPreferences | None = None,
    ) -> None:
        headers = [
            "№",
            "Тип",
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
            "Поставщик",
            "Дата покупки",
            "Примечание",
            "Детали источника",
            "Альтернативы",
            "Ссылка конкурента",
        ]

        for col, header in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9E1F2")
            cell.border = _thin_border()
            cell.alignment = Alignment(wrap_text=True, horizontal="center")

        row = 2
        for result in results:
            row = self._write_detail_row(
                ws, row, result, row_type="Позиция ТЗ", preferences=preferences
            )
            if result.is_kit and result.kit_components:
                for comp_idx, component in enumerate(result.kit_components, start=1):
                    row = self._write_kit_component_row(
                        ws,
                        row,
                        result,
                        component,
                        comp_idx,
                        preferences=preferences,
                    )

        for col in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(col)].width = 18
        ws.column_dimensions["C"].width = 35
        ws.column_dimensions["D"].width = 35
        ws.column_dimensions["P"].width = 40
        ws.column_dimensions["S"].width = 36

    def _write_detail_row(
        self,
        ws,
        row: int,
        result: MatchResult,
        row_type: str,
        component: KitComponentLine | None = None,
        component_index: int | None = None,
        preferences: KpPreferences | None = None,
    ) -> int:
        markup_amount = None
        if result.unit_base_price is not None and result.unit_price is not None:
            markup_amount = _money(result.unit_price - result.unit_base_price)

        number = result.tz_item.number
        if component_index is not None:
            number = f"{result.tz_item.number}.{component_index}"

        name = result.tz_item.name
        matched_name = result.matched_name or "—"
        unit_cost = result.unit_cost
        unit_base_price = result.unit_base_price
        unit_price = result.unit_price
        total_price = result.total_price
        supplier = result.supplier
        purchase_date = result.purchase_date
        notes = result.notes
        quantity = result.tz_item.quantity
        competitor_url: str | None = None

        if component is not None:
            name = f"  ↳ {component.name}"
            matched_name = (
                component.catalog_matched_name
                if component.found_in_catalog and component.catalog_matched_name
                else component.name
            )
            unit_cost = component.unit_cost
            unit_base_price = component.unit_price
            unit_price = None
            total_price = (
                round(component.unit_price * component.quantity, 2)
                if component.unit_price is not None
                else None
            )
            supplier = component.supplier if component.found_in_catalog else None
            purchase_date = (
                component.purchase_date if component.found_in_catalog else None
            )
            notes_parts: list[str] = []
            if component.found_in_catalog:
                notes_parts.append("найдено в каталоге")
            elif component.price_list_price is not None:
                notes_parts.append(f"прайс: {component.price_list_price}")
            notes = " | ".join(notes_parts)
            quantity = component.quantity
            competitor_url = component.competitor_url
            if component.competitor_platform:
                notes = (
                    f"{notes} | {component.competitor_platform}".strip(" |")
                    if notes
                    else component.competitor_platform
                )
        else:
            links = competitor_link_urls(
                result.comparison,
                result.tz_item.name,
                preferences,
                limit=1,
            )
            competitor_url = links[0] if links else None

        values = [
            number,
            row_type,
            name,
            matched_name,
            STATUS_LABELS[result.status] if component is None else "Состав комплекта",
            SOURCE_LABELS.get(result.source.value, result.source.value),
            round(result.match_score, 1) if component is None else None,
            quantity,
            unit_cost,
            unit_base_price,
            markup_amount if component is None else None,
            unit_price,
            total_price,
            supplier,
            purchase_date,
            notes,
            result.source_detail if component is None else "",
            "; ".join(result.alternatives[:3]) if component is None else "",
            competitor_url or "—",
        ]

        link_col = len(values)
        for col, value in enumerate(values[:-1], start=1):
            cell = ws.cell(row=row, column=col, value=value)
            cell.border = _thin_border()
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if col in (9, 10, 11, 12, 13) and isinstance(value, (int, float)):
                cell.number_format = '#,##0.00'
            if component is not None:
                cell.fill = PatternFill("solid", fgColor="F3F4F6")

        if competitor_url:
            _write_hyperlink(ws, row, link_col, competitor_url)
        else:
            cell = ws.cell(row=row, column=link_col, value="—")
            cell.border = _thin_border()
            if component is not None:
                cell.fill = PatternFill("solid", fgColor="F3F4F6")

        if component is None:
            status_cell = ws.cell(row=row, column=5)
            status_cell.fill = PatternFill(
                "solid", fgColor=STATUS_COLORS[result.status]
            )

        return row + 1

    def _write_kit_component_row(
        self,
        ws,
        row: int,
        result: MatchResult,
        component: KitComponentLine,
        component_index: int,
        preferences: KpPreferences | None = None,
    ) -> int:
        return self._write_detail_row(
            ws,
            row,
            result,
            row_type="Составляющая",
            component=component,
            component_index=component_index,
            preferences=preferences,
        )

    def _build_kit_sheet(self, ws, results: list[MatchResult]) -> None:
        headers = [
            "№ ТЗ",
            "Комплект (позиция ТЗ)",
            "№ в составе",
            "Составляющая",
            "Кол-во",
            "Себест. ед.",
            "Цена ед.",
            "Сумма",
            "Поставщик",
            "Дата покупки",
            "Цена в прайсе",
            "Итого комплект (база)",
            "Итого комплект (КП)",
        ]

        for col, header in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9E1F2")
            cell.border = _thin_border()
            cell.alignment = Alignment(wrap_text=True, horizontal="center")

        row = 2
        kit_results = [r for r in results if r.is_kit and r.kit_components]
        if not kit_results:
            ws.cell(row=2, column=1, value="— комплекты с составом не найдены")
            return

        for result in kit_results:
            kit_base = result.unit_base_price
            kit_kp = result.unit_price
            first_component_row = row

            block_start = row
            for comp_idx, component in enumerate(result.kit_components, start=1):
                line_sum = (
                    round(component.unit_price * component.quantity, 2)
                    if component.unit_price is not None
                    else None
                )
                comp_supplier = component.supplier if component.found_in_catalog else None
                comp_purchase_date = (
                    component.purchase_date if component.found_in_catalog else None
                )
                display_name = component.name
                if component.found_in_catalog and component.catalog_matched_name:
                    display_name = (
                        f"{component.name} (каталог: {component.catalog_matched_name})"
                    )
                values = [
                    result.tz_item.number if comp_idx == 1 else "",
                    result.tz_item.name if comp_idx == 1 else "",
                    comp_idx,
                    display_name,
                    component.quantity,
                    component.unit_cost,
                    component.unit_price,
                    line_sum,
                    comp_supplier or "",
                    comp_purchase_date or "",
                    component.price_list_price,
                    kit_base if comp_idx == 1 else "",
                    kit_kp if comp_idx == 1 else "",
                ]
                for col, value in enumerate(values, start=1):
                    cell = ws.cell(row=row, column=col, value=value)
                    cell.border = _thin_border()
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                    if col in (6, 7, 8, 11, 12, 13) and isinstance(value, (int, float)):
                        cell.number_format = '#,##0.00'
                row += 1

                if component.found_in_catalog and component.supplier:
                    supplier_values = [
                        "",
                        "",
                        "",
                        f"  ↳ {component.supplier}",
                        component.quantity,
                        "",
                        component.unit_price,
                        line_sum,
                        component.supplier,
                        component.purchase_date,
                        component.price_list_price,
                        "",
                        "",
                    ]
                    for col, value in enumerate(supplier_values, start=1):
                        cell = ws.cell(row=row, column=col, value=value)
                        cell.border = _thin_border()
                        cell.alignment = Alignment(wrap_text=True, vertical="top")
                        cell.fill = PatternFill("solid", fgColor="F3F4F6")
                        if col in (7, 8, 11) and isinstance(value, (int, float)):
                            cell.number_format = '#,##0.00'
                    row += 1

            if row - 1 > block_start:
                for merge_col in (1, 2, 12, 13):
                    ws.merge_cells(
                        start_row=block_start,
                        start_column=merge_col,
                        end_row=row - 1,
                        end_column=merge_col,
                    )
                    ws.cell(row=block_start, column=merge_col).alignment = Alignment(
                        vertical="top", wrap_text=True
                    )

            row += 1

        ws.column_dimensions["B"].width = 36
        ws.column_dimensions["D"].width = 42
        for col_letter in ("F", "G", "H", "K", "L", "M"):
            ws.column_dimensions[col_letter].width = 14

    def _build_comparison_sheet(self, ws, results: list[MatchResult]) -> None:
        headers = [
            "№ ТЗ",
            "Позиция ТЗ",
            "Источник",
            "Найдено",
            "Себест.",
            "Цена",
            "Поставщик",
            "Дата покупки",
            "Совпадение %",
            "Примечание",
        ]

        for col, header in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9E1F2")
            cell.border = _thin_border()

        row = 2
        for result in results:
            quotes = list(result.comparison)
            if not quotes and not result.kit_components:
                continue

            for quote in quotes:
                values = [
                    result.tz_item.number,
                    result.tz_item.name,
                    quote.label,
                    quote.matched_name,
                    quote.cost,
                    quote.price,
                    quote.supplier,
                    quote.purchase_date,
                    round(quote.match_score, 1) if quote.match_score else None,
                    quote.notes,
                ]
                for col, value in enumerate(values, start=1):
                    cell = ws.cell(row=row, column=col, value=value)
                    cell.border = _thin_border()
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                    if col in (5, 6) and isinstance(value, (int, float)):
                        cell.number_format = '#,##0.00'
                row += 1

            for component in result.kit_components:
                values = [
                    result.tz_item.number,
                    result.tz_item.name,
                    "Состав комплекта",
                    component.name,
                    component.unit_cost,
                    component.unit_price,
                    component.supplier if component.found_in_catalog else None,
                    component.purchase_date if component.found_in_catalog else None,
                    component.quantity,
                    (
                        "каталог"
                        if component.found_in_catalog
                        else (
                            f"прайс: {component.price_list_price}"
                            if component.price_list_price is not None
                            else ""
                        )
                    ),
                ]
                for col, value in enumerate(values, start=1):
                    cell = ws.cell(row=row, column=col, value=value)
                    cell.border = _thin_border()
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                    if col in (5, 6) and isinstance(value, (int, float)):
                        cell.number_format = '#,##0.00'
                row += 1

        for col in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(col)].width = 16
        ws.column_dimensions["B"].width = 32
        ws.column_dimensions["D"].width = 36

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

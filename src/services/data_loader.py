from __future__ import annotations

import logging
import re
from pathlib import Path

import xlrd
from openpyxl import load_workbook

from src.config import REGISTRY_PHOTOS_DIR
from src.services.models import CatalogItem, PriceListItem, RegistryItem

logger = logging.getLogger(__name__)

REGISTRY_PHOTO_COLUMNS = range(4, 7)


def _normalize_name(value: str) -> str:
    text = value.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[«»\"']", "", text)
    return text


def load_catalog(path: Path) -> list[CatalogItem]:
    if not path.exists():
        return []

    items: list[CatalogItem] = []
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]

    for row in ws.iter_rows(min_row=2, values_only=True):
        name = str(row[0]).strip() if row[0] else ""
        if not name:
            continue

        cost = row[3] if isinstance(row[3], (int, float)) else None
        sale_price = row[1] if isinstance(row[1], (int, float)) else None
        unit = str(row[4]).strip() if row[4] else "шт"
        stock = row[2] if isinstance(row[2], (int, float)) else None

        items.append(
            CatalogItem(
                name=name,
                cost=float(cost) if cost is not None else None,
                price=float(sale_price) if sale_price is not None else None,
                unit=unit or "шт",
                stock=float(stock) if stock is not None else None,
                source_file=path.name,
            )
        )

    wb.close()
    return items


def _extract_registry_photos(path: Path, photos_dir: Path) -> None:
    photos_dir.mkdir(parents=True, exist_ok=True)
    marker = photos_dir / ".extracted"

    if marker.exists() and marker.stat().st_mtime >= path.stat().st_mtime:
        return

    for cached in photos_dir.iterdir():
        if cached.name != ".extracted":
            cached.unlink()

    wb = load_workbook(path, read_only=False)
    ws = wb[wb.sheetnames[0]]
    row_images: dict[int, list[tuple[int, object]]] = {}

    for image in ws._images:
        anchor = image.anchor
        if not hasattr(anchor, "_from"):
            continue
        row_idx = anchor._from.row
        col_idx = anchor._from.col
        if col_idx in REGISTRY_PHOTO_COLUMNS:
            row_images.setdefault(row_idx, []).append((col_idx, image))

    extracted = 0
    for row_idx, images in row_images.items():
        item_idx = row_idx - 1
        if item_idx < 0:
            continue
        for photo_num, (_, image) in enumerate(sorted(images, key=lambda item: item[0])):
            suffix = Path(image.path or "photo.png").suffix or ".png"
            filename = (
                f"{item_idx:04d}{suffix.lower()}"
                if photo_num == 0
                else f"{item_idx:04d}_{photo_num + 1}{suffix.lower()}"
            )
            (photos_dir / filename).write_bytes(image._data())
            extracted += 1

    marker.touch()
    wb.close()
    logger.info("Извлечено фото из реестра: %s", extracted)


def _resolve_registry_photo_files(photos_dir: Path, item_idx: int) -> list[str]:
    files: list[str] = []
    prefixes = [f"{item_idx:04d}"] + [f"{item_idx:04d}_{num}" for num in range(2, 10)]
    for prefix in prefixes:
        found: str | None = None
        for suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            candidate = f"{prefix}{suffix}"
            if (photos_dir / candidate).is_file():
                found = candidate
                break
        if not found:
            break
        files.append(found)
    return files


def load_registry(path: Path, photos_dir: Path | None = None) -> list[RegistryItem]:
    if not path.exists():
        return []

    cache_dir = photos_dir or REGISTRY_PHOTOS_DIR
    _extract_registry_photos(path, cache_dir)

    items: list[RegistryItem] = []
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]

    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True)):
        if not row:
            continue
        name = str(row[0]).strip() if row[0] else ""
        if not name:
            continue

        quantity = row[1] if len(row) > 1 and isinstance(row[1], (int, float)) else 0.0
        condition = str(row[3]).strip() if len(row) > 3 and row[3] else None
        link = str(row[7]).strip() if len(row) > 7 and row[7] else None
        photo_files = _resolve_registry_photo_files(cache_dir, idx)

        items.append(
            RegistryItem(
                name=name,
                quantity=float(quantity),
                condition=condition if condition and condition != "None" else None,
                link=link if link and link != "None" else None,
                photo_files=photo_files,
            )
        )

    wb.close()
    return items


def _optional_number(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _parse_price_row(row: list, sheet_name: str, supplier_name: str) -> PriceListItem | None:
    if len(row) < 4:
        return None

    code = str(row[0]).strip() if row[0] else ""
    name = str(row[1]).strip() if row[1] else ""
    price = row[3] if isinstance(row[3], (int, float)) else None
    recommended_qty = _optional_number(row[2]) if len(row) > 2 else None
    order_qty = _optional_number(row[4]) if len(row) > 4 else None
    order_sum = _optional_number(row[5]) if len(row) > 5 else None

    if not name or price is None or price <= 0:
        return None
    if not code or len(code) > 8:
        return None

    return PriceListItem(
        code=code,
        name=name,
        price=float(price),
        sheet=sheet_name,
        supplier=supplier_name,
        recommended_qty=recommended_qty,
        order_qty=order_qty,
        order_sum=order_sum,
    )


def _load_price_list_xls(path: Path, supplier_name: str) -> list[PriceListItem]:
    items: list[PriceListItem] = []
    wb = xlrd.open_workbook(str(path))
    skip_sheets = {"Лист1", "ПРОСМОТР ЗАКАЗА"}

    for sheet_name in wb.sheet_names():
        if sheet_name in skip_sheets:
            continue

        ws = wb.sheet_by_name(sheet_name)
        for row_idx in range(ws.nrows):
            item = _parse_price_row(ws.row_values(row_idx), sheet_name, supplier_name)
            if item:
                items.append(item)

    return items


def _load_price_list_xlsx(path: Path, supplier_name: str) -> list[PriceListItem]:
    items: list[PriceListItem] = []
    wb = load_workbook(path, read_only=True, data_only=True)
    skip_sheets = {"Лист1", "ПРОСМОТР ЗАКАЗА"}

    for sheet_name in wb.sheetnames:
        if sheet_name in skip_sheets:
            continue

        ws = wb[sheet_name]
        for row in ws.iter_rows(values_only=True):
            item = _parse_price_row(list(row), sheet_name, supplier_name)
            if item:
                items.append(item)

    wb.close()
    return items


def load_price_list(path: Path, supplier: str | None = None) -> list[PriceListItem]:
    if not path.exists():
        return []

    supplier_name = supplier or path.stem
    suffix = path.suffix.lower()

    if suffix == ".xls":
        return _load_price_list_xls(path, supplier_name)
    if suffix == ".xlsx":
        return _load_price_list_xlsx(path, supplier_name)

    raise ValueError(f"Неподдерживаемый формат прайса: {suffix}")


def parse_tz_docx(path: Path) -> list:
    from src.services.tz_parser import parse_tz

    return parse_tz(path)


def normalize_name(value: str) -> str:
    return _normalize_name(value)

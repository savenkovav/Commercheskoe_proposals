from __future__ import annotations

import logging
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.services.app_state import get_processor, reload_processor
from src.config import (
    OUTPUT_DIR,
    PROJECT_ROOT,
    REGISTRY_PHOTOS_DIR,
    USE_AI_INTERNET_SEARCH,
    WEB_BEHIND_PROXY,
    WEB_HOST,
    WEB_PORT,
)
from src.services.kp_chat_service import KpChatService
from src.services.kp_preferences import KpPreferences
from src.services.markup_settings import get_markup_percent, set_markup_percent
from src.services.models import KitComponentLine, MatchResult, MatchSource, MatchStatus, PriceQuote
from src.services.web_quote_priority import pick_internet_url
from src.services.tz_parser import resolve_tz_upload_filename
from src.services.price_list_manager import get_price_list_manager
from src.services.product_lookup import (
    LookupField,
    ProductLookupResult,
    ProductLookupService,
    get_field_labels,
    parse_lookup_query,
)
from src.services.static_source_manager import (
    StaticSourceManager,
    get_static_source_manager,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PRICE_EXTENSIONS = (".xls", ".xlsx")
DEMO_TZ_PATH = PROJECT_ROOT / "data" / "sample_tz.docx"

app = FastAPI(title="КП — коммерческие предложения", version="1.0.0")


class LookupRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class PriceMetaUpdate(BaseModel):
    name: str | None = None
    supplier: str | None = None


class MarkupUpdate(BaseModel):
    markup_percent: float = Field(ge=0, le=1000)


class KpChatRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


def _price_quote_to_dict(quote: PriceQuote) -> dict[str, Any]:
    return {
        "source": quote.source,
        "label": quote.label,
        "matched_name": quote.matched_name,
        "price": quote.price,
        "cost": quote.cost,
        "supplier": quote.supplier,
        "purchase_date": quote.purchase_date,
        "match_score": round(quote.match_score, 1),
        "url": quote.url,
        "notes": quote.notes,
    }


def _kit_component_to_dict(line: KitComponentLine) -> dict[str, Any]:
    return {
        "name": line.name,
        "unit_cost": line.unit_cost,
        "unit_price": line.unit_price,
        "quantity": line.quantity,
        "supplier": line.supplier,
        "purchase_date": line.purchase_date,
        "price_list_price": line.price_list_price,
        "competitor_url": line.competitor_url,
        "competitor_platform": line.competitor_platform,
        "found_in_catalog": line.found_in_catalog,
        "catalog_matched_name": line.catalog_matched_name,
    }


def _internet_url_from_result(result: MatchResult) -> str | None:
    web_quotes = [q for q in result.comparison if q.source == "web"]
    url = pick_internet_url(web_quotes, unit_base_price=result.unit_base_price)
    if url:
        return url
    detail = result.source_detail or ""
    match = re.search(r"https?://[^\s|]+", detail)
    if match:
        return match.group(0).rstrip("|")
    return None


def _match_result_to_dict(result: MatchResult) -> dict[str, Any]:
    return {
        "number": result.tz_item.number,
        "name": result.tz_item.name,
        "specifications": result.tz_item.specifications,
        "quantity": result.tz_item.quantity,
        "unit": result.tz_item.unit,
        "status": result.status.value,
        "source": result.source.value,
        "matched_name": result.matched_name,
        "match_score": round(result.match_score, 1),
        "unit_cost": result.unit_cost,
        "unit_base_price": result.unit_base_price,
        "unit_price": result.unit_price,
        "total_base_price": (
            round(result.unit_base_price * result.tz_item.quantity, 2)
            if result.unit_base_price is not None
            else None
        ),
        "total_cost": result.total_cost,
        "total_price": result.total_price,
        "notes": result.notes,
        "source_detail": result.source_detail,
        "alternatives": result.alternatives[:5],
        "supplier": result.supplier,
        "purchase_date": result.purchase_date,
        "is_kit": result.is_kit,
        "internet_priced": result.internet_priced,
        "internet_url": _internet_url_from_result(result),
        "comparison": [_price_quote_to_dict(q) for q in result.comparison],
        "competitors": [_price_quote_to_dict(q) for q in result.competitors],
        "kit_components": [_kit_component_to_dict(k) for k in result.kit_components],
        "price_list_check": (
            _price_quote_to_dict(result.price_list_check)
            if result.price_list_check
            else None
        ),
    }


def _summary_to_dict(summary, filename: str) -> dict[str, Any]:
    return {
        "total_items": summary.total_items,
        "exact_count": summary.exact_count,
        "similar_count": summary.similar_count,
        "not_found_count": summary.not_found_count,
        "total_cost": summary.total_cost,
        "total_base_price": summary.total_base_price,
        "total_price": summary.total_price,
        "processing_seconds": round(summary.processing_seconds, 1),
        "markup_percent": get_markup_percent(),
        "filename": filename,
        "download_url": None,
    }


def _attach_download_info(payload: dict[str, Any], output_path: Path | None) -> None:
    if (
        output_path
        and output_path.name.startswith("KP_")
        and output_path.suffix == ".xlsx"
        and output_path.exists()
    ):
        payload["has_download"] = True
        payload["summary"]["filename"] = output_path.name
        payload["summary"]["download_url"] = f"/api/files/{output_path.name}"
    else:
        payload["has_download"] = False
        payload["summary"]["download_url"] = None


def _safe_output_path(filename: str) -> Path:
    safe_name = Path(filename).name
    if not safe_name.startswith("KP_") or not safe_name.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Недопустимое имя файла")
    path = OUTPUT_DIR / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return path.resolve()


def _catalog_source(processor) -> dict[str, Any]:
    manager = get_static_source_manager()
    return manager.to_dict("catalog", len(processor.catalog))


def _registry_source(processor) -> dict[str, Any]:
    manager = get_static_source_manager()
    return manager.to_dict("registry", len(processor.registry))


def _static_source_response(source_id: str) -> dict[str, Any]:
    processor = get_processor()
    manager = get_static_source_manager()
    counts = {
        "catalog": len(processor.catalog),
        "registry": len(processor.registry),
    }
    return {"entry": manager.to_dict(source_id, counts[source_id])}


def _kp_chat_service() -> KpChatService:
    return KpChatService(get_processor())


def _parse_tz_path(tz_path: Path, use_ai: bool, *, filename: str = "") -> dict[str, Any]:
    processor = get_processor()
    start = time.perf_counter()
    tz_items = processor.parse_tz_file(tz_path)
    prefs = KpPreferences()
    results = processor.search_tz_items(
        tz_items,
        use_ai=use_ai,
        preferences=prefs,
        include_web=True,
    )
    summary = processor._build_summary(results, time.perf_counter() - start)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUT_DIR / f"KP_{timestamp}.xlsx"
    processor.excel.generate(results, summary, output_path)

    session_id = _kp_chat_service().create_session(
        tz_items,
        results,
        summary,
        output_path,
        use_ai=use_ai,
        tz_filename=filename or tz_path.name,
        parsed_only=False,
        auto_searched=True,
    )
    session = _kp_chat_service().store.get(session_id)
    welcome = session.chat_history[-1].text if session and session.chat_history else ""
    payload = {
        "session_id": session_id,
        "stage": "exported",
        "task_mode": "task1",
        "search_completed": True,
        "welcome_reply": welcome,
        "summary": _summary_to_dict(summary, output_path.name),
        "items": [_match_result_to_dict(r) for r in results],
        "ai_used": use_ai and processor.ai.enabled,
        "web_used": any(r.source == MatchSource.WEB for r in results),
    }
    _attach_download_info(payload, output_path)
    return payload


def _process_tz_path(
    tz_path: Path,
    use_ai: bool,
    *,
    parse_only: bool = True,
    filename: str = "",
) -> dict[str, Any]:
    if parse_only:
        return _parse_tz_path(tz_path, use_ai, filename=filename)

    processor = get_processor()
    output_path, summary, results, tz_items = processor.process_tz_file(
        tz_path,
        use_ai=use_ai,
    )
    session_id = _kp_chat_service().create_session(
        tz_items,
        results,
        summary,
        output_path,
        use_ai=use_ai,
        tz_filename=filename or tz_path.name,
        parsed_only=False,
    )
    payload = {
        "session_id": session_id,
        "stage": "searched",
        "task_mode": "task1_task2",
        "search_completed": True,
        "summary": _summary_to_dict(summary, output_path.name),
        "items": [_match_result_to_dict(r) for r in results],
        "ai_used": use_ai and processor.ai.enabled,
        "web_used": USE_AI_INTERNET_SEARCH and use_ai and processor.ai.enabled,
    }
    _attach_download_info(payload, output_path)
    return payload


def _lookup_result_to_dict(result: ProductLookupResult) -> dict[str, Any]:
    values = {
        get_field_labels()[field]: value
        for field, value in result.values.items()
    }
    status_label = {
        MatchStatus.EXACT: "exact",
        MatchStatus.SIMILAR: "similar",
        MatchStatus.NOT_FOUND: "not_found",
    }.get(result.status, "not_found")

    return {
        "query_name": result.query_name,
        "matched_name": result.matched_name,
        "match_score": round(result.match_score, 1),
        "status": status_label,
        "not_found": result.not_found,
        "values": values,
        "sources": result.sources,
        "alternatives": result.alternatives[:8],
        "available_fields": list(get_field_labels().values()),
        "catalog": result.catalog,
        "price_list": result.price_list,
        "registry": _serialize_registry_block(result.registry),
        "competitors": result.competitors,
        "ai_insight": result.ai_insight,
    }


def _kp_chat_response(chat_result: dict[str, Any], session_id: str) -> dict[str, Any]:
    processor = get_processor()
    summary = chat_result["summary"]
    results = chat_result["results"]
    output_path = chat_result["output_path"]
    filename = output_path.name if output_path.name != "pending.xlsx" else "pending.xlsx"
    payload = {
        "session_id": session_id,
        "reply": chat_result["reply"],
        "preferences": chat_result["preferences"],
        "markup_percent": chat_result["markup_percent"],
        "actions": chat_result["actions"],
        "stage": chat_result.get("stage", "intake"),
        "task_mode": chat_result.get("task_mode", "task1"),
        "search_completed": chat_result.get("search_completed", False),
        "summary": _summary_to_dict(summary, filename),
        "items": [_match_result_to_dict(r) for r in results],
        "ai_used": processor.ai.enabled,
        "web_used": processor.ai.enabled,
    }
    _attach_download_info(payload, output_path)
    lookup = chat_result.get("lookup")
    if isinstance(lookup, ProductLookupResult):
        payload["lookup"] = _lookup_result_to_dict(lookup)
    return payload


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    processor = get_processor()
    price_entries = processor.price_manager.list_entries()
    return {
        "catalog_count": len(processor.catalog),
        "registry_count": len(processor.registry),
        "price_items_count": len(processor.price_lists),
        "price_files_count": len(price_entries),
        "price_files": [
            {
                "id": entry.id,
                "name": entry.name,
                "supplier": entry.supplier,
                "items_count": entry.items_count,
                "updated_at": entry.updated_at,
            }
            for entry in price_entries
        ],
        "ai_enabled": processor.ai.enabled,
        "pii_enabled": processor.ai.anonymizer.enabled,
        "markup_percent": get_markup_percent(),
        "demo_available": DEMO_TZ_PATH.exists(),
        "catalog": _catalog_source(processor),
        "registry": _registry_source(processor),
    }


@app.get("/api/markup")
def api_get_markup() -> dict[str, float]:
    return {"markup_percent": get_markup_percent()}


@app.post("/api/markup")
def api_set_markup(body: MarkupUpdate) -> dict[str, float]:
    try:
        value = set_markup_percent(body.markup_percent)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"markup_percent": value}


@app.post("/api/process/demo")
def api_process_demo(use_ai: bool = True, parse_only: bool = True) -> dict[str, Any]:
    if not DEMO_TZ_PATH.exists():
        raise HTTPException(status_code=404, detail="Демо-файл data/sample_tz.docx не найден")
    try:
        return _process_tz_path(DEMO_TZ_PATH, use_ai=use_ai, parse_only=parse_only)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Demo processing failed")
        raise HTTPException(status_code=500, detail=f"Ошибка обработки: {exc}") from exc


@app.post("/api/kp/chat")
def api_kp_chat(body: KpChatRequest) -> dict[str, Any]:
    try:
        chat_result = _kp_chat_service().chat(body.session_id, body.message)
        return _kp_chat_response(chat_result, body.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("KP chat failed")
        raise HTTPException(status_code=500, detail=f"Ошибка чата: {exc}") from exc


@app.post("/api/process/upload")
async def api_process_upload(
    file: UploadFile = File(...),
    use_ai: bool = Form(default=True),
    parse_only: bool = Form(default=True),
) -> dict[str, Any]:
    try:
        content = await file.read()
        filename = resolve_tz_upload_filename(file.filename, content, file.content_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tz_path = Path(tmpdir) / filename
            tz_path.write_bytes(content)
            return _process_tz_path(
                tz_path,
                use_ai=use_ai,
                parse_only=parse_only,
                filename=filename,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Upload processing failed")
        raise HTTPException(status_code=500, detail=f"Ошибка обработки: {exc}") from exc


@app.get("/api/files/{filename}")
def api_download_file(filename: str) -> FileResponse:
    path = _safe_output_path(filename)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@app.post("/api/lookup")
def api_lookup(body: LookupRequest) -> dict[str, Any]:
    parsed = parse_lookup_query(body.query if body.query.startswith("/find") else f"/find {body.query}")
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="Не удалось распознать запрос. Пример: термометр лабораторный | цена, остаток",
        )

    processor = get_processor()
    lookup = ProductLookupService(
        processor.matcher,
        processor.ai,
        processor.tz_matcher.web_search,
    )
    result = lookup.lookup(parsed.product_name, parsed.requested_fields)
    return _lookup_result_to_dict(result)


def _serialize_registry_block(registry: dict[str, object]) -> dict[str, object]:
    payload = dict(registry)
    photo_files = payload.get("photo_files")
    if isinstance(photo_files, list):
        urls = ProductLookupService.registry_photo_urls(
            [str(name) for name in photo_files if isinstance(name, str)]
        )
        payload["photo_urls"] = urls
        payload["photo_url"] = urls[0] if urls else None
    else:
        payload["photo_urls"] = []
        payload["photo_url"] = None
    payload.pop("photo_files", None)

    items = payload.get("items")
    if isinstance(items, list):
        serialized_items: list[dict[str, object]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_payload = dict(item)
            item_photos = item_payload.pop("photo_files", [])
            if isinstance(item_photos, list):
                item_urls = ProductLookupService.registry_photo_urls(
                    [str(name) for name in item_photos if isinstance(name, str)]
                )
                item_payload["photo_urls"] = item_urls
                item_payload["photo_url"] = item_urls[0] if item_urls else None
            serialized_items.append(item_payload)
        payload["items"] = serialized_items

    return payload


def _safe_registry_photo_path(filename: str) -> Path:
    safe_name = Path(filename).name
    if not re.fullmatch(r"\d{4}(_\d+)?\.(png|jpe?g|gif|webp)", safe_name, flags=re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Недопустимое имя файла")
    path = REGISTRY_PHOTOS_DIR / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Фото не найдено")
    return path.resolve()


@app.get("/api/registry/photos/{filename}")
def api_registry_photo(filename: str) -> FileResponse:
    path = _safe_registry_photo_path(filename)
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = media_types.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)


@app.get("/api/prices")
def api_prices_list() -> dict[str, Any]:
    processor = get_processor()
    manager = get_price_list_manager()
    entries = manager.list_entries()
    return {
        "catalog": _catalog_source(processor),
        "registry": _registry_source(processor),
        "items": [
            {
                "id": entry.id,
                "type": "price_list",
                "type_label": "Прайс",
                "name": entry.name,
                "supplier": entry.supplier,
                "filename": entry.filename,
                "items_count": entry.items_count,
                "updated_at": entry.updated_at,
            }
            for entry in entries
        ],
    }


@app.post("/api/prices")
async def api_prices_add(
    name: str = Form(...),
    supplier: str = Form(default=""),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    filename = file.filename or "price.xls"
    if not filename.lower().endswith(PRICE_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Прайс должен быть .xls или .xlsx")

    manager = get_price_list_manager()
    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = Path(tmpdir) / filename
        source_path.write_bytes(await file.read())
        try:
            entry = manager.add(
                name=name.strip(),
                supplier=(supplier or name).strip(),
                source_path=source_path,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    total_items = reload_processor()
    return {
        "entry": {
            "id": entry.id,
            "name": entry.name,
            "supplier": entry.supplier,
            "items_count": entry.items_count,
            "updated_at": entry.updated_at,
        },
        "total_price_items": total_items,
    }


@app.put("/api/prices/{price_id}/file")
async def api_prices_replace(price_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    filename = file.filename or "data.xlsx"
    static_manager = get_static_source_manager()

    if StaticSourceManager.is_static_source(price_id):
        if not filename.lower().endswith(".xlsx"):
            raise HTTPException(status_code=400, detail="Каталог и реестр должны быть .xlsx")

        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / filename
            source_path.write_bytes(await file.read())
            try:
                static_manager.replace_file(price_id, source_path)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        reload_processor()
        return _static_source_response(price_id.lower())

    if not filename.lower().endswith(PRICE_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Прайс должен быть .xls или .xlsx")

    manager = get_price_list_manager()
    if not manager.get_entry(price_id):
        raise HTTPException(status_code=404, detail=f"Прайс {price_id} не найден")

    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = Path(tmpdir) / filename
        source_path.write_bytes(await file.read())
        try:
            entry = manager.replace(price_id, source_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    total_items = reload_processor()
    return {
        "entry": {
            "id": entry.id,
            "name": entry.name,
            "supplier": entry.supplier,
            "items_count": entry.items_count,
            "updated_at": entry.updated_at,
        },
        "total_price_items": total_items,
    }


@app.patch("/api/prices/{price_id}")
def api_prices_rename(price_id: str, body: PriceMetaUpdate) -> dict[str, Any]:
    static_manager = get_static_source_manager()
    if StaticSourceManager.is_static_source(price_id):
        if not body.name:
            raise HTTPException(status_code=400, detail="Укажите название")
        try:
            static_manager.update_name(price_id, body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _static_source_response(price_id.lower())

    manager = get_price_list_manager()
    try:
        entry = manager.update_meta(
            price_id,
            name=body.name,
            supplier=body.supplier,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    reload_processor()
    return {
        "entry": {
            "id": entry.id,
            "name": entry.name,
            "supplier": entry.supplier,
            "items_count": entry.items_count,
            "updated_at": entry.updated_at,
        }
    }


@app.delete("/api/prices/{price_id}")
def api_prices_remove(price_id: str) -> dict[str, Any]:
    static_manager = get_static_source_manager()
    if StaticSourceManager.is_static_source(price_id):
        try:
            config = static_manager.remove(price_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        reload_processor()
        return {
            "removed_id": config.source_id,
            "removed_name": static_manager.get_display_name(config.source_id),
        }

    manager = get_price_list_manager()
    try:
        entry = manager.remove(price_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    total_items = reload_processor()
    return {
        "removed_id": entry.id,
        "removed_name": entry.name,
        "total_price_items": total_items,
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logger.info("Веб-интерфейс: http://%s:%s", WEB_HOST, WEB_PORT)
    uvicorn.run(
        "src.web.server:app",
        host=WEB_HOST,
        port=WEB_PORT,
        reload=False,
        proxy_headers=WEB_BEHIND_PROXY,
        forwarded_allow_ips="*" if WEB_BEHIND_PROXY else None,
    )


if __name__ == "__main__":
    main()

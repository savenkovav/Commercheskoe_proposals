from __future__ import annotations

import logging
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.logging_config import setup_logging
from src.services.app_state import get_processor, reload_processor
from src.config import (
    AUTH_ENABLED,
    CATALOG_PATH,
    GOODS_REPORT_PATH,
    OUTPUT_DIR,
    PROCUREMENT_REPORT_PATH,
    PROJECT_ROOT,
    REGISTRY_PATH,
    REGISTRY_PHOTOS_DIR,
    STOCK_BALANCE_PATH,
    USE_AI_INTERNET_SEARCH,
    WEB_BEHIND_PROXY,
    WEB_HOST,
    WEB_PORT,
)
from src.services.auth_service import AUTH_COOKIE_NAME, ensure_default_admin, resolve_user_by_token
from src.services.user_db import get_user_database
from src.web.auth_routes import admin_router, auth_router, history_router
from src.services.kp_chat_service import KpChatService, WELCOME_MESSAGE
from src.services.kp_preferences import KpPreferences
from src.services.markup_settings import get_markup_percent, set_markup_percent
from src.services.pricing_rules import effective_markup_percent, format_markup_percent, is_internet_sourced_result
from src.services.meilisearch_service import meilisearch_health
from src.services.models import KitComponentLine, MatchResult, MatchSource, MatchStatus, PriceQuote
from src.services.web_quote_priority import pick_marketplace_search_urls, resolve_price_source_url
from src.services.tz_parser import resolve_tz_upload_filename
from src.services.tz_parser import extract_tz_document_text
from src.services.tz_rag_service import RagIndex, TZRagService
from src.services.price_list_manager import get_price_list_manager
from src.services.product_lookup import (
    LookupField,
    ProductLookupResult,
    ProductLookupService,
    get_field_labels,
    parse_lookup_query,
    resolve_freeform_product_lookup,
)
from src.services.static_source_manager import (
    StaticSourceManager,
    get_static_source_manager,
)
from src.services.document_rag_index import get_document_rag_index
from src.services.competitor_sites import COMPETITOR_SITES, competitor_sites_with_search
from src.services.competitor_site_manager import get_competitor_site_manager

logger = logging.getLogger(__name__)

setup_logging()

STATIC_DIR = Path(__file__).parent / "static"
PRICE_EXTENSIONS = (".xls", ".xlsx")
DEMO_TZ_PATH = PROJECT_ROOT / "data" / "sample_tz.docx"

app = FastAPI(title="КП — коммерческие предложения", version="1.0.0")
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(history_router)


@app.on_event("startup")
def _startup_auth() -> None:
    if AUTH_ENABLED:
        ensure_default_admin()


@app.on_event("startup")
def _startup_competitor_catalog_bootstrap() -> None:
    import threading

    def _run() -> None:
        try:
            from src.services.competitor_catalog_service import (
                _SITEMAP_CATALOG_MIN_PRODUCTS,
                reindex_all_competitor_sites,
                site_catalog_looks_complete,
                start_site_reindex_background,
            )
            from src.services.competitor_catalog_urls import get_competitor_catalog_url_registry
            from src.services.competitor_product_store import get_competitor_product_store
            from src.services.competitor_sites import competitor_sites_with_search

            store = get_competitor_product_store()
            index = _doc_rag_index_service()
            index.ensure_loaded()
            incomplete_domains = [
                site.domain
                for site in competitor_sites_with_search()
                if site.domain.lower().removeprefix("www.") in _SITEMAP_CATALOG_MIN_PRODUCTS
                and not site_catalog_looks_complete(
                    site.domain,
                    len(store.products_for_domain(site.domain)),
                )
            ]
            if (
                not incomplete_domains
                and store.stats()["products"] > 0
                and "competitor-catalog:all" in index._entries
            ):
                return

            registry = get_competitor_catalog_url_registry()
            registry.add_page(
                "https://skale.ru/magazin/folder/uchebnoe-oborudovanie-po-astronomii-i-astrofizike",
                domain="skale.ru",
                label="Скале",
                source="seed",
            )
            if incomplete_domains:
                for domain in incomplete_domains:
                    start_site_reindex_background(domain, index, force=True)
                return
            reindex_all_competitor_sites(_doc_rag_index_service(), force=False)
        except Exception:
            logger.exception("Competitor catalog startup bootstrap failed")

    threading.Thread(
        target=_run,
        daemon=True,
        name="competitor-catalog-bootstrap",
    ).start()


@app.middleware("http")
async def log_http_requests(request, call_next):
    if request.url.path.startswith("/app.") or request.url.path.endswith(
        (".css", ".js", ".svg", ".ico")
    ):
        return await call_next(request)

    started = time.perf_counter()
    logger.info("→ %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "← %s %s failed after %.0fms",
            request.method,
            request.url.path,
            (time.perf_counter() - started) * 1000,
        )
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "← %s %s %s %.0fms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


class LookupRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class PriceMetaUpdate(BaseModel):
    name: str | None = None
    supplier: str | None = None


class MarkupUpdate(BaseModel):
    markup_percent: float = Field(ge=0, le=1000)


class KpChatRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class KpSessionCreateRequest(BaseModel):
    use_ai: bool = True


class KpSelectionItemRequest(BaseModel):
    number: int = Field(ge=1)
    included: bool = True
    variant: str = Field(default="primary", max_length=64)
    kit_indices: list[int] | None = None
    web_indices: list[int] | None = None


class KpFormRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    selections: list[KpSelectionItemRequest] = Field(default_factory=list)


class RagQueryRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)


class RagSourceQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    source_type: str | None = Field(default=None, max_length=32)


class CompetitorSiteAnalyzeRequest(BaseModel):
    url: str = Field(min_length=4, max_length=500)
    label: str = Field(default="", max_length=200)


class CompetitorSiteIndexRequest(BaseModel):
    url: str = Field(min_length=4, max_length=500)
    label: str = Field(default="", max_length=200)
    product_sample_url: str | None = Field(default=None, max_length=800)
    price_html_hint: str | None = Field(default=None, max_length=8000)
    articul_html_hint: str | None = Field(default=None, max_length=8000)


class CompetitorSiteAddRequest(BaseModel):
    url: str = Field(min_length=4, max_length=500)
    label: str = Field(default="", max_length=200)
    product_sample_url: str | None = Field(default=None, max_length=800)
    price_html_hint: str | None = Field(default=None, max_length=8000)
    articul_html_hint: str | None = Field(default=None, max_length=8000)


class CompetitorSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=30)


class CompetitorReindexRequest(BaseModel):
    force: bool = False
    domains: list[str] | None = None
    background: bool = True


class CompetitorPageIndexRequest(BaseModel):
    url: str = Field(min_length=4, max_length=800)
    label: str = Field(default="", max_length=200)


class CompetitorEnrichImagesRequest(BaseModel):
    domain: str = Field(default="vrtorg.ru", min_length=3, max_length=200)
    label: str = Field(default="", max_length=200)
    limit: int | None = Field(default=None, ge=1, le=5000)

def _price_quote_to_dict(quote: PriceQuote) -> dict[str, Any]:
    return {
        "source": quote.source,
        "label": quote.label,
        "matched_name": quote.matched_name,
        "price": quote.price,
        "cost": quote.cost,
        "price_label": quote.price_label,
        "wholesale_price": quote.wholesale_price,
        "articul": quote.articul,
        "supplier": quote.supplier,
        "purchase_date": quote.purchase_date,
        "match_score": round(quote.match_score, 1),
        "url": quote.url,
        "notes": quote.notes,
        "image_url": quote.image_url,
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
    if not is_internet_sourced_result(result):
        return None
    preferred = None
    if result.unit_base_price is not None:
        for quote in result.comparison:
            if quote.source != "web":
                continue
            base = quote.cost if quote.cost is not None else quote.price
            if base is not None and abs(base - result.unit_base_price) < 0.01:
                preferred = quote
                break
    url = resolve_price_source_url(
        result.comparison,
        unit_base_price=result.unit_base_price,
        preferred=preferred,
        product_name=result.tz_item.name,
    )
    if url:
        return url
    detail = result.source_detail or ""
    match = re.search(r"https?://[^\s|]+", detail)
    if match:
        return match.group(0).rstrip("|")
    fallbacks = pick_marketplace_search_urls(result.comparison, result.tz_item.name, limit=1)
    return fallbacks[0] if fallbacks else None


def _marketplace_urls_from_result(result: MatchResult) -> list[str]:
    if not is_internet_sourced_result(result):
        return []
    has_product_url = any(
        quote.source == "web"
        and quote.url
        and "search" not in (quote.url or "").lower()
        and "catalog/0/search" not in (quote.url or "").lower()
        for quote in result.comparison
    )
    if has_product_url:
        return []
    return pick_marketplace_search_urls(result.comparison, result.tz_item.name, limit=3)


def _match_result_to_dict(result: MatchResult) -> dict[str, Any]:
    return {
        "number": result.tz_item.number,
        "name": result.tz_item.name,
        "specifications": result.tz_item.specifications,
        "country_of_origin": result.tz_item.country_of_origin,
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
        "applied_markup_pct": effective_markup_percent(result),
        "applied_markup_label": format_markup_percent(effective_markup_percent(result)),
        "internet_url": _internet_url_from_result(result),
        "marketplace_urls": _marketplace_urls_from_result(result),
        "comparison": [_price_quote_to_dict(q) for q in result.comparison],
        "competitors": [_price_quote_to_dict(q) for q in result.competitors],
        "kit_components": [_kit_component_to_dict(k) for k in result.kit_components],
        "price_list_check": (
            _price_quote_to_dict(result.price_list_check)
            if result.price_list_check
            else None
        ),
    }


def _summary_to_dict(summary, filename: str, *, pdf_filename: str | None = None) -> dict[str, Any]:
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
        "pdf_filename": pdf_filename,
        "pdf_download_url": None,
    }


def _attach_download_info(
    payload: dict[str, Any],
    output_path: Path | None,
    *,
    pdf_path: Path | None = None,
) -> None:
    formed = False
    if (
        output_path
        and output_path.name.startswith("KP_")
        and output_path.suffix == ".xlsx"
        and output_path.exists()
    ):
        payload["has_download"] = True
        payload["summary"]["filename"] = output_path.name
        payload["summary"]["download_url"] = f"/api/files/{output_path.name}"
        formed = True
    else:
        payload["has_download"] = False
        payload["summary"]["download_url"] = None

    if (
        pdf_path
        and pdf_path.name.startswith("KP_")
        and pdf_path.suffix == ".pdf"
        and pdf_path.exists()
    ):
        payload["has_pdf_download"] = True
        payload["summary"]["pdf_filename"] = pdf_path.name
        payload["summary"]["pdf_download_url"] = f"/api/files/{pdf_path.name}"
        formed = True
    else:
        payload["has_pdf_download"] = False
        payload["summary"]["pdf_download_url"] = None

    payload["kp_formed"] = formed


def _safe_output_path(filename: str) -> Path:
    safe_name = Path(filename).name
    if not safe_name.startswith("KP_"):
        raise HTTPException(status_code=400, detail="Недопустимое имя файла")
    if not (safe_name.endswith(".xlsx") or safe_name.endswith(".pdf")):
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
    from src.services.data_loader import load_goods_report, load_registry

    counts = {
        "catalog": len(processor.catalog),
        "registry": len(processor.registry),
        "goods_report": len(load_goods_report(GOODS_REPORT_PATH))
        if GOODS_REPORT_PATH.exists()
        else 0,
        "procurement": len(load_goods_report(PROCUREMENT_REPORT_PATH))
        if PROCUREMENT_REPORT_PATH and PROCUREMENT_REPORT_PATH.exists()
        else 0,
        "stock_balance": len(load_registry(STOCK_BALANCE_PATH))
        if STOCK_BALANCE_PATH and STOCK_BALANCE_PATH.exists()
        else 0,
    }
    return {"entry": manager.to_dict(source_id, counts.get(source_id, 0))}


def _is_public_web_path(path: str) -> bool:
    if path in {"/login.html"}:
        return True
    if path.startswith("/app.") or path.endswith((".css", ".js", ".svg", ".ico")):
        return True
    if path == "/api/auth/login":
        return True
    return False


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    if not AUTH_ENABLED or _is_public_web_path(request.url.path):
        return await call_next(request)
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    token = request.cookies.get(AUTH_COOKIE_NAME) or request.headers.get("X-Auth-Token")
    user = resolve_user_by_token(token)
    if user is None:
        return JSONResponse({"detail": "Требуется авторизация"}, status_code=401)
    request.state.user = user
    request.state.auth_token = token
    return await call_next(request)


def _current_user(request: Request):
    return getattr(request.state, "user", None)


def _record_tz_upload(
    request: Request,
    *,
    original_filename: str,
    items_count: int,
    task_mode: str,
    session_id: str,
) -> None:
    user = _current_user(request)
    if not AUTH_ENABLED or user is None:
        return
    get_user_database().record_tz_upload(
        user.id,
        filename=original_filename,
        original_filename=original_filename,
        items_count=items_count,
        task_mode=task_mode,
        session_id=session_id,
    )


def _record_file_export(
    request: Request,
    *,
    session_id: str | None,
    tz_filename: str | None,
    xlsx_filename: str,
    pdf_filename: str,
) -> None:
    user = _current_user(request)
    if not AUTH_ENABLED or user is None:
        return
    get_user_database().record_file_export(
        user.id,
        session_id=session_id,
        tz_filename=tz_filename,
        xlsx_filename=xlsx_filename,
        pdf_filename=pdf_filename,
    )


def _record_download(request: Request, filename: str) -> None:
    user = _current_user(request)
    if not AUTH_ENABLED or user is None:
        return
    db = get_user_database()
    export = db.find_export_by_filename(filename)
    file_type = "pdf" if filename.lower().endswith(".pdf") else "xlsx"
    db.record_download(
        user.id,
        filename=filename,
        file_type=file_type,
        export_id=int(export["id"]) if export else None,
    )


def _kp_chat_service() -> KpChatService:
    return KpChatService(get_processor())


def _doc_rag_index_service():
    service = _kp_chat_service()
    index = get_document_rag_index(service.rag)
    index.bootstrap(get_price_list_manager())
    return index


def _index_static_source_rag(source_id: str, *, force: bool = True) -> dict[str, str | int | bool]:
    static_manager = get_static_source_manager()
    config = static_manager.get_config(source_id)
    return _doc_rag_index_service().index_document(
        doc_id=f"{config.source_id}:main",
        source_type=config.source_id,
        source_name=static_manager.get_display_name(config.source_id),
        file_path=config.path,
        force=force,
    )


async def _upload_static_source_file(source_id: str, upload: UploadFile) -> dict[str, Any]:
    filename = upload.filename or "data.xlsx"
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Файл должен быть .xlsx")

    static_manager = get_static_source_manager()
    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = Path(tmpdir) / filename
        source_path.write_bytes(await upload.read())
        try:
            static_manager.replace_file(source_id, source_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    reload_processor()
    rag = _index_static_source_rag(source_id, force=True)
    response = _static_source_response(source_id.lower())
    response["rag"] = rag
    return response


def _competitor_site_to_dict(
    *,
    site_id: str,
    domain: str,
    label: str,
    url: str,
    search_url: str | None,
    builtin: bool,
    title: str = "",
    notes: str = "",
    status: str = "",
    added_at: str = "",
) -> dict[str, Any]:
    return {
        "id": site_id,
        "domain": domain,
        "label": label,
        "url": url,
        "search_url": search_url,
        "builtin": builtin,
        "title": title,
        "notes": notes,
        "status": status,
        "added_at": added_at,
    }


def _list_competitor_sites_payload() -> dict[str, Any]:
    manager = get_competitor_site_manager()
    builtin_rows = [
        _competitor_site_to_dict(
            site_id=f"builtin:{site.domain}",
            domain=site.domain,
            label=site.label,
            url=f"https://{site.domain}",
            search_url=site.search_url,
            builtin=True,
        )
        for site in COMPETITOR_SITES
    ]
    custom_rows = [
        _competitor_site_to_dict(
            site_id=entry.id,
            domain=entry.domain,
            label=entry.label,
            url=entry.url,
            search_url=entry.search_url,
            builtin=False,
            title=entry.title,
            notes=entry.notes,
            status=entry.status,
            added_at=entry.added_at,
        )
        for entry in manager.list_custom()
    ]
    return {
        "builtin": builtin_rows,
        "custom": custom_rows,
        "items": builtin_rows + custom_rows,
        "total": len(builtin_rows) + len(custom_rows),
    }


def _index_competitor_rag(
    entry,
    analysis: dict,
    *,
    skip_catalog: bool = False,
) -> dict[str, str | int | bool]:
    rag_text = str(analysis.get("rag_text") or "")
    if not rag_text.strip():
        rag_text = (
            f"Сайт конкурента: {entry.label}\n"
            f"Домен: {entry.domain}\n"
            f"URL: {entry.url}\n"
            f"Поиск: {entry.search_url or '—'}"
        )
    meta = _doc_rag_index_service().index_text(
        doc_id=f"competitor:{entry.id}",
        source_type="competitor",
        source_name=entry.label,
        text=rag_text,
        filename=entry.domain,
        force=True,
    )
    if skip_catalog:
        return {"meta": meta, "catalog": {"skipped": True}, "pages": []}

    from src.services.competitor_catalog_service import (
        apply_parsing_hints_from_entry,
        index_competitor_page_url,
        index_competitor_site_catalog,
    )
    from src.services.competitor_sites import CompetitorSite

    apply_parsing_hints_from_entry(entry)
    site = CompetitorSite(
        domain=entry.domain,
        label=entry.label or entry.domain,
        search_url=entry.search_url,
    )
    catalog = index_competitor_site_catalog(
        site,
        _doc_rag_index_service(),
        force=True,
        extra_urls=entry.catalog_urls,
    )
    pages: list[dict[str, object]] = []
    for page_url in entry.catalog_urls:
        pages.append(
            index_competitor_page_url(
                page_url,
                domain=entry.domain,
                site_label=entry.label,
                doc_rag_index=_doc_rag_index_service(),
            )
        )
    return {"meta": meta, "catalog": catalog, "pages": pages}


def _index_competitor_site_meta(entry, analysis: dict) -> dict[str, str | int | bool]:
    return _index_competitor_rag(entry, analysis, skip_catalog=True)


def _normalize_task_mode(task_mode: str | None, *, parse_only: bool | None = None) -> str:
    if task_mode in ("task1", "task1_task2"):
        return task_mode
    if parse_only is False:
        return "task1_task2"
    return "task1"


def _process_tz_upload(
    tz_path: Path,
    use_ai: bool,
    *,
    task_mode: str = "task1",
    filename: str = "",
    tz_items: list | None = None,
    rag_index=None,
) -> dict[str, Any]:
    task_mode = _normalize_task_mode(task_mode)
    include_web = task_mode == "task1_task2"
    processor = get_processor()
    start = time.perf_counter()
    parsed_items = tz_items if tz_items is not None else processor.parse_tz_file(tz_path)
    if rag_index is None:
        rag_service = TZRagService(processor.ai)
        rag_index = rag_service.build_index(
            extract_tz_document_text(tz_path),
            parsed_items,
            filename=filename or tz_path.name,
        )
    prefs = KpPreferences()
    results = processor.search_tz_items(
        parsed_items,
        use_ai=use_ai,
        preferences=prefs,
        include_web=include_web,
    )
    summary = processor._build_summary(results, time.perf_counter() - start)

    session_id = _kp_chat_service().create_session(
        parsed_items,
        results,
        summary,
        None,
        use_ai=use_ai,
        tz_filename=filename or tz_path.name,
        parsed_only=False,
        auto_searched=True,
        rag_index=rag_index,
        task_mode=task_mode,
    )
    session = _kp_chat_service().store.get(session_id)
    welcome = session.chat_history[-1].text if session and session.chat_history else ""
    payload = {
        "session_id": session_id,
        "stage": "searched",
        "task_mode": task_mode,
        "search_completed": True,
        "kp_formed": False,
        "welcome_reply": welcome,
        "summary": _summary_to_dict(summary, "pending.xlsx"),
        "items": [_match_result_to_dict(r) for r in results],
        "ai_used": use_ai and processor.ai.enabled,
        "web_used": include_web and any(
            r.source == MatchSource.WEB or (r.competitors and len(r.competitors) > 0)
            for r in results
        ),
        "rag": {
            "enabled": True,
            "chunks": len(rag_index.chunks),
            "vectorized": bool(rag_index.vectors),
        },
    }
    _attach_download_info(payload, None)
    return payload


def _process_tz_path(
    tz_path: Path,
    use_ai: bool,
    *,
    task_mode: str = "task1",
    parse_only: bool | None = None,
    filename: str = "",
) -> dict[str, Any]:
    normalized_mode = _normalize_task_mode(task_mode, parse_only=parse_only)
    processor = get_processor()
    parsed_items = processor.parse_tz_file(tz_path)
    rag_service = TZRagService(processor.ai)
    rag_index = rag_service.build_index(
        extract_tz_document_text(tz_path),
        parsed_items,
        filename=filename or tz_path.name,
    )
    return _process_tz_upload(
        tz_path,
        use_ai,
        task_mode=normalized_mode,
        filename=filename,
        tz_items=parsed_items,
        rag_index=rag_index,
    )


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

    registry = _serialize_registry_block(result.registry)
    photo_urls = list(registry.get("photo_urls") or [])
    if not photo_urls:
        items = (result.competitors or {}).get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                image_url = item.get("image_url")
                if isinstance(image_url, str) and image_url.strip():
                    photo_urls.append(image_url.strip())

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
        "registry": registry,
        "competitors": result.competitors,
        "ai_insight": result.ai_insight,
        "photo_urls": photo_urls,
        "photo_url": photo_urls[0] if photo_urls else None,
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
    from src.services.competitor_product_store import get_competitor_product_store

    processor = get_processor()
    price_entries = processor.price_manager.list_entries()
    rag_docs_stats = _doc_rag_index_service().stats()
    competitor_store = get_competitor_product_store()
    competitor_store.reload()
    competitor_catalog = competitor_store.stats()
    return {
        "catalog_count": len(processor.catalog),
        "registry_count": len(processor.registry),
        "price_items_count": len(processor.price_lists),
        "competitor_products_count": competitor_catalog["products"],
        "competitor_sites_count": competitor_catalog["sites"],
        "competitor_products_by_domain": competitor_catalog.get("by_domain", {}),
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
        "meilisearch": meilisearch_health(),
        "rag_docs": rag_docs_stats,
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
def api_process_demo(
    use_ai: bool = True,
    task_mode: str = "task1",
    parse_only: bool | None = None,
) -> dict[str, Any]:
    if not DEMO_TZ_PATH.exists():
        raise HTTPException(status_code=404, detail="Демо-файл data/sample_tz.docx не найден")
    try:
        return _process_tz_path(
            DEMO_TZ_PATH,
            use_ai=use_ai,
            task_mode=task_mode,
            parse_only=parse_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Demo processing failed")
        raise HTTPException(status_code=500, detail=f"Ошибка обработки: {exc}") from exc


@app.post("/api/kp/session")
def api_kp_session_create(body: KpSessionCreateRequest = KpSessionCreateRequest()) -> dict[str, Any]:
    session_id = _kp_chat_service().create_free_session(use_ai=body.use_ai)
    return {
        "session_id": session_id,
        "welcome_reply": WELCOME_MESSAGE,
        "search_completed": False,
        "stage": "intake",
    }


@app.post("/api/kp/chat")
def api_kp_chat(body: KpChatRequest) -> dict[str, Any]:
    try:
        service = _kp_chat_service()
        session_id = body.session_id
        session_recreated = False
        if session_id:
            session = service.store.get(session_id)
            if not session:
                session_id = service.create_free_session()
                session_recreated = True
        else:
            session_id = service.create_free_session()
            session_recreated = True
        chat_result = service.chat(session_id, body.message)
        response = _kp_chat_response(chat_result, session_id)
        response["session_recreated"] = session_recreated
        return response
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("KP chat failed")
        raise HTTPException(status_code=500, detail=f"Ошибка чата: {exc}") from exc


@app.post("/api/rag/query")
def api_rag_query(body: RagQueryRequest) -> dict[str, Any]:
    service = _kp_chat_service()
    session = service.store.get(body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    index = RagIndex(
        chunks=session.rag_chunks,
        vectors=session.rag_vectors,
    )
    if not index.chunks:
        return {
            "session_id": body.session_id,
            "query": body.query,
            "top_k": body.top_k,
            "retrieval_mode": "empty",
            "chunks": [],
            "total_chunks": 0,
        }

    rows = service.rag.retrieve_debug(body.query, index, top_k=body.top_k)
    retrieval_mode = "vector" if index.vectors else "lexical"
    chunks = [
        {
            "rank": rank,
            "chunk_id": row.get("chunk_id"),
            "filename": row.get("filename"),
            "score": row.get("score"),
            "start": row.get("start"),
            "end": row.get("end"),
            "text": row.get("text"),
        }
        for rank, row in enumerate(rows, start=1)
    ]
    return {
        "session_id": body.session_id,
        "query": body.query,
        "top_k": body.top_k,
        "retrieval_mode": retrieval_mode,
        "chunks": chunks,
        "total_chunks": len(index.chunks),
    }


@app.post("/api/rag/query/sources")
def api_rag_query_sources(body: RagSourceQueryRequest) -> dict[str, Any]:
    index = _doc_rag_index_service()
    rows = index.query(
        body.query,
        source_type=body.source_type.strip().lower() if body.source_type else None,
        top_k=body.top_k,
    )
    return {
        "query": body.query,
        "top_k": body.top_k,
        "source_type": body.source_type,
        "stats": index.stats(),
        "chunks": [
            {
                "rank": rank,
                "doc_id": row.get("doc_id"),
                "source_type": row.get("source_type"),
                "source_name": row.get("source_name"),
                "chunk_id": row.get("chunk_id"),
                "filename": row.get("filename"),
                "score": row.get("score"),
                "start": row.get("start"),
                "end": row.get("end"),
                "text": row.get("text"),
            }
            for rank, row in enumerate(rows, start=1)
        ],
    }


def _kp_selection_items(body: KpFormRequest) -> list:
    from src.services.kp_selection import KpSelectionItem

    return [
        KpSelectionItem(
            number=item.number,
            included=item.included,
            variant=item.variant,
            kit_indices=tuple(item.kit_indices) if item.kit_indices is not None else None,
            web_indices=tuple(item.web_indices) if item.web_indices is not None else None,
        )
        for item in body.selections
    ]


def _kp_selected_results_and_summary(session, selections: list):
    from src.services.kp_selection import apply_kp_selections

    selected_results = apply_kp_selections(session.results, selections)
    if not selected_results:
        raise HTTPException(
            status_code=400,
            detail="Выберите хотя бы одну позицию для формирования КП",
        )
    processor = get_processor()
    summary = processor._build_summary(
        selected_results,
        session.summary.processing_seconds,
    )
    return selected_results, summary


@app.post("/api/kp/selection/preview")
def api_kp_selection_preview(body: KpFormRequest) -> dict[str, Any]:
    store = _kp_chat_service().store
    session = store.get(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    if not session.search_completed:
        raise HTTPException(status_code=400, detail="Сначала выполните поиск по ТЗ")

    selections = _kp_selection_items(body)
    _, summary = _kp_selected_results_and_summary(session, selections)
    session.stage = "selection_saved"
    store.save()

    return {
        "session_id": session.session_id,
        "stage": "selection_saved",
        "summary": _summary_to_dict(summary, session.output_path.name if session.output_path else "pending.xlsx"),
        "selected_count": summary.total_items,
    }


@app.post("/api/kp/form")
def api_kp_form(request: Request, body: KpFormRequest) -> dict[str, Any]:
    from src.services.pdf_generator import PdfGenerator

    store = _kp_chat_service().store
    session = store.get(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    if not session.search_completed:
        raise HTTPException(status_code=400, detail="Сначала выполните поиск по ТЗ")

    selections = _kp_selection_items(body)
    selected_results, summary = _kp_selected_results_and_summary(session, selections)
    processor = get_processor()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    xlsx_path = OUTPUT_DIR / f"KP_{timestamp}.xlsx"
    pdf_path = OUTPUT_DIR / f"KP_{timestamp}.pdf"
    processor.excel.generate(
        selected_results,
        summary,
        xlsx_path,
        preferences=session.preferences,
        task_mode=session.task_mode,
        with_margin=True,
    )
    PdfGenerator().generate(selected_results, summary, pdf_path)

    session.summary = summary
    session.output_path = xlsx_path
    session.pdf_path = pdf_path
    session.stage = "exported"
    store.save()

    payload = {
        "session_id": session.session_id,
        "stage": "exported",
        "task_mode": session.task_mode,
        "search_completed": True,
        "summary": _summary_to_dict(summary, xlsx_path.name, pdf_filename=pdf_path.name),
        "selected_count": len(selected_results),
    }
    _attach_download_info(payload, xlsx_path, pdf_path=pdf_path)
    _record_file_export(
        request,
        session_id=session.session_id,
        tz_filename=session.tz_filename,
        xlsx_filename=xlsx_path.name,
        pdf_filename=pdf_path.name,
    )
    return payload


@app.post("/api/process/upload")
async def api_process_upload(
    request: Request,
    file: UploadFile = File(...),
    use_ai: bool = Form(default=True),
    task_mode: str = Form(default="task1"),
    parse_only: bool | None = Form(default=None),
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
            payload = _process_tz_path(
                tz_path,
                use_ai=use_ai,
                task_mode=task_mode,
                parse_only=parse_only,
                filename=filename,
            )
            if payload.get("session_id"):
                _record_tz_upload(
                    request,
                    original_filename=filename,
                    items_count=len(payload.get("items") or []),
                    task_mode=str(payload.get("task_mode") or task_mode),
                    session_id=str(payload["session_id"]),
                )
            return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Upload processing failed")
        raise HTTPException(status_code=500, detail=f"Ошибка обработки: {exc}") from exc


@app.get("/api/files/{filename}")
def api_download_file(request: Request, filename: str) -> FileResponse:
    path = _safe_output_path(filename)
    media_type = (
        "application/pdf"
        if path.suffix.lower() == ".pdf"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    _record_download(request, path.name)
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
    )


@app.post("/api/lookup")
def api_lookup(body: LookupRequest) -> dict[str, Any]:
    started = time.perf_counter()
    parsed = resolve_freeform_product_lookup(body.query)
    if not parsed:
        parsed = parse_lookup_query(
            body.query if body.query.startswith("/find") else f"/find {body.query}"
        )
    if not parsed:
        logger.warning("Lookup parse failed: %r", body.query[:120])
        raise HTTPException(
            status_code=400,
            detail="Не удалось распознать запрос. Пример: термометр лабораторный | цена, остаток",
        )

    logger.info(
        "Lookup start product=%r fields=%s",
        parsed.product_name,
        [field.value for field in parsed.requested_fields],
    )
    processor = get_processor()
    lookup = ProductLookupService(
        processor.matcher,
        processor.ai,
        processor.tz_matcher.web_search,
    )
    result = lookup.lookup(parsed.product_name, parsed.requested_fields)
    logger.info(
        "Lookup done product=%r status=%s score=%.1f %.0fms",
        parsed.product_name,
        result.status.value,
        result.match_score,
        (time.perf_counter() - started) * 1000,
    )
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


@app.get("/api/competitors")
def api_competitors_list() -> dict[str, Any]:
    from src.services.competitor_product_store import get_competitor_product_store

    payload = _list_competitor_sites_payload()
    payload["rag_docs"] = _doc_rag_index_service().stats()
    store = get_competitor_product_store()
    store.reload()
    payload["catalog_products"] = store.stats()
    return payload


@app.get("/api/competitors/catalog/db")
def api_competitors_catalog_db(domain: str | None = None) -> dict[str, Any]:
    from src.services.competitor_product_store import get_competitor_product_store

    store = get_competitor_product_store()
    if domain:
        normalized = domain.lower().removeprefix("www.").strip()
        if not normalized:
            raise HTTPException(status_code=400, detail="Укажите domain")
        report = store.catalog_db_report(domain=normalized)
        if not report["sites"]:
            raise HTTPException(status_code=404, detail=f"Каталог для {normalized} не найден")
        return report
    return store.catalog_db_report()


@app.post("/api/competitors/analyze")
def api_competitors_analyze(body: CompetitorSiteAnalyzeRequest) -> dict[str, Any]:
    manager = get_competitor_site_manager()
    try:
        analysis = manager.analyze_url(body.url, label=body.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"analysis": analysis}


@app.get("/api/competitors/index/status")
def api_competitors_index_status(url: str) -> dict[str, Any]:
    from src.services.competitor_catalog_service import get_index_phase_label, get_reindex_job

    manager = get_competitor_site_manager()
    try:
        normalized = manager.normalize_url(url)
        domain = manager.domain_from_url(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    normalized = domain.lower().removeprefix("www.")
    draft = manager.get_draft(domain)
    job = get_reindex_job(normalized)
    running = bool(job and job.get("running"))
    phase = str(job.get("phase") or "") if job else ""
    analysis = (job or {}).get("analysis") or (draft.analysis if draft else None)
    from src.services.competitor_product_store import get_competitor_product_store

    store = get_competitor_product_store()
    store.reload()
    is_builtin = bool(
        (draft and draft.builtin)
        or (job and job.get("is_builtin"))
    )
    return {
        "domain": domain,
        "running": running,
        "phase": phase or None,
        "phase_label": get_index_phase_label(phase if phase else None),
        "index_completed": bool(draft and draft.indexed and not running),
        "is_builtin": is_builtin,
        "catalog": draft.index_result if draft else None,
        "analysis": analysis,
        "error": job.get("error") if job else None,
        "catalog_products": store.stats(),
    }


@app.get("/api/competitors/index/logs")
def api_competitors_index_logs(domain: str, since: int = 0) -> dict[str, Any]:
    from src.services.competitor_catalog_service import get_index_logs, get_reindex_job

    normalized = domain.lower().removeprefix("www.").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Укажите domain")
    job = get_reindex_job(normalized)
    return {
        "domain": normalized,
        "logs": get_index_logs(normalized, since=since),
        "running": bool(job and job.get("running")),
        "phase": job.get("phase") if job else None,
    }


@app.post("/api/competitors/index")
def api_competitors_index(body: CompetitorSiteIndexRequest) -> dict[str, Any]:
    from src.services.competitor_catalog_service import start_competitor_site_index_background
    from src.services.competitor_product_store import get_competitor_product_store

    try:
        result = start_competitor_site_index_background(
            url=body.url,
            label=body.label,
            product_sample_url=body.product_sample_url,
            price_html_hint=body.price_html_hint,
            articul_html_hint=body.articul_html_hint,
            doc_rag_index=_doc_rag_index_service(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = get_competitor_product_store()
    store.reload()
    return {
        "started": result.get("started", False),
        "running": result.get("running", False),
        "domain": result.get("domain"),
        "phase": result.get("phase"),
        "is_builtin": bool(result.get("is_builtin")),
        "index_completed": False,
        "message": result.get("message"),
        "catalog_products": store.stats(),
    }


@app.post("/api/competitors")
def api_competitors_add(body: CompetitorSiteAddRequest) -> dict[str, Any]:
    manager = get_competitor_site_manager()
    try:
        entry, analysis = manager.add_from_indexed_draft(
            body.url,
            label=body.label,
            product_sample_url=body.product_sample_url,
            price_html_hint=body.price_html_hint,
            articul_html_hint=body.articul_html_hint,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from src.services.competitor_catalog_service import set_domain_parsing_hints

    set_domain_parsing_hints(
        entry.domain,
        product_sample_url=entry.product_sample_url or "",
        price_html_hint=entry.price_html_hint or "",
        articul_html_hint=entry.articul_html_hint or "",
    )
    rag = _index_competitor_site_meta(entry, analysis)
    from src.services.competitor_product_store import get_competitor_product_store

    return {
        "entry": _competitor_site_to_dict(
            site_id=entry.id,
            domain=entry.domain,
            label=entry.label,
            url=entry.url,
            search_url=entry.search_url,
            builtin=False,
            title=entry.title,
            notes=entry.notes,
            status=entry.status,
            added_at=entry.added_at,
        ),
        "analysis": analysis,
        "rag": rag,
        "catalog_products": get_competitor_product_store().stats(),
    }


@app.delete("/api/competitors/{site_id}")
def api_competitors_remove(site_id: str) -> dict[str, Any]:
    manager = get_competitor_site_manager()
    try:
        entry = manager.remove(site_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _doc_rag_index_service().remove_document(f"competitor:{entry.id}")
    _doc_rag_index_service().remove_document(f"competitor-catalog:{entry.domain}")
    from src.services.competitor_catalog_urls import get_competitor_catalog_url_registry
    from src.services.competitor_product_store import get_competitor_product_store

    get_competitor_product_store().remove_domain(entry.domain)
    get_competitor_catalog_url_registry().remove_domain(entry.domain)
    from src.services.competitor_catalog_service import sync_unified_competitor_rag

    sync_unified_competitor_rag(_doc_rag_index_service())
    return {
        "removed_id": entry.id,
        "removed_domain": entry.domain,
        "catalog_products": get_competitor_product_store().stats(),
    }


@app.post("/api/competitors/reindex")
def api_competitors_reindex(body: CompetitorReindexRequest) -> dict[str, Any]:
    from src.services.competitor_catalog_service import (
        _SITEMAP_CATALOG_MIN_PRODUCTS,
        index_competitor_site_catalog,
        list_reindex_jobs,
        reindex_all_competitor_sites,
        start_site_reindex_background,
        sync_unified_competitor_rag,
    )
    from src.services.competitor_product_store import get_competitor_product_store
    from src.services.competitor_sites import competitor_sites_with_search

    index = _doc_rag_index_service()

    try:
        if body.domains:
            normalized_domains = {
                domain.lower().removeprefix("www.") for domain in body.domains if domain.strip()
            }
            selected_sites = [
                site
                for site in competitor_sites_with_search()
                if site.domain.lower().removeprefix("www.") in normalized_domains
            ]
            if not selected_sites:
                raise HTTPException(status_code=400, detail="Указанные домены не найдены")

            use_background = body.background or any(
                site.domain.lower().removeprefix("www.") in _SITEMAP_CATALOG_MIN_PRODUCTS
                for site in selected_sites
            )
            if use_background:
                reindex_force = body.force or True
                jobs = [
                    start_site_reindex_background(
                        site.domain,
                        index,
                        force=reindex_force,
                    )
                    for site in selected_sites
                ]
                return {
                    "mode": "background",
                    "jobs": jobs,
                    "active_jobs": list_reindex_jobs(),
                    "catalog_products": get_competitor_product_store().stats(),
                    "rag_docs": index.stats(),
                }

            results: list[dict[str, Any]] = []
            for site in selected_sites:
                results.append(
                    index_competitor_site_catalog(site, index, force=body.force)
                    | {"domain": site.domain, "label": site.label}
                )
            sync_unified_competitor_rag(index)
            store = get_competitor_product_store()
            store.reload()
            return {
                "mode": "sync",
                "sites": results,
                "catalog_products": store.stats(),
                "rag_docs": index.stats(),
            }

        return reindex_all_competitor_sites(index, force=True)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Competitor reindex failed domains=%s", body.domains)
        raise HTTPException(status_code=500, detail=f"Ошибка индексации: {exc}") from exc


@app.get("/api/competitors/reindex/status")
def api_competitors_reindex_status(domain: str | None = None) -> dict[str, Any]:
    from src.services.competitor_catalog_service import get_reindex_job, list_reindex_jobs
    from src.services.competitor_product_store import get_competitor_product_store

    store = get_competitor_product_store()
    store.reload()
    if domain:
        normalized = domain.lower().removeprefix("www.")
        return {
            "job": get_reindex_job(normalized),
            "catalog_products": store.stats(),
        }
    return {
        "jobs": list_reindex_jobs(),
        "catalog_products": store.stats(),
    }


@app.post("/api/competitors/pages/index")
def api_competitors_index_page(body: CompetitorPageIndexRequest) -> dict[str, Any]:
    from src.services.competitor_catalog_service import index_competitor_page_url
    from src.services.competitor_product_store import get_competitor_product_store
    from src.services.competitor_site_manager import get_competitor_site_manager
    from src.services.competitor_sites import competitor_label_for_url

    manager = get_competitor_site_manager()
    try:
        normalized = manager.normalize_url(body.url)
        domain = manager.domain_from_url(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    label = body.label.strip() or competitor_label_for_url(normalized) or domain
    result = index_competitor_page_url(
        normalized,
        domain=domain,
        site_label=label,
        doc_rag_index=_doc_rag_index_service(),
    )
    return {
        "result": result,
        "catalog_products": get_competitor_product_store().stats(),
        "rag_docs": _doc_rag_index_service().stats(),
    }


@app.post("/api/competitors/enrich-images")
def api_competitors_enrich_images(body: CompetitorEnrichImagesRequest) -> dict[str, Any]:
    from src.services.competitor_catalog_service import enrich_site_product_images
    from src.services.competitor_product_store import get_competitor_product_store
    from src.services.competitor_sites import competitor_label_for_url

    domain = body.domain.lower().removeprefix("www.")
    label = body.label.strip() or competitor_label_for_url(f"https://{domain}/") or domain
    result = enrich_site_product_images(
        domain,
        site_label=label,
        limit=body.limit,
        doc_rag_index=_doc_rag_index_service(),
    )
    return {
        "result": result,
        "catalog_products": get_competitor_product_store().stats(),
        "rag_docs": _doc_rag_index_service().stats(),
    }


@app.post("/api/competitors/search")
def api_competitors_search(body: CompetitorSearchRequest) -> dict[str, Any]:
    started = time.perf_counter()
    processor = get_processor()
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Введите название товара")

    quotes = processor.tz_matcher.web_search.search_competitor_offers(
        query,
        limit=body.limit,
    )
    items = [_price_quote_to_dict(quote) for quote in quotes]
    sites_searched = len({quote.label for quote in quotes if quote.label})
    logger.info(
        "Competitor search query=%r results=%s sites=%s %.0fms",
        query[:120],
        len(items),
        sites_searched,
        (time.perf_counter() - started) * 1000,
    )
    return {
        "query": query,
        "items": items,
        "count": len(items),
        "sites_searched": sites_searched or len(_list_competitor_sites_payload()["items"]),
        "processing_seconds": round(time.perf_counter() - started, 2),
    }


@app.get("/api/prices")
def api_prices_list() -> dict[str, Any]:
    from src.services.data_loader import load_goods_report, load_registry

    processor = get_processor()
    manager = get_price_list_manager()
    static_manager = get_static_source_manager()
    entries = manager.list_entries()
    catalog = _catalog_source(processor)
    registry = _registry_source(processor)
    goods_report = (
        static_manager.to_dict("goods_report", len(load_goods_report(GOODS_REPORT_PATH)))
        if GOODS_REPORT_PATH.exists()
        else None
    )
    procurement = (
        static_manager.to_dict(
            "procurement",
            len(load_goods_report(PROCUREMENT_REPORT_PATH)),
        )
        if PROCUREMENT_REPORT_PATH and PROCUREMENT_REPORT_PATH.exists()
        else None
    )
    stock_balance = (
        static_manager.to_dict(
            "stock_balance",
            len(load_registry(STOCK_BALANCE_PATH)),
        )
        if STOCK_BALANCE_PATH and STOCK_BALANCE_PATH.exists()
        else None
    )
    price_items = [
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
    ]
    return {
        "catalog": catalog,
        "registry": registry,
        "goods_report": goods_report,
        "procurement": procurement,
        "stock_balance": stock_balance,
        "items": price_items,
        "catalogs": [catalog] if catalog else [],
        "prices": price_items,
        "stock": [item for item in [stock_balance, registry] if item],
        "reports": [item for item in [goods_report, procurement] if item],
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
    rag = _doc_rag_index_service().index_document(
        doc_id=f"price:{entry.id}",
        source_type="price",
        source_name=entry.name,
        file_path=manager.file_path(entry),
        force=True,
    )
    return {
        "entry": {
            "id": entry.id,
            "name": entry.name,
            "supplier": entry.supplier,
            "items_count": entry.items_count,
            "updated_at": entry.updated_at,
        },
        "total_price_items": total_items,
        "rag": rag,
    }


@app.post("/api/sources/catalog/upload")
async def api_upload_catalog(file: UploadFile = File(...)) -> dict[str, Any]:
    return await _upload_static_source_file("catalog", file)


@app.post("/api/sources/registry/upload")
async def api_upload_registry(file: UploadFile = File(...)) -> dict[str, Any]:
    return await _upload_static_source_file("registry", file)


@app.post("/api/sources/stock_balance/upload")
async def api_upload_stock_balance(file: UploadFile = File(...)) -> dict[str, Any]:
    return await _upload_static_source_file("stock_balance", file)


@app.post("/api/sources/procurement/upload")
async def api_upload_procurement(file: UploadFile = File(...)) -> dict[str, Any]:
    return await _upload_static_source_file("procurement", file)


@app.post("/api/sources/goods_report/upload")
async def api_upload_goods_report(file: UploadFile = File(...)) -> dict[str, Any]:
    return await _upload_static_source_file("goods_report", file)


@app.post("/api/sources/reload")
def api_sources_reload() -> dict[str, Any]:
    """Перезагрузить каталог, реестр остатков и прайсы из data/."""
    total_items = reload_processor()
    rag_index = _doc_rag_index_service()
    rag: dict[str, object] = {}
    if CATALOG_PATH.exists():
        rag["catalog"] = rag_index.index_document(
            doc_id="catalog:main",
            source_type="catalog",
            source_name=CATALOG_PATH.stem,
            file_path=CATALOG_PATH,
            force=True,
        )
    if REGISTRY_PATH.exists():
        rag["registry"] = rag_index.index_document(
            doc_id="registry:main",
            source_type="registry",
            source_name=REGISTRY_PATH.stem,
            file_path=REGISTRY_PATH,
            force=True,
        )
    if STOCK_BALANCE_PATH and STOCK_BALANCE_PATH.exists():
        rag["stock_balance"] = rag_index.index_document(
            doc_id="margin:stock_balance",
            source_type="margin",
            source_name=STOCK_BALANCE_PATH.stem,
            file_path=STOCK_BALANCE_PATH,
            force=True,
        )
    if PROCUREMENT_REPORT_PATH and PROCUREMENT_REPORT_PATH.exists():
        rag["procurement"] = rag_index.index_document(
            doc_id="margin:procurement",
            source_type="margin",
            source_name=PROCUREMENT_REPORT_PATH.stem,
            file_path=PROCUREMENT_REPORT_PATH,
            force=True,
        )
    if GOODS_REPORT_PATH.exists():
        rag["goods_report"] = rag_index.index_document(
            doc_id="margin:goods_report",
            source_type="margin",
            source_name=GOODS_REPORT_PATH.stem,
            file_path=GOODS_REPORT_PATH,
            force=True,
        )
    processor = get_processor()
    return {
        "catalog_items": len(processor.catalog),
        "registry_items": len(processor.registry),
        "price_list_items": total_items,
        "rag": rag,
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
        rag = _index_static_source_rag(price_id.lower(), force=True)
        response = _static_source_response(price_id.lower())
        response["rag"] = rag
        return response

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
    rag = _doc_rag_index_service().index_document(
        doc_id=f"price:{entry.id}",
        source_type="price",
        source_name=entry.name,
        file_path=manager.file_path(entry),
        force=True,
    )
    return {
        "entry": {
            "id": entry.id,
            "name": entry.name,
            "supplier": entry.supplier,
            "items_count": entry.items_count,
            "updated_at": entry.updated_at,
        },
        "total_price_items": total_items,
        "rag": rag,
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
        _doc_rag_index_service().remove_document(f"{config.source_id}:main")
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
    _doc_rag_index_service().remove_document(f"price:{entry.id}")
    return {
        "removed_id": entry.id,
        "removed_name": entry.name,
        "total_price_items": total_items,
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn

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

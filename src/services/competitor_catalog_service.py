from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx

from src.config import (
    COMPETITOR_INDEX_MAX_URLS,
    COMPETITOR_INDEX_REQUEST_TIMEOUT,
    COMPETITOR_INDEX_WORKERS,
    COMPETITOR_SEARCH_FALLBACK_THRESHOLD,
    WEB_SEARCH_TIMEOUT,
)
from src.services.competitor_sites import (
    CompetitorSite,
    competitor_label_for_url,
    competitor_sites_with_search,
    is_competitor_product_page_url,
)
from src.services.data_loader import normalize_name
from src.services.fuzzy_scoring import name_match_score
from src.services.models import PriceQuote
from src.services.web_search_service import (
    PRICE_ON_REQUEST_LABEL,
    extract_prices_from_text,
    price_on_request_label,
)

logger = logging.getLogger(__name__)

# Минимум товаров в store, чтобы считать sitemap-каталог полностью проиндексированным.
_SITEMAP_CATALOG_MIN_PRODUCTS: dict[str, int] = {
    "vrtorg.ru": 500,
    "labkabinet.ru": 1000,
    "stronikum.ru": 100,
    "td-school.ru": 100,
    "n-72.ru": 1000,
    "epp24.ru": 1000,
    "zarnitza.ru": 500,
}

_TD_SCHOOL_DEFAULT_SAMPLE_URL = "https://td-school.ru/index.php?page=100"

_reindex_jobs: dict[str, dict[str, object]] = {}
_reindex_lock = threading.Lock()
_index_logs: dict[str, list[dict[str, object]]] = {}
_index_log_lock = threading.Lock()

_INDEX_PHASE_LABELS: dict[str, str] = {
    "starting": "Запуск индексации…",
    "analyze": "Изучаю структуру сайта…",
    "sample": "Индексирую образец карточки товара…",
    "sitemap": "Поиск карты сайта (sitemap)…",
    "catalog": "Индексация каталога товаров…",
    "rag": "Сохранение в базу данных и RAG…",
    "done": "Индексация завершена",
}


def clear_index_logs(domain: str) -> None:
    normalized = domain.lower().removeprefix("www.")
    with _index_log_lock:
        _index_logs[normalized] = []


def append_index_log(domain: str, message: str, *, level: str = "info") -> None:
    normalized = domain.lower().removeprefix("www.")
    entry: dict[str, object] = {
        "id": 0,
        "ts": time.time(),
        "level": level,
        "message": message.strip(),
    }
    with _index_log_lock:
        rows = _index_logs.setdefault(normalized, [])
        entry["id"] = len(rows) + 1
        rows.append(entry)
        if len(rows) > 500:
            del rows[: len(rows) - 500]


def get_index_logs(domain: str, *, since: int = 0) -> list[dict[str, object]]:
    normalized = domain.lower().removeprefix("www.")
    with _index_log_lock:
        rows = list(_index_logs.get(normalized, []))
    if since > 0:
        rows = [row for row in rows if int(row.get("id", 0)) > since]
    return rows


def get_index_phase_label(phase: str | None) -> str:
    if not phase:
        return "Индексация…"
    return _INDEX_PHASE_LABELS.get(phase, "Индексация…")


def _set_index_phase(normalized: str, phase: str) -> None:
    with _reindex_lock:
        job = _reindex_jobs.get(normalized)
        if job:
            job["phase"] = phase

_CLASS_ATTR_RE = re.compile(r'class="([^"]+)"', re.I)
_ID_ATTR_RE = re.compile(r'id="([^"]+)"', re.I)
_ITEMPROP_ATTR_RE = re.compile(r'itemprop="([^"]+)"', re.I)
_SITEMAP_LOC_RE = re.compile(r"<loc>\s*(https?://[^<]+)\s*</loc>", re.I)


@dataclass
class CompetitorParsingHints:
    product_sample_url: str = ""
    price_html_hint: str = ""
    articul_html_hint: str = ""


_domain_parsing_hints: dict[str, CompetitorParsingHints] = {}


def set_domain_parsing_hints(
    domain: str,
    *,
    product_sample_url: str = "",
    price_html_hint: str = "",
    articul_html_hint: str = "",
) -> None:
    normalized = domain.lower().removeprefix("www.")
    if not any((product_sample_url, price_html_hint, articul_html_hint)):
        _domain_parsing_hints.pop(normalized, None)
        return
    _domain_parsing_hints[normalized] = CompetitorParsingHints(
        product_sample_url=product_sample_url.strip(),
        price_html_hint=price_html_hint.strip(),
        articul_html_hint=articul_html_hint.strip(),
    )


def get_domain_parsing_hints(domain: str) -> CompetitorParsingHints | None:
    normalized = domain.lower().removeprefix("www.")
    return _domain_parsing_hints.get(normalized)


def apply_parsing_hints_from_entry(entry) -> None:
    if not entry:
        return
    set_domain_parsing_hints(
        entry.domain,
        product_sample_url=entry.product_sample_url or "",
        price_html_hint=entry.price_html_hint or "",
        articul_html_hint=entry.articul_html_hint or "",
    )


def _hint_attr_tokens(hint: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for match in _CLASS_ATTR_RE.finditer(hint):
        for cls in match.group(1).split():
            cleaned = cls.strip()
            if cleaned:
                tokens.append(("class", cleaned))
    id_match = _ID_ATTR_RE.search(hint)
    if id_match:
        tokens.append(("id", id_match.group(1).strip()))
    for match in _ITEMPROP_ATTR_RE.finditer(hint):
        tokens.append(("itemprop", match.group(1).strip()))
    return tokens


def _extract_text_by_hint(html: str, hint: str) -> str | None:
    hint = hint.strip()
    if not hint or not html:
        return None

    for token_type, token_value in _hint_attr_tokens(hint):
        if token_type == "class":
            open_match = re.search(
                rf'<[^>]+class="[^"]*\b{re.escape(token_value)}\b[^"]*"[^>]*>',
                html,
                re.I,
            )
            if open_match:
                chunk = html[open_match.start() : open_match.start() + 1200]
                text = _strip_html_text(chunk)
                if text:
                    return text
            pattern = rf'class="[^"]*\b{re.escape(token_value)}\b[^"]*"[^>]*>(?P<body>.*?)</'
            match = re.search(pattern, html, re.I | re.S)
            if match:
                text = _strip_html_text(match.group("body"))
                if text:
                    return text
        elif token_type == "id":
            pattern = rf'id="{re.escape(token_value)}"[^>]*>(?P<body>.*?)</'
            match = re.search(pattern, html, re.I | re.S)
            if match:
                text = _strip_html_text(match.group("body"))
                if text:
                    return text
        elif token_type == "itemprop":
            pattern = rf'itemprop="{re.escape(token_value)}"[^>]*(?:content="(?P<content>[^"]+)"|>(?P<body>[^<]+))'
            match = re.search(pattern, html, re.I | re.S)
            if match:
                text = (match.group("content") or match.group("body") or "").strip()
                if text:
                    return text

    compact_hint = re.sub(r"\s+", " ", hint)
    compact_html = re.sub(r"\s+", " ", html)
    if len(compact_hint) >= 12 and compact_hint in compact_html:
        start = compact_html.index(compact_hint)
        tail = compact_html[start : start + len(compact_hint) + 120]
        text = _strip_html_text(tail)
        if text:
            return text
    return None


def _parse_price_from_hint(html: str, hint: str) -> float | None:
    text = _extract_text_by_hint(html, hint)
    if not text:
        return None
    prices = extract_prices_from_text(text)
    return prices[0] if prices else _parse_price(text)


def _parse_articul_from_hint(html: str, hint: str) -> str | None:
    text = _extract_text_by_hint(html, hint)
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = re.sub(r"^(арт\.?|артикул|sku|код)\s*[:\-]?\s*", "", cleaned, flags=re.I)
    return cleaned[:80] or None


def _infer_product_path_prefix(sample_url: str) -> str | None:
    path = urlparse(sample_url).path.rstrip("/")
    if not path:
        return None
    markers = (
        "/catalog/product/",
        "/magazin/product/",
        "/product/",
        "/tovar/",
        "/goods/",
        "/item/",
        "/card/",
    )
    lower = path.lower()
    for marker in markers:
        if marker in lower:
            idx = lower.index(marker)
            return path[: idx + len(marker)]
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 2:
        return "/" + "/".join(segments[:-1]) + "/"
    return None


def _is_query_param_product_url(url: str) -> bool:
    parsed = urlparse(url)
    page_values = parse_qs(parsed.query).get("page", [])
    if not page_values:
        return False
    page_id = page_values[0].strip()
    return page_id.isdigit() and int(page_id) > 0


def _is_product_page_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    if _is_product_detail_path(path):
        return True
    if _is_query_param_product_url(url):
        return True
    if is_competitor_product_page_url(url):
        return True
    return False


def _canonical_td_school_product_url(domain: str, page_id: str) -> str:
    normalized = domain.lower().removeprefix("www.")
    return f"https://{normalized}/index.php?page={page_id}"


_TD_SCHOOL_PRODUCT_HREF_RE = re.compile(
    r'href=["\']([^"\']*?(?:index\.php\?page=(\d+)|[?&]page=(\d+))[^"\']*)["\']',
    re.I,
)


def _url_matches_product_pattern(url: str, sample_url: str | None) -> bool:
    if _is_query_param_product_url(url):
        return True
    if is_competitor_product_page_url(url):
        return True
    if not sample_url:
        return False
    if _is_query_param_product_url(sample_url):
        return _is_query_param_product_url(url)
    prefix = _infer_product_path_prefix(sample_url)
    if not prefix:
        return False
    path = urlparse(url).path
    return path.startswith(prefix) and path.rstrip("/") != prefix.rstrip("/")


def _discover_generic_sitemap_product_urls(
    domain: str,
    sample_url: str | None = None,
    *,
    limit: int | None = None,
) -> list[str]:
    resolved_limit = COMPETITOR_INDEX_MAX_URLS if limit is None else limit
    normalized = domain.lower().removeprefix("www.")
    root = f"https://{normalized}"
    queue = [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"]
    seen_sitemaps: set[str] = set()
    product_urls: list[str] = []
    seen_products: set[str] = set()

    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            while queue and len(product_urls) < resolved_limit:
                sitemap_url = queue.pop(0)
                if sitemap_url in seen_sitemaps:
                    continue
                seen_sitemaps.add(sitemap_url)
                try:
                    response = client.get(sitemap_url)
                    response.raise_for_status()
                except Exception:
                    continue
                body = response.text[:2_000_000]
                locs = _SITEMAP_LOC_RE.findall(body)
                for loc in locs:
                    clean = loc.strip().split("#")[0]
                    if not clean:
                        continue
                    if clean.endswith(".xml") and normalized in urlparse(clean).netloc.lower():
                        if clean not in seen_sitemaps:
                            queue.append(clean)
                        continue
                    if normalized not in urlparse(clean).netloc.lower().removeprefix("www."):
                        continue
                    if not _url_matches_product_pattern(clean, sample_url):
                        continue
                    if clean in seen_products:
                        continue
                    seen_products.add(clean)
                    product_urls.append(clean)
                    if len(product_urls) >= resolved_limit:
                        break
    except Exception:
        logger.exception("Failed to discover sitemap for %s", domain)

    return product_urls


def _format_index_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return ""
    total = int(seconds)
    if total < 60:
        return f", ~{total} сек"
    minutes = total // 60
    if minutes < 120:
        return f", ~{minutes} мин"
    hours = minutes // 60
    return f", ~{hours} ч {minutes % 60} мин"


def _fetch_single_product_page(
    site: CompetitorSite,
    product_url: str,
    *,
    request_timeout: float | httpx.Timeout | None = None,
) -> list[CompetitorCatalogProduct]:
    timeout = request_timeout if request_timeout is not None else COMPETITOR_INDEX_REQUEST_TIMEOUT
    response = None
    for attempt in range(3):
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
            ) as client:
                response = client.get(product_url)
                if response.status_code in {429, 502, 503, 504} and attempt < 2:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                response.raise_for_status()
                return parse_catalog_html(
                    response.text[:700_000],
                    domain=site.domain,
                    site_label=site.label,
                    page_url=str(response.url),
                )
        except Exception:
            if attempt >= 2:
                logger.debug("Product fetch failed %s", product_url, exc_info=True)
                return []
            time.sleep(0.6 * (attempt + 1))
    return []


def _fetch_products_from_url_list(
    site: CompetitorSite,
    product_urls: list[str],
    *,
    checkpoint_every: int = 500,
    request_timeout: float | httpx.Timeout | None = None,
    max_workers: int | None = None,
) -> list[CompetitorCatalogProduct]:
    from src.services.competitor_product_store import get_competitor_product_store

    if not product_urls:
        return []

    products: list[CompetitorCatalogProduct] = []
    seen_names: set[str] = set()
    seen_url_keys: set[str] = set()
    state_lock = threading.Lock()
    total = len(product_urls)
    store = get_competitor_product_store() if checkpoint_every > 0 else None
    workers = min(max_workers or COMPETITOR_INDEX_WORKERS, total)
    logger.info("%s: indexing %s product URLs with %s workers", site.domain, total, workers)
    append_index_log(
        site.domain,
        f"Загрузка {total} страниц товаров ({workers} параллельных потоков)…",
    )

    started_at = time.time()
    completed = 0
    progress_step = max(1, min(25, total // 40 or 1))

    def _report_progress(current_completed: int, current_products: int) -> None:
        if current_completed != 1 and current_completed % progress_step != 0 and current_completed != total:
            return
        elapsed = max(time.time() - started_at, 0.001)
        rate = current_completed / elapsed
        remaining = total - current_completed
        eta = _format_index_eta(remaining / rate if rate > 0 else 0)
        append_index_log(
            site.domain,
            f"  → {current_completed}/{total} URL, товаров: {current_products}{eta}",
        )

    def _merge_page_products(page_products: list[CompetitorCatalogProduct]) -> None:
        nonlocal completed
        from src.services.competitor_catalog_db import product_dedup_key

        with state_lock:
            for item in page_products:
                name_key = normalize_name(item.name)
                url_key = product_dedup_key(item)
                if name_key in seen_names or url_key in seen_url_keys:
                    continue
                seen_names.add(name_key)
                seen_url_keys.add(url_key)
                products.append(item)
            completed += 1
            current_completed = completed
            current_products = len(products)

        _report_progress(current_completed, current_products)

        if store and checkpoint_every > 0 and current_products and (
            current_completed % checkpoint_every == 0 or current_completed == total
        ):
            with state_lock:
                snapshot = list(products)
            saved = store.replace_site_products(
                site.domain,
                snapshot,
                site_label=site.label,
            )
            logger.info(
                "%s checkpoint %s/%s urls, %s products saved",
                site.domain,
                current_completed,
                total,
                saved,
            )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _fetch_single_product_page,
                site,
                product_url,
                request_timeout=request_timeout,
            )
            for product_url in product_urls
        ]
        for future in as_completed(futures):
            try:
                page_products = future.result()
            except Exception:
                logger.debug("Product future failed for %s", site.domain, exc_info=True)
                with state_lock:
                    completed += 1
                    current_completed = completed
                    current_products = len(products)
                _report_progress(current_completed, current_products)
                continue
            _merge_page_products(page_products)

    logger.info("%s catalog complete: %s products", site.domain, len(products))
    append_index_log(site.domain, f"Сбор завершён: {len(products)} товаров")
    return products


def site_catalog_looks_complete(domain: str, product_count: int) -> bool:
    normalized = domain.lower().removeprefix("www.")
    min_products = _SITEMAP_CATALOG_MIN_PRODUCTS.get(normalized)
    if min_products is None:
        return product_count > 0
    return product_count >= min_products


def get_reindex_job(domain: str) -> dict[str, object] | None:
    normalized = domain.lower().removeprefix("www.")
    job = _reindex_jobs.get(normalized)
    return dict(job) if job else None


def list_reindex_jobs() -> dict[str, dict[str, object]]:
    return {domain: dict(job) for domain, job in _reindex_jobs.items()}


def start_site_reindex_background(
    domain: str,
    doc_rag_index,
    *,
    force: bool = True,
    site: CompetitorSite | None = None,
    extra_urls: list[str] | None = None,
) -> dict[str, object]:
    normalized = domain.lower().removeprefix("www.")
    with _reindex_lock:
        job = _reindex_jobs.get(normalized)
        if job and job.get("running"):
            return {
                "started": False,
                "running": True,
                "domain": normalized,
                "message": "Индексация уже выполняется",
                **job,
            }
        _reindex_jobs[normalized] = {
            "domain": normalized,
            "running": True,
            "started_at": time.time(),
            "force": force,
        }

    def _run() -> None:
        try:
            resolved_site = site
            if resolved_site is None:
                resolved_site = next(
                    (
                        item
                        for item in competitor_sites_with_search()
                        if item.domain.lower().removeprefix("www.") == normalized
                    ),
                    None,
                )
            if resolved_site is None:
                _reindex_jobs[normalized] = {
                    "domain": normalized,
                    "running": False,
                    "error": f"Сайт {normalized} не найден в списке конкурентов",
                    "finished_at": time.time(),
                }
                return

            logger.info("Background reindex started for %s force=%s", normalized, force)
            append_index_log(normalized, f"Индексация каталога {normalized}…")
            _set_index_phase(normalized, "catalog")
            result = index_competitor_site_catalog(
                resolved_site,
                doc_rag_index,
                force=force,
                extra_urls=extra_urls,
            )
            sync_unified_competitor_rag(doc_rag_index)
            from src.services.competitor_product_store import get_competitor_product_store
            from src.services.competitor_site_manager import get_competitor_site_manager

            store_stats = get_competitor_product_store().stats()
            get_competitor_site_manager().mark_draft_indexed(normalized, result)
            products_count = result.get("products") or result.get("store_products") or 0
            append_index_log(
                normalized,
                f"Индексация завершена. Товаров: {products_count}",
                level="success",
            )
            _reindex_jobs[normalized] = {
                "domain": normalized,
                "running": False,
                "phase": "done",
                "started_at": _reindex_jobs[normalized]["started_at"],
                "finished_at": time.time(),
                "result": result,
                "catalog_products": store_stats,
            }
            logger.info(
                "Background reindex finished for %s products=%s",
                normalized,
                store_stats.get("by_domain", {}).get(normalized, 0),
            )
        except Exception as exc:
            logger.exception("Background reindex failed for %s", normalized)
            append_index_log(normalized, f"Ошибка индексации: {exc}", level="error")
            _reindex_jobs[normalized] = {
                "domain": normalized,
                "running": False,
                "phase": "error",
                "error": str(exc),
                "finished_at": time.time(),
            }

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"reindex-{normalized}",
    ).start()
    return {
        "started": True,
        "running": True,
        "domain": normalized,
        "force": force,
        "message": (
            f"Запущена фоновая индексация {normalized}. "
            "Проверяйте статус: GET /api/competitors/reindex/status?domain="
            f"{normalized}"
        ),
    }


def start_competitor_site_index_background(
    *,
    url: str,
    label: str,
    product_sample_url: str | None,
    price_html_hint: str | None,
    articul_html_hint: str | None,
    doc_rag_index,
) -> dict[str, object]:
    from src.services.competitor_site_manager import get_competitor_site_manager

    manager = get_competitor_site_manager()
    normalized_url = manager.normalize_url(url)
    domain = manager.domain_from_url(normalized_url)
    normalized = domain.lower().removeprefix("www.")

    from src.services.competitor_sites import get_builtin_competitor_site

    is_builtin = get_builtin_competitor_site(domain) is not None

    with _reindex_lock:
        job = _reindex_jobs.get(normalized)
        if job and job.get("running"):
            return {
                "started": False,
                "running": True,
                "domain": normalized,
                "message": "Индексация уже выполняется",
                **job,
            }
        clear_index_logs(normalized)
        append_index_log(normalized, "Запуск индексации…")
        _reindex_jobs[normalized] = {
            "domain": normalized,
            "running": True,
            "phase": "starting",
            "started_at": time.time(),
            "is_builtin": is_builtin,
        }

    def _run() -> None:
        analysis: dict[str, object] = {}
        try:
            append_index_log(normalized, "Изучаю структуру сайта…")
            _set_index_phase(normalized, "analyze")
            draft, analysis = manager.prepare_index_draft(
                url,
                label=label,
                product_sample_url=product_sample_url,
                price_html_hint=price_html_hint,
                articul_html_hint=articul_html_hint,
            )
            if draft.builtin:
                append_index_log(
                    normalized,
                    "Встроенный сайт — выполняется обновление каталога товаров",
                )
            append_index_log(normalized, f"Домен: {draft.domain}")
            title = analysis.get("title")
            if title:
                append_index_log(normalized, f"Заголовок: {title}")
            search_url = analysis.get("search_url")
            if search_url:
                append_index_log(normalized, f"Поиск на сайте: {search_url}")
            notes = analysis.get("notes")
            if notes:
                append_index_log(normalized, str(notes))

            set_domain_parsing_hints(
                draft.domain,
                product_sample_url=draft.product_sample_url or "",
                price_html_hint=draft.price_html_hint or "",
                articul_html_hint=draft.articul_html_hint or "",
            )
            if draft.price_html_hint:
                append_index_log(normalized, "Подсказка HTML для цены сохранена")
            if draft.articul_html_hint:
                append_index_log(normalized, "Подсказка HTML для артикула сохранена")

            from src.services.competitor_sites import get_builtin_competitor_site

            builtin_site = get_builtin_competitor_site(draft.domain) if draft.builtin else None
            site = builtin_site or CompetitorSite(
                domain=draft.domain,
                label=draft.label,
                search_url=draft.search_url,
            )
            extra_urls: list[str] = []
            if draft.product_sample_url:
                extra_urls.append(draft.product_sample_url)
                append_index_log(
                    normalized,
                    f"Индексирую образец карточки: {draft.product_sample_url}",
                )
                _set_index_phase(normalized, "sample")
                index_competitor_page_url(
                    draft.product_sample_url,
                    domain=draft.domain,
                    site_label=draft.label,
                    doc_rag_index=doc_rag_index,
                )
                append_index_log(normalized, "Образец карточки проиндексирован")

            append_index_log(normalized, "Поиск sitemap.xml…")
            _set_index_phase(normalized, "sitemap")
            sitemap_urls = _discover_generic_sitemap_product_urls(
                draft.domain,
                draft.product_sample_url,
            )
            append_index_log(normalized, f"Найдено товарных URL в sitemap: {len(sitemap_urls)}")
            if len(sitemap_urls) >= COMPETITOR_INDEX_MAX_URLS:
                append_index_log(
                    normalized,
                    f"Достигнут лимит {COMPETITOR_INDEX_MAX_URLS} URL "
                    "(увеличьте COMPETITOR_INDEX_MAX_URLS в .env для полного каталога)",
                )

            append_index_log(normalized, "Индексация каталога товаров…")
            _set_index_phase(normalized, "catalog")
            catalog = index_competitor_site_catalog(
                site,
                doc_rag_index,
                force=True,
                extra_urls=extra_urls or None,
            )
            products_count = catalog.get("products") or catalog.get("store_products") or 0
            append_index_log(normalized, f"Товаров в каталоге: {products_count}")
            if catalog.get("skipped"):
                append_index_log(normalized, "Каталог уже был проиндексирован — данные обновлены")

            append_index_log(normalized, "Сохранение в базу данных…")
            _set_index_phase(normalized, "rag")
            manager.mark_draft_indexed(draft.domain, catalog)

            from src.services.competitor_product_store import get_competitor_product_store

            store_stats = get_competitor_product_store().stats()
            append_index_log(
                normalized,
                f"Индексация завершена. Товаров: {products_count}",
                level="success",
            )
            _reindex_jobs[normalized] = {
                "domain": normalized,
                "running": False,
                "phase": "done",
                "started_at": _reindex_jobs[normalized]["started_at"],
                "finished_at": time.time(),
                "result": catalog,
                "analysis": analysis,
                "catalog_products": store_stats,
                "is_builtin": draft.builtin,
            }
        except Exception as exc:
            logger.exception("Competitor site index failed for %s", normalized)
            append_index_log(normalized, f"Ошибка: {exc}", level="error")
            _reindex_jobs[normalized] = {
                "domain": normalized,
                "running": False,
                "phase": "error",
                "error": str(exc),
                "analysis": analysis,
                "finished_at": time.time(),
            }

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"index-{normalized}",
    ).start()
    return {
        "started": True,
        "running": True,
        "domain": normalized,
        "phase": "starting",
        "is_builtin": is_builtin,
    }


_CATALOG_SEED_URLS: dict[str, list[str]] = {
    "skale.ru": [
        "https://skale.ru/magazin",
        "https://skale.ru/magazin/folder/uchebnoe-oborudovanie-po-astronomii-i-astrofizike",
        "https://skale.ru/prays-list",
    ],
    "xn----7sbbumkojddmeoc1a7r.xn--p1acf": [
        "https://xn----7sbbumkojddmeoc1a7r.xn--p1acf/products/",
    ],
    "n-72.ru": [
        "https://n-72.ru/catalog/",
        "https://n-72.ru/sitemap.xml",
    ],
    "stronikum.ru": [
        "https://stronikum.ru/prices",
        "https://stronikum.ru/sitemap.xml",
    ],
    "labkabinet.ru": [
        "https://labkabinet.ru/catalog/",
        "https://labkabinet.ru/sitemap.xml",
    ],
    "vrtorg.ru": [
        "https://vrtorg.ru/catalog/",
        "https://vrtorg.ru/sitemap.xml",
    ],
    "td-school.ru": [
        "https://td-school.ru/",
        "https://td-school.ru/index.php",
    ],
    "epp24.ru": [
        "https://epp24.ru/sitemap_index.xml",
        "https://epp24.ru/",
    ],
    "zarnitza.ru": [
        "https://zarnitza.ru/catalog/",
        "https://zarnitza.ru/sitemap.xml",
    ],
}

_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_ARTICUL_RE = re.compile(r"Артикул:\s*(?:<span>)?([^\s<]+)", re.I)
_N72_SITEMAP_PRODUCT_RE = re.compile(
    r"<loc>(https://n-72\.ru/catalog/product/[^<]+)</loc>",
    re.I,
)
_N72_PREVIEW_RE = re.compile(
    r'href="(?P<url>/catalog/product/[^"]+)".*?'
    r'class="n72r-product-preview__title">(?P<name>[^<]+)</a>.*?'
    r'class="price_value">(?P<price>[^<]+)</span>',
    re.I | re.S,
)
_PRODUCT_LINE_RE = re.compile(
    r"^\[product\]\s*domain=(?P<domain>[^|]+)\s*\|\s*site=(?P<site>[^|]+)\s*\|"
    r"\s*name=(?P<name>[^|]+)\s*\|\s*price=(?P<price>[^|]*)\s*\|"
    r"\s*url=(?P<url>[^|]*)\s*\|\s*articul=(?P<articul>[^|]*)(?:\s*\|\s*price_label=(?P<price_label>[^|]*))?"
    r"(?:\s*\|\s*wholesale_price=(?P<wholesale_price>[^|]*))?"
    r"(?:\s*\|\s*image_url=(?P<image_url>[^|]*))?"
    r"(?:\s*\|\s*details=(?P<details>[^|]*))?"
    r"(?:\s*\|\s*description=(?P<description>.*))?",
    re.I,
)


@dataclass
class CompetitorCatalogProduct:
    domain: str
    site_label: str
    name: str
    price: float | None
    url: str | None
    articul: str | None = None
    price_label: str | None = None
    details: str | None = None
    wholesale_price: float | None = None
    image_url: str | None = None
    description: str | None = None


_STRONIKUM_PRODUCT_PATH_RE = re.compile(r"^/\d+_[^/]+/\d+_", re.I)
_STRONIKUM_SITEMAP_PRODUCT_RE = re.compile(
    r"<loc>(https://stronikum\.ru/\d+_[^/]+/\d+_[^<]+)</loc>",
    re.I,
)
_STRONIKUM_CATEGORY_ROW_RE = re.compile(
    r'href="(?P<url>(?:/\d+_[^/]+/)?\d+_[^"]+)"[^>]*>(?P<name>[^<]+)</a>.*?'
    r'<td[^>]*(?:align="right")?[^>]*>(?P<price>\d[\d\s]*)\s*(?:р\.|</td>)',
    re.I | re.S,
)


def _strip_html_text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html)).strip()


def _canonical_stronikum_product_url(url: str) -> str:
    clean = url.split("#")[0].split("?")[0]
    if clean.endswith(".modal"):
        clean = clean[:-6]
    return clean


def _is_stronikum_product_path(path: str) -> bool:
    return bool(_STRONIKUM_PRODUCT_PATH_RE.match(path.rstrip("/")))


def _parse_stronikum_product_html(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> CompetitorCatalogProduct | None:
    canonical_url = _canonical_stronikum_product_url(page_url)
    path = urlparse(canonical_url).path
    if not _is_stronikum_product_path(path):
        return None

    name_match = re.search(
        r'<h1[^>]*itemprop="name"[^>]*>(?P<name>[^<]+)',
        html,
        re.I | re.S,
    )
    if not name_match:
        name_match = re.search(r'itemprop="name">(?P<name>[^<]+)', html, re.I)
    if not name_match:
        return None

    name = re.sub(r"\s+", " ", name_match.group("name")).strip()
    if len(name) < 4:
        return None

    articul = None
    articul_match = re.search(
        r'class="product-code"[^>]*>.*?Артикул:\s*(\d+)',
        html,
        re.I | re.S,
    )
    if articul_match:
        articul = articul_match.group(1)
    else:
        body_match = re.search(r'class="product-body"[^>]*id="(\d+)"', html, re.I)
        if body_match:
            articul = body_match.group(1)

    price = None
    retail_match = re.search(r"Цена:\s*(\d[\d\s]*)\s*RUB", html, re.I)
    if retail_match:
        price = _parse_price(retail_match.group(1))

    wholesale_price = None
    wholesale_match = re.search(
        r'itemprop="offers"[^>]*>.*?itemprop="price">(\d+)',
        html,
        re.I | re.S,
    )
    if wholesale_match:
        wholesale_price = float(wholesale_match.group(1))

    details = None
    desc_match = re.search(
        r'itemprop="description"[^>]*>(?P<body>.*?)</div>',
        html,
        re.I | re.S,
    )
    if desc_match:
        details = _strip_html_text(desc_match.group("body"))[:800] or None

    return CompetitorCatalogProduct(
        domain=domain,
        site_label=site_label,
        name=name[:300],
        price=price,
        url=canonical_url,
        articul=articul,
        details=details,
        wholesale_price=wholesale_price,
    )


def _parse_stronikum_category_or_search_rows(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if 'class="product-row"' not in html and "price-products" not in html:
        return []

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    for match in _STRONIKUM_CATEGORY_ROW_RE.finditer(html):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        url = _absolute_url(domain, match.group("url"), page_url)
        if not url or not _is_stronikum_product_path(urlparse(url).path):
            continue
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=None,
                wholesale_price=_parse_price(match.group("price")),
                url=_canonical_stronikum_product_url(url),
                articul=None,
            )
        )
    return products


def _is_labkabinet_product_path(path: str) -> bool:
    return bool(re.match(r"^/product/[^/]+/?$", path.rstrip("/"), re.I))


def _labkabinet_product_html_slice(html: str) -> str:
    """Labkabinet puts h1/price/articul late in HTML (~650k+); slice around product block."""
    for marker in ("<h1", 'itemprop="name"'):
        idx = html.find(marker)
        if idx >= 0:
            return html[max(0, idx - 2_000) : idx + 25_000]
    if len(html) > 100_000:
        return html[-100_000:]
    return html


def _parse_labkabinet_product_html(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> CompetitorCatalogProduct | None:
    path = urlparse(page_url.split("#")[0]).path
    if not _is_labkabinet_product_path(path):
        return None

    title_match = re.search(r"<h1[^>]*>\s*(?P<name>[^<]+?)\s*</h1>", html, re.I | re.S)
    if not title_match:
        title_match = re.search(
            r'itemprop="name"[^>]*content="(?P<name>[^"]+)"',
            html,
            re.I,
        )
    if not title_match:
        title_match = re.search(r'itemprop="name"[^>]*>\s*(?P<name>[^<]+?)\s*</', html, re.I)
    if not title_match:
        return None

    name = unescape(re.sub(r"\s+", " ", title_match.group("name")).strip())
    if len(name) < 4:
        return None

    focused = _focus_product_price_html(html)
    price = None
    price_attr = re.search(
        r'class="price"[^>]*data-value="(\d[\d\s.]*)"',
        focused,
        re.I,
    )
    if price_attr:
        price = _parse_price(price_attr.group(1))
    if price is None:
        price = _extract_primary_product_price(focused)

    articul = None
    articul_match = re.search(
        r'class="block_articule"[^>]*>.*?Артикул:\s*([A-Za-z0-9\-_.]+)',
        html,
        re.I | re.S,
    )
    if not articul_match:
        articul_match = _ARTICUL_RE.search(html)

    details = None
    desc_match = re.search(
        r'itemprop="description"[^>]*content="(?P<body>[^"]+)"',
        html,
        re.I,
    )
    if desc_match:
        details = _strip_html_text(desc_match.group("body"))[:800] or None
    if not details:
        desc_match = re.search(
            r'itemprop="description"[^>]*>(?P<body>.*?)</div>',
            html,
            re.I | re.S,
        )
        if desc_match:
            details = _strip_html_text(desc_match.group("body"))[:800] or None

    price_label = price_on_request_label(focused) if price is None else None
    return CompetitorCatalogProduct(
        domain=domain,
        site_label=site_label,
        name=name[:300],
        price=price,
        url=page_url.split("#")[0],
        articul=articul_match.group(1) if articul_match else None,
        price_label=price_label,
        details=details,
    )


def _parse_labkabinet_catalog_items(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if "item-title" not in html and "price_value" not in html:
        return []

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'href="(?P<url>/product/[^"]+)".*?'
        r'class="item-title"[^>]*>\s*(?P<name>[^<]+?)\s*</.*?'
        r'class="price_value">(?P<price>[^<]+)</span>',
        re.I | re.S,
    )
    for match in pattern.finditer(html):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        url = _absolute_url(domain, match.group("url"), page_url)
        if not url or not _is_labkabinet_product_path(urlparse(url).path):
            continue
        chunk = html[match.start() : match.start() + 2500]
        articul_match = _ARTICUL_RE.search(chunk)
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=_parse_price(match.group("price")),
                url=url,
                articul=articul_match.group(1) if articul_match else None,
            )
        )
    return products


def _parse_labkabinet_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if domain.lower().removeprefix("www.") != "labkabinet.ru":
        return []

    product = _parse_labkabinet_product_html(
        _labkabinet_product_html_slice(html),
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if product:
        return [product]

    return _parse_labkabinet_catalog_items(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )


def _discover_labkabinet_product_urls() -> list[str]:
    index_url = "https://labkabinet.ru/sitemap.xml"
    product_re = re.compile(
        r"<loc>(https://labkabinet\.ru/product/[^<]+)</loc>",
        re.I,
    )
    sitemap_link_re = re.compile(
        r"<loc>(https://labkabinet\.ru/sitemap[^<]*\.xml)</loc>",
        re.I,
    )
    urls: list[str] = []
    seen: set[str] = set()

    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            index_response = client.get(index_url)
            index_response.raise_for_status()
            sitemap_urls = sitemap_link_re.findall(index_response.text)
            if not sitemap_urls:
                sitemap_urls = ["https://labkabinet.ru/sitemap-iblock-2.xml"]

            for sitemap_url in sitemap_urls:
                try:
                    response = client.get(sitemap_url)
                    response.raise_for_status()
                except Exception:
                    continue
                for url in product_re.findall(response.text):
                    clean = url.split("#")[0].split("?")[0]
                    if clean in seen:
                        continue
                    seen.add(clean)
                    urls.append(clean)
    except Exception:
        logger.exception("Failed to fetch labkabinet sitemap")

    return urls


def fetch_labkabinet_catalog(
    site: CompetitorSite,
    *,
    checkpoint_every: int = 500,
) -> list[CompetitorCatalogProduct]:
    from src.services.competitor_product_store import get_competitor_product_store

    product_urls = _discover_labkabinet_product_urls()
    if not product_urls:
        logger.warning("Labkabinet sitemap empty")
        return []

    products: list[CompetitorCatalogProduct] = []
    seen_names: set[str] = set()
    total = len(product_urls)
    store = get_competitor_product_store() if checkpoint_every > 0 else None
    logger.info("Labkabinet: indexing %s products from sitemap", total)

    with httpx.Client(
        timeout=WEB_SEARCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
    ) as client:
        for index, product_url in enumerate(product_urls, start=1):
            try:
                response = client.get(product_url)
                response.raise_for_status()
            except Exception:
                logger.debug("Labkabinet product fetch failed %s", product_url, exc_info=True)
                continue

            product = _parse_labkabinet_product_html(
                _labkabinet_product_html_slice(response.text),
                domain=site.domain,
                site_label=site.label,
                page_url=str(response.url),
            )
            if not product:
                continue
            key = normalize_name(product.name)
            if key in seen_names:
                continue
            seen_names.add(key)
            products.append(product)

            if store and checkpoint_every > 0 and products and (
                index % checkpoint_every == 0 or index == total
            ):
                saved = store.replace_site_products(
                    site.domain,
                    products,
                    site_label=site.label,
                )
                logger.info(
                    "Labkabinet checkpoint %s/%s urls, %s products saved",
                    index,
                    total,
                    saved,
                )
            elif index % 500 == 0 or index == total:
                logger.info("Labkabinet indexed %s/%s products", index, total)

    logger.info("Labkabinet catalog complete: %s products", len(products))
    return products


_VRTORG_PRODUCT_PATH_RE = re.compile(r"^/catalog/.+/\d+/?$", re.I)
_VRTORG_SITEMAP_PRODUCT_RE = re.compile(
    r"<loc>(https://vrtorg\.ru/catalog/[^<]+/\d+/)</loc>",
    re.I,
)
_VRTORG_SITEMAP_INDEX_RE = re.compile(
    r"<loc>(https://vrtorg\.ru/sitemap[^<]*\.xml)</loc>",
    re.I,
)


def _is_vrtorg_product_path(path: str) -> bool:
    return bool(_VRTORG_PRODUCT_PATH_RE.match(path.rstrip("/")))


def _vrtorg_product_html_slice(html: str) -> str:
    marker_positions: list[int] = []
    for marker in (
        "product-gallery__image",
        "product-buy__price-current",
        'class="product__title"',
        'class="product__code"',
        "<h1",
    ):
        idx = html.find(marker)
        if idx >= 0:
            marker_positions.append(idx)
    if marker_positions:
        start = max(0, min(marker_positions) - 4_000)
        end = max(marker_positions) + 12_000
        return html[start:end]
    if len(html) > 120_000:
        return html[:120_000]
    return html


def _extract_vrtorg_product_image(html: str, *, domain: str, page_url: str) -> str | None:
    for match in re.finditer(r"<img\b[^>]*class=\"product-gallery__image\"[^>]*>", html, re.I | re.S):
        tag = match.group(0)
        src_match = re.search(r'\ssrc="([^"]+)"', tag, re.I)
        if src_match:
            return _absolute_url(domain, src_match.group(1), page_url)
    for match in re.finditer(r"<img\b[^>]*itemprop=\"contentUrl\"[^>]*>", html, re.I | re.S):
        tag = match.group(0)
        if "product-gallery" not in tag.lower():
            continue
        src_match = re.search(r'\ssrc="([^"]+)"', tag, re.I)
        if src_match:
            return _absolute_url(domain, src_match.group(1), page_url)
    return None


def _parse_vrtorg_product_html(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> CompetitorCatalogProduct | None:
    path = urlparse(page_url.split("#")[0]).path
    if not _is_vrtorg_product_path(path):
        return None

    title_match = re.search(
        r'<h1[^>]*class="product__title"[^>]*>\s*(?P<name>[^<]+?)\s*</h1>',
        html,
        re.I | re.S,
    )
    if not title_match:
        title_match = re.search(r"<h1[^>]*>\s*(?P<name>[^<]+?)\s*</h1>", html, re.I | re.S)
    if not title_match:
        return None

    name = unescape(re.sub(r"\s+", " ", title_match.group("name")).strip())
    if len(name) < 4:
        return None

    focused = _vrtorg_product_html_slice(html)
    price = None
    price_match = re.search(
        r'class="product-buy__price-current"[^>]*>(?P<price>[^<]+)',
        focused,
        re.I,
    )
    if price_match:
        price = _parse_price(unescape(price_match.group("price")))
    if price is None:
        price = _extract_primary_product_price(focused)

    articul = None
    articul_match = re.search(
        r'class="product__code"[^>]*>\s*Артикул:\s*(?P<articul>[^<]+)',
        html,
        re.I,
    )
    if not articul_match:
        articul_match = _ARTICUL_RE.search(html)

    image_url = _extract_vrtorg_product_image(html, domain=domain, page_url=page_url)

    price_label = price_on_request_label(focused) if price is None else None
    return CompetitorCatalogProduct(
        domain=domain,
        site_label=site_label,
        name=name[:300],
        price=price,
        url=page_url.split("#")[0],
        articul=articul_match.group("articul").strip() if articul_match else None,
        price_label=price_label,
        image_url=image_url,
    )


def _parse_vrtorg_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if domain.lower().removeprefix("www.") != "vrtorg.ru":
        return []

    product = _parse_vrtorg_product_html(
        _vrtorg_product_html_slice(html),
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    return [product] if product else []


def _discover_vrtorg_product_urls() -> list[str]:
    index_url = "https://vrtorg.ru/sitemap.xml"
    urls: list[str] = []
    seen: set[str] = set()

    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            index_response = client.get(index_url)
            index_response.raise_for_status()
            sitemap_urls = _VRTORG_SITEMAP_INDEX_RE.findall(index_response.text)
            if not sitemap_urls:
                sitemap_urls = ["https://vrtorg.ru/sitemap-iblock-9.xml"]

            for sitemap_url in sitemap_urls:
                if "iblock-9" not in sitemap_url.lower():
                    continue
                try:
                    response = client.get(sitemap_url)
                    response.raise_for_status()
                except Exception:
                    continue
                for url in _VRTORG_SITEMAP_PRODUCT_RE.findall(response.text):
                    clean = url.split("#")[0].split("?")[0]
                    if clean in seen:
                        continue
                    seen.add(clean)
                    urls.append(clean)
    except Exception:
        logger.exception("Failed to fetch vrtorg sitemap")

    return urls


def fetch_vrtorg_catalog(
    site: CompetitorSite,
    *,
    checkpoint_every: int = 500,
) -> list[CompetitorCatalogProduct]:
    from src.services.competitor_product_store import get_competitor_product_store

    product_urls = _discover_vrtorg_product_urls()
    if not product_urls:
        logger.warning("Vrtorg sitemap empty")
        return []

    products: list[CompetitorCatalogProduct] = []
    seen_names: set[str] = set()
    total = len(product_urls)
    store = get_competitor_product_store() if checkpoint_every > 0 else None
    logger.info("Vrtorg: indexing %s products from sitemap", total)

    with httpx.Client(
        timeout=WEB_SEARCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
    ) as client:
        for index, product_url in enumerate(product_urls, start=1):
            response = None
            for attempt in range(3):
                try:
                    response = client.get(product_url)
                    if response.status_code in {429, 502, 503, 504} and attempt < 2:
                        time.sleep(0.6 * (attempt + 1))
                        continue
                    response.raise_for_status()
                    break
                except Exception:
                    if attempt >= 2:
                        logger.debug(
                            "Vrtorg product fetch failed %s",
                            product_url,
                            exc_info=True,
                        )
                        response = None
                    else:
                        time.sleep(0.6 * (attempt + 1))
            if response is None:
                continue

            product = _parse_vrtorg_product_html(
                _vrtorg_product_html_slice(response.text),
                domain=site.domain,
                site_label=site.label,
                page_url=str(response.url),
            )
            if not product:
                continue
            key = normalize_name(product.name)
            if key in seen_names:
                continue
            seen_names.add(key)
            products.append(product)

            if store and checkpoint_every > 0 and products and (
                index % checkpoint_every == 0 or index == total
            ):
                saved = store.replace_site_products(
                    site.domain,
                    products,
                    site_label=site.label,
                )
                logger.info(
                    "Vrtorg checkpoint %s/%s urls, %s products saved",
                    index,
                    total,
                    saved,
                )
            elif index % 500 == 0 or index == total:
                logger.info("Vrtorg indexed %s/%s products", index, total)

    logger.info("Vrtorg catalog complete: %s products", len(products))
    return products


def _parse_stronikum_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if domain.lower().removeprefix("www.") != "stronikum.ru":
        return []

    product = _parse_stronikum_product_html(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if product:
        return [product]

    return _parse_stronikum_category_or_search_rows(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )


def _discover_stronikum_product_urls() -> list[str]:
    sitemap_url = "https://stronikum.ru/sitemap.xml"
    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            response = client.get(sitemap_url)
            response.raise_for_status()
            urls = _STRONIKUM_SITEMAP_PRODUCT_RE.findall(response.text)
            dedup: list[str] = []
            seen: set[str] = set()
            for url in urls:
                clean = _canonical_stronikum_product_url(url)
                if clean in seen:
                    continue
                seen.add(clean)
                dedup.append(clean)
            return dedup
    except Exception:
        logger.exception("Failed to fetch stronikum sitemap")
        return []


def _discover_stronikum_category_urls() -> list[str]:
    menu_url = "https://stronikum.ru/prices"
    category_re = re.compile(r'href="(/(\d+_[^"/]+))"', re.I)
    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            response = client.get(menu_url)
            response.raise_for_status()
            urls: list[str] = []
            seen: set[str] = set()
            for match in category_re.finditer(response.text):
                path = match.group(1)
                if "/" in path.strip("/"):
                    continue
                absolute = f"https://stronikum.ru{path}"
                if absolute in seen:
                    continue
                seen.add(absolute)
                urls.append(absolute)
            return urls
    except Exception:
        logger.exception("Failed to discover stronikum categories")
        return []


def fetch_stronikum_catalog(site: CompetitorSite) -> list[CompetitorCatalogProduct]:
    product_urls = _discover_stronikum_product_urls()
    if not product_urls:
        logger.warning("Stronikum sitemap empty, falling back to category crawl")
        category_urls = _discover_stronikum_category_urls()
        products: list[CompetitorCatalogProduct] = []
        seen_names: set[str] = set()
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            for category_url in category_urls:
                try:
                    response = client.get(category_url)
                    response.raise_for_status()
                except Exception:
                    continue
                for item in _parse_stronikum_category_or_search_rows(
                    response.text[:700_000],
                    domain=site.domain,
                    site_label=site.label,
                    page_url=str(response.url),
                ):
                    key = normalize_name(item.name)
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    products.append(item)
        return products

    products: list[CompetitorCatalogProduct] = []
    seen_names: set[str] = set()
    total = len(product_urls)
    logger.info("Stronikum: indexing %s products from sitemap", total)

    with httpx.Client(
        timeout=WEB_SEARCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
    ) as client:
        for index, product_url in enumerate(product_urls, start=1):
            modal_url = f"{product_url}.modal"
            html = ""
            final_url = product_url
            try:
                response = client.get(modal_url)
                if response.status_code >= 400:
                    response = client.get(product_url)
                response.raise_for_status()
                html = response.text[:200_000]
                final_url = str(response.url)
            except Exception:
                logger.debug("Stronikum product fetch failed %s", product_url, exc_info=True)
                continue

            product = _parse_stronikum_product_html(
                html,
                domain=site.domain,
                site_label=site.label,
                page_url=final_url,
            )
            if not product:
                continue
            key = normalize_name(product.name)
            if key in seen_names:
                continue
            seen_names.add(key)
            products.append(product)

            if index % 250 == 0 or index == total:
                logger.info("Stronikum indexed %s/%s products", index, total)

    logger.info("Stronikum catalog complete: %s products", len(products))
    return products


def _site_root(domain: str) -> str:
    return f"https://{domain.removeprefix('www.')}"


def _absolute_url(domain: str, href: str, base_url: str) -> str:
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return ""
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("http"):
        return href.split("#")[0]
    if href.startswith("/"):
        return f"{_site_root(domain)}{href}"
    return urljoin(base_url.rstrip("/") + "/", href)


def _parse_price(raw: str | None) -> float | None:
    if not raw:
        return None
    text = raw.replace("\xa0", " ").replace("&nbsp;", " ").strip()
    if not text or text in {"—", "-", "none", "null"}:
        return None
    prices = extract_prices_from_text(text)
    if prices:
        return max(prices)
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    try:
        value = float(cleaned.replace(",", "."))
    except ValueError:
        return None
    if 10 <= value <= 50_000_000:
        return round(value, 2)
    return None


def _extract_title_near(href_index: int, html: str) -> str:
    window = html[max(0, href_index - 400) : href_index + 400]
    plain = re.sub(r"\s+", " ", _TAG_RE.sub(" ", window)).strip()
    plain = re.sub(r"Артикул:\s*[A-Za-z0-9\-_.]+", "", plain, flags=re.I)
    plain = re.sub(r"Добавить к сравнению|Купить", "", plain, flags=re.I)
    return plain.strip(" |-")


_PREVIEW_PRODUCT_NAME_RE = re.compile(
    r'itemprop="name"\s+href="(?P<url>[^"]+)"[^>]*>\s*(?P<name>[^<]+?)\s*</a>',
    re.I | re.S,
)


def _focus_product_price_html(html: str) -> str:
    for marker in (
        "js_price_wrapper",
        "offers_price",
        "price_value_block",
        "product-buy__price-current",
        'itemprop="price"',
        "<h1",
    ):
        idx = html.find(marker)
        if idx >= 0:
            return html[idx : idx + 12_000]
    related_idx = html.find("Покупают вместе")
    if related_idx > 0:
        return html[:related_idx]
    return html[:120_000]


def _extract_primary_product_price(html: str) -> float | None:
    focused = _focus_product_price_html(html)
    prices = extract_prices_from_text(focused)
    if prices:
        return min(prices)
    return None


def _is_n72_product_url(url: str) -> bool:
    return "/catalog/product/" in urlparse(url).path.lower()


_N72_SITEMAP_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
_N72_REQUEST_TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)


def _discover_n72_product_urls() -> list[str]:
    sitemap_url = "https://n-72.ru/sitemap.xml"
    limit = COMPETITOR_INDEX_MAX_URLS
    try:
        with httpx.Client(
            timeout=_N72_SITEMAP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            response = client.get(sitemap_url)
            response.raise_for_status()
            body = response.text
    except Exception:
        logger.exception("Failed to load n-72 sitemap")
        return []

    seen: set[str] = set()
    urls: list[str] = []
    for match in _N72_SITEMAP_PRODUCT_RE.finditer(body):
        clean = match.group(1).strip().split("#")[0]
        if clean in seen:
            continue
        seen.add(clean)
        urls.append(clean)
        if len(urls) >= limit:
            break

    logger.info("n-72.ru: discovered %s product URLs in sitemap (limit %s)", len(urls), limit)
    return urls


def _extract_n72_product_image(html: str, *, domain: str, page_url: str) -> str | None:
    for pattern in (
        r'<a[^>]*href="(?P<url>/upload/[^"]+)"[^>]*class="[^"]*popup_link',
        r'class="popup_link[^"]*"[^>]*href="(?P<url>/upload/[^"]+)"',
        r'id="[^"]*_pict"[^>]*src="(?P<url>/upload/[^"]+)"',
        r'itemprop="image"[^>]*(?:src|content)="(?P<url>/upload/[^"]+)"',
    ):
        match = re.search(pattern, html, re.I | re.S)
        if not match:
            continue
        absolute = _absolute_url(domain, match.group("url"), page_url)
        if absolute:
            return absolute
    return None


def _extract_n72_description(html: str) -> str | None:
    desc_match = re.search(
        r'class="info-desc-hight[^"]*"[^>]*>(?P<body>.*?)(?=</div>\s*<div\b|\Z)',
        html,
        re.I | re.S,
    )
    if not desc_match:
        return None
    text = _strip_html_text(desc_match.group("body"))
    if len(text) < 20:
        return None
    return text[:4000]


def _parse_n72_product_html(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> CompetitorCatalogProduct | None:
    if not _is_n72_product_url(page_url):
        return None

    name_match = re.search(r"<h1[^>]*>\s*(?P<name>[^<]+?)\s*</h1>", html, re.I | re.S)
    if not name_match:
        name_match = re.search(
            r'itemprop="name"[^>]*content="(?P<name>[^"]+)"',
            html,
            re.I | re.S,
        )
    if not name_match:
        return None

    name = re.sub(r"\s+", " ", name_match.group("name")).strip()
    if len(name) < 4:
        return None

    price = None
    price_match = re.search(r'class="price_value">(?P<price>[^<]+)</span>', html, re.I | re.S)
    if price_match:
        price = _parse_price(price_match.group("price"))
    if price is None:
        prices = extract_prices_from_text(html[:120_000])
        if prices:
            price = min(prices)

    price_label = price_on_request_label(html[:80_000]) if price is None else None

    return CompetitorCatalogProduct(
        domain=domain,
        site_label=site_label,
        name=name[:300],
        price=price,
        url=page_url.split("#")[0],
        articul=None,
        price_label=price_label,
        image_url=_extract_n72_product_image(html, domain=domain, page_url=page_url),
        description=_extract_n72_description(html),
    )


def _parse_n72_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if domain.lower().removeprefix("www.") != "n-72.ru":
        return []

    if _is_n72_product_url(page_url):
        product = _parse_n72_product_html(
            html,
            domain=domain,
            site_label=site_label,
            page_url=page_url,
        )
        return [product] if product else []

    return _parse_n72_product_previews(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )


def fetch_n72_catalog(
    site: CompetitorSite,
    *,
    checkpoint_every: int = 500,
) -> list[CompetitorCatalogProduct]:
    product_urls = _discover_n72_product_urls()
    if not product_urls:
        append_index_log(site.domain, "Sitemap n-72.ru пуст — обход каталога", level="error")
        return fetch_catalog_products(site, max_pages=12)

    append_index_log(
        site.domain,
        f"Индексация по sitemap n-72.ru: {len(product_urls)} URL",
    )
    return _fetch_products_from_url_list(
        site,
        product_urls,
        checkpoint_every=checkpoint_every,
        request_timeout=_N72_REQUEST_TIMEOUT,
        max_workers=min(8, COMPETITOR_INDEX_WORKERS),
    )


_ZARNITZA_SITEMAP_PRODUCT_RE = re.compile(
    r"<loc>(https://zarnitza\.ru/catalog/[^<]+)</loc>",
    re.I,
)
_ZARNITZA_LISTING_BLOCK_RE = re.compile(
    r'<div class="cart-articul">(?P<body>.*?)</div>\s*<!-- /.cart-articul -->',
    re.I | re.S,
)
_ZARNITZA_CATEGORY_SEEDS: tuple[str, ...] = (
    "https://zarnitza.ru/catalog/",
    "https://zarnitza.ru/catalog/uchlab/",
    "https://zarnitza.ru/catalog/mekhatronika-i-robototekhnika/",
    "https://zarnitza.ru/catalog/meditsina/",
    "https://zarnitza.ru/catalog/podgotovka-professionalnykh-kadrov/",
    "https://zarnitza.ru/catalog/avtogorodki-i-pdd/",
    "https://zarnitza.ru/catalog/avtoshkola-i-avtodrom/",
    "https://zarnitza.ru/catalog/hit/",
    "https://zarnitza.ru/catalog/agrotekhklassy/",
    "https://zarnitza.ru/catalog/oborudovanie-po-prikazu-804-ministerstva-prosveshcheniya-rf/",
)
_ZARNITZA_REQUEST_TIMEOUT = httpx.Timeout(connect=20.0, read=60.0, write=20.0, pool=20.0)
_ZARNITZA_MAX_CRAWL_PAGES = 4000


def _is_zarnitza_domain(domain: str) -> bool:
    return domain.lower().removeprefix("www.") == "zarnitza.ru"


def _is_zarnitza_product_page_html(html: str) -> bool:
    return "main__card-product" in html and 'itemprop="price"' in html


def _normalize_zarnitza_catalog_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.netloc.lower().removeprefix("www.") != "zarnitza.ru":
        return ""
    path = parsed.path or "/"
    if not path.startswith("/catalog"):
        return ""
    clean = f"https://zarnitza.ru{path.rstrip('/')}/"
    if clean == "https://zarnitza.ru/catalog/":
        return clean
    return clean


def _extract_zarnitza_listing_product_urls(html: str, *, page_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _ZARNITZA_LISTING_BLOCK_RE.finditer(html):
        body = match.group("body")
        for href_match in re.finditer(r'href="(?P<url>/catalog/[^"#?]+/?)"', body, re.I):
            absolute = _absolute_url("zarnitza.ru", href_match.group("url"), page_url)
            if not absolute:
                continue
            normalized = _normalize_zarnitza_catalog_url(absolute)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)
    return urls


def _extract_zarnitza_catalog_links(html: str, *, page_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for href in _HREF_RE.findall(html):
        if href.startswith("javascript:"):
            continue
        absolute = _absolute_url("zarnitza.ru", href, page_url)
        if not absolute:
            continue
        parsed = urlparse(absolute.split("#")[0])
        if not parsed.path.startswith("/catalog"):
            continue
        if parsed.query and parse_qs(parsed.query).get("PAGEN_1"):
            page_link = absolute.split("#")[0]
            if page_link not in seen:
                seen.add(page_link)
                urls.append(page_link)
            continue
        normalized = _normalize_zarnitza_catalog_url(absolute)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def _extract_zarnitza_pagination_urls(html: str, *, page_url: str) -> list[str]:
    parsed = urlparse(page_url.split("#")[0])
    if "PAGEN_1" in (parsed.query or ""):
        base = page_url.split("?")[0].split("#")[0]
    else:
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"
    max_page = 1
    for match in re.finditer(r"PAGEN_1=(\d+)", html):
        try:
            max_page = max(max_page, int(match.group(1)))
        except ValueError:
            continue
    if max_page <= 1:
        return []
    return [f"{base}?PAGEN_1={page_num}" for page_num in range(2, max_page + 1)]


def _discover_zarnitza_sitemap_urls() -> list[str]:
    index_url = "https://zarnitza.ru/sitemap.xml"
    product_urls: list[str] = []
    seen: set[str] = set()
    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            index_response = client.get(index_url)
            index_response.raise_for_status()
            sitemap_urls = re.findall(
                r"<loc>(https://zarnitza\.ru/sitemap[^<]+\.xml)</loc>",
                index_response.text,
                re.I,
            )
            if not sitemap_urls:
                sitemap_urls = ["https://zarnitza.ru/sitemap-iblock-10.xml"]
            for sitemap_url in sitemap_urls:
                if "iblock-10" not in sitemap_url.lower():
                    continue
                try:
                    response = client.get(sitemap_url)
                    response.raise_for_status()
                except Exception:
                    continue
                for url in _ZARNITZA_SITEMAP_PRODUCT_RE.findall(response.text):
                    normalized = _normalize_zarnitza_catalog_url(url)
                    if not normalized or normalized in seen:
                        continue
                    seen.add(normalized)
                    product_urls.append(normalized)
    except Exception:
        logger.exception("Failed to fetch zarnitza sitemap")
    return product_urls


def _discover_zarnitza_product_urls() -> list[str]:
    limit = COMPETITOR_INDEX_MAX_URLS
    queue: deque[str] = deque(_ZARNITZA_CATEGORY_SEEDS)
    seen_pages: set[str] = set()
    product_urls: list[str] = []
    seen_products: set[str] = set()

    for seed in _discover_zarnitza_sitemap_urls():
        if seed not in seen_pages:
            queue.append(seed)

    append_index_log("zarnitza.ru", "Обход каталога zarnitza.ru (BFS)…")

    try:
        with httpx.Client(
            timeout=_ZARNITZA_REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            while queue and len(product_urls) < limit and len(seen_pages) < _ZARNITZA_MAX_CRAWL_PAGES:
                page_url = queue.popleft()
                page_key = page_url.split("#")[0]
                if page_key in seen_pages:
                    continue
                seen_pages.add(page_key)

                try:
                    response = client.get(page_url)
                    response.raise_for_status()
                except Exception:
                    logger.debug("Zarnitza crawl failed %s", page_url, exc_info=True)
                    continue

                html = response.text[:700_000]
                final_url = str(response.url).split("#")[0]

                if _is_zarnitza_product_page_html(html):
                    product_url = _normalize_zarnitza_catalog_url(final_url) or final_url
                    if product_url not in seen_products:
                        seen_products.add(product_url)
                        product_urls.append(product_url)
                    continue

                for product_url in _extract_zarnitza_listing_product_urls(html, page_url=final_url):
                    if product_url in seen_products:
                        continue
                    seen_products.add(product_url)
                    product_urls.append(product_url)
                    if len(product_urls) >= limit:
                        break

                if len(product_urls) >= limit:
                    break

                catalog_links = _extract_zarnitza_catalog_links(html, page_url=final_url)
                pagination_links = _extract_zarnitza_pagination_urls(html, page_url=final_url)
                for link in pagination_links:
                    link_key = link.split("#")[0]
                    if link_key not in seen_pages:
                        queue.appendleft(link)
                for link in catalog_links:
                    link_key = link.split("#")[0]
                    if link_key not in seen_pages:
                        queue.append(link)

                if len(seen_pages) % 100 == 0:
                    append_index_log(
                        "zarnitza.ru",
                        f"  → обход {len(seen_pages)} стр., найдено {len(product_urls)} товаров",
                    )
    except Exception:
        logger.exception("Zarnitza catalog crawl failed")

    logger.info(
        "zarnitza.ru: discovered %s product URLs (%s pages crawled, limit %s)",
        len(product_urls),
        len(seen_pages),
        limit,
    )
    return product_urls[:limit]


def _extract_zarnitza_product_image(html: str, *, page_url: str) -> str | None:
    for pattern in (
        r'itemprop="image"[^>]*src="(?P<url>/upload/iblock/[^"]+)"',
        r'href="(?P<url>/upload/iblock/[^"]+\.(?:png|jpe?g|webp))"',
        r'(?:src|data-src)="(?P<url>/upload/iblock/[^"]+\.(?:png|jpe?g|webp))"',
        r'(?:src|data-src)="(?P<url>/upload/resize_cache/iblock/[^"]+\.(?:png|jpe?g|webp))"',
    ):
        match = re.search(pattern, html, re.I | re.S)
        if not match:
            continue
        absolute = _absolute_url("zarnitza.ru", match.group("url"), page_url)
        if absolute and "resize_cache" not in absolute:
            return absolute
    match = re.search(
        r'(?:src|data-src)="(?P<url>/upload/resize_cache/iblock/[^"]+)"',
        html,
        re.I,
    )
    if match:
        return _absolute_url("zarnitza.ru", match.group("url"), page_url)
    return None


def _extract_zarnitza_description(html: str) -> str | None:
    desc_match = re.search(
        r'id="tab_opisanie"[^>]*>.*?<div class="text">(?P<body>.*?)</div>',
        html,
        re.I | re.S,
    )
    if not desc_match:
        return None
    text = _strip_html_text(desc_match.group("body"))
    if len(text) < 20:
        return None
    return text[:4000]


def _parse_zarnitza_product_html(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> CompetitorCatalogProduct | None:
    if not _is_zarnitza_product_page_html(html):
        return None

    name_match = re.search(r"<h1[^>]*>\s*(?P<name>.*?)\s*</h1>", html, re.I | re.S)
    if not name_match:
        return None
    name = re.sub(r"\s+", " ", _strip_html_text(name_match.group("name"))).strip()
    if len(name) < 4:
        return None

    price = None
    price_match = re.search(r'itemprop="price"\s+content="(?P<price>[\d.]+)"', html, re.I)
    if price_match:
        price = _parse_price(price_match.group("price"))
    if price is None:
        price_match = re.search(
            r'class="main__card-product--price".*?class="new-price">\s*(?P<price>[\d\s]+)',
            html,
            re.I | re.S,
        )
        if price_match:
            price = _parse_price(price_match.group("price"))

    articul = None
    articul_match = re.search(
        r'class="articul"[^>]*>\s*Артикул:\s*(?P<articul>[^\s<]+)',
        html,
        re.I | re.S,
    )
    if articul_match:
        articul = articul_match.group("articul").strip()

    price_label = price_on_request_label(html[:80_000]) if price is None else None

    return CompetitorCatalogProduct(
        domain=domain,
        site_label=site_label,
        name=name[:300],
        price=price,
        url=page_url.split("#")[0],
        articul=articul,
        price_label=price_label,
        image_url=_extract_zarnitza_product_image(html, page_url=page_url),
        description=_extract_zarnitza_description(html),
    )


def _parse_zarnitza_listing_previews(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if "cart-articul" not in html:
        return []

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    for match in _ZARNITZA_LISTING_BLOCK_RE.finditer(html):
        body = match.group("body")
        link_match = re.search(
            r'class="info"[^>]*>\s*<a[^>]*href="(?P<url>/catalog/[^"]+)"[^>]*>(?P<name>.*?)</a>',
            body,
            re.I | re.S,
        )
        if not link_match:
            link_match = re.search(
                r'class="image"[^>]*>\s*<a[^>]*href="(?P<url>/catalog/[^"]+)"',
                body,
                re.I | re.S,
            )
            if not link_match:
                continue
            name = ""
        else:
            name = re.sub(r"\s+", " ", unescape(_strip_html_text(link_match.group("name")))).strip()

        url = _absolute_url(domain, link_match.group("url"), page_url)
        if not url:
            continue
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)

        if not name:
            name_match = re.search(r'alt="([^"]+)"', body, re.I)
            name = name_match.group(1).strip() if name_match else key.rstrip("/").split("/")[-1]
        if len(name) < 4:
            continue

        articul_match = re.search(
            r'class="articul"[^>]*>\s*Артикул:\s*(?P<articul>[^\s<]+)',
            body,
            re.I | re.S,
        )
        price = None
        price_label = None
        price_match = re.search(r'class="new-price">\s*(?P<price>[\d\s]+)', body, re.I | re.S)
        if price_match:
            price = _parse_price(price_match.group("price"))
        elif re.search(r'class="query"', body, re.I):
            price_label = "По запросу"

        image_url = None
        image_match = re.search(
            r'(?:src|data-src)="(?P<url>/upload/[^"]+\.(?:png|jpe?g|webp))"',
            body,
            re.I,
        )
        if image_match:
            image_url = _absolute_url(domain, image_match.group("url"), page_url)

        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=price,
                url=key,
                articul=articul_match.group("articul").strip() if articul_match else None,
                price_label=price_label,
                image_url=image_url,
            )
        )
    return products


def _parse_zarnitza_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if not _is_zarnitza_domain(domain):
        return []

    if _is_zarnitza_product_page_html(html):
        product = _parse_zarnitza_product_html(
            html,
            domain=domain,
            site_label=site_label,
            page_url=page_url,
        )
        return [product] if product else []

    return _parse_zarnitza_listing_previews(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )


def fetch_zarnitza_catalog(
    site: CompetitorSite,
    *,
    checkpoint_every: int = 500,
) -> list[CompetitorCatalogProduct]:
    product_urls = _discover_zarnitza_product_urls()
    if not product_urls:
        append_index_log(site.domain, "Каталог zarnitza.ru пуст — обход seed URL", level="error")
        return fetch_catalog_products(site, max_pages=24)

    append_index_log(
        site.domain,
        f"Индексация zarnitza.ru: {len(product_urls)} URL товаров",
    )
    return _fetch_products_from_url_list(
        site,
        product_urls,
        checkpoint_every=checkpoint_every,
        request_timeout=_ZARNITZA_REQUEST_TIMEOUT,
        max_workers=min(8, COMPETITOR_INDEX_WORKERS),
    )


def _parse_n72_product_previews(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if "n72r-product-preview" not in html:
        return []

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    for match in _N72_PREVIEW_RE.finditer(html):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        url = _absolute_url(domain, match.group("url"), page_url)
        chunk = html[match.start() : match.start() + 3000]
        articul_match = re.search(
            r'data-value="([A-Za-z0-9\-_.]+)"[^>]*>\s*<span>\s*Артикул:',
            chunk,
            re.I | re.S,
        )
        if not articul_match:
            articul_match = _ARTICUL_RE.search(chunk)
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=_parse_price(match.group("price")),
                url=url or None,
                articul=articul_match.group(1) if articul_match else None,
            )
        )
    return products


def _is_product_detail_path(path: str) -> bool:
    lower = path.lower().rstrip("/")
    if "/catalog/product/" in lower:
        return True
    if re.match(r"^/product/[^/]+$", lower):
        return True
    if _is_vrtorg_product_path(lower):
        return True
    return "/products/" in lower and lower.count("/") >= 3


def _parse_preview_product_blocks(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if "preview_product" not in html.lower() and 'itemprop="name"' not in html.lower():
        return []

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    for match in _PREVIEW_PRODUCT_NAME_RE.finditer(html):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        url = _absolute_url(domain, match.group("url"), page_url)
        chunk = html[match.start() : match.start() + 2500]
        articul_match = _ARTICUL_RE.search(chunk)
        prices = extract_prices_from_text(chunk)
        label = price_on_request_label(chunk) if not prices else None
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=prices[0] if prices else None,
                url=url or None,
                articul=articul_match.group(1) if articul_match else None,
                price_label=label,
            )
        )
    return products


def _parse_td_school_price(html: str) -> float | None:
    price_match = re.search(r'class="price"[^>]*>(?P<body>.*?</div>)', html, re.I | re.S)
    if not price_match:
        return None
    body = re.sub(r"<span[^>]*>\s*</span>", "", price_match.group("body"), flags=re.I)
    plain = _strip_html_text(body)
    rub_match = re.search(r"Цена:\s*([\d\s]+)\s*руб", plain, re.I)
    if rub_match:
        parsed = _parse_price(rub_match.group(1))
        if parsed is not None:
            return parsed
    prices = extract_prices_from_text(plain)
    if prices:
        return min(prices)
    return _parse_price(plain)


def _parse_td_school_product_html(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> CompetitorCatalogProduct | None:
    if not _is_query_param_product_url(page_url):
        return None
    if not _is_td_school_product_html(html):
        return None

    title_match = re.search(r"<h1[^>]*>\s*(?P<name>[^<]+?)\s*</h1>", html, re.I | re.S)
    if not title_match:
        return None
    name = re.sub(r"\s+", " ", title_match.group("name")).strip()
    if len(name) < 4:
        return None

    hints = get_domain_parsing_hints(domain)
    articul = None
    code_match = re.search(r'class="productcode"[^>]*>\s*([^<]+)', html, re.I)
    if code_match:
        articul = code_match.group(1).strip()
    elif hints and hints.articul_html_hint:
        articul = _parse_articul_from_hint(html, hints.articul_html_hint)

    price = _parse_td_school_price(html)
    price_label = None
    if price is None and hints and hints.price_html_hint:
        price = _parse_price_from_hint(html, hints.price_html_hint)
    if price is None:
        price_label = price_on_request_label(html)

    parsed = urlparse(page_url)
    page_id = parse_qs(parsed.query).get("page", [""])[0]
    canonical = _canonical_td_school_product_url(domain, page_id) if page_id else page_url.split("#")[0]

    return CompetitorCatalogProduct(
        domain=domain,
        site_label=site_label,
        name=name[:300],
        price=price,
        url=canonical,
        articul=articul,
        price_label=price_label,
    )


def _is_td_school_product_html(html: str) -> bool:
    return bool(
        re.search(r'class="productcode"', html, re.I)
        and re.search(r'class="price"', html, re.I)
        and re.search(r"<h1[^>]*>", html, re.I)
    )


def _td_school_page_urls_from_html(html: str, *, domain: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for page_id in re.findall(r"index\.php\?page=(\d+)", html, re.I):
        absolute = _canonical_td_school_product_url(domain, page_id)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    for match in _TD_SCHOOL_PRODUCT_HREF_RE.finditer(html):
        page_id = match.group(2) or match.group(3)
        if not page_id:
            continue
        absolute = _canonical_td_school_product_url(domain, page_id)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls


def _parse_td_school_catalog_links(
    html: str,
    *,
    domain: str,
    page_url: str,
) -> tuple[list[str], list[str]]:
    product_urls: list[str] = []
    category_urls: list[str] = []
    seen_products: set[str] = set()
    seen_categories: set[str] = set()

    for match in _TD_SCHOOL_PRODUCT_HREF_RE.finditer(html):
        page_id = match.group(2) or match.group(3)
        if not page_id:
            continue
        absolute = _canonical_td_school_product_url(domain, page_id)
        if absolute not in seen_products:
            seen_products.add(absolute)
            product_urls.append(absolute)

    for href in _HREF_RE.findall(html):
        lower = href.lower()
        if "page=" in lower:
            continue
        if "index.php" not in lower and not lower.startswith("/"):
            continue
        if any(skip in lower for skip in ("search=", "logout", "cart", "mailto:", "javascript:")):
            continue
        absolute = _absolute_url(domain, href, page_url)
        if not absolute or domain not in urlparse(absolute).netloc.lower():
            continue
        if absolute in seen_categories:
            continue
        seen_categories.add(absolute)
        category_urls.append(absolute)

    return product_urls, category_urls


def _discover_td_school_product_urls(*, max_pages: int = 800) -> list[str]:
    domain = "td-school.ru"
    seeds = [
        "https://td-school.ru/",
        "https://td-school.ru/index.php",
    ]
    product_urls: set[str] = set()
    seen_pages: set[str] = set()
    queue: list[str] = list(seeds)
    pages_fetched = 0

    append_index_log(domain, "Обход каталога td-school.ru для поиска карточек товаров…")

    with httpx.Client(
        timeout=WEB_SEARCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
    ) as client:
        while queue and pages_fetched < max_pages:
            page_url = queue.pop(0)
            normalized_page = page_url.split("#")[0]
            if normalized_page in seen_pages:
                continue
            seen_pages.add(normalized_page)
            pages_fetched += 1
            try:
                response = client.get(page_url)
                response.raise_for_status()
            except Exception:
                continue
            html = response.text[:700_000]
            final_url = str(response.url).split("#")[0]

            if _is_td_school_product_html(html):
                parsed = urlparse(final_url)
                page_id = parse_qs(parsed.query).get("page", [""])[0]
                if page_id:
                    product_urls.add(_canonical_td_school_product_url(domain, page_id))

            for child_url in _td_school_page_urls_from_html(html, domain=domain):
                if child_url.split("#")[0] not in seen_pages and child_url not in queue:
                    queue.append(child_url)

            for href in _HREF_RE.findall(html):
                lower = href.lower()
                if any(skip in lower for skip in ("search=", "logout", "cart", "mailto:", "javascript:")):
                    continue
                absolute = _absolute_url(domain, href, final_url)
                if not absolute or domain not in urlparse(absolute).netloc.lower():
                    continue
                norm = absolute.split("#")[0]
                if norm in seen_pages or absolute in queue:
                    continue
                if "index.php" in lower or norm.endswith("td-school.ru") or norm.endswith("td-school.ru/"):
                    queue.append(absolute)

            if pages_fetched % 50 == 0:
                append_index_log(
                    domain,
                    f"  → страниц: {pages_fetched}, очередь: {len(queue)}, карточек: {len(product_urls)}",
                )

    result = sorted(
        product_urls,
        key=lambda url: int(parse_qs(urlparse(url).query).get("page", ["0"])[0]),
    )
    append_index_log(domain, f"Найдено карточек товаров в каталоге: {len(result)}")
    return result[:COMPETITOR_INDEX_MAX_URLS]


def fetch_td_school_catalog(
    site: CompetitorSite,
    *,
    checkpoint_every: int = 500,
) -> list[CompetitorCatalogProduct]:
    hints = get_domain_parsing_hints(site.domain)
    if not hints or not hints.price_html_hint:
        set_domain_parsing_hints(
            site.domain,
            product_sample_url=_TD_SCHOOL_DEFAULT_SAMPLE_URL,
            price_html_hint='<div class="price">',
            articul_html_hint='class="productcode"',
        )

    hints = get_domain_parsing_hints(site.domain)
    sample_url = hints.product_sample_url if hints else _TD_SCHOOL_DEFAULT_SAMPLE_URL

    sitemap_urls = _discover_generic_sitemap_product_urls(site.domain, sample_url)
    product_urls = sitemap_urls or _discover_td_school_product_urls()
    if sample_url and sample_url not in product_urls:
        product_urls = [sample_url, *product_urls]

    if not product_urls:
        append_index_log(site.domain, "Не удалось найти URL карточек товаров", level="error")
        return []

    append_index_log(
        site.domain,
        f"Индексация {len(product_urls)} карточек td-school.ru…",
    )
    return _fetch_products_from_url_list(
        site,
        product_urls,
        checkpoint_every=checkpoint_every,
    )


def _parse_td_school_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if domain.lower().removeprefix("www.") != "td-school.ru":
        return []

    product = _parse_td_school_product_html(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    return [product] if product else []


def _is_epp24_product_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host != "epp24.ru":
        return False
    return "/product/" in parsed.path.lower()


def _extract_epp24_product_image(html: str) -> str | None:
    for pattern in (
        r'data-large_image="(?P<url>[^"]+)"',
        r'class="[^"]*wp-post-image[^"]*"[^>]*(?:data-src|src)="(?P<url>[^"]+)"',
        r'property="og:image"\s+content="(?P<url>[^"]+)"',
    ):
        match = re.search(pattern, html, re.I | re.S)
        if not match:
            continue
        url = match.group("url").strip()
        if url and not url.startswith("data:"):
            return url
    return None


def _extract_epp24_description(html: str) -> str | None:
    desc_match = re.search(
        r'(?:id="tab-description"|class="[^"]*woocommerce-Tabs-panel--description[^"]*")'
        r'[^>]*>(?P<body>.*?)(?=</div>\s*<div\b|\Z)',
        html,
        re.I | re.S,
    )
    if not desc_match:
        return None
    text = _strip_html_text(desc_match.group("body"))
    text = re.sub(r"^Описание\s*", "", text, flags=re.I).strip()
    if len(text) < 20:
        return None
    return text[:4000]


def _parse_epp24_product_html(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> CompetitorCatalogProduct | None:
    if not _is_epp24_product_url(page_url):
        return None

    name_match = re.search(r"<h1[^>]*>\s*(?P<name>[^<]+?)\s*</h1>", html, re.I | re.S)
    if not name_match:
        name_match = re.search(r'itemprop="name"[^>]*>\s*(?P<name>[^<]+?)\s*</', html, re.I | re.S)
    if not name_match:
        return None

    name = re.sub(r"\s+", " ", name_match.group("name")).strip()
    if len(name) < 4:
        return None

    price = None
    price_match = re.search(
        r'class="price"[^>]*>.*?woocommerce-Price-amount[^>]*>.*?(\d[\d\s]*)',
        html,
        re.I | re.S,
    )
    if price_match:
        price = _parse_price(price_match.group(1))
    if price is None:
        price_match = re.search(
            r'itemprop="price"\s+content="(?P<price>[\d.]+)"',
            html,
            re.I,
        )
        if price_match:
            price = _parse_price(price_match.group("price"))

    articul = None
    articul_match = re.search(r'class="sku"[^>]*>([^<]+)</span>', html, re.I | re.S)
    if articul_match:
        articul = articul_match.group(1).strip()

    image_url = _extract_epp24_product_image(html)
    description = _extract_epp24_description(html)

    return CompetitorCatalogProduct(
        domain=domain,
        site_label=site_label,
        name=name[:300],
        price=price,
        url=page_url.split("#")[0],
        articul=articul,
        image_url=image_url,
        description=description,
    )


def _parse_epp24_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if domain.lower().removeprefix("www.") != "epp24.ru":
        return []
    product = _parse_epp24_product_html(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    return [product] if product else []


def _parse_product_detail_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if not _is_product_page_url(page_url):
        return []

    title_match = re.search(r"<h1[^>]*>\s*(?P<name>[^<]+?)\s*</h1>", html, re.I | re.S)
    if not title_match:
        title_match = re.search(
            r'itemprop="name"[^>]*>\s*(?P<name>[^<]+?)\s*</',
            html,
            re.I | re.S,
        )
    if not title_match:
        return []

    name = re.sub(r"\s+", " ", title_match.group("name")).strip()
    if len(name) < 4:
        return []

    focused = _focus_product_price_html(html)
    hints = get_domain_parsing_hints(domain)
    articul_match = re.search(
        r'data-value="([A-Za-z0-9\-_.]+)"[^>]*>\s*<span>\s*Артикул:',
        html[:120_000],
        re.I | re.S,
    )
    if not articul_match:
        articul_match = _ARTICUL_RE.search(html[:120_000])
    articul = articul_match.group(1) if articul_match else None
    if hints and hints.articul_html_hint and not articul:
        articul = _parse_articul_from_hint(html, hints.articul_html_hint)

    price = _extract_primary_product_price(focused)
    if price is None and hints and hints.price_html_hint:
        price = _parse_price_from_hint(html, hints.price_html_hint)
    price_label = price_on_request_label(focused) if price is None else None
    return [
        CompetitorCatalogProduct(
            domain=domain,
            site_label=site_label,
            name=name[:300],
            price=price,
            url=page_url.split("#")[0],
            articul=articul,
            price_label=price_label,
        )
    ]


def _parse_shop2_products(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    blocks = re.findall(
        r'class="product-top"(?P<body>.*?)class="price-current"><strong[^>]*>(?P<price>[^<]+)</strong>',
        html,
        re.I | re.S,
    )
    for body, price_raw in blocks:
        name_match = re.search(
            r'class="product-name"><a\s+href="(?P<url>[^"]+)">(?P<name>[^<]+)</a>',
            body,
            re.I | re.S,
        )
        if not name_match:
            continue
        name = re.sub(r"\s+", " ", name_match.group("name")).strip()
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        articul_match = re.search(r'class="article">Артикул:\s*<span>(?P<articul>[^<]+)</span>', body, re.I)
        url = _absolute_url(domain, name_match.group("url"), page_url)
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=_parse_price(price_raw),
                url=url or None,
                articul=articul_match.group("articul").strip() if articul_match else None,
            )
        )
    return products


def parse_catalog_html(html: str, *, domain: str, site_label: str, page_url: str) -> list[CompetitorCatalogProduct]:
    if not html.strip():
        return []

    stronikum_products = _parse_stronikum_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if stronikum_products:
        return stronikum_products

    labkabinet_products = _parse_labkabinet_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if labkabinet_products:
        return labkabinet_products

    vrtorg_products = _parse_vrtorg_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if vrtorg_products:
        return vrtorg_products

    td_school_products = _parse_td_school_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if td_school_products:
        return td_school_products

    epp24_products = _parse_epp24_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if epp24_products:
        return epp24_products

    n72_products = _parse_n72_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if n72_products:
        return n72_products

    zarnitza_products = _parse_zarnitza_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if zarnitza_products:
        return zarnitza_products

    shop2_products = _parse_shop2_products(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if shop2_products:
        return shop2_products

    preview_products = _parse_preview_product_blocks(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if preview_products and _is_product_detail_path(urlparse(page_url).path.lower()):
        path_parts = [part for part in urlparse(page_url).path.split("/") if part]
        if len(path_parts) >= 3:
            detail_products = _parse_product_detail_page(
                html,
                domain=domain,
                site_label=site_label,
                page_url=page_url,
            )
            if detail_products:
                return detail_products

    if preview_products:
        return preview_products

    detail_products = _parse_product_detail_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if detail_products:
        return detail_products

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()

    for match in _HREF_RE.finditer(html):
        href = match.group(1)
        lower = href.lower()
        if not any(
            token in lower
            for token in (
                "/products/",
                "/product/",
                "/catalog/product/",
                "/tovar/",
                "/goods/",
                "/item/",
                "/magazin/",
            )
        ):
            continue
        if "/folder/" in lower or "/search" in lower or "/cart" in lower:
            continue
        url = _absolute_url(domain, href, page_url)
        if not url or domain not in urlparse(url).netloc.lower():
            continue
        name = _extract_title_near(match.start(), html)
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        chunk = html[match.start() : match.start() + 2500]
        articul_match = _ARTICUL_RE.search(chunk)
        prices = extract_prices_from_text(chunk)
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=prices[0] if prices else None,
                url=url,
                articul=articul_match.group(1) if articul_match else None,
            )
        )

    plain = re.sub(r"\s+", " ", _TAG_RE.sub("\n", html))
    blocks = re.split(r"(?=Артикул:\s*[A-Za-z0-9\-_.]+)", plain)
    for block in blocks:
        articul_match = _ARTICUL_RE.search(block)
        if not articul_match:
            continue
        prices = extract_prices_from_text(block)
        if not prices:
            continue
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        name = ""
        for line in lines:
            if "Артикул:" in line:
                continue
            if "руб" in line.lower():
                continue
            if len(line) >= 8:
                name = line
                break
        if not name:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=prices[0],
                url=None,
                articul=articul_match.group(1),
            )
        )

    return products


def _discover_folder_urls(html: str, *, domain: str, page_url: str, limit: int = 12) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for href in _HREF_RE.findall(html):
        lower = href.lower()
        if "/magazin/folder/" not in lower and "/catalog/" not in lower:
            continue
        absolute = _absolute_url(domain, href, page_url)
        if not absolute or absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
        if len(urls) >= limit:
            break
    return urls


def resolve_catalog_urls(
    site: CompetitorSite,
    *,
    extra_urls: list[str] | None = None,
) -> list[str]:
    from src.services.competitor_catalog_urls import get_competitor_catalog_url_registry

    urls: list[str] = []
    if site.search_url:
        urls.append(site.search_url.format(query=quote_plus("")))
    urls.extend(_CATALOG_SEED_URLS.get(site.domain, []))
    urls.append(_site_root(site.domain))
    urls.extend(get_competitor_catalog_url_registry().urls_for_domain(site.domain))
    if extra_urls:
        urls.extend(extra_urls)

    dedup: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = url.strip().split("#")[0]
        if normalized and normalized not in seen:
            seen.add(normalized)
            dedup.append(normalized)
    return dedup


def fetch_catalog_page(
    page_url: str,
    *,
    domain: str,
    site_label: str,
) -> list[CompetitorCatalogProduct]:
    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            response = client.get(page_url)
            response.raise_for_status()
            return parse_catalog_html(
                response.text[:700_000],
                domain=domain,
                site_label=site_label,
                page_url=str(response.url),
            )
    except Exception:
        logger.debug("Catalog page fetch failed %s", page_url, exc_info=True)
        return []


def fetch_catalog_products(
    site: CompetitorSite,
    *,
    max_pages: int = 6,
    extra_urls: list[str] | None = None,
) -> list[CompetitorCatalogProduct]:
    normalized_domain = site.domain.lower().removeprefix("www.")
    if normalized_domain == "stronikum.ru":
        return fetch_stronikum_catalog(site)
    if normalized_domain == "labkabinet.ru":
        return fetch_labkabinet_catalog(site)
    if normalized_domain == "vrtorg.ru":
        return fetch_vrtorg_catalog(site)
    if normalized_domain == "td-school.ru":
        return fetch_td_school_catalog(site)
    if normalized_domain == "n-72.ru":
        return fetch_n72_catalog(site)
    if normalized_domain == "zarnitza.ru":
        return fetch_zarnitza_catalog(site)

    hints = get_domain_parsing_hints(normalized_domain)
    sample_url = hints.product_sample_url if hints else None
    sitemap_urls = _discover_generic_sitemap_product_urls(
        normalized_domain,
        sample_url,
    )
    if sitemap_urls:
        append_index_log(
            site.domain,
            f"Индексация по sitemap: {len(sitemap_urls)} URL",
        )
        return _fetch_products_from_url_list(site, sitemap_urls)

    dedup_urls = resolve_catalog_urls(site, extra_urls=extra_urls)
    append_index_log(
        site.domain,
        f"Sitemap не найден — обход {min(max_pages, len(dedup_urls))} страниц каталога",
    )

    products: list[CompetitorCatalogProduct] = []
    seen_names: set[str] = set()
    seen_urls: set[str] = set()

    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            discovered: list[str] = []
            page_count = len(list(dedup_urls[:max_pages]))
            for page_index, page_url in enumerate(list(dedup_urls[:max_pages]), start=1):
                if page_url in seen_urls:
                    continue
                seen_urls.add(page_url)
                append_index_log(
                    site.domain,
                    f"  → страница {page_index}/{page_count}: {page_url}",
                )
                try:
                    response = client.get(page_url)
                    response.raise_for_status()
                except Exception:
                    logger.debug("Catalog fetch failed %s", page_url, exc_info=True)
                    continue
                html = response.text[:700_000]
                final_url = str(response.url)
                discovered.extend(
                    _discover_folder_urls(
                        html,
                        domain=site.domain,
                        page_url=final_url,
                    )
                )
                page_products = parse_catalog_html(
                    html,
                    domain=site.domain,
                    site_label=site.label,
                    page_url=final_url,
                )
                for item in page_products:
                    key = normalize_name(item.name)
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    products.append(item)
            if discovered:
                append_index_log(
                    site.domain,
                    f"  → найдено дополнительных разделов: {len(discovered)}",
                )

            for page_url in discovered:
                if page_url in seen_urls:
                    continue
                seen_urls.add(page_url)
                try:
                    response = client.get(page_url)
                    response.raise_for_status()
                except Exception:
                    continue
                for item in parse_catalog_html(
                    response.text[:700_000],
                    domain=site.domain,
                    site_label=site.label,
                    page_url=str(response.url),
                ):
                    key = normalize_name(item.name)
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    products.append(item)
    except Exception:
        logger.exception("Catalog crawl failed for %s", site.domain)

    append_index_log(site.domain, f"Сбор завершён: {len(products)} товаров")
    return products


def products_to_rag_text(
    products: list[CompetitorCatalogProduct],
    *,
    site: CompetitorSite | None = None,
    title: str = "",
) -> str:
    header_label = site.label if site else title or "Все конкуренты"
    header_domain = site.domain if site else "all"
    lines = [
        f"Каталог конкурента: {header_label}",
        f"Домен: {header_domain}",
        f"Поиск: {site.search_url if site else '—'}",
        f"Позиций: {len(products)}",
        "",
    ]
    for product in products:
        lines.append(
            "[product] "
            f"domain={product.domain} | site={product.site_label} | "
            f"name={product.name} | price={product.price or ''} | "
            f"url={product.url or ''} | articul={product.articul or ''} | "
            f"price_label={product.price_label or ''} | "
            f"wholesale_price={product.wholesale_price or ''} | "
            f"image_url={product.image_url or ''} | "
            f"details={product.details or ''} | "
            f"description={product.description or ''}"
        )
    return "\n".join(lines)


def index_competitor_page_url(
    page_url: str,
    *,
    domain: str,
    site_label: str,
    doc_rag_index,
) -> dict[str, int | bool | str]:
    from src.services.competitor_catalog_urls import get_competitor_catalog_url_registry
    from src.services.competitor_product_store import get_competitor_product_store

    products = fetch_catalog_page(page_url, domain=domain, site_label=site_label)
    store = get_competitor_product_store()
    added, updated = store.merge_products(products, domain=domain, site_label=site_label)
    store.record_indexed_page(
        page_url,
        domain=domain,
        site_label=site_label,
        products_count=len(products),
    )
    get_competitor_catalog_url_registry().add_page(
        page_url,
        domain=domain,
        label=site_label,
        source="page_index",
    )

    page_doc_id = f"competitor-page:{domain}:{abs(hash(page_url)) % 10_000_000}"
    rag_result: dict[str, int | bool | str] = {"indexed": False, "chunks": 0}
    if products:
        rag_result = doc_rag_index.index_text(
            doc_id=page_doc_id,
            source_type="competitor",
            source_name=f"{site_label} | {page_url}",
            text=(
                f"Страница каталога: {page_url}\n"
                f"Домен: {domain}\n"
                f"Позиций: {len(products)}\n\n"
                + products_to_rag_text(products, title=site_label)
            ),
            filename=page_url,
            force=True,
        )

    sync_unified_competitor_rag(doc_rag_index)
    return {
        "url": page_url,
        "domain": domain,
        "products_found": len(products),
        "products_added": added,
        "products_updated": updated,
        "rag": rag_result,
    }


_VRTORG_IMAGE_ENRICH_DOMAINS = {"vrtorg.ru"}


def enrich_site_product_images(
    domain: str,
    *,
    site_label: str = "",
    limit: int | None = None,
    doc_rag_index=None,
) -> dict[str, int | bool | str]:
    normalized = domain.lower().removeprefix("www.")
    if normalized not in _VRTORG_IMAGE_ENRICH_DOMAINS:
        return {
            "domain": normalized,
            "supported": False,
            "message": "Обогащение фото пока доступно только для vrtorg.ru",
        }

    from src.services.competitor_product_store import get_competitor_product_store

    store = get_competitor_product_store()
    label = site_label or store._site_labels.get(normalized, normalized)
    targets = [
        product
        for product in store.products_for_domain(normalized)
        if product.url and not product.image_url
    ]
    if limit is not None and limit > 0:
        targets = targets[:limit]

    checked = 0
    updated = 0
    failed = 0
    for product in targets:
        checked += 1
        fetched = fetch_catalog_page(
            product.url or "",
            domain=normalized,
            site_label=label,
        )
        if not fetched:
            failed += 1
            continue
        incoming = fetched[0]
        if not incoming.image_url:
            failed += 1
            continue
        _added, batch_updated = store.merge_products(
            [incoming],
            domain=normalized,
            site_label=label,
        )
        if batch_updated:
            updated += batch_updated

    if updated and doc_rag_index is not None:
        sync_unified_competitor_rag(doc_rag_index)

    total = len(store.products_for_domain(normalized))
    with_image = sum(
        1 for product in store.products_for_domain(normalized) if product.image_url
    )
    without_image = total - with_image
    if total == 0:
        message = (
            "В индексе 0 товаров для этого сайта. "
            "enrich-images не загружает каталог — запустите "
            'POST /api/competitors/reindex с {"domains":["'
            f'{normalized}"], "force": true, "background": true}}'
        )
    elif checked == 0 and without_image == 0:
        message = (
            f"В индексе {total} товаров, у всех уже есть фото. "
            "Для полной индексации каталога используйте /api/competitors/reindex."
        )
    else:
        message = (
            f"Проверено {checked} товаров без фото, обновлено {updated}, "
            f"осталось без фото: {without_image}."
        )

    return {
        "domain": normalized,
        "supported": True,
        "checked": checked,
        "updated": updated,
        "failed": failed,
        "total_products": total,
        "products_with_image": with_image,
        "remaining_without_image": without_image,
        "message": message,
    }


def sync_unified_competitor_rag(doc_rag_index) -> dict[str, int | bool]:
    from src.services.competitor_product_store import get_competitor_product_store

    products = get_competitor_product_store().iter_products()
    if not products:
        return {"indexed": False, "chunks": 0, "products": 0}

    text = products_to_rag_text(products, title="Каталог всех конкурентов")
    result = doc_rag_index.index_text(
        doc_id="competitor-catalog:all",
        source_type="competitor",
        source_name="Каталог конкурентов",
        text=text,
        filename="competitor-catalog-all",
        force=True,
    )
    result["products"] = len(products)
    return result


def index_competitor_site_catalog(
    site: CompetitorSite,
    doc_rag_index,
    *,
    force: bool = False,
    extra_urls: list[str] | None = None,
) -> dict[str, int | bool]:
    from src.services.competitor_catalog_urls import get_competitor_catalog_url_registry
    from src.services.competitor_product_store import get_competitor_product_store

    doc_id = f"competitor-catalog:{site.domain}"
    doc_rag_index.ensure_loaded()
    store = get_competitor_product_store()
    existing_count = len(store.products_for_domain(site.domain))
    if (
        not force
        and store.has_site(site.domain)
        and doc_id in doc_rag_index._entries
        and site_catalog_looks_complete(site.domain, existing_count)
    ):
        entry = doc_rag_index._entries[doc_id]
        return {
            "indexed": True,
            "products": existing_count,
            "chunks": len(entry.chunks),
            "skipped": True,
            "reason": "catalog_already_complete",
        }

    for page_url in extra_urls or []:
        get_competitor_catalog_url_registry().add_page(
            page_url,
            domain=site.domain,
            label=site.label,
            source="site_index",
        )

    append_index_log(site.domain, f"Сбор каталога с {site.domain}…")
    products = fetch_catalog_products(site, extra_urls=extra_urls)
    existing_products = store.products_for_domain(site.domain)
    if not products and existing_products:
        logger.warning(
            "Catalog fetch returned 0 products for %s — keeping %s existing entries",
            site.domain,
            len(existing_products),
        )
        products = existing_products
        store_count = len(existing_products)
    elif products:
        store_count = store.replace_site_products(
            site.domain,
            products,
            site_label=site.label,
        )
    else:
        store_count = 0
    for page_url in resolve_catalog_urls(site, extra_urls=extra_urls)[:12]:
        store.record_indexed_page(
            page_url,
            domain=site.domain,
            site_label=site.label,
            products_count=len(
                [p for p in products if p.url and page_url.split("#")[0] in (p.url or "")]
            )
            or len(products),
        )
    store.save()
    products = store.products_for_domain(site.domain)

    if not products:
        append_index_log(site.domain, "Товары не найдены", level="error")
        return {"indexed": False, "products": 0, "chunks": 0, "store_products": store_count}

    append_index_log(site.domain, f"Запись {len(products)} товаров в RAG-индекс…")
    text = products_to_rag_text(products, site=site)
    result = doc_rag_index.index_text(
        doc_id=doc_id,
        source_type="competitor",
        source_name=site.label,
        text=text,
        filename=f"{site.domain}-catalog",
        force=True,
    )
    result["products"] = len(products)
    result["store_products"] = store_count
    sync_unified_competitor_rag(doc_rag_index)
    return result


def reindex_all_competitor_sites(
    doc_rag_index,
    *,
    force: bool = True,
) -> dict[str, object]:
    from src.services.competitor_site_manager import get_competitor_site_manager
    from src.services.competitor_product_store import get_competitor_product_store

    results: list[dict[str, object]] = []
    manager = get_competitor_site_manager()

    for site in competitor_sites_with_search():
        try:
            result = index_competitor_site_catalog(site, doc_rag_index, force=force)
            results.append({"domain": site.domain, "label": site.label, **result})
        except Exception:
            logger.exception("Failed to reindex competitor site %s", site.domain)
            results.append({"domain": site.domain, "label": site.label, "indexed": False, "error": True})

    for entry in manager.list_custom():
        apply_parsing_hints_from_entry(entry)
        site = CompetitorSite(
            domain=entry.domain,
            label=entry.label or entry.domain,
            search_url=entry.search_url,
        )
        try:
            result = index_competitor_site_catalog(
                site,
                doc_rag_index,
                force=force,
                extra_urls=entry.catalog_urls,
            )
            for page_url in entry.catalog_urls:
                index_competitor_page_url(
                    page_url,
                    domain=entry.domain,
                    site_label=entry.label,
                    doc_rag_index=doc_rag_index,
                )
            results.append({"domain": entry.domain, "label": entry.label, "custom": True, **result})
        except Exception:
            logger.exception("Failed to reindex custom competitor site %s", entry.domain)
            results.append(
                {"domain": entry.domain, "label": entry.label, "custom": True, "indexed": False, "error": True}
            )

    unified = sync_unified_competitor_rag(doc_rag_index)
    store_stats = get_competitor_product_store().stats()
    rag_stats = doc_rag_index.stats()
    return {
        "sites": results,
        "unified_rag": unified,
        "catalog_products": store_stats,
        "rag_docs": rag_stats,
    }


def bootstrap_competitor_catalogs(doc_rag_index, *, max_new_sites: int | None = None) -> None:
    from src.services.competitor_product_store import get_competitor_product_store

    store = get_competitor_product_store()
    sites = sorted(
        competitor_sites_with_search(),
        key=lambda site: 0 if site.domain in _CATALOG_SEED_URLS else 1,
    )
    indexed_new = 0
    for site in sites:
        doc_id = f"competitor-catalog:{site.domain}"
        doc_rag_index.ensure_loaded()
        if store.has_site(site.domain) and doc_id in doc_rag_index._entries:
            continue
        if max_new_sites is not None and indexed_new >= max_new_sites:
            break
        try:
            index_competitor_site_catalog(site, doc_rag_index)
            indexed_new += 1
        except Exception:
            logger.exception("Failed to index competitor catalog for %s", site.domain)


def bootstrap_competitor_catalogs_priority(
    doc_rag_index,
    *,
    domains: list[str] | None = None,
) -> None:
    if not domains:
        bootstrap_competitor_catalogs(doc_rag_index, max_new_sites=1)
        return
    domain_set = {domain.lower() for domain in domains}
    for site in competitor_sites_with_search():
        if site.domain.lower() not in domain_set:
            continue
        try:
            index_competitor_site_catalog(site, doc_rag_index, force=False)
        except Exception:
            logger.exception("Failed to index priority competitor catalog for %s", site.domain)


def parse_product_from_chunk(text: str) -> CompetitorCatalogProduct | None:
    match = _PRODUCT_LINE_RE.search(text.strip())
    if match:
        return CompetitorCatalogProduct(
            domain=match.group("domain").strip(),
            site_label=match.group("site").strip(),
            name=match.group("name").strip(),
            price=_parse_price(match.group("price")),
            url=(match.group("url").strip() or None),
            articul=(match.group("articul").strip() or None),
            price_label=(match.group("price_label") or "").strip() or None,
            details=(match.group("details") or "").strip() or None,
            wholesale_price=_parse_price(match.group("wholesale_price")),
            image_url=(match.group("image_url") or "").strip() or None,
            description=(match.group("description") or "").strip() or None,
        )

    domain = ""
    for token in text.split():
        if token.startswith("domain="):
            domain = token.split("=", 1)[1]
            break
    if "name=" not in text:
        return None
    name_part = text.split("name=", 1)[1]
    name = name_part.split("|", 1)[0].strip()
    if len(name) < 4:
        return None
    price = None
    if "price=" in text:
        price_raw = text.split("price=", 1)[1].split("|", 1)[0].strip()
        price = _parse_price(price_raw)
    url = None
    if "url=" in text:
        url_raw = text.split("url=", 1)[1].split("|", 1)[0].strip()
        url = url_raw or None
    articul = None
    if "articul=" in text:
        articul_raw = text.split("articul=", 1)[1].split("|", 1)[0].strip()
        articul = articul_raw or None
    price_label = None
    if "price_label=" in text:
        price_label_raw = text.split("price_label=", 1)[1].split("|", 1)[0].strip()
        price_label = price_label_raw or None
    image_url = None
    if "image_url=" in text:
        image_url_raw = text.split("image_url=", 1)[1].split("|", 1)[0].strip()
        image_url = image_url_raw or None
    description = None
    if "description=" in text:
        description_raw = text.split("description=", 1)[1].strip()
        description = description_raw or None
    return CompetitorCatalogProduct(
        domain=domain or "unknown",
        site_label=competitor_label_for_url(url) or domain or "Конкурент",
        name=name,
        price=price,
        url=url,
        articul=articul,
        price_label=price_label,
        image_url=image_url,
        description=description,
    )


def _iter_catalog_products(doc_rag_index) -> list[CompetitorCatalogProduct]:
    from src.services.competitor_product_store import get_competitor_product_store

    store_products = get_competitor_product_store().iter_products()
    if store_products:
        return store_products

    doc_rag_index.ensure_loaded()
    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    product_line_re = re.compile(r"\[product\][^\n]+", re.I)
    for entry in doc_rag_index._entries.values():
        if not str(entry.doc_id).startswith("competitor-catalog:"):
            continue
        full_text = "\n".join(str(chunk.get("text", "")) for chunk in entry.chunks)
        for line in product_line_re.findall(full_text):
            product = parse_product_from_chunk(line)
            if not product:
                continue
            key = normalize_name(product.name)
            if key in seen:
                continue
            seen.add(key)
            products.append(product)
    return products


def enrich_catalog_product_price(
    product: CompetitorCatalogProduct,
) -> CompetitorCatalogProduct:
    if product.price is not None or product.price_label:
        return product
    if not product.url or not is_competitor_product_page_url(product.url):
        return product

    fetched = fetch_catalog_page(
        product.url,
        domain=product.domain,
        site_label=product.site_label,
    )
    if not fetched:
        return product

    for item in fetched:
        if normalize_name(item.name) == normalize_name(product.name):
            return CompetitorCatalogProduct(
                domain=product.domain,
                site_label=product.site_label,
                name=product.name,
                price=item.price if item.price is not None else product.price,
                url=product.url,
                articul=item.articul or product.articul,
                price_label=item.price_label or product.price_label,
                details=item.details or product.details,
                wholesale_price=item.wholesale_price or product.wholesale_price,
                image_url=item.image_url or product.image_url,
                description=item.description or product.description,
            )
    if fetched[0].price is not None or fetched[0].price_label:
        item = fetched[0]
        return CompetitorCatalogProduct(
            domain=product.domain,
            site_label=product.site_label,
            name=product.name,
            price=item.price,
            url=product.url,
            articul=item.articul or product.articul,
            price_label=item.price_label,
            details=item.details or product.details,
            wholesale_price=item.wholesale_price or product.wholesale_price,
            image_url=item.image_url or product.image_url,
            description=item.description or product.description,
        )
    return product


def _score_catalog_products(
    query: str,
    products: list[CompetitorCatalogProduct],
    *,
    limit: int,
) -> list[PriceQuote]:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return []

    scored: list[tuple[float, CompetitorCatalogProduct]] = []
    seen: set[str] = set()

    for product in products:
        if product.url and not is_competitor_product_page_url(product.url):
            continue
        key = normalize_name(product.name)
        if key in seen:
            continue
        score = float(name_match_score(normalized_query, key))
        query_words = normalized_query.split()
        token_match = bool(
            query_words and all(word in key for word in query_words if len(word) >= 3)
        )
        if score < COMPETITOR_SEARCH_FALLBACK_THRESHOLD and not token_match:
            continue
        seen.add(key)
        if product.price is None and not product.price_label and product.url:
            product = enrich_catalog_product_price(product)
        scored.append((max(score, 96.0 if token_match else score), product))

    scored.sort(key=lambda item: item[0], reverse=True)
    quotes: list[PriceQuote] = []
    for score, product in scored[:limit]:
        label = product.site_label or competitor_label_for_url(product.url) or product.domain
        quotes.append(
            PriceQuote(
                source="web",
                label=label,
                matched_name=product.name,
                price=product.price,
                cost=product.price,
                price_label=product.price_label,
                wholesale_price=product.wholesale_price,
                articul=product.articul,
                match_score=round(score, 1),
                url=product.url,
                notes=(
                    f"Индекс каталога | articul: {product.articul}"
                    if product.articul
                    else "Индекс каталога конкурента"
                ),
                image_url=product.image_url,
            )
        )
    return quotes


def search_competitor_catalog_rag(
    query: str,
    doc_rag_index,
    *,
    limit: int = 10,
) -> list[PriceQuote]:
    from src.services.competitor_product_store import get_competitor_product_store

    store = get_competitor_product_store()
    candidates: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()

    def _add(product: CompetitorCatalogProduct | None) -> None:
        if not product or not product.name.strip():
            return
        key = normalize_name(product.name)
        if not key or key in seen:
            return
        seen.add(key)
        candidates.append(product)

    for product in store.search_products(query, limit=max(limit * 3, 24)):
        _add(product)

    if doc_rag_index is not None:
        rows = doc_rag_index.query(query, source_type="competitor", top_k=max(limit * 4, 16))
        for row in rows:
            text = str(row.get("text", ""))
            for line in re.findall(r"\[product\][^\n]+", text, re.I):
                _add(parse_product_from_chunk(line))
            if "[product]" not in text.lower():
                _add(parse_product_from_chunk(text))

    if not candidates:
        for product in _iter_catalog_products(doc_rag_index):
            _add(product)

    return _score_catalog_products(query, candidates, limit=limit)

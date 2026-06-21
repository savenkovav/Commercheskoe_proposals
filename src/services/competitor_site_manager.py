from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from src.config import COMPETITOR_SITES_REGISTRY_PATH, WEB_SEARCH_TIMEOUT
from src.services.competitor_sites import CompetitorSite

logger = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I | re.S)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SEARCH_HINTS = ("search", "query", "q=", "text=", "s=")


@dataclass
class CompetitorSiteEntry:
    id: str
    url: str
    domain: str
    label: str
    search_url: str | None
    catalog_urls: list[str]
    product_sample_url: str | None
    price_html_hint: str | None
    articul_html_hint: str | None
    notes: str
    builtin: bool
    title: str
    status: str
    added_at: str
    analyzed_at: str

    @classmethod
    def from_dict(cls, data: dict) -> CompetitorSiteEntry:
        catalog_urls = data.get("catalog_urls") or []
        if not catalog_urls and data.get("url"):
            catalog_urls = [str(data["url"])]
        return cls(
            id=str(data.get("id", "")),
            url=str(data.get("url", "")),
            domain=str(data.get("domain", "")),
            label=str(data.get("label", "")),
            search_url=data.get("search_url") or None,
            catalog_urls=[str(url) for url in catalog_urls if url],
            product_sample_url=data.get("product_sample_url") or None,
            price_html_hint=data.get("price_html_hint") or None,
            articul_html_hint=data.get("articul_html_hint") or None,
            notes=str(data.get("notes", "")),
            builtin=bool(data.get("builtin", False)),
            title=str(data.get("title", "")),
            status=str(data.get("status", "")),
            added_at=str(data.get("added_at", "")),
            analyzed_at=str(data.get("analyzed_at", "")),
        )


@dataclass
class CompetitorSiteDraft:
    url: str
    domain: str
    label: str
    search_url: str | None
    product_sample_url: str | None
    price_html_hint: str | None
    articul_html_hint: str | None
    analysis: dict
    builtin: bool = False
    indexed: bool = False
    index_result: dict | None = None
    indexed_at: str = ""


_SEARCH_SKIP_MARKERS = (
    ".css",
    ".js",
    ".min.css",
    ".min.js",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".woff",
    ".woff2",
    "/libs/",
    "/assets/",
    "/static/",
    "/upload/",
    "/bitrix/cache/",
)


class CompetitorSiteManager:
    def __init__(self, registry_path: Path = COMPETITOR_SITES_REGISTRY_PATH) -> None:
        self.registry_path = registry_path
        self._custom: list[CompetitorSiteEntry] = []
        self._drafts: dict[str, CompetitorSiteDraft] = {}
        self._load()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load(self) -> None:
        if not self.registry_path.exists():
            self._custom = []
            return
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
            rows = payload.get("sites", [])
            self._custom = [
                CompetitorSiteEntry.from_dict(row)
                for row in rows
                if isinstance(row, dict) and not row.get("builtin")
            ]
        except Exception:
            logger.exception("Failed to load competitor sites registry")
            self._custom = []

    def _save(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sites": [asdict(entry) for entry in self._custom],
        }
        self.registry_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def normalize_url(raw: str) -> str:
        text = raw.strip()
        if not text:
            raise ValueError("Укажите ссылку на сайт конкурента")
        if not text.startswith(("http://", "https://")):
            text = f"https://{text}"
        parsed = urlparse(text)
        if not parsed.netloc:
            raise ValueError("Некорректная ссылка")
        scheme = parsed.scheme or "https"
        return f"{scheme}://{parsed.netloc}{parsed.path or ''}".rstrip("/") or f"{scheme}://{parsed.netloc}"

    @staticmethod
    def domain_from_url(url: str) -> str:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if not host:
            raise ValueError("Не удалось определить домен")
        return host

    @staticmethod
    def _make_id(domain: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", domain.lower()).strip("_")[:40] or "site"
        return base

    def list_custom(self) -> list[CompetitorSiteEntry]:
        return list(self._custom)

    def get_custom(self, site_id: str) -> CompetitorSiteEntry | None:
        normalized = site_id.lower().strip()
        for entry in self._custom:
            if entry.id.lower() == normalized:
                return entry
        return None

    def as_competitor_sites(self) -> list[CompetitorSite]:
        return [
            CompetitorSite(
                domain=entry.domain,
                label=entry.label or entry.domain,
                search_url=entry.search_url,
            )
            for entry in self._custom
        ]

    def analyze_url(self, url: str, *, label: str = "") -> dict[str, str | bool | None]:
        normalized = self.normalize_url(url)
        domain = self.domain_from_url(normalized)
        fetch_url = normalized if urlparse(normalized).path not in ("", "/") else f"https://{domain}"

        title = ""
        notes = ""
        search_url: str | None = None
        status = "ok"
        rag_text = f"Сайт конкурента: {domain}\nURL: {normalized}\n"

        try:
            with httpx.Client(
                timeout=WEB_SEARCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
            ) as client:
                response = client.get(fetch_url)
                response.raise_for_status()
                html = response.text[:300_000]
                homepage_html = html
                if fetch_url != f"https://{domain}":
                    try:
                        home_response = client.get(f"https://{domain}")
                        home_response.raise_for_status()
                        homepage_html = home_response.text[:300_000]
                    except Exception:
                        homepage_html = html
        except Exception as exc:
            status = "fetch_failed"
            notes = f"Не удалось загрузить сайт: {exc}"
            rag_text += notes
            return {
                "url": normalized,
                "domain": domain,
                "label": label.strip() or domain,
                "title": title,
                "search_url": search_url,
                "notes": notes,
                "status": status,
                "rag_text": rag_text,
            }

        title_match = _TITLE_RE.search(html)
        if title_match:
            title = re.sub(r"\s+", " ", title_match.group(1)).strip()

        search_url = self._guess_search_url(homepage_html, domain)
        plain = re.sub(r"\s+", " ", _TAG_RE.sub(" ", html)).strip()
        rag_text += f"Заголовок: {title or domain}\n"
        rag_text += f"Страница: {fetch_url}\n"
        if search_url:
            rag_text += f"Поиск: {search_url}\n"
        rag_text += plain[:4000]

        if not search_url:
            notes = "Поисковая ссылка не определена автоматически — укажите вручную при необходимости"
        else:
            notes = "Поисковая ссылка определена автоматически"

        return {
            "url": normalized,
            "domain": domain,
            "label": label.strip() or title or domain,
            "title": title,
            "search_url": search_url,
            "catalog_url": normalized,
            "notes": notes,
            "status": status,
            "rag_text": rag_text,
        }

    @staticmethod
    def _guess_search_url(html: str, domain: str) -> str | None:
        root = f"https://{domain}"
        candidates: list[str] = []
        for href in _HREF_RE.findall(html):
            lower = href.lower()
            if any(marker in lower for marker in _SEARCH_SKIP_MARKERS):
                continue
            if not any(hint in lower for hint in _SEARCH_HINTS):
                continue
            if href.startswith("//"):
                absolute = f"https:{href}"
            elif href.startswith("http"):
                absolute = href
            elif href.startswith("/"):
                absolute = f"{root}{href}"
            else:
                absolute = f"{root}/{href.lstrip('/')}"
            parsed = urlparse(absolute)
            if domain not in parsed.netloc.lower():
                continue
            path_lower = parsed.path.lower()
            if any(path_lower.endswith(ext) for ext in (".css", ".js", ".map", ".xml")):
                continue
            candidates.append(absolute.split("#")[0])

        candidates.sort(
            key=lambda url: (
                0 if "/search" in urlparse(url).path.lower() else 1,
                0 if "{query}" in url else 1,
                len(url),
            )
        )

        for candidate in candidates:
            if "{query}" in candidate:
                return candidate
            if "?" in candidate:
                base, query = candidate.split("?", 1)
                parts = query.split("&")
                updated: list[str] = []
                replaced = False
                for part in parts:
                    key = part.split("=", 1)[0].lower()
                    if key in {"q", "query", "search", "text", "s"}:
                        updated.append(f"{part.split('=')[0]}={{query}}")
                        replaced = True
                    else:
                        updated.append(part)
                if replaced:
                    return f"{base}?{'&'.join(updated)}"
                return f"{candidate}&q={{query}}" if "?" in candidate else f"{candidate}?q={{query}}"

        return f"{root}/search?q={{query}}"

    @staticmethod
    def _normalize_optional_url(raw: str | None) -> str | None:
        text = (raw or "").strip()
        if not text:
            return None
        return CompetitorSiteManager.normalize_url(text)

    def get_draft(self, domain: str) -> CompetitorSiteDraft | None:
        normalized = domain.lower().removeprefix("www.")
        return self._drafts.get(normalized)

    def prepare_index_draft(
        self,
        url: str,
        *,
        label: str = "",
        product_sample_url: str | None = None,
        price_html_hint: str | None = None,
        articul_html_hint: str | None = None,
    ) -> tuple[CompetitorSiteDraft, dict[str, str | bool | None]]:
        analysis = self.analyze_url(url, label=label)
        domain = str(analysis["domain"])
        normalized = domain.lower().removeprefix("www.")

        from src.services.competitor_sites import get_builtin_competitor_site

        builtin_site = get_builtin_competitor_site(domain)
        resolved_label = str(analysis.get("label") or label.strip() or domain)
        resolved_search = analysis.get("search_url")
        if builtin_site:
            resolved_label = label.strip() or builtin_site.label
            resolved_search = builtin_site.search_url or resolved_search

        sample_url = self._normalize_optional_url(product_sample_url)
        draft = CompetitorSiteDraft(
            url=str(analysis["url"]),
            domain=domain,
            label=resolved_label,
            search_url=resolved_search,
            product_sample_url=sample_url,
            price_html_hint=(price_html_hint or "").strip() or None,
            articul_html_hint=(articul_html_hint or "").strip() or None,
            analysis=analysis,
            builtin=bool(builtin_site),
        )
        self._drafts[normalized] = draft
        return draft, analysis

    def mark_draft_indexed(self, domain: str, index_result: dict) -> CompetitorSiteDraft | None:
        normalized = domain.lower().removeprefix("www.")
        draft = self._drafts.get(normalized)
        if not draft:
            return None
        draft.indexed = True
        draft.index_result = index_result
        draft.indexed_at = self._now()
        return draft

    def clear_draft(self, domain: str) -> None:
        normalized = domain.lower().removeprefix("www.")
        self._drafts.pop(normalized, None)

    def add_from_indexed_draft(
        self,
        url: str,
        *,
        label: str = "",
        product_sample_url: str | None = None,
        price_html_hint: str | None = None,
        articul_html_hint: str | None = None,
    ) -> tuple[CompetitorSiteEntry, dict[str, str | bool | None]]:
        normalized_url = self.normalize_url(url)
        domain = self.domain_from_url(normalized_url)
        normalized = domain.lower().removeprefix("www.")
        draft = self._drafts.get(normalized)
        if not draft or not draft.indexed:
            raise ValueError(
                "Сначала нажмите «Проиндексировать» и дождитесь завершения индексации каталога"
            )

        existing = {entry.domain for entry in self._custom}
        if domain in existing:
            raise ValueError(f"Сайт {domain} уже добавлен")

        from src.services.competitor_sites import all_competitor_domains

        if domain in all_competitor_domains(include_custom=False):
            raise ValueError(
                f"Сайт {domain} уже есть во встроенном списке — "
                "добавление не требуется, используйте «Проиндексировать» для обновления каталога"
            )

        entry_id = self._make_id(domain)
        used = {entry.id for entry in self._custom}
        suffix = 2
        while entry_id in used:
            entry_id = f"{self._make_id(domain)}_{suffix}"
            suffix += 1

        resolved_search = draft.search_url
        if resolved_search and "{query}" not in resolved_search:
            resolved_search = f"{resolved_search.rstrip('/')}?q={{query}}"

        resolved_label = label.strip() or draft.label or domain
        resolved_sample = self._normalize_optional_url(product_sample_url) or draft.product_sample_url
        resolved_price_hint = (price_html_hint or "").strip() or draft.price_html_hint
        resolved_articul_hint = (articul_html_hint or "").strip() or draft.articul_html_hint

        catalog_urls = [draft.url]
        if resolved_sample and resolved_sample not in catalog_urls:
            catalog_urls.append(resolved_sample)

        now = self._now()
        entry = CompetitorSiteEntry(
            id=entry_id,
            url=draft.url,
            domain=domain,
            label=resolved_label,
            search_url=resolved_search,
            catalog_urls=catalog_urls,
            product_sample_url=resolved_sample,
            price_html_hint=resolved_price_hint,
            articul_html_hint=resolved_articul_hint,
            notes=str(draft.analysis.get("notes") or ""),
            builtin=False,
            title=str(draft.analysis.get("title") or ""),
            status=str(draft.analysis.get("status") or "ok"),
            added_at=now,
            analyzed_at=draft.indexed_at or now,
        )
        self._custom.append(entry)
        self._save()
        self._drafts.pop(normalized, None)

        from src.services.competitor_catalog_urls import get_competitor_catalog_url_registry

        registry = get_competitor_catalog_url_registry()
        for page_url in catalog_urls:
            registry.add_page(
                page_url,
                domain=domain,
                label=entry.label,
                source="site_add",
            )
        return entry, draft.analysis

    def add(
        self,
        url: str,
        *,
        label: str = "",
        search_url: str | None = None,
    ) -> tuple[CompetitorSiteEntry, dict[str, str | bool | None]]:
        return self.add_from_indexed_draft(
            url,
            label=label,
            product_sample_url=None,
            price_html_hint=None,
            articul_html_hint=None,
        )

    def remove(self, site_id: str) -> CompetitorSiteEntry:
        entry = self.get_custom(site_id)
        if not entry:
            raise ValueError(f"Сайт '{site_id}' не найден")
        if entry.builtin:
            raise ValueError("Встроенный сайт нельзя удалить")
        self._custom = [item for item in self._custom if item.id != entry.id]
        self._save()
        return entry


_manager: CompetitorSiteManager | None = None


def get_competitor_site_manager() -> CompetitorSiteManager:
    global _manager
    if _manager is None:
        _manager = CompetitorSiteManager()
    return _manager

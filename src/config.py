import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
WEB_BEHIND_PROXY = os.getenv("WEB_BEHIND_PROXY", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
PROXYAPI_API_KEY = os.getenv("PROXYAPI_API_KEY", "")
PROXYAPI_BASE_URL = os.getenv("PROXYAPI_BASE_URL", "https://api.proxyapi.ru/openai/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

MARKUP_PERCENT = float(os.getenv("MARKUP_PERCENT", "30"))
COMPANY_NAME = os.getenv("COMPANY_NAME", 'ООО «Учтендер»')
COMPANY_INN = os.getenv("COMPANY_INN", "")
COMPANY_KPP = os.getenv("COMPANY_KPP", "")
COMPANY_OGRN = os.getenv("COMPANY_OGRN", "")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "")
COMPANY_DIRECTOR = os.getenv(
    "COMPANY_DIRECTOR",
    "Генеральный директор Геккель Оксана Николаевна",
)
KP_SHEET_NAME = os.getenv("KP_SHEET_NAME", "КП")
KP_VAT_LABEL = os.getenv("KP_VAT_LABEL", "включая НДС 5%")
KP_TEMPLATES_DIR = PROJECT_ROOT / os.getenv("KP_TEMPLATES_DIR", "data/templates")
KP_STAMP_PATH = PROJECT_ROOT / os.getenv("KP_STAMP_PATH", "data/templates/kp_stamp.png")
KP_PDF_FONT_PATH = PROJECT_ROOT / os.getenv("KP_PDF_FONT_PATH", "data/templates/fonts/DejaVuSans.ttf")
KP_PDF_FONT_BOLD_PATH = PROJECT_ROOT / os.getenv(
    "KP_PDF_FONT_BOLD_PATH",
    "data/templates/fonts/DejaVuSans-Bold.ttf",
)
DELIVERY_TERMS = os.getenv("DELIVERY_TERMS", "адресная доставка включена в стоимость")
PAYMENT_TERMS = os.getenv("PAYMENT_TERMS", "безналичный расчет")
DELIVERY_DAYS = os.getenv("DELIVERY_DAYS", "15 рабочих дней после получения денежных средств")

CATALOG_PATH = PROJECT_ROOT / os.getenv("CATALOG_PATH", "data/catalog.xlsx")
GOODS_REPORT_PATH = PROJECT_ROOT / os.getenv("GOODS_REPORT_PATH", "data/goods_report.xlsx")
_procurement_report_raw = os.getenv("PROCUREMENT_REPORT_PATH", "").strip()
PROCUREMENT_REPORT_PATH = (
    PROJECT_ROOT / _procurement_report_raw if _procurement_report_raw else None
)
USE_GOODS_REPORT = os.getenv("USE_GOODS_REPORT", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
REGISTRY_PATH = PROJECT_ROOT / os.getenv("REGISTRY_PATH", "data/registry.xlsx")
REGISTRY_PHOTOS_DIR = PROJECT_ROOT / os.getenv("REGISTRY_PHOTOS_DIR", "data/registry_photos")

_price_lists_raw = os.getenv("PRICE_LISTS", "data/price_prirodovedenie.xls")
PRICE_LIST_PATHS = [
    PROJECT_ROOT / path.strip()
    for path in _price_lists_raw.split(",")
    if path.strip()
]

PRICES_DIR = PROJECT_ROOT / os.getenv("PRICES_DIR", "data/prices")
PRICES_REGISTRY_PATH = PROJECT_ROOT / os.getenv(
    "PRICES_REGISTRY_PATH", "data/prices_registry.json"
)
SOURCES_REGISTRY_PATH = PROJECT_ROOT / os.getenv(
    "SOURCES_REGISTRY_PATH", "data/sources_registry.json"
)

EXACT_MATCH_THRESHOLD = int(os.getenv("EXACT_MATCH_THRESHOLD", "90"))
SIMILAR_MATCH_THRESHOLD = int(os.getenv("SIMILAR_MATCH_THRESHOLD", "70"))
# Минимум совпадения в каталоге/прайсе для принятия локального совпадения
LOCAL_MATCH_THRESHOLD = int(os.getenv("LOCAL_MATCH_THRESHOLD", "95"))

OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

PII_ANONYMIZATION_ENABLED = os.getenv("PII_ANONYMIZATION_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PII_REDACT_ORG_DATA = os.getenv("PII_REDACT_ORG_DATA", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_extra_pii = os.getenv("PII_EXTRA_TERMS", "").strip()
PII_EXTRA_TERMS = [term.strip() for term in _extra_pii.split("|") if term.strip()]

USE_AI_INTERNET_SEARCH = os.getenv("USE_AI_INTERNET_SEARCH", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
WEB_SEARCH_EXACT_THRESHOLD = int(os.getenv("WEB_SEARCH_EXACT_THRESHOLD", "100"))
WEB_SEARCH_MAX_RESULTS = max(1, int(os.getenv("WEB_SEARCH_MAX_RESULTS", "3")))
WEB_PRICE_DISCOUNT_PERCENT = float(os.getenv("WEB_PRICE_DISCOUNT_PERCENT", "5"))
WEB_SEARCH_FETCH_PAGES = os.getenv("WEB_SEARCH_FETCH_PAGES", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
WEB_SEARCH_MAX_PAGE_FETCHES = max(0, int(os.getenv("WEB_SEARCH_MAX_PAGE_FETCHES", "2")))
WEB_SEARCH_TIMEOUT = float(os.getenv("WEB_SEARCH_TIMEOUT", "8"))
# Таймаут одного HTTP-запроса к сайту конкурента (короче общего WEB_SEARCH_TIMEOUT)
COMPETITOR_SEARCH_TIMEOUT = float(os.getenv("COMPETITOR_SEARCH_TIMEOUT", "5"))
# Макс. время интернет-поиска на одну позицию (0 — без лимита)
INTERNET_SEARCH_BUDGET_SECONDS = float(os.getenv("INTERNET_SEARCH_BUDGET_SECONDS", "25"))
# Лимит символов HTML при загрузке карточки товара
WEB_SEARCH_PAGE_MAX_CHARS = max(
    50_000, int(os.getenv("WEB_SEARCH_PAGE_MAX_CHARS", "200000"))
)
# Лимит для страниц поиска (выдача часто ниже по HTML, чем шапка/меню)
WEB_SEARCH_RESULTS_PAGE_MAX_CHARS = max(
    WEB_SEARCH_PAGE_MAX_CHARS,
    int(os.getenv("WEB_SEARCH_RESULTS_PAGE_MAX_CHARS", "700000")),
)

COMPETITOR_SEARCH_ENABLED = os.getenv("COMPETITOR_SEARCH_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
COMPETITOR_SEARCH_BATCH_SIZE = max(1, int(os.getenv("COMPETITOR_SEARCH_BATCH_SIZE", "3")))
COMPETITOR_SEARCH_MAX_RESULTS = max(
    1, int(os.getenv("COMPETITOR_SEARCH_MAX_RESULTS", "5"))
)
COMPETITOR_SEARCH_FALLBACK_THRESHOLD = int(
    os.getenv("COMPETITOR_SEARCH_FALLBACK_THRESHOLD", "95")
)
COMPETITOR_NATIVE_SEARCH_ENABLED = os.getenv(
    "COMPETITOR_NATIVE_SEARCH_ENABLED", "true"
).lower() in {
    "1",
    "true",
    "yes",
    "on",
}
COMPETITOR_NATIVE_SEARCH_MAX_FETCHES = max(
    1, int(os.getenv("COMPETITOR_NATIVE_SEARCH_MAX_FETCHES", "8"))
)
COMPETITOR_SEARCH_PARALLEL_WORKERS = max(
    1, int(os.getenv("COMPETITOR_SEARCH_PARALLEL_WORKERS", "14"))
)
# Параллельная индексация каталога конкурента (страницы товаров из sitemap)
COMPETITOR_INDEX_WORKERS = max(1, int(os.getenv("COMPETITOR_INDEX_WORKERS", "12")))
COMPETITOR_INDEX_MAX_URLS = max(100, int(os.getenv("COMPETITOR_INDEX_MAX_URLS", "8000")))
COMPETITOR_INDEX_REQUEST_TIMEOUT = float(
    os.getenv("COMPETITOR_INDEX_REQUEST_TIMEOUT", "6")
)

MEILISEARCH_ENABLED = os.getenv("MEILISEARCH_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MEILISEARCH_HOST = os.getenv("MEILISEARCH_HOST", "http://127.0.0.1:7700").rstrip("/")
MEILISEARCH_API_KEY = os.getenv("MEILISEARCH_API_KEY", "masterKey")
MEILISEARCH_INDEX = os.getenv("MEILISEARCH_INDEX", "products")
MEILISEARCH_AUTO_SYNC = os.getenv("MEILISEARCH_AUTO_SYNC", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MEILISEARCH_SEARCH_LIMIT = max(5, int(os.getenv("MEILISEARCH_SEARCH_LIMIT", "20")))

KP_PARALLEL_WORKERS = max(1, int(os.getenv("KP_PARALLEL_WORKERS", "4")))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip()
LOG_DIR = os.getenv("LOG_DIR", str(PROJECT_ROOT / "logs"))
LOG_FILE = os.getenv("LOG_FILE", "").strip()

SEARCH_KIT_COMPONENT_LINKS = os.getenv("SEARCH_KIT_COMPONENT_LINKS", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

TZ_PDF_OCR_ENABLED = os.getenv("TZ_PDF_OCR_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TZ_OCR_LANG = os.getenv("TZ_OCR_LANG", "rus+eng")
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip()

RAG_ENABLED = os.getenv("RAG_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
RAG_EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small").strip()
RAG_CHUNK_SIZE = max(300, int(os.getenv("RAG_CHUNK_SIZE", "1200")))
RAG_CHUNK_OVERLAP = max(0, int(os.getenv("RAG_CHUNK_OVERLAP", "150")))
RAG_TOP_K = max(1, int(os.getenv("RAG_TOP_K", "5")))
RAG_DOCS_INDEX_DIR = PROJECT_ROOT / os.getenv("RAG_DOCS_INDEX_DIR", "data/rag_docs_index")
RAG_DOCS_INDEX_DIR.mkdir(parents=True, exist_ok=True)

COMPETITOR_SITES_REGISTRY_PATH = PROJECT_ROOT / os.getenv(
    "COMPETITOR_SITES_REGISTRY_PATH", "data/competitor_sites_registry.json"
)
COMPETITOR_CATALOG_DB_PATH = PROJECT_ROOT / os.getenv(
    "COMPETITOR_CATALOG_DB_PATH", "data/competitor_catalog.db"
)
COMPETITOR_PRODUCTS_PATH = PROJECT_ROOT / os.getenv(
    "COMPETITOR_PRODUCTS_PATH", "data/competitor_products.json"
)
COMPETITOR_PRODUCTS_JSON_EXPORT = os.getenv(
    "COMPETITOR_PRODUCTS_JSON_EXPORT", "false"
).strip().lower() in {"1", "true", "yes", "on"}
COMPETITOR_CATALOG_URLS_PATH = PROJECT_ROOT / os.getenv(
    "COMPETITOR_CATALOG_URLS_PATH", "data/competitor_catalog_urls.json"
)
KP_SESSIONS_PATH = PROJECT_ROOT / os.getenv("KP_SESSIONS_PATH", "data/kp_sessions.json")

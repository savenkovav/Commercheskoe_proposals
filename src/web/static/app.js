const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const ALLOWED_TZ_EXTENSIONS = [".docx", ".pdf", ".xlsx", ".xls"];

function isAllowedTzFile(name) {
  const lower = name.toLowerCase();
  return ALLOWED_TZ_EXTENSIONS.some((ext) => lower.endsWith(ext));
}
const fmtMoney = (v) =>
  v == null ? "—" : `${Number(v).toLocaleString("ru-RU", { minimumFractionDigits: 2 })} ₽`;

const fmtCompetitorPrice = (item) => {
  if (item?.price_label) return item.price_label;
  if (typeof item?.price === "string" && item.price && Number.isNaN(Number(item.price))) {
    return item.price;
  }
  const retail = item?.price ?? item?.cost;
  const wholesale = item?.wholesale_price;
  if (retail != null && wholesale != null && Number(wholesale) !== Number(retail)) {
    return `${fmtMoney(retail)} · опт ${fmtMoney(wholesale)}`;
  }
  if (retail != null) return fmtMoney(retail);
  if (wholesale != null) return `опт ${fmtMoney(wholesale)}`;
  return "—";
};

const fmtQty = (value, unit = "шт.") => {
  if (value == null) return "—";
  const qty = Number(value);
  const text = Number.isInteger(qty)
    ? qty.toLocaleString("ru-RU")
    : qty.toLocaleString("ru-RU", { maximumFractionDigits: 3 });
  return `${text} ${unit || "шт."}`.trim();
};

const lineSum = (unitPrice, quantity) => {
  if (unitPrice == null || quantity == null) return null;
  return Math.round(Number(unitPrice) * Number(quantity) * 100) / 100;
};

const escapeHtml = (value) =>
  String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

function shortenUrlForDisplay(url, maxLen = 56) {
  if (!url) return "";
  if (url.length <= maxLen) return url;
  try {
    const parsed = new URL(url);
    const segment = parsed.pathname.split("/").filter(Boolean).pop() || "";
    const compact = segment ? `${parsed.hostname}/…/${segment}` : parsed.hostname;
    return compact.length <= maxLen ? compact : `${compact.slice(0, maxLen - 1)}…`;
  } catch {
    return `${url.slice(0, maxLen - 1)}…`;
  }
}

function renderChatLink(url, label) {
  if (!url) return "";
  const text = label || shortenUrlForDisplay(url);
  return `<a class="chat-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(url)}">${escapeHtml(text)}</a>`;
}

const statusBadge = (status, notes = "") => {
  if (notes && notes.includes("Ожидает поиска")) {
    return `<span class="badge badge--pending">Ожидает</span>`;
  }
  const labels = { exact: "Точно", similar: "Похоже", not_found: "Не найдено" };
  return `<span class="badge badge--${status}">${labels[status] || status}</span>`;
};

const taskModeLabel = (mode) => {
  if (mode === "task1_task2") return "Задача 1+2";
  if (mode === "task1") return "Задача 1";
  return "не выбрана";
};

const hasKpDownload = (data) => {
  const s = data?.summary;
  if (!s?.download_url) return false;
  return Boolean(data?.has_download) || (s.filename || "").startsWith("KP_");
};

const stageLabel = (stage, searchCompleted) => {
  const map = {
    intake: "ожидание ТЗ",
    parsed: "ТЗ разобрано",
    searched: "поиск выполнен",
    exported: "Excel готов",
  };
  if (!searchCompleted && stage === "parsed") return "поиск не запускался";
  return map[stage] || stage;
};

function updateAssistantMode(data) {
  const taskEl = $("#taskModeLabel");
  const stageEl = $("#stageLabel");
  if (!taskEl || !stageEl) return;
  taskEl.textContent = taskModeLabel(data?.task_mode);
  stageEl.textContent = stageLabel(data?.stage, data?.search_completed);
}

function showToast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("toast--error", isError);
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 4000);
}

function showOverlay(text = "Обработка...") {
  $("#overlayText").textContent = text;
  $("#overlay").classList.remove("hidden");
}

function hideOverlay() {
  $("#overlay").classList.add("hidden");
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Ошибка ${res.status}`);
  }
  return data;
}

function initTabs() {
  $$(".tabs__btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tabs__btn").forEach((b) => b.classList.remove("tabs__btn--active"));
      $$(".panel").forEach((p) => p.classList.remove("panel--active"));
      btn.classList.add("tabs__btn--active");
      $(`#panel-${btn.dataset.tab}`).classList.add("panel--active");
      if (btn.dataset.tab === "prices") loadPrices();
      if (btn.dataset.tab === "competitors") loadCompetitors();
      if (btn.dataset.tab === "status") loadStatus();
    });
  });
}

function setMarkupInput(value) {
  const input = $("#markupPercent");
  if (input) {
    input.value = String(value);
  }
}

async function applyMarkup() {
  const input = $("#markupPercent");
  const raw = String(input.value).replace(",", ".").trim();
  const value = Number(raw);

  if (!Number.isFinite(value) || value < 0 || value > 1000) {
    showToast("Наценка должна быть от 0 до 1000%", true);
    return;
  }

  try {
    const data = await api("/api/markup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markup_percent: value }),
    });
    setMarkupInput(data.markup_percent);
    showToast(`Наценка ${data.markup_percent}% применена`);
    loadStatus();
  } catch (e) {
    showToast(e.message, true);
  }
}

function initMarkup() {
  $("#btnApplyMarkup").addEventListener("click", applyMarkup);
  $("#markupPercent").addEventListener("keydown", (e) => {
    if (e.key === "Enter") applyMarkup();
  });
}

let cachedStatus = null;

function isAiUseRequested() {
  return Boolean($("#useAiUpload")?.checked);
}

function getEffectiveAiEnabled(status) {
  return Boolean(status?.ai_enabled && isAiUseRequested());
}

function aiStatusText(enabled, titleCase = false) {
  if (enabled) return titleCase ? "Вкл" : "вкл";
  return titleCase ? "Выкл" : "выкл";
}

function refreshAiStatusUi() {
  if (!cachedStatus) return;
  renderHeaderStats(cachedStatus);
  const aiValue = $("#statusAiCard .metric__value");
  if (aiValue) {
    aiValue.textContent = aiStatusText(getEffectiveAiEnabled(cachedStatus), true);
  }
}

function fmtCount(v) {
  if (v == null || Number.isNaN(Number(v))) return "0";
  return Number(v)
    .toLocaleString("ru-RU")
    .replace(/\u00a0/g, "\u202f")
    .replace(/ /g, "\u202f");
}

/** Без разделителя тысяч — для компактного pill в шапке (5981, не «5 981»). */
function fmtCountPlain(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "0";
  return String(Math.trunc(n));
}

function competitorStatsRows(status) {
  const byDomain = status?.competitor_products_by_domain;
  if (!byDomain || typeof byDomain !== "object") return [];
  return Object.entries(byDomain).sort((a, b) => b[1] - a[1]);
}

function renderCompetitorTooltip(status) {
  const rows = competitorStatsRows(status);
  if (!rows.length) {
    return `<div class="stat-tooltip__title">По сайтам</div><div class="stat-tooltip__empty">Нет проиндексированных товаров</div>`;
  }
  const body = rows
    .map(
      ([domain, count]) =>
        `<div class="stat-tooltip__row"><span>${escapeHtml(domain)}</span><strong>${fmtCountPlain(count)}</strong></div>`,
    )
    .join("");
  return `<div class="stat-tooltip__title">По сайтам</div>${body}`;
}

function applyCompetitorCatalogStats(stats) {
  if (!stats || cachedStatus == null) return;
  cachedStatus.competitor_products_count = stats.products ?? 0;
  cachedStatus.competitor_sites_count = stats.sites ?? 0;
  cachedStatus.competitor_products_by_domain = stats.by_domain ?? {};
  renderHeaderStats(cachedStatus);
}

function renderHeaderStats(status) {
  const aiOn = getEffectiveAiEnabled(status);
  const competitorCount = Number(status.competitor_products_count);
  const competitorLabel = Number.isFinite(competitorCount)
    ? fmtCountPlain(competitorCount)
    : "0";
  $("#headerStats").innerHTML = `
    <span class="stat-pill">Каталог: <strong>${fmtCount(status.catalog_count)}</strong></span>
    <span class="stat-pill">Прайсы: <strong>${fmtCount(status.price_items_count)}</strong></span>
    <span class="stat-pill stat-pill--sites has-tooltip" tabindex="0">
      На сайтах:<span class="stat-pill__num">${competitorLabel}</span>
      <span class="stat-tooltip" role="tooltip">${renderCompetitorTooltip(status)}</span>
    </span>
    <span class="stat-pill">AI: <strong>${aiStatusText(aiOn)}</strong></span>
  `;
}

async function loadInitialStatus() {
  try {
    const status = await api(`/api/status?t=${Date.now()}`);
    cachedStatus = status;
    renderHeaderStats(status);
    setMarkupInput(status.markup_percent ?? 30);
  } catch (e) {
    showToast(e.message, true);
  }
}

async function loadStatus() {
  try {
    const status = await api(`/api/status?t=${Date.now()}`);
    cachedStatus = status;
    renderHeaderStats(status);
    setMarkupInput(status.markup_percent ?? 30);

    const aiOn = getEffectiveAiEnabled(status);
    $("#statusGrid").innerHTML = `
      <div class="card"><div class="metric"><div class="metric__label">Каталог</div><div class="metric__value">${status.catalog_count}</div></div></div>
      <div class="card"><div class="metric"><div class="metric__label">Реестр</div><div class="metric__value">${status.registry_count}</div></div></div>
      <div class="card"><div class="metric"><div class="metric__label">Прайсы</div><div class="metric__value">${status.price_items_count}</div></div></div>
      <div class="card"><div class="metric"><div class="metric__label">Наценка</div><div class="metric__value">${status.markup_percent}%</div></div></div>
      <div class="card" id="statusAiCard"><div class="metric"><div class="metric__label">AI</div><div class="metric__value">${aiStatusText(aiOn, true)}</div></div></div>
      <div class="card"><div class="metric"><div class="metric__label">Защита ПДн</div><div class="metric__value">${status.pii_enabled ? "Вкл" : "Выкл"}</div></div></div>
    `;

    const list = $("#statusPriceList");
    const sourceRows = [
      status.catalog
        ? `<li><strong>${status.catalog.type_label}</strong> (${status.catalog.filename}) — ${status.catalog.items_count} поз.</li>`
        : null,
      status.registry
        ? `<li><strong>${status.registry.type_label}</strong> (${status.registry.filename}) — ${status.registry.items_count} поз.</li>`
        : null,
      ...(status.price_files || []).map(
        (p) =>
          `<li><strong>${p.name}</strong> (${p.id}) — ${p.items_count} поз., ${p.supplier}</li>`,
      ),
    ].filter(Boolean);

    if (!sourceRows.length) {
      list.innerHTML = "<li class='muted'>Таблицы не загружены</li>";
    } else {
      list.innerHTML = sourceRows.join("");
    }
  } catch (e) {
    showToast(e.message, true);
  }
}

const SOURCE_LABELS = {
  catalog: "Каталог",
  registry: "Реестр",
  price_list: "Прайс",
  web: "Интернет",
  ai: "AI",
  none: "—",
};

const LOCAL_MIN_MATCH_PERCENT = 95;
const WEB_MIN_MATCH_PERCENT = 100;
const COMPETITOR_MIN_MATCH_PERCENT = 95;

function isMarketplaceUrl(url) {
  if (!url) return false;
  const lower = url.toLowerCase();
  return (
    lower.includes("ozon.ru") ||
    lower.includes("wildberries.ru") ||
    lower.includes("market.yandex.ru")
  );
}

const COMPETITOR_DOMAINS = [
  "xn----7sbbumkojddmeoc1a7r.xn--p1acf",
  "n-72.ru",
  "stronikum.ru",
  "labkabinet.ru",
  "vrtorg.ru",
  "td-school.ru",
  "epp24.ru",
  "zarnitza.ru",
  "rostcom.com",
  "rene-edu.ru",
  "prioritet1.com",
  "orionedu.ru",
  "xn--54-vlc3b6bza.xn--p1ai",
  "skale.ru",
];

function hostFromUrl(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "").toLowerCase();
  } catch {
    return "";
  }
}

function isCompetitorUrl(url) {
  const host = hostFromUrl(url);
  if (!host) return false;
  return COMPETITOR_DOMAINS.some(
    (domain) => host === domain || host.endsWith("." + domain)
  );
}

function isSearchListingUrl(url) {
  if (!url) return false;
  const lower = url.toLowerCase();
  return (
    lower.includes("/search?") ||
    lower.includes("/search/") ||
    lower.includes("search?text=") ||
    lower.includes("catalog/0/search") ||
    lower.includes("?q=")
  );
}

function isProductPageUrl(url) {
  if (!url || isSearchListingUrl(url)) return false;
  const lower = url.toLowerCase();
  if (lower.includes("ozon.ru/product/")) return true;
  if (lower.includes("market.yandex.ru/product/")) return true;
  if (/wildberries\.ru\/catalog\/\d+\/detail\.aspx/i.test(lower)) return true;
  if (isMarketplaceUrl(url)) return false;
  return lower.startsWith("http://") || lower.startsWith("https://");
}

function webQuoteRank(q) {
  const url = q.url || "";
  const score = q.match_score || 0;
  const hasPrice = q.price != null || q.cost != null;
  const marketplace = isMarketplaceUrl(url);
  const productPage = isProductPageUrl(url);
  const searchPage = isSearchListingUrl(url);
  const competitor = isCompetitorUrl(url);
  const minScore = competitor ? COMPETITOR_MIN_MATCH_PERCENT : WEB_MIN_MATCH_PERCENT;
  const priceValue = q.price != null ? q.price : q.cost;
  const priceSort = priceValue != null ? Number(priceValue) : Number.POSITIVE_INFINITY;
  let tier = 9;
  if (!searchPage && score >= minScore) {
    if (competitor && hasPrice) tier = 0;
    else if (competitor && !hasPrice) tier = 3;
    else if (!marketplace && hasPrice) tier = 1;
    else if (productPage && hasPrice) tier = 2;
    else tier = 4;
  }
  return [tier, priceSort, productPage ? 0 : 1, -score, hasPrice ? 0 : 1];
}

function quoteMeetsMatchThreshold(q) {
  if (!q || q.match_score == null) return q?.source !== "web";
  if (q.source === "web") {
    const minScore = isCompetitorUrl(q.url) ? COMPETITOR_MIN_MATCH_PERCENT : WEB_MIN_MATCH_PERCENT;
    return q.match_score >= minScore;
  }
  return q.match_score >= LOCAL_MIN_MATCH_PERCENT;
}

function collectWebEntries(item) {
  const seen = new Set();
  const rows = [];
  for (const q of [...(item.comparison || []), ...(item.competitors || [])]) {
    if (q.source !== "web") continue;
    if (!quoteMeetsMatchThreshold(q)) continue;
    if (isSearchListingUrl(q.url)) continue;
    const key = q.url || `${q.label}|${q.matched_name}|${q.price}`;
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push(q);
  }
  rows.sort((a, b) => {
    const ka = webQuoteRank(a);
    const kb = webQuoteRank(b);
    for (let i = 0; i < ka.length; i += 1) {
      if (ka[i] !== kb[i]) return ka[i] - kb[i];
    }
    return 0;
  });
  return rows;
}

function hasItemDetails(item) {
  return (
    item.internet_priced ||
    collectWebEntries(item).length > 0 ||
    item.status === "similar" ||
    item.status === "not_found" ||
    (item.comparison && item.comparison.length) ||
    (item.competitors && item.competitors.length) ||
    (item.kit_components && item.kit_components.length) ||
    item.matched_name ||
    item.notes ||
    (item.alternatives && item.alternatives.length)
  );
}

const SOURCE_DETAIL_URL_RE = /https?:\/\/[^\s|]+/;

function parseSourceDetailText(text) {
  if (!text) return { label: "", url: null };
  const match = text.match(SOURCE_DETAIL_URL_RE);
  if (!match) return { label: text.trim(), url: null };
  const url = match[0].replace(/[|,;]+$/, "");
  const label = text
    .slice(0, match.index)
    .replace(/\s*[|–—-]\s*$/, "")
    .trim();
  return { label: label || text.trim(), url };
}

function resolveItemSourceUrl(item) {
  const parsed = parseSourceDetailText(item.source_detail || "");
  if (parsed.url) return parsed.url;
  if (item.internet_url) return item.internet_url;

  const allWebQuotes = [...(item.comparison || []), ...(item.competitors || [])].filter(
    (q) => q.source === "web" && q.url,
  );

  if (item.internet_priced && item.unit_base_price != null) {
    const priced = allWebQuotes.find((q) => {
      const price = q.price ?? q.cost;
      return price != null && Math.abs(price - item.unit_base_price) < 0.01;
    });
    if (priced?.url) return priced.url;
  }

  const webEntries = collectWebEntries(item);
  if (item.internet_priced && item.unit_base_price != null) {
    const selected = webEntries.find((q) => {
      const price = q.price ?? q.cost;
      return (
        q.url &&
        price != null &&
        Math.abs(price - item.unit_base_price) < 0.01
      );
    });
    if (selected?.url) return selected.url;
  }

  for (const q of webEntries) {
    if (q.url && !isSearchListingUrl(q.url)) return q.url;
  }
  for (const q of allWebQuotes) {
    if (q.url && !isSearchListingUrl(q.url)) return q.url;
  }
  return null;
}

function renderSourceDetailLine(item) {
  if (!item.source_detail) return null;
  const parsed = parseSourceDetailText(item.source_detail);
  const label = parsed.label || item.source_detail;
  const url = parsed.url || resolveItemSourceUrl(item);
  if (url) {
    return `<strong>Детали:</strong> <a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;
  }
  return `<strong>Детали:</strong> ${escapeHtml(label)}`;
}

function renderPrimaryMatchBlock(item) {
  const webEntries = collectWebEntries(item);
  const lines = [
    item.internet_priced
      ? `<strong>Источник:</strong> Интернет · цена КП −5% от найденной`
      : null,
    item.matched_name
      ? `<strong>Выбрано:</strong> ${escapeHtml(item.matched_name)} (${Math.round(item.match_score || 0)}%)`
      : null,
    !item.internet_priced && item.source
      ? `<strong>Источник:</strong> ${escapeHtml(SOURCE_LABELS[item.source] || item.source)}`
      : null,
    !item.source_detail && item.internet_url
      ? `<strong>Ссылка:</strong> <a href="${escapeHtml(item.internet_url)}" target="_blank" rel="noopener">${escapeHtml(item.internet_url)}</a>`
      : null,
    renderSourceDetailLine(item),
    item.unit_base_price != null
      ? `<strong>Цена баз.:</strong> ${fmtMoney(item.unit_base_price)}`
      : null,
    item.unit_price != null
      ? `<strong>Цена КП:</strong> ${fmtMoney(item.unit_price)}`
      : null,
    item.notes ? `<strong>Примечание:</strong> ${escapeHtml(item.notes)}` : null,
    item.alternatives && item.alternatives.length
      ? `<strong>Альтернативы:</strong> ${item.alternatives.map(escapeHtml).join(" · ")}`
      : null,
  ].filter(Boolean);
  if (!lines.length) return "";
  return `<div class="compare-block__primary">${lines.join("<br>")}</div>`;
}

function renderWebComparisonRows(webEntries, item) {
  if (!webEntries.length) return "";
  const rows = webEntries
    .map((q) => {
      const webPrice = q.price ?? q.cost;
      const isSelected =
        item.internet_priced &&
        item.unit_base_price != null &&
        webPrice != null &&
        Math.abs(webPrice - item.unit_base_price) < 0.01;
      const kpPrice = isSelected
        ? item.unit_price
        : webPrice != null && item.internet_priced
          ? Math.round(webPrice * 0.95 * 100) / 100
          : webPrice;
      return `
      <tr class="compare-row--competitor${isSelected ? " compare-row--selected" : ""}">
        <td>${escapeHtml(q.label || "Интернет")}</td>
        <td>${escapeHtml(q.matched_name || "—")}</td>
        <td>${fmtMoney(webPrice)}</td>
        <td>${fmtMoney(kpPrice)}</td>
        <td>—</td>
        <td>—</td>
        <td>${q.match_score ? `${Math.round(q.match_score)}%` : "—"}</td>
        <td>${
          q.url
            ? `<a href="${escapeHtml(q.url)}" target="_blank" rel="noopener">${escapeHtml(q.url)}</a>`
            : escapeHtml(q.notes || "—")
        }</td>
      </tr>`;
    })
    .join("");
  return `
      <h4 class="compare-block__subtitle">Интернет</h4>
      <table class="compare-table compare-table--web">
        <thead>
          <tr>
            <th>Источник</th>
            <th>Найдено</th>
            <th>Цена в интернете</th>
            <th>Цена КП</th>
            <th>Поставщик</th>
            <th>Дата покупки</th>
            <th>Совпадение</th>
            <th>Ссылка / примечание</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
}

function renderComparisonTable(item) {
  const comparison = (item.comparison || []).filter(
    (q) => q.source !== "web" && quoteMeetsMatchThreshold(q),
  );
  const webEntries = collectWebEntries(item);
  const primaryBlock = renderPrimaryMatchBlock(item);
  if (
    !comparison.length &&
    !webEntries.length &&
    !(item.kit_components || []).length &&
    !primaryBlock
  ) {
    return "";
  }

  const comparisonRows = comparison
    .map(
      (q) => `
      <tr>
        <td>${escapeHtml(q.label)}</td>
        <td>${escapeHtml(q.matched_name || "—")}</td>
        <td>${fmtMoney(q.cost ?? q.price)}</td>
        <td>${fmtMoney(q.price)}</td>
        <td>${escapeHtml(q.supplier || "—")}</td>
        <td>${escapeHtml(q.purchase_date || "—")}</td>
        <td>${q.match_score ? `${Math.round(q.match_score)}%` : "—"}</td>
        <td>${escapeHtml(q.notes || "—")}</td>
      </tr>`,
    )
    .join("");

  const kitRows = (item.kit_components || [])
    .map((k) => {
      const catalogLabel = k.found_in_catalog
        ? `<br><small class="muted">каталог: ${escapeHtml(k.catalog_matched_name || k.name)}</small>`
        : "";
      const supplierCell = k.found_in_catalog ? escapeHtml(k.supplier || "—") : "—";
      const dateCell = k.found_in_catalog ? escapeHtml(k.purchase_date || "—") : "—";
      const supplierRow =
        k.found_in_catalog && k.supplier
          ? `
      <tr class="compare-row--supplier">
        <td colspan="2">↳ ${escapeHtml(k.supplier)}</td>
        <td>—</td>
        <td>${fmtMoney(k.unit_price)}</td>
        <td>${escapeHtml(k.supplier)}</td>
        <td>${escapeHtml(k.purchase_date || "—")}</td>
        <td>${fmtQty(k.quantity, "шт")}</td>
        <td>${k.price_list_price != null ? `прайс: ${fmtMoney(k.price_list_price)}` : "—"}</td>
      </tr>`
          : "";
      return `
      <tr>
        <td colspan="2">${escapeHtml(k.name)}${catalogLabel}</td>
        <td>${fmtMoney(k.unit_cost)}</td>
        <td>${fmtMoney(k.unit_price)}</td>
        <td>${supplierCell}</td>
        <td>${dateCell}</td>
        <td>${fmtQty(k.quantity, "шт")}</td>
        <td>${k.competitor_url ? `<a href="${escapeHtml(k.competitor_url)}" target="_blank" rel="noopener">${escapeHtml(k.competitor_platform || "конкурент")}</a>` : k.price_list_price != null ? `прайс: ${fmtMoney(k.price_list_price)}` : "—"}</td>
      </tr>${supplierRow}`;
    })
    .join("");

  const meta = [];
  if (item.supplier) meta.push(`Поставщик: ${escapeHtml(item.supplier)}`);
  if (item.purchase_date) meta.push(`Дата покупки: ${escapeHtml(item.purchase_date)}`);
  if (item.is_kit) meta.push("Комплект");

  return `
    <div class="compare-block">
      ${primaryBlock}
      ${meta.length ? `<p class="compare-block__meta">${meta.join(" · ")}</p>` : ""}
      ${renderWebComparisonRows(webEntries, item)}
      ${
        comparisonRows
          ? `
      <table class="compare-table">
        <thead>
          <tr>
            <th>Источник</th>
            <th>Найдено</th>
            <th>Себест.</th>
            <th>Цена</th>
            <th>Поставщик</th>
            <th>Дата покупки</th>
            <th>Совпадение</th>
            <th>Примечание</th>
          </tr>
        </thead>
        <tbody>${comparisonRows}</tbody>
      </table>`
          : ""
      }
      ${
        kitRows
          ? `
      <h4 class="compare-block__subtitle">Состав комплекта</h4>
      <table class="compare-table compare-table--kit">
        <thead>
          <tr>
            <th colspan="2">Позиция</th>
            <th>Себест.</th>
            <th>Цена</th>
            <th>Поставщик</th>
            <th>Дата покупки</th>
            <th>Кол-во</th>
            <th>Прайс</th>
          </tr>
        </thead>
        <tbody>${kitRows}</tbody>
      </table>`
          : ""
      }
    </div>`;
}

let kpSessionId = null;
let kpSessionPromise = null;
let kpChatMessages = [];
let kpChatLoading = false;

async function ensureKpSession() {
  if (kpSessionId) return kpSessionId;
  if (!kpSessionPromise) {
    kpSessionPromise = api("/api/kp/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ use_ai: $("#useAiUpload")?.checked ?? true }),
    })
      .then((data) => {
        kpSessionId = data.session_id;
        if (data.welcome_reply && !kpChatMessages.length) {
          kpChatMessages.push({
            role: "assistant",
            text: data.welcome_reply,
            ts: Date.now(),
          });
        }
        return kpSessionId;
      })
      .finally(() => {
        kpSessionPromise = null;
      });
  }
  return kpSessionPromise;
}

function renderKpChatMessages() {
  const box = $("#kpChatMessages");
  if (!box) return;

  if (!kpChatMessages.length) {
    box.innerHTML = `
      <div class="chat-welcome">
        <p>Напишите название товара — поиск сразу по каталогу, прайсам, реестру и конкурентам. Для КП по ТЗ загрузите файл ниже.</p>
      </div>`;
    return;
  }

  box.innerHTML = kpChatMessages
    .map((msg) => {
      if (msg.role === "user") {
        return `
          <div class="chat-msg chat-msg--user">
            <div class="chat-msg__bubble">${escapeHtml(msg.text)}</div>
            <span class="chat-msg__time">${formatChatTime(msg.ts)}</span>
          </div>`;
      }
      if (msg.role === "error") {
        return `
          <div class="chat-msg chat-msg--error">
            <div class="chat-msg__bubble">${escapeHtml(msg.text)}</div>
            <span class="chat-msg__time">${formatChatTime(msg.ts)}</span>
          </div>`;
      }
      let actions = "";
      if (msg.actions?.run_local_search) {
        actions += `<div class="kp-chat-actions">Выполнен поиск по внутренним источникам</div>`;
      }
      if (msg.actions?.run_web_search) {
        actions += `<div class="kp-chat-actions">Добавлен анализ конкурентов</div>`;
      }
      if (msg.actions?.generate_excel) {
        actions += `<div class="kp-chat-actions">Excel обновлён</div>`;
      }
      if (msg.download_url) {
        actions += `<div class="kp-chat-actions"><a class="btn btn--secondary btn--small" href="${msg.download_url}" download>Скачать Excel</a></div>`;
      }
      if (msg.actions?.reprocessed_items?.length) {
        actions += `<div class="kp-chat-actions">Позиции: ${msg.actions.reprocessed_items.join(", ")}</div>`;
      }
      const lookupHtml = msg.lookup ? renderLookupResultHtml(msg.lookup) : "";
      return `
        <div class="chat-msg chat-msg--assistant">
          <div class="chat-msg__bubble">${escapeHtml(msg.text)}${actions}</div>
          ${lookupHtml ? `<div class="chat-msg__bubble chat-result">${lookupHtml}</div>` : ""}
          <span class="chat-msg__time">${formatChatTime(msg.ts)}</span>
        </div>`;
    })
    .join("");

  if (kpChatLoading) {
    box.insertAdjacentHTML(
      "beforeend",
      `<div class="chat-msg chat-msg--assistant">
        <div class="chat-msg__bubble">
          <span class="chat-msg__typing"><span></span><span></span><span></span></span>
          Обрабатываю запрос...
        </div>
      </div>`,
    );
  }

  requestAnimationFrame(() => {
    box.scrollTop = box.scrollHeight;
  });
}

function updateKpChatFormState() {
  const input = $("#kpChatInput");
  const sendBtn = $("#btnKpChatSend");
  const enabled = !kpChatLoading;
  if (input) input.disabled = !enabled;
  if (sendBtn) sendBtn.disabled = !enabled;
  $("#kpChatHints")?.querySelectorAll(".kp-chat-hint").forEach((btn) => {
    btn.disabled = !enabled;
  });
}

function resetKpChat(sessionId) {
  kpSessionId = sessionId || null;
  kpChatMessages = [];
  kpChatLoading = false;
  updateKpChatFormState();
  renderKpChatMessages();
}

async function sendKpChatMessage(text, isRetry = false) {
  const message = text.trim();
  if (!message || kpChatLoading) return;

  if (!isRetry) {
    kpChatMessages.push({ role: "user", text: message, ts: Date.now() });
  }
  kpChatLoading = true;
  updateKpChatFormState();
  renderKpChatMessages();

  try {
    await ensureKpSession();
    const data = await api("/api/kp/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: kpSessionId, message }),
    });
    if (data.session_id) {
      kpSessionId = data.session_id;
    }
    if (data.session_recreated && !isRetry) {
      kpChatMessages.push({
        role: "assistant",
        text: "Сессия была обновлена. Поиск по товару продолжается; для КП по ТЗ загрузите файл заново.",
        ts: Date.now(),
      });
    }
    kpChatMessages.push({
      role: "assistant",
      text: data.reply,
      actions: data.actions,
      lookup: data.lookup || null,
      ts: Date.now(),
    });
    if (data.markup_percent != null) {
      setMarkupInput(data.markup_percent);
    }
    renderProcessResult(data);
    if (data.actions?.generate_excel) {
      showToast("Excel сформирован");
    } else if (data.lookup) {
      showToast("Сводка по позиции готова");
    } else if (data.actions?.run_local_search) {
      showToast("Поиск выполнен");
    } else {
      showToast("Ответ получен");
    }
  } catch (e) {
    if (!isRetry && /сессия не найдена/i.test(String(e.message || ""))) {
      kpSessionId = null;
      kpSessionPromise = null;
      return sendKpChatMessage(text, true);
    }
    kpChatMessages.push({ role: "error", text: e.message, ts: Date.now() });
    showToast(e.message, true);
  } finally {
    kpChatLoading = false;
    updateKpChatFormState();
    renderKpChatMessages();
  }
}

function initKpChat() {
  const form = $("#kpChatForm");
  if (!form) return;

  ensureKpSession()
    .then(() => {
      updateKpChatFormState();
      renderKpChatMessages();
    })
    .catch((e) => showToast(e.message, true));

  updateKpChatFormState();

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#kpChatInput");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendKpChatMessage(text);
  });

  $("#kpChatInput")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  $("#kpChatHints")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-hint]");
    if (!btn) return;
    sendKpChatMessage(btn.dataset.hint);
  });
}

function renderProcessResult(data) {
  updateAssistantMode(data);
  const s = data.summary;
  const parsedOnly = !data.search_completed;
  const summaryEl = $("#resultsSummary");

  if (parsedOnly || !summaryEl) {
    summaryEl?.classList.add("hidden");
  } else {
    summaryEl.classList.remove("hidden");
    summaryEl.innerHTML = `
    <div class="summary-metrics">
      <div class="metric"><div class="metric__label">Позиций</div><div class="metric__value">${s.total_items}</div></div>
      <div class="metric metric--success"><div class="metric__label">Точных</div><div class="metric__value">${s.exact_count}</div></div>
      <div class="metric metric--warning"><div class="metric__label">Похожих</div><div class="metric__value">${s.similar_count}</div></div>
      <div class="metric metric--danger"><div class="metric__label">Не найдено</div><div class="metric__value">${s.not_found_count}</div></div>
      <div class="metric"><div class="metric__label">Себестоимость</div><div class="metric__value">${fmtMoney(s.total_cost)}</div></div>
      <div class="metric"><div class="metric__label">Цена без наценки</div><div class="metric__value">${fmtMoney(s.total_base_price)}</div></div>
      <div class="metric"><div class="metric__label">Цена КП</div><div class="metric__value">${fmtMoney(s.total_price)}</div></div>
    </div>
    <p class="muted" style="margin-top:12px">Время: ${s.processing_seconds} сек · ${taskModeLabel(data.task_mode)} · AI: ${data.ai_used ? "да" : "нет"}${data.web_used ? " · конкуренты" : ""}</p>
  `;
  }

  $("#resultsCard").classList.remove("hidden");
  const tbody = $("#resultsTable tbody");
  tbody.innerHTML = data.items
    .map(
      (item) => {
        const unitPrice = item.unit_base_price ?? item.unit_price;
        const lineTotalKp =
          item.total_price ?? lineSum(item.unit_price, item.quantity);
        const hasDetails = hasItemDetails(item);
        const detailId = `tz-detail-${item.number}`;
        return `
      <tr class="tz-row${hasDetails ? " tz-row--expandable" : ""}" ${hasDetails ? `data-detail="${detailId}"` : ""}>
        <td>${item.number}</td>
        <td>${escapeHtml(item.name)}${hasDetails ? ' <span class="tz-row__hint">▼</span>' : ""}</td>
        <td>${escapeHtml(item.matched_name || "—")}${
          item.source && item.source !== "none"
            ? `<br><small class="muted">${escapeHtml(SOURCE_LABELS[item.source] || item.source)}</small>`
            : ""
        }${
          item.internet_priced
            ? '<br><small class="muted">интернет −5%</small>'
            : `<br><small class="muted">${Math.round(item.match_score)}%</small>`
        }${
          item.internet_url
            ? `<br><a class="muted" href="${escapeHtml(item.internet_url)}" target="_blank" rel="noopener">ссылка</a>`
            : ""
        }</td>
        <td>${statusBadge(item.status, item.notes)}</td>
        <td>${fmtQty(item.quantity, item.unit)}</td>
        <td>${fmtMoney(unitPrice)}${item.internet_priced ? '<br><small class="muted">интернет</small>' : ""}</td>
        <td>${fmtMoney(item.unit_price)}${item.internet_priced ? '<br><small class="muted">−5%</small>' : ""}</td>
        <td>${fmtMoney(lineTotalKp)}</td>
      </tr>
      ${
        hasDetails
          ? `<tr class="tz-detail hidden" id="${detailId}"><td colspan="8">${renderComparisonTable(item)}</td></tr>`
          : ""
      }`;
      },
    )
    .join("");

  tbody.querySelectorAll(".tz-row--expandable").forEach((row) => {
    row.addEventListener("click", () => {
      const detail = document.getElementById(row.dataset.detail);
      if (!detail) return;
      detail.classList.toggle("hidden");
      row.classList.toggle("tz-row--open");
    });
  });

  const btn = $("#downloadBtn");
  if (btn) {
    if (hasKpDownload(data)) {
      const url = `${s.download_url}?t=${Date.now()}`;
      btn.href = url;
      btn.download = s.filename;
      btn.classList.remove("hidden");
      btn.removeAttribute("aria-disabled");
    } else {
      btn.href = "#";
      btn.classList.add("hidden");
      btn.setAttribute("aria-disabled", "true");
    }
  }

  if (data.session_id) {
    if (data.session_id !== kpSessionId) {
      kpSessionId = data.session_id;
      kpChatMessages = [];
      if (data.welcome_reply) {
        kpChatMessages.push({ role: "assistant", text: data.welcome_reply, ts: Date.now() });
      }
    }
    updateKpChatFormState();
    renderKpChatMessages();
  }
}

async function processUpload(taskMode) {
  const fileInput = $("#tzFile");
  if (!fileInput.files.length) return;

  const file = fileInput.files[0];
  if (!isAllowedTzFile(file.name)) {
    showToast(`Поддерживаются форматы: ${ALLOWED_TZ_EXTENSIONS.join(", ")}`, true);
    return;
  }

  const withCompetitors = taskMode === "task1_task2";
  showOverlay(
    withCompetitors
      ? "Читаю ТЗ, ищу в каталогах и анализирую конкурентов..."
      : "Читаю ТЗ и ищу в каталогах и прайсах...",
  );
  const form = new FormData();
  form.append("file", file);
  form.append("use_ai", $("#useAiUpload").checked);
  form.append("task_mode", taskMode);

  try {
    const data = await api("/api/process/upload", { method: "POST", body: form });
    renderProcessResult(data);
    const modeLabel = taskModeLabel(data.task_mode);
    showToast(
      data.search_completed
        ? `ТЗ обработано (${modeLabel})`
        : "ТЗ загружено в чат",
    );
    fileInput.value = "";
    $("#fileName").textContent = "";
    setUploadButtonsEnabled(false);
  } catch (e) {
    showToast(e.message, true);
  } finally {
    hideOverlay();
  }
}

function setUploadButtonsEnabled(enabled) {
  const disabled = !enabled;
  $("#btnProcessTask1")?.toggleAttribute("disabled", disabled);
  $("#btnProcessTask12")?.toggleAttribute("disabled", disabled);
}

function initUpload() {
  const zone = $("#uploadZone");
  const input = $("#tzFile");
  const btnTask1 = $("#btnProcessTask1");
  const btnTask12 = $("#btnProcessTask12");

  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("upload-zone--drag");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("upload-zone--drag"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("upload-zone--drag");
    if (e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      onFileSelected();
    }
  });

  input.addEventListener("change", onFileSelected);

  function onFileSelected() {
    const name = input.files[0]?.name || "";
    $("#fileName").textContent = name;
    setUploadButtonsEnabled(isAllowedTzFile(name));
  }

  btnTask1?.addEventListener("click", () => processUpload("task1"));
  btnTask12?.addEventListener("click", () => processUpload("task1_task2"));
}

const CHAT_STORAGE_KEY = "kp_lookup_chat_v1";

let chatMessages = [];
let chatHistoryIndex = [];
let chatLoading = false;

function loadChatState() {
  try {
    const raw = localStorage.getItem(CHAT_STORAGE_KEY);
    if (!raw) return;
    const data = JSON.parse(raw);
    if (Array.isArray(data.messages)) {
      chatMessages = data.messages;
    }
    if (Array.isArray(data.history)) {
      chatHistoryIndex = data.history;
    }
  } catch {
    chatMessages = [];
    chatHistoryIndex = [];
  }
}

function saveChatState() {
  localStorage.setItem(
    CHAT_STORAGE_KEY,
    JSON.stringify({ messages: chatMessages, history: chatHistoryIndex }),
  );
}

function formatChatTime(ts) {
  const date = new Date(ts);
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function addHistoryEntry(query, messageId) {
  chatHistoryIndex = [
    { id: messageId, query, ts: Date.now() },
    ...chatHistoryIndex.filter((item) => item.query !== query),
  ].slice(0, 50);
}

function renderChatHistorySidebar() {
  const list = $("#chatHistoryList");
  if (!chatHistoryIndex.length) {
    list.innerHTML = `<li class="chat-history__empty muted">Пока нет запросов</li>`;
    return;
  }

  list.innerHTML = chatHistoryIndex
    .map(
      (item) => `
      <li>
        <button type="button" class="chat-history__item" data-scroll-to="${escapeHtml(item.id)}">
          <span class="chat-history__text">${escapeHtml(item.query)}</span>
          <span class="chat-history__meta">${formatChatTime(item.ts)}</span>
        </button>
      </li>`,
    )
    .join("");

  list.querySelectorAll("[data-scroll-to]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const entry = chatHistoryIndex.find((item) => item.id === btn.dataset.scrollTo);
      if (entry) {
        $("#chatInput").value = entry.query;
      }
      const target = document.getElementById(btn.dataset.scrollTo);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.classList.add("chat-msg--highlight");
        setTimeout(() => target.classList.remove("chat-msg--highlight"), 1200);
      }
    });
  });
}

function scrollChatToBottom() {
  const box = $("#chatMessages");
  requestAnimationFrame(() => {
    box.scrollTop = box.scrollHeight;
  });
}

function renderChatMessages() {
  const box = $("#chatMessages");
  if (!chatMessages.length) {
    box.innerHTML = `
      <div class="chat-welcome">
        <p>Напишите название товара — поиск сразу по каталогу, прайсам, реестру и конкурентам. ТЗ не нужно.</p>
      </div>`;
    return;
  }

  box.innerHTML = chatMessages
    .map((msg) => {
      if (msg.role === "user") {
        return `
          <div class="chat-msg chat-msg--user" id="${escapeHtml(msg.id)}">
            <div class="chat-msg__bubble">${escapeHtml(msg.text)}</div>
            <span class="chat-msg__time">${formatChatTime(msg.ts)}</span>
          </div>`;
      }
      if (msg.role === "error") {
        return `
          <div class="chat-msg chat-msg--error" id="${escapeHtml(msg.id)}">
            <div class="chat-msg__bubble">${escapeHtml(msg.text)}</div>
            <span class="chat-msg__time">${formatChatTime(msg.ts)}</span>
          </div>`;
      }
      return `
        <div class="chat-msg chat-msg--assistant" id="${escapeHtml(msg.id)}">
          <div class="chat-msg__bubble chat-result">${renderLookupResultHtml(msg.result)}</div>
          <span class="chat-msg__time">${formatChatTime(msg.ts)}</span>
        </div>`;
    })
    .join("");

  if (chatLoading) {
    box.insertAdjacentHTML(
      "beforeend",
      `<div class="chat-msg chat-msg--assistant" id="chatTyping">
        <div class="chat-msg__bubble">
          <span class="chat-msg__typing"><span></span><span></span><span></span></span>
          Ищу в каталоге и прайсах...
        </div>
      </div>`,
    );
  }

  scrollChatToBottom();
}

function newChatId() {
  return `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

async function sendChatMessage(text) {
  const query = text.trim();
  if (!query || chatLoading) return;

  const userMsg = { id: newChatId(), role: "user", text: query, ts: Date.now() };
  chatMessages.push(userMsg);
  addHistoryEntry(query, userMsg.id);
  chatLoading = true;
  renderChatMessages();
  renderChatHistorySidebar();
  saveChatState();

  try {
    const data = await api("/api/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    chatMessages.push({
      id: newChatId(),
      role: "assistant",
      result: data,
      ts: Date.now(),
    });
  } catch (e) {
    chatMessages.push({
      id: newChatId(),
      role: "error",
      text: e.message,
      ts: Date.now(),
    });
  } finally {
    chatLoading = false;
    renderChatMessages();
    saveChatState();
  }
}

function clearChatHistory() {
  if (!chatMessages.length && !chatHistoryIndex.length) return;
  if (!confirm("Очистить историю чата?")) return;
  chatMessages = [];
  chatHistoryIndex = [];
  chatLoading = false;
  saveChatState();
  renderChatMessages();
  renderChatHistorySidebar();
}

function startNewChat() {
  chatMessages = [];
  chatLoading = false;
  saveChatState();
  renderChatMessages();
  $("#chatInput").focus();
}

function initChat() {
  loadChatState();
  renderChatMessages();
  renderChatHistorySidebar();

  $("#chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#chatInput");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendChatMessage(text);
  });

  $("#chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("#chatForm").requestSubmit();
    }
  });

  $("#btnClearChatHistory").addEventListener("click", clearChatHistory);
  $("#btnNewChat").addEventListener("click", startNewChat);
}

function renderMatchVariants(title, block, buildLines, missingText) {
  const items = block?.items?.length ? block.items : block?.found ? [block] : [];
  if (!items.length) {
    return `
      <div class="source-block source-block--missing">
        <h4>${title}</h4>
        <p>${missingText || "Не найдено"}</p>
      </div>`;
  }

  const variants = items
    .map((item) => {
      const lines = buildLines(item).filter(Boolean);
      const primaryBadge = item.is_primary
        ? '<span class="match-badge match-badge--primary">основной</span>'
        : "";
      return `
        <li class="match-variant${item.is_primary ? " match-variant--primary" : ""}">
          <div class="match-variant__head">
            <strong>${escapeHtml(item.name)}</strong>
            <span class="match-variant__score">${Math.round(item.match_score || 0)}%</span>
            ${primaryBadge}
          </div>
          <ul class="match-variant__details">
            ${lines.map((line) => `<li>${line}</li>`).join("")}
          </ul>
        </li>`;
    })
    .join("");

  return `
    <div class="source-block">
      <h4>${title} <span class="muted match-count">${items.length}</span></h4>
      <ul class="match-variants">${variants}</ul>
    </div>`;
}

function renderPhotoButton(url, alt, imgClass = "lookup-result__photo") {
  return `
    <button
      type="button"
      class="lookup-result__photo-btn"
      data-photo-src="${escapeHtml(url)}"
      data-photo-alt="${escapeHtml(alt)}"
      aria-label="Увеличить фото: ${escapeHtml(alt)}"
    >
      <img class="${imgClass}" src="${escapeHtml(url)}" alt="${escapeHtml(alt)}" loading="lazy" onerror="this.parentElement.remove()">
    </button>`;
}

function renderRegistryPhotos(registry) {
  const urls =
    registry?.photo_urls?.length > 0
      ? registry.photo_urls
      : registry?.photo_url
        ? [registry.photo_url]
        : [];
  const alt = registry?.found ? registry.name : "Нет фото";

  if (!urls.length) {
    return `<img class="lookup-result__photo lookup-result__photo--empty" src="/no-photo.svg" alt="${escapeHtml(alt)}">`;
  }

  const [mainUrl, ...extraUrls] = urls;
  const extras = extraUrls
    .map((url) => renderPhotoButton(url, alt, "lookup-result__photo lookup-result__photo--thumb"))
    .join("");

  return `
    <div class="lookup-result__photos">
      ${renderPhotoButton(mainUrl, alt)}
      ${extras ? `<div class="lookup-result__photo-stack">${extras}</div>` : ""}
    </div>`;
}

function renderLookupResultPhotos(registry, competitors, lookupData) {
  const registryUrls =
    registry?.photo_urls?.length > 0
      ? registry.photo_urls
      : registry?.photo_url
        ? [registry.photo_url]
        : [];
  if (registryUrls.length) {
    return renderRegistryPhotos(registry);
  }

  const topLevelUrls =
    lookupData?.photo_urls?.length > 0
      ? lookupData.photo_urls
      : lookupData?.photo_url
        ? [lookupData.photo_url]
        : [];
  if (topLevelUrls.length) {
    const alt =
      lookupData?.matched_name ||
      lookupData?.query_name ||
      competitors?.items?.[0]?.name ||
      "Фото товара";
    const [mainUrl, ...extraUrls] = topLevelUrls;
    const extras = extraUrls
      .map((url) => renderPhotoButton(url, alt, "lookup-result__photo lookup-result__photo--thumb"))
      .join("");
    return `
      <div class="lookup-result__photos">
        ${renderPhotoButton(mainUrl, alt)}
        ${extras ? `<div class="lookup-result__photo-stack">${extras}</div>` : ""}
      </div>`;
  }

  const competitorItems = competitors?.items?.length
    ? competitors.items
    : competitors?.found
      ? [competitors]
      : [];
  const competitorUrls = competitorItems
    .map((item) => item.image_url)
    .filter(Boolean);
  if (!competitorUrls.length) {
    return renderRegistryPhotos(registry);
  }

  const alt =
    competitorItems.find((item) => item.name)?.name ||
    competitorItems.find((item) => item.matched_name)?.matched_name ||
    "Фото товара";
  const [mainUrl, ...extraUrls] = competitorUrls;
  const extras = extraUrls
    .map((url) => renderPhotoButton(url, alt, "lookup-result__photo lookup-result__photo--thumb"))
    .join("");

  return `
    <div class="lookup-result__photos">
      ${renderPhotoButton(mainUrl, alt)}
      ${extras ? `<div class="lookup-result__photo-stack">${extras}</div>` : ""}
    </div>`;
}

function fitPhotoModalImage(img) {
  const previewSize = 220;
  const target = previewSize * 3;
  const maxW = window.innerWidth * 0.7;
  const maxH = window.innerHeight * 0.7;
  const { naturalWidth: w, naturalHeight: h } = img;
  if (!w || !h) return;

  const scaleUp = Math.max(target / w, target / h, 1);
  const scaleDown = Math.min(maxW / w, maxH / h);
  const scale = Math.min(scaleUp, scaleDown);

  img.style.width = `${Math.round(w * scale)}px`;
  img.style.height = `${Math.round(h * scale)}px`;
}

function openPhotoModal(src, alt) {
  if (!src || src.includes("no-photo")) return;
  const modal = $("#photoModal");
  const img = $("#photoModalImage");
  img.style.width = "";
  img.style.height = "";
  img.alt = alt || "Фото позиции";
  img.onload = () => {
    img.onload = null;
    fitPhotoModalImage(img);
  };
  img.src = src;
  if (img.complete) {
    img.onload = null;
    fitPhotoModalImage(img);
  }
  modal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closePhotoModal() {
  const modal = $("#photoModal");
  modal.classList.add("hidden");
  const img = $("#photoModalImage");
  img.src = "";
  img.style.width = "";
  img.style.height = "";
  document.body.style.overflow = "";
}

function initPhotoModal() {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".lookup-result__photo-btn");
    if (btn?.dataset.photoSrc) {
      openPhotoModal(btn.dataset.photoSrc, btn.dataset.photoAlt);
      return;
    }
    if (e.target.closest("[data-close-photo]")) {
      closePhotoModal();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#photoModal").classList.contains("hidden")) {
      closePhotoModal();
    }
  });
}

function renderAiInsightBlock(ai) {
  if (!ai?.requested) return "";

  const title = "Найденная информация в базе от нейросети";

  if (!ai.available) {
    return `
      <div class="source-block source-block--ai source-block--missing">
        <h4>${title}</h4>
        <p>${ai.message || "Нейросеть недоступна"}</p>
      </div>`;
  }

  if (!ai.found) {
    return `
      <div class="source-block source-block--ai source-block--missing">
        <h4>${title}</h4>
        <p>${ai.message || "Информация не найдена"}</p>
      </div>`;
  }

  const priceSource = ai.price_source || ai.source_label;
  const renderSearchLinks = (links) =>
    (links || [])
      .map(
        (item) =>
          `<a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.label)}</a>`,
      )
      .join(" · ");

  const sourceLine = (() => {
    const hasSearch = ai.search_links?.length > 0;
    if (!priceSource && !ai.product_url && !hasSearch) return null;

    let html = "Источник цены: ";
    html += priceSource ? escapeHtml(priceSource) : "оценка AI";

    if (ai.product_url) {
      html += ` — ${renderChatLink(ai.product_url, "открыть товар")}`;
    } else if (hasSearch) {
      html += ` · Поиск: ${renderSearchLinks(ai.search_links)}`;
    }
    return html;
  })();

  const lines = [
    ai.matched_name ? `${escapeHtml(ai.matched_name)} (${Math.round(ai.match_score || 0)}%)` : null,
    sourceLine,
    ai.link_note ? escapeHtml(ai.link_note) : null,
    ai.unit_cost ? `Себестоимость: ${ai.unit_cost}` : null,
    ai.unit_price_kp ? `Цена КП (+наценка): ${ai.unit_price_kp}` : null,
    ai.notes && ai.notes !== priceSource ? escapeHtml(ai.notes) : null,
  ].filter(Boolean);

  return `
    <div class="source-block source-block--ai">
      <h4>${title}</h4>
      <ul>${lines.map((line) => `<li>${line}</li>`).join("")}</ul>
    </div>`;
}

function renderCompetitorsBlock(competitors) {
  return renderMatchVariants(
    "Сайты конкурентов",
    competitors || {},
    (item) => [
      item.label ? `Источник: ${escapeHtml(item.label)}` : null,
      item.price || item.price_label
        ? `Цена: ${escapeHtml(item.price || item.price_label)}`
        : item.has_price === false
          ? "Цена не указана на сайте"
          : null,
      item.price_kp ? `Цена КП (−5%): ${item.price_kp}` : null,
      item.url ? renderChatLink(item.url, "Открыть на сайте") : null,
      item.notes ? escapeHtml(item.notes) : null,
    ],
    "На сайтах конкурентов не найдено",
  );
}

function renderLookupResultHtml(data) {
  const photoHtml = renderLookupResultPhotos(data.registry, data.competitors, data);

  const registryBlock = data.registry?.found
    ? renderMatchVariants(
        "Реестр остатков",
        data.registry,
        (item) => [
          `Остаток: ${item.quantity} шт.`,
          item.condition ? `Состояние: ${escapeHtml(item.condition)}` : null,
          item.link ? `Ссылка: ${escapeHtml(item.link)}` : null,
        ],
      )
    : `<div class="source-block source-block--warning">
         <h4>Реестр остатков</h4>
         <p>${data.registry?.message || "В Реестре остатков нет такого наименования"}</p>
       </div>`;

  const catalogBlock = renderMatchVariants(
    "Каталог",
    data.catalog || {},
    (item) => [
      item.cost ? `Себестоимость: ${item.cost}` : null,
      item.price ? `Цена: ${item.price}` : null,
      item.stock ? `Остаток: ${item.stock}` : null,
      item.unit ? `Ед. изм.: ${escapeHtml(item.unit)}` : null,
    ],
    "В каталоге не найдено",
  );

  const priceBlock = renderMatchVariants(
    "Прайс",
    data.price_list || {},
    (item) => [
      item.price ? `Цена: ${item.price}` : null,
      item.code ? `Код: ${escapeHtml(item.code)}` : null,
      item.supplier ? `Поставщик: ${escapeHtml(item.supplier)}` : null,
      `Рекомендованное кол-во на кабинет: ${item.recommended_qty ?? "—"}`,
      `Заказ: ${item.order_qty ?? "—"}`,
      `Сумма: ${item.order_sum ?? "—"}`,
    ],
    "В прайсах не найдено",
  );

  const aiBlock = renderAiInsightBlock(data.ai_insight);
  const competitorsBlock = renderCompetitorsBlock(data.competitors);

  if (data.not_found) {
    return `
      <div class="lookup-result__layout">
        <div>
          <h3>Не найдено: «${escapeHtml(data.query_name)}»</h3>
          ${catalogBlock}
          ${priceBlock}
          ${registryBlock}
          ${competitorsBlock}
          ${aiBlock}
          ${
            data.alternatives.length
              ? `<p class="muted" style="margin-top:12px">Похожие: ${data.alternatives.map(escapeHtml).join(" · ")}</p>`
              : ""
          }
        </div>
        <div>${photoHtml}</div>
      </div>`;
  }

  const kv = Object.entries(data.values)
    .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`)
    .join("");

  return `
    <div class="lookup-result__layout">
      <div>
        <h3>${escapeHtml(data.matched_name)}</h3>
        <p class="muted">Запрос: «${escapeHtml(data.query_name)}» · ${Math.round(data.match_score)}% · ${escapeHtml(data.status)}</p>
        <dl class="lookup-kv">${kv}</dl>
        ${catalogBlock}
        ${priceBlock}
        ${registryBlock}
        ${competitorsBlock}
        ${aiBlock}
        ${
          data.alternatives.length
            ? `<p class="muted" style="margin-top:12px">Альтернативы: ${data.alternatives.map(escapeHtml).join(" · ")}</p>`
            : ""
        }
      </div>
      <div>${photoHtml}</div>
    </div>`;
}

function renderDataSourceRow(row) {
  const isPrice = row.type === "price_list";
  const nameCell = isPrice
    ? `${escapeHtml(row.name)}<br><small class="muted">${escapeHtml(row.supplier || "")}</small>`
    : `${escapeHtml(row.name)}<br><small class="muted">${escapeHtml(row.filename || "")}</small>`;
  const count = row.exists === false ? "—" : row.items_count ?? "—";
  const actions = `<div class="price-actions">
      <button class="btn btn--secondary btn--small" data-replace="${escapeHtml(row.id)}">Заменить</button>
      <button class="btn btn--secondary btn--small" data-rename="${escapeHtml(row.id)}">Переименовать</button>
      <button class="btn btn--danger btn--small" data-remove="${escapeHtml(row.id)}">Удалить</button>
    </div>`;

  return `
    <tr>
      <td>${nameCell}</td>
      <td>${count}</td>
      <td>${actions}</td>
    </tr>`;
}

function isStaticSource(id) {
  return id === "catalog" || id === "registry";
}

function fileAcceptForSource(id) {
  return isStaticSource(id) ? ".xlsx" : ".xls,.xlsx";
}

function sourceLabel(id) {
  if (id === "catalog") return "каталог";
  if (id === "registry") return "остатки на складе";
  return id;
}

function collectDataSources(data) {
  return {
    catalogs: data.catalogs?.length ? data.catalogs : data.catalog ? [data.catalog] : [],
    prices: data.prices?.length ? data.prices : data.items || [],
    stock: data.stock?.length ? data.stock : data.registry ? [data.registry] : [],
  };
}

function renderDataSourceSection(title, rows) {
  if (!rows.length) {
    return `
      <div class="data-sources-section">
        <h3 class="data-sources-section__title">${escapeHtml(title)}</h3>
        <p class="muted">Нет загруженных файлов</p>
      </div>`;
  }

  return `
    <div class="data-sources-section">
      <h3 class="data-sources-section__title">${escapeHtml(title)}</h3>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Название</th>
              <th>Позиций</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>${rows.map(renderDataSourceRow).join("")}</tbody>
        </table>
      </div>
    </div>`;
}

function ragUploadMessage(rag) {
  if (!rag?.indexed) return "";
  const chunks = rag.chunks ?? 0;
  const mode = rag.vectorized ? "векторный" : "текстовый";
  return ` · RAG: ${chunks} чанков (${mode})`;
}

async function loadPrices() {
  try {
    const data = await api("/api/prices");
    const groups = collectDataSources(data);
    const container = $("#dataSourcesContainer");
    container.innerHTML = [
      renderDataSourceSection("Каталоги", groups.catalogs),
      renderDataSourceSection("Прайсы", groups.prices),
      renderDataSourceSection("Остатки на складе", groups.stock),
    ].join("");

    container.querySelectorAll("[data-replace]").forEach((btn) => {
      btn.addEventListener("click", () => replaceDataSource(btn.dataset.replace));
    });
    container.querySelectorAll("[data-rename]").forEach((btn) => {
      btn.addEventListener("click", () => renameDataSource(btn.dataset.rename));
    });
    container.querySelectorAll("[data-remove]").forEach((btn) => {
      btn.addEventListener("click", () => removeDataSource(btn.dataset.remove));
    });
  } catch (e) {
    showToast(e.message, true);
  }
}

async function addPrice() {
  const name = $("#priceName").value.trim();
  const supplier = $("#priceSupplier").value.trim();
  const file = $("#priceFile").files[0];
  if (!name || !file) {
    showToast("Укажите название и файл прайса", true);
    return;
  }

  showOverlay("Загружаю прайс и индексирую для RAG...");
  const form = new FormData();
  form.append("name", name);
  form.append("supplier", supplier || name);
  form.append("file", file);

  try {
    const data = await api("/api/prices", { method: "POST", body: form });
    showToast(`Прайс добавлен${ragUploadMessage(data.rag)}`);
    $("#priceName").value = "";
    $("#priceSupplier").value = "";
    $("#priceFile").value = "";
    loadPrices();
    loadStatus();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    hideOverlay();
  }
}

async function uploadCatalog() {
  const file = $("#catalogFile").files[0];
  if (!file) {
    showToast("Выберите файл каталога (.xlsx)", true);
    return;
  }
  if (!file.name.toLowerCase().endsWith(".xlsx")) {
    showToast("Каталог должен быть в формате .xlsx", true);
    return;
  }

  showOverlay("Загружаю каталог и индексирую для RAG...");
  const form = new FormData();
  form.append("file", file);

  try {
    const data = await api("/api/sources/catalog/upload", { method: "POST", body: form });
    const entry = data.entry || {};
    showToast(
      `Каталог обновлён: ${entry.items_count ?? "—"} поз.${ragUploadMessage(data.rag)}`,
    );
    $("#catalogFile").value = "";
    loadPrices();
    loadStatus();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    hideOverlay();
  }
}

async function uploadStock() {
  const file = $("#stockFile").files[0];
  if (!file) {
    showToast("Выберите файл остатков (.xlsx)", true);
    return;
  }
  if (!file.name.toLowerCase().endsWith(".xlsx")) {
    showToast("Остатки должны быть в формате .xlsx", true);
    return;
  }

  showOverlay("Загружаю остатки и индексирую для RAG...");
  const form = new FormData();
  form.append("file", file);

  try {
    const data = await api("/api/sources/registry/upload", { method: "POST", body: form });
    const entry = data.entry || {};
    showToast(
      `Остатки обновлены: ${entry.items_count ?? "—"} поз.${ragUploadMessage(data.rag)}`,
    );
    $("#stockFile").value = "";
    loadPrices();
    loadStatus();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    hideOverlay();
  }
}

function replaceDataSource(id) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = fileAcceptForSource(id);
  input.onchange = async () => {
    if (!input.files.length) return;
    const labels = {
      catalog: "каталог",
      registry: "реестр",
      default: "прайс",
    };
    showOverlay(`Обновляю ${labels[id] || labels.default} и индексирую для RAG...`);
    const form = new FormData();
    form.append("file", input.files[0]);
    try {
      const data = await api(`/api/prices/${id}/file`, { method: "PUT", body: form });
      showToast(`Таблица обновлена${ragUploadMessage(data.rag)}`);
      loadPrices();
      loadStatus();
    } catch (e) {
      showToast(e.message, true);
    } finally {
      hideOverlay();
    }
  };
  input.click();
}

async function renameDataSource(id) {
  const name = prompt("Новое название:");
  if (!name) return;

  const body = { name };
  if (!isStaticSource(id)) {
    body.supplier = prompt("Поставщик:") || name;
  }

  try {
    await api(`/api/prices/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    showToast("Название обновлено");
    loadPrices();
    loadStatus();
  } catch (e) {
    showToast(e.message, true);
  }
}

async function removeDataSource(id) {
  if (!confirm(`Удалить ${sourceLabel(id)}?`)) return;
  try {
    await api(`/api/prices/${id}`, { method: "DELETE" });
    showToast("Таблица удалена");
    loadPrices();
    loadStatus();
  } catch (e) {
    showToast(e.message, true);
  }
}

function renderCompetitorAnalysis(analysis) {
  const box = $("#competitorAnalysis");
  if (!analysis) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = `
    <p><strong>Домен:</strong> ${escapeHtml(analysis.domain || "—")}</p>
    <p><strong>Заголовок:</strong> ${escapeHtml(analysis.title || "—")}</p>
    <p><strong>Поиск:</strong> ${escapeHtml(analysis.search_url || "—")}</p>
    <p><strong>Статус:</strong> ${escapeHtml(analysis.status || "—")}</p>
    <p class="muted">${escapeHtml(analysis.notes || "")}</p>
  `;
  if (analysis.search_url && !$("#competitorSearchUrl").value.trim()) {
    $("#competitorSearchUrl").value = analysis.search_url;
  }
  if (analysis.label && !$("#competitorLabel").value.trim()) {
    $("#competitorLabel").value = analysis.label;
  }
}

function renderCompetitorRow(site) {
  const badge = site.builtin
    ? '<span class="competitor-badge">встроенный</span>'
    : '<span class="competitor-badge competitor-badge--custom">добавленный</span>';
  const actions = site.builtin
    ? "—"
    : `<button class="btn btn--danger btn--small" data-remove-competitor="${escapeHtml(site.id)}">Удалить</button>`;
  return `
    <tr>
      <td>${escapeHtml(site.label || site.domain)} ${badge}</td>
      <td><a href="${escapeHtml(site.url)}" target="_blank" rel="noopener">${escapeHtml(site.domain)}</a></td>
      <td>${site.search_url ? `<small>${escapeHtml(site.search_url)}</small>` : "—"}</td>
      <td>${actions}</td>
    </tr>`;
}

function renderCompetitorSection(title, rows) {
  if (!rows.length) {
    return `
      <div class="data-sources-section">
        <h3 class="data-sources-section__title">${escapeHtml(title)}</h3>
        <p class="muted">Нет сайтов</p>
      </div>`;
  }
  return `
    <div class="data-sources-section">
      <h3 class="data-sources-section__title">${escapeHtml(title)}</h3>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Название</th>
              <th>Домен</th>
              <th>Поиск</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>${rows.map(renderCompetitorRow).join("")}</tbody>
        </table>
      </div>
    </div>`;
}

async function loadCompetitors() {
  try {
    const data = await api("/api/competitors");
    if (data.catalog_products) {
      applyCompetitorCatalogStats(data.catalog_products);
    }
    const container = $("#competitorsList");
    container.innerHTML = [
      renderCompetitorSection("Добавленные сайты", data.custom || []),
      renderCompetitorSection("Встроенные сайты", data.builtin || []),
    ].join("");

    container.querySelectorAll("[data-remove-competitor]").forEach((btn) => {
      btn.addEventListener("click", () => removeCompetitorSite(btn.dataset.removeCompetitor));
    });
  } catch (e) {
    showToast(e.message, true);
  }
}

async function analyzeCompetitorSite() {
  const url = $("#competitorUrl").value.trim();
  if (!url) {
    showToast("Введите ссылку на сайт", true);
    return;
  }
  showOverlay("Анализирую сайт...");
  try {
    const data = await api("/api/competitors/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        label: $("#competitorLabel").value.trim(),
      }),
    });
    renderCompetitorAnalysis(data.analysis);
    showToast("Анализ завершён");
  } catch (e) {
    showToast(e.message, true);
  } finally {
    hideOverlay();
  }
}

async function addCompetitorSite() {
  const url = $("#competitorUrl").value.trim();
  if (!url) {
    showToast("Введите ссылку на сайт", true);
    return;
  }
  showOverlay("Добавляю сайт и индексирую для RAG...");
  try {
    const data = await api("/api/competitors", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        label: $("#competitorLabel").value.trim(),
        search_url: $("#competitorSearchUrl").value.trim() || null,
      }),
    });
    renderCompetitorAnalysis(data.analysis);
    showToast(`Сайт добавлен${ragUploadMessage(data.rag)}`);
    if (data.catalog_products) {
      applyCompetitorCatalogStats(data.catalog_products);
    }
    $("#competitorUrl").value = "";
    $("#competitorLabel").value = "";
    $("#competitorSearchUrl").value = "";
    loadCompetitors();
    loadStatus();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    hideOverlay();
  }
}

async function removeCompetitorSite(siteId) {
  if (!confirm("Удалить сайт конкурента из базы?")) return;
  try {
    const data = await api(`/api/competitors/${siteId}`, { method: "DELETE" });
    showToast("Сайт удалён");
    if (data.catalog_products) {
      applyCompetitorCatalogStats(data.catalog_products);
    } else {
      loadStatus();
    }
    loadCompetitors();
  } catch (e) {
    showToast(e.message, true);
  }
}

let competitorChatMessages = [];
let competitorChatLoading = false;

function formatCompetitorSiteLabel(label) {
  if (!label) return "—";
  return label.replace(/^Конкурент:\s*/i, "").trim() || label;
}

function renderCompetitorSearchResults(data) {
  if (!data.items?.length) {
    return `<p>По запросу «${escapeHtml(data.query)}» на сайтах конкурентов ничего не найдено.</p>`;
  }

  const rows = data.items
    .map((item) => {
      const photoHtml = item.image_url
        ? `<div class="competitor-result-item__photo">${renderPhotoButton(
            item.image_url,
            item.matched_name || "Фото товара",
            "competitor-result-item__image",
          )}</div>`
        : "";
      return `
        <div class="competitor-result-item">
          ${photoHtml}
          <div class="competitor-result-item__body">
          <div class="competitor-result-item__head">
            <strong class="competitor-result-item__title">${escapeHtml(formatCompetitorSiteLabel(item.label))}</strong>
            <span class="competitor-result-item__price">${escapeHtml(fmtCompetitorPrice(item))}</span>
          </div>
          <div class="competitor-result-item__name">${escapeHtml(item.matched_name || "—")}</div>
          ${item.articul ? `<div class="muted">Артикул: ${escapeHtml(item.articul)}</div>` : ""}
          <div class="muted">${item.match_score ? `${Math.round(item.match_score)}% совпадение` : ""}</div>
          ${
            item.url
              ? `<div class="chat-link-wrap">${renderChatLink(item.url, "Открыть на сайте")}</div>`
              : `<div class="muted">${escapeHtml(item.notes || "")}</div>`
          }
          </div>
        </div>`;
    })
    .join("");

  return `
    <p>Запрос: «${escapeHtml(data.query)}» · найдено: ${data.count} · сайтов: ${data.sites_searched} · ${data.processing_seconds} сек</p>
    <div class="competitor-result-list">${rows}</div>`;
}

function renderCompetitorChatMessages() {
  const box = $("#competitorChatMessages");
  if (!box) return;

  if (!competitorChatMessages.length) {
    box.innerHTML = `
      <div class="chat-welcome">
        <p>Напишите название товара — найду предложения на сайтах конкурентов из списка выше.</p>
      </div>`;
    return;
  }

  box.innerHTML = competitorChatMessages
    .map((msg) => {
      if (msg.role === "user") {
        return `
          <div class="chat-msg chat-msg--user">
            <div class="chat-msg__bubble">${escapeHtml(msg.text)}</div>
            <span class="chat-msg__time">${formatChatTime(msg.ts)}</span>
          </div>`;
      }
      if (msg.role === "error") {
        return `
          <div class="chat-msg chat-msg--error">
            <div class="chat-msg__bubble">${escapeHtml(msg.text)}</div>
            <span class="chat-msg__time">${formatChatTime(msg.ts)}</span>
          </div>`;
      }
      return `
        <div class="chat-msg chat-msg--assistant">
          <div class="chat-msg__bubble chat-result">${msg.html}</div>
          <span class="chat-msg__time">${formatChatTime(msg.ts)}</span>
        </div>`;
    })
    .join("");

  if (competitorChatLoading) {
    box.insertAdjacentHTML(
      "beforeend",
      `<div class="chat-msg chat-msg--assistant">
        <div class="chat-msg__bubble">
          <span class="chat-msg__typing"><span></span><span></span><span></span></span>
          Ищу на сайтах конкурентов...
        </div>
      </div>`,
    );
  }

  requestAnimationFrame(() => {
    box.scrollTop = box.scrollHeight;
  });
}

function updateCompetitorChatFormState() {
  const input = $("#competitorChatInput");
  const sendBtn = $("#btnCompetitorChatSend");
  const enabled = !competitorChatLoading;
  if (input) input.disabled = !enabled;
  if (sendBtn) sendBtn.disabled = !enabled;
  document.querySelectorAll("[data-competitor-hint]").forEach((btn) => {
    btn.disabled = !enabled;
  });
}

async function sendCompetitorChatMessage(text) {
  const query = text.trim();
  if (!query || competitorChatLoading) return;

  competitorChatMessages.push({ role: "user", text: query, ts: Date.now() });
  competitorChatLoading = true;
  updateCompetitorChatFormState();
  renderCompetitorChatMessages();

  try {
    const data = await api("/api/competitors/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, limit: 12 }),
    });
    competitorChatMessages.push({
      role: "assistant",
      html: renderCompetitorSearchResults(data),
      ts: Date.now(),
    });
    showToast(data.count ? `Найдено: ${data.count}` : "Ничего не найдено");
  } catch (e) {
    competitorChatMessages.push({ role: "error", text: e.message, ts: Date.now() });
    showToast(e.message, true);
  } finally {
    competitorChatLoading = false;
    updateCompetitorChatFormState();
    renderCompetitorChatMessages();
  }
}

function initCompetitorChat() {
  const form = $("#competitorChatForm");
  if (!form) return;

  updateCompetitorChatFormState();
  renderCompetitorChatMessages();

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#competitorChatInput");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendCompetitorChatMessage(text);
  });

  $("#competitorChatInput")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  document.querySelectorAll("[data-competitor-hint]").forEach((btn) => {
    btn.addEventListener("click", () => {
      sendCompetitorChatMessage(btn.dataset.competitorHint);
    });
  });
}

function initAiToggle() {
  $("#useAiUpload").addEventListener("change", refreshAiStatusUi);
}

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initUpload();
  initMarkup();
  initAiToggle();
  initChat();
  initKpChat();
  updateKpChatFormState();
  initPhotoModal();
  $("#btnAddPrice").addEventListener("click", addPrice);
  $("#btnUploadCatalog").addEventListener("click", uploadCatalog);
  $("#btnUploadStock").addEventListener("click", uploadStock);
  $("#btnAnalyzeCompetitor").addEventListener("click", analyzeCompetitorSite);
  $("#btnAddCompetitor").addEventListener("click", addCompetitorSite);
  initCompetitorChat();
  loadInitialStatus();
  setInterval(() => {
    if (document.visibilityState === "visible") loadInitialStatus();
  }, 30_000);
});

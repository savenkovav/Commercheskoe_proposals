const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const ALLOWED_TZ_EXTENSIONS = [".docx", ".pdf", ".xlsx", ".xls"];

function isAllowedTzFile(name) {
  const lower = name.toLowerCase();
  return ALLOWED_TZ_EXTENSIONS.some((ext) => lower.endsWith(ext));
}
const fmtMoney = (v) =>
  v == null ? "—" : `${Number(v).toLocaleString("ru-RU", { minimumFractionDigits: 2 })} ₽`;

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

const statusBadge = (status) => {
  const labels = { exact: "Точно", similar: "Похоже", not_found: "Не найдено" };
  return `<span class="badge badge--${status}">${labels[status] || status}</span>`;
};

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

function renderHeaderStats(status) {
  const aiOn = getEffectiveAiEnabled(status);
  $("#headerStats").innerHTML = `
    <span class="stat-pill">Каталог: <strong>${status.catalog_count}</strong></span>
    <span class="stat-pill">Прайсы: <strong>${status.price_items_count}</strong></span>
    <span class="stat-pill">AI: <strong>${aiStatusText(aiOn)}</strong></span>
  `;
}

async function loadInitialStatus() {
  try {
    const status = await api("/api/status");
    cachedStatus = status;
    renderHeaderStats(status);
    setMarkupInput(status.markup_percent ?? 30);
  } catch (e) {
    showToast(e.message, true);
  }
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
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

function renderProcessResult(data) {
  const s = data.summary;
  $("#summaryEmpty").classList.add("hidden");
  $("#summaryBlock").classList.remove("hidden");
  $("#summaryBlock").innerHTML = `
    <div class="summary-metrics">
      <div class="metric"><div class="metric__label">Позиций</div><div class="metric__value">${s.total_items}</div></div>
      <div class="metric metric--success"><div class="metric__label">Точных</div><div class="metric__value">${s.exact_count}</div></div>
      <div class="metric metric--warning"><div class="metric__label">Похожих</div><div class="metric__value">${s.similar_count}</div></div>
      <div class="metric metric--danger"><div class="metric__label">Не найдено</div><div class="metric__value">${s.not_found_count}</div></div>
      <div class="metric"><div class="metric__label">Себестоимость</div><div class="metric__value">${fmtMoney(s.total_cost)}</div></div>
      <div class="metric"><div class="metric__label">Цена без наценки</div><div class="metric__value">${fmtMoney(s.total_base_price)}</div></div>
      <div class="metric"><div class="metric__label">Цена КП</div><div class="metric__value">${fmtMoney(s.total_price)}</div></div>
    </div>
    <p class="muted" style="margin-top:12px">Время: ${s.processing_seconds} сек · AI: ${data.ai_used ? "да" : "нет"}</p>
  `;

  $("#resultsCard").classList.remove("hidden");
  const tbody = $("#resultsTable tbody");
  tbody.innerHTML = data.items
    .map(
      (item) => {
        const unitPrice = item.unit_base_price ?? item.unit_price;
        const lineTotalKp =
          item.total_price ?? lineSum(item.unit_price, item.quantity);
        return `
      <tr>
        <td>${item.number}</td>
        <td>${item.name}</td>
        <td>${item.matched_name || "—"}<br><small class="muted">${Math.round(item.match_score)}%</small></td>
        <td>${statusBadge(item.status)}</td>
        <td>${fmtQty(item.quantity, item.unit)}</td>
        <td>${fmtMoney(unitPrice)}</td>
        <td>${fmtMoney(item.unit_price)}</td>
        <td>${fmtMoney(lineTotalKp)}</td>
      </tr>`;
      },
    )
    .join("");

  const btn = $("#downloadBtn");
  btn.href = s.download_url;
  btn.download = s.filename;
}

async function processUpload() {
  const fileInput = $("#tzFile");
  if (!fileInput.files.length) return;

  const file = fileInput.files[0];
  if (!isAllowedTzFile(file.name)) {
    showToast(`Поддерживаются форматы: ${ALLOWED_TZ_EXTENSIONS.join(", ")}`, true);
    return;
  }

  showOverlay("Ищу позиции в каталоге и прайсах...");
  const form = new FormData();
  form.append("file", file);
  form.append("use_ai", $("#useAiUpload").checked);

  try {
    const data = await api("/api/process/upload", { method: "POST", body: form });
    renderProcessResult(data);
    showToast("КП сформировано");
    fileInput.value = "";
    $("#fileName").textContent = "";
    $("#btnProcess").disabled = true;
  } catch (e) {
    showToast(e.message, true);
  } finally {
    hideOverlay();
  }
}

function initUpload() {
  const zone = $("#uploadZone");
  const input = $("#tzFile");
  const btn = $("#btnProcess");

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
    btn.disabled = !isAllowedTzFile(name);
  }

  btn.addEventListener("click", processUpload);
}

async function doLookup() {
  const query = $("#lookupQuery").value.trim();
  if (!query) return;

  showOverlay("Поиск...");
  try {
    const data = await api("/api/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });

    renderLookupResult(data);
  } catch (e) {
    showToast(e.message, true);
  } finally {
    hideOverlay();
  }
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

function renderRegistryPhotos(registry) {
  const urls =
    registry?.photo_urls?.length > 0
      ? registry.photo_urls
      : registry?.photo_url
        ? [registry.photo_url]
        : [];
  const alt = registry?.found ? registry.name : "Нет фото";

  if (!urls.length) {
    return `<img class="lookup-result__photo" src="/no-photo.svg" alt="${alt}">`;
  }

  const [mainUrl, ...extraUrls] = urls;
  const extras = extraUrls
    .map(
      (url) =>
        `<img class="lookup-result__photo lookup-result__photo--thumb" src="${url}" alt="${alt}" onerror="this.src='/no-photo.svg'">`,
    )
    .join("");

  return `
    <div class="lookup-result__photos">
      <img class="lookup-result__photo" src="${mainUrl}" alt="${alt}" onerror="this.src='/no-photo.svg'">
      ${extras ? `<div class="lookup-result__photo-stack">${extras}</div>` : ""}
    </div>`;
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
      html += ` — <a href="${escapeHtml(ai.product_url)}" target="_blank" rel="noopener noreferrer">открыть товар</a>`;
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

function renderLookupResult(data) {
  const block = $("#lookupResult");
  block.classList.remove("hidden");

  const photoHtml = renderRegistryPhotos(data.registry);

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

  if (data.not_found) {
    block.innerHTML = `
      <div class="lookup-result__layout">
        <div>
          <h3>Не найдено: «${data.query_name}»</h3>
          ${catalogBlock}
          ${priceBlock}
          ${registryBlock}
          ${aiBlock}
          ${
            data.alternatives.length
              ? `<p class="muted" style="margin-top:12px">Похожие: ${data.alternatives.join(" · ")}</p>`
              : ""
          }
        </div>
        <div>${photoHtml}</div>
      </div>`;
    return;
  }

  const kv = Object.entries(data.values)
    .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`)
    .join("");

  block.innerHTML = `
    <div class="lookup-result__layout">
      <div>
        <h3>${data.matched_name}</h3>
        <p class="muted">Запрос: «${data.query_name}» · ${Math.round(data.match_score)}% · ${data.status}</p>
        <dl class="lookup-kv">${kv}</dl>
        ${catalogBlock}
        ${priceBlock}
        ${registryBlock}
        ${
          data.alternatives.length
            ? `<p class="muted" style="margin-top:12px">Альтернативы: ${data.alternatives.join(" · ")}</p>`
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
      <td>${escapeHtml(row.type_label || row.type || "—")}</td>
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
  if (id === "registry") return "реестр остатков";
  return id;
}

function collectDataSources(data) {
  const rows = [];
  if (data.catalog) rows.push(data.catalog);
  if (data.registry) rows.push(data.registry);
  if (data.items?.length) rows.push(...data.items);
  return rows;
}

async function loadPrices() {
  try {
    const data = await api("/api/prices");
    const tbody = $("#pricesTable tbody");
    const rows = collectDataSources(data);
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="muted">Нет загруженных таблиц</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(renderDataSourceRow).join("");

    tbody.querySelectorAll("[data-replace]").forEach((btn) => {
      btn.addEventListener("click", () => replaceDataSource(btn.dataset.replace));
    });
    tbody.querySelectorAll("[data-rename]").forEach((btn) => {
      btn.addEventListener("click", () => renameDataSource(btn.dataset.rename));
    });
    tbody.querySelectorAll("[data-remove]").forEach((btn) => {
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

  showOverlay("Загружаю прайс...");
  const form = new FormData();
  form.append("name", name);
  form.append("supplier", supplier || name);
  form.append("file", file);

  try {
    await api("/api/prices", { method: "POST", body: form });
    showToast("Прайс добавлен");
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
    showOverlay(`Обновляю ${labels[id] || labels.default}...`);
    const form = new FormData();
    form.append("file", input.files[0]);
    try {
      await api(`/api/prices/${id}/file`, { method: "PUT", body: form });
      showToast("Таблица обновлена");
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

function initAiToggle() {
  $("#useAiUpload").addEventListener("change", refreshAiStatusUi);
}

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initUpload();
  initMarkup();
  initAiToggle();
  $("#btnLookup").addEventListener("click", doLookup);
  $("#lookupQuery").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doLookup();
  });
  $("#btnAddPrice").addEventListener("click", addPrice);
  loadInitialStatus();
});

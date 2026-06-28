const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const ALLOWED_TZ_EXTENSIONS = [".doc", ".docx", ".pdf", ".xlsx", ".xls"];

function isAllowedTzFile(name) {
  const lower = name.toLowerCase();
  return ALLOWED_TZ_EXTENSIONS.some((ext) => lower.endsWith(ext));
}
const fmtMoney = (v) =>
  v == null ? "—" : `${Number(v).toLocaleString("ru-RU", { minimumFractionDigits: 2 })} ₽`;

const fmtPercent = (v) =>
  v == null || !Number.isFinite(v) ? "—" : `${Number(v).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;

function calcMarginPercent(totalCost, totalPrice) {
  const cost = Number(totalCost);
  const price = Number(totalPrice);
  if (!Number.isFinite(cost) || !Number.isFinite(price) || cost <= 0) {
    return null;
  }
  return ((price - cost) / cost) * 100;
}

let currentUser = null;
let openUserMenuId = null;
let userEditTargetId = null;
let userModalMode = "edit";

const ROLE_LABELS = { admin: "Администратор", manager: "Менеджер" };

function formatHistoryDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function switchToTab(tabName) {
  $$(".tabs__btn").forEach((b) => {
    b.classList.toggle("tabs__btn--active", b.dataset.tab === tabName);
  });
  $$(".panel").forEach((p) => {
    p.classList.toggle("panel--active", p.dataset.panel === tabName);
  });
  if (tabName === "prices") loadPrices();
  if (tabName === "competitors") loadCompetitors();
  if (tabName === "status") loadStatus();
  if (tabName === "history") loadHistory();
  if (tabName === "users") loadUsers();
}

function updateAuthUi() {
  const isAdmin = currentUser?.role === "admin";
  const userEl = $("#headerUser");
  const logoutBtn = $("#btnLogout");
  const usersTab = $("#tabUsersBtn");
  const historyTab = $("#tabHistoryBtn");

  if (currentUser && userEl) {
    userEl.classList.remove("hidden");
    userEl.innerHTML = `
      <span class="site-header__user-login header__user-login">${escapeHtml(currentUser.login)}</span>
      <span class="site-header__user-role header__user-role">${escapeHtml(ROLE_LABELS[currentUser.role] || currentUser.role)}</span>`;
  }
  logoutBtn?.classList.toggle("hidden", !currentUser);
  usersTab?.classList.toggle("hidden", !isAdmin);
  historyTab?.classList.toggle("hidden", !currentUser);
}

async function ensureAuth() {
  try {
    currentUser = await api("/api/auth/me");
    updateAuthUi();
    return true;
  } catch {
    window.location.href = "/login.html";
    return false;
  }
}

async function logoutUser() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    /* redirect anyway */
  }
  window.location.href = "/login.html";
}

function closeUserMenus() {
  openUserMenuId = null;
  document.querySelectorAll(".user-menu").forEach((menu) => menu.classList.add("hidden"));
}

function openUserCredentialsModal({ title, hint, html, showForm = false, userId = null, mode = "edit" }) {
  const modal = $("#userCredentialsModal");
  const form = $("#userEditForm");
  const saveBtn = $("#btnSaveUserCredentials");
  const loginInput = $("#editUserLogin");
  const passwordInput = $("#editUserPassword");
  $("#userCredentialsTitle").textContent = title;
  $("#userCredentialsHint").textContent = hint || "";
  $("#userCredentialsBox").innerHTML = html || "";
  $("#userCredentialsBox").classList.toggle("hidden", showForm);
  form?.classList.toggle("hidden", !showForm);
  saveBtn?.classList.toggle("hidden", !showForm);
  if (saveBtn) {
    saveBtn.textContent = mode === "create" ? "Создать" : "Сохранить";
  }
  userEditTargetId = userId;
  userModalMode = mode;
  if (showForm) {
    loginInput.value = "";
    passwordInput.value = "";
    if (mode === "create") {
      loginInput.placeholder = "Логин";
      passwordInput.placeholder = "Пароль";
    } else {
      loginInput.placeholder = "Новый логин";
      passwordInput.placeholder = "Новый пароль";
    }
  }
  modal?.classList.remove("hidden");
}

function closeUserCredentialsModal() {
  $("#userCredentialsModal")?.classList.add("hidden");
  userEditTargetId = null;
  userModalMode = "edit";
}

async function loadUsers() {
  if (currentUser?.role !== "admin") return;
  try {
    const data = await api("/api/admin/users");
    renderUsersTable(data.items || []);
  } catch (e) {
    showToast(e.message, true);
  }
}

function renderUsersTable(users) {
  const tbody = $("#usersTable tbody");
  if (!tbody) return;
  if (!users.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">Пользователи не найдены</td></tr>`;
    return;
  }

  tbody.innerHTML = users
    .map((user) => {
      const isSelf = user.id === currentUser?.id;
      const roleLabel = ROLE_LABELS[user.role] || user.role;
      const menuItems = [];
      if (user.role === "manager") {
        menuItems.push(`<button type="button" class="user-menu__item" data-user-action="promote" data-user-id="${user.id}">Сделать админом</button>`);
      }
      menuItems.push(`<button type="button" class="user-menu__item" data-user-action="edit" data-user-id="${user.id}" data-user-login="${escapeHtml(user.login)}">Изменить логин или пароль</button>`);
      if (!isSelf) {
        menuItems.push(`<button type="button" class="user-menu__item user-menu__item--danger" data-user-action="delete" data-user-id="${user.id}">Удалить</button>`);
      }
      return `
        <tr>
          <td><code>${escapeHtml(user.login)}</code></td>
          <td>${escapeHtml(roleLabel)}</td>
          <td>${formatHistoryDate(user.created_at)}</td>
          <td class="col-actions">
            <div class="user-actions">
              <button type="button" class="btn btn--icon" data-user-menu="${user.id}" aria-label="Действия">⋮</button>
              <div class="user-menu hidden" id="userMenu-${user.id}">
                ${menuItems.join("")}
              </div>
            </div>
          </td>
        </tr>`;
    })
    .join("");
}

function openAddManagerForm() {
  openUserCredentialsModal({
    title: "Новый менеджер",
    hint: "Введите логин и пароль. Допустимы латинские буквы и символы _ . @ % ! /",
    showForm: true,
    mode: "create",
  });
}

async function handleUserAction(action, userId, login = "") {
  if (action === "promote") {
    if (!window.confirm("Назначить пользователя администратором?")) return;
    try {
      await api(`/api/admin/users/${userId}/promote`, { method: "POST" });
      await loadUsers();
      showToast("Пользователь назначен администратором");
    } catch (e) {
      showToast(e.message, true);
    }
    return;
  }

  if (action === "edit") {
    openUserCredentialsModal({
      title: "Изменить логин или пароль",
      hint: "Заполните новый логин и/или пароль. Допустимы латинские буквы и символы _ . @ % ! /",
      showForm: true,
      userId,
    });
    if (login) $("#editUserLogin").placeholder = `Текущий: ${login}`;
    return;
  }

  if (action === "delete") {
    if (!window.confirm("Удалить пользователя?")) return;
    try {
      await api(`/api/admin/users/${userId}`, { method: "DELETE" });
      await loadUsers();
      showToast("Пользователь удалён");
    } catch (e) {
      showToast(e.message, true);
    }
  }
}

async function saveUserCredentials() {
  const login = $("#editUserLogin").value.trim();
  const password = $("#editUserPassword").value.trim();

  if (userModalMode === "create") {
    if (!login || !password) {
      showToast("Укажите логин и пароль", true);
      return;
    }
    try {
      await api("/api/admin/users/managers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login, password }),
      });
      closeUserCredentialsModal();
      await loadUsers();
      showToast("Менеджер создан");
    } catch (e) {
      showToast(e.message, true);
    }
    return;
  }

  if (!userEditTargetId) return;
  if (!login && !password) {
    showToast("Укажите новый логин или пароль", true);
    return;
  }
  try {
    await api(`/api/admin/users/${userEditTargetId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login: login || null, password: password || null }),
    });
    closeUserCredentialsModal();
    await loadUsers();
    showToast("Данные обновлены");
  } catch (e) {
    showToast(e.message, true);
  }
}

let pendingHistoryDeleteId = null;

function openHistoryDeleteModal(eventId, filename) {
  pendingHistoryDeleteId = eventId;
  const modal = $("#historyDeleteModal");
  const message = $("#historyDeleteMessage");
  if (message) {
    message.textContent = filename
      ? `Файл «${filename}» будет удалён из истории и больше не будет доступен для скачивания.`
      : "Запись будет удалена из истории.";
  }
  modal?.classList.remove("hidden");
}

function closeHistoryDeleteModal() {
  pendingHistoryDeleteId = null;
  $("#historyDeleteModal")?.classList.add("hidden");
}

async function confirmHistoryDelete() {
  if (pendingHistoryDeleteId == null) return;
  const eventId = pendingHistoryDeleteId;
  closeHistoryDeleteModal();
  try {
    await api(`/api/history/downloads/${eventId}`, { method: "DELETE" });
    showToast("Запись удалена из истории");
    await loadHistory();
  } catch (e) {
    showToast(e.message, true);
  }
}

async function loadHistory() {
  try {
    const data = await api("/api/history");
    renderHistory(data);
  } catch (e) {
    showToast(e.message, true);
  }
}

function renderHistory(data) {
  const isAdmin = data.role === "admin";
  const hint = $("#historyHint");
  if (hint) {
    hint.textContent = isAdmin
      ? "Все скачивания сформированных КП по всем менеджерам."
      : "Ваши скачивания сформированных КП.";
  }
  document.querySelectorAll(".col-history-user").forEach((el) => {
    el.classList.toggle("hidden", !isAdmin);
  });

  const downloadsBody = $("#historyDownloadsTable tbody");
  const uploads = data.uploads || [];
  const downloads = data.downloads || [];

  if (downloadsBody) {
    if (!downloads.length) {
      downloadsBody.innerHTML = `<tr><td colspan="5" class="muted">Скачиваний пока нет</td></tr>`;
    } else {
      downloadsBody.innerHTML = downloads
        .map((row) => `
          <tr>
            <td>${escapeHtml(row.filename)}</td>
            <td class="col-history-user${isAdmin ? "" : " hidden"}">${escapeHtml(row.user_login)}</td>
            <td>${escapeHtml(row.tz_filename || "—")}</td>
            <td>${formatHistoryDate(row.downloaded_at)}</td>
            <td>
              <div class="history-actions">
                <a class="btn btn--secondary btn--small" href="${escapeHtml(row.download_url)}?t=${Date.now()}" download>Скачать</a>
                <button
                  type="button"
                  class="btn btn--icon history-delete-btn"
                  data-history-delete="${row.id}"
                  data-history-filename="${escapeHtml(row.filename)}"
                  aria-label="Удалить из истории"
                  title="Удалить из истории"
                >×</button>
              </div>
            </td>
          </tr>`)
        .join("");
    }
  }

  const uploadsBody = $("#historyUploadsTable tbody");
  if (uploadsBody) {
    if (!uploads.length) {
      uploadsBody.innerHTML = `<tr><td colspan="5" class="muted">Загрузок ТЗ пока нет</td></tr>`;
    } else {
      uploadsBody.innerHTML = uploads
        .map((row) => `
          <tr>
            <td>${escapeHtml(row.original_filename)}</td>
            <td class="col-history-user${isAdmin ? "" : " hidden"}">${escapeHtml(row.user_login)}</td>
            <td>${row.items_count ?? "—"}</td>
            <td>${escapeHtml(taskModeLabel(row.task_mode))}</td>
            <td>${formatHistoryDate(row.created_at)}</td>
          </tr>`)
        .join("");
    }
  }
}

function initAuth() {
  $("#btnLogout")?.addEventListener("click", logoutUser);
  $("#btnAddManager")?.addEventListener("click", openAddManagerForm);
  $("#btnRefreshHistory")?.addEventListener("click", loadHistory);
  $("#btnKpHistory")?.addEventListener("click", () => switchToTab("history"));
  $("#btnLookupHistory")?.addEventListener("click", () => switchToTab("history"));
  $("#btnSaveUserCredentials")?.addEventListener("click", saveUserCredentials);
  $("#btnHistoryDeleteConfirm")?.addEventListener("click", confirmHistoryDelete);

  document.querySelectorAll("[data-close-history-delete]").forEach((el) => {
    el.addEventListener("click", closeHistoryDeleteModal);
  });

  $("#historyDownloadsTable")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-history-delete]");
    if (!btn) return;
    event.preventDefault();
    event.stopPropagation();
    openHistoryDeleteModal(
      Number(btn.dataset.historyDelete),
      btn.dataset.historyFilename || "",
    );
  });

  document.querySelectorAll("[data-close-modal]").forEach((el) => {
    el.addEventListener("click", closeUserCredentialsModal);
  });

  document.addEventListener("click", (event) => {
    const menuBtn = event.target.closest("[data-user-menu]");
    if (menuBtn) {
      event.stopPropagation();
      const userId = menuBtn.dataset.userMenu;
      const menu = document.getElementById(`userMenu-${userId}`);
      const shouldOpen = openUserMenuId !== userId;
      closeUserMenus();
      if (shouldOpen && menu) {
        menu.classList.remove("hidden");
        openUserMenuId = userId;
      }
      return;
    }

    const actionBtn = event.target.closest("[data-user-action]");
    if (actionBtn) {
      event.stopPropagation();
      closeUserMenus();
      handleUserAction(actionBtn.dataset.userAction, Number(actionBtn.dataset.userId), actionBtn.dataset.userLogin);
      return;
    }

    if (!event.target.closest(".user-actions")) {
      closeUserMenus();
    }
  });
}

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

const hasKpDownload = (data) => Boolean(data?.kp_formed && data?.summary?.download_url);

const WEB_PRICE_DISCOUNT_PERCENT = 5;

const roundMoney = (value) => Math.round(Number(value) * 100) / 100;

function getCurrentMarkupPercent() {
  const input = $("#markupPercent");
  if (input && input.value !== "") {
    const parsed = Number(input.value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return kpProcessData?.summary?.markup_percent ?? 30;
}

function shouldDeferInternetRowPrice(item, selection = {}) {
  if (!collectWebProductEntries(item).length) return false;
  const variant = selection.variant || savedVariantIdForItem(item.number) || "primary";
  if (variant !== "primary") return false;
  if (item.kit_components?.length) return false;
  if (isLocalDataSource(item) && item.unit_base_price != null) return false;
  return Boolean(item.internet_priced || item.source === "web");
}

function emptyItemPricing(item) {
  return {
    status: item.status,
    internetPriced: false,
    unitCost: null,
    unitBasePrice: null,
    unitPrice: null,
    totalCost: 0,
    totalBasePrice: 0,
    totalPrice: 0,
  };
}

function quotePricingFromItem(item, selection) {
  if (selection.variant === "primary") {
    if (shouldDeferInternetRowPrice(item, selection)) {
      return {
        unitCost: null,
        unitBasePrice: null,
        internetPriced: false,
        status: item.status,
      };
    }
    return {
      unitCost: item.unit_cost,
      unitBasePrice: item.unit_base_price,
      internetPriced: item.internet_priced,
      status: item.status,
    };
  }

  let quote = null;
  if (selection.variant.startsWith("local:")) {
    const index = Number.parseInt(selection.variant.split(":")[1], 10);
    const quotes = (item.comparison || []).filter(
      (q) => q.source !== "web" && quoteMeetsMatchThreshold(q),
    );
    quote = quotes[index];
  } else if (selection.variant.startsWith("web:")) {
    const index = Number.parseInt(selection.variant.split(":")[1], 10);
    const quotes = collectWebProductEntries(item);
    quote = quotes[index];
    if (quote) {
      const base = resolveWebQuoteBasePrice(item, index, quote, selection);
      return {
        unitCost: base,
        unitBasePrice: base,
        internetPriced: true,
        status: item.status,
      };
    }
  }

  if (!quote) {
    return {
      unitCost: item.unit_cost,
      unitBasePrice: item.unit_base_price,
      internetPriced: item.internet_priced,
      status: item.status,
    };
  }

  return {
    unitCost: quote.cost ?? quote.price ?? null,
    unitBasePrice: quote.price ?? quote.cost ?? null,
    internetPriced: selection.variant.startsWith("web:"),
    status: item.status,
  };
}

function pricingTotalsFromLine({ unitCost, unitBasePrice, quantity, internetPriced }) {
  const qty = quantity || 1;
  const totalCost = unitCost != null ? roundMoney(unitCost * qty) : 0;
  if (unitBasePrice == null) {
    return { totalCost, totalBasePrice: 0, totalPrice: 0, unitPrice: null };
  }
  const totalBasePrice = roundMoney(unitBasePrice * qty);
  const multiplier = internetPriced
    ? 1 - WEB_PRICE_DISCOUNT_PERCENT / 100
    : 1 + getCurrentMarkupPercent() / 100;
  const unitPrice = roundMoney(unitBasePrice * multiplier);
  const totalPrice = roundMoney(unitPrice * qty);
  return { totalCost, totalBasePrice, totalPrice, unitPrice };
}

function aggregateKitPricing(item, kitIndices) {
  const components = item.kit_components || [];
  if (!components.length) return null;
  const indices =
    kitIndices === null || kitIndices === undefined
      ? components.map((_, index) => index)
      : kitIndices;
  const selected = indices
    .filter((index) => index >= 0 && index < components.length)
    .map((index) => components[index]);
  if (!selected.length) {
    return {
      unitCost: null,
      unitBasePrice: null,
      unitPrice: null,
      totalCost: 0,
      totalBasePrice: 0,
      totalPrice: 0,
    };
  }
  const unitCostValues = selected
    .map((line) =>
      line.unit_cost != null ? roundMoney(line.unit_cost * (line.quantity || 1)) : null,
    )
    .filter((value) => value != null);
  const unitBaseValues = selected
    .map((line) =>
      line.unit_price != null ? roundMoney(line.unit_price * (line.quantity || 1)) : null,
    )
    .filter((value) => value != null);
  const unitCost = unitCostValues.length ? roundMoney(unitCostValues.reduce((a, b) => a + b, 0)) : null;
  const unitBasePrice = unitBaseValues.length
    ? roundMoney(unitBaseValues.reduce((a, b) => a + b, 0))
    : null;
  const totals = pricingTotalsFromLine({
    unitCost,
    unitBasePrice,
    quantity: item.quantity,
    internetPriced: item.internet_priced,
  });
  return {
    unitCost,
    unitBasePrice,
    unitPrice: totals.unitPrice,
    totalCost: totals.totalCost,
    totalBasePrice: totals.totalBasePrice,
    totalPrice: totals.totalPrice,
  };
}

function getKitIndicesFromUI(itemNumber) {
  const boxes = [...document.querySelectorAll(`.kp-kit-include[data-item="${itemNumber}"]`)];
  if (!boxes.length) return null;
  return boxes.filter((box) => box.checked).map((box) => Number(box.dataset.kitIndex));
}

function shouldAutoSelectSingleWebRow(item) {
  if (!shouldDeferInternetRowPrice(item)) return false;
  return collectWebProductEntries(item).length === 1;
}

function getDefaultWebIndices(item) {
  const saved = kpSavedSelections?.find((selection) => selection.number === item.number);
  if (saved?.web_indices) {
    return [...saved.web_indices];
  }
  if (shouldAutoSelectSingleWebRow(item)) {
    return [0];
  }
  return [];
}

function getWebIndicesFromUI(itemNumber) {
  const boxes = [...document.querySelectorAll(`.kp-web-include[data-item="${itemNumber}"]`)];
  if (boxes.length) {
    return boxes.filter((box) => box.checked).map((box) => Number(box.dataset.webIndex));
  }
  const item = kpProcessData?.items?.find((row) => row.number === itemNumber);
  return item ? getDefaultWebIndices(item) : [];
}

function hasPositionDetailSelection(item, selection = {}) {
  const kitIndices =
    selection.kit_indices !== undefined
      ? selection.kit_indices
      : getKitIndicesFromUI(item.number);
  const webIndices =
    selection.web_indices !== undefined
      ? selection.web_indices
      : getWebIndicesFromUI(item.number);
  const hasKitSelection = Boolean(item.kit_components?.length && kitIndices?.length);
  const hasWebSelection = Boolean(
    collectWebProductEntries(item).length &&
      webIndices?.some((index) => {
        const quote = collectWebProductEntries(item)[index];
        return quote && resolveWebQuoteBasePrice(item, index, quote) != null;
      }),
  );
  return hasKitSelection || hasWebSelection;
}

function kitSelectedBaseTotal(item, itemNumber = item.number) {
  if (!item.kit_components?.length) return 0;
  const indices = getKitIndicesFromUI(itemNumber);
  const pricing = aggregateKitPricing(
    item,
    indices === null ? item.kit_components.map((_, index) => index) : indices,
  );
  return pricing?.unitBasePrice ?? 0;
}

function updateKitComponentTotal(itemNumber) {
  const item = kpProcessData?.items?.find((row) => row.number === itemNumber);
  const kitTotalEl = document.querySelector(`.kp-kit-total[data-item="${itemNumber}"]`);
  if (!item || !kitTotalEl) return;
  kitTotalEl.textContent = fmtMoney(kitSelectedBaseTotal(item, itemNumber));
}

function isWebQuoteChecked(itemNumber, index) {
  const saved = kpSavedSelections?.find((selection) => selection.number === itemNumber);
  if (saved?.web_indices) {
    return saved.web_indices.includes(index);
  }
  const item = kpProcessData?.items?.find((row) => row.number === itemNumber);
  return Boolean(item && shouldAutoSelectSingleWebRow(item) && index === 0);
}

function isKitComponentChecked(itemNumber, index) {
  const saved = kpSavedSelections?.find((selection) => selection.number === itemNumber);
  if (saved?.kit_indices) {
    return saved.kit_indices.includes(index);
  }
  const box = document.querySelector(
    `.kp-kit-include[data-item="${itemNumber}"][data-kit-index="${index}"]`,
  );
  return box ? box.checked : true;
}

function computeItemPricing(item, selection = {}) {
  const variant = selection.variant || savedVariantIdForItem(item.number) || "primary";
  const kitIndices =
    selection.kit_indices !== undefined
      ? selection.kit_indices
      : getKitIndicesFromUI(item.number);
  const webIndices =
    selection.web_indices !== undefined
      ? selection.web_indices
      : getWebIndicesFromUI(item.number);
  const hasWebTable = collectWebProductEntries(item).length > 0;
  const deferInternetPrice = shouldDeferInternetRowPrice(item, { ...selection, variant });

  let base;
  if (item.kit_components?.length && kitIndices !== null) {
    const kitPricing = aggregateKitPricing(item, kitIndices);
    if (kitPricing) {
      base = {
        status: item.status,
        internetPriced: false,
        ...kitPricing,
      };
    }
  }
  if (!base) {
    if (deferInternetPrice) {
      if (!webIndices?.length) {
        return emptyItemPricing(item);
      }
      const webOnly = aggregateWebAddonPricing(item, webIndices, selection);
      return {
        status: item.status,
        internetPriced: Boolean(webOnly.unitBasePrice != null),
        unitCost: webOnly.unitCost,
        unitBasePrice: webOnly.unitBasePrice,
        unitPrice: webOnly.unitPrice,
        totalCost: webOnly.totalCost,
        totalBasePrice: webOnly.totalBasePrice,
        totalPrice: webOnly.totalPrice,
      };
    }
    const pricing = quotePricingFromItem(item, { ...selection, variant });
    const totals = pricingTotalsFromLine({
      unitCost: pricing.unitCost,
      unitBasePrice: pricing.unitBasePrice,
      quantity: item.quantity,
      internetPriced: pricing.internetPriced,
    });
    base = {
      status: pricing.status,
      internetPriced: pricing.internetPriced,
      unitCost: pricing.unitCost,
      unitBasePrice: pricing.unitBasePrice,
      unitPrice: totals.unitPrice,
      totalCost: totals.totalCost,
      totalBasePrice: totals.totalBasePrice,
      totalPrice: totals.totalPrice,
    };
  }
  if (!hasWebTable || deferInternetPrice) {
    return base;
  }
  const webAddon = aggregateWebAddonPricing(item, webIndices, selection);
  if (!webIndices?.length) {
    return base;
  }
  return mergeItemPricing(base, webAddon);
}

function syncKitSelectAll(itemNumber) {
  const boxes = [...document.querySelectorAll(`.kp-kit-include[data-item="${itemNumber}"]`)];
  const selectAll = document.querySelector(`.kp-kit-select-all[data-item="${itemNumber}"]`);
  if (!boxes.length || !selectAll) return;
  const checkedCount = boxes.filter((box) => box.checked).length;
  selectAll.checked = checkedCount === boxes.length;
  selectAll.indeterminate = checkedCount > 0 && checkedCount < boxes.length;
}

function updateWebAddonTotal(itemNumber) {
  const item = kpProcessData?.items?.find((row) => row.number === itemNumber);
  const totalEl = document.querySelector(`.kp-web-total[data-item="${itemNumber}"]`);
  if (!item || !totalEl) return;
  const addon = aggregateWebAddonPricing(item, getWebIndicesFromUI(itemNumber));
  totalEl.textContent = fmtMoney(addon.totalPrice);
}

function updateTzRowPricing(itemNumber) {
  const item = kpProcessData?.items?.find((row) => row.number === itemNumber);
  const row = document.querySelector(`.tz-row[data-item-number="${itemNumber}"]`);
  if (!item || !row) return;

  const pricing = computeItemPricing(item, {
    variant: savedVariantIdForItem(itemNumber),
    kit_indices: getKitIndicesFromUI(itemNumber),
    web_indices: getWebIndicesFromUI(itemNumber),
  });
  const unitBaseCell = row.querySelector(".tz-row__price-base");
  const unitKpCell = row.querySelector(".tz-row__price-kp");
  const lineTotalCell = row.querySelector(".tz-row__line-total");
  if (unitBaseCell) {
    unitBaseCell.innerHTML = `${fmtMoney(pricing.unitBasePrice)}${
      pricing.internetPriced ? '<br><small class="muted">интернет</small>' : ""
    }`;
  }
  if (unitKpCell) {
    unitKpCell.innerHTML = `${fmtMoney(pricing.unitPrice)}${
      pricing.internetPriced ? '<br><small class="muted">−5%</small>' : ""
    }`;
  }
  if (lineTotalCell) {
    lineTotalCell.textContent = fmtMoney(pricing.totalPrice);
  }

  const kitTotalEl = document.querySelector(`.kp-kit-total[data-item="${itemNumber}"]`);
  if (kitTotalEl) {
    kitTotalEl.textContent = fmtMoney(kitSelectedBaseTotal(item, itemNumber));
  }
  syncKitSelectAll(itemNumber);
}

function buildSummaryFromSelections(items, selections, baseSummary) {
  const itemsByNumber = Object.fromEntries(items.map((item) => [item.number, item]));
  const lines = selections
    .filter((selection) => selection.included)
    .map((selection) => {
      const item = itemsByNumber[selection.number];
      if (!item) return null;
      const pricing = computeItemPricing(item, selection);
      return { status: pricing.status, ...pricing };
    })
    .filter(Boolean);

  return {
    ...baseSummary,
    total_items: lines.length,
    exact_count: lines.filter((line) => line.status === "exact").length,
    similar_count: lines.filter((line) => line.status === "similar").length,
    not_found_count: lines.filter((line) => line.status === "not_found").length,
    total_cost: roundMoney(lines.reduce((sum, line) => sum + (line.totalCost || 0), 0)),
    total_base_price: roundMoney(lines.reduce((sum, line) => sum + (line.totalBasePrice || 0), 0)),
    total_price: roundMoney(lines.reduce((sum, line) => sum + (line.totalPrice || 0), 0)),
    markup_percent: getCurrentMarkupPercent(),
  };
}

let kpProcessData = null;
let kpFormed = false;
let kpExportSummary = null;
let kpSavedSelections = null;
let kpSelectionSaved = false;
let kpLastUploadedFileName = "";

function isMarketEstimateQuote(q) {
  const label = (q?.label || "").toLowerCase();
  return label.includes("оценка рынка") || label.includes("оценка ai");
}

function collectMarketEstimateQuotes(item) {
  const seen = new Set();
  const rows = [];
  for (const q of [...(item.comparison || []), ...(item.competitors || [])]) {
    if (!isMarketEstimateQuote(q)) continue;
    const key = q.url || `${q.label}|${q.matched_name}|${q.price}`;
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push(q);
  }
  return rows;
}

function isMarketEstimateLabel(label) {
  const text = (label || "").toLowerCase();
  return text.includes("оценка рынка") || text.includes("оценка ai");
}

function marketplaceLabelFromUrl(url) {
  const lower = (url || "").toLowerCase();
  if (lower.includes("ozon.ru")) return "Ozon";
  if (lower.includes("market.yandex.ru")) return "Яндекс.Маркет";
  if (lower.includes("wildberries.ru")) return "Wildberries";
  return shortenUrlForDisplay(url);
}

function buildMarketplaceSearchUrl(platformLabel, query) {
  const q = encodeURIComponent((query || "").trim());
  if (!q) return "";
  const lower = platformLabel.toLowerCase();
  if (lower.includes("ozon")) return `https://www.ozon.ru/search/?text=${q}`;
  if (lower.includes("яндекс") || lower.includes("market")) {
    return `https://market.yandex.ru/search?text=${q}`;
  }
  if (lower.includes("wild")) {
    return `https://www.wildberries.ru/catalog/0/search.aspx?search=${q}`;
  }
  return `https://www.ozon.ru/search/?text=${q}`;
}

function collectMarketplaceSearchLinks(item) {
  if (!shouldShowInternetLinks(item)) return [];
  const links = [];
  const seen = new Set();
  const push = (url, label) => {
    if (!url || !isSearchListingUrl(url) || seen.has(url)) return;
    seen.add(url);
    links.push({ url, label: label || marketplaceLabelFromUrl(url) });
  };

  if (Array.isArray(item.marketplace_urls)) {
    for (const url of item.marketplace_urls) push(url);
  }

  for (const q of [...(item.comparison || []), ...(item.competitors || [])]) {
    if (q.source !== "web" || !q.url || !isSearchListingUrl(q.url)) continue;
    const label = (q.label || "").replace(/^Интернет:\s*/i, "").trim();
    push(q.url, label);
  }

  if (!links.length) {
    const query = item.matched_name || item.name || "";
    for (const platform of ["Ozon", "Яндекс.Маркет", "Wildberries"]) {
      push(buildMarketplaceSearchUrl(platform, query), platform);
    }
  }
  return links;
}

function renderMarketplaceSearchLinks(item) {
  const links = collectMarketplaceSearchLinks(item);
  if (!links.length) return "";
  return links
    .map(
      (entry) =>
        `<a href="${escapeHtml(entry.url)}" target="_blank" rel="noopener">${escapeHtml(entry.label)}</a>`,
    )
    .join(" · ");
}

function renderMarketEstimateLink(q, item) {
  const url = q.url && !isSearchListingUrl(q.url) ? q.url : null;
  const title = (q.matched_name || "").trim();
  if (url) {
    const text = title || shortenUrlForDisplay(url);
    return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(text)}</a>`;
  }
  const marketLinks = item ? renderMarketplaceSearchLinks(item) : "";
  if (marketLinks) return marketLinks;
  if (title) return escapeHtml(title);
  return "—";
}

function renderMarketEstimateInfo(item) {
  const quotes = collectMarketEstimateQuotes(item);
  if (!quotes.length) return "";
  return quotes
    .map((q) => {
      const price = q.price ?? q.cost;
      return `
    <div class="compare-block__market-estimate">
      <strong>${escapeHtml(q.label || "Интернет (оценка рынка)")}:</strong>
      ${renderMarketEstimateLink(q, item)}${
        q.match_score ? `<span class="muted"> · ${Math.round(q.match_score)}%</span>` : ""
      }${price != null ? `<span class="muted"> · ${fmtMoney(price)}</span>` : ""}
    </div>`;
    })
    .join("");
}

function listItemVariants(item) {
  const variants = [
    {
      id: "primary",
      label: "Основное совпадение",
      name: "",
      meta: "",
      price: shouldDeferInternetRowPrice(item, { variant: "primary" })
        ? null
        : item.unit_base_price ?? item.unit_price ?? null,
      headerOnly: true,
    },
  ];
  (item.comparison || [])
    .filter((q) => q.source !== "web" && quoteMeetsMatchThreshold(q))
    .forEach((q, index) => {
      variants.push({
        id: `local:${index}`,
        label: q.label || "Источник",
        name: q.matched_name || "—",
        meta: q.match_score ? `${Math.round(q.match_score)}%` : "",
        price: q.price ?? q.cost ?? null,
        headerOnly: false,
      });
    });
  collectWebProductEntries(item).forEach((q, index) => {
    variants.push({
      id: `web:${index}`,
        label: q.label || "Интернет",
        name: q.matched_name || "—",
        meta: q.match_score ? `${Math.round(q.match_score)}%` : "",
        price: q.price ?? q.cost ?? null,
        headerOnly: false,
      });
    });
  return variants;
}

function formatVariantLine(variant) {
  if (variant.headerOnly) {
    return `<strong>${escapeHtml(variant.label)}:</strong>`;
  }
  const details = [variant.name, variant.meta].filter(Boolean).map(escapeHtml).join(" · ");
  const price = variant.price != null ? ` — ${fmtMoney(variant.price)}` : "";
  return `<strong>${escapeHtml(variant.label)}:</strong> ${details}${price}`;
}

function savedVariantIdForItem(itemNumber) {
  const saved = kpSavedSelections?.find((selection) => selection.number === itemNumber);
  if (saved?.variant) return saved.variant;
  const selected = document.querySelector(
    `.kp-variant-line--selected[data-item="${itemNumber}"]`,
  );
  return selected?.dataset.variant || "primary";
}

function renderVariantChoices(item) {
  const variants = listItemVariants(item);
  if (variants.length <= 1) return "";

  const selectedId = savedVariantIdForItem(item.number);
  const lines = variants.map((variant) => {
    const isSelected = variant.id === selectedId;
    return `<button
      type="button"
      class="kp-variant-line${isSelected ? " kp-variant-line--selected" : ""}"
      data-item="${item.number}"
      data-variant="${variant.id}"
    >${formatVariantLine(variant)}</button>`;
  });

  return `<div class="compare-block__primary kp-variant-block"><strong>Вариант для КП:</strong><br>${lines.join("<br>")}</div>`;
}

function disableDownloadLink(link) {
  if (!link) return;
  link.href = "#";
  link.removeAttribute("download");
  link.classList.add("btn--disabled");
  link.setAttribute("aria-disabled", "true");
}

function enableDownloadLink(link, url, filename) {
  if (!link || !url) return;
  link.href = `${url}?t=${Date.now()}`;
  if (filename) link.download = filename;
  link.classList.remove("btn--disabled");
  link.removeAttribute("aria-disabled");
}

function resetKpFormed() {
  kpFormed = false;
  kpExportSummary = null;
  updateKpExportButtons();
}

function resetKpSavedSelection() {
  kpSavedSelections = null;
  kpSelectionSaved = false;
  const panel = $("#kpSavedSelection");
  if (panel) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
  }
  resetKpFormed();
  updateAssistantMode(kpProcessData);
}

function resolveSelectionPreview(item, selection) {
  if (!selection?.included) return null;

  const base = {
    number: item.number,
    tzName: item.name,
    quantity: item.quantity,
    unit: item.unit,
  };

  const pricing = computeItemPricing(item, selection);
  const variantLabel =
    selection.variant === "primary"
      ? "Основное совпадение"
      : selection.variant.startsWith("local:")
        ? (item.comparison || []).filter(
            (q) => q.source !== "web" && quoteMeetsMatchThreshold(q),
          )[Number.parseInt(selection.variant.split(":")[1], 10)]?.label || "Источник"
        : selection.variant.startsWith("web:")
          ? collectWebProductEntries(item)[
              Number.parseInt(selection.variant.split(":")[1], 10)
            ]?.label || "Интернет"
          : SOURCE_LABELS[item.source] || item.source || "—";

  let matched = item.matched_name || item.name;
  if (selection.variant.startsWith("local:")) {
    const index = Number.parseInt(selection.variant.split(":")[1], 10);
    const quote = (item.comparison || []).filter(
      (q) => q.source !== "web" && quoteMeetsMatchThreshold(q),
    )[index];
    if (quote?.matched_name) matched = quote.matched_name;
  } else if (selection.variant.startsWith("web:")) {
    const index = Number.parseInt(selection.variant.split(":")[1], 10);
    const quote = collectWebProductEntries(item)[index];
    if (quote?.matched_name) matched = quote.matched_name;
  }

  if (item.kit_components?.length && selection.kit_indices?.length) {
    const parts = selection.kit_indices
      .map((index) => item.kit_components[index]?.name)
      .filter(Boolean);
    if (parts.length) {
      matched = `${matched} (${parts.length}/${item.kit_components.length} в составе)`;
    }
  }

  if (selection.web_indices?.length) {
    const webParts = selection.web_indices
      .map((index) => collectWebProductEntries(item)[index]?.matched_name)
      .filter(Boolean);
    if (webParts.length) {
      matched = `${matched} (+${webParts.length} из интернета)`;
    }
  }

  return {
    ...base,
    matched,
    source: selection.variant === "primary" ? SOURCE_LABELS[item.source] || item.source || "—" : variantLabel,
    unitPrice: pricing.unitPrice ?? pricing.unitBasePrice,
    total: pricing.totalPrice,
  };
}

function renderSavedSelectionList() {
  const panel = $("#kpSavedSelection");
  if (!panel || !kpSelectionSaved || !kpSavedSelections?.length || !kpProcessData?.items) {
    panel?.classList.add("hidden");
    return;
  }

  const itemsByNumber = Object.fromEntries(
    kpProcessData.items.map((item) => [item.number, item]),
  );
  const rows = kpSavedSelections
    .filter((selection) => selection.included)
    .map((selection) => resolveSelectionPreview(itemsByNumber[selection.number], selection))
    .filter(Boolean);

  if (!rows.length) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }

  const total = rows.reduce((sum, row) => sum + (row.total ?? 0), 0);
  panel.classList.remove("hidden");
  panel.innerHTML = `
    <div class="kp-saved-selection__head">
      <h3>Выбрано для КП: ${rows.length} поз.</h3>
      <p class="muted">Итого по выбранным: ${fmtMoney(total)}</p>
    </div>
    <div class="table-wrap">
      <table class="data-table kp-saved-selection__table">
        <thead>
          <tr>
            <th>#</th>
            <th>Позиция ТЗ</th>
            <th>Выбранный товар</th>
            <th>Источник</th>
            <th>Кол-во</th>
            <th>Цена КП</th>
            <th>Сумма</th>
          </tr>
        </thead>
        <tbody>
          ${rows
            .map(
              (row) => `
            <tr>
              <td>${row.number}</td>
              <td>${escapeHtml(row.tzName)}</td>
              <td>${escapeHtml(row.matched)}</td>
              <td>${escapeHtml(row.source)}</td>
              <td>${fmtQty(row.quantity, row.unit)}</td>
              <td>${fmtMoney(row.unitPrice)}</td>
              <td>${fmtMoney(row.total)}</td>
            </tr>`,
            )
            .join("")}
        </tbody>
      </table>
    </div>`;
}

async function saveKpSelection() {
  if (!kpProcessData?.search_completed) {
    showToast("Сначала выполните поиск по ТЗ", true);
    return;
  }

  const selections = getSelectionsFromUI();
  const included = selections.filter((item) => item.included);
  if (!included.length) {
    showToast("Отметьте хотя бы одну позицию", true);
    return;
  }

  for (const selection of included) {
    const item = kpProcessData.items.find((row) => row.number === selection.number);
    if (!item) continue;
    if (!item.kit_components?.length && !collectWebProductEntries(item).length) continue;
    if (!hasPositionDetailSelection(item, selection)) {
      showToast(
        `Позиция ${selection.number}: выберите состав комплекта и/или позиции из интернета`,
        true,
      );
      return;
    }
    const webIndices = selection.web_indices ?? getWebIndicesFromUI(item.number);
    for (const index of webIndices) {
      const quote = collectWebProductEntries(item)[index];
      if (!quote) continue;
      if (resolveWebQuoteBasePrice(item, index, quote) == null) {
        showToast(
          `Позиция ${selection.number}: укажите цену в интернете для выбранной строки`,
          true,
        );
        return;
      }
    }
  }

  try {
    if (kpSessionId) {
      const data = await api("/api/kp/selection/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: kpSessionId, selections }),
      });
      if (data.summary) {
        kpProcessData = {
          ...kpProcessData,
          summary: data.summary,
          stage: data.stage || "selection_saved",
        };
      }
    } else if (kpProcessData.summary) {
      kpProcessData = {
        ...kpProcessData,
        summary: buildSummaryFromSelections(
          kpProcessData.items,
          selections,
          kpProcessData.summary,
        ),
        stage: "selection_saved",
      };
    }

    kpSavedSelections = selections;
    kpSelectionSaved = true;
    resetKpFormed();
    renderSavedSelectionList();
    if (kpProcessData?.summary) {
      renderProcessSummary(kpProcessData);
    }
    updateKpExportButtons();
    updateAssistantMode(kpProcessData);
    showToast(`Выбор сохранён: ${included.length} поз.`);
  } catch (error) {
    showToast(error.message, true);
  }
}

function updateKpExportButtons() {
  const btnSave = $("#btnSaveKpSelection");
  const btnForm = $("#btnFormKp");
  if (btnSave) {
    btnSave.toggleAttribute("disabled", !kpProcessData?.search_completed);
  }
  if (btnForm) {
    btnForm.toggleAttribute("disabled", !kpSelectionSaved);
  }
  if (kpFormed && kpExportSummary) {
    enableDownloadLink($("#downloadPdfBtn"), kpExportSummary.pdf_download_url, kpExportSummary.pdf_filename);
    enableDownloadLink($("#downloadBtn"), kpExportSummary.download_url, kpExportSummary.filename);
  } else {
    disableDownloadLink($("#downloadPdfBtn"));
    disableDownloadLink($("#downloadBtn"));
  }
}

function getSelectionsFromUI() {
  if (!kpProcessData?.items?.length) return [];
  return kpProcessData.items.map((item) => {
    const includeEl = document.querySelector(`.kp-item-include[data-item="${item.number}"]`);
    const include = includeEl ? includeEl.checked : true;
    const checkedVariant = document.querySelector(
      `.kp-variant-line--selected[data-item="${item.number}"]`,
    );
    const kitIndices = getKitIndicesFromUI(item.number);
    const selection = {
      number: item.number,
      included: include,
      variant: checkedVariant?.dataset.variant || "primary",
    };
    if (item.kit_components?.length && kitIndices !== null) {
      selection.kit_indices = kitIndices;
    }
    if (collectWebProductEntries(item).length) {
      selection.web_indices = getWebIndicesFromUI(item.number);
      const manualPrices = collectWebManualPricesForSelection(item, selection.web_indices);
      if (manualPrices) {
        selection.web_manual_prices = manualPrices;
      }
    }
    return selection;
  });
}

function bindKpSelectionHandlers() {
  const tbody = $("#resultsTable tbody");
  const selectAll = $("#selectAllKpItems");
  selectAll?.addEventListener("change", (event) => {
    $$(".kp-item-include").forEach((checkbox) => {
      checkbox.checked = event.target.checked;
    });
    resetKpSavedSelection();
  });

  tbody?.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.classList.contains("kp-item-include")) {
      const boxes = $$(".kp-item-include");
      if (selectAll) {
        selectAll.checked = [...boxes].every((box) => box.checked);
        selectAll.indeterminate = !selectAll.checked && [...boxes].some((box) => box.checked);
      }
      resetKpSavedSelection();
      return;
    }
    if (target.classList.contains("kp-kit-include") || target.classList.contains("kp-kit-select-all")) {
      const itemNumber = Number(target.dataset.item);
      if (target.classList.contains("kp-kit-select-all")) {
        const checked = target.checked;
        $$(`.kp-kit-include[data-item="${itemNumber}"]`).forEach((box) => {
          box.checked = checked;
        });
      }
      $$(`.kp-kit-row[data-item="${itemNumber}"]`).forEach((row) => {
        const box = row.querySelector(".kp-kit-include");
        row.classList.toggle("kp-kit-row--excluded", box ? !box.checked : false);
      });
      resetKpSavedSelection();
      updateTzRowPricing(itemNumber);
      updateKitComponentTotal(itemNumber);
      return;
    }
    if (target.classList.contains("kp-web-include")) {
      const itemNumber = Number(target.dataset.item);
      const row = target.closest(".kp-web-row");
      if (row) {
        row.classList.toggle("kp-web-row--excluded", !target.checked);
      }
      resetKpSavedSelection();
      updateTzRowPricing(itemNumber);
      updateWebAddonTotal(itemNumber);
      updateKitComponentTotal(itemNumber);
      return;
    }
  });

  tbody?.addEventListener("input", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (!target.classList.contains("kp-web-price-input")) return;
    const itemNumber = Number(target.dataset.item);
    const webIndex = Number(target.dataset.webIndex);
    const price = parseMoneyInput(target.value);
    setWebManualPrice(itemNumber, webIndex, price);
    updateWebRowKpPrice(itemNumber, webIndex);
    resetKpSavedSelection();
    updateTzRowPricing(itemNumber);
    updateWebAddonTotal(itemNumber);
  });

  tbody?.addEventListener(
    "blur",
    (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement)) return;
      if (!target.classList.contains("kp-web-price-input")) return;
      const price = parseMoneyInput(target.value);
      if (price != null) {
        target.value = formatMoneyInputValue(price);
      }
    },
    true,
  );

  tbody?.addEventListener("click", (event) => {
    const variantBtn = event.target.closest(".kp-variant-line");
    if (variantBtn) {
      event.stopPropagation();
      const itemNumber = Number(variantBtn.dataset.item);
      $$(".kp-variant-line").forEach((btn) => {
        if (btn.dataset.item === String(itemNumber)) {
          btn.classList.toggle("kp-variant-line--selected", btn === variantBtn);
        }
      });
      resetKpSavedSelection();
      updateTzRowPricing(itemNumber);
      return;
    }
    if (event.target.closest(".kp-select-cell, .kp-variant-block, .kp-kit-select-cell, .kp-web-select-cell, .kp-web-price-cell")) {
      event.stopPropagation();
    }
  });
}

async function formKpDocument() {
  if (!kpSessionId) {
    showToast("Сначала выполните поиск по ТЗ", true);
    return;
  }
  if (!kpSelectionSaved || !kpSavedSelections?.length) {
    showToast('Сначала нажмите «Сохранить выбор»', true);
    return;
  }
  const included = kpSavedSelections.filter((item) => item.included);
  if (!included.length) {
    showToast("Выберите хотя бы одну позицию", true);
    return;
  }
  for (const selection of included) {
    const item = kpProcessData?.items?.find((row) => row.number === selection.number);
    if (!item) continue;
    if (!item.kit_components?.length && !collectWebProductEntries(item).length) continue;
    if (!hasPositionDetailSelection(item, selection)) {
      showToast(
        `Позиция ${selection.number}: выберите состав комплекта и/или позиции из интернета`,
        true,
      );
      return;
    }
    const webIndices = selection.web_indices ?? getWebIndicesFromUI(selection.number);
    for (const index of webIndices) {
      const quote = collectWebProductEntries(item)[index];
      if (!quote) continue;
      if (resolveWebQuoteBasePrice(item, index, quote) == null) {
        showToast(
          `Позиция ${selection.number}: укажите цену в интернете для выбранной строки`,
          true,
        );
        return;
      }
    }
  }
  showOverlay("Формирую КП (Excel и PDF)...");
  try {
    const data = await api("/api/kp/form", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: kpSessionId, selections: kpSavedSelections }),
    });
    kpFormed = true;
    kpExportSummary = data.summary;
    if (kpProcessData) {
      kpProcessData = { ...kpProcessData, ...data, kp_formed: true, items: kpProcessData.items };
      if (data.summary) {
        renderProcessSummary(kpProcessData);
      }
    }
    updateAssistantMode(kpProcessData || data);
    updateKpExportButtons();
    showToast(`КП сформировано: ${data.selected_count || 0} поз.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    hideOverlay();
  }
}

function renderProcessSummary(data) {
  const s = data.summary;
  const summaryEl = $("#resultsSummary");
  const summaryCard = $("#processSummaryCard");
  if (!summaryEl || !summaryCard || !s) return;

  const marginAmount = Number(s.total_price) - Number(s.total_cost);
  const marginPercent = calcMarginPercent(s.total_cost, s.total_price);
  const selectionNote = kpSelectionSaved ? " · по выбранным позициям" : "";
  const markupNote =
    s.markup_percent != null ? ` · наценка ${fmtPercent(s.markup_percent)}` : "";

  summaryCard.classList.remove("hidden");
  summaryEl.innerHTML = `
    <div class="summary-metrics">
      <div class="metric"><div class="metric__label">Позиций</div><div class="metric__value">${s.total_items}</div></div>
      <div class="metric metric--success"><div class="metric__label">Полное совпадение</div><div class="metric__value">${s.exact_count}</div></div>
      <div class="metric metric--warning"><div class="metric__label">Частичное совпадение</div><div class="metric__value">${s.similar_count}</div></div>
      <div class="metric metric--danger"><div class="metric__label">Не найдено</div><div class="metric__value">${s.not_found_count}</div></div>
      <div class="metric"><div class="metric__label">Себестоимость</div><div class="metric__value">${fmtMoney(s.total_cost)}</div></div>
      <div class="metric metric--accent"><div class="metric__label">Общая стоимость КП</div><div class="metric__value">${fmtMoney(s.total_price)}</div></div>
      <div class="metric"><div class="metric__label">Маржа</div><div class="metric__value">${fmtMoney(marginAmount)}<span class="metric__sub">${fmtPercent(marginPercent)}</span></div></div>
      <div class="metric"><div class="metric__label">Цена без наценки</div><div class="metric__value">${fmtMoney(s.total_base_price)}</div></div>
    </div>
    <p class="muted process-summary-note">Время: ${s.processing_seconds} сек · ${taskModeLabel(data.task_mode)} · AI: ${data.ai_used ? "да" : "нет"}${data.web_used ? " · конкуренты" : ""}${markupNote}${selectionNote}${data.kp_formed ? " · КП готово" : ""}</p>
  `;
}

const stageLabel = (stage, searchCompleted) => {
  const map = {
    intake: "ожидание ТЗ",
    parsed: "ТЗ разобрано",
    searched: "поиск выполнен — отметьте и сохраните выбор",
    selection_saved: "выбор сохранён — сформируйте КП",
    exported: "КП сформировано",
  };
  if (!searchCompleted && stage === "parsed") return "поиск не запускался";
  return map[stage] || stage;
};

function updateAssistantMode(data) {
  const taskEl = $("#taskModeLabel");
  const stageEl = $("#stageLabel");
  if (!taskEl || !stageEl) return;
  taskEl.textContent = taskModeLabel(data?.task_mode);
  let stage = data?.stage;
  if (data?.kp_formed || kpFormed) {
    stage = "exported";
  } else if (kpSelectionSaved) {
    stage = "selection_saved";
  }
  stageEl.textContent = stageLabel(stage, data?.search_completed);
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

function formatApiErrorDetail(detail) {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item.msg === "string") return item.msg;
        return JSON.stringify(item);
      })
      .join("; ");
  }
  if (typeof detail === "object") {
    return detail.msg || detail.message || JSON.stringify(detail);
  }
  return String(detail);
}

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: "include", ...options });
  if (res.status === 401 && !path.includes("/api/auth/login")) {
    window.location.href = "/login.html";
    throw new Error("Требуется авторизация");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const message = formatApiErrorDetail(data.detail) || `Ошибка ${res.status}`;
    throw new Error(message);
  }
  return data;
}

function initTabs() {
  $$(".tabs__btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      switchToTab(btn.dataset.tab);
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
  goods_report: "Товарный отчёт",
  web: "Интернет",
  ai: "AI",
  none: "—",
};

const LOCAL_DATA_SOURCES = new Set([
  "catalog",
  "registry",
  "price_list",
  "goods_report",
]);

function isLocalDataSource(item) {
  return LOCAL_DATA_SOURCES.has(item.source) && !item.internet_priced;
}

function shouldShowInternetLinks(item) {
  return Boolean(item.internet_priced || item.source === "web");
}

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
  return [tier, -score, priceSort, productPage ? 0 : 1, hasPrice ? 0 : 1];
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

function collectWebProductEntries(item) {
  return collectWebEntries(item).filter((q) => !isMarketEstimateQuote(q));
}

let kpWebManualPrices = {};

function parseMoneyInput(value) {
  const cleaned = String(value ?? "")
    .replace(/\s/g, "")
    .replace(",", ".")
    .replace(/[^\d.]/g, "");
  if (!cleaned) return null;
  const num = Number(cleaned);
  return Number.isFinite(num) && num >= 0 ? roundMoney(num) : null;
}

function formatMoneyInputValue(value) {
  if (value == null) return "";
  return Number(value).toLocaleString("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function getWebManualPrice(itemNumber, webIndex) {
  const saved = kpSavedSelections?.find((selection) => selection.number === itemNumber);
  const savedPrice = saved?.web_manual_prices?.[webIndex] ?? saved?.web_manual_prices?.[String(webIndex)];
  if (savedPrice != null) return roundMoney(savedPrice);
  return kpWebManualPrices[itemNumber]?.[webIndex] ?? null;
}

function setWebManualPrice(itemNumber, webIndex, price) {
  if (!kpWebManualPrices[itemNumber]) {
    kpWebManualPrices[itemNumber] = {};
  }
  if (price == null) {
    delete kpWebManualPrices[itemNumber][webIndex];
    return;
  }
  kpWebManualPrices[itemNumber][webIndex] = roundMoney(price);
}

function resolveWebQuoteBasePrice(item, webIndex, quote, selection = null) {
  const fromSelection =
    selection?.web_manual_prices?.[webIndex] ??
    selection?.web_manual_prices?.[String(webIndex)];
  if (fromSelection != null) return roundMoney(fromSelection);
  const manual = getWebManualPrice(item.number, webIndex);
  if (manual != null) return manual;
  return quote.price ?? quote.cost ?? null;
}

function webQuoteKpUnitPriceFromBase(base) {
  if (base == null) return null;
  return roundMoney(base * (1 - WEB_PRICE_DISCOUNT_PERCENT / 100));
}

function collectWebManualPricesForSelection(item, webIndices) {
  const entries = collectWebProductEntries(item);
  const manual = {};
  for (const index of webIndices || []) {
    const quote = entries[index];
    if (!quote) continue;
    const hasApiPrice = quote.price != null || quote.cost != null;
    const resolved = resolveWebQuoteBasePrice(item, index, quote);
    if (!hasApiPrice && resolved != null) {
      manual[index] = resolved;
    }
  }
  return Object.keys(manual).length ? manual : undefined;
}

function webQuoteKpUnitPrice(quote, item = null, webIndex = null) {
  const base =
    item != null && webIndex != null
      ? resolveWebQuoteBasePrice(item, webIndex, quote)
      : quote.price ?? quote.cost;
  return webQuoteKpUnitPriceFromBase(base);
}

function aggregateWebAddonPricing(item, webIndices, selection = null) {
  const entries = collectWebProductEntries(item);
  if (!entries.length || !webIndices?.length) {
    return {
      unitCost: 0,
      unitBasePrice: 0,
      unitPrice: 0,
      totalCost: 0,
      totalBasePrice: 0,
      totalPrice: 0,
    };
  }
  const selected = webIndices
    .filter((index) => index >= 0 && index < entries.length)
    .map((index) => ({ index, quote: entries[index] }));
  let unitCost = 0;
  let unitBasePrice = 0;
  let unitPrice = 0;
  for (const { index, quote } of selected) {
    const base = resolveWebQuoteBasePrice(item, index, quote, selection);
    if (base == null) continue;
    unitBasePrice += roundMoney(base);
    unitCost += roundMoney(base);
    const kp = webQuoteKpUnitPriceFromBase(base);
    if (kp != null) unitPrice += kp;
  }
  const qty = item.quantity || 1;
  return {
    unitCost: unitCost || null,
    unitBasePrice: unitBasePrice || null,
    unitPrice: unitPrice || null,
    totalCost: unitCost ? roundMoney(unitCost * qty) : 0,
    totalBasePrice: unitBasePrice ? roundMoney(unitBasePrice * qty) : 0,
    totalPrice: unitPrice ? roundMoney(unitPrice * qty) : 0,
  };
}

function mergeItemPricing(base, addon) {
  if (!addon?.totalPrice) return base;
  const merge = (left, right) => {
    if (left == null && right == null) return null;
    return roundMoney((left || 0) + (right || 0));
  };
  return {
    ...base,
    unitCost: merge(base.unitCost, addon.unitCost),
    unitBasePrice: merge(base.unitBasePrice, addon.unitBasePrice),
    unitPrice: merge(base.unitPrice, addon.unitPrice),
    totalCost: roundMoney((base.totalCost || 0) + (addon.totalCost || 0)),
    totalBasePrice: roundMoney((base.totalBasePrice || 0) + (addon.totalBasePrice || 0)),
    totalPrice: roundMoney((base.totalPrice || 0) + (addon.totalPrice || 0)),
  };
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
  if (isLocalDataSource(item)) return null;

  const parsed = parseSourceDetailText(item.source_detail || "");
  if (parsed.url) return parsed.url;
  if (!shouldShowInternetLinks(item)) return null;
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
  const marketLinks = collectMarketplaceSearchLinks(item);
  return marketLinks[0]?.url || null;
}

function renderSourceDetailLine(item) {
  if (!item.source_detail) return null;
  const parsed = parseSourceDetailText(item.source_detail);
  const label = parsed.label || item.source_detail;

  if (isLocalDataSource(item)) {
    return `<strong>Детали:</strong> ${escapeHtml(label)}`;
  }

  const url = parsed.url || resolveItemSourceUrl(item);
  const productUrl = url && !isSearchListingUrl(url) ? url : null;
  if (productUrl) {
    return `<strong>Детали:</strong> <a href="${escapeHtml(productUrl)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;
  }
  const marketLinks = renderMarketplaceSearchLinks(item);
  if (marketLinks && (isMarketEstimateLabel(label) || item.internet_priced)) {
    const prefix = isMarketEstimateLabel(label)
      ? "Интернет"
      : label.replace(/\s*\([^)]*\)\s*$/, "").trim() || "Интернет";
    return `<strong>Детали:</strong> ${escapeHtml(prefix)} · ${marketLinks}`;
  }
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
      ? `<strong>Выбрано:</strong> ${escapeHtml(item.matched_name)}`
      : null,
    !item.internet_priced && item.source
      ? `<strong>Источник:</strong> ${escapeHtml(SOURCE_LABELS[item.source] || item.source)}`
      : null,
    !item.source_detail && item.internet_url
      ? `<strong>Ссылка:</strong> <a href="${escapeHtml(item.internet_url)}" target="_blank" rel="noopener">${escapeHtml(item.internet_url)}</a>`
      : null,
    renderSourceDetailLine(item),
    item.unit_base_price != null && !shouldDeferInternetRowPrice(item)
      ? `<strong>Цена баз.:</strong> ${fmtMoney(item.unit_base_price)}`
      : null,
    item.unit_price != null && !shouldDeferInternetRowPrice(item)
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

function updateWebRowKpPrice(itemNumber, webIndex) {
  const item = kpProcessData?.items?.find((row) => row.number === itemNumber);
  const quote = item ? collectWebProductEntries(item)[webIndex] : null;
  const kpEl = document.querySelector(
    `.kp-web-kp-price[data-item="${itemNumber}"][data-web-index="${webIndex}"]`,
  );
  if (!kpEl || !quote) return;
  const base = resolveWebQuoteBasePrice(item, webIndex, quote);
  kpEl.textContent = fmtMoney(webQuoteKpUnitPriceFromBase(base));
}

function renderWebComparisonRows(item) {
  const productEntries = collectWebProductEntries(item);
  if (!productEntries.length) return "";
  const initialAddon = aggregateWebAddonPricing(item, getDefaultWebIndices(item));
  const rows = productEntries
    .map((q, webIndex) => {
      const checked = isWebQuoteChecked(item.number, webIndex);
      const hasApiPrice = q.price != null || q.cost != null;
      const webPrice = resolveWebQuoteBasePrice(item, webIndex, q);
      const kpPrice = webQuoteKpUnitPriceFromBase(webPrice);
      const manualValue = getWebManualPrice(item.number, webIndex);
      const priceCell = hasApiPrice
        ? fmtMoney(webPrice)
        : `<input
            type="text"
            class="kp-web-price-input"
            data-item="${item.number}"
            data-web-index="${webIndex}"
            inputmode="decimal"
            placeholder="0,00"
            value="${manualValue != null ? escapeHtml(formatMoneyInputValue(manualValue)) : ""}"
          >`;
      return `
      <tr class="kp-web-row compare-row--competitor${checked ? "" : " kp-web-row--excluded"}" data-item="${item.number}" data-web-index="${webIndex}">
        <td class="kp-web-select-cell">
          <input
            type="checkbox"
            class="kp-web-include"
            data-item="${item.number}"
            data-web-index="${webIndex}"
            ${checked ? "checked" : ""}
          >
        </td>
        <td>${escapeHtml(q.label || "Интернет")}</td>
        <td>${escapeHtml(q.matched_name || "—")}</td>
        <td class="kp-web-price-cell">${priceCell}</td>
        <td>
          <span
            class="kp-web-kp-price"
            data-item="${item.number}"
            data-web-index="${webIndex}"
          >${fmtMoney(kpPrice)}</span>
        </td>
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
      <p class="muted compare-block__kit-note">Отметьте позиции из интернета, чтобы добавить их стоимость в расчёт КП. Если цена не найдена — введите её вручную.</p>
      <table class="compare-table compare-table--web">
        <thead>
          <tr>
            <th class="kp-web-select-cell"></th>
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
        <tfoot>
          <tr class="compare-row--web-total">
            <td colspan="4"><strong>Сумма выбранных:</strong></td>
            <td colspan="5"><strong class="kp-web-total" data-item="${item.number}">${fmtMoney(initialAddon.totalPrice)}</strong></td>
          </tr>
        </tfoot>
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
    .map((k, kitIndex) => {
      const checked = isKitComponentChecked(item.number, kitIndex);
      const catalogLabel = k.found_in_catalog
        ? `<br><small class="muted">каталог: ${escapeHtml(k.catalog_matched_name || k.name)}</small>`
        : "";
      const supplierCell = k.found_in_catalog ? escapeHtml(k.supplier || "—") : "—";
      const dateCell = k.found_in_catalog ? escapeHtml(k.purchase_date || "—") : "—";
      const supplierRow =
        k.found_in_catalog && k.supplier
          ? `
      <tr class="compare-row--supplier kp-kit-row${checked ? "" : " kp-kit-row--excluded"}" data-item="${item.number}" data-kit-index="${kitIndex}">
        <td class="kp-kit-select-cell"></td>
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
      <tr class="kp-kit-row${checked ? "" : " kp-kit-row--excluded"}" data-item="${item.number}" data-kit-index="${kitIndex}">
        <td class="kp-kit-select-cell">
          <input
            type="checkbox"
            class="kp-kit-include"
            data-item="${item.number}"
            data-kit-index="${kitIndex}"
            ${checked ? "checked" : ""}
          >
        </td>
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
      ${renderVariantChoices(item)}
      ${renderMarketEstimateInfo(item)}
      ${primaryBlock}
      ${meta.length ? `<p class="compare-block__meta">${meta.join(" · ")}</p>` : ""}
      ${renderWebComparisonRows(item)}
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
      <h4 class="compare-block__subtitle">Состав комплекта (по ТЗ)</h4>
      <p class="muted compare-block__kit-note">Снимите галочку, чтобы исключить составляющую из расчёта цены комплекта.</p>
      <table class="compare-table compare-table--kit">
        <thead>
          <tr>
            <th class="kp-kit-select-cell">
              <input
                type="checkbox"
                class="kp-kit-select-all"
                data-item="${item.number}"
                checked
                title="Выбрать все составляющие"
              >
            </th>
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
        <tfoot>
          <tr class="compare-row--kit-total">
            <td colspan="4"><strong>Сумма выбранных:</strong></td>
            <td colspan="5"><strong class="kp-kit-total" data-item="${item.number}">${fmtMoney(kitSelectedBaseTotal(item))}</strong></td>
          </tr>
        </tfoot>
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
  kpProcessData = data;
  kpFormed = Boolean(data.kp_formed);
  kpExportSummary = kpFormed ? data.summary : null;
  if (!kpFormed) {
    kpSavedSelections = null;
    kpSelectionSaved = false;
  }
  updateAssistantMode(data);
  const parsedOnly = !data.search_completed;
  const summaryCard = $("#processSummaryCard");

  if (parsedOnly || !data.summary) {
    summaryCard?.classList.add("hidden");
  } else {
    renderProcessSummary(data);
  }

  renderSavedSelectionList();

  $("#resultsCard").classList.remove("hidden");
  const tbody = $("#resultsTable tbody");
  tbody.innerHTML = data.items
    .map(
      (item) => {
        const initialPricing = computeItemPricing(item, { variant: "primary" });
        const hasDetails = hasItemDetails(item);
        const detailId = `tz-detail-${item.number}`;
        const showInternetLabels = initialPricing.internetPriced;
        return `
      <tr class="tz-row${hasDetails ? " tz-row--expandable" : ""}" data-item-number="${item.number}" ${hasDetails ? `data-detail="${detailId}"` : ""}>
        <td class="kp-select-cell">
          <input type="checkbox" class="kp-item-include" data-item="${item.number}" checked>
        </td>
        <td>${item.number}</td>
        <td>${escapeHtml(item.name)}${hasDetails ? ' <span class="tz-row__hint">▼</span>' : ""}</td>
        <td>${escapeHtml(item.matched_name || "—")}${
          item.source && item.source !== "none"
            ? `<br><small class="muted">${escapeHtml(SOURCE_LABELS[item.source] || item.source)}</small>`
            : ""
        }${
          showInternetLabels
            ? '<br><small class="muted">интернет −5%</small>'
            : `<br><small class="muted">${Math.round(item.match_score)}%</small>`
        }${
          item.internet_url
            ? `<br><a class="muted" href="${escapeHtml(item.internet_url)}" target="_blank" rel="noopener">ссылка</a>`
            : ""
        }</td>
        <td>${statusBadge(item.status, item.notes)}</td>
        <td>${fmtQty(item.quantity, item.unit)}</td>
        <td class="tz-row__price-base">${fmtMoney(initialPricing.unitBasePrice)}${showInternetLabels ? '<br><small class="muted">интернет</small>' : ""}</td>
        <td class="tz-row__price-kp">${fmtMoney(initialPricing.unitPrice)}${showInternetLabels ? '<br><small class="muted">−5%</small>' : ""}</td>
        <td class="tz-row__line-total">${fmtMoney(initialPricing.totalPrice)}</td>
      </tr>
      ${
        hasDetails
          ? `<tr class="tz-detail hidden" id="${detailId}"><td colspan="9">${renderComparisonTable(item)}</td></tr>`
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

  data.items
    .filter(
      (item) => item.kit_components?.length || collectWebProductEntries(item).length,
    )
    .forEach((item) => updateTzRowPricing(item.number));

  const selectAll = $("#selectAllKpItems");
  if (selectAll) {
    selectAll.checked = true;
    selectAll.indeterminate = false;
  }

  updateKpExportButtons();

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
    kpLastUploadedFileName = file.name;
    $("#fileName").textContent = file.name;
    fileInput.value = "";
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
    kpLastUploadedFileName = name;
    $("#fileName").textContent = name;
    setUploadButtonsEnabled(isAllowedTzFile(name));
  }

  if (kpLastUploadedFileName) {
    $("#fileName").textContent = kpLastUploadedFileName;
  }

  btnTask1?.addEventListener("click", () => processUpload("task1"));
  btnTask12?.addEventListener("click", () => processUpload("task1_task2"));
  $("#btnFormKp")?.addEventListener("click", () => formKpDocument());
  $("#btnSaveKpSelection")?.addEventListener("click", () => saveKpSelection());
  bindKpSelectionHandlers();
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
            <strong>${escapeHtml(item.display_name || item.name)}</strong>
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

  return renderRegistryPhotos(registry);
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
  const items = competitors?.items?.length
    ? competitors.items
    : competitors?.found
      ? [competitors]
      : [];
  if (!items.length) {
    return `
      <div class="source-block source-block--missing">
        <h4>Сайты конкурентов</h4>
        <p>На сайтах конкурентов не найдено</p>
      </div>`;
  }

  const rows = items.map((item) => renderCompetitorResultItem(item, { showPriceKp: true })).join("");

  return `
    <div class="source-block">
      <h4>Сайты конкурентов <span class="muted match-count">${items.length}</span></h4>
      <div class="competitor-result-list">${rows}</div>
    </div>`;
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
      item.supplier
        ? `Поставщик: ${escapeHtml(String(item.supplier)).replace(/\n/g, "<br>")}`
        : null,
      item.actual_markup_pct ? `Фактическая наценка: ${escapeHtml(String(item.actual_markup_pct))}` : null,
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
  return (
    id === "catalog"
    || id === "registry"
    || id === "goods_report"
    || id === "procurement"
    || id === "stock_balance"
  );
}

function fileAcceptForSource(id) {
  return isStaticSource(id) ? ".xlsx" : ".xls,.xlsx";
}

function sourceLabel(id) {
  if (id === "catalog") return "каталог";
  if (id === "registry") return "реестр остатков";
  if (id === "stock_balance") return "остатки на дату";
  if (id === "goods_report") return "товарный отчёт";
  if (id === "procurement") return "отчёт по закупкам";
  return id;
}

function collectDataSources(data) {
  return {
    catalogs: data.catalogs?.length ? data.catalogs : data.catalog ? [data.catalog] : [],
    prices: data.prices?.length ? data.prices : data.items || [],
    stock: data.stock?.length
      ? data.stock
      : [data.stock_balance, data.registry].filter(Boolean),
    reports: data.reports?.length
      ? data.reports
      : [data.goods_report, data.procurement].filter(Boolean),
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
      renderDataSourceSection("Товарные отчёты", groups.reports),
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
    const data = await api("/api/sources/stock_balance/upload", { method: "POST", body: form });
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

function renderCompetitorAnalysis(analysis, catalog) {
  const box = $("#competitorAnalysis");
  if (!analysis) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  const productsCount =
    catalog?.products ??
    catalog?.store_products ??
    (analysis.domain && cachedStatus?.competitor_products_by_domain?.[analysis.domain]);
  const catalogLine = productsCount != null
    ? `<p><strong>Товаров в каталоге:</strong> ${escapeHtml(String(productsCount))}</p>`
    : "";
  box.innerHTML = `
    <p><strong>Домен:</strong> ${escapeHtml(analysis.domain || "—")}</p>
    <p><strong>Заголовок:</strong> ${escapeHtml(analysis.title || "—")}</p>
    <p><strong>Поиск:</strong> ${escapeHtml(analysis.search_url || "—")}</p>
    <p><strong>Статус:</strong> ${escapeHtml(analysis.status || "—")}</p>
    ${catalogLine}
    <p class="muted">${escapeHtml(analysis.notes || "")}</p>
  `;
}

let competitorSiteIndexed = false;
let competitorIndexedDomain = null;
let competitorSiteBuiltin = false;
let competitorIndexPollToken = 0;
let competitorIndexLogSince = 0;

function updateCompetitorIndexButton() {
  const indexBtn = $("#btnIndexCompetitor");
  if (indexBtn) indexBtn.disabled = Boolean(indexBtn.dataset.indexRunning === "1");
}

function resetCompetitorIndexState() {
  competitorSiteIndexed = false;
  competitorIndexedDomain = null;
  competitorSiteBuiltin = false;
  competitorIndexLogSince = 0;
  updateCompetitorIndexButton();
}

function showCompetitorIndexPanel(initialStatus = "Изучаю структуру сайта…") {
  const panel = $("#competitorIndexPanel");
  const status = $("#competitorIndexStatus");
  const log = $("#competitorIndexLog");
  if (!panel || !status || !log) return;
  panel.classList.remove("hidden");
  status.textContent = initialStatus;
  status.className = "competitor-index-status";
  log.innerHTML = "";
}

function hideCompetitorIndexPanel() {
  $("#competitorIndexPanel")?.classList.add("hidden");
}

function setCompetitorIndexStatus(text, state = "running") {
  const status = $("#competitorIndexStatus");
  if (!status) return;
  status.textContent = text;
  status.className = "competitor-index-status";
  if (state === "done") status.classList.add("competitor-index-status--done");
  if (state === "error") status.classList.add("competitor-index-status--error");
}

function formatCompetitorIndexLogTime(ts) {
  if (!ts) return "";
  const date = new Date(ts * 1000);
  return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function appendCompetitorIndexLogLines(lines) {
  const log = $("#competitorIndexLog");
  if (!log || !lines?.length) return;
  const html = lines
    .map((line) => {
      const level = line.level || "info";
      const time = formatCompetitorIndexLogTime(line.ts);
      return `<span class="competitor-index-log__line competitor-index-log__line--${escapeHtml(level)}">[${escapeHtml(time)}] ${escapeHtml(line.message || "")}</span>`;
    })
    .join("\n");
  log.insertAdjacentHTML("beforeend", html + "\n");
  log.scrollTop = log.scrollHeight;
  const last = lines[lines.length - 1];
  if (last?.id) competitorIndexLogSince = last.id;
}

async function pollCompetitorIndexProgress(domain, pollToken) {
  while (pollToken === competitorIndexPollToken) {
    const [logsData, statusData] = await Promise.all([
      api(`/api/competitors/index/logs?domain=${encodeURIComponent(domain)}&since=${competitorIndexLogSince}`),
      api(`/api/competitors/index/status?url=${encodeURIComponent($("#competitorUrl").value.trim())}`),
    ]);

    if (pollToken !== competitorIndexPollToken) return null;

    appendCompetitorIndexLogLines(logsData.logs || []);

    if (statusData.phase_label) {
      setCompetitorIndexStatus(statusData.phase_label, statusData.running ? "running" : "running");
    }

    if (statusData.catalog_products) {
      applyCompetitorCatalogStats(statusData.catalog_products);
    }

    if (statusData.index_completed) {
      competitorSiteIndexed = true;
      competitorIndexedDomain = domain;
      competitorSiteBuiltin = Boolean(statusData.is_builtin);
      setCompetitorIndexStatus(
        competitorSiteBuiltin ? "Каталог встроенного сайта обновлён" : "Индексация завершена",
        "done",
      );
      if (statusData.analysis || statusData.catalog) {
        renderCompetitorAnalysis(statusData.analysis || { domain }, statusData.catalog);
      }
      updateCompetitorIndexButton();
      const count = statusData.catalog?.products ?? statusData.catalog?.store_products ?? 0;
      showToast(
        competitorSiteBuiltin
          ? `Каталог обновлён: ${count} товаров`
          : `Индексация завершена: ${count} товаров`,
      );
      loadCompetitors();
      return statusData;
    }

    if (statusData.error && !statusData.running) {
      setCompetitorIndexStatus(`Ошибка: ${statusData.error}`, "error");
      throw new Error(String(statusData.error));
    }

    if (!statusData.running && !statusData.index_completed) {
      setCompetitorIndexStatus("Индексация остановлена", "error");
      return statusData;
    }

    await sleep(900);
  }
  return null;
}

function competitorIndexPayload() {
  return {
    url: $("#competitorUrl").value.trim(),
  };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function indexCompetitorSite() {
  const payload = competitorIndexPayload();
  if (!payload.url) {
    showToast("Введите ссылку на сайт", true);
    return;
  }
  resetCompetitorIndexState();
  competitorIndexPollToken += 1;
  const pollToken = competitorIndexPollToken;
  const indexBtn = $("#btnIndexCompetitor");
  if (indexBtn) indexBtn.dataset.indexRunning = "1";
  updateCompetitorIndexButton();
  showCompetitorIndexPanel("Изучаю структуру сайта…");
  appendCompetitorIndexLogLines([
    { id: 0, ts: Date.now() / 1000, level: "info", message: "Запрос отправлен…" },
  ]);

  try {
    const data = await api("/api/competitors/index", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!data.started) {
      throw new Error(data.message || "Не удалось запустить индексацию");
    }

    const domain = data.domain;
    if (!domain) {
      throw new Error("Не удалось определить домен сайта");
    }

    competitorSiteBuiltin = Boolean(data.is_builtin);
    competitorIndexedDomain = domain;
    setCompetitorIndexStatus(
      data.is_builtin ? "Обновление каталога встроенного сайта…" : "Изучаю структуру сайта…",
    );
    await pollCompetitorIndexProgress(domain, pollToken);
  } catch (e) {
    setCompetitorIndexStatus(e.message || "Ошибка индексации", "error");
    appendCompetitorIndexLogLines([
      {
        id: competitorIndexLogSince + 1,
        ts: Date.now() / 1000,
        level: "error",
        message: e.message || "Ошибка индексации",
      },
    ]);
    showToast(e.message, true);
  } finally {
    if (indexBtn) delete indexBtn.dataset.indexRunning;
    updateCompetitorIndexButton();
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

function renderCompetitorList(rows) {
  if (!rows.length) {
    return `<p class="muted">Нет сайтов</p>`;
  }
  return `
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
    </div>`;
}

async function refreshCompetitorIndexState() {
  const url = $("#competitorUrl")?.value.trim();
  if (!url) {
    resetCompetitorIndexState();
    return;
  }
  try {
    const data = await api(`/api/competitors/index/status?url=${encodeURIComponent(url)}`);
    competitorSiteIndexed = Boolean(data.index_completed);
    competitorIndexedDomain = data.domain || null;
    competitorSiteBuiltin = Boolean(data.is_builtin);
    updateCompetitorIndexButton();
    if (data.running) {
      showCompetitorIndexPanel(data.phase_label || "Индексация…");
      competitorIndexLogSince = 0;
      try {
        const logsData = await api(
          `/api/competitors/index/logs?domain=${encodeURIComponent(data.domain)}&since=0`
        );
        appendCompetitorIndexLogLines(logsData.logs || []);
      } catch (_) {
        /* ignore log restore errors */
      }
      competitorIndexPollToken += 1;
      const pollToken = competitorIndexPollToken;
      const indexBtn = $("#btnIndexCompetitor");
      if (indexBtn) indexBtn.dataset.indexRunning = "1";
      updateCompetitorIndexButton();
      pollCompetitorIndexProgress(data.domain, pollToken).finally(() => {
        if (indexBtn) delete indexBtn.dataset.indexRunning;
        updateCompetitorIndexButton();
      });
    } else if (data.index_completed) {
      setCompetitorIndexStatus(
        data.is_builtin ? "Каталог встроенного сайта обновлён" : "Индексация завершена",
        "done",
      );
      $("#competitorIndexPanel")?.classList.remove("hidden");
      if (data.analysis || data.catalog) {
        renderCompetitorAnalysis(data.analysis || { domain: data.domain }, data.catalog);
      }
    }
  } catch (_) {
    resetCompetitorIndexState();
  }
}

async function loadCompetitors() {
  try {
    const data = await api("/api/competitors");
    if (data.catalog_products) {
      applyCompetitorCatalogStats(data.catalog_products);
    }
    const container = $("#competitorsList");
    container.innerHTML = renderCompetitorList(data.builtin || []);

    container.querySelectorAll("[data-remove-competitor]").forEach((btn) => {
      btn.addEventListener("click", () => removeCompetitorSite(btn.dataset.removeCompetitor));
    });
    await refreshCompetitorIndexState();
  } catch (e) {
    showToast(e.message, true);
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

function renderCompetitorResultItem(item, options = {}) {
  const showPriceKp = Boolean(options.showPriceKp);
  const matchedName = item.matched_name || item.name || "—";
  const title = formatCompetitorSiteLabel(item.label) || "—";

  const photoHtml = item.image_url
    ? `<div class="competitor-result-item__photo">${renderPhotoButton(
        item.image_url,
        matchedName,
        "competitor-result-item__image",
      )}</div>`
    : "";

  const priceText =
    item.has_price === false
      ? "—"
      : showPriceKp && item.price
        ? item.price
        : fmtCompetitorPrice(item);

  const priceKpHtml =
    showPriceKp && item.price_kp
      ? `<div class="muted">Цена КП (−5%): ${escapeHtml(item.price_kp)}</div>`
      : "";

  const missingPriceHtml =
    showPriceKp && item.has_price === false
      ? `<div class="muted">Цена не указана на сайте</div>`
      : "";

  const articulHtml = item.articul
    ? `<div class="muted">Артикул: ${escapeHtml(item.articul)}</div>`
    : "";

  const matchHtml = item.match_score
    ? `<div class="muted">${Math.round(item.match_score)}% совпадение</div>`
    : "";

  const linkHtml = item.url
    ? `<div class="chat-link-wrap">${renderChatLink(item.url, "Открыть на сайте")}</div>`
    : !showPriceKp && item.notes
      ? `<div class="muted">${escapeHtml(item.notes)}</div>`
      : "";

  const notesHtml =
    showPriceKp && item.notes
      ? `<div class="muted">${escapeHtml(item.notes)}</div>`
      : "";

  return `
    <div class="competitor-result-item">
      ${photoHtml}
      <div class="competitor-result-item__body">
        <div class="competitor-result-item__head">
          <strong class="competitor-result-item__title">${escapeHtml(title)}</strong>
          <span class="competitor-result-item__price">${escapeHtml(priceText)}</span>
        </div>
        <div class="competitor-result-item__name">${escapeHtml(matchedName)}</div>
        ${articulHtml}
        ${matchHtml}
        ${missingPriceHtml}
        ${priceKpHtml}
        ${linkHtml}
        ${notesHtml}
      </div>
    </div>`;
}

function renderCompetitorSearchResults(data) {
  if (!data.items?.length) {
    return `<p>По запросу «${escapeHtml(data.query)}» на сайтах конкурентов ничего не найдено.</p>`;
  }

  const rows = data.items
    .map((item) => renderCompetitorResultItem(item))
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
      body: JSON.stringify({ query, limit: 30 }),
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

document.addEventListener("DOMContentLoaded", async () => {
  const authed = await ensureAuth();
  if (!authed) return;

  initAuth();
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
  $("#btnIndexCompetitor").addEventListener("click", indexCompetitorSite);
  $("#competitorUrl")?.addEventListener("input", resetCompetitorIndexState);
  updateCompetitorIndexButton();
  initCompetitorChat();
  loadInitialStatus();
  setInterval(() => {
    if (document.visibilityState === "visible") loadInitialStatus();
  }, 30_000);
});

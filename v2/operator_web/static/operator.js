const pageId = document.body?.dataset.operatorPage || "console";
const feedbackEl = document.getElementById("action-feedback");
const confirmModalEl = document.getElementById("confirm-modal");
const confirmModalTitleEl = document.getElementById("confirm-modal-title");
const confirmModalMessageEl = document.getElementById("confirm-modal-message");
const confirmModalOkEl = document.getElementById("confirm-ok");
const confirmModalCancelEl = document.getElementById("confirm-cancel");
const confirmModalBackdropEl = document.getElementById("confirm-modal-backdrop");
const operatorSwUrl = document.body?.dataset.operatorSwUrl || "/operator/sw.js";
const operatorScope = document.body?.dataset.operatorScope || "/operator/";

const EVENT_CATEGORY_LABELS = {
  status: "상태",
  decision: "판단",
  blocked: "차단",
  risk: "리스크",
  position: "포지션",
  report: "리포트",
  action: "액션",
};

let eventMemory = [];
let logMemory = [];
let logsState = {
  offset: 0,
  limit: 500,
  total: 0,
  hasPrev: false,
  hasNext: false,
};
let activeConfirmResolver = null;
let lastConsolePayload = null;
let pushRegistration = null;
let lastPushDiagnostic = null;
let lastClientLogKey = null;
const INPUT_DIRTY_WINDOW_MS = 8000;

function fmtNumber(value, digits = 4) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : "-";
}

function fmtMaybe(value) {
  return value === null || value === undefined || value === "" ? "-" : String(value);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) {
    el.textContent = fmtMaybe(value);
  }
}

function setInputValue(id, value) {
  const el = document.getElementById(id);
  if (el) {
    const dirtyAt = Number(el.dataset.userDirtyAt || "0");
    const isEditing = document.activeElement === el || (Date.now() - dirtyAt) < INPUT_DIRTY_WINDOW_MS;
    if (isEditing) {
      return;
    }
    el.value = value ?? "";
  }
}

function setSelectValue(id, value) {
  const el = document.getElementById(id);
  if (el) {
    const dirtyAt = Number(el.dataset.userDirtyAt || "0");
    const isEditing = document.activeElement === el || (Date.now() - dirtyAt) < INPUT_DIRTY_WINDOW_MS;
    if (isEditing) {
      return;
    }
    const normalized = String(value ?? "");
    const hasOption = Array.from(el.options || []).some((option) => option.value === normalized);
    if (normalized && !hasOption) {
      const option = document.createElement("option");
      option.value = normalized;
      option.textContent = `${normalized}초`;
      el.prepend(option);
    }
    el.value = normalized;
  }
}

function isPushSupported() {
  return (
    window.isSecureContext &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

function isIosDevice() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent || "");
}

function isStandaloneMode() {
  return Boolean(window.matchMedia?.("(display-mode: standalone)")?.matches || window.navigator.standalone);
}

function getPushDeviceId() {
  try {
    const key = "auto-trader-push-device-id";
    const existing = window.localStorage.getItem(key);
    if (existing) {
      return existing;
    }
    const created =
      window.crypto?.randomUUID?.() || `device-${Math.random().toString(36).slice(2, 10)}`;
    window.localStorage.setItem(key, created);
    return created;
  } catch (_error) {
    return `device-${Math.random().toString(36).slice(2, 10)}`;
  }
}

function buildDefaultDeviceLabel() {
  if (isIosDevice()) {
    return isStandaloneMode() ? "iPhone 운영앱" : "iPhone Safari";
  }
  return isStandaloneMode() ? "운영앱" : "브라우저 기기";
}

function pushDiagnosticMessage(reason) {
  return {
    server_unavailable: "서버 Web Push 준비가 아직 완료되지 않았습니다. 잠시 후 새로고침해 주세요.",
    secure_context_missing: "HTTPS 보안 컨텍스트가 아닙니다. 인증서 신뢰를 켠 뒤 https 주소의 홈 화면 앱으로 다시 열어 주세요.",
    notification_api_missing: "이 브라우저는 Notification API를 지원하지 않습니다.",
    service_worker_missing: "이 브라우저는 Service Worker를 지원하지 않습니다.",
    push_manager_missing: "이 iPhone/iOS 조합에서는 Web Push를 지원하지 않을 수 있습니다. iOS 16.4 이상인지 확인해 주세요.",
    ios_home_screen_required: "iPhone에서는 Safari 탭이 아니라 '홈 화면에 추가'한 앱에서만 푸시 연결이 됩니다.",
    notification_permission_denied: "알림 권한이 차단되어 있습니다. 설정에서 Safari/웹앱 알림 권한을 다시 허용해 주세요.",
    sw_registration_failed: "서비스 워커 등록에 실패했습니다. 홈 화면 앱을 닫았다가 다시 열고 재시도해 주세요.",
    push_subscription_failed: "브라우저 푸시 구독 생성에 실패했습니다. 홈 화면 앱에서 다시 시도해 주세요.",
  }[reason] || reason || "원인을 확인하지 못했습니다.";
}

async function postClientLog({ title, mainText, subText = null, category = "action", context = {} }) {
  const payload = {
    category,
    title,
    main_text: mainText,
    sub_text: subText,
    context: {
      ...context,
      event_time: new Date().toISOString(),
      page: pageId,
      href: window.location.href,
      userAgent: navigator.userAgent,
    },
  };
  const dedupeKey = JSON.stringify([payload.title, payload.main_text, payload.sub_text, payload.context?.error || ""]);
  if (dedupeKey === lastClientLogKey) {
    return;
  }
  lastClientLogKey = dedupeKey;
  try {
    await fetch("/operator/api/client-log", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
      keepalive: true,
    });
  } catch (_error) {
    // Best effort only.
  }
}

function computePushDiagnostic(push) {
  const serverReady = Boolean(push?.available);
  const secureContext = Boolean(window.isSecureContext);
  const notificationSupported = typeof window.Notification !== "undefined";
  const serviceWorkerSupported = "serviceWorker" in navigator;
  const pushManagerSupported = "PushManager" in window;
  const standalone = isStandaloneMode();
  const permission = notificationSupported ? Notification.permission : "unsupported";

  let reason = null;
  if (!serverReady) {
    reason = "server_unavailable";
  } else if (!secureContext) {
    reason = "secure_context_missing";
  } else if (!notificationSupported) {
    reason = "notification_api_missing";
  } else if (!serviceWorkerSupported) {
    reason = "service_worker_missing";
  } else if (!pushManagerSupported) {
    reason = "push_manager_missing";
  } else if (isIosDevice() && !standalone) {
    reason = "ios_home_screen_required";
  } else if (permission === "denied") {
    reason = "notification_permission_denied";
  }

  return {
    canSubscribe: reason === null,
    reason,
    secureContext,
    notificationSupported,
    serviceWorkerSupported,
    pushManagerSupported,
    standalone,
    permission,
    message: pushDiagnosticMessage(reason),
  };
}

function pushPermissionLabel(permission) {
  if (!isPushSupported()) {
    return "이 브라우저에서는 미지원";
  }
  return {
    granted: "허용됨",
    denied: "차단됨",
    default: "미결정",
  }[permission || "default"] || String(permission || "-");
}

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replaceAll("-", "+").replaceAll("_", "/");
  const rawData = window.atob(base64);
  return Uint8Array.from(rawData, (char) => char.charCodeAt(0));
}

async function ensurePushRegistration() {
  if (!isPushSupported()) {
    return null;
  }
  if (pushRegistration) {
    return pushRegistration;
  }
  await postClientLog({
    title: "push_sw_register_start",
    mainText: "service_worker_register_start",
    subText: operatorSwUrl,
    context: { scope: operatorScope },
  });
  const registration = await navigator.serviceWorker.register(operatorSwUrl, { scope: operatorScope });
  await postClientLog({
    title: "push_sw_register_ok",
    mainText: "service_worker_register_ok",
    subText: registration.scope,
    context: {
      active: Boolean(registration.active),
      waiting: Boolean(registration.waiting),
      installing: Boolean(registration.installing),
    },
  });
  if (registration.active) {
    pushRegistration = registration;
    await postClientLog({
      title: "push_sw_active_shortcut",
      mainText: "service_worker_active_shortcut",
      subText: registration.scope,
      context: {
        controller: Boolean(navigator.serviceWorker.controller),
      },
    });
    return pushRegistration;
  }
  const readyRegistration = await Promise.race([
    navigator.serviceWorker.ready,
    new Promise((_, reject) =>
      window.setTimeout(() => reject(new Error("sw_ready_timeout")), 8000)
    ),
  ]);
  pushRegistration = readyRegistration;
  await postClientLog({
    title: "push_sw_ready_ok",
    mainText: "service_worker_ready",
    subText: readyRegistration?.scope || operatorScope,
    context: {
      controller: Boolean(navigator.serviceWorker.controller),
    },
  });
  return pushRegistration || registration;
}

async function currentPushSubscription() {
  const registration = await ensurePushRegistration();
  if (!registration) {
    return null;
  }
  return registration.pushManager.getSubscription();
}

function renderPushDevices(items) {
  const wrap = document.getElementById("push-device-list");
  if (!wrap) {
    return;
  }
  wrap.innerHTML = "";
  if (!Array.isArray(items) || items.length === 0) {
    wrap.innerHTML = '<div class="event-empty">아직 연결된 기기가 없습니다. 홈 화면 앱에서 푸시 연결을 눌러 주세요.</div>';
    return;
  }
  for (const item of items) {
    const row = document.createElement("div");
    row.className = `push-device-row ${item?.active ? "is-active" : ""}`.trim();
    row.innerHTML = `
      <div class="push-device-top">
        <div class="push-device-name">${escapeHtml(fmtMaybe(item?.device_label || "이름 없는 기기"))}</div>
        <div class="push-device-meta">
          <span class="push-chip ${item?.active ? "active" : "offline"}">${item?.active ? "연결중" : "비활성"}</span>
          <span class="push-chip">${escapeHtml(fmtMaybe(item?.platform || "platform 미상"))}</span>
          <span class="push-chip">${item?.standalone ? "홈 화면 앱" : "브라우저"}</span>
        </div>
      </div>
      <div class="push-device-meta">
        <span>최근 성공: ${escapeHtml(fmtMaybe(item?.last_success_at || "-"))}</span>
        <span>최근 실패: ${escapeHtml(fmtMaybe(item?.last_failure_at || "-"))}</span>
        <span>엔드포인트: ${escapeHtml(fmtMaybe(item?.endpoint_hint || "-"))}</span>
      </div>
      <div class="event-subline">${escapeHtml(fmtMaybe(item?.last_error || "최근 오류 없음"))}</div>
    `;
    wrap.appendChild(row);
  }
}

async function renderPushState(push) {
  const serverReady = Boolean(push?.available);
  const diagnostic = computePushDiagnostic(push);
  lastPushDiagnostic = diagnostic;

  setText("push-availability", serverReady ? "Web Push 준비됨" : "Web Push 준비 필요");
  setText(
    "push-runtime-provider",
    push?.runtime_provider_enabled
      ? `${String(push?.runtime_provider || "none")} 운영 채널`
      : `${String(push?.runtime_provider || "none")} 운영 채널 아님`
  );
  setText("push-install-state", diagnostic.standalone ? "홈 화면 앱" : "브라우저 탭");
  setText("push-server-status", serverReady ? "준비됨" : push?.last_error || "미준비");
  setText(
    "push-provider-state",
    push?.runtime_provider_enabled ? "실운영 알림 사용중" : "테스트/대기 상태"
  );
  setText("push-permission-state", pushPermissionLabel(diagnostic.permission));
  setText("push-subscription-state", diagnostic.message);
  setText("push-subscription-count", push?.subscription_count);
  setText("push-last-error", push?.last_error);
  renderPushDevices(push?.devices || push?.subscriptions || []);

  const subscription = diagnostic.canSubscribe
    ? await currentPushSubscription().catch(() => null)
    : null;
  const hasSubscription = Boolean(subscription);
  setText(
    "push-subscription-state",
    hasSubscription ? "현재 기기 연결됨" : diagnostic.reason ? diagnostic.message : "현재 기기 미연결"
  );

  const labelInput = document.getElementById("push-device-label");
  if (labelInput && !labelInput.value) {
    labelInput.value = buildDefaultDeviceLabel();
  }
  const installHint = document.getElementById("push-install-hint");
  const deviceHint = document.getElementById("push-device-hint");
  if (installHint) {
    if (diagnostic.reason) {
      installHint.textContent = diagnostic.message;
    } else {
      installHint.textContent =
        "푸시 연결을 누르면 현재 기기를 운영용 수신 기기로 등록합니다. 연결 뒤에는 테스트 발송으로 바로 확인할 수 있습니다.";
    }
  }
  if (deviceHint) {
    deviceHint.textContent =
      `현재 기기 ID: ${getPushDeviceId()} / 라벨 추천: ${buildDefaultDeviceLabel()} / secure=${diagnostic.secureContext ? "yes" : "no"} / standalone=${diagnostic.standalone ? "yes" : "no"} / pushAPI=${diagnostic.pushManagerSupported ? "yes" : "no"}`;
  }

  const subscribeBtn = document.getElementById("push-subscribe-btn");
  const unsubscribeBtn = document.getElementById("push-unsubscribe-btn");
  const testBtn = document.getElementById("push-test-btn");
  if (subscribeBtn) {
    subscribeBtn.disabled = !serverReady;
    subscribeBtn.title = diagnostic.reason ? diagnostic.message : "현재 기기를 Web Push 수신 기기로 연결합니다.";
  }
  if (unsubscribeBtn) {
    unsubscribeBtn.disabled = !hasSubscription;
  }
  if (testBtn) {
    testBtn.disabled = !serverReady;
  }
}

function markFieldDirty(event) {
  const el = event?.target;
  if (!el || !("dataset" in el)) {
    return;
  }
  el.dataset.userDirtyAt = String(Date.now());
}

function renderList(id, items) {
  const el = document.getElementById(id);
  if (!el) {
    return;
  }
  el.innerHTML = "";
  if (!Array.isArray(items)) {
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = fmtMaybe(item);
    el.appendChild(li);
  }
}

function renderMapList(id, mapping) {
  const el = document.getElementById(id);
  if (!el) {
    return;
  }
  el.innerHTML = "";
  if (!mapping || typeof mapping !== "object") {
    return;
  }
  for (const [key, value] of Object.entries(mapping)) {
    const li = document.createElement("li");
    li.textContent = `${fmtMaybe(key)}: ${fmtMaybe(value)}`;
    el.appendChild(li);
  }
}

function renderPre(id, payload) {
  const el = document.getElementById(id);
  if (el) {
    const normalized = payload && Object.keys(payload).length > 0 ? payload : "-";
    el.textContent =
      typeof normalized === "string" ? normalized : JSON.stringify(normalized, null, 2);
  }
}

function populateSelect(id, options, currentValue) {
  const el = document.getElementById(id);
  if (!el) {
    return;
  }
  el.innerHTML = "";
  if (!Array.isArray(options)) {
    return;
  }
  for (const optionValue of options) {
    const option = document.createElement("option");
    option.value = String(optionValue);
    option.textContent = String(optionValue);
    if (String(optionValue) === String(currentValue ?? "")) {
      option.selected = true;
    }
    el.appendChild(option);
  }
}

function renderUniverseChips(symbols) {
  const wrap = document.getElementById("universe-symbols-chips");
  if (!wrap) {
    return;
  }
  wrap.innerHTML = "";
  if (!Array.isArray(symbols) || symbols.length === 0) {
    wrap.textContent = "-";
    return;
  }
  for (const symbol of symbols) {
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.innerHTML = `
      <span>${fmtMaybe(symbol)}</span>
      <button class="btn ghost btn-small universe-remove-btn" type="button" data-symbol="${fmtMaybe(symbol)}">해제</button>
    `;
    wrap.appendChild(chip);
  }
  document.querySelectorAll(".universe-remove-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const symbol = button.dataset.symbol || "";
      runConfirmedAction(
        {
          title: "운영 심볼을 해제할까요?",
          message: `${symbol || "-"} 심볼을 현재 운영 유니버스에서 제거합니다.`,
        },
        () => postAction("/operator/actions/universe/remove", { symbol })
      ).catch((error) => setFeedback(String(error), "failed"));
    });
  });
}

function relativeTime(value) {
  if (!value || value === "-") {
    return "-";
  }
  const ts = Date.parse(String(value));
  if (Number.isNaN(ts)) {
    return String(value);
  }
  const diffSec = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (diffSec < 60) {
    return `${diffSec}초 전`;
  }
  if (diffSec < 3600) {
    return `${Math.floor(diffSec / 60)}분 전`;
  }
  if (diffSec < 86400) {
    return `${Math.floor(diffSec / 3600)}시간 전`;
  }
  return `${Math.floor(diffSec / 86400)}일 전`;
}

function formatAbsoluteTime(value) {
  if (!value || value === "-") {
    return "-";
  }
  const ts = Date.parse(String(value));
  if (Number.isNaN(ts)) {
    return String(value);
  }
  const date = new Date(ts);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  const seconds = String(date.getSeconds()).padStart(2, "0");
  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

function getEventCategoryStyle(category) {
  return category === "status" ? "info" : String(category || "info");
}

function getEventCategoryLabel(event) {
  const rawCategory = String(event?.category || "status");
  return fmtMaybe(event?.category_label || EVENT_CATEGORY_LABELS[rawCategory] || rawCategory);
}

function buildEventRowMarkup(event) {
  const categoryStyle = getEventCategoryStyle(event?.category);
  return `
    <div class="event-row ${categoryStyle}">
      <div class="event-row-top">
        <div class="event-row-left">
          <span class="event-badge ${categoryStyle}">${escapeHtml(getEventCategoryLabel(event))}</span>
          <div class="event-copy">
            <div class="event-summaryline">
              <span class="event-title">${escapeHtml(fmtMaybe(event?.title))}</span>
              <span class="event-mainline">${escapeHtml(fmtMaybe(event?.main_text))}</span>
            </div>
            ${event?.sub_text ? `<div class="event-subline">${escapeHtml(fmtMaybe(event.sub_text))}</div>` : ""}
          </div>
        </div>
        <div class="event-time-stack">
          <span class="event-time-absolute">${escapeHtml(formatAbsoluteTime(event?.event_time))}</span>
          <span class="event-time-relative">${escapeHtml(relativeTime(event?.event_time))}</span>
        </div>
      </div>
    </div>
  `;
}

function renderEventCollection({ wrapId, countId, items, emptyMessage, metaId = null }) {
  const wrap = document.getElementById(wrapId);
  const count = document.getElementById(countId);
  const meta = metaId ? document.getElementById(metaId) : null;
  if (!wrap || !count) {
    return;
  }

  wrap.innerHTML = "";
  count.textContent = `${items.length}건`;

  if (meta) {
    meta.textContent = items.length > 0 ? `최신순 / 서버 영속 로그` : "조회된 로그 없음";
  }

  if (items.length === 0) {
    wrap.innerHTML = `<div class="event-empty">${escapeHtml(emptyMessage)}</div>`;
    return;
  }

  wrap.innerHTML = items.map((event) => buildEventRowMarkup(event)).join("");
}

function renderEventFeed() {
  renderEventCollection({
    wrapId: "event-feed",
    countId: "event-feed-count",
    items: eventMemory,
    emptyMessage: "최근 이벤트가 아직 없습니다.",
  });
}

async function loadEventFeed() {
  const resp = await fetch("/operator/api/events?limit=200", {
    headers: { Accept: "application/json" },
  });
  if (!resp.ok) {
    throw new Error(`events_load_failed:${resp.status}`);
  }
  const payload = await resp.json();
  eventMemory = Array.isArray(payload) ? payload : [];
  renderEventFeed();
}

function readLogsFilters() {
  const selectedLimit = Number(document.getElementById("logs-limit-select")?.value || "500");
  logsState.limit = Number.isFinite(selectedLimit) && selectedLimit > 0 ? selectedLimit : 500;
  return {
    limit: logsState.limit,
    category: document.getElementById("logs-category-select")?.value || "",
    query: (document.getElementById("logs-query-input")?.value || "").trim(),
  };
}

function renderLogsFeed() {
  renderEventCollection({
    wrapId: "logs-feed",
    countId: "logs-feed-count",
    metaId: "logs-feed-meta",
    items: logMemory,
    emptyMessage: "조회 조건에 맞는 운영 로그가 없습니다.",
  });
  const pageInfo = document.getElementById("logs-page-info");
  const prevBtn = document.getElementById("logs-prev");
  const nextBtn = document.getElementById("logs-next");
  const start = logsState.total === 0 ? 0 : logsState.offset + 1;
  const end = Math.min(logsState.offset + logMemory.length, logsState.total);
  if (pageInfo) {
    pageInfo.textContent = `${start} - ${end} / ${logsState.total}`;
  }
  if (prevBtn) {
    prevBtn.disabled = !logsState.hasPrev;
  }
  if (nextBtn) {
    nextBtn.disabled = !logsState.hasNext;
  }
}

async function loadLogsFeed() {
  const { limit, category, query } = readLogsFilters();
  const params = new URLSearchParams({
    limit: String(limit || 500),
    offset: String(logsState.offset || 0),
  });
  if (category) {
    params.set("category", category);
  }
  if (query) {
    params.set("query", query);
  }
  const resp = await fetch(`/operator/api/logs?${params.toString()}`, {
    headers: { Accept: "application/json" },
  });
  if (!resp.ok) {
    throw new Error(`logs_load_failed:${resp.status}`);
  }
  const payload = await resp.json();
  logMemory = Array.isArray(payload?.items) ? payload.items : [];
  logsState.limit = Number(payload?.limit || limit || 500);
  logsState.offset = Number(payload?.offset || 0);
  logsState.total = Number(payload?.total || 0);
  logsState.hasPrev = Boolean(payload?.has_prev);
  logsState.hasNext = Boolean(payload?.has_next);
  renderLogsFeed();
}

function setFeedback(message, status = "success") {
  if (!feedbackEl) {
    return;
  }
  feedbackEl.hidden = false;
  feedbackEl.className = `feedback ${status}`;
  feedbackEl.textContent = message;
}

function closeConfirmModal(confirmed) {
  if (!confirmModalEl) {
    return;
  }
  confirmModalEl.hidden = true;
  confirmModalEl.setAttribute("aria-hidden", "true");
  if (activeConfirmResolver) {
    const resolver = activeConfirmResolver;
    activeConfirmResolver = null;
    resolver(Boolean(confirmed));
  }
}

function requestConfirmation({
  title = "이 작업을 실행할까요?",
  message = "요청 내용을 다시 확인한 뒤 진행하세요.",
  confirmLabel = "확인",
} = {}) {
  if (!confirmModalEl || !confirmModalTitleEl || !confirmModalMessageEl || !confirmModalOkEl) {
    return Promise.resolve(window.confirm(message));
  }
  if (activeConfirmResolver) {
    activeConfirmResolver(false);
    activeConfirmResolver = null;
  }
  confirmModalTitleEl.textContent = title;
  confirmModalMessageEl.textContent = message;
  confirmModalOkEl.textContent = confirmLabel;
  confirmModalEl.hidden = false;
  confirmModalEl.setAttribute("aria-hidden", "false");
  window.setTimeout(() => confirmModalOkEl.focus(), 0);
  return new Promise((resolve) => {
    activeConfirmResolver = resolve;
  });
}

async function runConfirmedAction(confirmOptions, runner) {
  const confirmed = await requestConfirmation(confirmOptions);
  if (!confirmed) {
    return;
  }
  await runner();
}

function renderPositions(rows) {
  const wrap = document.getElementById("positions-list");
  const empty = document.getElementById("positions-empty");
  const closeAllBtn = document.getElementById("action-close-all");
  if (!wrap || !empty) {
    return;
  }
  wrap.innerHTML = "";
  if (!Array.isArray(rows) || rows.length === 0) {
    empty.hidden = false;
    if (closeAllBtn) {
      closeAllBtn.disabled = true;
    }
    return;
  }

  if (closeAllBtn) {
    closeAllBtn.disabled = false;
  }
  empty.hidden = true;

  const header = document.createElement("div");
  header.className = "table-row header";
  header.innerHTML =
    "<div>심볼</div><div>방향</div><div>수량</div><div>진입가</div><div>미실현PnL</div><div>동작</div>";
  wrap.appendChild(header);

  for (const row of rows) {
    const item = document.createElement("div");
    item.className = "table-row";
    item.innerHTML = `
      <div>${fmtMaybe(row.symbol)}</div>
      <div>${fmtMaybe(row.position_side)}</div>
      <div>${fmtNumber(row.position_amt, 4)}</div>
      <div>${fmtNumber(row.entry_price, 4)}</div>
      <div>${fmtNumber(row.unrealized_pnl, 4)}</div>
      <div><button class="btn ghost btn-small position-close-btn" type="button" data-symbol="${fmtMaybe(row.symbol)}">종료</button></div>
    `;
    wrap.appendChild(item);
  }

  document.querySelectorAll(".position-close-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const symbol = button.dataset.symbol || "";
      runConfirmedAction(
        {
          title: "개별 포지션을 종료할까요?",
          message: `${symbol || "-"} 포지션 종료 요청을 보냅니다.`,
        },
        () => postAction("/operator/actions/positions/close", { symbol })
      ).catch((error) => setFeedback(String(error), "failed"));
    });
  });
}

async function loadConsole() {
  const resp = await fetch("/operator/api/console", { headers: { Accept: "application/json" } });
  if (!resp.ok) {
    throw new Error(`console_load_failed:${resp.status}`);
  }
  const payload = await resp.json();
  lastConsolePayload = payload;

  await renderPushState(payload.push || {});

  setText("runtime-mode", `${fmtMaybe(payload.runtime?.mode)} / ${fmtMaybe(payload.runtime?.env)}`);
  setText("runtime-profile", payload.runtime?.profile);

  setText("engine-state", payload.engine?.state_label);
  setText("engine-updated-at", payload.engine?.updated_at);
  setText("health-ready", payload.health?.ready_label);
  setText("health-stale", payload.health?.stale ? "예" : "아니오");
  setText(
    "health-blocked",
    payload.health?.blocked_reason_label || (payload.health?.blocked ? "예" : "아니오")
  );
  setText(
    "health-busy",
    payload.health?.busy_reason_label || (payload.health?.busy ? "예" : "아니오")
  );
  renderList("health-stale-items", payload.health?.stale_items || []);

  setText("recovery-required", payload.recovery?.recovery_required ? "예" : "아니오");
  setText("recovery-state-uncertain", payload.recovery?.state_uncertain ? "예" : "아니오");
  setText("recovery-state-uncertain-reason", payload.recovery?.state_uncertain_reason_label);
  setText(
    "recovery-startup-ok",
    payload.recovery?.startup_reconcile_ok === null || payload.recovery?.startup_reconcile_ok === undefined
      ? "-"
      : payload.recovery?.startup_reconcile_ok
        ? "정상"
        : "실패"
  );
  setText("recovery-last-reconcile-at", payload.recovery?.last_reconcile_at);
  setText("recovery-submission-ok", payload.recovery?.submission_recovery_ok ? "정상" : "확인 필요");
  renderPre("recovery-watchdog", payload.recovery?.watchdog || {});

  setText("scheduler-tick-sec", `${fmtNumber(payload.scheduler?.tick_sec, 1)}초`);
  setText("controls-exec-mode", payload.controls?.exec_mode_default);
  setText("scheduler-last-action", payload.scheduler?.last_action_label);
  setText("scheduler-last-reason", payload.scheduler?.last_reason_label);
  setText("scheduler-last-error", payload.scheduler?.last_error);
  setText("scheduler-started-at", payload.scheduler?.tick_started_at);
  setText("scheduler-finished-at", payload.scheduler?.tick_finished_at);
  setSelectValue("scheduler-interval-select", payload.controls?.scheduler_tick_sec);
  setSelectValue("exec-mode-select", payload.controls?.exec_mode_default);

  setText("readiness-summary", payload.readiness?.summary);
  setText("readiness-private-error", payload.readiness?.private_error);
  setText("readiness-private-detail", payload.readiness?.private_error_detail);
  setText("report-time", payload.report?.reported_at);
  setText("report-status", payload.report?.status);
  setText("report-sent", payload.report?.notifier_sent ? "예" : "아니오");
  setText("report-error", payload.report?.notifier_error);
  renderPre("report-summary", payload.report?.summary || "-");
  populateSelect("preset-select", payload.controls?.preset_options || [], "normal");
  populateSelect("profile-template-select", payload.controls?.profile_template_options || [], null);
  setInputValue(
    "universe-symbols-text",
    Array.isArray(payload.controls?.universe_symbols) ? payload.controls.universe_symbols.join(",") : ""
  );
  renderUniverseChips(payload.controls?.universe_symbols || []);

  setText("capital-available", fmtNumber(payload.capital?.available_usdt, 4));
  setText("capital-wallet", fmtNumber(payload.capital?.wallet_usdt, 4));
  setText("capital-budget", fmtNumber(payload.capital?.budget_usdt, 4));
  setText("capital-notional", fmtNumber(payload.capital?.notional_usdt, 4));
  setText("capital-leverage", fmtNumber(payload.capital?.leverage, 2));
  setText("capital-block-reason", payload.capital?.block_reason_label);
  setInputValue("margin-budget-input", payload.risk_forms?.margin_budget?.margin_budget_usdt);
  setInputValue("max-leverage-input", payload.risk_forms?.margin_budget?.max_leverage);

  renderPositions(payload.positions || []);

  setText("risk-daily-pnl", `${fmtNumber(payload.risk?.daily_pnl_pct, 2)}%`);
  setText("risk-dd", `${fmtNumber(payload.risk?.drawdown_pct, 2)}%`);
  setText("risk-lose-streak", payload.risk?.lose_streak);
  setText("risk-cooldown", payload.risk?.cooldown_until);
  setText("risk-auto-reason", payload.risk?.last_auto_risk_reason_label);
  setInputValue("risk-basic-max-leverage", payload.risk_forms?.risk_basic?.max_leverage);
  setInputValue("risk-basic-max-exposure", payload.risk_forms?.risk_basic?.max_exposure_pct);
  setInputValue("risk-basic-max-notional", payload.risk_forms?.risk_basic?.max_notional_pct);
  setInputValue("risk-basic-per-trade", payload.risk_forms?.risk_basic?.per_trade_risk_pct);
  setInputValue("risk-advanced-daily-loss", payload.risk_forms?.risk_advanced?.daily_loss_limit_pct);
  setInputValue("risk-advanced-dd-limit", payload.risk_forms?.risk_advanced?.dd_limit_pct);
  setInputValue("risk-advanced-min-hold", payload.risk_forms?.risk_advanced?.min_hold_minutes);
  setInputValue("risk-advanced-score-conf", payload.risk_forms?.risk_advanced?.score_conf_threshold);
  setInputValue("notify-interval-input", payload.risk_forms?.notify_interval?.notify_interval_sec);
  setSelectValue("trailing-enabled-select", String(Boolean(payload.risk_forms?.trailing?.trailing_enabled)));
  setSelectValue("trailing-mode-select", payload.risk_forms?.trailing?.trailing_mode);
  setInputValue("trail-arm-input", payload.risk_forms?.trailing?.trail_arm_pnl_pct);
  setInputValue("trail-grace-input", payload.risk_forms?.trailing?.trail_grace_minutes);
  setInputValue("trail-distance-input", payload.risk_forms?.trailing?.trail_distance_pnl_pct);
  setSelectValue("atr-timeframe-select", payload.risk_forms?.trailing?.atr_trail_timeframe);
  setInputValue("atr-k-input", payload.risk_forms?.trailing?.atr_trail_k);
  setInputValue("atr-min-input", payload.risk_forms?.trailing?.atr_trail_min_pct);
  setInputValue("atr-max-input", payload.risk_forms?.trailing?.atr_trail_max_pct);
  setInputValue("profile-budget-input", payload.risk_forms?.margin_budget?.margin_budget_usdt);
  setInputValue("score-conf-input", payload.risk_forms?.scoring?.score_conf_threshold);
  setInputValue("score-gap-input", payload.risk_forms?.scoring?.score_gap_threshold);
  setInputValue("score-weight-10m", payload.risk_forms?.scoring?.weights?.["10m"]);
  setInputValue("score-weight-15m", payload.risk_forms?.scoring?.weights?.["15m"]);
  setInputValue("score-weight-30m", payload.risk_forms?.scoring?.weights?.["30m"]);
  setInputValue("score-weight-1h", payload.risk_forms?.scoring?.weights?.["1h"]);
  setInputValue("score-weight-4h", payload.risk_forms?.scoring?.weights?.["4h"]);
  setSelectValue(
    "momentum-filter-select",
    String(Boolean(payload.risk_forms?.scoring?.donchian_momentum_filter))
  );
  setInputValue("momentum-fast-input", payload.risk_forms?.scoring?.donchian_fast_ema_period);
  setInputValue("momentum-slow-input", payload.risk_forms?.scoring?.donchian_slow_ema_period);

  setText("recent-action", payload.recent_result?.last_action_label);
  setText("recent-reason", payload.recent_result?.last_reason_label);
  setText("recent-error", payload.recent_result?.last_error);
  setText("recent-blocked-reason", payload.recent_result?.blocked_reason_label);
  setText("recent-busy", payload.recent_result?.busy ? "예" : "아니오");
  setText("recent-stale", payload.recent_result?.stale ? "예" : "아니오");

  setText("alpha-id", payload.alpha?.last_alpha_id);
  setText("alpha-family", payload.alpha?.last_entry_family);
  setText("alpha-regime", payload.alpha?.last_regime);
  setText("alpha-block-reason", payload.alpha?.last_strategy_block_reason_label);
  setText("alpha-reject-focus", payload.alpha?.last_alpha_reject_focus);
  renderPre("alpha-reject-metrics", payload.alpha?.last_alpha_reject_metrics || {});
  renderPre("alpha-blocks", payload.alpha?.last_alpha_blocks || {});
  renderList("guidance-scope", payload.guidance?.panel_scope || []);
  renderList("guidance-safety", payload.guidance?.safety || []);
  renderMapList("guidance-state-meanings", payload.guidance?.state_meanings || {});
  renderList("guidance-first-checks", payload.guidance?.first_checks || []);
  const startBtn = document.getElementById("action-start");
  const pauseBtn = document.getElementById("action-pause");
  const panicBtn = document.getElementById("action-panic");
  const tickBtn = document.getElementById("action-tick");
  const inlineTickBtn = document.getElementById("action-tick-inline");

  if (startBtn) {
    startBtn.textContent = payload.engine?.start_label || "시작/재개";
    startBtn.disabled = !payload.engine?.can_start;
  }
  if (pauseBtn) {
    pauseBtn.disabled = !payload.engine?.can_pause;
  }
  if (panicBtn) {
    panicBtn.disabled = !payload.engine?.can_panic;
  }
  if (tickBtn) {
    tickBtn.disabled = !payload.scheduler?.can_tick;
  }
  if (inlineTickBtn) {
    inlineTickBtn.disabled = !payload.scheduler?.can_tick;
  }
}

async function postAction(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: body ? JSON.stringify(body) : null,
  });
  const payload = await resp.json();
  setFeedback(payload.summary || `요청 처리: ${resp.status}`, payload.status || "success");
  if (pageId === "logs") {
    await loadLogsFeed();
    return payload;
  }
  await loadConsole();
  await loadEventFeed();
  return payload;
}

function triggerDownload(downloadUrl) {
  if (!downloadUrl) {
    return;
  }
  const anchor = document.createElement("a");
  anchor.href = downloadUrl;
  anchor.download = "";
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

function bindActionButtons() {
  document.getElementById("manual-refresh")?.addEventListener("click", () => {
    loadConsole().catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("action-start")?.addEventListener("click", () => {
    runConfirmedAction(
      {
        title: "엔진을 시작/재개할까요?",
        message: "실제 운영 상태를 재개하거나 시작합니다.",
      },
      () => postAction("/operator/actions/start")
    ).catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("action-pause")?.addEventListener("click", () => {
    runConfirmedAction(
      {
        title: "엔진을 일시정지할까요?",
        message: "새 진입과 판단 흐름이 멈춥니다.",
      },
      () => postAction("/operator/actions/pause")
    ).catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("action-panic")?.addEventListener("click", () => {
    runConfirmedAction(
      {
        title: "패닉 절차를 실행할까요?",
        message: "안전 모드 전환과 포지션 정리 절차가 시작됩니다.",
      },
      () => postAction("/operator/actions/panic")
    ).catch((error) => setFeedback(String(error), "failed"));
  });
  const tickHandler = () => {
    runConfirmedAction(
      {
        title: "즉시 판단을 실행할까요?",
        message: "현재 조건 기준으로 즉시 판단/주문 경로를 평가합니다.",
      },
      () => postAction("/operator/actions/tick")
    ).catch((error) => setFeedback(String(error), "failed"));
  };
  document.getElementById("action-tick")?.addEventListener("click", tickHandler);
  document.getElementById("action-tick-inline")?.addEventListener("click", tickHandler);
  document.getElementById("action-reconcile")?.addEventListener("click", () => {
    runConfirmedAction(
      {
        title: "Reconcile을 실행할까요?",
        message: "런타임 상태와 거래소 상태를 다시 대조합니다.",
      },
      () => postAction("/operator/actions/reconcile")
    ).catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("action-cooldown-clear")?.addEventListener("click", () => {
    runConfirmedAction(
      {
        title: "쿨다운을 해제할까요?",
        message: "현재 적용 중인 진입/판단 쿨다운 상태를 초기화합니다.",
      },
      () => postAction("/operator/actions/cooldown-clear")
    ).catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("action-close-all")?.addEventListener("click", () => {
    runConfirmedAction(
      {
        title: "전체 포지션을 종료할까요?",
        message: "모든 열린 포지션에 종료 요청을 보냅니다.",
      },
      () => postAction("/operator/actions/positions/close-all")
    ).catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("action-report")?.addEventListener("click", () => {
    runConfirmedAction(
      {
        title: "리포트를 전송할까요?",
        message: "현재 운영 상태 기준으로 수동 리포트를 생성하고 전송합니다.",
      },
      () => postAction("/operator/actions/report")
    ).catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("push-subscribe-btn")?.addEventListener("click", async () => {
    try {
      const push = lastConsolePayload?.push || {};
      const diagnostic = computePushDiagnostic(push);
      await postClientLog({
        title: "push_subscribe_click",
        mainText: diagnostic.reason || "click_received",
        subText: diagnostic.message,
        context: diagnostic,
      });
      if (!push.public_key) {
        throw new Error(push?.last_error || "webpush_public_key_missing");
      }
      if (diagnostic.reason) {
        await postClientLog({
          title: "push_subscribe_blocked",
          mainText: diagnostic.reason,
          subText: diagnostic.message,
          context: diagnostic,
        });
        throw new Error(diagnostic.message);
      }
      const registration = await ensurePushRegistration();
      if (!registration) {
        await postClientLog({
          title: "push_subscribe_registration_missing",
          mainText: "sw_registration_failed",
          subText: pushDiagnosticMessage("sw_registration_failed"),
          context: diagnostic,
        });
        throw new Error(pushDiagnosticMessage("sw_registration_failed"));
      }
      let permission = Notification.permission;
      if (permission !== "granted") {
        permission = await Notification.requestPermission();
      }
      if (permission !== "granted") {
        throw new Error(pushPermissionLabel(permission));
      }
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(String(push.public_key)),
        });
      }
      if (!subscription) {
        throw new Error(pushDiagnosticMessage("push_subscription_failed"));
      }
      const deviceLabel = document.getElementById("push-device-label")?.value?.trim() || buildDefaultDeviceLabel();
      await postAction("/operator/api/push/subscribe", {
        subscription: subscription.toJSON(),
        device_id: getPushDeviceId(),
        device_label: deviceLabel,
        user_agent: navigator.userAgent,
        platform: navigator.platform || "web",
        standalone: isStandaloneMode(),
      });
    } catch (error) {
      await postClientLog({
        title: "push_subscribe_error",
        mainText: String(error?.name || "push_subscribe_error"),
        subText: String(error),
        context: {
          diagnostic: lastPushDiagnostic,
          error: String(error?.stack || error || ""),
        },
      });
      setFeedback(String(error), "failed");
    }
  });
  document.getElementById("push-unsubscribe-btn")?.addEventListener("click", async () => {
    try {
      const subscription = await currentPushSubscription();
      if (!subscription) {
        throw new Error("push_not_subscribed");
      }
      const endpoint = subscription.endpoint;
      try {
        await subscription.unsubscribe();
      } catch (_error) {
        // Keep server state cleanup even if browser-side unsubscribe returns false.
      }
      await postAction("/operator/api/push/unsubscribe", { endpoint });
    } catch (error) {
      setFeedback(String(error), "failed");
    }
  });
  document.getElementById("push-test-btn")?.addEventListener("click", () => {
    const deviceLabel = document.getElementById("push-device-label")?.value?.trim() || buildDefaultDeviceLabel();
    runConfirmedAction(
      {
        title: "현재 기기로 테스트 푸시를 보낼까요?",
        message: `${deviceLabel} 기준으로 웹 푸시 테스트를 발송합니다.`,
      },
      () => postAction("/operator/actions/push-test", { device_label: deviceLabel })
    ).catch((error) => setFeedback(String(error), "failed"));
  });
}

window.addEventListener("error", (event) => {
  postClientLog({
    title: "operator_js_error",
    mainText: String(event.message || "window_error"),
    subText: `${event.filename || "-"}:${event.lineno || 0}:${event.colno || 0}`,
    category: "action",
    context: {
      error: String(event.error?.stack || event.error || event.message || ""),
    },
  });
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  postClientLog({
    title: "operator_js_rejection",
    mainText: String(reason?.message || reason || "unhandledrejection"),
    subText: String(reason?.stack || reason || ""),
    category: "action",
    context: {
      error: String(reason?.stack || reason || ""),
    },
  });
});

function bindForms() {
  document
    .querySelectorAll("input, select, textarea")
    .forEach((el) => {
      el.addEventListener("input", markFieldDirty);
      el.addEventListener("change", markFieldDirty);
      el.addEventListener("focus", markFieldDirty);
    });

  document.getElementById("symbol-leverage-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const symbol = document.getElementById("symbol-input")?.value || "";
    const leverage = Number(document.getElementById("leverage-input")?.value);
    runConfirmedAction(
      {
        title: "심볼 레버리지를 변경할까요?",
        message: `${symbol || "-"} 레버리지를 ${fmtMaybe(leverage)}로 적용합니다.`,
      },
      () => postAction("/operator/actions/symbol-leverage", { symbol, leverage })
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("scheduler-interval-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const tick_sec = Number(document.getElementById("scheduler-interval-select")?.value);
    runConfirmedAction(
      {
        title: "판단/상태 알림 주기를 변경할까요?",
        message: `엔진 판단, 웹 로그, 모바일 알림 주기를 ${fmtMaybe(tick_sec)}초로 함께 적용합니다.`,
      },
      () => postAction("/operator/actions/scheduler-interval", { tick_sec })
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("exec-mode-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const exec_mode = document.getElementById("exec-mode-select")?.value || "MARKET";
    runConfirmedAction(
      {
        title: "실행 모드를 변경할까요?",
        message: `실행 모드를 ${exec_mode}로 적용합니다.`,
      },
      () => postAction("/operator/actions/exec-mode", { exec_mode })
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("margin-budget-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const amount_usdt = Number(document.getElementById("margin-budget-input")?.value);
    const leverageRaw = document.getElementById("max-leverage-input")?.value || "";
    const leverage = leverageRaw === "" ? null : Number(leverageRaw);
    runConfirmedAction(
      {
        title: "증거금 설정을 적용할까요?",
        message: `목표 증거금 ${fmtMaybe(amount_usdt)} USDT${leverage === null ? "" : ` / 최대 레버리지 ${fmtMaybe(leverage)}`}`,
      },
      () => postAction("/operator/actions/margin-budget", { amount_usdt, leverage })
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("risk-basic-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const body = {
      max_leverage: Number(document.getElementById("risk-basic-max-leverage")?.value),
      max_exposure_pct: Number(document.getElementById("risk-basic-max-exposure")?.value),
      max_notional_pct: Number(document.getElementById("risk-basic-max-notional")?.value),
      per_trade_risk_pct: Number(document.getElementById("risk-basic-per-trade")?.value),
    };
    runConfirmedAction(
      {
        title: "리스크 기본값을 적용할까요?",
        message: "최대 레버리지, 노출, 노셔널, 1회 리스크를 변경합니다.",
      },
      () => postAction("/operator/actions/risk-basic", body)
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("risk-advanced-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const body = {
      daily_loss_limit_pct: Number(document.getElementById("risk-advanced-daily-loss")?.value),
      dd_limit_pct: Number(document.getElementById("risk-advanced-dd-limit")?.value),
      min_hold_minutes: Number(document.getElementById("risk-advanced-min-hold")?.value),
      score_conf_threshold: Number(document.getElementById("risk-advanced-score-conf")?.value),
    };
    runConfirmedAction(
      {
        title: "리스크 고급값을 적용할까요?",
        message: "일일 손실, DD, 최소 보유 시간, 신뢰도 임계값을 변경합니다.",
      },
      () => postAction("/operator/actions/risk-advanced", body)
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("notify-interval-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const notify_interval_sec = Number(document.getElementById("notify-interval-input")?.value);
    runConfirmedAction(
      {
        title: "상태 알림 주기만 변경할까요?",
        message: `웹 로그와 모바일 알림 주기만 ${fmtMaybe(notify_interval_sec)}초로 적용합니다. 엔진 판단 주기는 유지됩니다.`,
      },
      () => postAction("/operator/actions/notify-interval", { notify_interval_sec })
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("preset-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = document.getElementById("preset-select")?.value || "normal";
    runConfirmedAction(
      {
        title: "운영 프리셋을 적용할까요?",
        message: `${name} 프리셋을 현재 런타임 설정에 반영합니다.`,
      },
      () => postAction("/operator/actions/preset", { name })
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("profile-template-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = document.getElementById("profile-template-select")?.value || "";
    const budgetRaw = document.getElementById("profile-budget-input")?.value || "";
    const budget_usdt = budgetRaw === "" ? null : Number(budgetRaw);
    runConfirmedAction(
      {
        title: "프로파일 템플릿을 적용할까요?",
        message: `${name || "-"} 템플릿${budget_usdt === null ? "" : ` / 예산 ${fmtMaybe(budget_usdt)} USDT`} 적용`,
      },
      () => postAction("/operator/actions/profile-template", { name, budget_usdt })
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("trailing-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const trailing_enabled = (document.getElementById("trailing-enabled-select")?.value || "true") === "true";
    const trailing_mode = document.getElementById("trailing-mode-select")?.value || "PCT";
    const body = {
      trailing_enabled,
      trailing_mode,
      trail_arm_pnl_pct: Number(document.getElementById("trail-arm-input")?.value),
      trail_grace_minutes: Number(document.getElementById("trail-grace-input")?.value),
      trail_distance_pnl_pct: Number(document.getElementById("trail-distance-input")?.value),
      atr_trail_timeframe: document.getElementById("atr-timeframe-select")?.value || "1h",
      atr_trail_k: Number(document.getElementById("atr-k-input")?.value),
      atr_trail_min_pct: Number(document.getElementById("atr-min-input")?.value),
      atr_trail_max_pct: Number(document.getElementById("atr-max-input")?.value),
    };
    runConfirmedAction(
      {
        title: "트레일링 설정을 적용할까요?",
        message: `${trailing_enabled ? "사용" : "미사용"} / 모드 ${trailing_mode} 기준으로 갱신합니다.`,
      },
      () => postAction("/operator/actions/trailing", body)
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("universe-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const symbols_text = document.getElementById("universe-symbols-text")?.value || "";
    runConfirmedAction(
      {
        title: "운영 심볼을 적용할까요?",
        message: "현재 입력한 심볼 목록으로 운영 유니버스를 교체합니다.",
      },
      () => postAction("/operator/actions/universe", { symbols_text })
    ).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("scoring-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const body = {
      tf_weight_10m: Number(document.getElementById("score-weight-10m")?.value),
      tf_weight_15m: Number(document.getElementById("score-weight-15m")?.value),
      tf_weight_30m: Number(document.getElementById("score-weight-30m")?.value),
      tf_weight_1h: Number(document.getElementById("score-weight-1h")?.value),
      tf_weight_4h: Number(document.getElementById("score-weight-4h")?.value),
      score_conf_threshold: Number(document.getElementById("score-conf-input")?.value),
      score_gap_threshold: Number(document.getElementById("score-gap-input")?.value),
      donchian_momentum_filter:
        (document.getElementById("momentum-filter-select")?.value || "true") === "true",
      donchian_fast_ema_period: Number(document.getElementById("momentum-fast-input")?.value),
      donchian_slow_ema_period: Number(document.getElementById("momentum-slow-input")?.value),
    };
    runConfirmedAction(
      {
        title: "판단식 설정을 적용할까요?",
        message: "스코어 가중치, 임계값, 모멘텀 필터를 변경합니다.",
      },
      () => postAction("/operator/actions/scoring", body)
    ).catch((error) => setFeedback(String(error), "failed"));
  });
}

function bindConfirmModal() {
  confirmModalOkEl?.addEventListener("click", () => closeConfirmModal(true));
  confirmModalCancelEl?.addEventListener("click", () => closeConfirmModal(false));
  confirmModalBackdropEl?.addEventListener("click", () => closeConfirmModal(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !confirmModalEl?.hidden) {
      closeConfirmModal(false);
    }
  });
}

function bindLogsPage() {
  document.getElementById("logs-export-bundle-quick")?.addEventListener("click", () => {
    runConfirmedAction(
      {
        title: "빠른 로그 번들을 추출할까요?",
        message: "최근 운영 이벤트와 최근 로그 tail 기준으로 빠르게 번들을 생성합니다.",
      },
      async () => {
        const payload = await postAction("/operator/actions/debug-bundle", { mode: "quick" });
        triggerDownload(payload?.result?.download_url);
      }
    ).catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("logs-export-bundle-full")?.addEventListener("click", () => {
    runConfirmedAction(
      {
        title: "전체 로그 번들을 추출할까요?",
        message: "누적 운영 이벤트, DB 이력, 전체 로그 파일을 묶습니다. 파일이 크고 시간이 더 걸릴 수 있습니다.",
      },
      async () => {
        const payload = await postAction("/operator/actions/debug-bundle", { mode: "full" });
        triggerDownload(payload?.result?.download_url);
      }
    ).catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("logs-refresh")?.addEventListener("click", () => {
    loadLogsFeed().catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("logs-filter-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    logsState.offset = 0;
    loadLogsFeed().catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("logs-prev")?.addEventListener("click", () => {
    logsState.offset = Math.max(0, logsState.offset - logsState.limit);
    loadLogsFeed().catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("logs-next")?.addEventListener("click", () => {
    logsState.offset += logsState.limit;
    loadLogsFeed().catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("logs-limit-select")?.addEventListener("change", () => {
    logsState.offset = 0;
    loadLogsFeed().catch((error) => setFeedback(String(error), "failed"));
  });
}

function initNav() {
  const toggle = document.getElementById("operator-nav-toggle");
  const nav = document.getElementById("operator-nav");
  if (!toggle || !nav) {
    return;
  }
  const closeNav = () => {
    nav.classList.remove("is-open");
    toggle.setAttribute("aria-expanded", "false");
  };
  toggle.addEventListener("click", () => {
    const isOpen = nav.classList.toggle("is-open");
    toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
  });
  nav.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", closeNav);
  });
  window.addEventListener("resize", () => {
    if (window.innerWidth > 720) {
      closeNav();
    }
  });
}

function initAdaptiveDisclosures() {
  const shouldCollapse = window.innerWidth <= 720;
  document.querySelectorAll(".adaptive-disclosure[data-mobile-collapsed='true']").forEach((el) => {
    el.open = !shouldCollapse;
  });
}

function initConsolePage() {
  bindActionButtons();
  bindForms();
  initAdaptiveDisclosures();
  loadConsole().catch((error) => setFeedback(String(error), "failed"));
  loadEventFeed().catch((error) => setFeedback(String(error), "failed"));
  window.setInterval(() => {
    loadConsole().catch(() => {});
    loadEventFeed().catch(() => {});
  }, 5000);
}

function initLogsPage() {
  bindLogsPage();
  initAdaptiveDisclosures();
  loadLogsFeed().catch((error) => setFeedback(String(error), "failed"));
  window.setInterval(() => {
    loadLogsFeed().catch(() => {});
  }, 15000);
}

initNav();
bindConfirmModal();
if (pageId === "logs") {
  initLogsPage();
} else {
  initConsolePage();
}

const feedbackEl = document.getElementById("action-feedback");

function fmtNumber(value, digits = 4) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : "-";
}

function fmtMaybe(value) {
  return value === null || value === undefined || value === "" ? "-" : String(value);
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) {
    el.textContent = fmtMaybe(value);
  }
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

function renderPre(id, payload) {
  const el = document.getElementById(id);
  if (el) {
    const normalized = payload && Object.keys(payload).length > 0 ? payload : "-";
    el.textContent = typeof normalized === "string" ? normalized : JSON.stringify(normalized, null, 2);
  }
}

function renderPositions(rows) {
  const wrap = document.getElementById("positions-list");
  const empty = document.getElementById("positions-empty");
  if (!wrap || !empty) {
    return;
  }
  wrap.innerHTML = "";
  if (!Array.isArray(rows) || rows.length === 0) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  const header = document.createElement("div");
  header.className = "table-row header";
  header.innerHTML = "<div>심볼</div><div>방향</div><div>수량</div><div>진입가</div><div>미실현PnL</div>";
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
    `;
    wrap.appendChild(item);
  }
}

function setFeedback(message, ok = true) {
  if (!feedbackEl) {
    return;
  }
  feedbackEl.hidden = false;
  feedbackEl.className = ok ? "feedback ok" : "feedback";
  feedbackEl.textContent = message;
}

async function loadConsole() {
  const resp = await fetch("/operator/api/console", { headers: { Accept: "application/json" } });
  if (!resp.ok) {
    throw new Error(`console_load_failed:${resp.status}`);
  }
  const payload = await resp.json();

  setText("runtime-mode", `${fmtMaybe(payload.runtime?.mode)} / ${fmtMaybe(payload.runtime?.env)}`);
  setText("runtime-profile", payload.runtime?.profile);

  setText("engine-state", payload.engine?.state_label);
  setText("engine-updated-at", payload.engine?.updated_at);
  setText("health-ready", payload.health?.ready_label);
  setText("health-stale", payload.health?.stale ? "예" : "아니오");
  setText("health-blocked", payload.health?.blocked_reason_label || (payload.health?.blocked ? "예" : "아니오"));
  setText("health-busy", payload.health?.busy_reason_label || (payload.health?.busy ? "예" : "아니오"));
  renderList("health-stale-items", payload.health?.stale_items || []);

  setText("scheduler-tick-sec", `${fmtNumber(payload.scheduler?.tick_sec, 1)}초`);
  setText("scheduler-last-action", payload.scheduler?.last_action_label);
  setText("scheduler-last-reason", payload.scheduler?.last_reason_label);
  setText("scheduler-last-error", payload.scheduler?.last_error);
  setText("scheduler-started-at", payload.scheduler?.tick_started_at);
  setText("scheduler-finished-at", payload.scheduler?.tick_finished_at);

  setText("readiness-summary", payload.readiness?.summary);
  setText("readiness-private-error", payload.readiness?.private_error);
  setText("readiness-private-detail", payload.readiness?.private_error_detail);

  setText("capital-available", fmtNumber(payload.capital?.available_usdt, 4));
  setText("capital-wallet", fmtNumber(payload.capital?.wallet_usdt, 4));
  setText("capital-budget", fmtNumber(payload.capital?.budget_usdt, 4));
  setText("capital-notional", fmtNumber(payload.capital?.notional_usdt, 4));
  setText("capital-leverage", fmtNumber(payload.capital?.leverage, 2));
  setText("capital-block-reason", payload.capital?.block_reason_label);

  renderPositions(payload.positions || []);

  setText("risk-daily-pnl", `${fmtNumber(payload.risk?.daily_pnl_pct, 2)}%`);
  setText("risk-dd", `${fmtNumber(payload.risk?.drawdown_pct, 2)}%`);
  setText("risk-lose-streak", payload.risk?.lose_streak);
  setText("risk-cooldown", payload.risk?.cooldown_until);
  setText("risk-auto-reason", payload.risk?.last_auto_risk_reason_label);

  setText("alpha-id", payload.alpha?.last_alpha_id);
  setText("alpha-family", payload.alpha?.last_entry_family);
  setText("alpha-regime", payload.alpha?.last_regime);
  setText("alpha-block-reason", payload.alpha?.last_strategy_block_reason_label);
  setText("alpha-reject-focus", payload.alpha?.last_alpha_reject_focus);
  renderPre("alpha-reject-metrics", payload.alpha?.last_alpha_reject_metrics || {});
  renderPre("alpha-blocks", payload.alpha?.last_alpha_blocks || {});

  const startBtn = document.getElementById("action-start");
  const pauseBtn = document.getElementById("action-pause");
  const panicBtn = document.getElementById("action-panic");
  const tickBtn = document.getElementById("action-tick");

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
}

async function postAction(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: body ? JSON.stringify(body) : null,
  });
  const payload = await resp.json();
  setFeedback(payload.summary || `요청 처리: ${resp.status}`, resp.ok && payload.ok !== false);
  await loadConsole();
}

function bindActions() {
  document.getElementById("manual-refresh")?.addEventListener("click", () => {
    loadConsole().catch((error) => setFeedback(String(error), false));
  });
  document.getElementById("action-start")?.addEventListener("click", () => {
    postAction("/operator/actions/start").catch((error) => setFeedback(String(error), false));
  });
  document.getElementById("action-pause")?.addEventListener("click", () => {
    postAction("/operator/actions/pause").catch((error) => setFeedback(String(error), false));
  });
  document.getElementById("action-panic")?.addEventListener("click", () => {
    postAction("/operator/actions/panic").catch((error) => setFeedback(String(error), false));
  });
  document.getElementById("action-tick")?.addEventListener("click", () => {
    postAction("/operator/actions/tick").catch((error) => setFeedback(String(error), false));
  });
  document.getElementById("symbol-leverage-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const symbol = document.getElementById("symbol-input")?.value || "";
    const leverage = Number(document.getElementById("leverage-input")?.value);
    postAction("/operator/actions/symbol-leverage", { symbol, leverage }).catch((error) =>
      setFeedback(String(error), false)
    );
  });
}

bindActions();
loadConsole().catch((error) => setFeedback(String(error), false));
window.setInterval(() => {
  loadConsole().catch(() => {});
}, 5000);

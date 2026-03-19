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

function setInputValue(id, value) {
  const el = document.getElementById(id);
  if (el) {
    el.value = value ?? "";
  }
}

function setSelectValue(id, value) {
  const el = document.getElementById(id);
  if (el) {
    el.value = String(value ?? "");
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

function setFeedback(message, status = "success") {
  if (!feedbackEl) {
    return;
  }
  feedbackEl.hidden = false;
  feedbackEl.className = `feedback ${status}`;
  feedbackEl.textContent = message;
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
      postAction("/operator/actions/positions/close", { symbol }).catch((error) =>
        setFeedback(String(error), "failed")
      );
    });
  });
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
  populateSelect("preset-select", payload.controls?.preset_options || [], "normal");
  populateSelect("profile-template-select", payload.controls?.profile_template_options || [], null);

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
  await loadConsole();
}

function bindActionButtons() {
  document.getElementById("manual-refresh")?.addEventListener("click", () => {
    loadConsole().catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("action-start")?.addEventListener("click", () => {
    postAction("/operator/actions/start").catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("action-pause")?.addEventListener("click", () => {
    postAction("/operator/actions/pause").catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("action-panic")?.addEventListener("click", () => {
    postAction("/operator/actions/panic").catch((error) => setFeedback(String(error), "failed"));
  });
  const tickHandler = () => {
    postAction("/operator/actions/tick").catch((error) => setFeedback(String(error), "failed"));
  };
  document.getElementById("action-tick")?.addEventListener("click", tickHandler);
  document.getElementById("action-tick-inline")?.addEventListener("click", tickHandler);
  document.getElementById("action-reconcile")?.addEventListener("click", () => {
    postAction("/operator/actions/reconcile").catch((error) => setFeedback(String(error), "failed"));
  });
  document.getElementById("action-cooldown-clear")?.addEventListener("click", () => {
    postAction("/operator/actions/cooldown-clear").catch((error) =>
      setFeedback(String(error), "failed")
    );
  });
  document.getElementById("action-close-all")?.addEventListener("click", () => {
    postAction("/operator/actions/positions/close-all").catch((error) =>
      setFeedback(String(error), "failed")
    );
  });
}

function bindForms() {
  document.getElementById("symbol-leverage-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const symbol = document.getElementById("symbol-input")?.value || "";
    const leverage = Number(document.getElementById("leverage-input")?.value);
    postAction("/operator/actions/symbol-leverage", { symbol, leverage }).catch((error) =>
      setFeedback(String(error), "failed")
    );
  });

  document.getElementById("scheduler-interval-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const tick_sec = Number(document.getElementById("scheduler-interval-select")?.value);
    postAction("/operator/actions/scheduler-interval", { tick_sec }).catch((error) =>
      setFeedback(String(error), "failed")
    );
  });

  document.getElementById("exec-mode-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const exec_mode = document.getElementById("exec-mode-select")?.value || "MARKET";
    postAction("/operator/actions/exec-mode", { exec_mode }).catch((error) =>
      setFeedback(String(error), "failed")
    );
  });

  document.getElementById("margin-budget-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const amount_usdt = Number(document.getElementById("margin-budget-input")?.value);
    const leverageRaw = document.getElementById("max-leverage-input")?.value || "";
    const leverage = leverageRaw === "" ? null : Number(leverageRaw);
    postAction("/operator/actions/margin-budget", { amount_usdt, leverage }).catch((error) =>
      setFeedback(String(error), "failed")
    );
  });

  document.getElementById("risk-basic-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    postAction("/operator/actions/risk-basic", {
      max_leverage: Number(document.getElementById("risk-basic-max-leverage")?.value),
      max_exposure_pct: Number(document.getElementById("risk-basic-max-exposure")?.value),
      max_notional_pct: Number(document.getElementById("risk-basic-max-notional")?.value),
      per_trade_risk_pct: Number(document.getElementById("risk-basic-per-trade")?.value),
    }).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("risk-advanced-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    postAction("/operator/actions/risk-advanced", {
      daily_loss_limit_pct: Number(document.getElementById("risk-advanced-daily-loss")?.value),
      dd_limit_pct: Number(document.getElementById("risk-advanced-dd-limit")?.value),
      min_hold_minutes: Number(document.getElementById("risk-advanced-min-hold")?.value),
      score_conf_threshold: Number(document.getElementById("risk-advanced-score-conf")?.value),
    }).catch((error) => setFeedback(String(error), "failed"));
  });

  document.getElementById("notify-interval-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const notify_interval_sec = Number(document.getElementById("notify-interval-input")?.value);
    postAction("/operator/actions/notify-interval", { notify_interval_sec }).catch((error) =>
      setFeedback(String(error), "failed")
    );
  });

  document.getElementById("preset-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = document.getElementById("preset-select")?.value || "normal";
    postAction("/operator/actions/preset", { name }).catch((error) =>
      setFeedback(String(error), "failed")
    );
  });

  document.getElementById("profile-template-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = document.getElementById("profile-template-select")?.value || "";
    const budgetRaw = document.getElementById("profile-budget-input")?.value || "";
    const budget_usdt = budgetRaw === "" ? null : Number(budgetRaw);
    postAction("/operator/actions/profile-template", { name, budget_usdt }).catch((error) =>
      setFeedback(String(error), "failed")
    );
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
    postAction("/operator/actions/trailing", body).catch((error) =>
      setFeedback(String(error), "failed")
    );
  });
}

bindActionButtons();
bindForms();
loadConsole().catch((error) => setFeedback(String(error), "failed"));
window.setInterval(() => {
  loadConsole().catch(() => {});
}, 5000);

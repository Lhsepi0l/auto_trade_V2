from __future__ import annotations

import json
from typing import Any

from v2.backtest.common import _to_float
from v2.backtest.decision_types import _ReplayDecision
from v2.backtest.providers import _ReplaySnapshotProvider
from v2.backtest.reporting import _write_replay_report
from v2.backtest.row_loader import _extract_snapshot_time, _load_replay_frames
from v2.backtest.runtime_deps import get_build_runtime
from v2.config.loader import EffectiveConfig
from v2.kernel import build_default_kernel
from v2.tpsl import BracketConfig, BracketPlanner


def _build_replay_cycle_record(
    *,
    tick: int,
    cycle: Any,
    decision: dict[str, Any],
    symbol: str,
    bracket_planner: BracketPlanner,
) -> dict[str, Any]:
    candidate = cycle.candidate
    risk = cycle.risk
    size = cycle.size
    execution = cycle.execution

    if candidate is None:
        would_enter = False
    else:
        would_enter = cycle.state in {"dry_run", "executed"}

    decision_payload = decision if isinstance(decision, dict) else {}
    strategy_decision = decision_payload.get("decision")
    if not isinstance(strategy_decision, dict):
        strategy_decision = {}
    base_decision: dict[str, Any] = strategy_decision if strategy_decision else decision_payload

    entry_price = _to_float(strategy_decision.get("entry_price"))
    if (entry_price is None or entry_price <= 0) and candidate is not None:
        entry_price = _to_float(candidate.entry_price)
    sl_tp: dict[str, float] | None = None
    strategy_sl_tp = base_decision.get("sl_tp")
    if isinstance(strategy_sl_tp, dict):
        tp_price = _to_float(strategy_sl_tp.get("take_profit"))
        tp_final_price = _to_float(strategy_sl_tp.get("take_profit_final"))
        sl_price = _to_float(strategy_sl_tp.get("stop_loss"))
        if (
            entry_price is not None
            and entry_price > 0
            and tp_price is not None
            and tp_price > 0
            and sl_price is not None
            and sl_price > 0
        ):
            sl_tp = {
                "take_profit": float(tp_price),
                "stop_loss": float(sl_price),
            }
            if tp_final_price is not None and tp_final_price > 0:
                sl_tp["take_profit_final"] = float(tp_final_price)
    if entry_price and entry_price > 0 and candidate is not None:
        if sl_tp is None:
            try:
                levels = bracket_planner.levels(
                    entry_price=entry_price,
                    side="LONG" if candidate.side == "BUY" else "SHORT",
                )
                sl_tp = {
                    "take_profit": float(levels["take_profit"]),
                    "stop_loss": float(levels["stop_loss"]),
                }
            except (TypeError, ValueError):
                sl_tp = None

    cycle_record: dict[str, Any] = {
        "tick": tick,
        "symbol": symbol,
        "state": cycle.state,
        "reason": cycle.reason,
        "would_enter": would_enter,
    }
    if candidate is not None:
        cycle_record["candidate"] = {
            "symbol": candidate.symbol,
            "side": candidate.side,
            "score": candidate.score,
            "alpha_id": getattr(candidate, "alpha_id", None),
            "entry_price": candidate.entry_price,
            "reason": candidate.reason,
        }
    else:
        cycle_record["candidate"] = None

    if risk is not None:
        cycle_record["risk"] = {
            "allow": bool(risk.allow),
            "reason": risk.reason,
            "max_notional": risk.max_notional,
        }

    if size is not None:
        cycle_record["size"] = {
            "qty": size.qty,
            "notional": size.notional,
            "leverage": size.leverage,
            "reason": size.reason,
        }

    if execution is not None:
        cycle_record["execution"] = {
            "ok": bool(execution.ok),
            "order_id": execution.order_id,
            "reason": execution.reason,
        }

    if base_decision or candidate is not None:
        side_value = base_decision.get("side")
        if side_value is None and candidate is not None:
            side_value = candidate.side
        intent_value = base_decision.get("intent")
        if intent_value is None and isinstance(side_value, str):
            if side_value == "BUY":
                intent_value = "LONG"
            elif side_value == "SELL":
                intent_value = "SHORT"

        cycle_record["decision"] = {
            "intent": intent_value,
            "side": side_value,
            "score": base_decision.get("score"),
            "alpha_id": base_decision.get("alpha_id"),
            "regime": base_decision.get("regime"),
            "allowed_side": base_decision.get("allowed_side"),
            "reason": base_decision.get("reason"),
            "blocks": base_decision.get("blocks"),
            "filters": base_decision.get("filters"),
            "signals": base_decision.get("signals"),
            "alpha_blocks": base_decision.get("alpha_blocks"),
            "indicators": base_decision.get("indicators"),
            "entry_mode": base_decision.get("entry_mode"),
            "sideways": base_decision.get("sideways"),
            "stop_hint": base_decision.get("stop_hint"),
            "management_hint": base_decision.get("management_hint"),
            "risk_per_trade_pct": base_decision.get("risk_per_trade_pct"),
            "max_effective_leverage": base_decision.get("max_effective_leverage"),
            "reverse_exit_min_r": base_decision.get("reverse_exit_min_r"),
            "execution": base_decision.get("execution"),
            "entry_price": entry_price,
            "sl_tp": sl_tp,
        }

    return cycle_record


def _run_replay(
    cfg: EffectiveConfig,
    *,
    replay_path: str,
    report_dir: str,
    report_path: str | None = None,
) -> int:
    _storage, state_store, _ops, _adapter, _rest_client = get_build_runtime()(cfg)
    state_store.set(mode=cfg.mode, status="RUNNING")
    frames = _load_replay_frames(
        path=replay_path, default_symbol=cfg.behavior.exchange.default_symbol
    )
    if not frames:
        print(
            json.dumps(
                {"replay": {"source": replay_path, "cycles": 0}, "error": "no replay data"},
                ensure_ascii=True,
            )
        )
        return 1

    if cfg.behavior.ops.pause_on_start:
        _ops.pause()

    collector = _ReplayDecision()
    snapshot_provider = _ReplaySnapshotProvider(frames)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        tick=0,
        dry_run=True,
        rest_client=_rest_client,
        snapshot_provider=snapshot_provider,
        overheat_fetcher=None,
        journal_logger=collector,
    )

    bracket_planner = BracketPlanner(
        cfg=BracketConfig(
            take_profit_pct=cfg.behavior.tpsl.take_profit_pct,
            stop_loss_pct=cfg.behavior.tpsl.stop_loss_pct,
        )
    )

    rows: list[dict[str, Any]] = []
    for tick in range(len(frames)):
        cycle = kernel.run_once()
        decision = collector.take()
        symbol = cfg.behavior.exchange.default_symbol
        if tick < len(frames):
            symbol = frames[tick].symbol or symbol
        row = _build_replay_cycle_record(
            tick=tick,
            cycle=cycle,
            decision=decision,
            symbol=symbol,
            bracket_planner=bracket_planner,
        )

        if isinstance(frames[tick].meta, dict):
            row.update({k: frames[tick].meta[k] for k in frames[tick].meta if k not in row})
        snapshot_time = _extract_snapshot_time(frames[tick].meta)
        if snapshot_time is not None:
            row["snapshot_time"] = snapshot_time
        rows.append(row)

    report_file = _write_replay_report(
        cfg=cfg,
        replay_source=replay_path,
        rows=rows,
        report_dir=report_dir,
        report_path=report_path,
    )
    print(json.dumps({"replay": {"status": "completed", "report": report_file}}, ensure_ascii=True))
    return 0

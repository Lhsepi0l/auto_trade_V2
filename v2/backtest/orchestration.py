from __future__ import annotations

import time
from collections import Counter
from typing import Any

from v2.backtest.common import _candidate_to_payload, _to_float, _to_int
from v2.backtest.decision_types import _ReplayDecisionBySymbol
from v2.backtest.providers import _HistoricalPortfolioSnapshotProvider
from v2.backtest.runtime_deps import get_build_strategy_selector
from v2.backtest.snapshots import _FundingRateRow, _Kline15m
from v2.config.loader import EffectiveConfig
from v2.kernel import Candidate
from v2.tpsl import BracketPlanner


def _portfolio_history_limit(params: dict[str, Any] | None) -> int:
    source = params or {}

    def _ival(name: str, default: int) -> int:
        value = _to_int(source.get(name))
        return max(int(value if value is not None else default), 1)

    required_4h = max(_ival("ema_slow_4h", 200) + 5, _ival("adx_period_4h", 14) + 5)
    required_1h = max(_ival("ema_bias_period_1h", 34) + 5, _ival("rsi_period_1h", 14) + 5)
    bb_period_15m = _ival("bb_period_15m", 20)
    premium_lookback_3d = _ival("premium_zscore_lookback_3d_15m", 288)
    required_15m = max(
        _ival("atr_period_15m", 14) + 5,
        _ival("ema_pullback_period_15m", 20) + 5,
        _ival("donchian_period_15m", 20) + 2,
        bb_period_15m + 5,
        _ival("volume_sma_period_15m", 20) + 2,
        _ival("swing_lookback_15m", 8) + 2,
        _ival("squeeze_lookback_15m", 48) + bb_period_15m + 2,
        premium_lookback_3d + 2,
    )
    return max(required_4h, required_1h, required_15m) + 8


def _build_local_backtest_cycle_input(
    *,
    cycle: Any,
    decision: dict[str, Any],
    bracket_planner: BracketPlanner,
) -> tuple[dict[str, Any], str]:
    candidate = cycle.candidate
    size = cycle.size
    state = str(cycle.state)
    would_enter = candidate is not None and state in {"dry_run", "executed"}

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
                    entry_price=float(entry_price),
                    side="LONG" if candidate.side == "BUY" else "SHORT",
                )
                sl_tp = {
                    "take_profit": float(levels["take_profit"]),
                    "stop_loss": float(levels["stop_loss"]),
                }
            except (TypeError, ValueError):
                sl_tp = None

    decision_side = base_decision.get("side")
    if decision_side is None and candidate is not None:
        decision_side = candidate.side

    row: dict[str, Any] = {
        "state": state,
        "reason": cycle.reason,
        "would_enter": bool(would_enter),
        "candidate": None,
        "size": None,
        "decision": {},
    }
    if candidate is not None:
        row["candidate"] = {
            "side": candidate.side,
            "score": candidate.score,
            "alpha_id": getattr(candidate, "alpha_id", None),
            "entry_price": candidate.entry_price,
        }
    if size is not None:
        row["size"] = {
            "qty": size.qty,
        }
    if base_decision or candidate is not None:
        row["decision"] = {
            "side": decision_side,
            "score": base_decision.get("score"),
            "alpha_id": base_decision.get("alpha_id"),
            "entry_tier": base_decision.get("entry_tier"),
            "regime": base_decision.get("regime"),
            "entry_price": entry_price,
            "alpha_blocks": base_decision.get("alpha_blocks"),
            "risk_per_trade_pct": base_decision.get("risk_per_trade_pct"),
            "max_effective_leverage": base_decision.get("max_effective_leverage"),
            "reverse_exit_min_r": base_decision.get("reverse_exit_min_r"),
            "sl_tp": sl_tp,
            "execution": base_decision.get("execution"),
        }

    return row, state


def _build_local_backtest_portfolio_rows(
    *,
    cfg: EffectiveConfig,
    candles_by_symbol: dict[str, dict[str, list[_Kline15m]]],
    premium_by_symbol: dict[str, list[_Kline15m]] | None = None,
    funding_by_symbol: dict[str, list[_FundingRateRow]] | None = None,
    market_intervals: list[str],
    strategy_runtime_params: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    provider = _HistoricalPortfolioSnapshotProvider(
        candles_by_symbol=candles_by_symbol,
        premium_by_symbol=premium_by_symbol,
        funding_by_symbol=funding_by_symbol,
        market_intervals=market_intervals,
        history_limit=_portfolio_history_limit(strategy_runtime_params),
    )
    strategy = get_build_strategy_selector()(
        behavior=cfg.behavior,
        snapshot_provider=provider,
        overheat_fetcher=None,
        journal_logger=None,
    )
    runtime_updater = getattr(strategy, "set_runtime_params", None)
    if callable(runtime_updater) and strategy_runtime_params:
        runtime_updater(**strategy_runtime_params)

    symbols = sorted(
        str(symbol).strip().upper() for symbol in candles_by_symbol.keys() if str(symbol).strip()
    )

    def _candidate_from_decision(symbol: str, decision: dict[str, Any]) -> Candidate | None:
        intent = str(decision.get("intent") or "NONE").upper()
        side = str(decision.get("side") or "NONE").upper()
        if intent not in {"LONG", "SHORT"} or side not in {"BUY", "SELL"}:
            return None

        score = _to_float(decision.get("score")) or 0.0
        if score <= 0.0:
            return None

        indicators = decision.get("indicators")
        atr_hint = None
        spread_pct = None
        if isinstance(indicators, dict):
            atr_hint = _to_float(indicators.get("atr14_15m"))
            spread_bps = _to_float(indicators.get("spread_estimate_bps"))
            if spread_bps is not None:
                spread_pct = float(spread_bps) / 100.0

        return Candidate(
            symbol=symbol,
            side="BUY" if side == "BUY" else "SELL",
            score=float(score),
            raw_score=_to_float(decision.get("raw_score")),
            portfolio_score=_to_float(decision.get("portfolio_score")),
            portfolio_bucket=str(decision.get("portfolio_bucket") or "").strip() or None,
            alpha_id=str(decision.get("alpha_id") or "").strip() or None,
            entry_family=str(decision.get("entry_family") or "").strip() or None,
            reason=str(decision.get("reason") or "entry_signal"),
            source=str(getattr(strategy, "name", "strategy")),
            entry_price=_to_float(decision.get("entry_price")),
            stop_price_hint=_to_float(decision.get("stop_price_hint")),
            stop_distance_frac=_to_float(decision.get("stop_distance_frac")),
            volatility_hint=atr_hint,
            regime_hint=str(decision.get("regime") or "").strip().upper() or None,
            regime_strength=_to_float(decision.get("regime_strength")),
            risk_per_trade_pct=_to_float(decision.get("risk_per_trade_pct")),
            max_effective_leverage=_to_float(
                decision.get("max_effective_leverage")
            ),
            expected_move_frac=_to_float(decision.get("expected_move_frac")),
            required_move_frac=_to_float(decision.get("required_move_frac")),
            spread_pct=spread_pct,
        )

    def _no_candidate_reason(skipped: dict[str, str]) -> str:
        if not skipped:
            return "no_candidate"
        ordered = sorted((symbol, str(reason or "no_candidate")) for symbol, reason in skipped.items())
        reasons = sorted({reason for _, reason in ordered})
        if len(reasons) == 1:
            return reasons[0]
        snippet = ";".join(f"{symbol}:{reason}" for symbol, reason in ordered[:3])
        return f"no_candidate_multi:{snippet}"

    rows: list[dict[str, Any]] = []
    state_counter: Counter[str] = Counter()
    total_ticks = len(provider)
    replay_started_at = time.monotonic()
    last_progress_pct = -5

    for tick in range(total_ticks):
        snapshot = provider()
        if not snapshot:
            continue
        symbols_market = snapshot.get("symbols")
        decisions: dict[str, dict[str, Any]] = {}
        ranked: list[Candidate] = []
        skipped: dict[str, str] = {}

        for symbol in symbols:
            symbol_snapshot = dict(snapshot)
            if isinstance(symbols_market, dict):
                market = symbols_market.get(symbol)
                if isinstance(market, dict):
                    symbol_snapshot["market"] = market
            symbol_snapshot["symbol"] = symbol

            decision = strategy.decide(symbol_snapshot) if strategy is not None else {}
            if not isinstance(decision, dict):
                decision = {}
            decisions[symbol] = _ReplayDecisionBySymbol._compact_payload(decision)

            candidate = _candidate_from_decision(symbol, decision)
            if candidate is not None:
                ranked.append(candidate)
                continue
            skipped[symbol] = str(decision.get("reason") or "no_candidate")

        ranked.sort(
            key=lambda item: (
                float(item.portfolio_score if item.portfolio_score is not None else item.score),
                float(item.score),
            ),
            reverse=True,
        )
        state = "dry_run" if ranked else "no_candidate"
        reason = "would_execute" if ranked else _no_candidate_reason(skipped)
        rows.append(
            {
                "tick": tick,
                "timestamp": snapshot.get("timestamp"),
                "open_time": snapshot.get("open_time"),
                "close_time": snapshot.get("close_time"),
                "state": state,
                "reason": reason,
                "ranked_candidates": [_candidate_to_payload(item) for item in ranked],
                "decisions": decisions,
                "candles": snapshot.get("candles") or {},
            }
        )
        state_counter[state] += 1
        if total_ticks > 0:
            progress_pct = int(((tick + 1) / total_ticks) * 100.0)
            rounded_pct = min((progress_pct // 5) * 5, 100)
            if rounded_pct >= last_progress_pct + 5 or tick + 1 == total_ticks:
                elapsed_sec = int(time.monotonic() - replay_started_at)
                print(
                    "[LOCAL_BACKTEST] portfolio replay "
                    f"{rounded_pct}% ({tick + 1}/{total_ticks}) elapsed={elapsed_sec}s"
                )
                last_progress_pct = rounded_pct
    return rows, state_counter

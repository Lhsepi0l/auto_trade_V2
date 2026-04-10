from __future__ import annotations

import copy
from typing import Any

from v2.kernel.contracts import Candidate, CandidateSelector, KernelContext
from v2.strategies.alpha_shared import _to_float
from v2.strategies.base import StrategyPlugin
from v2.strategies.ra_2026_alpha_v2_evaluators import (
    evaluate_alpha_breakout,
    evaluate_alpha_drift,
    evaluate_alpha_expansion,
    evaluate_alpha_pullback,
)
from v2.strategies.ra_2026_alpha_v2_helpers import (
    _aggregate_block_reason,
    _AlphaEvaluation,
    _breakout_stability_edge_score,
    _build_shared_context,
    _common_cost_reason,
    _common_stop,
    _DriftSetupState,
    _expansion_cost_near_pass_allowed,
    _expansion_quality_score_v2,
    _SharedContext,
    _to_int,
)
from v2.strategies.ra_2026_alpha_v2_params import RA2026AlphaV2Params
from v2.strategies.ra_2026_alpha_v2_payload import (
    _build_entry_payload,
    _decision_none,
)

__all__ = [
    "RA2026AlphaV2",
    "RA2026AlphaV2CandidateSelector",
    "RA2026AlphaV2Params",
    "_SharedContext",
    "_common_cost_reason",
    "_common_stop",
    "_expansion_cost_near_pass_allowed",
    "_expansion_quality_score_v2",
    "_breakout_stability_edge_score",
]


class RA2026AlphaV2(StrategyPlugin):
    name = "ra_2026_alpha_v2"

    def __init__(self, *, params: dict[str, Any] | None = None, logger: Any | None = None) -> None:
        self._cfg = RA2026AlphaV2Params.from_params(params)
        self._logger = logger
        self._drift_setups: dict[str, _DriftSetupState] = {}

    def set_runtime_params(self, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        merged = self._cfg.__dict__.copy()
        merged.update(kwargs)
        self._cfg = RA2026AlphaV2Params.from_params(merged)

    def _alpha_breakout(self, *, ctx: _SharedContext) -> _AlphaEvaluation:
        return evaluate_alpha_breakout(
            ctx=ctx,
            cfg=self._cfg,
        )

    def _alpha_pullback(self, *, ctx: _SharedContext) -> _AlphaEvaluation:
        return evaluate_alpha_pullback(
            ctx=ctx,
            cfg=self._cfg,
        )

    def _alpha_expansion(self, *, ctx: _SharedContext) -> _AlphaEvaluation:
        return evaluate_alpha_expansion(
            ctx=ctx,
            cfg=self._cfg,
        )

    def _alpha_drift(
        self,
        *,
        ctx: _SharedContext,
        open_time_ms: int | None,
    ) -> _AlphaEvaluation:
        return evaluate_alpha_drift(
            ctx=ctx,
            cfg=self._cfg,
            drift_setups=self._drift_setups,
            open_time_ms=open_time_ms,
        )

    def decide(self, market_snapshot: dict[str, Any]) -> dict[str, Any]:
        symbol = str(market_snapshot.get("symbol") or "").strip().upper()
        if symbol not in self._cfg.supported_symbols:
            return _decision_none(
                symbol=symbol or "BTCUSDT",
                reason="unsupported_symbol",
                regime="UNKNOWN",
                allowed_side="NONE",
                indicators={},
            )

        market = market_snapshot.get("market")
        if not isinstance(market, dict):
            return _decision_none(
                symbol=symbol,
                reason="missing_market",
                regime="UNKNOWN",
                allowed_side="NONE",
                indicators={},
            )

        ctx, shared_reason = _build_shared_context(symbol=symbol, market=market, cfg=self._cfg)
        if ctx is None:
            return _decision_none(
                symbol=symbol,
                reason=shared_reason,
                regime="UNKNOWN",
                allowed_side="NONE",
                indicators={},
            )

        evaluations: list[_AlphaEvaluation] = []
        for alpha_id in self._cfg.enabled_alphas:
            if alpha_id == "alpha_breakout":
                evaluations.append(self._alpha_breakout(ctx=ctx))
            elif alpha_id == "alpha_pullback":
                evaluations.append(self._alpha_pullback(ctx=ctx))
            elif alpha_id == "alpha_expansion":
                evaluations.append(self._alpha_expansion(ctx=ctx))
            elif alpha_id == "alpha_drift":
                evaluations.append(
                    self._alpha_drift(
                        ctx=ctx,
                        open_time_ms=_to_int(market_snapshot.get("open_time")),
                    )
                )

        alpha_diagnostics = {
            item.alpha_id: {
                "state": "candidate" if item.payload is not None else "blocked",
                "reason": item.reason,
                "score": float(item.score),
                "metrics": copy.deepcopy(item.diagnostics or {}),
            }
            for item in evaluations
        }
        candidates = [item for item in evaluations if item.payload is not None]
        if candidates:
            best = max(candidates, key=lambda item: float(item.score))
            payload = _build_entry_payload(
                alpha_id=best.alpha_id,
                side=str(best.payload.get("side") or "LONG"),  # type: ignore[arg-type]
                entry_price=float(best.payload["entry_price"]),
                stop_price=float(best.payload["stop_price"]),
                stop_distance_frac=float(best.payload["stop_distance_frac"]),
                score=float(best.score),
                ctx=ctx,
                cfg=self._cfg,
                alpha_diagnostics=alpha_diagnostics,
                entry_meta=best.payload,
            )
            return payload

        final_reason = _aggregate_block_reason([item.reason for item in evaluations])
        allowed_side = ctx.regime_side if ctx.regime_side != "NONE" else ctx.bias_side
        return _decision_none(
            symbol=symbol,
            reason=final_reason,
            regime=ctx.regime,
            allowed_side=allowed_side,
            indicators=dict(ctx.indicators),
            alpha_diagnostics=alpha_diagnostics,
        )


class RA2026AlphaV2CandidateSelector(CandidateSelector):
    def __init__(
        self,
        *,
        strategy: StrategyPlugin,
        symbols: list[str],
        snapshot_provider: Any | None = None,
        overheat_fetcher: Any | None = None,
        journal_logger: Any | None = None,
    ) -> None:
        self._strategy = strategy
        normalized = [str(sym).strip().upper() for sym in symbols if str(sym).strip()]
        self._symbols = normalized or ["BTCUSDT"]
        self._snapshot_provider = snapshot_provider
        self._overheat_fetcher = overheat_fetcher
        self._journal_logger = journal_logger
        self._last_no_candidate_reason: str | None = None
        self._last_no_candidate_context: dict[str, Any] | None = None
        self._sync_strategy_supported_symbols()

    def get_last_no_candidate_reason(self) -> str | None:
        return self._last_no_candidate_reason

    def get_last_no_candidate_context(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._last_no_candidate_context)

    def _sync_strategy_supported_symbols(self) -> None:
        updater = getattr(self._strategy, "set_runtime_params", None)
        if callable(updater):
            updater(supported_symbols=list(self._symbols))

    def set_symbols(self, symbols: list[str]) -> None:
        normalized = [str(sym).strip().upper() for sym in symbols if str(sym).strip()]
        if normalized:
            self._symbols = normalized
            self._sync_strategy_supported_symbols()

    def set_strategy_runtime_params(self, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        updater = getattr(self._strategy, "set_runtime_params", None)
        if callable(updater):
            updater(**kwargs)

    def inspect_symbol_decision(
        self,
        *,
        symbol: str,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        symbol_u = str(symbol).strip().upper()
        if not symbol_u:
            return None
        base_snapshot: dict[str, Any] = {"symbol": symbol_u}
        provided = snapshot if isinstance(snapshot, dict) else None
        if provided is None and self._snapshot_provider is not None:
            candidate_snapshot = self._snapshot_provider()
            if isinstance(candidate_snapshot, dict):
                provided = candidate_snapshot
        if isinstance(provided, dict):
            base_snapshot.update(provided)

        symbols_market = base_snapshot.get("symbols")
        if isinstance(symbols_market, dict):
            market = symbols_market.get(symbol_u)
            if isinstance(market, dict):
                base_snapshot["market"] = market
        base_snapshot["symbol"] = symbol_u

        decision = self._strategy.decide(base_snapshot)
        return copy.deepcopy(decision) if isinstance(decision, dict) else None

    def select(self, *, context: KernelContext) -> Candidate | None:
        _ = context
        self._last_no_candidate_reason = None
        self._last_no_candidate_context = None
        base_snapshot: dict[str, Any] = {"symbol": self._symbols[0]}
        if self._snapshot_provider is not None:
            provided = self._snapshot_provider()
            if isinstance(provided, dict):
                base_snapshot.update(provided)

        symbols_market = base_snapshot.get("symbols")
        candidates: list[Candidate] = []
        skipped: dict[str, str] = {}
        skipped_context: dict[str, dict[str, Any]] = {}

        for symbol in self._symbols:
            symbol_snapshot = dict(base_snapshot)
            if isinstance(symbols_market, dict):
                market = symbols_market.get(symbol)
                if isinstance(market, dict):
                    symbol_snapshot["market"] = market
            _ = self._overheat_fetcher
            symbol_snapshot["symbol"] = symbol

            decision = self._strategy.decide(symbol_snapshot)
            if self._journal_logger is not None:
                self._journal_logger(decision)

            intent = str(decision.get("intent") or "NONE")
            side = str(decision.get("side") or "NONE")
            if intent not in {"LONG", "SHORT"} or side not in {"BUY", "SELL"}:
                skipped[symbol] = str(decision.get("reason") or "no_entry")
                skipped_context[symbol] = {
                    "reason": str(decision.get("reason") or "no_entry"),
                    "alpha_blocks": copy.deepcopy(decision.get("alpha_blocks") or {}),
                    "alpha_reject_metrics": copy.deepcopy(
                        decision.get("alpha_reject_metrics")
                        or {
                            alpha_id: (meta.get("metrics") or {})
                            for alpha_id, meta in (decision.get("alpha_diagnostics") or {}).items()
                            if isinstance(meta, dict)
                            and str(meta.get("state") or "").strip() != "candidate"
                            and isinstance(meta.get("metrics"), dict)
                            and meta.get("metrics")
                        }
                    ),
                }
                continue

            score = _to_float(decision.get("score")) or 0.0
            if score <= 0.0:
                skipped[symbol] = "non_positive_score"
                continue

            indicators = decision.get("indicators")
            atr_hint = None
            spread_pct = None
            if isinstance(indicators, dict):
                atr_hint = _to_float(indicators.get("atr14_15m"))
                spread_bps = _to_float(indicators.get("spread_estimate_bps"))
                if spread_bps is not None:
                    spread_pct = float(spread_bps) / 100.0

            candidates.append(
                Candidate(
                    symbol=symbol,
                    side="BUY" if side == "BUY" else "SELL",
                    score=float(score),
                    alpha_id=str(decision.get("alpha_id") or "").strip() or None,
                    entry_family=str(decision.get("entry_family") or "").strip() or None,
                    reason=str(decision.get("reason") or "entry_signal"),
                    source=str(getattr(self._strategy, "name", "ra_2026_alpha_v2")),
                    entry_price=_to_float(decision.get("entry_price")),
                    stop_price_hint=_to_float(decision.get("stop_price_hint")),
                    stop_distance_frac=_to_float(decision.get("stop_distance_frac")),
                    volatility_hint=atr_hint,
                    regime_hint=str(decision.get("regime") or "").upper() or None,
                    regime_strength=_to_float(decision.get("regime_strength")),
                    risk_per_trade_pct=_to_float(decision.get("risk_per_trade_pct")),
                    max_effective_leverage=_to_float(decision.get("max_effective_leverage")),
                    expected_move_frac=_to_float(decision.get("expected_move_frac")),
                    required_move_frac=_to_float(decision.get("required_move_frac")),
                    spread_pct=spread_pct,
                    take_profit_hint=_to_float((decision.get("sl_tp") or {}).get("take_profit")),
                    execution_hints=(
                        copy.deepcopy(decision.get("execution"))
                        if isinstance(decision.get("execution"), dict)
                        else None
                    ),
                )
            )

        if candidates:
            return max(candidates, key=lambda item: float(item.score))

        if skipped:
            ordered = sorted(skipped.items())
            reasons = sorted({reason for _, reason in ordered})
            self._last_no_candidate_context = copy.deepcopy(skipped_context)
            if len(reasons) == 1:
                self._last_no_candidate_reason = reasons[0]
            else:
                snippet = ";".join(f"{sym}:{reason}" for sym, reason in ordered[:3])
                self._last_no_candidate_reason = f"no_candidate_multi:{snippet}"
        else:
            self._last_no_candidate_reason = "no_candidate"
        return None

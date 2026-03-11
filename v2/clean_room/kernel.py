from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, cast, runtime_checkable

from v2.clean_room.contracts import (
    Candidate,
    CandidateSelector,
    ExecutionService,
    KernelContext,
    KernelCycleResult,
    PortfolioCycleResult,
    RankedCandidateSelector,
    RiskDecision,
    RiskGate,
    Sizer,
)
from v2.clean_room.defaults import (
    AlwaysAllowedRiskGate,
    BinanceLiveExecutionService,
    FixedNotionalSizer,
    LiveRuntimeRiskGate,
    NoopCandidateSelector,
    ReplaySafeExecutionService,
    RiskAwareSizer,
)
from v2.clean_room.portfolio import (
    PortfolioRoutingConfig,
    route_ranked_candidates,
)
from v2.common.async_bridge import run_async_blocking
from v2.config.loader import BehaviorConfig, RiskConfig
from v2.engine import EngineStateStore
from v2.strategies.base import StrategyPlugin

logger = logging.getLogger(__name__)


@runtime_checkable
class UniverseSymbolsMutableSelector(Protocol):
    def set_symbols(self, symbols: list[str]) -> None: ...


@runtime_checkable
class NoCandidateReasonAwareSelector(Protocol):
    def get_last_no_candidate_reason(self) -> str | None: ...


@runtime_checkable
class LeverageConfigMutableSizer(Protocol):
    def set_leverage_config(
        self,
        *,
        symbol_leverage_map: dict[str, float],
        max_leverage: float,
    ) -> None: ...


@runtime_checkable
class NotionalConfigMutableSizer(Protocol):
    def set_notional_config(
        self,
        *,
        fallback_notional: float,
        max_notional: float | None,
    ) -> None: ...


@runtime_checkable
class StrategyRuntimeMutableSelector(Protocol):
    def set_strategy_runtime_params(self, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class TradeKernelConfig:
    mode: str
    profile: str
    default_symbol: str
    dry_run: bool
    tick: int = 0
    allow_reentry: bool = False
    max_open_positions: int = 1
    max_new_entries_per_tick: int = 1


class TradeKernel:
    def __init__(
        self,
        *,
        state_store: EngineStateStore,
        candidate_selector: CandidateSelector,
        risk_gate: RiskGate,
        sizer: Sizer,
        executor: ExecutionService,
        config: TradeKernelConfig,
        market_data_probe: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._state_store = state_store
        self._selector = candidate_selector
        self._risk_gate = risk_gate
        self._sizer = sizer
        self._executor = executor
        self._config = config
        self._runtime_tick = int(config.tick)
        self._market_data_probe = market_data_probe
        self._runtime_context: dict[str, Any] = {}
        self._last_portfolio_cycle: PortfolioCycleResult | None = None

    def _build_context(self) -> KernelContext:
        runtime = self._runtime_context
        return KernelContext(
            mode=self._config.mode,
            profile=self._config.profile,
            symbol=self._config.default_symbol,
            tick=self._runtime_tick,
            dry_run=self._config.dry_run,
            daily_loss_limit_pct=float(runtime.get("daily_loss_limit_pct"))
            if runtime.get("daily_loss_limit_pct") is not None
            else None,
            dd_limit_pct=float(runtime.get("dd_limit_pct"))
            if runtime.get("dd_limit_pct") is not None
            else None,
            daily_loss_used_pct=float(runtime.get("daily_loss_used_pct"))
            if runtime.get("daily_loss_used_pct") is not None
            else None,
            dd_used_pct=float(runtime.get("dd_used_pct"))
            if runtime.get("dd_used_pct") is not None
            else None,
            lose_streak=int(runtime.get("lose_streak"))
            if runtime.get("lose_streak") is not None
            else None,
            cooldown_until=float(runtime.get("cooldown_until"))
            if runtime.get("cooldown_until") is not None
            else None,
            risk_score_min=float(runtime.get("risk_score_min"))
            if runtime.get("risk_score_min") is not None
            else None,
            spread_max_pct=float(runtime.get("spread_max_pct"))
            if runtime.get("spread_max_pct") is not None
            else None,
            dd_scale_start_pct=float(runtime.get("dd_scale_start_pct"))
            if runtime.get("dd_scale_start_pct") is not None
            else None,
            dd_scale_max_pct=float(runtime.get("dd_scale_max_pct"))
            if runtime.get("dd_scale_max_pct") is not None
            else None,
            dd_scale_min_factor=float(runtime.get("dd_scale_min_factor"))
            if runtime.get("dd_scale_min_factor") is not None
            else None,
            recent_blocks=runtime.get("recent_blocks")
            if isinstance(runtime.get("recent_blocks"), dict)
            else None,
        )

    def _ops_blocked(self, candidate: Candidate | None) -> KernelCycleResult | None:
        state = self._state_store.get()
        if state.operational.paused:
            return KernelCycleResult(
                state="blocked",
                reason="ops_paused",
                candidate=candidate,
                risk=RiskDecision(allow=False, reason="ops_paused"),
            )
        if state.operational.safe_mode:
            return KernelCycleResult(
                state="blocked",
                reason="safe_mode",
                candidate=candidate,
                risk=RiskDecision(allow=False, reason="safe_mode"),
            )
        if not self._config.allow_reentry:
            candidate_symbol = (
                str(candidate.symbol if candidate is not None else "").strip().upper()
            )
            if candidate_symbol and candidate_symbol in {
                str(sym).strip().upper() for sym in state.current_position.keys()
            }:
                return KernelCycleResult(
                    state="blocked",
                    reason="position_open",
                    candidate=candidate,
                    risk=RiskDecision(allow=False, reason="position_open"),
                )
        if candidate is None:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)
        return None

    def _global_ops_blocked(self) -> KernelCycleResult | None:
        state = self._state_store.get()
        if state.operational.paused:
            return KernelCycleResult(
                state="blocked",
                reason="ops_paused",
                candidate=None,
                risk=RiskDecision(allow=False, reason="ops_paused"),
            )
        if state.operational.safe_mode:
            return KernelCycleResult(
                state="blocked",
                reason="safe_mode",
                candidate=None,
                risk=RiskDecision(allow=False, reason="safe_mode"),
            )
        return None

    @staticmethod
    def _blocked_cycle_result(*, candidate: Candidate, reason: str) -> KernelCycleResult:
        return KernelCycleResult(
            state="blocked",
            reason=reason,
            candidate=candidate,
            risk=RiskDecision(allow=False, reason=reason),
        )

    def _rank_candidates(self, *, context: KernelContext) -> list[Candidate]:
        if isinstance(self._selector, RankedCandidateSelector):
            ranked = self._selector.rank(context=context)
            return [item for item in ranked if isinstance(item, Candidate)]
        selected = self._selector.select(context=context)
        return [selected] if selected is not None else []

    def _size_and_execute(
        self, *, candidate: Candidate, context: KernelContext
    ) -> KernelCycleResult:
        risk = self._risk_gate.evaluate(candidate=candidate, context=context)
        if not risk.allow:
            return KernelCycleResult(
                state="risk_rejected",
                reason=risk.reason,
                candidate=candidate,
                risk=risk,
            )

        size = self._sizer.size(candidate=candidate, risk=risk, context=context)
        if size.qty <= 0:
            return KernelCycleResult(
                state="size_invalid",
                reason=str(size.reason or "invalid_size"),
                candidate=candidate,
                risk=risk,
                size=size,
            )

        result = self._executor.execute(candidate=candidate, size=size, context=context)
        if not result.ok:
            return KernelCycleResult(
                state="execution_failed",
                reason=str(result.reason or "execution_failed"),
                candidate=candidate,
                risk=risk,
                size=size,
                execution=result,
            )

        if context.dry_run:
            return KernelCycleResult(
                state="dry_run",
                reason="would_execute",
                candidate=candidate,
                risk=risk,
                size=size,
                execution=result,
            )
        return KernelCycleResult(
            state="executed",
            reason="executed",
            candidate=candidate,
            risk=risk,
            size=size,
            execution=result,
        )

    def run_portfolio_cycle(self) -> PortfolioCycleResult:
        context = self._build_context()
        ranked = self._rank_candidates(context=context)
        if not ranked:
            reason = "no_candidate"
            if isinstance(self._selector, NoCandidateReasonAwareSelector):
                observed = self._selector.get_last_no_candidate_reason()
                if observed:
                    reason = observed
            primary = KernelCycleResult(state="no_candidate", reason=reason, candidate=None)
            out = PortfolioCycleResult(
                primary_result=primary,
                open_position_count=len(self._state_store.get().current_position),
                max_open_positions=max(int(self._config.max_open_positions), 1),
            )
            self._last_portfolio_cycle = out
            return out

        if not isinstance(self._selector, RankedCandidateSelector):
            blocked = self._ops_blocked(candidate=ranked[0])
            if blocked is not None:
                out = PortfolioCycleResult(
                    primary_result=blocked,
                    results=[blocked],
                    blocked_reasons={blocked.reason: 1},
                    open_position_count=len(self._state_store.get().current_position),
                    max_open_positions=max(int(self._config.max_open_positions), 1),
                )
                self._last_portfolio_cycle = out
                return out
            result = self._size_and_execute(candidate=ranked[0], context=context)
            out = PortfolioCycleResult(
                primary_result=result,
                selected_candidates=[ranked[0]] if result.state in {"dry_run", "executed"} else [],
                results=[result],
                open_position_count=len(self._state_store.get().current_position),
                max_open_positions=max(int(self._config.max_open_positions), 1),
            )
            self._last_portfolio_cycle = out
            return out

        blocked = self._global_ops_blocked()
        if blocked is not None:
            out = PortfolioCycleResult(
                primary_result=blocked,
                results=[blocked],
                blocked_reasons={blocked.reason: len(ranked)},
                open_position_count=len(self._state_store.get().current_position),
                max_open_positions=max(int(self._config.max_open_positions), 1),
            )
            self._last_portfolio_cycle = out
            return out

        state = self._state_store.get()
        open_symbols = {
            str(sym).strip().upper()
            for sym in state.current_position.keys()
            if str(sym).strip()
        }
        routing = route_ranked_candidates(
            candidates=ranked,
            open_symbols=open_symbols,
            allow_reentry=self._config.allow_reentry,
            config=PortfolioRoutingConfig(
                max_open_positions=int(self._config.max_open_positions),
                max_new_entries_per_tick=int(self._config.max_new_entries_per_tick),
            ),
        )

        results: list[KernelCycleResult] = []
        selected_candidates: list[Candidate] = []
        for candidate in routing.selected:
            cycle_result = self._size_and_execute(candidate=candidate, context=context)
            results.append(cycle_result)
            if cycle_result.state in {"dry_run", "executed"}:
                selected_candidates.append(candidate)
        for candidate, reason in routing.blocked:
            results.append(self._blocked_cycle_result(candidate=candidate, reason=reason))

        primary = next(
            (item for item in results if item.state in {"dry_run", "executed"}),
            None,
        )
        if primary is None and results:
            primary = results[0]
        if primary is None:
            primary = KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)
        out = PortfolioCycleResult(
            primary_result=primary,
            selected_candidates=selected_candidates,
            results=results,
            blocked_reasons=routing.blocked_reasons,
            open_position_count=routing.open_position_count,
            max_open_positions=routing.max_open_positions,
        )
        self._last_portfolio_cycle = out
        return out

    def run_once(self) -> KernelCycleResult:
        try:
            return self.run_portfolio_cycle().primary_result
        finally:
            self._runtime_tick += 1

    def last_portfolio_cycle(self) -> PortfolioCycleResult | None:
        return self._last_portfolio_cycle

    def reconcile(self) -> dict[str, Any]:
        state = self._state_store.replay_from_journal()
        return {
            "status": state.status,
            "open_orders": list(state.open_orders.keys()),
            "positions": list(state.current_position.keys()),
            "fills": len(state.last_fills),
        }

    def set_universe_symbols(self, symbols: list[str]) -> None:
        if isinstance(self._selector, UniverseSymbolsMutableSelector):
            self._selector.set_symbols(symbols)

    def set_symbol_leverage_map(self, mapping: dict[str, float], *, max_leverage: float) -> None:
        if isinstance(self._sizer, LeverageConfigMutableSizer):
            self._sizer.set_leverage_config(
                symbol_leverage_map=mapping,
                max_leverage=max_leverage,
            )

    def set_notional_config(
        self,
        *,
        fallback_notional: float,
        max_notional: float | None,
    ) -> None:
        if isinstance(self._sizer, NotionalConfigMutableSizer):
            self._sizer.set_notional_config(
                fallback_notional=fallback_notional,
                max_notional=max_notional,
            )

    def set_strategy_runtime_params(self, **kwargs: Any) -> None:
        if isinstance(self._selector, StrategyRuntimeMutableSelector):
            self._selector.set_strategy_runtime_params(**kwargs)

    def set_runtime_context(self, **kwargs: Any) -> None:
        self._runtime_context = dict(kwargs)

    def set_tick(self, tick: int) -> None:
        self._runtime_tick = int(tick)

    def probe_market_data(self) -> dict[str, Any] | None:
        if self._market_data_probe is None:
            return None
        return self._market_data_probe()


def _build_default_risk_gate(risk_cfg: RiskConfig | None = None) -> RiskGate:
    if risk_cfg is None:
        return AlwaysAllowedRiskGate()
    return LiveRuntimeRiskGate(
        daily_loss_limit_pct=float(getattr(risk_cfg, "daily_loss_limit_pct", -0.02) or -0.02),
        dd_limit_pct=float(getattr(risk_cfg, "dd_limit_pct", -0.15) or -0.15),
    )


def _build_default_sizer(behavior: BehaviorConfig | None = None) -> Sizer:
    if behavior is None:
        return FixedNotionalSizer(fallback_notional=10.0, leverage=1.0)
    default_leverage = float(getattr(behavior.risk, "max_leverage", 1.0) or 1.0)
    return RiskAwareSizer(fallback_notional=10.0, default_leverage=default_leverage)


def _run_async_blocking(thunk: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return run_async_blocking(lambda: cast(Coroutine[Any, Any, Any], thunk()))


def _build_market_snapshot_provider(
    *,
    rest_client: Any,
    symbols: list[str],
    behavior: BehaviorConfig | None = None,
) -> Callable[[], dict[str, Any]] | None:
    if rest_client is None:
        return None

    symbol_list = [str(sym).upper() for sym in symbols if str(sym).strip()]
    if not symbol_list:
        return None
    primary_symbol = symbol_list[0]
    intervals = ["10m", "15m", "30m", "1h", "4h"]
    if behavior is not None and getattr(behavior, "exchange", None) is not None:
        configured = getattr(behavior.exchange, "market_intervals", None)
        if isinstance(configured, list) and configured:
            normalized = []
            for value in configured:
                interval = str(value).strip()
                if interval:
                    normalized.append(interval)
            if normalized:
                intervals = normalized
    seen_intervals: set[str] = set()
    ordered_intervals: list[str] = []
    for interval in intervals:
        if interval in seen_intervals:
            continue
        seen_intervals.add(interval)
        ordered_intervals.append(interval)
    intervals = ordered_intervals
    cache: dict[str, dict[str, Any]] = {
        sym: {interval: [] for interval in intervals} for sym in symbol_list
    }
    cache_updated_at: datetime | None = None

    def _provider() -> dict[str, Any]:
        nonlocal cache, cache_updated_at

        now = datetime.now(timezone.utc)
        if cache_updated_at is None or (now - cache_updated_at).total_seconds() >= 10:
            for sym in symbol_list:
                for interval in intervals:
                    try:
                        payload = _run_async_blocking(
                            lambda sym=sym, interval=interval: rest_client.public_request(
                                "GET",
                                "/fapi/v1/klines",
                                params={"symbol": sym, "interval": interval, "limit": 260},
                            )
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "market_snapshot_fetch_failed interval=%s symbol=%s",
                            interval,
                            sym,
                        )
                        payload = []
                    if isinstance(payload, list):
                        cache[sym][interval] = [
                            row for row in payload if isinstance(row, (list, tuple, dict))
                        ]
                    else:
                        cache[sym][interval] = []
                try:
                    book_ticker = _run_async_blocking(
                        lambda sym=sym: rest_client.public_request(
                            "GET",
                            "/fapi/v1/ticker/bookTicker",
                            params={"symbol": sym},
                        )
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("book_ticker_fetch_failed symbol=%s", sym)
                    book_ticker = None
                cache[sym]["book_ticker"] = book_ticker if isinstance(book_ticker, dict) else None
            cache_updated_at = now

        symbols_payload = {
            sym: {
                interval: cache.get(sym, {}).get(interval, []) for interval in intervals
            }
            for sym in symbol_list
        }
        for sym in symbol_list:
            symbols_payload[sym]["book_ticker"] = cache.get(sym, {}).get("book_ticker")
        primary_market = symbols_payload[primary_symbol]
        return {
            "symbol": primary_symbol,
            "market": {interval: primary_market.get(interval, []) for interval in intervals},
            "symbols": symbols_payload,
        }

    return _provider


def _build_overheat_fetcher(
    *,
    rest_client: Any,
    symbol: str,
) -> Callable[[str], tuple[float, float] | None] | None:
    if rest_client is None:
        return None

    cache_by_symbol: dict[str, tuple[datetime, float, float]] = {}
    fallback_symbol = str(symbol).upper().strip()

    def _fetch(requested_symbol: str) -> tuple[float, float] | None:
        nonlocal cache_by_symbol

        target_symbol = str(requested_symbol).upper().strip() or fallback_symbol
        if not target_symbol:
            return None

        now = datetime.now(timezone.utc)
        cached = cache_by_symbol.get(target_symbol)
        if cached is not None and (now - cached[0]).total_seconds() < 30:
            return cached[1], cached[2]

        try:
            payload_funding = _run_async_blocking(
                lambda: rest_client.public_request(
                    "GET",
                    "/fapi/v1/premiumIndex",
                    params={"symbol": target_symbol},
                )
            )
            if not isinstance(payload_funding, dict):
                return None

            payload_ratio = _run_async_blocking(
                lambda: rest_client.public_request(
                    "GET",
                    "/fapi/v1/globalLongShortAccountRatio",
                    params={"symbol": target_symbol, "period": "5m", "limit": 1},
                )
            )
            if isinstance(payload_ratio, list):
                payload_ratio = payload_ratio[0] if payload_ratio else {}
            if not isinstance(payload_ratio, dict):
                return None

            raw_funding = payload_funding.get("lastFundingRate")
            raw_ratio = payload_ratio.get("longShortRatio")
            try:
                funding = float(raw_funding) if raw_funding is not None else None
                ratio = float(raw_ratio) if raw_ratio is not None else None
            except (TypeError, ValueError):
                return None

            if funding is None or ratio is None:
                return None

            cache_by_symbol[target_symbol] = (now, funding, ratio)
            return funding, ratio
        except Exception:  # noqa: BLE001
            logger.exception("overheat_fetch_failed symbol=%s", target_symbol)
            return None

    return _fetch


def _build_strategy_selector(
    *,
    behavior: BehaviorConfig,
    snapshot_provider: Callable[[], dict[str, Any]] | None,
    overheat_fetcher: Callable[[str], tuple[float, float] | None] | None,
    journal_logger: Callable[[dict[str, Any]], None] | None,
) -> StrategyPlugin | None:
    enabled_entries = [entry for entry in behavior.strategies if bool(entry.enabled)]
    if not enabled_entries:
        return None
    if len(enabled_entries) != 1:
        raise ValueError("unsupported_strategy:multiple_enabled_strategies")

    entry = enabled_entries[0]
    strategy_name = str(entry.name).strip()
    if strategy_name == "ra_2026_alpha_v2":
        from v2.strategies.ra_2026_alpha_v2 import RA2026AlphaV2 as StrategyImpl
    else:
        raise ValueError(f"unsupported_strategy:{strategy_name}")

    _ = snapshot_provider
    _ = overheat_fetcher
    return StrategyImpl(
        params=entry.params,
        logger=journal_logger,
    )


def build_default_kernel(
    *,
    state_store: EngineStateStore,
    behavior: BehaviorConfig,
    profile: str,
    mode: str,
    tick: int = 0,
    dry_run: bool = True,
    rest_client: Any | None = None,
    universe_symbols: list[str] | None = None,
    symbol_leverage_map: dict[str, float] | None = None,
    max_leverage: float | None = None,
    snapshot_provider: Callable[[], dict[str, Any]] | None = None,
    market_data_observer: Callable[[dict[str, Any]], None] | None = None,
    overheat_fetcher: Callable[[str], tuple[float, float] | None] | None = None,
    journal_logger: Callable[[dict[str, Any]], None] | None = None,
) -> TradeKernel:
    if hasattr(behavior, "risk"):
        risk_cfg = behavior.risk
    else:
        risk_cfg = None

    symbols = [
        str(sym).upper()
        for sym in (universe_symbols or [behavior.exchange.default_symbol])
        if str(sym).strip()
    ]
    if not symbols:
        symbols = [behavior.exchange.default_symbol]

    if snapshot_provider is None:
        snapshot_provider = _build_market_snapshot_provider(
            rest_client=rest_client,
            symbols=symbols,
            behavior=behavior,
        )

    if snapshot_provider is not None and market_data_observer is not None:
        raw_snapshot_provider = snapshot_provider

        def _observed_snapshot_provider() -> dict[str, Any]:
            snapshot = raw_snapshot_provider()
            if isinstance(snapshot, dict):
                market_data_observer(snapshot)
            return snapshot

        snapshot_provider = _observed_snapshot_provider

    if overheat_fetcher is None:
        overheat_fetcher = _build_overheat_fetcher(
            rest_client=rest_client,
            symbol=behavior.exchange.default_symbol,
        )

    strategy = _build_strategy_selector(
        behavior=behavior,
        snapshot_provider=snapshot_provider,
        overheat_fetcher=overheat_fetcher,
        journal_logger=journal_logger,
    )

    candidate_selector: CandidateSelector
    if strategy is None:
        candidate_selector = NoopCandidateSelector()
    else:
        strategy_name = str(getattr(strategy, "name", "")).strip()
        if strategy_name == "ra_2026_alpha_v2":
            from v2.strategies.ra_2026_alpha_v2 import (
                RA2026AlphaV2CandidateSelector as SelectorImpl,
            )
        else:
            raise ValueError(f"unsupported_strategy:{strategy_name}")

        candidate_selector = SelectorImpl(
            strategy=strategy,
            symbols=symbols,
            snapshot_provider=snapshot_provider,
            overheat_fetcher=overheat_fetcher,
            journal_logger=journal_logger,
        )

    if max_leverage is None:
        max_leverage = float(getattr(behavior.risk, "max_leverage", 1.0) or 1.0)
    sizer = _build_default_sizer(behavior=behavior)
    if isinstance(sizer, LeverageConfigMutableSizer):
        sizer.set_leverage_config(
            symbol_leverage_map=symbol_leverage_map or {},
            max_leverage=max_leverage,
        )

    return TradeKernel(
        state_store=state_store,
        candidate_selector=candidate_selector,
        risk_gate=_build_default_risk_gate(risk_cfg=risk_cfg),
        sizer=sizer,
        executor=(
            ReplaySafeExecutionService(enabled=True)
            if dry_run or rest_client is None
            else BinanceLiveExecutionService(
                rest_client=rest_client,
                storage=state_store.runtime_storage(),
            )
        ),
        config=TradeKernelConfig(
            mode=mode,
            profile=profile,
            default_symbol=behavior.exchange.default_symbol,
            dry_run=dry_run,
            tick=tick,
            allow_reentry=behavior.engine.allow_reentry,
            max_open_positions=max(int(getattr(behavior.engine, "max_open_positions", 1) or 1), 1),
            max_new_entries_per_tick=max(
                1,
                min(int(getattr(behavior.engine, "max_open_positions", 1) or 1), 2),
            ),
        ),
        market_data_probe=snapshot_provider,
    )

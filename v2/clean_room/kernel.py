from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from v2.clean_room.contracts import (
    Candidate,
    CandidateSelector,
    ExecutionService,
    KernelContext,
    KernelCycleResult,
    RiskDecision,
    RiskGate,
    Sizer,
)
from v2.clean_room.defaults import (
    AlwaysAllowedRiskGate,
    BinanceLiveExecutionService,
    FixedNotionalSizer,
    NoopCandidateSelector,
    ReplaySafeExecutionService,
)
from v2.config.loader import BehaviorConfig, RiskConfig
from v2.engine import EngineStateStore
from v2.strategies.base import StrategyPlugin


@dataclass(frozen=True)
class TradeKernelConfig:
    mode: str
    profile: str
    default_symbol: str
    dry_run: bool
    tick: int = 0
    allow_reentry: bool = False


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
    ) -> None:
        self._state_store = state_store
        self._selector = candidate_selector
        self._risk_gate = risk_gate
        self._sizer = sizer
        self._executor = executor
        self._config = config

    def _build_context(self) -> KernelContext:
        return KernelContext(
            mode=self._config.mode,
            profile=self._config.profile,
            symbol=self._config.default_symbol,
            tick=self._config.tick,
            dry_run=self._config.dry_run,
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
        if (not self._config.allow_reentry) and len(state.current_position) > 0:
            return KernelCycleResult(
                state="blocked",
                reason="position_open",
                candidate=candidate,
                risk=RiskDecision(allow=False, reason="position_open"),
            )
        if candidate is None:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)
        return None

    def _size_and_execute(self, *, candidate: Candidate, context: KernelContext) -> KernelCycleResult:
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

    def run_once(self) -> KernelCycleResult:
        context = self._build_context()
        candidate = self._selector.select(context=context)
        if candidate is None:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

        blocked = self._ops_blocked(candidate=candidate)
        if blocked is not None:
            return blocked
        return self._size_and_execute(candidate=candidate, context=context)

    def reconcile(self) -> dict[str, Any]:
        state = self._state_store.replay_from_journal()
        return {
            "status": state.status,
            "open_orders": list(state.open_orders.keys()),
            "positions": list(state.current_position.keys()),
            "fills": len(state.last_fills),
        }


def _build_default_risk_gate(risk_cfg: RiskConfig | None = None) -> RiskGate:
    _ = risk_cfg
    return AlwaysAllowedRiskGate()


def _build_default_sizer(behavior: BehaviorConfig | None = None) -> Sizer:
    _ = behavior
    return FixedNotionalSizer(fallback_notional=10.0, leverage=1.0)


def _run_async_blocking(thunk: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(cast(Coroutine[Any, Any, Any], thunk()))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, cast(Coroutine[Any, Any, Any], thunk()))
        return future.result()


def _build_market_snapshot_provider(
    *,
    rest_client: Any,
    symbol: str,
) -> Callable[[], dict[str, Any]] | None:
    if rest_client is None:
        return None

    cache: dict[str, list[Any]] = {"4h": [], "1h": [], "15m": []}
    cache_updated_at: datetime | None = None

    def _provider() -> dict[str, Any]:
        nonlocal cache, cache_updated_at

        now = datetime.now(timezone.utc)
        if cache_updated_at is None or (now - cache_updated_at).total_seconds() >= 10:
            for interval in ("4h", "1h", "15m"):
                payload = _run_async_blocking(
                    lambda interval=interval: rest_client.public_request(
                        "GET",
                        "/fapi/v1/klines",
                        params={"symbol": symbol, "interval": interval, "limit": 260},
                    )
                )
                if isinstance(payload, list):
                    cache[interval] = [row for row in payload if isinstance(row, (list, tuple, dict))]
                else:
                    cache[interval] = []
            cache_updated_at = now

        return {
            "symbol": symbol,
            "market": {
                "4h": cache["4h"],
                "1h": cache["1h"],
                "15m": cache["15m"],
            },
        }

    return _provider


def _build_overheat_fetcher(
    *,
    rest_client: Any,
    symbol: str,
) -> Callable[[str], tuple[float, float] | None] | None:
    if rest_client is None:
        return None

    cache: dict[str, float] = {}
    cache_updated_at: datetime | None = None

    def _fetch(_) -> tuple[float, float] | None:
        _ = _
        nonlocal cache, cache_updated_at

        now = datetime.now(timezone.utc)
        if cache_updated_at is not None and (now - cache_updated_at).total_seconds() < 30:
            if "funding" in cache and "ratio" in cache:
                return cache["funding"], cache["ratio"]

        payload_funding = _run_async_blocking(
            lambda: rest_client.public_request(
                "GET",
                "/fapi/v1/premiumIndex",
                params={"symbol": symbol},
            )
        )
        if not isinstance(payload_funding, dict):
            return None

        payload_ratio = _run_async_blocking(
            lambda: rest_client.public_request(
                "GET",
                "/fapi/v1/globalLongShortAccountRatio",
                params={"symbol": symbol, "period": "5m", "limit": 1},
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

        cache = {"funding": funding, "ratio": ratio}
        cache_updated_at = now
        return funding, ratio

    return _fetch


def _build_strategy_selector(
    *,
    behavior: BehaviorConfig,
    snapshot_provider: Callable[[], dict[str, Any]] | None,
    overheat_fetcher: Callable[[str], tuple[float, float] | None] | None,
    journal_logger: Callable[[dict[str, Any]], None] | None,
) -> StrategyPlugin | None:
    for entry in behavior.strategies:
        if not entry.enabled:
            continue
        if entry.name == "strategy_pack_v1":
            from v2.strategies.strategy_pack_v1 import StrategyPackV1

            return StrategyPackV1(params=entry.params, overheat_fetcher=overheat_fetcher, logger=journal_logger)
    return None


def build_default_kernel(
    *,
    state_store: EngineStateStore,
    behavior: BehaviorConfig,
    profile: str,
    mode: str,
    tick: int = 0,
    dry_run: bool = True,
    rest_client: Any | None = None,
    snapshot_provider: Callable[[], dict[str, Any]] | None = None,
    overheat_fetcher: Callable[[str], tuple[float, float] | None] | None = None,
    journal_logger: Callable[[dict[str, Any]], None] | None = None,
) -> TradeKernel:
    if hasattr(behavior, "risk"):
        risk_cfg = behavior.risk
    else:
        risk_cfg = None

    if snapshot_provider is None:
        snapshot_provider = _build_market_snapshot_provider(
            rest_client=rest_client,
            symbol=behavior.exchange.default_symbol,
        )

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
        from v2.strategies.strategy_pack_v1 import StrategyPackV1CandidateSelector

        candidate_selector = StrategyPackV1CandidateSelector(
            strategy=strategy,
            symbol=behavior.exchange.default_symbol,
            snapshot_provider=snapshot_provider,
            journal_logger=journal_logger,
        )

    return TradeKernel(
        state_store=state_store,
        candidate_selector=candidate_selector,
        risk_gate=_build_default_risk_gate(risk_cfg=risk_cfg),
        sizer=_build_default_sizer(behavior=behavior),
        executor=(
            ReplaySafeExecutionService(enabled=True)
            if dry_run or rest_client is None
            else BinanceLiveExecutionService(rest_client=rest_client)
        ),
        config=TradeKernelConfig(
            mode=mode,
            profile=profile,
            default_symbol=behavior.exchange.default_symbol,
            dry_run=dry_run,
            tick=tick,
            allow_reentry=behavior.engine.allow_reentry,
        ),
    )

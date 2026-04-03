from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from v2.config.loader import load_effective_config
from v2.control import build_runtime_controller, create_control_http_app
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore
from v2.exchange import BinanceRESTError
from v2.exchange.types import ResyncSnapshot
from v2.kernel import build_default_kernel
from v2.kernel.contracts import (
    Candidate,
    ExecutionResult,
    KernelContext,
    KernelCycleResult,
    PortfolioCycleResult,
    SizePlan,
)
from v2.notify import NotificationMessage, Notifier
from v2.ops import OpsController
from v2.storage import RuntimeStorage


def _build_app(tmp_path, *, profile: str = "ra_2026_alpha_v2_expansion_verified_q070"):  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile=profile, mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    return create_control_http_app(controller=controller)


def _build_controller(tmp_path, *, profile: str = "ra_2026_alpha_v2_expansion_verified_q070"):  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile=profile, mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_controller.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    return build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )


def _build_live_controller(  # type: ignore[no-untyped-def]
    tmp_path,
    *,
    profile: str = "ra_2026_alpha_v2_expansion_verified_q070",
    rest_client: Any | None,
    kernel: Any | None = None,
    user_stream_manager: Any | None = None,
    market_data_state: dict[str, Any] | None = None,
    runtime_lock_active: bool = True,
    dirty_restart_detected: bool = False,
):
    cfg = load_effective_config(
        profile=profile,
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_live_controller.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    if kernel is None:
        kernel = build_default_kernel(
            state_store=state_store,
            behavior=cfg.behavior,
            profile=cfg.profile,
            mode=cfg.mode,
            dry_run=False,
            rest_client=rest_client,
        )
    if rest_client is not None and not hasattr(rest_client, "get_open_algo_orders"):
        async def _get_open_algo_orders(*, symbol=None):  # type: ignore[no-untyped-def]
            _ = symbol
            return []

        rest_client.get_open_algo_orders = _get_open_algo_orders  # type: ignore[attr-defined]
    if rest_client is not None and not hasattr(rest_client, "cancel_algo_order"):
        async def _cancel_algo_order(*, params):  # type: ignore[no-untyped-def]
            _ = params
            return {}

        rest_client.cancel_algo_order = _cancel_algo_order  # type: ignore[attr-defined]
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest_client,
        user_stream_manager=user_stream_manager,
        market_data_state=market_data_state,
        runtime_lock_active=runtime_lock_active,
        dirty_restart_detected=dirty_restart_detected,
    )
    return controller, state_store, ops


def test_control_api_contract(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = _build_app(tmp_path)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    assert status.json()["engine_state"]["state"] in {"STOPPED", "PAUSED", "RUNNING", "KILLED"}
    assert "last_alpha_id" in status.json()["pnl"]

    start = client.post("/start")
    assert start.status_code == 200
    assert start.json()["state"] == "RUNNING"

    risk = client.get("/risk")
    assert risk.status_code == 200
    assert "max_leverage" in risk.json()

    set_resp = client.post("/set", json={"key": "margin_budget_usdt", "value": "150"})
    assert set_resp.status_code == 200
    assert set_resp.json()["applied_value"] == 150

    lev = client.post("/symbol-leverage", json={"symbol": "BTCUSDT", "leverage": 7})
    assert lev.status_code == 200
    assert lev.json()["symbol_leverage_map"]["BTCUSDT"] == 7.0

    scheduler = client.get("/scheduler")
    assert scheduler.status_code == 200
    assert scheduler.json()["tick_sec"] >= 1

    interval = client.post("/scheduler/interval", json={"tick_sec": 5})
    assert interval.status_code == 200
    assert interval.json()["tick_sec"] == 5.0

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["ok"] is True

    report = client.post("/report")
    assert report.status_code == 200
    assert report.json()["kind"] == "DAILY_REPORT"
    assert report.json()["notifier_enabled"] is False
    assert report.json()["notifier_sent"] is False
    assert report.json()["notifier_error"] is None

    cooldown = client.post("/cooldown/clear")
    assert cooldown.status_code == 200
    assert cooldown.json()["lose_streak"] == 0

    close = client.post("/trade/close", json={"symbol": "BTCUSDT"})
    assert close.status_code == 200
    assert close.json()["symbol"] == "BTCUSDT"

    close_all = client.post("/trade/close_all")
    assert close_all.status_code == 200
    assert close_all.json()["symbol"] == "ALL"

    panic = client.post("/panic")
    assert panic.status_code == 200
    assert panic.json()["engine_state"]["state"] == "KILLED"

    stop = client.post("/stop")
    assert stop.status_code == 200
    assert stop.json()["state"] == "PAUSED"


def test_verified_q070_profile_seeds_runtime_defaults_and_readiness(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    app = _build_app(tmp_path, profile="ra_2026_alpha_v2_expansion_verified_q070")
    client = TestClient(app)

    risk = client.get("/risk")
    assert risk.status_code == 200
    risk_payload = risk.json()
    assert risk_payload["margin_use_pct"] == 0.1
    assert risk_payload["max_leverage"] == 50.0
    assert risk_payload["risk_score_min"] == 0.6
    assert risk_payload["lose_streak_n"] == 2
    assert risk_payload["cooldown_hours"] == 4
    assert risk_payload["universe_symbols"] == ["BTCUSDT"]
    assert risk_payload["trend_adx_min_4h"] == 14.0
    assert risk_payload["trend_adx_rising_lookback_4h"] == 0
    assert risk_payload["min_volume_ratio_15m"] == 0.8
    assert risk_payload["expansion_buffer_bps"] == 1.5
    assert risk_payload["expansion_quality_score_v2_min"] == 0.7

    readiness = client.get("/readiness")
    assert readiness.status_code == 200
    payload = readiness.json()
    assert payload["target"] == "alpha_expansion_verified_q070"
    assert payload["profile"] == "ra_2026_alpha_v2_expansion_verified_q070"
    assert payload["enabled_symbols"] == ["BTCUSDT"]
    assert payload["overall"] == "caution"
    assert payload["checks"]["profile"]["status"] == "pass"
    assert payload["checks"]["strategy"]["status"] == "pass"
    assert payload["checks"]["symbols"]["status"] == "pass"
    assert payload["checks"]["margin_use_pct"]["status"] == "pass"
    assert payload["checks"]["max_leverage"]["status"] == "pass"
    assert payload["checks"]["max_leverage"]["detail"] == 50.0
    assert payload["checks"]["mode"]["status"] == "warn"

    status = client.get("/status")
    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["live_readiness"]["profile"] == "ra_2026_alpha_v2_expansion_verified_q070"
    assert status_payload["config_summary"]["strategy_runtime"]["trend_adx_min_4h"] == 14.0
    assert status_payload["config_summary"]["strategy_runtime"]["min_volume_ratio_15m"] == 0.8
    assert status_payload["config_summary"]["strategy_runtime"]["squeeze_percentile_threshold"] == 0.35
    assert status_payload["config_summary"]["strategy_runtime"]["expansion_body_ratio_min"] == 0.18
    assert status_payload["config_summary"]["strategy_runtime"]["expansion_close_location_min"] == 0.35
    assert status_payload["config_summary"]["strategy_runtime"]["expansion_quality_score_v2_min"] == 0.7


def test_set_strategy_runtime_values_syncs_kernel_runtime_params(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_verified_q070", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_runtime_params.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    kernel.set_strategy_runtime_params = MagicMock()  # type: ignore[method-assign]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    resp = client.post("/set", json={"key": "trend_enter_adx_4h", "value": "24"})
    assert resp.status_code == 200
    kernel.set_strategy_runtime_params.assert_called()
    kwargs = kernel.set_strategy_runtime_params.call_args.kwargs
    assert kwargs["trend_adx_min_4h"] == 24.0
    assert kwargs["trend_adx_rising_lookback_4h"] == 0
    assert kwargs["breakout_buffer_bps"] == 3.0
    assert kwargs["min_volume_ratio_15m"] == 0.8
    assert kwargs["expansion_buffer_bps"] == 1.5
    assert kwargs["expansion_range_atr_min"] == 0.7
    assert kwargs["expected_move_cost_mult"] == 1.6
    assert kwargs["expansion_quality_score_v2_min"] == 0.7


def test_universe_symbols_runtime_sync_preserves_strategy_supported_symbols(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)

    set_uni = controller.set_value(key="universe_symbols", value="ETHUSDT")
    assert set_uni["applied_value"] == ["ETHUSDT"]

    selector = getattr(controller.kernel, "_selector", None)
    strategy = getattr(selector, "_strategy", None)
    assert getattr(selector, "_symbols", None) == ["ETHUSDT"]
    assert getattr(getattr(strategy, "_cfg", None), "supported_symbols", None) == ("ETHUSDT",)

    controller.set_value(key="trend_enter_adx_4h", value="24")
    assert getattr(getattr(strategy, "_cfg", None), "supported_symbols", None) == ("ETHUSDT",)


def test_legacy_strategy_runtime_defaults_migrate_to_profile_values(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_verified_q070", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_runtime_migrate.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.save_runtime_risk_config(
        config={
            "trend_enter_adx_4h": 22.0,
            "breakout_buffer_bps": 8.0,
            "min_volume_ratio_15m": 1.2,
        }
    )
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    risk = controller.get_risk()
    assert risk["trend_adx_min_4h"] == 14.0
    assert risk["breakout_buffer_bps"] == 3.0
    assert risk["min_volume_ratio_15m"] == 0.8

    persisted = state_store.load_runtime_risk_config()
    assert "trend_adx_min_4h" not in persisted
    assert "breakout_buffer_bps" not in persisted
    assert "min_volume_ratio_15m" not in persisted


def test_strategy_runtime_profile_change_resets_to_current_profile_defaults(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_runtime_profile_reset.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.save_runtime_risk_config(
        config={
            "squeeze_percentile_threshold": 0.35,
            "expansion_quality_score_v2_min": 0.70,
            "min_volume_ratio_15m": 0.9,
        }
    )
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    expected_quality_floor = cfg.behavior.strategies[0].params["expansion_quality_score_v2_min"]
    risk = controller.get_risk()
    assert risk["squeeze_percentile_threshold"] == 0.3
    assert risk["expansion_quality_score_v2_min"] == expected_quality_floor
    assert risk["min_volume_ratio_15m"] == 0.9

    persisted = state_store.load_runtime_risk_config()
    assert "squeeze_percentile_threshold" not in persisted
    assert "expansion_quality_score_v2_min" not in persisted
    assert "min_volume_ratio_15m" not in persisted


def test_strategy_runtime_same_profile_drops_persisted_override(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_runtime_profile_preserve.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.save_runtime_risk_config(
        config={
            "squeeze_percentile_threshold": 0.28,
            "min_volume_ratio_15m": 0.95,
        }
    )
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    risk = controller.get_risk()
    assert risk["squeeze_percentile_threshold"] == 0.3
    assert risk["min_volume_ratio_15m"] == 0.9

    persisted = state_store.load_runtime_risk_config()
    assert "squeeze_percentile_threshold" not in persisted
    assert "min_volume_ratio_15m" not in persisted


def test_set_value_strategy_runtime_override_is_runtime_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path, profile="ra_2026_alpha_v2_expansion_live_candidate")

    result = controller.set_value(key="min_volume_ratio_15m", value="0.95")
    assert result["applied_value"] == 0.95
    assert controller.get_risk()["min_volume_ratio_15m"] == 0.95

    persisted = controller.state_store.load_runtime_risk_config()
    assert "min_volume_ratio_15m" not in persisted

    rebuilt = _build_controller(tmp_path, profile="ra_2026_alpha_v2_expansion_live_candidate")
    assert rebuilt.get_risk()["min_volume_ratio_15m"] == 0.9


def test_q070_profile_owned_risk_overrides_are_runtime_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_verified_q070", mode="shadow", env="testnet", env_map={})
    sqlite_path = str(tmp_path / "control_q070_runtime_only.sqlite3")
    cfg.behavior.storage.sqlite_path = sqlite_path

    storage1 = RuntimeStorage(sqlite_path=sqlite_path)
    storage1.ensure_schema()
    state_store1 = EngineStateStore(storage=storage1, mode=cfg.mode)
    event_bus1 = EventBus()
    scheduler1 = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus1)
    ops1 = OpsController(state_store=state_store1, exchange=None)
    kernel1 = build_default_kernel(
        state_store=state_store1,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller1 = build_runtime_controller(
        cfg=cfg,
        state_store=state_store1,
        ops=ops1,
        kernel=kernel1,
        scheduler=scheduler1,
        event_bus=event_bus1,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    app1 = create_control_http_app(controller=controller1)
    client1 = TestClient(app1)

    assert client1.post("/set", json={"key": "max_leverage", "value": "35"}).status_code == 200
    assert client1.post("/set", json={"key": "margin_use_pct", "value": "0.2"}).status_code == 200
    assert client1.post("/set", json={"key": "universe_symbols", "value": "BTCUSDT,ETHUSDT"}).status_code == 200

    runtime_payload = client1.get("/risk").json()
    assert runtime_payload["max_leverage"] == 35.0
    assert runtime_payload["margin_use_pct"] == 0.2
    assert runtime_payload["universe_symbols"] == ["BTCUSDT", "ETHUSDT"]

    persisted = state_store1.load_runtime_risk_config()
    assert "max_leverage" not in persisted
    assert "margin_use_pct" not in persisted
    assert "universe_symbols" not in persisted

    storage2 = RuntimeStorage(sqlite_path=sqlite_path)
    storage2.ensure_schema()
    state_store2 = EngineStateStore(storage=storage2, mode=cfg.mode)
    event_bus2 = EventBus()
    scheduler2 = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus2)
    ops2 = OpsController(state_store=state_store2, exchange=None)
    kernel2 = build_default_kernel(
        state_store=state_store2,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller2 = build_runtime_controller(
        cfg=cfg,
        state_store=state_store2,
        ops=ops2,
        kernel=kernel2,
        scheduler=scheduler2,
        event_bus=event_bus2,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    payload = create_control_http_app(controller=controller2)
    risk = TestClient(payload).get("/risk").json()
    assert risk["max_leverage"] == 50.0
    assert risk["margin_use_pct"] == 0.1
    assert risk["universe_symbols"] == ["BTCUSDT"]


def test_derived_risk_state_is_not_restored_from_persistence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_runtime_derived.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.save_runtime_risk_config(
        config={
            "dd_lock": True,
            "dd_used_pct": 0.21,
            "last_auto_risk_reason": "drawdown_limit",
            "last_auto_risk_at": "2026-03-14T00:00:00+00:00",
            "last_block_reason": "ops_paused",
            "last_strategy_block_reason": "volume_missing",
            "runtime_equity_peak_usdt": 100.0,
            "runtime_equity_now_usdt": 80.0,
            "recent_blocks": {"ops_paused": 3},
        }
    )
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    risk = controller.get_risk()
    assert risk["dd_lock"] is False
    assert risk["dd_used_pct"] == 0.0
    assert risk["last_auto_risk_reason"] is None
    assert risk.get("last_block_reason") is None
    assert risk.get("last_strategy_block_reason") is None
    assert risk.get("recent_blocks", {}) == {}

    persisted = state_store.load_runtime_risk_config()
    assert "dd_lock" not in persisted
    assert "dd_used_pct" not in persisted
    assert "last_auto_risk_reason" not in persisted
    assert "last_block_reason" not in persisted
    assert "last_strategy_block_reason" not in persisted
    assert "runtime_equity_peak_usdt" not in persisted


def test_live_drawdown_uses_wallet_equity_not_margin_budget(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _BalanceREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "970.0", "walletBalance": "1000.0"}]

    controller, state_store, _ops = _build_live_controller(
        tmp_path,
        profile="ra_2026_alpha_v2_expansion_verified_q070",
        rest_client=_BalanceREST(),
    )
    controller._risk["margin_budget_usdt"] = 35.0
    controller._risk["margin_use_pct"] = 0.1
    state_store.startup_reconcile(
        snapshot=ResyncSnapshot(
            positions=[
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.01",
                    "entryPrice": "100000",
                    "unRealizedProfit": "-20.0",
                }
            ]
        ),
        reason="drawdown_wallet_basis",
    )

    risk = controller.get_risk()

    assert risk["dd_lock"] is False
    assert risk["dd_used_pct"] == 0.02


def test_live_drawdown_is_suppressed_when_wallet_equity_basis_is_unavailable(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    class _FailBalanceREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            raise RuntimeError("balance_down")

    controller, state_store, _ops = _build_live_controller(
        tmp_path,
        profile="ra_2026_alpha_v2_expansion_verified_q070",
        rest_client=_FailBalanceREST(),
    )
    controller._risk["margin_budget_usdt"] = 35.0
    controller._risk["margin_use_pct"] = 0.1
    state_store.startup_reconcile(
        snapshot=ResyncSnapshot(
            positions=[
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.01",
                    "entryPrice": "100000",
                    "unRealizedProfit": "-20.0",
                }
            ]
        ),
        reason="drawdown_missing_wallet_basis",
    )

    risk = controller.get_risk()

    assert risk["dd_lock"] is False
    assert risk["dd_used_pct"] == 0.0


def test_set_value_waits_for_controller_lock(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)

    entered = threading.Event()
    finished = threading.Event()

    def _worker() -> None:
        entered.set()
        controller.set_value(key="breakout_buffer_bps", value="11")
        finished.set()

    controller._lock.acquire()
    t = threading.Thread(target=_worker)
    try:
        t.start()
        assert entered.wait(timeout=1.0)
        assert not finished.wait(timeout=0.1)
    finally:
        controller._lock.release()

    t.join(timeout=1.0)
    assert finished.is_set()
    assert controller.get_risk().get("breakout_buffer_bps") == 11


def test_control_api_tick_emits_status_notification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    notifier = Notifier(enabled=True)
    notifier.send = MagicMock()  # type: ignore[method-assign]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    _ = client.post("/set", json={"key": "notify_interval_sec", "value": "1"})
    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["ok"] is True
    assert notifier.send.call_count >= 1


def test_control_api_report_reflects_notifier_send_result(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_report_notify.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    notifier = Notifier(
        enabled=True,
        provider="ntfy",
        ntfy_topic="ops-alerts",
    )
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=False, error="ConnectError: boom")
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    report = client.post("/report")
    assert report.status_code == 200
    payload = report.json()
    assert payload["notifier_enabled"] is True
    assert payload["notifier_sent"] is False
    assert payload["notifier_error"] == "ConnectError: boom"


def test_control_api_start_emits_ntfy_engine_start_notification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_ntfy_start.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    notifier.send_notification.reset_mock()

    out = controller.start()

    assert out["state"] == "RUNNING"
    assert notifier.send_notification.call_count == 1
    notification = notifier.send_notification.call_args[0][0]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "엔진 시작"
    assert "모의 | testnet" in notification.body
    assert cfg.profile not in notification.body
    controller.stop(emit_event=False)


def test_control_api_manual_tick_emits_attention_notification_for_blocked_result(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_ntfy_manual_tick.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelBlocked:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="blocked",
                reason="confidence_below_threshold",
                candidate=None,
            )

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelBlocked(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    notifier.send_notification.reset_mock()

    out = controller.tick_scheduler_now()

    assert out["ok"] is True
    assert out["snapshot"]["last_action"] == "blocked"
    assert notifier.send_notification.call_count == 1
    notification = notifier.send_notification.call_args[0][0]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "즉시 판단 보류"
    assert "최소 신호 점수 미달" in notification.body


def test_control_api_scheduler_tick_emits_no_candidate_notification_for_ntfy(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_ntfy_scheduler_tick.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoCandidate:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="volume_missing",
                candidate=None,
            )

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoCandidate(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    notifier.send_notification.reset_mock()

    out = controller.tick_scheduler_now()

    assert out["ok"] is True
    assert out["snapshot"]["last_action"] == "no_candidate"
    assert notifier.send_notification.call_count == 1
    notification = notifier.send_notification.call_args[0][0]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "즉시 판단 대기"
    assert "거래량 조건 미충족" in notification.body


def test_control_api_runtime_cycle_emits_scheduler_no_candidate_notification_for_ntfy(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_ntfy_runtime_cycle.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoCandidate:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="volume_missing",
                candidate=None,
            )

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoCandidate(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    notifier.send_notification.reset_mock()

    out = controller._run_cycle_once_locked(trigger_source="scheduler")

    assert out["ok"] is True
    assert out["snapshot"]["last_action"] == "no_candidate"
    assert notifier.send_notification.call_count == 1
    notification = notifier.send_notification.call_args[0][0]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "실시간 판단 대기"
    assert "거래량 조건 미충족" in notification.body


def test_control_api_runtime_cycle_emits_entry_opened_notification_for_executed_result(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_ntfy_entry_open.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecuted:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="BTCUSDT",
                    side="BUY",
                    score=1.0,
                    entry_price=100000.0,
                ),
                size=SizePlan(
                    symbol="BTCUSDT",
                    qty=0.01,
                    leverage=3.0,
                    notional=1000.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(ok=True, order_id="oid-1", reason="live_order_submitted"),
            )

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecuted(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    notifier.send_notification.reset_mock()

    out = controller._run_cycle_once_locked(trigger_source="scheduler")

    assert out["ok"] is True
    assert out["snapshot"]["last_action"] == "executed"
    assert notifier.send_notification.call_count == 1
    notification = notifier.send_notification.call_args[0][0]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "BTCUSDT LONG 진입"
    assert "주문 실행 완료" in notification.body
    assert "qty=0.010000" in notification.body
    assert "lev=3.0" in notification.body


def test_control_api_runtime_cycle_emits_clear_order_failure_notification(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_ntfy_order_failure.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecutionFailed:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="execution_failed",
                reason="live_order_failed:BinanceRESTError:-2019",
                candidate=Candidate(
                    symbol="BTCUSDT",
                    side="BUY",
                    score=1.0,
                    entry_price=100000.0,
                ),
            )

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecutionFailed(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    notifier.send_notification.reset_mock()

    out = controller._run_cycle_once_locked(trigger_source="scheduler")

    assert out["ok"] is True
    assert out["snapshot"]["last_action"] == "execution_failed"
    assert notifier.send_notification.call_count == 1
    notification = notifier.send_notification.call_args[0][0]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "실시간 진입 실패"
    assert "진입 시도: BTCUSDT | LONG" in notification.body
    assert "가용 마진 부족" in notification.body


def test_scheduler_cycle_notification_respects_notify_interval_window(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_ntfy_scheduler_window.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    class _KernelNoCandidate:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="volume_missing",
                candidate=None,
            )

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoCandidate(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    controller._risk["notify_interval_sec"] = 600
    notifier.send_notification.reset_mock()

    first = controller._run_cycle_once_locked(trigger_source="scheduler")
    second = controller._run_cycle_once_locked(trigger_source="scheduler")

    assert first["ok"] is True
    assert second["ok"] is True
    assert notifier.send_notification.call_count == 1
    events = controller.state_store.runtime_storage().list_operator_events(limit=20)
    cycle_events = [row for row in events if row.get("event_type") == "cycle_result"]
    assert len(cycle_events) == 1


def test_control_api_stale_transition_emits_ntfy_attention_notification(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller.notifier = notifier
    notifier.send_notification.reset_mock()

    controller._log_event(
        "stale_transition",
        stale_type="market_data",
        stale=True,
        age_sec=95.0,
    )

    assert notifier.send_notification.call_count == 1
    notification = notifier.send_notification.call_args[0][0]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "시장 데이터 지연"
    assert "갱신 지연 감지" in notification.body


def test_control_api_scheduler_position_open_block_emits_deduped_ntfy_heartbeat(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_ntfy_position_open.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelBlocked:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="blocked",
                reason="position_open",
                candidate=None,
            )

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelBlocked(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    notifier.send_notification.reset_mock()

    out = controller._run_cycle_once_locked(trigger_source="scheduler")

    assert out["ok"] is True
    assert out["snapshot"]["last_action"] == "blocked"
    assert notifier.send_notification.call_count == 1
    notification = notifier.send_notification.call_args[0][0]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "포지션 관리중"
    assert "기존 포지션 관리 중" in notification.body
    assert notification.dedupe_key == "cycle_result:scheduler:position_open"
    assert notification.suppress_window_sec == 900.0


def test_control_api_scheduler_position_open_heartbeat_is_suppressed_within_window(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_ntfy_position_open_dedupe.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelBlocked:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="blocked",
                reason="position_open",
                candidate=None,
            )

    now = {"value": 1000.0}
    monkeypatch.setattr("v2.control.api.time.monotonic", lambda: now["value"])
    monkeypatch.setattr("v2.notify.notifier.time.monotonic", lambda: now["value"])

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier._send_ntfy = MagicMock()  # type: ignore[method-assign]
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelBlocked(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    controller._risk["notify_interval_sec"] = 1
    notifier._send_ntfy.reset_mock()

    first = controller._run_cycle_once_locked(trigger_source="scheduler")
    now["value"] = 1002.0
    second = controller._run_cycle_once_locked(trigger_source="scheduler")

    assert first["ok"] is True
    assert second["ok"] is True
    assert notifier._send_ntfy.call_count == 1
    snapshot = controller._status_snapshot()["notification"]
    assert snapshot["last_status"] == "suppressed"
    assert snapshot["last_title"] == "포지션 관리중"
    assert snapshot["last_dedupe_key"] == "cycle_result:scheduler:position_open"


def test_control_api_stale_transition_suppresses_duplicate_ntfy_alert(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    now = {"value": 100.0}

    monkeypatch.setattr("v2.notify.notifier.time.monotonic", lambda: now["value"])
    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier._send_ntfy = MagicMock()  # type: ignore[method-assign]
    controller.notifier = notifier

    controller._log_event(
        "stale_transition",
        stale_type="market_data",
        stale=True,
        age_sec=95.0,
    )
    now["value"] = 120.0
    controller._log_event(
        "stale_transition",
        stale_type="market_data",
        stale=True,
        age_sec=115.0,
    )

    assert notifier._send_ntfy.call_count == 1
    snapshot = controller._status_snapshot()["notification"]
    assert snapshot["last_status"] == "suppressed"
    assert snapshot["last_event_type"] == "stale_transition"
    assert snapshot["last_suppressed_count"] == 1


def test_control_api_auto_risk_trip_emits_ntfy_notification(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller.notifier = notifier
    notifier.send_notification.reset_mock()

    controller._maybe_apply_auto_risk_circuit(
        KernelCycleResult(
            state="blocked",
            reason="daily_loss_limit",
            candidate=None,
        )
    )

    assert notifier.send_notification.call_count == 1
    notification = notifier.send_notification.call_args[0][0]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "자동 리스크 트립"
    assert "일일 손실 제한 도달" in notification.body


def test_control_api_set_notify_interval_emits_immediate_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_set.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    notifier = Notifier(enabled=True)
    notifier.send = MagicMock()  # type: ignore[method-assign]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    resp = client.post("/set", json={"key": "notify_interval_sec", "value": "30"})
    assert resp.status_code == 200
    assert resp.json()["applied_value"] == 30
    assert notifier.send.call_count >= 1


def test_status_loop_emits_while_running_without_cycle(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_loop_running.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    notifier = Notifier(enabled=True)
    notifier.send = MagicMock()  # type: ignore[method-assign]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )

    controller._risk["notify_interval_sec"] = 1
    controller._status_thread_stop.set()
    if controller._status_thread is not None:
        controller._status_thread.join(timeout=1.0)
    controller._status_thread_stop.clear()
    controller._start_status_loop()
    controller._running = True
    controller._last_status_notify_at = datetime.now(timezone.utc)
    notifier.send.reset_mock()

    time.sleep(1.2)
    assert notifier.send.call_count >= 1

    controller._running = False
    controller._status_thread_stop.set()
    if controller._status_thread is not None:
        controller._status_thread.join(timeout=1.0)


def test_scheduler_interval_does_not_override_notify_interval(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_scheduler_notify_split.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    set_notify = client.post("/set", json={"key": "notify_interval_sec", "value": "1800"})
    assert set_notify.status_code == 200
    interval = client.post("/scheduler/interval", json={"tick_sec": 30})
    assert interval.status_code == 200

    risk = client.get("/risk")
    assert risk.status_code == 200
    assert risk.json()["notify_interval_sec"] == 1800
    assert risk.json()["scheduler_tick_sec"] == 30


def test_set_notify_interval_does_not_change_scheduler_tick(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_scheduler_split.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    interval = client.post("/scheduler/interval", json={"tick_sec": 30})
    assert interval.status_code == 200
    set_notify = client.post("/set", json={"key": "notify_interval_sec", "value": "1800"})
    assert set_notify.status_code == 200

    scheduler_resp = client.get("/scheduler")
    assert scheduler_resp.status_code == 200
    assert scheduler_resp.json()["tick_sec"] == 30.0


def test_scheduler_interval_updates_default_freshness_thresholds(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    controller._risk["user_ws_stale_sec"] = 120.0
    controller._risk["market_data_stale_sec"] = 60.0
    controller._risk["watchdog_interval_sec"] = 30.0

    result = controller.set_scheduler_interval(600)
    risk = controller.get_risk()

    assert result["tick_sec"] == 600.0
    assert risk["scheduler_tick_sec"] == 600
    assert risk["user_ws_stale_sec"] == 2400.0
    assert risk["market_data_stale_sec"] == 1200.0
    assert risk["watchdog_interval_sec"] == 600.0


def test_status_summary_translates_action_and_reason_to_korean(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_translate.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    notifier = Notifier(enabled=False)
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )

    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "no_candidate"
    summary = controller._status_summary()
    assert "판단=대기" in summary
    assert "사유=현재 진입 후보가 없습니다" in summary
    assert "프로필=" not in summary
    assert "실거래활성=" not in summary


def test_status_summary_translates_prefixed_reason_head(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_translate_prefixed.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "no_entry:donchian"
    summary = controller._status_summary()
    assert "사유=돈치안 진입 조건 미충족" in summary


def test_status_summary_translates_strategy_block_reason_to_korean(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_translate_strategy.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "regime_adx_rising_missing"
    summary = controller._status_summary()
    assert "판단=대기" in summary
    assert "사유=레짐 ADX 상승 추세 조건 미충족" in summary


def test_status_summary_translates_portfolio_reason(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_translate_portfolio.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    controller._last_cycle["last_action"] = "blocked"
    controller._last_cycle["last_decision_reason"] = "portfolio_cap_reached"
    summary = controller._status_summary()
    assert "사유=포트폴리오 최대 포지션 도달" in summary


def test_status_summary_includes_signed_pnl(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_pnl.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    state_store.startup_reconcile(
        snapshot=ResyncSnapshot(
            positions=[
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.01",
                    "entryPrice": "100000",
                    "unRealizedProfit": "5.0",
                },
                {
                    "symbol": "ETHUSDT",
                    "positionAmt": "-0.02",
                    "entryPrice": "2000",
                    "unRealizedProfit": "-2.0",
                },
            ]
        ),
        reason="status_pnl_test",
    )
    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "no_candidate"

    summary = controller._status_summary()
    assert "미실현PnL=+3.0000 USDT" in summary
    assert "BTCUSDT:+5.0000" in summary
    assert "ETHUSDT:-2.0000" in summary


def test_status_summary_prefers_live_positions_and_side_labels(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_live_positions.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    class _LivePosREST:
        async def get_positions(self):  # type: ignore[no-untyped-def]
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.010",
                    "entryPrice": "100000",
                    "markPrice": "101000",
                    "unRealizedProfit": "1.50",
                },
                {
                    "symbol": "ETHUSDT",
                    "positionAmt": "-0.100",
                    "entryPrice": "3000",
                    "markPrice": "2995",
                    "unRealizedProfit": "-0.50",
                },
            ]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_LivePosREST(),
    )
    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "no_candidate"

    summary = controller._status_summary()
    assert "포지션=BTCUSDT[롱], ETHUSDT[숏]" in summary
    assert "미실현PnL=+1.0000 USDT" in summary


def test_control_api_tick_handles_kernel_exception(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_error.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _BrokenKernel:
        def run_once(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_BrokenKernel(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    payload = tick.json()
    assert payload["ok"] is False
    assert str(payload.get("error", "")).startswith("cycle_failed:")
    assert payload["snapshot"]["last_action"] == "error"


def test_control_api_tick_places_tpsl_brackets_after_live_execution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecuted:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="ETHUSDT",
                    side="BUY",
                    score=1.0,
                    reason="test",
                    entry_price=2000.0,
                ),
                size=SizePlan(
                    symbol="ETHUSDT",
                    qty=0.02,
                    leverage=5.0,
                    notional=40.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(ok=True, order_id="oid-1", reason="live_order_submitted"),
            )

    class _AlgoREST:
        def __init__(self) -> None:
            self.algo_orders: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.algo_orders.append(dict(params))
            return {"clientAlgoId": str(params.get("clientAlgoId") or "")}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

    rest = _AlgoREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecuted(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    payload = tick.json()
    assert payload["ok"] is True
    assert payload["snapshot"]["last_action"] == "executed"
    assert payload["snapshot"]["bracket"]["state"] == "active"

    assert len(rest.algo_orders) == 2
    algo_types = {str(row.get("type") or "") for row in rest.algo_orders}
    assert algo_types == {"TAKE_PROFIT_MARKET", "STOP_MARKET"}


def test_control_api_tick_does_not_duplicate_active_brackets_for_same_symbol(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_dedup.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecuted:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="BTCUSDT",
                    side="BUY",
                    score=1.0,
                    reason="test",
                    entry_price=70000.0,
                ),
                size=SizePlan(
                    symbol="BTCUSDT",
                    qty=0.002,
                    leverage=5.0,
                    notional=140.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(ok=True, order_id="oid-1", reason="live_order_submitted"),
            )

    class _AlgoREST:
        def __init__(self) -> None:
            self.algo_orders: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            payload = dict(params)
            self.algo_orders.append(payload)
            return {"clientAlgoId": str(payload.get("clientAlgoId") or "")}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            symbol_u = str(symbol or "").upper()
            return [
                {
                    "symbol": str(row.get("symbol") or ""),
                    "clientAlgoId": str(row.get("clientAlgoId") or ""),
                }
                for row in self.algo_orders
                if not symbol_u or str(row.get("symbol") or "").upper() == symbol_u
            ]

    rest = _AlgoREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecuted(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    first = client.post("/scheduler/tick")
    second = client.post("/scheduler/tick")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(rest.algo_orders) == 2
    assert second.json()["snapshot"]["bracket"]["state"] == "active"
    assert second.json()["snapshot"]["bracket"]["reused"] is True


def test_control_api_tick_reports_bracket_failure_in_last_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_fail.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecuted:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="BTCUSDT",
                    side="BUY",
                    score=1.0,
                    reason="test",
                    entry_price=100000.0,
                ),
                size=SizePlan(
                    symbol="BTCUSDT",
                    qty=0.001,
                    leverage=5.0,
                    notional=100.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(ok=True, order_id="oid-2", reason="live_order_submitted"),
            )

    class _AlgoRESTFail:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            raise RuntimeError("algo_reject")

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecuted(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_AlgoRESTFail(),
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    payload = tick.json()
    assert payload["ok"] is True
    assert payload["snapshot"]["last_action"] == "executed"
    assert str(payload["snapshot"].get("last_error") or "").startswith("bracket_failed:")
    bracket = payload["snapshot"].get("bracket")
    assert isinstance(bracket, dict)
    assert bracket.get("state") == "failed"
    rows = storage.list_bracket_states()
    assert len(rows) == 1
    assert rows[0]["state"] == "CLEANED"
    assert rows[0]["tp_order_client_id"] is None
    assert rows[0]["sl_order_client_id"] is None


def test_control_api_recovers_active_brackets_on_live_boot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_recover.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="CREATED",
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="no_candidate",
                candidate=None,
            )

    class _AlgoRESTRecover:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [{"symbol": "BTCUSDT", "clientAlgoId": "tp-1"}]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    _ = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_AlgoRESTRecover(),
    )

    rows = storage.list_bracket_states()
    assert len(rows) == 1
    assert rows[0]["state"] == "ACTIVE"


def test_control_api_bracket_poller_repairs_missing_leg_when_position_still_open(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_poller.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )
    storage.save_runtime_marker(
        marker_key="position_management_state",
        payload={
            "positions": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "stop_price": 99.0,
                    "take_profit_price": 102.0,
                    "risk_per_unit": 1.0,
                    "entry_time_ms": int(time.time() * 1000),
                }
            }
        },
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="no_candidate",
                candidate=None,
            )

    class _AlgoRESTPoller:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []
            self.place_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            payload = dict(params)
            self.place_calls.append(payload)
            return {"clientAlgoId": str(payload.get("clientAlgoId") or "")}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [{"symbol": "BTCUSDT", "clientAlgoId": "tp-1"}]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    rest = _AlgoRESTPoller()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert len(rows) == 1
    assert rows[0]["state"] == "ACTIVE"
    assert rows[0]["tp_order_client_id"] != "tp-1"
    assert rows[0]["sl_order_client_id"] != "sl-1"
    canceled_ids = {str(item.get("clientAlgoId") or "") for item in rest.cancel_calls}
    assert canceled_ids == {"tp-1", "sl-1"}
    assert len(rest.place_calls) == 2
    place_types = {str(item.get("type") or "") for item in rest.place_calls}
    assert place_types == {"TAKE_PROFIT_MARKET", "STOP_MARKET"}


def test_control_api_bracket_poller_cancels_extra_managed_algo_orders(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_extra.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="v2tp-current",
        sl_order_client_id="v2sl-current",
        state="ACTIVE",
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="no_candidate",
                candidate=None,
            )

    class _AlgoRESTPoller:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [
                {"symbol": "BTCUSDT", "clientAlgoId": "v2tp-current"},
                {"symbol": "BTCUSDT", "clientAlgoId": "v2sl-current"},
                {"symbol": "BTCUSDT", "clientAlgoId": "v2tp-old"},
                {"symbol": "BTCUSDT", "clientAlgoId": "v2sl-old"},
                {"symbol": "BTCUSDT", "clientAlgoId": "manual-order"},
            ]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    rest = _AlgoRESTPoller()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert len(rows) == 1
    assert rows[0]["state"] == "ACTIVE"
    canceled_ids = {str(item.get("clientAlgoId") or "") for item in rest.cancel_calls}
    assert canceled_ids == {"v2tp-old", "v2sl-old"}


def test_control_api_bracket_poller_sends_take_profit_alert_with_realized_pnl(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_tp_alert.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )
    _ = storage.insert_fill(
        fill_id="fill-tp-1",
        client_id="tp-1",
        exchange_id="1",
        symbol="BTCUSDT",
        side="SELL",
        qty=0.01,
        price=101.0,
        realized_pnl=5.25,
        fill_time_ms=int(time.time() * 1000),
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTTakeProfit:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [{"symbol": "BTCUSDT", "clientAlgoId": "sl-1"}]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0"}]

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=_AlgoRESTTakeProfit(),
    )
    notifier.send_notification.reset_mock()

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    assert notifier.send_notification.call_count == 1
    notification = notifier.send_notification.call_args[0][0]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "익절 완료!"
    assert "BTCUSDT" in notification.body
    assert "+5.2500 USDT" in notification.body


def test_control_api_bracket_poller_sends_stop_loss_alert_with_realized_pnl(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_sl_alert.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )
    _ = storage.insert_fill(
        fill_id="fill-sl-1",
        client_id="sl-1",
        exchange_id="2",
        symbol="BTCUSDT",
        side="SELL",
        qty=0.01,
        price=99.0,
        realized_pnl=-1.75,
        fill_time_ms=int(time.time() * 1000),
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTStopLoss:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [{"symbol": "BTCUSDT", "clientAlgoId": "tp-1"}]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0"}]

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=_AlgoRESTStopLoss(),
    )

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    messages = [call.args[0] for call in notifier.send_notification.call_args_list]
    assert any(
        isinstance(message, NotificationMessage)
        and message.title == "손절 완료!"
        and "BTCUSDT" in message.body
        and "-1.7500 USDT" in message.body
        for message in messages
    )


def test_control_api_bracket_poller_recovers_take_profit_alert_when_both_legs_are_missing(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_tp_missing_both.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )
    _ = storage.insert_fill(
        fill_id="fill-tp-both-missing-1",
        client_id="tp-1",
        exchange_id="tp-both-missing-1",
        symbol="BTCUSDT",
        side="SELL",
        qty=0.01,
        price=101.0,
        realized_pnl=4.2,
        fill_time_ms=int(time.time() * 1000),
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTBothMissing:
        def __init__(self) -> None:
            self.open_algo_calls = 0

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            self.open_algo_calls += 1
            if self.open_algo_calls == 1:
                return [
                    {"symbol": "BTCUSDT", "clientAlgoId": "tp-1"},
                    {"symbol": "BTCUSDT", "clientAlgoId": "sl-1"},
                ]
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0"}]

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000", "balance": "1000"}]

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )

    rest = _AlgoRESTBothMissing()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=rest,
    )
    notifier.send_notification.reset_mock()

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    messages = [call.args[0] for call in notifier.send_notification.call_args_list]
    assert any(
        isinstance(message, NotificationMessage)
        and message.title == "익절 완료!"
        and "BTCUSDT" in message.body
        and "+4.2000 USDT" in message.body
        for message in messages
    )


def test_control_api_bracket_poller_recovers_take_profit_alert_from_income_when_fill_not_yet_persisted(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_income_fallback.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="ETHUSDT",
        tp_order_client_id="tp-2",
        sl_order_client_id="sl-2",
        state="ACTIVE",
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTIncomeFallback:
        def __init__(self) -> None:
            self.open_algo_calls = 0

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            self.open_algo_calls += 1
            if self.open_algo_calls == 1:
                return [
                    {"symbol": "ETHUSDT", "clientAlgoId": "tp-2"},
                    {"symbol": "ETHUSDT", "clientAlgoId": "sl-2"},
                ]
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "ETHUSDT", "positionAmt": "0"}]

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000", "balance": "1000"}]

        async def signed_request(
            self,
            method: str,
            path: str,
            *,
            params: dict[str, Any],
        ) -> list[dict[str, Any]]:
            _ = method
            _ = path
            _ = params
            return [
                {
                    "symbol": "ETHUSDT",
                    "income": "2.75",
                    "time": int(time.time() * 1000),
                }
            ]

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )

    rest = _AlgoRESTIncomeFallback()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=rest,
    )
    notifier.send_notification.reset_mock()

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    messages = [call.args[0] for call in notifier.send_notification.call_args_list]
    assert any(
        isinstance(message, NotificationMessage)
        and message.title == "익절 완료!"
        and "ETHUSDT" in message.body
        and "+2.7500 USDT" in message.body
        for message in messages
    )


def test_control_api_boot_bracket_recovery_emits_take_profit_alert_when_exit_already_happened(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_boot_tp_alert.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-boot-1",
        sl_order_client_id="sl-boot-1",
        state="ACTIVE",
    )
    _ = storage.insert_fill(
        fill_id="fill-boot-tp-1",
        client_id="tp-boot-1",
        exchange_id="boot-ex-1",
        symbol="BTCUSDT",
        side="SELL",
        qty=0.01,
        price=101.0,
        realized_pnl=3.75,
        fill_time_ms=int(time.time() * 1000),
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTBootRecover:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0"}]

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000", "balance": "1000"}]

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )

    _ = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=_AlgoRESTBootRecover(),
    )

    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    messages = [call.args[0] for call in notifier.send_notification.call_args_list]
    assert any(
        isinstance(message, NotificationMessage)
        and message.title == "익절 완료!"
        and "BTCUSDT" in message.body
        and "+3.7500 USDT" in message.body
        for message in messages
    )


def test_control_api_sl_symbol_flatten_cooldown_scopes_by_symbol(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_sl_flatten_once.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTNoop:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=_AlgoRESTNoop(),
    )
    controller._running = True
    controller._risk["sl_flatten_cooldown_sec"] = 3600

    calls: list[str] = []

    async def _fake_close_position(
        *,
        symbol: str,
        notify_reason: str = "forced_close",
    ) -> dict[str, object]:
        _ = notify_reason
        calls.append(symbol)
        return {"symbol": symbol, "detail": {}}

    controller.close_position = _fake_close_position  # type: ignore[method-assign]

    controller._maybe_trigger_symbol_sl_flatten(trigger_symbol="BTCUSDT")
    controller._running = True
    controller._maybe_trigger_symbol_sl_flatten(trigger_symbol="BTCUSDT")
    controller._maybe_trigger_symbol_sl_flatten(trigger_symbol="ETHUSDT")

    assert calls == ["BTCUSDT", "ETHUSDT"]
    assert controller._watchdog_state["last_sl_flatten_symbol"] == "ETHUSDT"
    assert state_store.get().status == "STOPPED"


def test_control_api_bracket_poller_uses_realized_pnl_sign_for_alert_headline(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_sign_alert.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )
    _ = storage.insert_fill(
        fill_id="fill-sl-positive-1",
        client_id="sl-1",
        exchange_id="3",
        symbol="BTCUSDT",
        side="SELL",
        qty=0.01,
        price=100.3,
        realized_pnl=0.5631,
        fill_time_ms=int(time.time() * 1000),
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTStopLegMissing:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [{"symbol": "BTCUSDT", "clientAlgoId": "tp-1"}]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0"}]

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=_AlgoRESTStopLegMissing(),
    )

    controller._poll_brackets_once()

    messages = [call.args[0] for call in notifier.send_notification.call_args_list]
    assert any(
        isinstance(message, NotificationMessage)
        and message.title == "익절 완료!"
        and "+0.5631 USDT" in message.body
        for message in messages
    )


def test_control_api_bracket_poller_uses_breakeven_headline_for_zero_realized_pnl(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_zero_alert.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )
    _ = storage.insert_fill(
        fill_id="fill-sl-zero-1",
        client_id="sl-1",
        exchange_id="4",
        symbol="BTCUSDT",
        side="SELL",
        qty=0.01,
        price=100.0,
        realized_pnl=0.0,
        fill_time_ms=int(time.time() * 1000),
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTStopLegMissing:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [{"symbol": "BTCUSDT", "clientAlgoId": "tp-1"}]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0"}]

    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=_AlgoRESTStopLegMissing(),
    )

    controller._poll_brackets_once()

    messages = [call.args[0] for call in notifier.send_notification.call_args_list]
    assert any(
        isinstance(message, NotificationMessage)
        and message.title == "손익없음 청산!"
        and "0.0000 USDT" in message.body
        for message in messages
    )


def test_control_api_bracket_poller_cleans_open_algos_when_position_flat(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_flat.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="ETHUSDT",
        tp_order_client_id="tp-2",
        sl_order_client_id="sl-2",
        state="ACTIVE",
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="no_candidate",
                candidate=None,
            )

    class _AlgoRESTFlat:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [
                {"symbol": "ETHUSDT", "clientAlgoId": "tp-2"},
                {"symbol": "ETHUSDT", "clientAlgoId": "sl-2"},
                {"symbol": "ETHUSDT", "clientAlgoId": "extra-3"},
            ]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "ETHUSDT", "positionAmt": "0"}]

    rest = _AlgoRESTFlat()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    canceled = {str(item.get("clientAlgoId") or "") for item in rest.cancel_calls}
    assert {"tp-2", "sl-2", "extra-3"}.issubset(canceled)


def test_control_api_trailing_exit_closes_position_on_profit_drawdown(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_trailing.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-t",
        sl_order_client_id="sl-t",
        state="ACTIVE",
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="no_candidate",
                candidate=None,
            )

    class _AlgoRESTTrailing:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []
            self.close_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [
                {"symbol": "BTCUSDT", "clientAlgoId": "tp-t"},
                {"symbol": "BTCUSDT", "clientAlgoId": "sl-t"},
            ]

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.01",
                    "entryPrice": "100.0",
                    "markPrice": "101.0",
                    "positionSide": "BOTH",
                }
            ]

        async def close_position_market(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            position_side: str = "BOTH",
        ) -> dict[str, str]:
            self.close_calls.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": str(quantity),
                    "position_side": position_side,
                }
            )
            return {}

    rest = _AlgoRESTTrailing()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    controller._risk["trailing_enabled"] = True
    controller._risk["trailing_mode"] = "PCT"
    controller._risk["trail_arm_pnl_pct"] = 1.0
    controller._risk["trail_distance_pnl_pct"] = 0.5
    controller._risk["trail_grace_minutes"] = 0
    controller._trailing_state["BTCUSDT"] = {
        "first_seen_mono": time.monotonic() - 600.0,
        "peak_pnl_pct": 2.0,
        "armed": True,
    }

    controller._poll_brackets_once()

    assert len(rest.close_calls) == 1
    close = rest.close_calls[0]
    assert close["symbol"] == "BTCUSDT"
    assert close["side"] == "SELL"
    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    assert float(controller._status_snapshot()["watchdog"]["last_trailing_distance_pct"]) == 0.5


def test_control_api_records_position_management_plan_for_executed_cycle(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    cycle = KernelCycleResult(
        state="executed",
        reason="executed",
        candidate=Candidate(
            symbol="BTCUSDT",
            side="BUY",
            score=1.0,
            entry_price=100.0,
            stop_price_hint=98.0,
            take_profit_hint=104.0,
            execution_hints={
                "time_stop_bars": 12,
                "progress_check_bars": 6,
                "progress_min_mfe_r": 0.35,
                "progress_extend_trigger_r": 1.0,
                "progress_extend_bars": 6,
                "entry_quality_score_v2": 0.82,
            },
        ),
        size=SizePlan(symbol="BTCUSDT", qty=0.01, leverage=3.0, notional=1.0),
        execution=ExecutionResult(ok=True, order_id="oid-1", reason="live_order_submitted"),
    )

    controller._record_position_management_plan(cycle=cycle)

    payload = controller.state_store.runtime_storage().load_runtime_marker(
        marker_key="position_management_state"
    )
    assert payload is not None
    plan = payload["positions"]["BTCUSDT"]
    assert plan["entry_price"] == 100.0
    assert plan["stop_price"] == 98.0
    assert plan["take_profit_price"] == 104.0
    assert plan["progress_check_bars"] == 6


def test_control_api_position_management_progress_failure_closes_position(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_position_manage_progress.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

        def probe_market_data(self) -> dict[str, object]:
            now_ms = int(time.time() * 1000)
            return {
                "symbol": "BTCUSDT",
                "market": {
                    "15m": [
                        [now_ms - 900000, "100.0", "100.4", "99.8", "100.1", "1500", now_ms]
                    ]
                },
                "symbols": {},
            }

    class _AlgoRESTNoop:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.01",
                    "entryPrice": "100.0",
                    "markPrice": "100.1",
                    "positionSide": "BOTH",
                }
            ]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_AlgoRESTNoop(),
    )

    entry_time_ms = int(time.time() * 1000) - (2 * 15 * 60 * 1000)
    storage.save_runtime_marker(
        marker_key="position_management_state",
        payload={
            "positions": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "stop_price": 98.0,
                    "risk_per_unit": 2.0,
                    "entry_time_ms": entry_time_ms,
                    "max_favorable_price": 100.0,
                    "max_favorable_r": 0.0,
                    "progress_check_bars": 1,
                    "progress_min_mfe_r": 0.5,
                    "progress_extend_trigger_r": 0.0,
                    "progress_extend_bars": 0,
                    "time_stop_bars": 12,
                    "current_time_stop_bars": 12,
                    "selective_extension_proof_bars": 0,
                    "selective_extension_min_mfe_r": 0.0,
                    "selective_extension_min_regime_strength": 0.0,
                    "selective_extension_min_bias_strength": 0.0,
                    "selective_extension_min_quality_score_v2": 0.0,
                    "selective_extension_time_stop_bars": 0,
                    "selective_extension_move_stop_to_be_at_r": 0.0,
                }
            }
        },
    )

    calls: list[tuple[str, str]] = []

    async def _fake_close_position(
        *,
        symbol: str,
        notify_reason: str = "forced_close",
    ) -> dict[str, object]:
        calls.append((symbol, notify_reason))
        return {"symbol": symbol, "detail": {}}

    controller.close_position = _fake_close_position  # type: ignore[method-assign]

    controller._poll_brackets_once()

    assert calls == [("BTCUSDT", "progress_failed_close")]
    payload = storage.load_runtime_marker(marker_key="position_management_state")
    assert payload is not None
    assert payload["positions"] == {}


def test_control_api_position_management_selective_extension_extends_hold_window(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_position_manage_extend.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

        def probe_market_data(self) -> dict[str, object]:
            now_ms = int(time.time() * 1000)
            return {
                "symbol": "BTCUSDT",
                "market": {
                    "15m": [
                        [now_ms - 900000, "100.0", "103.0", "99.9", "102.2", "2500", now_ms]
                    ]
                },
                "symbols": {},
            }

    class _AlgoRESTNoop:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.01",
                    "entryPrice": "100.0",
                    "markPrice": "102.2",
                    "positionSide": "BOTH",
                }
            ]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_AlgoRESTNoop(),
    )

    entry_time_ms = int(time.time() * 1000) - (15 * 60 * 1000)
    storage.save_runtime_marker(
        marker_key="position_management_state",
        payload={
            "positions": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "stop_price": 98.0,
                    "risk_per_unit": 2.0,
                    "entry_time_ms": entry_time_ms,
                    "entry_regime_strength": 0.7,
                    "entry_bias_strength": 0.7,
                    "entry_quality_score_v2": 0.82,
                    "max_favorable_price": 100.0,
                    "max_favorable_r": 0.0,
                    "progress_check_bars": 0,
                    "progress_min_mfe_r": 0.0,
                    "progress_extend_trigger_r": 0.5,
                    "progress_extend_bars": 3,
                    "time_stop_bars": 12,
                    "current_time_stop_bars": 12,
                    "selective_extension_proof_bars": 2,
                    "selective_extension_min_mfe_r": 0.75,
                    "selective_extension_min_regime_strength": 0.55,
                    "selective_extension_min_bias_strength": 0.55,
                    "selective_extension_min_quality_score_v2": 0.78,
                    "selective_extension_time_stop_bars": 24,
                    "selective_extension_move_stop_to_be_at_r": 0.75,
                }
            }
        },
    )

    controller._poll_brackets_once()

    payload = storage.load_runtime_marker(marker_key="position_management_state")
    assert payload is not None
    plan = payload["positions"]["BTCUSDT"]
    assert plan["progress_extension_applied"] is True
    assert plan["selective_extension_activated"] is True
    assert plan["current_time_stop_bars"] == 24
    assert plan["breakeven_protection_armed"] is True


def test_control_api_position_management_partial_reduce_reprices_bracket(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_position_manage_partial.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="v2tp-old",
        sl_order_client_id="v2sl-old",
        state="ACTIVE",
    )
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

        def probe_market_data(self) -> dict[str, object]:
            now_ms = int(time.time() * 1000)
            return {
                "symbol": "BTCUSDT",
                "market": {
                    "15m": [
                        [now_ms - 900000, "100.0", "102.8", "99.8", "102.5", "2400", now_ms]
                    ]
                },
                "symbols": {},
            }

    class _AlgoRESTManage:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []
            self.place_calls: list[dict[str, str]] = []
            self.reduce_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.place_calls.append(dict(params))
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [
                {"symbol": "BTCUSDT", "clientAlgoId": "v2tp-old"},
                {"symbol": "BTCUSDT", "clientAlgoId": "v2sl-old"},
            ]

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.04",
                    "entryPrice": "100.0",
                    "markPrice": "102.5",
                    "positionSide": "BOTH",
                }
            ]

        async def place_reduce_only_market_order(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            position_side: str = "BOTH",
        ) -> dict[str, str]:
            self.reduce_calls.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": f"{quantity:.8f}",
                    "position_side": position_side,
                }
            )
            return {}

    rest = _AlgoRESTManage()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    entry_time_ms = int(time.time() * 1000) - (15 * 60 * 1000)
    storage.save_runtime_marker(
        marker_key="position_management_state",
        payload={
            "positions": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "stop_price": 98.0,
                    "take_profit_price": 104.0,
                    "risk_per_unit": 2.0,
                    "entry_time_ms": entry_time_ms,
                    "max_favorable_price": 100.0,
                    "max_favorable_r": 0.0,
                    "progress_check_bars": 0,
                    "progress_min_mfe_r": 0.0,
                    "progress_extend_trigger_r": 0.0,
                    "progress_extend_bars": 0,
                    "reward_risk_reference_r": 2.0,
                    "tp_partial_ratio": 0.25,
                    "tp_partial_at_r": 1.0,
                    "move_stop_to_be_at_r": 1.0,
                    "time_stop_bars": 12,
                    "current_time_stop_bars": 12,
                    "selective_extension_proof_bars": 0,
                    "selective_extension_min_mfe_r": 0.0,
                    "selective_extension_min_regime_strength": 0.0,
                    "selective_extension_min_bias_strength": 0.0,
                    "selective_extension_min_quality_score_v2": 0.0,
                    "selective_extension_time_stop_bars": 0,
                    "selective_extension_take_profit_r": 0.0,
                    "selective_extension_move_stop_to_be_at_r": 0.0,
                    "partial_reduce_done": False,
                    "breakeven_protection_armed": False,
                }
            }
        },
    )

    controller._poll_brackets_once()

    assert len(rest.reduce_calls) == 1
    assert rest.reduce_calls[0]["side"] == "SELL"
    assert rest.reduce_calls[0]["quantity"] == "0.01000000"
    canceled_ids = {str(item.get("clientAlgoId") or "") for item in rest.cancel_calls}
    assert canceled_ids == {"v2tp-old", "v2sl-old"}
    assert len(rest.place_calls) == 2
    trigger_prices = {call["type"]: call["triggerPrice"] for call in rest.place_calls}
    assert trigger_prices["TAKE_PROFIT_MARKET"] == "104"
    assert trigger_prices["STOP_MARKET"] == "100"
    payload = storage.load_runtime_marker(marker_key="position_management_state")
    assert payload is not None
    plan = payload["positions"]["BTCUSDT"]
    assert plan["partial_reduce_done"] is True
    assert plan["breakeven_protection_armed"] is True
    assert plan["stop_price"] == 100.0


def test_control_api_position_management_selective_extension_reprices_take_profit(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_position_manage_tp.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="v2tp-old",
        sl_order_client_id="v2sl-old",
        state="ACTIVE",
    )
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

        def probe_market_data(self) -> dict[str, object]:
            now_ms = int(time.time() * 1000)
            return {
                "symbol": "BTCUSDT",
                "market": {
                    "15m": [
                        [now_ms - 900000, "100.0", "102.8", "99.8", "102.4", "2400", now_ms]
                    ]
                },
                "symbols": {},
            }

    class _AlgoRESTManage:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []
            self.place_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.place_calls.append(dict(params))
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [
                {"symbol": "BTCUSDT", "clientAlgoId": "v2tp-old"},
                {"symbol": "BTCUSDT", "clientAlgoId": "v2sl-old"},
            ]

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.03",
                    "entryPrice": "100.0",
                    "markPrice": "102.4",
                    "positionSide": "BOTH",
                }
            ]

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "100.0", "walletBalance": "100.0"}]

    rest = _AlgoRESTManage()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    entry_time_ms = int(time.time() * 1000) - (15 * 60 * 1000)
    storage.save_runtime_marker(
        marker_key="position_management_state",
        payload={
            "positions": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "stop_price": 98.0,
                    "take_profit_price": 104.0,
                    "risk_per_unit": 2.0,
                    "entry_time_ms": entry_time_ms,
                    "entry_regime_strength": 0.7,
                    "entry_bias_strength": 0.7,
                    "entry_quality_score_v2": 0.82,
                    "max_favorable_price": 100.0,
                    "max_favorable_r": 0.0,
                    "progress_check_bars": 0,
                    "progress_min_mfe_r": 0.0,
                    "progress_extend_trigger_r": 0.0,
                    "progress_extend_bars": 0,
                    "reward_risk_reference_r": 2.0,
                    "tp_partial_ratio": 0.0,
                    "tp_partial_at_r": 0.0,
                    "move_stop_to_be_at_r": 0.0,
                    "time_stop_bars": 12,
                    "current_time_stop_bars": 12,
                    "selective_extension_proof_bars": 2,
                    "selective_extension_min_mfe_r": 0.75,
                    "selective_extension_min_regime_strength": 0.55,
                    "selective_extension_min_bias_strength": 0.55,
                    "selective_extension_min_quality_score_v2": 0.78,
                    "selective_extension_time_stop_bars": 24,
                    "selective_extension_take_profit_r": 2.5,
                    "selective_extension_move_stop_to_be_at_r": 0.0,
                    "partial_reduce_done": True,
                    "breakeven_protection_armed": False,
                }
            }
        },
    )

    controller._poll_brackets_once()

    canceled_ids = {str(item.get("clientAlgoId") or "") for item in rest.cancel_calls}
    assert canceled_ids == {"v2tp-old", "v2sl-old"}
    trigger_prices = {call["type"]: call["triggerPrice"] for call in rest.place_calls}
    assert trigger_prices["TAKE_PROFIT_MARKET"] == "105"
    assert trigger_prices["STOP_MARKET"] == "98"
    payload = storage.load_runtime_marker(marker_key="position_management_state")
    assert payload is not None
    plan = payload["positions"]["BTCUSDT"]
    assert plan["selective_extension_activated"] is True
    assert plan["take_profit_price"] == 105.0


def test_control_api_position_management_signal_weakness_reduces_position(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_position_manage_weak.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="v2tp-old",
        sl_order_client_id="v2sl-old",
        state="ACTIVE",
    )
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelWeak:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

        def probe_market_data(self) -> dict[str, object]:
            now_ms = int(time.time() * 1000)
            return {
                "symbol": "BTCUSDT",
                "market": {
                    "15m": [
                        [now_ms - 900000, "100.0", "101.4", "99.8", "100.7", "1800", now_ms]
                    ]
                },
                "symbols": {},
            }

        def inspect_symbol_decision(
            self,
            *,
            symbol: str,
            snapshot: dict[str, object] | None = None,
        ) -> dict[str, object]:
            _ = symbol
            _ = snapshot
            return {
                "intent": "NONE",
                "reason": "volume_missing",
                "score": 0.0,
                "regime_strength": 0.4,
                "bias_strength": 0.4,
            }

    class _AlgoRESTManage:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []
            self.place_calls: list[dict[str, str]] = []
            self.reduce_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.place_calls.append(dict(params))
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [
                {"symbol": "BTCUSDT", "clientAlgoId": "v2tp-old"},
                {"symbol": "BTCUSDT", "clientAlgoId": "v2sl-old"},
            ]

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.04",
                    "entryPrice": "100.0",
                    "markPrice": "100.7",
                    "positionSide": "BOTH",
                }
            ]

        async def place_reduce_only_market_order(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            position_side: str = "BOTH",
        ) -> dict[str, str]:
            self.reduce_calls.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": f"{quantity:.8f}",
                    "position_side": position_side,
                }
            )
            return {}

    rest = _AlgoRESTManage()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelWeak(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    entry_time_ms = int(time.time() * 1000) - (2 * 15 * 60 * 1000)
    storage.save_runtime_marker(
        marker_key="position_management_state",
        payload={
            "positions": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "alpha_id": "alpha_expansion",
                    "entry_regime": "TREND_UP",
                    "entry_score": 0.9,
                    "entry_regime_strength": 0.8,
                    "entry_bias_strength": 0.8,
                    "entry_price": 100.0,
                    "stop_price": 98.0,
                    "take_profit_price": 104.0,
                    "risk_per_unit": 2.0,
                    "entry_time_ms": entry_time_ms,
                    "max_favorable_price": 100.0,
                    "max_favorable_r": 0.0,
                    "weak_reduce_stage": 0,
                    "progress_check_bars": 0,
                    "progress_min_mfe_r": 0.0,
                    "progress_extend_trigger_r": 0.0,
                    "progress_extend_bars": 0,
                    "reward_risk_reference_r": 2.0,
                    "tp_partial_ratio": 0.0,
                    "tp_partial_at_r": 0.0,
                    "move_stop_to_be_at_r": 0.0,
                    "time_stop_bars": 12,
                    "current_time_stop_bars": 12,
                    "selective_extension_proof_bars": 0,
                    "selective_extension_min_mfe_r": 0.0,
                    "selective_extension_min_regime_strength": 0.0,
                    "selective_extension_min_bias_strength": 0.0,
                    "selective_extension_min_quality_score_v2": 0.0,
                    "selective_extension_time_stop_bars": 0,
                    "selective_extension_take_profit_r": 0.0,
                    "selective_extension_move_stop_to_be_at_r": 0.0,
                    "partial_reduce_done": False,
                    "breakeven_protection_armed": False,
                }
            }
        },
    )

    controller._poll_brackets_once()

    assert len(rest.reduce_calls) == 1
    assert rest.reduce_calls[0]["quantity"] == "0.00800000"
    payload = storage.load_runtime_marker(marker_key="position_management_state")
    assert payload is not None
    plan = payload["positions"]["BTCUSDT"]
    assert plan["weak_reduce_stage"] == 1
    assert plan["stop_price"] == 100.0


def test_control_api_position_management_signal_weakness_second_stage_reduces_again(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_position_manage_weak_stage2.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelWeak:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

        def probe_market_data(self) -> dict[str, object]:
            now_ms = int(time.time() * 1000)
            return {
                "symbol": "BTCUSDT",
                "market": {
                    "15m": [
                        [now_ms - 900000, "100.0", "100.8", "99.7", "100.2", "1600", now_ms]
                    ]
                },
                "symbols": {},
            }

        def inspect_symbol_decision(
            self,
            *,
            symbol: str,
            snapshot: dict[str, object] | None = None,
        ) -> dict[str, object]:
            _ = symbol
            _ = snapshot
            return {
                "intent": "NONE",
                "reason": "quality_score_v2_missing",
                "score": 0.42,
                "regime_strength": 0.3,
                "bias_strength": 0.3,
                "regime": "UNKNOWN",
            }

    class _AlgoRESTManage:
        def __init__(self) -> None:
            self.reduce_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.032",
                    "entryPrice": "100.0",
                    "markPrice": "100.2",
                    "positionSide": "BOTH",
                }
            ]

        async def place_reduce_only_market_order(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            position_side: str = "BOTH",
        ) -> dict[str, str]:
            self.reduce_calls.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": f"{quantity:.8f}",
                    "position_side": position_side,
                }
            )
            return {}

    rest = _AlgoRESTManage()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelWeak(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    storage.save_runtime_marker(
        marker_key="position_management_state",
        payload={
            "positions": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "alpha_id": "alpha_expansion",
                    "entry_regime": "UNKNOWN",
                    "entry_score": 0.9,
                    "entry_regime_strength": 0.8,
                    "entry_bias_strength": 0.8,
                    "entry_price": 100.0,
                    "stop_price": 100.0,
                    "take_profit_price": 104.0,
                    "risk_per_unit": 2.0,
                    "entry_time_ms": int(time.time() * 1000),
                    "weak_reduce_stage": 1,
                    "partial_reduce_done": False,
                    "max_favorable_price": 100.0,
                    "max_favorable_r": 0.0,
                    "time_stop_bars": 12,
                    "current_time_stop_bars": 12,
                }
            }
        },
    )

    controller._poll_brackets_once()

    assert len(rest.reduce_calls) == 1
    assert rest.reduce_calls[0]["quantity"] == "0.01232000"
    payload = storage.load_runtime_marker(marker_key="position_management_state")
    assert payload is not None
    assert payload["positions"]["BTCUSDT"]["weak_reduce_stage"] == 2


def test_control_api_position_management_signal_flip_closes_position(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_position_manage_flip.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelFlip:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

        def probe_market_data(self) -> dict[str, object]:
            return {"symbol": "BTCUSDT", "market": {"15m": []}, "symbols": {}}

        def inspect_symbol_decision(
            self,
            *,
            symbol: str,
            snapshot: dict[str, object] | None = None,
        ) -> dict[str, object]:
            _ = symbol
            _ = snapshot
            return {
                "intent": "SHORT",
                "reason": "entry_signal",
                "score": 0.8,
                "regime_strength": 0.7,
                "bias_strength": 0.7,
            }

    class _AlgoRESTNoop:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.02",
                    "entryPrice": "100.0",
                    "markPrice": "99.8",
                    "positionSide": "BOTH",
                }
            ]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelFlip(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_AlgoRESTNoop(),
    )
    storage.save_runtime_marker(
        marker_key="position_management_state",
        payload={
            "positions": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry_score": 0.9,
                    "entry_regime_strength": 0.8,
                    "entry_bias_strength": 0.8,
                    "entry_price": 100.0,
                    "stop_price": 98.0,
                    "risk_per_unit": 2.0,
                    "entry_time_ms": int(time.time() * 1000),
                }
            }
        },
    )

    calls: list[tuple[str, str]] = []

    async def _fake_close_position(
        *,
        symbol: str,
        notify_reason: str = "forced_close",
    ) -> dict[str, object]:
        calls.append((symbol, notify_reason))
        return {"symbol": symbol, "detail": {}}

    controller.close_position = _fake_close_position  # type: ignore[method-assign]

    controller._poll_brackets_once()

    assert calls == [("BTCUSDT", "signal_flip_close")]


def test_control_api_position_management_runner_lock_reprices_after_partial_reduce(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_position_manage_runner.sqlite3")
    storage = RuntimeStorage(sqlite_path=str(cfg.behavior.storage.sqlite_path))
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="v2tp-old",
        sl_order_client_id="v2sl-old",
        state="ACTIVE",
    )
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelRunner:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

        def probe_market_data(self) -> dict[str, object]:
            now_ms = int(time.time() * 1000)
            return {
                "symbol": "BTCUSDT",
                "market": {
                    "15m": [
                        [now_ms - 900000, "100.0", "104.5", "100.2", "104.0", "2600", now_ms]
                    ]
                },
                "symbols": {},
            }

        def inspect_symbol_decision(
            self,
            *,
            symbol: str,
            snapshot: dict[str, object] | None = None,
        ) -> dict[str, object]:
            _ = symbol
            _ = snapshot
            return {
                "intent": "LONG",
                "reason": "entry_signal",
                "score": 0.75,
                "regime_strength": 0.65,
                "bias_strength": 0.65,
            }

    class _AlgoRESTManage:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []
            self.place_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.place_calls.append(dict(params))
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [
                {"symbol": "BTCUSDT", "clientAlgoId": "v2tp-old"},
                {"symbol": "BTCUSDT", "clientAlgoId": "v2sl-old"},
            ]

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.03",
                    "entryPrice": "100.0",
                    "markPrice": "104.0",
                    "positionSide": "BOTH",
                }
            ]

    rest = _AlgoRESTManage()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelRunner(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    storage.save_runtime_marker(
        marker_key="position_management_state",
        payload={
            "positions": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry_score": 0.9,
                    "entry_regime_strength": 0.8,
                    "entry_bias_strength": 0.8,
                    "entry_price": 100.0,
                    "stop_price": 100.0,
                    "take_profit_price": 105.0,
                    "risk_per_unit": 2.0,
                    "volatility_frac": 0.01,
                    "entry_time_ms": int(time.time() * 1000),
                    "partial_reduce_done": True,
                    "runner_lock_stage": 0,
                    "max_favorable_price": 100.0,
                    "max_favorable_r": 0.0,
                    "time_stop_bars": 18,
                    "current_time_stop_bars": 18,
                }
            }
        },
    )

    controller._poll_brackets_once()

    trigger_prices = {call["type"]: call["triggerPrice"] for call in rest.place_calls}
    assert trigger_prices["TAKE_PROFIT_MARKET"] == "105"
    assert trigger_prices["STOP_MARKET"] == "102"
    payload = storage.load_runtime_marker(marker_key="position_management_state")
    assert payload is not None
    plan = payload["positions"]["BTCUSDT"]
    assert plan["runner_lock_stage"] == 2
    assert plan["stop_price"] == 102.0


def test_control_api_adaptive_regime_tpsl_scales_with_bull_multiplier(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_adaptive.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecuted:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="BTCUSDT",
                    side="BUY",
                    score=1.0,
                    reason="entry_pullback_long",
                    entry_price=100.0,
                    regime_hint="BULL",
                ),
                size=SizePlan(
                    symbol="BTCUSDT",
                    qty=0.01,
                    leverage=3.0,
                    notional=1.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(
                    ok=True, order_id="oid-adaptive", reason="live_order_submitted"
                ),
            )

    class _AlgoREST:
        def __init__(self) -> None:
            self.algo_orders: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.algo_orders.append(dict(params))
            return {"clientAlgoId": str(params.get("clientAlgoId") or "")}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    rest = _AlgoREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecuted(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    assert (
        client.post("/set", json={"key": "tpsl_policy", "value": "adaptive_regime"}).status_code
        == 200
    )
    assert (
        client.post("/set", json={"key": "tpsl_base_take_profit_pct", "value": "0.02"}).status_code
        == 200
    )
    assert (
        client.post("/set", json={"key": "tpsl_base_stop_loss_pct", "value": "0.01"}).status_code
        == 200
    )
    assert (
        client.post("/set", json={"key": "tpsl_regime_mult_bull", "value": "1.2"}).status_code
        == 200
    )

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["ok"] is True

    assert len(rest.algo_orders) == 2
    by_type = {str(row.get("type") or ""): row for row in rest.algo_orders}
    tp_trigger = float(str(by_type["TAKE_PROFIT_MARKET"]["triggerPrice"]))
    sl_trigger = float(str(by_type["STOP_MARKET"]["triggerPrice"]))
    assert tp_trigger == 102.4
    assert sl_trigger == 98.8


def test_control_api_atr_tpsl_method_uses_candidate_volatility_hint(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_atr.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecuted:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="ETHUSDT",
                    side="BUY",
                    score=1.0,
                    reason="entry_pullback_long",
                    entry_price=100.0,
                    volatility_hint=3.0,
                    regime_hint="BULL",
                ),
                size=SizePlan(
                    symbol="ETHUSDT",
                    qty=0.01,
                    leverage=3.0,
                    notional=1.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(
                    ok=True, order_id="oid-atr", reason="live_order_submitted"
                ),
            )

    class _AlgoREST:
        def __init__(self) -> None:
            self.algo_orders: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.algo_orders.append(dict(params))
            return {"clientAlgoId": str(params.get("clientAlgoId") or "")}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "ETHUSDT", "positionAmt": "0.01"}]

    rest = _AlgoREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecuted(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    assert client.post("/set", json={"key": "tpsl_method", "value": "atr"}).status_code == 200
    assert client.post("/set", json={"key": "tpsl_tp_atr", "value": "2"}).status_code == 200
    assert client.post("/set", json={"key": "tpsl_sl_atr", "value": "1"}).status_code == 200

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["ok"] is True

    assert len(rest.algo_orders) == 2
    by_type = {str(row.get("type") or ""): row for row in rest.algo_orders}
    tp_trigger = float(str(by_type["TAKE_PROFIT_MARKET"]["triggerPrice"]))
    sl_trigger = float(str(by_type["STOP_MARKET"]["triggerPrice"]))
    assert tp_trigger == 106.0
    assert sl_trigger == 97.0


def test_control_api_tick_from_async_endpoint_with_rest_snapshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_async_snapshot.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _FakeREST:
        async def public_request(self, method: str, path: str, *, params=None):  # type: ignore[no-untyped-def]
            _ = method
            if path == "/fapi/v1/klines":
                out = []
                base = 100.0
                for idx in range(260):
                    o = base + idx * 0.01
                    h = o + 0.05
                    low_price = o - 0.05
                    c = o + 0.01
                    out.append(
                        [
                            idx,
                            str(o),
                            str(h),
                            str(low_price),
                            str(c),
                            "10",
                            idx + 1,
                            "0",
                            "0",
                            "0",
                            "0",
                            "0",
                        ]
                    )
                return out
            if path == "/fapi/v1/premiumIndex":
                return {"lastFundingRate": "0.0001"}
            if path == "/fapi/v1/globalLongShortAccountRatio":
                return [{"longShortRatio": "1.0"}]
            return {}

    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=_FakeREST(),
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    payload = tick.json()
    assert payload["ok"] is True
    assert payload["snapshot"]["last_action"] != "error"


def test_control_api_status_uses_live_usdt_balance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_balance.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    class _BalanceREST:
        async def get_balances(self):  # type: ignore[no-untyped-def]
            return [{"asset": "USDT", "availableBalance": "321.45", "walletBalance": "345.67"}]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_BalanceREST(),
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["capital_snapshot"]["available_usdt"] == 321.45
    assert payload["binance"]["usdt_balance"]["wallet"] == 345.67
    assert payload["binance"]["usdt_balance"]["source"] == "exchange"
    assert payload["binance"]["private_error"] is None


def test_control_api_status_prefers_live_positions_for_snapshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(
        tmp_path / "control_status_live_positions_snapshot.sqlite3"
    )
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    class _LiveREST:
        async def get_balances(self):  # type: ignore[no-untyped-def]
            return [{"asset": "USDT", "availableBalance": "50", "walletBalance": "50"}]

        async def get_positions(self):  # type: ignore[no-untyped-def]
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.002",
                    "entryPrice": "100000",
                    "unRealizedProfit": "2.2",
                }
            ]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_LiveREST(),
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    row = payload["binance"]["positions"]["BTCUSDT"]
    assert row["position_amt"] == 0.002
    assert row["position_side"] == "LONG"
    assert row["unrealized_pnl"] == 2.2


def test_control_api_status_does_not_fallback_to_stale_positions_when_live_positions_are_flat(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(
        tmp_path / "control_status_live_positions_flat.sqlite3"
    )
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    state_store.apply_reconciliation(
        open_orders=[],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "0.01", "entryPrice": "100000"}],
        balances=[],
        reason="seed_stale_position",
    )
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=False,
        rest_client=None,
    )

    class _LiveRESTFlat:
        async def get_balances(self):  # type: ignore[no-untyped-def]
            return [{"asset": "USDT", "availableBalance": "50", "walletBalance": "50"}]

        async def get_positions(self):  # type: ignore[no-untyped-def]
            return [{"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0"}]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_LiveRESTFlat(),
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["binance"]["positions"] == {}


def test_control_api_close_all_uses_live_positions_when_available(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _FlattenExchange:
        def __init__(self) -> None:
            self.cancelled_symbols: list[str] = []
            self.flattened_symbols: set[str] = set()

        async def cancel_all_open_orders(self, *, symbol: str) -> dict[str, Any]:
            self.cancelled_symbols.append(symbol)
            return {}

        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
            _ = symbol
            return []

        async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            _ = params
            return {}

        async def get_positions(self) -> list[dict[str, Any]]:
            if "ETHUSDT" in self.flattened_symbols:
                return [{"symbol": "ETHUSDT", "positionAmt": "0"}]
            return [{"symbol": "ETHUSDT", "positionAmt": "0.02"}]

        async def place_reduce_only_market_order(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            position_side: str = "BOTH",
        ) -> dict[str, Any]:
            _ = side
            _ = quantity
            _ = position_side
            self.flattened_symbols.add(symbol)
            return {"symbol": symbol}

    controller, state_store, _ops = _build_live_controller(tmp_path, rest_client=_HealthyREST())
    state_store.apply_reconciliation(
        open_orders=[],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "0.01", "entryPrice": "100000"}],
        balances=[],
        reason="seed_stale_position",
    )
    exchange = _FlattenExchange()
    controller.ops.exchange = exchange
    controller.rest_client = exchange  # type: ignore[assignment]

    out = asyncio.run(controller.close_all())

    assert out["detail"]["results"][0]["symbol"] == "ETHUSDT"
    assert exchange.cancelled_symbols == ["ETHUSDT"]


def test_control_api_status_marks_balance_source_as_fallback_when_live_fetch_unavailable(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    app = _build_app(tmp_path)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["binance"]["usdt_balance"]["source"] == "fallback"
    assert payload["binance"]["private_error"] == "rest_client_unavailable"


def test_control_api_status_notional_tracks_effective_budget_leverage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    assert controller.set_value(key="universe_symbols", value="BTCUSDT,ETHUSDT")["applied_value"] == [
        "BTCUSDT",
        "ETHUSDT",
    ]
    assert controller.set_value(key="margin_budget_usdt", value="35")["applied_value"] == 35
    assert controller.set_value(key="max_leverage", value="20")["applied_value"] == 20
    assert controller.set_symbol_leverage(symbol="ETHUSDT", leverage=7)["symbol_leverage_map"]["ETHUSDT"] == 7.0

    payload = controller._status_snapshot()
    assert payload["capital_snapshot"]["budget_usdt"] == 3.5
    assert payload["capital_snapshot"]["leverage"] == 7.0
    assert payload["capital_snapshot"]["notional_usdt"] == 24.5


def test_control_api_status_reports_safe_mode_without_mislabeling_as_ops_paused(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    controller.ops.safe_mode()

    payload = controller._status_snapshot()

    assert payload["capital_snapshot"]["blocked"] is True
    assert payload["capital_snapshot"]["block_reason"] == "safe_mode"


def test_control_api_status_surfaces_alpha_reject_diagnostics(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_alpha_reject.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoCandidateDiagnostics:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="volume_missing", candidate=None)

        def get_last_no_candidate_context(self) -> dict[str, Any]:
            return {
                "BTCUSDT": {
                    "reason": "volume_missing",
                    "alpha_blocks": {
                        "alpha_breakout": "volume_missing",
                        "alpha_expansion": "trigger_missing",
                    },
                    "alpha_reject_metrics": {
                        "alpha_breakout": {
                            "vol_ratio_15m": 0.82,
                            "min_volume_ratio_15m": 0.9,
                        }
                    },
                }
            }

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoCandidateDiagnostics(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    out = controller.tick_scheduler_now()
    status = controller._status_snapshot()

    assert out["snapshot"]["last_decision_reason"] == "volume_missing"
    assert status["config_summary"]["risk_runtime"]["last_alpha_reject_focus"] == (
        "alpha_breakout:volume_missing, alpha_expansion:trigger_missing"
    )
    assert status["config_summary"]["risk_runtime"]["last_alpha_blocks"]["alpha_breakout"] == (
        "volume_missing"
    )
    assert (
        status["config_summary"]["risk_runtime"]["last_alpha_reject_metrics"]["alpha_breakout"][
            "min_volume_ratio_15m"
        ]
        == 0.9
    )


def test_control_api_status_uses_recent_cached_balance_after_fetch_failure(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_balance_cached.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    class _FlakyBalanceREST:
        def __init__(self) -> None:
            self.calls = 0

        async def get_balances(self):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return [{"asset": "USDT", "availableBalance": "111.11", "walletBalance": "222.22"}]
            raise BinanceRESTError(
                status_code=500,
                code=-1000,
                message="internal error",
                path="/fapi/v2/balance",
            )

    rest = _FlakyBalanceREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    first = client.get("/status")
    assert first.status_code == 200
    p1 = first.json()
    assert p1["binance"]["usdt_balance"]["source"] == "exchange"
    assert p1["binance"]["private_error"] is None

    second = client.get("/status")
    assert second.status_code == 200
    p2 = second.json()
    assert p2["capital_snapshot"]["available_usdt"] == 111.11
    assert p2["binance"]["usdt_balance"]["wallet"] == 222.22
    assert p2["binance"]["usdt_balance"]["source"] in {"exchange", "exchange_cached"}
    assert p2["binance"]["private_error"] is None


def test_control_api_status_retries_balance_fetch_once_before_fallback(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_balance_retry.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    class _RetryBalanceREST:
        def __init__(self) -> None:
            self.calls = 0

        async def get_balances(self):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary_failure")
            return [{"asset": "USDT", "availableBalance": "77.7", "walletBalance": "88.8"}]

    rest = _RetryBalanceREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    assert rest.calls == 2
    assert payload["binance"]["usdt_balance"]["source"] == "exchange"
    assert payload["binance"]["private_error"] is None


def test_control_api_tick_coalesces_when_background_cycle_completes(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_tick_coalesce.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    controller._running = True
    controller._cycle_seq = 3
    controller._cycle_done_seq = 2
    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "no_candidate"

    _ = controller._lock.acquire(timeout=1.0)

    def _finish_cycle() -> None:
        time.sleep(0.25)
        controller._cycle_done_seq = 3
        controller._last_cycle["tick_finished_at"] = "2026-01-01T00:00:00+00:00"

    worker = threading.Thread(target=_finish_cycle, daemon=True)
    worker.start()
    try:
        out = controller.tick_scheduler_now()
    finally:
        controller._lock.release()
        worker.join(timeout=1.0)

    assert out["ok"] is True
    assert out["error"] is None
    assert bool(out["snapshot"].get("coalesced")) is True


def test_control_api_syncs_kernel_runtime_overrides(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_verified_q070", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_sync.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelStub:
        def __init__(self) -> None:
            self.symbols: list[str] = []
            self.mapping: dict[str, float] = {}
            self.max_leverage: float = 1.0
            self.fallback_notional: float = 0.0
            self.max_notional: float | None = None

        def set_universe_symbols(self, symbols: list[str]) -> None:
            self.symbols = list(symbols)

        def set_symbol_leverage_map(
            self, mapping: dict[str, float], *, max_leverage: float
        ) -> None:
            self.mapping = dict(mapping)
            self.max_leverage = float(max_leverage)

        def set_notional_config(
            self,
            *,
            fallback_notional: float,
            max_notional: float | None,
        ) -> None:
            self.fallback_notional = float(fallback_notional)
            self.max_notional = max_notional

        def run_once(self):  # type: ignore[no-untyped-def]
            from v2.kernel.contracts import KernelCycleResult

            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    kernel = _KernelStub()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    set_uni = controller.set_value(key="universe_symbols", value="BTCUSDT,ETHUSDT")
    assert set_uni["applied_value"] == ["BTCUSDT", "ETHUSDT"]
    assert kernel.symbols == ["BTCUSDT", "ETHUSDT"]

    set_budget = controller.set_value(key="margin_budget_usdt", value="35")
    assert set_budget["applied_value"] == 35
    assert kernel.fallback_notional == 3.5

    set_cap = controller.set_value(key="max_position_notional_usdt", value="120")
    assert set_cap["applied_value"] == 120
    assert kernel.max_notional == 120.0
    assert kernel.fallback_notional == 3.5

    _ = controller.set_value(key="max_leverage", value="20")
    assert kernel.fallback_notional == 3.5
    lev = controller.set_symbol_leverage(symbol="ETHUSDT", leverage=7)
    assert lev["symbol_leverage_map"]["ETHUSDT"] == 7.0
    assert kernel.fallback_notional == 3.5
    assert kernel.mapping.get("ETHUSDT") == 7.0
    assert kernel.max_leverage == 20.0


def test_control_api_symbol_leverage_lifts_runtime_max_when_needed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path, profile="ra_2026_alpha_v2_expansion_live_candidate")

    payload = controller.set_symbol_leverage(symbol="BTCUSDT", leverage=12)

    assert payload["symbol_leverage_map"]["BTCUSDT"] == 12.0
    assert payload["max_leverage"] == 12.0

    status_payload = controller._status_snapshot()
    assert status_payload["capital_snapshot"]["leverage"] == 12.0
    assert status_payload["capital_snapshot"]["notional_usdt"] == 1200.0

    candidate = Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0)
    context = KernelContext(
        mode="shadow",
        profile=controller.cfg.profile,
        symbol="BTCUSDT",
        tick=1,
        dry_run=True,
    )
    risk = controller.kernel._risk_gate.evaluate(candidate=candidate, context=context)
    size = controller.kernel._sizer.size(candidate=candidate, risk=risk, context=context)
    assert size.leverage == 12.0


def test_control_api_persists_risk_config_across_restart(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    sqlite_path = str(tmp_path / "control_persist.sqlite3")
    cfg.behavior.storage.sqlite_path = sqlite_path

    storage1 = RuntimeStorage(sqlite_path=sqlite_path)
    storage1.ensure_schema()
    state_store1 = EngineStateStore(storage=storage1, mode=cfg.mode)
    event_bus1 = EventBus()
    scheduler1 = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus1)
    ops1 = OpsController(state_store=state_store1, exchange=None)
    kernel1 = build_default_kernel(
        state_store=state_store1,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller1 = build_runtime_controller(
        cfg=cfg,
        state_store=state_store1,
        ops=ops1,
        kernel=kernel1,
        scheduler=scheduler1,
        event_bus=event_bus1,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    assert controller1.set_value(key="margin_budget_usdt", value="250")["applied_value"] == 250
    assert controller1.set_value(key="universe_symbols", value="BTCUSDT,ETHUSDT")["applied_value"] == [
        "BTCUSDT",
        "ETHUSDT",
    ]
    assert controller1.set_value(key="max_leverage", value="15")["applied_value"] == 15
    assert controller1.set_symbol_leverage(symbol="ETHUSDT", leverage=8)["symbol_leverage_map"]["ETHUSDT"] == 8.0

    storage2 = RuntimeStorage(sqlite_path=sqlite_path)
    storage2.ensure_schema()
    state_store2 = EngineStateStore(storage=storage2, mode=cfg.mode)
    event_bus2 = EventBus()
    scheduler2 = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus2)
    ops2 = OpsController(state_store=state_store2, exchange=None)
    kernel2 = build_default_kernel(
        state_store=state_store2,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller2 = build_runtime_controller(
        cfg=cfg,
        state_store=state_store2,
        ops=ops2,
        kernel=kernel2,
        scheduler=scheduler2,
        event_bus=event_bus2,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    payload = controller2.get_risk()
    assert payload["margin_budget_usdt"] == 250
    assert payload["max_leverage"] == 15
    assert payload["universe_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert payload["symbol_leverage_map"]["ETHUSDT"] == 8.0


def test_control_api_restores_kernel_runtime_overrides_after_restart(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    sqlite_path = str(tmp_path / "control_kernel_restart.sqlite3")
    cfg.behavior.storage.sqlite_path = sqlite_path

    class _KernelStub:
        def __init__(self) -> None:
            self.symbols: list[str] = []
            self.mapping: dict[str, float] = {}
            self.max_leverage: float = 1.0
            self.fallback_notional: float = 0.0
            self.max_notional: float | None = None

        def set_universe_symbols(self, symbols: list[str]) -> None:
            self.symbols = list(symbols)

        def set_symbol_leverage_map(
            self, mapping: dict[str, float], *, max_leverage: float
        ) -> None:
            self.mapping = dict(mapping)
            self.max_leverage = float(max_leverage)

        def set_notional_config(
            self,
            *,
            fallback_notional: float,
            max_notional: float | None,
        ) -> None:
            self.fallback_notional = float(fallback_notional)
            self.max_notional = max_notional

        def run_once(self):  # type: ignore[no-untyped-def]
            from v2.kernel.contracts import KernelCycleResult

            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    storage1 = RuntimeStorage(sqlite_path=sqlite_path)
    storage1.ensure_schema()
    state_store1 = EngineStateStore(storage=storage1, mode=cfg.mode)
    event_bus1 = EventBus()
    scheduler1 = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus1)
    ops1 = OpsController(state_store=state_store1, exchange=None)
    kernel1 = _KernelStub()
    controller1 = build_runtime_controller(
        cfg=cfg,
        state_store=state_store1,
        ops=ops1,
        kernel=kernel1,
        scheduler=scheduler1,
        event_bus=event_bus1,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    assert controller1.set_value(key="margin_budget_usdt", value="250")["applied_value"] == 250
    assert controller1.set_value(key="max_leverage", value="15")["applied_value"] == 15
    assert controller1.set_value(key="universe_symbols", value="BTCUSDT,ETHUSDT")["applied_value"] == [
        "BTCUSDT",
        "ETHUSDT",
    ]
    assert controller1.set_symbol_leverage(symbol="ETHUSDT", leverage=8)["symbol_leverage_map"]["ETHUSDT"] == 8.0

    storage2 = RuntimeStorage(sqlite_path=sqlite_path)
    storage2.ensure_schema()
    state_store2 = EngineStateStore(storage=storage2, mode=cfg.mode)
    event_bus2 = EventBus()
    scheduler2 = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus2)
    ops2 = OpsController(state_store=state_store2, exchange=None)
    kernel2 = _KernelStub()
    _ = build_runtime_controller(
        cfg=cfg,
        state_store=state_store2,
        ops=ops2,
        kernel=kernel2,
        scheduler=scheduler2,
        event_bus=event_bus2,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    assert kernel2.fallback_notional == 250.0
    assert kernel2.max_notional is None
    assert kernel2.max_leverage == 15.0
    assert kernel2.mapping == {"ETHUSDT": 8.0}
    assert kernel2.symbols == ["BTCUSDT", "ETHUSDT"]


def test_persisted_scheduler_tick_migrates_default_stale_thresholds_on_restart(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    sqlite_path = str(tmp_path / "control_scheduler_threshold_restart.sqlite3")
    cfg.behavior.storage.sqlite_path = sqlite_path

    storage1 = RuntimeStorage(sqlite_path=sqlite_path)
    storage1.ensure_schema()
    state_store1 = EngineStateStore(storage=storage1, mode=cfg.mode)
    event_bus1 = EventBus()
    scheduler1 = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus1)
    ops1 = OpsController(state_store=state_store1, exchange=None)
    kernel1 = build_default_kernel(
        state_store=state_store1,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller1 = build_runtime_controller(
        cfg=cfg,
        state_store=state_store1,
        ops=ops1,
        kernel=kernel1,
        scheduler=scheduler1,
        event_bus=event_bus1,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    controller1._risk["scheduler_tick_sec"] = 600
    controller1._risk["user_ws_stale_sec"] = 120.0
    controller1._risk["market_data_stale_sec"] = 60.0
    controller1._risk["watchdog_interval_sec"] = 30.0
    controller1._persist_risk_config()

    storage2 = RuntimeStorage(sqlite_path=sqlite_path)
    storage2.ensure_schema()
    state_store2 = EngineStateStore(storage=storage2, mode=cfg.mode)
    event_bus2 = EventBus()
    scheduler2 = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus2)
    ops2 = OpsController(state_store=state_store2, exchange=None)
    kernel2 = build_default_kernel(
        state_store=state_store2,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller2 = build_runtime_controller(
        cfg=cfg,
        state_store=state_store2,
        ops=ops2,
        kernel=kernel2,
        scheduler=scheduler2,
        event_bus=event_bus2,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )

    risk = controller2.get_risk()
    assert risk["scheduler_tick_sec"] == 600
    assert risk["user_ws_stale_sec"] == 2400.0
    assert risk["market_data_stale_sec"] == 1200.0
    assert risk["watchdog_interval_sec"] == 600.0


def test_live_tick_does_not_preblock_multi_symbol_scan_when_live_position_exists(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_live_block_reentry.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _LivePosREST:
        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
            _ = symbol
            return []

        async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            _ = params
            return {}

    kernel = _KernelNoCandidate()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_LivePosREST(),
    )
    controller._running = True
    controller._risk["universe_symbols"] = ["BTCUSDT", "ETHUSDT"]
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["snapshot"]["last_action"] == "no_candidate"
    assert tick.json()["snapshot"]["last_decision_reason"] == "no_candidate"
    assert tick.json()["snapshot"]["last_error"] is None
    assert tick.json().get("error") is None
    assert kernel.calls == 1


def test_live_tick_allows_reentry_when_flag_enabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_live_allow_reentry.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _LivePosREST:
        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
            _ = symbol
            return []

        async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            _ = params
            return {}

    kernel = _KernelNoCandidate()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_LivePosREST(),
    )
    controller._running = True
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    enable = client.post("/set", json={"key": "allow_reentry", "value": "true"})
    assert enable.status_code == 200

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["snapshot"]["last_action"] == "no_candidate"
    assert kernel.calls == 1


def test_live_reentry_fetch_failure_blocks_and_sets_uncertainty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _FlakyPositionsREST:
        def __init__(self) -> None:
            self.position_calls = 0

        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            self.position_calls += 1
            if self.position_calls == 1:
                return []
            raise RuntimeError("positions_down")

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    now_iso = datetime.now(timezone.utc).isoformat()
    kernel = _KernelNoCandidate()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_FlakyPositionsREST(),
        kernel=kernel,
        market_data_state={
            "last_market_data_at": now_iso,
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": now_iso,
        },
    )
    controller._running = True
    controller._user_stream_started = True
    controller._user_stream_started_at = now_iso
    controller._last_private_stream_ok_at = now_iso

    out = controller.tick_scheduler_now()

    assert out["snapshot"]["last_action"] == "blocked"
    assert out["snapshot"]["last_decision_reason"] == "state_uncertain"
    assert controller._status_snapshot()["state_uncertain"] is True
    assert controller._status_snapshot()["state_uncertain_reason"] == "live_positions_fetch_failed"
    assert kernel.calls == 0


def test_live_startup_reconcile_populates_exchange_position_and_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.010",
                    "entryPrice": "100000",
                    "unRealizedProfit": "5.0",
                }
            ]

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, state_store, ops = _build_live_controller(tmp_path, rest_client=_ReconREST())

    state = state_store.get()
    assert state.current_position["BTCUSDT"].position_amt == 0.01
    assert state.last_reconcile_at is not None
    assert ops.can_open_new_entries() is True

    status = controller._status_snapshot()
    assert status["state_uncertain"] is False
    assert status["startup_reconcile_ok"] is True
    assert status["last_reconcile_at"] is not None


def test_start_auto_reconciles_dirty_restart_before_running(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        dirty_restart_detected=True,
    )

    out = controller.start()
    status = controller._status_snapshot()

    assert out["state"] == "RUNNING"
    assert status["recovery_required"] is False
    assert status["startup_reconcile_ok"] is True
    assert ops.can_open_new_entries() is True
    controller.stop()


def test_start_does_not_spawn_duplicate_worker_thread(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def _fake_loop_worker() -> None:
        entered.set()
        release.wait(timeout=2.0)

    controller._loop_worker = _fake_loop_worker  # type: ignore[method-assign]

    first = controller.start()
    assert first["state"] == "RUNNING"
    assert entered.wait(timeout=1.0)
    thread = controller._thread
    assert thread is not None

    second = controller.start()
    assert second["state"] == "RUNNING"
    assert controller._thread is thread

    release.set()
    controller._thread_stop.set()
    thread.join(timeout=1.0)
    controller.stop()


def test_stop_waits_for_worker_thread_to_drain(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    started = threading.Event()

    def _worker() -> None:
        started.set()
        while not controller._thread_stop.is_set():
            time.sleep(0.01)

    worker = threading.Thread(target=_worker, daemon=True)
    controller._running = True
    controller._thread = worker
    worker.start()
    assert started.wait(timeout=1.0)

    out = controller.stop()

    assert out["state"] == "PAUSED"
    assert worker.is_alive() is False
    assert controller._running is False


def test_live_startup_reconcile_failure_sets_uncertainty_and_safe_mode(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _FailREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return []

    controller, _state_store, ops = _build_live_controller(tmp_path, rest_client=_FailREST())

    status = controller._status_snapshot()
    assert status["state_uncertain"] is True
    assert status["startup_reconcile_ok"] is False
    assert str(status["state_uncertain_reason"]).startswith("startup_reconcile:")
    assert ops.can_open_new_entries() is False


def test_live_uncertainty_blocks_new_entries_before_kernel_execution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _FailREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return []

    class _KernelExecuted:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100000.0),
                size=SizePlan(symbol="BTCUSDT", qty=0.01, leverage=2.0, notional=1000.0),
                execution=ExecutionResult(ok=True, order_id="oid-1", reason="live_order_submitted"),
            )

    kernel = _KernelExecuted()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_FailREST(),
        kernel=kernel,
    )

    out = controller.tick_scheduler_now()
    assert out["ok"] is True
    assert out["snapshot"]["last_action"] == "blocked"
    assert out["snapshot"]["last_decision_reason"] == "state_uncertain"
    assert kernel.calls == 0


def test_live_user_stream_disconnect_resync_and_event_flow_update_state(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "100000"}]

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _FakeUserStreamManager:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False
            self.on_event = None
            self.on_resync = None
            self.on_disconnect = None
            self.on_private_ok = None

        def start(  # type: ignore[no-untyped-def]
            self, *, on_event=None, on_resync=None, on_disconnect=None, on_private_ok=None
        ):
            self.started = True
            self.on_event = on_event
            self.on_resync = on_resync
            self.on_disconnect = on_disconnect
            self.on_private_ok = on_private_ok

        async def stop(self) -> None:
            self.stopped = True

    manager = _FakeUserStreamManager()
    controller, state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        user_stream_manager=manager,
    )

    try:
        asyncio.run(controller.start_live_services())
        assert manager.started is True
        assert controller._status_snapshot()["state_uncertain"] is False

        asyncio.run(manager.on_disconnect("socket_closed"))
        assert controller._status_snapshot()["state_uncertain"] is True

        asyncio.run(
            manager.on_resync(
                ResyncSnapshot(
                    open_orders=[],
                    positions=[
                        {"symbol": "ETHUSDT", "positionAmt": "0.050", "entryPrice": "2500.0"}
                    ],
                    balances=[{"asset": "USDT", "availableBalance": "1000"}],
                )
            )
        )
        status = controller._status_snapshot()
        assert status["state_uncertain"] is False
        assert state_store.get().current_position["ETHUSDT"].position_amt == 0.05

        asyncio.run(
            manager.on_event(
                {
                    "e": "ACCOUNT_UPDATE",
                    "E": 1700000002000,
                    "a": {"P": [{"s": "ETHUSDT", "pa": "0", "ep": "2500.0", "up": "0"}]},
                }
            )
        )
        assert "ETHUSDT" not in state_store.get().current_position
    finally:
        asyncio.run(controller.stop_live_services())

    assert manager.stopped is True


def test_live_controller_boot_does_not_emit_transient_not_ready_notification(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_verified_q070",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_live_boot_notify.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
            _ = symbol
            return []

        async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            _ = params
            return {}

    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=False,
        rest_client=_ReconREST(),
    )
    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )

    _ = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=_ReconREST(),
        user_stream_manager=None,
        market_data_state={
            "last_market_data_at": None,
            "last_market_symbol_count": 0,
            "last_market_data_source_ok_at": None,
            "last_market_data_source_fail_at": None,
            "last_market_data_source_error": None,
        },
        runtime_lock_active=True,
    )

    assert notifier.send_notification.call_count == 0


def test_start_live_services_does_not_emit_transient_not_ready_notification(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _FakeUserStreamManager:
        def __init__(self) -> None:
            self.started = False

        def start(  # type: ignore[no-untyped-def]
            self, *, on_event=None, on_resync=None, on_disconnect=None, on_private_ok=None
        ):
            _ = on_event
            _ = on_resync
            _ = on_disconnect
            _ = on_private_ok
            self.started = True

        async def stop(self) -> None:
            return None

    manager = _FakeUserStreamManager()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        user_stream_manager=manager,
        market_data_state={
            "last_market_data_at": None,
            "last_market_symbol_count": 0,
            "last_market_data_source_ok_at": None,
            "last_market_data_source_fail_at": None,
            "last_market_data_source_error": None,
        },
    )
    notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )
    controller.notifier = notifier
    notifier.send_notification.reset_mock()

    asyncio.run(controller.start_live_services())

    assert manager.started is True
    assert notifier.send_notification.call_count == 0


def test_start_live_services_primes_market_data_before_first_tick(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return []

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _FakeUserStreamManager:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        def start(  # type: ignore[no-untyped-def]
            self, *, on_event=None, on_resync=None, on_disconnect=None, on_private_ok=None
        ):
            _ = on_event
            _ = on_resync
            _ = on_disconnect
            _ = on_private_ok
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    manager = _FakeUserStreamManager()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        user_stream_manager=manager,
        market_data_state={
            "last_market_data_at": None,
            "last_market_symbol_count": 0,
            "last_market_data_source_ok_at": None,
            "last_market_data_source_fail_at": None,
            "last_market_data_source_error": None,
        },
    )

    def _prime_market_data() -> None:
        controller._market_data_state["last_market_data_at"] = datetime.now(timezone.utc).isoformat()
        controller._market_data_state["last_market_data_source_ok_at"] = controller._market_data_state[
            "last_market_data_at"
        ]
        controller._market_data_state["last_market_symbol_count"] = 1

    controller._maybe_probe_market_data = _prime_market_data  # type: ignore[method-assign]

    try:
        asyncio.run(controller.start_live_services())
        readyz = controller._readyz_snapshot()
        assert manager.started is True
        assert readyz["last_market_data_at"] is not None
        assert readyz["last_market_data_source_ok_at"] is not None
    finally:
        asyncio.run(controller.stop_live_services())


def test_live_user_stream_private_ok_clears_disconnect_uncertainty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._risk["user_ws_stale_sec"] = 60.0

    asyncio.run(controller._handle_user_stream_disconnect("socket_closed"))
    status = controller._status_snapshot()
    assert status["state_uncertain"] is True
    assert status["state_uncertain_reason"] == "socket_closed"
    assert controller._user_stream_last_error == "socket_closed"

    asyncio.run(controller._handle_user_stream_private_ok("keepalive"))
    status = controller._status_snapshot()
    assert status["state_uncertain"] is False
    assert status["state_uncertain_reason"] is None
    assert controller._user_stream_last_error is None


def test_live_user_stream_private_ok_does_not_clear_non_stream_uncertainty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._risk["user_ws_stale_sec"] = 60.0
    controller._set_state_uncertain(reason="live_positions_fetch_failed", engage_safe_mode=False)

    asyncio.run(controller._handle_user_stream_private_ok("keepalive"))
    status = controller._status_snapshot()
    assert status["state_uncertain"] is True
    assert status["state_uncertain_reason"] == "live_positions_fetch_failed"


def test_live_user_stream_resync_failure_sets_uncertainty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _FakeUserStreamManager:
        def __init__(self) -> None:
            self.on_resync = None
            self.on_private_ok = None

        def start(  # type: ignore[no-untyped-def]
            self, *, on_event=None, on_resync=None, on_disconnect=None, on_private_ok=None
        ):
            _ = on_event
            _ = on_disconnect
            self.on_resync = on_resync
            self.on_private_ok = on_private_ok

        async def stop(self) -> None:
            return None

    manager = _FakeUserStreamManager()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        user_stream_manager=manager,
    )

    original = controller.state_store.startup_reconcile

    def _broken_startup_reconcile(*, snapshot, reason):  # type: ignore[no-untyped-def]
        _ = snapshot
        _ = reason
        raise RuntimeError("resync_boom")

    controller.state_store.startup_reconcile = _broken_startup_reconcile  # type: ignore[method-assign]
    try:
        asyncio.run(controller.start_live_services())
        try:
            asyncio.run(
                manager.on_resync(
                    ResyncSnapshot(
                        open_orders=[],
                        positions=[],
                        balances=[{"asset": "USDT", "availableBalance": "1000"}],
                    )
                )
            )
        except RuntimeError as exc:
            assert str(exc) == "resync_boom"
        status = controller._status_snapshot()
        assert status["state_uncertain"] is True
        assert status["state_uncertain_reason"] == "resync_failed:RuntimeError"
    finally:
        controller.state_store.startup_reconcile = original  # type: ignore[method-assign]
        asyncio.run(controller.stop_live_services())


def test_live_uncertainty_reconcile_action_clears_state_when_exchange_recovers(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _FailREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return []

    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "100000"}]

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, state_store, _ops = _build_live_controller(tmp_path, rest_client=_FailREST())
    assert controller._status_snapshot()["state_uncertain"] is True

    controller.rest_client = _HealthyREST()
    out = asyncio.run(controller.reconcile_now())

    assert out["ok"] is True
    assert out["state_uncertain"] is False
    assert out["startup_reconcile_ok"] is True
    assert state_store.get().current_position["BTCUSDT"].position_amt == 0.01


def test_live_uncertainty_still_allows_flatten_reduce_only_recovery(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _FailREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return []

    class _FlattenExchange:
        def __init__(self) -> None:
            self.reduce_only_calls: list[dict[str, Any]] = []

        async def cancel_all_open_orders(self, *, symbol: str) -> dict[str, Any]:
            _ = symbol
            return {}

        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
            _ = symbol
            return []

        async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            _ = params
            return {}

        async def get_positions(self) -> list[dict[str, Any]]:
            if self.reduce_only_calls:
                return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

        async def place_reduce_only_market_order(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            position_side: str = "BOTH",
        ) -> dict[str, Any]:
            self.reduce_only_calls.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "position_side": position_side,
                }
            )
            return {}

    controller, _state_store, _ops = _build_live_controller(tmp_path, rest_client=_FailREST())
    exchange = _FlattenExchange()
    controller.ops.exchange = exchange

    out = asyncio.run(controller.close_position(symbol="BTCUSDT"))

    assert out["symbol"] == "BTCUSDT"
    assert len(exchange.reduce_only_calls) == 1
    assert exchange.reduce_only_calls[0]["side"] == "SELL"


def test_live_market_data_stale_blocks_new_entries_and_unblocks_when_fresh(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    market_data_state = {
        "last_market_data_at": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
        "last_market_symbol_count": 1,
    }
    kernel = _KernelNoCandidate()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        kernel=kernel,
        market_data_state=market_data_state,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    controller._user_stream_started = True
    controller._user_stream_started_at = now_iso
    controller._last_private_stream_ok_at = now_iso
    controller._risk["market_data_stale_sec"] = 5.0
    controller._risk["user_ws_stale_sec"] = 60.0

    blocked = controller.tick_scheduler_now()
    assert blocked["snapshot"]["last_decision_reason"] == "market_data_stale"
    assert kernel.calls == 0

    controller._market_data_state["last_market_data_at"] = datetime.now(timezone.utc).isoformat()
    recovered = controller.tick_scheduler_now()
    assert recovered["snapshot"]["last_decision_reason"] == "no_candidate"
    assert kernel.calls == 1


def test_live_position_open_cycle_refreshes_market_data_before_stale_trip(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    class _OpenPositionREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    now = datetime.now(timezone.utc)
    recent_iso = (now - timedelta(seconds=4)).isoformat()
    market_data_state = {
        "last_market_data_at": recent_iso,
        "last_market_symbol_count": 1,
        "last_market_data_source_ok_at": recent_iso,
        "last_market_data_source_fail_at": None,
        "last_market_data_source_error": None,
    }
    kernel = _KernelNoCandidate()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_OpenPositionREST(),
        kernel=kernel,
        market_data_state=market_data_state,
    )
    now_iso = now.isoformat()
    controller._running = True
    controller._user_stream_started = True
    controller._user_stream_started_at = now_iso
    controller._last_private_stream_ok_at = now_iso
    controller._risk["market_data_stale_sec"] = 5.0
    controller._risk["user_ws_stale_sec"] = 60.0

    probe_calls: list[int] = []

    def _prime_market_data() -> None:
        probe_calls.append(1)
        refreshed = datetime.now(timezone.utc).isoformat()
        controller._market_data_state["last_market_data_at"] = refreshed
        controller._market_data_state["last_market_data_source_ok_at"] = refreshed
        controller._market_data_state["last_market_symbol_count"] = 1
        controller._market_data_state["last_market_data_source_fail_at"] = None
        controller._market_data_state["last_market_data_source_error"] = None

    controller._maybe_probe_market_data = _prime_market_data  # type: ignore[method-assign]

    out = controller.tick_scheduler_now()

    assert out["ok"] is True
    assert out["snapshot"]["last_decision_reason"] == "position_open"
    assert len(probe_calls) == 1
    assert controller._freshness_snapshot()["market_data_stale"] is False
    assert kernel.calls == 0


def test_live_position_open_cycle_does_not_reemit_stale_entry_notification(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    class _SwitchingPositionREST:
        def __init__(self) -> None:
            self.position_amt = "0"

        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return [{"symbol": "BTCUSDT", "positionAmt": self.position_amt, "entryPrice": "66547.9"}]

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _StickyExecutedKernel:
        def __init__(self) -> None:
            self.calls = 0
            self._last_portfolio_cycle: PortfolioCycleResult | None = None

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            result = KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="BTCUSDT",
                    side="SELL",
                    score=1.0,
                    entry_price=66547.9,
                ),
                size=SizePlan(
                    symbol="BTCUSDT",
                    qty=0.015553,
                    leverage=45.0,
                    notional=1035.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(ok=True, order_id="oid-short-1", reason="live_order_submitted"),
            )
            self._last_portfolio_cycle = PortfolioCycleResult(
                primary_result=result,
                selected_candidates=[result.candidate] if result.candidate is not None else [],
                results=[result],
                open_position_count=0,
                max_open_positions=1,
            )
            return result

        def last_portfolio_cycle(self) -> PortfolioCycleResult | None:
            return self._last_portfolio_cycle

    rest = _SwitchingPositionREST()
    kernel = _StickyExecutedKernel()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=rest,
        kernel=kernel,
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    controller._running = True
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    controller.notifier = Notifier(enabled=True, provider="ntfy", ntfy_topic="ops-alerts")
    controller.notifier.send_notification = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=True, error=None)
    )

    first = controller.tick_scheduler_now()
    assert first["snapshot"]["last_action"] == "executed"
    assert kernel.calls == 1

    controller.notifier.send_notification.reset_mock()  # type: ignore[attr-defined]
    rest.position_amt = "0.015553"

    second = controller.tick_scheduler_now()

    assert second["snapshot"]["last_decision_reason"] == "position_open"
    assert kernel.calls == 1
    assert controller.notifier.send_notification.call_count == 1  # type: ignore[attr-defined]
    notification = controller.notifier.send_notification.call_args[0][0]  # type: ignore[attr-defined]
    assert isinstance(notification, NotificationMessage)
    assert notification.title == "포지션 관리중"


def test_live_user_stream_stale_blocks_new_entries_and_private_ok_unblocks(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    market_data_state = {
        "last_market_data_at": datetime.now(timezone.utc).isoformat(),
        "last_market_symbol_count": 1,
    }
    kernel = _KernelNoCandidate()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        kernel=kernel,
        market_data_state=market_data_state,
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=120)
    ).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    controller._risk["market_data_stale_sec"] = 60.0
    controller._risk["user_ws_stale_sec"] = 5.0

    blocked = controller.tick_scheduler_now()
    assert blocked["snapshot"]["last_decision_reason"] == "user_ws_stale"
    assert kernel.calls == 0

    asyncio.run(controller._handle_user_stream_private_ok("test"))
    recovered = controller.tick_scheduler_now()
    assert recovered["snapshot"]["last_decision_reason"] == "no_candidate"
    assert kernel.calls == 1


def test_live_stale_state_still_allows_flatten_and_reconcile(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _FlattenExchange:
        def __init__(self) -> None:
            self.reduce_only_calls = 0

        async def cancel_all_open_orders(self, *, symbol: str) -> dict[str, Any]:
            _ = symbol
            return {}

        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
            _ = symbol
            return []

        async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            _ = params
            return {}

        async def get_positions(self) -> list[dict[str, Any]]:
            if self.reduce_only_calls > 0:
                return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

        async def place_reduce_only_market_order(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            position_side: str = "BOTH",
        ) -> dict[str, Any]:
            _ = symbol
            _ = side
            _ = quantity
            _ = position_side
            self.reduce_only_calls += 1
            return {}

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        market_data_state={
            "last_market_data_at": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
            "last_market_symbol_count": 1,
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    controller._risk["market_data_stale_sec"] = 5.0
    controller.ops.exchange = _FlattenExchange()

    out = asyncio.run(controller.close_position(symbol="BTCUSDT"))
    assert out["symbol"] == "BTCUSDT"

    reconcile = asyncio.run(controller.reconcile_now())
    assert reconcile["ok"] is True


def test_readyz_and_healthz_reflect_uncertainty_stale_and_recovery(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        rest_client=_HealthyREST(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
        },
        dirty_restart_detected=True,
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    app = create_control_http_app(controller=controller)

    client = TestClient(app)
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["ready"] is False

    blocked = client.get("/readyz")
    assert blocked.status_code == 503
    assert blocked.json()["recovery_required"] is True

    reconcile = client.post("/reconcile")
    assert reconcile.status_code == 200

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True

    status = client.get("/status")
    assert status.status_code == 200
    assert status.json()["health"]["ready"] is True


def test_tick_auto_reconciles_dirty_restart_before_kernel_execution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    now_iso = datetime.now(timezone.utc).isoformat()
    kernel = _KernelNoCandidate()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        kernel=kernel,
        market_data_state={
            "last_market_data_at": now_iso,
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": now_iso,
        },
        dirty_restart_detected=True,
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = now_iso
    controller._last_private_stream_ok_at = now_iso

    out = controller.tick_scheduler_now()

    assert out["snapshot"]["last_decision_reason"] == "no_candidate"
    assert controller._status_snapshot()["recovery_required"] is False
    assert kernel.calls == 1


def test_readyz_fails_for_uncertainty_and_stale_freshness(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    market_data_state = {
        "last_market_data_at": datetime.now(timezone.utc).isoformat(),
        "last_market_symbol_count": 1,
    }
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        market_data_state=market_data_state,
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    app = create_control_http_app(controller=controller)

    client = TestClient(app)
    healthy = client.get("/readyz")
    assert healthy.status_code == 200

    controller._set_state_uncertain(reason="manual_test", engage_safe_mode=False)
    uncertain = client.get("/readyz")
    assert uncertain.status_code == 503
    assert uncertain.json()["state_uncertain"] is True

    controller._clear_state_uncertain()
    controller._last_private_stream_ok_at = (
        datetime.now(timezone.utc) - timedelta(seconds=120)
    ).isoformat()
    controller._risk["user_ws_stale_sec"] = 5.0
    stale_user = client.get("/readyz")
    assert stale_user.status_code == 503
    assert stale_user.json()["user_ws_stale"] is True

    controller._last_private_stream_ok_at = datetime.now(timezone.utc).isoformat()
    controller._market_data_state["last_market_data_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=120)
    ).isoformat()
    controller._risk["market_data_stale_sec"] = 5.0
    stale_market = client.get("/readyz")
    assert stale_market.status_code == 503
    assert stale_market.json()["market_data_stale"] is True


def test_readyz_stays_ready_with_fresh_user_stream_even_if_reconcile_age_is_old(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    now_iso = datetime.now(timezone.utc).isoformat()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        market_data_state={
            "last_market_data_at": now_iso,
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": now_iso,
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = now_iso
    controller._last_private_stream_ok_at = now_iso
    controller.state_store.get().last_reconcile_at = (
        datetime.now(timezone.utc) - timedelta(seconds=600)
    ).isoformat()

    readyz = controller._readyz_snapshot()
    readiness = controller._live_readiness_snapshot()

    assert readyz["ready"] is True
    assert readiness["checks"]["startup_reconcile"]["status"] == "pass"
    assert readiness["checks"]["startup_reconcile"]["detail"]["reconcile_age_ok"] is False
    assert readiness["checks"]["startup_reconcile"]["detail"]["reconcile_live_sync_ok"] is True


def test_readyz_fails_when_bracket_recovery_fails(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    async def _fail_recover(self) -> dict[str, Any]:
        raise RuntimeError("recover_boom")

    monkeypatch.setattr("v2.control.api.BracketService.recover", _fail_recover)

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at

    readyz = controller._readyz_snapshot()

    assert readyz["ready"] is False
    assert readyz["bracket_recovery_ok"] is False
    assert readyz["recovery_required"] is True
    assert readyz["recovery_reason"] == "bracket_recovery_exception"


def test_readyz_fails_when_private_rest_is_rate_limited_even_with_cached_balance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _RateLimitedREST:
        def __init__(self) -> None:
            self.balance_calls = 0

        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            self.balance_calls += 1
            if self.balance_calls == 1:
                return [{"asset": "USDT", "availableBalance": "1000", "walletBalance": "1000"}]
            raise BinanceRESTError(
                status_code=429,
                code=-1003,
                message="too many requests",
                path="/fapi/v2/balance",
            )

    now_iso = datetime.now(timezone.utc).isoformat()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_RateLimitedREST(),
        market_data_state={
            "last_market_data_at": now_iso,
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": now_iso,
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = now_iso
    controller._last_private_stream_ok_at = now_iso

    _ = controller._readyz_snapshot()
    second = controller._readyz_snapshot()

    assert second["ready"] is False
    assert second["private_auth_ok"] is False
    assert second["private_error"] == "balance_rate_limited"
    assert second["private_error_detail"] is not None


def test_runtime_risk_key_alias_normalizes_to_risk_per_trade_pct(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)

    out = controller.set_value(key="per_trade_risk_pct", value="7")
    risk = controller.get_risk()

    assert out["key"] == "risk_per_trade_pct"
    assert out["applied_value"] == 7
    assert risk["risk_per_trade_pct"] == 7
    assert risk["per_trade_risk_pct"] == 7


def test_controller_structured_logs_include_phase_b_fields(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
        },
    )
    with caplog.at_level(logging.INFO):
        controller._set_state_uncertain(reason="phase_b_test", engage_safe_mode=False)

    record = next(
        rec for rec in caplog.records if getattr(rec, "event", "") == "uncertainty_transition"
    )
    assert record.mode == "live"
    assert record.profile == controller.cfg.profile
    assert record.state_uncertain is True
    assert hasattr(record, "safe_mode")


def test_submit_review_required_blocks_until_manual_reconcile_resolves(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    storage = state_store.runtime_storage()
    storage.upsert_submission_intent(
        intent_id="intent-1",
        client_order_id="cid-1",
        symbol="BTCUSDT",
        side="BUY",
        status="REVIEW_REQUIRED",
    )

    blocked = controller.tick_scheduler_now()
    assert blocked["snapshot"]["last_decision_reason"] == "submit_recovery_required"

    out = asyncio.run(controller.reconcile_now())
    assert out["ok"] is True
    status = controller._status_snapshot()
    assert status["submission_recovery"]["pending_review_count"] == 0
    assert status["health"]["submission_recovery_ok"] is True


def test_manual_recovery_runs_bracket_recovery_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        dirty_restart_detected=True,
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at

    async def _recover() -> dict[str, Any]:
        return {"recovered": 1}

    controller._bracket_service.recover = _recover  # type: ignore[method-assign]
    out = asyncio.run(controller.reconcile_now())

    assert out["ok"] is True
    assert controller._status_snapshot()["boot_recovery"]["bracket_recovery"]["ok"] is True


def test_ambiguous_submit_failure_enters_uncertainty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _KernelAmbiguousFailure:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="execution_failed",
                reason="live_order_review_required:not_found_after_submit_error",
                candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100000.0),
            )

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        kernel=_KernelAmbiguousFailure(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at

    out = controller.tick_scheduler_now()
    assert out["snapshot"]["last_decision_reason"] == "live_order_review_required:not_found_after_submit_error"
    assert controller._status_snapshot()["state_uncertain"] is True


def test_status_exposes_runtime_identity_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        rest_client=_HealthyREST(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    status = controller._status_snapshot()
    assert status["profile"] == "ra_2026_alpha_v2_expansion_live_candidate"
    assert status["mode"] == "live"
    assert status["env"] == "testnet"
    assert status["runtime_identity"]["live_trading_enabled"] is False

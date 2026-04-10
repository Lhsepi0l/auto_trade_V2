from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from v2.config.loader import load_effective_config
from v2.exchange import BinanceRESTClient
from v2.run import (
    _build_control_balance_rest_client,
    _build_parser,
    _configure_runtime_logging,
    _dirty_runtime_marker,
    _evaluate_runtime_preflight,
    _is_vol_target_backtest_strategy,
    _live_runtime_lock,
    main,
)


def test_v2_shadow_startup_prints_effective_config(capsys) -> None:  # type: ignore[no-untyped-def]
    rc = main(["--mode", "shadow"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[v2] effective config" in out
    assert "[v2] runtime banner" in out
    assert '"live_trading_enabled": false' in out
    assert '"profile": "ra_2026_alpha_v2_expansion_verified_q070"' in out
    assert "[v2] started" in out


def test_control_balance_client_created_in_shadow_when_env_keys_present() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    client = _build_control_balance_rest_client(cfg=cfg, runtime_rest_client=None)
    assert isinstance(client, BinanceRESTClient)


def test_control_balance_client_reuses_runtime_client_when_available() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    runtime_client = object()
    client = _build_control_balance_rest_client(cfg=cfg, runtime_rest_client=runtime_client)
    assert client is runtime_client


def test_alpha_profile_shadow_startup_prints_effective_config(capsys) -> None:  # type: ignore[no-untyped-def]
    rc = main(["--profile", "ra_2026_alpha_v2", "--mode", "shadow"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"profile": "ra_2026_alpha_v2"' in out


def test_alpha_profile_is_supported_for_local_backtest() -> None:
    assert _is_vol_target_backtest_strategy("ra_2026_alpha_v2") is True
    assert _is_vol_target_backtest_strategy("ebc_v1_continuation") is True
    assert _is_vol_target_backtest_strategy("ra_2026_v1") is False


def test_live_runtime_lock_rejects_second_holder(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lock_path = tmp_path / "live_runtime.lock"
    monkeypatch.setenv("V2_RUNTIME_LOCK_FILE", str(lock_path))

    with _live_runtime_lock(enabled=True):
        with pytest.raises(RuntimeError, match="runtime_lock_held:"):
            with _live_runtime_lock(enabled=True):
                pass


def test_configure_runtime_logging_is_idempotent(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("V2_LOG_DIR", str(tmp_path))
    _configure_runtime_logging(component="phase_b_test")
    _configure_runtime_logging(component="phase_b_test")

    root = logging.getLogger()
    assert len(root.handlers) == 2

    logging.getLogger("phase_b_test").info("phase_b_log_probe")
    assert (tmp_path / "phase_b_test.log").exists()


def test_dirty_runtime_marker_writes_clean_shutdown_state(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="prod",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    marker_path = tmp_path / "live_runtime.dirty"
    state_path = tmp_path / "live_runtime_state.json"
    monkeypatch.setenv("V2_RUNTIME_DIRTY_MARKER_FILE", str(marker_path))
    monkeypatch.setenv("V2_RUNTIME_STATE_FILE", str(state_path))

    with _dirty_runtime_marker(
        enabled=True,
        cfg=cfg,
        state_provider=lambda: {"engine_state": "STOPPED"},
    ) as dirty_restart_detected:
        assert dirty_restart_detected is False
        assert marker_path.exists()

    assert marker_path.exists() is False
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["clean_shutdown"] is True
    assert payload["shutdown_reason"] == "graceful_shutdown"
    assert payload["last_state"]["engine_state"] == "STOPPED"


def test_runtime_preflight_reports_good_and_bad_gate() -> None:
    class _Controller:
        def __init__(self, *, ready: bool, private_auth_ok: bool = True) -> None:
            self._ready = ready
            self._private_auth_ok = private_auth_ok

        def _readyz_snapshot(self) -> dict[str, object]:
            return {
                "ready": self._ready,
                "recovery_required": not self._ready,
                "state_uncertain": not self._ready,
                "state_uncertain_reason": "gate_blocked" if not self._ready else None,
                "user_ws_stale": False,
                "market_data_stale": False,
                "market_data_source_stale": False,
                "market_data_source_error": None,
                "private_auth_ok": self._private_auth_ok,
            }

        def _live_readiness_snapshot(self) -> dict[str, object]:
            return {"overall": "ready" if self._ready else "blocked"}

    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_verified_q070",
        mode="live",
        env="prod",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    good = _evaluate_runtime_preflight(
        controller=_Controller(ready=True),
        cfg=cfg,
        host="127.0.0.1",
        port=8101,
    )
    bad = _evaluate_runtime_preflight(
        controller=_Controller(ready=False),
        cfg=cfg,
        host="0.0.0.0",
        port=8101,
    )

    assert good["ok"] is True
    assert bad["ok"] is False


def test_deploy_prep_parser_defaults_to_runtime_test_scope() -> None:
    parser = _build_parser()
    args = parser.parse_args([])
    assert args.test_scope == "runtime"
    assert args.profile == "ra_2026_alpha_v2_expansion_verified_q070"


def test_deploy_prep_parser_accepts_full_test_scope() -> None:
    parser = _build_parser()
    args = parser.parse_args(["--test-scope", "full"])
    assert args.test_scope == "full"


def test_serve_control_http_wires_webpush_service_when_operator_web_enabled(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    from v2.runtime import serve as serve_module
    from v2.storage import RuntimeStorage

    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_verified_q070",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "runtime.sqlite3"))
    storage.ensure_schema()

    class _StateStore:
        def __init__(self) -> None:
            self._status = SimpleNamespace(status="STOPPED")

        def set(self, *, mode: str, status: str) -> None:
            _ = mode
            self._status = SimpleNamespace(status=status)

        def get(self):
            return self._status

    captured: dict[str, object] = {}

    @contextmanager
    def _noop_lock(**kwargs):
        _ = kwargs
        yield

    @contextmanager
    def _noop_dirty_marker(**kwargs):
        _ = kwargs
        yield False

    monkeypatch.setattr(serve_module, "_configure_runtime_logging", lambda **_: None)
    monkeypatch.setattr(serve_module, "_live_runtime_lock", _noop_lock)
    monkeypatch.setattr(serve_module, "_dirty_runtime_marker", _noop_dirty_marker)
    monkeypatch.setattr(
        serve_module,
        "_build_runtime",
        lambda cfg: (
            storage,
            _StateStore(),
            SimpleNamespace(),
            SimpleNamespace(),
            None,
        ),
    )
    monkeypatch.setattr(serve_module, "build_notifier_from_config", lambda cfg: SimpleNamespace())
    monkeypatch.setattr(serve_module, "build_default_kernel", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(serve_module, "_build_control_balance_rest_client", lambda **kwargs: None)
    monkeypatch.setattr(serve_module, "create_control_http_app", lambda **kwargs: "app")

    def _capture_controller(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(serve_module, "build_runtime_controller", _capture_controller)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)

    rc = serve_module.serve_control_http(
        cfg,
        host="127.0.0.1",
        port=8101,
        enable_operator_web=True,
    )

    assert rc == 0
    assert captured["webpush_service"] is not None
    assert captured["webpush_service"].availability_snapshot()["public_key"]


def test_live_prod_direct_boot_is_blocked(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_verified_q070",
        mode="live",
        env="prod",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )

    monkeypatch.setattr("v2.run.load_effective_config", lambda **_: cfg)
    monkeypatch.setattr("v2.run._boot", lambda *_, **__: pytest.fail("unexpected direct boot"))

    rc = main(["--mode", "live", "--env", "prod"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "direct boot" in out
    assert "--control-http" in out


def test_live_prod_ops_http_is_blocked(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_verified_q070",
        mode="live",
        env="prod",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )

    monkeypatch.setattr("v2.run.load_effective_config", lambda **_: cfg)
    monkeypatch.setattr(
        "v2.run._serve_ops_http",
        lambda *_, **__: pytest.fail("unexpected ops-http start"),
    )

    rc = main(["--mode", "live", "--env", "prod", "--ops-http"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "--ops-http" in out
    assert "--control-http" in out

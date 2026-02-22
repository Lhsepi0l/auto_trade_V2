from __future__ import annotations

from v2.config.loader import load_effective_config
from v2.exchange import BinanceRESTClient
from v2.run import _build_control_balance_rest_client, main


def test_v2_shadow_startup_prints_effective_config(capsys) -> None:  # type: ignore[no-untyped-def]
    rc = main(["--mode", "shadow"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[v2] effective config" in out
    assert '"mode": "shadow"' in out
    assert "[v2] started" in out


def test_control_balance_client_created_in_shadow_when_env_keys_present() -> None:
    cfg = load_effective_config(
        profile="normal",
        mode="shadow",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    client = _build_control_balance_rest_client(cfg=cfg, runtime_rest_client=None)
    assert isinstance(client, BinanceRESTClient)


def test_control_balance_client_reuses_runtime_client_when_available() -> None:
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    runtime_client = object()
    client = _build_control_balance_rest_client(cfg=cfg, runtime_rest_client=runtime_client)
    assert client is runtime_client

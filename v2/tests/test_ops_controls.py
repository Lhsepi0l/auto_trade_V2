from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from v2.config.loader import load_root_config
from v2.engine import EngineStateStore
from v2.ops import OpsController, create_ops_http_app
from v2.run import main
from v2.storage import RuntimeStorage


class _FakeExchange:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._open_orders: list[dict[str, Any]] = [{"symbol": "BTCUSDT", "orderId": 1}]
        self._open_algo: list[dict[str, Any]] = [
            {"symbol": "BTCUSDT", "clientAlgoId": "algo-1"},
            {"symbol": "BTCUSDT", "algoId": 9002},
        ]
        self._positions: list[dict[str, Any]] = [{"symbol": "BTCUSDT", "positionAmt": "0.25"}]

    async def cancel_all_open_orders(self, *, symbol: str) -> dict[str, Any]:
        self.calls.append(f"cancel_regular:{symbol}")
        self._open_orders = [row for row in self._open_orders if str(row.get("symbol") or "") != symbol]
        return {"msg": "ok"}

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return list(self._open_orders)

    async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol is None:
            return list(self._open_algo)
        return [row for row in self._open_algo if str(row.get("symbol") or "") == symbol]

    async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
        key = "clientAlgoId" if params.get("clientAlgoId") is not None else "algoId"
        value = params.get(key)
        self.calls.append(f"cancel_algo:{params.get('symbol')}:{key}:{value}")
        target = str(value or "")
        self._open_algo = [row for row in self._open_algo if str(row.get(key) or "") != target]
        return {"msg": "ok"}

    async def get_positions(self) -> list[dict[str, Any]]:
        return list(self._positions)

    async def place_reduce_only_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        position_side: str = "BOTH",
    ) -> dict[str, Any]:
        _ = position_side
        self.calls.append(f"close_position:{symbol}:{side}:{quantity}")
        self._positions = [
            {"symbol": row["symbol"], "positionAmt": "0"} if str(row.get("symbol") or "") == symbol else row
            for row in self._positions
        ]
        return {"status": "FILLED"}


def _state_store(tmp_path: Path) -> EngineStateStore:
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "ops.sqlite3"))
    storage.ensure_schema()
    return EngineStateStore(storage=storage, mode="shadow")


def _write_temp_config(tmp_path: Path) -> Path:
    base = load_root_config().model_dump()
    base["base"]["storage"]["sqlite_path"] = str(tmp_path / "run_ops.sqlite3")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
    return path


def test_pause_safe_resume_persist_across_restart(tmp_path: Path) -> None:
    state = _state_store(tmp_path)
    ops = OpsController(state_store=state, exchange=None)

    ops.pause()
    assert not ops.can_open_new_entries()

    restarted = _state_store(tmp_path)
    assert restarted.get().operational.paused is True

    ops2 = OpsController(state_store=restarted, exchange=None)
    ops2.safe_mode()
    restarted2 = _state_store(tmp_path)
    assert restarted2.get().operational.paused is True
    assert restarted2.get().operational.safe_mode is True

    ops3 = OpsController(state_store=restarted2, exchange=None)
    ops3.resume()
    restarted3 = _state_store(tmp_path)
    assert restarted3.get().operational.paused is False
    assert restarted3.get().operational.safe_mode is False


@pytest.mark.asyncio
async def test_flatten_closes_all_and_verifies(tmp_path: Path) -> None:
    state = _state_store(tmp_path)
    fake = _FakeExchange()
    ops = OpsController(state_store=state, exchange=fake)

    result = await ops.flatten(symbol="BTCUSDT")

    assert result.open_regular_orders == 0
    assert result.open_algo_orders == 0
    assert result.position_amt == 0.0
    assert fake.calls[0] == "cancel_regular:BTCUSDT"
    assert fake.calls[1].startswith("cancel_algo:BTCUSDT:clientAlgoId:")
    assert fake.calls[2].startswith("cancel_algo:BTCUSDT:algoId:")
    assert fake.calls[3].startswith("close_position:BTCUSDT:SELL:")


def test_cli_ops_action_pause(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = _write_temp_config(tmp_path)
    rc = main(["--mode", "shadow", "--config", str(cfg), "--ops-action", "pause"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"action": "pause"' in out


def test_http_ops_endpoints(tmp_path: Path) -> None:
    state = _state_store(tmp_path)
    fake = _FakeExchange()
    ops = OpsController(state_store=state, exchange=fake)
    app = create_ops_http_app(ops=ops)
    client = TestClient(app)

    r1 = client.post("/ops/pause")
    assert r1.status_code == 200
    assert r1.json()["paused"] is True

    r2 = client.post("/ops/safe_mode")
    assert r2.status_code == 200
    assert r2.json()["safe_mode"] is True

    r3 = client.post("/ops/resume")
    assert r3.status_code == 200
    assert r3.json()["paused"] is False

    r4 = client.post("/ops/flatten", json={"symbol": "BTCUSDT"})
    assert r4.status_code == 200
    body = r4.json()
    assert body["symbol"] == "BTCUSDT"
    assert body["open_regular_orders"] == 0
    assert body["open_algo_orders"] == 0
    assert body["position_amt"] == 0.0

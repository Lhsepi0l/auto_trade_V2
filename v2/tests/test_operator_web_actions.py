from __future__ import annotations

from fastapi.testclient import TestClient

from v2.tests.test_operator_web_routes import _build_operator_app


def test_operator_action_tick_and_panic_paths(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_build_operator_app(tmp_path))

    tick = client.post("/operator/actions/tick")
    assert tick.status_code == 200
    assert tick.json()["action"] == "tick_now"
    assert "summary" in tick.json()

    panic = client.post("/operator/actions/panic")
    assert panic.status_code == 200
    assert panic.json()["action"] == "panic"


def test_operator_position_actions_are_available(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_build_operator_app(tmp_path))

    close_one = client.post("/operator/actions/positions/close", json={"symbol": "BTCUSDT"})
    assert close_one.status_code == 200
    assert close_one.json()["action"] == "close_position"

    close_all = client.post("/operator/actions/positions/close-all")
    assert close_all.status_code == 200
    assert close_all.json()["action"] == "close_all"

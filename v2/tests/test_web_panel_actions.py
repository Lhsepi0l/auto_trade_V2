from __future__ import annotations

from fastapi.testclient import TestClient

from v2.tests.test_web_panel_routes import _build_operator_app


def test_operator_action_tick_and_panic_paths(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_build_operator_app(tmp_path))

    tick = client.post("/operator/actions/tick")
    assert tick.status_code == 200
    assert tick.json()["action"] == "tick_now"
    assert "summary" in tick.json()

    panic = client.post("/operator/actions/panic")
    assert panic.status_code == 200
    assert panic.json()["action"] == "panic"

from __future__ import annotations

from v2.run import main


def test_v2_shadow_startup_prints_effective_config(capsys) -> None:  # type: ignore[no-untyped-def]
    rc = main(["--mode", "shadow"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[v2] effective config" in out
    assert '"mode": "shadow"' in out
    assert "[v2] started" in out

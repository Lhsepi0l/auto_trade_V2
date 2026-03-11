from __future__ import annotations

from v2.run import (
    LOCAL_BACKTEST_INITIAL_CAPITAL_USDT,
    _locked_local_backtest_initial_capital,
    _write_local_backtest_markdown,
)


def test_local_backtest_initial_capital_is_hard_locked() -> None:
    assert _locked_local_backtest_initial_capital(1.0) == LOCAL_BACKTEST_INITIAL_CAPITAL_USDT
    assert _locked_local_backtest_initial_capital(9999.0) == LOCAL_BACKTEST_INITIAL_CAPITAL_USDT
    assert _locked_local_backtest_initial_capital(None) == 30.0


def test_markdown_includes_english_initial_capital_line(tmp_path) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "generated_at": "2026-03-03T00:00:00+00:00",
        "backtest": {
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "years": 3,
            "initial_capital_usdt": 30.0,
        },
        "summary": {
            "total_initial_capital": 30.0,
            "total_final_equity": 31.0,
            "total_net_profit": 1.0,
            "gross_trade_pnl": 1.2,
            "gross_profit": 2.0,
            "gross_loss": 1.0,
            "total_fees": 0.2,
            "fee_to_gross_profit_pct": 10.0,
            "fee_to_trade_gross_pct": 16.6,
            "total_funding_pnl": 0.0,
            "total_return_pct": 3.3,
            "win_rate_pct": 50.0,
            "profit_factor": 1.2,
            "max_drawdown_pct": 5.0,
            "total_trades": 2,
        },
        "symbols": [],
    }
    target = tmp_path / "local_backtest_20260303_000000.json"
    target.write_text("{}", encoding="utf-8")
    md_path = _write_local_backtest_markdown(report_payload=payload, target_json=target)
    rendered = md_path.read_text(encoding="utf-8")
    assert "Initial Capital (USDT): 30.00" in rendered

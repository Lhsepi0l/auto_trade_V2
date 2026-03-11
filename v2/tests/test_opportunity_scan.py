from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from local_backtest.opportunity_scan import (
    OpportunityEvent,
    ScanConfig,
    _build_scan_gate,
    _load_registry,
    _scan_symbol_rows,
    can_proceed_to_strategy_build,
    main,
    run_opportunity_scan,
)
from v2.run import (
    _FundingRateRow,
    _Kline15m,
    _write_funding_csv,
    _write_klines_csv,
)


def _build_scan_fixture(
    *,
    base_start_ms: int = 1_757_462_400_000,  # 2025-09-10T00:00:00Z
    total_bars: int = 176,
    event_indices: list[int] | None = None,
) -> tuple[list[_Kline15m], list[_Kline15m], list[_FundingRateRow]]:
    if event_indices is None:
        event_indices = [128, 132, 136, 140]

    ohlcv_rows: list[_Kline15m] = []
    premium_rows: list[_Kline15m] = []
    event_index_set = set(event_indices)

    for idx in range(total_bars):
        open_time_ms = base_start_ms + (idx * 900_000)
        close_time_ms = open_time_ms + 899_999
        base_price = 100.0 + (((idx % 6) - 3.0) * 0.02)
        open_price = base_price - 0.03
        high = base_price + 0.24
        low = base_price - 0.24
        close = base_price
        volume = 1_000.0 + (idx * 2.0)
        premium_close = -0.00015 + ((idx % 5) * 0.00001)
        if idx in event_index_set:
            open_price = 99.62
            high = 100.18
            low = 99.02
            close = 99.82
            volume = 2_400.0
            premium_close = -0.00480
        ohlcv_rows.append(
            _Kline15m(
                open_time_ms=open_time_ms,
                close_time_ms=close_time_ms,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
        premium_rows.append(
            _Kline15m(
                open_time_ms=open_time_ms,
                close_time_ms=close_time_ms,
                open=premium_close,
                high=premium_close + 0.00005,
                low=premium_close - 0.00005,
                close=premium_close,
                volume=0.0,
            )
        )

    funding_rows = [
        _FundingRateRow(
            funding_time_ms=base_start_ms + (((idx + 1) * 8 * 60 * 60 * 1000) - 1),
            funding_rate=-0.00007,
        )
        for idx in range(8)
    ]
    return ohlcv_rows, premium_rows, funding_rows


def _write_symbol_cache(
    *,
    cache_root: Path,
    symbol: str,
    ohlcv_rows: list[_Kline15m],
    premium_rows: list[_Kline15m],
    funding_rows: list[_FundingRateRow],
) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    _write_klines_csv(
        path=cache_root / f"klines_{symbol.lower()}_15m_1y.csv",
        symbol=symbol,
        rows=ohlcv_rows,
    )
    _write_klines_csv(
        path=cache_root / f"premium_{symbol.lower()}_15m_1y.csv",
        symbol=symbol,
        rows=premium_rows,
    )
    _write_funding_csv(
        path=cache_root / f"funding_{symbol.lower()}_1y.csv",
        symbol=symbol,
        rows=funding_rows,
    )


def _write_registry(
    *,
    path: Path,
    status: str = "active",
    kill_reason: str = "",
    best_report_path: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "policy": {
                    "active_experiment_limit": 1,
                    "default_scan_family": "crowding_plus_liquidity",
                    "max_salvage_attempts": 1,
                    "implementation_gate": "scan_gate_keep_only",
                },
                "experiments": [
                    {
                        "experiment_id": "rr_2026_scan_001",
                        "hypothesis": "scan test",
                        "data_axes": ["premium_index", "funding_rate", "ohlcv"],
                        "universe": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
                        "window": {
                            "start_utc": "2025-09-10T00:00:00Z",
                            "end_utc": "2025-09-12T00:00:00Z",
                        },
                        "scan_family": "crowding_plus_liquidity",
                        "status": status,
                        "kill_reason": kill_reason,
                        "best_report_path": best_report_path,
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


def test_scan_symbol_rows_emits_candidate_event() -> None:
    ohlcv_rows, premium_rows, funding_rows = _build_scan_fixture()
    cfg = ScanConfig(
        start_ms=1_757_462_400_000,
        end_ms=1_757_635_200_000,
        symbols=["BTCUSDT"],
        market_intervals=["15m", "1h", "4h"],
        data_axes=["premium_index", "funding_rate", "ohlcv"],
        scan_family="crowding_plus_liquidity",
        experiment_id="rr_2026_scan_001",
        registry_path=Path("/tmp/registry.yaml"),
        report_dir=Path("/tmp/reports"),
        cache_root=Path("/tmp/cache"),
    )

    events, block_counter, eligible_bars = _scan_symbol_rows(
        symbol="BTCUSDT",
        ohlcv_rows_15m=ohlcv_rows,
        premium_rows_15m=premium_rows,
        funding_rows=funding_rows,
        cfg=cfg,
    )

    assert events
    assert eligible_bars > 0
    assert block_counter["entry_signal"] >= 1
    first = events[0]
    assert first.symbol == "BTCUSDT"
    assert first.side == "LONG"
    assert first.gross_edge_pct > first.roundtrip_cost_pct
    assert first.edge_after_cost_pct < first.gross_edge_pct


def test_build_scan_gate_surfaces_multiple_failure_reasons() -> None:
    gate = _build_scan_gate(
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
        events=[
            OpportunityEvent(
                symbol="BTCUSDT",
                timestamp_ms=0,
                side="LONG",
                session_hour_utc=8,
                premium_zscore_24h=-2.2,
                funding_sum_24h=-0.00025,
                gross_edge_pct=0.12,
                roundtrip_cost_pct=0.18,
                edge_after_cost_pct=-0.06,
                hold_bars=20,
                reference_level=99.4,
                entry_price=100.0,
            )
        ],
        events_per_symbol={"BTCUSDT": 1, "ETHUSDT": 0, "SOLUSDT": 0, "BNBUSDT": 0},
        edge_after_cost_values=[-0.06],
        hold_values=[20.0],
        fortnight_counts={0: 1},
    )

    assert gate["verdict"] == "KILL"
    assert "candidate_events" in gate["reasons"]
    assert "events_per_symbol_min" in gate["reasons"]
    assert "top_edge_decile_median_after_cost" in gate["reasons"]
    assert "median_hold_bars" in gate["reasons"]


def test_load_registry_rejects_multiple_active_experiments(tmp_path: Path) -> None:
    registry_path = tmp_path / "research" / "registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "policy": {"active_experiment_limit": 1},
                "experiments": [
                    {
                        "experiment_id": "exp_a",
                        "hypothesis": "a",
                        "data_axes": ["ohlcv"],
                        "universe": ["BTCUSDT"],
                        "window": {
                            "start_utc": "2025-01-01T00:00:00Z",
                            "end_utc": "2025-02-01T00:00:00Z",
                        },
                        "status": "active",
                        "kill_reason": "",
                        "best_report_path": "",
                    },
                    {
                        "experiment_id": "exp_b",
                        "hypothesis": "b",
                        "data_axes": ["ohlcv"],
                        "universe": ["ETHUSDT"],
                        "window": {
                            "start_utc": "2025-01-01T00:00:00Z",
                            "end_utc": "2025-02-01T00:00:00Z",
                        },
                        "status": "active",
                        "kill_reason": "",
                        "best_report_path": "",
                    },
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="at most one active experiment"):
        _load_registry(registry_path)


def test_can_proceed_to_strategy_build_blocks_killed_pre_implementation(tmp_path: Path) -> None:
    registry_path = tmp_path / "research" / "registry.yaml"
    _write_registry(
        path=registry_path,
        status="killed_pre_implementation",
        kill_reason="candidate_events",
        best_report_path="local_backtest/reports/scan_rr_2026_scan_001.json",
    )

    allowed, reason = can_proceed_to_strategy_build(
        registry_path,
        experiment_id="rr_2026_scan_001",
    )

    assert allowed is False
    assert reason == "killed_pre_implementation"


def test_run_opportunity_scan_writes_report_and_includes_cost_adjustment(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    cache_root = report_dir / "_cache"
    registry_path = tmp_path / "research" / "registry.yaml"
    _write_registry(path=registry_path)

    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
    for symbol in symbols:
        ohlcv_rows, premium_rows, funding_rows = _build_scan_fixture()
        _write_symbol_cache(
            cache_root=cache_root,
            symbol=symbol,
            ohlcv_rows=ohlcv_rows,
            premium_rows=premium_rows,
            funding_rows=funding_rows,
        )

    payload = run_opportunity_scan(
        ScanConfig(
            start_ms=1_757_462_400_000,
            end_ms=1_757_635_200_000,
            symbols=symbols,
            market_intervals=["15m", "1h", "4h"],
            data_axes=["premium_index", "funding_rate", "ohlcv"],
            scan_family="crowding_plus_liquidity",
            experiment_id="rr_2026_scan_001",
            registry_path=registry_path,
            report_dir=report_dir,
            cache_root=cache_root,
        )
    )

    report_path = report_dir / "scan_rr_2026_scan_001.json"
    markdown_path = report_dir / "scan_rr_2026_scan_001.md"
    assert report_path.exists()
    assert markdown_path.exists()

    rendered = markdown_path.read_text(encoding="utf-8")
    assert "# Opportunity Scan 리포트" in rendered
    assert payload["summary"]["candidate_events"] > 0
    assert payload["summary"]["roundtrip_cost_distribution"]["count"] == payload["summary"][
        "candidate_events"
    ]

    reloaded = json.loads(report_path.read_text(encoding="utf-8"))
    assert reloaded["summary"]["scan_gate"]["verdict"] == "KILL"
    assert reloaded["summary"]["scan_family"] == "crowding_plus_liquidity"

    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    experiment = registry["experiments"][0]
    assert experiment["best_report_path"] == str(report_path)
    assert experiment["status"] == "killed_pre_implementation"


def test_main_updates_registry_to_killed_pre_implementation_on_scan_fail(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    cache_root = report_dir / "_cache"
    registry_path = tmp_path / "research" / "registry.yaml"
    _write_registry(path=registry_path)

    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
    for symbol in symbols:
        ohlcv_rows, premium_rows, funding_rows = _build_scan_fixture()
        _write_symbol_cache(
            cache_root=cache_root,
            symbol=symbol,
            ohlcv_rows=ohlcv_rows,
            premium_rows=premium_rows,
            funding_rows=funding_rows,
        )

    exit_code = main(
        [
            "--experiment-id",
            "rr_2026_scan_001",
            "--symbols",
            ",".join(symbols),
            "--start-utc",
            "2025-09-10T00:00:00Z",
            "--end-utc",
            "2025-09-12T00:00:00Z",
            "--registry-path",
            str(registry_path),
            "--cache-root",
            str(cache_root),
            "--report-dir",
            str(report_dir),
        ]
    )

    assert exit_code == 0
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    experiment = registry["experiments"][0]
    assert experiment["status"] == "killed_pre_implementation"
    assert experiment["kill_reason"]

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from v2.backtest.common import _to_float
from v2.config.loader import EffectiveConfig


def _write_replay_report(
    *,
    cfg: EffectiveConfig,
    replay_source: str,
    rows: list[dict[str, Any]],
    report_dir: str,
    report_path: str | None,
) -> str:
    report_root = Path(report_dir)
    report_root.mkdir(parents=True, exist_ok=True)

    state_counter = Counter(row.get("state") for row in rows)
    regime_counter = Counter()
    block_counter = Counter()
    would_enter = 0
    for row in rows:
        if row.get("would_enter"):
            would_enter += 1
        decision = row.get("decision")
        if isinstance(decision, dict):
            blocks_raw = decision.get("blocks")
            if isinstance(blocks_raw, list):
                for block in blocks_raw:
                    if isinstance(block, str):
                        block_counter[block] += 1
            regime = decision.get("regime")
            if isinstance(regime, str):
                regime_counter[regime] += 1

    report_payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "replay": {
            "source": replay_source,
            "profile": cfg.profile,
            "mode": cfg.mode,
            "symbol": cfg.behavior.exchange.default_symbol,
        },
        "summary": {
            "total_cycles": len(rows),
            "would_enter": would_enter,
            "state_distribution": dict(state_counter),
            "regime_distribution": dict(regime_counter),
            "block_distribution": dict(block_counter),
            "first_tick": rows[0].get("tick") if rows else None,
            "last_tick": rows[-1].get("tick") if rows else None,
        },
        "cycles": rows,
    }

    if report_path is not None:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        symbol = cfg.behavior.exchange.default_symbol.lower()
        target = report_root / f"replay_{symbol}_{stamp}.json"

    target.write_text(json.dumps(report_payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return str(target)


def _cleanup_local_backtest_artifacts(report_root: Path) -> int:
    removed = 0
    if not report_root.exists():
        return removed
    for pattern in ("backtest_*.csv", "backtest_*.sqlite3"):
        for path in report_root.glob(pattern):
            if not path.is_file():
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def _write_local_backtest_markdown(*, report_payload: dict[str, Any], target_json: Path) -> Path:
    backtest = report_payload.get("backtest")
    summary = report_payload.get("summary")
    symbols = report_payload.get("symbols")

    if not isinstance(backtest, dict):
        backtest = {}
    if not isinstance(summary, dict):
        summary = {}
    if not isinstance(symbols, list):
        symbols = []

    def _fmt(value: Any, digits: int = 2) -> str:
        number = _to_float(value)
        if number is None:
            return "-"
        return f"{number:.{digits}f}"

    def _top_entry_blocks(
        source: Any, *, limit: int = 6
    ) -> list[tuple[str, int]]:
        if not isinstance(source, dict):
            return []
        items: list[tuple[str, int]] = []
        for key, value in source.items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            items.append((str(key), count))
        items.sort(key=lambda item: (-item[1], item[0]))
        return items[: max(int(limit), 1)]

    top_entry_blocks = _top_entry_blocks(summary.get("entry_block_distribution"))
    alpha_stats = summary.get("alpha_stats")
    if not isinstance(alpha_stats, dict):
        alpha_stats = {}
    window_slices = summary.get("window_slices_6m")
    if not isinstance(window_slices, list):
        window_slices = []
    research_gate = summary.get("research_gate")
    if not isinstance(research_gate, dict):
        research_gate = {}

    md_path = target_json.with_suffix(".md")
    lines: list[str] = [
        "# 로컬 백테스트 리포트",
        "",
        f"- 생성시각: {report_payload.get('generated_at', '-')}",
        f"- Initial Capital (USDT): {_fmt(backtest.get('initial_capital_usdt', summary.get('total_initial_capital')))}",
        f"- 심볼: {', '.join(str(sym) for sym in backtest.get('symbols', [])) if isinstance(backtest.get('symbols'), list) else '-'}",
        f"- 기간(년): {backtest.get('years', '-')}",
        f"- 기간 구간(UTC): {backtest.get('backtest_start_utc', '-')} ~ {backtest.get('backtest_end_utc', '-')}",
        f"- 구간 모드: {backtest.get('window_mode', '-')}",
        f"- 포지션 사이징 모드: {backtest.get('position_sizing_mode', 'fixed_leverage')}",
        f"- 고정 레버리지: {backtest.get('fixed_leverage', '-')}x",
        f"- 포지션 증거금 사용률: {round(float(backtest.get('fixed_leverage_margin_use_pct', 0.0)) * 100, 2)}%",
        f"- 일일 손실 제한: {round(float(backtest.get('daily_loss_limit_pct', 0.0)) * 100, 2)}%",
        f"- 자본 보호 하한: {round(float(backtest.get('equity_floor_pct', 0.0)) * 100, 2)}%",
        f"- 트레이드 최대 손실 캡: {round(float(backtest.get('max_trade_margin_loss_fraction', 0.0)) * 100, 2)}% of margin",
        f"- 최소 시그널 점수: {round(float(backtest.get('min_signal_score', 0.0)), 3)}",
        f"- 역신호 청산 최소 수익률: {round(float(backtest.get('reverse_exit_min_profit_pct', 0.0)) * 100, 3)}%",
        f"- 역신호 청산 최소 점수: {round(float(backtest.get('reverse_exit_min_signal_score', 0.0)), 3)}",
        f"- 드로우다운 감속 시작: {round(float(backtest.get('drawdown_scale_start_pct', 0.0)) * 100, 2)}%",
        f"- 드로우다운 감속 최대: {round(float(backtest.get('drawdown_scale_end_pct', 0.0)) * 100, 2)}%",
        f"- 감속 최소 배수: {round(float(backtest.get('drawdown_margin_scale_min', 0.0)) * 100, 2)}%",
        f"- 손절 연속 트리거: {int(backtest.get('stoploss_streak_trigger', 0))}회",
        f"- 손절 쿨다운 봉수: {int(backtest.get('stoploss_cooldown_bars', 0))}봉",
        f"- 손실 후 쿨다운 봉수: {int(backtest.get('loss_cooldown_bars', 0))}봉",
        "",
        "## 전체 요약",
        "",
        "| 항목 | 값 |",
        "| --- | ---: |",
        f"| 초기 자본 (USDT) | {_fmt(summary.get('total_initial_capital'))} |",
        f"| 최종 자산 (USDT) | {_fmt(summary.get('total_final_equity'))} |",
        f"| 순손익 (USDT) | {_fmt(summary.get('total_net_profit'))} |",
        f"| 총 거래 손익 (USDT) | {_fmt(summary.get('gross_trade_pnl'))} |",
        f"| 총 수익액 (USDT) | {_fmt(summary.get('gross_profit'))} |",
        f"| 총 손실액 (USDT) | {_fmt(summary.get('gross_loss'))} |",
        f"| 총 수수료 (USDT) | {_fmt(summary.get('total_fees'))} |",
        f"| 수수료/총수익액 (%) | {_fmt(summary.get('fee_to_gross_profit_pct'))} |",
        f"| 수수료/총거래손익 (%) | {_fmt(summary.get('fee_to_trade_gross_pct'))} |",
        f"| 총 펀딩손익 (USDT) | {_fmt(summary.get('total_funding_pnl'))} |",
        f"| 총 수익률 (%) | {_fmt(summary.get('total_return_pct'))} |",
        f"| 승률 (%) | {_fmt(summary.get('win_rate_pct'))} |",
        f"| PF | {_fmt(summary.get('profit_factor'), digits=3)} |",
        f"| 최대 낙폭 (%) | {_fmt(summary.get('max_drawdown_pct'))} |",
        f"| 총 거래 수 | {int(summary.get('total_trades', 0)) if str(summary.get('total_trades', '')).isdigit() else summary.get('total_trades', '-')} |",
    ]

    if research_gate:
        checks = research_gate.get("checks")
        if not isinstance(checks, list):
            checks = []
        lines.extend(
            [
                "",
                "## 연구 게이트",
                "",
                f"- 트랙: {research_gate.get('track', '-')}",
                f"- 최종 판정: {research_gate.get('verdict', '-')}",
                "",
                "| 체크 | 판정 | 순손익 | PF | 최대낙폭(%) | 거래수 | 수수료/총거래손익(%) | 사유 |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        if checks:
            for item in checks:
                if not isinstance(item, dict):
                    continue
                metrics = item.get("metrics")
                if not isinstance(metrics, dict):
                    metrics = {}
                reasons = item.get("reasons")
                if isinstance(reasons, list) and reasons:
                    reason_text = ", ".join(str(reason) for reason in reasons)
                else:
                    reason_text = "-"
                lines.append(
                    "| "
                    + f"{item.get('name', '-')}"
                    + " | "
                    + f"{item.get('verdict', '-')}"
                    + " | "
                    + f"{_fmt(metrics.get('net_profit'))}"
                    + " | "
                    + f"{_fmt(metrics.get('profit_factor'), digits=3)}"
                    + " | "
                    + f"{_fmt(metrics.get('max_drawdown_pct'))}"
                    + " | "
                    + f"{int(metrics.get('trades', 0))}"
                    + " | "
                    + f"{_fmt(metrics.get('fee_to_trade_gross_pct'))}"
                    + " | "
                    + f"{reason_text}"
                    + " |"
                )
        else:
            lines.append("| 없음 | - | - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## 진입 차단 상위 사유",
            "",
            "| 사유 | 횟수 |",
            "| --- | ---: |",
        ]
    )

    if top_entry_blocks:
        for reason, count in top_entry_blocks:
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| 없음 | 0 |")

    lines.extend(
        [
            "",
            "## 알파별 요약",
            "",
            "| 알파 | 순손익(USDT) | PF | 거래수 | 최대낙폭(%) | 상위 차단사유 |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )

    if alpha_stats:
        for alpha_id, payload in sorted(alpha_stats.items()):
            if not isinstance(payload, dict):
                continue
            block_top = payload.get("block_top")
            block_summary = "-"
            if isinstance(block_top, list) and block_top:
                rendered: list[str] = []
                for item in block_top[:3]:
                    if not isinstance(item, dict):
                        continue
                    rendered.append(
                        f"{item.get('reason', '-')}: {int(item.get('count', 0))}"
                    )
                if rendered:
                    block_summary = ", ".join(rendered)
            lines.append(
                "| "
                + f"{alpha_id}"
                + " | "
                + f"{_fmt(payload.get('net_profit'))}"
                + " | "
                + f"{_fmt(payload.get('profit_factor'), digits=3)}"
                + " | "
                + f"{int(payload.get('trades', 0))}"
                + " | "
                + f"{_fmt(payload.get('max_drawdown_pct'))}"
                + " | "
                + f"{block_summary}"
                + " |"
            )
    else:
        lines.append("| 없음 | 0.00 | - | 0 | 0.00 | - |")

    portfolio_slots = summary.get("portfolio_open_slots_usage")
    bucket_blocks = summary.get("bucket_block_distribution")
    capital_utilization = summary.get("capital_utilization")
    simultaneous_hist = summary.get("simultaneous_position_histogram")
    if any(
        isinstance(item, dict) and item
        for item in (portfolio_slots, bucket_blocks, capital_utilization, simultaneous_hist)
    ):
        lines.extend(
            [
                "",
                "## 포트폴리오 요약",
                "",
                f"- 슬롯 사용 히스토그램: {portfolio_slots if isinstance(portfolio_slots, dict) else '-'}",
                f"- 버킷 차단 분포: {bucket_blocks if isinstance(bucket_blocks, dict) else '-'}",
                f"- 동시 포지션 히스토그램: {simultaneous_hist if isinstance(simultaneous_hist, dict) else '-'}",
                f"- 자본 활용도: {capital_utilization if isinstance(capital_utilization, dict) else '-'}",
            ]
        )

    lines.extend(
        [
            "",
            "## 6개월 구간 요약",
            "",
            "| 구간 | 순손익(USDT) | PF | 거래수 | 최대낙폭(%) |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )

    if window_slices:
        for item in window_slices:
            if not isinstance(item, dict):
                continue
            lines.append(
                "| "
                + f"{item.get('label', '-')}"
                + " | "
                + f"{_fmt(item.get('net_profit'))}"
                + " | "
                + f"{_fmt(item.get('profit_factor'), digits=3)}"
                + " | "
                + f"{int(item.get('trades', 0))}"
                + " | "
                + f"{_fmt(item.get('max_drawdown_pct'))}"
                + " |"
            )
    else:
        lines.append("| 없음 | 0.00 | - | 0 | 0.00 |")

    lines.extend(
        [
            "",
            "## 심볼별 요약",
            "",
            "| 심볼 | 순손익(USDT) | 수익률(%) | 승률(%) | 거래수 | 최대낙폭(%) |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )

    for item in symbols:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| "
            + f"{item.get('symbol', '-')}"
            + " | "
            + f"{_fmt(item.get('net_profit'))}"
            + " | "
            + f"{_fmt(item.get('total_return_pct'))}"
            + " | "
            + f"{_fmt(item.get('win_rate_pct'))}"
            + " | "
            + f"{int(item.get('total_trades', 0)) if str(item.get('total_trades', '')).isdigit() else item.get('total_trades', '-')}"
            + " | "
            + f"{_fmt(item.get('max_drawdown_pct'))}"
            + " |"
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path

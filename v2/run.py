from __future__ import annotations

import argparse
import asyncio
import csv
import json
import signal
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from v2.clean_room import build_default_kernel
from v2.config.loader import EffectiveConfig, load_effective_config, render_effective_config
from v2.control import build_runtime_controller, create_control_http_app
from v2.core import Event, EventBus, Scheduler
from v2.engine import EngineStateStore, OrderManager
from v2.exchange import BackoffPolicy, BinanceAdapter, BinanceRESTClient
from v2.notify import Notifier
from v2.ops import OpsController, create_ops_http_app
from v2.risk import KillSwitch, RiskManager
from v2.storage import RuntimeStorage
from v2.tpsl import BracketConfig, BracketPlanner


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V2 scaffold runner")
    parser.add_argument(
        "--profile", default="normal", choices=["conservative", "normal", "aggressive"]
    )
    parser.add_argument("--mode", default="shadow", choices=["shadow", "live"])
    parser.add_argument("--env", default="testnet", choices=["testnet", "prod"])
    parser.add_argument("--env-file", default=".env", help="path to dotenv file for secrets")
    parser.add_argument("--config", default=None, help="path to v2 config.yaml")
    parser.add_argument(
        "--ops-action",
        default="none",
        choices=["none", "pause", "resume", "safe_mode", "flatten"],
    )
    parser.add_argument("--ops-symbol", default=None, help="symbol for ops actions like flatten")
    parser.add_argument(
        "--ops-http", action="store_true", help="run optional HTTP ops control server"
    )
    parser.add_argument("--ops-http-host", default="127.0.0.1")
    parser.add_argument("--ops-http-port", type=int, default=8102)
    parser.add_argument(
        "--control-http",
        action="store_true",
        help="run v2 full control HTTP API (Discord compatible)",
    )
    parser.add_argument("--control-http-host", default="127.0.0.1")
    parser.add_argument("--control-http-port", type=int, default=8101)
    parser.add_argument("--replay", default=None, help="path to replay source")
    parser.add_argument("--report-dir", default="v2/reports", help="directory for replay reports")
    parser.add_argument("--report-path", default=None, help="write report to exact path")
    parser.add_argument("--loop", action="store_true", help="run continuous tick loop")
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="max cycle count when --loop is enabled (0 = unlimited)",
    )
    parser.add_argument(
        "--deploy-prep", action="store_true", help="run one-command deployment preparation"
    )
    parser.add_argument(
        "--keep-reports", type=int, default=None, help="retention count for deploy-prep reports"
    )
    return parser


def _build_runtime(
    cfg: EffectiveConfig,
) -> tuple[RuntimeStorage, EngineStateStore, OpsController, BinanceAdapter, Any | None]:
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    adapter = BinanceAdapter.from_effective_config(cfg)
    rest = adapter.create_rest_client(cfg=cfg)
    ops = OpsController(state_store=state_store, exchange=rest)
    return storage, state_store, ops, adapter, rest


@dataclass(frozen=True)
class _ReplayFrame:
    symbol: str
    market: dict[str, Any]
    meta: dict[str, Any]


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_json_loads(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _extract_meta(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "event_time",
        "timestamp",
        "time",
        "datetime",
        "open_time",
        "close_time",
    ):
        if key in payload:
            out[key] = payload[key]
    return out


def _extract_snapshot_time(meta: dict[str, Any]) -> Any:
    for key in ("event_time", "timestamp", "time", "datetime", "open_time", "close_time"):
        if key in meta:
            return meta[key]
    return None


def _normalize_snapshot(payload: dict[str, Any], *, default_symbol: str) -> _ReplayFrame | None:
    symbol = str(payload.get("symbol") or payload.get("default_symbol") or default_symbol).upper()

    raw_market = payload.get("market")
    if not isinstance(raw_market, dict):
        raw_market = {key: payload.get(key) for key in ("4h", "1h", "15m")}

    market: dict[str, Any] = {}
    for interval in ("4h", "1h", "15m"):
        rows = raw_market.get(interval)
        if rows is None:
            continue
        if isinstance(rows, str):
            rows = _safe_json_loads(rows)
        if isinstance(rows, list):
            market[interval] = rows

    if len(market) == 0:
        return None
    return _ReplayFrame(symbol=symbol, market=market, meta=_extract_meta(payload))


def _normalize_replay_rows(items: list[Any], *, default_symbol: str) -> list[_ReplayFrame]:
    normalized = [
        _normalize_snapshot(row, default_symbol=default_symbol)
        for row in items
        if isinstance(row, dict)
    ]
    return [row for row in normalized if row is not None]


def _load_replay_rows_json(
    *, path: Path, default_symbol: str, is_jsonl: bool = False
) -> list[_ReplayFrame]:
    if is_jsonl:
        jsonl_rows: list[Any] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            raw_line = line.strip()
            if not raw_line:
                continue
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                jsonl_rows.append(parsed)
        return _normalize_replay_rows(jsonl_rows, default_symbol=default_symbol)

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        rows_payload: Any = raw.get("rows")
        if isinstance(rows_payload, list):
            return _normalize_replay_rows(rows_payload, default_symbol=default_symbol)
        if isinstance(rows_payload, str):
            nested = _safe_json_loads(rows_payload)
            if isinstance(nested, list):
                return _normalize_replay_rows(nested, default_symbol=default_symbol)
        normalized = _normalize_snapshot(raw, default_symbol=default_symbol)
        return [normalized] if normalized is not None else []
    if isinstance(raw, list):
        return _normalize_replay_rows(raw, default_symbol=default_symbol)
    return []


def _load_replay_rows_csv(*, path: Path, default_symbol: str) -> list[_ReplayFrame]:
    frames: list[_ReplayFrame] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return frames

    reader = csv.DictReader(lines)
    rows = [row for row in reader if isinstance(row, dict)]
    if len(rows) == 0:
        return frames

    first = rows[0]
    has_market_fields = any(k in first for k in ("market_4h", "market_1h", "market_15m"))
    if has_market_fields:
        for raw in rows:
            if raw is None:
                continue
            payload = dict(raw)
            for key in ("market_4h", "market_1h", "market_15m"):
                if key in payload:
                    raw_market = _safe_json_loads(payload[key])
                    if isinstance(raw_market, list):
                        payload[key.replace("market_", "")] = raw_market
            normalized = _normalize_snapshot(payload, default_symbol=default_symbol)
            if normalized is not None:
                frames.append(normalized)
        return frames

    market_frames: dict[str, list[dict[str, float]]] = {"4h": [], "1h": [], "15m": []}
    default_symbol_upper = default_symbol.upper()
    for raw in rows:
        if raw is None:
            continue
        interval = str(raw.get("interval") or "").strip()
        if interval not in market_frames:
            continue

        open_v = _to_float(raw.get("open"))
        high_v = _to_float(raw.get("high"))
        low_v = _to_float(raw.get("low"))
        close_v = _to_float(raw.get("close"))
        if open_v is None or high_v is None or low_v is None or close_v is None:
            continue

        candle = {
            "open": open_v,
            "high": high_v,
            "low": low_v,
            "close": close_v,
        }
        market_frames[interval].append(candle)

        if all(market_frames[k] for k in ("4h", "1h", "15m")):
            payload: dict[str, Any] = {
                "symbol": str(raw.get("symbol") or default_symbol_upper),
                "market": {
                    "4h": list(market_frames["4h"]),
                    "1h": list(market_frames["1h"]),
                    "15m": list(market_frames["15m"]),
                },
            }
            for key in ("timestamp", "time", "datetime", "open_time", "close_time", "event_time"):
                if key in raw:
                    payload[key] = raw[key]
            normalized = _normalize_snapshot(payload, default_symbol=default_symbol_upper)
            if normalized is not None:
                frames.append(normalized)
    return frames


def _load_replay_frames(path: str | None, default_symbol: str) -> list[_ReplayFrame]:
    if path is None:
        return []
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"replay path not found: {source}")
    suffix = source.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        return _load_replay_rows_json(path=source, default_symbol=default_symbol, is_jsonl=True)
    if suffix == ".json":
        return _load_replay_rows_json(path=source, default_symbol=default_symbol)
    if suffix == ".csv":
        return _load_replay_rows_csv(path=source, default_symbol=default_symbol)

    try:
        rows = _load_replay_rows_json(path=source, default_symbol=default_symbol)
        if rows:
            return rows
    except json.JSONDecodeError:
        pass
    return _load_replay_rows_csv(path=source, default_symbol=default_symbol)


class _ReplaySnapshotProvider:
    def __init__(self, frames: list[_ReplayFrame]) -> None:
        self._frames = frames
        self._index = -1

    def __call__(self) -> dict[str, Any]:
        self._index += 1
        if self._index >= len(self._frames):
            return {}
        frame = self._frames[self._index]
        payload: dict[str, Any] = {
            "symbol": frame.symbol,
            "market": frame.market,
            "meta": frame.meta,
            "tick": self._index,
        }
        return payload


@dataclass
class _ReplayDecision:
    payload: dict[str, Any] = field(default_factory=dict)

    def __call__(self, payload: dict[str, Any]) -> None:
        if isinstance(payload, dict):
            self.payload = payload

    def take(self) -> dict[str, Any]:
        out = dict(self.payload)
        self.payload = {}
        return out


def _build_replay_cycle_record(
    *,
    tick: int,
    cycle: Any,
    decision: dict[str, Any],
    symbol: str,
    bracket_planner: BracketPlanner,
) -> dict[str, Any]:
    candidate = cycle.candidate
    risk = cycle.risk
    size = cycle.size
    execution = cycle.execution

    if candidate is None:
        would_enter = False
    else:
        would_enter = cycle.state in {"dry_run", "executed"}

    decision_payload = decision if decision else {}
    strategy_decision = decision_payload.get("decision")
    if not isinstance(strategy_decision, dict):
        strategy_decision = {}

    entry_price = _to_float(strategy_decision.get("entry_price"))
    sl_tp: dict[str, float] | None = None
    if entry_price and entry_price > 0 and candidate is not None:
        try:
            levels = bracket_planner.levels(
                entry_price=entry_price,
                side="LONG" if candidate.side == "BUY" else "SHORT",
            )
            sl_tp = {
                "take_profit": float(levels["take_profit"]),
                "stop_loss": float(levels["stop_loss"]),
            }
        except (TypeError, ValueError):
            sl_tp = None

    cycle_record: dict[str, Any] = {
        "tick": tick,
        "symbol": symbol,
        "state": cycle.state,
        "reason": cycle.reason,
        "would_enter": would_enter,
    }
    if candidate is not None:
        cycle_record["candidate"] = {
            "symbol": candidate.symbol,
            "side": candidate.side,
            "score": candidate.score,
            "entry_price": candidate.entry_price,
            "reason": candidate.reason,
        }
    else:
        cycle_record["candidate"] = None

    if risk is not None:
        cycle_record["risk"] = {
            "allow": bool(risk.allow),
            "reason": risk.reason,
            "max_notional": risk.max_notional,
        }

    if size is not None:
        cycle_record["size"] = {
            "qty": size.qty,
            "notional": size.notional,
            "leverage": size.leverage,
            "reason": size.reason,
        }

    if execution is not None:
        cycle_record["execution"] = {
            "ok": bool(execution.ok),
            "order_id": execution.order_id,
            "reason": execution.reason,
        }

    if strategy_decision:
        cycle_record["decision"] = {
            "intent": strategy_decision.get("intent"),
            "side": strategy_decision.get("side"),
            "score": strategy_decision.get("score"),
            "regime": strategy_decision.get("regime"),
            "allowed_side": strategy_decision.get("allowed_side"),
            "reason": strategy_decision.get("reason"),
            "blocks": strategy_decision.get("blocks"),
            "filters": strategy_decision.get("filters"),
            "signals": strategy_decision.get("signals"),
            "indicators": strategy_decision.get("indicators"),
            "entry_mode": strategy_decision.get("entry_mode"),
            "sideways": strategy_decision.get("sideways"),
            "stop_hint": strategy_decision.get("stop_hint"),
            "management_hint": strategy_decision.get("management_hint"),
            "entry_price": strategy_decision.get("entry_price"),
            "sl_tp": sl_tp,
        }

    return cycle_record


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
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        symbol = cfg.behavior.exchange.default_symbol.lower()
        target = report_root / f"replay_{symbol}_{stamp}.json"

    target.write_text(json.dumps(report_payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return str(target)


def _run_replay(
    cfg: EffectiveConfig,
    *,
    replay_path: str,
    report_dir: str,
    report_path: str | None = None,
) -> int:
    _storage, state_store, _ops, _adapter, _rest_client = _build_runtime(cfg)
    state_store.set(mode=cfg.mode, status="RUNNING")
    frames = _load_replay_frames(
        path=replay_path, default_symbol=cfg.behavior.exchange.default_symbol
    )
    if not frames:
        print(
            json.dumps(
                {"replay": {"source": replay_path, "cycles": 0}, "error": "no replay data"},
                ensure_ascii=True,
            )
        )
        return 1

    if cfg.behavior.ops.pause_on_start:
        _ops.pause()

    collector = _ReplayDecision()
    snapshot_provider = _ReplaySnapshotProvider(frames)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        tick=0,
        dry_run=True,
        rest_client=_rest_client,
        snapshot_provider=snapshot_provider,
        overheat_fetcher=None,
        journal_logger=collector,
    )

    bracket_planner = BracketPlanner(
        cfg=BracketConfig(
            take_profit_pct=cfg.behavior.tpsl.take_profit_pct,
            stop_loss_pct=cfg.behavior.tpsl.stop_loss_pct,
        )
    )

    rows: list[dict[str, Any]] = []
    for tick in range(len(frames)):
        cycle = kernel.run_once()
        decision = collector.take()
        symbol = cfg.behavior.exchange.default_symbol
        if tick < len(frames):
            symbol = frames[tick].symbol or symbol
        row = _build_replay_cycle_record(
            tick=tick,
            cycle=cycle,
            decision=decision,
            symbol=symbol,
            bracket_planner=bracket_planner,
        )

        if isinstance(frames[tick].meta, dict):
            row.update({k: frames[tick].meta[k] for k in frames[tick].meta if k not in row})
        snapshot_time = _extract_snapshot_time(frames[tick].meta)
        if snapshot_time is not None:
            row["snapshot_time"] = snapshot_time
        rows.append(row)

    report_file = _write_replay_report(
        cfg=cfg,
        replay_source=replay_path,
        rows=rows,
        report_dir=report_dir,
        report_path=report_path,
    )
    print(json.dumps({"replay": {"status": "completed", "report": report_file}}, ensure_ascii=True))
    return 0


def _boot(cfg: EffectiveConfig, *, loop_enabled: bool = False, max_cycles: int = 0) -> None:
    event_bus = EventBus()
    storage, state_store, ops, adapter, rest_client = _build_runtime(cfg)
    state_store.set(mode=cfg.mode, status="RUNNING")

    if cfg.behavior.ops.pause_on_start:
        ops.pause()

    risk = RiskManager(config=cfg.behavior.risk)
    _ = risk.validate_leverage(cfg.behavior.risk.max_leverage)
    kill_switch = KillSwitch()
    if kill_switch.tripped and cfg.behavior.ops.flatten_on_kill:
        _ = asyncio.run(ops.flatten(symbol=cfg.behavior.exchange.default_symbol))

    _ = adapter.ping()

    brackets = BracketPlanner(
        cfg=BracketConfig(
            take_profit_pct=cfg.behavior.tpsl.take_profit_pct,
            stop_loss_pct=cfg.behavior.tpsl.stop_loss_pct,
        )
    )
    _ = brackets.levels(entry_price=100.0)

    notifier = Notifier(
        enabled=cfg.behavior.notify.enabled,
        provider=cfg.behavior.notify.provider,
        webhook_url=cfg.secrets.notify_webhook_url,
    )
    notifier.send("v2 boot completed")

    journal_logger = None

    if cfg.mode == "shadow":

        def _journal_logger(payload: dict[str, Any]) -> None:
            print(
                json.dumps(
                    {
                        "strategy": "strategy_pack_v1",
                        "regime": payload.get("regime"),
                        "allowed_side": payload.get("allowed_side"),
                        "signals": payload.get("signals"),
                        "blocks": payload.get("blocks"),
                        "decision": payload.get("decision", {}).get("intent")
                        if isinstance(payload.get("decision"), dict)
                        else None,
                        "reasons": payload.get("decision", {}).get("reason")
                        if isinstance(payload.get("decision"), dict)
                        else None,
                    },
                    ensure_ascii=True,
                )
            )

        journal_logger = _journal_logger

    order_manager = OrderManager(event_bus=event_bus)

    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        tick=0,
        dry_run=cfg.mode == "shadow",
        rest_client=rest_client,
        journal_logger=journal_logger,
    )
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)

    def _on_scheduler_tick(event: Event) -> None:
        _ = storage.append_journal(
            event_type=event.topic,
            payload_json=render_effective_config(cfg),
        )

    event_bus.subscribe(
        "scheduler.tick",
        _on_scheduler_tick,
    )
    stop_requested = False

    def _request_stop(_sig: int, _frame: object | None) -> None:
        nonlocal stop_requested
        stop_requested = True

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _request_stop)
        except (OSError, RuntimeError, ValueError):
            continue

    cycles = 0
    while True:
        cycle = kernel.run_once()
        event_bus.publish(
            Event(
                topic="kernel.cycle",
                payload={
                    "state": cycle.state,
                    "reason": cycle.reason,
                    "symbol": cycle.candidate.symbol if cycle.candidate is not None else None,
                },
            )
        )
        if cycle.state in {"executed", "execution_failed"}:
            notifier.send(f"v2 cycle {cycle.state}: {cycle.reason}")

        scheduler.run_once()

        if ops.can_open_new_entries():
            order_manager.submit({"symbol": cfg.behavior.exchange.default_symbol, "mode": cfg.mode})
        else:
            event_bus.publish(
                Event(
                    topic="order.entry_blocked",
                    payload={"symbol": cfg.behavior.exchange.default_symbol, "paused": True},
                )
            )

        cycles += 1
        if not loop_enabled:
            break
        if max_cycles > 0 and cycles >= max_cycles:
            break
        if stop_requested:
            break
        asyncio.run(asyncio.sleep(max(0.2, float(cfg.behavior.scheduler.tick_seconds))))

    event_bus.publish(Event(topic="v2.started", payload={"profile": cfg.profile, "env": cfg.env}))


def _run_ops_action(cfg: EffectiveConfig, *, action: str, symbol: str | None) -> int:
    _storage, _state_store, ops, _adapter, _rest = _build_runtime(cfg)
    symbol_v = symbol or cfg.behavior.exchange.default_symbol

    if action == "pause":
        ops.pause()
        print(json.dumps({"action": "pause", "paused": True}, ensure_ascii=True))
        return 0
    if action == "resume":
        ops.resume()
        print(
            json.dumps({"action": "resume", "paused": False, "safe_mode": False}, ensure_ascii=True)
        )
        return 0
    if action == "safe_mode":
        ops.safe_mode()
        print(
            json.dumps(
                {"action": "safe_mode", "paused": True, "safe_mode": True}, ensure_ascii=True
            )
        )
        return 0
    if action == "flatten":
        result = asyncio.run(ops.flatten(symbol=symbol_v))
        print(
            json.dumps(
                {
                    "action": "flatten",
                    "symbol": result.symbol,
                    "paused": result.paused,
                    "safe_mode": result.safe_mode,
                    "open_regular_orders": result.open_regular_orders,
                    "open_algo_orders": result.open_algo_orders,
                    "position_amt": result.position_amt,
                },
                ensure_ascii=True,
            )
        )
        return 0
    return 0


def _serve_ops_http(cfg: EffectiveConfig, *, host: str, port: int) -> int:
    import uvicorn

    _storage, _state_store, ops, _adapter, _rest = _build_runtime(cfg)
    app = create_ops_http_app(ops=ops)
    uvicorn.run(app, host=host, port=port)
    return 0


def _serve_control_http(cfg: EffectiveConfig, *, host: str, port: int) -> int:
    import uvicorn

    event_bus = EventBus()
    storage, state_store, ops, adapter, rest_client = _build_runtime(cfg)
    state_store.set(mode=cfg.mode, status="STOPPED")

    notifier = Notifier(
        enabled=cfg.behavior.notify.enabled,
        provider=cfg.behavior.notify.provider,
        webhook_url=cfg.secrets.notify_webhook_url,
    )

    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        tick=0,
        dry_run=cfg.mode == "shadow",
        rest_client=rest_client,
        journal_logger=None,
    )
    _ = storage
    _ = adapter
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    balance_rest_client = _build_control_balance_rest_client(
        cfg=cfg, runtime_rest_client=rest_client
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=balance_rest_client,
    )
    app = create_control_http_app(controller=controller)
    uvicorn.run(app, host=host, port=port)
    return 0


def _build_control_balance_rest_client(
    *, cfg: EffectiveConfig, runtime_rest_client: Any | None
) -> Any | None:
    if runtime_rest_client is not None:
        return runtime_rest_client
    if not cfg.secrets.binance_api_key or not cfg.secrets.binance_api_secret:
        return None
    return BinanceRESTClient(
        env=cfg.env,
        api_key=cfg.secrets.binance_api_key,
        api_secret=cfg.secrets.binance_api_secret,
        recv_window_ms=cfg.behavior.exchange.recv_window_ms,
        time_sync_enabled=True,
        rate_limit_per_sec=cfg.behavior.exchange.request_rate_limit_per_sec,
        backoff_policy=BackoffPolicy(
            base_seconds=cfg.behavior.exchange.backoff_base_seconds,
            cap_seconds=cfg.behavior.exchange.backoff_cap_seconds,
        ),
    )


def _run_deploy_prep(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "v2" / "scripts" / "deploy_prep.sh"
    cmd = [
        "bash",
        str(script),
        "--profile",
        str(args.profile),
        "--mode",
        str(args.mode),
        "--env",
        str(args.env),
        "--config",
        str(args.config if args.config else "config/config.yaml"),
        "--report-dir",
        str(args.report_dir),
    ]
    if args.keep_reports is not None:
        cmd.extend(["--keep-reports", str(max(int(args.keep_reports), 0))])
    done = subprocess.run(cmd, cwd=str(repo_root), check=False)
    return int(done.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.deploy_prep:
        return _run_deploy_prep(args)

    effective = load_effective_config(
        profile=args.profile,
        mode=args.mode,
        env=args.env,
        config_path=args.config,
        env_file_path=args.env_file,
    )

    print("[v2] effective config")
    print(render_effective_config(effective))

    if args.ops_http:
        return _serve_ops_http(effective, host=args.ops_http_host, port=args.ops_http_port)

    if args.control_http:
        return _serve_control_http(
            effective, host=args.control_http_host, port=args.control_http_port
        )

    if args.replay is not None:
        return _run_replay(
            effective,
            replay_path=args.replay,
            report_dir=args.report_dir,
            report_path=args.report_path,
        )

    if args.ops_action != "none":
        return _run_ops_action(effective, action=args.ops_action, symbol=args.ops_symbol)

    _boot(effective, loop_enabled=args.loop, max_cycles=max(int(args.max_cycles), 0))
    print("[v2] started")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

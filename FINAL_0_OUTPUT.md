# FINAL-0 Output

This file contains full code for files changed/added in FINAL-0 (Repo Audit & Safety Switch).

## apps/trader_engine/config.py

```python
from __future__ import annotations

from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TraderSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = Field(default="dev", description="Runtime environment name")

    db_path: str = Field(default="./data/auto_trader.sqlite3", description="SQLite DB file path")

    log_level: str = Field(default="INFO", description="Root log level")
    log_dir: str = Field(default="./logs", description="Directory for log files")
    log_json: bool = Field(default=False, description="Emit JSON logs")

    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000)

    # Exchange settings (Binance USDT-M Futures; 조회 전용)
    binance_api_key: str = Field(default="")
    binance_api_secret: str = Field(default="")
    binance_base_url: str = Field(default="https://fapi.binance.com")

    # NOTE: Symbol universe is stored in DB config (risk_config table). This env is kept
    # only for backward compatibility and initial bootstrapping.
    allowed_symbols: str = Field(default="BTCUSDT,ETHUSDT,XAUTUSDT")

    request_timeout_sec: float = Field(default=8.0)
    retry_count: int = Field(default=3)
    retry_backoff: float = Field(default=0.25)

    binance_recv_window_ms: int = Field(default=5000)

    # Safety switches (real account protection)
    trading_dry_run: bool = Field(
        default=True,
        description="If true, block any NEW entry/scale/rebalance orders. Close/PANIC are allowed unless DRY_RUN_STRICT.",
    )
    dry_run_strict: bool = Field(
        default=False,
        description="If true AND TRADING_DRY_RUN=true, also block close/PANIC orders (maximum safety).",
    )

    # Optional notifications (if empty => disabled)
    discord_webhook_url: str = Field(default="", description="Discord webhook for critical alerts (optional)")

    # Execution (trade) controls (MVP)
    exec_limit_timeout_sec: float = Field(default=3.0)
    exec_limit_retries: int = Field(default=2)
    exec_split_parts: int = Field(default=3)

    # Policy guards (risk engine)
    spread_guard_max_pct: float = Field(default=0.5, description="Max spread percent allowed before guard triggers")
    spread_guard_action: str = Field(default="block_market", description="block_market|block_all")
    risk_stop_on_daily_loss: bool = Field(default=False, description="If true, STOP engine when daily loss limit hit")

    # Scheduler (STEP6)
    scheduler_enabled: bool = Field(default=False, description="If true, run scheduler loop inside API process")
    scheduler_tick_sec: int = Field(default=1800, description="Scheduler decision tick interval in seconds (default 30m)")
    score_threshold: float = Field(default=0.35, description="Entry threshold for long/short score (0..1)")
    reverse_threshold: float = Field(default=0.55, description="Exit threshold for strong reverse signal (0..1)")
    vol_shock_threshold_pct: float = Field(default=2.0, description="ATR%% threshold to tag VOL_SHOCK")

    # AI signal (STEP7) - signal only, no execution authority
    ai_mode: str = Field(default="stub", description="stub|openai|local")
    ai_conf_threshold: float = Field(default=0.65, description="AI confidence threshold; below => HOLD")
    manual_risk_tag: str = Field(default="", description="Optional manual risk tag override (e.g. NEWS_RISK)")

    # Behavior defaults
    engine_poll_interval_sec: int = Field(default=2)

    @property
    def allowed_symbols_list(self) -> List[str]:
        return [s.strip().upper() for s in self.allowed_symbols.split(",") if s.strip()]


def load_settings() -> TraderSettings:
    return TraderSettings()

```

## apps/trader_engine/domain/enums.py

```python
from __future__ import annotations

from enum import Enum


class EngineState(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    COOLDOWN = "COOLDOWN"
    PANIC = "PANIC"


class RiskConfigKey(str, Enum):
    per_trade_risk_pct = "per_trade_risk_pct"
    max_exposure_pct = "max_exposure_pct"
    max_notional_pct = "max_notional_pct"
    max_leverage = "max_leverage"
    # Loss limits are stored as ratios (e.g. -0.02 for -2%)
    daily_loss_limit_pct = "daily_loss_limit_pct"
    dd_limit_pct = "dd_limit_pct"
    lose_streak_n = "lose_streak_n"
    cooldown_hours = "cooldown_hours"
    min_hold_minutes = "min_hold_minutes"
    score_conf_threshold = "score_conf_threshold"
    score_gap_threshold = "score_gap_threshold"
    exec_limit_timeout_sec = "exec_limit_timeout_sec"
    exec_limit_retries = "exec_limit_retries"
    notify_interval_sec = "notify_interval_sec"
    spread_max_pct = "spread_max_pct"
    allow_market_when_wide_spread = "allow_market_when_wide_spread"
    universe_symbols = "universe_symbols"
    enable_watchdog = "enable_watchdog"
    watchdog_interval_sec = "watchdog_interval_sec"
    shock_1m_pct = "shock_1m_pct"
    shock_from_entry_pct = "shock_from_entry_pct"


class RiskPresetName(str, Enum):
    conservative = "conservative"
    normal = "normal"
    aggressive = "aggressive"


# Placeholders for A-stage expansion.
class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExecHint(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SPLIT = "SPLIT"

```

## apps/trader_engine/domain/models.py

```python
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from apps.trader_engine.domain.enums import EngineState


class RiskConfig(BaseModel):
    # Percent units: 0..100
    per_trade_risk_pct: float = Field(ge=0, le=100)
    max_exposure_pct: float = Field(ge=0, le=100)
    max_notional_pct: float = Field(ge=0, le=100)

    # Hardcap is enforced again in RiskService, but keep it here too.
    max_leverage: float = Field(ge=1, le=50)

    # Loss limits are negative ratios (allowed range: -1..0)
    # Example: -0.02 == -2%
    daily_loss_limit_pct: float = Field(ge=-1, le=0, default=-0.02)
    dd_limit_pct: float = Field(ge=-1, le=0, default=-0.15)

    lose_streak_n: int = Field(ge=1, le=10)
    cooldown_hours: float = Field(ge=1, le=72)

    # Strategy/execution controls (stored in the same singleton config row).
    min_hold_minutes: int = Field(ge=0, le=24 * 60, default=240)
    score_conf_threshold: float = Field(ge=0, le=1, default=0.65)
    score_gap_threshold: float = Field(ge=0, le=1, default=0.20)

    exec_limit_timeout_sec: float = Field(gt=0, le=60, default=5.0)
    exec_limit_retries: int = Field(ge=0, le=10, default=2)
    notify_interval_sec: int = Field(ge=10, le=3600)

    spread_max_pct: float = Field(ge=0, le=0.1, default=0.0015)
    allow_market_when_wide_spread: bool = Field(default=False)

    universe_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "XAUTUSDT"])

    enable_watchdog: bool = Field(default=True)
    watchdog_interval_sec: int = Field(ge=1, le=300, default=10)

    shock_1m_pct: float = Field(ge=0, le=0.5, default=0.010)
    shock_from_entry_pct: float = Field(ge=0, le=0.5, default=0.012)

    @field_validator("universe_symbols", mode="before")
    @classmethod
    def _parse_universe_symbols(cls, v):  # type: ignore[no-untyped-def]
        # Accept list[str] or CSV-like strings from DB/env.
        if v is None:
            return ["BTCUSDT", "ETHUSDT", "XAUTUSDT"]
        if isinstance(v, str):
            parts = [p.strip().upper() for p in v.split(",") if p.strip()]
            return parts
        if isinstance(v, (list, tuple)):
            return [str(x).strip().upper() for x in v if str(x).strip()]
        return v


class EngineStateRow(BaseModel):
    state: EngineState
    updated_at: datetime


class PnLState(BaseModel):
    # Stored as a singleton row (id=1). "day" is YYYY-MM-DD in UTC.
    day: str
    daily_realized_pnl: float = 0.0
    equity_peak: float = 0.0
    lose_streak: int = 0
    cooldown_until: datetime | None = None
    last_entry_symbol: str | None = None
    last_entry_at: datetime | None = None
    last_block_reason: str | None = None
    updated_at: datetime

```

## apps/trader_engine/services/risk_config_service.py

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

from apps.trader_engine.domain.enums import RiskConfigKey, RiskPresetName
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.storage.repositories import RiskConfigRepo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskConfigValidationError(Exception):
    message: str


_PRESETS: Dict[RiskPresetName, RiskConfig] = {
    RiskPresetName.conservative: RiskConfig(
        per_trade_risk_pct=0.5,
        max_exposure_pct=10,
        max_notional_pct=20,
        max_leverage=3,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.05,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=300,
    ),
    RiskPresetName.normal: RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=20,
        max_notional_pct=50,
        max_leverage=5,
        # Default policy baseline (percent units):
        # - daily_loss_limit: -2% (block new entries; optional STOP is handled by RiskService setting)
        # - dd_limit: -15% (PANIC)
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=120,
    ),
    RiskPresetName.aggressive: RiskConfig(
        per_trade_risk_pct=2,
        max_exposure_pct=40,
        max_notional_pct=80,
        max_leverage=10,
        daily_loss_limit_pct=-0.10,
        dd_limit_pct=-0.20,
        lose_streak_n=2,
        cooldown_hours=1,
        notify_interval_sec=60,
    ),
}


class RiskConfigService:
    def __init__(self, *, risk_config_repo: RiskConfigRepo) -> None:
        self._risk_config_repo = risk_config_repo

    def get_config(self) -> RiskConfig:
        cfg = self._risk_config_repo.get()
        if cfg is None:
            cfg = _PRESETS[RiskPresetName.normal]
            self._risk_config_repo.upsert(cfg)
            logger.info("risk_config_bootstrapped", extra={"preset": RiskPresetName.normal.value})
        else:
            # Forward-fill any newly added config fields/columns with model defaults.
            try:
                self._risk_config_repo.upsert(cfg)
            except Exception:
                pass
        return cfg

    def apply_preset(self, name: RiskPresetName) -> RiskConfig:
        cfg = _PRESETS[name]
        self._risk_config_repo.upsert(cfg)
        logger.info("risk_config_preset_applied", extra={"preset": name.value})
        return cfg

    def set_value(self, key: RiskConfigKey, value: str) -> RiskConfig:
        cfg = self.get_config()
        updated = cfg.model_copy()

        try:
            parsed: Any = self._parse_value(key, value)
        except ValueError as e:
            raise RiskConfigValidationError(str(e)) from e

        # Set and validate via pydantic (domain model constraints).
        payload = updated.model_dump()
        payload[key.value] = parsed
        try:
            validated = RiskConfig(**payload)
        except Exception as e:  # pydantic ValidationError
            raise RiskConfigValidationError(str(e)) from e

        self._risk_config_repo.upsert(validated)
        logger.info("risk_config_value_set", extra={"key": key.value})
        return validated

    @staticmethod
    def _parse_value(key: RiskConfigKey, value: str) -> Any:
        value = value.strip()
        if key in {
            RiskConfigKey.lose_streak_n,
            RiskConfigKey.notify_interval_sec,
            RiskConfigKey.min_hold_minutes,
            RiskConfigKey.exec_limit_retries,
            RiskConfigKey.watchdog_interval_sec,
        }:
            try:
                return int(value)
            except Exception as e:
                raise ValueError(f"invalid_int_for_{key.value}") from e

        if key in {RiskConfigKey.allow_market_when_wide_spread, RiskConfigKey.enable_watchdog}:
            v = value.lower()
            if v in ("1", "true", "t", "yes", "y", "on"):
                return True
            if v in ("0", "false", "f", "no", "n", "off"):
                return False
            raise ValueError(f"invalid_bool_for_{key.value}")

        if key == RiskConfigKey.universe_symbols:
            # CSV: BTCUSDT,ETHUSDT,XAUTUSDT
            parts = [p.strip().upper() for p in value.split(",") if p.strip()]
            if not parts:
                raise ValueError("universe_symbols_empty")
            return parts

        # Everything else: float
        try:
            return float(value)
        except Exception as e:
            raise ValueError(f"invalid_float_for_{key.value}") from e

```

## apps/trader_engine/services/risk_service.py

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Optional

from apps.trader_engine.domain.enums import EngineState, ExecHint
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_config_service import RiskConfigService

logger = logging.getLogger(__name__)


DecisionKind = Literal["ALLOW", "BLOCK", "PANIC"]


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: Optional[str] = None
    until: Optional[datetime] = None


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _pct_str(x: float) -> str:
    try:
        return f"{float(x):.4f}"
    except Exception:
        return str(x)


class RiskService:
    """Policy guard (risk engine) that decides whether an intent may place a real order.

    IMPORTANT: This service is expected to be called immediately before execution.
    If the decision is BLOCK or PANIC, execution must not send an order.
    """

    def __init__(
        self,
        *,
        risk: RiskConfigService,
        engine: EngineService,
        pnl: PnLService,
        stop_on_daily_loss: bool = False,
    ) -> None:
        self._risk = risk
        self._engine = engine
        self._pnl = pnl
        self._stop_on_daily_loss = bool(stop_on_daily_loss)

    def evaluate_pre_trade(
        self,
        intent: Mapping[str, Any],
        account_state: Mapping[str, Any],
        pnl_state: Mapping[str, Any],
        market_state: Mapping[str, Any],
    ) -> Decision:
        """Main entrypoint for pre-trade policy evaluation."""
        now = _utcnow()

        # If the engine is COOLDOWN but there's no active cooldown marker, recover to RUNNING.
        # This avoids a "stuck COOLDOWN" state if pnl_state was reset/cleared.
        cur_state = self._engine.get_state().state

        # Cooldown: active cooldown blocks entries.
        cd_until = pnl_state.get("cooldown_until")
        if isinstance(cd_until, datetime) and cd_until.tzinfo is None:
            cd_until = cd_until.replace(tzinfo=timezone.utc)
        if isinstance(cd_until, datetime) and now < cd_until:
            self._engine.set_state(EngineState.COOLDOWN)
            return Decision(kind="BLOCK", reason="cooldown_active", until=cd_until)

        # If cooldown has expired, allow engine to resume RUNNING automatically.
        if cur_state == EngineState.COOLDOWN and (cd_until is None or (isinstance(cd_until, datetime) and now >= cd_until)):
            self._engine.set_state(EngineState.RUNNING)
            # Persist cooldown cleared (best-effort).
            try:
                self._pnl.set_cooldown_until(cooldown_until=None)
            except Exception:
                pass

        if isinstance(cd_until, datetime) and now >= cd_until:
            if self._engine.get_state().state == EngineState.COOLDOWN:
                self._engine.set_state(EngineState.RUNNING)
            # Persist cooldown cleared (best-effort).
            try:
                self._pnl.set_cooldown_until(cooldown_until=None)
            except Exception:
                pass

        # Daily loss / drawdown checks (percent units).
        daily_pnl_pct = float(pnl_state.get("daily_pnl_pct") or 0.0)
        dd_pct = float(pnl_state.get("drawdown_pct") or 0.0)

        cfg = self._risk.get_config()

        # Compare ratios (e.g. -0.02 for -2%) against metrics (percent) converted to ratios.
        daily_pnl_ratio = daily_pnl_pct / 100.0
        dd_ratio = dd_pct / 100.0

        if daily_pnl_ratio <= float(cfg.daily_loss_limit_pct):
            if self._stop_on_daily_loss:
                self._engine.set_state(EngineState.STOPPED)
            return Decision(
                kind="BLOCK",
                reason=f"daily_loss_limit_reached:{_pct_str(daily_pnl_ratio)}<= {cfg.daily_loss_limit_pct}",
                until=None,
            )

        # dd_limit_pct is negative ratio; breach triggers PANIC.
        if dd_ratio <= float(cfg.dd_limit_pct):
            self._engine.panic()
            return Decision(kind="PANIC", reason=f"dd_limit_reached:{_pct_str(dd_ratio)}<= {cfg.dd_limit_pct}", until=None)

        # Lose streak based cooldown (if not already active).
        lose_streak = int(pnl_state.get("lose_streak") or 0)
        if lose_streak >= int(cfg.lose_streak_n):
            until = now + timedelta(hours=float(cfg.cooldown_hours))
            try:
                self._pnl.set_cooldown_until(cooldown_until=until)
            except Exception:
                pass
            self._engine.set_state(EngineState.COOLDOWN)
            return Decision(kind="BLOCK", reason="lose_streak_cooldown", until=until)

        # Spread guard (top-of-book based).
        spread_dec = self.spread_guard(intent=intent, market_state=market_state)
        if spread_dec.kind != "ALLOW":
            return spread_dec

        # Hard constraints (leverage / notional / exposure / 1-asset rule).
        cons = self.enforce_constraints(intent=intent, account_state=account_state)
        if cons.kind != "ALLOW":
            return cons

        return Decision(kind="ALLOW")

    def enforce_constraints(self, *, intent: Mapping[str, Any], account_state: Mapping[str, Any]) -> Decision:
        cfg = self._risk.get_config()

        equity_usdt = float(account_state.get("equity_usdt") or 0.0)
        if equity_usdt <= 0.0:
            return Decision(kind="BLOCK", reason="equity_unavailable")

        # Leverage: enforce user cfg but also hard cap <= 50.
        lev_in = intent.get("leverage")
        lev = float(lev_in) if lev_in is not None else 1.0
        max_lev = min(float(cfg.max_leverage), 50.0)
        if lev > max_lev:
            return Decision(kind="BLOCK", reason="leverage_above_max_leverage")

        # Single-asset safety: block if other symbols are already open.
        symbol = str(intent.get("symbol", "")).upper()
        open_symbols = account_state.get("open_symbols") or []
        if isinstance(open_symbols, (list, tuple)):
            others = [str(s).upper() for s in open_symbols if str(s).upper() and str(s).upper() != symbol]
            if others:
                return Decision(kind="BLOCK", reason="single_asset_rule_violation")

        notional = float(intent.get("notional_usdt_est") or intent.get("notional_usdt") or 0.0)
        if notional <= 0.0:
            return Decision(kind="BLOCK", reason="notional_unavailable")

        # Exposure cap: projected total exposure must be within max_exposure_pct of equity.
        existing_exposure = float(account_state.get("total_exposure_notional_usdt") or 0.0)
        projected = existing_exposure + notional
        max_exposure = equity_usdt * (float(cfg.max_exposure_pct) / 100.0)
        if projected > max_exposure:
            return Decision(kind="BLOCK", reason="exposure_above_max_exposure")

        # Notional cap (direct) within max_notional_pct of equity.
        max_notional = equity_usdt * (float(cfg.max_notional_pct) / 100.0)
        if notional > max_notional:
            return Decision(kind="BLOCK", reason="notional_above_max_notional")

        # per_trade_risk_pct: treat as margin budget; scale by leverage to a notional ceiling.
        # This is intentionally conservative and simple for MVP.
        max_margin = equity_usdt * (float(cfg.per_trade_risk_pct) / 100.0)
        max_notional_from_risk = max_margin * lev
        if notional > max_notional_from_risk:
            return Decision(kind="BLOCK", reason="per_trade_risk_exceeded")

        return Decision(kind="ALLOW")

    def spread_guard(self, *, intent: Mapping[str, Any], market_state: Mapping[str, Any]) -> Decision:
        cfg = self._risk.get_config()
        exec_hint = intent.get("exec_hint")
        try:
            hint = ExecHint(str(getattr(exec_hint, "value", exec_hint)).upper())
        except Exception:
            hint = ExecHint.MARKET

        bid = float(market_state.get("bid") or 0.0)
        ask = float(market_state.get("ask") or 0.0)
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            # If book is missing, treat as unsafe for MARKET.
            if hint == ExecHint.MARKET:
                return Decision(kind="BLOCK", reason="book_unavailable_market_disabled")
            return Decision(kind="ALLOW")

        mid = (ask + bid) / 2.0
        if mid <= 0.0:
            if hint == ExecHint.MARKET:
                return Decision(kind="BLOCK", reason="book_unavailable_market_disabled")
            return Decision(kind="ALLOW")

        spread_ratio = (ask - bid) / mid
        # Defensive: if misconfigured with percent-like values (> 0.1), assume it's percent and convert.
        max_ratio = float(cfg.spread_max_pct)
        if max_ratio > 0.1:
            max_ratio = max_ratio / 100.0
        if spread_ratio < max_ratio:
            return Decision(kind="ALLOW")

        if hint == ExecHint.MARKET and not bool(cfg.allow_market_when_wide_spread):
            return Decision(kind="BLOCK", reason=f"spread_too_wide_market_disabled:{_pct_str(spread_ratio)}")

        return Decision(kind="ALLOW")

```

## apps/trader_engine/services/execution_service.py

```python
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint
from apps.trader_engine.exchange.binance_usdm import BinanceAuthError, BinanceHTTPError, BinanceUSDMClient
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_service import RiskService
from apps.trader_engine.services.risk_config_service import RiskConfigService

logger = logging.getLogger(__name__)


Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class ExecutionRejected(Exception):
    message: str


@dataclass(frozen=True)
class ExecutionValidationError(Exception):
    message: str


def _dec(x: Any) -> Decimal:
    return Decimal(str(x))


def _floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return qty
    return (qty // step) * step


def _floor_to_tick(px: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return px
    return (px // tick) * tick


def _direction_to_entry_side(direction: Direction) -> Side:
    return "BUY" if direction == Direction.LONG else "SELL"


def _direction_to_close_side(position_amt: float) -> Side:
    # positionAmt > 0 means long; close is SELL. positionAmt < 0 means short; close is BUY.
    return "SELL" if position_amt > 0 else "BUY"


def _is_filled(status: Any) -> bool:
    return str(status).upper() == "FILLED"


def _coerce_enum(raw: Any, enum_cls: type[Enum], *, err: str) -> Enum:
    if isinstance(raw, enum_cls):
        return raw
    if raw is None:
        raise ExecutionValidationError(err)
    # Pydantic can hand us Enum instances from the request model_dump(). Use .value if present.
    if hasattr(raw, "value"):
        raw = getattr(raw, "value")
    try:
        return enum_cls(str(raw).upper())
    except Exception as e:
        raise ExecutionValidationError(err) from e


class ExecutionService:
    """Order/close execution for Binance USDT-M Futures (One-way only).

    Safety goals:
    - Orders only when engine is RUNNING
    - Reject when engine is PANIC
    - Enforce single-asset position rule
    - No leverage auto-adjust (never calls set_leverage automatically)
    """

    def __init__(
        self,
        *,
        client: BinanceUSDMClient,
        engine: EngineService,
        risk: RiskConfigService,
        pnl: Optional[PnLService] = None,
        policy: Optional[RiskService] = None,
        allowed_symbols: Sequence[str],
        split_parts: int = 3,
        dry_run: bool = True,
        dry_run_strict: bool = False,
    ) -> None:
        self._client = client
        self._engine = engine
        self._risk = risk
        self._pnl = pnl
        self._policy = policy
        self._allowed_symbols = [s.upper() for s in allowed_symbols]
        self._split_parts = max(int(split_parts), 2)
        self._dry_run = bool(dry_run)
        self._dry_run_strict = bool(dry_run_strict)

    def _require_not_panic(self) -> None:
        st = self._engine.get_state().state
        if st == EngineState.PANIC:
            raise ExecutionRejected("engine_in_panic")

    def _require_running_for_enter(self) -> None:
        st = self._engine.get_state().state
        if st == EngineState.PANIC:
            raise ExecutionRejected("engine_in_panic")
        # COOLDOWN is evaluated by RiskService; allow the request through so the caller
        # receives a specific risk block reason rather than a generic engine_not_running.
        if st not in (EngineState.RUNNING, EngineState.COOLDOWN):
            raise ExecutionRejected(f"engine_not_running:{st.value}")

    def _require_one_way_mode(self) -> None:
        # This is a hard requirement. If hedge mode is on, refuse to trade.
        try:
            ok = self._client.get_position_mode_one_way()
        except BinanceAuthError as e:
            raise ExecutionRejected("binance_auth_error") from e
        except BinanceHTTPError as e:
            raise ExecutionRejected(f"binance_http_{e.status_code}_code_{e.code}") from e
        if not ok:
            raise ExecutionRejected("hedge_mode_enabled")

    def _validate_symbol(self, symbol: str) -> str:
        # Kept for backward-compat; use _normalize_symbol + _validate_symbol_for_entry.
        return self._validate_symbol_for_entry(symbol)

    def _normalize_symbol(self, symbol: str) -> str:
        sym = symbol.strip().upper()
        if not sym:
            raise ExecutionValidationError("symbol_required")
        return sym

    def _validate_symbol_for_entry(self, symbol: str) -> str:
        sym = self._normalize_symbol(symbol)
        if self._allowed_symbols and sym not in self._allowed_symbols:
            raise ExecutionValidationError("symbol_not_allowed")
        return sym

    def _book(self, symbol: str) -> Mapping[str, Any]:
        bt = self._client.get_book_ticker(symbol)
        return bt

    def _best_price_ref(self, *, symbol: str, side: Side) -> Decimal:
        bt = self._book(symbol)
        bid = _dec(bt.get("bidPrice", "0") or "0")
        ask = _dec(bt.get("askPrice", "0") or "0")
        if side == "BUY":
            return ask if ask > 0 else bid
        return bid if bid > 0 else ask

    def _round_qty(self, *, symbol: str, qty: Decimal, is_market: bool) -> Decimal:
        f = self._client.get_symbol_filters(symbol=symbol)
        step = _dec(f.get("step_size") or "0")
        min_qty = _dec(f.get("min_qty") or "0")
        q = _floor_to_step(qty, step) if step > 0 else qty
        if min_qty > 0 and q < min_qty:
            raise ExecutionValidationError("quantity_below_min_qty")
        return q

    def _round_price(self, *, symbol: str, px: Decimal) -> Decimal:
        f = self._client.get_symbol_filters(symbol=symbol)
        tick = _dec(f.get("tick_size") or "0")
        return _floor_to_tick(px, tick) if tick > 0 else px

    def _check_min_notional(self, *, symbol: str, qty: Decimal, price_ref: Decimal) -> None:
        f = self._client.get_symbol_filters(symbol=symbol)
        mn = f.get("min_notional")
        if mn is None:
            return
        min_notional = _dec(mn)
        if min_notional <= 0:
            return
        notional = qty * price_ref
        if notional < min_notional:
            raise ExecutionValidationError("notional_below_min_notional")

    def _get_open_positions(self) -> Dict[str, Dict[str, float]]:
        return self._client.get_open_positions_any()

    def _assert_single_asset_rule_or_raise(self, positions: Mapping[str, Any]) -> None:
        if len(positions) > 1:
            # This should never happen if the rule is respected. Treat as hard stop.
            raise ExecutionRejected("multiple_open_positions_detected")

    def close_position(self, symbol: str) -> Dict[str, Any]:
        self._require_not_panic()
        if self._dry_run and self._dry_run_strict:
            raise ExecutionRejected("dry_run_strict_close_blocked")
        # Closing must be allowed even if the symbol isn't in the bot's allowed list.
        sym = self._normalize_symbol(symbol)

        # Always cancel orders first for the symbol.
        try:
            canceled = self._client.cancel_all_open_orders(symbol=sym)
        except Exception as e:  # noqa: BLE001
            logger.warning("cancel_all_open_orders_failed", extra={"symbol": sym, "err": type(e).__name__})
            canceled = []

        positions = self._get_open_positions()
        pos = positions.get(sym)
        if not pos:
            return {"symbol": sym, "closed": False, "reason": "no_open_position", "canceled": len(canceled)}

        amt = float(pos.get("position_amt", 0.0) or 0.0)
        if abs(amt) <= 0:
            return {"symbol": sym, "closed": False, "reason": "no_open_position", "canceled": len(canceled)}

        side = _direction_to_close_side(amt)
        qty = _dec(abs(amt))
        qty = self._round_qty(symbol=sym, qty=qty, is_market=True)

        bal_before = None
        try:
            bal_before = self._client.get_account_balance_usdtm()
        except Exception:
            bal_before = None

        try:
            order = self._client.place_order_market(symbol=sym, side=side, quantity=float(qty), reduce_only=True)
        except BinanceAuthError as e:
            raise ExecutionRejected("binance_auth_error") from e
        except BinanceHTTPError as e:
            raise ExecutionRejected(f"binance_http_{e.status_code}_code_{e.code}") from e

        # Best-effort realized PnL tracking: wallet balance delta around close.
        if self._pnl and bal_before and isinstance(bal_before, dict):
            try:
                time.sleep(0.2)
                bal_after = self._client.get_account_balance_usdtm()
                w0 = float(bal_before.get("wallet") or 0.0)
                w1 = float(bal_after.get("wallet") or 0.0)
                positions = self._client.get_open_positions_any()
                upnl = sum(float(r.get("unrealized_pnl") or 0.0) for r in positions.values())
                equity = w1 + upnl
                self._pnl.apply_realized_pnl_delta(realized_delta_usdt=(w1 - w0), equity_usdt=equity)
            except Exception:
                logger.exception("pnl_update_failed_on_close", extra={"symbol": sym})

        return {"symbol": sym, "closed": True, "canceled": len(canceled), "order": _safe_order(order)}

    def close_all_positions(self) -> Dict[str, Any]:
        self._require_not_panic()
        if self._dry_run and self._dry_run_strict:
            raise ExecutionRejected("dry_run_strict_close_blocked")
        bal_before = None
        try:
            bal_before = self._client.get_account_balance_usdtm()
        except Exception:
            bal_before = None
        positions = self._get_open_positions()
        if not positions:
            return {"closed": False, "reason": "no_open_position"}
        if len(positions) == 1:
            sym = next(iter(positions.keys()))
            return self.close_position(sym)
        # Defensive: even if rule is violated, prefer closing everything.
        out = self._panic_guarded_close_all(force=True)
        if self._pnl and bal_before and isinstance(bal_before, dict):
            try:
                time.sleep(0.2)
                bal_after = self._client.get_account_balance_usdtm()
                w0 = float(bal_before.get("wallet") or 0.0)
                w1 = float(bal_after.get("wallet") or 0.0)
                positions2 = self._client.get_open_positions_any()
                upnl = sum(float(r.get("unrealized_pnl") or 0.0) for r in positions2.values())
                equity = w1 + upnl
                self._pnl.apply_realized_pnl_delta(realized_delta_usdt=(w1 - w0), equity_usdt=equity)
            except Exception:
                logger.exception("pnl_update_failed_on_close_all")
        out["warning"] = "multiple_open_positions_detected"
        return out

    def enter_position(
        self,
        intent: Mapping[str, Any],
    ) -> Dict[str, Any]:
        self._require_running_for_enter()
        self._require_one_way_mode()
        if self._dry_run:
            if self._pnl:
                try:
                    self._pnl.set_last_block_reason("dry_run_enabled")
                except Exception:
                    pass
            logger.warning("dry_run_blocked_enter", extra={"symbol": intent.get("symbol"), "hint": intent.get("exec_hint")})
            raise ExecutionRejected("dry_run_enabled")

        symbol = self._validate_symbol_for_entry(str(intent.get("symbol", "")))
        direction = _coerce_enum(intent.get("direction"), Direction, err="invalid_direction")  # type: ignore[assignment]
        exec_hint = _coerce_enum(intent.get("exec_hint"), ExecHint, err="invalid_exec_hint")  # type: ignore[assignment]

        # Size inputs
        qty_in = intent.get("qty")
        notional_usdt = intent.get("notional_usdt")

        if qty_in is None and notional_usdt is None:
            raise ExecutionValidationError("qty_or_notional_required")

        # Optional leverage validation only (no auto-adjust).
        cfg = self._risk.get_config()
        lev = intent.get("leverage")
        if lev is not None:
            try:
                lev_f = float(lev)
            except Exception as e:
                raise ExecutionValidationError("invalid_leverage") from e
            if lev_f > cfg.max_leverage:
                raise ExecutionValidationError("leverage_above_max_leverage")

        side = _direction_to_entry_side(direction)

        # Sync: existing positions (across entire account) + enforce single-asset rule.
        positions = self._get_open_positions()
        if positions:
            self._assert_single_asset_rule_or_raise(positions)
            open_sym = next(iter(positions.keys()))
            open_amt = float(positions[open_sym].get("position_amt", 0.0) or 0.0)
            if open_sym != symbol:
                # Must close existing position before entering another symbol.
                self._panic_guarded_close_all(force=True)
            else:
                # Same symbol; check direction.
                if open_amt > 0 and direction == Direction.SHORT:
                    self._panic_guarded_close(symbol=symbol, force=True)
                elif open_amt < 0 and direction == Direction.LONG:
                    self._panic_guarded_close(symbol=symbol, force=True)
                else:
                    # Same symbol same direction: disallow for MVP.
                    raise ExecutionRejected("adding_to_position_not_allowed")

        # Determine reference price for sizing.
        price_ref = self._best_price_ref(symbol=symbol, side=side)
        if price_ref <= 0:
            raise ExecutionRejected("book_ticker_unavailable")

        qty: Decimal
        if qty_in is not None:
            qty = _dec(qty_in)
        else:
            qty = _dec(notional_usdt) / price_ref

        is_market = exec_hint == ExecHint.MARKET
        qty = self._round_qty(symbol=symbol, qty=qty, is_market=is_market)
        self._check_min_notional(symbol=symbol, qty=qty, price_ref=price_ref)

        if qty <= 0:
            raise ExecutionValidationError("quantity_invalid")

        # ------------------------------------------------------------
        # RISK POLICY GUARD (hard block point before any real order)
        # This must run immediately before we submit orders to Binance.
        # If it returns BLOCK/PANIC, we must not place an order.
        # ------------------------------------------------------------
        if self._policy and self._pnl:
            try:
                bal = self._client.get_account_balance_usdtm()
                pos = self._client.get_open_positions_any()
                wallet = float(bal.get("wallet") or 0.0)
                upnl = sum(float(r.get("unrealized_pnl") or 0.0) for r in pos.values())
                equity = wallet + upnl

                # Update peak tracking before evaluation.
                st = self._pnl.update_equity_peak(equity_usdt=equity)
                metrics = self._pnl.compute_metrics(st=st, equity_usdt=equity)

                bt = self._book(symbol)
                bid = float(bt.get("bidPrice", 0) or 0.0)
                ask = float(bt.get("askPrice", 0) or 0.0)

                # Approximate exposure from open positions using current book mid.
                total_exposure = 0.0
                open_symbols = list(pos.keys())
                for sym0, row0 in pos.items():
                    try:
                        bt0 = self._client.get_book_ticker(sym0)
                        bid0 = float(bt0.get("bidPrice", 0) or 0.0)
                        ask0 = float(bt0.get("askPrice", 0) or 0.0)
                        mid0 = (bid0 + ask0) / 2.0 if (bid0 and ask0) else 0.0
                        amt0 = float(row0.get("position_amt") or 0.0)
                        total_exposure += abs(amt0) * float(mid0 or 0.0)
                    except Exception:
                        continue

                notional_est = float(qty * price_ref)
                enriched_intent = dict(intent)
                enriched_intent["symbol"] = symbol
                enriched_intent["exec_hint"] = exec_hint
                enriched_intent["notional_usdt_est"] = notional_est

                acc_state = {
                    "wallet_usdt": wallet,
                    "upnl_usdt": upnl,
                    "equity_usdt": equity,
                    "open_symbols": open_symbols,
                    "total_exposure_notional_usdt": total_exposure,
                }
                pnl_state = {
                    "day": st.day,
                    "daily_realized_pnl": st.daily_realized_pnl,
                    "equity_peak": st.equity_peak,
                    "lose_streak": st.lose_streak,
                    "cooldown_until": st.cooldown_until,
                    "daily_pnl_pct": metrics.daily_pnl_pct,
                    "drawdown_pct": metrics.drawdown_pct,
                }
                mkt_state = {"bid": bid, "ask": ask}

                dec = self._policy.evaluate_pre_trade(
                    enriched_intent,
                    acc_state,
                    pnl_state,
                    mkt_state,
                )
                if dec.kind != "ALLOW":
                    reason = dec.reason or "risk_blocked"
                    self._pnl.set_last_block_reason(reason)
                    if dec.kind == "PANIC":
                        raise ExecutionRejected(f"risk_panic:{reason}")
                    raise ExecutionRejected(reason)

                # Clear last block reason on success path.
                self._pnl.set_last_block_reason(None)
            except ExecutionRejected:
                raise
            except Exception as e:  # noqa: BLE001
                # Guard failures should fail closed for safety.
                logger.exception("risk_guard_failed", extra={"err": type(e).__name__})
                if self._pnl:
                    try:
                        self._pnl.set_last_block_reason("risk_guard_failed")
                    except Exception:
                        pass
                raise ExecutionRejected("risk_guard_failed") from e

        # Safety: clear any stale open orders for the target symbol before a new entry.
        try:
            _ = self._client.cancel_all_open_orders(symbol=symbol)
        except Exception:
            pass

        if exec_hint == ExecHint.MARKET:
            try:
                order = self._client.place_order_market(
                    symbol=symbol, side=side, quantity=float(qty), reduce_only=False
                )
            except BinanceAuthError as e:
                raise ExecutionRejected("binance_auth_error") from e
            except BinanceHTTPError as e:
                raise ExecutionRejected(f"binance_http_{e.status_code}_code_{e.code}") from e
            if self._pnl:
                try:
                    from datetime import datetime, timezone

                    self._pnl.set_last_entry(symbol=symbol, at=datetime.now(tz=timezone.utc))
                except Exception:
                    pass
            return {"symbol": symbol, "hint": exec_hint.value, "orders": [_safe_order(order)]}

        if exec_hint == ExecHint.LIMIT:
            out = self._enter_limit(symbol=symbol, side=side, qty=qty)
            if self._pnl:
                try:
                    from datetime import datetime, timezone

                    self._pnl.set_last_entry(symbol=symbol, at=datetime.now(tz=timezone.utc))
                except Exception:
                    pass
            return out

        if exec_hint == ExecHint.SPLIT:
            out = self._enter_split(symbol=symbol, side=side, qty=qty)
            if self._pnl:
                try:
                    from datetime import datetime, timezone

                    self._pnl.set_last_entry(symbol=symbol, at=datetime.now(tz=timezone.utc))
                except Exception:
                    pass
            return out

        raise ExecutionValidationError("unsupported_exec_hint")

    def panic(self) -> Dict[str, Any]:
        if self._dry_run and self._dry_run_strict:
            raise ExecutionRejected("dry_run_strict_panic_blocked")
        # PANIC lock first.
        row = self._engine.panic()
        # Best-effort cleanup. Do not raise; return what happened.
        info: Dict[str, Any] = {"engine_state": row.state.value, "updated_at": row.updated_at.isoformat()}
        cleanup = self._panic_guarded_close_all(force=True)
        info.update({"cleanup": cleanup})
        return info

    def _panic_guarded_close(self, *, symbol: str, force: bool) -> Dict[str, Any]:
        try:
            if force:
                # bypass engine RUNNING check, but still block if PANIC is already set.
                return self.close_position(symbol)
            return self.close_position(symbol)
        except Exception as e:  # noqa: BLE001
            logger.exception("close_position_failed", extra={"symbol": symbol, "err": type(e).__name__})
            return {"ok": False, "symbol": symbol, "error": f"{type(e).__name__}: {e}"}

    def _panic_guarded_close_all(self, *, force: bool) -> Dict[str, Any]:
        # force=True is used for emergency cleanup paths (PANIC, forced symbol switch).
        try:
            if not force:
                self._require_not_panic()
            positions = self._get_open_positions()
            # Cancel open orders for enabled symbols AND any symbols we detect open positions on.
            cancels = 0
            cancel_syms = set(self._allowed_symbols) | set(positions.keys())
            for sym in sorted(cancel_syms):
                try:
                    cancels += len(self._client.cancel_all_open_orders(symbol=sym))
                except Exception:
                    continue
            if not positions:
                return {"ok": True, "closed": False, "reason": "no_open_position", "canceled": cancels}
            # Close every open position, even if multiple exist (defensive).
            orders: List[Dict[str, Any]] = []
            for sym, row in positions.items():
                amt = float(row.get("position_amt", 0.0) or 0.0)
                if abs(amt) <= 0:
                    continue
                side = _direction_to_close_side(amt)
                qty = self._round_qty(symbol=sym, qty=_dec(abs(amt)), is_market=True)
                o = self._client.place_order_market(symbol=sym, side=side, quantity=float(qty), reduce_only=True)
                orders.append(_safe_order(o))
            return {"ok": True, "closed": bool(orders), "canceled": cancels, "orders": orders}
        except Exception as e:  # noqa: BLE001
            logger.exception("panic_close_all_failed", extra={"err": type(e).__name__})
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _enter_limit(self, *, symbol: str, side: Side, qty: Decimal) -> Dict[str, Any]:
        orders: List[Dict[str, Any]] = []
        last_err: Optional[str] = None
        cfg = self._risk.get_config()
        limit_retries = int(cfg.exec_limit_retries)
        limit_timeout_sec = float(cfg.exec_limit_timeout_sec)

        for attempt in range(limit_retries + 1):
            price_ref = self._best_price_ref(symbol=symbol, side=side)
            px = self._round_price(symbol=symbol, px=price_ref)
            if px <= 0:
                raise ExecutionRejected("book_ticker_unavailable")
            try:
                placed = self._client.place_order_limit(
                    symbol=symbol,
                    side=side,
                    quantity=float(qty),
                    price=float(px),
                    post_only=False,
                    reduce_only=False,
                )
                orders.append(_safe_order(placed))
                oid = _extract_order_id(placed)
                if oid is None:
                    # Nothing more we can do reliably.
                    return {"symbol": symbol, "hint": ExecHint.LIMIT.value, "orders": orders}

                deadline = time.time() + limit_timeout_sec
                while time.time() < deadline:
                    o = self._client.get_order(symbol=symbol, order_id=oid)
                    if _is_filled(o.get("status")):
                        orders.append(_safe_order(o))
                        return {"symbol": symbol, "hint": ExecHint.LIMIT.value, "orders": orders}
                    time.sleep(0.2)

                # Not filled -> cancel all open orders for symbol.
                self._client.cancel_all_open_orders(symbol=symbol)
                last_err = "limit_timeout"
            except BinanceHTTPError as e:
                last_err = f"http_{e.status_code} code={e.code}"
            except BinanceAuthError:
                last_err = "binance_auth_error"
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"

        raise ExecutionRejected(last_err or "limit_failed")

    def _enter_split(self, *, symbol: str, side: Side, qty: Decimal) -> Dict[str, Any]:
        # Split qty into N parts and submit sequential LIMIT orders.
        parts = self._split_parts
        part_qty = qty / _dec(parts)
        # Round down each part; remainder is added to last part (rounded again).
        rounded_parts: List[Decimal] = []
        for _ in range(parts - 1):
            q = self._round_qty(symbol=symbol, qty=part_qty, is_market=False)
            rounded_parts.append(q)
        last = qty - sum(rounded_parts, start=_dec("0"))
        last = self._round_qty(symbol=symbol, qty=last, is_market=False)
        rounded_parts.append(last)

        orders: List[Dict[str, Any]] = []
        for q in rounded_parts:
            if q <= 0:
                continue
            res = self._enter_limit(symbol=symbol, side=side, qty=q)
            orders.extend(res.get("orders", []))
        return {"symbol": symbol, "hint": ExecHint.SPLIT.value, "orders": orders}


def _extract_order_id(payload: Mapping[str, Any]) -> Optional[int]:
    if "orderId" in payload:
        try:
            return int(payload["orderId"])
        except Exception:
            return None
    if "order_id" in payload:
        try:
            return int(payload["order_id"])
        except Exception:
            return None
    return None


def _safe_order(order: Mapping[str, Any]) -> Dict[str, Any]:
    # Keep a safe subset; never include signature/api keys.
    return {
        "symbol": order.get("symbol"),
        "order_id": order.get("orderId", order.get("order_id")),
        "client_order_id": order.get("clientOrderId", order.get("client_order_id")),
        "side": order.get("side"),
        "type": order.get("type"),
        "status": order.get("status"),
        "price": order.get("price"),
        "avg_price": order.get("avgPrice"),
        "orig_qty": order.get("origQty", order.get("orig_qty")),
        "executed_qty": order.get("executedQty", order.get("executed_qty")),
        "update_time": order.get("updateTime", order.get("time")),
    }

```

## apps/trader_engine/services/binance_service.py

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from apps.trader_engine.exchange.binance_usdm import (
    BinanceAuthError,
    BinanceHTTPError,
    BinanceUSDMClient,
)

logger = logging.getLogger(__name__)

FIXED_TARGET_SYMBOLS = {"BTCUSDT", "ETHUSDT", "XAUTUSDT"}


@dataclass
class BinanceStartupState:
    enabled_symbols: List[str]
    disabled_symbols: List[Dict[str, str]]
    time_offset_ms: int
    time_measured_at_ms: int
    server_time_ms: int
    ok: bool
    error: Optional[str] = None


class BinanceService:
    def __init__(
        self,
        *,
        client: BinanceUSDMClient,
        allowed_symbols: Sequence[str],
        spread_wide_pct: float = 0.005,
    ) -> None:
        self._client = client
        requested = [s.strip().upper() for s in allowed_symbols if s and s.strip()]
        if not requested:
            requested = sorted(FIXED_TARGET_SYMBOLS)
        self._ignored_symbols = [s for s in requested if s not in FIXED_TARGET_SYMBOLS]
        self._allowed_symbols = [s for s in requested if s in FIXED_TARGET_SYMBOLS]
        # Stored as ratio (e.g. 0.0015 == 0.15%). If given a percent-like value (> 0.1),
        # assume it's percent and convert.
        x = float(spread_wide_pct)
        self._spread_wide_ratio = (x / 100.0) if x > 0.1 else x

        self._startup: Optional[BinanceStartupState] = None

    def close(self) -> None:
        self._client.close()

    @property
    def enabled_symbols(self) -> List[str]:
        if not self._startup:
            return []
        return list(self._startup.enabled_symbols)

    @property
    def disabled_symbols(self) -> List[Dict[str, str]]:
        if not self._startup:
            return []
        return list(self._startup.disabled_symbols)

    def startup(self) -> BinanceStartupState:
        """Validate symbols + measure time offset. Never logs secrets."""
        try:
            server_time_ms = self._client.get_server_time_ms()
            offset = self._client.time_sync.measure(server_time_ms=server_time_ms)
            enabled, disabled = self._client.validate_symbols(self._allowed_symbols)
            if self._ignored_symbols:
                disabled = [{"symbol": s, "reason": "not_in_fixed_target_list"} for s in self._ignored_symbols] + list(
                    disabled
                )
            st = BinanceStartupState(
                enabled_symbols=enabled,
                disabled_symbols=disabled,
                time_offset_ms=offset.offset_ms,
                time_measured_at_ms=offset.measured_at_ms,
                server_time_ms=server_time_ms,
                ok=True,
                error=None,
            )
            self._startup = st
            return st
        except Exception as e:  # noqa: BLE001
            # Do not prevent API boot; surface error in /status.
            msg = f"{type(e).__name__}: {e}"
            st = BinanceStartupState(
                enabled_symbols=[],
                disabled_symbols=[{"symbol": s, "reason": "startup_failed"} for s in self._allowed_symbols],
                time_offset_ms=0,
                time_measured_at_ms=0,
                server_time_ms=0,
                ok=False,
                error=msg,
            )
            self._startup = st
            logger.warning("binance_startup_failed", extra={"err": type(e).__name__})
            return st

    def get_status(self) -> Mapping[str, Any]:
        if not self._startup:
            self.startup()
        assert self._startup is not None

        enabled = self._startup.enabled_symbols
        disabled = self._startup.disabled_symbols

        # Public (no key required): book ticker for spread.
        spreads: Dict[str, Any] = {}
        for sym in enabled:
            try:
                bt = self._client.get_book_ticker(sym)
                bid = float(bt.get("bidPrice", 0) or 0)
                ask = float(bt.get("askPrice", 0) or 0)
                spread = max(ask - bid, 0.0)
                mid = (ask + bid) / 2.0 if (ask and bid) else 0.0
                spread_ratio = (spread / mid) if mid else 0.0
                spread_pct = spread_ratio * 100.0
                spreads[sym] = {
                    "bid": bid,
                    "ask": ask,
                    "spread": spread,
                    "spread_pct": spread_pct,
                    "is_wide": bool(spread_ratio >= self._spread_wide_ratio),
                }
            except Exception as e:  # noqa: BLE001
                spreads[sym] = {"error": f"{type(e).__name__}: {e}"}

        # Private (API key/secret required): balance/positions/open orders.
        private_ok = True
        private_error: Optional[str] = None
        usdt_balance: Optional[Dict[str, Any]] = None
        positions: Optional[Dict[str, Any]] = None
        open_orders: Optional[Dict[str, Any]] = None

        try:
            usdt_balance = self._client.get_account_balance_usdtm()
            positions = self._client.get_positions_usdtm(enabled)
            open_orders = self._client.get_open_orders_usdtm(enabled)
        except BinanceAuthError as e:
            private_ok = False
            private_error = str(e)
        except BinanceHTTPError as e:
            private_ok = False
            private_error = f"http_{e.status_code} code={e.code} msg={e.msg}"
        except Exception as e:  # noqa: BLE001
            private_ok = False
            private_error = f"{type(e).__name__}: {e}"

        return {
            "startup_ok": self._startup.ok,
            "startup_error": self._startup.error,
            "enabled_symbols": enabled,
            "disabled_symbols": disabled,
            "server_time_ms": self._startup.server_time_ms,
            "time_offset_ms": self._startup.time_offset_ms,
            "time_measured_at_ms": self._startup.time_measured_at_ms,
            "private_ok": private_ok,
            "private_error": private_error,
            "usdt_balance": usdt_balance,
            "positions": positions,
            "open_orders": open_orders,
            "spreads": spreads,
        }

```

## apps/trader_engine/services/ai_service.py

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Literal, Mapping, Optional

logger = logging.getLogger(__name__)


TargetAsset = Literal["BTCUSDT", "ETHUSDT", "XAUTUSDT"]
SignalDirection = Literal["LONG", "SHORT", "HOLD"]
SignalExecHint = Literal["MARKET", "LIMIT", "SPLIT"]
RiskTag = Literal["NORMAL", "VOL_SHOCK", "NEWS_RISK"]


@dataclass(frozen=True)
class AiSignal:
    """AI signal output (signal only, no execution authority)."""

    target_asset: TargetAsset
    direction: SignalDirection
    confidence: float  # 0..1
    exec_hint: SignalExecHint
    risk_tag: RiskTag
    notes: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "target_asset": self.target_asset,
            "direction": self.direction,
            "confidence": float(self.confidence),
            "exec_hint": self.exec_hint,
            "risk_tag": self.risk_tag,
            "notes": self.notes,
        }


class AiService:
    """AI interface layer.

    IMPORTANT SECURITY BOUNDARY:
    - This service returns signals only.
    - It must never call execution / place orders.
    - Actual execution remains owned by Risk Guard + ExecutionService.
    """

    def __init__(
        self,
        *,
        mode: str = "stub",
        conf_threshold: float = 0.65,
        manual_risk_tag: str = "",
    ) -> None:
        self._mode = str(mode or "stub").strip().lower()
        self._conf_threshold = float(conf_threshold)
        self._manual_risk_tag = str(manual_risk_tag or "").strip().upper()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def conf_threshold(self) -> float:
        return self._conf_threshold

    def get_signal(self, context: Mapping[str, Any]) -> AiSignal:
        """Return an AI recommendation signal based on the provided context.

        Context is a plain mapping to keep the interface stable.
        """
        if self._mode == "stub":
            return self._stub_signal(context)
        if self._mode == "openai":
            raise NotImplementedError("AI_MODE=openai is not wired in MVP; use stub mode")
        if self._mode == "local":
            raise NotImplementedError("AI_MODE=local is not wired in MVP; use stub mode")
        # Fail closed: unknown modes fall back to stub.
        logger.warning("ai_mode_unknown_fallback_to_stub", extra={"mode": self._mode})
        return self._stub_signal(context)

    def _stub_signal(self, context: Mapping[str, Any]) -> AiSignal:
        # Deterministic: derive from scheduler candidate + scores.
        cand = context.get("candidate") or {}
        sym = str((cand.get("symbol") or "BTCUSDT")).upper()
        if sym not in ("BTCUSDT", "ETHUSDT", "XAUTUSDT"):
            sym = "BTCUSDT"

        # Candidate fields (from DecisionService.pick_candidate)
        direction = str(cand.get("direction") or "HOLD").upper()
        strength = float(cand.get("strength") or 0.0)
        vol_tag = str(cand.get("vol_tag") or "NORMAL").upper()

        # Manual override tag if provided.
        risk_tag: RiskTag
        if self._manual_risk_tag == "NEWS_RISK":
            risk_tag = "NEWS_RISK"
        elif vol_tag == "VOL_SHOCK":
            risk_tag = "VOL_SHOCK"
        else:
            risk_tag = "NORMAL"

        if direction not in ("LONG", "SHORT"):
            return AiSignal(
                target_asset=sym,  # type: ignore[return-value]
                direction="HOLD",
                confidence=0.0,
                exec_hint="LIMIT",
                risk_tag=risk_tag,
                notes="no_candidate",
            )

        conf = max(0.0, min(1.0, strength))
        if conf < self._conf_threshold:
            return AiSignal(
                target_asset=sym,  # type: ignore[return-value]
                direction="HOLD",
                confidence=conf,
                exec_hint="LIMIT",
                risk_tag=risk_tag,
                notes=f"below_threshold:{self._conf_threshold}",
            )

        # In stub: recommend LIMIT by default (safer with spread guards).
        return AiSignal(
            target_asset=sym,  # type: ignore[return-value]
            direction="LONG" if direction == "LONG" else "SHORT",
            confidence=conf,
            exec_hint="LIMIT",
            risk_tag=risk_tag,
            notes="stub_from_rule_candidate",
        )

```

## apps/trader_engine/services/pnl_service.py

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from apps.trader_engine.domain.models import PnLState
from apps.trader_engine.storage.repositories import PnLStateRepo

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _utc_day_str(ts: Optional[datetime] = None) -> str:
    t = ts or _utcnow()
    return t.date().isoformat()


@dataclass(frozen=True)
class PnLMetrics:
    equity_usdt: float
    daily_pnl_pct: float
    drawdown_pct: float


class PnLService:
    """Minimal PnL tracking to support policy guards.

    Notes:
    - "daily_realized_pnl" is tracked as a running sum for the current UTC day.
    - Realized PnL is measured by wallet balance deltas around close actions (best-effort).
    - equity_peak is tracked to compute simple drawdown (equity vs peak).
    """

    def __init__(self, *, repo: PnLStateRepo) -> None:
        self._repo = repo

    def get_or_bootstrap(self) -> PnLState:
        st = self._repo.get()
        now = _utcnow()
        today = _utc_day_str(now)
        if st is None:
            st = PnLState(
                day=today,
                daily_realized_pnl=0.0,
                equity_peak=0.0,
                lose_streak=0,
                cooldown_until=None,
                last_entry_symbol=None,
                last_entry_at=None,
                last_block_reason=None,
                updated_at=now,
            )
            self._repo.upsert(st)
            return st

        # Day rollover: reset only daily realized PnL; keep streak/cooldown as risk state.
        if st.day != today:
            st = st.model_copy(
                update={
                    "day": today,
                    "daily_realized_pnl": 0.0,
                    "updated_at": now,
                }
            )
            self._repo.upsert(st)
        return st

    def set_last_block_reason(self, reason: Optional[str]) -> None:
        st = self.get_or_bootstrap()
        now = _utcnow()
        st = st.model_copy(update={"last_block_reason": reason, "updated_at": now})
        self._repo.upsert(st)

    def set_last_entry(self, *, symbol: Optional[str], at: Optional[datetime]) -> None:
        st = self.get_or_bootstrap()
        now = _utcnow()
        st = st.model_copy(update={"last_entry_symbol": symbol, "last_entry_at": at, "updated_at": now})
        self._repo.upsert(st)

    def update_equity_peak(self, *, equity_usdt: float) -> PnLState:
        st = self.get_or_bootstrap()
        now = _utcnow()
        peak = float(st.equity_peak or 0.0)
        if peak <= 0.0 or equity_usdt > peak:
            st = st.model_copy(update={"equity_peak": float(equity_usdt), "updated_at": now})
            self._repo.upsert(st)
        return st

    def apply_realized_pnl_delta(self, *, realized_delta_usdt: float, equity_usdt: float) -> PnLState:
        st = self.get_or_bootstrap()
        now = _utcnow()

        daily = float(st.daily_realized_pnl or 0.0) + float(realized_delta_usdt)
        # Lose streak is based on realized PnL sign at close.
        if realized_delta_usdt < 0:
            lose_streak = int(st.lose_streak or 0) + 1
        else:
            lose_streak = 0

        peak = float(st.equity_peak or 0.0)
        if peak <= 0.0 or equity_usdt > peak:
            peak = float(equity_usdt)

        st = st.model_copy(
            update={
                "daily_realized_pnl": daily,
                "lose_streak": lose_streak,
                "equity_peak": peak,
                "updated_at": now,
            }
        )
        self._repo.upsert(st)
        logger.info(
            "pnl_state_updated",
            extra={
                "day": st.day,
                "daily_realized_pnl": st.daily_realized_pnl,
                "lose_streak": st.lose_streak,
                "equity_peak": st.equity_peak,
            },
        )
        return st

    def set_cooldown_until(self, *, cooldown_until: Optional[datetime]) -> PnLState:
        st = self.get_or_bootstrap()
        now = _utcnow()
        st = st.model_copy(update={"cooldown_until": cooldown_until, "updated_at": now})
        self._repo.upsert(st)
        return st

    def compute_metrics(self, *, st: PnLState, equity_usdt: float) -> PnLMetrics:
        equity = float(equity_usdt or 0.0)
        if equity <= 0.0:
            return PnLMetrics(equity_usdt=equity, daily_pnl_pct=0.0, drawdown_pct=0.0)

        daily_realized = float(st.daily_realized_pnl or 0.0)
        daily_pnl_pct = (daily_realized / equity) * 100.0

        peak = float(st.equity_peak or 0.0)
        if peak <= 0.0:
            drawdown_pct = 0.0
        else:
            drawdown_pct = ((equity - peak) / peak) * 100.0

        return PnLMetrics(equity_usdt=equity, daily_pnl_pct=daily_pnl_pct, drawdown_pct=drawdown_pct)

```

## apps/trader_engine/storage/db.py

```python
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Iterable, Optional


SCHEMA_MIGRATIONS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS risk_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        per_trade_risk_pct REAL NOT NULL,
        max_exposure_pct REAL NOT NULL,
        max_notional_pct REAL NOT NULL,
        max_leverage REAL NOT NULL,
        -- Legacy percent-unit fields kept for backward compatibility (e.g. -2 for -2%)
        daily_loss_limit REAL NOT NULL,
        dd_limit REAL NOT NULL,
        -- Preferred ratio-unit fields (e.g. -0.02 for -2%)
        daily_loss_limit_pct REAL,
        dd_limit_pct REAL,
        lose_streak_n INTEGER NOT NULL,
        cooldown_hours REAL NOT NULL,
        notify_interval_sec INTEGER NOT NULL,
        min_hold_minutes INTEGER,
        score_conf_threshold REAL,
        score_gap_threshold REAL,
        exec_limit_timeout_sec REAL,
        exec_limit_retries INTEGER,
        spread_max_pct REAL,
        allow_market_when_wide_spread INTEGER,
        universe_symbols TEXT,
        enable_watchdog INTEGER,
        watchdog_interval_sec INTEGER,
        shock_1m_pct REAL,
        shock_from_entry_pct REAL,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS engine_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        state TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS status_snapshot (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS pnl_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        day TEXT NOT NULL,
        daily_realized_pnl REAL NOT NULL,
        equity_peak REAL NOT NULL,
        lose_streak INTEGER NOT NULL,
        cooldown_until TEXT,
        last_entry_symbol TEXT,
        last_entry_at TEXT,
        last_block_reason TEXT,
        updated_at TEXT NOT NULL
    )
    """.strip(),
]


@dataclass
class Database:
    conn: sqlite3.Connection
    lock: threading.RLock

    def execute(self, sql: str, params: Iterable[object] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def executescript(self, sql: str) -> None:
        with self.lock:
            self.conn.executescript(sql)
            self.conn.commit()


def connect(db_path: str) -> Database:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,
        isolation_level=None,  # autocommit mode; we still guard with a lock
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return Database(conn=conn, lock=threading.RLock())


def get_schema_version(db: Database) -> int:
    try:
        row = db.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        if not row or row["v"] is None:
            return 0
        return int(row["v"])
    except sqlite3.OperationalError:
        return 0


def migrate(db: Database) -> None:
    # Simple linear migrations: apply SCHEMA_MIGRATIONS entries in order.
    # Each entry index is its version number.
    db.executescript(SCHEMA_MIGRATIONS[0])
    current = get_schema_version(db)

    for version in range(current + 1, len(SCHEMA_MIGRATIONS)):
        db.executescript(SCHEMA_MIGRATIONS[version])
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
            (version,),
        )

    # Best-effort forward-compatible schema tweaks for existing DBs.
    _ensure_columns(db)
    _backfill_derived_columns(db)


def _table_columns(db: Database, table: str) -> set[str]:
    try:
        rows = db.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]) for r in rows if r and r["name"]}
    except Exception:
        return set()


def _ensure_columns(db: Database) -> None:
    # SQLite lacks "ADD COLUMN IF NOT EXISTS", so we check first.
    risk_cols = _table_columns(db, "risk_config")
    if risk_cols:
        adds: list[tuple[str, str]] = [
            ("daily_loss_limit_pct", "REAL"),
            ("dd_limit_pct", "REAL"),
            ("min_hold_minutes", "INTEGER"),
            ("score_conf_threshold", "REAL"),
            ("score_gap_threshold", "REAL"),
            ("exec_limit_timeout_sec", "REAL"),
            ("exec_limit_retries", "INTEGER"),
            ("spread_max_pct", "REAL"),
            ("allow_market_when_wide_spread", "INTEGER"),
            ("universe_symbols", "TEXT"),
            ("enable_watchdog", "INTEGER"),
            ("watchdog_interval_sec", "INTEGER"),
            ("shock_1m_pct", "REAL"),
            ("shock_from_entry_pct", "REAL"),
        ]
        for name, typ in adds:
            if name in risk_cols:
                continue
            try:
                db.execute(f"ALTER TABLE risk_config ADD COLUMN {name} {typ}")
            except Exception:
                pass

    pnl_cols = _table_columns(db, "pnl_state")
    if pnl_cols:
        adds2: list[tuple[str, str]] = [
            ("last_entry_symbol", "TEXT"),
            ("last_entry_at", "TEXT"),
        ]
        for name, typ in adds2:
            if name in pnl_cols:
                continue
            try:
                db.execute(f"ALTER TABLE pnl_state ADD COLUMN {name} {typ}")
            except Exception:
                pass


def _backfill_derived_columns(db: Database) -> None:
    # Backfill ratio-unit loss limits from legacy percent-unit columns when present.
    cols = _table_columns(db, "risk_config")
    if not cols:
        return
    if "daily_loss_limit_pct" in cols and "daily_loss_limit" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET daily_loss_limit_pct = (daily_loss_limit / 100.0)
                WHERE id=1 AND (daily_loss_limit_pct IS NULL)
                """.strip()
            )
        except Exception:
            pass
    if "dd_limit_pct" in cols and "dd_limit" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET dd_limit_pct = (dd_limit / 100.0)
                WHERE id=1 AND (dd_limit_pct IS NULL)
                """.strip()
            )
        except Exception:
            pass


def close(db: Database) -> None:
    with db.lock:
        db.conn.close()

```

## apps/trader_engine/storage/repositories.py

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import EngineStateRow, PnLState, RiskConfig
from apps.trader_engine.storage.db import Database


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    # ISO 8601 parser for the limited scope of this project.
    return datetime.fromisoformat(value)


class RiskConfigRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> Optional[RiskConfig]:
        row = self._db.conn.execute("SELECT * FROM risk_config WHERE id=1").fetchone()
        if not row:
            return None
        keys = set(row.keys())
        payload: Dict[str, Any] = {}
        for k in RiskConfig.model_fields.keys():
            if k in keys:
                # If an existing DB row has newly-added columns, they'll be NULL.
                # Do not pass None into pydantic; let model defaults apply instead.
                v = row[k]
                if v is None:
                    continue
                payload[k] = v

        # Backward-compat: derive ratio-unit loss limits from legacy percent columns.
        if "daily_loss_limit_pct" not in payload:
            if "daily_loss_limit_pct" in keys and row["daily_loss_limit_pct"] is not None:
                payload["daily_loss_limit_pct"] = float(row["daily_loss_limit_pct"])
            elif "daily_loss_limit" in keys and row["daily_loss_limit"] is not None:
                payload["daily_loss_limit_pct"] = float(row["daily_loss_limit"]) / 100.0

        if "dd_limit_pct" not in payload:
            if "dd_limit_pct" in keys and row["dd_limit_pct"] is not None:
                payload["dd_limit_pct"] = float(row["dd_limit_pct"])
            elif "dd_limit" in keys and row["dd_limit"] is not None:
                payload["dd_limit_pct"] = float(row["dd_limit"]) / 100.0

        return RiskConfig(**payload)

    def upsert(self, cfg: RiskConfig) -> None:
        # Keep legacy percent-unit columns in sync for older DBs/tools.
        daily_loss_limit_legacy = float(cfg.daily_loss_limit_pct) * 100.0
        dd_limit_legacy = float(cfg.dd_limit_pct) * 100.0
        universe_csv = ",".join([s.strip().upper() for s in (cfg.universe_symbols or []) if s.strip()])

        self._db.execute(
            """
            INSERT INTO risk_config(
                id,
                per_trade_risk_pct,
                max_exposure_pct,
                max_notional_pct,
                max_leverage,
                daily_loss_limit,
                dd_limit,
                daily_loss_limit_pct,
                dd_limit_pct,
                lose_streak_n,
                cooldown_hours,
                notify_interval_sec,
                min_hold_minutes,
                score_conf_threshold,
                score_gap_threshold,
                exec_limit_timeout_sec,
                exec_limit_retries,
                spread_max_pct,
                allow_market_when_wide_spread,
                universe_symbols,
                enable_watchdog,
                watchdog_interval_sec,
                shock_1m_pct,
                shock_from_entry_pct,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                per_trade_risk_pct=excluded.per_trade_risk_pct,
                max_exposure_pct=excluded.max_exposure_pct,
                max_notional_pct=excluded.max_notional_pct,
                max_leverage=excluded.max_leverage,
                daily_loss_limit=excluded.daily_loss_limit,
                dd_limit=excluded.dd_limit,
                daily_loss_limit_pct=excluded.daily_loss_limit_pct,
                dd_limit_pct=excluded.dd_limit_pct,
                lose_streak_n=excluded.lose_streak_n,
                cooldown_hours=excluded.cooldown_hours,
                notify_interval_sec=excluded.notify_interval_sec,
                min_hold_minutes=excluded.min_hold_minutes,
                score_conf_threshold=excluded.score_conf_threshold,
                score_gap_threshold=excluded.score_gap_threshold,
                exec_limit_timeout_sec=excluded.exec_limit_timeout_sec,
                exec_limit_retries=excluded.exec_limit_retries,
                spread_max_pct=excluded.spread_max_pct,
                allow_market_when_wide_spread=excluded.allow_market_when_wide_spread,
                universe_symbols=excluded.universe_symbols,
                enable_watchdog=excluded.enable_watchdog,
                watchdog_interval_sec=excluded.watchdog_interval_sec,
                shock_1m_pct=excluded.shock_1m_pct,
                shock_from_entry_pct=excluded.shock_from_entry_pct,
                updated_at=excluded.updated_at
            """.strip(),
            (
                cfg.per_trade_risk_pct,
                cfg.max_exposure_pct,
                cfg.max_notional_pct,
                cfg.max_leverage,
                daily_loss_limit_legacy,
                dd_limit_legacy,
                float(cfg.daily_loss_limit_pct),
                float(cfg.dd_limit_pct),
                cfg.lose_streak_n,
                cfg.cooldown_hours,
                cfg.notify_interval_sec,
                int(cfg.min_hold_minutes),
                float(cfg.score_conf_threshold),
                float(cfg.score_gap_threshold),
                float(cfg.exec_limit_timeout_sec),
                int(cfg.exec_limit_retries),
                float(cfg.spread_max_pct),
                int(bool(cfg.allow_market_when_wide_spread)),
                universe_csv,
                int(bool(cfg.enable_watchdog)),
                int(cfg.watchdog_interval_sec),
                float(cfg.shock_1m_pct),
                float(cfg.shock_from_entry_pct),
                _utcnow_iso(),
            ),
        )


class EngineStateRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> EngineStateRow:
        row = self._db.conn.execute("SELECT * FROM engine_state WHERE id=1").fetchone()
        if not row:
            # Default bootstrap state (persisted).
            state = EngineStateRow(state=EngineState.STOPPED, updated_at=datetime.now(tz=timezone.utc))
            self.upsert(state)
            return state
        return EngineStateRow(state=EngineState(row["state"]), updated_at=_parse_dt(row["updated_at"]))

    def upsert(self, state: EngineStateRow) -> None:
        self._db.execute(
            """
            INSERT INTO engine_state(id, state, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state=excluded.state,
                updated_at=excluded.updated_at
            """.strip(),
            (state.state.value, state.updated_at.isoformat()),
        )


class StatusSnapshotRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_json(self) -> Optional[Dict[str, Any]]:
        row = self._db.conn.execute("SELECT json FROM status_snapshot WHERE id=1").fetchone()
        if not row:
            return None
        return json.loads(row["json"])

    def upsert_json(self, payload: Dict[str, Any]) -> None:
        self._db.execute(
            """
            INSERT INTO status_snapshot(id, json, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                json=excluded.json,
                updated_at=excluded.updated_at
            """.strip(),
            (json.dumps(payload, ensure_ascii=True, default=str), _utcnow_iso()),
        )


class PnLStateRepo:
    """Persist minimal PnL/risk-related state required for policy guards.

    Stored as a singleton row (id=1).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> Optional[PnLState]:
        row = self._db.conn.execute("SELECT * FROM pnl_state WHERE id=1").fetchone()
        if not row:
            return None
        cooldown_until = row["cooldown_until"]
        keys = set(row.keys())
        last_entry_at = row["last_entry_at"] if "last_entry_at" in keys else None
        last_entry_symbol = row["last_entry_symbol"] if "last_entry_symbol" in keys else None
        return PnLState(
            day=str(row["day"]),
            daily_realized_pnl=float(row["daily_realized_pnl"] or 0.0),
            equity_peak=float(row["equity_peak"] or 0.0),
            lose_streak=int(row["lose_streak"] or 0),
            cooldown_until=_parse_dt(cooldown_until) if cooldown_until else None,
            last_entry_symbol=str(last_entry_symbol) if last_entry_symbol is not None else None,
            last_entry_at=_parse_dt(last_entry_at) if last_entry_at else None,
            last_block_reason=str(row["last_block_reason"]) if row["last_block_reason"] is not None else None,
            updated_at=_parse_dt(row["updated_at"]),
        )

    def upsert(self, st: PnLState) -> None:
        self._db.execute(
            """
            INSERT INTO pnl_state(
                id,
                day,
                daily_realized_pnl,
                equity_peak,
                lose_streak,
                cooldown_until,
                last_entry_symbol,
                last_entry_at,
                last_block_reason,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                day=excluded.day,
                daily_realized_pnl=excluded.daily_realized_pnl,
                equity_peak=excluded.equity_peak,
                lose_streak=excluded.lose_streak,
                cooldown_until=excluded.cooldown_until,
                last_entry_symbol=excluded.last_entry_symbol,
                last_entry_at=excluded.last_entry_at,
                last_block_reason=excluded.last_block_reason,
                updated_at=excluded.updated_at
            """.strip(),
            (
                st.day,
                float(st.daily_realized_pnl),
                float(st.equity_peak),
                int(st.lose_streak),
                st.cooldown_until.isoformat() if st.cooldown_until else None,
                st.last_entry_symbol,
                st.last_entry_at.isoformat() if st.last_entry_at else None,
                st.last_block_reason,
                st.updated_at.isoformat(),
            ),
        )

```

## apps/trader_engine/scheduler.py

```python
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.decision_service import DecisionService, SymbolScores
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionRejected, ExecutionService
from apps.trader_engine.services.ai_service import AiService, AiSignal
from apps.trader_engine.services.market_data_service import MarketDataService
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.services.sizing_service import SizingService

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class SchedulerSnapshot:
    tick_started_at: str
    tick_finished_at: Optional[str]
    engine_state: str
    enabled_symbols: List[str]
    candidate: Optional[Dict[str, Any]]
    ai_signal: Optional[Dict[str, Any]]
    scores: Dict[str, Any]
    last_action: Optional[str] = None
    last_error: Optional[str] = None


class TraderScheduler:
    """Async scheduler loop to compute scores and trigger entry/exit decisions."""

    def __init__(
        self,
        *,
        engine: EngineService,
        risk: RiskConfigService,
        pnl: PnLService,
        binance: BinanceService,
        market_data: MarketDataService,
        decision: DecisionService,
        ai: AiService,
        sizing: SizingService,
        execution: ExecutionService,
        tick_sec: float = 1800.0,
        score_threshold: float = 0.35,
        reverse_threshold: float = 0.55,
    ) -> None:
        self._engine = engine
        self._risk = risk
        self._pnl = pnl
        self._binance = binance
        self._market_data = market_data
        self._decision = decision
        self._ai = ai
        self._sizing = sizing
        self._execution = execution
        self._tick_sec = float(tick_sec)
        self._score_threshold = float(score_threshold)
        self._reverse_threshold = float(reverse_threshold)

        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self.snapshot: Optional[SchedulerSnapshot] = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="trader_scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        # Align to tick boundary loosely; MVP: simple sleep loop.
        while not self._stop.is_set():
            started = _utcnow().isoformat()
            st = self._engine.get_state().state

            enabled = list(self._binance.enabled_symbols)
            snap = SchedulerSnapshot(
                tick_started_at=started,
                tick_finished_at=None,
                engine_state=st.value,
                enabled_symbols=enabled,
                candidate=None,
                ai_signal=None,
                scores={},
                last_action=None,
                last_error=None,
            )
            self.snapshot = snap

            try:
                await self._tick(snap)
            except Exception as e:  # noqa: BLE001
                logger.exception("scheduler_tick_failed", extra={"err": type(e).__name__})
                snap.last_error = f"{type(e).__name__}: {e}"
            finally:
                snap.tick_finished_at = _utcnow().isoformat()
                self.snapshot = snap

            await self._sleep_until_next()

    async def _sleep_until_next(self) -> None:
        # Keep it simple; ensure responsiveness to stop event.
        total = max(self._tick_sec, 1.0)
        end = time.time() + total
        while time.time() < end:
            if self._stop.is_set():
                return
            await asyncio.sleep(min(1.0, end - time.time()))

    async def _tick(self, snap: SchedulerSnapshot) -> None:
        st = self._engine.get_state().state
        enabled = list(self._binance.enabled_symbols)
        cfg = self._risk.get_config()

        # Fetch status in a thread to avoid blocking event loop (requests-based client).
        b: Mapping[str, Any] = await asyncio.to_thread(self._binance.get_status)

        # Compute equity from wallet + uPnL (best-effort).
        bal = (b.get("usdt_balance") or {}) if isinstance(b, dict) else {}
        positions = (b.get("positions") or {}) if isinstance(b, dict) else {}
        wallet = float(bal.get("wallet") or 0.0)
        available = float(bal.get("available") or 0.0)
        upnl = 0.0
        open_pos_symbol = None
        open_pos_amt = 0.0
        open_pos_upnl = 0.0
        if isinstance(positions, dict):
            for sym, row in positions.items():
                if not isinstance(row, dict):
                    continue
                amt = float(row.get("position_amt") or 0.0)
                if abs(amt) > 0:
                    open_pos_symbol = str(sym).upper()
                    open_pos_amt = amt
                    open_pos_upnl = float(row.get("unrealized_pnl") or 0.0)
                upnl += float(row.get("unrealized_pnl") or 0.0)
        equity = wallet + upnl

        # Update PnL peak tracking (also helps /status).
        await asyncio.to_thread(self._pnl.update_equity_peak, equity_usdt=equity)

        # Compute scores for each enabled symbol (multi timeframe).
        scores: List[SymbolScores] = []
        score_payload: Dict[str, Any] = {}
        for sym in enabled:
            candles_by_interval: Dict[str, Any] = {}
            # MVP: 30m tick computes 30m/1h/4h each tick.
            for itv in ("30m", "1h", "4h"):
                cs = await asyncio.to_thread(self._market_data.get_klines, symbol=sym, interval=itv, limit=220)
                candles_by_interval[itv] = cs
            s = self._decision.score_symbol(symbol=sym, candles_by_interval=candles_by_interval)
            scores.append(s)
            score_payload[sym] = {
                "long_score": s.long_score,
                "short_score": s.short_score,
                "composite": s.composite,
                "vol_tag": s.vol_tag,
                "timeframes": {k: asdict(v) for k, v in s.timeframes.items()},
            }

        # Candidate selection is controlled by DB config (single source of truth).
        th = float(cfg.score_conf_threshold)
        gap = float(cfg.score_gap_threshold)
        scored: List[tuple[float, SymbolScores]] = []
        for s in scores:
            if s.vol_tag == "VOL_SHOCK":
                continue
            strength = max(float(s.long_score), float(s.short_score))
            scored.append((strength, s))
        scored.sort(key=lambda x: x[0], reverse=True)

        candidate = None
        if scored:
            best_strength, best = scored[0]
            second_strength = scored[1][0] if len(scored) > 1 else 0.0
            if best_strength >= th and (best_strength - second_strength) >= gap:
                direction0 = "LONG" if best.long_score >= best.short_score else "SHORT"
                candidate = {
                    "symbol": best.symbol,
                    "direction": direction0,
                    "strength": float(best_strength),
                    "composite": float(best.composite),
                    "vol_tag": best.vol_tag,
                }
        snap.scores = score_payload
        snap.candidate = dict(candidate) if candidate else None

        # AI signal is "advisory" only; never has execution authority.
        st_pnl = await asyncio.to_thread(self._pnl.get_or_bootstrap)
        ai_ctx = {
            "candidate": snap.candidate,
            "scores": snap.scores,
            "engine_state": st.value,
            "position": {
                "symbol": open_pos_symbol,
                "amt": open_pos_amt,
                "upnl": open_pos_upnl,
            },
            "pnl": {
                "cooldown_until": getattr(st_pnl, "cooldown_until", None),
                "lose_streak": getattr(st_pnl, "lose_streak", 0),
            },
            "spreads": b.get("spreads") if isinstance(b, dict) else {},
        }
        ai_sig: AiSignal = self._ai.get_signal(ai_ctx)
        snap.ai_signal = ai_sig.as_dict()

        logger.info(
            "scheduler_tick",
            extra={
                "engine_state": st.value,
                "enabled_symbols": enabled,
                "candidate": snap.candidate,
                "ai_signal": snap.ai_signal,
                "open_pos_symbol": open_pos_symbol,
            },
        )

        # Trade gating by engine state:
        if st != EngineState.RUNNING:
            # In STOPPED/COOLDOWN/PANIC: do not enter/close; only refresh snapshot.
            return

        # MVP hold/close policy: if open position exists, consider close on strong reverse or vol shock.
        if open_pos_symbol:
            sym = open_pos_symbol
            srow = next((x for x in scores if x.symbol == sym), None)
            if not srow:
                return
            reverse = False
            if open_pos_amt > 0 and srow.composite <= -self._reverse_threshold:
                reverse = True
            if open_pos_amt < 0 and srow.composite >= self._reverse_threshold:
                reverse = True
            if srow.vol_tag == "VOL_SHOCK":
                reverse = True

            if reverse:
                # Min hold guard: avoid churn unless vol shock.
                min_hold = int(cfg.min_hold_minutes)
                allow_close = True
                if min_hold > 0 and srow.vol_tag != "VOL_SHOCK":
                    if st_pnl.last_entry_symbol and st_pnl.last_entry_at:
                        if st_pnl.last_entry_symbol.upper() == sym.upper():
                            held_min = ( _utcnow() - st_pnl.last_entry_at ).total_seconds() / 60.0
                            if held_min < float(min_hold):
                                allow_close = False
                                snap.last_action = f"hold_min:{sym}:{int(held_min)}/{min_hold}"

                if not allow_close:
                    return
                try:
                    out = await asyncio.to_thread(self._execution.close_position, sym)
                    snap.last_action = f"close:{sym}"
                    snap.last_error = None
                    logger.info("scheduler_close", extra={"symbol": sym, "detail": out})
                except ExecutionRejected as e:
                    snap.last_action = f"close:{sym}"
                    snap.last_error = str(e)
                except Exception as e:  # noqa: BLE001
                    snap.last_action = f"close:{sym}"
                    snap.last_error = f"{type(e).__name__}: {e}"
            else:
                # Keep winners by default; also keep losers unless reverse signal.
                _ = open_pos_upnl
            return

        # No open position: attempt entry on candidate.
        if not candidate:
            return

        # AI veto: if HOLD or low confidence, do nothing (rule-based candidate is advisory).
        if ai_sig.direction == "HOLD":
            snap.last_action = "ai_hold"
            return

        # If AI recommends a different asset/direction than rule candidate, ignore by default (rule-first policy).
        if ai_sig.target_asset != str(candidate.get("symbol", "")).upper() or ai_sig.direction != str(
            candidate.get("direction", "")
        ).upper():
            logger.info(
                "ai_conflict_ignored_rule_first",
                extra={"rule": snap.candidate, "ai": snap.ai_signal},
            )

        sym = str(candidate["symbol"]).upper()
        direction = Direction.LONG if str(candidate["direction"]).upper() == "LONG" else Direction.SHORT

        # Use 30m ATR% as stop distance proxy (fallback to 1.0%).
        srow = next((x for x in scores if x.symbol == sym), None)
        atr_pct = 1.0
        if srow and "30m" in srow.timeframes:
            atr_pct = float(srow.timeframes["30m"].atr_pct or 1.0)
        stop_distance_pct = max(float(atr_pct), 0.5)

        # Reference price from current book.
        bt = (b.get("spreads") or {}).get(sym) if isinstance(b, dict) else None
        bid = float((bt or {}).get("bid") or 0.0) if isinstance(bt, dict) else 0.0
        ask = float((bt or {}).get("ask") or 0.0) if isinstance(bt, dict) else 0.0
        price = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0
        if price <= 0.0:
            # Fall back to last close.
            last = await asyncio.to_thread(self._market_data.get_last_close, symbol=sym, interval="30m", limit=2)
            price = float(last or 0.0)
        if price <= 0.0:
            snap.last_error = "price_unavailable"
            return

        cfg = self._risk.get_config()
        size = await asyncio.to_thread(
            self._sizing.compute,
            symbol=sym,
            risk=cfg,
            equity_usdt=equity,
            available_usdt=available,
            price=price,
            stop_distance_pct=stop_distance_pct,
            existing_exposure_notional_usdt=0.0,
        )
        if size.target_notional_usdt <= 0 or size.target_qty <= 0:
            snap.last_error = f"sizing_blocked:{size.capped_by or 'unknown'}"
            return

        intent = {
            "symbol": sym,
            "direction": direction,
            # AI exec_hint is advisory; RiskService may still block/override MARKET via spread guard.
            "exec_hint": ExecHint(str(ai_sig.exec_hint).upper())
            if str(ai_sig.exec_hint).upper() in ("MARKET", "LIMIT", "SPLIT")
            else ExecHint.LIMIT,
            "notional_usdt": float(size.target_notional_usdt),
        }
        try:
            out = await asyncio.to_thread(self._execution.enter_position, intent)
            snap.last_action = f"enter:{sym}:{direction.value}"
            snap.last_error = None
            logger.info("scheduler_enter", extra={"symbol": sym, "direction": direction.value, "detail": out})
        except ExecutionRejected as e:
            snap.last_action = f"enter:{sym}:{direction.value}"
            snap.last_error = str(e)
        except Exception as e:  # noqa: BLE001
            snap.last_action = f"enter:{sym}:{direction.value}"
            snap.last_error = f"{type(e).__name__}: {e}"

```

## apps/trader_engine/api/schemas.py

```python
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from pydantic import Field

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint, RiskConfigKey, RiskPresetName


class RiskConfigSchema(BaseModel):
    per_trade_risk_pct: float
    max_exposure_pct: float
    max_notional_pct: float
    max_leverage: float
    daily_loss_limit_pct: float
    dd_limit_pct: float
    lose_streak_n: int
    cooldown_hours: float
    min_hold_minutes: int
    score_conf_threshold: float
    score_gap_threshold: float
    exec_limit_timeout_sec: float
    exec_limit_retries: int
    notify_interval_sec: int
    spread_max_pct: float
    allow_market_when_wide_spread: bool
    universe_symbols: List[str]
    enable_watchdog: bool
    watchdog_interval_sec: int
    shock_1m_pct: float
    shock_from_entry_pct: float


class EngineStateSchema(BaseModel):
    state: EngineState
    updated_at: datetime


class DisabledSymbolSchema(BaseModel):
    symbol: str
    reason: str


class BinanceStatusSchema(BaseModel):
    startup_ok: bool
    startup_error: Optional[str] = None

    enabled_symbols: List[str]
    disabled_symbols: List[DisabledSymbolSchema]

    server_time_ms: int
    time_offset_ms: int
    time_measured_at_ms: int

    private_ok: bool
    private_error: Optional[str] = None

    usdt_balance: Optional[Dict[str, float]] = None
    positions: Optional[Dict[str, Dict[str, float]]] = None
    open_orders: Optional[Dict[str, List[Dict[str, Any]]]] = None
    spreads: Dict[str, Any]


class PnLStatusSchema(BaseModel):
    day: str
    daily_realized_pnl: float
    equity_peak: float
    daily_pnl_pct: float
    drawdown_pct: float
    lose_streak: int
    cooldown_until: Optional[datetime] = None
    last_block_reason: Optional[str] = None


class CandidateSchema(BaseModel):
    symbol: str
    direction: str
    strength: float
    composite: float
    vol_tag: str


class AiSignalSchema(BaseModel):
    target_asset: str
    direction: str
    confidence: float
    exec_hint: str
    risk_tag: str
    notes: Optional[str] = None


class SchedulerSnapshotSchema(BaseModel):
    tick_started_at: str
    tick_finished_at: Optional[str] = None
    engine_state: str
    enabled_symbols: List[str]
    candidate: Optional[CandidateSchema] = None
    ai_signal: Optional[AiSignalSchema] = None
    scores: Dict[str, Any] = Field(default_factory=dict)
    last_action: Optional[str] = None
    last_error: Optional[str] = None


class StatusResponse(BaseModel):
    dry_run: bool = False
    dry_run_strict: bool = False
    config_summary: Dict[str, Any] = Field(default_factory=dict)
    last_error: Optional[str] = None
    engine_state: EngineStateSchema
    risk_config: RiskConfigSchema
    binance: Optional[BinanceStatusSchema] = None
    pnl: Optional[PnLStatusSchema] = None
    scheduler: Optional[SchedulerSnapshotSchema] = None


class SetValueRequest(BaseModel):
    key: RiskConfigKey
    value: str


class PresetRequest(BaseModel):
    name: RiskPresetName


class TradeEnterRequest(BaseModel):
    symbol: str
    direction: Direction
    exec_hint: ExecHint
    notional_usdt: Optional[float] = None
    qty: Optional[float] = None
    leverage: Optional[float] = None


class TradeCloseRequest(BaseModel):
    symbol: str


class TradeResult(BaseModel):
    symbol: str
    hint: Optional[str] = None
    orders: List[Dict[str, Any]] = Field(default_factory=list)
    detail: Optional[Dict[str, Any]] = None

```

## apps/trader_engine/api/routes.py

```python
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status

from apps.trader_engine.api.schemas import (
    EngineStateSchema,
    PnLStatusSchema,
    PresetRequest,
    RiskConfigSchema,
    SetValueRequest,
    StatusResponse,
    SchedulerSnapshotSchema,
    TradeCloseRequest,
    TradeEnterRequest,
    TradeResult,
)
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.engine_service import EngineConflict, EngineService
from apps.trader_engine.services.execution_service import (
    ExecutionRejected,
    ExecutionService,
    ExecutionValidationError,
)
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_config_service import RiskConfigService, RiskConfigValidationError

logger = logging.getLogger(__name__)

router = APIRouter()


def _engine_service(request: Request) -> EngineService:
    return request.app.state.engine_service  # type: ignore[attr-defined]


def _risk_service(request: Request) -> RiskConfigService:
    return request.app.state.risk_config_service  # type: ignore[attr-defined]


def _binance_service(request: Request) -> BinanceService:
    return request.app.state.binance_service  # type: ignore[attr-defined]


def _execution_service(request: Request) -> ExecutionService:
    return request.app.state.execution_service  # type: ignore[attr-defined]


def _pnl_service(request: Request) -> PnLService:
    return request.app.state.pnl_service  # type: ignore[attr-defined]


@router.get("/", include_in_schema=False)
def root() -> Dict[str, Any]:
    return {"ok": True, "hint": "see /docs, /health, /status"}


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/status", response_model=StatusResponse)
def get_status(
    request: Request,
    engine: EngineService = Depends(_engine_service),
    risk: RiskConfigService = Depends(_risk_service),
    binance: BinanceService = Depends(_binance_service),
    pnl: PnLService = Depends(_pnl_service),
) -> StatusResponse:
    state = engine.get_state()
    cfg = risk.get_config()
    b = binance.get_status()
    settings = getattr(request.app.state, "settings", None)

    pnl_payload = None
    try:
        st = pnl.get_or_bootstrap()
        bal = (b.get("usdt_balance") or {}) if isinstance(b, dict) else {}
        pos = (b.get("positions") or {}) if isinstance(b, dict) else {}
        wallet = float(bal.get("wallet") or 0.0)
        upnl = 0.0
        if isinstance(pos, dict):
            for row in pos.values():
                if isinstance(row, dict):
                    upnl += float(row.get("unrealized_pnl") or 0.0)
        equity = wallet + upnl

        st2 = pnl.update_equity_peak(equity_usdt=equity)
        m = pnl.compute_metrics(st=st2, equity_usdt=equity)
        pnl_payload = PnLStatusSchema(
            day=st2.day,
            daily_realized_pnl=float(st2.daily_realized_pnl),
            equity_peak=float(st2.equity_peak),
            daily_pnl_pct=float(m.daily_pnl_pct),
            drawdown_pct=float(m.drawdown_pct),
            lose_streak=int(st2.lose_streak),
            cooldown_until=st2.cooldown_until,
            last_block_reason=st2.last_block_reason,
        )
    except Exception:
        logger.exception("pnl_status_failed")

    sched = (
        SchedulerSnapshotSchema(**request.app.state.scheduler.snapshot.__dict__)  # type: ignore[attr-defined]
        if getattr(request.app.state, "scheduler", None) and getattr(request.app.state.scheduler, "snapshot", None)
        else None
    )

    last_error = None
    if sched and isinstance(sched, SchedulerSnapshotSchema) and sched.last_error:
        last_error = sched.last_error
    elif isinstance(b, dict) and (b.get("private_error") or b.get("startup_error")):
        last_error = str(b.get("private_error") or b.get("startup_error"))

    summary = {
        "universe_symbols": cfg.universe_symbols,
        "max_leverage": cfg.max_leverage,
        "daily_loss_limit_pct": cfg.daily_loss_limit_pct,
        "dd_limit_pct": cfg.dd_limit_pct,
        "lose_streak_n": cfg.lose_streak_n,
        "cooldown_hours": cfg.cooldown_hours,
        "min_hold_minutes": cfg.min_hold_minutes,
        "score_conf_threshold": cfg.score_conf_threshold,
        "score_gap_threshold": cfg.score_gap_threshold,
        "exec_limit_timeout_sec": cfg.exec_limit_timeout_sec,
        "exec_limit_retries": cfg.exec_limit_retries,
        "spread_max_pct": cfg.spread_max_pct,
        "allow_market_when_wide_spread": cfg.allow_market_when_wide_spread,
        "enable_watchdog": cfg.enable_watchdog,
        "watchdog_interval_sec": cfg.watchdog_interval_sec,
        "shock_1m_pct": cfg.shock_1m_pct,
        "shock_from_entry_pct": cfg.shock_from_entry_pct,
    }

    return StatusResponse(
        dry_run=bool(getattr(settings, "trading_dry_run", False)) if settings else False,
        dry_run_strict=bool(getattr(settings, "dry_run_strict", False)) if settings else False,
        config_summary=summary,
        last_error=last_error,
        engine_state=EngineStateSchema(state=state.state, updated_at=state.updated_at),
        risk_config=RiskConfigSchema(**cfg.model_dump()),
        binance=b,
        pnl=pnl_payload,
        scheduler=sched,
    )


@router.post("/start", response_model=EngineStateSchema)
def start(engine: EngineService = Depends(_engine_service)) -> EngineStateSchema:
    try:
        row = engine.start()
        return EngineStateSchema(state=row.state, updated_at=row.updated_at)
    except EngineConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/stop", response_model=EngineStateSchema)
def stop(engine: EngineService = Depends(_engine_service)) -> EngineStateSchema:
    try:
        row = engine.stop()
        return EngineStateSchema(state=row.state, updated_at=row.updated_at)
    except EngineConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/panic", response_model=EngineStateSchema)
def panic(
    engine: EngineService = Depends(_engine_service),
    exe: ExecutionService = Depends(_execution_service),
) -> EngineStateSchema:
    # PANIC should lock state + attempt best-effort cancel/close.
    _ = exe.panic()
    row = engine.get_state()
    return EngineStateSchema(state=row.state, updated_at=row.updated_at)


@router.get("/risk", response_model=RiskConfigSchema)
def get_risk(risk: RiskConfigService = Depends(_risk_service)) -> RiskConfigSchema:
    cfg = risk.get_config()
    return RiskConfigSchema(**cfg.model_dump())


@router.post("/set", response_model=RiskConfigSchema)
def set_value(
    req: SetValueRequest,
    risk: RiskConfigService = Depends(_risk_service),
) -> RiskConfigSchema:
    try:
        cfg = risk.set_value(req.key, req.value)
        return RiskConfigSchema(**cfg.model_dump())
    except RiskConfigValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.message) from e


@router.post("/preset", response_model=RiskConfigSchema)
def preset(
    req: PresetRequest,
    risk: RiskConfigService = Depends(_risk_service),
) -> RiskConfigSchema:
    cfg = risk.apply_preset(req.name)
    return RiskConfigSchema(**cfg.model_dump())


@router.post("/trade/enter", response_model=TradeResult)
def trade_enter(
    req: TradeEnterRequest,
    exe: ExecutionService = Depends(_execution_service),
) -> TradeResult:
    try:
        out = exe.enter_position(req.model_dump())
        return TradeResult(symbol=out.get("symbol", req.symbol), hint=out.get("hint"), orders=out.get("orders", []))
    except ExecutionValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.message) from e
    except ExecutionRejected as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/trade/close", response_model=TradeResult)
def trade_close(
    req: TradeCloseRequest,
    exe: ExecutionService = Depends(_execution_service),
) -> TradeResult:
    try:
        out = exe.close_position(req.symbol)
        return TradeResult(symbol=out.get("symbol", req.symbol), detail=out)
    except ExecutionValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.message) from e
    except ExecutionRejected as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/trade/close_all", response_model=TradeResult)
def trade_close_all(
    exe: ExecutionService = Depends(_execution_service),
) -> TradeResult:
    try:
        out = exe.close_all_positions()
        return TradeResult(symbol=str(out.get("symbol", "")), detail=out)
    except ExecutionRejected as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e

```

## apps/trader_engine/main.py

```python
from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from apps.trader_engine.api.routes import router
from apps.trader_engine.config import load_settings
from apps.trader_engine.exchange.binance_usdm import BinanceCredentials, BinanceUSDMClient
from apps.trader_engine.exchange.time_sync import TimeSync
from apps.trader_engine.logging_setup import LoggingConfig, setup_logging
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionService
from apps.trader_engine.services.market_data_service import MarketDataService
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.decision_service import DecisionService
from apps.trader_engine.services.ai_service import AiService
from apps.trader_engine.services.risk_service import RiskService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.services.sizing_service import SizingService
from apps.trader_engine.storage.db import close, connect, migrate
from apps.trader_engine.storage.repositories import EngineStateRepo, PnLStateRepo, RiskConfigRepo, StatusSnapshotRepo
from apps.trader_engine.scheduler import TraderScheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    setup_logging(LoggingConfig(level=settings.log_level, log_dir=settings.log_dir, json=settings.log_json))

    db = connect(settings.db_path)
    migrate(db)

    engine_state_repo = EngineStateRepo(db)
    risk_config_repo = RiskConfigRepo(db)
    _status_snapshot_repo = StatusSnapshotRepo(db)  # reserved for later wiring
    pnl_state_repo = PnLStateRepo(db)

    engine_service = EngineService(engine_state_repo=engine_state_repo)
    risk_config_service = RiskConfigService(risk_config_repo=risk_config_repo)
    pnl_service = PnLService(repo=pnl_state_repo)
    # Ensure defaults exist at boot.
    _ = engine_service.get_state()
    cfg = risk_config_service.get_config()
    _ = pnl_service.get_or_bootstrap()

    # Binance USDT-M Futures (議고쉶 ?꾩슜)
    binance_client = BinanceUSDMClient(
        BinanceCredentials(api_key=settings.binance_api_key, api_secret=settings.binance_api_secret),
        base_url=settings.binance_base_url,
        time_sync=TimeSync(),
        timeout_sec=settings.request_timeout_sec,
        retry_count=settings.retry_count,
        retry_backoff=settings.retry_backoff,
        recv_window_ms=settings.binance_recv_window_ms,
    )
    binance_service = BinanceService(
        client=binance_client,
        allowed_symbols=cfg.universe_symbols,
        spread_wide_pct=cfg.spread_max_pct,
    )
    binance_service.startup()

    policy = RiskService(
        risk=risk_config_service,
        engine=engine_service,
        pnl=pnl_service,
        stop_on_daily_loss=bool(settings.risk_stop_on_daily_loss),
    )

    execution_service = ExecutionService(
        client=binance_client,
        engine=engine_service,
        risk=risk_config_service,
        pnl=pnl_service,
        policy=policy,
        allowed_symbols=binance_service.enabled_symbols,
        split_parts=settings.exec_split_parts,
        dry_run=bool(settings.trading_dry_run),
        dry_run_strict=bool(settings.dry_run_strict),
    )

    market_data_service = MarketDataService(client=binance_client, cache_ttl_sec=20.0)
    decision_service = DecisionService(vol_shock_threshold_pct=settings.vol_shock_threshold_pct)
    ai_service = AiService(
        mode=settings.ai_mode,
        conf_threshold=settings.ai_conf_threshold,
        manual_risk_tag=settings.manual_risk_tag,
    )
    sizing_service = SizingService(client=binance_client)

    scheduler = TraderScheduler(
        engine=engine_service,
        risk=risk_config_service,
        pnl=pnl_service,
        binance=binance_service,
        market_data=market_data_service,
        decision=decision_service,
        ai=ai_service,
        sizing=sizing_service,
        execution=execution_service,
        tick_sec=float(settings.scheduler_tick_sec),
        score_threshold=float(settings.score_threshold),
        reverse_threshold=float(settings.reverse_threshold),
    )

    app.state.settings = settings
    app.state.db = db
    app.state.engine_service = engine_service
    app.state.risk_config_service = risk_config_service
    app.state.pnl_service = pnl_service
    app.state.risk_service = policy
    app.state.binance_service = binance_service
    app.state.execution_service = execution_service
    app.state.market_data_service = market_data_service
    app.state.decision_service = decision_service
    app.state.ai_service = ai_service
    app.state.sizing_service = sizing_service
    app.state.scheduler = scheduler
    app.state.scheduler_snapshot = None

    logger.info("api_boot", extra={"db_path": settings.db_path})
    try:
        if bool(settings.scheduler_enabled):
            scheduler.start()
            logger.info(
                "scheduler_started",
                extra={"tick_sec": settings.scheduler_tick_sec, "score_threshold": settings.score_threshold},
            )
        yield
    finally:
        try:
            try:
                await scheduler.stop()
            except Exception:
                pass
            binance_service.close()
        except Exception:
            pass
        close(db)


def create_app() -> FastAPI:
    app = FastAPI(title="auto-trader control api", version="0.2.0", lifespan=lifespan)
    app.include_router(router)
    return app


# FastAPI entrypoint for uvicorn:
#   uvicorn apps.trader_engine.main:app --reload
app = create_app()


def main() -> int:
    parser = argparse.ArgumentParser(prog="auto-trader")
    parser.add_argument("--api", action="store_true", help="run control API via uvicorn")
    args = parser.parse_args()

    if not args.api:
        # Simple non-server boot: initializes DB + defaults then exits.
        settings = load_settings()
        setup_logging(LoggingConfig(level=settings.log_level, log_dir=settings.log_dir, json=settings.log_json))
        db = connect(settings.db_path)
        migrate(db)
        try:
            EngineService(engine_state_repo=EngineStateRepo(db))
            RiskConfigService(risk_config_repo=RiskConfigRepo(db)).get_config()
            logger.info("boot_ok", extra={"db_path": settings.db_path})
            return 0
        finally:
            close(db)

    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "apps.trader_engine.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

```

## apps/discord_bot/commands.py

```python
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from apps.discord_bot.client import APIError, TraderAPIClient

logger = logging.getLogger(__name__)

RISK_KEYS: List[str] = [
    "per_trade_risk_pct",
    "max_exposure_pct",
    "max_notional_pct",
    "max_leverage",
    "daily_loss_limit_pct",
    "dd_limit_pct",
    "lose_streak_n",
    "cooldown_hours",
    "min_hold_minutes",
    "score_conf_threshold",
    "score_gap_threshold",
    "exec_limit_timeout_sec",
    "exec_limit_retries",
    "notify_interval_sec",
    "spread_max_pct",
    "allow_market_when_wide_spread",
    "universe_symbols",
    "enable_watchdog",
    "watchdog_interval_sec",
    "shock_1m_pct",
    "shock_from_entry_pct",
]

PRESETS: List[str] = ["conservative", "normal", "aggressive"]


async def _safe_defer(interaction: discord.Interaction) -> bool:
    """Acknowledge the interaction quickly.

    Discord requires an initial response within a short deadline (~3s). If our
    event loop is busy or the user retries quickly, the interaction token can
    expire and defer() raises NotFound (10062).
    """
    try:
        await interaction.response.defer(thinking=True)
        return True
    except discord.InteractionResponded:
        return True
    except discord.NotFound:
        # Can't respond via interaction token anymore. Best-effort: post to channel.
        try:
            cmd = getattr(getattr(interaction, "command", None), "name", None)
            created = getattr(interaction, "created_at", None)
            logger.warning("discord_unknown_interaction", extra={"command": cmd, "created_at": str(created)})
        except Exception:
            pass
        try:
            ch = interaction.channel
            if ch is not None and hasattr(ch, "send"):
                await ch.send("Interaction expired (Discord timeout). Try the command again.")
        except Exception:
            pass
        return False


def _truncate(s: str, *, limit: int = 1800) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _fmt_money(x: Any) -> str:
    try:
        return f"{float(x):.4f}"
    except Exception:
        return str(x)


def _fmt_status_payload(payload: Dict[str, Any]) -> str:
    engine = payload.get("engine_state") or {}
    risk = payload.get("risk_config") or {}
    summary = payload.get("config_summary") or {}
    binance = payload.get("binance") or {}
    pnl = payload.get("pnl") or {}
    sched = payload.get("scheduler") or {}
    dry_run = bool(payload.get("dry_run", False))
    dry_run_strict = bool(payload.get("dry_run_strict", False))
    last_error = payload.get("last_error", None)

    state = str(engine.get("state", "UNKNOWN"))
    panic = state.upper() == "PANIC"
    state_line = f"Engine: {state}"
    if panic:
        state_line = f":warning: {state_line} (PANIC)"

    enabled = binance.get("enabled_symbols") or []
    disabled = binance.get("disabled_symbols") or []

    bal = binance.get("usdt_balance") or {}
    wallet = _fmt_money(bal.get("wallet", "n/a"))
    available = _fmt_money(bal.get("available", "n/a"))

    positions = binance.get("positions") or {}
    pos_lines: List[str] = []
    if isinstance(positions, dict):
        for sym in sorted(positions.keys()):
            row = positions.get(sym) or {}
            amt = row.get("position_amt", 0)
            pnl = row.get("unrealized_pnl", 0)
            lev = row.get("leverage", 0)
            entry = row.get("entry_price", 0)
            pos_lines.append(
                f"- {sym}: amt={amt} entry={entry} pnl={pnl} lev={lev}"
            )

    open_orders = binance.get("open_orders") or {}
    oo_total = 0
    if isinstance(open_orders, dict):
        for v in open_orders.values():
            if isinstance(v, list):
                oo_total += len(v)

    spread_wide: List[str] = []
    spreads = binance.get("spreads") or {}
    if isinstance(spreads, dict):
        for sym, row in spreads.items():
            if isinstance(row, dict) and row.get("is_wide"):
                spread_wide.append(f"- {sym}: spread_pct={row.get('spread_pct')}")

    lines: List[str] = []
    lines.append(state_line)
    lines.append(f"DRY_RUN: {dry_run} (strict={dry_run_strict})")
    lines.append(f"Enabled symbols: {', '.join(enabled) if enabled else '(none)'}")
    if disabled:
        # Show only first few.
        d0 = []
        for d in disabled[:5]:
            if isinstance(d, dict):
                d0.append(f"{d.get('symbol')}({d.get('reason')})")
        lines.append(f"Disabled symbols: {', '.join(d0)}")
    lines.append(f"USDT balance: wallet={wallet}, available={available}")
    lines.append(f"Open orders: {oo_total}")
    if pos_lines:
        lines.append("Positions:")
        lines.extend(pos_lines[:10])
    if spread_wide:
        lines.append("Wide spreads:")
        lines.extend(spread_wide[:5])

    # Policy guard / PnL snapshot (if available).
    if isinstance(pnl, dict) and pnl:
        dd = pnl.get("drawdown_pct", "n/a")
        dp = pnl.get("daily_pnl_pct", "n/a")
        ls = pnl.get("lose_streak", "n/a")
        cd = pnl.get("cooldown_until", None)
        lbr = pnl.get("last_block_reason", None)
        lines.append(f"PnL: daily_pct={dp} dd_pct={dd} lose_streak={ls}")
        if cd:
            lines.append(f"Cooldown until: {cd}")
        if lbr:
            lines.append(f"Last block: {lbr}")

    # Scheduler snapshot (if enabled)
    if isinstance(sched, dict) and sched:
        cand = sched.get("candidate") or {}
        ai = sched.get("ai_signal") or {}
        if isinstance(cand, dict) and cand.get("symbol"):
            lines.append(
                f"Candidate: {cand.get('symbol')} {cand.get('direction')} "
                f"strength={cand.get('strength')} vol={cand.get('vol_tag')}"
            )
        if isinstance(ai, dict) and ai.get("target_asset"):
            lines.append(
                f"AI: {ai.get('target_asset')} {ai.get('direction')} "
                f"conf={ai.get('confidence')} hint={ai.get('exec_hint')} tag={ai.get('risk_tag')}"
            )
        la = sched.get("last_action")
        le = sched.get("last_error")
        if la:
            lines.append(f"Scheduler last_action: {la}")
        if le:
            lines.append(f"Scheduler last_error: {le}")
    if last_error:
        lines.append(f"Last error: {last_error}")

    # Risk is often useful, but keep it short for /status.
    if isinstance(summary, dict) and summary:
        lines.append(
            "Config: "
            f"symbols={','.join(summary.get('universe_symbols') or [])} "
            f"max_lev={summary.get('max_leverage')} "
            f"dl={summary.get('daily_loss_limit_pct')} "
            f"dd={summary.get('dd_limit_pct')} "
            f"spread={summary.get('spread_max_pct')}"
        )
    elif isinstance(risk, dict):
        lines.append(
            f"Risk: per_trade={risk.get('per_trade_risk_pct')}% "
            f"max_lev={risk.get('max_leverage')} "
            f"notify={risk.get('notify_interval_sec')}s"
        )

    return _truncate("\n".join(lines))


def _fmt_json(payload: Any) -> str:
    import json

    try:
        s = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True)
    except Exception:
        s = str(payload)
    return _truncate(s, limit=1900)


class RemoteControl(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TraderAPIClient) -> None:
        self.bot = bot
        self.api = api

    @app_commands.command(name="status", description="Show trader_engine status (summary)")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.get_status()
            assert isinstance(payload, dict)
            msg = _fmt_status_payload(payload)
            await interaction.followup.send(f"```text\n{msg}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="risk", description="Get current risk config")
    async def risk(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.get_risk()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="start", description="POST /start")
    async def start(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.start()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="stop", description="POST /stop")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.stop()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="panic", description="POST /panic")
    async def panic(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.panic()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="close", description="Close a position for a symbol (reduceOnly)")
    @app_commands.describe(symbol="Symbol, e.g. BTCUSDT")
    async def close(self, interaction: discord.Interaction, symbol: str) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.close_position(symbol.strip().upper())
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="closeall", description="Close any open position (single-asset rule)")
    async def closeall(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.close_all()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="set", description="POST /set (risk config)")
    @app_commands.describe(key="Risk config key", value="New value (string)")
    @app_commands.choices(
        key=[app_commands.Choice(name=k, value=k) for k in RISK_KEYS],
    )
    async def set_value(
        self,
        interaction: discord.Interaction,
        key: app_commands.Choice[str],
        value: str,
    ) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.set_value(key.value, value)
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="preset", description="POST /preset (risk config)")
    @app_commands.choices(name=[app_commands.Choice(name=p, value=p) for p in PRESETS])
    async def preset(
        self,
        interaction: discord.Interaction,
        name: app_commands.Choice[str],
    ) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.preset(name.value)
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)


async def setup_commands(bot: commands.Bot, api: TraderAPIClient) -> None:
    await bot.add_cog(RemoteControl(bot, api))

```

## apps/discord_bot/bot.py

```python
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Optional

import discord
from discord.ext import commands

from apps.discord_bot.client import TraderAPIClient
from apps.discord_bot.commands import setup_commands
from apps.discord_bot.config import load_settings
from apps.trader_engine.logging_setup import LoggingConfig, setup_logging

logger = logging.getLogger(__name__)


class RemoteBot(commands.Bot):
    def __init__(
        self,
        *,
        api: TraderAPIClient,
        guild_id: int = 0,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.api = api
        self.guild_id = guild_id

    async def setup_hook(self) -> None:
        await setup_commands(self, self.api)

    async def on_ready(self) -> None:
        # Sync app commands.
        try:
            if self.guild_id:
                guild = discord.Object(id=self.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("discord_commands_synced_guild", extra={"count": len(synced), "guild_id": self.guild_id})
            else:
                synced = await self.tree.sync()
                logger.info("discord_commands_synced_global", extra={"count": len(synced)})
        except Exception:
            logger.exception("discord_command_sync_failed")

        logger.info("discord_ready", extra={"user": str(self.user) if self.user else "unknown"})

    async def close(self) -> None:
        try:
            await self.api.aclose()
        except Exception:
            pass
        await super().close()


async def run() -> None:
    settings = load_settings()
    setup_logging(LoggingConfig(level=settings.log_level, log_dir=settings.log_dir, json=settings.log_json))

    if not settings.discord_bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing in .env")

    api = TraderAPIClient(
        base_url=settings.trader_api_base_url,
        timeout_sec=settings.trader_api_timeout_sec,
        retry_count=settings.trader_api_retry_count,
        retry_backoff=settings.trader_api_retry_backoff,
    )

    bot = RemoteBot(api=api, guild_id=settings.discord_guild_id)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def _shutdown(reason: str) -> None:
        # Idempotent shutdown: multiple signals can arrive.
        if stop_event.is_set():
            return
        logger.info("discord_shutdown_requested", extra={"reason": reason})
        stop_event.set()
        with contextlib.suppress(Exception):
            await bot.close()

    def _handle_signal(sig: int, _frame: object | None = None) -> None:
        # Avoid raising KeyboardInterrupt; request an orderly shutdown instead.
        try:
            signame = signal.Signals(sig).name
        except Exception:
            signame = str(sig)
        loop.call_soon_threadsafe(lambda: asyncio.create_task(_shutdown(signame)))

    # Windows doesn't support asyncio's add_signal_handler() reliably.
    for s in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if s is None:
            continue
        try:
            signal.signal(s, _handle_signal)
        except Exception:
            # Best-effort: fall back to default handler if we can't set it.
            pass

    bot_task = asyncio.create_task(bot.start(settings.discord_bot_token))
    await stop_event.wait()

    if not bot_task.done():
        bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot_task


def main() -> int:
    try:
        asyncio.run(run())
        return 0
    except KeyboardInterrupt:
        # Should be rare now (we install a SIGINT handler), but keep it quiet.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

```

## .env.example

```dotenv
# Trader Engine (infra/runtime)
ENV=dev
DB_PATH=./data/auto_trader.sqlite3
LOG_LEVEL=INFO
LOG_DIR=./logs
LOG_JSON=false
API_HOST=127.0.0.1
API_PORT=8000

# Safety switches (RECOMMENDED for real accounts)
# If true: block any NEW entry orders (enter/scale/rebalance). /close and /panic are still allowed by default.
TRADING_DRY_RUN=true
# If true AND TRADING_DRY_RUN=true: also block /close and /panic (maximum safety).
DRY_RUN_STRICT=false

# Optional Discord webhook for alerts (empty => disabled)
DISCORD_WEBHOOK_URL=

# Binance USDT-M Futures
# IMPORTANT: Do NOT enable withdrawal permissions on this API key.
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_BASE_URL=https://fapi.binance.com
REQUEST_TIMEOUT_SEC=8
RETRY_COUNT=3
RETRY_BACKOFF=0.25
BINANCE_RECV_WINDOW_MS=5000

# NOTE: The trading universe and most policy parameters are stored in SQLite (risk_config singleton row).
# This env var is kept only for backward compatibility / initial bootstrap.
ALLOWED_SYMBOLS=BTCUSDT,ETHUSDT,XAUTUSDT

# Execution (MVP)
EXEC_SPLIT_PARTS=3

# Policy behavior (still env; optional)
RISK_STOP_ON_DAILY_LOSS=false

# Scheduler (STEP6)
SCHEDULER_ENABLED=false
SCHEDULER_TICK_SEC=1800
REVERSE_THRESHOLD=0.55
VOL_SHOCK_THRESHOLD_PCT=2.0

# AI signal (STEP7) - signal only, never executes orders
AI_MODE=stub
AI_CONF_THRESHOLD=0.65
MANUAL_RISK_TAG=

# Discord Bot (slash command remote)
DISCORD_BOT_TOKEN=
TRADER_API_BASE_URL=http://127.0.0.1:8000
# Development: sync commands to a single guild for fast iteration
DISCORD_GUILD_ID=


```

## README.md

```markdown
# auto-trader

Binance USDT-M Futures auto-trader (Trader Engine + Discord Bot).

Key components:
- `apps/trader_engine`: FastAPI control plane + scheduler + risk/execution (USDT-M futures only)
- `apps/discord_bot`: Discord slash-command remote for `/status`, `/start`, `/stop`, `/panic`, `/close`

## Safety (Read This First)

- USDT-M Futures only: spot/coin-m/withdrawal features are not implemented.
- Do not enable withdrawal permissions on the Binance API key used here. This project does not require withdrawals.
- `TRADING_DRY_RUN=true` blocks NEW entries (enter/scale/rebalance). It is enabled by default in `.env.example`.
- `/close` and `/panic` are allowed in dry-run by default for operational safety.
  - Set `DRY_RUN_STRICT=true` to block `/close` and `/panic` too (maximum safety).
- Default boot state is `STOPPED`. No orders are allowed until you call `/start`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
pip install -e ".[dev]"
copy .env.example .env
```

## Run (Trader Engine)

```powershell
.\.venv\Scripts\python.exe -m apps.trader_engine.main --api
```

### Control API quick check

```powershell
curl http://127.0.0.1:8000/status
curl -X POST http://127.0.0.1:8000/start
```

## Config (Single Source Of Truth)

Most trading/policy parameters live in SQLite as a singleton row in `risk_config` (id=1). `.env` is primarily for
infra/runtime (DB path, logging, API keys, dry-run, scheduler enable).

## Run (Discord Bot)

```powershell
.\.venv\Scripts\python.exe -m apps.discord_bot.bot
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```


```

## tests/test_repos.py

```python
from __future__ import annotations

from datetime import datetime, timezone

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import EngineStateRow, PnLState, RiskConfig
from apps.trader_engine.storage.db import connect, migrate
from apps.trader_engine.storage.repositories import EngineStateRepo, PnLStateRepo, RiskConfigRepo


def test_risk_config_upsert_and_get(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    db = connect(str(db_path))
    migrate(db)

    repo = RiskConfigRepo(db)
    assert repo.get() is None

    cfg = RiskConfig(
        per_trade_risk_pct=1.0,
        max_exposure_pct=20.0,
        max_notional_pct=50.0,
        max_leverage=3,
        daily_loss_limit_pct=-0.05,
        dd_limit_pct=-0.10,
        lose_streak_n=3,
        cooldown_hours=2.0,
        notify_interval_sec=60,
    )
    repo.upsert(cfg)

    got = repo.get()
    assert got is not None
    assert got.per_trade_risk_pct == 1.0
    assert got.notify_interval_sec == 60


def test_engine_state_upsert_and_get(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    db = connect(str(db_path))
    migrate(db)

    repo = EngineStateRepo(db)
    initial = repo.get()
    assert initial.state == EngineState.STOPPED

    row = EngineStateRow(state=EngineState.RUNNING, updated_at=datetime.now(tz=timezone.utc))
    repo.upsert(row)

    got = repo.get()
    assert got.state == EngineState.RUNNING


def test_pnl_state_upsert_and_get(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    db = connect(str(db_path))
    migrate(db)

    repo = PnLStateRepo(db)
    assert repo.get() is None

    row = PnLState(
        day="2026-02-09",
        daily_realized_pnl=-12.5,
        equity_peak=1000.0,
        lose_streak=2,
        cooldown_until=None,
        last_block_reason="cooldown_active",
        updated_at=datetime.now(tz=timezone.utc),
    )
    repo.upsert(row)
    got = repo.get()
    assert got is not None
    assert got.day == "2026-02-09"
    assert got.lose_streak == 2
    assert got.last_block_reason == "cooldown_active"

```

## tests/test_risk_service.py

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.risk_service import Decision, RiskService


@dataclass
class _FakeEngine:
    state: EngineState = EngineState.RUNNING

    def get_state(self):
        class _Row:
            def __init__(self, s: EngineState) -> None:
                self.state = s

        return _Row(self.state)

    def set_state(self, s: EngineState):
        self.state = s
        return self.get_state()

    def panic(self):
        self.state = EngineState.PANIC
        return self.get_state()


class _FakeRiskCfg:
    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> RiskConfig:
        return self._cfg


class _FakePnL:
    def __init__(self) -> None:
        self.cooldown_until: Optional[datetime] = None

    def set_cooldown_until(self, *, cooldown_until: Optional[datetime]):
        self.cooldown_until = cooldown_until
        return None


def _mk(cfg: RiskConfig) -> tuple[RiskService, _FakeEngine, _FakePnL]:
    eng = _FakeEngine()
    pnl = _FakePnL()
    svc = RiskService(
        risk=_FakeRiskCfg(cfg),  # type: ignore[arg-type]
        engine=eng,  # type: ignore[arg-type]
        pnl=pnl,  # type: ignore[arg-type]
        stop_on_daily_loss=False,
    )
    return svc, eng, pnl


def test_cooldown_active_blocks_and_sets_engine_cooldown():
    cfg = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=20,
        max_notional_pct=50,
        max_leverage=5,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=120,
    )
    svc, eng, _pnl = _mk(cfg)
    until = datetime.now(tz=timezone.utc) + timedelta(minutes=10)
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "MARKET"},
        {"equity_usdt": 1000.0, "notional_usdt_est": 10.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": until, "daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind == "BLOCK"
    assert dec.reason == "cooldown_active"
    assert eng.state == EngineState.COOLDOWN


def test_stuck_cooldown_recovers_to_running_when_no_cooldown_marker():
    cfg = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=20,
        max_notional_pct=50,
        max_leverage=5,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=120,
    )
    svc, eng, _pnl = _mk(cfg)
    eng.state = EngineState.COOLDOWN
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": None, "daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind in ("ALLOW", "BLOCK", "PANIC")
    assert eng.state == EngineState.RUNNING


def test_drawdown_limit_panics_engine():
    cfg = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=20,
        max_notional_pct=50,
        max_leverage=5,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.10,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=120,
    )
    svc, eng, _pnl = _mk(cfg)
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "MARKET", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": None, "daily_pnl_pct": 0.0, "drawdown_pct": -50.0, "lose_streak": 0},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind == "PANIC"
    assert eng.state == EngineState.PANIC


def test_spread_guard_blocks_market_only_by_default():
    cfg = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=100,
        max_notional_pct=100,
        max_leverage=50,
        daily_loss_limit_pct=-1.0,
        dd_limit_pct=-1.0,
        lose_streak_n=10,
        cooldown_hours=1,
        notify_interval_sec=120,
        spread_max_pct=0.005,  # 0.5%
        allow_market_when_wide_spread=False,
    )
    svc, _eng, _pnl = _mk(cfg)
    wide = {"bid": 100.0, "ask": 101.0}  # ~0.995% spread
    dec1 = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "MARKET", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": None, "daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0},
        wide,
    )
    assert dec1.kind == "BLOCK"
    dec2 = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": None, "daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0},
        wide,
    )
    assert dec2.kind == "ALLOW"


def test_constraints_block_on_leverage_above_cap():
    cfg = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=100,
        max_notional_pct=100,
        max_leverage=3,
        daily_loss_limit_pct=-1.0,
        dd_limit_pct=-1.0,
        lose_streak_n=10,
        cooldown_hours=1,
        notify_interval_sec=120,
    )
    svc, _eng, _pnl = _mk(cfg)
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 100.0, "leverage": 10},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": None, "daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind == "BLOCK"
    assert dec.reason == "leverage_above_max_leverage"

```

## tests/test_sizing_service.py

```python
from __future__ import annotations

from dataclasses import dataclass

from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.sizing_service import SizingService


@dataclass
class _FakeClient:
    step_size: float = 0.001
    min_qty: float = 0.0
    min_notional: float | None = None

    def get_symbol_filters(self, *, symbol: str):
        return {
            "symbol": symbol,
            "step_size": self.step_size,
            "min_qty": self.min_qty,
            "min_notional": self.min_notional,
        }


def test_sizing_computes_notional_from_risk_and_stop_distance():
    risk = RiskConfig(
        per_trade_risk_pct=1.0,
        max_exposure_pct=100.0,
        max_notional_pct=100.0,
        max_leverage=10.0,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6.0,
        notify_interval_sec=120,
    )
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    # equity 1000, risk 1% => $10 risk budget. stop distance 2% => $500 notional.
    res = svc.compute(
        symbol="BTCUSDT",
        risk=risk,
        equity_usdt=1000.0,
        available_usdt=1000.0,
        price=100.0,
        stop_distance_pct=2.0,
    )
    assert abs(res.target_notional_usdt - 500.0) < 1e-6
    assert res.target_qty > 0


def test_sizing_respects_max_notional_pct_cap():
    risk = RiskConfig(
        per_trade_risk_pct=5.0,
        max_exposure_pct=100.0,
        max_notional_pct=10.0,
        max_leverage=10.0,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6.0,
        notify_interval_sec=120,
    )
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    res = svc.compute(
        symbol="BTCUSDT",
        risk=risk,
        equity_usdt=1000.0,
        available_usdt=1000.0,
        price=100.0,
        stop_distance_pct=1.0,
    )
    assert res.target_notional_usdt <= 100.0 + 1e-6
    assert res.capped_by in ("max_notional_pct", None)

```


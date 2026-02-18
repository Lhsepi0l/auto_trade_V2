from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

from pydantic import ValidationError

from apps.trader_engine.domain.enums import CapitalMode, RiskConfigKey, RiskPresetName
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.storage.repositories import RiskConfigRepo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskConfigValidationError(Exception):
    message: str


_PRESETS: Dict[RiskPresetName, RiskConfig] = {
    RiskPresetName.conservative: RiskConfig(
        per_trade_risk_pct=0.5,
        max_exposure_pct=0.10,
        max_notional_pct=20,
        max_leverage=3,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.05,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=1800,
    ),
    RiskPresetName.normal: RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=0.20,
        max_notional_pct=50,
        max_leverage=5,
        # Default policy baseline (percent units):
        # - daily_loss_limit: -2% (block new entries; optional STOP is handled by RiskService setting)
        # - dd_limit: -15% (PANIC)
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=1800,
    ),
    RiskPresetName.aggressive: RiskConfig(
        per_trade_risk_pct=2,
        max_exposure_pct=0.40,
        max_notional_pct=80,
        max_leverage=10,
        daily_loss_limit_pct=-0.10,
        dd_limit_pct=-0.20,
        lose_streak_n=2,
        cooldown_hours=1,
        notify_interval_sec=1800,
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
            except Exception as e:  # noqa: BLE001
                logger.warning("risk_config_forward_fill_failed", extra={"err": type(e).__name__}, exc_info=True)
        return cfg

    def apply_preset(self, name: RiskPresetName) -> RiskConfig:
        cfg = _PRESETS[name]
        self._risk_config_repo.upsert(cfg)
        logger.info("risk_config_preset_applied", extra={"preset": name.value})
        return cfg

    def set_value(self, key: RiskConfigKey, value: str) -> RiskConfig:
        cfg = self.get_config()
        updated = cfg.model_copy()
        if key == RiskConfigKey.symbol_leverage_map:
            raise RiskConfigValidationError("symbol_leverage_map_is_set_via_symbol_leverage_endpoint")

        try:
            parsed: Any = self._parse_value(key, value)
        except ValueError as e:
            raise RiskConfigValidationError(str(e)) from e

        # Set and validate via pydantic (domain model constraints).
        payload = updated.model_dump()
        payload[key.value] = parsed
        try:
            validated = RiskConfig(**payload)
        except ValidationError as e:
            details = []
            for item in e.errors():
                loc = ".".join([str(x) for x in item.get("loc", ())]) or "config"
                msg = str(item.get("msg", "invalid_value"))
                details.append(f"{loc}: {msg}")
            raise RiskConfigValidationError("; ".join(details)) from e

        self._risk_config_repo.upsert(validated)
        logger.info("risk_config_value_set", extra={"key": key.value})
        return validated

    def get_leverage_for_symbol(self, *, symbol: str) -> float:
        cfg = self.get_config()
        sym = str(symbol or "").strip().upper()
        if not sym:
            return float(cfg.max_leverage)

        cfg_map = cfg.symbol_leverage_map or {}
        if sym in cfg_map:
            try:
                lev = float(cfg_map[sym])
            except Exception:
                return float(cfg.max_leverage)
            if 1.0 <= lev <= float(cfg.max_leverage):
                return lev
            if lev > float(cfg.max_leverage):
                return float(cfg.max_leverage)
        return float(cfg.max_leverage)

    def set_symbol_leverage(self, *, symbol: str, leverage: float) -> RiskConfig:
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise RiskConfigValidationError("symbol_is_required")
        cfg = self.get_config()

        try:
            lev = float(leverage)
        except Exception as e:
            raise RiskConfigValidationError("symbol_leverage_must_be_float") from e

        if lev < 0.0 or lev > 50.0:
            raise RiskConfigValidationError("symbol_leverage_must_be_between_0_and_50")

        if lev > float(cfg.max_leverage):
            raise RiskConfigValidationError("symbol_leverage_exceeds_max_leverage")

        payload = cfg.model_dump()
        m = dict(payload.get("symbol_leverage_map") or {})
        if lev <= 0:
            m.pop(sym, None)
        else:
            m[sym] = lev

        try:
            validated = RiskConfig(**payload | {"symbol_leverage_map": m})
        except Exception as e:
            raise RiskConfigValidationError(str(e)) from e

        self._risk_config_repo.upsert(validated)
        logger.info("risk_config_symbol_leverage_set", extra={"symbol": sym, "leverage": lev})
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
            RiskConfigKey.atr_mult_mean_window,
            RiskConfigKey.trail_grace_minutes,
        }:
            try:
                return int(value)
            except Exception as e:
                raise ValueError(f"invalid_int_for_{key.value}") from e

        if key in {RiskConfigKey.allow_market_when_wide_spread, RiskConfigKey.enable_watchdog, RiskConfigKey.trailing_enabled}:
            v = value.lower()
            if v in ("1", "true", "t", "yes", "y", "on"):
                return True
            if v in ("0", "false", "f", "no", "n", "off"):
                return False
            raise ValueError(f"invalid_bool_for_{key.value}")

        if key in {RiskConfigKey.max_exposure_pct, RiskConfigKey.max_position_notional_usdt}:
            if value.lower() in {"", "none", "null"}:
                return None
        if key == RiskConfigKey.max_exposure_pct:
            txt = value.strip()
            if txt.endswith("%"):
                raw = txt[:-1].strip()
                try:
                    return float(raw) / 100.0
                except Exception as e:
                    raise ValueError("invalid_percent_for_max_exposure_pct") from e
            try:
                ratio = float(txt)
            except Exception as e:
                raise ValueError("invalid_float_for_max_exposure_pct") from e
            # Prevent ambiguous percent-like input ("20"). Require either ratio (0..1)
            # or an explicit percent suffix ("20%").
            if ratio > 1.0:
                raise ValueError("invalid_ratio_or_percent_suffix_for_max_exposure_pct")
            return ratio

        if key == RiskConfigKey.capital_mode:
            v = value.strip().upper()
            if v not in {
                CapitalMode.PCT_AVAILABLE.value,
                CapitalMode.FIXED_USDT.value,
                CapitalMode.MARGIN_BUDGET_USDT.value,
            }:
                raise ValueError("invalid_capital_mode")
            return v

        if key == RiskConfigKey.universe_symbols:
            # CSV: BTCUSDT,ETHUSDT,XAUUSDT
            parts = [p.strip().upper() for p in value.split(",") if p.strip()]
            if not parts:
                raise ValueError("universe_symbols_empty")
            return parts

        if key == RiskConfigKey.exec_mode_default:
            v = value.strip().upper()
            if v not in {"LIMIT", "MARKET", "SPLIT"}:
                raise ValueError("invalid_exec_mode_default")
            return v

        if key == RiskConfigKey.trailing_mode:
            v = value.strip().upper()
            if v not in {"PCT", "ATR"}:
                raise ValueError("invalid_trailing_mode")
            return v
        if key == RiskConfigKey.atr_trail_timeframe:
            v = value.strip().lower()
            if v not in {"15m", "1h", "4h"}:
                raise ValueError("invalid_atr_trail_timeframe")
            return v

        if key == RiskConfigKey.max_notional_pct:
            txt = value.strip().lower().replace(" ", "")
            # Friendly inputs:
            # - 100 / 1000 / 2000 (percent of equity notionals)
            # - 10x / x10 / 10배 (equity multiple => *100)
            if txt.endswith("%"):
                txt = txt[:-1]
            if txt.endswith("x"):
                try:
                    return float(txt[:-1]) * 100.0
                except Exception as e:
                    raise ValueError("invalid_max_notional_pct_multiple") from e
            if txt.startswith("x"):
                try:
                    return float(txt[1:]) * 100.0
                except Exception as e:
                    raise ValueError("invalid_max_notional_pct_multiple") from e
            if txt.endswith("배"):
                try:
                    return float(txt[:-1]) * 100.0
                except Exception as e:
                    raise ValueError("invalid_max_notional_pct_multiple") from e
            try:
                return float(txt)
            except Exception as e:
                raise ValueError("invalid_float_for_max_notional_pct") from e

        # Everything else: float
        try:
            return float(value)
        except Exception as e:
            raise ValueError(f"invalid_float_for_{key.value}") from e

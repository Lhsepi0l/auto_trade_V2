from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ModeName = Literal["shadow", "live"]
EnvName = Literal["testnet", "prod"]


class SchedulerConfig(BaseModel):
    tick_seconds: int = Field(default=30, ge=1, le=3600)
    max_drift_seconds: int = Field(default=5, ge=0, le=120)


class ExchangeConfig(BaseModel):
    venue: Literal["binance-usdm"] = "binance-usdm"
    recv_window_ms: int = Field(default=5000, ge=1000, le=60000)
    default_symbol: str = "BTCUSDT"
    request_rate_limit_per_sec: float = Field(default=5.0, ge=0.1, le=50.0)
    backoff_base_seconds: float = Field(default=0.5, ge=0.05, le=10.0)
    backoff_cap_seconds: float = Field(default=10.0, ge=0.1, le=120.0)
    user_stream_keepalive_seconds: int = Field(default=30 * 60, ge=60, le=59 * 60)
    user_stream_reconnect_min_seconds: float = Field(default=1.0, ge=0.1, le=30.0)
    user_stream_reconnect_max_seconds: float = Field(default=30.0, ge=0.5, le=300.0)
    user_stream_connection_ttl_seconds: int = Field(default=23 * 60 * 60, ge=60, le=24 * 60 * 60)
    user_stream_reorder_window_ms: int = Field(default=300, ge=0, le=5000)
    market_intervals: list[Literal["15m", "1h", "4h"]] = Field(default_factory=lambda: ["15m", "1h", "4h"])


class EngineConfig(BaseModel):
    allow_reentry: bool = False
    max_open_positions: int = Field(default=1, ge=1, le=100)


class RiskConfig(BaseModel):
    max_leverage: float = Field(default=5.0, ge=1.0, le=125.0)
    max_exposure_pct: float = Field(default=0.20, ge=0.0, le=1.0)
    daily_loss_limit_pct: float = Field(default=-0.02, ge=-1.0, le=0.0)
    dd_limit_pct: float = Field(default=-0.15, ge=-1.0, le=0.0)


class TPSLConfig(BaseModel):
    take_profit_pct: float = Field(default=0.02, ge=0.0, le=1.0)
    stop_loss_pct: float = Field(default=0.01, ge=0.0, le=1.0)
    trailing_enabled: bool = False


class StrategyEntry(BaseModel):
    name: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class StorageConfig(BaseModel):
    sqlite_path: str = "data/v2_runtime.sqlite3"
    journal_table: str = "runtime_journal"


class OpsConfig(BaseModel):
    pause_on_start: bool = False
    flatten_on_kill: bool = True


class NotifyConfig(BaseModel):
    enabled: bool = False
    provider: Literal["none", "discord"] = "none"


class BehaviorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_name: str = "auto-trader-v2"
    timezone: str = "UTC"
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    tpsl: TPSLConfig = Field(default_factory=TPSLConfig)
    strategies: list[StrategyEntry] = Field(default_factory=list)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    ops: OpsConfig = Field(default_factory=OpsConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inherits: str | None = None
    app_name: str | None = None
    timezone: str | None = None
    scheduler: SchedulerConfig | None = None
    exchange: ExchangeConfig | None = None
    engine: EngineConfig | None = None
    risk: RiskConfig | None = None
    tpsl: TPSLConfig | None = None
    strategies: list[StrategyEntry] | None = None
    storage: StorageConfig | None = None
    ops: OpsConfig | None = None
    notify: NotifyConfig | None = None


class RootConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=2, ge=1)
    base: BehaviorConfig
    profiles: dict[str, ProfileConfig]

    @model_validator(mode="after")
    def ensure_required_profiles(self) -> "RootConfig":
        required = {"conservative", "normal", "aggressive"}
        missing = required.difference(set(self.profiles.keys()))
        if missing:
            joined = ", ".join(sorted(missing))
            raise ValueError(f"missing required profiles: {joined}")
        return self


class SecretConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binance_api_key: str | None = None
    binance_api_secret: str | None = None
    notify_webhook_url: str | None = None

    @staticmethod
    def _none_if_blank(value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    @classmethod
    def from_env(cls, env_map: dict[str, str] | None = None) -> "SecretConfig":
        source = env_map if env_map is not None else dict(os.environ)
        return cls(
            binance_api_key=cls._none_if_blank(source.get("BINANCE_API_KEY")),
            binance_api_secret=cls._none_if_blank(source.get("BINANCE_API_SECRET")),
            notify_webhook_url=cls._none_if_blank(source.get("DISCORD_WEBHOOK_URL")),
        )


class EffectiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str
    mode: ModeName
    env: EnvName
    behavior: BehaviorConfig
    secrets: SecretConfig


def _load_env_file(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    target = Path(path)
    if not target.exists() or not target.is_file():
        return {}

    out: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        k = key.strip()
        if not k:
            continue
        v = value.strip()
        if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
            v = v[1:-1]
        out[k] = v
    return out


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
            continue
        out[key] = value
    return out


def _resolve_profile_overrides(root: RootConfig, profile: str, seen: set[str] | None = None) -> dict[str, Any]:
    if profile not in root.profiles:
        raise ValueError(f"unknown profile: {profile}")

    chain = seen if seen is not None else set()
    if profile in chain:
        trace = " -> ".join(list(chain) + [profile])
        raise ValueError(f"profile inheritance cycle detected: {trace}")

    chain.add(profile)
    node = root.profiles[profile]

    merged: dict[str, Any] = {}
    if node.inherits:
        merged = _resolve_profile_overrides(root, node.inherits, chain)

    current = node.model_dump(exclude_none=True)
    current.pop("inherits", None)
    merged = _deep_merge(merged, current)

    chain.remove(profile)
    return merged


def _config_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    return Path(__file__).resolve().parent / "config.yaml"


def load_root_config(path: str | Path | None = None) -> RootConfig:
    target = _config_path(path)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config yaml root must be a mapping")
    return RootConfig.model_validate(data)


def load_effective_config(
    *,
    profile: str = "normal",
    mode: ModeName = "shadow",
    env: EnvName = "testnet",
    config_path: str | Path | None = None,
    env_map: dict[str, str] | None = None,
    env_file_path: str | Path | None = ".env",
) -> EffectiveConfig:
    root = load_root_config(config_path)

    profile_overrides = _resolve_profile_overrides(root, profile)
    merged = _deep_merge(root.base.model_dump(), profile_overrides)

    try:
        behavior = BehaviorConfig.model_validate(merged)
    except ValidationError as exc:
        raise ValueError(f"effective config validation failed: {exc}") from exc

    file_env = _load_env_file(env_file_path)
    if env_map is None:
        merged_env = dict(os.environ)
        merged_env.update(file_env)
    else:
        merged_env: dict[str, str] = {}
        if env_file_path is not None and str(env_file_path) != ".env":
            merged_env.update(file_env)
        merged_env.update(env_map)

    secrets = SecretConfig.from_env(merged_env)
    if mode == "live" and (not secrets.binance_api_key or not secrets.binance_api_secret):
        raise ValueError("live mode requires BINANCE_API_KEY and BINANCE_API_SECRET in environment")

    return EffectiveConfig(profile=profile, mode=mode, env=env, behavior=behavior, secrets=secrets)


def render_effective_config(cfg: EffectiveConfig) -> str:
    payload = {
        "profile": cfg.profile,
        "mode": cfg.mode,
        "env": cfg.env,
        "behavior": cfg.behavior.model_dump(),
    }
    return json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True)

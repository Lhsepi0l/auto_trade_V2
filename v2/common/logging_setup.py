from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Optional


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    log_dir: str = "./logs"
    json: bool = False
    component: str = "engine"
    file_max_bytes: int = 10 * 1024 * 1024
    file_backup_count: int = 14


_REDACTED = "***REDACTED***"
_SENSITIVE_KEY_PATTERNS = (
    "api_key",
    "api_secret",
    "secret",
    "token",
    "password",
    "authorization",
    "signature",
    "webhook",
)
_DISCORD_WEBHOOK_RE = re.compile(r"https://discord\.com/api/webhooks/\S+", re.IGNORECASE)
_ASSIGN_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|secret|token|password|authorization)\b\s*([=:])\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


def _is_sensitive_key(key: str) -> bool:
    s = str(key or "").lower()
    return any(p in s for p in _SENSITIVE_KEY_PATTERNS)


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        s = _DISCORD_WEBHOOK_RE.sub(_REDACTED, value)
        s = _ASSIGN_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", s)
        s = _BEARER_RE.sub(f"Bearer {_REDACTED}", s)
        s = _AWS_KEY_RE.sub(_REDACTED, s)
        return s
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, *, default_component: str = "engine") -> None:
        super().__init__()
        self._default_component = default_component

    def format(self, record: logging.LogRecord) -> str:
        event = record.__dict__.get("event")
        if event is None:
            event = str(record.msg)

        payload: MutableMapping[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "component": str(record.__dict__.get("component") or self._default_component),
            "event": str(event),
            "symbol": record.__dict__.get("symbol"),
            "side": record.__dict__.get("side"),
            "action": record.__dict__.get("action"),
            "reason": record.__dict__.get("reason"),
            "run_id": record.__dict__.get("run_id"),
            "cycle_id": record.__dict__.get("cycle_id"),
            "intent_id": record.__dict__.get("intent_id"),
            "client_order_id": record.__dict__.get("client_order_id"),
            "logger": record.name,
            "msg": _redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc_info"] = _redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack_info"] = _redact(record.stack_info)
        for k, v in record.__dict__.items():
            if k.startswith("_"):
                continue
            if k in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
            }:
                continue
            try:
                json.dumps(v)
                payload[k] = _redact(v, key=k)
            except Exception:
                payload[k] = _redact(repr(v), key=k)
        return json.dumps(payload, ensure_ascii=True)


class RedactingTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        safe = logging.makeLogRecord(dict(record.__dict__))
        safe.msg = _redact(record.getMessage())
        safe.args = ()
        if safe.exc_info:
            safe.exc_text = str(_redact(self.formatException(safe.exc_info)))
        if safe.stack_info:
            safe.stack_info = str(_redact(safe.stack_info))
        return super().format(safe)


def setup_logging(cfg: LoggingConfig) -> None:
    os.makedirs(cfg.log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.level.upper(), logging.INFO))

    for h in list(root.handlers):
        root.removeHandler(h)

    json_formatter = JsonFormatter(default_component=cfg.component)
    if cfg.json:
        console_formatter: logging.Formatter = json_formatter
    else:
        console_formatter = RedactingTextFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(console_formatter)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=os.path.join(cfg.log_dir, f"{cfg.component}.log"),
        maxBytes=max(int(cfg.file_max_bytes), 1024 * 1024),
        backupCount=max(int(cfg.file_backup_count), 7),
        encoding="utf-8",
    )
    file_handler.setFormatter(json_formatter)
    root.addHandler(file_handler)


def get_logger(
    name: Optional[str] = None,
    *,
    extra: Optional[Mapping[str, Any]] = None,
) -> logging.Logger | logging.LoggerAdapter[logging.Logger]:
    logger = logging.getLogger(name if name else __name__)
    if extra:
        return logging.LoggerAdapter(logger, extra)  # type: ignore[return-value]
    return logger

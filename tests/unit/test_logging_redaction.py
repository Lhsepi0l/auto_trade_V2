from __future__ import annotations

import io
import json
import logging

import pytest

from apps.trader_engine.logging_setup import JsonFormatter, RedactingTextFormatter


@pytest.mark.unit
def test_json_formatter_redacts_exception_trace_secrets() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(default_component="test"))
    logger = logging.getLogger("tests.logging.json_redaction")
    logger.handlers = [handler]
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    try:
        raise RuntimeError("boom API_SECRET=trace-secret")
    except RuntimeError:
        logger.exception("failed API_SECRET=msg-secret")
    payload = json.loads(stream.getvalue().strip().splitlines()[-1])
    msg = str(payload.get("msg") or "")
    exc_info = str(payload.get("exc_info") or "")
    assert "trace-secret" not in exc_info
    assert "msg-secret" not in msg
    assert "***REDACTED***" in (msg + exc_info)


@pytest.mark.unit
def test_plain_formatter_redacts_exception_trace_secrets() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingTextFormatter(fmt="%(levelname)s %(message)s"))
    logger = logging.getLogger("tests.logging.text_redaction")
    logger.handlers = [handler]
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    try:
        raise RuntimeError("boom API_SECRET=trace-secret")
    except RuntimeError:
        logger.exception("failed API_SECRET=msg-secret")
    out = stream.getvalue()
    assert "trace-secret" not in out
    assert "msg-secret" not in out
    assert "***REDACTED***" in out

from __future__ import annotations

from importlib import import_module
from typing import Any


def _run_attr(name: str) -> Any:
    return getattr(import_module("v2.run"), name)


def get_build_strategy_selector() -> Any:
    return _run_attr("_build_strategy_selector")


def get_build_runtime() -> Any:
    return _run_attr("_build_runtime")


def get_local_backtest_symbol_replay_worker() -> Any:
    return _run_attr("_run_local_backtest_symbol_replay_worker")

from __future__ import annotations

import argparse

from v2.config.loader import EffectiveConfig


def validate_runtime_entry_path(args: argparse.Namespace, cfg: EffectiveConfig) -> str | None:
    if not (str(cfg.mode) == "live" and str(cfg.env) == "prod"):
        return None

    if bool(args.ops_http):
        return (
            "live/prod에서는 --ops-http를 사용할 수 없습니다. "
            "--control-http 경로를 사용하세요."
        )

    is_runtime_boot = not any(
        [
            bool(args.control_http),
            bool(args.runtime_preflight),
            args.ops_action != "none",
            args.local_backtest,
            args.replay is not None,
        ]
    )
    if is_runtime_boot:
        return (
            "live/prod direct boot는 차단됩니다. "
            "--control-http 기반 경로를 사용하세요."
        )

    return None

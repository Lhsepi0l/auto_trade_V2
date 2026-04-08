from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _to_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return bool(default)


def normalize_management_policy(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized == "tp1_runner":
        return normalized
    return None


@dataclass(frozen=True)
class PositionManagementSpec:
    management_policy: str | None = None
    time_stop_bars: int = 0
    stop_exit_cooldown_bars: int = 0
    profit_exit_cooldown_bars: int = 0
    progress_check_bars: int = 0
    progress_min_mfe_r: float = 0.0
    progress_extend_trigger_r: float = 0.0
    progress_extend_bars: int = 0
    loss_streak_trigger: int = 0
    loss_streak_cooldown_bars: int = 0
    reward_risk_reference_r: float = 0.0
    tp_partial_ratio: float = 0.0
    tp_partial_at_r: float = 0.0
    move_stop_to_be_at_r: float = 0.0
    allow_reverse_exit: bool = True
    reduce_only_exit: bool = True
    entry_quality_score_v2: float = 0.0
    quality_exit_applied: bool = False
    entry_regime_strength: float = 0.0
    entry_bias_strength: float = 0.0
    selective_extension_proof_bars: int = 0
    selective_extension_min_mfe_r: float = 0.0
    selective_extension_min_regime_strength: float = 0.0
    selective_extension_min_bias_strength: float = 0.0
    selective_extension_min_quality_score_v2: float = 0.0
    selective_extension_time_stop_bars: int = 0
    selective_extension_take_profit_r: float = 0.0
    selective_extension_move_stop_to_be_at_r: float = 0.0

    @classmethod
    def from_execution_hints(cls, hints: Mapping[str, Any] | None) -> "PositionManagementSpec":
        source = hints if isinstance(hints, Mapping) else {}
        return cls(
            management_policy=normalize_management_policy(source.get("management_policy")),
            time_stop_bars=max(_to_int(source.get("time_stop_bars")), 0),
            stop_exit_cooldown_bars=max(_to_int(source.get("stop_exit_cooldown_bars")), 0),
            profit_exit_cooldown_bars=max(_to_int(source.get("profit_exit_cooldown_bars")), 0),
            progress_check_bars=max(_to_int(source.get("progress_check_bars")), 0),
            progress_min_mfe_r=max(_to_float(source.get("progress_min_mfe_r")), 0.0),
            progress_extend_trigger_r=max(
                _to_float(source.get("progress_extend_trigger_r")),
                0.0,
            ),
            progress_extend_bars=max(_to_int(source.get("progress_extend_bars")), 0),
            loss_streak_trigger=max(_to_int(source.get("loss_streak_trigger")), 0),
            loss_streak_cooldown_bars=max(_to_int(source.get("loss_streak_cooldown_bars")), 0),
            reward_risk_reference_r=max(_to_float(source.get("reward_risk_reference_r")), 0.0),
            tp_partial_ratio=min(max(_to_float(source.get("tp_partial_ratio")), 0.0), 1.0),
            tp_partial_at_r=max(_to_float(source.get("tp_partial_at_r")), 0.0),
            move_stop_to_be_at_r=max(_to_float(source.get("move_stop_to_be_at_r")), 0.0),
            allow_reverse_exit=_to_bool(source.get("allow_reverse_exit"), default=True),
            reduce_only_exit=_to_bool(source.get("reduce_only_exit"), default=True),
            entry_quality_score_v2=max(_to_float(source.get("entry_quality_score_v2")), 0.0),
            quality_exit_applied=_to_bool(source.get("quality_exit_applied"), default=False),
            entry_regime_strength=max(_to_float(source.get("entry_regime_strength")), 0.0),
            entry_bias_strength=max(_to_float(source.get("entry_bias_strength")), 0.0),
            selective_extension_proof_bars=max(
                _to_int(source.get("selective_extension_proof_bars")),
                0,
            ),
            selective_extension_min_mfe_r=max(
                _to_float(source.get("selective_extension_min_mfe_r")),
                0.0,
            ),
            selective_extension_min_regime_strength=max(
                _to_float(source.get("selective_extension_min_regime_strength")),
                0.0,
            ),
            selective_extension_min_bias_strength=max(
                _to_float(source.get("selective_extension_min_bias_strength")),
                0.0,
            ),
            selective_extension_min_quality_score_v2=max(
                _to_float(source.get("selective_extension_min_quality_score_v2")),
                0.0,
            ),
            selective_extension_time_stop_bars=max(
                _to_int(source.get("selective_extension_time_stop_bars")),
                0,
            ),
            selective_extension_take_profit_r=max(
                _to_float(source.get("selective_extension_take_profit_r")),
                0.0,
            ),
            selective_extension_move_stop_to_be_at_r=max(
                _to_float(source.get("selective_extension_move_stop_to_be_at_r")),
                0.0,
            ),
        )

    @classmethod
    def from_plan(cls, plan: Mapping[str, Any] | None) -> "PositionManagementSpec":
        return cls.from_execution_hints(plan)

    def uses_runner_management(self) -> bool:
        return normalize_management_policy(self.management_policy) == "tp1_runner"

    def to_execution_hints(self) -> dict[str, float | int | bool | str]:
        payload: dict[str, float | int | bool | str] = {
            "time_stop_bars": int(self.time_stop_bars),
            "stop_exit_cooldown_bars": int(self.stop_exit_cooldown_bars),
            "profit_exit_cooldown_bars": int(self.profit_exit_cooldown_bars),
            "progress_check_bars": int(self.progress_check_bars),
            "progress_min_mfe_r": float(self.progress_min_mfe_r),
            "progress_extend_trigger_r": float(self.progress_extend_trigger_r),
            "progress_extend_bars": int(self.progress_extend_bars),
            "loss_streak_trigger": int(self.loss_streak_trigger),
            "loss_streak_cooldown_bars": int(self.loss_streak_cooldown_bars),
            "reward_risk_reference_r": float(self.reward_risk_reference_r),
            "tp_partial_ratio": float(self.tp_partial_ratio),
            "tp_partial_at_r": float(self.tp_partial_at_r),
            "move_stop_to_be_at_r": float(self.move_stop_to_be_at_r),
            "allow_reverse_exit": bool(self.allow_reverse_exit),
            "reduce_only_exit": bool(self.reduce_only_exit),
            "entry_quality_score_v2": float(self.entry_quality_score_v2),
            "quality_exit_applied": bool(self.quality_exit_applied),
            "entry_regime_strength": float(self.entry_regime_strength),
            "entry_bias_strength": float(self.entry_bias_strength),
            "selective_extension_proof_bars": int(self.selective_extension_proof_bars),
            "selective_extension_min_mfe_r": float(self.selective_extension_min_mfe_r),
            "selective_extension_min_regime_strength": float(
                self.selective_extension_min_regime_strength
            ),
            "selective_extension_min_bias_strength": float(self.selective_extension_min_bias_strength),
            "selective_extension_min_quality_score_v2": float(
                self.selective_extension_min_quality_score_v2
            ),
            "selective_extension_time_stop_bars": int(self.selective_extension_time_stop_bars),
            "selective_extension_take_profit_r": float(self.selective_extension_take_profit_r),
            "selective_extension_move_stop_to_be_at_r": float(
                self.selective_extension_move_stop_to_be_at_r
            ),
        }
        policy = normalize_management_policy(self.management_policy)
        if policy is not None:
            payload["management_policy"] = policy
        return payload

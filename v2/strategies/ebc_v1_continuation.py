from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Any, Literal

from v2.kernel.contracts import Candidate, CandidateSelector, KernelContext
from v2.strategies.alpha_shared import _clamp_score, _to_bars, _to_float, ema
from v2.strategies.base import DesiredPosition, StrategyPlugin

Side = Literal["LONG", "SHORT", "NONE"]


def _stddev(values: list[float]) -> float:
    if not values:
        return 0.0
    avg = mean(values)
    variance = sum((float(item) - float(avg)) ** 2 for item in values) / float(len(values))
    return variance**0.5


def _bollinger_bands(closes: list[float], period: int, std_mult: float) -> tuple[float, float, float] | None:
    if period <= 1 or len(closes) < period:
        return None
    window = [float(item) for item in closes[-period:]]
    mid = mean(window)
    dev = _stddev(window)
    upper = float(mid) + (float(std_mult) * float(dev))
    lower = float(mid) - (float(std_mult) * float(dev))
    return float(lower), float(mid), float(upper)


def _cci(highs: list[float], lows: list[float], closes: list[float], period: int) -> float | None:
    if period <= 1 or min(len(highs), len(lows), len(closes)) < period:
        return None
    tps = [
        (float(highs[-period + idx]) + float(lows[-period + idx]) + float(closes[-period + idx])) / 3.0
        for idx in range(period)
    ]
    tp_mean = mean(tps)
    mean_dev = mean(abs(float(tp) - float(tp_mean)) for tp in tps)
    if mean_dev <= 1e-12:
        return 0.0
    return (float(tps[-1]) - float(tp_mean)) / (0.015 * float(mean_dev))


@dataclass(frozen=True)
class EBCParams:
    supported_symbols: tuple[str, ...] = ("BTCUSDT",)
    side_mode: str = "BOTH"
    ema_period_12h: int = 50
    ema_period_2h: int = 34
    ema_period_30m: int = 20
    ema_period_5m: int = 20
    bb_period_30m: int = 20
    bb_std_30m: float = 2.0
    cci_period_2h: int = 20
    cci_period_5m: int = 20
    cci_pullback_floor: float = -50.0
    cci_trigger_level: float = 0.0
    squeeze_width_frac_max: float = 0.035
    stop_loss_pct: float = 0.006
    take_profit_r: float = 1.8
    time_stop_bars: int = 24
    min_entry_score: float = 0.0

    @classmethod
    def from_params(cls, raw: dict[str, Any] | None) -> "EBCParams":
        source = raw or {}

        def _i(name: str, default: int) -> int:
            try:
                return max(int(source.get(name, default)), 1)
            except (TypeError, ValueError):
                return int(default)

        def _f(name: str, default: float) -> float:
            try:
                return float(source.get(name, default))
            except (TypeError, ValueError):
                return float(default)

        supported = source.get("supported_symbols", cls.supported_symbols)
        if isinstance(supported, str):
            symbols = tuple(token.strip().upper() for token in supported.split(",") if token.strip())
        elif isinstance(supported, (list, tuple)):
            symbols = tuple(str(token).strip().upper() for token in supported if str(token).strip())
        else:
            symbols = cls.supported_symbols
        return cls(
            supported_symbols=symbols or cls.supported_symbols,
            side_mode=(
                str(source.get("side_mode", cls.side_mode)).strip().upper()
                if str(source.get("side_mode", cls.side_mode)).strip().upper() in {"BOTH", "LONG", "SHORT"}
                else str(cls.side_mode)
            ),
            ema_period_12h=_i("ema_period_12h", cls.ema_period_12h),
            ema_period_2h=_i("ema_period_2h", cls.ema_period_2h),
            ema_period_30m=_i("ema_period_30m", cls.ema_period_30m),
            ema_period_5m=_i("ema_period_5m", cls.ema_period_5m),
            bb_period_30m=_i("bb_period_30m", cls.bb_period_30m),
            bb_std_30m=max(_f("bb_std_30m", cls.bb_std_30m), 0.1),
            cci_period_2h=_i("cci_period_2h", cls.cci_period_2h),
            cci_period_5m=_i("cci_period_5m", cls.cci_period_5m),
            cci_pullback_floor=_f("cci_pullback_floor", cls.cci_pullback_floor),
            cci_trigger_level=_f("cci_trigger_level", cls.cci_trigger_level),
            squeeze_width_frac_max=max(_f("squeeze_width_frac_max", cls.squeeze_width_frac_max), 0.0),
            stop_loss_pct=max(_f("stop_loss_pct", cls.stop_loss_pct), 0.0001),
            take_profit_r=max(_f("take_profit_r", cls.take_profit_r), 0.5),
            time_stop_bars=_i("time_stop_bars", cls.time_stop_bars),
            min_entry_score=min(max(_f("min_entry_score", cls.min_entry_score), 0.0), 1.0),
        )


def _frame(raw: Any) -> tuple[list[float], list[float], list[float]] | None:
    bars = _to_bars(raw)
    if not bars:
        return None
    closes = [float(bar.close) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    return closes, highs, lows


class EBCV1Continuation(StrategyPlugin):
    name = "ebc_v1_continuation"

    def __init__(self, *, params: dict[str, Any] | None = None, logger: Any | None = None) -> None:
        self._cfg = EBCParams.from_params(params)
        self._logger = logger

    def set_runtime_params(self, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        merged = self._cfg.__dict__.copy()
        merged.update(kwargs)
        self._cfg = EBCParams.from_params(merged)

    def decide(self, market_snapshot: dict[str, Any]) -> dict[str, Any]:
        symbol = str(market_snapshot.get("symbol") or "").strip().upper()
        if symbol not in self._cfg.supported_symbols:
            return DesiredPosition(symbol=symbol or "BTCUSDT", reason="unsupported_symbol").to_dict()
        market = market_snapshot.get("market")
        if not isinstance(market, dict):
            return DesiredPosition(symbol=symbol, reason="missing_market").to_dict()

        f12h = _frame(market.get("12h"))
        f2h = _frame(market.get("2h"))
        f30m = _frame(market.get("30m"))
        f5m = _frame(market.get("5m"))
        if not all((f12h, f2h, f30m, f5m)):
            return DesiredPosition(symbol=symbol, reason="insufficient_multitimeframe_data").to_dict()

        closes_12h, highs_12h, lows_12h = f12h
        closes_2h, highs_2h, lows_2h = f2h
        closes_30m, highs_30m, lows_30m = f30m
        closes_5m, highs_5m, lows_5m = f5m

        ema_12h = ema(closes_12h, self._cfg.ema_period_12h)
        ema_2h = ema(closes_2h, self._cfg.ema_period_2h)
        ema_30m = ema(closes_30m, self._cfg.ema_period_30m)
        ema_5m = ema(closes_5m, self._cfg.ema_period_5m)
        cci_2h = _cci(highs_2h, lows_2h, closes_2h, self._cfg.cci_period_2h)
        cci_5m = _cci(highs_5m, lows_5m, closes_5m, self._cfg.cci_period_5m)
        bands_30m = _bollinger_bands(closes_30m, self._cfg.bb_period_30m, self._cfg.bb_std_30m)

        if None in {ema_12h, ema_2h, ema_30m, ema_5m, cci_2h, cci_5m} or bands_30m is None:
            return DesiredPosition(symbol=symbol, reason="indicator_unavailable").to_dict()

        lower_30m, mid_30m, upper_30m = bands_30m
        width_frac_30m = (float(upper_30m) - float(lower_30m)) / max(abs(float(mid_30m)), 1e-9)
        regime_side: Side = "LONG" if float(closes_12h[-1]) >= float(ema_12h) else "SHORT"
        regime_strength = _clamp_score(
            abs(float(closes_12h[-1]) - float(ema_12h)) / max(abs(float(ema_12h)) * 0.01, 1e-9)
        )
        bias_side: Side
        if float(closes_2h[-1]) >= float(ema_2h) and float(cci_2h) >= float(self._cfg.cci_pullback_floor):
            bias_side = "LONG"
        elif float(closes_2h[-1]) <= float(ema_2h) and float(cci_2h) <= -float(self._cfg.cci_pullback_floor):
            bias_side = "SHORT"
        else:
            bias_side = "NONE"

        if regime_side == "NONE" or bias_side == "NONE" or regime_side != bias_side:
            return DesiredPosition(symbol=symbol, reason="bias_missing").to_dict()
        if str(self._cfg.side_mode) != "BOTH" and str(bias_side) != str(self._cfg.side_mode):
            return DesiredPosition(
                symbol=symbol,
                reason="side_not_allowed",
                regime="BULL" if bias_side == "LONG" else "BEAR",
                allowed_side=str(self._cfg.side_mode),
                blocks=["side_mode"],
            ).to_dict()

        if float(width_frac_30m) > float(self._cfg.squeeze_width_frac_max):
            return DesiredPosition(symbol=symbol, reason="setup_missing").to_dict()

        if bias_side == "LONG":
            setup_ok = float(closes_30m[-1]) >= float(ema_30m)
            trigger_ok = float(closes_5m[-1]) >= float(ema_5m) and float(cci_5m) >= float(self._cfg.cci_trigger_level)
        else:
            setup_ok = float(closes_30m[-1]) <= float(ema_30m)
            trigger_ok = float(closes_5m[-1]) <= float(ema_5m) and float(cci_5m) <= -float(self._cfg.cci_trigger_level)

        if not setup_ok:
            return DesiredPosition(symbol=symbol, reason="setup_missing").to_dict()
        if not trigger_ok:
            return DesiredPosition(symbol=symbol, reason="trigger_missing").to_dict()

        entry_price = float(closes_5m[-1])
        stop_hint = (
            float(entry_price) * (1.0 - float(self._cfg.stop_loss_pct))
            if bias_side == "LONG"
            else float(entry_price) * (1.0 + float(self._cfg.stop_loss_pct))
        )
        bias_strength = _clamp_score(
            (
                min(
                    abs(float(closes_2h[-1]) - float(ema_2h)) / max(abs(float(ema_2h)) * 0.005, 1e-9),
                    1.0,
                )
                * 0.5
            )
            + (min(abs(float(cci_2h)) / 200.0, 1.0) * 0.5)
        )
        score = _clamp_score(
            0.55
            + min(abs(float(cci_5m)) / 200.0, 1.0) * 0.15
            + min(abs(float(cci_2h)) / 200.0, 1.0) * 0.10
            + max(1.0 - (float(width_frac_30m) / max(float(self._cfg.squeeze_width_frac_max), 1e-9)), 0.0) * 0.10
        )
        indicators = {
            "ema_12h": float(ema_12h),
            "ema_2h": float(ema_2h),
            "ema_30m": float(ema_30m),
            "ema_5m": float(ema_5m),
            "cci_2h": float(cci_2h),
            "cci_5m": float(cci_5m),
            "bb_width_frac_30m": float(width_frac_30m),
            "regime_strength": float(regime_strength),
            "bias_strength": float(bias_strength),
        }
        regime = "BULL" if bias_side == "LONG" else "BEAR"
        allowed_side = "LONG" if bias_side == "LONG" else "SHORT"
        if float(score) < float(self._cfg.min_entry_score):
            return DesiredPosition(
                symbol=symbol,
                score=float(score),
                reason="quality_score_below_min",
                regime=regime,
                allowed_side=allowed_side,
                indicators=indicators,
                blocks=["min_entry_score"],
            ).to_dict()
        payload = dict(
            DesiredPosition(
                symbol=symbol,
                intent="LONG" if bias_side == "LONG" else "SHORT",
                side="BUY" if bias_side == "LONG" else "SELL",
                score=float(score),
                reason="entry_signal",
                entry_price=float(entry_price),
                stop_hint=float(stop_hint),
                management_hint="ebc_continuation",
                regime=regime,
                allowed_side=allowed_side,
                signals={"ebc_v1_continuation": True},
                blocks=[],
                indicators=indicators,
            ).to_dict()
        )
        payload["entry_family"] = "ebc_continuation"
        payload["alpha_id"] = "alpha_ebc"
        payload["execution"] = {
            "reward_risk_reference_r": float(self._cfg.take_profit_r),
            "time_stop_bars": int(self._cfg.time_stop_bars),
            "entry_quality_score_v2": float(score),
            "entry_regime_strength": float(regime_strength),
            "entry_bias_strength": float(bias_strength),
        }
        return payload


class EBCV1ContinuationCandidateSelector(CandidateSelector):
    def __init__(
        self,
        *,
        strategy: StrategyPlugin,
        symbols: list[str],
        snapshot_provider: Any | None = None,
        overheat_fetcher: Any | None = None,
        journal_logger: Any | None = None,
    ) -> None:
        self._strategy = strategy
        self._symbols = [str(sym).strip().upper() for sym in symbols if str(sym).strip()] or ["BTCUSDT"]
        self._snapshot_provider = snapshot_provider
        self._overheat_fetcher = overheat_fetcher
        self._journal_logger = journal_logger

    def select(self, *, context: KernelContext) -> Candidate | None:
        _ = context
        snapshot = self._snapshot_provider() if self._snapshot_provider is not None else {}
        if not isinstance(snapshot, dict):
            return None
        decision = self._strategy.decide(snapshot)
        if self._journal_logger is not None and isinstance(decision, dict):
            self._journal_logger(decision)
        if not isinstance(decision, dict):
            return None
        intent = str(decision.get("intent") or "NONE")
        side = str(decision.get("side") or "NONE")
        if intent not in {"LONG", "SHORT"} or side not in {"BUY", "SELL"}:
            return None
        score = _to_float(decision.get("score")) or 0.0
        entry_price = _to_float(decision.get("entry_price")) or 0.0
        if score <= 0.0 or entry_price <= 0.0:
            return None
        return Candidate(
            symbol=str(decision.get("symbol") or self._symbols[0]),
            side=side,
            score=float(score),
            entry_price=float(entry_price),
            source="ebc_v1_continuation",
        )

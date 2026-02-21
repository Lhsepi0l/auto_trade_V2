from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import sqrt
from typing import Any, Literal, cast

from v2.clean_room.contracts import Candidate, CandidateSelector, KernelContext
from v2.strategies.base import AllowedSide, DesiredPosition, Regime, StrategyPlugin

TradeMode = Literal["pullback", "donchian"]


@dataclass(frozen=True)
class _Candle:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class _OverheatConfig:
    funding_block_threshold: float = 0.0006
    ratio_long_block: float = 1.45
    ratio_short_block: float = 0.65
    ttl_seconds: int = 30


@dataclass
class _OverheatState:
    updated_at: datetime
    funding_rate: float
    ratio: float


@dataclass
class _DecisionDebug:
    regime: str = "UNKNOWN"
    allowed_side: str = "NONE"
    signals: dict[str, bool] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    indicators: dict[str, Any] = field(default_factory=dict)


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_candles(raw: list[Any]) -> list[_Candle]:
    out: list[_Candle] = []
    for row in raw:
        if isinstance(row, dict):
            o = _to_float(row.get("open"))
            h = _to_float(row.get("high"))
            low = _to_float(row.get("low"))
            c = _to_float(row.get("close"))
        elif isinstance(row, (list, tuple)) and len(row) >= 4:
            o = _to_float(row[1])
            h = _to_float(row[2])
            low = _to_float(row[3])
            c = _to_float(row[4])
        else:
            continue

        if o is None or h is None or low is None or c is None:
            continue
        out.append(_Candle(open=o, high=h, low=low, close=c))
    return out


def _sma(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    recent = values[-period:]
    return sum(recent) / float(period)


def _ema(values: list[float], period: int) -> float | None:
    period = int(period)
    if period <= 1:
        return _to_float(values[-1]) if values else None
    if len(values) < period:
        return None

    alpha = 2.0 / (period + 1.0)
    value = float(values[0])
    for row in values[1:]:
        value = alpha * float(row) + (1.0 - alpha) * value
    return value


def _atr(candles: list[_Candle], period: int = 14) -> float | None:
    period = int(period)
    if len(candles) < period + 1 or period <= 1:
        return None

    trs: list[float] = []
    prev = candles[0].close
    for candle in candles[1:]:
        tr = max(candle.high - candle.low, abs(candle.high - prev), abs(candle.low - prev))
        trs.append(float(tr))
        prev = candle.close

    if len(trs) < period:
        return None
    return sum(trs[-period:]) / float(period)


def _rsi(closes: list[float], period: int = 14) -> float | None:
    period = int(period)
    if period <= 0 or len(closes) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    for idx in range(-period, 0):
        diff = closes[idx] - closes[idx - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff

    avg_gain = gains / float(period)
    avg_loss = losses / float(period)
    if avg_loss <= 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger(closes: list[float], period: int = 20, k: float = 2.0) -> tuple[float, float, float] | None:
    period = int(period)
    if period <= 0 or len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / float(period)
    variance = sum((x - mean) ** 2 for x in window) / float(period)
    deviation = sqrt(variance) if variance >= 0 else 0.0
    return mean + (k * deviation), mean - (k * deviation), mean


def _donchian(candles: list[_Candle], period: int = 20) -> tuple[float, float] | None:
    period = int(period)
    if period <= 0 or len(candles) < period:
        return None
    window = candles[-period:]
    highs = [row.high for row in window]
    lows = [row.low for row in window]
    return max(highs), min(lows)


def _adx(candles: list[_Candle], period: int = 14) -> float | None:
    period = int(period)
    if len(candles) < period + 1 or period <= 1:
        return None

    prev = candles[0]
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []

    for current in candles[1:]:
        up_move = current.high - prev.high
        down_move = prev.low - current.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

        tr = max(current.high - current.low, abs(current.high - prev.close), abs(current.low - prev.close))
        trs.append(float(tr))
        prev = current

    if len(trs) < period:
        return None

    atr = sum(trs[:period]) / float(period)
    plus_dm_avg = sum(plus_dm[:period]) / float(period)
    minus_dm_avg = sum(minus_dm[:period]) / float(period)
    if atr <= 0:
        return 0.0

    plus_di = 100.0 * (plus_dm_avg / atr)
    minus_di = 100.0 * (minus_dm_avg / atr)
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100.0 if (plus_di + minus_di) > 0 else 0.0
    dx_values = deque([dx], maxlen=period)

    for i in range(period, len(trs)):
        tr = trs[i]
        p = plus_dm[i]
        m = minus_dm[i]
        atr = (atr * (period - 1) + tr) / float(period)
        plus_dm_avg = (plus_dm_avg * (period - 1) + p) / float(period)
        minus_dm_avg = (minus_dm_avg * (period - 1) + m) / float(period)
        if atr <= 0:
            dx_values.append(0.0)
            continue
        plus_di = 100.0 * (plus_dm_avg / atr)
        minus_di = 100.0 * (minus_dm_avg / atr)
        dx = (
            abs(plus_di - minus_di) / (plus_di + minus_di) * 100.0
            if (plus_di + minus_di) > 0
            else 0.0
        )
        dx_values.append(dx)

    if not dx_values:
        return None
    return float(sum(dx_values) / float(len(dx_values)))


def _supertrend(
    candles: list[_Candle],
    atr_period: int = 10,
    multiplier: float = 3.0,
) -> tuple[Literal[1, -1, 0], int, float | None]:
    period = max(int(atr_period), 1)
    if len(candles) < period + 2:
        return 0, 0, None

    tr: list[float] = []
    prev = candles[0].close
    for row in candles[1:]:
        tr_value = max(row.high - row.low, abs(row.high - prev), abs(row.low - prev))
        tr.append(float(tr_value))
        prev = row.close

    if len(tr) < period:
        return 0, 0, None

    atr_values = [0.0] * len(candles)
    first_atr = sum(tr[:period]) / float(period)
    atr_values[period] = first_atr
    for idx in range(period + 1, len(candles)):
        atr_values[idx] = (tr[idx - 1] + atr_values[idx - 1] * (period - 1)) / float(period)

    direction: Literal[1, -1, 0] = 0
    flip_count = 0
    prev_upper = 0.0
    prev_lower = 0.0

    for idx in range(period, len(candles)):
        candle = candles[idx]
        atr = atr_values[idx]
        if atr <= 0:
            continue

        hl2 = (candle.high + candle.low) / 2.0
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr
        prev_close = candles[idx - 1].close

        if idx == period:
            final_upper = basic_upper
            final_lower = basic_lower
            direction = 1 if candle.close >= final_lower else -1
        else:
            final_upper = basic_upper if (basic_upper < prev_upper) or (prev_close > prev_upper) else prev_upper
            final_lower = basic_lower if (basic_lower > prev_lower) or (prev_close < prev_lower) else prev_lower

            next_dir = direction
            if direction == -1 and candle.close > final_upper:
                next_dir = 1
            elif direction == 1 and candle.close < final_lower:
                next_dir = -1

            if next_dir != direction:
                flip_count += 1
            direction = next_dir

        prev_upper = final_upper
        prev_lower = final_lower

    if direction == 0:
        return 0, flip_count, None
    return direction, flip_count, atr_values[-1]


class StrategyPackV1(StrategyPlugin):
    def __init__(
        self,
        *,
        name: str = "strategy_pack_v1",
        params: dict[str, Any] | None = None,
        logger: Any | None = None,
        overheat_fetcher: Any | None = None,
    ) -> None:
        p = params or {}
        self.name = name
        self._logger = logger
        self._entry_mode: TradeMode = "pullback"
        entry_mode = str(p.get("entry_mode", "pullback")).lower()
        if entry_mode in {"pullback", "donchian"}:
            self._entry_mode = cast(TradeMode, entry_mode)
        else:
            self._entry_mode = cast(TradeMode, "pullback")

        self._mean_reversion_enabled = bool(p.get("mean_reversion_enabled", False))
        self._supertrend_period = int(p.get("supertrend_atr_period", 10))
        self._supertrend_multiplier = float(p.get("supertrend_multiplier", 3.0))
        self._adx_period = int(p.get("adx_period", 14))
        self._adx_threshold = float(p.get("adx_threshold", 18.0))
        self._donchian_period = int(p.get("donchian_period", 20))
        self._donchian_anti_fake = float(p.get("donchian_anti_fake", 0.2))
        self._atr_period = int(p.get("atr_period", 14))
        self._bb_period = int(p.get("bb_period", 20))
        self._bb_std = float(p.get("bb_std", 2.0))
        self._rsi_period = int(p.get("rsi_period", 14))
        self._ema200_gating = bool(p.get("ema200_gating", True))
        self._sideways_flip_limit = int(p.get("supertrend_flip_limit", 3))
        self._overheat_cfg = _OverheatConfig(
            funding_block_threshold=float(p.get("overheat_funding_threshold", 0.0006)),
            ratio_long_block=float(p.get("overheat_ratio_long", 1.45)),
            ratio_short_block=float(p.get("overheat_ratio_short", 0.65)),
            ttl_seconds=int(p.get("overheat_cache_ttl", 30)),
        )

        self._overheat_fetcher = overheat_fetcher
        self._overheat_state: _OverheatState | None = None

    def _collect_market(self, market_snapshot: dict[str, Any]) -> dict[str, list[_Candle]]:
        raw_market = market_snapshot.get("market", {})
        out: dict[str, list[_Candle]] = {}
        for timeframe in ("4h", "1h", "15m"):
            raw_rows = raw_market.get(timeframe)
            out[timeframe] = _to_candles(raw_rows) if isinstance(raw_rows, list) else []
        return out

    def _collect_symbol(self, market_snapshot: dict[str, Any], default: str) -> str:
        for key in ("symbol", "default_symbol"):
            value = market_snapshot.get(key)
            if isinstance(value, str) and value:
                return value
        return default

    def _emit_journal(self, payload: dict[str, Any]) -> None:
        if self._logger is None:
            return
        try:
            self._logger(dict(payload))
        except TypeError:
            payload_copy = copy.deepcopy(payload)
            self._logger(payload_copy)

    def _fetch_overheat(self, symbol: str) -> tuple[float, float] | None:
        if self._overheat_fetcher is None:
            return None

        now = datetime.now(timezone.utc)
        cached = self._overheat_state
        if cached is not None:
            ttl = timedelta(seconds=max(self._overheat_cfg.ttl_seconds, 1))
            if now <= cached.updated_at + ttl:
                return cached.funding_rate, cached.ratio

        payload = self._overheat_fetcher(symbol)
        if isinstance(payload, tuple) and len(payload) == 2:
            funding = _to_float(payload[0])
            ratio = _to_float(payload[1])
        elif isinstance(payload, dict):
            funding = _to_float(payload.get("funding_rate") or payload.get("lastFundingRate"))
            ratio = _to_float(payload.get("ratio") or payload.get("longShortRatio"))
        else:
            return None

        if funding is None or ratio is None:
            return None

        self._overheat_state = _OverheatState(updated_at=now, funding_rate=funding, ratio=ratio)
        return funding, ratio

    def _eval_overheat_blocks(self, allowed_side: str, symbol: str) -> list[str]:
        if allowed_side == "NONE":
            return []

        data = self._fetch_overheat(symbol)
        if data is None:
            return []

        funding_rate, long_short_ratio = data
        blocks: list[str] = []

        if allowed_side in {"LONG", "BOTH"}:
            if funding_rate >= self._overheat_cfg.funding_block_threshold:
                blocks.append("overheat_funding_long")
            if long_short_ratio >= self._overheat_cfg.ratio_long_block:
                blocks.append("overheat_ratio_long")

        if allowed_side in {"SHORT", "BOTH"}:
            if funding_rate <= -self._overheat_cfg.funding_block_threshold:
                blocks.append("overheat_funding_short")
            if long_short_ratio <= self._overheat_cfg.ratio_short_block:
                blocks.append("overheat_ratio_short")

        return blocks

    def _ema200_gate(self, candles_4h: list[_Candle]) -> str:
        if len(candles_4h) < 200:
            return "NONE"

        closes = [row.close for row in candles_4h]
        ema_curr = _ema(closes, period=200)
        ema_prev = _ema(closes[:-1], period=200)
        if ema_curr is None or ema_prev is None:
            return "NONE"

        latest = closes[-1]
        if ema_curr > ema_prev and latest > ema_curr:
            return "LONG"
        if ema_curr < ema_prev and latest < ema_curr:
            return "SHORT"
        return "NONE"

    def _regime(self, candles_4h: list[_Candle], debug: _DecisionDebug) -> tuple[Regime, bool]:
        direction, flip_count, atr = _supertrend(
            candles_4h,
            atr_period=max(self._supertrend_period, 1),
            multiplier=self._supertrend_multiplier,
        )
        adx = _adx(candles_4h, period=max(self._adx_period, 1))

        debug.indicators["supertrend_direction"] = int(direction)
        debug.indicators["supertrend_lookback"] = len(candles_4h)
        debug.indicators["supertrend_flips"] = int(flip_count)
        debug.indicators["adx"] = None if adx is None else round(float(adx), 4)
        debug.indicators["atr_4h"] = None if atr is None else round(float(atr), 6)

        if direction == 0 or atr is None:
            if self._ema200_gating:
                ema_gate = self._ema200_gate(candles_4h)
                if ema_gate in {"LONG", "SHORT"}:
                    return cast_regime(ema_gate), True
            return "SIDEWAYS", True

        if adx is not None and adx < self._adx_threshold:
            if self._ema200_gating:
                ema_gate = self._ema200_gate(candles_4h)
                if ema_gate in {"LONG", "SHORT"}:
                    return cast_regime(ema_gate), True
            return "SIDEWAYS", True

        if flip_count >= self._sideways_flip_limit:
            return "SIDEWAYS", True

        return ("BULL" if direction > 0 else "BEAR"), False

    def _allowed_side(self, regime: Regime) -> str:
        if regime == "BULL":
            return "LONG"
        if regime == "BEAR":
            return "SHORT"
        return "NONE"

    def _entry_signal_1h(self, candles_1h: list[_Candle], mode: str, allowed_side: str, debug: _DecisionDebug) -> dict[str, Any]:
        signal: dict[str, Any] = {
            "long": False,
            "short": False,
            "mode": mode,
            "mean_reversion": False,
        }

        if len(candles_1h) < max(self._donchian_period, 2):
            return signal

        atr = _atr(candles_1h, period=max(self._atr_period, 1))
        atr_value = atr if atr is not None else 0.0
        debug.indicators["atr_1h"] = None if atr is None else round(float(atr), 6)

        donchian = _donchian(candles_1h, period=max(self._donchian_period, 2))
        if donchian is None:
            return signal

        upper, lower = donchian
        last = candles_1h[-1]
        prev = candles_1h[-2]
        debug.indicators["donchian_upper_1h"] = round(float(upper), 6)
        debug.indicators["donchian_lower_1h"] = round(float(lower), 6)

        if mode == "donchian":
            min_distance = self._donchian_anti_fake * max(atr_value, 0.000001)
            if (
                allowed_side in {"LONG", "BOTH"}
                and last.close > upper
                and (last.close - upper) >= min_distance
            ):
                signal["long"] = True
            if (
                allowed_side in {"SHORT", "BOTH"}
                and last.close < lower
                and (lower - last.close) >= min_distance
            ):
                signal["short"] = True
            return signal

        if allowed_side in {"LONG", "BOTH"} and prev.close <= lower and last.close > lower:
            signal["long"] = True
        if allowed_side in {"SHORT", "BOTH"} and prev.close >= upper and last.close < upper:
            signal["short"] = True

        return signal

    def _mean_reversion_signal_1h(
        self,
        candles_1h: list[_Candle],
        allowed_side: str,
        debug: _DecisionDebug,
    ) -> tuple[dict[str, Any], bool]:
        signal: dict[str, Any] = {
            "long": False,
            "short": False,
            "mode": self._entry_mode,
            "mean_reversion": True,
        }

        if len(candles_1h) < max(self._bb_period, self._rsi_period) + 1:
            return signal, False

        closes = [row.close for row in candles_1h]
        last = closes[-1]
        bb = _bollinger(closes, period=max(self._bb_period, 1), k=self._bb_std)
        rsi = _rsi(closes, period=max(self._rsi_period, 1))

        if bb is None or rsi is None:
            return signal, False

        upper, lower, mid = bb
        debug.indicators["bb_upper_1h"] = round(float(upper), 6)
        debug.indicators["bb_lower_1h"] = round(float(lower), 6)
        debug.indicators["bb_mid_1h"] = round(float(mid), 6)

        debug.indicators["rsi_1h"] = round(float(rsi), 4)

        if allowed_side in {"LONG", "BOTH"} and last < lower and rsi < 35:
            signal["long"] = True
            return signal, True
        if allowed_side in {"SHORT", "BOTH"} and last > upper and rsi > 65:
            signal["short"] = True
            return signal, True

        return signal, False

    def _build_reason(self, intent: str, mode: str, blocks: list[str]) -> str:
        if intent == "LONG":
            return (
                f"entry_{mode}_long"
                if not any(block.startswith("overheat_") for block in blocks)
                else "blocked"
            )
        if intent == "SHORT":
            return (
                f"entry_{mode}_short"
                if not any(block.startswith("overheat_") for block in blocks)
                else "blocked"
            )
        if blocks:
            return ",".join(blocks)
        return f"no_entry:{mode}"

    def _build_management_hint(self, signal: dict[str, Any], last_candle: float | None) -> float | None:
        if not signal.get("mean_reversion"):
            return None

        if last_candle is None or last_candle <= 0:
            return None

        return 1.25

    def decide(self, market_snapshot: dict[str, Any]) -> dict[str, Any]:
        symbol = self._collect_symbol(market_snapshot, "BTCUSDT")
        markets = self._collect_market(market_snapshot)
        candles_4h = markets["4h"]
        candles_1h = markets["1h"]

        debug = _DecisionDebug()

        if len(candles_4h) < max(self._supertrend_period + 2, 40):
            payload = {
                "symbol": symbol,
                "intent": "NONE",
                "side": "NONE",
                "score": 0.0,
                "reason": "insufficient_4h_data",
                "regime": "UNKNOWN",
                "allowed_side": "NONE",
                "signals": {},
                "blocks": ["insufficient_4h_data"],
                "indicators": {},
                "filters": {"insufficient_data": True},
            }
            self._emit_journal(payload)
            return payload

        regime, is_sideways = self._regime(candles_4h, debug)
        allowed_side = self._allowed_side(regime)
        debug.regime = regime
        debug.allowed_side = allowed_side

        blocks = list[str]()
        if regime == "SIDEWAYS":
            blocks.append("sideways_regime")

        if is_sideways and not self._mean_reversion_enabled:
            blocks.append("sideways_mr_disabled")

        overheat_blocks = self._eval_overheat_blocks(allowed_side, symbol)
        if overheat_blocks:
            blocks.extend(overheat_blocks)
            debug.filters["overheat_blocked"] = True

        signal = self._entry_signal_1h(candles_1h, self._entry_mode, allowed_side, debug)
        debug.signals.update(
            {
                "1h_long": bool(signal.get("long")),
                "1h_short": bool(signal.get("short")),
            }
        )

        if is_sideways and self._mean_reversion_enabled:
            mr_signal, mr_ok = self._mean_reversion_signal_1h(candles_1h, allowed_side, debug)
            debug.signals["mean_reversion_enabled"] = True
            if mr_ok:
                signal = mr_signal
            else:
                signal["long"] = False
                signal["short"] = False

        debug.filters["is_sideways"] = is_sideways
        debug.filters["entry_mode"] = self._entry_mode
        debug.filters["mean_reversion_enabled"] = self._mean_reversion_enabled
        debug.filters["overheat_blocks"] = overheat_blocks

        raw_intent = "NONE"
        side = "NONE"
        score = 0.0
        reason = self._build_reason("NONE", self._entry_mode, blocks)
        entry_price = None
        stop_hint = None
        management_hint = None

        if signal.get("long") and allowed_side in {"LONG", "BOTH"} and not blocks:
            raw_intent = "LONG"
            side = "BUY"
            score = 1.0
            reason = self._build_reason("LONG", self._entry_mode, blocks)
            if candles_1h:
                entry_price = candles_1h[-1].close
            stop_hint = self._build_management_hint(signal, candles_1h[-1].close if candles_1h else None)
            management_hint = "mean_reversion" if signal.get("mean_reversion") else None

        elif signal.get("short") and allowed_side in {"SHORT", "BOTH"} and not blocks:
            raw_intent = "SHORT"
            side = "SELL"
            score = 1.0
            reason = self._build_reason("SHORT", self._entry_mode, blocks)
            if candles_1h:
                entry_price = candles_1h[-1].close
            stop_hint = self._build_management_hint(signal, candles_1h[-1].close if candles_1h else None)
            management_hint = "mean_reversion" if signal.get("mean_reversion") else None
        elif signal.get("long") or signal.get("short"):
            reason = "regime_block"

        if blocks and raw_intent in {"NONE"}:
            reason = ";".join(blocks)

        decision = dict(
            DesiredPosition(
                symbol=symbol,
                intent=raw_intent,
                side=side,
                score=score,
                reason=reason,
                entry_price=entry_price,
                stop_hint=stop_hint,
                management_hint=management_hint,
                regime=cast_regime(regime),
                allowed_side=cast_allowed(allowed_side),
                signals=signal,
                blocks=blocks,
                indicators=debug.indicators,
            ).to_dict()
        )

        decision["allowed_side"] = allowed_side
        decision["regime"] = regime
        decision["sideways"] = is_sideways
        decision["filters"] = debug.filters
        decision["entry_mode"] = self._entry_mode
        if not blocks:
            decision.pop("blocks", None)

        self._emit_journal(
            {
                "symbol": symbol,
                "regime": regime,
                "allowed_side": allowed_side,
                "is_sideways": is_sideways,
                "signals": signal,
                "filters": debug.filters,
                "indicators": debug.indicators,
                "decision": copy.deepcopy(decision),
            }
        )

        return decision


def cast_regime(value: str) -> Regime:
    if value == "BULL":
        return "BULL"
    if value == "BEAR":
        return "BEAR"
    if value == "SIDEWAYS":
        return "SIDEWAYS"
    return "UNKNOWN"


def cast_allowed(value: str) -> AllowedSide:
    if value == "LONG":
        return "LONG"
    if value == "SHORT":
        return "SHORT"
    return "NONE"


class StrategyPackV1CandidateSelector(CandidateSelector):
    def __init__(
        self,
        *,
        strategy: StrategyPlugin,
        symbol: str,
        snapshot_provider: Any | None = None,
        journal_logger: Any | None = None,
    ) -> None:
        self._strategy = strategy
        self._symbol = symbol
        self._snapshot_provider = snapshot_provider
        self._journal_logger = journal_logger

    def select(self, *, context: KernelContext) -> Candidate | None:
        _ = context
        snapshot: dict[str, Any] = {"symbol": self._symbol}

        if self._snapshot_provider is not None:
            provided = self._snapshot_provider()
            if isinstance(provided, dict):
                snapshot.update(copy.deepcopy(provided))

        decision = self._strategy.decide(snapshot)
        if self._journal_logger is not None:
            self._journal_logger(decision)

        if decision.get("intent") not in {"LONG", "SHORT"}:
            return None

        side = str(decision.get("side") or "NONE")
        if side == "BUY":
            trade_side = "BUY"
        elif side == "SELL":
            trade_side = "SELL"
        else:
            return None

        intent = str(decision.get("intent") or "NONE")
        if intent == "LONG" and side != "BUY":
            return None
        if intent == "SHORT" and side != "SELL":
            return None

        score = float(decision.get("score", 0.0) or 0.0)
        if score <= 0:
            return None

        entry_price = _to_float(decision.get("entry_price"))
        stop_hint = _to_float(decision.get("stop_hint"))

        return Candidate(
            symbol=str(decision.get("symbol") or self._symbol),
            side=trade_side,
            score=score,
            reason=str(decision.get("reason") or "intent_provided"),
            entry_price=entry_price,
            volatility_hint=stop_hint,
        )

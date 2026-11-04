import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Price = Annotated[float, Field(gt=0)]
Percent = Annotated[float, Field(ge=0, le=100)]
Timestamp = Annotated[int, Field(gt=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class H1(StrictModel):
    confirmed: bool
    bar_close_ms: Timestamp
    trend: Literal["bullish", "bearish"]
    close: Price
    ema_fast: Price
    ema_slow: Price


class M5(StrictModel):
    open: Price
    high: Price
    low: Price
    close: Price
    ema: Price
    rsi: Percent
    adx: Percent
    atr: Price
    plus_di: Percent
    minus_di: Percent

    @model_validator(mode="after")
    def coherent_candle(self):
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("OHLC values are inconsistent")
        return self


class Swing(StrictModel):
    price: Price
    pivot_close_ms: Timestamp
    confirmed_at_ms: Timestamp
    age_bars: Annotated[int, Field(ge=0)]
    active: bool


class MarketStructure(StrictModel):
    timeframe: Literal["5"]
    method: Literal["confirmed_pivots"]
    left_bars: Annotated[int, Field(ge=1, le=20)]
    right_bars: Annotated[int, Field(ge=1, le=20)]
    max_age_bars: Annotated[int, Field(ge=1, le=288)]
    state: Literal["bullish", "bearish", "ranging", "insufficient"]
    recent_high: Swing | None
    previous_high: Swing | None
    recent_low: Swing | None
    previous_low: Swing | None

    def derived_state(self):
        points = (self.recent_high, self.previous_high, self.recent_low, self.previous_low)
        if any(point is None or point.age_bars > self.max_age_bars for point in points):
            return "insufficient"
        if self.recent_high.price > self.previous_high.price and self.recent_low.price > self.previous_low.price:
            return "bullish"
        if self.recent_high.price < self.previous_high.price and self.recent_low.price < self.previous_low.price:
            return "bearish"
        return "ranging"

    @model_validator(mode="after")
    def coherent_structure(self):
        for recent, previous in ((self.recent_high, self.previous_high), (self.recent_low, self.previous_low)):
            if previous is not None and recent is None:
                raise ValueError("Previous pivot requires recent pivot")
            if recent and previous and not (previous.pivot_close_ms < recent.pivot_close_ms and previous.confirmed_at_ms < recent.confirmed_at_ms and previous.age_bars > recent.age_bars):
                raise ValueError("Pivots must be ordered by confirmation time")
        if self.state != self.derived_state():
            raise ValueError("Structure state does not match pivot evidence")
        return self


class Context(StrictModel):
    prior_range_low: Price
    prior_range_high: Price
    range_bars: Annotated[int, Field(ge=2)]
    market_structure: MarketStructure | None

    @model_validator(mode="after")
    def ordered_range(self):
        if self.prior_range_low > self.prior_range_high:
            raise ValueError("Prior range is inverted")
        return self


class XMSource(StrictModel):
    provider: Literal["xm_mt5_demo"]
    server: Annotated[str, Field(min_length=1, max_length=80)]
    broker_symbol: Annotated[str, Field(min_length=1, max_length=40)]
    engine_version: Literal["xm-0.1"]
    context_time_basis: Literal["broker_server_wall_clock"]
    bar_close_server_ms: Timestamp
    snapshot_utc_s: Timestamp
    quote_server_ms: Timestamp
    observed_utc_offset_s: Annotated[int, Field(ge=-50400, le=50400)]
    bid: Price
    ask: Price
    price_tick: Price
    input_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

    @model_validator(mode="after")
    def coherent(self):
        if self.ask < self.bid or self.observed_utc_offset_s % 900:
            raise ValueError("Invalid XM source quote or offset")
        quote_utc_s = self.quote_server_ms / 1000 - self.observed_utc_offset_s
        if not -30 <= self.snapshot_utc_s - quote_utc_s <= 120:
            raise ValueError("XM quote is stale or in the future")
        return self


class Signal(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=3)]
    strategy_version: Literal["0.1", "0.2", "xm-0.1"]
    event: Literal["signal"]
    signal_id: Annotated[str, Field(min_length=1, max_length=160)]
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9_]+:XAUUSD[A-Z0-9_.-]*$", max_length=80)]
    timeframe: Literal["5"]
    bar_close_ms: Timestamp
    candle_confirmed: bool
    side: Literal["BUY", "SELL"]
    entry_basis: Literal["signal_close"]
    entry: Price
    sl: Price
    tp: Price
    risk_reward: Price
    h1: H1
    m5: M5
    context: Context
    source: XMSource | None = None

    @model_validator(mode="after")
    def consistent_signal(self):
        if not self.candle_confirmed or not self.h1.confirmed:
            raise ValueError("Only confirmed candles are accepted")
        expected_id = f"{self.symbol}:5:{self.bar_close_ms}:{self.side}"
        if self.schema_version == 2:
            expected_id += ":0.2"
        if self.schema_version == 3:
            expected_id += ":xm-0.1"
        if (self.schema_version, self.strategy_version) not in ((1, "0.1"), (2, "0.2"), (3, "xm-0.1")):
            raise ValueError("Schema and strategy versions do not match")
        event_context_ms = self.bar_close_ms
        if self.schema_version == 3:
            if self.source is None or self.symbol != "XM:XAUUSD":
                raise ValueError("XM signals require XM source evidence")
            event_context_ms = self.source.bar_close_server_ms
            if event_context_ms - self.source.observed_utc_offset_s * 1000 != self.bar_close_ms:
                raise ValueError("XM event time does not match observed offset")
            if not -30 <= self.source.snapshot_utc_s - self.bar_close_ms / 1000 <= 300:
                raise ValueError("XM event is not near its source snapshot")
        elif self.source is not None or self.symbol == "XM:XAUUSD":
            raise ValueError("XM source requires schema 3")
        structure = self.context.market_structure
        if (self.schema_version == 1 and structure is not None) or (self.schema_version >= 2 and structure is None):
            raise ValueError("Structure payload does not match schema version")
        if self.signal_id != expected_id:
            raise ValueError("signal_id does not match symbol, time and side")
        if self.bar_close_ms % 300_000:
            raise ValueError("M5 close must align to a 5-minute boundary")
        if self.h1.bar_close_ms > event_context_ms - 300_000:
            raise ValueError("H1 must precede the current M5 candle")
        if not math.isclose(self.entry, self.m5.close, rel_tol=0, abs_tol=1e-7):
            raise ValueError("Signal entry must match M5 close")
        if self.side == "BUY":
            valid = self.sl < self.entry < self.tp and self.h1.trend == "bullish"
            trend = self.h1.close > self.h1.ema_fast > self.h1.ema_slow
        else:
            valid = self.tp < self.entry < self.sl and self.h1.trend == "bearish"
            trend = self.h1.close < self.h1.ema_fast < self.h1.ema_slow
        if not valid or not trend:
            raise ValueError("Side, levels or H1 trend are inconsistent")
        actual_rr = abs(self.tp - self.entry) / abs(self.entry - self.sl)
        if not math.isclose(actual_rr, self.risk_reward, rel_tol=1e-6, abs_tol=1e-6):
            raise ValueError("risk_reward does not match prices")
        if structure:
            for point in (structure.recent_high, structure.previous_high, structure.recent_low, structure.previous_low):
                if point is None:
                    continue
                if point.pivot_close_ms % 300000 or point.confirmed_at_ms % 300000:
                    raise ValueError("Pivot times must align to M5")
                if point.confirmed_at_ms - point.pivot_close_ms < structure.right_bars * 300000:
                    raise ValueError("Pivot lacks right-hand confirmation bars")
                elapsed = event_context_ms - point.confirmed_at_ms
                if elapsed < point.age_bars * 300000 or (point.age_bars == 0 and elapsed != 0):
                    raise ValueError("Invalid pivot age or future confirmation")
            if structure.recent_high and structure.recent_high.active and self.entry > structure.recent_high.price:
                raise ValueError("Resistance already broken by current close")
            if structure.recent_low and structure.recent_low.active and self.entry < structure.recent_low.price:
                raise ValueError("Support already broken by current close")
        return self

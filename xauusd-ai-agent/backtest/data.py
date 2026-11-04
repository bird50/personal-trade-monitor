"""Load broker M1 history and resample it by broker server time.

Bars are bid prices stamped with the broker *server wall clock* (``time_server_s`` is the
bar open). Times are never converted with an assumed UTC offset; the header's
``observed_utc_offset_s`` is kept as metadata only (it is the offset at export time and
does not describe historical DST periods).
"""
import hashlib
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

COLUMNS = ("time_server_s", "open", "high", "low", "close", "tick_volume", "spread_points")
FRAMES = {"M1": 60, "M5": 300, "H1": 3600, "H4": 14400, "D1": 86400}
_META = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)")


@dataclass
class History:
    m1: list[dict]
    meta: dict = field(default_factory=dict)
    point: float = 0.01
    digits: int = 2
    sha256: str = ""
    path: str = ""

    def ask(self, bar: dict, key="close"):
        return ask_price(bar[key], bar["spread_points"], self.point, self.digits)


def ask_price(bid: float, spread_points: int, point: float, digits: int) -> float:
    return round(bid + spread_points * point, digits)


def parse_meta(line: str) -> dict:
    """Parse ``# key=value ...`` headers; values may contain spaces (``server=XMGlobal-MT5 7``)."""
    return {k: v.strip() for k, v in _META.findall(line.lstrip("#").strip())}


def load_m1(path, point=None, digits=None) -> History:
    path = Path(path)
    raw = path.read_bytes()
    meta, bars, header = {}, [], None
    previous = None
    for number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            meta.update(parse_meta(line))
            continue
        cells = [c.strip() for c in line.split(",")]
        if header is None:
            header = tuple(cells)
            if header != COLUMNS:
                raise ValueError(f"Unexpected CSV columns {header}; expected {COLUMNS}")
            continue
        if len(cells) != len(COLUMNS):
            raise ValueError(f"Line {number}: expected {len(COLUMNS)} cells")
        t = int(cells[0])
        o, h, l, c = (float(x) for x in cells[1:5])
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError(f"Line {number}: inconsistent OHLC")
        if t % 60:
            raise ValueError(f"Line {number}: M1 time is not minute aligned")
        if previous is not None and t <= previous:
            raise ValueError(f"Line {number}: bars must be unique and oldest first")
        previous = t
        bars.append({"open_server_s": t, "open": o, "high": h, "low": l, "close": c,
                     "tick_volume": int(cells[5]), "spread_points": int(cells[6])})
    if header is None or not bars:
        raise ValueError("CSV has no bars")
    point = float(point if point is not None else meta.get("point", 0.01))
    digits = int(digits if digits is not None else meta.get("digits", 2))
    return History(bars, meta, point, digits, hashlib.sha256(raw).hexdigest(), str(path))


def resample(bars: list[dict], period: int) -> list[dict]:
    """Aggregate oldest-first bars into server-time buckets ``floor(t / period) * period``.

    ``spread_points`` is the closing sub-bar spread (used for the close ask) and
    ``spread_max_points`` the widest spread inside the bucket.
    """
    out, current = [], None
    for bar in bars:
        start = bar["open_server_s"] // period * period
        if current is None or current["open_server_s"] != start:
            current = {"open_server_s": start, "open": bar["open"], "high": bar["high"], "low": bar["low"],
                       "close": bar["close"], "tick_volume": bar["tick_volume"],
                       "spread_points": bar["spread_points"], "spread_max_points": bar["spread_points"]}
            out.append(current)
            continue
        current["high"] = max(current["high"], bar["high"])
        current["low"] = min(current["low"], bar["low"])
        current["close"] = bar["close"]
        current["tick_volume"] += bar["tick_volume"]
        current["spread_points"] = bar["spread_points"]
        current["spread_max_points"] = max(current["spread_max_points"], bar["spread_points"])
    return out


def write_csv(path, bars: list[dict], meta: dict | None = None, digits=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if meta:
        lines.append("# " + " ".join(f"{k}={v}" for k, v in meta.items()))
    lines.append(",".join(COLUMNS))
    for b in bars:
        lines.append(f"{b['open_server_s']},{b['open']:.{digits}f},{b['high']:.{digits}f},{b['low']:.{digits}f},"
                     f"{b['close']:.{digits}f},{b['tick_volume']},{b['spread_points']}")
    path.write_text("\n".join(lines) + "\n")
    return path


SYNTHETIC_META = {"source": "synthetic", "server": "XMGlobal-MT5 7", "symbol": "GOLD", "timeframe": "PERIOD_M1",
                  "point": "0.01", "digits": "2", "observed_utc_offset_s": "7200",
                  "time_basis": "broker_server_wall_clock", "price": "bid"}


def synthetic_m1(days=20, start_server_s=1_767_571_200, seed=7, price=2400.0, spread_points=25,
                 weekends=True, drift=0.0, regime_minutes=720):
    """Deterministic random-walk M1 bid bars for tests and demonstrations (not market data).

    The drift flips sign every ``regime_minutes`` so trend and counter-trend phases both occur.
    ``start_server_s`` defaults to Monday 2026-01-05 00:00 server time.
    """
    rng = random.Random(seed)
    bars, close = [], price
    for minute in range(days * 1440):
        t = start_server_s + minute * 60
        weekday = (t // 86400 + 3) % 7  # 1970-01-01 was a Thursday; 0 = Monday.
        if weekends and weekday >= 5:
            continue
        regime = 1 if (minute // regime_minutes) % 2 == 0 else -1
        open_ = close
        step = rng.gauss(regime * (drift or 0.02), 0.35)
        close = max(1.0, open_ + step)
        high = max(open_, close) + abs(rng.gauss(0, 0.15))
        low = min(open_, close) - abs(rng.gauss(0, 0.15))
        bars.append({"open_server_s": t, "open": round(open_, 2), "high": round(high, 2),
                     "low": round(low, 2), "close": round(close, 2),
                     "tick_volume": 20 + int(abs(rng.gauss(0, 30))),
                     "spread_points": spread_points + rng.choice((0, 0, 0, 5, 10))})
        bars[-1]["high"] = max(bars[-1]["high"], bars[-1]["open"], bars[-1]["close"])
        bars[-1]["low"] = min(bars[-1]["low"], bars[-1]["open"], bars[-1]["close"])
        close = bars[-1]["close"]
    return bars

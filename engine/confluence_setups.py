"""
confluence_setups.py

The three-setup rule set from "conflunence trading bot.md" (Intraday Forex Multi-Regime
Strategy, EUR/USD + USD/JPY), implemented on top of the Book 2 indicator frame
(confluence_engine_book2.compute_indicators_book2 + chart_patterns_book2) so the
detectors for flags, double tops/bottoms, head & shoulders and the volume profile are
the already-built ones.

  Setup 1  ORB        opening-range breakout
  Setup 2  PULLBACK   flag/pennant/wedge continuation at VWAP + HVN
  Setup 3  REVERSAL   double top/bottom or H&S at VAH/VAL, VWAP cross

Every item on a setup's "Confluence Checklist" is REQUIRED (the document lists them as a
checklist, not as a points system). Each signal carries the per-item booleans so the
journal shows exactly what fired. Bars where all but one item held are reported as
near-misses (never traded) so the rule strictness can be reviewed with real data.

Stops are the document's structural stops, not fixed pips. A signal whose structural stop
is tighter than MIN_SL_PIPS or wider than the setup's max is SKIPPED, never altered.
Target = 1:2 R:R from the actual fill (applied by the runner). Trailing stop = 1% of price
(applied by the runner).

WHAT IS NOT IMPLEMENTED (cannot be tested from OHLCV candles; each is a documented gap):
  * ORB "low-volume vacuum ahead" - only the HVN/POC origin of the breakout is tested.
  * ORB fallback to a horizontal-channel breakout when the opening range is wider than the
    average daily range - the ORB is skipped that day instead.
  * Reversal "testing major daily/weekly supply/demand zones".
  * Pullback "volume dries up" is measured as mean flag volume < 0.8x the prior 20-bar mean.
Interpretation choices (the document is silent or ambiguous) are the constants below.
"""

from dataclasses import dataclass
from datetime import time as dtime

import numpy as np
import pandas as pd

SETUPS = ("orb", "pullback", "reversal")


@dataclass(frozen=True)
class InstrumentProfile:
    name: str
    pip_size: float
    open_tz: str          # timezone whose local clock defines the session open
    open_time: dtime      # session open that anchors VWAP and the opening range


PROFILES = {
    # London open (EUR/USD) / New York open (USD/JPY), as specified.
    "EUR_USD": InstrumentProfile("EUR_USD", 0.0001, "Europe/London", dtime(8, 0)),
    "USD_JPY": InstrumentProfile("USD_JPY", 0.01, "America/New_York", dtime(9, 30)),
}

OR_MINUTES = 30              # opening range length
ORB_WINDOW_MINUTES = 60      # breakout bar must CLOSE within this long after the open
VOL_MULT, VOL_AVG_BARS = 1.5, 10          # "1.5x greater than the 10-candle average"
MARUBOZU_BODY_FRAC = 0.80
ENGULF_BODY_FRAC = 0.60
VWAP_SLOPE_BARS = 4          # slope measured over one hour of 15m bars
MIN_SESSION_BARS = 4         # VWAP-dependent setups need an hour of session before the VWAP means anything
VWAP_MIN_SLOPE_PIPS = 3.0    # "distinct angle": VWAP moved at least this over VWAP_SLOPE_BARS
FLAG_BARS = 8                # the pullback / flag lookback (matches cfg.flag_channel_bars)
VWAP_TEST_TOL_PIPS = 3.0
STRETCH_Z = 2.0              # reversal "rubber band": |close-vwap| z-score
STRETCH_LOOKBACK = 30
REVERSAL_CONFIRM_BARS = 5    # pattern break and VWAP cross must be this close together
STRUCTURE_BARS = 30          # reversal structure extreme lookback
STOP_BUFFER_PIPS = 1.0
MIN_SL_PIPS = 5.0
MAX_SL_PIPS = {"orb": 40.0, "pullback": 40.0, "reversal": 60.0}
RR = 2.0


def session_key(df, prof):
    local = df.index.tz_convert(prof.open_tz)
    return pd.Series((local - pd.Timedelta(hours=prof.open_time.hour, minutes=prof.open_time.minute)).date,
                     index=df.index)


def anchored_vwap(df, prof, key=None):
    """VWAP reset at each local session open."""
    key = session_key(df, prof) if key is None else key
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = (tp * df["Volume"]).groupby(key).cumsum()
    vv = df["Volume"].groupby(key).cumsum()
    return pv / vv.replace(0, np.nan)


def _within(s, n):
    """True if s was True on any of the last n bars (including this one)."""
    return s.astype(float).rolling(n, min_periods=1).max() > 0


def _base(df, prof):
    o, h, l, c, v = (df[k] for k in ("Open", "High", "Low", "Close", "Volume"))
    pip = prof.pip_size
    x = pd.DataFrame(index=df.index)
    key = session_key(df, prof)
    x["vwap"] = anchored_vwap(df, prof, key)
    # Slope is measured inside the session only (never against the previous session's VWAP).
    ref = x["vwap"].groupby(key).shift(VWAP_SLOPE_BARS).fillna(x["vwap"].groupby(key).transform("first"))
    x["vwap_move_pips"] = (x["vwap"] - ref) / pip
    x["session_age"] = x["vwap"].groupby(key).cumcount()          # 0 on the opening bar
    rng = (h - l).replace(0, np.nan)
    x["body_frac"] = (c - o).abs() / rng
    x["vol_surge"] = v > VOL_MULT * v.rolling(VOL_AVG_BARS).mean().shift(1)
    x["bull_marubozu"] = (c > o) & (x["body_frac"] >= MARUBOZU_BODY_FRAC)
    x["bear_marubozu"] = (c < o) & (x["body_frac"] >= MARUBOZU_BODY_FRAC)
    po, pc = o.shift(1), c.shift(1)
    x["bull_engulf_strong"] = (c > o) & (pc < po) & (c > po) & (o < pc) & (x["body_frac"] >= ENGULF_BODY_FRAC)
    x["bear_engulf_strong"] = (c < o) & (pc > po) & (c < po) & (o > pc) & (x["body_frac"] >= ENGULF_BODY_FRAC)
    # Morning / evening star: big move bar, small-bodied middle bar, third bar retraces past the first's midpoint.
    body = (c - o).abs()
    big1 = body.shift(2) >= 0.6 * rng.shift(2)
    small2 = body.shift(1) <= 0.3 * rng.shift(1)
    mid1 = (o.shift(2) + c.shift(2)) / 2
    x["morning_star"] = big1 & (c.shift(2) < o.shift(2)) & small2 & (c > o) & (c > mid1)
    x["evening_star"] = big1 & (c.shift(2) > o.shift(2)) & small2 & (c < o) & (c < mid1)
    return x, (o, h, l, c, v)


def _bool(df, col):
    return df[col].fillna(False).astype(bool) if col in df else pd.Series(False, index=df.index)


def _grade(df, col):
    return df[col].fillna(0) if col in df else pd.Series(0, index=df.index)


def _finish(df, prof, setup, items_long, items_short, sl_long, sl_short, extra=None):
    """items_* : dict name -> bool Series. Builds the signal frame + near-miss flags."""
    pip = prof.pip_size
    n_long = sum(v.astype(int) for v in items_long.values())
    n_short = sum(v.astype(int) for v in items_short.values())
    need_l, need_s = len(items_long), len(items_short)
    c = df["Close"]
    sl_long_p, sl_short_p = (c - sl_long) / pip, (sl_short - c) / pip
    ok_l = (sl_long_p >= MIN_SL_PIPS) & (sl_long_p <= MAX_SL_PIPS[setup])
    ok_s = (sl_short_p >= MIN_SL_PIPS) & (sl_short_p <= MAX_SL_PIPS[setup])
    long_full, short_full = (n_long == need_l), (n_short == need_s)
    out = pd.DataFrame(index=df.index)
    out["long"] = long_full & ok_l & ~(short_full & ok_s)
    out["short"] = short_full & ok_s & ~(long_full & ok_l)
    out["stop_price"] = np.where(out["long"], sl_long, np.where(out["short"], sl_short, np.nan))
    out["sl_pips"] = np.where(out["long"], sl_long_p, np.where(out["short"], sl_short_p, np.nan)).round(1)
    out["near_miss"] = ((n_long == need_l - 1) | (n_short == need_s - 1)) & ~(out["long"] | out["short"])
    out["skipped_stop_distance"] = (long_full & ~ok_l) | (short_full & ~ok_s)
    out["setup"] = setup
    for k in items_long:
        out[f"L_{k}"] = items_long[k]
    for k in items_short:
        out[f"S_{k}"] = items_short[k]
    return out


# --------------------------------------------------------------------------
# Setup 1: ORB
# --------------------------------------------------------------------------

def orb_signals(df, prof):
    x, (o, h, l, c, v) = _base(df, prof)
    pip = prof.pip_size
    local = df.index.tz_convert(prof.open_tz)
    mins = pd.Series(local.hour * 60 + local.minute, index=df.index)
    open_min = prof.open_time.hour * 60 + prof.open_time.minute
    day = pd.Series(local.date, index=df.index)

    in_or = (mins >= open_min) & (mins < open_min + OR_MINUTES)
    or_hi = h.where(in_or).groupby(day).transform("max")
    or_lo = l.where(in_or).groupby(day).transform("min")
    or_bars = in_or.groupby(day).transform("sum")

    daily = pd.DataFrame({"h": h.groupby(day).max(), "l": l.groupby(day).min()})
    adr = ((daily["h"] - daily["l"]).shift(1).rolling(20, min_periods=5).mean())
    adr_bar = day.map(adr)
    valid = (or_bars >= OR_MINUTES // 15) & ((or_hi - or_lo) <= adr_bar)

    bar_close_min = mins + 15
    window = valid & (mins >= open_min + OR_MINUTES) & (bar_close_min <= open_min + ORB_WINDOW_MINUTES)
    up, dn = window & (c > or_hi), window & (c < or_lo)
    first_up = up & (up.groupby(day).cumsum() == 1) & ~(dn.groupby(day).cumsum() > 0)
    first_dn = dn & (dn.groupby(day).cumsum() == 1) & ~(up.groupby(day).cumsum() > 0)

    hvn_origin = _within(_bool(df, "near_hvn") | _bool(df, "near_poc") | _bool(df, "at_vp_level"), 4)
    long_items = {
        "range_breakout": first_up,
        "vwap": (c > x["vwap"]) & (x["vwap_move_pips"] > 0),
        "vol_profile": hvn_origin,
        "volume": x["vol_surge"],
        "candle": x["bull_marubozu"] | x["bull_engulf_strong"],
    }
    short_items = {
        "range_breakout": first_dn,
        "vwap": (c < x["vwap"]) & (x["vwap_move_pips"] < 0),
        "vol_profile": hvn_origin,
        "volume": x["vol_surge"],
        "candle": x["bear_marubozu"] | x["bear_engulf_strong"],
    }
    buf = STOP_BUFFER_PIPS * pip
    return _finish(df, prof, "orb", long_items, short_items, or_lo - buf, or_hi + buf)


# --------------------------------------------------------------------------
# Setup 2: Pullback / continuation
# --------------------------------------------------------------------------

def pullback_signals(df, prof):
    x, (o, h, l, c, v) = _base(df, prof)
    pip = prof.pip_size
    tol = VWAP_TEST_TOL_PIPS * pip
    flag_l, flag_s = _grade(df, "flag_grade_long") >= 1, _grade(df, "flag_grade_short") >= 1

    tested_long = _within((l <= x["vwap"] + tol) & (h >= x["vwap"] - tol), FLAG_BARS)
    tested_short = tested_long
    flag_vol = v.rolling(FLAG_BARS).mean().shift(1)
    dries_up = flag_vol < 0.8 * v.rolling(20).mean().shift(FLAG_BARS + 1)
    hvn_zone = _within(_bool(df, "near_hvn") | _bool(df, "at_vp_level"), FLAG_BARS)
    rej_long = _within(_bool(df, "hammer") | _bool(df, "bullish_reversal_candle"), FLAG_BARS)
    rej_short = _within(_bool(df, "shooting_star") | _bool(df, "bearish_reversal_candle"), FLAG_BARS)

    formed = x["session_age"] >= MIN_SESSION_BARS
    long_items = {
        "trend": formed & (x["vwap_move_pips"] >= VWAP_MIN_SLOPE_PIPS) & (c > x["vwap"]),
        "flag": flag_l,
        "vwap_test": tested_long,
        "vol_profile": hvn_zone & dries_up,
        "volume": x["vol_surge"],
        "candle": rej_long,
    }
    short_items = {
        "trend": formed & (x["vwap_move_pips"] <= -VWAP_MIN_SLOPE_PIPS) & (c < x["vwap"]),
        "flag": flag_s,
        "vwap_test": tested_short,
        "vol_profile": hvn_zone & dries_up,
        "volume": x["vol_surge"],
        "candle": rej_short,
    }
    buf = STOP_BUFFER_PIPS * pip
    swing_low = l.rolling(FLAG_BARS).min() - buf
    swing_high = h.rolling(FLAG_BARS).max() + buf
    return _finish(df, prof, "pullback", long_items, short_items, swing_low, swing_high)


# --------------------------------------------------------------------------
# Setup 3: Structural reversal
# --------------------------------------------------------------------------

def reversal_signals(df, prof):
    x, (o, h, l, c, v) = _base(df, prof)
    pip = prof.pip_size
    dev = c - x["vwap"]
    z = dev / dev.rolling(96, min_periods=30).std()
    stretched_up = _within(z >= STRETCH_Z, STRETCH_LOOKBACK)      # stretched above VWAP -> short reversal
    stretched_dn = _within(z <= -STRETCH_Z, STRETCH_LOOKBACK)

    pat_short = (_grade(df, "double_top_grade_short") >= 2) | (_grade(df, "hs_grade_short") >= 2)
    pat_long = (_grade(df, "double_bottom_grade_long") >= 2) | (_grade(df, "inverse_hs_grade_long") >= 2)
    # grade >= 2 requires the lower-volume-on-the-final-extreme condition (volume divergence) per the engine's rubric.

    cross_dn = (c < x["vwap"]) & (c.shift(1) >= x["vwap"].shift(1))
    cross_up = (c > x["vwap"]) & (c.shift(1) <= x["vwap"].shift(1))
    n = REVERSAL_CONFIRM_BARS
    formed = x["session_age"] >= MIN_SESSION_BARS
    cross_dn, cross_up = cross_dn & formed, cross_up & formed
    short_trigger = (_within(pat_short, n) & _within(cross_dn, n) & (pat_short | cross_dn) & (c < x["vwap"]))
    long_trigger = (_within(pat_long, n) & _within(cross_up, n) & (pat_long | cross_up) & (c > x["vwap"]))

    edge_short = _within(_bool(df, "near_vah"), STRUCTURE_BARS)
    edge_long = _within(_bool(df, "near_val"), STRUCTURE_BARS)
    star_short = _within(x["evening_star"] | x["bear_engulf_strong"], n)
    star_long = _within(x["morning_star"] | x["bull_engulf_strong"], n)

    short_items = {"stretched": stretched_up, "pattern_and_vwap_cross": short_trigger,
                   "value_area_edge": edge_short, "volume": _within(x["vol_surge"], n), "candle": star_short}
    long_items = {"stretched": stretched_dn, "pattern_and_vwap_cross": long_trigger,
                  "value_area_edge": edge_long, "volume": _within(x["vol_surge"], n), "candle": star_long}
    buf = STOP_BUFFER_PIPS * pip
    peak = h.rolling(STRUCTURE_BARS).max() + buf
    trough = l.rolling(STRUCTURE_BARS).min() - buf
    return _finish(df, prof, "reversal", long_items, short_items, trough, peak)


_FUNCS = {"orb": orb_signals, "pullback": pullback_signals, "reversal": reversal_signals}


def compute_setup_signals(df_ind, instrument, setup):
    return _FUNCS[setup](df_ind, PROFILES[instrument])


def latest_setup_signals(df_ind, instrument):
    """Evaluate all three setups on the last closed bar. Returns (signals, near_misses)
    where signals is a list of dicts (direction, setup, stop_price, sl_pips, items) -
    several setups can fire on the same bar; the runner takes the first in SETUPS order."""
    hits, near = [], []
    for s in SETUPS:
        row = compute_setup_signals(df_ind, instrument, s).iloc[-1]
        if row["long"] or row["short"]:
            side = "L" if row["long"] else "S"
            hits.append({
                "setup": s, "direction": "long" if row["long"] else "short",
                "stop_price": float(row["stop_price"]), "sl_pips": float(row["sl_pips"]),
                "items": {k[2:]: bool(row[k]) for k in row.index if k.startswith(side + "_")},
            })
        elif row["near_miss"]:
            near.append({"setup": s})
        elif row["skipped_stop_distance"]:
            near.append({"setup": s, "skipped": "stop_distance_out_of_range"})
    return hits, near

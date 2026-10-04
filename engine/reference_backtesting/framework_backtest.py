"""
framework_backtest.py

Backtest of the "Confluence Trading Framework" document (VWAP(20)/VWAP(50) trend,
pullback to VWAP(20), Volume Profile support, volume > 1.5x MA, candle trigger,
fixed SL/TP per mode, breakeven + trail, reverse-signal exit, 1% risk sizing)
so it can be compared with the live Book 2 engine on identical data, costs and
metrics.

This implements the document's RULES (its "Entry signals" / "Exit rules" text),
not its Python snippet. The snippet differs from the text: it has no Volume
Profile check, no volume-into-close check, no pin/engulfing test, no trailing or
reverse-signal exit, takes its stop from the candle extreme rather than the
fixed SL, and computes stop_loss_pips as (price diff * 10000 / 10), which is 1/10
of the real pip distance and would size positions ~10x too large.

Interpretation choices (the document is silent or ambiguous; each is a flag or
constant below so it can be changed and re-run):
  * VWAP(N) = rolling N-bar VWAP of typical price (no session anchor).
  * "Pulled back to VWAP(20)": bar low <= VWAP(20) for longs (high >= for shorts),
    while the close is back on the right side of it.
  * Volume Profile: rolling VP over VP_LOOKBACK bars, VP_BINS bins. "Near HVN" =
    within VP_TOL_PIPS of the centre of a top-quartile-volume bin; "rejected at
    POC" = within VP_TOL_PIPS of the POC. --no-vp disables it to show its effect.
  * "Volume increases into the close": OHLCV cannot see intrabar volume, so the
    proxy is bar volume > previous bar volume.
  * Candle: bullish engulfing (close > prior open, bullish bar) OR pin bar
    (lower wick >= body, close in top quarter), AND close in top half of range
    and close > open. Shorts mirror.
  * Entry at signal-bar close. A bar touching both stop and target counts as the
    stop (same convention as backtest_book2.py).
  * Trailing: once best price is +0.5R, stop = max(entry, best - trail).

Usage:
    python3 framework_backtest.py --csv eur_usd_15m_2013-01_to_2023-09.csv --csv eur_usd_15m_2023-10_to_2026-10.csv \
        --mode scalp --cost-pips 1.0 --out-json framework_scalp.json
"""

import argparse
import json
import math

import numpy as np
import pandas as pd

PIP = 0.0001
PIP_VALUE_PER_LOT = 10.0
MODES = {
    # timeframe, TP pips, SL pips, trail pips
    "scalp": {"rule": "15min", "tp": 20.0, "sl": 10.0, "trail": 10.0},
    "swing": {"rule": "1h", "tp": 75.0, "sl": 30.0, "trail": 20.0},
}
VWAP_FAST, VWAP_SLOW, VOL_MA, VOL_MULT = 20, 50, 20, 1.5
VP_LOOKBACK, VP_BINS, VP_TOL_PIPS = 100, 24, 3.0
BE_TRIGGER_R = 0.5


def load(paths):
    frames = []
    for p in paths:
        d = pd.read_csv(p)
        d = d.rename(columns={d.columns[0]: "time"})
        d["time"] = pd.to_datetime(d["time"], utc=True)
        frames.append(d.set_index("time"))
    df = pd.concat(frames).sort_index()
    return df[~df.index.duplicated(keep="last")]


def resample(df, rule):
    if rule == "15min":
        return df
    return df.resample(rule).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna()


def rolling_vwap(df, n):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    return (tp * df["Volume"]).rolling(n).sum() / df["Volume"].rolling(n).sum()


def volume_profile_flags(df, use_vp):
    """near_support (HVN or POC) / near_resistance flags per bar."""
    n = len(df)
    if not use_vp:
        ones = np.ones(n, dtype=bool)
        return ones, ones
    close = df["Close"].to_numpy()
    high, low = df["High"].to_numpy(), df["Low"].to_numpy()
    vol = df["Volume"].to_numpy().astype(float)
    tp = (high + low + close) / 3
    near = np.zeros(n, dtype=bool)
    tol = VP_TOL_PIPS * PIP
    for i in range(VP_LOOKBACK, n):
        t, v = tp[i - VP_LOOKBACK:i], vol[i - VP_LOOKBACK:i]   # profile excludes the signal bar
        lo, hi = t.min(), t.max()
        if hi <= lo:
            continue
        hist, edges = np.histogram(t, bins=VP_BINS, range=(lo, hi), weights=v)
        centres = (edges[:-1] + edges[1:]) / 2
        poc = centres[hist.argmax()]
        hvn = centres[hist >= np.quantile(hist, 0.75)]
        levels = np.append(hvn, poc)
        near[i] = np.abs(levels - close[i]).min() <= tol
    # Same node test serves both sides: a node under a long entry is support, over a short entry resistance.
    return near, near


def signals(df, use_vp):
    o, h, l, c, v = (df[k] for k in ("Open", "High", "Low", "Close", "Volume"))
    vw20, vw50 = rolling_vwap(df, VWAP_FAST), rolling_vwap(df, VWAP_SLOW)
    vol_ok = (v > VOL_MULT * v.rolling(VOL_MA).mean()) & (v > v.shift(1))
    rng = (h - l).replace(0, np.nan)
    body = (c - o).abs()
    pos = (c - l) / rng                                  # 0 = close at low, 1 = close at high
    prev_o, prev_c = o.shift(1), c.shift(1)

    bull_engulf = (c > prev_o) & (c > o) & (prev_c < prev_o)
    bull_pin = ((np.minimum(o, c) - l) >= body) & (pos >= 0.75)
    bear_engulf = (c < prev_o) & (c < o) & (prev_c > prev_o)
    bear_pin = ((h - np.maximum(o, c)) >= body) & (pos <= 0.25)

    sup, res = volume_profile_flags(df, use_vp)
    long_sig = ((vw20 > vw50) & (l <= vw20) & (c >= vw20) & vol_ok & sup
                & (bull_engulf | bull_pin) & (pos >= 0.5) & (c > o))
    short_sig = ((vw20 < vw50) & (h >= vw20) & (c <= vw20) & vol_ok & res
                 & (bear_engulf | bear_pin) & (pos <= 0.5) & (c < o))
    return long_sig.fillna(False).to_numpy(), short_sig.fillna(False).to_numpy()


def simulate(df, mode, cost_pips, starting_equity, use_vp=True, use_trail=True, use_reverse=True):
    m = MODES[mode]
    long_sig, short_sig = signals(df, use_vp)
    idx = df.index
    O, H, L, C = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    sl, tp, trail = m["sl"] * PIP, m["tp"] * PIP, m["trail"] * PIP

    equity, trades, pos = starting_equity, [], None

    def close_trade(i, price, reason):
        nonlocal equity, pos
        pips = (price - pos["entry"]) / PIP * (1 if pos["dir"] == "long" else -1) - cost_pips
        pnl = pips * PIP_VALUE_PER_LOT * pos["lots"]
        equity += pnl
        trades.append({
            "entry_time": pos["time"].isoformat(), "exit_time": idx[i].isoformat(), "direction": pos["dir"],
            "pips": round(pips, 1), "pnl": round(pnl, 2), "equity_after": round(equity, 2),
            "r_multiple": round(pips / m["sl"], 2), "exit_reason": reason,
        })
        pos = None

    for i in range(len(df)):
        if pos is not None:
            long_ = pos["dir"] == "long"
            stop = pos["stop"]
            if (L[i] <= stop) if long_ else (H[i] >= stop):
                close_trade(i, stop, pos["stop_reason"])
            elif (H[i] >= pos["target"]) if long_ else (L[i] <= pos["target"]):
                close_trade(i, pos["target"], "target")
            else:
                if use_trail:
                    pos["best"] = max(pos["best"], H[i]) if long_ else min(pos["best"], L[i])
                    move = (pos["best"] - pos["entry"]) if long_ else (pos["entry"] - pos["best"])
                    if move + 1e-9 >= BE_TRIGGER_R * sl:
                        new = max(pos["entry"], pos["best"] - trail) if long_ else min(pos["entry"], pos["best"] + trail)
                        if (long_ and new > pos["stop"]) or (not long_ and new < pos["stop"]):
                            pos["stop"], pos["stop_reason"] = new, "trail"
                            if (long_ and L[i] <= new) or (not long_ and H[i] >= new):
                                close_trade(i, new, "trail")
                if pos is not None and use_reverse and ((long_ and short_sig[i]) or (not long_ and long_sig[i])):
                    close_trade(i, C[i], "reverse_signal")

        if pos is None and (long_sig[i] or short_sig[i]) and not (long_sig[i] and short_sig[i]):
            d = "long" if long_sig[i] else "short"
            lots = equity * 0.01 / (m["sl"] * PIP_VALUE_PER_LOT)
            e = C[i]
            pos = {"dir": d, "entry": e, "time": idx[i], "lots": lots, "best": e, "stop_reason": "stop",
                   "stop": e - sl if d == "long" else e + sl,
                   "target": e + tp if d == "long" else e - tp}
    return trades, equity


def side(trades):
    if not trades:
        return {"n": 0, "win_pct": None, "pf": None, "avg_r": None}
    w = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    lo = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    return {"n": len(trades), "win_pct": round(100 * sum(t["pnl"] > 0 for t in trades) / len(trades), 1),
            "pf": round(w / lo, 2) if lo else None,
            "avg_r": round(float(np.mean([t["r_multiple"] for t in trades])), 3)}


def r_pf(trades):
    w = sum(t["r_multiple"] for t in trades if t["r_multiple"] > 0)
    lo = abs(sum(t["r_multiple"] for t in trades if t["r_multiple"] < 0))
    return round(w / lo, 2) if lo else None


def sharpe_daily(trades, start):
    if len(trades) < 2:
        return None
    s = pd.Series({pd.Timestamp(t["exit_time"]).normalize(): t["equity_after"] for t in trades})
    s = s[~s.index.duplicated(keep="last")]
    days = pd.date_range(s.index.min(), s.index.max(), freq="D", tz=s.index.tz)
    eq = s.reindex(days).ffill()
    rets = eq.pct_change().dropna()
    return round(float(rets.mean() / rets.std() * math.sqrt(365)), 2) if rets.std() > 0 else None


def summarise(trades, final_equity, start):
    eq = [start] + [t["equity_after"] for t in trades]
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak)
    by_year = {}
    for t in trades:
        by_year.setdefault(t["entry_time"][:4], []).append(t)
    return {
        "all": side(trades), "long": side([t for t in trades if t["direction"] == "long"]),
        "short": side([t for t in trades if t["direction"] == "short"]),
        "r_profit_factor": r_pf(trades), "sharpe_daily_ann": sharpe_daily(trades, start),
        "return_pct": round((final_equity / start - 1) * 100, 1), "max_dd_pct": round(mdd * 100, 1),
        "per_year_pf": {y: side(t)["pf"] for y, t in sorted(by_year.items())},
        "per_year_n": {y: len(t) for y, t in sorted(by_year.items())},
        "exit_reasons": pd.Series([t["exit_reason"] for t in trades]).value_counts().to_dict() if trades else {},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", action="append", required=True, help="15m OHLCV csv; repeat to concatenate")
    ap.add_argument("--mode", choices=sorted(MODES), required=True)
    ap.add_argument("--cost-pips", type=float, default=1.0)
    ap.add_argument("--starting-equity", type=float, default=10000.0)
    ap.add_argument("--since", default=None, help="only trade from this date (e.g. 2025-10-01)")
    ap.add_argument("--no-vp", action="store_true")
    ap.add_argument("--no-trail", action="store_true")
    ap.add_argument("--no-reverse", action="store_true")
    ap.add_argument("--out-json", default=None)
    a = ap.parse_args()

    df = resample(load(a.csv), MODES[a.mode]["rule"])
    if a.since:
        df = df[df.index >= pd.Timestamp(a.since, tz="UTC") - pd.Timedelta(days=30)]  # keep warmup
    trades, final = simulate(df, a.mode, a.cost_pips, a.starting_equity, not a.no_vp, not a.no_trail, not a.no_reverse)
    if a.since:
        trades = [t for t in trades if t["entry_time"] >= a.since]
    res = summarise(trades, final, a.starting_equity)
    res["config"] = vars(a)
    print(json.dumps({k: v for k, v in res.items() if k != "config"}, indent=1))
    if a.out_json:
        json.dump({**res, "trades": trades}, open(a.out_json, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()

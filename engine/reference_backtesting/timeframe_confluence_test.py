"""
timeframe_confluence_test.py

PRE-REGISTERED test of the confluence theory across 5-, 15- and 30-minute bars (rules fixed before any
result was seen). ORB is excluded for now.

THEORY    Entries work best where several of these line up: volume profile, VWAP, volume, price action
          (candle signal) and chart pattern. Find the sweet spot, and see whether a different timeframe
          helps.

DESIGN    Same bar-level framework as pillar_test.py. Every bar of 14 years (2013-01 .. 2026-10) is scored
          for both directions with the engine's own score_row_book2, and each (bar, direction) is a
          hypothetical entry at the bar close with a 1:2 stop/target. Edge = the entry's R minus the mean R of
          entries in the same year, direction and hour, so cost, drift and time of day cancel. Intervals
          resample whole calendar weeks. Blocked longs (three-bar distribution rule) are excluded.

TIMEFRAMES  5m and 30m are scaled from the 15m engine so each means the same thing in clock time:
              * windows measured in time x (15 / tf):  2,000-bar trend average, 480-bar volume profile,
                400-bar outcome horizon
              * distances measured in pips x sqrt(tf / 15), since price moves scale with the square root of
                time:  15-pip stop (5m 8.7, 15m 15.0, 30m 21.2), pattern and tolerance thresholds in pips
              * everything defined by bar shapes is unchanged (3-bar rules, flag and pattern bar counts, the
                0..10 score and its threshold of 7)
            30m bars are the 15m bars combined (verified identical to Oanda's own 30m candles).

PILLAR FLAGS (direction-aware, active when):
            chart pattern >0 | candle >0 | volume == 2 (a climax, absorption or churn bar) | VWAP >0 | volume
            profile >0.  Confluence count k = number of active flags (0..5); a pattern = the exact set.

HYPOTHESES  Bonferroni over 15 tests (alpha = 0.05 / 15 = 0.0033, two-sided 99.67% interval).
  T1  The engine's signals (final score >= 7, best direction) have edge > 0, in each timeframe (3 tests).
  T2  Edge rises with confluence count k (slope > 0), in each timeframe (3 tests).
  T3  Sweet spot (9 tests): per timeframe, the 3 patterns with the highest edge on the TRAIN half
      (2013-2019, at least 150 entries) keep a positive edge on the TEST half (2020-2026). The mirror run
      (train 2020-2026, test 2013-2019) is reported as a robustness check.
  T4  Timeframe: descriptive only. Reported: signals per year, and expectancy after 1- and 2-pip costs.

VERDICT   SUPPORTED: the 99.67% interval excludes zero in the expected direction AND (T1, T2) both halves have
          the expected sign.  WEAK: only the 95% interval excludes zero.  Otherwise NOT SUPPORTED.
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--tf", type=int, choices=[5, 15, 30])
ap.add_argument("--csv5", default="eur_usd_5m_2013-01_to_2026-10.csv")
ap.add_argument("--csv15-old", default="eur_usd_15m_2013-01_to_2023-09.csv")
ap.add_argument("--csv15-new", default="eur_usd_15m_2023-10_to_2026-10.csv")
ap.add_argument("--out-dir", default=".")
ap.add_argument("--summary", action="store_true", help="print the cross-timeframe comparison from saved results")
args = ap.parse_args()

NAMES = ["chart", "candle", "volume2", "vwap", "vprofile"]
Z95, Z997 = 1.959964, 2.935
B = 3000
RR, COSTS = 2.0, (1.0, 2.0)


def pattern_name(p):
    return "+".join(n for i, n in enumerate(NAMES) if p >> i & 1) or "(none)"


def verdict_text(lo95, hi95, lo997, hi997, halves=None, positive=True):
    s = 1 if positive else -1
    ok997 = lo997 > 0 if positive else hi997 < 0
    ok95 = lo95 > 0 if positive else hi95 < 0
    both = True if halves is None else all(s * h > 0 for h in halves)
    return "SUPPORTED" if (ok997 and both) else ("WEAK" if ok95 else "NOT SUPPORTED")


# ====================================================================== summary mode
if args.summary:
    res = {tf: json.load(open(os.path.join(args.out_dir, f"tf{tf}_results.json"))) for tf in (5, 15, 30)}
    print("\n================ CROSS-TIMEFRAME SUMMARY (14 years, 1:2 exit) ================")
    print(f"{'':<34}" + "".join(f"{str(tf) + 'm':>16}" for tf in res))
    rows = [
        ("stop / target (pips)", lambda r: f"{r['stop_pips']}/{r['stop_pips'] * RR:.1f}"),
        ("hypothetical entries (resolved)", lambda r: f"{r['n_entries']:,}"),
        ("engine signals (score>=7)", lambda r: f"{r['signals']['n']}"),
        ("  signals per year", lambda r: f"{r['signals']['per_year']:.0f}"),
        ("  win rate %", lambda r: f"{r['signals']['win']:.1f}"),
        ("  edge vs random (R/entry)", lambda r: f"{r['signals']['edge']['est']:+.3f}"),
        ("  95% interval", lambda r: f"[{r['signals']['edge']['lo95']:+.2f},{r['signals']['edge']['hi95']:+.2f}]"),
        ("  mean R after 1-pip cost", lambda r: f"{r['signals']['meanR']['1.0']:+.3f}"),
        ("  mean R after 2-pip cost", lambda r: f"{r['signals']['meanR']['2.0']:+.3f}"),
        ("  cost of 1 pip in R", lambda r: f"{1.0 / r['stop_pips']:.3f}"),
        ("T1 signals beat random", lambda r: r["verdicts"]["T1"][0]),
        ("T2 edge rises with k", lambda r: r["verdicts"]["T2"][0]),
        ("  slope per extra pillar (R)", lambda r: f"{r['k_slope']['pooled'][0]:+.4f}"),
    ]
    for label, fn in rows:
        print(f"{label:<34}" + "".join(f"{fn(res[tf]):>16}" for tf in res))
    print("\nT3 sweet-spot candidates (selected on 2013-2019, tested on 2020-2026):")
    for tf, r in res.items():
        for c in r["t3"]["primary"]:
            print(f"  {tf:>2}m {c['name']:<34} train edge {c['train_edge']:+.3f} (n={c['n_train']})  ->  test edge {c['test']['est']:+.3f} "
                  f"95% [{c['test']['lo95']:+.3f},{c['test']['hi95']:+.3f}] (n={c['n_test']})  {c['verdict']}")
    print("T3 mirror check (selected on 2020-2026, tested on 2013-2019):")
    for tf, r in res.items():
        for c in r["t3"]["mirror"]:
            print(f"  {tf:>2}m {c['name']:<34} train edge {c['train_edge']:+.3f} (n={c['n_train']})  ->  test edge {c['test']['est']:+.3f} "
                  f"95% [{c['test']['lo95']:+.3f},{c['test']['hi95']:+.3f}] (n={c['n_test']})  {c['verdict']}")
    sys.exit(0)

# ====================================================================== per-timeframe run
from confluence_engine_book2 import Book2Config, compute_indicators_book2, score_row_book2  # noqa: E402
from backtest_confluence import load_data  # noqa: E402

TF = args.tf
f_time, f_vol = 15.0 / TF, (TF / 15.0) ** 0.5
cfg = Book2Config(
    mtf_bias_lookback=round(2000 * f_time), vp_lookback_bars=round(480 * f_time),
    stop_pips=round(15.0 * f_vol, 1), poc_tolerance_pips=3.0 * f_vol, dead_zone_edge_tolerance_pips=5.0 * f_vol,
    double_pattern_tolerance_pips=4.0 * f_vol, flag_pole_min_pips=15.0 * f_vol, ob_impulse_min_pips=12.0 * f_vol)
P, STOP_P = cfg.pip_size, cfg.stop_pips
HORIZON, WARMUP = round(400 * f_time), int(cfg.mtf_bias_lookback * 1.25)
rng = np.random.default_rng(7)
T0 = time.time()


def log(m):
    print(f"[{time.time() - T0:5.0f}s] [{TF}m] {m}", flush=True)


def load_bars():
    if TF == 5:
        return load_data(args.csv5)
    d15 = pd.concat([load_data(args.csv15_old), load_data(args.csv15_new)]).sort_index()
    d15 = d15[~d15.index.duplicated()]
    if TF == 15:
        return d15
    g = d15.resample("30min", label="left", closed="left")
    d30 = g.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
    return d30[g["Close"].count() >= 2]


df = load_bars()
log(f"{len(df)} bars {df.index.min()} -> {df.index.max()} | stop {STOP_P} pips, mtf {cfg.mtf_bias_lookback}, vp {cfg.vp_lookback_bars}, horizon {HORIZON}")
ind = compute_indicators_book2(df, cfg)
log("indicators done")

need = ["chart_pattern_grade_long", "chart_pattern_grade_short", "bullish_reversal_candle", "ob_fvg_combined_long",
        "bearish_reversal_candle", "ob_fvg_combined_short", "rvol", "vol_climax", "vol_absorption", "vol_churn",
        "above_vwap", "below_vwap", "vwap_slope", "vwap_reclaim", "vwap_failure_break", "at_vp_level", "near_hvn",
        "mtf_bias_known", "mtf_bias_up", "vol_distribution_3bar"]
sub = ind[[c for c in need if c in ind.columns]].copy()
for c in sub.columns:
    if sub[c].dtype == object or sub[c].dtype == bool:
        sub[c] = sub[c].fillna(False).astype(bool)
n = len(sub)
comp = np.zeros((n, 2, 7), dtype=np.int8)
blocked = np.zeros(n, dtype=bool)
CH = 50000
for a in range(WARMUP, n, CH):
    for j, r in enumerate(sub.iloc[a:a + CH].to_dict("records")):
        i = a + j
        blocked[i] = bool(r.get("vol_distribution_3bar", False))
        for d, name in enumerate(("long", "short")):
            _, c = score_row_book2(r, name, cfg)
            comp[i, d] = (c["chart_pattern"], c["candle"], c["volume"], c["vwap"], c["vol_profile"], c["mtf_penalty"], c["raw_total"])
del sub
log("scores done")

H, L, C = ind["High"].values, ind["Low"].values, ind["Close"].values
Hw, Lw = sliding_window_view(H[1:], HORIZON), sliding_window_view(L[1:], HORIZON)
last = n - HORIZON - 1
idx_all = np.arange(WARMUP, last + 1)


def outcomes(direction):
    out = np.zeros(len(idx_all), dtype=np.int8)
    step = max(2000, int(6_000_000 / HORIZON))
    for a in range(0, len(idx_all), step):
        i0, i1 = idx_all[a], idx_all[min(a + step, len(idx_all)) - 1]
        entry = C[i0:i1 + 1][:, None]
        hw, lw = Hw[i0:i1 + 1], Lw[i0:i1 + 1]
        if direction == 0:
            ht, hs = hw >= entry + RR * STOP_P * P, lw <= entry - STOP_P * P
        else:
            ht, hs = lw <= entry - RR * STOP_P * P, hw >= entry + STOP_P * P
        none = HORIZON + 1
        ti = np.where(ht.any(1), ht.argmax(1), none)
        si = np.where(hs.any(1), hs.argmax(1), none)
        out[a:a + len(ti)] = np.where((ti == none) & (si == none), 0, np.where(si <= ti, -1, 1))
    return out


times = ind.index[idx_all]
frames = []
for d in (0, 1):
    cd = comp[idx_all, d].astype(int)
    frames.append(pd.DataFrame({
        "t": times, "dir": 1 if d == 0 else -1, "chart": cd[:, 0], "candle": cd[:, 1], "volume": cd[:, 2],
        "vwap": cd[:, 3], "vp": cd[:, 4], "pen": cd[:, 5], "raw": cd[:, 6], "outcome": outcomes(d),
        "blocked": (blocked[idx_all] & (d == 0))}))
obs = pd.concat(frames, ignore_index=True)
del frames, comp, Hw, Lw
log(f"outcomes done: {len(obs)} entries, unresolved {(obs.outcome == 0).mean() * 100:.2f}%")
obs["final"] = np.maximum(0, obs.raw - obs.pen)
obs.loc[obs.blocked, "final"] = -1
fl, fs = obs[obs.dir == 1].set_index("t")["final"], obs[obs.dir == -1].set_index("t")["final"]
obs["signal"] = False
obs.loc[obs.dir == 1, "signal"] = ((fl >= cfg.execution_threshold) & (fl >= fs)).values
obs.loc[obs.dir == -1, "signal"] = ((fs >= cfg.execution_threshold) & (fs > fl)).values
obs = obs[(obs.outcome != 0) & ~obs.blocked].copy()


def R_at(outcome, cost):
    return np.where(outcome == 1, (RR * STOP_P - cost) / STOP_P, -(STOP_P + cost) / STOP_P)


obs["R"] = R_at(obs.outcome.values, COSTS[0])
obs["win"] = obs.outcome == 1
obs["year"] = obs.t.dt.year
obs["hour"] = obs.t.dt.hour
obs["half"] = np.where(obs.year <= 2019, "A", "B")
obs["edge"] = obs.R - obs.groupby(["year", "dir", "hour"]).R.transform("mean")
flags = np.column_stack([(obs.chart > 0), (obs.candle > 0), (obs.volume == 2), (obs.vwap > 0), (obs.vp > 0)]).astype(int)
obs["k"] = flags.sum(1)
obs["pat"] = (flags * (1 << np.arange(5))).sum(1)
iso = obs.t.dt.isocalendar()
obs["wk"] = pd.factorize(iso.year.astype(int) * 100 + iso.week.astype(int))[0]
G = obs.wk.max() + 1
years = obs.year.nunique()
log(f"analysis frame: {len(obs)} entries, {G} week clusters, base win {obs.win.mean() * 100:.1f}%, mean R {obs.R.mean():+.3f}")

EDGE, WKS = obs.edge.values, obs.wk.values


def boot(mask_a, mask_b=None):
    def sk(m):
        w = WKS[m]
        return np.bincount(w, weights=EDGE[m], minlength=G), np.bincount(w, minlength=G).astype(float)
    sA, kA = sk(mask_a)
    draws = rng.integers(0, G, size=(B, G))
    mA = sA[draws].sum(1) / np.maximum(kA[draws].sum(1), 1)
    pt = sA.sum() / max(kA.sum(), 1)
    if mask_b is not None:
        sB, kB = sk(mask_b)
        mA = mA - sB[draws].sum(1) / np.maximum(kB[draws].sum(1), 1)
        pt -= sB.sum() / max(kB.sum(), 1)
    lo95, hi95 = np.percentile(mA, [2.5, 97.5])
    lo997, hi997 = np.percentile(mA, [0.167, 99.833])
    return dict(est=float(pt), lo95=float(lo95), hi95=float(hi95), lo997=float(lo997), hi997=float(hi997))


def ols_cluster(X, y, wk):
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ (X.T @ y)
    u = y - X @ beta
    S = np.column_stack([np.bincount(wk, weights=X[:, j] * u, minlength=G) for j in range(X.shape[1])])
    V = XtX_inv @ (S.T @ S) @ XtX_inv * (G / (G - 1))
    return beta, np.sqrt(np.diag(V))


res = dict(tf=TF, stop_pips=STOP_P, n_entries=int(len(obs)), base_win=float(obs.win.mean() * 100), years=int(years))
A_, B_ = (obs.half == "A").values, (obs.half == "B").values

# ------------------------------------------------------------------ engine signals (T1)
sig = obs.signal.values
t1 = boot(sig)
halfs = {h: boot(sig & m)["est"] for h, m in (("A", A_), ("B", B_))}
res["signals"] = dict(n=int(sig.sum()), per_year=float(sig.sum() / years), win=float(obs.win[sig].mean() * 100), edge=t1,
                      halves=halfs, meanR={str(c): float(R_at(obs.outcome.values[sig], c).mean()) for c in COSTS})
v1 = verdict_text(t1["lo95"], t1["hi95"], t1["lo997"], t1["hi997"], list(halfs.values()))
print(f"\n=== {TF}m: ENGINE SIGNALS (score>=7): n={sig.sum()} ({sig.sum() / years:.0f}/yr) win {res['signals']['win']:.1f}% "
      f"edge {t1['est']:+.3f} 95% [{t1['lo95']:+.3f},{t1['hi95']:+.3f}] halves {halfs['A']:+.3f}/{halfs['B']:+.3f} | "
      f"meanR 1pip {res['signals']['meanR']['1.0']:+.3f} 2pip {res['signals']['meanR']['2.0']:+.3f}  -> T1 {v1}")

# ------------------------------------------------------------------ confluence count (T2)
print(f"\n--- {TF}m edge by confluence count k (active pillars)")
print(f"{'k':<4}{'n':>10}{'win%':>7}{'edge':>9}   95% CI")
by_k = {}
for k in range(6):
    m = (obs.k == k).values
    if m.sum() < 200:
        continue
    b = boot(m)
    by_k[k] = dict(n=int(m.sum()), win=float(obs.win[m].mean() * 100), **b)
    print(f"{k:<4}{m.sum():>10}{obs.win[m].mean() * 100:>7.1f}{b['est']:>+9.3f}   [{b['lo95']:+.3f}, {b['hi95']:+.3f}]")
sl = {}
for label, m in (("pooled", np.ones(len(obs), bool)), ("A", A_), ("B", B_)):
    X = np.column_stack([np.ones(m.sum()), obs.k.values[m].astype(float)])
    sl[label] = ols_cluster(X, EDGE[m], WKS[m])
bk, sk_ = sl["pooled"][0][1], sl["pooled"][1][1]
v2 = verdict_text(bk - Z95 * sk_, bk + Z95 * sk_, bk - Z997 * sk_, bk + Z997 * sk_, [sl["A"][0][1], sl["B"][0][1]])
print(f"slope of edge per extra pillar: {bk:+.4f} (se {sk_:.4f}); halves {sl['A'][0][1]:+.4f} / {sl['B'][0][1]:+.4f}  -> T2 {v2}")
res["by_k"] = by_k
res["k_slope"] = {k: [float(v[0][1]), float(v[1][1])] for k, v in sl.items()}

# ------------------------------------------------------------------ sweet spot: select on one half, test on the other (T3)
print(f"\n--- {TF}m edge by exact pillar pattern (point estimates; n = entries)")
print(f"{'pattern':<36}{'nA':>9}{'edgeA':>8}{'nB':>9}{'edgeB':>8}")
g = obs.groupby(["pat", "half"]).edge.agg(["size", "mean"]).unstack("half")
table = {}
for p in range(32):
    nA = int(g[("size", "A")].get(p, 0)) if ("size", "A") in g else 0
    nB = int(g[("size", "B")].get(p, 0)) if ("size", "B") in g else 0
    eA = float(g[("mean", "A")].get(p, np.nan))
    eB = float(g[("mean", "B")].get(p, np.nan))
    table[p] = (nA, eA, nB, eB)
    if nA + nB >= 100:
        print(f"{pattern_name(p):<36}{nA:>9}{eA:>+8.3f}{nB:>9}{eB:>+8.3f}")


def select_and_test(train_half, test_half):
    out = []
    ti, tj = (0, 1) if train_half == "A" else (2, 3)
    ei = (1, 3)[0 if train_half == "A" else 1]
    cands = [(p, v) for p, v in table.items() if v[0 if train_half == "A" else 2] >= 150 and not np.isnan(v[1 if train_half == "A" else 3])]
    cands.sort(key=lambda pv: -pv[1][1 if train_half == "A" else 3])
    for p, v in cands[:3]:
        m_test = (obs.pat == p).values & (obs.half == test_half).values
        b = boot(m_test)
        vd = verdict_text(b["lo95"], b["hi95"], b["lo997"], b["hi997"])
        out.append(dict(pattern=int(p), name=pattern_name(p), n_train=int(v[0 if train_half == "A" else 2]),
                        train_edge=float(v[1 if train_half == "A" else 3]), n_test=int(m_test.sum()), test=b,
                        win_test=float(obs.win[m_test].mean() * 100),
                        meanR_test={str(c): float(R_at(obs.outcome.values[m_test], c).mean()) for c in COSTS}, verdict=vd))
    return out


prim, mirror = select_and_test("A", "B"), select_and_test("B", "A")
res["t3"] = dict(primary=prim, mirror=mirror)
for title, cs in (("selected on 2013-2019, tested on 2020-2026 (T3)", prim), ("MIRROR: selected on 2020-2026, tested on 2013-2019", mirror)):
    print(f"\n--- {TF}m sweet-spot candidates {title}")
    for c in cs:
        print(f"  {c['name']:<34} train edge {c['train_edge']:+.3f} (n={c['n_train']}) -> test edge {c['test']['est']:+.3f} "
              f"95% [{c['test']['lo95']:+.3f},{c['test']['hi95']:+.3f}] 99.67% [{c['test']['lo997']:+.3f},{c['test']['hi997']:+.3f}] "
              f"n={c['n_test']} win {c['win_test']:.1f}% meanR@1pip {c['meanR_test']['1.0']:+.3f}  {c['verdict']}")

res["verdicts"] = {"T1": [v1, f"edge {t1['est']:+.3f}"], "T2": [v2, f"slope {bk:+.4f}"],
                   "T3": [c["verdict"] for c in prim]}
print(f"\n######## {TF}m PRE-REGISTERED VERDICTS: T1 {v1} | T2 {v2} | T3 {[c['verdict'] for c in prim]}")
json.dump(res, open(os.path.join(args.out_dir, f"tf{TF}_results.json"), "w"), indent=1, default=float)
log("DONE")

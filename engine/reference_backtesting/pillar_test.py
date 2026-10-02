"""
PRE-REGISTERED per-pillar test of the Book 2 engine (hypotheses fixed before any result was seen).

QUESTION  Does each scoring pillar, and the 0-10 score built from them, predict which entries win?

DESIGN    Bar-level, not trade-level. Every bar from 2013-01 to 2026-10 (342k bars, 14 years) is scored
          for BOTH directions with the engine's own score_row_book2. Each (bar, direction) is a
          hypothetical entry at the bar close with the live exit: 15-pip stop, 30-pip target, 1-pip
          round-trip cost, stop wins any same-bar tie, 400-bar horizon (unresolved entries dropped).
          Hypothetical entries overlap, so every interval is a bootstrap over calendar weeks.
          Outcome R is measured against a drift-free baseline: the mean R of all entries in the same
          year, direction and hour of day. "Edge" = outcome R minus that baseline (so cost, drift and
          time-of-day effects cancel). A random entry has an expected R of about -0.07 at 1-pip cost.

HYPOTHESES (all one-sided: the engine's designers expect a positive effect)
  P0  Selection works:  the engine's own signals (final score >= 7, best direction, not blocked) have
                        mean edge > 0.
  P1  Score ladder:     edge rises with the final score (slope of edge on score > 0).
  P2  Chart pattern:    coefficient > 0 in the joint regression of edge on the five pillars + MTF flag.
  P3  Candle:           coefficient > 0.
  P4  Volume:           coefficient > 0.
  P5  VWAP:             coefficient > 0.
  P6  Volume profile:   coefficient > 0.
  P7  MTF alignment:    entries WITH the 2,000-bar trend have higher edge than entries against it
                        (the engine's -2 penalty rests on this).
  P8  Distribution rule: longs blocked by the three-bar distribution rule have lower edge than other longs.

VERDICT RULE (nine hypotheses, so Bonferroni: alpha = 0.05 / 9 = 0.0056)
  SUPPORTED     the 99.4% interval excludes zero in the expected direction AND the point estimate has
                the expected sign in BOTH halves (2013-2019, 2020-2026).
  WEAK          the 95% interval excludes zero in the expected direction, but SUPPORTED fails.
  NOT SUPPORTED otherwise.
"""
import argparse, os, sys, json, time
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]
from confluence_engine_book2 import Book2Config, compute_indicators_book2, score_row_book2
from backtest_confluence import load_data

ap = argparse.ArgumentParser(description="Pre-registered per-pillar test (see module docstring).")
ap.add_argument("--old-csv", default="eur_usd_15m_2013-01_to_2023-09.csv")
ap.add_argument("--new-csv", default="eur_usd_15m_2023-10_to_2026-10.csv")
ap.add_argument("--out-dir", default=".")
args = ap.parse_args()
OLD, NEW = args.old_csv, args.new_csv
COST, STOP_P, RR, HORIZON, WARMUP, B = 1.0, 15.0, 2.0, 400, 2500, 3000
cfg = Book2Config()
P = cfg.pip_size
rng = np.random.default_rng(11)
T0 = time.time()
Z95, Z994 = 1.959964, 2.774  # two-sided normal quantiles for 95% and 99.44%


def log(m):
    print(f"[{time.time() - T0:5.0f}s] {m}", flush=True)


# ------------------------------------------------------------------ data + indicators
df = pd.concat([load_data(OLD), load_data(NEW)]).sort_index()
df = df[~df.index.duplicated()]
log(f"{len(df)} candles {df.index.min()} -> {df.index.max()}")
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
recs = sub.to_dict("records")
n = len(recs)

# ------------------------------------------------------------------ pillar scores, engine's own functions
comp = np.zeros((n, 2, 7), dtype=np.int8)  # chart, candle, volume, vwap, vp, penalty, raw_total
blocked = np.zeros(n, dtype=bool)
for i in range(WARMUP, n):
    r = recs[i]
    blocked[i] = bool(r.get("vol_distribution_3bar", False))
    for d, name in enumerate(("long", "short")):
        _, c = score_row_book2(r, name, cfg)
        comp[i, d] = (c["chart_pattern"], c["candle"], c["volume"], c["vwap"], c["vol_profile"], c["mtf_penalty"], c["raw_total"])
log("scores done")

# ------------------------------------------------------------------ forward outcomes
H, L, C = ind["High"].values, ind["Low"].values, ind["Close"].values
Hw, Lw = sliding_window_view(H[1:], HORIZON), sliding_window_view(L[1:], HORIZON)
last = n - HORIZON - 1
idx_all = np.arange(WARMUP, last + 1)
win_R, loss_R = (RR * STOP_P - COST) / STOP_P, -(STOP_P + COST) / STOP_P


def outcomes(direction):
    out = np.zeros(len(idx_all), dtype=np.int8)  # +1 win, -1 loss, 0 unresolved
    for a in range(0, len(idx_all), 15000):
        i0, i1 = idx_all[a], idx_all[min(a + 15000, len(idx_all)) - 1]
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


frames = []
times = ind.index[idx_all]
for d in (0, 1):
    o = outcomes(d)
    cd = comp[idx_all, d].astype(int)
    f = pd.DataFrame({"t": times, "dir": 1 if d == 0 else -1, "chart": cd[:, 0], "candle": cd[:, 1], "volume": cd[:, 2],
                      "vwap": cd[:, 3], "vp": cd[:, 4], "pen": cd[:, 5], "raw": cd[:, 6], "outcome": o,
                      "blocked": (blocked[idx_all] & (d == 0))})
    frames.append(f)
obs = pd.concat(frames, ignore_index=True)
log(f"outcomes done: {len(obs)} entries, unresolved {(obs.outcome == 0).mean() * 100:.2f}%")
obs["final"] = np.maximum(0, obs.raw - obs.pen)
obs.loc[obs.blocked, "final"] = -1
# the engine's actual signal rule, bar by bar
fl = obs[obs.dir == 1].set_index("t")["final"]
fs = obs[obs.dir == -1].set_index("t")["final"]
long_wins = (fl >= cfg.execution_threshold) & (fl >= fs)
short_wins = (fs >= cfg.execution_threshold) & (fs > fl)
obs["signal"] = False
obs.loc[obs.dir == 1, "signal"] = long_wins.values
obs.loc[obs.dir == -1, "signal"] = short_wins.values

obs = obs[obs.outcome != 0].copy()
obs["R"] = np.where(obs.outcome == 1, win_R, loss_R)
obs["win"] = obs.outcome == 1
obs["year"] = obs.t.dt.year
obs["hour"] = obs.t.dt.hour
obs["mtf_against"] = (obs.pen > 0).astype(int)
obs["half"] = np.where(obs.year <= 2019, "A", "B")
obs["base"] = obs.groupby(["year", "dir", "hour"]).R.transform("mean")
obs["edge"] = obs.R - obs.base
iso = obs.t.dt.isocalendar()
obs["wk"] = pd.factorize(iso.year.astype(int) * 100 + iso.week.astype(int))[0]
G = obs.wk.max() + 1
log(f"analysis frame ready: {len(obs)} resolved entries, {G} week clusters, base win rate {obs.win.mean() * 100:.1f}%, mean R {obs.R.mean():+.3f}")
obs.to_pickle(os.path.join(args.out_dir, "pillar_obs.pkl"))


# ------------------------------------------------------------------ cluster bootstrap helpers
def cluster_sums(mask):
    wk = obs.wk.values[mask]
    s = np.bincount(wk, weights=obs.edge.values[mask], minlength=G)
    k = np.bincount(wk, minlength=G).astype(float)
    return s, k


def boot(maskA, maskB=None):
    sA, kA = cluster_sums(maskA)
    if maskB is not None:
        sB, kB = cluster_sums(maskB)
    draws = rng.integers(0, G, size=(B, G))
    mA = sA[draws].sum(1) / np.maximum(kA[draws].sum(1), 1)
    pt = sA.sum() / max(kA.sum(), 1)
    if maskB is not None:
        mB = sB[draws].sum(1) / np.maximum(kB[draws].sum(1), 1)
        pt = pt - sB.sum() / max(kB.sum(), 1)
        mA = mA - mB
    lo95, hi95 = np.percentile(mA, [2.5, 97.5])
    lo99, hi99 = np.percentile(mA, [0.28, 99.72])
    return dict(est=pt, lo95=lo95, hi95=hi95, lo99=lo99, hi99=hi99)


def ols_cluster(X, y, wk):
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ (X.T @ y)
    u = y - X @ beta
    S = np.column_stack([np.bincount(wk, weights=X[:, j] * u, minlength=G) for j in range(X.shape[1])])
    V = XtX_inv @ (S.T @ S) @ XtX_inv * (G / (G - 1))
    return beta, np.sqrt(np.diag(V))


def fmt(d, nd=3):
    return f"{d['est']:+.{nd}f}  95% [{d['lo95']:+.{nd}f}, {d['hi95']:+.{nd}f}]  99.4% [{d['lo99']:+.{nd}f}, {d['hi99']:+.{nd}f}]"


results = {}
allm = np.ones(len(obs), dtype=bool)

print("\n=============== BASE RATES ===============")
print(f"resolved entries {len(obs)} | win rate {obs.win.mean() * 100:.1f}% | mean R {obs.R.mean():+.3f} "
      f"(breakeven win rate at 1-pip cost {-loss_R / (win_R - loss_R) * 100:.1f}%)")


def table(title, col, levels):
    print(f"\n--- edge by {title} (edge = R minus same-year/direction/hour baseline)")
    print(f"{'level':<8}{'n':>9}{'win%':>7}{'meanR':>8}{'edge':>9}   95% CI")
    rows = {}
    for lv in levels:
        m = (obs[col] == lv).values
        if m.sum() < 200:
            continue
        b = boot(m)
        rows[int(lv)] = dict(n=int(m.sum()), win=float(obs.win[m].mean() * 100), meanR=float(obs.R[m].mean()), **b)
        print(f"{lv:<8}{m.sum():>9}{obs.win[m].mean() * 100:>7.1f}{obs.R[m].mean():>+8.3f}{b['est']:>+9.3f}   [{b['lo95']:+.3f}, {b['hi95']:+.3f}]")
    return rows


results["by_final"] = table("final score (after MTF penalty)", "final", range(0, 11))
for nm, col, lv in (("chart pattern", "chart", range(0, 4)), ("candle", "candle", range(0, 3)),
                    ("volume", "volume", range(0, 3)), ("VWAP", "vwap", range(0, 3)), ("volume profile", "vp", range(0, 2)),
                    ("MTF penalty (0 = with trend, 2 = against)", "pen", (0, 2))):
    results["by_" + col] = table(nm, col, lv)

# ---------------- joint regression
print("\n=============== JOINT REGRESSION: edge ~ pillars + MTF-against ===============")
cols = ["chart", "candle", "volume", "vwap", "vp", "mtf_against"]
nz = (~obs.blocked).values  # blocked longs are a separate hard rule (P8)
reg = {}
for label, mask in (("pooled", nz), ("2013-2019", nz & (obs.half == "A").values), ("2020-2026", nz & (obs.half == "B").values)):
    X = np.column_stack([np.ones(mask.sum())] + [obs[c].values[mask].astype(float) for c in cols])
    beta, se = ols_cluster(X, obs.edge.values[mask], obs.wk.values[mask])
    reg[label] = (beta, se)
print(f"{'':<14}" + "".join(f"{c:>26}" for c in ["pooled", "2013-2019", "2020-2026"]))
for j, c in enumerate(["const"] + cols):
    line = f"{c:<14}"
    for label in ("pooled", "2013-2019", "2020-2026"):
        b, s = reg[label]
        line += f"{b[j]:>+14.4f} ({s[j]:.4f})".rjust(26)
    print(line)
results["reg"] = {k: dict(beta=v[0].tolist(), se=v[1].tolist()) for k, v in reg.items()}

# slope of edge on final score
sl = {}
for label, mask in (("pooled", nz), ("2013-2019", nz & (obs.half == "A").values), ("2020-2026", nz & (obs.half == "B").values)):
    X = np.column_stack([np.ones(mask.sum()), obs.final.values[mask].astype(float)])
    sl[label] = ols_cluster(X, obs.edge.values[mask], obs.wk.values[mask])
print("\nslope of edge per +1 final-score point:", {k: f"{v[0][1]:+.4f} (se {v[1][1]:.4f})" for k, v in sl.items()})

# ---------------- selection (the engine's actual rule)
print("\n=============== P0: ENGINE SIGNALS (score >= 7, best direction, not blocked) ===============")
sig = obs.signal.values
p0 = boot(sig)
print(f"signals {sig.sum()} ({sig.sum() / len(obs) * 100:.2f}% of entries) | win {obs.win[sig].mean() * 100:.1f}% | meanR {obs.R[sig].mean():+.3f}")
print("edge", fmt(p0))
half = {}
for h in ("A", "B"):
    m = sig & (obs.half == h).values
    half[h] = boot(m)
    print(f"  half {h}: n={m.sum()}  win {obs.win[m].mean() * 100:.1f}%  meanR {obs.R[m].mean():+.3f}  edge {half[h]['est']:+.3f} [{half[h]['lo95']:+.3f}, {half[h]['hi95']:+.3f}]")
for dname, dv in (("long", 1), ("short", -1)):
    m = sig & (obs.dir == dv).values
    b = boot(m)
    print(f"  {dname}: n={m.sum()}  win {obs.win[m].mean() * 100:.1f}%  meanR {obs.R[m].mean():+.3f}  edge {b['est']:+.3f} [{b['lo95']:+.3f}, {b['hi95']:+.3f}]")
print("  by year (signals): " + ", ".join(f"{y}: n={int((sig & (obs.year == y).values).sum())} edge {boot(sig & (obs.year == y).values)['est']:+.2f}" for y in sorted(obs.year.unique())))
results["p0"] = dict(**p0, n=int(sig.sum()), half={k: v["est"] for k, v in half.items()},
                     win=float(obs.win[sig].mean() * 100), meanR=float(obs.R[sig].mean()))

# ---------------- within the signal set: which pillar levels separate winners?
print("\n--- inside the engine's signals: edge by pillar level (does a stronger pillar pick better trades among signals?)")
for nm, col in (("chart", "chart"), ("candle", "candle"), ("volume", "volume"), ("vwap", "vwap"), ("vp", "vp"), ("final", "final")):
    line = f"{nm:<8}"
    for lv in sorted(obs[col][sig].unique()):
        m = sig & (obs[col] == lv).values
        if m.sum() >= 150:
            b = boot(m)
            line += f" | {lv}: n={m.sum()} edge {b['est']:+.2f}"
    print(line)

# ---------------- P7, P8
print("\n=============== P7: with-trend vs against-trend (MTF) ===============")
p7 = boot(((obs.mtf_against == 0) & ~obs.blocked).values, ((obs.mtf_against == 1) & ~obs.blocked).values)
print("edge(with) - edge(against)", fmt(p7))
p7h = {h: boot(((obs.mtf_against == 0) & ~obs.blocked & (obs.half == h)).values, ((obs.mtf_against == 1) & ~obs.blocked & (obs.half == h)).values)["est"] for h in ("A", "B")}
print("  by half:", {k: f"{v:+.3f}" for k, v in p7h.items()})
print("\n=============== P8: three-bar distribution block on longs ===============")
lg = (obs.dir == 1).values
p8 = boot(lg & obs.blocked.values, lg & ~obs.blocked.values)
print(f"blocked longs n={int((lg & obs.blocked.values).sum())}  edge(blocked) - edge(other longs)", fmt(p8))
p8h = {h: boot(lg & obs.blocked.values & (obs.half == h).values, lg & ~obs.blocked.values & (obs.half == h).values)["est"] for h in ("A", "B")}
print("  by half:", {k: f"{v:+.3f}" for k, v in p8h.items()})


# ---------------- verdicts
def verdict(est, lo95, hi95, lo99, hi99, halves, positive=True):
    s = 1 if positive else -1
    ok99 = (lo99 > 0) if positive else (hi99 < 0)
    ok95 = (lo95 > 0) if positive else (hi95 < 0)
    both = all(s * h > 0 for h in halves)
    return "SUPPORTED" if (ok99 and both) else ("WEAK" if ok95 else "NOT SUPPORTED")


print("\n######## PRE-REGISTERED VERDICTS ########")
V = {}
V["P0"] = (verdict(**{k: p0[k] for k in ("est", "lo95", "hi95", "lo99", "hi99")}, halves=[half["A"]["est"], half["B"]["est"]]), f"edge {p0['est']:+.3f} R/entry, n={int(sig.sum())}; halves {half['A']['est']:+.3f} / {half['B']['est']:+.3f}")
b, s = sl["pooled"]
sl_h = [sl["2013-2019"][0][1], sl["2020-2026"][0][1]]
est = b[1]; se = s[1]
V["P1"] = (verdict(est, est - Z95 * se, est + Z95 * se, est - Z994 * se, est + Z994 * se, sl_h), f"slope {est:+.4f} per point (se {se:.4f}); halves {sl_h[0]:+.4f} / {sl_h[1]:+.4f}")
bp, sp = reg["pooled"]
for k, (name, j) in enumerate((("chart pattern", 1), ("candle", 2), ("volume", 3), ("VWAP", 4), ("volume profile", 5)), start=2):
    est, se = bp[j], sp[j]
    hv = [reg["2013-2019"][0][j], reg["2020-2026"][0][j]]
    V[f"P{k}"] = (verdict(est, est - Z95 * se, est + Z95 * se, est - Z994 * se, est + Z994 * se, hv), f"{name}: {est:+.4f} R per point (se {se:.4f}); halves {hv[0]:+.4f} / {hv[1]:+.4f}")
V["P7"] = (verdict(p7["est"], p7["lo95"], p7["hi95"], p7["lo99"], p7["hi99"], list(p7h.values())), f"with-trend minus against-trend {p7['est']:+.3f} R; halves {p7h['A']:+.3f} / {p7h['B']:+.3f}")
V["P8"] = (verdict(p8["est"], p8["lo95"], p8["hi95"], p8["lo99"], p8["hi99"], list(p8h.values()), positive=False), f"blocked minus other longs {p8['est']:+.3f} R; halves {p8h['A']:+.3f} / {p8h['B']:+.3f}")
for k, (v, d) in V.items():
    print(f"{k}: {v:<14} {d}")
results["verdicts"] = {k: dict(verdict=v, detail=d) for k, v, d in ((k, *V[k]) for k in V)}
json.dump(results, open(os.path.join(args.out_dir, "pillar_results.json"), "w"), indent=1, default=float)
log("DONE")

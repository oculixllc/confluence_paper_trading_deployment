"""
learned_model_test.py

PRE-REGISTERED test: can a model LEARN a profitable combination of the book's indicators, judged only on
years it never saw? (Rules fixed before any model was trained.) ORB excluded.

REQUIRES  scikit-learn (pip install scikit-learn), in addition to numpy, pandas and scipy.

WHY       The hand-weighted 0-10 score and its 32 on/off patterns found nothing (see
          timeframe_confluence_test.py). A learned model can use the continuous version of every book concept
          and weigh them itself. If it also finds nothing out of sample, there is no edge in these indicators to
          automate; if it finds one, the model's behaviour tells us which book theory carries it.

FEATURES  Each (bar, direction) is a hypothetical entry at the bar close with a 1:2 stop/target (same exit as
          the live strategy, scaled by timeframe as in timeframe_confluence_test.py). Every feature is oriented
          so that + means "in favour of this trade". All use only data up to the entry bar.
            trend        close vs the 2,000-bar average, 1-day momentum, 5-day efficiency ratio
            VWAP         distance to VWAP (in ATRs and in VWAP std-devs), VWAP slope, reclaim/failure break,
                         confirmed +/-2 std-dev touch
            vol profile  distance to POC, value-area high and low, at-level, near HVN, near LVN, inside value area
            volume       log relative volume, fast/slow volume ratio, climax, absorption, churn, distribution
            candle       body, range, support-side and against-side wick, reversal candle, engulfing, hammer,
                         doji, order block + fair value gap
            chart        pattern grade for the trade direction and for the opposite direction
            context      log ATR, direction
          Distances are in ATRs (1 day of bars), so they mean the same thing across years and timeframes.

MODELS    Fixed in advance, never tuned on test data.
            logit  L2 logistic regression, C = 0.05, standardised inputs
            hgb    gradient-boosted trees: depth 3, 200 iterations, learning rate 0.05, min 1,000 samples/leaf
          Target: did the 1:2 trade hit its target before its stop. A 7-day gap separates train from test.

SPLITS    primary: train 2013-2019 -> test 2020-2026.   mirror: train 2020-2026 -> test 2013-2019.
          (--walk-forward adds yearly retraining, 2017-2026, as a supplementary live-like estimate.)

HYPOTHESES  Bonferroni over 8 tests (alpha = 0.05 / 8 = 0.00625, two-sided 99.375% interval); per model, per
            timeframe (15m and 30m).
  L1  Ranking:    test-set AUC > 0.5, in BOTH primary and mirror.
  L2  Selection:  the model's top 0.5% of entries in each test year have edge > 0 (edge = R minus the mean R of
                  entries in the same year, direction and hour), in BOTH primary and mirror.
  L3  Tradeable:  taking those entries one at a time (never two open positions) gives a mean R after a 2-pip
                  round-trip cost whose 95% interval is above zero in the primary test, and a positive point
                  estimate in the mirror.
  A model has a LEARNABLE EDGE at a timeframe only if L1, L2 and L3 all hold. It counts as PROVEN only if the
  same model passes at both timeframes AND the chart-pattern columns pass a look-ahead audit.
BENCHMARK   The engine's own signals (score >= 7), same metrics.

OTHER PAIRS (added before the USD/JPY run; nothing else changed)  The identical rules are applied to another pair
            to see whether the EUR/USD result is specific to EUR/USD. The pip size is the pair's (JPY = 0.01) and
            every pip distance (stop, patterns, tolerances) is scaled by the ratio of the pair's median
            1-day-average true range to EUR/USD's, measured from price data alone (USD/JPY: stop 17.5 pips on 15m).
            A model has a learnable edge on the pair only if L1, L2 and L3 hold there. The indicator suite is
            judged to carry a pair-independent edge only if it holds on EUR/USD AND on the second pair.

ICHIMOKU SUITE (added before the run; nothing else changed)  --feature-set ichi tests the combination
            ichimoku + VWAP + volume profile + price action, dropping the volume and chart-pattern features.
            Ichimoku uses the standard 9/26/52-bar periods at every timeframe (a bar-count convention, so 15m, 30m
            and 60m cover different clock time). The cloud at a bar is taken from values computed 26 bars earlier,
            and the Chikou span is price versus price 26 bars ago, so nothing looks ahead. Features: distance beyond
            the cloud in the trade direction, cloud thickness, colour of the cloud ahead, Tenkan-Kijun gap, price
            versus Kijun and Tenkan, Chikou, and a Tenkan/Kijun cross. Tested on EUR/USD and USD/JPY at 15m, 30m
            and 60m with the same models and the same L1/L2/L3 rules. A suite counts as having a learnable edge only
            if the same model passes at the same timeframe on BOTH pairs (replication across pairs is the guard
            against a lucky pass in this larger set of runs).
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--tf", type=int, choices=[15, 30, 60], required=True)
ap.add_argument("--feature-set", choices=["book", "ichi"], default="book")
ap.add_argument("--csv15-old", default="eur_usd_15m_2013-01_to_2023-09.csv")
ap.add_argument("--csv15-new", default="eur_usd_15m_2023-10_to_2026-10.csv")
ap.add_argument("--csv15", nargs="+", default=None, help="15m CSV file(s) in time order; overrides --csv15-old/--csv15-new")
ap.add_argument("--label", default="EURUSD", help="pair label used in output names")
ap.add_argument("--pip-size", type=float, default=0.0001, help="0.0001 for most pairs, 0.01 for JPY pairs")
ap.add_argument("--stop-pips-15m", type=float, default=15.0, help="15m stop in pips (scales every pip distance)")
ap.add_argument("--out-dir", default=".")
ap.add_argument("--walk-forward", action="store_true")
args = ap.parse_args()

from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402
from confluence_engine_book2 import Book2Config, compute_indicators_book2, score_row_book2  # noqa: E402
from backtest_confluence import load_data  # noqa: E402

TF = args.tf
f_time, f_vol = 15.0 / TF, (TF / 15.0) ** 0.5
pr = args.stop_pips_15m / 15.0  # pip-distance scale for this pair
cfg = Book2Config(
    pip_size=args.pip_size, mtf_bias_lookback=round(2000 * f_time), vp_lookback_bars=round(480 * f_time),
    stop_pips=round(15.0 * pr * f_vol, 1), poc_tolerance_pips=3.0 * pr * f_vol, dead_zone_edge_tolerance_pips=5.0 * pr * f_vol,
    double_pattern_tolerance_pips=4.0 * pr * f_vol, flag_pole_min_pips=15.0 * pr * f_vol, ob_impulse_min_pips=12.0 * pr * f_vol)
P, STOP_P, RR = cfg.pip_size, cfg.stop_pips, 2.0
HORIZON, WARMUP = round(400 * f_time), int(cfg.mtf_bias_lookback * 1.25)
ATR_N, ER_N = round(96 * f_time), round(480 * f_time)
COSTS = (1.0, 2.0)
BOOT, AUC_BOOT = 3000, 200
rng = np.random.default_rng(5)
T0 = time.time()
NY = "America/New_York"


def log(m):
    print(f"[{time.time() - T0:5.0f}s] [{args.label} {TF}m] {m}", flush=True)


# ====================================================================== bars, indicators, engine score
d15 = pd.concat([load_data(f) for f in (args.csv15 or [args.csv15_old, args.csv15_new])]).sort_index()
d15 = d15[~d15.index.duplicated()]
if TF == 15:
    df = d15
else:
    g = d15.resample(f"{TF}min", label="left", closed="left")
    df = g.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})[g["Close"].count() >= max(2, TF // 15 - 1)]
log(f"{len(df)} bars | stop {STOP_P} pips, horizon {HORIZON}")
ind = compute_indicators_book2(df, cfg)
n = len(ind)
log("indicators done")

need = ["chart_pattern_grade_long", "chart_pattern_grade_short", "bullish_reversal_candle", "ob_fvg_combined_long",
        "bearish_reversal_candle", "ob_fvg_combined_short", "rvol", "vol_climax", "vol_absorption", "vol_churn",
        "above_vwap", "below_vwap", "vwap_slope", "vwap_reclaim", "vwap_failure_break", "at_vp_level", "near_hvn",
        "mtf_bias_known", "mtf_bias_up", "vol_distribution_3bar"]
sub = ind[need].copy()
for c in sub.columns:
    if sub[c].dtype == object or sub[c].dtype == bool:
        sub[c] = sub[c].fillna(False).astype(bool)
comp = np.zeros((n, 2, 2), dtype=np.int16)  # raw_total, penalty
blocked = np.zeros(n, dtype=bool)
for a in range(WARMUP, n, 50000):
    for j, r in enumerate(sub.iloc[a:a + 50000].to_dict("records")):
        i = a + j
        blocked[i] = bool(r.get("vol_distribution_3bar", False))
        for d, name in enumerate(("long", "short")):
            _, c = score_row_book2(r, name, cfg)
            comp[i, d] = (c["raw_total"], c["mtf_penalty"])
del sub
fin = np.maximum(0, comp[:, :, 0] - comp[:, :, 1]).astype(int)
fin[:, 0] = np.where(blocked, -1, fin[:, 0])
log("engine scores done")

# ====================================================================== outcomes (with time to resolution)
H, L, C = ind["High"].values, ind["Low"].values, ind["Close"].values
Hw, Lw = sliding_window_view(H[1:], HORIZON), sliding_window_view(L[1:], HORIZON)
idx_all = np.arange(WARMUP, n - HORIZON)


def outcomes(direction):
    out = np.zeros(len(idx_all), dtype=np.int8)
    dur = np.full(len(idx_all), HORIZON, dtype=np.int32)
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
        res = np.where((ti == none) & (si == none), 0, np.where(si <= ti, -1, 1))
        out[a:a + len(ti)] = res
        dur[a:a + len(ti)] = np.where(res != 0, np.minimum(ti, si) + 1, HORIZON)
    return out, dur


# ====================================================================== oriented features
c_, o_, h_, l_ = ind["Close"], ind["Open"], ind["High"], ind["Low"]
tr = pd.concat([h_ - l_, (h_ - c_.shift()).abs(), (l_ - c_.shift()).abs()], axis=1).max(axis=1)
atr = tr.rolling(ATR_N).mean().replace(0, np.nan)
er = (c_ - c_.shift(ER_N)).abs() / c_.diff().abs().rolling(ER_N).sum()
upper, lower = h_ - np.maximum(o_, c_), np.minimum(o_, c_) - l_
vstd = ind["vwap_std"].replace(0, np.nan)
f = lambda s: s.astype(float)  # noqa: E731
ICHI = args.feature_set == "ichi"
if ICHI:
    tenkan = (h_.rolling(9).max() + l_.rolling(9).min()) / 2
    kijun = (h_.rolling(26).max() + l_.rolling(26).min()) / 2
    a_now = (tenkan + kijun) / 2
    b_now = (h_.rolling(52).max() + l_.rolling(52).min()) / 2
    span_a, span_b = a_now.shift(26), b_now.shift(26)
    cloud_top, cloud_bot = np.maximum(span_a, span_b), np.minimum(span_a, span_b)
    tk_up = ((tenkan > kijun) & (tenkan.shift() <= kijun.shift())).astype(float)
    tk_dn = ((tenkan < kijun) & (tenkan.shift() >= kijun.shift())).astype(float)


def features(d):
    long_ = d > 0
    pick = lambda a, b: f(ind[a] if long_ else ind[b])  # noqa: E731
    X = pd.DataFrame({
        "trend_dist": d * (c_ - ind["mtf_bias_sma"]) / atr, "momentum_1d": d * (c_ - c_.shift(ATR_N)) / atr, "efficiency_5d": er,
        "vwap_dist_atr": d * (c_ - ind["vwap"]) / atr, "vwap_dist_std": (d * (c_ - ind["vwap"]) / vstd).clip(-6, 6),
        "vwap_slope": d * ind["vwap_slope"] / atr, "vwap_reclaim_or_fail": pick("vwap_reclaim", "vwap_failure_break"),
        "vwap_touch2_confirmed": pick("vwap_touch_minus2_confirmed", "vwap_touch_plus2_confirmed"),
        "poc_dist": d * (c_ - ind["poc"]) / atr, "vah_dist": d * (c_ - ind["vah"]) / atr, "val_dist": d * (c_ - ind["val"]) / atr,
        "vp_at_level": f(ind["at_vp_level"]), "vp_near_hvn": f(ind["near_hvn"]), "vp_near_lvn": f(ind["near_lvn"]),
        "vp_in_value_area": f((c_ <= ind["vah"]) & (c_ >= ind["val"])),
        "rvol_log": np.log(ind["rvol"].clip(lower=0.05)), "vol_fast_slow": np.log((ind["vol_ema_fast"] / ind["vol_ema_slow"]).clip(lower=0.05)),
        "vol_climax": f(ind["vol_climax"]), "vol_absorption": f(ind["vol_absorption"]), "vol_churn": f(ind["vol_churn"]),
        "vol_distribution": f(ind["vol_distribution_bar"]), "vol_distribution_3bar": f(ind["vol_distribution_3bar"]) * (1.0 if long_ else 0.0),
        "candle_body": d * (c_ - o_) / atr, "candle_range": (h_ - l_) / atr,
        "wick_support": (lower if long_ else upper) / atr, "wick_against": (upper if long_ else lower) / atr,
        "candle_reversal": pick("bullish_reversal_candle", "bearish_reversal_candle"),
        "candle_engulfing": pick("bullish_engulfing", "bearish_engulfing"), "candle_hammer": pick("hammer", "shooting_star"),
        "candle_doji": f(ind["doji"]), "ob_fvg_combined": pick("ob_fvg_combined_long", "ob_fvg_combined_short"),
        "chart_grade": pick("chart_pattern_grade_long", "chart_pattern_grade_short"),
        "chart_grade_opposite": pick("chart_pattern_grade_short", "chart_pattern_grade_long"),
        "atr_log": np.log(atr / P), "direction": float(d),
    })
    if ICHI:
        X["ichi_cloud_dist"] = ((c_ - cloud_top) if long_ else (cloud_bot - c_)) / atr
        X["ichi_cloud_thickness"] = (cloud_top - cloud_bot) / atr
        X["ichi_future_cloud"] = d * (a_now - b_now) / atr
        X["ichi_tenkan_kijun"] = d * (tenkan - kijun) / atr
        X["ichi_price_kijun"] = d * (c_ - kijun) / atr
        X["ichi_price_tenkan"] = d * (c_ - tenkan) / atr
        X["ichi_chikou"] = d * (c_ - c_.shift(26)) / atr
        X["ichi_tk_cross"] = tk_up if long_ else tk_dn
    return X.iloc[idx_all].reset_index(drop=True).astype("float32")


GROUPS = {
    "trend": ["trend_dist", "momentum_1d", "efficiency_5d"],
    "vwap": ["vwap_dist_atr", "vwap_dist_std", "vwap_slope", "vwap_reclaim_or_fail", "vwap_touch2_confirmed"],
    "volume profile": ["poc_dist", "vah_dist", "val_dist", "vp_at_level", "vp_near_hvn", "vp_near_lvn", "vp_in_value_area"],
    "volume": ["rvol_log", "vol_fast_slow", "vol_climax", "vol_absorption", "vol_churn", "vol_distribution", "vol_distribution_3bar"],
    "candle / price action": ["candle_body", "candle_range", "wick_support", "wick_against", "candle_reversal", "candle_engulfing",
                              "candle_hammer", "candle_doji", "ob_fvg_combined"],
    "chart pattern": ["chart_grade", "chart_grade_opposite"],
    "context": ["atr_log", "direction"],
    "ichimoku": ["ichi_cloud_dist", "ichi_cloud_thickness", "ichi_future_cloud", "ichi_tenkan_kijun", "ichi_price_kijun",
                 "ichi_price_tenkan", "ichi_chikou", "ichi_tk_cross"],
}
USED = ["ichimoku", "vwap", "volume profile", "candle / price action", "context"] if ICHI else \
    ["trend", "vwap", "volume profile", "volume", "candle / price action", "chart pattern", "context"]
GROUPS = {k: GROUPS[k] for k in USED}
EXPECT_POSITIVE = (["ichi_cloud_dist", "ichi_tenkan_kijun", "ichi_chikou", "ichi_tk_cross", "vwap_reclaim_or_fail", "candle_reversal", "vp_at_level"]
                   if ICHI else ["trend_dist", "vwap_reclaim_or_fail", "candle_reversal", "chart_grade", "ob_fvg_combined", "vp_at_level"])

times = ind.index[idx_all]
parts = []
for d_i, d in enumerate((1, -1)):
    out, dur = outcomes(d_i)
    X = features(d)
    meta = pd.DataFrame({"t": times, "bar_i": idx_all, "dir": d, "outcome": out, "dur": dur,
                         "eng": fin[idx_all, d_i], "eng_other": fin[idx_all, 1 - d_i]})
    parts.append(pd.concat([meta, X], axis=1))
data = pd.concat(parts, ignore_index=True)
del parts
data = data[data.outcome != 0].reset_index(drop=True)
feat_cols = [c for g in GROUPS.values() for c in g]
data["eng_signal"] = np.where(data.dir == 1, (data.eng >= cfg.execution_threshold) & (data.eng >= data.eng_other),
                              (data.eng >= cfg.execution_threshold) & (data.eng > data.eng_other))
data["y"] = (data.outcome == 1).astype(int)
data["year"] = data.t.dt.year
data["hour"] = data.t.dt.hour


def R_at(outcome, cost):
    return np.where(outcome == 1, (RR * STOP_P - cost) / STOP_P, -(STOP_P + cost) / STOP_P)


data["R"] = R_at(data.outcome.values, COSTS[0])
data["edge"] = data.R - data.groupby(["year", "dir", "hour"]).R.transform("mean")
iso = data.t.dt.isocalendar()
data["wk"] = pd.factorize(iso.year.astype(int) * 100 + iso.week.astype(int))[0]
G = data.wk.max() + 1
log(f"dataset: {len(data)} entries x {len(feat_cols)} features, base win {data.y.mean() * 100:.1f}%, {G} week clusters")

# ====================================================================== helpers
Z95_PCT, BONF_PCT = (2.5, 97.5), (0.3125, 99.6875)


def boot_mean(vals, wk):
    s = np.bincount(wk, weights=vals, minlength=G)
    k = np.bincount(wk, minlength=G).astype(float)
    draws = rng.integers(0, G, size=(BOOT, G))
    m = s[draws].sum(1) / np.maximum(k[draws].sum(1), 1)
    lo95, hi95 = np.percentile(m, Z95_PCT)
    lo, hi = np.percentile(m, BONF_PCT)
    return dict(est=float(s.sum() / max(k.sum(), 1)), lo95=float(lo95), hi95=float(hi95), lo=float(lo), hi=float(hi))


def auc_of(score, y):
    r = rankdata(score)
    npos = y.sum()
    return (r[y == 1].sum() - npos * (npos + 1) / 2) / (npos * (len(y) - npos))


def auc_boot(score, y, wk):
    order = np.argsort(wk, kind="stable")
    cuts = np.cumsum(np.bincount(wk, minlength=G))[:-1]
    weeks = np.split(order, cuts)
    est = auc_of(score, y)
    bs = []
    for _ in range(AUC_BOOT):
        idx = np.concatenate([weeks[i] for i in rng.integers(0, G, G)])
        bs.append(auc_of(score[idx], y[idx]))
    return dict(est=float(est), lo95=float(np.percentile(bs, 2.5)), hi95=float(np.percentile(bs, 97.5)),
                lo=float(np.percentile(bs, BONF_PCT[0])), hi=float(np.percentile(bs, BONF_PCT[1])))


def tradeable(sel):
    """Take selected entries one at a time: a new entry waits until the open position has resolved."""
    sel = sel.sort_values(["bar_i", "pred"], ascending=[True, False])
    keep, free_at = [], -1
    for b, d_, ix in zip(sel.bar_i.values, sel.dur.values, sel.index.values):
        if b >= free_at:
            keep.append(ix)
            free_at = b + d_
    return sel.loc[keep]


def evaluate(test, label):
    """Metrics for one scored test frame (needs column 'pred'); returns a dict."""
    out = {}
    out["auc"] = auc_boot(test.pred.values, test.y.values, test.wk.values)
    yrs = test.year.nunique()
    for q in (0.005, 0.001):
        thr = test.groupby("year").pred.transform(lambda s: s.quantile(1 - q))
        top = test[test.pred >= thr]
        tr_ = tradeable(top)
        e = boot_mean(top.edge.values, top.wk.values)
        row = dict(n=int(len(top)), per_year=float(len(top) / yrs), win=float(top.y.mean() * 100), edge=e,
                   trades=int(len(tr_)), trades_per_year=float(len(tr_) / yrs), trade_win=float(tr_.y.mean() * 100))
        for c in COSTS:
            Rc = R_at(tr_.outcome.values, c)
            row[f"trade_meanR_{int(c)}"] = boot_mean(Rc, tr_.wk.values)
            w, l_ = Rc[Rc > 0].sum(), -Rc[Rc < 0].sum()
            row[f"trade_pf_{int(c)}"] = float(w / l_) if l_ else float("inf")
        out[f"top{q * 100:g}pct"] = row
    return out


def engine_eval(test):
    sig = test[test.eng_signal].copy()
    sig["pred"] = sig.eng.astype(float)
    yrs = test.year.nunique()
    tr_ = tradeable(sig)
    row = dict(n=int(len(sig)), per_year=float(len(sig) / yrs), win=float(sig.y.mean() * 100), edge=boot_mean(sig.edge.values, sig.wk.values),
               trades=int(len(tr_)), trades_per_year=float(len(tr_) / yrs), trade_win=float(tr_.y.mean() * 100))
    for c in COSTS:
        Rc = R_at(tr_.outcome.values, c)
        row[f"trade_meanR_{int(c)}"] = boot_mean(Rc, tr_.wk.values)
        w, l_ = Rc[Rc > 0].sum(), -Rc[Rc < 0].sum()
        row[f"trade_pf_{int(c)}"] = float(w / l_) if l_ else float("inf")
    return row


def make_model(kind):
    if kind == "logit":
        return LogisticRegression(C=0.05, max_iter=300)
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=1000,
                                          l2_regularization=1.0, random_state=0)


class Fitted:
    def __init__(self, kind, Xtr, ytr):
        self.kind, self.m = kind, make_model(kind)
        if kind == "logit":
            self.sc = StandardScaler().fit(Xtr)
            self.m.fit(self.prep(Xtr), ytr)
        else:
            self.m.fit(Xtr, ytr)

    def prep(self, X):
        return np.clip(np.nan_to_num((X - self.sc.mean_) / self.sc.scale_), -8, 8)

    def predict(self, X):
        return self.m.predict_proba(self.prep(X) if self.kind == "logit" else X)[:, 1]


ts = lambda s: pd.Timestamp(s, tz=NY)  # noqa: E731
T_2020, T_2020_PURGED, T_2019_PURGED = ts("2020-01-01"), ts("2020-01-08"), ts("2019-12-25")
SPLITS = {
    "primary": (data.t < T_2019_PURGED, data.t >= T_2020),
    "mirror": (data.t >= T_2020_PURGED, data.t < T_2020),
}
Xall = data[feat_cols].values
res = dict(pair=args.label, tf=TF, stop_pips=STOP_P, n=int(len(data)), splits={})
fitted = {}

print(f"\n################ {TF}m — benchmark: the engine's own signals (score >= 7) ################")
for sp, (trm, tem) in SPLITS.items():
    test = data[tem.values]
    eb = engine_eval(test)
    res.setdefault("engine", {})[sp] = eb
    print(f"[{sp:<7}] signals {eb['n']} ({eb['per_year']:.0f}/yr) edge {eb['edge']['est']:+.3f} 95% [{eb['edge']['lo95']:+.3f},{eb['edge']['hi95']:+.3f}] | "
          f"tradeable {eb['trades']} trades ({eb['trades_per_year']:.0f}/yr) mean R after 2pip {eb['trade_meanR_2']['est']:+.3f} "
          f"95% [{eb['trade_meanR_2']['lo95']:+.3f},{eb['trade_meanR_2']['hi95']:+.3f}] PF {eb['trade_pf_2']:.2f}")

for kind in ("logit", "hgb"):
    for sp, (trm, tem) in SPLITS.items():
        mdl = Fitted(kind, Xall[trm.values], data.y.values[trm.values])
        fitted[(kind, sp)] = mdl
        test = data[tem.values].copy()
        test["pred"] = mdl.predict(Xall[tem.values])
        ev = evaluate(test, f"{kind}/{sp}")
        res["splits"][f"{kind}/{sp}"] = ev
        print(f"\n---------------- {TF}m {kind.upper()} — {sp}: train {int(trm.sum())} entries -> test {int(tem.sum())} entries ----------------")
        a = ev["auc"]
        print(f"AUC {a['est']:.4f}  95% [{a['lo95']:.4f},{a['hi95']:.4f}]  99.4% [{a['lo']:.4f},{a['hi']:.4f}]")
        for q in ("top0.5pct", "top0.1pct"):
            r = ev[q]
            print(f"{q:<10} entries {r['n']} ({r['per_year']:.0f}/yr) win {r['win']:.1f}% edge {r['edge']['est']:+.3f} 95% [{r['edge']['lo95']:+.3f},{r['edge']['hi95']:+.3f}] "
                  f"99.4% [{r['edge']['lo']:+.3f},{r['edge']['hi']:+.3f}] | tradeable {r['trades']} ({r['trades_per_year']:.0f}/yr) win {r['trade_win']:.1f}% "
                  f"meanR 1pip {r['trade_meanR_1']['est']:+.3f} 2pip {r['trade_meanR_2']['est']:+.3f} 95% [{r['trade_meanR_2']['lo95']:+.3f},{r['trade_meanR_2']['hi95']:+.3f}] "
                  f"PF2 {r['trade_pf_2']:.2f}")

# ---------------- what the models use
lg = fitted[("logit", "primary")]
coef = pd.Series(lg.m.coef_[0], index=feat_cols)
lg2 = fitted[("logit", "mirror")]
coef2 = pd.Series(lg2.m.coef_[0], index=feat_cols)
print(f"\n=== {TF}m logistic coefficients (standardised; + raises P(win)); trained 2013-19 | trained 2020-26 ===")
for name in coef.abs().sort_values(ascending=False).head(14).index:
    print(f"  {name:<24} {coef[name]:+.4f} | {coef2[name]:+.4f}  {'(signs agree)' if np.sign(coef[name]) == np.sign(coef2[name]) else '(signs DISAGREE)'}")
agree = sum(1 for k in EXPECT_POSITIVE if coef[k] > 0)
agree2 = sum(1 for k in EXPECT_POSITIVE if coef2[k] > 0)
print(f"book-theory features expected to help ({len(EXPECT_POSITIVE)}): positive coefficient in {agree} (train 2013-19) and {agree2} (train 2020-26)")
res["coef"] = {"primary": coef.to_dict(), "mirror": coef2.to_dict(), "theory_agree": [agree, agree2, len(EXPECT_POSITIVE)]}

mdl = fitted[("hgb", "primary")]
tem = SPLITS["primary"][1].values
Xt, yt = Xall[tem], data.y.values[tem]
base_auc = auc_of(mdl.predict(Xt), yt)
print(f"\n=== {TF}m group importance (HGB, primary test): AUC drop when a group is shuffled (baseline AUC {base_auc:.4f}) ===")
imp = {}
for gname, cols in GROUPS.items():
    ix = [feat_cols.index(c) for c in cols]
    drops = []
    for _ in range(2):
        Xs = Xt.copy()
        Xs[:, ix] = Xs[np.ix_(rng.permutation(len(Xs)), ix)]
        drops.append(base_auc - auc_of(mdl.predict(Xs), yt))
    imp[gname] = float(np.mean(drops))
    print(f"  {gname:<22} {imp[gname]:+.4f}")
res["group_importance"] = imp

# ---------------- optional walk-forward
if args.walk_forward:
    print(f"\n=== {TF}m WALK-FORWARD (retrain every year on all earlier years; test year shown) ===")
    wf = {}
    for kind in ("logit", "hgb"):
        parts = []
        for Y in range(2017, 2027):
            trm = (data.t < ts(f"{Y}-01-01") - pd.Timedelta(days=7)).values
            tem = (data.year == Y).values
            m = Fitted(kind, Xall[trm], data.y.values[trm])
            tp = data[tem].copy()
            tp["pred"] = m.predict(Xall[tem])
            parts.append(tp)
        pooled = pd.concat(parts)
        ev = evaluate(pooled, f"{kind}/walk")
        wf[kind] = ev
        r = ev["top0.5pct"]
        print(f"{kind:<6} pooled 2017-2026: AUC {ev['auc']['est']:.4f} [{ev['auc']['lo95']:.4f},{ev['auc']['hi95']:.4f}] | top0.5% edge {r['edge']['est']:+.3f} "
              f"[{r['edge']['lo95']:+.3f},{r['edge']['hi95']:+.3f}] | tradeable {r['trades']} ({r['trades_per_year']:.0f}/yr) meanR 2pip {r['trade_meanR_2']['est']:+.3f} "
              f"[{r['trade_meanR_2']['lo95']:+.3f},{r['trade_meanR_2']['hi95']:+.3f}]")
    res["walk_forward"] = wf

# ---------------- pre-registered verdicts
print(f"\n######## {TF}m PRE-REGISTERED VERDICTS ########")
V = {}
for kind in ("logit", "hgb"):
    p, m = res["splits"][f"{kind}/primary"], res["splits"][f"{kind}/mirror"]
    l1 = p["auc"]["lo"] > 0.5 and m["auc"]["lo"] > 0.5
    l2 = p["top0.5pct"]["edge"]["lo"] > 0 and m["top0.5pct"]["edge"]["lo"] > 0
    l3 = p["top0.5pct"]["trade_meanR_2"]["lo95"] > 0 and m["top0.5pct"]["trade_meanR_2"]["est"] > 0
    V[kind] = dict(L1=l1, L2=l2, L3=l3, learnable=l1 and l2 and l3)
    print(f"{kind:<6} L1 ranking (AUC>0.5 both splits): {'PASS' if l1 else 'FAIL'} | L2 selection (top 0.5% edge>0 both): {'PASS' if l2 else 'FAIL'} | "
          f"L3 tradeable (2-pip meanR>0): {'PASS' if l3 else 'FAIL'}  ->  {'LEARNABLE EDGE' if V[kind]['learnable'] else 'NO LEARNABLE EDGE'}")
res["verdicts"] = V
json.dump(res, open(os.path.join(args.out_dir, f"learned_{args.label}{'_ichi' if ICHI else ''}_tf{TF}_results.json"), "w"), indent=1, default=float)
log("DONE")

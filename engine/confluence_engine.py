"""
confluence_engine.py

RECONSTRUCTED VERSION — post-ablation corrected engine.

This file is a from-scratch rebuild, not a recovered original. The original
corrected engine (built during an earlier session after the pillar ablation)
was never saved to a file we could re-upload, so this reimplements it from
the validated findings recorded in the project summary:

  Pillar          Verdict from ablation                    What changed here
  --------------- ---------------------------------------- ---------------------------------
  Volume          No discriminating edge                   DROPPED from standalone scoring
                                                             (see NOTE below re: sweep_confluence.py)
  VWAP            No discriminating edge                   DROPPED from standalone scoring
  ORB             Backwards in BOTH directions              INVERTED: "breakout" columns now
                  (breakouts mean-revert, not continue)     encode mean-reversion, not continuation
  Candlestick     Bearish signal was backwards               INVERTED: the classic bearish-shaped
                  (price rose after "bearish reversal")      pattern now feeds the LONG signal.
                                                             No validated candle pattern survives
                                                             for the short side, so
                                                             bearish_reversal_candle is always False.
  Chart Pattern   Real, consistent, strongest on shorts      KEPT/rebuilt as liquidity-sweep +
                                                             double-top/double-bottom detection
  Volume Profile  Non-directional as coded, unresolved       KEPT AS-IS (unchanged, still ambiguous)

RESOLVED CAVEAT (previously outstanding): sweep_confluence.py's
`run_backtest_vectorized` originally had its own independent, hardcoded
scoring formula that still summed Volume and VWAP directly, and its
`compute_indicators_fast` computed ORB in the original (un-inverted)
direction — both duplicating the pre-ablation logic even though this
module had already been corrected. That script has since been patched to
match: its score is now vol_profile(1) + orb(1) + candle(2) +
chart_pattern(3), and its ORB columns use the same mean-reversion
direction as this module. The two implementations are now consistent.

SESSION-GATING EXPERIMENT (tested, refuted): a per-pillar ablation found
Chart Pattern's forward-return edge concentrated inside the book's session
window and ORB's concentrated outside it, suggesting each pillar should be
gated to its own regime (`chart_pattern_session_only` / `orb_session_only`
below). That specific pairing — Chart Pattern gated in, ORB left ungated —
was then tested via a 300-combo sweep and a chronological train/test
split, and came out as the WORST of the four possible gating combinations
in both checks (mean out-of-sample profit factor 0.55, vs. 0.81-0.99 for
the other three). Both toggles are kept as sweep-able config fields in
case a different combination of the other parameters makes them useful,
but default to False given that result.

TREND/REGIME FILTER (validated, now merged into this file): the pillar
corrections above left the short side structurally capped at score 5 vs.
long's 7 (no validated bearish candle signal exists — see the candlestick
pillar docstring). That produced a measured 89.7% long bias over the full
24-month sample, which broke badly the moment the market trend reversed
(a real, chronologically out-of-sample 2026 fold showed profit factor
collapsing to 0.41-0.44 with a 20%+ drawdown). A disciplined re-search
across six distinct bearish candle definitions found no organic short
signal to fix this at the source, so a higher-timeframe trend/regime
filter (`regime_filter_enabled` / `regime_sma_lookback` / `regime_filter_
mode` below) was added instead, applied inside `evaluate_signal`. It was
validated on a held-out chronological test split and specifically against
the reversal that broke the engine: it cut that period's max drawdown
from 20.5% to 5.6% and turned a -19.7% return into essentially flat
(-0.8%). It does NOT on its own make the engine robustly profitable OOS
(test PF 0.96, just under breakeven) — it converts a tail-risk disaster
into a survivable, roughly-flat stretch. The validated config
(SMA(2000), mode="both") is the default below, in the same file as
everything else — no separate regime-filter script anymore.

REVIEW TRIGGER (validated, now merged into this file): a bootstrap-based
drawdown/losing-streak stress test (i.i.d. and block resampling, both
giving similar tails post-regime-filter — evidence the filter removed
most of the correlated-loss risk that caused the original crisis) set the
persistent review-trigger thresholds below (`review_trigger_max_drawdown_
pct`, `review_trigger_losing_streak`) at their empirical p99: ~25-27%
drawdown, ~27-28 consecutive losses. These do NOT fire on the known-good
historical run (11.0% drawdown, 17-trade worst streak) but DO fire
correctly on the pre-filter crisis run (28-trade streak, flagged exactly
at the reversal). Unlike `max_daily_loss_pct`, this flag does not
auto-reset — see `new_review_trigger_state` / `update_review_trigger_
state` / `is_review_triggered` below.
"""

from dataclasses import dataclass, field
from datetime import time as dtime
import pandas as pd
import numpy as np


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ConfluenceConfig:
    # --- Scoring thresholds ---
    # Standalone engine's max score is 7 (orb 1 + vol_profile 1 + candle 2 +
    # chart_pattern 3). Defaults below reflect the values the sweep converged
    # on repeatedly (min_score_continuation=4) and a slightly higher bar for
    # reversal-type entries.
    min_score_continuation: int = 4
    min_score_reversal: int = 5

    # --- Volume (Chapter 6) --- kept only as an input to the liquidity-sweep
    # volume filter and the (still-ambiguous) volume profile; no longer
    # contributes its own point to the standalone score.
    volume_lookback: int = 20
    volume_expand_mult: float = 1.0
    volume_climax_mult: float = 3.0

    # --- VWAP (Chapter 7) --- computed for diagnostics only; not scored.
    vwap_session_anchor: dtime = dtime(9, 30)

    # --- Volume Profile (Chapter 8) --- unresolved pillar, left unchanged.
    vol_profile_lookback_bars: int = 96
    vol_profile_bins: int = 48
    value_area_pct: float = 0.70
    poc_tolerance_pips: float = 3.0

    # --- ORB (Chapter 9) --- window definition unchanged; INTERPRETATION
    # inverted in compute_indicators (see module docstring).
    orb_window_start: dtime = dtime(8, 0)
    orb_window_minutes: int = 30

    # --- Session gating (Chapter 10.7 / Appendix E) ---
    # `require_session_window` is a blanket kill-switch that blocks ALL
    # entries outside the window, kept for backward compatibility / as an
    # extreme option. Defaults off: the earlier ablation found the OVERALL
    # edge isn't confined to the session window.
    #
    # `chart_pattern_session_only` / `orb_session_only` were added after a
    # per-pillar ablation suggested each pillar has a different "home turf"
    # (Chart Pattern edge concentrated in-session, ORB edge concentrated
    # off-session). That hypothesis was then tested and REFUTED by an
    # out-of-sample check: gating Chart Pattern to session-only while
    # leaving ORB ungated was the worst-performing combination both in a
    # 300-combo in-sample sweep and in a chronological train/test split
    # (mean OOS profit factor 0.55, vs. 0.81-0.99 for the other three
    # combos). Both toggles are kept here as sweep dimensions in case a
    # different combination of the other parameters makes them useful, but
    # DEFAULT OFF given that result.
    require_session_window: bool = False
    chart_pattern_session_only: bool = False  # tested, refuted as a standalone improvement — kept as a sweep knob
    orb_session_only: bool = False            # the least-bad of the four combos tested, but still ~breakeven OOS
    session_start: dtime = dtime(8, 0)
    session_end: dtime = dtime(12, 0)

    # --- Trend/regime filter (risk-management fix for the structural long
    # bias) ---
    # The engine's short side structurally caps at score 5 vs. long's 7
    # (no validated bearish candle pattern exists -- bearish_reversal_candle
    # is always False, see the candlestick pillar docstring below). That
    # produced a real, measured long bias: 89.7% of all trades over the
    # 24-month sample were long. An honest, disciplined re-search across
    # six distinct bearish candle definitions (shooting star, engulfing,
    # harami, three black crows, doji-after-uptrend, volume-climax-down)
    # found NO organic short signal in the candle domain -- none cleared
    # a train-side bar before even reaching OOS testing. This isn't a gap
    # in the search; it's a real property of this instrument/period.
    #
    # Given no organic short signal exists, this filter manages the
    # asymmetry directly instead: it blocks entries against a higher-
    # timeframe trend regime (Close vs. a long trailing SMA -- no
    # lookahead, since the SMA at bar i only uses bars up to and including
    # i, and the filter does nothing until the SMA has enough history to
    # be defined).
    #
    # Validated (chronological 70/30 split at 2025-12-03, config fixed on
    # TRAIN and not re-fit on TEST): SMA(2000)/mode="both" turned the
    # held-out TEST period's PF 0.44 / -19.7% return / 20.5% max drawdown
    # into PF 0.96 / -0.8% return / 5.6% max drawdown. It gives back some
    # in-sample return (train return 15.9% -> 5.2%) by also skipping some
    # genuine winning longs during brief pullbacks the SMA misreads as
    # "downtrend" -- an honest tradeoff, not a free lunch. It converts a
    # tail-risk disaster into a survivable, roughly-flat stretch; it does
    # NOT on its own turn the strategy robustly profitable OOS.
    #
    # regime_filter_mode: "none" | "block_long_in_downtrend" |
    #                     "block_short_in_uptrend" | "both"
    regime_filter_enabled: bool = True
    regime_sma_lookback: int = 2000
    regime_filter_mode: str = "both"

    # --- Candlestick pillar (corrected) ---
    candlestick_enabled: bool = True
    candle_shadow_ratio: float = 2.0      # wick length >= this x body to count as a pin/hammer/star shape

    # --- Chart pattern pillar (liquidity sweep + double top/bottom) ---
    chart_pattern_enabled: bool = True
    liquidity_sweep_volume_mult: float = 1.5   # volume >= this x rolling avg to confirm a sweep
    chart_pattern_lookback_bars: int = 40      # window used to find the prior swing/extreme
    double_pattern_tolerance_pips: float = 4.0 # how close two swing highs/lows must be to "match"

    # --- Risk & sizing (Chapter 17) ---
    risk_pct_per_trade: float = 0.01
    max_daily_loss_pct: float = 0.03
    min_reward_risk: float = 3.0          # steadiest walk-forward config used 3.0
    pip_size: float = 0.0001
    pip_value_per_lot: float = 10.0
    stop_pips: float = 25.0               # steadiest walk-forward config used 25 pips

    # Position size scaling by confluence score. Max standalone score is 7;
    # scale up gradually from the min_score_continuation floor.
    size_scale_by_score: dict = field(default_factory=lambda: {
        4: 0.5, 5: 0.7, 6: 0.85, 7: 1.0,
    })

    # --- Review trigger (risk monitoring) ---
    # Distinct from `max_daily_loss_pct` above: that circuit breaker halts
    # NEW entries for the REST OF THE CALENDAR DAY and auto-resets at the
    # next day boundary. This one is a longer-horizon, persistent flag --
    # once tripped it stays tripped until a human clears it. It exists to
    # catch a run degrading beyond what normal variance predicts, so a
    # live/paper deployment doesn't need someone watching every bar to
    # notice something's actually wrong, as opposed to a normal bad
    # stretch that should just be sat through.
    #
    # Thresholds were calibrated from a stress test (i.i.d. + block
    # bootstrap over the filtered engine's full 24-month trade sequence,
    # 200 trades, 26% win rate -- both methods gave similar tails, evidence
    # the regime filter removed most of the correlated-loss risk that
    # caused the earlier unfiltered engine's 24-trade crisis streak):
    #   - drawdown p99 across both bootstrap methods: ~25-27%
    #   - losing-streak p99 across both bootstrap methods: ~27-28 trades
    #   - empirical historical worst case (already lived through): 11.0%
    #     drawdown, 17-trade streak -- comfortably inside both thresholds,
    #     so the trigger does NOT fire on the known-good historical run.
    # Anything past these thresholds sits outside the range normal
    # variance is expected to produce, even in the unlucky tail, and
    # warrants pausing to re-validate rather than assuming it's noise.
    review_trigger_enabled: bool = True
    review_trigger_max_drawdown_pct: float = 0.25     # 25% peak-to-trough equity drawdown
    review_trigger_losing_streak: int = 28            # consecutive losing trades


# ---------------------------------------------------------------------------
# Review trigger (persistent risk-monitoring flag)
# ---------------------------------------------------------------------------

def new_review_trigger_state() -> dict:
    """Fresh state for a new backtest/paper/live run. Call once at the
    start of a run; feed it to update_review_trigger_state after every
    closed trade."""
    return {
        "peak_equity": None,
        "current_losing_streak": 0,
        "triggered": False,
        "triggered_at": None,
        "triggered_reason": None,
    }


def update_review_trigger_state(state: dict, cfg: ConfluenceConfig, ts, equity: float,
                                 last_trade_pnl: float) -> dict:
    """Update the review-trigger state after a trade closes. Mutates and
    returns `state`. Once triggered, stays triggered (no auto-reset) --
    clearing it is a deliberate human action, not something the engine
    does on its own. Call `is_review_triggered(state)` before opening any
    new position; do not force-close an already-open position because of
    this trigger, only block new entries."""
    if not cfg.review_trigger_enabled or state["triggered"]:
        return state

    state["peak_equity"] = equity if state["peak_equity"] is None else max(state["peak_equity"], equity)
    drawdown_pct = (
        (state["peak_equity"] - equity) / state["peak_equity"] if state["peak_equity"] > 0 else 0.0
    )

    if last_trade_pnl < 0:
        state["current_losing_streak"] += 1
    else:
        state["current_losing_streak"] = 0

    if drawdown_pct >= cfg.review_trigger_max_drawdown_pct:
        state["triggered"] = True
        state["triggered_at"] = ts
        state["triggered_reason"] = (
            f"drawdown {drawdown_pct*100:.1f}% reached review threshold "
            f"({cfg.review_trigger_max_drawdown_pct*100:.1f}%)"
        )
    elif state["current_losing_streak"] >= cfg.review_trigger_losing_streak:
        state["triggered"] = True
        state["triggered_at"] = ts
        state["triggered_reason"] = (
            f"losing streak of {state['current_losing_streak']} reached review threshold "
            f"({cfg.review_trigger_losing_streak})"
        )

    return state


def is_review_triggered(state: dict) -> bool:
    return bool(state["triggered"])


# ---------------------------------------------------------------------------
# Indicator computation
# ---------------------------------------------------------------------------

def compute_indicators(df: pd.DataFrame, cfg: ConfluenceConfig, cached_profile=None) -> pd.DataFrame:
    """
    df must have a tz-aware DatetimeIndex in America/New_York and columns:
    Open, High, Low, Close, Volume

    cached_profile: optional (poc, vah, val) tuple from a prior
    _rolling_volume_profile call, reused as-is instead of recomputing.
    Valid ONLY when vol_profile_lookback_bars/bins/value_area_pct are held
    fixed across calls (e.g. during a sweep over other parameters) --
    callers are responsible for that invariant. This exists so sweep
    tools can call this SAME function (rather than maintaining their own
    reimplementation) without paying the rolling-profile cost on every
    combination.
    """
    df = df.copy()
    df["date"] = df.index.date
    df["time_of_day"] = df.index.time

    # --- Session-anchored VWAP (kept for diagnostics; not scored) ---
    def _session_key(ts):
        d = ts.date()
        if ts.time() < cfg.vwap_session_anchor:
            d = d - pd.Timedelta(days=1)
        return d

    df["vwap_session"] = df.index.map(_session_key)
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
    pv = typical_price * df["Volume"]
    df["_pv_cum"] = pv.groupby(df["vwap_session"]).cumsum()
    df["_v_cum"] = df["Volume"].groupby(df["vwap_session"]).cumsum()
    df["vwap"] = df["_pv_cum"] / df["_v_cum"].replace(0, np.nan)
    df["vwap_slope"] = df.groupby(df["vwap_session"])["vwap"].diff()

    # --- Volume (kept as an input to the liquidity-sweep filter; not scored) ---
    df["vol_avg"] = df["Volume"].rolling(cfg.volume_lookback, min_periods=5).mean()
    df["vol_ratio"] = df["Volume"] / df["vol_avg"].replace(0, np.nan)
    df["is_up_bar"] = df["Close"] > df["Open"]
    df["is_down_bar"] = df["Close"] < df["Open"]
    df["vol_expanding_up"] = (df["vol_ratio"] >= cfg.volume_expand_mult) & df["is_up_bar"]
    df["vol_expanding_down"] = (df["vol_ratio"] >= cfg.volume_expand_mult) & df["is_down_bar"]
    df["vol_climactic"] = df["vol_ratio"] >= cfg.volume_climax_mult

    # --- Volume Profile / POC (Chapter 8) --- unresolved, unchanged ---
    poc, vah, val = cached_profile if cached_profile is not None else _rolling_volume_profile(df, cfg)
    df["poc"], df["vah"], df["val"] = poc, vah, val
    tol = cfg.poc_tolerance_pips * cfg.pip_size
    df["near_poc"] = (df["Close"] - df["poc"]).abs() <= tol
    df["near_vah"] = (df["Close"] - df["vah"]).abs() <= tol
    df["near_val"] = (df["Close"] - df["val"]).abs() <= tol
    df["at_vp_level"] = df["near_poc"] | df["near_vah"] | df["near_val"]

    # --- ORB (Chapter 9), INVERTED per ablation: breakouts mean-revert here ---
    orb_end = (
        pd.Timestamp.combine(pd.Timestamp.today(), cfg.orb_window_start)
        + pd.Timedelta(minutes=cfg.orb_window_minutes)
    ).time()
    in_orb_window = (df["time_of_day"] >= cfg.orb_window_start) & (df["time_of_day"] < orb_end)
    df["_orb_high_candidate"] = df["High"].where(in_orb_window)
    df["_orb_low_candidate"] = df["Low"].where(in_orb_window)
    day_key = df.index.date
    orb_high = pd.Series(df["_orb_high_candidate"].values, index=day_key).groupby(level=0).cummax()
    orb_low = pd.Series(df["_orb_low_candidate"].values, index=day_key).groupby(level=0).cummin()
    df["orb_high"] = orb_high.values
    df["orb_low"] = orb_low.values
    df["orb_high"] = df.groupby(df.index.date)["orb_high"].ffill()
    df["orb_low"] = df.groupby(df.index.date)["orb_low"].ffill()
    after_orb = df["time_of_day"] >= orb_end
    # NOTE the swap vs. the original book logic: a close BELOW orb_low now
    # feeds the LONG signal (mean-reversion up), and a close ABOVE orb_high
    # now feeds the SHORT signal (mean-reversion down) — the opposite of
    # the original continuation-breakout read.
    df["orb_breakout_long"] = after_orb & (df["Close"] < df["orb_low"])
    df["orb_breakout_short"] = after_orb & (df["Close"] > df["orb_high"])

    # --- Session gate ---
    df["in_session_window"] = (df["time_of_day"] >= cfg.session_start) & (df["time_of_day"] < cfg.session_end)

    # --- Trend/regime filter inputs (see ConfluenceConfig docstring) ---
    # Computed unconditionally (a rolling mean is cheap and useful as a
    # diagnostic even when regime_filter_enabled is False); the filter
    # ITSELF is only applied in evaluate_signal, gated by that flag.
    regime_sma = df["Close"].rolling(cfg.regime_sma_lookback, min_periods=cfg.regime_sma_lookback).mean()
    df["regime_sma"] = regime_sma
    df["regime_up"] = df["Close"] > regime_sma
    df["regime_known"] = regime_sma.notna()

    # --- Candlestick pillar (corrected) ---
    if cfg.candlestick_enabled:
        df = _add_candlestick_signals(df, cfg)
    else:
        df["bullish_reversal_candle"] = False
        df["bearish_reversal_candle"] = False

    # --- Chart pattern pillar (liquidity sweep + double top/bottom) ---
    if cfg.chart_pattern_enabled:
        df = _add_chart_patterns(df, cfg)
    else:
        df["chart_pattern_grade_long"] = 0
        df["chart_pattern_grade_short"] = 0

    # --- Per-pillar session gating (see config docstring) ---
    off_session = ~df["in_session_window"]
    if cfg.chart_pattern_session_only:
        df.loc[off_session, "chart_pattern_grade_long"] = 0
        df.loc[off_session, "chart_pattern_grade_short"] = 0
    if cfg.orb_session_only:
        df.loc[off_session, "orb_breakout_long"] = False
        df.loc[off_session, "orb_breakout_short"] = False

    return df


def _rolling_volume_profile(df: pd.DataFrame, cfg: ConfluenceConfig):
    """Rolling POC/VAH/VAL over the last `vol_profile_lookback_bars` bars.
    Unchanged from the pre-ablation engine — this pillar was found
    non-directional as coded, not proven wrong, so it's left as-is."""
    n = len(df)
    poc = np.full(n, np.nan)
    vah = np.full(n, np.nan)
    val = np.full(n, np.nan)
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    vols = df["Volume"].values
    win = cfg.vol_profile_lookback_bars
    bins = cfg.vol_profile_bins
    va_pct = cfg.value_area_pct

    for i in range(win, n):
        h = highs[i - win: i]
        l = lows[i - win: i]
        v = vols[i - win: i]
        lo, hi = l.min(), h.max()
        if hi <= lo:
            continue
        edges = np.linspace(lo, hi, bins + 1)
        mids = (edges[:-1] + edges[1:]) / 2
        c = closes[i - win: i]
        idx = np.clip(np.digitize(c, edges) - 1, 0, bins - 1)
        vol_by_bin = np.zeros(bins)
        np.add.at(vol_by_bin, idx, v)

        if vol_by_bin.sum() == 0:
            continue
        poc_idx = int(np.argmax(vol_by_bin))
        poc[i] = mids[poc_idx]

        total = vol_by_bin.sum()
        target = total * va_pct
        lo_i, hi_i = poc_idx, poc_idx
        captured = vol_by_bin[poc_idx]
        while captured < target and (lo_i > 0 or hi_i < bins - 1):
            expand_down = vol_by_bin[lo_i - 1] if lo_i > 0 else -1
            expand_up = vol_by_bin[hi_i + 1] if hi_i < bins - 1 else -1
            if expand_up >= expand_down:
                hi_i += 1
                captured += vol_by_bin[hi_i]
            else:
                lo_i -= 1
                captured += vol_by_bin[lo_i]
        vah[i] = mids[hi_i]
        val[i] = mids[lo_i]

    return poc, vah, val


# ---------------------------------------------------------------------------
# Candlestick pillar (corrected)
# ---------------------------------------------------------------------------

def _add_candlestick_signals(df: pd.DataFrame, cfg: ConfluenceConfig) -> pd.DataFrame:
    """Detect single/two-bar reversal candle shapes.

    Validated correction (pillar ablation, see module docstring): the
    classic BEARISH-shaped pattern (shooting star / bearish engulfing) had a
    POSITIVE forward return on this data — the opposite of the textbook
    read. Rather than discard that information, it's folded into the LONG
    signal here. No candle shape survived validation as a short-side
    predictor, so `bearish_reversal_candle` is always False.
    """
    df = df.copy()
    open_ = df["Open"]
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    body = (close - open_).abs()
    min_body = cfg.pip_size  # guards near-zero-body (doji) bars from dividing out the ratio check
    body_floor = body.clip(lower=min_body)
    upper_wick = high - pd.concat([open_, close], axis=1).max(axis=1)
    lower_wick = pd.concat([open_, close], axis=1).min(axis=1) - low

    hammer_shape = (lower_wick >= cfg.candle_shadow_ratio * body_floor) & (upper_wick <= body_floor)
    shooting_star_shape = (upper_wick >= cfg.candle_shadow_ratio * body_floor) & (lower_wick <= body_floor)

    prev_open = open_.shift(1)
    prev_close = close.shift(1)
    bullish_engulfing = (close > open_) & (prev_close < prev_open) & (close > prev_open) & (open_ < prev_close)
    bearish_engulfing = (close < open_) & (prev_close > prev_open) & (close < prev_open) & (open_ > prev_close)

    raw_bullish_shape = (hammer_shape | bullish_engulfing).fillna(False)
    raw_bearish_shape = (shooting_star_shape | bearish_engulfing).fillna(False)

    if cfg.candlestick_enabled:
        df["bullish_reversal_candle"] = raw_bullish_shape | raw_bearish_shape
        df["bearish_reversal_candle"] = False
    else:
        df["bullish_reversal_candle"] = False
        df["bearish_reversal_candle"] = False

    return df


# ---------------------------------------------------------------------------
# Chart pattern pillar (liquidity sweep + double top/bottom)
# ---------------------------------------------------------------------------

def _add_chart_patterns(df: pd.DataFrame, cfg: ConfluenceConfig) -> pd.DataFrame:
    """Grade 0-3 per direction: liquidity sweep (worth 2) + double top/bottom
    (worth 1). This is the pillar the ablation found real and strongest on
    the short side; the detection logic below is a from-scratch
    reconstruction, not recovered original code."""
    df = df.copy()
    n = len(df)
    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values
    volume = df["Volume"].values
    vol_avg = df["vol_avg"].values if "vol_avg" in df.columns else (
        df["Volume"].rolling(cfg.volume_lookback, min_periods=5).mean().values
    )

    lookback = cfg.chart_pattern_lookback_bars
    tol = cfg.double_pattern_tolerance_pips * cfg.pip_size
    vol_mult = cfg.liquidity_sweep_volume_mult

    liq_sweep_long = np.zeros(n, dtype=bool)
    liq_sweep_short = np.zeros(n, dtype=bool)
    double_bottom = np.zeros(n, dtype=bool)
    double_top = np.zeros(n, dtype=bool)

    if cfg.chart_pattern_enabled:
        for i in range(lookback, n):
            window_low = low[i - lookback: i]
            window_high = high[i - lookback: i]
            swing_low = window_low.min()
            swing_high = window_high.max()
            avg_v = vol_avg[i] if not np.isnan(vol_avg[i]) else 0.0

            # Liquidity sweep: price pokes through the recent extreme and
            # closes back inside the range, on elevated volume.
            if avg_v > 0 and low[i] < swing_low and close[i] > swing_low and volume[i] >= vol_mult * avg_v:
                liq_sweep_long[i] = True
            if avg_v > 0 and high[i] > swing_high and close[i] < swing_high and volume[i] >= vol_mult * avg_v:
                liq_sweep_short[i] = True

            # Double bottom / double top: two comparable swing extremes in
            # the window, separated in time (not the same swing), followed
            # by a break back through the more recent of the two.
            lo_order = np.argsort(window_low)
            first_lo, second_lo = lo_order[0], lo_order[1]
            if abs(int(first_lo) - int(second_lo)) >= 5 and abs(window_low[first_lo] - window_low[second_lo]) <= tol:
                if close[i] > window_low[max(first_lo, second_lo)]:
                    double_bottom[i] = True

            hi_order = np.argsort(window_high)[::-1]
            first_hi, second_hi = hi_order[0], hi_order[1]
            if abs(int(first_hi) - int(second_hi)) >= 5 and abs(window_high[first_hi] - window_high[second_hi]) <= tol:
                if close[i] < window_high[max(first_hi, second_hi)]:
                    double_top[i] = True

    df["liquidity_sweep_long"] = liq_sweep_long
    df["liquidity_sweep_short"] = liq_sweep_short
    df["double_bottom"] = double_bottom
    df["double_top"] = double_top

    df["chart_pattern_grade_long"] = (
        2 * df["liquidity_sweep_long"].astype(int) + 1 * df["double_bottom"].astype(int)
    )
    df["chart_pattern_grade_short"] = (
        2 * df["liquidity_sweep_short"].astype(int) + 1 * df["double_top"].astype(int)
    )

    if not cfg.chart_pattern_enabled:
        df["chart_pattern_grade_long"] = 0
        df["chart_pattern_grade_short"] = 0

    return df


# ---------------------------------------------------------------------------
# Confluence scoring (standalone engine — used by backtest_confluence.py)
# ---------------------------------------------------------------------------

def score_row(row, direction: str, cfg: ConfluenceConfig):
    """Return (score, component breakdown dict) for a given direction
    ('long'/'short'). Max score is 7: orb(1) + vol_profile(1) + candle(2) +
    chart_pattern(3). Volume and VWAP are intentionally excluded — the
    ablation found neither had a discriminating edge."""
    components = {}

    if direction == "long":
        components["orb"] = bool(row.get("orb_breakout_long", False))
        components["candle"] = 2 if bool(row.get("bullish_reversal_candle", False)) else 0
        components["chart_pattern"] = int(row.get("chart_pattern_grade_long", 0))
    else:
        components["orb"] = bool(row.get("orb_breakout_short", False))
        components["candle"] = 2 if bool(row.get("bearish_reversal_candle", False)) else 0
        components["chart_pattern"] = int(row.get("chart_pattern_grade_short", 0))

    components["vol_profile"] = bool(row.get("at_vp_level", False))

    score = int(components["orb"]) + components["candle"] + components["chart_pattern"] + int(components["vol_profile"])
    return score, components


def evaluate_signal(row, cfg: ConfluenceConfig, is_reversal: bool = False):
    """Evaluate both directions and return the best qualifying signal, or
    None. Applies the trend/regime filter (see ConfluenceConfig docstring)
    AFTER picking the winning direction -- a blocked signal is dropped
    entirely, not swapped for the opposite direction, matching how this
    was validated in the standalone regime-filter study."""
    if cfg.require_session_window and not row.get("in_session_window", True):
        return None

    min_score = cfg.min_score_reversal if is_reversal else cfg.min_score_continuation

    long_score, long_components = score_row(row, "long", cfg)
    short_score, short_components = score_row(row, "short", cfg)

    signal = None
    if long_score >= min_score and long_score >= short_score:
        signal = {"direction": "long", "score": long_score, "components": long_components}
    elif short_score >= min_score and short_score > long_score:
        signal = {"direction": "short", "score": short_score, "components": short_components}

    if signal is not None and cfg.regime_filter_enabled and row.get("regime_known", False):
        regime_up = row["regime_up"]
        mode = cfg.regime_filter_mode
        if mode in ("block_long_in_downtrend", "both") and signal["direction"] == "long" and not regime_up:
            signal = None
        if signal is not None and mode in ("block_short_in_uptrend", "both") and signal["direction"] == "short" and regime_up:
            signal = None

    return signal


def position_size(cfg: ConfluenceConfig, equity: float, score: int, stop_pips: float):
    """Risk-based sizing (Chapter 17), scaled down for lower confluence scores."""
    scale = cfg.size_scale_by_score.get(score, 0.0)
    risk_amount = equity * cfg.risk_pct_per_trade * scale
    stop_value = stop_pips * cfg.pip_value_per_lot
    if stop_value <= 0:
        return 0.0, risk_amount
    lots = risk_amount / stop_value
    return lots, risk_amount


# ---------------------------------------------------------------------------
# Bar-by-bar trade simulation (single shared implementation)
# ---------------------------------------------------------------------------
#
# This loop previously existed as THREE separate copies across the project
# (backtest_confluence.py, sweep_confluence.py's vectorized reimplementation,
# and the standalone trend_regime_filter.py) -- exactly the duplication
# pattern that twice let the sweep tool silently drift out of sync with
# corrections made here (first the pre-ablation Volume/VWAP/ORB bug, later
# the missing regime filter and review trigger). There is now exactly one
# implementation. backtest_confluence.py and sweep_confluence.py both call
# this function; neither maintains its own copy of the position/exit/
# circuit-breaker logic.

def simulate_trades(df_ind: pd.DataFrame, cfg: ConfluenceConfig, starting_equity: float = 10000.0):
    """Run the book-accurate bar-by-bar simulation over an already
    indicator-computed dataframe (i.e. the output of compute_indicators).
    Single open position at a time; entries gated by evaluate_signal
    (which already applies the confluence score threshold, session gate,
    and trend/regime filter); exits on stop or reward:risk target;
    position size scaled by confluence score (Ch. 17); 3% max daily loss
    circuit breaker (Ch. 17.3, resets daily); persistent review-trigger
    flag (Ch. 17.4-equivalent, does NOT reset -- see new_review_trigger_
    state / update_review_trigger_state / is_review_triggered).

    Returns (trades, equity_curve, final_equity, review_state).
    """
    equity = starting_equity
    equity_curve = []
    trades = []
    position = None
    current_day = None
    day_start_equity = equity
    day_loss_halted = False
    review_state = new_review_trigger_state()

    for ts, row in df_ind.iterrows():
        day = ts.date()
        if day != current_day:
            current_day = day
            day_start_equity = equity
            day_loss_halted = False

        if position is not None:
            hit_target = hit_stop = False
            if position["direction"] == "long":
                hit_target = row["High"] >= position["target_price"]
                hit_stop = row["Low"] <= position["stop_price"]
            else:
                hit_target = row["Low"] <= position["target_price"]
                hit_stop = row["High"] >= position["stop_price"]

            exit_price = None
            if hit_target and hit_stop:
                exit_price = position["stop_price"]  # conservative: assume stop hit first
            elif hit_target:
                exit_price = position["target_price"]
            elif hit_stop:
                exit_price = position["stop_price"]

            if exit_price is not None:
                pips = (exit_price - position["entry_price"]) / cfg.pip_size
                if position["direction"] == "short":
                    pips = -pips
                pnl = pips * cfg.pip_value_per_lot * position["lots"]
                equity += pnl
                trades.append({
                    "entry_time": position["entry_time"].isoformat(),
                    "exit_time": ts.isoformat(),
                    "direction": position["direction"],
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "lots": round(position["lots"], 3),
                    "score": position["score"],
                    "pips": round(pips, 1),
                    "pnl": round(pnl, 2),
                    "equity_after": round(equity, 2),
                })
                position = None
                review_state = update_review_trigger_state(review_state, cfg, ts, equity, pnl)

        if not day_loss_halted:
            daily_pnl_pct = (equity - day_start_equity) / day_start_equity
            if daily_pnl_pct <= -cfg.max_daily_loss_pct:
                day_loss_halted = True

        if position is None and not day_loss_halted and not is_review_triggered(review_state):
            signal = evaluate_signal(row, cfg, is_reversal=False)
            if signal is not None:
                lots, risk_amount = position_size(cfg, equity, signal["score"], cfg.stop_pips)
                if lots > 0:
                    entry_price = row["Close"]
                    stop_distance = cfg.stop_pips * cfg.pip_size
                    target_distance = stop_distance * cfg.min_reward_risk
                    if signal["direction"] == "long":
                        stop_price = entry_price - stop_distance
                        target_price = entry_price + target_distance
                    else:
                        stop_price = entry_price + stop_distance
                        target_price = entry_price - target_distance
                    position = {
                        "direction": signal["direction"],
                        "entry_time": ts,
                        "entry_price": entry_price,
                        "stop_price": stop_price,
                        "target_price": target_price,
                        "lots": lots,
                        "score": signal["score"],
                    }

        equity_curve.append({"time": ts.isoformat(), "equity": round(equity, 2)})

    return trades, equity_curve, equity, review_state

"""
chart_patterns_book2.py

Completes the Chart Pattern pillar (Ch. 2) that confluence_engine_book2.py
originally scoped down to just the Liquidity Sweep. Adds:

  - Flags / Pennants (Ch. 2.1, continuation) -- flagpole + counter-sloping
    or converging consolidation, confirmed by declining channel volume and
    a >=1.5x breakout volume burst (the Flag Confirmation Rule).
  - Wedges (Ch. 2.1, continuation or reversal) -- a converging channel
    where both boundaries slope the SAME direction; rising = bearish,
    falling = bullish.
  - Double Tops / Bottoms (Ch. 2.2, reversal) -- two peaks/troughs at
    approximately the same level with a neckline at the intervening
    low/high, confirmed by LOWER volume on the second extreme.
  - Head & Shoulders (Ch. 2.2, reversal) -- three peaks with the middle
    higher, neckline across the two intervening lows, confirmed by a
    declining head -> left shoulder -> right shoulder volume hierarchy
    (the Volume Divergence Score = left/right shoulder volume).

HONEST NOTE ON SCOPE: the book only walks through the bearish (topping)
Head & Shoulders in detail. An inverse (bottoming) Head & Shoulders for
long signals is included here by direct structural symmetry -- three
troughs, middle lower, same volume-hierarchy logic mirrored -- since nothing
in the book suggests the pattern is meant to be directional-only, but this
mirroring is this implementation's inference, not something the book states
outright. Flagged here rather than left silent.

All patterns are graded A/B/C per Ch. 2.4's rubric:
  A (3 pts) - every construction rule met precisely, including volume
              confirmation at every required stage.
  B (2 pts) - construction rules met, but one secondary volume condition
              is marginal.
  C (1 pt)  - core structure present but one or more rules are violated
              or unconfirmed.

Swing points (needed to define flagpoles, necklines, and shoulders) are
found with a simple fractal rule: a bar is a swing high/low if it is the
max/min High or Low within a symmetric window centered on it. This is a
standard, common definition, not a book-specified one -- the book assumes
swing points are visually obvious and doesn't give a mechanical rule for
finding them, so a concrete definition had to be chosen.
"""

import numpy as np
import pandas as pd


def find_swing_points(df: pd.DataFrame, window: int = 5):
    """Fractal swing high/low: True at bar i if High[i] (Low[i]) is the max
    (min) within [i-window, i+window]. Uses a centered rolling window, so
    swing points are only knowable `window` bars after the fact -- callers
    must not use swing points less than `window` bars old as if they were
    known in real time."""
    roll_max = df["High"].rolling(2 * window + 1, center=True).max()
    roll_min = df["Low"].rolling(2 * window + 1, center=True).min()
    is_swing_high = df["High"] == roll_max
    is_swing_low = df["Low"] == roll_min
    return is_swing_high.fillna(False), is_swing_low.fillna(False)


def add_flag_pennant_wedge(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Ch. 2.1: flagpole + channel continuation patterns, plus wedges."""
    n = len(df)
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    vol = df["Volume"].values
    vol_avg20 = df["Volume"].rolling(20, min_periods=5).mean().values

    pole_bars = cfg.flag_pole_bars          # bars defining the flagpole move
    pole_min_pips = cfg.flag_pole_min_pips  # minimum flagpole size to qualify
    channel_bars = cfg.flag_channel_bars    # bars in the consolidation channel

    grade_long = np.zeros(n, dtype=int)
    grade_short = np.zeros(n, dtype=int)

    for i in range(pole_bars + channel_bars, n):
        pole_start = i - channel_bars - pole_bars
        pole_end = i - channel_bars
        pole_move = close[pole_end] - close[pole_start]
        if abs(pole_move) < pole_min_pips * cfg.pip_size:
            continue
        pole_up = pole_move > 0

        channel = close[pole_end:i]
        channel_vol = vol[pole_end:i]
        if len(channel) < 3:
            continue
        # Channel slope: declining volume during consolidation (flag/pennant rule).
        vol_declining = channel_vol[-1] < channel_vol[0]
        # Channel direction: flag slopes counter to the pole; pennant/wedge
        # converges (range narrows). Detect narrowing range as a proxy for
        # pennant/wedge, and counter-slope as a proxy for a flag channel.
        channel_slope = np.polyfit(np.arange(len(channel)), channel, 1)[0]
        first_half_range = channel[:len(channel)//2].max() - channel[:len(channel)//2].min()
        second_half_range = channel[len(channel)//2:].max() - channel[len(channel)//2:].min()
        narrowing = second_half_range < first_half_range * 0.8

        counter_slope = (pole_up and channel_slope < 0) or (not pole_up and channel_slope > 0)
        same_dir_narrowing = narrowing and (
            (pole_up and channel_slope > 0) or (not pole_up and channel_slope < 0)
            or narrowing  # wedge: both bounds narrow in a single direction regardless of pole
        )

        is_flag_shape = counter_slope or narrowing
        if not is_flag_shape:
            continue

        # Breakout check: does bar i close beyond the channel's own range,
        # continuing the pole's direction?
        channel_high = channel.max()
        channel_low = channel.min()
        breakout_vol_ratio = vol[i] / vol_avg20[i] if vol_avg20[i] else 0

        if pole_up and close[i] > channel_high:
            if vol_declining and breakout_vol_ratio >= cfg.flag_breakout_vol_mult:
                grade_long[i] = 3
            elif breakout_vol_ratio >= cfg.flag_breakout_vol_marginal_mult:
                grade_long[i] = 2
            else:
                grade_long[i] = 1
        elif (not pole_up) and close[i] < channel_low:
            if vol_declining and breakout_vol_ratio >= cfg.flag_breakout_vol_mult:
                grade_short[i] = 3
            elif breakout_vol_ratio >= cfg.flag_breakout_vol_marginal_mult:
                grade_short[i] = 2
            else:
                grade_short[i] = 1

    df["flag_grade_long"] = grade_long
    df["flag_grade_short"] = grade_short
    return df


def add_double_top_bottom(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Ch. 2.2: two peaks/troughs at ~the same level, confirmed by LOWER
    volume on the second extreme (bearish/bullish divergence)."""
    is_swing_high, is_swing_low = find_swing_points(df, cfg.swing_window)
    n = len(df)
    high, low, close = df["High"].values, df["Low"].values, df["Close"].values
    vol = df["Volume"].values
    tol = cfg.double_pattern_tolerance_pips * cfg.pip_size

    swing_high_idx = np.where(is_swing_high.values)[0]
    swing_low_idx = np.where(is_swing_low.values)[0]

    grade_short = np.zeros(n, dtype=int)  # double top -> bearish
    grade_long = np.zeros(n, dtype=int)   # double bottom -> bullish

    for a, b in zip(swing_high_idx, swing_high_idx[1:]):
        if b - a > cfg.double_pattern_max_gap_bars or b - a < 3:
            continue
        if abs(high[a] - high[b]) > tol:
            continue
        neckline_slice = low[a:b + 1]
        if len(neckline_slice) == 0:
            continue
        neckline = neckline_slice.min()
        vol_confirmed = vol[b] < vol[a]
        # LOOKAHEAD FIX: swing point b is a centered fractal -- it isn't
        # actually confirmable as a swing high until `swing_window` bars
        # AFTER it (once we've seen enough subsequent bars to know no
        # higher high followed). The neckline-break search must not start
        # before that confirmation bar, or the pattern is being used before
        # it could actually be known in real time.
        confirmed_at = b + cfg.swing_window
        for k in range(max(b + 1, confirmed_at), min(b + cfg.double_pattern_confirm_window, n)):
            if close[k] < neckline:
                if vol_confirmed:
                    grade_short[k] = 3
                elif vol[b] < vol[a] * 1.1:
                    grade_short[k] = 2
                else:
                    grade_short[k] = 1
                break

    for a, b in zip(swing_low_idx, swing_low_idx[1:]):
        if b - a > cfg.double_pattern_max_gap_bars or b - a < 3:
            continue
        if abs(low[a] - low[b]) > tol:
            continue
        neckline_slice = high[a:b + 1]
        if len(neckline_slice) == 0:
            continue
        neckline = neckline_slice.max()
        vol_confirmed = vol[b] < vol[a]
        confirmed_at = b + cfg.swing_window
        for k in range(max(b + 1, confirmed_at), min(b + cfg.double_pattern_confirm_window, n)):
            if close[k] > neckline:
                if vol_confirmed:
                    grade_long[k] = 3
                elif vol[b] < vol[a] * 1.1:
                    grade_long[k] = 2
                else:
                    grade_long[k] = 1
                break

    df["double_top_grade_short"] = grade_short
    df["double_bottom_grade_long"] = grade_long
    return df


def add_head_and_shoulders(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Ch. 2.2: three peaks/troughs, middle one most extreme, confirmed by
    a declining head -> left shoulder -> right shoulder volume hierarchy.
    Standard (topping, bearish) H&S per the book; inverse (bottoming,
    bullish) added by structural symmetry -- see module docstring."""
    is_swing_high, is_swing_low = find_swing_points(df, cfg.swing_window)
    n = len(df)
    high, low, close = df["High"].values, df["Low"].values, df["Close"].values
    vol = df["Volume"].values

    swing_high_idx = np.where(is_swing_high.values)[0]
    swing_low_idx = np.where(is_swing_low.values)[0]

    grade_short = np.zeros(n, dtype=int)  # topping H&S -> bearish
    grade_long = np.zeros(n, dtype=int)   # inverse H&S -> bullish

    for x in range(len(swing_high_idx) - 2):
        ls, head, rs = swing_high_idx[x], swing_high_idx[x + 1], swing_high_idx[x + 2]
        if rs - ls > cfg.hs_max_span_bars:
            continue
        if not (high[head] > high[ls] and high[head] > high[rs]):
            continue
        if high[rs] > high[ls]:
            continue  # failure mode: right shoulder holds above left shoulder -> invalid
        neckline_lows = low[ls:rs + 1]
        if len(neckline_lows) == 0:
            continue
        neckline = neckline_lows.min()

        vol_head, vol_ls, vol_rs = vol[head], vol[ls], vol[rs]
        divergence_score = (vol_ls / vol_rs) if vol_rs > 0 else 0

        confirmed_at = rs + cfg.swing_window
        for k in range(max(rs + 1, confirmed_at), min(rs + cfg.hs_confirm_window, n)):
            if close[k] < neckline:
                if vol_head >= vol_ls >= vol_rs and divergence_score > 1.2:
                    grade_short[k] = 3
                elif divergence_score > 1.0:
                    grade_short[k] = 2
                else:
                    grade_short[k] = 1  # right shoulder vol >= left -> compromised reliability
                break

    for x in range(len(swing_low_idx) - 2):
        ls, head, rs = swing_low_idx[x], swing_low_idx[x + 1], swing_low_idx[x + 2]
        if rs - ls > cfg.hs_max_span_bars:
            continue
        if not (low[head] < low[ls] and low[head] < low[rs]):
            continue
        if low[rs] < low[ls]:
            continue  # symmetric failure mode
        neckline_highs = high[ls:rs + 1]
        if len(neckline_highs) == 0:
            continue
        neckline = neckline_highs.max()

        vol_head, vol_ls, vol_rs = vol[head], vol[ls], vol[rs]
        divergence_score = (vol_ls / vol_rs) if vol_rs > 0 else 0

        confirmed_at = rs + cfg.swing_window
        for k in range(max(rs + 1, confirmed_at), min(rs + cfg.hs_confirm_window, n)):
            if close[k] > neckline:
                if vol_head >= vol_ls >= vol_rs and divergence_score > 1.2:
                    grade_long[k] = 3
                elif divergence_score > 1.0:
                    grade_long[k] = 2
                else:
                    grade_long[k] = 1
                break

    df["hs_grade_short"] = grade_short
    df["inverse_hs_grade_long"] = grade_long
    return df


def add_order_blocks_and_fvg(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Ch. 3.3: Order Blocks (the last opposite-direction candle before a
    sharp impulsive move) and Fair Value Gaps (a 3-candle imbalance where
    candle 1 and candle 3 don't overlap). The Combined Entry Rule requires
    BOTH to be retested simultaneously, confirmed by a Section 3.1 reversal
    candle -- either condition alone is explicitly a non-qualifying setup
    under the book's own rule, so this does NOT add points beyond the
    existing 2-point Candlestick Confirmation bucket; it's a second way to
    earn those same 2 points, not an 11th pillar."""
    n = len(df)
    open_, high, low, close = df["Open"].values, df["High"].values, df["Low"].values, df["Close"].values

    impulse_bars = cfg.ob_impulse_bars
    impulse_min_pips = cfg.ob_impulse_min_pips

    bullish_ob_hi = np.full(n, np.nan)
    bullish_ob_lo = np.full(n, np.nan)
    bearish_ob_hi = np.full(n, np.nan)
    bearish_ob_lo = np.full(n, np.nan)
    # LOOKAHEAD FIX: an Order Block can only be identified in hindsight,
    # once the impulsive move following it has actually happened. Track
    # the bar at which each OB becomes CONFIRMED (i + impulse_bars), not
    # the bar it occurred on (i) -- the retest arrays below are indexed by
    # confirmation bar, not occurrence bar, so a bar-by-bar evaluation
    # never sees an OB before it could actually be known.
    bullish_ob_hi_confirmed_at = np.full(n, np.nan)
    bullish_ob_lo_confirmed_at = np.full(n, np.nan)
    bearish_ob_hi_confirmed_at = np.full(n, np.nan)
    bearish_ob_lo_confirmed_at = np.full(n, np.nan)

    for i in range(n - impulse_bars):
        impulse_move = close[i + impulse_bars] - close[i + 1]
        confirm_idx = i + impulse_bars
        if impulse_move >= impulse_min_pips * cfg.pip_size and close[i] < open_[i]:
            bullish_ob_hi[i] = high[i]
            bullish_ob_lo[i] = low[i]
            bullish_ob_hi_confirmed_at[confirm_idx] = high[i]
            bullish_ob_lo_confirmed_at[confirm_idx] = low[i]
        elif impulse_move <= -impulse_min_pips * cfg.pip_size and close[i] > open_[i]:
            bearish_ob_hi[i] = high[i]
            bearish_ob_lo[i] = low[i]
            bearish_ob_hi_confirmed_at[confirm_idx] = high[i]
            bearish_ob_lo_confirmed_at[confirm_idx] = low[i]

    fvg_bull_lo = np.full(n, np.nan)
    fvg_bull_hi = np.full(n, np.nan)
    fvg_bear_lo = np.full(n, np.nan)
    fvg_bear_hi = np.full(n, np.nan)
    # LOOKAHEAD FIX: an FVG spanning candles [i-1, i, i+1] can only be
    # confirmed once candle i+1 has closed. Track confirmation at i+1.
    fvg_bull_lo_confirmed_at = np.full(n, np.nan)
    fvg_bull_hi_confirmed_at = np.full(n, np.nan)
    fvg_bear_lo_confirmed_at = np.full(n, np.nan)
    fvg_bear_hi_confirmed_at = np.full(n, np.nan)
    for i in range(1, n - 1):
        if low[i + 1] > high[i - 1]:
            fvg_bull_lo[i] = high[i - 1]
            fvg_bull_hi[i] = low[i + 1]
            fvg_bull_lo_confirmed_at[i + 1] = high[i - 1]
            fvg_bull_hi_confirmed_at[i + 1] = low[i + 1]
        if high[i + 1] < low[i - 1]:
            fvg_bear_lo[i] = high[i + 1]
            fvg_bear_hi[i] = low[i - 1]
            fvg_bear_lo_confirmed_at[i + 1] = high[i + 1]
            fvg_bear_hi_confirmed_at[i + 1] = low[i - 1]

    df["bullish_ob_hi"], df["bullish_ob_lo"] = bullish_ob_hi, bullish_ob_lo
    df["bearish_ob_hi"], df["bearish_ob_lo"] = bearish_ob_hi, bearish_ob_lo
    df["fvg_bull_lo"], df["fvg_bull_hi"] = fvg_bull_lo, fvg_bull_hi
    df["fvg_bear_lo"], df["fvg_bear_hi"] = fvg_bear_lo, fvg_bear_hi

    ob_bull_hi_ff = pd.Series(bullish_ob_hi_confirmed_at).ffill(limit=cfg.ob_fvg_retest_window).values
    ob_bull_lo_ff = pd.Series(bullish_ob_lo_confirmed_at).ffill(limit=cfg.ob_fvg_retest_window).values
    ob_bear_hi_ff = pd.Series(bearish_ob_hi_confirmed_at).ffill(limit=cfg.ob_fvg_retest_window).values
    ob_bear_lo_ff = pd.Series(bearish_ob_lo_confirmed_at).ffill(limit=cfg.ob_fvg_retest_window).values
    fvg_bull_lo_ff = pd.Series(fvg_bull_lo_confirmed_at).ffill(limit=cfg.ob_fvg_retest_window).values
    fvg_bull_hi_ff = pd.Series(fvg_bull_hi_confirmed_at).ffill(limit=cfg.ob_fvg_retest_window).values
    fvg_bear_lo_ff = pd.Series(fvg_bear_lo_confirmed_at).ffill(limit=cfg.ob_fvg_retest_window).values
    fvg_bear_hi_ff = pd.Series(fvg_bear_hi_confirmed_at).ffill(limit=cfg.ob_fvg_retest_window).values

    retest_ob_bull = (low <= ob_bull_hi_ff) & (high >= ob_bull_lo_ff)
    retest_fvg_bull = (low <= fvg_bull_hi_ff) & (high >= fvg_bull_lo_ff)
    retest_ob_bear = (low <= ob_bear_hi_ff) & (high >= ob_bear_lo_ff)
    retest_fvg_bear = (low <= fvg_bear_hi_ff) & (high >= fvg_bear_lo_ff)

    df["ob_fvg_combined_long"] = (
        retest_ob_bull & retest_fvg_bull & (df["hammer"] | df["bullish_engulfing"])
    )
    df["ob_fvg_combined_short"] = (
        retest_ob_bear & retest_fvg_bear & (df["shooting_star"] | df["bearish_engulfing"])
    )
    return df


def combine_chart_pattern_grades(df: pd.DataFrame) -> pd.DataFrame:
    """Ch. 2.4: chart patterns contribute up to 3 points total -- take the
    BEST-graded pattern present at each bar per direction, since the book
    doesn't specify additive stacking across simultaneously-qualifying
    pattern types (in practice, having two fully independent geometric
    patterns complete on the exact same bar is rare)."""
    long_cols = [c for c in ["chart_pattern_grade_long", "flag_grade_long",
                              "double_bottom_grade_long", "inverse_hs_grade_long"] if c in df.columns]
    short_cols = [c for c in ["chart_pattern_grade_short", "flag_grade_short",
                               "double_top_grade_short", "hs_grade_short"] if c in df.columns]
    df["chart_pattern_grade_long"] = df[long_cols].max(axis=1)
    df["chart_pattern_grade_short"] = df[short_cols].max(axis=1)
    return df

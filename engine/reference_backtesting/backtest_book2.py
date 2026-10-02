"""
backtest_book2.py

Bar-by-bar simulation for the faithful Book 2 engine
(confluence_engine_book2.py). Mirrors backtest_confluence.py's loop
structure (single open position, daily loss circuit breaker, persistent
review trigger) but drives entries off evaluate_signal_book2's 0-10
score and 7/10 threshold instead of v1's scoring.

Exit rules are pluggable (ExitRules / EXIT_PRESETS). The default,
"baseline", is the live strategy: fixed 15-pip stop and a 2R target.

Usage:
    python3 backtest_book2.py --csv eur_usd_15m_24mo.csv
    python3 backtest_book2.py --csv eur_usd_15m_24mo.csv --exit-variant breakeven_1r
    python3 backtest_book2.py --csv eur_usd_15m_24mo.csv --compare --cost-pips 1.0

Intrabar ordering is unknowable from OHLC, so every ambiguity is resolved
against the strategy: a bar touching both stop and target counts as a stop,
and a bar that ratchets the stop and also trades back through the new level
is stopped out at that level on the same bar.
"""

import argparse
import json
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from confluence_engine_book2 import (
    Book2Config, compute_indicators_book2, evaluate_signal_book2, position_size_book2,
)
from confluence_engine import new_review_trigger_state, update_review_trigger_state, is_review_triggered
from backtest_confluence import load_data, compute_metrics


EPS = 1e-9  # a move of exactly N pips must count as N pips despite float error


@dataclass(frozen=True)
class ExitRules:
    use_target: bool = True
    breakeven_trigger_r: Optional[float] = None   # move stop to entry once price is this many R in profit
    breakeven_offset_pips: float = 0.0
    trail_trigger_r: Optional[float] = None       # start trailing once price is this many R in profit
    trail_pips: Optional[float] = None            # trailing distance behind the best price


EXIT_PRESETS = {
    "baseline": ExitRules(),
    "breakeven_1r": ExitRules(breakeven_trigger_r=1.0),
    "be_trail_1r": ExitRules(trail_trigger_r=1.0, trail_pips=15.0),
    "be_trail_1r_notarget": ExitRules(use_target=False, trail_trigger_r=1.0, trail_pips=15.0),
    "be_trail_wide_notarget": ExitRules(use_target=False, trail_trigger_r=1.0, trail_pips=22.5),
    "pure_trail_15": ExitRules(trail_trigger_r=0.0, trail_pips=15.0),
}


def manage_exit(position: dict, row, rules: ExitRules, cfg: Book2Config):
    """Evaluate one bar against an open position. Returns (exit_price, reason)
    or (None, None). Mutates position's stop_price / best_price / stop_reason."""
    long_ = position["direction"] == "long"
    entry, stop, target = position["entry_price"], position["stop_price"], position["target_price"]
    hi, lo = row["High"], row["Low"]

    # Levels in force at the start of the bar; a stop wins any tie with the target.
    if (lo <= stop) if long_ else (hi >= stop):
        return stop, position["stop_reason"]
    if rules.use_target and ((hi >= target) if long_ else (lo <= target)):
        return target, "target"

    best = max(position["best_price"], hi) if long_ else min(position["best_price"], lo)
    position["best_price"] = best
    move = (best - entry) if long_ else (entry - best)
    risk = cfg.stop_pips * cfg.pip_size

    candidates = []
    if rules.breakeven_trigger_r is not None and move + EPS >= rules.breakeven_trigger_r * risk:
        off = rules.breakeven_offset_pips * cfg.pip_size
        candidates.append(((entry + off) if long_ else (entry - off), "breakeven"))
    if rules.trail_pips is not None and rules.trail_trigger_r is not None \
            and move + EPS >= rules.trail_trigger_r * risk:
        dist = rules.trail_pips * cfg.pip_size
        candidates.append(((best - dist) if long_ else (best + dist), "trail"))

    for level, reason in candidates:
        if (long_ and level > position["stop_price"]) or (not long_ and level < position["stop_price"]):
            position["stop_price"], position["stop_reason"] = level, reason

    if position["stop_price"] != stop:
        new_stop = position["stop_price"]
        adverse = lo if long_ else hi
        if (long_ and adverse <= new_stop) or (not long_ and adverse >= new_stop):
            return new_stop, position["stop_reason"]
    return None, None


def run_backtest_book2(df: pd.DataFrame, cfg: Book2Config, starting_equity: float = 10000.0,
                       exit_rules: Optional[ExitRules] = None, cost_pips: float = 0.0,
                       df_ind: Optional[pd.DataFrame] = None):
    rules = exit_rules or ExitRules()
    if df_ind is None:
        df_ind = compute_indicators_book2(df, cfg)

    equity = starting_equity
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
            exit_price, reason = manage_exit(position, row, rules, cfg)

            if exit_price is not None:
                pips = (exit_price - position["entry_price"]) / cfg.pip_size
                if position["direction"] == "short":
                    pips = -pips
                pips -= cost_pips
                pnl = pips * cfg.pip_value_per_lot * position["lots"]
                equity += pnl
                trades.append({
                    "entry_time": position["entry_time"].isoformat(), "exit_time": ts.isoformat(),
                    "direction": position["direction"], "entry_price": position["entry_price"],
                    "exit_price": exit_price, "lots": round(position["lots"], 3),
                    "score": position["score"], "pips": round(pips, 1), "pnl": round(pnl, 2),
                    "equity_after": round(equity, 2),
                    "exit_reason": reason, "r_multiple": round(pips / cfg.stop_pips, 2),
                })
                position = None
                review_state = update_review_trigger_state(review_state, cfg, ts, equity, pnl)

        if not day_loss_halted:
            daily_pnl_pct = (equity - day_start_equity) / day_start_equity
            if daily_pnl_pct <= -cfg.max_daily_loss_pct:
                day_loss_halted = True

        if position is None and not day_loss_halted and not is_review_triggered(review_state):
            signal = evaluate_signal_book2(row, cfg)
            if signal is not None:
                lots, risk_amount = position_size_book2(cfg, equity, signal["score"], cfg.stop_pips)
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
                        "direction": signal["direction"], "entry_time": ts, "entry_price": entry_price,
                        "stop_price": stop_price, "target_price": target_price,
                        "lots": lots, "score": signal["score"],
                        "best_price": entry_price, "stop_reason": "stop",
                    }

    return trades, equity, review_state


def _side_stats(trades):
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "avg_r": 0.0}
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    return {
        "trades": len(trades),
        "win_rate": round(sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100, 1),
        "profit_factor": round(wins / losses, 2) if losses else float("inf"),
        "avg_r": round(sum(t["r_multiple"] for t in trades) / len(trades), 2),
    }


def compare_exit_variants(df: pd.DataFrame, cfg: Book2Config, starting_equity: float = 10000.0,
                          cost_pips: float = 0.0):
    """Run every preset on identical entries (indicators computed once)."""
    df_ind = compute_indicators_book2(df, cfg)
    results = {}
    for name, rules in EXIT_PRESETS.items():
        trades, final_equity, review_state = run_backtest_book2(
            df, cfg, starting_equity, rules, cost_pips, df_ind=df_ind)
        results[name] = {
            "metrics": compute_metrics(trades, starting_equity, final_equity),
            "all": _side_stats(trades),
            "long": _side_stats([t for t in trades if t["direction"] == "long"]),
            "short": _side_stats([t for t in trades if t["direction"] == "short"]),
            "review_triggered": review_state["triggered"],
            "trades": trades,
        }
    return results


def print_comparison(results: dict, cost_pips: float):
    print(f"\nExit-variant comparison (identical entries, cost {cost_pips} pips/round trip)")
    header = (f"{'variant':<24}{'trades':>7}{'win%':>7}{'PF':>7}{'avgR':>7}{'ret%':>8}{'maxDD%':>8}"
              f"{'L-PF':>7}{'S-PF':>7}{'L-n':>5}{'S-n':>5}  review")
    print(header)
    print("-" * len(header))
    for name, r in results.items():
        m, a = r["metrics"], r["all"]
        print(f"{name:<24}{a['trades']:>7}{a['win_rate']:>7}{a['profit_factor']:>7}{a['avg_r']:>7}"
              f"{m.get('total_return_pct', 0):>8}{m.get('max_drawdown_pct', 0):>8}"
              f"{r['long']['profit_factor']:>7}{r['short']['profit_factor']:>7}"
              f"{r['long']['trades']:>5}{r['short']['trades']:>5}  "
              f"{'FIRED' if r['review_triggered'] else '-'}")
    print("\nVariants can differ in trade count: a different exit changes when the next entry is available.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="eur_usd_15m_24mo.csv")
    parser.add_argument("--starting-equity", type=float, default=10000.0)
    parser.add_argument("--out-json", default="book2_backtest_results.json")
    parser.add_argument("--exit-variant", default="baseline", choices=sorted(EXIT_PRESETS))
    parser.add_argument("--compare", action="store_true", help="run every exit variant side by side")
    parser.add_argument("--cost-pips", type=float, default=0.0,
                        help="round-trip spread/slippage cost deducted from every trade, in pips")
    args = parser.parse_args()

    df = load_data(args.csv)
    cfg = Book2Config()

    print(f"Loaded {len(df)} candles from {df.index.min()} to {df.index.max()}")

    if args.compare:
        results = compare_exit_variants(df, cfg, args.starting_equity, args.cost_pips)
        print_comparison(results, args.cost_pips)
        with open(args.out_json, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nSaved to {args.out_json}")
        return

    print(f"Running faithful Book 2 (0-10 score, 7/10 threshold) backtest, exit variant: {args.exit_variant}...")
    trades, final_equity, review_state = run_backtest_book2(
        df, cfg, args.starting_equity, EXIT_PRESETS[args.exit_variant], args.cost_pips)
    metrics = compute_metrics(trades, args.starting_equity, final_equity)
    print(json.dumps(metrics, indent=2))
    if review_state["triggered"]:
        print(f"\n*** REVIEW TRIGGER FIRED: {review_state['triggered_reason']} ***")

    with open(args.out_json, "w") as f:
        json.dump({"metrics": metrics, "trades": trades, "review_trigger": review_state}, f, indent=2, default=str)
    print(f"\nSaved to {args.out_json}")


if __name__ == "__main__":
    main()

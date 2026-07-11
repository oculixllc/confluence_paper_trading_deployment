"""
backtest_book2.py

Bar-by-bar simulation for the faithful Book 2 engine
(confluence_engine_book2.py). Mirrors backtest_confluence.py's loop
structure (single open position, daily loss circuit breaker, persistent
review trigger) but drives entries off evaluate_signal_book2's 0-10
score and 7/10 threshold instead of v1's scoring.

Usage:
    python3 backtest_book2.py --csv eur_usd_15m_24mo.csv
"""

import argparse
import json

import pandas as pd

from confluence_engine_book2 import (
    Book2Config, compute_indicators_book2, evaluate_signal_book2, position_size_book2,
)
from confluence_engine import new_review_trigger_state, update_review_trigger_state, is_review_triggered
from backtest_confluence import load_data, compute_metrics


def run_backtest_book2(df: pd.DataFrame, cfg: Book2Config, starting_equity: float = 10000.0):
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
            hit_target = hit_stop = False
            if position["direction"] == "long":
                hit_target = row["High"] >= position["target_price"]
                hit_stop = row["Low"] <= position["stop_price"]
            else:
                hit_target = row["Low"] <= position["target_price"]
                hit_stop = row["High"] >= position["stop_price"]

            exit_price = None
            if hit_target and hit_stop:
                exit_price = position["stop_price"]
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
                    "entry_time": position["entry_time"].isoformat(), "exit_time": ts.isoformat(),
                    "direction": position["direction"], "entry_price": position["entry_price"],
                    "exit_price": exit_price, "lots": round(position["lots"], 3),
                    "score": position["score"], "pips": round(pips, 1), "pnl": round(pnl, 2),
                    "equity_after": round(equity, 2),
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
                    }

    return trades, equity, review_state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="eur_usd_15m_24mo.csv")
    parser.add_argument("--starting-equity", type=float, default=10000.0)
    parser.add_argument("--out-json", default="book2_backtest_results.json")
    args = parser.parse_args()

    df = load_data(args.csv)
    cfg = Book2Config()

    print(f"Loaded {len(df)} candles from {df.index.min()} to {df.index.max()}")
    print("Running faithful Book 2 (0-10 score, 7/10 threshold) backtest...")

    trades, final_equity, review_state = run_backtest_book2(df, cfg, args.starting_equity)
    metrics = compute_metrics(trades, args.starting_equity, final_equity)
    print(json.dumps(metrics, indent=2))
    if review_state["triggered"]:
        print(f"\n*** REVIEW TRIGGER FIRED: {review_state['triggered_reason']} ***")

    with open(args.out_json, "w") as f:
        json.dump({"metrics": metrics, "trades": trades, "review_trigger": review_state}, f, indent=2, default=str)
    print(f"\nSaved to {args.out_json}")


if __name__ == "__main__":
    main()

"""
backtest_confluence.py

Runs the book-accurate ConfluenceEngine (confluence_engine.py) over real
historical EUR/USD data. Bar-by-bar simulation:
  - one open position at a time
  - entries gated by confluence score (Ch. 10) and session window (Ch. 10.7)
  - exits: minimum 2:1 reward:risk target, or structural stop (Ch. 14/15)
  - position size scaled by confluence score, risk-based (Ch. 17)
  - 3% max daily loss circuit breaker (Ch. 17.3)

Usage:
    python3 backtest_confluence.py --csv eur_usd_15m_ny.csv
"""

import argparse
import json
import pandas as pd

from confluence_engine import ConfluenceConfig, compute_indicators, simulate_trades


def load_data(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    time_col = df.columns[0]
    df = df.rename(columns={time_col: "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert("America/New_York")
    df = df.set_index("time").sort_index()
    return df


def run_backtest(df: pd.DataFrame, cfg: ConfluenceConfig, starting_equity: float = 10000.0):
    """Thin wrapper: compute indicators, then run the single shared
    bar-by-bar simulation (confluence_engine.simulate_trades). No
    simulation logic lives in this file anymore -- see that function's
    docstring for why duplicating it was a recurring source of drift."""
    df_ind = compute_indicators(df, cfg)
    return simulate_trades(df_ind, cfg, starting_equity)


def compute_metrics(trades, starting_equity, final_equity):
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0, "profit_factor": 0,
            "total_pnl": 0, "total_return_pct": 0, "final_equity": starting_equity,
        }
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    win_rate = len(wins) / len(trades) * 100
    total_pnl = final_equity - starting_equity

    equity_series = [starting_equity] + [t["equity_after"] for t in trades]
    peak = equity_series[0]
    max_dd = 0
    for e in equity_series:
        peak = max(peak, e)
        dd = (peak - e) / peak
        max_dd = max(max_dd, dd)

    by_score = {}
    for s in (2, 3, 4):
        s_trades = [t for t in trades if t["score"] == s]
        s_wins = [t for t in s_trades if t["pnl"] > 0]
        by_score[s] = {
            "trades": len(s_trades),
            "win_rate": round(len(s_wins) / len(s_trades) * 100, 1) if s_trades else 0,
        }

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_pnl / starting_equity * 100, 2),
        "final_equity": round(final_equity, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "breakdown_by_confluence_score": by_score,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--starting-equity", type=float, default=10000.0)
    parser.add_argument("--out-json", default="backtest_results.json")
    args = parser.parse_args()

    df = load_data(args.csv)
    cfg = ConfluenceConfig()

    print(f"Loaded {len(df)} candles from {df.index.min()} to {df.index.max()}")
    print("Running book-accurate confluence backtest...")

    trades, equity_curve, final_equity, review_state = run_backtest(df, cfg, args.starting_equity)
    metrics = compute_metrics(trades, args.starting_equity, final_equity)

    print(json.dumps(metrics, indent=2))
    if review_state["triggered"]:
        print(f"\n*** REVIEW TRIGGER FIRED at {review_state['triggered_at']}: "
              f"{review_state['triggered_reason']} ***")
        print("New entries were blocked for the remainder of this run once triggered.")
    else:
        print("\nReview trigger: not fired (run stayed within normal-variance bounds).")

    with open(args.out_json, "w") as f:
        json.dump({"metrics": metrics, "trades": trades, "config": cfg.__dict__,
                   "review_trigger": review_state}, f, indent=2, default=str)
    print(f"\nSaved full results to {args.out_json}")


if __name__ == "__main__":
    main()

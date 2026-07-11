"""
journal_book2.py

Local, dependency-free journal viewer for the Book 2 paper-trading runner.
Reads paper_trading_log_book2.jsonl directly (the same file the runner
always writes, regardless of whether the Cloudflare dashboard is set up)
and prints Book 2 Ch. 11.2's journal fields plus rolling metrics.

Use this if you don't want to deploy the Cloudflare dashboard, or want a
quick terminal check alongside it.

Usage:
    python3 journal_book2.py
    python3 journal_book2.py --log paper_trading_log_book2.jsonl --out journal.csv
"""

import argparse
import csv
import json
from pathlib import Path


def load_events(log_path: Path):
    events = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def build_journal(events):
    trades = {}
    for e in events:
        if e.get("event") == "position_opened":
            trades[e["trade_id"]] = {
                "trade_id": e["trade_id"], "entry_time": e.get("entry_time"),
                "direction": e.get("direction"), "setup_score": e.get("score"),
                "entry_price": e.get("entry_price"), "stop_price": e.get("stop_price"),
                "target_price": e.get("target_price"), "lots": e.get("lots"),
                "exit_time": None, "exit_price": None, "realized_pnl": None,
            }
        elif e.get("event") == "trade_closed":
            t = trades.get(e["trade_id"])
            if t is None:
                continue
            t["exit_time"] = e.get("close_time")
            t["exit_price"] = e.get("exit_price")
            t["realized_pnl"] = e.get("realized_pnl")

    rows = []
    for t in trades.values():
        rr_planned = None
        rr_actual = None
        if t["entry_price"] is not None and t["stop_price"] is not None:
            risk_dist = abs(t["entry_price"] - t["stop_price"])
            if t["target_price"] is not None and risk_dist:
                reward_dist = abs(t["target_price"] - t["entry_price"])
                rr_planned = round(reward_dist / risk_dist, 2)
            if t["exit_price"] is not None and risk_dist:
                if t["direction"] == "long":
                    realized_dist = t["exit_price"] - t["entry_price"]
                else:
                    realized_dist = t["entry_price"] - t["exit_price"]
                rr_actual = round(realized_dist / risk_dist, 2)
        status = "open" if t["exit_time"] is None else (
            "win" if (t["realized_pnl"] or 0) > 0 else "loss" if (t["realized_pnl"] or 0) < 0 else "flat"
        )
        rows.append({**t, "rr_planned": rr_planned, "rr_actual": rr_actual, "status": status})

    rows.sort(key=lambda r: r["entry_time"] or "")
    return rows


def print_summary(rows):
    closed = [r for r in rows if r["status"] in ("win", "loss", "flat")]
    wins = [r for r in closed if r["status"] == "win"]
    losses = [r for r in closed if r["status"] == "loss"]
    gross_win = sum(r["realized_pnl"] for r in wins)
    gross_loss = abs(sum(r["realized_pnl"] for r in losses))
    win_rate = (len(wins) / len(closed) * 100) if closed else 0
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0

    print("=" * 100)
    print(f"Closed trades: {len(closed)}  |  Open: {len(rows) - len(closed)}  |  "
          f"Win rate: {win_rate:.1f}%  |  Profit factor: {pf:.2f}  |  "
          f"Net PnL: {gross_win - gross_loss:+.2f}")
    print("=" * 100)


def print_table(rows):
    header = f"{'entry_time':<26}{'dir':<7}{'score':>6}{'entry':>10}{'stop':>10}{'target':>10}{'exit':>10}{'rr_plan':>9}{'rr_act':>8}{'pnl':>9}  status"
    print(header)
    for r in rows:
        print(
            f"{(r['entry_time'] or '')[:25]:<26}{(r['direction'] or ''):<7}{r['setup_score'] or 0:>6}"
            f"{r['entry_price'] or 0:>10.5f}{r['stop_price'] or 0:>10.5f}{r['target_price'] or 0:>10.5f}"
            f"{(r['exit_price'] or 0):>10.5f}{(r['rr_planned'] or 0):>9.2f}{(r['rr_actual'] or 0):>8.2f}"
            f"{(r['realized_pnl'] or 0):>9.2f}  {r['status']}"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="paper_trading_log_book2.jsonl")
    parser.add_argument("--out", default=None, help="optional CSV export path")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"No log file at {log_path} yet -- nothing has run, or it hasn't logged anything.")
        return

    events = load_events(log_path)
    rows = build_journal(events)

    if not rows:
        print("No trades logged yet.")
        return

    print_summary(rows)
    print_table(rows)

    if args.out:
        with open(args.out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nSaved CSV to {args.out}")


if __name__ == "__main__":
    main()

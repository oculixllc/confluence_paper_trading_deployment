"""
trailing_target_grid.py

PRE-REGISTERED test (rules fixed before any result was seen): a trailing stop worth 1% of account value,
with a take profit between 1R and 2R, against the same take profits with the fixed stop.

"1% of account value" = a 15-pip trail. Position size is set so that 15 pips (the stop distance) loses
exactly 1% of the account, so a stop that trails 15 pips behind the best price risks 1% of the account at
all times. The trail starts at entry and only ever moves in the trade's favor.

GRID      take profit R in {1.0, 1.25, 1.5, 1.75, 2.0}  x  exit in {fixed 15-pip stop, 15-pip trailing stop}
DATA      the full 14 years (2013-01 .. 2026-10), one continuous run per cell, entries identical across cells
          except where an earlier exit frees the next entry.
COSTS     1-pip and 2-pip round trip (live fills suggest about 2).
METRICS   R-based profit factor (sum of winning R / sum of losing R), so compounding does not tilt the later
          years; per-half and per-year profit factor; trade count; return and drawdown from the equity curve.

DECISION  A trailing configuration is worth deploying only if ALL hold:
            1. profit factor > 1.2 at 1-pip cost and > 1.0 at 2-pip cost,
            2. it beats the fixed-stop control at the same take profit (1-pip cost),
            3. profit factor > 1.0 in BOTH halves (2013-2019 and 2020-2026),
            4. the engine's 25% drawdown review trigger does not fire at 1-pip cost.
"""
import argparse
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]
from confluence_engine_book2 import Book2Config, compute_indicators_book2  # noqa: E402
from backtest_book2 import run_backtest_book2, ExitRules  # noqa: E402
from backtest_confluence import load_data, compute_metrics  # noqa: E402

TARGETS = [1.0, 1.25, 1.5, 1.75, 2.0]
EXITS = {"fixed": ExitRules(), "trail15": ExitRules(trail_trigger_r=0.0, trail_pips=15.0)}


def r_pf(rs):
    w = sum(r for r in rs if r > 0)
    l = -sum(r for r in rs if r < 0)
    return round(w / l, 2) if l else float("inf")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old-csv", default="eur_usd_15m_2013-01_to_2023-09.csv")
    ap.add_argument("--new-csv", default="eur_usd_15m_2023-10_to_2026-10.csv")
    ap.add_argument("--starting-equity", type=float, default=10000.0)
    args = ap.parse_args()

    df = pd.concat([load_data(args.old_csv), load_data(args.new_csv)]).sort_index()
    df = df[~df.index.duplicated()]
    print(f"{len(df)} candles {df.index.min()} -> {df.index.max()}", flush=True)
    ind = compute_indicators_book2(df, Book2Config())

    rows = []
    for target in TARGETS:
        cfg = Book2Config(min_reward_risk=target)
        for name, rules in EXITS.items():
            cell = {"target": target, "exit": name}
            for cost in (1.0, 2.0):
                trades, eq, rs = run_backtest_book2(df, cfg, args.starting_equity, rules, cost, df_ind=ind)
                m = compute_metrics(trades, args.starting_equity, eq)
                t = pd.DataFrame(trades)
                year = pd.to_datetime(t.entry_time, utc=True).dt.year
                R = t.r_multiple
                cell[f"c{int(cost)}"] = dict(
                    n=len(t), win=m["win_rate"], pf=r_pf(R), avgR=round(R.mean(), 3), ret=m["total_return_pct"],
                    dd=m["max_drawdown_pct"], fired=bool(rs["triggered"]),
                    pfA=r_pf(R[year <= 2019]), pfB=r_pf(R[year >= 2020]),
                    years_up=int(sum(r_pf(R[year == y]) > 1 for y in sorted(year.unique()))), years=int(year.nunique()),
                )
            rows.append(cell)
            c1, c2 = cell["c1"], cell["c2"]
            print(f"target {target:<5} {name:<8} | 1pip: n={c1['n']:<4} win {c1['win']:>5}% PF {c1['pf']:<5} avgR {c1['avgR']:+.3f} ret {c1['ret']:>7}% DD {c1['dd']:>5}% "
                  f"halves {c1['pfA']}/{c1['pfB']} yrs+ {c1['years_up']}/{c1['years']} {'REVIEW-TRIGGER' if c1['fired'] else ''}"
                  f" | 2pip: PF {c2['pf']:<5} avgR {c2['avgR']:+.3f} ret {c2['ret']:>7}% {'REVIEW-TRIGGER' if c2['fired'] else ''}", flush=True)

    print("\n######## PRE-REGISTERED DECISION (trailing configurations) ########")
    by = {(r["target"], r["exit"]): r for r in rows}
    any_pass = False
    for target in TARGETS:
        tr, fx = by[(target, "trail15")], by[(target, "fixed")]
        c1, c2 = tr["c1"], tr["c2"]
        tests = {
            "PF>1.2 @1pip": c1["pf"] > 1.2, "PF>1.0 @2pip": c2["pf"] > 1.0,
            "beats fixed @1pip": c1["pf"] > fx["c1"]["pf"],
            "both halves >1": c1["pfA"] > 1 and c1["pfB"] > 1, "no review trigger": not c1["fired"],
        }
        ok = all(tests.values())
        any_pass = any_pass or ok
        print(f"target {target:<5} {'DEPLOY-WORTHY' if ok else 'FAILS':<14} " + ", ".join(f"{k}: {'ok' if v else 'NO'}" for k, v in tests.items()))
    print("\nVERDICT:", "at least one trailing configuration passes all four rules" if any_pass else "no trailing configuration passes the pre-registered rules")


if __name__ == "__main__":
    main()

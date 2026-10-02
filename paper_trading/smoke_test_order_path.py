"""
smoke_test_order_path.py

One-off check of the live order path on the Oanda PRACTICE account. It places a
1-unit EUR/USD market order through the runner's own place_order (stop set as a
distance from the fill), attaches the take-profit with attach_take_profit, reads
the trade back to confirm the stop and target sit exactly 15 and 30 pips from the
real fill, then closes it. The trade is always closed, even if a check fails.

Run only while the market is open, and only when the runner has no open position.
It refuses to run otherwise.

Usage (on the server, from the runner's directory):
    cd /home/ubuntu/paper-trading
    set -a; source .env; set +a
    venv/bin/python3 smoke_test_order_path.py
"""

import json
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.getcwd(), HERE, os.path.join(os.path.dirname(HERE), "engine")]

import oanda_paper_trading_book2 as m  # noqa: E402
from confluence_engine_book2 import Book2Config  # noqa: E402

TOL = 1.5e-5  # prices are quoted to 5 decimals


def main():
    api_key, account_id = os.environ.get("OANDA_API_KEY"), os.environ.get("OANDA_ACCOUNT_ID")
    if not api_key or not account_id:
        sys.exit("ERROR: set OANDA_API_KEY and OANDA_ACCOUNT_ID in your environment.")

    state = m.load_state()
    if state.get("position") is not None:
        sys.exit("REFUSING: the runner has an open position in its state file.")

    cfg = Book2Config()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})

    if m.get_open_trades(session, account_id):
        sys.exit("REFUSING: the account already has open trades.")

    stop_distance = cfg.stop_pips * cfg.pip_size
    target_distance = stop_distance * cfg.min_reward_risk
    result = m.place_order(session, account_id, cfg, "long", 0.00001, stop_distance)  # 1 unit
    fill = result.get("orderFillTransaction")
    if fill is None:
        reason = (result.get("orderCancelTransaction") or {}).get("reason") or json.dumps(result)[:300]
        sys.exit(f"NOT FILLED (market closed?): {reason}")

    opened = fill.get("tradeOpened", {})
    trade_id = opened.get("tradeID")
    entry = float(opened.get("price", fill.get("price")))
    checks, ok = [], True
    try:
        m.attach_take_profit(session, account_id, trade_id, entry + target_distance)
        trade = m.oanda_get(session, f"/v3/accounts/{account_id}/trades/{trade_id}")["trade"]
        sl = float(trade["stopLossOrder"]["price"])
        tp = float(trade["takeProfitOrder"]["price"])
        for name, got, want in (("stop", sl, entry - stop_distance), ("target", tp, entry + target_distance)):
            good = abs(got - want) <= TOL
            ok = ok and good
            checks.append(f"  {name}: {got:.5f} (expected {want:.5f}) {'OK' if good else 'MISMATCH'}")
    except Exception as e:  # noqa: BLE001
        ok = False
        checks.append(f"  check failed: {e}")
    finally:
        m.close_trade_at_market(session, account_id, trade_id)

    print(f"trade {trade_id} filled at {entry:.5f}, now closed")
    print("\n".join(checks))
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

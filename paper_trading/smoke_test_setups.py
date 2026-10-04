"""
smoke_test_setups.py

One-off check of the 3-setup bot's order path on the Oanda PRACTICE account. For each instrument it
places a 1-unit long market order through the runner's own place_order (structural stop 15 pips below
the current bid, 1% trailing stop), attaches the 2R take-profit, reads the trade back to confirm the
stop, target and trailing distance are what the runner asked for, then closes it. The trade is always
closed, even if a check fails. Also prints the account's currency and margin rate (sizing assumes USD).

Run only while the market is open, and only when the account has no open trades (it refuses otherwise).

Usage (on the server, from the runner's directory):
    cd /home/ubuntu/paper-trading
    set -a; source .env; set +a
    OANDA_ACCOUNT_ID=101-001-20779621-002 venv/bin/python3 smoke_test_setups.py
    optional: INSTRUMENTS=EUR_USD  (default EUR_USD,USD_JPY)
"""

import json
import os
import sys

import requests

sys.path[:0] = [os.getcwd(), os.path.dirname(os.path.abspath(__file__))]

import oanda_paper_trading_book2 as b2  # noqa: E402
import oanda_paper_trading_setups as S  # noqa: E402
from confluence_setups import PROFILES  # noqa: E402


def check_instrument(session, account_id, inst):
    prof = PROFILES[inst]
    dec = S.PRICE_DECIMALS[inst]
    tol = 1.5 * 10 ** -dec
    px = b2.oanda_get(session, f"/v3/accounts/{account_id}/pricing", {"instruments": inst})["prices"][0]
    if not px.get("tradeable", True):
        return None, [f"  {inst}: not tradeable right now (market closed?)"]
    bid = float(px["bids"][0]["price"])
    stop_price = bid - 15 * prof.pip_size
    trail = bid * S.DEFAULT_TRAIL_PCT / 100.0

    result = S.place_order(session, account_id, inst, 1, stop_price, trail)
    fill = result.get("orderFillTransaction")
    if fill is None:
        reason = (result.get("orderCancelTransaction") or {}).get("reason") or json.dumps(result)[:300]
        return False, [f"  {inst}: NOT FILLED: {reason}"]

    opened = fill.get("tradeOpened", {})
    trade_id, entry = opened.get("tradeID"), float(opened.get("price", fill.get("price")))
    target = entry + 2 * (entry - stop_price)
    lines, ok = [f"  {inst}: trade {trade_id} filled at {entry:.{dec}f}"], True
    try:
        b2.attach_take_profit(session, account_id, trade_id, round(target, dec))
        trade = b2.oanda_get(session, f"/v3/accounts/{account_id}/trades/{trade_id}")["trade"]
        got = {
            "stop": (float(trade["stopLossOrder"]["price"]), round(stop_price, dec)),
            "target": (float(trade["takeProfitOrder"]["price"]), round(target, dec)),
            "trail distance": (float(trade["trailingStopLossOrder"]["distance"]), round(trail, dec)),
        }
        for name, (g, want) in got.items():
            good = abs(g - want) <= tol
            ok = ok and good
            lines.append(f"    {name}: {g} (expected {want}) {'OK' if good else 'MISMATCH'}")
    except Exception as e:  # noqa: BLE001
        ok = False
        lines.append(f"    check failed: {e}")
    finally:
        b2.close_trade_at_market(session, account_id, trade_id)
        lines.append("    closed")
    return ok, lines


def main():
    api_key, account_id = os.environ.get("OANDA_API_KEY"), os.environ.get("OANDA_ACCOUNT_ID")
    if not api_key or not account_id:
        sys.exit("ERROR: set OANDA_API_KEY and OANDA_ACCOUNT_ID in your environment.")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})

    acct = b2.oanda_get(session, f"/v3/accounts/{account_id}/summary")["account"]
    print(f"account {account_id}: currency={acct.get('currency')} balance={acct.get('balance')} marginRate={acct.get('marginRate')}")
    if acct.get("currency") != "USD":
        sys.exit("REFUSING: sizing assumes a USD account.")
    if b2.get_open_trades(session, account_id):
        sys.exit("REFUSING: the account already has open trades.")

    results = []
    for inst in [i.strip() for i in os.environ.get("INSTRUMENTS", "EUR_USD,USD_JPY").split(",") if i.strip()]:
        ok, lines = check_instrument(session, account_id, inst)
        print("\n".join(lines))
        results.append(ok)
    if any(r is None for r in results):
        print("INCONCLUSIVE (market closed for at least one instrument)")
        sys.exit(2)
    print("PASS" if all(results) else "FAIL")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

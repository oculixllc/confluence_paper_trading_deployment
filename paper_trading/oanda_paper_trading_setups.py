"""
oanda_paper_trading_setups.py

Paper-trading runner for the three-setup rule set (ORB, Pullback, Reversal) from
"conflunence trading bot.md", on EUR/USD and USD/JPY, against an OANDA PRACTICE account.
Hardcoded to the practice endpoint (it reuses the helpers in oanda_paper_trading_book2,
which has no live URL anywhere). There is no live-trading path here.

Rules applied (see engine/confluence_setups.py for the signals):
  * Risk 1% of balance if the structural stop is hit, AND margin used <= 5% of balance.
    Units = the smaller of the two limits; the risk actually taken is logged.
  * Stop = the setup's structural stop (absolute price). Target = 1:2 R:R from the actual fill.
  * Trailing stop = 1% of price, as an Oanda trailing-stop order (distance), unless changed
    in the journal's control panel.
  * Entry = market order at the close of the signal bar. One position per instrument.

Control panel (journal Worker): this bot only trades while "enabled" is on there, and starts
PAUSED. risk_pct / tp_pct / trail_pct set there override the defaults for NEW trades only;
every trade is logged with the settings it used.

Setup:
    export OANDA_API_KEY="<practice token>"
    export OANDA_ACCOUNT_ID="101-001-20779621-002"
    export DASHBOARD_URL=... DASHBOARD_TOKEN=...          # same as the Book 2 runner
    optional: INSTRUMENTS=EUR_USD,USD_JPY  BOT_ID=setups
Cron (every 15 min, 2 min after the bar close), run from the flattened deployment directory:
    2,17,32,47 * * * * cd /path && /usr/bin/python3 oanda_paper_trading_setups.py >> cron_setups.log 2>&1
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

import oanda_paper_trading_book2 as b2          # practice-only helpers (oanda_get/post/put, retries, reconcile lookups)
import bot_control
from confluence_engine_book2 import Book2Config, compute_indicators_book2
from confluence_setups import PROFILES, RR, SETUPS, latest_setup_signals

GRANULARITY = "M15"
CANDLES_NEEDED = 2200
PRICE_DECIMALS = {"EUR_USD": 5, "USD_JPY": 3}
MARGIN_CAP_PCT = 5.0
DEFAULT_TRAIL_PCT = 1.0            # the rules' trailing stop: 1% of price
UNITS_PER_LOT = 100000

BOT_ID = os.environ.get("BOT_ID", "setups")
STATE_PATH = Path(f"paper_trading_state_{BOT_ID}.json")
LOG_PATH = Path(f"paper_trading_log_{BOT_ID}.jsonl")


def log_event(event: dict):
    event = {**event, "bot_id": BOT_ID, "logged_at": datetime.now(timezone.utc).isoformat()}
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(event, default=str) + "\n")
    print(json.dumps(event, default=str))
    url = os.environ.get("DASHBOARD_URL")
    if url:
        try:
            headers = {"Content-Type": "application/json"}
            if os.environ.get("DASHBOARD_TOKEN"):
                headers["Authorization"] = f"Bearer {os.environ['DASHBOARD_TOKEN']}"
            requests.post(f"{url.rstrip('/')}/api/event", json=event, headers=headers, timeout=5)
        except Exception as e:
            print(f"WARNING: dashboard push failed (non-fatal): {e}")


def load_state(instruments) -> dict:
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    state.setdefault("instruments", {})
    for i in instruments:
        state["instruments"].setdefault(i, {"position": None, "last_processed_bar": None})
    return state


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def fmt(inst, price):
    return f"{price:.{PRICE_DECIMALS[inst]}f}"


# ---------------------------------------------------------------- sizing (pure, unit-tested)

def size_units(inst, balance, price, sl_dist, risk_pct, margin_rate, margin_cap_pct=MARGIN_CAP_PCT):
    """USD account. Returns (units, info). units = floor(min(risk-limited, margin-limited))."""
    if sl_dist <= 0 or price <= 0:
        return 0, {"binding": "invalid"}
    usd_quote = inst.endswith("_USD")                  # EUR_USD: loss in USD; USD_JPY: loss in JPY
    loss_per_unit = sl_dist if usd_quote else sl_dist / price
    margin_per_unit = price * margin_rate if usd_quote else margin_rate
    u_risk = balance * risk_pct / 100.0 / loss_per_unit
    u_margin = balance * margin_cap_pct / 100.0 / margin_per_unit
    units = int(math.floor(min(u_risk, u_margin)))
    return units, {
        "binding": "margin" if u_margin < u_risk else "risk",
        "requested_risk_pct": risk_pct,
        "realized_risk_pct": round(units * loss_per_unit / balance * 100.0, 4),
        "margin_used_pct": round(units * margin_per_unit / balance * 100.0, 3),
    }


# ---------------------------------------------------------------- Oanda calls

def fetch_candles(session, inst, count) -> pd.DataFrame:
    data = b2.oanda_get(session, f"/v3/instruments/{inst}/candles",
                        params={"granularity": GRANULARITY, "price": "M", "count": count})
    rows = []
    for c in data.get("candles", []):
        if not c.get("complete", True):
            continue
        m = c["mid"]
        rows.append({"time": b2.parse_oanda_time(c["time"]), "Open": float(m["o"]), "High": float(m["h"]),
                     "Low": float(m["l"]), "Close": float(m["c"]), "Volume": int(c["volume"])})
    df = pd.DataFrame(rows).set_index("time").sort_index()
    df.index = df.index.tz_convert("America/New_York")
    return df


def account_summary(session, account_id):
    acct = b2.oanda_get(session, f"/v3/accounts/{account_id}/summary")["account"]
    if acct.get("currency") != "USD":
        raise RuntimeError(f"account currency is {acct.get('currency')}; sizing assumes USD")
    return float(acct["balance"]), float(acct["marginRate"])


def place_order(session, account_id, inst, units, stop_price, trail_distance):
    order = {"type": "MARKET", "instrument": inst, "units": str(units), "timeInForce": "FOK",
             "positionFill": "DEFAULT", "stopLossOnFill": {"price": fmt(inst, stop_price)}}
    if trail_distance:
        order["trailingStopLossOnFill"] = {"distance": fmt(inst, trail_distance)}
    return b2.oanda_post(session, f"/v3/accounts/{account_id}/orders", {"order": order})


# ---------------------------------------------------------------- per-instrument logic

def reconcile(session, account_id, inst, ist):
    pos = ist["position"]
    if pos is None:
        return
    if any(t["id"] == pos["trade_id"] for t in b2.get_open_trades(session, account_id)):
        return
    closed = b2.get_closed_trade(session, account_id, pos["trade_id"])
    balance, _ = account_summary(session, account_id)
    log_event({"event": "trade_closed", "instrument": inst, "trade_id": pos["trade_id"],
               "direction": pos["direction"], "entry_price": pos["entry_price"],
               "exit_price": float(closed.get("averageClosePrice", pos["entry_price"])),
               "realized_pnl": float(closed.get("realizedPL", 0.0)), "close_time": closed.get("closeTime"),
               "equity": balance})
    ist["position"] = None


def flatten(session, account_id, inst, ist):
    pos = ist["position"]
    if pos is None:
        return
    b2.close_trade_at_market(session, account_id, pos["trade_id"])
    log_event({"event": "flattened_by_control_panel", "instrument": inst, "trade_id": pos["trade_id"]})
    # reconcile() records the close as a normal trade_closed on the next pass


def try_enter(session, account_id, inst, ist, df_ind, ctrl):
    if ist["position"] is not None:
        return
    hits, near = latest_setup_signals(df_ind, inst)
    bar_time = df_ind.index[-1].isoformat()
    log_event({"event": "bar_evaluated", "instrument": inst, "bar_time": bar_time,
               "close": float(df_ind["Close"].iloc[-1]),
               "signal": hits[0] if hits else None, "other_signals": hits[1:], "near_misses": near})
    if not hits:
        return
    sig = hits[0]
    balance, margin_rate = account_summary(session, account_id)
    price = float(df_ind["Close"].iloc[-1])
    stop_dist = abs(price - sig["stop_price"])
    risk_pct = float(ctrl["risk_pct"])
    units, info = size_units(inst, balance, price, stop_dist, risk_pct, margin_rate)
    if units < 1:
        log_event({"event": "skipped_entry", "instrument": inst, "reason": "size_below_1_unit", "sizing": info})
        return
    signed = units if sig["direction"] == "long" else -units

    trail_pct = DEFAULT_TRAIL_PCT if ctrl.get("trail_pct") is None else float(ctrl["trail_pct"])
    trail_distance = price * trail_pct / 100.0 if trail_pct > 0 else None
    result = place_order(session, account_id, inst, signed, sig["stop_price"], trail_distance)
    fill = result.get("orderFillTransaction")
    if fill is None:
        log_event({"event": "order_not_filled", "instrument": inst, "order_result": result})
        return
    opened = fill.get("tradeOpened", {})
    trade_id = opened.get("tradeID")
    entry = float(opened.get("price", fill.get("price", price)))
    risk_dist = abs(entry - sig["stop_price"])
    tp_dist = entry * float(ctrl["tp_pct"]) / 100.0 if ctrl.get("tp_pct") is not None else RR * risk_dist
    target = entry + tp_dist if sig["direction"] == "long" else entry - tp_dist

    ist["position"] = {"trade_id": trade_id, "direction": sig["direction"], "entry_price": entry,
                       "stop_price": sig["stop_price"], "target_price": target, "units": signed,
                       "setup": sig["setup"], "entry_time": bar_time}
    log_event({"event": "position_opened", "instrument": inst, "trade_id": trade_id, "direction": sig["direction"],
               "entry_price": entry, "stop_price": sig["stop_price"], "target_price": target,
               "lots": abs(signed) / UNITS_PER_LOT, "entry_time": bar_time, "setup": sig["setup"],
               "components": sig["items"], "sizing": info, "tp_pct": ctrl.get("tp_pct"), "trail_pct": trail_pct,
               "risk_pct": risk_pct, "config_version": ctrl.get("version"), "run_id": ctrl.get("run_id"),
               "signal_price": price})
    try:
        b2.attach_take_profit(session, account_id, trade_id, round(target, PRICE_DECIMALS[inst]))
    except Exception as e:
        # A trade without its target breaks the 1:2 rule, so flatten (stop is already live).
        log_event({"event": "take_profit_attach_failed", "instrument": inst, "trade_id": trade_id, "message": str(e)})
        b2.close_trade_at_market(session, account_id, trade_id)
        log_event({"event": "closed_at_market_after_tp_failure", "instrument": inst, "trade_id": trade_id})


def main():
    api_key, account_id = os.environ.get("OANDA_API_KEY"), os.environ.get("OANDA_ACCOUNT_ID")
    if not api_key or not account_id:
        print("ERROR: set OANDA_API_KEY and OANDA_ACCOUNT_ID.", file=sys.stderr)
        sys.exit(1)
    instruments = [i.strip() for i in os.environ.get("INSTRUMENTS", "EUR_USD,USD_JPY").split(",") if i.strip()]
    bad = [i for i in instruments if i not in PROFILES]
    if bad:
        print(f"ERROR: unsupported instruments {bad}; supported: {sorted(PROFILES)}", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})
    state = load_state(instruments)
    ctrl, source = bot_control.fetch_config(BOT_ID, Path("."), default_enabled=False)

    try:
        log_event({"event": "heartbeat", "enabled": ctrl["enabled"], "config_source": source,
                   "config_version": ctrl.get("version")})
        for inst in instruments:
            ist = state["instruments"][inst]
            reconcile(session, account_id, inst, ist)
            if ctrl["flatten_requested"]:
                flatten(session, account_id, inst, ist)
                continue
            if not ctrl["enabled"]:
                log_event({"event": "skipped_entry", "instrument": inst, "reason": "paused_by_control_panel"})
                continue
            df = fetch_candles(session, inst, CANDLES_NEEDED)
            bar = df.index[-1].isoformat()
            if ist.get("last_processed_bar") == bar:
                continue
            df_ind = compute_indicators_book2(df, Book2Config(pip_size=PROFILES[inst].pip_size))
            try_enter(session, account_id, inst, ist, df_ind, ctrl)
            ist["last_processed_bar"] = bar
        if ctrl["flatten_requested"]:
            bot_control.ack_flatten(BOT_ID)
    except Exception as e:
        log_event({"event": "ERROR", "message": str(e)})
        raise
    finally:
        save_state(state)


if __name__ == "__main__":
    main()

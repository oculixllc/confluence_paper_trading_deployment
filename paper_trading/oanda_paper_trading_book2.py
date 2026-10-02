"""
oanda_paper_trading_book2.py

Paper-trading runner for the faithful Book 2 engine
(confluence_engine_book2.py + chart_patterns_book2.py), against an OANDA
PRACTICE/DEMO account only. Structurally identical in design to
oanda_paper_trading.py (the v1 runner): hardcoded to the practice
endpoint with no live-endpoint code path anywhere in this file, stateless
cron-invoked execution, persistent state across runs, reconciles against
Oanda's own trade records rather than assuming.

The only real differences from the v1 runner:
  - Imports from confluence_engine_book2 / chart_patterns_book2 instead
    of confluence_engine, and calls evaluate_signal_book2 (0-10 score,
    7/10 threshold) instead of v1's evaluate_signal.
  - CANDLES_NEEDED reflects Book 2's warmup requirements: the MTF bias
    SMA (mtf_bias_lookback, 2000 bars) is the dominant one, same as v1's
    regime filter; the Volume Profile composite window (480 bars) and
    RVOL's 15-prior-session time-of-day lookback are both smaller.
  - No separate v1 review-trigger import needed beyond what
    confluence_engine already exposes (reused as-is; Book2Config carries
    the same review_trigger_* fields for compatibility, exactly as
    backtest_book2.py already relies on).

Setup:
    pip install requests pandas numpy

    export OANDA_API_KEY="your-practice-api-token"
    export OANDA_ACCOUNT_ID="your-practice-account-id"

    Optional, for the live Cloudflare dashboard (see worker/ directory):
    export DASHBOARD_URL="https://your-worker-subdomain.workers.dev"
    export DASHBOARD_TOKEN="<same value as the Worker's INGEST_TOKEN secret>"
    If DASHBOARD_URL is unset, this runs exactly as before -- local JSONL
    log only, no network calls beyond Oanda itself.

Usage (run manually, or via cron):
    python3 oanda_paper_trading_book2.py

Suggested cron entry (every 15 min, 2 min after each bar close):
    2,17,32,47 * * * * cd /path/to/project && /usr/bin/python3 oanda_paper_trading_book2.py >> cron_book2.log 2>&1
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from confluence_engine_book2 import (
    Book2Config, compute_indicators_book2, evaluate_signal_book2, position_size_book2,
)
from confluence_engine import new_review_trigger_state, update_review_trigger_state, is_review_triggered

# --- Hardcoded to practice only. No live URL exists in this file. ---
OANDA_PRACTICE_URL = "https://api-fxpractice.oanda.com"
INSTRUMENT = "EUR_USD"
GRANULARITY = "M15"
CANDLES_NEEDED = 2200  # dominated by mtf_bias_lookback(2000) + warmup buffer

STATE_PATH = Path("paper_trading_state_book2.json")
LOG_PATH = Path("paper_trading_log_book2.jsonl")


DASHBOARD_URL = os.environ.get("DASHBOARD_URL")        # e.g. https://your-worker.workers.dev
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN")     # matches the Worker's INGEST_TOKEN secret


def log_event(event: dict):
    event["logged_at"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(event, default=str) + "\n")
    print(json.dumps(event, default=str))

    # Best-effort push to the dashboard -- never let a dashboard/network
    # problem interrupt actual trading logic. Local JSONL log above is
    # always the source of truth; this is purely for the live UI.
    if DASHBOARD_URL:
        try:
            headers = {"Content-Type": "application/json"}
            if DASHBOARD_TOKEN:
                headers["Authorization"] = f"Bearer {DASHBOARD_TOKEN}"
            requests.post(f"{DASHBOARD_URL.rstrip('/')}/api/event", json=event, headers=headers, timeout=5)
        except Exception as e:
            print(f"WARNING: dashboard push failed (non-fatal): {e}")


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {
        "position": None,
        "review_state": new_review_trigger_state(),
        "day_start_equity": None,
        "day_loss_halted": False,
        "current_day": None,
        "last_processed_bar": None,
    }


def save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def parse_oanda_time(ts: str) -> datetime:
    ts = ts.replace("Z", "+00:00")
    if "." in ts:
        head, rest = ts.split(".", 1)
        frac, tz = rest[:9], rest[9:]
        ts = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(ts)


class OandaAPIError(RuntimeError):
    def __init__(self, method, path, status_code, text):
        super().__init__(f"Oanda {method} {path} failed [{status_code}]: {text}")
        self.status_code = status_code


# Statuses worth retrying: Oanda's practice API has returned 404 NO_SUCH_TRADE
# for a closed trade for ~21h (2026-08-21) and intermittent 5xx.
TRANSIENT_STATUSES = {404, 429, 500, 502, 503, 504}


def oanda_get(session, path, params=None):
    resp = session.get(f"{OANDA_PRACTICE_URL}{path}", params=params, timeout=30)
    if resp.status_code != 200:
        raise OandaAPIError("GET", path, resp.status_code, resp.text)
    return resp.json()


def oanda_post(session, path, body):
    resp = session.post(f"{OANDA_PRACTICE_URL}{path}", json=body, timeout=30)
    if resp.status_code not in (200, 201):
        raise OandaAPIError("POST", path, resp.status_code, resp.text)
    return resp.json()


def oanda_put(session, path, body):
    resp = session.put(f"{OANDA_PRACTICE_URL}{path}", json=body, timeout=30)
    if resp.status_code not in (200, 201):
        raise OandaAPIError("PUT", path, resp.status_code, resp.text)
    return resp.json()


def with_retry(fn, attempts=3, base_delay=2.0):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except OandaAPIError as e:
            if e.status_code not in TRANSIENT_STATUSES:
                raise
            last = e
        except requests.RequestException as e:
            last = e
        if i < attempts - 1:
            time.sleep(base_delay * 2 ** i)
    raise last


def fetch_recent_candles(session, count: int) -> pd.DataFrame:
    params = {"granularity": GRANULARITY, "price": "M", "count": count}
    data = oanda_get(session, f"/v3/instruments/{INSTRUMENT}/candles", params=params)
    candles = [c for c in data.get("candles", []) if c.get("complete", True)]
    rows = []
    for c in candles:
        mid = c["mid"]
        rows.append({
            "time": parse_oanda_time(c["time"]),
            "Open": float(mid["o"]), "High": float(mid["h"]),
            "Low": float(mid["l"]), "Close": float(mid["c"]),
            "Volume": int(c["volume"]),
        })
    df = pd.DataFrame(rows).set_index("time").sort_index()
    df.index = df.index.tz_convert("America/New_York")
    return df


def get_account_balance(session, account_id: str) -> float:
    data = oanda_get(session, f"/v3/accounts/{account_id}/summary")
    return float(data["account"]["balance"])


def get_open_trades(session, account_id: str):
    data = oanda_get(session, f"/v3/accounts/{account_id}/openTrades")
    return data.get("trades", [])


def closed_trade_from_transactions(session, account_id: str, trade_id: str):
    """Fallback when /trades/{id} is unavailable: rebuild the close from the
    ORDER_FILL transaction that closed the trade. A trade's ID equals the ID
    of the fill that opened it, so everything after it is searched."""
    data = oanda_get(session, f"/v3/accounts/{account_id}/transactions/sinceid", {"id": trade_id})
    pl, price, close_time = 0.0, None, None
    for tx in data.get("transactions", []):
        if tx.get("type") != "ORDER_FILL":
            continue
        for c in tx.get("tradesClosed") or []:
            if c.get("tradeID") == trade_id:
                pl += float(c.get("realizedPL", 0.0))
                price = float(c.get("price", tx.get("price")))
                close_time = tx.get("time")
    if close_time is None:
        raise RuntimeError(f"no closing fill found for trade {trade_id} in transaction history")
    return {"realizedPL": pl, "averageClosePrice": price, "closeTime": close_time,
            "_source": "transactions"}


def get_closed_trade(session, account_id: str, trade_id: str):
    try:
        return with_retry(lambda: oanda_get(session, f"/v3/accounts/{account_id}/trades/{trade_id}"))["trade"]
    except (OandaAPIError, requests.RequestException) as primary:
        try:
            return closed_trade_from_transactions(session, account_id, trade_id)
        except Exception as fallback:
            raise RuntimeError(
                f"closed-trade lookup failed for {trade_id}: primary={primary}; fallback={fallback}"
            ) from primary


def place_order(session, account_id: str, cfg: Book2Config, direction: str,
                 lots: float, stop_distance: float):
    # Stop is a distance from the actual fill (not an absolute price off the
    # signal bar's close), so realized risk matches the position sizing.
    # Take Profit has no distance option in Oanda's API; see attach_take_profit.
    units = int(round(lots * 100000))
    if direction == "short":
        units = -units
    body = {
        "order": {
            "type": "MARKET", "instrument": INSTRUMENT, "units": str(units),
            "timeInForce": "FOK", "positionFill": "DEFAULT",
            "stopLossOnFill": {"distance": f"{stop_distance:.5f}"},
        }
    }
    return oanda_post(session, f"/v3/accounts/{account_id}/orders", body)


def attach_take_profit(session, account_id: str, trade_id: str, target_price: float):
    body = {"takeProfit": {"price": f"{target_price:.5f}", "timeInForce": "GTC"}}
    return with_retry(lambda: oanda_put(
        session, f"/v3/accounts/{account_id}/trades/{trade_id}/orders", body))


def close_trade_at_market(session, account_id: str, trade_id: str):
    return with_retry(lambda: oanda_put(
        session, f"/v3/accounts/{account_id}/trades/{trade_id}/close", {"units": "ALL"}))


def reconcile_open_position(session, account_id, cfg, state):
    pos = state["position"]
    if pos is None:
        return state

    open_trades = get_open_trades(session, account_id)
    still_open = any(t["id"] == pos["trade_id"] for t in open_trades)
    if still_open:
        return state

    closed = get_closed_trade(session, account_id, pos["trade_id"])
    if closed.get("_source"):
        log_event({"event": "closed_trade_lookup_fallback", "trade_id": pos["trade_id"],
                   "source": closed["_source"]})
    realized_pnl = float(closed.get("realizedPL", 0.0))
    close_time = closed.get("closeTime")
    exit_price = float(closed.get("averageClosePrice", pos["entry_price"]))

    ts = parse_oanda_time(close_time) if close_time else datetime.now(timezone.utc)
    balance = get_account_balance(session, account_id)
    state["review_state"] = update_review_trigger_state(state["review_state"], cfg, ts, balance, realized_pnl)

    # Single event carries both the trade-close fields (for the `trades` table)
    # and equity/review_state (for the `account_snapshots` table) -- see
    # worker/src/index.js's handleEvent, which checks for `review_state` on
    # any event, not just trade_closed specifically.
    log_event({
        "event": "trade_closed", "trade_id": pos["trade_id"], "direction": pos["direction"],
        "entry_price": pos["entry_price"], "exit_price": exit_price, "score": pos["score"],
        "realized_pnl": realized_pnl, "close_time": close_time,
        "equity": balance, "review_state": state["review_state"],
    })

    day = ts.date().isoformat()
    if state["current_day"] != day:
        state["current_day"] = day
        state["day_start_equity"] = balance - realized_pnl
        state["day_loss_halted"] = False
    if not state["day_loss_halted"] and state["day_start_equity"]:
        daily_pnl_pct = (balance - state["day_start_equity"]) / state["day_start_equity"]
        if daily_pnl_pct <= -cfg.max_daily_loss_pct:
            state["day_loss_halted"] = True
            log_event({"event": "daily_loss_halt_triggered", "daily_pnl_pct": daily_pnl_pct})

    if state["review_state"]["triggered"]:
        log_event({"event": "REVIEW_TRIGGER_FIRED", "reason": state["review_state"]["triggered_reason"]})

    state["position"] = None
    return state


def try_open_position(session, account_id, cfg, df_ind, state):
    latest = df_ind.iloc[-1]
    latest_time = df_ind.index[-1]

    if state["position"] is not None:
        return state
    if state.get("day_loss_halted"):
        log_event({"event": "skipped_entry", "reason": "daily_loss_halted"})
        return state
    if is_review_triggered(state["review_state"]):
        log_event({"event": "skipped_entry", "reason": "review_trigger_active",
                   "reason_detail": state["review_state"]["triggered_reason"]})
        return state

    signal = evaluate_signal_book2(latest, cfg)
    log_event({"event": "bar_evaluated", "bar_time": latest_time.isoformat(),
               "close": float(latest["Close"]), "signal": signal})

    if signal is None:
        return state

    balance = get_account_balance(session, account_id)
    lots, risk_amount = position_size_book2(cfg, balance, signal["score"], cfg.stop_pips)
    if lots <= 0:
        return state

    signal_price = float(latest["Close"])
    stop_distance = cfg.stop_pips * cfg.pip_size
    target_distance = stop_distance * cfg.min_reward_risk

    order_result = place_order(session, account_id, cfg, signal["direction"], lots, stop_distance)
    fill = order_result.get("orderFillTransaction")
    if fill is None:
        log_event({"event": "order_not_filled", "order_result": order_result})
        return state

    opened = fill.get("tradeOpened", {})
    trade_id = opened.get("tradeID")
    entry_price = float(opened.get("price", fill.get("price", signal_price)))
    if signal["direction"] == "long":
        stop_price = entry_price - stop_distance
        target_price = entry_price + target_distance
        adverse_slippage_pips = (entry_price - signal_price) / cfg.pip_size
    else:
        stop_price = entry_price + stop_distance
        target_price = entry_price - target_distance
        adverse_slippage_pips = (signal_price - entry_price) / cfg.pip_size

    # Recorded before the Take Profit is attached so a failure below can never
    # leave an open trade the runner doesn't know about.
    state["position"] = {
        "direction": signal["direction"], "entry_price": entry_price,
        "stop_price": stop_price, "target_price": target_price,
        "lots": lots, "score": signal["score"], "components": signal["components"],
        "trade_id": trade_id, "entry_time": latest_time.isoformat(),
        "signal_price": signal_price, "adverse_slippage_pips": round(adverse_slippage_pips, 2),
    }
    log_event({"event": "position_opened", **state["position"]})

    try:
        attach_take_profit(session, account_id, trade_id, target_price)
    except Exception as e:
        # Stop is already live, so loss is bounded; without a target the trade
        # would break the strategy's 2R exit, so flatten it. Reconcile records
        # the close as a normal trade_closed on the next run.
        log_event({"event": "take_profit_attach_failed", "trade_id": trade_id, "message": str(e)})
        close_trade_at_market(session, account_id, trade_id)
        log_event({"event": "closed_at_market_after_tp_failure", "trade_id": trade_id})
    return state


def main():
    api_key = os.environ.get("OANDA_API_KEY")
    account_id = os.environ.get("OANDA_ACCOUNT_ID")
    if not api_key or not account_id:
        print("ERROR: set OANDA_API_KEY and OANDA_ACCOUNT_ID in your environment.", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})

    cfg = Book2Config()
    state = load_state()

    try:
        state = reconcile_open_position(session, account_id, cfg, state)

        df = fetch_recent_candles(session, CANDLES_NEEDED)
        latest_bar_time = df.index[-1].isoformat()
        if state.get("last_processed_bar") == latest_bar_time:
            log_event({"event": "no_new_bar", "bar_time": latest_bar_time})
            save_state(state)
            return

        df_ind = compute_indicators_book2(df, cfg)
        state = try_open_position(session, account_id, cfg, df_ind, state)
        state["last_processed_bar"] = latest_bar_time

    except Exception as e:
        log_event({"event": "ERROR", "message": str(e)})
        raise
    finally:
        save_state(state)


if __name__ == "__main__":
    main()

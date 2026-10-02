"""
download_oanda_candles.py

Downloads historical EUR/USD 15-minute candles (mid prices, with volume) from
Oanda's practice API into the CSV layout backtest_confluence.load_data expects:
    time,Open,High,Low,Close,Volume     (time is UTC; load_data converts to New York)

Uses the same instrument, granularity and price component (mid) as the live
runner, so the backtest sees the same volume definition as paper trading.
Read-only market-data requests; only OANDA_API_KEY is needed.

Usage:
    export OANDA_API_KEY="your-practice-api-token"
    python3 download_oanda_candles.py --from 2023-10-01 --out eur_usd_15m.csv
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

OANDA_PRACTICE_URL = "https://api-fxpractice.oanda.com"
INSTRUMENT = "EUR_USD"
GRANULARITY = "M15"
PAGE = 5000  # Oanda's maximum candles per request
STEP = timedelta(minutes=15)


def fetch_page(session, start: datetime):
    params = {"granularity": GRANULARITY, "price": "M", "count": PAGE,
              "from": start.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")}
    for attempt in range(4):
        resp = session.get(f"{OANDA_PRACTICE_URL}/v3/instruments/{INSTRUMENT}/candles",
                           params=params, timeout=60)
        if resp.status_code == 200:
            return resp.json().get("candles", [])
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** (attempt + 1))
            continue
        raise RuntimeError(f"candles request failed [{resp.status_code}]: {resp.text[:300]}")


def parse_time(ts: str) -> datetime:
    head = ts.split(".")[0].rstrip("Z")
    return datetime.fromisoformat(head).replace(tzinfo=timezone.utc)


def download(session, start: datetime, end: datetime):
    rows, cursor = [], start
    while cursor < end:
        candles = fetch_page(session, cursor)
        if not candles:
            break
        last = cursor
        for c in candles:
            t = parse_time(c["time"])
            last = max(last, t)
            if not c.get("complete", True) or t >= end:
                continue
            m = c["mid"]
            rows.append({"time": t.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                         "Open": float(m["o"]), "High": float(m["h"]),
                         "Low": float(m["l"]), "Close": float(m["c"]),
                         "Volume": int(c["volume"])})
        print(f"  ...through {last.isoformat()}  ({len(rows)} candles)", file=sys.stderr)
        if last + STEP <= cursor:
            break
        cursor = last + STEP
        time.sleep(0.3)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", required=True, help="UTC start date, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", default=None, help="UTC end date (exclusive); default now")
    parser.add_argument("--out", default="eur_usd_15m.csv")
    args = parser.parse_args()

    api_key = os.environ.get("OANDA_API_KEY")
    if not api_key:
        print("ERROR: set OANDA_API_KEY in your environment.", file=sys.stderr)
        sys.exit(1)

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
           if args.end else datetime.now(timezone.utc))

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})

    rows = download(session, start, end)
    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time")
    df.to_csv(args.out, index=False)
    print(f"Wrote {len(df)} candles {df['time'].iloc[0]} -> {df['time'].iloc[-1]} to {args.out}")


if __name__ == "__main__":
    main()

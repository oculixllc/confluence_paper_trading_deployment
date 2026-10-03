"""
download_oanda_candles.py

Downloads historical candles (EUR_USD by default) (mid prices, with volume) from Oanda's
practice API into the CSV layout backtest_confluence.load_data expects:
    time,Open,High,Low,Close,Volume     (time is UTC; load_data converts to New York)

Uses the same instrument and price component (mid) as the live runner, so the
backtest sees the same volume definition as paper trading. Rows are written to
disk page by page, so memory stays flat even for millions of 5-minute bars.
Read-only market-data requests; only OANDA_API_KEY is needed.

Usage:
    export OANDA_API_KEY="your-practice-api-token"
    python3 download_oanda_candles.py --from 2023-10-01 --out eur_usd_15m.csv
    python3 download_oanda_candles.py --from 2013-01-01 --granularity M5 --out eur_usd_5m.csv
    python3 download_oanda_candles.py --from 2013-01-01 --instrument USD_JPY --out usd_jpy_15m.csv
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

OANDA_PRACTICE_URL = "https://api-fxpractice.oanda.com"
PAGE = 5000  # Oanda's maximum candles per request
STEPS = {"M5": timedelta(minutes=5), "M15": timedelta(minutes=15), "M30": timedelta(minutes=30)}
HEADER = "time,Open,High,Low,Close,Volume\n"


def fetch_page(session, instrument, granularity, start: datetime):
    params = {"granularity": granularity, "price": "M", "count": PAGE,
              "from": start.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")}
    for attempt in range(4):
        resp = session.get(f"{OANDA_PRACTICE_URL}/v3/instruments/{instrument}/candles",
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


def download(session, instrument, granularity, start: datetime, end: datetime, out):
    step = STEPS[granularity]
    cursor, written, last_written = start, 0, None
    out.write(HEADER)
    while cursor < end:
        candles = fetch_page(session, instrument, granularity, cursor)
        if not candles:
            break
        last = cursor
        for c in candles:
            t = parse_time(c["time"])
            last = max(last, t)
            if not c.get("complete", True) or t >= end or (last_written is not None and t <= last_written):
                continue
            m = c["mid"]
            out.write(f"{t.strftime('%Y-%m-%d %H:%M:%S+00:00')},{float(m['o'])},{float(m['h'])},"
                      f"{float(m['l'])},{float(m['c'])},{int(c['volume'])}\n")
            written, last_written = written + 1, t
        out.flush()
        print(f"  ...through {last.isoformat()}  ({written} candles)", file=sys.stderr, flush=True)
        if last + step <= cursor:
            break
        cursor = last + step
        time.sleep(0.3)
    return written, last_written


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", required=True, help="UTC start date, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", default=None, help="UTC end date (exclusive); default now")
    parser.add_argument("--granularity", default="M15", choices=sorted(STEPS))
    parser.add_argument("--instrument", default="EUR_USD", help="Oanda instrument, e.g. EUR_USD, USD_JPY")
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

    with open(args.out, "w") as out:
        written, last = download(session, args.instrument, args.granularity, start, end, out)
    print(f"Wrote {written} {args.granularity} candles through {last} to {args.out}")


if __name__ == "__main__":
    main()

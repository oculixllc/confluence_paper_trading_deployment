"""Offline tests for the 3-setup runner. Run from repo root:
   PYTHONPATH=engine:paper_trading python3 -m pytest paper_trading/tests -q
"""
import json
import pandas as pd
import pytest

import oanda_paper_trading_setups as R


# ---------------- sizing ----------------

def test_size_eurusd_risk_limited_when_stop_is_wide():
    # $10,000, 1% risk = $100, 50-pip stop -> 20,000 units; margin 2% => $452 (4.5%) < 5% cap => risk binds.
    u, info = R.size_units("EUR_USD", 10_000, 1.13, 0.0050, 1.0, 0.02)
    assert u == 20_000 and info["binding"] == "risk"
    assert info["realized_risk_pct"] == pytest.approx(1.0, abs=0.01)


def test_size_margin_cap_binds_on_tight_stop():
    # $1,000, 15-pip stop -> risk-limited 6,666 units, margin cap 5% = $50 => 50/(1.13*0.02)=2,212 units.
    u, info = R.size_units("EUR_USD", 1_000, 1.13, 0.0015, 1.0, 0.02)
    assert u == 2212 and info["binding"] == "margin"
    assert info["margin_used_pct"] <= 5.0 and info["realized_risk_pct"] < 1.0


def test_size_usdjpy_units_are_usd_and_loss_converted():
    # $10,000 risk $100, stop 0.30 JPY at 150 -> loss/unit = 0.002 USD => 50,000 units; margin 50,000*0.02=$1,000 (10%) > 5% cap
    u, info = R.size_units("USD_JPY", 10_000, 150.0, 0.30, 1.0, 0.02)
    assert u == 25_000 and info["binding"] == "margin"          # 5% of 10,000 / 0.02
    assert info["realized_risk_pct"] == pytest.approx(0.5, abs=0.01)


def test_size_rejects_bad_inputs():
    assert R.size_units("EUR_USD", 1000, 1.1, 0, 1.0, 0.02)[0] == 0


# ---------------- runner with a fake Oanda ----------------

class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._b, self.text = status, body, json.dumps(body)
    def json(self):
        return self._b


class FakeSession:
    def __init__(self, balance=1000.0, open_trades=()):
        self.calls, self.balance, self.open_trades = [], balance, list(open_trades)
        self.fail_tp = False
    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        if url.endswith("/summary"):
            return FakeResp(200, {"account": {"currency": "USD", "balance": str(self.balance), "marginRate": "0.02"}})
        if url.endswith("/openTrades"):
            return FakeResp(200, {"trades": [{"id": t} for t in self.open_trades]})
        if "/trades/" in url:
            return FakeResp(200, {"trade": {"realizedPL": "-5.0", "averageClosePrice": "1.12000", "closeTime": "2026-10-05T10:00:00Z"}})
        raise AssertionError(url)
    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        return FakeResp(201, {"orderFillTransaction": {"price": "1.13000", "tradeOpened": {"tradeID": "77", "price": "1.13002"}}})
    def put(self, url, json=None, timeout=None):
        self.calls.append(("PUT", url, json))
        if self.fail_tp and url.endswith("/orders"):
            return FakeResp(400, {"err": "x"})
        return FakeResp(200, {})


def make_df():
    idx = pd.date_range("2026-10-05 08:00", periods=5, freq="15min", tz="America/New_York")
    return pd.DataFrame({"Open": 1.13, "High": 1.131, "Low": 1.129, "Close": 1.13, "Volume": 100}, index=idx)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(R, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(R, "LOG_PATH", tmp_path / "l.jsonl")
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    events = []
    monkeypatch.setattr(R, "log_event", lambda e: events.append(e))
    return events


SIG = {"setup": "orb", "direction": "long", "stop_price": 1.12850, "sl_pips": 15.0,
       "items": {"range_breakout": True, "vwap": True}}
CTRL = {"enabled": True, "flatten_requested": False, "risk_pct": 1.0, "tp_pct": None, "trail_pct": None,
        "version": 3, "run_id": 2}


def test_entry_places_structural_stop_1pct_trail_and_2r_target(monkeypatch, isolate):
    monkeypatch.setattr(R, "latest_setup_signals", lambda df, inst: ([SIG], []))
    s, ist = FakeSession(), {"position": None, "last_processed_bar": None}
    R.try_enter(s, "ACC", "EUR_USD", ist, make_df(), CTRL)
    order = next(c for c in s.calls if c[0] == "POST")[2]["order"]
    assert order["stopLossOnFill"]["price"] == "1.12850"
    assert order["trailingStopLossOnFill"]["distance"] == "0.01130"           # 1% of 1.13
    assert int(order["units"]) == 2212                                        # margin-capped on $1,000
    put = next(c for c in s.calls if c[0] == "PUT")
    risk = 1.13002 - 1.12850
    assert float(put[2]["takeProfit"]["price"]) == pytest.approx(1.13002 + 2 * risk, abs=1e-5)
    opened = next(e for e in isolate if e["event"] == "position_opened")
    assert opened["sizing"]["binding"] == "margin" and opened["config_version"] == 3 and opened["setup"] == "orb"
    assert ist["position"]["trade_id"] == "77"


def test_short_sign_and_tp_pct_override(monkeypatch, isolate):
    sig = {**SIG, "direction": "short", "stop_price": 1.13150}
    monkeypatch.setattr(R, "latest_setup_signals", lambda df, inst: ([sig], []))
    s, ist = FakeSession(), {"position": None}
    R.try_enter(s, "ACC", "EUR_USD", ist, make_df(), {**CTRL, "tp_pct": 0.2, "trail_pct": 0})
    order = next(c for c in s.calls if c[0] == "POST")[2]["order"]
    assert int(order["units"]) < 0 and "trailingStopLossOnFill" not in order
    put = next(c for c in s.calls if c[0] == "PUT")
    assert float(put[2]["takeProfit"]["price"]) == pytest.approx(1.13002 * (1 - 0.002), abs=1e-5)


def test_tp_attach_failure_flattens(monkeypatch, isolate):
    monkeypatch.setattr(R, "latest_setup_signals", lambda df, inst: ([SIG], []))
    s, ist = FakeSession(), {"position": None}
    s.fail_tp = True
    R.try_enter(s, "ACC", "EUR_USD", ist, make_df(), CTRL)
    assert any(c[0] == "PUT" and c[1].endswith("/close") for c in s.calls)
    assert any(e["event"] == "closed_at_market_after_tp_failure" for e in isolate)


def test_no_entry_when_position_open(monkeypatch, isolate):
    monkeypatch.setattr(R, "latest_setup_signals", lambda df, inst: ([SIG], []))
    s, ist = FakeSession(), {"position": {"trade_id": "1"}}
    R.try_enter(s, "ACC", "EUR_USD", ist, make_df(), CTRL)
    assert not any(c[0] == "POST" for c in s.calls)


def test_reconcile_records_close(isolate):
    s, ist = FakeSession(open_trades=[]), {"position": {"trade_id": "77", "direction": "long", "entry_price": 1.13}}
    R.reconcile(s, "ACC", "EUR_USD", ist)
    assert ist["position"] is None
    ev = next(e for e in isolate if e["event"] == "trade_closed")
    assert ev["exit_price"] == 1.12 and ev["realized_pnl"] == -5.0


def test_flatten_closes_position(isolate):
    s, ist = FakeSession(), {"position": {"trade_id": "77"}}
    R.flatten(s, "ACC", "EUR_USD", ist)
    assert any(c[0] == "PUT" and c[1].endswith("/trades/77/close") for c in s.calls)

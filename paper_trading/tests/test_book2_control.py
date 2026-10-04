"""Control-panel hook in the live Book 2 runner. PYTHONPATH=engine:paper_trading python3 -m pytest paper_trading/tests -q"""
import pandas as pd
import pytest

import oanda_paper_trading_book2 as B
import bot_control


class Sess:
    def __init__(self):
        self.headers = {}
        self.calls = []
    def get(self, *a, **k): raise AssertionError("unexpected GET")
    def put(self, url, json=None, timeout=None):
        self.calls.append(("PUT", url))
        class R:
            status_code = 200
            def json(self): return {}
        return R()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OANDA_API_KEY", "k"); monkeypatch.setenv("OANDA_ACCOUNT_ID", "a")
    monkeypatch.setattr(B, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(B, "LOG_PATH", tmp_path / "log.jsonl")
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    sess = Sess()
    monkeypatch.setattr(B.requests, "Session", lambda: sess)
    events, calls = [], {"try_open": 0, "ack": 0}
    monkeypatch.setattr(B, "log_event", lambda e: events.append(e))
    monkeypatch.setattr(B, "reconcile_open_position", lambda s, a, c, st: st)
    idx = pd.date_range("2026-10-05 10:00", periods=3, freq="15min", tz="America/New_York")
    monkeypatch.setattr(B, "fetch_recent_candles", lambda s, n: pd.DataFrame({"Close": 1.0}, index=idx))
    monkeypatch.setattr(B, "compute_indicators_book2", lambda df, cfg: df)
    def fake_try(s, a, c, df, st):
        calls["try_open"] += 1
        return st
    monkeypatch.setattr(B, "try_open_position", fake_try)
    monkeypatch.setattr(bot_control, "ack_flatten", lambda bot: calls.__setitem__("ack", calls["ack"] + 1))
    return sess, events, calls


def set_ctrl(monkeypatch, **kw):
    cfg = {**bot_control.DEFAULTS, **kw}
    monkeypatch.setattr(bot_control, "fetch_config", lambda bot, d=None, default_enabled=True: (cfg, "live"))


def test_no_dashboard_behaviour_unchanged(env):
    sess, events, calls = env
    B.main()
    assert calls["try_open"] == 1
    assert not any(e["event"] == "skipped_entry" for e in events)


def test_paused_skips_entries_and_marks_bar_processed(env, monkeypatch):
    sess, events, calls = env
    set_ctrl(monkeypatch, enabled=False)
    B.main()
    assert calls["try_open"] == 0
    assert any(e.get("reason") == "paused_by_control_panel" for e in events)
    assert B.load_state()["last_processed_bar"] is not None


def test_flatten_closes_open_trade_and_acks(env, monkeypatch):
    sess, events, calls = env
    st = B.load_state(); st["position"] = {"trade_id": "9"}; B.save_state(st)
    monkeypatch.setattr(B, "reconcile_open_position", lambda s, a, c, state: state)
    set_ctrl(monkeypatch, enabled=False, flatten_requested=True)
    B.main()
    assert ("PUT", "https://api-fxpractice.oanda.com/v3/accounts/a/trades/9/close") in sess.calls
    assert calls["ack"] == 1 and calls["try_open"] == 0

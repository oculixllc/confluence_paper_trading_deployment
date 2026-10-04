"""
bot_control.py

Client for the dashboard Worker's bot-control API. A runner calls fetch_config() once per
cron run to learn whether it is allowed to open new trades and which parameters to use.

Failure behaviour (deliberate):
  * Dashboard unreachable  -> the last config that was successfully fetched (cached on disk)
    keeps applying, so a paused bot stays paused and a running bot keeps running through a
    Cloudflare/network blip.
  * Never fetched, no cache -> `default_enabled` decides. The 3-setup bot passes False: it
    does nothing until you press Start in the journal.
  * DASHBOARD_URL unset     -> no control plane at all; DEFAULTS apply (enabled).
Open positions are never touched by a failed fetch; only an explicit flatten request closes them.
"""

import json
import os
from pathlib import Path

import requests

DEFAULTS = {
    "enabled": True, "flatten_requested": False,
    "risk_pct": 1.0, "tp_pct": None, "trail_pct": None,
    "version": 0, "run_id": 0,
}


def _base():
    url = os.environ.get("DASHBOARD_URL")
    return url.rstrip("/") if url else None


def _headers():
    h = {"Content-Type": "application/json"}
    if os.environ.get("DASHBOARD_TOKEN"):
        h["Authorization"] = f"Bearer {os.environ['DASHBOARD_TOKEN']}"
    return h


def fetch_config(bot_id: str, cache_dir: Path = Path("."), default_enabled: bool = True):
    """Returns (config_dict, source) with source in {"live", "cache", "default", "no_dashboard"}."""
    cache = Path(cache_dir) / f"bot_config_cache_{bot_id}.json"
    base = _base()
    defaults = {**DEFAULTS, "enabled": default_enabled}
    if base is None:
        return {**DEFAULTS}, "no_dashboard"
    try:
        r = requests.get(f"{base}/api/bot-config", params={"bot": bot_id}, headers=_headers(), timeout=8)
        if r.status_code == 200:
            cfg = {**defaults, **r.json()}
            cfg["enabled"] = bool(cfg["enabled"])
            cfg["flatten_requested"] = bool(cfg["flatten_requested"])
            cache.write_text(json.dumps(cfg))
            return cfg, "live"
        err = f"HTTP {r.status_code}"
    except Exception as e:  # network, timeout, bad JSON
        err = str(e)
    if cache.exists():
        try:
            return {**defaults, **json.loads(cache.read_text())}, "cache"
        except Exception:
            pass
    print(f"WARNING: bot-control fetch failed ({err}); using defaults (enabled={default_enabled})")
    return defaults, "default"


def ack_flatten(bot_id: str):
    """Tell the Worker the flatten request was carried out so it clears the flag."""
    base = _base()
    if base is None:
        return
    try:
        requests.post(f"{base}/api/event", json={"event": "flatten_done", "bot_id": bot_id},
                      headers=_headers(), timeout=8)
    except Exception as e:
        print(f"WARNING: flatten ack failed (non-fatal): {e}")

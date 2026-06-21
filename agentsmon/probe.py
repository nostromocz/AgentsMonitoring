"""Service probing — record an up/down sample for each configured service.

A *service* is anything you want SLA/uptime history for (a daemon, a gateway, a bridge): it has a
``process`` pattern (pgrep) and/or a ``health_url``. ``probe_once`` writes one sample per service
to the uptime DB. The dashboard runs this on a background thread, so simply leaving the dashboard
open builds the history — no separate probe service required.
"""
from __future__ import annotations

import subprocess
import time
import urllib.request

from . import db


def _proc_up(pattern: str) -> bool:
    if not pattern:
        return True
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def _http(url: str, timeout: float = 4) -> tuple[bool, float | None]:
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return (200 <= r.status < 400), round(time.time() - t0, 3)
    except Exception:
        return False, None


def probe_once(cfg: dict) -> None:
    for s in cfg.get("services", []):
        name = s.get("name")
        if not name:
            continue
        proc = _proc_up(s.get("process", ""))
        ok, lat, detail = proc, None, f"proc={'up' if proc else 'down'}"
        if s.get("health_url"):
            http_ok, lat = _http(s["health_url"])
            ok = proc and http_ok
            detail += f" http={'ok' if http_ok else 'down'}"
        db.record(name, ok, lat, detail)

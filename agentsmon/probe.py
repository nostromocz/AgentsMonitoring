"""Service probing — record an up/down sample for each configured service.

A *service* is anything you want SLA/uptime history for (a daemon, a gateway, a bridge): it has a
``process`` pattern (pgrep) and/or a ``health_url``. ``probe_once`` writes one sample per service
to the uptime DB. The dashboard runs this on a background thread, so simply leaving the dashboard
open builds the history — no separate probe service required.
"""
from __future__ import annotations

import http.client
import subprocess
import time
import urllib.parse

from . import db


def _proc_up(pattern: str) -> bool:
    if not pattern:
        return True
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def _http(url: str, timeout: float = 4) -> tuple[bool, float | None]:
    """Health check returning (ok, **warm** round-trip latency). We do a warm-up request to
    establish the TCP+TLS connection, then time a second request on the same connection — so the
    reported latency is the server's actual response time, not the one-off handshake cost (which
    for a remote HTTPS endpoint can be ~55 ms and would otherwise dwarf the real latency)."""
    p = urllib.parse.urlparse(url)
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    conn = None
    try:
        conn = cls(p.hostname, p.port, timeout=timeout)
        conn.request("GET", path)          # warm-up: TCP + TLS handshake happens here
        conn.getresponse().read()
        t0 = time.time()                   # timed request reuses the established connection
        conn.request("GET", path)
        r = conn.getresponse()
        r.read()
        return (200 <= r.status < 400), round(time.time() - t0, 3)
    except Exception:
        return False, None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _system_health(cfg: dict) -> tuple[bool, float | None, str]:
    """Availability of the **whole multi-agent system**, not any single component.

    Strict rule (chosen deliberately): the system is *up* only when **every** monitored
    component is up — all configured agents alive, all daemons running, all pinned daemons and
    real services healthy. Any one down = a system outage. Latency = the average current latency
    across all health-checked components (a single system-wide number)."""
    from . import config as _config, detect
    down: list[str] = []
    lats: dict[str, float] = {}

    alive = {a["name"] for a in detect.discover_agents(_config.agent_matches(cfg)) if a.get("alive")}
    for a in cfg.get("agents", []):
        if a.get("enabled", True) and a.get("name") and a["name"] not in alive:
            down.append(a["name"])

    # Daemons (keepalive list) + pinned daemons + real (non-system) services. Anything that
    # advertises a health endpoint is judged by that endpoint — authoritative. The process
    # pattern is the liveness signal ONLY when there's no health_url, since command lines vary
    # by install method (venv / pip --user / pipx) and a stale regex would fake an outage.
    checks = list(cfg.get("daemons", []))
    checks += list(cfg.get("pinned_daemons", []))
    checks += [s for s in cfg.get("services", []) if s.get("kind") != "system"]
    for c in checks:
        name = c.get("name") or c.get("process") or c.get("pattern") or "?"
        url = c.get("health_url")
        if url:
            ok, lat = _http(url)
            if lat is not None:
                lats.setdefault(url, lat)
            if not ok:
                down.append(name)
        else:
            pat = c.get("process") or c.get("pattern") or ""
            if pat and not _proc_up(pat):
                down.append(name)

    uniq: list[str] = []
    for n in down:
        if n not in uniq:
            uniq.append(n)
    up = not uniq
    avg = round(sum(lats.values()) / len(lats), 3) if lats else None
    detail = "all components up" if up else "down: " + ", ".join(uniq[:5])
    return up, avg, detail


def probe_once(cfg: dict) -> None:
    for s in cfg.get("services", []):
        name = s.get("name")
        if not name:
            continue
        if s.get("kind") == "system":
            ok, lat, detail = _system_health(cfg)
            db.record(name, ok, lat, detail)
            continue
        proc = _proc_up(s.get("process", ""))
        ok, lat, detail = proc, None, f"proc={'up' if proc else 'down'}"
        if s.get("health_url"):
            http_ok, lat = _http(s["health_url"])
            ok = proc and http_ok
            detail += f" http={'ok' if http_ok else 'down'}"
        db.record(name, ok, lat, detail)

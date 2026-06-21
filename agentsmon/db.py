"""Tiny SQLite uptime store — powers per-service SLA + uptime + timeline.

One table, ``probes(ts, service, up, latency, detail)``: every probe of a monitored service
appends a row. From that we derive current status, current uptime streak, SLA over a window, and
a bucketed timeline for the dashboard. Standard library only (sqlite3, WAL mode for safe
concurrent read while the probe thread writes).
"""
from __future__ import annotations

import sqlite3
import time

from . import config


def _path() -> str:
    return str(config.state_dir() / "uptime.sqlite")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_path(), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("""CREATE TABLE IF NOT EXISTS probes(
        ts INTEGER NOT NULL, service TEXT NOT NULL, up INTEGER NOT NULL,
        latency REAL, detail TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_probes_service_ts ON probes(service, ts)")
    return c


def record(service: str, up: bool, latency: float | None = None, detail: str = "",
           ts: int | None = None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO probes(ts,service,up,latency,detail) VALUES(?,?,?,?,?)",
                  (int(ts if ts is not None else time.time()), service, 1 if up else 0, latency, detail))


def last(service: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM probes WHERE service=? ORDER BY ts DESC LIMIT 1",
                      (service,)).fetchone()
    return dict(r) if r else None


def sla(service: str, window_seconds: int) -> tuple[float | None, int]:
    """(% of samples that were up in the window, sample count)."""
    since = int(time.time()) - window_seconds
    with _conn() as c:
        rows = c.execute("SELECT up, COUNT(*) n FROM probes WHERE service=? AND ts>=? GROUP BY up",
                         (service, since)).fetchall()
    total = sum(r["n"] for r in rows)
    up = sum(r["n"] for r in rows if r["up"])
    return (100.0 * up / total if total else None), total


def uptime_seconds(service: str) -> int | None:
    """Seconds in the current up-streak (since the last down sample, or since first ever sample)."""
    now = int(time.time())
    with _conn() as c:
        cur = last(service)
        if not cur or not cur["up"]:
            return 0 if cur else None
        down = c.execute("SELECT MAX(ts) t FROM probes WHERE service=? AND up=0",
                         (service,)).fetchone()
        first = c.execute("SELECT MIN(ts) t FROM probes WHERE service=?", (service,)).fetchone()
    if down and down["t"]:
        return now - int(down["t"])
    if first and first["t"]:
        return now - int(first["t"])
    return None


def history_seconds(service: str) -> int:
    """How long we've been recording this service (newest − oldest sample)."""
    with _conn() as c:
        r = c.execute("SELECT MIN(ts) a, MAX(ts) b FROM probes WHERE service=?", (service,)).fetchone()
    return int(r["b"] - r["a"]) if r and r["a"] is not None else 0


def timeline(service: str, window_seconds: int, buckets: int) -> list[dict]:
    """Bucket the window into *buckets* slices. Each → {start: epoch, uptime_pct: float|None}
    (percent of up samples in that bucket; None when no data). The UI greens a bucket at ≥99 %."""
    now = int(time.time())
    start = now - window_seconds
    size = max(1, window_seconds // buckets)
    agg = [[0, 0] for _ in range(buckets)]   # [up_samples, total_samples]
    with _conn() as c:
        rows = c.execute("SELECT ts, up FROM probes WHERE service=? AND ts>=? ORDER BY ts",
                         (service, start)).fetchall()
    for r in rows:
        idx = min(buckets - 1, (int(r["ts"]) - start) // size)
        agg[idx][1] += 1
        agg[idx][0] += int(r["up"])
    out = []
    for i, (up, total) in enumerate(agg):
        out.append({"start": start + i * size,
                    "uptime_pct": (round(100.0 * up / total, 2) if total else None)})
    return out

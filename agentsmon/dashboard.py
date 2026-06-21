"""Live status web page — `agentsmon dashboard`.

Pure standard-library HTTP server (no Flask/FastAPI). Serves one self-contained page that polls
``/api/state`` and renders:
  • Persistent agents      — live tmux agent detection
  • one card per service   — availability with current status, uptime, SLA % and a timeline

A background thread probes the configured services on an interval and appends to the uptime DB,
so just leaving the dashboard running builds the history. Binds 127.0.0.1 by default; optional
HTTP Basic auth (see config.dashboard.auth). All UI text is English.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, detect, probe


def password_hash(password: str) -> str:
    """Hash a dashboard password for storage (we never keep the plaintext in config)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _auth_ok(header: str | None, user: str, pwhash: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return False
    u, _, pw = raw.partition(":")
    return hmac.compare_digest(u, user) and hmac.compare_digest(password_hash(pw), pwhash)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agents Monitoring</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#0b0f17;color:#e5e9f0;font:15px/1.5 system-ui,-apple-system,sans-serif}
 .wrap{max-width:860px;margin:0 auto;padding:28px 18px}
 h1{font-size:20px;margin:0 0 2px} .sub{color:#8b95a7;font-size:13px;margin-bottom:18px}
 h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#8b95a7;margin:26px 0 10px}
 .card{display:flex;align-items:center;gap:12px;background:#131a26;border:1px solid #1e2737;
   border-radius:10px;padding:11px 14px;margin:7px 0}
 .dot{width:10px;height:10px;border-radius:50%;flex:0 0 auto}
 .up{background:#34d399;box-shadow:0 0 8px #34d39988} .down{background:#f87171} .idle{background:#475569}
 .name{font-weight:600} .meta{color:#8b95a7;font-size:13px;margin-left:auto;text-align:right;white-space:nowrap}
 .tag{font-size:11px;color:#9aa6b8;background:#1c2434;border-radius:5px;padding:1px 7px;margin-left:8px}
 .muted{color:#64748b}
 .svc{background:#131a26;border:1px solid #1e2737;border-radius:10px;padding:13px 15px;margin:8px 0}
 .svchead{display:flex;align-items:center;gap:10px}
 .stats{margin-left:auto;text-align:right;font-size:13px;color:#8b95a7}
 .sla{color:#e5e9f0;font-weight:600}
 .tl{display:flex;gap:1px;margin-top:11px;height:26px}
 .tl span{flex:1;border-radius:1px;background:#1e2737}
 .tl .u{background:#34d399} .tl .d{background:#f87171}
</style></head><body><div class="wrap">
<h1>🤖 Agents Monitoring</h1>
<div class="sub" id="sub">loading…</div>
<h2>Persistent agents</h2><div id="agents"></div>
<div id="services"></div>
</div><script>
function age(s){if(s==null)return "?";let d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
 return d?d+"d "+h+"h":h?h+"h "+m+"m":m+"m";}
function fmtTime(t){return t?new Date(t*1000).toLocaleString():"–";}
function agentCard(a){return `<div class="card"><span class="dot ${a.alive?'up':'idle'}"></span>
 <span class="name">${a.name}</span><span class="tag">${a.alive?a.label:'idle'}</span>
 <span class="meta">${a.alive?'running':'idle shell'} · age ${age(a.age)}${a.session_id?` · ${a.session_id.slice(0,8)}`:''}</span></div>`;}
function timeline(buckets){return `<div class="tl">`+buckets.map(b=>
 `<span class="${b==='up'?'u':b==='down'?'d':''}"></span>`).join("")+`</div>`;}
function svcCard(s){
 let sla=s.sla==null?"—":s.sla.toFixed(2)+"%";
 return `<div class="svc"><div class="svchead">
   <span class="dot ${s.up?'up':'down'}"></span><span class="name">${s.name}</span>
   <span class="stats">uptime <b>${age(s.uptime_seconds)}</b> · SLA <span class="sla">${sla}</span>
     <span class="muted">(${s.sla_window_days}d)</span></span></div>
   ${timeline(s.timeline)}
   <div class="meta" style="margin-top:6px;font-size:12px">${s.detail||""} · last check ${fmtTime(s.last_ts)}</div></div>`;}
async function refresh(){
 try{const d=await (await fetch("/api/state")).json();
  document.getElementById("sub").textContent="updated "+new Date(d.time*1000).toLocaleTimeString();
  const A=document.getElementById("agents");
  A.innerHTML=d.agents.length?d.agents.map(agentCard).join(""):"<div class='card muted'>no tmux sessions found</div>";
  const S=document.getElementById("services");
  S.innerHTML=d.services.map(s=>`<h2>${s.name}</h2>`+svcCard(s)).join("");
 }catch(e){document.getElementById("sub").textContent="connection lost…";}
}
refresh();setInterval(refresh,POLL*1000);
</script></body></html>"""


def _service_state(cfg: dict) -> list[dict]:
    win = int(cfg.get("probe", {}).get("sla_window_days", 90)) * 86400
    tdays = int(cfg.get("probe", {}).get("timeline_days", 90))
    out = []
    for s in cfg.get("services", []):
        name = s.get("name")
        if not name:
            continue
        cur = db.last(name)
        sla_pct, _ = db.sla(name, win)
        out.append({
            "name": name,
            "up": bool(cur and cur["up"]),
            "detail": cur["detail"] if cur else "no data yet",
            "last_ts": cur["ts"] if cur else None,
            "uptime_seconds": db.uptime_seconds(name),
            "sla": sla_pct,
            "sla_window_days": cfg.get("probe", {}).get("sla_window_days", 90),
            "timeline": db.timeline(name, tdays * 86400, tdays),
        })
    return out


def _state() -> bytes:
    cfg = config.load()
    data = {
        "time": int(time.time()),
        "agents": detect.discover_agents(config.agent_matches(cfg)),
        "services": _service_state(cfg),
    }
    return json.dumps(data).encode()


def _probe_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            probe.probe_once(config.load())
        except Exception:
            pass
        stop.wait(int(config.load().get("probe", {}).get("interval_seconds", 60)))


def serve(host: str, port: int) -> None:
    cfg = config.load()
    poll = cfg.get("dashboard", {}).get("poll_seconds", 15)
    page = PAGE.replace("POLL", str(poll)).encode()
    auth = cfg.get("dashboard", {}).get("auth") or {}
    auth_user, auth_hash = auth.get("user"), auth.get("pwhash")

    if cfg.get("services"):
        threading.Thread(target=_probe_loop, args=(threading.Event(),), daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _denied(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Agents Monitoring"')
            self.end_headers()

        def do_GET(self):
            if auth_user and auth_hash and not _auth_ok(self.headers.get("Authorization"),
                                                        auth_user, auth_hash):
                return self._denied()
            if self.path.startswith("/api/state"):
                body = _state()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/" or self.path.startswith("/index"):
                body = page
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"Agents Monitoring dashboard → http://{host}:{port}  (Ctrl-C to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()

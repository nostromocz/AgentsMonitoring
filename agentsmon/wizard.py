"""Setup wizard — `agentsmon setup`.

Auto-detects the agents already running in tmux, lets you choose which to supervise, proposes a
restart command for each, optionally watches common daemons (OpenClaw, Hermes), writes the
config, and installs the boot service. Designed to need almost no typing.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import config, detect, service

#: Auto-derived restart command per kind ({id} = session id). Includes the "run unattended" flag,
#: since a supervised agent must come back able to work without an approval prompt.
RESTART_DEFAULTS = {
    "claude-code": "claude --dangerously-skip-permissions --resume {id}",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox resume {id}",
    "antigravity": "agy --conversation {id} --dangerously-skip-permissions",
    "aider": "aider",
    "gemini": "gemini",
}
MATCH_KEYWORD = {"claude-code": "claude", "codex": "codex", "antigravity": "agy",
                 "aider": "aider", "gemini": "gemini"}


def _auto_restart(a: dict) -> str:
    """Build the restart command for a detected agent — no user typing needed."""
    tpl = RESTART_DEFAULTS.get(a["kind"], "")
    if not tpl:
        return ""
    sid = a.get("session_id")
    if sid:
        return tpl.replace("{id}", sid)
    # No session id → drop the resume/conversation argument, keep the base launch.
    return re.sub(r"\s*(--resume|resume|--conversation)\s*\{id\}", "", tpl).strip()


def primary_ip() -> str:
    """This machine's primary outbound IP — the usable address when the dashboard is exposed
    (``0.0.0.0``). Falls back to localhost if offline."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
COMMON_DAEMONS = [
    {"name": "OpenClaw", "pattern": "openclaw", "health_url": "http://127.0.0.1:18789/health",
     "restart": "nohup openclaw gateway > ~/openclaw.log 2>&1 &"},
    {"name": "Hermes", "pattern": "hermes_cli.main gateway",
     "restart": "nohup hermes gateway run --replace > ~/hermes.log 2>&1 &"},
]


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return val or default


def _yes(prompt: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    ans = _ask(f"{prompt} ({d})").lower()
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def _ask_secret(prompt: str) -> str:
    import getpass
    try:
        return getpass.getpass(f"{prompt}: ").strip()
    except (EOFError, Exception):
        return _ask(prompt)


def run() -> int:
    print("=== Agents Monitoring setup ===\n")
    if not shutil.which("tmux"):
        print("⚠️  tmux not found — agents run inside tmux, so install tmux first.")
    print("Scanning tmux for running agents…\n")
    found = detect.discover_agents()
    live = [a for a in found if a["alive"]]
    idle = [a for a in found if not a["alive"]]
    for a in live:
        sid = f"  [{a['session_id'][:8]}]" if a.get("session_id") else ""
        print(f"  • {a['name']}  →  {a['label']}{sid}")
    for a in idle:
        print(f"  · {a['name']}  (idle shell)")
    if not found:
        print("  (no tmux sessions found)")
    print()

    agents = []
    for a in live:
        if not _yes(f"Supervise '{a['name']}' ({a['label']})?"):
            continue
        restart = _auto_restart(a)                              # auto — no typing
        cwd = detect._session_cwd(a["name"]) or str(Path.home())  # auto from the tmux pane
        agents.append({"name": a["name"], "label": a["label"],
                       "match": MATCH_KEYWORD.get(a["kind"], a["kind"]),
                       "restart": restart, "cwd": cwd, "enabled": True})
        print(f"    ↻ auto restart: {restart or '(none)'}")

    daemons = []
    for d in COMMON_DAEMONS:
        if subprocess.run(["pgrep", "-f", d["pattern"]], capture_output=True).returncode == 0:
            if _yes(f"Watch daemon '{d['name']}' (detected running)?"):
                daemons.append(dict(d))                         # includes its default restart
                if d.get("restart"):
                    print(f"    ↻ auto restart: {d['restart']}")

    # Dashboard reach: localhost always works; ask whether to also expose it on the machine's IP.
    print("\nThe dashboard is always reachable on this machine (http://127.0.0.1).")
    expose = _yes("Also make it reachable from outside — on the server's IP / the internet?",
                  default_yes=False)
    host = "0.0.0.0" if expose else "127.0.0.1"
    port = _ask("Dashboard port", "8765")

    cfg = config.load()
    cfg["dashboard"].update({"host": host, "port": int(port) if port.isdigit() else 8765})
    if expose:
        print("⚠️  Exposed beyond localhost — a login is strongly recommended.")
    # HTTP auth — default yes when exposed.
    if _yes("Protect the dashboard with a login (HTTP auth)?", default_yes=expose):
        from . import dashboard
        user = _ask("    username", "admin")
        pw = _ask_secret("    password (hidden)")
        while not pw:
            pw = _ask_secret("    password can't be empty (hidden)")
        cfg["dashboard"]["auth"] = {"user": user, "pwhash": dashboard.password_hash(pw)}
        print("    ✓ HTTP auth enabled (password stored only as a hash).")
    else:
        cfg["dashboard"].pop("auth", None)

    cfg["agents"] = agents
    cfg["daemons"] = daemons
    path = config.save(cfg)
    print(f"\n✓ Saved config to {path}")
    print(f"  Supervising {len(agents)} agent(s), watching {len(daemons)} daemon(s).")

    if _yes("\nInstall the boot service now (keepalive + dashboard, start on login/boot)?"):
        service.install()
    print("\nAll set. Check status anytime with:  agentsmon status")
    if host in ("0.0.0.0", "::"):
        print(f"Dashboard: http://{primary_ip()}:{port}   (local: http://127.0.0.1:{port})")
    else:
        print(f"Dashboard: http://127.0.0.1:{port}")
    return 0

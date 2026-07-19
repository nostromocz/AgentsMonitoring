"""`agentsmon update` — pull the latest code and reload, without re-running setup."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import agentsmon


def _head(src) -> str:
    r = subprocess.run(["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _summary(src, before: str, pull_output: str) -> str:
    """One tidy line instead of git's raw diffstat.

    The diffstat was printed verbatim and cut at 400 characters, so a wide update ended
    mid-word — which reads like a crash to anyone watching, and this is a tool people run
    while someone is showing them how it works.
    """
    after = _head(src)
    if not after or after == before:
        return "✓ Already up to date."
    n = subprocess.run(["git", "-C", str(src), "diff", "--name-only", f"{before}..{after}"],
                       capture_output=True, text=True)
    zmen = len([x for x in n.stdout.splitlines() if x.strip()]) if n.returncode == 0 else 0
    kus = f"{zmen} file{'s' if zmen != 1 else ''} changed" if zmen else "no file changes"
    return f"✓ Updated {before} → {after} ({kus})."


def run() -> int:
    src = Path.home() / ".agentsmon-src"
    if not (src / ".git").is_dir():
        print("No ~/.agentsmon-src clone found — update manually (git pull / pip install -U).")
        return 1
    before = _head(src)
    r = subprocess.run(["git", "-C", str(src), "pull", "--ff-only"], capture_output=True, text=True)
    if r.returncode != 0:
        # Only a failure is worth showing raw — that is when the detail matters.
        print((r.stdout + r.stderr).strip()[:800] or "(no output)")
        return 1
    print(_summary(src, before, r.stdout))
    # If running from site-packages (a pip install), reinstall from the refreshed clone.
    if str(src.resolve()) not in str(Path(agentsmon.__file__).resolve()):
        if subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", str(src)],
                          capture_output=True).returncode != 0:
            subprocess.run([sys.executable, "-m", "pip", "install", "--user",
                            "--break-system-packages", "--upgrade", str(src)], capture_output=True)
    # Migrate the saved config to the current schema (e.g. fold old per-daemon availability cards
    # into the synthetic Multi-Agent System card) so existing installs upgrade cleanly.
    from . import config, wizard
    try:
        cfg = config.load(include_modules=False)
        if wizard.migrate_config(cfg):
            config.save(cfg)
            print("✓ Config migrated to the current layout.")
    except Exception as e:
        print(f"(config migration skipped: {e})")

    # Reload the dashboard on the new code — restart it IMMEDIATELY (don't leave a gap until the
    # next cron tick). Kill it, then kick the launcher so it comes straight back.
    if shutil.which("pkill"):
        subprocess.run(["pkill", "-f", "agentsmon dashboard"], capture_output=True)
    launcher = config.state_dir() / "agentsmon-launch.sh"
    if launcher.exists():
        subprocess.run(["sh", str(launcher)], capture_output=True)
    print("✓ Updated and reloaded. Add new bots with:  agentsmon add")
    return 0

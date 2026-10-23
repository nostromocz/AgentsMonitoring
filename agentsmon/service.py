"""Boot persistence — keep the dashboard + keepalive running across reboots.

We use **cron** (a launcher run `@reboot` and every minute) rather than systemd ``--user`` or a
macOS LaunchAgent. On a headless server reached over SSH there's often no user D-Bus / systemd
instance (``systemctl --user`` fails with "Failed to connect to bus: No medium found") and a
macOS LaunchAgent needs a GUI login session. A cron launcher that nohups the dashboard (guarded
by pgrep) and runs one keepalive pass works everywhere, no login session required.
"""
from __future__ import annotations

import os
import signal
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import agentsmon
from . import config

MARKER = "agentsmon-launch.sh"   # identifies our crontab lines
DASHBOARD_ERE = r"(^|[[:space:]])-m[[:space:]]+agentsmon[[:space:]]+dashboard$"


def _python() -> str:
    return sys.executable or "python3"


def _pythonpath() -> str:
    # Parent of the package dir, so the launcher imports agentsmon whether pip-installed or run
    # straight from a clone.
    return str(Path(agentsmon.__file__).resolve().parent.parent)


def _launcher_path() -> Path:
    return config.state_dir() / MARKER


def _shell_word(value: object) -> str:
    text = str(value)
    if any(char in text for char in ("\0", "\n", "\r")):
        raise ValueError("shell paths must not contain NUL or line breaks")
    return shlex.quote(text)


def _cron_word(value: object) -> str:
    # Cron treats unescaped percent as a command terminator/stdin separator, even inside quotes.
    return _shell_word(value).replace("%", r"\%")


def _launcher_content() -> str:
    state = config.state_dir()
    log = state / "agentsmon.log"
    pythonpath = _shell_word(_pythonpath())
    config_path = _shell_word(config.DEFAULT_PATH)
    state_word = _shell_word(state)
    python = _shell_word(_python())
    log_word = _shell_word(log)
    pidfile = _shell_word(state / "dashboard.pid")
    return f"""#!/bin/sh
# Agents Monitoring launcher — started by cron (@reboot + every minute). Idempotent: starts the
# dashboard only if it isn't running, then runs one keepalive pass (a no-op if disabled / no agents).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONPATH={pythonpath}
export AGENTSMON_CONFIG={config_path}
export AGENTSMON_STATE={state_word}
PY={python}
mkdir -p {state_word}
# Guard on the dashboard's own pidfile (a plain `pgrep -f "agentsmon dashboard"` also matched any
# shell whose command line mentioned the dashboard, and then nothing was started — 2026-09-04).
PIDFILE={pidfile}
"$PY" -m agentsmon.service dashboard-running >> {log_word} 2>&1
DASHBOARD_STATUS=$?
if [ "$DASHBOARD_STATUS" -eq 1 ]; then
  nohup "$PY" -m agentsmon dashboard >> {log_word} 2>&1 &
fi
"$PY" -m agentsmon keepalive >> {log_word} 2>&1
"""


def _write_launcher() -> Path:
    path = _launcher_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A cron shell may still be reading the old launcher: never truncate it in place.
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".launcher-", delete=False) as out:
            staged = Path(out.name)
            out.write(_launcher_content())
            out.flush()
            os.fchmod(out.fileno(), 0o755)
            os.fsync(out.fileno())
        os.replace(staged, path)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_fd = os.open(path.parent, flags)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return path


def refresh_launcher() -> bool:
    """Repair an installed launcher without installing a service for manual users."""
    path = _launcher_path()
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0
    if current == _launcher_content() and mode == 0o755:
        return False
    _write_launcher()
    return True


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    start: str


def _is_dashboard_argv(argv: list[str]) -> bool:
    """Accept only the executable form used by our launcher, never a textual mention."""
    return len(argv) == 4 and argv[1:] == ["-m", "agentsmon", "dashboard"]


def _procfs_available(pid: int) -> bool:
    proc = Path("/proc") / str(pid)
    return all((proc / name).exists() for name in ("status", "cmdline", "stat"))


def _process_identity_proc(pid: int) -> _ProcessIdentity | None:
    """Read a dashboard identity from Linux procfs."""
    proc = Path("/proc") / str(pid)
    try:
        status = (proc / "status").read_text("utf-8")
        uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
        uids = [int(value) for value in uid_line.split()[1:]]
        argv = [part.decode("utf-8", "surrogateescape")
                for part in (proc / "cmdline").read_bytes().split(b"\0") if part]
        fields = (proc / "stat").read_text("utf-8").rsplit(") ", 1)[1].split()
        start = fields[19]  # field 22 overall; fields after pid/(comm) begin at field 3
    except (OSError, StopIteration, ValueError, IndexError):
        return None
    if not uids or any(uid != os.getuid() for uid in uids[:2]) or not _is_dashboard_argv(argv):
        return None
    return _ProcessIdentity(pid, start)


def _process_identity_ps(pid: int) -> _ProcessIdentity | None:
    """Read a dashboard identity from portable/macOS ``ps`` output, failing closed."""
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "pid=", "-o", "uid=", "-o", "lstart=",
             "-o", "command="],
            capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"},
        )
    except OSError:
        return None
    if result.returncode != 0 or result.stderr.strip():
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    fields = lines[0].split(None, 7)
    if len(fields) != 8:
        return None
    try:
        found_pid, uid = int(fields[0]), int(fields[1])
        start = " ".join(fields[2:7])
        time.strptime(start, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    argv = fields[7].split()
    if found_pid != pid or uid != os.getuid() or not _is_dashboard_argv(argv):
        return None
    return _ProcessIdentity(pid, start)


def _process_identity(pid: int) -> _ProcessIdentity | None:
    """Return a stable identity only for a current-UID dashboard process."""
    if pid <= 1 or pid == os.getpid():
        return None
    if _procfs_available(pid):
        return _process_identity_proc(pid)
    return _process_identity_ps(pid)


def _dashboard_identities() -> list[_ProcessIdentity]:
    """Find dashboard processes, validating UID, argv, and start identity for every candidate."""
    from . import dashboard as _dash
    candidates: list[int] = []
    try:
        pid = int(_dash.pidfile_path().read_text("utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    identity = _process_identity(pid)
    if identity is not None:
        return [identity]
    try:
        result = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", DASHBOARD_ERE],
            capture_output=True, text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot search for dashboard processes: {exc}") from exc
    if result.returncode == 1 and not result.stderr.strip():
        return []
    if result.returncode != 0:
        detail = result.stderr.strip() or f"pgrep exited {result.returncode}"
        raise RuntimeError(f"cannot search for dashboard processes: {detail}")
    words = result.stdout.split()
    if any(not word.isdigit() for word in words):
        raise RuntimeError("cannot search for dashboard processes: malformed pgrep output")
    candidates = [int(word) for word in words]
    identities = [_process_identity(candidate) for candidate in candidates]
    return [found for found in identities if found is not None]


def _dashboard_pids() -> list[int]:
    return [identity.pid for identity in _dashboard_identities()]


def _signal_dashboard(identity: _ProcessIdentity, sig: int) -> bool:
    """Signal the same validated process, using pidfd when Linux/Python provide it."""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is not None and pidfd_send is not None:
        try:
            fd = pidfd_open(identity.pid)
        except OSError:
            return False
        try:
            if _process_identity(identity.pid) != identity:
                return False
            pidfd_send(fd, sig)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)
    # Do not check-then-kill a PID here: it can be recycled between those operations.  pkill selects
    # by current UID and the complete dashboard command at the instant it sends the signal.
    signal_name = {signal.SIGTERM: "TERM", signal.SIGKILL: "KILL"}.get(sig)
    if signal_name is None:
        return False
    try:
        result = subprocess.run(
            ["pkill", f"-{signal_name}", "-u", str(os.getuid()), "-f", DASHBOARD_ERE],
            capture_output=True, text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def stop_dashboard(wait: float = 8.0) -> None:
    """Stop the running dashboard and WAIT until it is gone (port freed) before relaunching.
    Otherwise the launcher's guard still sees the dying process, or the new one hits "address
    already in use" — either way the STALE pre-config dashboard keeps serving."""
    identities = _dashboard_identities()
    for identity in identities:
        _signal_dashboard(identity, signal.SIGTERM)
    deadline = time.time() + wait
    while identities and time.time() < deadline:
        identities = [identity for identity in identities if _alive(identity)]
        if identities and time.time() > deadline - wait / 2:
            for identity in identities:
                _signal_dashboard(identity, signal.SIGKILL)
        time.sleep(0.3)


def _alive(identity: _ProcessIdentity) -> bool:
    return _process_identity(identity.pid) == identity


def install() -> int:
    if not shutil.which("cron") and not shutil.which("crontab"):
        print("⚠️  crontab not found. Run these yourself under any process manager:")
        print(f"    {_python()} -m agentsmon dashboard &")
        print(f"    {_python()} -m agentsmon keepalive --loop &")
        return 1
    launcher = _write_launcher()
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except OSError:
        existing = ""
    lines = [ln for ln in existing.splitlines() if MARKER not in ln]
    cron_launcher = _cron_word(launcher)
    lines.append(f"@reboot {cron_launcher}")
    lines.append(f"* * * * * {cron_launcher}")
    proc = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True,
                          capture_output=True)
    if proc.returncode != 0:
        print(f"✗ couldn't update crontab: {proc.stderr.strip()}")
        return 1
    # Stop any dashboard already running, so the launcher restarts it with the CURRENT config
    # (host/port/auth). Without this, a re-run can't change a live dashboard — its pgrep guard
    # would just leave the stale one bound to the old address. (Safe: our own process is
    # "agentsmon setup/service", not "agentsmon dashboard".)
    stop_dashboard()
    # Kick it once now so the dashboard comes up immediately on the configured host.
    subprocess.run(["sh", str(launcher)], capture_output=True)
    print("  ✓ installed cron launcher (@reboot + every minute) — survives logout/reboot.")
    print(f"    launcher: {launcher}")
    print("    No systemd/launchd needed; works headless over SSH.")
    return 0


def uninstall_cron() -> None:
    """Remove our crontab lines (used by the uninstaller)."""
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except OSError:
        return
    kept = [ln for ln in existing.splitlines() if MARKER not in ln]
    subprocess.run(["crontab", "-"], input="\n".join(kept) + ("\n" if kept else ""), text=True,
                   capture_output=True)


def main() -> int:
    return install()


if __name__ == "__main__":
    if sys.argv[1:] == ["dashboard-running"]:
        try:
            running = bool(_dashboard_identities())
        except RuntimeError as exc:
            print(f"agentsmon: {exc}", file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(0 if running else 1)
    raise SystemExit(main())

"""Regression coverage for selectively integrated v0.3.12 runtime fixes."""
from pathlib import Path
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from agentsmon import __main__ as cli, dashboard, db, service


class SafeDashboardLifecycle(unittest.TestCase):
    def test_signal_fallback_selects_current_uid_and_exact_command_atomically(self):
        identity = service._ProcessIdentity(4242, "start")
        completed = MagicMock(returncode=0, stderr="")
        with patch.object(service.os, "pidfd_open", None), \
             patch.object(service.signal, "pidfd_send_signal", None), \
             patch.object(service.subprocess, "run", return_value=completed) as run, \
             patch.object(service.os, "kill") as kill:
            self.assertTrue(service._signal_dashboard(identity, service.signal.SIGTERM))
        kill.assert_not_called()
        self.assertEqual(
            run.call_args.args[0],
            ["pkill", "-TERM", "-u", str(os.getuid()), "-f", service.DASHBOARD_ERE],
        )

    def test_dashboard_fallback_search_is_scoped_to_current_uid(self):
        missing = Path("/definitely/missing/dashboard.pid")
        result = MagicMock(stdout="4242\n", stderr="", returncode=0)
        def identity(pid):
            return service._ProcessIdentity(pid, "123") if pid == 4242 else None
        with patch("agentsmon.dashboard.pidfile_path", return_value=missing), \
             patch.object(service.subprocess, "run", return_value=result) as run, \
             patch.object(service, "_process_identity", side_effect=identity):
            self.assertEqual(service._dashboard_pids(), [4242])
        self.assertEqual(
            run.call_args.args[0],
            ["pgrep", "-u", str(os.getuid()), "-f", service.DASHBOARD_ERE],
        )

    def test_dashboard_search_reports_pgrep_regex_errors(self):
        failed = MagicMock(stdout="", stderr="pgrep: regex error", returncode=2)
        with patch("agentsmon.dashboard.pidfile_path", return_value=Path("/missing")), \
             patch.object(service.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "regex error"):
                service._dashboard_identities()

    def test_real_pgrep_finds_dashboard_with_missing_or_stale_pidfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"dashboard": {"host": "127.0.0.1", "port": 0}}))
            env = dict(os.environ, AGENTSMON_STATE=str(root), AGENTSMON_CONFIG=str(cfg))
            proc = subprocess.Popen([sys.executable, "-m", "agentsmon", "dashboard"], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not (root / "dashboard.pid").exists():
                    time.sleep(0.05)
                for contents in (None, "999999999"):
                    candidate = root / ("missing.pid" if contents is None else "stale.pid")
                    if contents is not None:
                        candidate.write_text(contents, encoding="utf-8")
                    with self.subTest(pidfile=contents), \
                         patch("agentsmon.dashboard.pidfile_path", return_value=candidate):
                        self.assertIn(proc.pid, service._dashboard_pids())
            finally:
                proc.terminate()
                proc.wait(timeout=2)

    def test_portable_ps_identity_requires_uid_and_exact_dashboard_command(self):
        start = "Fri Oct 23 12:34:56 2026"
        valid = f"4242 {os.getuid()} {start} /usr/bin/python3 -m agentsmon dashboard\n"
        completed = MagicMock(stdout=valid, stderr="", returncode=0)
        with patch.object(service.subprocess, "run", return_value=completed) as run:
            self.assertEqual(service._process_identity_ps(4242),
                             service._ProcessIdentity(4242, start))
        self.assertEqual(
            run.call_args.args[0],
            ["ps", "-ww", "-p", "4242", "-o", "pid=", "-o", "uid=", "-o", "lstart=",
             "-o", "command="],
        )

        invalid = (
            f"4242 {os.getuid() + 1} {start} /usr/bin/python3 -m agentsmon dashboard\n",
            f"4242 {os.getuid()} {start} /usr/bin/python3 -m agentsmon dashboard extra\n",
            f"4242 {os.getuid()} nonsense /usr/bin/python3 -m agentsmon dashboard\n",
            valid + valid,
        )
        for output in invalid:
            with self.subTest(output=output), \
                 patch.object(service.subprocess, "run",
                              return_value=MagicMock(stdout=output, stderr="", returncode=0)):
                self.assertIsNone(service._process_identity_ps(4242))

    def test_process_identity_uses_portable_ps_without_procfs(self):
        expected = service._ProcessIdentity(4242, "portable-start")
        with patch.object(service, "_procfs_available", return_value=False), \
             patch.object(service, "_process_identity_ps", return_value=expected) as portable:
            self.assertEqual(service._process_identity(4242), expected)
        portable.assert_called_once_with(4242)

    def test_launcher_lookup_errors_fail_closed_without_duplicate_start(self):
        content = service._launcher_content()
        self.assertIn("DASHBOARD_STATUS=$?", content)
        self.assertIn('if [ "$DASHBOARD_STATUS" -eq 1 ]; then', content)
        self.assertNotIn('if ! "$PY" -m agentsmon.service dashboard-running', content)

    def test_launcher_does_not_let_reused_positive_pid_suppress_start(self):
        # A real dashboard already running for this UID is a legitimate reason for the launcher
        # not to start a second one.  The test remains fully exercised in isolated CI/test hosts.
        if service._dashboard_pids():
            self.skipTest("current UID already has a validated agentsmon dashboard")
        with tempfile.TemporaryDirectory(prefix="agentsmon state ' % ") as tmp:
            root = Path(tmp)
            cfg = root / "config with ' and %.json"
            cfg.write_text(json.dumps({"dashboard": {"host": "127.0.0.1", "port": 0}}))
            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                (root / "dashboard.pid").write_text(str(sleeper.pid))
                with patch.object(service.config, "state_dir", return_value=root), \
                     patch.object(service.config, "DEFAULT_PATH", cfg):
                    launcher = service._write_launcher()
                env = dict(os.environ, AGENTSMON_STATE=str(root), AGENTSMON_CONFIG=str(cfg))
                subprocess.run(["sh", str(launcher)], env=env, check=True, timeout=10)
                deadline = time.time() + 5
                while time.time() < deadline:
                    started = int((root / "dashboard.pid").read_text())
                    if started != sleeper.pid:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("malformed/reused pidfile suppressed dashboard startup")
                identity = service._process_identity(started)
                self.assertIsNotNone(identity)
                self.assertEqual(identity.pid if identity else None, started)
                with patch("agentsmon.dashboard.pidfile_path", return_value=root / "dashboard.pid"):
                    service.stop_dashboard(wait=3)
                self.assertIsNone(sleeper.poll(), "the stale pidfile target was killed")
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=2)

    def test_real_dashboard_pid_is_validated_and_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"dashboard": {"host": "127.0.0.1", "port": 0}}))
            env = dict(os.environ, AGENTSMON_STATE=str(root), AGENTSMON_CONFIG=str(cfg))
            proc = subprocess.Popen([sys.executable, "-m", "agentsmon", "dashboard"], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not (root / "dashboard.pid").exists():
                    time.sleep(0.05)
                with patch("agentsmon.dashboard.pidfile_path", return_value=root / "dashboard.pid"):
                    self.assertEqual(service._dashboard_pids(), [proc.pid])
                    service.stop_dashboard(wait=3)
                proc.wait(timeout=2)
                self.assertIsNotNone(proc.returncode)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_launcher_write_is_atomic_and_refresh_repairs_stale_content(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(service.config, "state_dir", return_value=Path(tmp)), \
             patch.object(service, "refresh_bytecode", return_value=None, create=True):
            path = Path(tmp) / service.MARKER
            path.write_text("stale", encoding="utf-8")
            self.assertTrue(service.refresh_launcher())
            content = path.read_text(encoding="utf-8")
            self.assertIn('-m agentsmon.service dashboard-running', content)
            self.assertNotIn('\npgrep ', content)
            subprocess.run(["sh", "-n", str(path)], check=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            self.assertFalse(service.refresh_launcher())
            self.assertEqual(list(Path(tmp).glob(".launcher-*")), [])

    def test_launcher_quotes_every_dynamic_path_and_rejects_line_injection(self):
        special = Path("/tmp/state dir/'quote' $dollar;semi%percent")
        dynamic = {
            "state": special,
            "config": Path("/tmp/config dir/'quote'%value"),
            "python": "/tmp/python dir/'quote'%bin",
            "package": "/tmp/package dir/'quote'%src",
        }
        with patch.object(service.config, "state_dir", return_value=dynamic["state"]), \
             patch.object(service.config, "DEFAULT_PATH", dynamic["config"]), \
             patch.object(service, "_python", return_value=dynamic["python"]), \
             patch.object(service, "_pythonpath", return_value=dynamic["package"]):
            content = service._launcher_content()
        for value in (*dynamic.values(), special / "agentsmon.log"):
            self.assertIn(shlex.quote(str(value)), content)
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as script:
            script.write(content)
            script.flush()
            subprocess.run(["sh", "-n", script.name], check=True)

        for injected in ("bad\ncommand", "bad\rcommand", "bad\0command"):
            with self.subTest(value=repr(injected)), \
                 patch.object(service, "_python", return_value=injected):
                with self.assertRaises(ValueError):
                    service._launcher_content()

    def test_install_quotes_launcher_path_and_escapes_cron_percent(self):
        launcher = Path("/tmp/launcher dir/'quote'%job")
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            return MagicMock(stdout="", stderr="", returncode=0)

        with patch.object(service.shutil, "which", return_value="/usr/bin/crontab"), \
             patch.object(service, "_write_launcher", return_value=launcher), \
             patch.object(service, "stop_dashboard"), \
             patch.object(service.subprocess, "run", side_effect=run):
            self.assertEqual(service.install(), 0)
        payload = next(kwargs["input"] for args, kwargs in calls if args == ["crontab", "-"])
        cron_word = shlex.quote(str(launcher)).replace("%", r"\%")
        self.assertIn(f"@reboot {cron_word}\n", payload)
        self.assertIn(f"* * * * * {cron_word}\n", payload)

    def test_launcher_write_fsyncs_file_and_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(service.config, "state_dir", return_value=Path(tmp)), \
             patch.object(service.os, "fsync", wraps=os.fsync) as fsync:
            service._write_launcher()
        self.assertGreaterEqual(fsync.call_count, 2)

    def test_dashboard_entrypoint_repairs_stale_launcher_content_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / service.MARKER
            launcher.write_text("stale", encoding="utf-8")
            launcher.chmod(0o600)
            cfg = {"dashboard": {"host": "127.0.0.1", "port": 8765}}
            with patch.object(service.config, "state_dir", return_value=root), \
                 patch.object(service.config, "load", return_value=cfg), \
                 patch.object(dashboard, "serve") as serve:
                self.assertEqual(cli.main(["dashboard"]), 0)
                expected = service._launcher_content()
            serve.assert_called_once_with("127.0.0.1", 8765)
            self.assertEqual(launcher.read_text(encoding="utf-8"), expected)
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)

    def test_refresh_repairs_executable_mode_even_when_content_is_current(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(service.config, "state_dir", return_value=Path(tmp)):
            launcher = service._write_launcher()
            launcher.chmod(0o600)
            self.assertTrue(service.refresh_launcher())
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)


class DatabaseRuntimeCorrectness(unittest.TestCase):
    def test_uptime_starts_at_first_success_and_resets_only_after_qualifying_outage(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(db.config, "state_dir", return_value=Path(tmp)), \
             patch.object(db.time, "time", return_value=200):
            for ts, up in ((100, False), (110, False), (120, True)):
                db.record("initial-failures", up, ts=ts)
            self.assertEqual(db.uptime_seconds("initial-failures", min_outage=3), 80)

            for ts, up in ((100, True), (110, False), (120, True),
                           (130, False), (140, False), (150, False), (160, True)):
                db.record("outages", up, ts=ts)
            self.assertEqual(db.uptime_seconds("outages", min_outage=3), 40)

    def test_real_transactions_commit_rollback_and_close(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(db.config, "state_dir", return_value=Path(tmp)):
            with db._db() as connection:
                connection.execute(
                    "INSERT INTO probes(ts,service,up,latency,detail) VALUES(1,'committed',1,NULL,'')"
                )
            with self.assertRaises(db.sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            self.assertEqual(db.last("committed")["up"], 1)

            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with db._db() as rolled_back:
                    rolled_back.execute(
                        "INSERT INTO probes(ts,service,up,latency,detail) "
                        "VALUES(2,'rolled-back',1,NULL,'')"
                    )
                    raise RuntimeError("rollback")
            with self.assertRaises(db.sqlite3.ProgrammingError):
                rolled_back.execute("SELECT 1")
            self.assertIsNone(db.last("rolled-back"))

    def test_dashboard_executes_observed_uptime_edges(self):
        fmt = dashboard.PAGE[
            dashboard.PAGE.index("function fmtDuration") : dashboard.PAGE.index("function fmtTime")
        ]
        render = dashboard.PAGE[
            dashboard.PAGE.index("function renderService") : dashboard.PAGE.index("function renderAgents")
        ]
        harness = f"""
{fmt}
const STATE={{up:['dot','UP','text','head'],nodata:['','','','']}};
const nodes=new Proxy({{}},{{get:(o,k)=>o[k]||(o[k]={{}})}});
const q=(root, selector)=>nodes[selector];
const fmtLat=(value)=>String(value);
const renderTimeline=()=>{{}};
{render}
const base={{state:'up',sla:null,sla_window_days:90,sla_samples:0,metric:'x',
  latency_ms:null,timeline:[],timeline_days:90}};
console.log(JSON.stringify([null,0,86399,86400].map(value=>{{
  renderService({{}},{{...base,uptime_seconds:value}}); return nodes['.m-uptime'].textContent;
}})));
"""
        result = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), ["–", "0m", "23h 59m", "1d 0h"])
        self.assertIn("current observed uptime", dashboard.PAGE.lower())


if __name__ == "__main__":
    unittest.main()

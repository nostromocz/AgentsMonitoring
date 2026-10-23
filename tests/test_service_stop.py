"""Stopping the dashboard must hit exactly the dashboard (2026-09-04): a bare
``pkill -f "agentsmon dashboard"`` also killed the shell script that mentioned it."""
import os, subprocess, tempfile, time, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTSMON_STATE", tempfile.mkdtemp(prefix="agentsmon-test-state-"))
from agentsmon import dashboard, service  # noqa: E402


class StopDashboard(unittest.TestCase):
    def test_reused_pidfile_does_not_kill_unrelated_process(self):
        victim = subprocess.Popen(["sleep", "30"])
        bystander = subprocess.Popen(["sh", "-c", "echo agentsmon dashboard >/dev/null; sleep 30"])
        try:
            dashboard.pidfile_path().write_text(str(victim.pid), encoding="utf-8")
            with patch.object(service.subprocess, "run",
                              return_value=MagicMock(stdout="", stderr="", returncode=1)):
                service.stop_dashboard(wait=0.2)
            time.sleep(0.5)
            self.assertIsNone(victim.poll(), "a reused positive PID was killed")
            self.assertIsNone(bystander.poll(), "a process merely mentioning the dashboard was killed")
        finally:
            for p in (victim, bystander):
                if p.poll() is None:
                    p.kill()
                p.wait(timeout=2)

    def test_pidfile_rejects_init_and_self(self):
        for unsafe_pid in (1, os.getpid()):
            with self.subTest(pid=unsafe_pid), \
                 patch.object(dashboard, "pidfile_path", return_value=Path("ignored")), \
                 patch.object(Path, "read_text", return_value=str(unsafe_pid)), \
                 patch.object(service.subprocess, "run",
                              return_value=MagicMock(stdout="", stderr="", returncode=1)), \
                 patch.object(service.os, "kill") as kill:
                self.assertEqual(service._dashboard_pids(), [])
                kill.assert_not_called()

    def test_write_pidfile_records_our_pid(self):
        dashboard.write_pidfile()
        self.assertEqual(int(dashboard.pidfile_path().read_text()), os.getpid())


if __name__ == "__main__":
    unittest.main()


class ActionCsrf(unittest.TestCase):
    """A cross-site form cannot set a custom header; the dashboard page always does (2026-09-05)."""
    def test_action_requires_the_page_header(self):
        from agentsmon import dashboard as d
        self.assertTrue(d._action_allowed({"X-Agentsmon": "action"}))
        self.assertTrue(d._action_allowed({"X-Agentsmon": " Action "}))
        self.assertFalse(d._action_allowed({}))
        self.assertFalse(d._action_allowed({"X-Agentsmon": "yes"}))

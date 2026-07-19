import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import probe


class HermesPlatformHealthTests(unittest.TestCase):
    def test_connected_platform_is_up(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "gateway_state.json"
            state_file.write_text(json.dumps({
                "gateway_state": "running",
                "platforms": {"telegram": {"state": "connected"}},
            }))
            service = {
                "kind": "hermes_platform",
                "platform": "telegram",
                "health_url": "http://127.0.0.1:8642/health",
                "state_file": str(state_file),
            }

            with patch.object(probe, "_http", return_value=(True, 0.012)):
                up, latency, detail = probe._hermes_platform_health(service)

        self.assertTrue(up)
        self.assertEqual(latency, 0.012)
        self.assertEqual(detail, "gateway=running telegram=connected")

    def test_untrusted_runtime_values_never_leak(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "gateway_state.json"
            untrusted_value = "unexpected-runtime-state"
            state_file.write_text(json.dumps({
                "gateway_state": "running",
                "platforms": {
                    "telegram": {
                        "state": untrusted_value,
                        "error_message": "sensitive-runtime-message",
                    },
                },
            }))
            service = {
                "platform": "telegram",
                "health_url": "http://127.0.0.1:8642/health",
                "state_file": str(state_file),
            }

            with patch.object(probe, "_http", return_value=(True, 0.012)):
                up, _latency, detail = probe._hermes_platform_health(service)

        self.assertFalse(up)
        self.assertEqual(detail, "gateway=running telegram=other")
        self.assertNotIn(untrusted_value, detail)
        self.assertNotIn("sensitive-runtime-message", detail)

    def test_probe_once_records_disconnected_platform_as_down(self):
        service = {
            "name": "Hermes Telegram Gateway",
            "kind": "hermes_platform",
            "platform": "telegram",
            "health_url": "http://127.0.0.1:8642/health",
            "state_file": "/unused/in/unit-test.json",
        }
        with (
            patch.object(probe, "_hermes_platform_health",
                         return_value=(False, 0.008, "gateway=running telegram=disconnected")) as health,
            patch.object(probe.db, "record") as record,
        ):
            probe.probe_once({"services": [service]})

        health.assert_called_once_with(service)
        record.assert_called_once_with(
            "Hermes Telegram Gateway", False, 0.008,
            "gateway=running telegram=disconnected",
        )

    def test_disconnected_platform_makes_system_availability_down(self):
        service = {
            "name": "Hermes Telegram Gateway",
            "kind": "hermes_platform",
            "platform": "telegram",
            "health_url": "http://127.0.0.1:8642/health",
            "state_file": "/unused/in/unit-test.json",
        }
        cfg = {"agents": [], "daemons": [], "pinned_daemons": [], "services": [service]}
        with (
            patch.object(probe, "_hermes_platform_health",
                         return_value=(False, 0.008, "gateway=running telegram=disconnected")) as health,
            patch.object(probe, "_http", return_value=(True, 0.008)),
        ):
            up, latency, detail = probe._system_health(cfg)

        health.assert_called_once_with(service)
        self.assertFalse(up)
        self.assertEqual(latency, 0.008)
        self.assertEqual(detail, "down: Hermes Telegram Gateway")

    def test_malformed_runtime_state_fails_closed_without_crashing(self):
        cases = [
            "not-json",
            json.dumps({"gateway_state": "running", "platforms": []}),
            json.dumps({"gateway_state": "running", "platforms": {}}),
            json.dumps({"gateway_state": "stopped", "platforms": {"telegram": {"state": "connected"}}}),
        ]
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "gateway_state.json"
            service = {
                "platform": "telegram",
                "health_url": "http://127.0.0.1:8642/health",
                "state_file": str(state_file),
            }
            for raw in cases:
                with self.subTest(raw=raw):
                    state_file.write_text(raw)
                    with patch.object(probe, "_http", return_value=(True, 0.005)):
                        up, latency, detail = probe._hermes_platform_health(service)
                    self.assertFalse(up)
                    self.assertEqual(latency, 0.005)
                    self.assertNotIn("not-json", detail)

    def test_http_or_state_file_failure_is_down(self):
        service = {
            "platform": "telegram",
            "health_url": "http://127.0.0.1:8642/health",
            "state_file": "/definitely/missing/gateway_state.json",
        }
        with patch.object(probe, "_http", return_value=(False, None)):
            up, latency, detail = probe._hermes_platform_health(service)
        self.assertFalse(up)
        self.assertIsNone(latency)
        self.assertEqual(detail, "gateway=down")

        with patch.object(probe, "_http", return_value=(True, 0.004)):
            up, latency, detail = probe._hermes_platform_health(service)
        self.assertFalse(up)
        self.assertEqual(latency, 0.004)
        self.assertEqual(detail, "gateway=unknown platform=unknown")


if __name__ == "__main__":
    unittest.main()

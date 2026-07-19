import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import db, wizard


class HermesPlatformWizardTests(unittest.TestCase):
    def test_detects_telegram_without_copying_runtime_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "gateway_state.json"
            state_file.write_text(json.dumps({
                "gateway_state": "running",
                "platforms": {
                    "telegram": {
                        "state": "connected",
                        "error_message": "secret-error-value",
                        "token": "secret-token-value",
                        "chat_id": "secret-chat-value",
                    },
                },
            }))
            with patch.object(wizard, "_hermes_gateway_state_file", return_value=state_file):
                service = wizard._hermes_platform_service("telegram")

        self.assertEqual(service, {
            "name": "Hermes Telegram Gateway",
            "kind": "hermes_platform",
            "platform": "telegram",
            "health_url": "http://127.0.0.1:8642/health",
            "state_file": str(state_file),
        })
        serialized = json.dumps(service)
        self.assertNotIn("secret-token-value", serialized)
        self.assertNotIn("secret-chat-value", serialized)
        self.assertNotIn("secret-error-value", serialized)

    def test_migration_replaces_legacy_bridge_monitor_idempotently(self):
        new_service = {
            "name": "Hermes Telegram Gateway",
            "kind": "hermes_platform",
            "platform": "telegram",
            "health_url": "http://127.0.0.1:8642/health",
            "state_file": "/home/test/.hermes/gateway_state.json",
        }
        cfg = {
            "services": [
                {"name": "Telegram Bridge Status", "process": "agent2telegram run",
                 "health_url": "https://api.telegram.org/"},
                {"name": "Unrelated Service", "health_url": "http://127.0.0.1:9999/health"},
            ],
            "daemons": [
                {"name": "Telegram Bridge", "pattern": "agent2telegram run", "restart": "legacy"},
                {"name": "Hermes", "pattern": "hermes_cli.main gateway"},
            ],
            "pinned_daemons": [],
        }
        with (
            patch.object(wizard, "_hermes_platform_service", return_value=new_service),
            patch.object(wizard.detect, "daemon_telegram_bot", return_value=None),
        ):
            changed = wizard.migrate_config(cfg)
            changed_again = wizard.migrate_config(cfg)

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual([s["name"] for s in cfg["services"]], [
            "Multi-Agent System Availability", "Unrelated Service", "Hermes Telegram Gateway",
        ])
        self.assertEqual([d["name"] for d in cfg["daemons"]], ["Hermes"])
        self.assertEqual(cfg["services"].count(new_service), 1)

    def test_migration_keeps_services_without_the_exact_legacy_signature(self):
        unrelated = [
            {
                "name": "Telegram Bridge Status",
                "process": "custom-telegram-relay",
                "health_url": "http://127.0.0.1:9999/health",
            },
            {
                "name": "Telegram Bridge Status",
                "process": "agent2telegram run",
            },
        ]
        cfg = {"services": list(unrelated), "daemons": [], "pinned_daemons": []}
        with (
            patch.object(wizard, "_hermes_platform_service", return_value=None),
            patch.object(wizard.detect, "daemon_telegram_bot", return_value=None),
        ):
            changed = wizard.migrate_config(cfg)

        self.assertTrue(changed)  # the canonical system card is still added
        for service in unrelated:
            self.assertIn(service, cfg["services"])

    def test_add_persists_monitor_migration_even_without_other_candidates(self):
        new_service = {
            "name": "Hermes Telegram Gateway",
            "kind": "hermes_platform",
            "platform": "telegram",
            "health_url": "http://127.0.0.1:8642/health",
            "state_file": "/home/test/.hermes/gateway_state.json",
        }
        cfg = {
            "services": [{
                "name": "Telegram Bridge Status",
                "process": "agent2telegram run",
                "health_url": "https://api.telegram.org/",
            }],
            "daemons": [{"name": "Telegram Bridge", "pattern": "agent2telegram run"}],
            "pinned_daemons": [],
        }
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "config.json"
            config_path.write_text("{}")
            with (
                patch.object(wizard.config, "DEFAULT_PATH", config_path),
                patch.object(wizard.config, "load", return_value=cfg),
                patch.object(wizard.config, "save") as save,
                patch.object(wizard.service, "install") as install,
                patch.object(wizard, "_scan_candidates", return_value=[]),
                patch.object(wizard, "_hermes_platform_service", return_value=new_service),
                patch.object(wizard.detect, "daemon_telegram_bot", return_value=None),
            ):
                result = wizard.add()

        self.assertEqual(result, 0)
        save.assert_called_once_with(cfg)
        install.assert_called_once_with()
        self.assertEqual([s["name"] for s in cfg["services"]], [
            "Multi-Agent System Availability", "Hermes Telegram Gateway",
        ])
        self.assertEqual(cfg["daemons"], [])

    def test_migration_preserves_legacy_database_series(self):
        new_service = {
            "name": "Hermes Telegram Gateway",
            "kind": "hermes_platform",
            "platform": "telegram",
            "health_url": "http://127.0.0.1:8642/health",
            "state_file": "/home/test/.hermes/gateway_state.json",
        }
        cfg = {
            "services": [{
                "name": "Telegram Bridge Status",
                "process": "agent2telegram run",
                "health_url": "https://api.telegram.org/",
            }],
            "daemons": [],
            "pinned_daemons": [],
        }
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            with patch.object(db.config, "state_dir", return_value=state_dir):
                db.record("Telegram Bridge Status", True, 0.1, "legacy", ts=100)
                with (
                    patch.object(wizard, "_hermes_platform_service", return_value=new_service),
                    patch.object(wizard.detect, "daemon_telegram_bot", return_value=None),
                ):
                    wizard.migrate_config(cfg)
                old_last = db.last("Telegram Bridge Status")
                new_last = db.last("Hermes Telegram Gateway")

        self.assertIsNotNone(old_last)
        self.assertEqual((old_last or {})["detail"], "legacy")
        self.assertIsNone(new_last)


if __name__ == "__main__":
    unittest.main()

"""Fail-closed tests for destructive dashboard row actions."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from agentsmon import dashboard


class DashboardActionAuthorizationTests(unittest.TestCase):
    @patch.object(dashboard.keepalive, "_start")
    @patch.object(dashboard, "_set_enabled")
    @patch.object(dashboard.config, "load")
    def test_restart_is_rejected_without_explicit_dashboard_actions(
        self, load, set_enabled, start
    ):
        load.return_value = {
            "agents": [
                {
                    "name": "Gwen - Codex",
                    "restart": "codex --resume",
                    "enabled": False,
                }
            ]
        }

        ok, message = dashboard._agent_action("Gwen - Codex", "restart")

        self.assertFalse(ok)
        self.assertIn("not allowed", message)
        set_enabled.assert_not_called()
        start.assert_not_called()

    @patch.object(dashboard.keepalive, "_start")
    @patch.object(dashboard, "_set_enabled")
    @patch.object(dashboard.config, "load")
    def test_restart_remains_available_for_explicitly_managed_agent(
        self, load, set_enabled, start
    ):
        managed = {
            "name": "Gwen - Claude",
            "restart": "claude --continue",
            "enabled": True,
            "dashboard_actions": True,
        }
        load.return_value = {"agents": [managed]}

        ok, message = dashboard._agent_action("Gwen - Claude", "restart")

        self.assertTrue(ok)
        self.assertIn("restarting", message)
        set_enabled.assert_called_once_with("Gwen - Claude", True)
        start.assert_called_once()

    @patch.object(dashboard.keepalive, "_start")
    @patch.object(dashboard, "_set_enabled")
    @patch.object(dashboard.config, "load")
    def test_duplicate_name_or_non_boolean_permission_is_rejected(
        self, load, set_enabled, start
    ):
        cases = (
            {
                "agents": [{"name": "Shared", "dashboard_actions": True}],
                "daemons": [{"name": "Shared", "dashboard_actions": False}],
            },
            {
                "agents": [{"name": "Shared", "dashboard_actions": True}],
                "daemons": [{"name": "Shared", "dashboard_actions": True}],
            },
            {"agents": [{"name": "Shared", "dashboard_actions": "true"}]},
        )
        for cfg in cases:
            with self.subTest(cfg=cfg):
                load.return_value = cfg
                ok, message = dashboard._agent_action("Shared", "restart")
                self.assertFalse(ok)
                self.assertIn("not allowed", message)

        set_enabled.assert_not_called()
        start.assert_not_called()


class DashboardActionStateTests(unittest.TestCase):
    @patch.object(dashboard.detect, "telegram_links", return_value={})
    @patch.object(dashboard.detect, "pinned_agents", return_value=[])
    @patch.object(dashboard.detect, "discover_agents")
    def test_state_explicitly_enables_actions_for_managed_agent(
        self, discover, pinned, telegram_links
    ):
        discover.return_value = [
            {
                "name": "Gwen - Claude",
                "alive": True,
                "label": "Claude",
                "vendor": "anthropic",
            }
        ]
        cfg = {
            "agents": [
                {
                    "name": "Gwen - Claude",
                    "restart": "claude --continue",
                    "dashboard_actions": True,
                }
            ]
        }

        state = dashboard._agents_state(cfg)

        self.assertTrue(state[0]["actions_enabled"])

    @patch.object(dashboard.detect, "telegram_links", return_value={})
    @patch.object(dashboard.detect, "pinned_agents")
    @patch.object(dashboard.detect, "discover_agents", return_value=[])
    def test_state_denies_actions_for_duplicate_name(
        self, discover, pinned, telegram_links
    ):
        pinned.return_value = [{"name": "Shared", "alive": True}]
        cfg = {
            "agents": [{"name": "Shared", "dashboard_actions": True}],
            "daemons": [{"name": "Shared", "dashboard_actions": True}],
        }

        state = dashboard._agents_state(cfg)

        self.assertFalse(state[0]["actions_enabled"])


class DashboardActionRenderingTests(unittest.TestCase):
    def test_row_actions_are_rendered_only_when_explicitly_enabled(self):
        render = dashboard.PAGE[
            dashboard.PAGE.index("function renderAgents") : dashboard.PAGE.index("async function refresh")
        ]

        self.assertRegex(render, r"const actions\s*=\s*a\.actions_enabled\s*\?")
        self.assertIn("${actions}", render)


if __name__ == "__main__":
    unittest.main()

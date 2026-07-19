"""Regression tests for configured agent display overrides."""

import unittest
from unittest.mock import patch

from agentsmon import dashboard


class AgentDisplayOverrideTests(unittest.TestCase):
    def test_tmux_agent_name_color_override_reaches_dashboard_state(self):
        cfg = {
            "agents": [{"name": "Gwen - Claude", "name_color": "blue"}],
            "pinned_daemons": [],
        }
        detected = [{"name": "Gwen - Claude", "alive": True, "kind": "claude"}]

        with (
            patch.object(dashboard.detect, "discover_agents", return_value=detected),
            patch.object(dashboard.detect, "telegram_links", return_value={}),
        ):
            rows = dashboard._agents_state(cfg)

        self.assertEqual(rows[0].get("name_color"), "blue")


if __name__ == "__main__":
    unittest.main()

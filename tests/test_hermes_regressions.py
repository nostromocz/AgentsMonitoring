import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import config, dashboard, keepalive, probe, wizard


class HermesRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'gateway_state.json'
        self.hermes = {'name': 'Hermes Telegram Gateway', 'kind': 'hermes_platform',
                       'platform': 'telegram', 'health_url': 'http://localhost/health',
                       'state_file': str(self.state), 'process': 'must-not-probe'}
        self.legacy = {'name': 'Telegram Bridge Status', 'process': 'agent2telegram run',
                       'health_url': 'https://api.telegram.org/'}
        self.daemon = {'name': 'Telegram Bridge', 'pattern': 'agent2telegram run', 'restart': 'legacy'}

    def test_http_state_matrix(self):
        states = [None, b'not-json', b'\xff', b'[]', b'{}',
                  {'gateway_state': [], 'platforms': {'telegram': {'state': {}}}}]
        for gateway in ['running', 'starting', 'stopped']:
            for platform in ['connected', 'disconnected', 'connecting', 'failed']:
                states.append({'gateway_state': gateway, 'platforms': {'telegram': {'state': platform}}})
        for state in states:
            for http_up in (False, True):
                with self.subTest(state=state, http_up=http_up):
                    if state is None:
                        self.state.unlink(missing_ok=True)
                    else:
                        self.state.write_bytes(state if isinstance(state, bytes) else json.dumps(state).encode())
                    with patch.object(probe, '_http', return_value=(http_up, .01)):
                        up, latency, detail = probe._hermes_platform_health(self.hermes)
                    expected = http_up and isinstance(state, dict) and state.get('gateway_state') == 'running' and state.get('platforms', {}).get('telegram', {}).get('state') == 'connected'
                    self.assertEqual(up, bool(expected))
                    self.assertEqual(latency, .01)
                    self.assertNotIn('not-json', detail)

    def test_special_dispatch_modules_and_heartbeat_coexist(self):
        heartbeat = self.root / 'heartbeat'
        heartbeat.touch()
        module = {'name': 'Module', 'command': 'unused', 'health_url': 'must-not-http'}
        cfg = {'services': [self.hermes, module], 'agents': [], 'keepalive': {'enabled': True}}
        with (patch.object(keepalive, 'heartbeat_path', return_value=heartbeat),
              patch.object(probe, '_http', side_effect=AssertionError('generic HTTP')),
              patch.object(probe, '_proc_up', side_effect=AssertionError('generic process')),
              patch.object(probe, '_command', side_effect=AssertionError('module rerun')),
              patch.object(probe, '_hermes_platform_health', return_value=(True, .02, 'connected')),
              patch.object(probe.db, 'last', return_value={'ts': 1000, 'up': True, 'latency': .04}),
              patch.object(probe.time, 'time', return_value=1000)):
            os.utime(heartbeat, (1000, 1000))
            self.assertEqual(probe._system_health(cfg), (True, .03, 'all components up'))
            os.utime(heartbeat, (0, 0))
            self.assertIn('keepalive', probe._system_health(cfg)[2])
            os.utime(heartbeat, (1000, 1000))
            with patch.object(probe.db, 'last', return_value={'ts': 0, 'up': True}):
                self.assertIn('Module', probe._system_health(cfg)[2])
        cfg['services'] = [{'name': 'System', 'kind': 'system'}, self.hermes, {'name': 'Module', 'command': 'check'}]
        with (patch.object(probe, '_system_health', return_value=(True, None, 'all')) as system,
              patch.object(probe, '_hermes_platform_health', return_value=(False, .02, 'disconnected')) as hermes,
              patch.object(probe, '_proc_up', return_value=True) as process,
              patch.object(probe, '_http', side_effect=AssertionError('generic HTTP')),
              patch.object(probe, '_command', return_value=(True, .04, 'module')) as command,
              patch.object(probe.db, 'record') as record):
            probe.probe_once(cfg)
            system.assert_called_once_with(cfg)
            hermes.assert_called_once_with(self.hermes)
            process.assert_called_once_with('')
            command.assert_called_once_with('check', 15.0)
            self.assertEqual(record.call_count, 3)

    def test_no_valid_runtime_preserves_legacy_and_persisted_replacement_allows_removal(self):
        for raw in (None, 'invalid', '[]', '{}', '{"platforms":{"telegram":null}}',
                    '{"platforms":{"telegram":{}}}',
                    '{"gateway_state":[],"platforms":{"telegram":{"state":"connected"}}}'):
            with self.subTest(raw=raw):
                if raw is None:
                    self.state.unlink(missing_ok=True)
                else:
                    self.state.write_text(raw)
                cfg = {'services': [dict(wizard.SYSTEM_SERVICE), self.legacy], 'daemons': [self.daemon]}
                original = copy.deepcopy(cfg)
                with patch.object(wizard, '_hermes_gateway_state_file', return_value=self.state):
                    self.assertFalse(wizard.migrate_config(cfg))
                    self.assertEqual(cfg, original)
                    cfg['services'].append(self.hermes)
                    self.assertTrue(wizard.migrate_config(cfg))
                    self.assertNotIn(self.legacy, cfg['services'])
                    self.assertEqual(cfg['daemons'], [])
                    self.assertFalse(wizard.migrate_config(cfg))

    def test_disconnected_runtime_is_a_valid_replacement(self):
        self.state.write_text(json.dumps({
            'gateway_state': 'running', 'platforms': {'telegram': {'state': 'disconnected'}}}))
        cfg = {'services': [self.legacy], 'daemons': [self.daemon]}
        with patch.object(wizard, '_hermes_gateway_state_file', return_value=self.state):
            self.assertTrue(wizard.migrate_config(cfg))
            self.assertFalse(wizard.migrate_config(cfg))
        self.assertEqual(cfg['daemons'], [])
        self.assertEqual([s.get('kind') for s in cfg['services']], ['system', 'hermes_platform'])

    def test_add_without_runtime_keeps_legacy_and_does_not_save(self):
        path = self.root / 'config.json'
        original = {'services': [dict(wizard.SYSTEM_SERVICE), self.legacy], 'daemons': [self.daemon]}
        config.save(original, path)
        saved = path.read_bytes()
        with (patch.object(config, 'DEFAULT_PATH', path),
              patch.object(wizard, '_hermes_gateway_state_file', return_value=self.state),
              patch.object(wizard, '_scan_candidates', return_value=[]),
              patch.object(wizard, '_telegram_bridge_service', return_value=self.legacy),
              patch.object(config, 'save') as save,
              patch.object(wizard.service, 'install') as install):
            self.assertEqual(wizard.add(), 0)
            save.assert_not_called()
            install.assert_not_called()
        self.assertEqual(path.read_bytes(), saved)

    def test_persistence_safe_load_save_and_migration(self):
        modules = self.root / 'modules'
        modules.mkdir()
        executable = modules / 'check'
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o700)
        path = self.root / 'config.json'
        config.save({'modules_dir': str(modules), 'services': [self.legacy], 'daemons': [self.daemon]}, path)
        with patch.object(wizard, '_hermes_platform_service', return_value=self.hermes):
            cfg = config.load(path, include_modules=False)
            self.assertFalse(any(s.get('source') == 'module' for s in cfg['services']))
            self.assertTrue(wizard.migrate_config(cfg))
            config.save(cfg, path)
            saved = path.read_text()
            self.assertFalse(wizard.migrate_config(config.load(path, include_modules=False)))
            runtime = config.load(path)
            self.assertEqual(sum(s.get('source') == 'module' for s in runtime['services']), 1)
            config.save(runtime, path)
            self.assertEqual(path.read_text(), saved)
            self.assertEqual(sum(s.get('source') == 'module' for s in runtime['services']), 1)
        executable.unlink()
        self.assertFalse(any(s.get('source') == 'module' for s in config.load(path)['services']))

    def test_dashboard_and_add_save_without_module_discovery(self):
        path = self.root / 'config.json'
        for action in ('dashboard', 'add'):
            with self.subTest(action=action):
                config.save({'services': [self.legacy], 'daemons': [self.daemon]}, path)
                with (patch.object(config, 'DEFAULT_PATH', path),
                      patch.object(config, 'discover_modules', side_effect=AssertionError('discovered while saving')),
                      patch.object(wizard, '_hermes_platform_service', return_value=self.hermes),
                      patch.object(wizard, '_scan_candidates', return_value=[]),
                      patch.object(wizard.service, 'install')):
                    if action == 'add':
                        self.assertEqual(wizard.add(), 0)
                    else:
                        # Stop at the runtime load, after startup migration has saved.
                        real_load = config.load
                        def load(*args, **kwargs):
                            if kwargs.get('include_modules') is False:
                                return real_load(*args, **kwargs)
                            raise RuntimeError('runtime load reached')
                        with patch.object(config, 'load', side_effect=load):
                            with self.assertRaisesRegex(RuntimeError, 'runtime load reached'):
                                dashboard.serve('127.0.0.1', 0)
                saved = json.loads(path.read_text())
                self.assertIn(self.hermes, saved['services'])
                self.assertNotIn(self.legacy, saved['services'])
                self.assertFalse(any(s.get('source') == 'module' for s in saved['services']))

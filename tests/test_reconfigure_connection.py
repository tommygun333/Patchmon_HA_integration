"""No-Home-Assistant-install tests for the real native reconfigure flow body.

Extracts the current flow method and URL validator from config_flow.py so the
core behavior is exercised without importing HA or its Voluptuous dependency.
"""
from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import urlparse

FLOW = Path(__file__).resolve().parents[1] / 'custom_components' / 'patchmon' / 'config_flow.py'


class FakeVol:
    class Invalid(Exception):
        pass

    @staticmethod
    def Required(name, default=None):
        return name

    @staticmethod
    def Schema(value):
        return value


class FakePatchMonError(Exception):
    pass


class FakeConnectionError(FakePatchMonError):
    pass


class FakeAuthenticationError(FakePatchMonError):
    pass


class FakePermissionError(FakePatchMonError):
    pass


class FakeTwoFactorRequired(FakeAuthenticationError):
    pass


@dataclass
class FakeEntry:
    entry_id: str = 'original-entry-id'
    unique_id: str = 'https://patchmon.home'
    title: str = 'patchmon.home'
    data: dict = field(default_factory=lambda: {
        'url': 'https://patchmon.home',
        'api_id': 'scope-id', 'api_key': 'secret',
        'username': 'patchuser', 'password': 'patchpass',
        'verify_ssl': True,
    })
    options: dict = field(default_factory=lambda: {
        'auto_security_hosts': {'proxmox': ['pve-uuid'], 'pbs': ['pbs-uuid']},
        'tracked_packages': {'pve-uuid': ['openssl']},
        'scan_interval': 15,
    })


class FakeClient:
    def __init__(self, url, error=None):
        self.base_url = url.strip().rstrip('/')
        for suffix in ('/api/v1', '/api'):
            if self.base_url.endswith(suffix):
                self.base_url = self.base_url[:-len(suffix)]
                break
        self._error = error
        self.reads = 0

    async def async_get_hosts(self):
        self.reads += 1
        if self._error is not None:
            raise self._error
        return [{'id': 'pve-uuid'}]


def extract_functions():
    tree = ast.parse(FLOW.read_text(encoding='utf-8'))
    valid_url = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_valid_url')
    flow = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PatchMonConfigFlow')
    method = next(n for n in flow.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == 'async_step_reconfigure')
    module = ast.fix_missing_locations(ast.Module(body=[valid_url, method], type_ignores=[]))
    return compile(module, str(FLOW), 'exec')


class FakeFlow:
    def __init__(self, entry=None, others=(), client_error=None):
        self.entry = entry or FakeEntry()
        self.all_entries = [self.entry, *others]
        self.hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_entries=lambda domain: self.all_entries
        ))
        self.client_error = client_error
        self.client = None
        self.last_updated = None

    def _get_reconfigure_entry(self):
        return self.entry

    def async_show_form(self, **kwargs):
        return {'type': 'form', **kwargs}

    def async_update_and_abort(self, entry, *, unique_id, data_updates):
        self.last_updated = {'entry': entry, 'unique_id': unique_id,
                             'data_updates': dict(data_updates)}
        entry.data.update(data_updates)
        entry.unique_id = unique_id
        return {'type': 'abort', 'reason': 'reconfigure_successful'}


class ReconfigureTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = {
            'Any': object, 'ConfigFlowResult': object,
            'urlparse': urlparse, 'vol': FakeVol,
            'CONF_URL': 'url', 'CONF_VERIFY_SSL': 'verify_ssl',
            'DOMAIN': 'patchmon', 'URL_SELECTOR': 'url_selector',
            'PatchMonError': FakePatchMonError,
            'PatchMonConnectionError': FakeConnectionError,
            'PatchMonAuthenticationError': FakeAuthenticationError,
            'PatchMonPermissionError': FakePermissionError,
            'PatchMonTwoFactorRequired': FakeTwoFactorRequired,
        }
        exec(extract_functions(), cls.ns)

    async def invoke(self, flow, user_input=None):
        self.ns['_client_from_input'] = lambda _hass, data: self._make_client(flow, data)
        return await self.ns['async_step_reconfigure'](flow, user_input)

    @staticmethod
    def _make_client(flow, data):
        client = FakeClient(data['url'], flow.client_error)
        flow.client = client
        return client

    async def test_defaults_show_current_url_without_exposing_secrets(self):
        f = FakeFlow()
        res = await self.invoke(f)
        self.assertEqual(res['step_id'], 'reconfigure')
        self.assertEqual(set(res['data_schema']), {'url', 'verify_ssl'})
        self.assertIsNone(f.last_updated)

    async def test_changes_url_and_unique_id_without_replacing_entry_or_options(self):
        f = FakeFlow()
        options_ref = f.entry.options
        options_before = f.entry.options.copy()
        data_before = f.entry.data.copy()
        result = await self.invoke(f, {'url': 'http://192.168.1.33:3000/', 'verify_ssl': False})
        self.assertEqual(result['type'], 'abort')
        self.assertEqual(f.entry.entry_id, 'original-entry-id')
        self.assertEqual(f.entry.title, 'patchmon.home')
        self.assertEqual(f.entry.unique_id, 'http://192.168.1.33:3000')
        self.assertEqual(f.entry.data['url'], 'http://192.168.1.33:3000')
        self.assertIs(f.entry.options, options_ref)
        self.assertEqual(f.entry.options, options_before)
        for key in ('api_id', 'api_key', 'username', 'password'):
            self.assertEqual(f.entry.data[key], data_before[key])
        self.assertEqual(f.last_updated['data_updates'],
                         {'url': 'http://192.168.1.33:3000', 'verify_ssl': False})
        self.assertEqual(f.client.reads, 1)

    async def test_unreachable_url_keeps_all_existing_settings(self):
        f = FakeFlow(client_error=FakeConnectionError('DNS unavailable'))
        original = f.entry.data.copy()
        res = await self.invoke(f, {'url': 'http://192.168.1.33:3000', 'verify_ssl': True})
        self.assertEqual(res['errors'], {'base': 'cannot_connect'})
        self.assertEqual(f.entry.data, original)
        self.assertEqual(f.entry.unique_id, 'https://patchmon.home')
        self.assertIsNone(f.last_updated)

    async def test_auth_errors_do_not_write_url(self):
        f = FakeFlow(client_error=FakeAuthenticationError('bad scope'))
        res = await self.invoke(f, {'url': 'http://192.168.1.33:3000', 'verify_ssl': True})
        self.assertEqual(res['errors'], {'base': 'invalid_auth'})
        self.assertEqual(f.entry.data['url'], 'https://patchmon.home')

    async def test_prevents_two_entries_from_sharing_destination_url(self):
        other = FakeEntry(entry_id='another', unique_id='http://192.168.1.33:3000')
        f = FakeFlow(others=[other])
        res = await self.invoke(f, {'url': 'http://192.168.1.33:3000', 'verify_ssl': False})
        self.assertEqual(res['errors'], {'base': 'already_configured'})
        self.assertEqual(f.client.reads, 0)
        self.assertIsNone(f.last_updated)

    async def test_reusing_own_url_is_not_duplicate(self):
        f = FakeFlow()
        res = await self.invoke(f, {'url': 'https://patchmon.home', 'verify_ssl': True})
        self.assertEqual(res['type'], 'abort')

    async def test_rejects_address_without_scheme(self):
        f = FakeFlow()
        res = await self.invoke(f, {'url': '192.168.1.33:3000'})
        self.assertEqual(res['errors'], {'url': 'invalid_url'})
        self.assertIsNone(f.last_updated)

    async def test_unique_id_tracks_canonical_url(self):
        f = FakeFlow()
        res = await self.invoke(f, {'url': 'http://192.168.1.33:3000/api/v1/',
                                    'verify_ssl': True})
        self.assertEqual(res['type'], 'abort')
        self.assertEqual(f.entry.data['url'], 'http://192.168.1.33:3000')
        self.assertEqual(f.entry.unique_id, 'http://192.168.1.33:3000')


if __name__ == '__main__':
    unittest.main()

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from aiohttp.test_utils import TestClient, TestServer
import asyncio
from hub import Controller, make_app, sender_available

SPEAKER = dict(connected=True, paired=True, bonded=True, trusted=True, name='ULT FIELD 1')
LOOPBACK = '536870919\tmodule-loopback\tsource=audiohub_network.monitor sink=bluez_output.sony latency_msec=20\t\n'


class FakePactl:
    """Stands in for hub.command and records what the controller asks PipeWire to do."""

    def __init__(self, sinks='', loaded=False, sink_inputs='[]'):
        self.calls = []
        self.sinks = sinks
        self.sink_inputs = sink_inputs
        self.loaded = loaded
        self.loaded_args = None

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[:4] == ('pactl', 'list', 'short', 'modules'):
            return LOOPBACK if self.loaded else ''
        if args[:4] == ('pactl', 'list', 'short', 'sinks'):
            return self.sinks
        if args[:5] == ('pactl', '-f', 'json', 'list', 'sink-inputs'):
            return self.sink_inputs
        if args[:3] == ('pactl', 'load-module', 'module-loopback'):
            self.loaded_args = args
            self.loaded = True
            return '536870919'
        if args[:2] == ('pactl', 'unload-module'):
            self.loaded = False
        if args[:4] == ('pactl', '-f', 'json', 'list'):
            return '[]'
        return ''

    def count(self, verb):
        return sum(1 for call in self.calls if call[:2] == ('pactl', verb))


PHONE = 'bluez_input.AA_BB_CC_DD_EE_01.2'


class FakeBluetoothctl:
    """A bluetoothctl that prints some prompts, then stays quiet like a real one."""

    def __init__(self, lines):
        self.sent = []
        self.returncode = None
        self.stdin = self
        self.stdout = self.output(lines)

    async def output(self, lines):
        for line in lines:
            yield line.encode()
        await asyncio.sleep(3600)

    def write(self, data):
        self.sent.append(data.decode().strip())

    def is_closing(self):
        return False

    async def wait(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.controller = Controller(Path(self.temp.name) / 'settings.json')
        self.client = TestClient(TestServer(make_app(self.controller)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    def headers(self):
        return {'X-AudioHub-Token': self.controller.token}

    async def test_foreign_site_cannot_change_audio_settings(self):
        response = await self.client.post('/api/settings', json={'volume': 0},
            headers={**self.headers(), 'Origin': 'https://attacker.example'})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.controller.store.data['volume'], 30)

    async def test_missing_token_cannot_disconnect_speaker(self):
        response = await self.client.post('/api/speaker/disconnect', json={})
        self.assertEqual(response.status, 403)
        self.assertTrue(self.controller.store.data['speaker_enabled'])

    async def test_invalid_sender_does_not_persist_or_start_receiver(self):
        response = await self.client.post('/api/settings', json={'sender_ip': '127.0.0.1'}, headers=self.headers())
        self.assertEqual(response.status, 400)
        self.assertFalse(self.controller.store.path.exists())
        self.assertIsNone(self.controller.receiver)

    async def test_rebinding_host_cannot_read_status(self):
        response = await self.client.get('/api/status', headers={'Host': 'attacker.example'})
        self.assertEqual(response.status, 403)

    async def test_bluetooth_calls_need_the_token_and_reject_bad_input(self):
        self.assertEqual((await self.client.post('/api/bluetooth/pair', json={})).status, 403)
        for action, body in [('reboot', {}), ('forget', {'address': '74:68:59:68:28:17'}),
                             ('forget', {'address': 'not-a-mac'}), ('forget', [1])]:
            with self.subTest(action=action, body=body):
                response = await self.client.post('/api/bluetooth/' + action, json=body, headers=self.headers())
                self.assertEqual(response.status, 400)

    async def test_pairing_window_answers_prompts_trusts_new_devices_and_closes(self):
        process = FakeBluetoothctl(['[agent] Confirm passkey 123456 (yes/no): ', '[CHG] Device AA:BB:CC:DD:EE:01 Paired: yes\r\n',
                                    '[CHG] Device 74:68:59:68:28:17 Paired: yes'])
        trusted = []
        async def spawn(*args, **kwargs):
            return process
        with mock.patch('hub.asyncio.create_subprocess_exec', spawn), mock.patch('hub.bluetooth_devices', lambda: []), \
                mock.patch('hub.bluetooth_trust', trusted.append):
            await self.controller.start_pairing()
            await asyncio.sleep(.1)
            self.assertGreater(self.controller.status()['bluetooth_in']['pairing'], 100)
            await self.controller.stop_pairing()
        # Trust goes over D-Bus: a line written to stdin can be swallowed as the answer to a pending prompt.
        self.assertEqual(process.sent, ['default-agent', 'pairable on', 'discoverable on', 'yes',
                                        'discoverable off', 'pairable off', 'quit'])
        self.assertEqual(trusted, ['AA:BB:CC:DD:EE:01'])
        self.assertEqual(self.controller.status()['bluetooth_in']['pairing'], 0)

    async def test_paired_phone_that_is_not_trusted_gets_trusted_so_audio_can_connect(self):
        devices = [dict(address='AA:BB:CC:DD:EE:01', name='Mac', connected=False, paired=True, trusted=False),
                   dict(address='AA:BB:CC:DD:EE:02', name='PC', connected=False, paired=True, trusted=True),
                   dict(address='AA:BB:CC:DD:EE:03', name='Scan only', connected=True, paired=False, trusted=False)]
        trusted = []
        with mock.patch('hub.bluetooth_devices', lambda: devices), mock.patch('hub.bluetooth_trust', trusted.append):
            await self.controller.refresh_bluetooth_inputs()
        self.assertEqual(trusted, ['AA:BB:CC:DD:EE:01'])

    async def test_phone_stream_is_moved_to_its_own_sink_and_routed_only_while_it_plays(self):
        phone = json.dumps([{'index': 8168, 'sink': 37, 'properties': {'node.name': PHONE, 'api.bluez5.profile': 'a2dp-source'}}])
        pactl = FakePactl(sinks='37\taudiohub_network\tPipeWire\tfloat32le 2ch 48000Hz\tIDLE\n'
                                '45\taudiohub_bluetooth\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING\n', sink_inputs=phone)
        self.controller.cached = dict(speaker=SPEAKER, sink='bluez_output.sony', sink_index=10)
        with mock.patch('hub.command', pactl):
            await self.controller.refresh_activity()
            await self.controller.route_audio()
            self.assertIn(('pactl', 'move-sink-input', '8168', 'audiohub_bluetooth'), pactl.calls)
            self.assertEqual(self.controller.store.data['source'], 'bluetooth')
            self.assertTrue(self.controller.status()['bluetooth_in']['streaming'])
            self.assertIn('source=audiohub_bluetooth.monitor', pactl.loaded_args)
            pactl.sinks = pactl.sinks.replace('RUNNING', 'SUSPENDED')
            pactl.sink_inputs = '[]'
            self.controller.activity.last_active = None
            await self.controller.refresh_activity()
            await self.controller.route_audio()
        self.assertFalse(pactl.loaded)
        self.assertFalse(self.controller.status()['bluetooth_in']['streaming'])

    async def test_bluetooth_sink_is_created_when_missing_and_streams_already_there_are_left_alone(self):
        phone = json.dumps([{'index': 8168, 'sink': 45, 'properties': {'api.bluez5.profile': 'a2dp-source'}}])
        pactl = FakePactl(sinks='45\taudiohub_bluetooth\tPipeWire\tfloat32le 2ch 48000Hz\tIDLE\n', sink_inputs=phone)
        with mock.patch('hub.command', pactl):
            await self.controller.refresh_activity()
        self.assertEqual(pactl.count('move-sink-input'), 0)
        missing = FakePactl(sinks='')
        with mock.patch('hub.command', missing):
            await self.controller.refresh_activity()
        self.assertTrue(any(call[:3] == ('pactl', 'load-module', 'module-null-sink') and 'sink_name=audiohub_bluetooth' in call
                            for call in missing.calls))

    async def test_unknown_operation_is_rejected(self):
        response = await self.client.post('/api/speaker/reboot', json={}, headers=self.headers())
        self.assertEqual(response.status, 400)

    async def test_slow_bluetooth_reconnect_does_not_block_the_dashboard(self):
        release = threading.Event()

        def bluetooth(action='status'):
            if action == 'connect':
                release.wait(5)
                raise RuntimeError('org.bluez.Error.Failed: br-connection-page-timeout')
            return {**SPEAKER, 'connected': False}

        with mock.patch('hub.bluetooth', bluetooth), mock.patch('hub.command', FakePactl()):
            await self.controller.snapshot()
            self.controller.maybe_reconnect()
            await asyncio.sleep(.1)
            self.assertFalse(self.controller.reconnect_task.done())
            response = await asyncio.wait_for(
                self.client.post('/api/settings', json={'volume': 40}, headers=self.headers()), 2)
            self.assertEqual(response.status, 200)
            release.set()
            await self.controller.reconnect_task
        self.assertEqual(self.controller.reconnect_failures, 1)
        self.assertGreater(self.controller.next_reconnect, time.monotonic() + 15)

    async def test_failed_reconnect_is_not_retried_before_its_backoff_ends(self):
        with mock.patch('hub.bluetooth', lambda action='status': {**SPEAKER, 'connected': False}), \
                mock.patch('hub.command', FakePactl()):
            await self.controller.snapshot()
            self.controller.next_reconnect = time.monotonic() + 60
            self.controller.maybe_reconnect()
        self.assertIsNone(self.controller.reconnect_task)

    async def test_loopback_exists_only_while_audio_plays_and_follows_node_changes(self):
        pactl = FakePactl()
        controller = self.controller
        controller.cached = dict(speaker=SPEAKER, sink='bluez_output.sony', sink_index=10)
        with mock.patch('hub.command', pactl):
            await controller.route_audio()
            self.assertEqual(pactl.count('load-module'), 0)
            controller.activity.touch(time.monotonic())
            await controller.route_audio()
            self.assertEqual(pactl.count('load-module'), 1)
            await controller.route_audio()
            self.assertEqual(pactl.count('load-module'), 1)
            controller.cached['sink_index'] = 11
            await controller.route_audio()
            self.assertEqual((pactl.count('unload-module'), pactl.count('load-module')), (1, 2))
            controller.activity.last_active -= 1000
            await controller.route_audio()
            self.assertEqual((pactl.count('unload-module'), pactl.count('load-module')), (2, 2))
            self.assertIsNone(controller.route)

    async def test_loopback_left_by_a_previous_run_is_removed_when_nothing_plays(self):
        pactl = FakePactl(loaded=True)
        self.controller.cached = dict(speaker=SPEAKER, sink='bluez_output.sony', sink_index=10)
        with mock.patch('hub.command', pactl):
            await self.controller.route_audio()
        self.assertFalse(pactl.loaded)

    async def test_source_follows_whatever_starts_playing_when_selected_one_is_silent(self):
        pactl = FakePactl(sinks='37\taudiohub_network\tPipeWire\tfloat32le 2ch 48000Hz\tIDLE\n'
                                '41\taudiohub_airplay\tPipeWire\tfloat32le 2ch 44100Hz\tRUNNING\n')
        self.controller.cached = dict(speaker=SPEAKER, sink='bluez_output.sony', sink_index=10)
        with mock.patch('hub.command', pactl):
            await self.controller.refresh_activity()
            await self.controller.route_audio()
        self.assertEqual(self.controller.store.data['source'], 'airplay')
        self.assertEqual(self.controller.route[0], 'airplay')
        self.assertIn('AirPlay', self.controller.last_action)

    async def test_unicast_probe_accepts_only_a_protocol_reply(self):
        class ProbeServer(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport
            def datagram_received(self, data, address):
                self.transport.sendto(b'UNICAST' if data == b'MODE_PROBE' else b'wrong', address)
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(ProbeServer, local_addr=('127.0.0.1', 0))
        try:
            self.assertTrue(await sender_available('127.0.0.1', transport.get_extra_info('sockname')[1]))
        finally:
            transport.close()


if __name__ == '__main__':
    unittest.main()

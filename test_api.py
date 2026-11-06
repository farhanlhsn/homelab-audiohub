import tempfile
import unittest
from pathlib import Path
from aiohttp.test_utils import TestClient, TestServer
import asyncio
from hub import Controller, make_app, sender_available


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

    async def test_unknown_operation_is_rejected(self):
        response = await self.client.post('/api/speaker/reboot', json={}, headers=self.headers())
        self.assertEqual(response.status, 400)

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

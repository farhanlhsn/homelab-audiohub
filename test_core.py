import json
import tempfile
import unittest
from pathlib import Path

from core import ConfigStore, clean_ip, clean_mac, should_reconnect, loopback_ids


class CoreTests(unittest.TestCase):
    def test_loopback_ids_use_real_pactl_short_output(self):
        output = '536870912\tmodule-null-sink\tsink_name=audiohub_network\t\n536870925\tmodule-loopback\tsource=audiohub_network.monitor sink=bluez_output.SONY\t\n536870926\tmodule-loopback\tsource=other.monitor sink=other\t\n'
        self.assertEqual(loopback_ids(output), [536870925])

    def test_sender_must_be_a_host_on_home_lan(self):
        self.assertEqual(clean_ip('192.168.8.249'), '192.168.8.249')
        for value in ['127.0.0.1', '8.8.8.8', '192.168.8.0', '192.168.8.255',
                      '192.168.8.240', '192.168.8.249; reboot', 'evil.example', True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                clean_ip(value)

    def test_bluetooth_address_cannot_contain_arguments(self):
        self.assertEqual(clean_mac('74:68:59:68:28:17'), '74:68:59:68:28:17')
        for value in ['74:68:59:68:28:17\nquit', '--help', 'unknown']:
            with self.assertRaises(ValueError):
                clean_mac(value)

    def test_failed_update_keeps_previous_file_and_config(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(Path(d) / 'settings.json')
            store.update({'volume': 25, 'buffer_ms': 80})
            before = store.path.read_bytes()
            for patch in [{'volume': 101}, {'buffer_ms': -1}, {'quantum': 1},
                          {'volume': True}, {'source': 'shell'}, {'arbitrary': 1},
                          {'sender_ip': '10.0.0.1'}, {'auto_reconnect': 'true'}]:
                with self.subTest(patch=patch), self.assertRaises(ValueError):
                    store.update(patch)
                self.assertEqual(store.path.read_bytes(), before)
                self.assertEqual(store.data['volume'], 25)

    def test_network_cannot_be_enabled_without_a_sender(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(Path(d) / 'settings.json')
            with self.assertRaises(ValueError):
                store.update({'network_enabled': True})
            self.assertFalse(store.path.exists())

    def test_settings_survive_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            store = ConfigStore(path)
            store.update({'source': 'spotify', 'speaker_enabled': False, 'buffer_ms': 120})
            restored = ConfigStore(path)
            self.assertEqual(restored.data['source'], 'spotify')
            self.assertFalse(restored.data['speaker_enabled'])
            self.assertEqual(restored.data['buffer_ms'], 120)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_manual_disconnect_overrides_automatic_reconnect(self):
        self.assertTrue(should_reconnect({'speaker_enabled': True, 'auto_reconnect': True}, False))
        self.assertFalse(should_reconnect({'speaker_enabled': False, 'auto_reconnect': True}, False))
        self.assertFalse(should_reconnect({'speaker_enabled': True, 'auto_reconnect': False}, False))
        self.assertFalse(should_reconnect({'speaker_enabled': True, 'auto_reconnect': True}, True))


if __name__ == '__main__':
    unittest.main()

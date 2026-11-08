import base64
import html
import json
import tempfile
import unittest
from pathlib import Path

from core import (ActivityTracker, ConfigStore, agent_reply, apply_airplay_item, bluetooth_streams_to_move,
                  bluez_track, clean_ip, clean_mac, connection_list, didl_track, lan_address, latency_report,
                  loopback_ids, loopback_target, next_retry_delay, parse_shairport_metadata, peer_addresses, pick_source,
                  should_reconnect, sink_indexes, sink_states, sources_playing, transport_delays, transport_state)

SS = ('Recv-Q Send-Q                    Local Address:Port                     Peer Address:Port Process\n'
      '0      0      [2001:db8::240]:7000 [2001:db8::243]:49466\n'
      '0      0                              192.168.8.240:7000                   192.168.8.249:51022\n'
      '0      0      [2001:db8::240]:7000 [2001:db8::243]:49470\n')
NEIGH = ('192.168.8.243 dev enx00e04c398dc8 lladdr 02:00:00:00:00:43 REACHABLE\n'
         '192.168.8.249 dev enx00e04c398dc8 lladdr 02:00:00:00:00:49 REACHABLE\n'
         '2001:db8::243 dev enx00e04c398dc8 lladdr 02:00:00:00:00:43 STALE\n'
         'fe80::1 dev enx00e04c398dc8 lladdr aa:bb:cc:dd:ee:ff router STALE\n')


def item(kind, code, text=None):
    """One item as shairport-sync writes it to its metadata pipe."""
    data = b'' if text is None else text.encode()
    body = '<item><type>%s</type><code>%s</code><length>%d</length>\n' % (kind.encode().hex(), code.encode().hex(), len(data))
    if text is not None:
        body += '<data encoding="base64">\n%s</data>' % base64.b64encode(data).decode()
    return (body + '</item>\n').encode()

INDEXES = {'audiohub_network': 37, 'audiohub_spotify': 39, 'audiohub_airplay': 41, 'audiohub_bluetooth': 45, 'audiohub_dlna': 46}


SONY_DEV = '/org/bluez/hci0/dev_74_68_59_68_28_17'
SETTINGS = {'buffer_ms': 80, 'quantum': 1024}


class LatencyTests(unittest.TestCase):
    def test_transport_delays_are_split_between_the_speaker_and_the_phone_and_read_in_tenths_of_ms(self):
        objects = {SONY_DEV + '/sep1/fd0': {'org.bluez.MediaTransport1': {'Delay': 1400}},
                   '/org/bluez/hci0/dev_CC_08_FA_AF_17_BE/sep2/fd1': {'org.bluez.MediaTransport1': {'Delay': 3575}},
                   SONY_DEV: {'org.bluez.Device1': {}}}
        self.assertEqual(transport_delays(objects, SONY_DEV), (140.0, 357.5))
        self.assertEqual(transport_delays({SONY_DEV + '/sep1/fd0': {'org.bluez.MediaTransport1': {'State': 'idle'}}}, SONY_DEV), (None, None))
        self.assertEqual(transport_delays({}, SONY_DEV), (None, None))

    def test_speaker_leg_adds_the_reported_a2dp_delay_to_what_the_hub_itself_adds(self):
        report = latency_report('network', SETTINGS, 140.0, True, None)
        self.assertEqual(report['output']['ms'], 181)  # 140 + 1024/48000 s (21) + 20 loopback
        self.assertEqual([part['ms'] for part in report['output']['parts']], [140, 21, 20])
        self.assertEqual(report['output']['parts'][0]['note'], 'dilaporkan Sony')

    def test_last_known_speaker_delay_is_labelled_as_such(self):
        report = latency_report('network', SETTINGS, 140.0, False, None)
        self.assertEqual(report['output']['parts'][0]['note'], 'terakhir dilaporkan Sony')

    def test_without_any_speaker_delay_the_speaker_leg_and_total_are_unknown(self):
        report = latency_report('network', SETTINGS, None, False, None)
        self.assertIsNone(report['output']['ms'])
        self.assertIsNone(report['output']['parts'][0]['ms'])
        self.assertIsNone(report['total_ms'])

    def test_wfas_input_is_its_configured_buffer_and_the_total_is_the_sum(self):
        report = latency_report('network', SETTINGS, 140.0, True, None)
        self.assertEqual((report['input']['ms'], report['total_ms']), (80, 261))
        self.assertIn('pengaturan', report['input']['note'])

    def test_airplay_input_is_the_protocol_figure_set_by_the_sender(self):
        report = latency_report('airplay', SETTINGS, 140.0, True, None)
        self.assertEqual((report['input']['ms'], report['total_ms']), (2000, 2181))
        self.assertIn('pengirim', report['input']['note'])
        self.assertIn('instan', report['input']['note'])

    def test_airplay_is_an_upper_bound_because_the_sender_decides_how_much_it_buffers(self):
        report = latency_report('airplay', SETTINGS, 140.0, True, None)
        self.assertTrue(report['input']['upper_bound'])
        self.assertTrue(report['total_upper_bound'])
        self.assertTrue(report['lines']['airplay']['upper_bound'])

    def test_only_airplay_is_reported_as_an_upper_bound(self):
        report = latency_report('network', SETTINGS, 140.0, True, 357.5)
        self.assertEqual({source: line['upper_bound'] for source, line in report['lines'].items()},
                         {'bluetooth': False, 'airplay': True, 'dlna': False, 'network': False, 'spotify': False})
        self.assertFalse(report['input']['upper_bound'])
        self.assertFalse(report['total_upper_bound'])

    def test_bluetooth_input_is_the_delay_the_hub_reports_to_the_phone(self):
        report = latency_report('bluetooth', SETTINGS, 140.0, True, 357.5)
        self.assertEqual(report['input']['ms'], 358)
        self.assertEqual(report['total_ms'], 539)
        self.assertIsNone(latency_report('bluetooth', SETTINGS, 140.0, True, None)['input']['ms'])

    def test_sources_without_a_steady_figure_show_no_input_and_no_total(self):
        for source in ('dlna', 'spotify'):
            with self.subTest(source=source):
                report = latency_report(source, SETTINGS, 140.0, True, None)
                self.assertIsNone(report['input']['ms'])
                self.assertIsNone(report['total_ms'])
                self.assertEqual(report['output']['ms'], 181)

    def test_every_line_reports_its_own_input_figure_and_where_it_comes_from(self):
        report = latency_report('network', SETTINGS, 140.0, True, 357.5)
        self.assertEqual({source: (line['ms'], line['kind']) for source, line in report['lines'].items()},
                         {'bluetooth': (358, 'reported'), 'airplay': (2000, 'protocol'), 'dlna': (None, None),
                          'network': (80, 'setting'), 'spotify': (None, None)})
        self.assertEqual(report['input'], {'ms': 80, 'note': report['lines']['network']['note'], 'upper_bound': False})

    def test_the_speaker_legs_parts_say_whether_they_were_reported_or_set(self):
        parts = latency_report('network', SETTINGS, 140.0, True, None)['output']['parts']
        self.assertEqual([part['kind'] for part in parts], ['reported', 'setting', 'setting'])

    def test_a_larger_quantum_lengthens_the_hub_leg(self):
        self.assertEqual(latency_report('network', {'buffer_ms': 80, 'quantum': 2048}, 140.0, True, None)['output']['ms'], 203)


class ConnectionTests(unittest.TestCase):
    def test_peers_are_read_from_ss_output_once_each(self):
        self.assertEqual(peer_addresses(SS), ['2001:db8::243', '192.168.8.249'])
        self.assertEqual(peer_addresses(''), [])
        self.assertEqual(peer_addresses('Recv-Q Send-Q Local Address:Port Peer Address:Port\n'), [])

    def test_an_ipv6_peer_is_shown_by_the_ipv4_address_of_the_same_device(self):
        self.assertEqual(lan_address('2001:db8::243', NEIGH), '192.168.8.243')
        self.assertEqual(lan_address('192.168.8.249', NEIGH), '192.168.8.249')
        self.assertEqual(lan_address('2001:db8::dead', NEIGH), '2001:db8::dead')
        self.assertEqual(lan_address('fe80::1', NEIGH), 'fe80::1')

    def test_connections_list_every_connected_device_with_its_connection_type(self):
        states = {'audiohub_bluetooth': 'RUNNING', 'audiohub_airplay': 'IDLE', 'audiohub_dlna': 'IDLE', 'audiohub_network': 'IDLE'}
        devices = [dict(name='Xiaomi', connected=True), dict(name='Mac', connected=False)]
        rows = connection_list(devices, ['192.168.8.243'], '', '192.168.8.245', 'PC Kantor', states)
        self.assertEqual(rows, [dict(name='Xiaomi', type='Bluetooth', source='bluetooth', playing=True),
                                dict(name='192.168.8.243', type='AirPlay', source='airplay', playing=False),
                                dict(name='PC Kantor', type='Wi-Fi (WFAS)', source='network', playing=False)])

    def test_airplay_client_name_is_used_only_when_it_is_unambiguous(self):
        self.assertEqual(connection_list([], ['192.168.8.243'], "Farhan's Mac", '', '', {})[0]['name'], "Farhan's Mac")
        names = [row['name'] for row in connection_list([], ['192.168.8.243', '192.168.8.249'], "Farhan's Mac", '', '', {})]
        self.assertEqual(names, ['192.168.8.243', '192.168.8.249'])

    def test_dlna_appears_only_while_it_plays_and_nothing_connected_gives_an_empty_list(self):
        self.assertEqual(connection_list([], [], '', '', '', {'audiohub_dlna': 'RUNNING'}),
                         [dict(name='Pemutar DLNA', type='DLNA', source='dlna', playing=True)])
        self.assertEqual(connection_list([], [], '', '', '', {}), [])

    def test_wfas_unnamed_sender_falls_back_to_its_address(self):
        self.assertEqual(connection_list([], [], '', '192.168.8.245', '', {})[0]['name'], '192.168.8.245')

    def test_eq_is_on_by_default_survives_restart_and_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(Path(d) / 'settings.json')
            self.assertTrue(store.data['eq_enabled'])
            store.update({'eq_enabled': False})
            self.assertFalse(ConfigStore(Path(d) / 'settings.json').data['eq_enabled'])
            with self.assertRaises(ValueError):
                store.update({'eq_enabled': 'yes'})

    def test_loopback_target_is_the_eq_sink_only_when_enabled_and_present(self):
        self.assertEqual(loopback_target('bluez_output.sony', True, True), 'audiohub_eq')
        self.assertEqual(loopback_target('bluez_output.sony', False, True), 'bluez_output.sony')
        self.assertEqual(loopback_target('bluez_output.sony', True, False), 'bluez_output.sony')
        self.assertIsNone(loopback_target(None, True, True))

    def test_auto_source_is_on_by_default_survives_restart_and_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(Path(d) / 'settings.json')
            self.assertTrue(store.data['auto_source'])
            store.update({'auto_source': False})
            self.assertFalse(ConfigStore(Path(d) / 'settings.json').data['auto_source'])
            with self.assertRaises(ValueError):
                store.update({'auto_source': 'yes'})


class NowPlayingTests(unittest.TestCase):
    def test_metadata_pipe_items_are_parsed_even_when_a_read_ends_mid_item(self):
        stream = item('core', 'minm', 'Lagu') + item('core', 'asar', 'Artis') + item('ssnc', 'pend')
        items, rest = parse_shairport_metadata(stream[:50])
        self.assertEqual(items, [])
        items, rest = parse_shairport_metadata(rest + stream[50:])
        self.assertEqual(items, [('core', 'minm', b'Lagu'), ('core', 'asar', b'Artis'), ('ssnc', 'pend', b'')])
        self.assertEqual(rest.strip(), b'')

    def test_metadata_pipe_garbage_is_survived_and_the_buffer_stays_bounded(self):
        items, rest = parse_shairport_metadata(b'noise <item>not closed ' + b'x' * 300000)
        self.assertEqual(items, [])
        self.assertLessEqual(len(rest), 65536)
        items, _ = parse_shairport_metadata(b'<item><type>zz</type><code>1</code><length>1</length></item>' + item('core', 'minm', 'Ok'))
        self.assertEqual(items, [('core', 'minm', b'Ok')])

    def test_airplay_title_artist_album_follow_the_track_and_clear_when_playback_ends(self):
        now = {}
        for kind, code, data in [('ssnc', 'mdst', b''), ('core', 'minm', 'Lagu — Baru'.encode()),
                                 ('core', 'asar', b'Artis'), ('core', 'asal', b'Album')]:
            now = apply_airplay_item(now, kind, code, data)
        self.assertEqual(now, {'title': 'Lagu — Baru', 'artist': 'Artis', 'album': 'Album'})
        now = apply_airplay_item(now, 'ssnc', 'mdst', b'')
        now = apply_airplay_item(now, 'core', 'minm', b'Lagu Lain')
        self.assertEqual(now, {'title': 'Lagu Lain'})
        self.assertEqual(apply_airplay_item(now, 'ssnc', 'pend', b''), {})
        self.assertEqual(apply_airplay_item(now, 'core', 'asgn', b'Genre'), now)

    def test_metadata_text_is_trimmed_and_survives_invalid_bytes(self):
        now = apply_airplay_item({}, 'core', 'minm', b'a' * 500)
        self.assertEqual(len(now['title']), 120)
        self.assertEqual(apply_airplay_item({}, 'core', 'asar', b'\xff\xfeX')['artist'].endswith('X'), True)

    def test_dlna_track_is_read_from_the_didl_inside_the_soap_reply(self):
        didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"><item id="1"><dc:title>Lagu &amp; Co</dc:title>'
                '<upnp:artist role="AlbumArtist">Artis</upnp:artist><upnp:album>Album</upnp:album></item></DIDL-Lite>')
        reply = '<s:Envelope><s:Body><u:GetPositionInfoResponse><TrackMetaData>%s</TrackMetaData></u:GetPositionInfoResponse></s:Body></s:Envelope>' % html.escape(didl)
        self.assertEqual(didl_track(reply), {'title': 'Lagu & Co', 'artist': 'Artis', 'album': 'Album'})

    def test_dlna_track_is_none_without_usable_metadata(self):
        for reply in ['', '<TrackMetaData>NOT_IMPLEMENTED</TrackMetaData>', '<TrackMetaData></TrackMetaData>',
                      '<TrackMetaData>&lt;DIDL-Lite&gt;&lt;item&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;</TrackMetaData>', 'garbage']:
            with self.subTest(reply=reply):
                self.assertIsNone(didl_track(reply))

    def test_bluetooth_track_comes_from_the_avrcp_player_only_while_it_is_not_stopped(self):
        track = {'Title': 'Lagu', 'Artist': 'Artis', 'Album': 'Album', 'Duration': '1000'}
        self.assertEqual(bluez_track({'Status': 'playing', 'Track': track}), {'title': 'Lagu', 'artist': 'Artis', 'album': 'Album'})
        self.assertEqual(bluez_track({'Status': 'paused', 'Track': {'Title': 'Lagu'}}), {'title': 'Lagu'})
        self.assertIsNone(bluez_track({'Status': 'stopped', 'Track': track}))
        self.assertIsNone(bluez_track({'Status': 'playing', 'Track': {}}))
        self.assertIsNone(bluez_track({}))


class CoreTests(unittest.TestCase):
    def test_pairing_agent_confirms_prompts_and_trusts_new_devices(self):
        self.assertEqual(agent_reply('[agent] Confirm passkey 123456 (yes/no): '), 'yes')
        self.assertEqual(agent_reply('\x1b[0;94m[agent]\x1b[0m Authorize service 0000110d (yes/no):'), 'yes')
        self.assertEqual(agent_reply('[CHG] Device AA:BB:CC:DD:EE:01 Paired: yes'), 'trust AA:BB:CC:DD:EE:01')
        self.assertEqual(agent_reply('\x1b[0;93m[CHG]\x1b[0m Device AA:BB:CC:DD:EE:01 Paired: yes\r'), 'trust AA:BB:CC:DD:EE:01')

    def test_pairing_agent_ignores_the_speaker_and_unrelated_output(self):
        self.assertIsNone(agent_reply('[CHG] Device 74:68:59:68:28:17 Paired: yes'))
        self.assertIsNone(agent_reply('[CHG] Device AA:BB:CC:DD:EE:01 Connected: yes'))
        self.assertIsNone(agent_reply('[CHG] Device AA:BB:CC:DD:EE:01 Paired: yes; rm -rf /'))
        self.assertIsNone(agent_reply(''))

    def test_phone_streams_are_moved_to_the_bluetooth_sink_once(self):
        # Real shape: WirePlumber autoconnects the phone's A2DP stream to the default sink.
        inputs = [{'index': 7001, 'sink': 41, 'properties': {'node.name': 'AudioHub AirPlay'}},
                  {'index': 8168, 'sink': 37, 'properties': {'node.name': 'bluez_input.CC_08.2', 'api.bluez5.profile': 'a2dp-source'}},
                  {'index': 8170, 'sink': 45, 'properties': {'node.name': 'bluez_input.AA_BB.2', 'api.bluez5.profile': 'a2dp-source'}},
                  {'index': 8181, 'sink': 2806, 'properties': {'node.name': 'output.loopback-1'}}]
        self.assertEqual(bluetooth_streams_to_move(inputs, 45), [8168])
        self.assertEqual(bluetooth_streams_to_move([], 45), [])

    def test_sink_indexes_use_real_pactl_short_output(self):
        output = '37\taudiohub_network\tPipeWire\tfloat32le 2ch 48000Hz\tIDLE\n45\taudiohub_bluetooth\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING\n'
        self.assertEqual(sink_indexes(output), {'audiohub_network': 37, 'audiohub_bluetooth': 45})

    def test_bluetooth_is_a_valid_source_and_followed_when_it_plays(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(Path(d) / 'settings.json')
            self.assertEqual(store.update({'source': 'bluetooth'})['source'], 'bluetooth')
        self.assertEqual(pick_source('network', {'audiohub_bluetooth': 'RUNNING'}), 'bluetooth')

    def test_activity_comes_from_streams_not_from_the_sink_state(self):
        # A corked shairport stream stays open and an idle renderer keeps its stream; a sink
        # whose monitor is being read reports RUNNING for both, so only the streams tell the truth.
        inputs = [{'index': 1, 'sink': 41, 'corked': True}, {'index': 2, 'sink': 46, 'corked': False},
                  {'index': 3, 'sink': 45, 'corked': False}, {'index': 4, 'sink': 2806, 'corked': False}]
        states = sources_playing(inputs, INDEXES, 'STOPPED')
        self.assertEqual(states['audiohub_airplay'], 'IDLE')
        self.assertEqual(states['audiohub_dlna'], 'IDLE')
        self.assertEqual(states['audiohub_bluetooth'], 'RUNNING')
        self.assertEqual(states['audiohub_network'], 'IDLE')
        self.assertEqual(sources_playing(inputs, INDEXES, 'PLAYING')['audiohub_dlna'], 'RUNNING')
        self.assertEqual(sources_playing(inputs, INDEXES, 'PAUSED_PLAYBACK')['audiohub_dlna'], 'IDLE')
        self.assertEqual(sources_playing(inputs, INDEXES, None)['audiohub_dlna'], 'IDLE')

    def test_transport_state_is_read_from_the_renderer_reply(self):
        reply = ('<s:Envelope><s:Body><u:GetTransportInfoResponse><CurrentTransportState>PLAYING</CurrentTransportState>'
                 '<CurrentTransportStatus>OK</CurrentTransportStatus></u:GetTransportInfoResponse></s:Body></s:Envelope>')
        self.assertEqual(transport_state(reply), 'PLAYING')
        self.assertIsNone(transport_state('<html>not upnp</html>'))
        self.assertIsNone(transport_state(''))

    def test_a_sink_that_does_not_exist_yet_is_idle(self):
        states = sources_playing([{'index': 1, 'sink': 41, 'corked': False}], {'audiohub_airplay': 41}, None)
        self.assertEqual(states['audiohub_airplay'], 'RUNNING')
        self.assertEqual(states['audiohub_dlna'], 'IDLE')

    def test_dlna_is_a_valid_source_and_followed_when_it_plays(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(Path(d) / 'settings.json')
            self.assertEqual(store.update({'source': 'dlna'})['source'], 'dlna')
        self.assertEqual(pick_source('network', {'audiohub_dlna': 'RUNNING'}), 'dlna')
        self.assertEqual(pick_source('dlna', {'audiohub_dlna': 'RUNNING', 'audiohub_airplay': 'RUNNING'}), 'dlna')

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

    def test_sink_states_use_real_pactl_short_output(self):
        output = ('37\taudiohub_network\tPipeWire\tfloat32le 2ch 48000Hz\tIDLE\n'
                  '41\taudiohub_airplay\tPipeWire\tfloat32le 2ch 44100Hz\tRUNNING\n'
                  'garbage line\n')
        self.assertEqual(sink_states(output), {'audiohub_network': 'IDLE', 'audiohub_airplay': 'RUNNING'})

    def test_reconnect_backs_off_and_stops_growing(self):
        delays = [next_retry_delay(n) for n in range(7)]
        self.assertEqual(delays, [20, 40, 80, 160, 300, 300, 300])

    def test_speaker_link_stays_up_only_briefly_after_audio_stops(self):
        tracker = ActivityTracker(hold=90)
        self.assertFalse(tracker.active(0))
        tracker.touch(100)
        self.assertTrue(tracker.active(189))
        self.assertFalse(tracker.active(190))

    def test_selected_source_wins_while_it_is_playing(self):
        states = {'audiohub_network': 'RUNNING', 'audiohub_airplay': 'RUNNING'}
        self.assertEqual(pick_source('network', states), 'network')

    def test_playing_source_is_followed_when_selected_one_is_silent(self):
        states = {'audiohub_network': 'IDLE', 'audiohub_airplay': 'RUNNING', 'audiohub_spotify': 'SUSPENDED'}
        self.assertEqual(pick_source('network', states), 'airplay')

    def test_a_source_that_starts_playing_takes_over_from_one_that_was_already_running(self):
        # An AirPlay session can stay open and silent for hours; the newcomer must still be heard.
        before = {'audiohub_airplay': 'RUNNING', 'audiohub_dlna': 'IDLE'}
        after = {'audiohub_airplay': 'RUNNING', 'audiohub_dlna': 'RUNNING'}
        self.assertEqual(pick_source('airplay', after, before), 'dlna')
        self.assertEqual(pick_source('dlna', after, after), 'dlna')
        self.assertEqual(pick_source('airplay', after, after), 'airplay')

    def test_nothing_is_taken_over_without_a_previous_reading(self):
        after = {'audiohub_airplay': 'RUNNING', 'audiohub_dlna': 'RUNNING'}
        self.assertEqual(pick_source('airplay', after, None), 'airplay')

    def test_the_selected_source_is_kept_when_it_is_among_those_that_just_started(self):
        before = {'audiohub_airplay': 'IDLE', 'audiohub_dlna': 'IDLE'}
        after = {'audiohub_airplay': 'RUNNING', 'audiohub_dlna': 'RUNNING'}
        self.assertEqual(pick_source('dlna', after, before), 'dlna')
        self.assertEqual(pick_source('network', after, before), 'airplay')  # first in SOURCES order

    def test_selection_is_kept_when_nothing_is_playing(self):
        self.assertEqual(pick_source('spotify', {'audiohub_airplay': 'IDLE'}), 'spotify')


if __name__ == '__main__':
    unittest.main()

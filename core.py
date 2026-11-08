"""Validated, persistent operator settings for one homelab audio bridge."""
import base64
import binascii
import html
import ipaddress
import json
import os
import re
from pathlib import Path

SONY = '74:68:59:68:28:17'
SOURCES = ('bluetooth', 'airplay', 'dlna', 'spotify', 'network')
DEFAULTS = dict(source='network', sender_ip='', buffer_ms=80, quantum=1024,
                volume=30, auto_reconnect=True, speaker_enabled=True,
                network_enabled=False, auto_source=True, eq_enabled=True)


def clean_ip(value):
    if not isinstance(value, str):
        raise ValueError('Alamat perangkat harus berupa IP LAN.')
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValueError('Masukkan IP perangkat, misalnya 192.168.8.249.') from None
    if address not in ipaddress.IPv4Network('192.168.8.0/24') or str(address) in {
        '192.168.8.0', '192.168.8.1', '192.168.8.240', '192.168.8.241', '192.168.8.255'
    }:
        raise ValueError('Pilih IP perangkat pengirim di jaringan 192.168.8.x.')
    return str(address)


def clean_mac(value):
    if not isinstance(value, str) or re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', value) is None:
        raise ValueError('Alamat Bluetooth tidak valid.')
    return value.upper()


def should_reconnect(settings, connected):
    return bool(settings['speaker_enabled'] and settings['auto_reconnect'] and not connected)


def next_retry_delay(failures):
    """Seconds to wait before the next Bluetooth connect attempt."""
    return min(20 * 2 ** failures, 300)


def sink_states(text):
    """Map sink name to state from `pactl list short sinks`."""
    states = {}
    for line in text.splitlines():
        fields = line.split('\t')
        if len(fields) >= 5:
            states[fields[1]] = fields[4]
    return states


def sink_indexes(text):
    """Map sink name to its index from `pactl list short sinks`."""
    indexes = {}
    for line in text.splitlines():
        fields = line.split('\t')
        if len(fields) >= 2 and fields[0].isdigit():
            indexes[fields[1]] = int(fields[0])
    return indexes


def sources_playing(sink_inputs, indexes, dlna_state):
    """RUNNING/IDLE per source, from the streams rather than the sink state: reading a sink's monitor
    keeps it RUNNING even when its only stream is corked (shairport) or the renderer sits idle with
    its stream open (gmediarender), which would pin the speaker awake and block every other source.
    DLNA additionally needs the renderer's own transport state to be PLAYING."""
    states = {}
    for source in SOURCES:
        name = 'audiohub_' + source
        index = indexes.get(name)
        playing = index is not None and any(item.get('sink') == index and not item.get('corked') for item in sink_inputs)
        if source == 'dlna':
            playing = playing and dlna_state == 'PLAYING'
        states[name] = 'RUNNING' if playing else 'IDLE'
    return states


LOOPBACK_MS = 20
SAMPLE_RATE = 48000


def transport_delays(objects, speaker_path):
    """A2DP transport delays in ms from BlueZ's managed objects: (speaker we send to, phone we receive from).
    BlueZ reports the delay in tenths of a millisecond; either is None when no transport is acquired."""
    speaker = phone = None
    for path, interfaces in objects.items():
        transport = interfaces.get('org.bluez.MediaTransport1')
        if transport is None or 'Delay' not in transport:
            continue
        if str(path).startswith(speaker_path + '/'):
            speaker = int(transport['Delay']) / 10
        else:
            phone = int(transport['Delay']) / 10
    return speaker, phone


def latency_report(source, settings, speaker_delay_ms, speaker_live, phone_delay_ms):
    """Estimated latency per leg. Every figure carries a note saying where it comes from, because only
    the speaker's A2DP delay is reported by a device; the rest are hub settings or protocol figures,
    and the true end-to-end latency (it ends in the speaker) cannot be measured from the hub."""
    quantum_ms = settings['quantum'] / SAMPLE_RATE * 1000
    known = speaker_delay_ms is not None
    parts = [dict(name='Bluetooth ke Sony', ms=round(speaker_delay_ms) if known else None, kind='reported',
                  note='dilaporkan Sony' if speaker_live else 'terakhir dilaporkan Sony'),
             dict(name='PipeWire', ms=round(quantum_ms), kind='setting', note='pengaturan hub'),
             dict(name='Loopback', ms=LOOPBACK_MS, kind='setting', note='pengaturan hub')]
    output = dict(ms=round(speaker_delay_ms + quantum_ms + LOOPBACK_MS) if known else None, parts=parts)
    bluetooth_ms = round(phone_delay_ms) if phone_delay_ms is not None else None
    lines = {
        'network': dict(ms=settings['buffer_ms'], kind='setting', note='buffer WFAS (pengaturan)'),
        'airplay': dict(ms=2000, kind='protocol', upper_bound=True,
                        note='ditetapkan pengirim: instan dari Safari, ±2 detik dari Control Center; AirPlay 2 bisa berbeda'),
        'bluetooth': dict(ms=bluetooth_ms, kind='reported' if bluetooth_ms is not None else None,
                          note='dilaporkan hub ke perangkat; belum termasuk buffer pengirim'),
        'dlna': dict(ms=None, kind=None, note='tidak tetap; buffer 2 detik hanya di awal pemutaran'),
        'spotify': dict(ms=None, kind=None, note='tidak tersedia'),
    }
    for line in lines.values():
        line.setdefault('upper_bound', False)
    chosen = lines.get(source, dict(ms=None, kind=None, note='tidak tersedia', upper_bound=False))
    total = chosen['ms'] + output['ms'] if chosen['ms'] is not None and output['ms'] is not None else None
    return dict(source=source, input=dict(ms=chosen['ms'], note=chosen['note'], upper_bound=chosen['upper_bound']),
                lines=lines, output=output, total_ms=total, total_upper_bound=chosen['upper_bound'] and total is not None)


def peer_addresses(text):
    """Distinct remote addresses from `ss -tn state established` output (Peer Address:Port column)."""
    peers = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[0].isdigit():
            peer = fields[3].rsplit(':', 1)[0].strip('[]')
            if peer and peer not in peers:
                peers.append(peer)
    return peers


def lan_address(peer, neighbors):
    """An IPv6 peer is shown by the IPv4 address of the same device, matched by MAC in `ip neigh` output."""
    if ':' not in peer:
        return peer
    macs = {}
    for line in neighbors.splitlines():
        fields = line.split()
        if len(fields) >= 5 and 'lladdr' in fields:
            macs[fields[0]] = fields[fields.index('lladdr') + 1].lower()
    mac = macs.get(peer)
    if mac:
        for address, other in macs.items():
            if other == mac and address.count('.') == 3:
                return address
    return peer


def connection_list(bt_devices, airplay_peers, airplay_name, wfas_ip, wfas_name, states):
    """Connected devices with their connection type. DLNA has no persistent link, so it is listed while playing."""
    def playing(source):
        return states.get('audiohub_' + source) == 'RUNNING'
    rows = [dict(name=device['name'], type='Bluetooth', source='bluetooth', playing=playing('bluetooth'))
            for device in bt_devices if device.get('connected')]
    rows += [dict(name=airplay_name if airplay_name and len(airplay_peers) == 1 else peer, type='AirPlay', source='airplay', playing=playing('airplay'))
             for peer in airplay_peers]
    if playing('dlna'):
        rows.append(dict(name='Pemutar DLNA', type='DLNA', source='dlna', playing=True))
    if wfas_ip:
        rows.append(dict(name=wfas_name or wfas_ip, type='Wi-Fi (WFAS)', source='network', playing=playing('network')))
    return rows


MAX_TEXT = 120
MAX_PIPE_BUFFER = 65536
PIPE_ITEM = re.compile(r'<item><type>([0-9a-f]+)</type><code>([0-9a-f]+)</code><length>(\d+)</length>\s*'
                       r'(?:<data encoding="base64">\s*([^<]*)</data>)?\s*</item>')


def clean_text(value):
    """Track text comes from other devices: single line, bounded."""
    return ' '.join(str(value).split())[:MAX_TEXT]


def parse_shairport_metadata(buffer):
    """Complete items from shairport-sync's metadata pipe plus the unparsed rest (kept bounded)."""
    text = buffer.decode('latin1')
    items, end = [], 0
    for found in PIPE_ITEM.finditer(text):
        end = found.end()
        try:
            items.append((bytes.fromhex(found.group(1)).decode('latin1'), bytes.fromhex(found.group(2)).decode('latin1'),
                          base64.b64decode(found.group(4) or '')))
        except (ValueError, binascii.Error):
            continue
    return items, text[end:][-MAX_PIPE_BUFFER:].encode('latin1')


def apply_airplay_item(now, kind, code, data):
    """Fold one metadata item into the current track (title/artist/album)."""
    if kind == 'ssnc' and code in ('mdst', 'pend'):
        return {}
    field = {'minm': 'title', 'asar': 'artist', 'asal': 'album'}.get(code) if kind == 'core' else None
    text = clean_text(data.decode('utf-8', 'replace')) if field else ''
    return {**now, field: text} if text else now


def didl_track(xml):
    """Title/artist/album from the DIDL-Lite inside a UPnP GetPositionInfo reply, or None."""
    found = re.search(r'<TrackMetaData>([^<]*)</TrackMetaData>', xml)
    if not found:
        return None
    didl = html.unescape(found.group(1))

    def pick(tag):
        match = re.search(r'<%s(?:\s[^>]*)?>([^<]*)</%s>' % (tag, tag), didl)
        return clean_text(html.unescape(match.group(1))) if match else ''
    track = {key: value for key, value in (('title', pick('dc:title')), ('artist', pick('upnp:artist')),
                                           ('album', pick('upnp:album'))) if value}
    return track or None


def bluez_track(player):
    """Track from a BlueZ AVRCP MediaPlayer1 (Status + Track dict), or None when stopped/unknown."""
    if str(player.get('Status', '')).lower() == 'stopped':
        return None
    track = player.get('Track') or {}
    result = {key: clean_text(track.get(name, '')) for key, name in (('title', 'Title'), ('artist', 'Artist'), ('album', 'Album'))}
    result = {key: value for key, value in result.items() if value}
    return result or None


def transport_state(xml):
    """CurrentTransportState from a UPnP GetTransportInfo reply (PLAYING, STOPPED, ...)."""
    found = re.search(r'<CurrentTransportState>([A-Z_]+)</CurrentTransportState>', xml)
    return found.group(1) if found else None


def bluetooth_streams_to_move(sink_inputs, target):
    """A phone's A2DP stream arrives as a playback stream that WirePlumber autoconnects to the
    default sink; these are the ones not yet on our dedicated Bluetooth sink."""
    return [item['index'] for item in sink_inputs
            if item.get('properties', {}).get('api.bluez5.profile') == 'a2dp-source' and item.get('sink') != target]


ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|[\x01\x02\r]')
PAIRED = re.compile(r'Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2}) Paired: yes\s*$')


def agent_reply(line):
    """Answer for one line of bluetoothctl output while the pairing window is open."""
    line = ANSI.sub('', line).strip()
    if '(yes/no)' in line:
        return 'yes'
    found = PAIRED.search(line)
    if found and found.group(1) != SONY:
        return 'trust ' + found.group(1)
    return None


def pick_source(current, states, previous=None):
    """The source that just started playing wins; otherwise keep the selected one while it plays,
    else follow whichever one is playing. `previous` is the last reading (None on the first)."""
    if previous is not None:
        started = [source for source in SOURCES
                   if states.get('audiohub_' + source) == 'RUNNING' and previous.get('audiohub_' + source) != 'RUNNING']
        if started and current not in started:
            return started[0]
    if states.get('audiohub_' + current) == 'RUNNING':
        return current
    for source in SOURCES:
        if states.get('audiohub_' + source) == 'RUNNING':
            return source
    return current


class ActivityTracker:
    """Keeps the speaker link up for a while after the last audio."""

    def __init__(self, hold=90):
        self.hold = hold
        self.last_active = None

    def touch(self, now):
        self.last_active = now

    def active(self, now):
        return self.last_active is not None and now - self.last_active < self.hold


def loopback_target(sink, eq_enabled, eq_ready):
    """Where the loopback delivers: the EQ stage (which feeds the speaker) when it is enabled and loaded."""
    if sink is None:
        return None
    return 'audiohub_eq' if eq_enabled and eq_ready else sink


def loopback_ids(text):
    result = []
    for line in text.splitlines():
        fields = line.split('\t')
        if len(fields) >= 3 and fields[1] == 'module-loopback' and 'source=audiohub_' in fields[2]:
            result.append(int(fields[0]))
    return result


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path)
        self.data = DEFAULTS.copy()
        if self.path.exists():
            self.data = self.validate(json.loads(self.path.read_text()))

    def validate(self, patch):
        if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
            raise ValueError('Pengaturan tidak dikenal.')
        data = {**self.data, **patch}
        if data['source'] not in SOURCES:
            raise ValueError('Sumber audio tidak valid.')
        for key, low, high in [('volume', 0, 100), ('buffer_ms', 20, 250)]:
            if type(data[key]) is not int or not low <= data[key] <= high:
                raise ValueError(f'{key} harus berada antara {low} dan {high}.')
        if type(data['quantum']) is not int or data['quantum'] not in (256, 512, 1024, 2048):
            raise ValueError('Quantum harus 256, 512, 1024, atau 2048 sampel.')
        for key in ('auto_reconnect', 'speaker_enabled', 'network_enabled', 'auto_source', 'eq_enabled'):
            if type(data[key]) is not bool:
                raise ValueError(f'{key} harus berupa boolean.')
        if data['sender_ip'] != '':
            data['sender_ip'] = clean_ip(data['sender_ip'])
        elif data['network_enabled']:
            raise ValueError('Pilih atau masukkan IP perangkat pengirim terlebih dahulu.')
        return data

    def update(self, patch):
        data = self.validate(patch)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)
        self.data = data
        return dict(data)

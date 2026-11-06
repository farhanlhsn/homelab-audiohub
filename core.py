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
                network_enabled=False)


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
        for key in ('auto_reconnect', 'speaker_enabled', 'network_enabled'):
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

"""LAN control panel for the homelab's dedicated audio session."""
import asyncio
import contextlib
import hmac
import json
import logging
import math
import os
import secrets
import socket
import stat
import struct
import time
import wave
from pathlib import Path

from aiohttp import web, ClientError, ClientSession, ClientTimeout
from core import (ActivityTracker, ConfigStore, SONY, agent_reply, apply_airplay_item, bluetooth_streams_to_move,
                  bluez_track, clean_ip, clean_mac, clean_text, connection_list, didl_track, lan_address,
                  latency_report, loopback_ids, loopback_target, next_retry_delay, parse_shairport_metadata, peer_addresses,
                  pick_source, should_reconnect, sink_indexes, sink_states, sources_playing, transport_delays,
                  transport_state, LOOPBACK_MS, SOURCES)

SOURCE_NAMES = dict(network='audio perangkat', spotify='Spotify', airplay='AirPlay', bluetooth='Bluetooth', dlna='DLNA')
# Sinks created on demand (no engine restart); the DLNA unit also creates its own before it starts.
EXTRA_SINKS = dict(audiohub_bluetooth='AudioHub-Bluetooth', audiohub_dlna='AudioHub-DLNA')
DLNA_CONTROL = 'http://192.168.8.240:49494/upnp/control/rendertransport1'
AIRPLAY_METADATA_PIPE = '/run/audiohub/shairport-metadata'
A2DP_SOURCE_UUID = '0000110a-0000-1000-8000-00805f9b34fb'
ADAPTER_ALIAS = 'Homelab Audio'
PAIRING_SECONDS = 120
BASE = Path(__file__).resolve().parent
WFAS = '/opt/homelab-audiohub/vendor/wfas/bin/WiFi Audio Streaming'
log = logging.getLogger('audiohub')


async def command(*args, timeout=8, check=True):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.communicate()
        raise
    out = stdout.decode(errors='replace').strip()
    if check and process.returncode:
        raise RuntimeError((stderr.decode(errors='replace') or out or 'Perintah gagal.')[-600:])
    return out


async def sender_available(ip, port=9090):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await loop.sock_sendto(sock, b'MODE_PROBE', (ip, port))
        data, remote = await asyncio.wait_for(loop.sock_recvfrom(sock, 128), 1.2)
        return remote[0] == ip and data.strip() == b'UNICAST'
    except (asyncio.TimeoutError, OSError):
        return False
    finally:
        sock.close()


def bluetooth(action='status'):
    # Close every private bus connection; the existing root bus is already full.
    import dbus
    bus = dbus.SystemBus(private=True)
    try:
        path = '/org/bluez/hci0/dev_' + SONY.replace(':', '_')
        device = bus.get_object('org.bluez', path)
        props = dbus.Interface(device, 'org.freedesktop.DBus.Properties')
        if action == 'connect':
            props.Set('org.bluez.Device1', 'Trusted', dbus.Boolean(True))
            dbus.Interface(device, 'org.bluez.Device1').Connect(timeout=18)
        elif action == 'disconnect':
            dbus.Interface(device, 'org.bluez.Device1').Disconnect(timeout=10)
        data = props.GetAll('org.bluez.Device1')
        result = {'connected': bool(data.get('Connected')), 'paired': bool(data.get('Paired')), 'bonded': bool(data.get('Bonded')),
                  'trusted': bool(data.get('Trusted')), 'address': SONY, 'name': str(data.get('Name', 'ULT FIELD 1'))}
        managed = dbus.Interface(bus.get_object('org.bluez', '/'), 'org.freedesktop.DBus.ObjectManager').GetManagedObjects()
        for obj_path, interfaces in managed.items():
            if str(obj_path) == path and 'org.bluez.Battery1' in interfaces:
                result['battery'] = int(interfaces['org.bluez.Battery1']['Percentage'])
        result['delay_ms'], result['input_delay_ms'] = transport_delays(managed, path)
        return result
    finally:
        bus.close()


def pairable(value):
    import dbus
    bus = dbus.SystemBus(private=True)
    try:
        props = dbus.Interface(bus.get_object('org.bluez', '/org/bluez/hci0'), 'org.freedesktop.DBus.Properties')
        previous = bool(props.Get('org.bluez.Adapter1', 'Pairable'))
        props.Set('org.bluez.Adapter1', 'Pairable', dbus.Boolean(value))
        return previous
    finally:
        bus.close()


def bluetooth_devices():
    """Phones and PCs that can stream to us over A2DP (the Sony is an output, not an input)."""
    import dbus
    bus = dbus.SystemBus(private=True)
    try:
        managed = dbus.Interface(bus.get_object('org.bluez', '/'), 'org.freedesktop.DBus.ObjectManager').GetManagedObjects()
        devices = []
        for interfaces in managed.values():
            data = interfaces.get('org.bluez.Device1')
            if not data or str(data.get('Address')).upper() == SONY:
                continue
            if A2DP_SOURCE_UUID in [str(uuid) for uuid in data.get('UUIDs', [])] and (data.get('Paired') or data.get('Connected')):
                devices.append(dict(address=str(data['Address']).upper(), name=str(data.get('Alias') or data.get('Name') or data['Address'])[:60],
                                    connected=bool(data.get('Connected')), paired=bool(data.get('Paired')),
                                    trusted=bool(data.get('Trusted'))))
        return sorted(devices, key=lambda d: (not d['connected'], d['name'].lower()))
    finally:
        bus.close()


def bluetooth_trust(address):
    """Trusted devices are authorised for A2DP without an agent; otherwise bluez denies the audio profile."""
    import dbus
    address = clean_mac(address)
    if address == SONY:
        return
    bus = dbus.SystemBus(private=True)
    try:
        props = dbus.Interface(bus.get_object('org.bluez', '/org/bluez/hci0/dev_' + address.replace(':', '_')), 'org.freedesktop.DBus.Properties')
        props.Set('org.bluez.Device1', 'Trusted', dbus.Boolean(True))
    finally:
        bus.close()


def bluetooth_forget(address):
    address = clean_mac(address)
    if address == SONY:
        raise ValueError('Sony tidak dihapus dari sini.')
    import dbus
    bus = dbus.SystemBus(private=True)
    try:
        adapter = dbus.Interface(bus.get_object('org.bluez', '/org/bluez/hci0'), 'org.bluez.Adapter1')
        adapter.RemoveDevice(dbus.ObjectPath('/org/bluez/hci0/dev_' + address.replace(':', '_')))
    finally:
        bus.close()


def bluetooth_track():
    """Title/artist/album the connected phone or Mac reports over AVRCP (the Sony is not a source)."""
    import dbus
    bus = dbus.SystemBus(private=True)
    try:
        managed = dbus.Interface(bus.get_object('org.bluez', '/'), 'org.freedesktop.DBus.ObjectManager').GetManagedObjects()
        for path, interfaces in managed.items():
            player = interfaces.get('org.bluez.MediaPlayer1')
            if player is None or SONY.replace(':', '_') in str(path):
                continue
            track = bluez_track({'Status': str(player.get('Status', '')),
                                 'Track': {str(key): str(value) for key, value in dict(player.get('Track', {})).items()}})
            if track:
                return track
        return None
    finally:
        bus.close()


def bluetooth_alias(name):
    import dbus
    bus = dbus.SystemBus(private=True)
    try:
        props = dbus.Interface(bus.get_object('org.bluez', '/org/bluez/hci0'), 'org.freedesktop.DBus.Properties')
        if str(props.Get('org.bluez.Adapter1', 'Alias')) != name:
            props.Set('org.bluez.Adapter1', 'Alias', dbus.String(name))
    finally:
        bus.close()


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, controller):
        self.controller = controller

    def datagram_received(self, data, addr):
        try:
            ip = clean_ip(addr[0])
            parts = data[:2048].decode('utf-8').strip().split(';')
            if len(parts) < 4 or parts[0] != 'WIFI_AUDIO_STREAMER_DISCOVERY':
                return
            port = int(parts[3])
            if not 1024 <= port <= 65535:
                return
            self.controller.peers[ip] = dict(ip=ip, name=parts[1][:80], port=port,
                multicast=parts[2].upper() == 'MULTICAST', last_seen=time.time(),
                secured=any(p in ('auth=KEY', 'auth=ASK') for p in parts))
        except (ValueError, UnicodeError):
            pass


class Controller:
    def __init__(self, state):
        self.store = ConfigStore(state)
        self.token = secrets.token_urlsafe(32)
        # lock guards settings and routing; bt_lock serialises slow Bluetooth calls
        # so a reconnect to a sleeping speaker never blocks the dashboard.
        self.lock = asyncio.Lock()
        self.bt_lock = asyncio.Lock()
        self.activity = ActivityTracker()
        self.audio_event = asyncio.Event()
        self.reconnect_task = None
        self.reconnect_failures = 0
        self.next_reconnect = 0
        self.bt_streaming = False
        self.sony_delay_ms = None
        self.sony_delay_live = False
        self.input_delay_ms = None
        self.previous_states = None
        self.airplay_track = {}
        self.airplay_peers = []
        self.airplay_client = ''
        self.dlna_track = None
        self.bt_track = None
        self.metadata_buffer = b''
        self.bt_devices = []
        self.pair_task = None
        self.pair_until = 0
        self.receiver = None
        self.receiver_key = None
        self.receiver_message = 'Belum terhubung ke perangkat pengirim.'
        self.receiver_connected = False
        self.reader = None
        self.last_receiver_attempt = 0
        self.route = None
        self.route_module = None
        self.peers = {}
        self.discovery_transport = None
        self.last_error = ''
        self.last_action = 'Hub siap.'
        self.cached = dict(speaker=dict(connected=False, paired=False, name='ULT FIELD 1', address=SONY),
                           sink=None, sink_index=None, codec=None, audio_ready=False, eq_ready=False)

    async def snapshot(self):
        try:
            speaker = await asyncio.to_thread(bluetooth)
        except Exception:
            speaker = dict(connected=False, paired=False, trusted=False, name='ULT FIELD 1', address=SONY)
        # The speaker's A2DP delay only exists while a transport is acquired; keep the last value for display.
        self.sony_delay_live = speaker.get('delay_ms') is not None
        if self.sony_delay_live:
            self.sony_delay_ms = speaker['delay_ms']
        self.input_delay_ms = speaker.get('input_delay_ms')
        sinks = json.loads(await command('pactl', '-f', 'json', 'list', 'sinks'))
        sink = next((s for s in sinks if s.get('properties', {}).get('api.bluez5.address') == SONY
                     or ('bluez_output.' + SONY.replace(':', '_')) in s.get('name', '')), None)
        self.cached = dict(speaker=speaker, sink=sink['name'] if sink else None,
            sink_index=sink.get('index') if sink else None,
            codec=sink.get('properties', {}).get('api.bluez5.codec') if sink else None,
            sample_spec=sink.get('sample_specification') if sink else None,
            audio_ready=sink is not None, sink_state=sink.get('state') if sink else None,
            eq_ready=any(s.get('name') == 'audiohub_eq' for s in sinks))
        return self.cached

    async def dlna_request(self, action):
        """One AVTransport SOAP call to the local renderer: the reply text, or None if it does not answer."""
        envelope = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                    '<u:%s xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID>'
                    '</u:%s></s:Body></s:Envelope>' % (action, action))
        headers = {'Content-Type': 'text/xml; charset="utf-8"',
                   'SOAPACTION': '"urn:schemas-upnp-org:service:AVTransport:1#%s"' % action}
        try:
            async with ClientSession(timeout=ClientTimeout(total=1.5)) as session:
                async with session.post(DLNA_CONTROL, data=envelope, headers=headers) as response:
                    return await response.text()
        except (ClientError, asyncio.TimeoutError, OSError):
            return None

    async def dlna_transport_state(self):
        """PLAYING/STOPPED/PAUSED_PLAYBACK from the renderer; its audio stream stays open after Stop."""
        reply = await self.dlna_request('GetTransportInfo')
        return transport_state(reply) if reply else None

    async def dlna_position(self):
        reply = await self.dlna_request('GetPositionInfo')
        return didl_track(reply) if reply else None

    def read_pipe(self, fd):
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if chunk:
            self.feed_airplay_metadata(chunk)

    def feed_airplay_metadata(self, chunk):
        items, self.metadata_buffer = parse_shairport_metadata(self.metadata_buffer + chunk)
        for kind, code, data in items:
            self.airplay_track = apply_airplay_item(self.airplay_track, kind, code, data)
            if kind == 'ssnc' and code == 'snam':
                self.airplay_client = clean_text(data.decode('utf-8', 'replace'))

    async def refresh_airplay_peers(self):
        """Who has an AirPlay 2 session open: the peers of port 7000, an IPv6 one shown by its IPv4 address."""
        try:
            peers = peer_addresses(await command('ss', '-tn', 'state', 'established', '( sport = :7000 )'))
            neighbors = await command('ip', 'neigh') if any(':' in peer for peer in peers) else ''
            self.airplay_peers = list(dict.fromkeys(lan_address(peer, neighbors) for peer in peers))
        except Exception as error:
            log.info('Sesi AirPlay tidak terbaca: %s', str(error)[-90:])

    async def read_airplay_metadata(self):
        """Follow shairport-sync's metadata pipe. It is opened read-write so it never reports EOF between
        sessions, and reopened if shairport recreates it."""
        loop = asyncio.get_running_loop()
        while True:
            fd = None
            try:
                fd = os.open(AIRPLAY_METADATA_PIPE, os.O_RDWR | os.O_NONBLOCK)
                info = os.fstat(fd)
                if not stat.S_ISFIFO(info.st_mode):
                    raise OSError('bukan pipa')
                loop.add_reader(fd, self.read_pipe, fd)
                while os.stat(AIRPLAY_METADATA_PIPE).st_ino == info.st_ino:
                    await asyncio.sleep(5)
            except OSError:
                pass
            finally:
                if fd is not None:
                    loop.remove_reader(fd)
                    os.close(fd)
            await asyncio.sleep(5)

    async def refresh_track(self, source, states):
        """Best effort: reading a title must never get in the way of the audio."""
        self.dlna_track = self.bt_track = None
        try:
            if source == 'dlna' and states.get('audiohub_dlna') == 'RUNNING':
                self.dlna_track = await self.dlna_position()
            elif source == 'bluetooth' and states.get('audiohub_bluetooth') == 'RUNNING':
                self.bt_track = await asyncio.to_thread(bluetooth_track)
        except Exception as error:
            log.info('Judul lagu tidak terbaca: %s', str(error)[-90:])

    def now_playing(self):
        source = self.store.data['source']
        if (self.previous_states or {}).get('audiohub_' + source) != 'RUNNING':
            return None
        track = {'airplay': self.airplay_track, 'dlna': self.dlna_track, 'bluetooth': self.bt_track}.get(source)
        return dict(track, source=source) if track else None

    async def refresh_activity(self):
        """Note which source is playing and follow a new one when the selected source is silent."""
        sinks = await command('pactl', 'list', 'short', 'sinks')
        missing = [name for name in EXTRA_SINKS if name not in sink_states(sinks)]
        for name in missing:
            await command('pactl', 'load-module', 'module-null-sink', 'sink_name=' + name, 'rate=48000',
                          'channels=2', 'sink_properties=device.description=' + EXTRA_SINKS[name])
        if missing:
            sinks = await command('pactl', 'list', 'short', 'sinks')
        indexes = sink_indexes(sinks)
        inputs = json.loads(await command('pactl', '-f', 'json', 'list', 'sink-inputs'))
        target = indexes.get('audiohub_bluetooth')
        if target is not None:
            moves = bluetooth_streams_to_move(inputs, target)
            for index in moves:
                await command('pactl', 'move-sink-input', str(index), 'audiohub_bluetooth', check=False)
            if moves:
                inputs = json.loads(await command('pactl', '-f', 'json', 'list', 'sink-inputs'))
        dlna_open = any(item.get('sink') == indexes.get('audiohub_dlna') and not item.get('corked') for item in inputs)
        states = sources_playing(inputs, indexes, await self.dlna_transport_state() if dlna_open else None)
        self.bt_streaming = states['audiohub_bluetooth'] == 'RUNNING'
        old = dict(self.store.data)
        source = pick_source(old['source'], states, self.previous_states) if old['auto_source'] else old['source']
        self.previous_states = states
        if states.get('audiohub_' + source) == 'RUNNING':
            self.activity.touch(time.monotonic())
        if source != old['source']:
            self.store.update({'source': source})
            await self.apply(old)
            self.last_action = 'Beralih otomatis ke ' + SOURCE_NAMES[source] + '.'
        await self.refresh_track(source, states)

    async def route_audio(self):
        # The loopback keeps the A2DP transport busy, so it only exists while audio
        # plays (plus a short hold) and the Sony can idle or sleep otherwise. The sink's
        # node index is part of the key: a recreated Bluetooth node needs a fresh loopback.
        sink = self.cached.get('sink')
        data = self.store.data
        playing = self.activity.active(time.monotonic())
        target = loopback_target(sink, data['eq_enabled'], self.cached.get('eq_ready', False))
        wanted = ((data['source'], 'audiohub_' + data['source'] + '.monitor', target, self.cached.get('sink_index'))
                  if sink and data['speaker_enabled'] and playing else None)
        owned = loopback_ids(await command('pactl', 'list', 'short', 'modules'))
        if wanted == self.route and (self.route_module in owned if wanted else not owned):
            return
        for module in owned:
            await command('pactl', 'unload-module', str(module), check=False)
        self.route = None
        self.route_module = None
        if wanted:
            module_id = await command('pactl', 'load-module', 'module-loopback',
                f'source={wanted[1]}', f'sink={target}', f'latency_msec={LOOPBACK_MS}', 'source_dont_move=true', 'sink_dont_move=true')
            self.route_module = int(module_id)
            self.route = wanted
            await command('pactl', 'set-sink-volume', sink, str(self.store.data['volume']) + '%')

    async def stop_receiver(self):
        if self.receiver and self.receiver.returncode is None:
            self.receiver.terminate()
            try:
                await asyncio.wait_for(self.receiver.wait(), 4)
            except asyncio.TimeoutError:
                self.receiver.kill()
                await self.receiver.wait()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        self.receiver = None
        self.receiver_key = None
        self.receiver_connected = False

    async def read_receiver(self, process):
        async for raw in process.stdout:
            line = raw.decode(errors='replace').strip()
            log.info('WFAS %s', line[:350])
            try:
                event = json.loads(line)
            except ValueError:
                continue
            name = event.get('event', '')
            message = str(event.get('message', ''))
            if name == 'client_connecting':
                self.receiver_message = 'Menghubungkan ke ' + str(event.get('server', 'perangkat'))
            elif 'disconnect' in name or 'error' in name or 'timeout' in name or 'incompatible' in name:
                self.receiver_connected = False
                self.receiver_message = message or 'Periksa aplikasi pengirim dan firewall perangkat.'
            elif 'connected' in name:
                self.receiver_connected = True
                self.receiver_message = message or 'Audio perangkat tersambung.'
        self.receiver_connected = False

    async def sync_receiver(self):
        data = self.store.data
        # Once "Dengarkan" is on, keep listening to that sender whichever source is selected:
        # its audio then joins the automatic source switching like every other source.
        wanted = (data['sender_ip'], data['buffer_ms']) if data['network_enabled'] and data['sender_ip'] else None
        if not wanted:
            if self.receiver:
                await self.stop_receiver()
            return
        if self.receiver_key == wanted and self.receiver and self.receiver.returncode is None:
            return
        if time.monotonic() - self.last_receiver_attempt < 10:
            return
        await self.stop_receiver()
        self.last_receiver_attempt = time.monotonic()
        if not await sender_available(wanted[0]):
            self.receiver_message = 'Menunggu WFAS. Jalankan Send / Server dalam mode Unicast dan izinkan firewall jaringan pribadi.'
            return
        self.receiver_message = 'Mencari aplikasi pengirim di ' + wanted[0]
        env = {**os.environ, 'PULSE_SINK': 'audiohub_network', 'JAVA_TOOL_OPTIONS': '-Xmx256m -Djava.awt.headless=true'}
        self.receiver = await asyncio.create_subprocess_exec(WFAS, '--client', '--connect', wanted[0],
            '--latency', str(wanted[1]), '--output', 'default', '--json',
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env, limit=65536)
        self.receiver_key = wanted
        self.reader = asyncio.create_task(self.read_receiver(self.receiver))

    async def apply(self, old):
        data = self.store.data
        await command('pw-metadata', '-n', 'settings', '0', 'clock.force-quantum', str(data['quantum']))
        if self.cached.get('sink'):
            await command('pactl', 'set-sink-volume', self.cached['sink'], str(data['volume']) + '%')
        if any(old.get(k) != data[k] for k in ('sender_ip', 'buffer_ms', 'network_enabled')):
            await self.stop_receiver()
            self.last_receiver_attempt = 0
        await self.route_audio()
        await self.sync_receiver()

    async def pair_speaker(self):
        previous = await asyncio.to_thread(pairable, True)
        scanner = None
        try:
            # Pairable/bondable must be enabled for a persistent bond.
            scanner = await asyncio.create_subprocess_exec('bluetoothctl', '--timeout', '25', 'scan', 'on',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            for _ in range(12):
                try:
                    info = await asyncio.to_thread(bluetooth)
                    break
                except Exception:
                    await asyncio.sleep(.5)
            else:
                raise ValueError('Sony belum terdeteksi. Aktifkan mode pairing pada speaker, lalu coba lagi.')
            if info.get('connected') and not info.get('bonded'):
                await asyncio.to_thread(bluetooth, 'disconnect')
                await asyncio.sleep(.5)
            if not info.get('bonded'):
                await command('bluetoothctl', '--agent', 'NoInputNoOutput', '--timeout', '20', 'pair', SONY, timeout=23, check=False)
            info = await asyncio.to_thread(bluetooth)
            if not info.get('bonded'):
                raise ValueError('Pairing belum tersimpan. Pastikan Sony masih dalam mode pairing.')
        finally:
            if scanner and scanner.returncode is None:
                scanner.terminate()
                await scanner.wait()
            await asyncio.to_thread(pairable, previous)

    async def play_test_tone(self):
        await self.snapshot()
        if not self.cached.get('sink'):
            raise ValueError('Sony belum siap menerima audio. Sambungkan speaker terlebih dahulu.')
        path = self.store.path.parent / 'test.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(48000)
            frames = bytearray()
            for i in range(48000):
                gain = min(i / 2400, (48000 - i) / 2400, 1)
                sample = int(32767 * 0.10 * gain * math.sin(2 * math.pi * 523.25 * i / 48000))
                frames.extend(struct.pack('<hh', sample, sample))
            audio.writeframes(frames)
        await command('paplay', '--device', self.cached['sink'], str(path), timeout=6)

    async def speaker_action(self, action):
        if action not in ('connect', 'disconnect', 'pair', 'test'):
            raise ValueError('Aksi speaker tidak dikenal.')
        if action == 'test':
            async with self.lock:
                await self.play_test_tone()
                self.last_action = 'Nada uji dikirim ke Sony.'
        elif action == 'disconnect':
            async with self.lock:
                self.store.update({'speaker_enabled': False})
                await self.route_audio()
            async with self.bt_lock:
                await asyncio.to_thread(bluetooth, 'disconnect')
            self.last_action = 'Sony diputus. Reconnect otomatis ditahan sampai disambungkan kembali.'
        else:
            async with self.lock:
                self.store.update({'speaker_enabled': True})
            self.reconnect_failures = 0
            self.next_reconnect = 0
            async with self.bt_lock:
                if action == 'pair':
                    await self.pair_speaker()
                await asyncio.to_thread(bluetooth, 'connect')
            await asyncio.sleep(1)
            async with self.lock:
                await self.snapshot()
                await self.route_audio()
                self.last_action = 'Sony tersambung.' if self.cached.get('sink') else 'Bluetooth tersambung, menunggu keluaran audio.'

    async def refresh_bluetooth_inputs(self):
        try:
            self.bt_devices = await asyncio.to_thread(bluetooth_devices)
            for device in self.bt_devices:
                if device['paired'] and not device['trusted']:
                    await asyncio.to_thread(bluetooth_trust, device['address'])
                    device['trusted'] = True
        except Exception as error:
            log.info('Daftar perangkat Bluetooth: %s', str(error)[-90:])

    async def start_pairing(self):
        if self.pair_task and not self.pair_task.done():
            return
        self.pair_until = time.monotonic() + PAIRING_SECONDS
        self.pair_task = asyncio.create_task(self.pairing_window())
        self.last_action = 'Mode pairing Bluetooth aktif selama %d detik.' % PAIRING_SECONDS

    async def stop_pairing(self):
        if self.pair_task and not self.pair_task.done():
            self.pair_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.pair_task
        self.last_action = 'Mode pairing Bluetooth dihentikan.'

    async def pairing_window(self):
        # A short, user-triggered window: discoverable + pairable with an auto-accepting agent.
        # New devices are trusted so they can reconnect later without an agent.
        process = await asyncio.create_subprocess_exec('stdbuf', '-oL', 'bluetoothctl', '--agent', 'NoInputNoOutput',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

        def send(text):
            if process.returncode is None and process.stdin and not process.stdin.is_closing():
                process.stdin.write(text.encode() + b'\n')

        async def answer_prompts():
            async for raw in process.stdout:
                reply = agent_reply(raw.decode(errors='replace'))
                if reply and reply.startswith('trust '):
                    log.info('Pairing Bluetooth: %s', reply)
                    await asyncio.to_thread(bluetooth_trust, reply[6:])
                elif reply:
                    log.info('Pairing Bluetooth: %s', reply)
                    send(reply)

        try:
            for line in ('default-agent', 'pairable on', 'discoverable on'):
                send(line)
            await asyncio.wait_for(answer_prompts(), PAIRING_SECONDS)
        except asyncio.TimeoutError:
            pass
        finally:
            for line in ('discoverable off', 'pairable off', 'quit'):
                send(line)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 3)
            if process.returncode is None:
                process.kill()
                await process.wait()
            self.pair_until = 0
            await self.refresh_bluetooth_inputs()

    def maybe_reconnect(self):
        speaker = self.cached['speaker']
        if speaker['connected']:
            self.reconnect_failures = 0
            return
        running = self.reconnect_task is not None and not self.reconnect_task.done()
        if (running or not speaker['paired'] or not should_reconnect(self.store.data, False)
                or time.monotonic() < self.next_reconnect):
            return
        self.reconnect_task = asyncio.create_task(self.reconnect())

    async def reconnect(self):
        # Runs outside self.lock: a sleeping speaker can take ~20s to time out, and bluez
        # may already be reconnecting on its own (busy/in-progress), so failures just back off.
        try:
            async with self.bt_lock:
                await asyncio.to_thread(bluetooth, 'connect')
        except Exception as error:
            delay = next_retry_delay(self.reconnect_failures)
            self.reconnect_failures += 1
            self.next_reconnect = time.monotonic() + delay
            log.info('Sony belum tersambung (%s); mencoba lagi dalam %d detik.', str(error)[-90:], delay)
            return
        self.reconnect_failures = 0
        self.next_reconnect = 0
        async with self.lock:
            await self.snapshot()
            await self.route_audio()

    async def tick(self):
        async with self.lock:
            await self.snapshot()
            await self.refresh_activity()
            await self.route_audio()
            await self.sync_receiver()
        self.maybe_reconnect()
        await self.refresh_bluetooth_inputs()
        await self.refresh_airplay_peers()

    async def watch_audio(self):
        # React to sink changes at once instead of waiting for the next tick, so audio that
        # starts after an idle period is only clipped by a few hundred milliseconds.
        while True:
            process = None
            try:
                process = await asyncio.create_subprocess_exec('pactl', 'subscribe',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                async for raw in process.stdout:
                    if b'sink' in raw:
                        self.audio_event.set()
            except OSError as error:
                log.warning('pactl subscribe: %s', error)
            finally:
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
            await asyncio.sleep(2)

    async def react_to_audio(self):
        while True:
            await self.audio_event.wait()
            await asyncio.sleep(.2)
            self.audio_event.clear()
            try:
                async with self.lock:
                    await self.refresh_activity()
                    await self.route_audio()
            except Exception as error:
                log.warning('Audio event: %s', error)

    async def loop(self):
        while True:
            try:
                await self.tick()
                self.last_error = ''
            except Exception as error:
                self.last_error = str(error)[-350:]
                log.warning('Audio tick: %s', error)
            await asyncio.sleep(3)

    async def lifecycle(self, app):
        # Listen only for announcements; do not scan arbitrary LAN hosts.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', 9091))
        for address in ('192.168.8.240', '192.168.8.241'):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                socket.inet_aton('239.255.0.1') + socket.inet_aton(address))
        sock.setblocking(False)
        self.discovery_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: Discovery(self), sock=sock)
        await command('pactl', 'set-default-sink', 'audiohub_network')
        await command('pw-metadata', '-n', 'settings', '0', 'clock.force-quantum', str(self.store.data['quantum']))
        try:
            await asyncio.to_thread(bluetooth_alias, ADAPTER_ALIAS)
        except Exception as error:
            log.warning('Nama adapter Bluetooth tidak diubah: %s', error)
        tasks = [asyncio.create_task(self.loop()), asyncio.create_task(self.watch_audio()),
                 asyncio.create_task(self.react_to_audio()), asyncio.create_task(self.read_airplay_metadata())]
        yield
        tasks += [task for task in (self.reconnect_task, self.pair_task) if task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.stop_receiver()
        self.discovery_transport.close()

    def status(self):
        now = time.time()
        peers = [p for p in self.peers.values() if now - p['last_seen'] < 60]
        return dict(settings=self.store.data, **self.cached, peers=peers,
                    receiver=dict(running=bool(self.receiver and self.receiver.returncode is None),
                                  connected=self.receiver_connected, message=self.receiver_message),
                    route_ready=bool(self.cached.get('sink') and self.store.data['speaker_enabled']),
                    route_active=self.route is not None, now_playing=self.now_playing(),
                    playing={source: (self.previous_states or {}).get('audiohub_' + source) == 'RUNNING' for source in SOURCES},
                    latency=latency_report(self.store.data['source'], self.store.data, self.sony_delay_ms,
                                           self.sony_delay_live, self.input_delay_ms),
                    connections=connection_list(
                        self.bt_devices, self.airplay_peers, self.airplay_client,
                        self.store.data['sender_ip'] if self.receiver_connected else '',
                        self.peers.get(self.store.data['sender_ip'], {}).get('name', ''), self.previous_states or {}),
                    bluetooth_in=dict(pairing=max(0, round(self.pair_until - time.monotonic())), devices=self.bt_devices,
                                      name=ADAPTER_ALIAS, streaming=self.bt_streaming),
                    last_action=self.last_action,
                    error=self.last_error, token=self.token)


def make_app(controller):
    allowed = {'192.168.8.240', '192.168.8.241', '127.0.0.1', 'localhost', 'audio.home.arpa'}

    @web.middleware
    async def guard(request, handler):
        host = request.host.split(':')[0].lower()
        if host not in allowed:
            return web.json_response({'error': 'Host tidak diizinkan.'}, status=403)
        if request.method not in ('GET', 'HEAD'):
            origin = request.headers.get('Origin')
            if origin and origin != 'http://' + request.host:
                return web.json_response({'error': 'Origin tidak diizinkan.'}, status=403)
            token = request.headers.get('X-AudioHub-Token', '')
            if not hmac.compare_digest(token, controller.token):
                return web.json_response({'error': 'Muat ulang dashboard sebelum mengubah pengaturan.'}, status=403)
        try:
            response = await handler(request)
        except web.HTTPException:
            raise
        except (ValueError, json.JSONDecodeError) as error:
            response = web.json_response({'error': str(error)}, status=400)
        except Exception as error:
            log.exception('Request failed')
            response = web.json_response({'error': str(error)[-400:]}, status=503)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self' http://192.168.8.240:8123 http://homeassistant.home.arpa http://ha.home.arpa; base-uri 'none'; form-action 'self'"
        return response

    async def status(request):
        return web.json_response(controller.status())

    async def settings(request):
        patch = await request.json()
        async with controller.lock:
            old = dict(controller.store.data)
            controller.store.update(patch)
            await controller.apply(old)
            controller.last_action = 'Pengaturan tersimpan.'
        return web.json_response(controller.status())

    async def speaker(request):
        await controller.speaker_action(request.match_info['action'])
        return web.json_response(controller.status())

    async def bluetooth_in(request):
        action = request.match_info['action']
        if action == 'pair':
            await controller.start_pairing()
        elif action == 'cancel':
            await controller.stop_pairing()
        elif action == 'forget':
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('Alamat Bluetooth tidak valid.')
            await asyncio.to_thread(bluetooth_forget, body.get('address'))
            await controller.refresh_bluetooth_inputs()
            controller.last_action = 'Perangkat Bluetooth dihapus.'
        else:
            raise ValueError('Aksi Bluetooth tidak dikenal.')
        return web.json_response(controller.status())

    async def spotify(request):
        async with ClientSession(timeout=ClientTimeout(total=3)) as session:
            async with session.get('http://127.0.0.1:3678/status') as response:
                if response.status != 200:
                    return web.json_response({'ready': True, 'active': False})
                data = await response.json()
                # Never forward credentials or account authentication endpoints.
                return web.json_response({key: data.get(key) for key in ('active', 'paused', 'track', 'position', 'duration')})

    async def index(request):
        return web.FileResponse(BASE / 'static' / 'index.html')

    app = web.Application(middlewares=[guard], client_max_size=8192)
    app.router.add_get('/', index)
    app.router.add_get('/api/status', status)
    app.router.add_post('/api/settings', settings)
    app.router.add_post('/api/speaker/{action}', speaker)
    app.router.add_post('/api/bluetooth/{action}', bluetooth_in)
    app.router.add_get('/api/spotify', spotify)
    app.router.add_static('/static/', BASE / 'static')
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    controller = Controller(os.environ.get('AUDIOHUB_STATE', '/var/lib/audiohub/settings.json'))
    app = make_app(controller)
    app.cleanup_ctx.append(controller.lifecycle)
    web.run_app(app, host=os.environ.get('AUDIOHUB_BIND', '192.168.8.240'), port=int(os.environ.get('AUDIOHUB_PORT', '8096')), access_log=None)

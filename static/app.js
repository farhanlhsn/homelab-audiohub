'use strict';
const $ = id => document.getElementById(id);
let state, token = '', busy = false;
let noticeTimer;
function notice(message, error = false) {
  clearTimeout(noticeTimer);
  $('notice').hidden = false;
  $('notice').className = 'notice' + (error ? ' error' : '');
  $('notice').textContent = message;
  noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 9000);
}
async function request(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-AudioHub-Token': token}, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Layanan belum dapat dihubungi.');
  return result;
}
function render(next) {
  state = next; token = next.token;
  const connected = next.speaker.connected;
  $('overall').textContent = next.audio_ready ? 'Speaker siap' : connected ? 'Menyiapkan audio' : 'Speaker belum tersambung';
  $('overall').className = 'badge ' + (next.audio_ready ? 'good' : 'wait');
  $('speaker-detail').textContent = next.audio_ready ? `Bluetooth A2DP${next.codec ? ' · ' + next.codec.toUpperCase() : ''}` : connected ? 'Bluetooth tersambung, menunggu audio.' : next.speaker.paired ? 'Sony sudah dipasangkan. Nyalakan speaker untuk menyambung.' : 'Nyalakan Sony dan aktifkan mode pairing.';
  $('battery').textContent = next.speaker.battery === undefined ? '' : next.speaker.battery + '% baterai';
  $('connect').textContent = connected ? 'Sambungkan ulang' : 'Sambungkan';
  $('test').disabled = !next.audio_ready || busy;
  $('disconnect').disabled = !connected || busy;
  $('pair').hidden = next.speaker.paired;
  $('auto').checked = next.settings.auto_reconnect;
  $('route-status').textContent = next.route_ready ? 'Diteruskan ke Sony' : 'Menunggu speaker';
  for (const el of document.querySelectorAll('[data-source]')) {
    const selected = el.dataset.source === next.settings.source;
    el.setAttribute('aria-selected', String(selected));
    $(el.dataset.source + '-pane').hidden = !selected;
  }
  if (document.activeElement !== $('volume')) $('volume').value = next.settings.volume;
  $('volume-value').textContent = $('volume').value + '%';
  if (document.activeElement !== $('buffer')) $('buffer').value = next.settings.buffer_ms;
  $('buffer-value').textContent = next.settings.buffer_ms + ' ms';
  $('buffer-badge').textContent = next.settings.buffer_ms + ' ms';
  if (document.activeElement !== $('quantum')) $('quantum').value = next.settings.quantum;
  if (document.activeElement !== $('sender-ip')) $('sender-ip').value = next.settings.sender_ip;
  for (const el of document.querySelectorAll('[data-buffer]')) el.classList.toggle('selected', Number(el.dataset.buffer) === next.settings.buffer_ms && Number(el.dataset.quantum) === next.settings.quantum);
  $('receiver-status').textContent = next.settings.network_enabled ? next.receiver.message : 'Penerima audio perangkat dihentikan.';
  $('stop-network').hidden = !next.settings.network_enabled;
  const peers = $('peers'); peers.replaceChildren();
  if (!next.peers.length) {
    const p = document.createElement('p'); p.className = 'empty';
    p.textContent = 'Belum ada pengirim terdeteksi. Mulai WFAS atau masukkan IP perangkat.'; peers.append(p);
  } else for (const peer of next.peers) {
    const button = document.createElement('button'); button.className = 'peer';
    const name = document.createElement('strong'); name.textContent = peer.name || peer.ip;
    const ip = document.createElement('span'); ip.textContent = peer.ip;
    button.append(name, ip); button.onclick = () => act('/api/settings', {sender_ip: peer.ip, network_enabled: true, source: 'network'}, 'Menghubungkan audio perangkat.'); peers.append(button);
  }
  $('updated').textContent = 'Diperbarui ' + new Date().toLocaleTimeString('id-ID');
  if (next.error) $('route-status').textContent = 'Perlu perhatian: ' + next.error;
}
async function act(path, data, message) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(el => el.disabled = true);
  try { render(await request(path, data)); notice(message || state.last_action); }
  catch (e) { notice(e.message, true); }
  finally {
    busy = false; document.querySelectorAll('button').forEach(el => el.disabled = false);
    if (state) render(state);
  }
}
async function refresh() {
  if (busy) return;
  try {
    render(await request('/api/status'));
    if (state.settings.source === 'spotify') {
      const info = await request('/api/spotify');
      $('spotify-status').textContent = info.track?.name ? `${info.track.name}${info.track.artist_names ? ' — ' + info.track.artist_names.join(', ') : ''}${info.paused ? ' (dijeda)' : ''}` : 'Pilih Homelab Sony dari aplikasi Spotify untuk mulai.';
    }
  } catch (e) { $('overall').textContent = 'Dashboard terputus'; $('overall').className = 'badge wait'; }
}
$('connect').onclick = () => act('/api/speaker/connect', {});
$('disconnect').onclick = () => act('/api/speaker/disconnect', {});
$('pair').onclick = () => act('/api/speaker/pair', {}, 'Permintaan pairing selesai.');
$('test').onclick = () => act('/api/speaker/test', {}, 'Nada uji dikirim ke Sony.');
$('auto').onchange = () => act('/api/settings', {auto_reconnect: $('auto').checked});
$('volume').oninput = () => { $('volume-value').textContent = $('volume').value + '%'; };
$('volume').onchange = () => act('/api/settings', {volume: Number($('volume').value)}, 'Volume tersimpan.');
$('buffer').oninput = () => { $('buffer-value').textContent = $('buffer').value + ' ms'; };
$('buffer').onchange = () => act('/api/settings', {buffer_ms: Number($('buffer').value)}, 'Buffer jaringan tersimpan.');
$('quantum').onchange = () => act('/api/settings', {quantum: Number($('quantum').value)}, 'Blok pemrosesan tersimpan.');
$('start-network').onclick = () => act('/api/settings', {sender_ip: $('sender-ip').value.trim(), network_enabled: true, source: 'network'}, 'Penerima WFAS diaktifkan.');
$('stop-network').onclick = () => act('/api/settings', {network_enabled: false}, 'Audio perangkat dihentikan.');
$('sender-ip').onkeydown = e => { if (e.key === 'Enter') $('start-network').click(); };
document.querySelectorAll('[data-source]').forEach(el => el.onclick = () => act('/api/settings', {source: el.dataset.source}, 'Sumber audio dipilih.'));
document.querySelectorAll('[data-buffer]').forEach(el => el.onclick = () => act('/api/settings', {buffer_ms: Number(el.dataset.buffer), quantum: Number(el.dataset.quantum)}, 'Preset buffer diterapkan.'));
refresh(); setInterval(refresh, 3000);

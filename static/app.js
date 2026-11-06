'use strict';
const $ = id => document.getElementById(id);
const LINES = [
  {id: 'bluetooth', name: 'Bluetooth', badge: 'B', v: '--bt'},
  {id: 'airplay', name: 'AirPlay', badge: 'A', v: '--ap'},
  {id: 'dlna', name: 'DLNA', badge: 'D', v: '--dl'},
  {id: 'network', name: 'WFAS', badge: 'W', v: '--wf'},
  {id: 'spotify', name: 'Spotify', badge: 'S', v: '--sp', closed: true},
];
const LINE = Object.fromEntries(LINES.map(l => [l.id, l]));
const PAIR_WINDOW = 120;
let state, token = '', busy = false, noticeTimer, volTimer = null, wasPairing = false;
const shown = {};

const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const trim = (s, n) => s.length > n ? s.slice(0, n - 1) + '…' : s;
const fmt = ms => ms == null ? 'tidak diketahui' : ms >= 1000 ? (ms / 1000).toFixed(1).replace('.', ',') + ' s' : ms + ' ms';
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const glyph = (kind, x, y, color) => {
  const at = `cx="${x}" cy="${y}" r="`;
  if (kind === 'reported') return `<circle ${at}4" fill="${color}"/>`;
  if (kind === 'setting') return `<circle ${at}3.2" fill="none" stroke="${color}" stroke-width="1.8"/>`;
  return `<circle ${at}3.2" fill="none" stroke="${color}" stroke-width="1.8" stroke-dasharray="2.2 1.6"/>`;
};

function notice(message, error = false) {
  clearTimeout(noticeTimer);
  const box = $('notice');
  box.hidden = false;
  box.className = 'toast' + (error ? ' error' : '');
  box.textContent = message;
  noticeTimer = setTimeout(() => { box.hidden = true; }, error ? 14000 : 7000);
}
async function request(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-AudioHub-Token': token}, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Layanan belum dapat dihubungi.');
  return result;
}

function activeSource(next) {
  const selected = next.settings.source;
  if (next.playing[selected]) return selected;
  return LINES.map(l => l.id).find(id => next.playing[id]) || null;
}

/* ---- sign band: what is playing, over which line ---- */
function bandView(next) {
  const bt = next.bluetooth_in;
  const attention = next.error ? ' · Perlu perhatian: ' + next.error : '';
  if (bt.pairing) return {line: 'bluetooth', badge: 'B', pairing: true, title: 'Mode pairing aktif · ', text: `Di perangkat, buka Bluetooth lalu pilih “${bt.name}”.`};
  if (!next.speaker.connected) {
    return {line: 'idle', badge: '!', title: 'Sony belum tersambung',
      text: (next.speaker.paired ? 'Nyalakan speaker atau tekan Sambungkan.' : 'Nyalakan Sony dan aktifkan mode pairing.') + attention};
  }
  const active = activeSource(next);
  if (!active) {
    return {line: 'idle', badge: '·', dim: true, title: 'Tidak ada yang memutar',
      text: (next.route_ready ? 'Sony siaga dan bisa tidur. Mulai putar dari perangkat mana pun.' : 'Menunggu speaker.') + attention};
  }
  const devices = next.connections.filter(c => c.source === active);
  const device = devices.length ? devices[0].name : active === 'dlna' ? 'Pemutar DLNA' : '';
  const track = next.now_playing && next.now_playing.source === active ? next.now_playing : null;
  const second = track ? (track.title ? track.title + (track.artist ? ' — ' + track.artist : '') : track.artist || '')
    : active === 'network' ? 'Audio langsung dari perangkat' : 'Judul tidak dikirim oleh pengirim';
  const total = active === next.settings.source ? next.latency.total_ms : null;
  return {line: active, badge: LINE[active].badge, title: LINE[active].name + (device ? ' · ' + device : ''),
    text: second + attention, total};
}
function renderBand(next, offline) {
  const view = offline ? {line: 'idle', badge: '!', title: 'Hub tidak terjangkau', text: 'Mencoba menyambung lagi…'} : bandView(next);
  const key = JSON.stringify(view);
  if (shown.band !== key) {
    shown.band = key;
    const band = $('band');
    band.dataset.line = view.line;
    const roundel = el('div', 'roundel', view.badge);
    roundel.setAttribute('aria-hidden', 'true');
    if (view.dim) roundel.style.opacity = '.35';
    const message = el('div', 'msg');
    const heading = el('h1', '', view.title);
    if (view.pairing) heading.append(el('span', 'count'));
    message.append(heading, el('p', '', view.text));
    if (view.pairing) {
      const bar = el('div', 'bar'); bar.append(el('i'));
      message.append(bar);
    }
    if (view.total !== undefined) {
      message.append(el('p', 'mtot', view.total == null ? '' : '≈ ' + fmt(view.total) + ' player → Sony'));
    }
    const row = el('div', 'row');
    row.append(roundel, message);
    if (view.total !== undefined) {
      const meta = el('div', 'meta');
      meta.append(el('strong', '', view.total == null ? '—' : '≈ ' + fmt(view.total)), 'perkiraan player → Sony');
      row.append(meta);
    }
    band.replaceChildren(row);
  }
  // the countdown ticks outside the live region's text so a screen reader is not interrupted every poll
  const count = $('band').querySelector('.count');
  if (count && !offline) {
    const seconds = next.bluetooth_in.pairing;
    count.textContent = String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
    count.setAttribute('aria-hidden', 'true');
    $('band').querySelector('.bar i').style.transform = `scaleX(${(seconds / PAIR_WINDOW).toFixed(3)})`;
  }
}

/* ---- the map ---- */
function mapInfo(next) {
  const active = activeSource(next);
  return LINES.map(l => {
    const names = next.connections.filter(c => c.source === l.id).map(c => c.name);
    const lat = next.latency.lines[l.id] || {};
    return {
      id: l.id, active: l.id === active, ms: lat.ms ?? null, kind: lat.kind || null,
      device: l.closed ? 'tutup' : names.length ? names[0] + (names.length > 1 ? ' +' + (names.length - 1) : '') : 'menunggu',
    };
  });
}
function mapSvg(info, outMs, vertical, motion) {
  const ink = 'var(--ink)', muted = 'var(--muted)';
  const activeLine = info.find(i => i.active);
  const order = info.filter(i => !i.active).concat(activeLine ? [activeLine] : []);
  const col = i => `var(${LINE[i.id].v})`;
  const line = {w: i => i.active ? 10 : 6, op: i => i.active ? 1 : (LINE[i.id].closed ? .6 : .4), dash: i => LINE[i.id].closed ? ' stroke-dasharray="2 12"' : ''};
  const roundel = (i, x, y) => {
    const def = LINE[i.id], closed = def.closed;
    const ring = i.active ? `<circle class="ring" cx="${x}" cy="${y}" r="20" fill="none" stroke="${ink}" stroke-width="2.5"/>` : '';
    const body = `<circle cx="${x}" cy="${y}" r="15" fill="${closed ? 'var(--panel)' : col(i)}"${closed ? ` stroke="${muted}" stroke-width="2" stroke-dasharray="3 3"` : ''}/>`;
    const letter = `<text x="${x}" y="${y + 6}" text-anchor="middle" font-size="17" font-weight="700" fill="${closed ? muted : 'var(--on-line)'}">${def.badge}</text>`;
    if (closed) return ring + body + letter;
    const hit = `<circle cx="${x}" cy="${y}" r="24" fill="transparent"/>`;
    return `<g class="stop" data-source="${i.id}" role="button" tabindex="0" aria-label="Pakai jalur ${def.name}${i.active ? ', sedang aktif' : ''}">${hit}${ring}${body}${letter}</g>`;
  };
  const latency = (i, x, y, anchor) => {
    const text = i.ms == null ? '—' : fmt(i.ms), width = text.length * 7.4;
    if (i.ms == null) return `<text x="${x}" y="${y}" text-anchor="${anchor}" font-size="14" font-weight="600" fill="${muted}">${text}</text>`;
    const gx = anchor === 'middle' ? x - width / 2 - 8 : x, tx = anchor === 'middle' ? x + 6 : x + 10;
    return `${glyph(i.kind || 'setting', gx, y - 5, muted)}<text x="${tx}" y="${y}" text-anchor="${anchor}" font-size="14" font-weight="600" fill="${ink}">${text}</text>`;
  };
  const hub = (x, y) => `<circle cx="${x}" cy="${y}" r="12" fill="var(--panel)" stroke="${ink}" stroke-width="4"/>`;
  const outText = outMs == null ? '—' : outMs + ' ms';
  const trunk = activeLine ? `var(${LINE[activeLine.id].v})` : 'var(--rule)';
  const motionDot = d => activeLine && motion
    ? `<circle r="6" fill="var(--panel)" stroke="${trunk}" stroke-width="3"><animateMotion dur="${vertical ? 5 : 6}s" repeatCount="indefinite" path="${d}" calcMode="linear"/></circle>` : '';
  let out = '', W, H, path = '';
  if (vertical) {
    W = 358; H = 340;
    const xs = [36, 107.5, 179, 250.5, 322], hx = 179, hubY = 272;
    info.forEach((i, n) => { i.d = `M${xs[n]},93 V${hubY - Math.abs(xs[n] - hx)} L${hx},${hubY}`; });
    order.forEach(i => { out += `<path class="line" d="${i.d}" fill="none" stroke="${col(i)}" stroke-width="${line.w(i)}" stroke-linecap="round" stroke-linejoin="round" opacity="${line.op(i)}"${line.dash(i)}/>`; });
    out += `<path class="trunk" d="M${hx},${hubY} V${H}" fill="none" stroke="${trunk}" stroke-width="10"/>`;
    info.forEach((i, n) => {
      const x = xs[n];
      out += `<text x="${x}" y="15" text-anchor="middle" font-size="13" font-weight="700" fill="${ink}">${LINE[i.id].name}</text>`;
      out += `<text x="${x}" y="31" text-anchor="middle" font-size="11.5" fill="${muted}">${esc(trim(i.device, 11))}</text>`;
      out += latency(i, x, 52, 'middle') + roundel(i, x, 76);
    });
    out += hub(hx, hubY);
    out += `<text x="${hx + 20}" y="${hubY - 4}" font-size="14" font-weight="700" fill="${ink}">Homelab</text>`;
    out += `<text x="${hx + 20}" y="${hubY + 32}" font-size="12.5" fill="${muted}">Hub → Sony</text>`;
    out += glyph('setting', hx + 25, hubY + 50, muted) + `<text x="${hx + 34}" y="${hubY + 55}" font-size="14" font-weight="700" fill="${ink}">${outText}</text>`;
    if (activeLine) path = activeLine.d + ` V${H}`;
  } else {
    W = 660; H = 344;
    const hubX = 470, ymid = 172, ys = info.map((_, n) => 40 + n * 66);
    info.forEach((i, n) => { i.d = `M195,${ys[n]} H${hubX - 26 - Math.abs(ys[n] - ymid)} L${hubX - 26},${ymid} H${hubX}`; });
    order.forEach(i => { out += `<path class="line" d="${i.d}" fill="none" stroke="${col(i)}" stroke-width="${line.w(i)}" stroke-linecap="round" stroke-linejoin="round" opacity="${line.op(i)}"${line.dash(i)}/>`; });
    out += `<path class="trunk" d="M${hubX},${ymid} H${W - 4}" fill="none" stroke="${trunk}" stroke-width="10"/>`;
    info.forEach((i, n) => {
      const y = ys[n];
      out += `<text x="0" y="${y - 2}" font-size="15" font-weight="700" fill="${ink}">${LINE[i.id].name}</text>`;
      out += `<text x="0" y="${y + 16}" font-size="13" fill="${muted}">${esc(trim(i.device, 24))}</text>`;
      out += roundel(i, 170, y) + latency(i, 214, y - 14, 'start');
    });
    out += hub(hubX, ymid);
    out += `<text x="${hubX}" y="${ymid - 26}" text-anchor="middle" font-size="15" font-weight="700" fill="${ink}">Homelab</text>`;
    out += `<text x="565" y="${ymid - 18}" text-anchor="middle" font-size="13" fill="${muted}">Hub → Sony</text>`;
    out += glyph('setting', 535, ymid + 31, muted) + `<text x="545" y="${ymid + 36}" font-size="15" font-weight="700" fill="${ink}">${outText}</text>`;
    if (activeLine) path = activeLine.d + ` H${W - 4}`;
  }
  const label = activeLine
    ? `Peta jalur audio: jalur ${LINE[activeLine.id].name} aktif${activeLine.device !== 'menunggu' ? ', dari ' + esc(activeLine.device) : ''}, melalui Homelab ke Sony.`
    : 'Peta jalur audio: tidak ada jalur yang aktif. Semua jalur bertemu di Homelab lalu menuju Sony.';
  return `<svg viewBox="0 0 ${W} ${H}" role="group" aria-label="${label}">${out}${motionDot(path)}</svg>`;
}
function renderMap(next) {
  const vertical = !matchMedia('(min-width:900px)').matches;
  const motion = !matchMedia('(prefers-reduced-motion: reduce)').matches;
  const info = mapInfo(next), outMs = next.latency.output.ms;
  const key = JSON.stringify([info, outMs, vertical, motion]);
  if (shown.map === key) return;
  shown.map = key;
  $('mapbox').innerHTML = mapSvg(info, outMs, vertical, motion);
}

/* ---- terminus: the speaker ---- */
function renderSpeaker(next) {
  const connected = next.speaker.connected;
  const battery = next.speaker.battery === undefined ? '' : ` · baterai ${next.speaker.battery}%`;
  $('sony-detail').textContent = next.audio_ready
    ? `Tersambung · A2DP${next.codec ? ' ' + next.codec.toUpperCase() : ''}${battery}`
    : connected ? 'Bluetooth tersambung, menunggu audio.'
      : next.speaker.paired ? 'Sony sudah dipasangkan. Nyalakan speaker untuk menyambung.' : 'Nyalakan Sony dan aktifkan mode pairing.';
  $('connect').hidden = connected;
  $('test').disabled = !next.audio_ready || busy;
  $('disconnect').disabled = !connected || busy;
  $('hub-dot').className = next.audio_ready ? 'ok' : '';
  $('hub-text').textContent = next.audio_ready ? 'Homelab tersambung' : 'Speaker belum siap';
  if (document.activeElement !== $('vol') && volTimer === null) {
    $('vol').value = next.settings.volume;
    $('volnum').textContent = next.settings.volume;
  }
  const dot = kind => `<svg viewBox="0 0 10 10" aria-hidden="true">${glyph(kind, 5, 5, 'currentColor')}</svg>`;
  const parts = next.latency.output.parts;
  $('hops').innerHTML = parts.map(p => `<li>${dot(p.kind)}<span>${esc(p.name)}<br><span class="note">${esc(p.note)}</span></span><span class="n">${p.ms == null ? '—' : p.ms + ' ms'}</span></li>`).join('')
    + `<li class="total"><span>Hub → Sony</span><span class="n">${next.latency.output.ms == null ? '—' : '≈ ' + next.latency.output.ms + ' ms'}</span></li>`;
}

/* ---- boarding list: Bluetooth devices, WFAS senders ---- */
function renderBoard(next) {
  const bt = next.bluetooth_in;
  $('bt-name').textContent = bt.name;
  $('bt-pair').textContent = bt.pairing ? 'Batalkan pairing' : 'Pasangkan perangkat baru';
  $('bt-state').textContent = bt.pairing ? `Mode pairing aktif ${bt.pairing} dtk.` : bt.streaming ? 'Menerima audio dari perangkat.' : '';
  if (bt.pairing && !wasPairing) $('row-bluetooth').open = true;
  wasPairing = !!bt.pairing;
  const devices = $('bt-devices'); devices.replaceChildren();
  if (!bt.devices.length) devices.append(el('p', 'empty', 'Belum ada perangkat yang dipasangkan.'));
  for (const device of bt.devices) {
    const row = el('div', 'dev'), info = el('div');
    info.append(el('strong', '', device.name), el('span', '', device.connected ? 'Tersambung' : 'Dipasangkan'));
    const forget = el('button', 'link', 'Hapus');
    forget.type = 'button';
    forget.setAttribute('aria-label', 'Hapus ' + device.name);
    forget.onclick = () => act('/api/bluetooth/forget', {address: device.address}, 'Perangkat dihapus.');
    row.append(info, forget); devices.append(row);
  }
  $('receiver-status').textContent = next.settings.network_enabled ? next.receiver.message : 'Penerima audio perangkat dihentikan.';
  $('stop-network').hidden = !next.settings.network_enabled;
  if (document.activeElement !== $('sender-ip')) $('sender-ip').value = next.settings.sender_ip;
  const peers = $('peers'); peers.replaceChildren();
  if (!next.peers.length) peers.append(el('p', 'empty', 'Belum ada pengirim terdeteksi. Mulai WFAS atau masukkan IP perangkat.'));
  for (const peer of next.peers) {
    const button = el('button', 'dev'); button.type = 'button';
    const info = el('div');
    info.append(el('strong', '', peer.name || peer.ip), el('span', '', peer.ip));
    button.append(info);
    button.onclick = () => act('/api/settings', {sender_ip: peer.ip, network_enabled: true, source: 'network'}, 'Menghubungkan audio perangkat.');
    peers.append(button);
  }
}

function renderSettings(next) {
  $('auto').checked = next.settings.auto_reconnect;
  $('auto-source').checked = next.settings.auto_source;
  if (document.activeElement !== $('buffer')) $('buffer').value = next.settings.buffer_ms;
  $('buffer-value').textContent = next.settings.buffer_ms + ' ms';
  if (document.activeElement !== $('quantum')) $('quantum').value = next.settings.quantum;
  for (const button of document.querySelectorAll('[data-buffer]')) {
    const on = Number(button.dataset.buffer) === next.settings.buffer_ms && Number(button.dataset.quantum) === next.settings.quantum;
    button.classList.toggle('selected', on);
    button.setAttribute('aria-pressed', String(on));
  }
}

function render(next, offline = false) {
  state = next; token = next.token;
  renderBand(next, offline);
  renderMap(next);
  renderSpeaker(next);
  renderBoard(next);
  renderSettings(next);
  $('updated').textContent = 'Diperbarui ' + new Date().toLocaleTimeString('id-ID');
}

async function act(path, data, message) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(button => button.disabled = true);
  try { render(await request(path, data)); notice(message || state.last_action); }
  catch (error) { notice(error.message, true); }
  finally {
    busy = false; document.querySelectorAll('button').forEach(button => button.disabled = false);
    if (state) render(state);
  }
}
async function refresh() {
  if (busy) return;
  try { render(await request('/api/status')); }
  catch (error) {
    $('hub-dot').className = ''; $('hub-text').textContent = 'Dashboard terputus';
    if (state) renderBand(state, true);
  }
}
function nudge(delta) {
  const range = $('vol');
  range.value = Math.max(0, Math.min(100, Number(range.value) + delta));
  $('volnum').textContent = range.value;
  clearTimeout(volTimer);
  volTimer = setTimeout(() => { volTimer = null; act('/api/settings', {volume: Number(range.value)}, 'Volume tersimpan.'); }, 450);
}
function pick(source) { act('/api/settings', {source}, 'Sumber audio dipilih.'); }

$('mapbox').addEventListener('click', event => { const stop = event.target.closest('[data-source]'); if (stop) pick(stop.dataset.source); });
$('mapbox').addEventListener('keydown', event => {
  const stop = event.target.closest('[data-source]');
  if (stop && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); pick(stop.dataset.source); }
});
$('connect').onclick = () => act('/api/speaker/connect', {});
$('reconnect').onclick = () => act('/api/speaker/connect', {}, 'Sony disambungkan ulang.');
$('disconnect').onclick = () => act('/api/speaker/disconnect', {});
$('pair').onclick = () => act('/api/speaker/pair', {}, 'Permintaan pairing selesai.');
$('test').onclick = () => act('/api/speaker/test', {}, 'Nada uji dikirim ke Sony.');
$('bt-pair').onclick = () => act('/api/bluetooth/' + (state.bluetooth_in.pairing ? 'cancel' : 'pair'), {});
$('auto').onchange = () => act('/api/settings', {auto_reconnect: $('auto').checked});
$('auto-source').onchange = () => act('/api/settings', {auto_source: $('auto-source').checked}, $('auto-source').checked ? 'Sumber dipilih otomatis.' : 'Sumber dikunci sesuai pilihan.');
$('vol').oninput = () => { $('volnum').textContent = $('vol').value; };
$('vol').onchange = () => act('/api/settings', {volume: Number($('vol').value)}, 'Volume tersimpan.');
$('down').onclick = () => nudge(-5);
$('up').onclick = () => nudge(5);
$('buffer').oninput = () => { $('buffer-value').textContent = $('buffer').value + ' ms'; };
$('buffer').onchange = () => act('/api/settings', {buffer_ms: Number($('buffer').value)}, 'Buffer jaringan tersimpan.');
$('quantum').onchange = () => act('/api/settings', {quantum: Number($('quantum').value)}, 'Blok pemrosesan tersimpan.');
$('start-network').onclick = () => act('/api/settings', {sender_ip: $('sender-ip').value.trim(), network_enabled: true, source: 'network'}, 'Penerima WFAS diaktifkan.');
$('stop-network').onclick = () => act('/api/settings', {network_enabled: false}, 'Audio perangkat dihentikan.');
$('sender-ip').onkeydown = event => { if (event.key === 'Enter') $('start-network').click(); };
document.querySelectorAll('[data-buffer]').forEach(button => button.onclick = () => act('/api/settings', {buffer_ms: Number(button.dataset.buffer), quantum: Number(button.dataset.quantum)}, 'Preset buffer diterapkan.'));
matchMedia('(min-width:900px)').addEventListener('change', () => { shown.map = null; if (state) renderMap(state); });
refresh(); setInterval(refresh, 3000);

'use strict';
const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('dogtrack-token') || '';
let map, marker, trail, position, firstPosition = true, timer;
const modes = {
  0: [10, 600, 'seconds', true, 'Tracks movement frequently and reports less often while resting.'],
  1: [10, 600, 'seconds', false, 'GPS and the network stay on. Frequent updates use more battery.'],
  2: [10, 60, 'minutes', false, 'GPS wakes for each report; TCP stays connected to receive commands.'],
  3: [1, 24, 'hours', false, 'Wakes for one report, then sleeps. Changes must wait for its next connection.'],
  4: [10, 600, 'seconds', false, 'Reports while moving and automatically sleeps after 10 minutes without vibration.'],
  7: [10, 1440, 'minutes', true, 'A slower movement interval conserves battery; separate reports while resting.']
};
function modeFields() {
  const [min, max, unit, still, help] = modes[$('mode').value];
  $('period').min = min; $('period').max = max;
  $('period').value = Math.max(min, Math.min(max, Number($('period').value)));
  $('period-label').textContent = `Reporting period (${unit})`;
  $('still-field').hidden = !still; $('mode-help').textContent = help;
}
function message(text) { $('message').textContent = text; }
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
    headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) lock();
    throw new Error(result.error || 'Request failed');
  }
  return result;
}
function lock() {
  setMapExpanded(false);
  clearTimeout(timer); token = ''; sessionStorage.removeItem('dogtrack-token');
  $('dashboard').hidden = true; $('logout').hidden = true; $('login').hidden = false;
}
function ago(seconds) {
  const age = Math.max(0, Math.floor(Date.now()/1000 - seconds));
  if (age < 60) return `${age}s ago`;
  if (age < 3600) return `${Math.floor(age/60)}m ago`;
  if (age < 86400) return `${Math.floor(age/3600)}h ago`;
  return `${Math.floor(age/86400)}d ago`;
}
function initMap() {
  if (map || !window.L) return;
  map = L.map('map').setView([50, 14], 5);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    referrerPolicy: 'origin',
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  trail = L.polyline([], {color: '#135e4b', weight: 4, opacity: .65}).addTo(map);
  new ResizeObserver(() => map.invalidateSize({pan: false})).observe($('map'));
}
function updateMapExpanded(expanded) {
  $('map-card').classList.toggle('expanded', expanded);
  document.body.classList.toggle('map-expanded', expanded);
  $('fullscreen').textContent = expanded ? 'Exit full screen' : 'Full screen';
  $('fullscreen').setAttribute('aria-pressed', String(expanded));
  requestAnimationFrame(() => map?.invalidateSize({pan: false}));
}
async function setMapExpanded(expanded) {
  updateMapExpanded(expanded);
  try {
    if (expanded && $('map-card').requestFullscreen) await $('map-card').requestFullscreen();
    else if (!expanded && document.fullscreenElement === $('map-card')) await document.exitFullscreen();
  } catch {
    // The viewport-filling layout remains usable when native fullscreen is unavailable.
  }
}
document.addEventListener('fullscreenchange', () => {
  updateMapExpanded(document.fullscreenElement === $('map-card'));
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && $('map-card').classList.contains('expanded')) setMapExpanded(false);
});
$('fullscreen').onclick = () => setMapExpanded(!$('map-card').classList.contains('expanded'));
function renderCommands(commands) {
  $('commands').replaceChildren();
  if (!commands.length) { const li = document.createElement('li'); li.textContent = 'No commands yet'; $('commands').append(li); }
  for (const command of commands) {
    const li = document.createElement('li'), head = document.createElement('div'); head.className = 'head';
    const code = document.createElement('code'); code.textContent = command.text;
    const status = document.createElement('span'); status.className = `command-status ${command.status}`; status.textContent = command.status;
    head.append(code, status); li.append(head);
    const time = document.createElement('small'); time.textContent = new Date(command.created_at*1000).toLocaleString(); li.append(time);
    if (command.reply) { const pre = document.createElement('pre'); pre.textContent = command.reply; li.append(pre); }
    if (command.status === 'queued') {
      const cancel = document.createElement('button'); cancel.textContent = 'Cancel';
      cancel.onclick = () => action(() => api('/api/cancel', {id: command.id})); li.append(cancel);
    }
    $('commands').append(li);
  }
}
async function refresh() {
  const refreshToken = token;
  try {
    const [state, history] = await Promise.all([api('/api/state'), api('/api/history')]);
    if (!token || token !== refreshToken) return;
    $('login').hidden = true; $('dashboard').hidden = false; $('logout').hidden = false;
    $('demo').hidden = !state.demo; $('name').textContent = state.name;
    $('connection').textContent = state.connected ? '● Connected' : '○ Disconnected / sleeping';
    $('connection').className = `badge ${state.connected ? 'online' : ''}`;
    $('seen').textContent = state.latest ? `Report received ${ago(state.latest.received_at)}` : 'Waiting for first report';
    $('battery').textContent = state.latest ? `${state.latest.voltage.toFixed(3)} V` : '—';
    $('event').textContent = state.latest ? `Device event: ${state.latest.event}` : 'No report yet';
    position = state.position;
    $('speed').textContent = state.latest?.fix === 'A' && state.latest.speed_kmh !== null ? `${state.latest.speed_kmh} km/h` : '—';
    const latestHasFix = state.latest?.latitude !== null && state.latest?.latitude !== undefined;
    $('fix').textContent = !position ? 'No GPS fix' : state.latest?.fix === 'A' && latestHasFix ? 'Valid GPS' : 'Last known';
    $('position-time').textContent = position ? `Location timestamp: ${new Date(position.recorded_at).toLocaleString()}${position.fix === 'L' ? ' · device reused an old fix' : ''}` : 'Waiting for GPS';
    $('coordinates').textContent = position ? `${position.latitude.toFixed(5)}, ${position.longitude.toFixed(5)}` : '—';
    $('confirmed').textContent = state.last_confirmed_mode ? `Last acknowledged request: ${state.last_confirmed_mode.text}. Read configuration to check current device settings.` : 'No mode change confirmed by this server yet.';
    $('device').textContent = `Device IMEI: ${state.imei} · Refreshes every 5 seconds`;
    initMap();
    if (map) { map.invalidateSize(); trail.setLatLngs(history.map(point => [point.latitude, point.longitude])); }
    if (position) {
      $('directions').hidden = false;
      $('directions').href = `https://www.google.com/maps/search/?api=1&query=${position.latitude},${position.longitude}`;
      $('map-empty').hidden = !!map;
      if (!map) $('map-empty').textContent = 'Map library unavailable. Use “Open in Maps” below.';
      if (map) {
        const latlng = [position.latitude, position.longitude];
        if (!marker) marker = L.circleMarker(latlng, {radius: 11, color: 'white', weight: 3, fillColor: '#135e4b', fillOpacity: 1}).addTo(map);
        else marker.setLatLng(latlng);
        if (firstPosition) { map.setView(latlng, 16); firstPosition = false; }
      }
    }
    renderCommands(state.commands);
  } catch (error) { message(`Unable to refresh: ${error.message}`); }
  finally { clearTimeout(timer); if (token) timer = setTimeout(refresh, 5000); }
}
async function action(fn) {
  try { await fn(); message('Request saved. See command activity for delivery and confirmation.'); await refresh(); }
  catch (error) { message(error.message); }
}
$('login-form').onsubmit = async event => {
  event.preventDefault(); token = $('token').value.trim();
  try { await api('/api/state'); sessionStorage.setItem('dogtrack-token', token); $('token').value = ''; message(''); await refresh(); }
  catch (error) { message(error.message); }
};
$('logout').onclick = lock;
$('mode').onchange = modeFields;
$('center').onclick = () => { if (map && position) map.setView([position.latitude,position.longitude],16); };
$('settings').onsubmit = event => {
  event.preventDefault();
  action(() => api('/api/commands', {kind:'mode', mode:Number($('mode').value), period:Number($('period').value), still_hours:Number($('still').value)}));
};
$('save-last').onclick = () => action(() => api('/api/commands', {kind:'last_position', enabled:Number($('last-position').value)}));
$('read-config').onclick = () => action(() => api('/api/commands', {kind:'read_config', page:Number($('config-page').value)}));
modeFields();
if (token) refresh();

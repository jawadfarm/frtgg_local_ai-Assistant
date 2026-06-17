/* runner.js — dashboard logic for the core supervisor */
'use strict';

const TOKEN_KEY = 'runner_auth_token';
const BASE = '';                 // same origin as runner.py
let _token = localStorage.getItem(TOKEN_KEY) || '';
let _selected = null;            // currently viewed service name
let _es = null;                  // EventSource (real-time stream)

/* ── helpers ──────────────────────────────────────────────── */
function $(id){ return document.getElementById(id); }

async function api(path, body, method){
  const headers = {};
  if (body) headers['Content-Type'] = 'application/json';
  if (_token) headers['X-Auth-Token'] = _token;
  const r = await fetch(BASE + path, {
    method: method || (body ? 'POST' : 'GET'),
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401){ clearToken(); showLogin(); throw new Error('unauthorized'); }
  let data = {};
  try { data = await r.json(); } catch(e){}
  return { ok: r.ok, status: r.status, data };
}

function toast(msg, kind){
  const t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = msg;
  $('toast').appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

/* ── auth ─────────────────────────────────────────────────── */
function clearToken(){ _token = ''; localStorage.removeItem(TOKEN_KEY); }
function saveToken(t){ _token = t; localStorage.setItem(TOKEN_KEY, t); }

function showLogin(){
  closeStream();
  $('login-overlay').classList.remove('hidden');
  $('app').classList.remove('show');
  $('login-pw').value = '';
  setTimeout(() => $('login-pw').focus(), 50);
}
function hideLogin(){
  $('login-overlay').classList.add('hidden');
  $('app').classList.add('show');
}
function toggleEye(){
  const i = $('login-pw');
  i.type = i.type === 'password' ? 'text' : 'password';
}
function loginError(msg){
  $('login-error-msg').textContent = msg;
  $('login-error').classList.add('show');
}

async function doLogin(){
  $('login-error').classList.remove('show');
  const pw = $('login-pw').value;
  $('login-btn').disabled = true;
  try {
    const { ok, data } = await api('/api/login', { password: pw });
    if (ok && data.status === 'ok'){
      saveToken(data.token);
      hideLogin();
      boot();
    } else {
      const m = { wrong_password:'wrong password', no_password_set:'no password set on server' };
      loginError(m[data.message] || data.message || 'login failed');
    }
  } catch(e){
    loginError('cannot reach runner');
  } finally {
    $('login-btn').disabled = false;
  }
}

function doLogout(){
  clearToken();
  showLogin();
}

/* ── services ─────────────────────────────────────────────── */
function setConn(ok){
  const c = $('conn');
  c.classList.toggle('ok', ok);
  $('conn-text').textContent = ok ? 'connected' : 'offline';
}

const GROUP_LABELS = { core: 'core servers', apis: 'apis', added: 'added services' };

function cardHTML(s){
  const card = document.createElement('div');
  card.className = 'card' + (s.name === _selected ? ' active' : '');
  const running = s.status === 'running';
  const ports = (s.ports && s.ports.length) ? s.ports : null;
  // header line: status · pid/external
  const meta = running
      ? (s.owned ? ('pid ' + s.pid) : 'external')
      : '';
  // ports line: every port the running process tree is listening on
  const portsLine = ports
      ? `<div class="svc-ports">listening: ${ports.map(p => ':' + p).join('  ')}</div>`
      : (running ? `<div class="svc-ports dim">listening: — (no ports yet)</div>` : '');
  card.innerHTML = `
    <div class="card-top">
      <span class="svc-name">${s.name}</span>
      <span class="badge">${s.interpreter}</span>
    </div>
    <div class="stat ${s.status}">
      <span class="dot"></span>${s.status}${meta ? ' · ' + meta : ''}
    </div>
    ${portsLine}
    <div class="svc-src">${s.source || ''}</div>
    <div class="card-actions">
      <button class="btn go"     ${running ? 'disabled' : ''} data-act="start" data-n="${s.name}">▶ start</button>
      <button class="btn danger" ${running ? '' : 'disabled'} data-act="stop" data-n="${s.name}">■ stop</button>
      ${s.removable ? `<button class="btn" data-act="remove" data-n="${s.name}">✕</button>` : ''}
    </div>`;
  card.querySelector('.svc-name').onclick = () => selectLog(s.name);
  card.querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = () => svcAction(b.dataset.act, b.dataset.n);
  });
  return card;
}

function renderServices(list){
  const root = $('sections');
  root.innerHTML = '';
  ['core', 'apis', 'added'].forEach(group => {
    const items = list.filter(s => s.group === group);
    if (!items.length) return;
    const title = document.createElement('div');
    title.className = 'section-title';
    const up = items.filter(s => s.status === 'running').length;
    title.textContent = `${GROUP_LABELS[group]} · ${up}/${items.length} running`;
    root.appendChild(title);
    const grid = document.createElement('div');
    grid.className = 'grid';
    items.forEach(s => grid.appendChild(cardHTML(s)));
    root.appendChild(grid);
  });
  if (!_selected && list.length) selectLog(list[0].name);
}

async function refresh(){
  try {
    const { ok, data } = await api('/api/services');
    if (ok && data.status === 'ok'){
      setConn(true);
      renderServices(data.services);
    }
  } catch(e){ setConn(false); }
}

// Force a full re-detection now (re-scan apis folder + re-probe every service).
async function forceRefresh(){
  try {
    const { ok, data } = await api('/api/refresh', {});
    if (ok && data.status === 'ok'){
      setConn(true);
      renderServices(data.services);
      toast('re-detected all services', 'ok');
    }
  } catch(e){ setConn(false); toast('refresh failed', 'err'); }
}

async function svcAction(act, name){
  const path = act === 'start' ? '/api/start'
             : act === 'stop'  ? '/api/stop'
             : '/api/remove_file';
  const { ok, data } = await api(path, { name });
  if (ok && data.status === 'ok'){
    toast(`${name}: ${act} ok`, 'ok');
    if (act === 'remove' && name === _selected){ _selected = null; }
  } else {
    toast(`${name}: ${data.message || 'failed'}`, 'err');
  }
  refresh();
}

async function startAll(){
  await api('/api/start_all', {});
  toast('run full system', 'ok'); refresh();
}
async function stopAll(){
  await api('/api/stop_all', {});
  toast('stop all', 'ok'); refresh();
}
async function startGroup(group){
  const { ok, data } = await api('/api/start_group', { group });
  toast(ok ? `run ${group} (${data.count})` : 'failed', ok ? 'ok' : 'err');
  refresh();
}

/* ── logs ─────────────────────────────────────────────────── */
function selectLog(name){
  _selected = name;
  $('log-name').textContent = name;
  $('log').innerHTML = '';
  openStream();   // reopen the SSE stream so this service's logs backfill + stream
}
function clearLogView(){ $('log').innerHTML = ''; }

function appendLog(line){
  const div = document.createElement('div');
  const t = line.text || '';
  if (/<.*error.*>|<failed/i.test(t)) div.className = 'err';
  else if (/^<.*>$/.test(t.trim())) div.className = 'sys';
  div.textContent = t;
  const box = $('log');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.appendChild(div);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

/* ── real-time stream (SSE) ───────────────────────────────── */
function closeStream(){
  if (_es){ _es.close(); _es = null; }
}
function openStream(){
  closeStream();
  if (!_token) return;
  const url = `/api/events?token=${encodeURIComponent(_token)}`
            + `&name=${encodeURIComponent(_selected || '')}`;
  _es = new EventSource(url);
  _es.addEventListener('status', e => { setConn(true); renderServices(JSON.parse(e.data)); });
  _es.addEventListener('log',    e => { appendLog(JSON.parse(e.data)); });
  _es.onopen  = () => setConn(true);
  _es.onerror = () => {
    setConn(false);
    // surface auth failures (e.g. after a password change) as a login prompt
    api('/api/services').catch(() => {});
  };
}

/* ── add file ─────────────────────────────────────────────── */
async function addFile(){
  const file = $('add-path').value.trim();
  const useBundled = $('add-310').checked;
  $('add-msg').textContent = '';
  if (!file){ msg('add-msg', 'enter a file path', 'err'); return; }
  const body = { file };
  if (useBundled) body.python = '3.10';
  const { ok, data } = await api('/api/add_file', body);
  if (ok && data.status === 'ok'){
    msg('add-msg', `added ${data.service.name}`, 'ok');
    $('add-path').value = '';
    refresh();
  } else {
    msg('add-msg', data.message || 'failed', 'err');
  }
}

/* ── settings ─────────────────────────────────────────────── */
async function loadConfig(){
  try {
    const { ok, data } = await api('/api/config');
    if (ok && data.status === 'ok'){
      $('cfg-private').checked = !!data.config.allowed_private_network;
      $('cfg-public').checked  = !!data.config.allowed_public_network;
    }
  } catch(e){}
}
async function saveConfig(){
  const body = {
    allowed_private_network: $('cfg-private').checked,
    allowed_public_network:  $('cfg-public').checked,
  };
  const { ok } = await api('/api/config', body);
  toast(ok ? 'settings saved' : 'save failed', ok ? 'ok' : 'err');
}

async function changePassword(){
  const newPw = $('cp-new').value;
  const oldPw = $('cp-old').value;
  $('cp-msg').textContent = '';
  if (!newPw){ msg('cp-msg', 'enter a new password', 'err'); return; }
  const body = { new_password: newPw };
  if (oldPw) body.old_password = oldPw;
  const { ok, data } = await api('/api/change_password', body);
  if (ok && data.status === 'ok'){
    msg('cp-msg', 'password changed — tokens invalidated, log in again', 'ok');
    $('cp-new').value = ''; $('cp-old').value = '';
    setTimeout(() => { clearToken(); showLogin(); }, 1400);
  } else {
    const m = { wrong_password:'wrong current password', empty_password:'empty password',
                no_password_set:'no password set' };
    msg('cp-msg', m[data.message] || data.message || 'failed', 'err');
  }
}

function msg(id, text, kind){
  const el = $(id);
  el.textContent = text;
  el.className = 'msg ' + (kind || '');
}

/* ── boot ─────────────────────────────────────────────────── */
function boot(){
  loadConfig();
  refresh();        // immediate first paint
  openStream();     // then real-time status + logs via SSE
}

(function init(){
  if (_token){ hideLogin(); boot(); }
  else showLogin();
})();

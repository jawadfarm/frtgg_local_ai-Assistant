// ══════════════════════════════════════════════════════════════════
//  shared.js  —  state · auth · api · toast · utils
// ══════════════════════════════════════════════════════════════════

// ─── Auth ─────────────────────────────────────────────────────────
const TOKEN_KEY = 'frtgg_auth_token';
let _authToken  = localStorage.getItem(TOKEN_KEY) || '';

// Persisted draft of the message input, so a refresh / reopened tab keeps
// whatever the user was typing before they hit send.
const DRAFT_KEY = 'frtgg_input_draft';

// In-memory only (intentionally NOT persisted): once the user chooses "Skip
// systemapi", ignore systemapi_Authentication events for the rest of this page
// session. A refresh / new tab resets it, so the prompt can appear again.
let _systemapiSkipped = false;

// Set when the user picks a "skip systemapi" action; consumed while resolving the
// login response so systemapi is dropped instead of prompted. llm-server is never
// skippable. `_pendingLlmPass` keeps the password the user typed so the llm-server
// login can be auto-retried after a reconnect / set-password recovery.
let _skipSystemapiOnLogin = false;
let _pendingLlmPass        = '';
// systemapi flag from the most recent /api/login, so that after an llm-server
// recovery (set / sync / reconnect) we still prompt for systemapi if its
// password differs from chat's, instead of booting straight away.
let _lastSysState          = null;
// One-shot guard so the active session is auto-loaded once per boot (and not
// re-loaded on every SSE reconnect).
let _autoLoadDone          = false;

function _saveToken(t) { _authToken = t; localStorage.setItem(TOKEN_KEY, t); }
function _clearToken() { _authToken = ''; localStorage.removeItem(TOKEN_KEY); }

function _showLoginOverlay() {
  document.getElementById('login-overlay').classList.remove('hidden');
  document.getElementById('h-logout-btn').style.display = 'none';
  _resetSystemapiLoginPanel();
  setTimeout(() => document.getElementById('login-pw-input').focus(), 100);
}
// ── login recovery sub-panels: show / hide on the login overlay ─────
// All recovery panels (systemapi + the llm-server panels) share one show/hide
// mechanism: hide the main access-key controls, reveal one panel, and relocate
// the error row under it. _resetAllLoginPanels restores the default state.
const _RECOVERY_PANELS = [
  'systemapi-login-panel', 'llm-login-panel', 'llm-reconnect-panel', 'llm-setpw-panel'
];
const _MAIN_LOGIN_CONTROLS = ['login-pw-input', 'login-btn', 'login-skip-sys-btn', 'login-server-btn'];

function _setMainControlsDisplay(show) {
  _MAIN_LOGIN_CONTROLS.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = show ? '' : 'none';
  });
  document.querySelectorAll('#login-overlay .login-box > .login-label, #login-overlay .login-box > .login-input-wrap')
    .forEach(el => { el.style.display = show ? '' : 'none'; });
}

function _resetAllLoginPanels() {
  _RECOVERY_PANELS.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  const dual = document.getElementById('llm-dual-sys-wrap');
  if (dual) dual.style.display = 'none';
  // restore the error row to its original spot (just above the systemapi panel)
  const anchor = document.getElementById('systemapi-login-panel');
  const err    = document.getElementById('login-error');
  if (anchor && err) anchor.before(err);
  _setMainControlsDisplay(true);
}
// Back-compat alias (called from _showLoginOverlay and the legacy flow).
function _resetSystemapiLoginPanel() { _resetAllLoginPanels(); }

function _showRecoveryPanel(panelId) {
  _setMainControlsDisplay(false);
  _RECOVERY_PANELS.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = (id === panelId ? 'block' : 'none');
  });
  const panel = document.getElementById(panelId);
  const err   = document.getElementById('login-error');
  if (panel && err) panel.after(err);   // errors belong under the related field
  _setLoginError(null);
}

function _showSystemapiLoginPanel() {
  _showRecoveryPanel('systemapi-login-panel');
  setTimeout(() => {
    const inp = document.getElementById('systemapi-pw-input');
    if (inp) { inp.value = ''; inp.focus(); }
  }, 60);
}
// Re-prompt for the systemapi password on-screen during a live session (the
// systemapi password changed mid-session). Reuses the login overlay's systemapi
// panel; the user is still chat-authenticated, so we don't re-boot on success.
function promptSystemapiLogin() {
  const overlay = document.getElementById('login-overlay');
  if (!overlay) return;
  overlay.classList.remove('hidden');
  document.getElementById('h-logout-btn').style.display = 'none';
  // _showSystemapiLoginPanel is idempotent, so repeated failures just keep the
  // panel in view.
  _showSystemapiLoginPanel();
}
function _hideLoginOverlay() {
  document.getElementById('login-overlay').classList.add('hidden');
  document.getElementById('h-logout-btn').style.display = 'flex';
}
function toggleLoginEye() {
  const inp = document.getElementById('login-pw-input');
  const btn = document.getElementById('login-eye-btn');
  if (inp.type === 'password') { inp.type = 'text'; btn.textContent = '\uD83D\uDE48'; }
  else { inp.type = 'password'; btn.textContent = '\uD83D\uDC41'; }
}
// Generic show/hide for the recovery-panel password fields (keeps the SVG icon \u2014
// just flips the input type, no emoji swap).
function toggleEye(inputId, _btnId) {
  const inp = document.getElementById(inputId);
  if (inp) inp.type = inp.type === 'password' ? 'text' : 'password';
}
function _setLoginLoading(v) {
  document.getElementById('login-btn').disabled = v;
  document.getElementById('login-dots').classList.toggle('hidden', !v);
}
// ── Red alarm glow on a logo (wrong password / errors) ──────────────
// Tints the cyan logo red + red halo. No transform (preserves centering).
function _flashLogoRed(el, count) {
  if (!el) return;
  count = count || 1;
  if (!document.getElementById('logo-red-burst-style')) {
    const st = document.createElement('style');
    st.id = 'logo-red-burst-style';
    st.textContent =
      '@keyframes logoBurstRed{' +
        '0%{opacity:.5;filter:hue-rotate(0deg) drop-shadow(0 0 45px rgba(0,229,255,.35))}' +
        '40%{opacity:1;filter:hue-rotate(160deg) saturate(1.8) brightness(1.45) drop-shadow(0 0 110px rgba(255,30,60,1)) drop-shadow(0 0 44px rgba(255,80,110,.95))}' +
        '100%{opacity:.5;filter:hue-rotate(0deg) drop-shadow(0 0 45px rgba(0,229,255,.35))}}';
    document.head.appendChild(st);
  }
  const dur = 0.5;
  const ms  = dur * count * 1000;
  el.classList.remove('glow');
  el.style.animation = 'none';
  el.style.zIndex = '40';            // lift above panels so the flash is visible
  void el.offsetWidth;               // reflow → restart animation
  el.style.animation = 'logoBurstRed ' + dur + 's ease ' + count;
  clearTimeout(el._redT);
  el._redT = setTimeout(function () { el.style.animation = ''; el.style.zIndex = ''; }, ms + 80);
}
function _setLoginError(msg) {
  const el    = document.getElementById('login-error');
  const msgEl = document.getElementById('login-error-msg');
  if (msg) {
    msgEl.textContent = msg; el.classList.add('visible');
    _flashLogoRed(document.querySelector('.login-bg-logo'), 2);
    const inp = document.getElementById('login-pw-input');
    inp.style.borderColor = 'rgba(255,60,90,.5)';
    inp.style.boxShadow   = '0 0 0 3px rgba(255,60,90,.1)';
    inp.focus(); inp.select();
  } else {
    el.classList.remove('visible');
    document.getElementById('login-pw-input').style.borderColor = '';
    document.getElementById('login-pw-input').style.boxShadow   = '';
  }
}
async function doLogin(skipSys) {
  const pw = document.getElementById('login-pw-input').value;
  if (!pw) { _setLoginError('enter password'); return; }
  _skipSystemapiOnLogin = !!skipSys;   // honored while resolving the response
  _systemapiSkipped     = !!skipSys;   // also gate live SSE systemapi-auth events
  _pendingLlmPass       = pw;          // reused for llm auto-retry after recovery
  _resetAllLoginPanels();
  _setLoginLoading(true); _setLoginError(null);
  try {
    const r = await fetch(B + '/api/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw })
    });
    const d = await r.json();
    if (r.ok && d.status === 'ok') {
      _saveToken(d.token);
      _resolveBackends(d);   // decides: boot, or show a recovery panel
    } else {
      const msgs = { wrong_password: 'wrong password', no_password_set: 'no password configured on server' };
      _setLoginError(msgs[d.message] || d.message || 'authentication failed');
    }
  } catch(e) {
    _setLoginError('server unreachable');
    document.getElementById('login-server-btn').classList.add('visible');
  }
  finally    { _setLoginLoading(false); }
}

// Main-screen "login & skip systemapi" button.
function doLoginSkipSys() { doLogin(true); }

// Decide what to do after /api/login succeeds. llm-server is MANDATORY, so it is
// resolved first; systemapi is skippable and keeps the prior behavior.
function _resolveBackends(d) {
  const llm = d['llm-server'];
  const sys = d.systemapi;
  _lastSysState = sys;          // remember for the post-llm-recovery step
  const pwInp = document.getElementById('login-pw-input');

  // ── llm-server (mandatory) ──
  if (llm === 'offline')         { _showLlmReconnectPanel(); return; }
  if (llm === 'no_password_set') { _showLlmSetPwPanel();     return; }
  if (llm === 'login_failed')    {
    _showLlmLoginPanel(sys === 'login_failed' && !_skipSystemapiOnLogin);
    return;
  }

  // ── llm-server OK → resolve systemapi (skippable) ──
  _afterLlmResolved(true);
}

// Resolve systemapi once llm-server is satisfied. Shared by the fresh-login path
// and every llm-server recovery path (set / sync / reconnect), so a systemapi
// password that differs from chat's still gets prompted instead of skipped.
// `fresh` only controls the "authenticated" toast on a clean first login.
function _afterLlmResolved(fresh) {
  const pwInp = document.getElementById('login-pw-input');
  if (_skipSystemapiOnLogin) {
    // best-effort: drop the backend's pending systemapi state, then boot
    fetch(B + '/apis/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': _authToken },
      body: JSON.stringify({ systemapi_pass: false })
    }).catch(() => {});
    if (pwInp) pwInp.value = '';
    _hideLoginOverlay(); toast('logged in without systemapi', 'warn'); _bootIfNeeded();
    return;
  }
  if (_lastSysState === 'login_failed') {
    if (pwInp) pwInp.value = '';
    _showSystemapiLoginPanel();   // keeps the overlay up to collect the systemapi pw
    return;
  }
  if (pwInp) pwInp.value = '';
  _hideLoginOverlay();
  if (_lastSysState === 'offline') toast('systemapi is offline', 'warn');
  else if (fresh)                  toast('authenticated', 'ok');
  _bootIfNeeded();
}

// ─── systemapi login (after systemapi_login_failed) ────────────────
async function doSystemapiLogin() {
  const spInp = document.getElementById('systemapi-pw-input');
  const sp = spInp.value;
  if (!sp) { _setLoginError('enter systemapi password'); spInp.focus(); return; }
  _setLoginLoading(true); _setLoginError(null);
  try {
    // raw fetch (not api()): a wrong systemapi pw returns 401, and api() would
    // wrongly treat any 401 as a chat-token failure and force a full logout.
    const r = await fetch(B + '/apis/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': _authToken },
      body: JSON.stringify({ systemapi_pass: sp })
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.status === 'ok') {
      _systemapiSkipped = false;   // reconnected — resume reacting to auth events
      document.getElementById('systemapi-pw-input').value = '';
      _hideLoginOverlay(); toast('systemapi connected', 'ok'); _bootIfNeeded();
    } else if (d.message === 'systemapi_offline') {
      _hideLoginOverlay(); toast('systemapi is offline', 'warn'); _bootIfNeeded();
    } else {
      _setLoginError('wrong systemapi password');
      const inp = document.getElementById('systemapi-pw-input');
      if (inp) { inp.focus(); inp.select(); }
    }
  } catch(e) {
    _setLoginError('server unreachable');
  }
  finally { _setLoginLoading(false); }
}

async function skipSystemapiLogin() {
  // best-effort: tell the backend to drop its pending systemapi-login state
  try {
    await fetch(B + '/apis/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': _authToken },
      body: JSON.stringify({ systemapi_pass: false })
    });
  } catch(e) {}
  _systemapiSkipped = true;   // remember the choice for this page session
  document.getElementById('systemapi-pw-input').value = '';
  _hideLoginOverlay(); toast('logged in without systemapi', 'warn'); _bootIfNeeded();
}

// ─── llm-server recovery: wrong password / dual-failure panel ──────
function _showLlmLoginPanel(dual) {
  _showRecoveryPanel('llm-login-panel');
  const w = document.getElementById('llm-dual-sys-wrap');
  if (w) w.style.display = dual ? 'block' : 'none';
  setTimeout(() => {
    const i = document.getElementById('llm-pw-input');
    if (i) { i.value = ''; i.focus(); }
    const s = document.getElementById('llm-dual-sys-input');
    if (s) s.value = '';
  }, 60);
}

// Submit the llm-server password (and optionally the systemapi password / skip)
// to /apis/login. llm-server is mandatory: once it is OK we boot regardless of
// systemapi (which may still need attention or be skipped).
async function _doLlmConnect(skipSys) {
  const lpInp = document.getElementById('llm-pw-input');
  const lp    = lpInp ? lpInp.value : '';
  if (!lp) { _setLoginError('enter llm-server password'); if (lpInp) lpInp.focus(); return; }

  const dualWrap    = document.getElementById('llm-dual-sys-wrap');
  const dualVisible = dualWrap && dualWrap.style.display !== 'none';
  const body = { llmserver_pass: lp };
  if (skipSys)            body.systemapi_pass = false;
  else if (dualVisible)   {
    const sp = document.getElementById('llm-dual-sys-input').value;
    if (sp) body.systemapi_pass = sp;
  }

  _setLoginLoading(true); _setLoginError(null);
  try {
    // raw fetch (not api()): a wrong password returns 401, and api() would treat
    // any 401 as a chat-token failure and force a full logout.
    const r = await fetch(B + '/apis/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': _authToken },
      body: JSON.stringify(body)
    });
    const d = await r.json().catch(() => ({}));
    const llm = d['llm-server'];

    if (llm === 'ok') {
      _pendingLlmPass = lp;
      // Explicit skip → drop systemapi and boot.
      if (skipSys) {
        _skipSystemapiOnLogin = true; _systemapiSkipped = true;
        _hideLoginOverlay(); toast('llm-server connected', 'ok'); _bootIfNeeded();
        return;
      }
      // Dual panel: a systemapi password was submitted alongside → honor that
      // fresh result; otherwise fall back to the state remembered from /api/login.
      if (dualVisible && d.systemapi === 'login_failed') {
        toast('llm-server connected', 'ok');
        _showSystemapiLoginPanel();
        return;
      }
      if (dualVisible && d.systemapi) _lastSysState = d.systemapi;
      toast('llm-server connected', 'ok');
      _afterLlmResolved(false);   // single, shared systemapi resolution
      return;
    }
    if (llm === 'no_password_set')                       { _showLlmSetPwPanel();     return; }
    if (llm === 'offline' || d.message === 'llmserver_offline') { _showLlmReconnectPanel(); return; }
    _setLoginError('wrong llm-server password');
    if (lpInp) { lpInp.focus(); lpInp.select(); }
  } catch(e) {
    _setLoginError('server unreachable');
  } finally { _setLoginLoading(false); }
}
function connectLlm()        { _doLlmConnect(false); }
function connectLlmSkipSys() { _doLlmConnect(true);  }

// ─── llm-server recovery: offline → reconnect panel ────────────────
function _setSchemeToggle(scheme) {
  const btn = document.getElementById('llm-rc-scheme');
  if (!btn) return;
  const https = scheme === 'https';
  btn.textContent = https ? 'HTTPS' : 'HTTP';
  btn.classList.toggle('https', https);
  btn.dataset.scheme = https ? 'https' : 'http';
}
function toggleLlmScheme() {
  const btn = document.getElementById('llm-rc-scheme');
  if (!btn) return;
  _setSchemeToggle(btn.dataset.scheme === 'https' ? 'http' : 'https');
}
function _showLlmReconnectPanel() {
  _showRecoveryPanel('llm-reconnect-panel');
  const ip   = document.getElementById('llm-rc-ip');
  const port = document.getElementById('llm-rc-port');
  if (ip)   ip.value   = _cfg.llm_host || '';
  if (port) port.value = _cfg.llm_http || 8112;
  _setSchemeToggle(_cfg.llm_scheme || 'http');
  setTimeout(() => { if (ip) ip.focus(); }, 60);
}
async function doLlmReconnect() {
  const ip     = document.getElementById('llm-rc-ip').value.trim();
  const port   = parseInt(document.getElementById('llm-rc-port').value) || 8112;
  const scheme = document.getElementById('llm-rc-scheme').dataset.scheme || 'http';
  if (!ip) { _setLoginError('enter llm-server IP'); return; }
  _setLoginLoading(true); _setLoginError(null);
  try {
    const r = await fetch(B + '/reconnect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': _authToken },
      body: JSON.stringify({ 'llm-server': [scheme, ip, port] })
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.status === 'ok') {
      Object.assign(_cfg, { llm_host: ip, llm_http: port, llm_scheme: scheme });
      localStorage.setItem(NET_KEY, JSON.stringify(_cfg));
      toast('llm-server reconnected', 'ok');
      await _autoLlmLoginAndBoot();   // reuse the password already typed
    } else {
      _setLoginError(d.message || 'cannot reach llm-server');
    }
  } catch(e) {
    _setLoginError('server unreachable');
  } finally { _setLoginLoading(false); }
}
// After a successful reconnect / set / sync, log into llm-server with the stored
// password and boot. Falls back to the password panel if that isn't possible.
async function _autoLlmLoginAndBoot() {
  if (!_pendingLlmPass) { _showLlmLoginPanel(false); return; }
  try {
    const r = await fetch(B + '/apis/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': _authToken },
      body: JSON.stringify({ llmserver_pass: _pendingLlmPass })
    });
    const d = await r.json().catch(() => ({}));
    if (d['llm-server'] === 'ok')              { _afterLlmResolved(false); return; }
    if (d['llm-server'] === 'no_password_set') { _showLlmSetPwPanel(); return; }
    _showLlmLoginPanel(false);
  } catch(e) {
    _showLlmLoginPanel(false);
  }
}

// ─── llm-server recovery: no password set → set / sync panel ───────
function _showLlmSetPwPanel() {
  _showRecoveryPanel('llm-setpw-panel');
  setTimeout(() => {
    const i = document.getElementById('llm-setpw-input');
    if (i) { i.value = ''; i.focus(); }
  }, 60);
}
async function setLlmPassword() {
  const inp = document.getElementById('llm-setpw-input');
  const pw  = inp ? inp.value : '';
  if (!pw) { _setLoginError('enter a password'); if (inp) inp.focus(); return; }
  _setLoginLoading(true); _setLoginError(null);
  try {
    const r = await fetch(B + '/set_password?password=' + encodeURIComponent(pw), {
      method: 'POST', headers: { 'X-Auth-Token': _authToken }
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.status === 'ok') {
      _pendingLlmPass = pw;
      toast('llm-server password set', 'ok');
      _afterLlmResolved(false);   // now prompt for systemapi if its pw differs
    } else if (r.status === 409) {
      _setLoginError('password already set — log in instead');
    } else {
      _setLoginError(d.message || 'could not set password');
    }
  } catch(e) {
    _setLoginError('server unreachable');
  } finally { _setLoginLoading(false); }
}
async function syncLlmPassword() {
  _setLoginLoading(true); _setLoginError(null);
  try {
    // no query → backend reuses the password it already holds in memory
    const r = await fetch(B + '/set_password', {
      method: 'POST', headers: { 'X-Auth-Token': _authToken }
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.status === 'ok') {
      toast('password synced with chat server', 'ok');
      _afterLlmResolved(false);   // now prompt for systemapi if its pw differs
    } else if ((d.message || '').includes('No password stored in memory')) {
      _setLoginError('no stored password — type one above, or re-login first');
    } else if (r.status === 409) {
      _setLoginError('password already set — log in instead');
    } else {
      _setLoginError(d.message || 'sync failed');
    }
  } catch(e) {
    _setLoginError('server unreachable');
  } finally { _setLoginLoading(false); }
}

// "← back" link on every recovery panel → return to the main access-key form.
function backToMainLogin() {
  _resetAllLoginPanels();
  setTimeout(() => { const i = document.getElementById('login-pw-input'); if (i) i.focus(); }, 60);
}

// Boot the app on first login, but skip a re-boot when the systemapi prompt was
// shown mid-session (SSE already connected) — that would duplicate the stream.
function _bootIfNeeded() {
  if (_sseSource) return;
  _bootApp();
}

// ─── Login: Server Settings (custom chat server IP/port) ──────────
function toggleLoginServerSettings() {
  const panel = document.getElementById('login-server-panel');
  const opening = !panel.classList.contains('open');
  panel.classList.toggle('open', opening);
  if (opening) {
    document.getElementById('login-server-ip').value   = _cfg.chat_host;
    document.getElementById('login-server-port').value = _cfg.chat_port;
    setTimeout(() => document.getElementById('login-server-ip').focus(), 60);
  }
}

function applyLoginServerSettings() {
  const host = document.getElementById('login-server-ip').value.trim();
  const port = parseInt(document.getElementById('login-server-port').value) || 8900;
  if (!host) { _setLoginError('enter server IP'); return; }
  Object.assign(_cfg, { chat_host: host, chat_port: port });
  B = _buildBaseUrl(_cfg.chat_host, _cfg.chat_port);
  localStorage.setItem(NET_KEY, JSON.stringify(_cfg));
  _updateHeaderUrl();
  _setLoginError(null);
  document.getElementById('login-server-panel').classList.remove('open');
  toast('chat server → ' + B, 'ok');
  setTimeout(() => document.getElementById('login-pw-input').focus(), 60);
}
function doLogout() {
  _clearToken();
  _autoLoadDone = false;
  if (typeof _sseSource !== 'undefined' && _sseSource) {
    try { _sseSource.close(); } catch {}
    _sseSource = null;
  }
  setConn(false); clearChat(); _showLoginOverlay(); toast('logged out', 'warn');
}

// ─── Network config ───────────────────────────────────────────────
const NET_KEY = 'frtgg_network_config';
function _defaultNet() {
  return {
    chat_host:  location.hostname || '127.0.0.1',
    chat_port:  parseInt(location.port) || 8900,
    llm_host:   '192.168.100.20', llm_tcp: 8111, llm_http: 8112, llm_scheme: 'http',
    sys_host:   '127.0.0.1', sys_port: 8300, audio_port: 8801
  };
}
// Parse URL overrides once on load
(function() {
  const p = new URLSearchParams(location.search);
  const host = p.get('host'), port = p.get('port');
  if (host || port) {
    const saved = JSON.parse(localStorage.getItem(NET_KEY) || '{}');
    if (host) saved.chat_host = host;
    if (port) saved.chat_port = parseInt(port);
    localStorage.setItem(NET_KEY, JSON.stringify(saved));
  }
})();
let _cfg = Object.assign(_defaultNet(), JSON.parse(localStorage.getItem(NET_KEY) || 'null') || {});
let B    = `http://${_cfg.chat_host}:${_cfg.chat_port}`;

function _buildBaseUrl(host, port) { return `http://${host}:${port}`; }
function _updateHeaderUrl() {
  const el  = document.getElementById('h-server-url');
  if (el)  el.textContent = `${_cfg.chat_host}:${_cfg.chat_port}`;
  const cur = document.getElementById('current-chat-url');
  if (cur) cur.textContent = B;
}

// ─── Shared state ─────────────────────────────────────────────────
let commands         = [];
let currentSession   = null;
let settingsMeta     = null;
let sseConnected     = false;
let _isGenerating    = false;
let _autoSaveEnabled = false;
let _sseSource       = null;
let _devRefreshInterval = null;

// ─── Save-Think state ─────────────────────────────────────────────
let _saveThinking        = true;
let _currentSessionName  = null;

function _updateSaveThinkBtn() {
  const btn = document.getElementById('save-think-btn');
  if (!btn) return;
  btn.classList.toggle('think-on', _saveThinking);
  btn.textContent = _saveThinking ? 'THINK SAVE: ON' : 'THINK SAVE: OFF';
  btn.title = _saveThinking
    ? 'Save thinking is ON — click to disable'
    : 'Save thinking is OFF — click to enable';
}

async function _fetchSaveThinkState() {
  try {
    const d = await api('/api/sessions', null, 'GET');
    _currentSessionName = d.current_session || null;
    _saveThinking       = d.save_thinking   || false;
    _updateSaveThinkBtn();
  } catch(e) {
    console.warn('[save-think] could not fetch state:', e.message);
  }
}

async function toggleSaveThink() {
  if (!_currentSessionName) {
    toast('no active session — load a session first', 'warn');
    return;
  }
  const newVal = !_saveThinking;
  try {
    const d = await api('/api/llm/save-thinking', {
      save:         newVal,
      session_name: _currentSessionName
    });
    if (d.status === 'ok') {
      _saveThinking = newVal;
      _updateSaveThinkBtn();
      toast(`think saving ${newVal ? 'ON' : 'OFF'}`, newVal ? 'ok' : 'warn');
    } else {
      toast(d.message || 'failed to update', 'err');
    }
  } catch(e) {
    toast('error: ' + e.message, 'err');
  }
}

// ─── API helper ───────────────────────────────────────────────────
async function api(path, body, method) {
  const m = method || (body ? 'POST' : 'GET');
  const headers = {};
  if (body)        headers['Content-Type']  = 'application/json';
  if (_authToken)  headers['X-Auth-Token']  = _authToken;
  const r = await fetch(B + path, {
    method: m, headers,
    body: body ? JSON.stringify(body) : undefined
  });
  if (r.status === 401) { _clearToken(); _showLoginOverlay(); throw new Error('unauthorized'); }
  if (!r.ok) {
    let msg = r.statusText;
    try { const d = await r.json(); msg = d.message || d.error || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

// ─── Toast ────────────────────────────────────────────────────────
function toast(msg, type = 'info') {
  if (type === 'err') _flashLogoRed(document.getElementById('app-bg-logo'), 2);
  const el = document.createElement('div');
  el.className = `toast ${type}`; el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => {
    el.style.opacity    = '0';
    el.style.transition = 'opacity .3s';
    setTimeout(() => el.remove(), 300);
  }, 3000);
}

// ─── Utils ────────────────────────────────────────────────────────
function setConn(v) {
  sseConnected = v;
  document.getElementById('connDot').className = 'conn-dot' + (v ? ' live' : '');
  document.getElementById('connTxt').textContent = v ? 'live' : 'connecting\u2026';
}
function now() {
  const d = new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map(n => String(n).padStart(2, '0')).join(':');
}
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function eJs(s) { return s.replace(/\\/g, '\\\\').replace(/'/g, "\\'"); }

// ─── Tab switching ────────────────────────────────────────────────
function switchTab(name, btn) {
  document.querySelectorAll('.h-pill').forEach(b  => b.classList.remove('active'));
  document.querySelectorAll('.rpanel').forEach(p  => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(`rp-${name}`).classList.add('active');
  if (name === 'sessions')  loadSessionList();
  if (name === 'commands')  loadCommands();
  if (name === 'settings')  { loadSettings(); refreshModelStatus(); loadSavedModels(); }
  if (name === 'prompt')    loadPrompt();
  if (name === 'network')   loadNetworkConfig();
  if (name === 'models')    loadSavedModels();
  if (name === 'developer') devRefresh();
  if (name === 'gpus')     loadGpuSystem();
  if (name === 'cmdsessions' && typeof startCmdPolling === 'function') startCmdPolling();
}

// ─── Quick Save Think state ────────────────────────────────────────
let _quickThinkVal = null;

function setQuickThink(val, btn) {
  _quickThinkVal = val === 'auto' ? null : val === 'on';
  const hints = {
    auto: 'AUTO — llm-server will detect on first load',
    off:  'OFF — model does not support thinking',
    on:   'ON  — model supports thinking'
  };
  const colors = {
    auto: { bg:'rgba(255,179,0,.15)',  color:'var(--amber)' },
    off:  { bg:'rgba(255,60,90,.12)',  color:'var(--red)'   },
    on:   { bg:'rgba(57,255,126,.12)', color:'var(--green)' }
  };
  document.querySelectorAll('#qs-think-seg button').forEach(b => {
    b.style.background = 'var(--s2)';
    b.style.color      = 'var(--muted)';
  });
  btn.style.background = colors[val].bg;
  btn.style.color      = colors[val].color;
  const hint = document.getElementById('qs-think-hint');
  if (hint) hint.textContent = hints[val];
}

async function setModelThinking(path, val) {
  try {
    const d = await api('/api/saved-models/set-thinking', {
      path,
      supports_thinking: val
    });
    if (d.status === 'ok') {
      const label = val === null ? 'AUTO' : val ? 'ON' : 'OFF';
      toast(`thinking set to ${label}`, 'ok');
      loadSavedModels();
    } else {
      toast(d.message || 'failed', 'err');
    }
  } catch(e) {
    toast('error: ' + e.message, 'err');
  }
}

function _thinkBadgeHtml(path, supportsThinking) {
  const state = (supportsThinking === null || supportsThinking === undefined)
    ? 'auto' : supportsThinking ? 'on' : 'off';
  const styles = {
    auto: 'background:rgba(255,179,0,.1);color:var(--amber);border:1px solid rgba(255,179,0,.3)',
    off:  'background:rgba(255,60,90,.08);color:var(--red);border:1px solid rgba(255,60,90,.25)',
    on:   'background:rgba(57,255,126,.1);color:var(--green);border:1px solid rgba(57,255,126,.25)'
  };
  const labels   = { auto:'THINK: AUTO', off:'THINK: OFF', on:'THINK: ON' };
  const nextVals = { auto: false, off: true, on: null };
  const nextVal  = JSON.stringify(nextVals[state]);
  return `<button
    onclick="setModelThinking('${eJs(path)}',${nextVal})"
    style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:1px;cursor:pointer;font-family:var(--mono);flex-shrink:0;${styles[state]}"
    title="click to cycle: AUTO -> OFF -> ON -> AUTO"
    >${labels[state]}</button>`;
}

// ─── Load Path Think state ─────────────────────────────────────────
let _loadPathThinkVal = null;

function setLoadPathThink(val, btn) {
  _loadPathThinkVal = val === 'auto' ? null : val === 'on';
  const hints = {
    auto: 'AUTO — llm-server will detect on first load',
    off:  'OFF — model does not support thinking',
    on:   'ON  — model supports thinking'
  };
  const colors = {
    auto: { bg:'rgba(255,179,0,.15)',  color:'var(--amber)' },
    off:  { bg:'rgba(255,60,90,.12)',  color:'var(--red)'   },
    on:   { bg:'rgba(57,255,126,.12)', color:'var(--green)' }
  };
  document.querySelectorAll('#lp-think-seg button').forEach(b => {
    b.style.background = 'var(--s2)';
    b.style.color      = 'var(--muted)';
  });
  btn.style.background = colors[val].bg;
  btn.style.color      = colors[val].color;
  const hint = document.getElementById('lp-think-hint');
  if (hint) hint.textContent = hints[val];
}

// ─── Sidebar 3-state cycle: normal → wide → hidden → normal ───────
const _SIDEBAR_KEY    = 'frtgg_sidebar_state';
const _SIDEBAR_STATES = ['normal', 'wide', 'hidden'];
let _sidebarState = localStorage.getItem(_SIDEBAR_KEY);
// migrate the old boolean collapse key
if (!_sidebarState) {
  const _old = localStorage.getItem('frtgg_sidebar_collapsed');
  _sidebarState = _old === '1' ? 'hidden' : 'normal';
}
if (!_SIDEBAR_STATES.includes(_sidebarState)) _sidebarState = 'normal';

function _applySidebarState(animated) {
  const rp     = document.getElementById('right-panel');
  const icon   = document.getElementById('sidebar-toggle-icon');
  const toggle = document.getElementById('sidebar-toggle');
  if (!rp) return;
  if (!animated) rp.style.transition = 'none';
  rp.classList.toggle('wide',      _sidebarState === 'wide');
  rp.classList.toggle('collapsed', _sidebarState === 'hidden');
  const _SVG = (d) => '<svg class="ic" width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + '</svg>';
  const icons = {
    normal: _SVG('<path d="M10 3 5 8l5 5"/><path d="M14 3 9 8l5 5"/>'),
    wide:   _SVG('<path d="M6 3l5 5-5 5"/>'),
    hidden: _SVG('<path d="M10 3 5 8l5 5"/>')
  };
  const titles = { normal: 'Expand sidebar', wide: 'Hide sidebar', hidden: 'Show sidebar' };
  if (icon)   icon.innerHTML = icons[_sidebarState];
  if (toggle) {
    toggle.setAttribute('aria-expanded', _sidebarState === 'hidden' ? 'false' : 'true');
    toggle.title = titles[_sidebarState];
  }
  if (!animated) requestAnimationFrame(() => { rp.style.transition = ''; });
}

function _isNarrowScreen() {
  return window.matchMedia('(max-width: 820px)').matches;
}

function toggleSidebar() {
  // On narrow/portrait screens the panel is a full overlay, so "wide" is
  // meaningless — cycle only between shown (normal) and hidden.
  const states = _isNarrowScreen() ? ['normal', 'hidden'] : _SIDEBAR_STATES;
  const i = states.indexOf(_sidebarState);
  _sidebarState = states[(i + 1) % states.length];
  localStorage.setItem(_SIDEBAR_KEY, _sidebarState);
  _applySidebarState(true);
}

// ─── Prompt Expand Overlay ────────────────────────────────────────
let _expandOpener = null; // focus restoration

function openPromptExpand() {
  const overlay = document.getElementById('prompt-expand-overlay');
  const field   = document.getElementById('prompt-expand-field');
  const main    = document.getElementById('input-field');
  if (!overlay || !field) return;
  _expandOpener = document.activeElement;
  // Copy existing text from main input
  field.value = main ? main.value : '';
  _updateExpandCharCount();
  overlay.classList.add('visible');
  setTimeout(() => field.focus(), 80);
}

function closePromptExpand() {
  const overlay = document.getElementById('prompt-expand-overlay');
  if (overlay) overlay.classList.remove('visible');
  // restore focus to triggering element
  if (_expandOpener && _expandOpener.focus) {
    setTimeout(() => _expandOpener.focus(), 50);
  }
  _expandOpener = null;
}

function sendFromExpand() {
  const field = document.getElementById('prompt-expand-field');
  const main  = document.getElementById('input-field');
  if (!field || !main) return;
  main.value = field.value;
  closePromptExpand();
  if (!_isGenerating) sendMsg();
}

function _updateExpandCharCount() {
  const field = document.getElementById('prompt-expand-field');
  const label = document.getElementById('prompt-expand-charcount');
  if (field && label) label.textContent = field.value.length.toLocaleString() + ' chars';
}

// ─── Prompt Text Overlay (current prompt read / new prompt compose) ──
let _ptOverlayMode   = 'read'; // 'read' | 'new-prompt'
let _ptOverlayOpener = null;   // focus restoration

function _openPromptTextOverlay(title, text, mode) {
  _ptOverlayMode   = mode || 'read';
  _ptOverlayOpener = document.activeElement;
  const overlay   = document.getElementById('prompt-text-overlay');
  const area      = document.getElementById('prompt-text-area');
  const titleEl   = document.getElementById('ptoverlay-title');
  const saveBtn   = document.getElementById('ptoverlay-save-btn');
  const hintEl    = document.getElementById('ptoverlay-hint');
  if (!overlay || !area) return;

  area.value      = text || '';
  area.readOnly   = (_ptOverlayMode === 'read');
  titleEl.textContent = title || 'prompt';

  if (_ptOverlayMode === 'read') {
    saveBtn.style.display = 'none';
    hintEl.textContent    = 'read-only · Esc to close';
  } else {
    saveBtn.style.display = '';
    hintEl.textContent    = 'Ctrl+Enter to apply · Esc to close';
  }

  _updatePtOverlayCount();
  overlay.classList.add('visible');
  setTimeout(() => area.focus(), 80);
}

function _updatePtOverlayCount() {
  const area  = document.getElementById('prompt-text-area');
  const label = document.getElementById('ptoverlay-charcount');
  if (area && label) label.textContent = area.value.length.toLocaleString() + ' chars';
}

function openCurrentPromptExpand() {
  const text = document.getElementById('prompt-view')?.textContent?.trim() || '';
  _openPromptTextOverlay('current system prompt', text, 'read');
}

function openNewPromptExpand() {
  const text = document.getElementById('new-prompt-text')?.value || '';
  _openPromptTextOverlay('edit prompt text', text, 'new-prompt');
}

function savePromptTextOverlay() {
  const area = document.getElementById('prompt-text-area');
  if (!area) return;
  if (_ptOverlayMode === 'new-prompt') {
    const target = document.getElementById('new-prompt-text');
    if (target) { target.value = area.value; }
  }
  closePromptTextOverlay();
}

function copyPromptText() {
  const area = document.getElementById('prompt-text-area');
  if (!area) return;
  navigator.clipboard.writeText(area.value).then(() => {
    toast('Copied to clipboard', 'ok');
  }).catch(() => {
    // fallback
    area.select();
    document.execCommand('copy');
    toast('Copied', 'ok');
  });
}

function closePromptTextOverlay() {
  document.getElementById('prompt-text-overlay')?.classList.remove('visible');
  if (_ptOverlayOpener && _ptOverlayOpener.focus) {
    setTimeout(() => _ptOverlayOpener.focus(), 50);
  }
  _ptOverlayOpener = null;
}

// Keyboard wiring for ptoverlay (set up once on DOMContentLoaded)
document.addEventListener('DOMContentLoaded', () => {
  const area = document.getElementById('prompt-text-area');
  if (area) {
    area.addEventListener('input', _updatePtOverlayCount);
    area.addEventListener('keydown', e => {
      if (e.key === 'Escape') { closePromptTextOverlay(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        if (_ptOverlayMode !== 'read') savePromptTextOverlay();
      }
    });
  }
  document.getElementById('prompt-text-overlay')?.addEventListener('mousedown', e => {
    if (e.target === document.getElementById('prompt-text-overlay')) closePromptTextOverlay();
  });
});

// Auto-load the server's active session on login so the chat opens on the
// running conversation instead of an empty screen. Called once from the SSE
// onopen handler (so the session_history broadcast isn't missed). A short delay
// lets any in-flight generation announce itself first — we never reload over a
// live stream, since a same-session reload would stop it server-side.
function _maybeAutoLoadSession() {
  if (_autoLoadDone) return;
  _autoLoadDone = true;
  setTimeout(async () => {
    if (_isGenerating || (typeof _streamBubble !== 'undefined' && _streamBubble)) return;
    try {
      const d   = await api('/api/sessions', null, 'GET');
      const cur = d && d.current_session;
      if (!cur) return;                                   // no active session
      if (_isGenerating || (typeof _streamBubble !== 'undefined' && _streamBubble)) return;
      if (typeof loadSession === 'function') loadSession(cur);
    } catch (e) { /* leave the chat empty on failure */ }
  }, 500);
}

// ─── Boot ─────────────────────────────────────────────────────────
function _bootApp() {
  _autoLoadDone = false;   // a fresh login should auto-load again
  if (typeof _initChatScrollWatch === 'function') _initChatScrollWatch();
  connectSSE();
  loadSessionList();
  loadCommands();
  loadNetworkConfig();
  refreshModelStatus();
  loadSavedModels();
  _fetchSaveThinkState();
  _devRefreshInterval = setInterval(() => {
    if (!document.hidden) devRefresh();
  }, 10000);

  // ── Apply sidebar state without animation on boot ──
  // On narrow/portrait screens start with the chat visible (panel slid away).
  if (_isNarrowScreen() && _sidebarState !== 'hidden') _sidebarState = 'hidden';
  _applySidebarState(false);

  // ── Prompt expand overlay wiring ──
  const expandField = document.getElementById('prompt-expand-field');
  if (expandField) {
    expandField.addEventListener('input', _updateExpandCharCount);
    expandField.addEventListener('keydown', e => {
      if (e.key === 'Escape') { closePromptExpand(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); sendFromExpand(); }
    });
  }
  document.getElementById('prompt-expand-overlay')?.addEventListener('mousedown', e => {
    if (e.target === document.getElementById('prompt-expand-overlay')) closePromptExpand();
  });

  // ── Input field: Enter/Shift+Enter + auto-resize + draft persistence ──
  const field = document.getElementById('input-field');
  if (field) {
    field.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!_isGenerating) sendMsg();
      }
    });

    field.addEventListener('input', () => {
      field.style.height = 'auto';
      field.style.height = Math.min(field.scrollHeight, 300) + 'px';
      // Save the in-progress message so a refresh doesn't lose it.
      if (field.value) localStorage.setItem(DRAFT_KEY, field.value);
      else             localStorage.removeItem(DRAFT_KEY);
    });

    // Restore a draft left over from a previous page load.
    const draft = localStorage.getItem(DRAFT_KEY);
    if (draft) {
      field.value = draft;
      field.style.height = 'auto';
      field.style.height = Math.min(field.scrollHeight, 300) + 'px';
    }
  }
}

// ─── Init: auto-login from a stored token on page load ────────────
// The login overlay is shown by default. If a token from a previous session is
// still stored (localStorage), validate it against the chat server and boot
// straight in — so a refresh / reopening the tab doesn't re-prompt for the
// password. Runs on DOMContentLoaded so chat.js / dev.js / prompt.js functions
// (connectSSE, loadSessionList, …) that _bootApp calls are already defined.
async function _init() {
  if (!_authToken) { _showLoginOverlay(); return; }
  try {
    const r = await fetch(B + '/api/sessions', { headers: { 'X-Auth-Token': _authToken } });
    if (r.status === 401) { _clearToken(); _showLoginOverlay(); return; }  // token rejected
    if (!r.ok)            { _showLoginOverlay(); return; }                 // server error → let user retry
    _hideLoginOverlay();
    _bootApp();
  } catch {
    _showLoginOverlay();   // server unreachable — show login so the user can retry / change server
  }
}
document.addEventListener('DOMContentLoaded', _init);

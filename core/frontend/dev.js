// ══════════════════════════════════════════════════════════════════
//  dev.js  —  sessions · settings · models · developer panel
// ══════════════════════════════════════════════════════════════════

// ─── Sessions ─────────────────────────────────────────────────────
async function loadSessionList() {
  try {
    const d  = await api('/api/sessions', null, 'GET');
    currentSession   = d.current_session;
    const el = document.getElementById('session-list');
    if (!d.sessions || !d.sessions.length) {
      el.innerHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:20px">No saved sessions</div>';
      return;
    }
    el.innerHTML = d.sessions.map(s => `
      <div class="sess-item ${s.name === currentSession ? 'active' : ''}" onclick="loadSession('${eJs(s.name)}')">
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
            <span class="sess-name">${esc(s.name)}</span>
            ${s.name === currentSession ? '<span class="sess-badge">active</span>' : ''}
          </div>
          <div class="sess-meta">${esc(s.saved_at)} · ${esc(String(s.turn_count))} turns</div>
        </div>
        <div class="sess-actions">
          <button class="btn btn-amber" onclick="event.stopPropagation();clearSession('${eJs(s.name)}')" style="padding:4px 8px;font-size:11px">clear</button>
          <button class="btn btn-red" onclick="event.stopPropagation();delSession('${eJs(s.name)}')" style="padding:4px 8px;font-size:11px">del</button>
        </div>
      </div>`).join('');
  } catch(e) {
    document.getElementById('session-list').innerHTML =
      `<div style="color:var(--red);font-size:12px;padding:20px">Error: ${esc(e.message)}</div>`;
  }
}
async function saveSession() {
  const name = document.getElementById('sess-name').value.trim();
  if (!name) { toast('Enter session name', 'warn'); return; }
  try {
    const r = await api('/api/sessions/save', { name });
    if (r.status === 'ok') { toast(r.message || 'Saved', 'ok'); document.getElementById('sess-name').value = ''; loadSessionList(); }
    else toast(r.message || r.error, 'err');
  } catch(e) { toast(e.message, 'err'); }
}
async function loadSession(name) {
  try {
    const r = await api('/api/sessions/load', { name });
    if (r.status !== 'ok') toast(r.message || r.error, 'err');
    // success toast + session-list refresh arrive via the SSE 'session_loaded' event
  } catch(e) { toast(e.message, 'err'); }
}
async function newSession() {
  const saveName = document.getElementById('sess-name').value.trim();
  const body     = saveName ? { save_current_as: saveName } : {};
  if (!confirm(saveName ? `Save current as "${saveName}" and start new?` : 'Start new session? Current history cleared.')) return;
  _expectingNewSession = true;
  try {
    const r = await api('/api/sessions/new', body);
    if (r.status === 'ok') {
      toast(r.message || 'New session', 'info');
      document.getElementById('sess-name').value = '';
      clearChat(); appendDivider('NEW SESSION'); loadSessionList();
    } else { _expectingNewSession = false; toast(r.message || r.error, 'err'); }
  } catch(e) { _expectingNewSession = false; toast(e.message, 'err'); }
}
async function clearSession(name) {
  if (!confirm(`Clear history of "${name}"? This cannot be undone.`)) return;
  try {
    const r = await api('/api/sessions/clear', { name });
    if (r.status === 'ok') { toast(r.message || 'History cleared', 'warn'); loadSessionList(); }
    else toast(r.message || r.error, 'err');
  } catch(e) { toast(e.message, 'err'); }
}
async function delSession(name) {
  if (!confirm(`Delete "${name}"? Cannot be undone.`)) return;
  try {
    const r = await api('/api/sessions/delete', { name });
    if (r.status === 'ok') { toast(r.message || 'Deleted', 'warn'); loadSessionList(); }
    else toast(r.message || r.error, 'err');
  } catch(e) { toast(e.message, 'err'); }
}

// ─── Settings ─────────────────────────────────────────────────────
async function loadSettings() {
  try {
    settingsMeta = await api('/api/llm/settings', null, 'GET');
    renderSettings();
      loadCachePrompt();
  } catch(e) {
    document.getElementById('settings-body').innerHTML =
      '<div style="color:var(--red);font-size:12px;padding:20px">LLM server unreachable</div>';
  }
}
function renderSettings() {
  if (!settingsMeta) return;
  const cur = settingsMeta.current || {}, bnd = settingsMeta.bounds || {};
  const re  = settingsMeta.valid_reasoning_efforts || ['none','low','medium','high'];
  const fields = [
    { key:'temperature',      label:'Temperature',      type:'number', step:'.05', ...(bnd.temperature      ? {min:bnd.temperature.min,      max:bnd.temperature.max}      : {}) },
    { key:'top_p',            label:'Top-P',            type:'number', step:'.05', ...(bnd.top_p            ? {min:bnd.top_p.min,            max:bnd.top_p.max}            : {}) },
    { key:'top_k',            label:'Top-K',            type:'number', step:'1',   ...(bnd.top_k            ? {min:bnd.top_k.min,            max:bnd.top_k.max}            : {}) },
    { key:'min_p',            label:'Min-P',            type:'number', step:'.01', ...(bnd.min_p            ? {min:bnd.min_p.min,            max:bnd.min_p.max}            : {}) },
    { key:'presence_penalty', label:'Presence Penalty', type:'number', step:'.1',  ...(bnd.presence_penalty ? {min:bnd.presence_penalty.min, max:bnd.presence_penalty.max} : {}) },
    { key:'repeat_penalty',   label:'Repeat Penalty',   type:'number', step:'.05', ...(bnd.repeat_penalty   ? {min:bnd.repeat_penalty.min,   max:bnd.repeat_penalty.max}   : {}) },
    { key:'max_tokens',       label:'Max Tokens',       type:'number', step:'64',  ...(bnd.max_tokens       ? {min:bnd.max_tokens.min,       max:bnd.max_tokens.max}       : {}) },
    { key:'reasoning_effort', label:'Reasoning Effort', type:'select', options:re },
  ];
  document.getElementById('settings-body').innerHTML = fields.map(f => {
    const val = cur[f.key] ?? '';
    if (f.type === 'select') {
      const opts = f.options.map(o => `<option value="${esc(o)}" ${o===val?'selected':''}>${esc(o)}</option>`).join('');
      return `<div class="setting-row"><label class="setting-label">${esc(f.label)}</label><select class="setting-input" id="st-${f.key}">${opts}</select></div>`;
    }
    return `<div class="setting-row">
      <label class="setting-label">${esc(f.label)}${f.min!=null?`<span style="color:var(--muted);font-size:9px"> (${f.min}–${f.max})</span>`:''}</label>
      <input class="setting-input" id="st-${f.key}" type="${f.type}" value="${esc(String(val))}"
        ${f.step ? `step="${f.step}"` : ''} ${f.min!=null ? `min="${f.min}" max="${f.max}"` : ''}>
    </div>`;
  }).join('');
}
async function applySettings() {
  const keys    = ['model_path','temperature','top_p','top_k','min_p','presence_penalty','repeat_penalty','max_tokens','reasoning_effort'];
  const payload = {};
  for (const k of keys) { const el = document.getElementById('st-'+k); if (el) payload[k] = el.value; }
  try {
    const r = await api('/api/llm/settings', payload);
    if (r.status === 'ok') toast(r.message || 'Applied', 'ok');
    else toast(r.message || r.error, 'err');
  } catch(e) { toast(e.message, 'err'); }
}
function toggleSettingsBody() {
  const body = document.getElementById('settings-body');
  const btn  = document.getElementById('settings-toggle-btn');
  if (!body) return;
  const wasHidden = body.style.display === 'none';
  body.style.display = wasHidden ? 'flex' : 'none';
  if (btn) {
    const triUp   = '<svg class="ic" width="10" height="10" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 4.5l5 7H3z"/></svg>';
    const triDown = '<svg class="ic" width="10" height="10" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M3 5.5h10L8 12z"/></svg>';
    btn.innerHTML = (wasHidden ? triUp + ' hide' : triDown + ' show');
  }
  // when collapsed, let the model / saved-models section expand into the freed space
  const collapsed = !wasHidden;
  const sec  = document.querySelector('#rp-settings .model-loader-section');
  const list = document.getElementById('saved-models-list');
  if (sec)  sec.classList.toggle('section-expanded', collapsed);
  if (list) list.classList.toggle('models-expanded', collapsed);
}
async function resetSettings() {
  if (!confirm('Reset all settings to defaults?')) return;
  try {
    const r = await api('/api/llm/settings/reset', {}, 'POST');
    if (r.status === 'ok') { toast('Reset', 'warn'); loadSettings(); }
    else toast(r.message, 'err');
  } catch(e) { toast(e.message, 'err'); }
}

// ─── Saved Models ─────────────────────────────────────────────────
async function loadSavedModels() {
  const els = [
    document.getElementById('saved-models-list'),
    document.getElementById('saved-models-list-new')
  ].filter(Boolean);
  try {
    const d    = await api('/api/saved-models', null, 'GET');
    _autoSaveEnabled = d.autoSave || false;
    _renderAutoSaveBadge();
    const models   = d.models || [];
    const countEl  = document.getElementById('saved-count');
    if (countEl) countEl.textContent = models.length + ' items';
    if (!models.length) {
      els.forEach(el => el.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:10px 0">No saved models</div>');
      return;
    }
    els.forEach(el => {
      el.innerHTML = '';
      models.forEach(m => {
        const item = document.createElement('div');
        item.className = 'saved-model-item';

        const info = document.createElement('div');
        info.style.cssText = 'flex:1;min-width:0';
        info.innerHTML = `<div class="saved-model-name" title="${esc(m.path)}">${esc(m.name)}</div><div class="saved-model-path">${esc(m.path)}</div>`;

        const state = (m.supports_thinking === null || m.supports_thinking === undefined) ? 'auto' : m.supports_thinking ? 'on' : 'off';
        const styles = {
          auto: 'background:rgba(255,179,0,.1);color:var(--amber);border:1px solid rgba(255,179,0,.3)',
          off:  'background:rgba(255,60,90,.08);color:var(--red);border:1px solid rgba(255,60,90,.25)',
          on:   'background:rgba(57,255,126,.1);color:var(--green);border:1px solid rgba(57,255,126,.25)'
        };
        const labels   = { auto:'THINK: AUTO', off:'THINK: OFF', on:'THINK: ON' };
        const nextVals = { auto: false, off: true, on: null };

        const thinkBtn = document.createElement('button');
        thinkBtn.style.cssText = `padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:1px;cursor:pointer;font-family:var(--mono);flex-shrink:0;${styles[state]}`;
        thinkBtn.title = 'click to cycle: AUTO -> OFF -> ON -> AUTO';
        thinkBtn.textContent = labels[state];
        thinkBtn.addEventListener('click', () => setModelThinking(m.path, nextVals[state]));

        const loadBtn = document.createElement('button');
        loadBtn.className = 'saved-model-load-btn';
        loadBtn.textContent = 'load';
        loadBtn.addEventListener('click', () => loadModelByPath(m.path));

        const delBtn = document.createElement('button');
        delBtn.className = 'saved-model-del-btn';
        delBtn.innerHTML = '&#215;';
        delBtn.addEventListener('click', () => deleteSavedModel(m.name, m.path));

        item.appendChild(info);
        item.appendChild(thinkBtn);
        item.appendChild(loadBtn);
        item.appendChild(delBtn);
        el.appendChild(item);
      });
    });
  } catch(e) {
    els.forEach(el => el.innerHTML = `<div style="color:var(--red);font-size:11px;padding:8px 0">Error: ${esc(e.message)}</div>`);
  }
}
// ─── Models sub-tabs (Library / Loaded) ──────────────────────────
function switchModelsSubtab(name, btn) {
  document.querySelectorAll('#models-subtab-bar .gpu-sub-pill').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  const isLib = name === 'library';
  const lib = document.getElementById('models-sub-library');
  const ld  = document.getElementById('models-sub-loaded');
  if (lib) lib.style.display = isLib ? 'flex' : 'none';
  if (ld)  ld.style.display  = isLib ? 'none' : 'flex';
  if (isLib) loadSavedModels(); else loadLoadedModels();
}

function refreshModelsPanel() {
  const ld = document.getElementById('models-sub-loaded');
  if (ld && ld.style.display !== 'none') loadLoadedModels();
  else loadSavedModels();
}

// ─── Loaded models (multi-model) ──────────────────────────────────
async function loadLoadedModels() {
  const list   = document.getElementById('loaded-models-list');
  const banner = document.getElementById('active-model-banner');
  if (!list && !banner) return;
  try {
    const [loaded, active] = await Promise.all([
      api('/api/loaded_model', null, 'GET'),
      api('/api/active_model', null, 'GET'),
    ]);
    renderLoadedModels(loaded || {}, active || {});
  } catch(e) {
    if (list)   list.innerHTML   = `<div style="color:var(--red);font-size:11px;padding:8px 0">Error: ${esc(e.message)}</div>`;
    if (banner) banner.innerHTML = `<div style="color:var(--red);font-size:11px;padding:8px 0">Error: ${esc(e.message)}</div>`;
  }
}

function _gpuLabel(gpuName, gpuIndex) {
  const idx = Array.isArray(gpuIndex) ? gpuIndex.join(' + ') : gpuIndex;
  if (gpuName == null && idx == null) return 'CPU';
  if (idx == null) return esc(String(gpuName));
  return `GPU ${esc(String(idx))} · ${esc(String(gpuName))}`;
}

function renderLoadedModels(loaded, active) {
  const list   = document.getElementById('loaded-models-list');
  const banner = document.getElementById('active-model-banner');
  const countEl = document.getElementById('loaded-count');
  const activeIdx = (active && active.active_index != null) ? active.active_index : null;

  // Keep the in-chat "current model" chip in sync with the ACTIVE model in real
  // time (handles load auto-activate, eject auto-promote, and manual switch).
  // Skipped while a load is polling so the transient "loading…" label set by
  // _setModelStatusUI isn't overwritten.
  if (typeof _setChatModelBar === 'function' && !_modelStatusInterval) {
    if (activeIdx != null && active && active.model) {
      _setChatModelBar('ready', String(active.model).split(/[/\\]/).pop());
    } else {
      _setChatModelBar('idle', '');
    }
  }

  if (banner) {
    if (activeIdx == null) {
      banner.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:8px 0">No active model</div>';
    } else {
      banner.innerHTML = `
        <div class="dev-stat-row"><span class="dev-stat-key">Index</span><span class="dev-stat-val" style="color:var(--cyan)">${esc(String(activeIdx))}</span></div>
        <div class="dev-stat-row"><span class="dev-stat-key">Model</span><span class="dev-stat-val" style="color:var(--green)">${esc(active.model || '—')}</span></div>
        <div class="dev-stat-row"><span class="dev-stat-key">GPU</span><span class="dev-stat-val">${_gpuLabel(active.gpu, active.gpu_index)}</span></div>`;
    }
  }

  if (!list) return;
  const indices = Object.keys(loaded);
  if (countEl) countEl.textContent = indices.length + ' loaded';
  if (!indices.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:20px">No models loaded</div>';
    return;
  }

  list.innerHTML = '';
  indices.sort((a, b) => parseInt(a) - parseInt(b)).forEach(idx => {
    const [name, gpuName, gpuIndex] = loaded[idx];
    const isActive = String(idx) === String(activeIdx);

    const item = document.createElement('div');
    item.className = 'saved-model-item';

    const badge = document.createElement('span');
    badge.style.cssText = 'font-size:10px;font-weight:700;font-family:var(--mono);color:var(--cyan);background:rgba(0,229,255,.08);border:1px solid rgba(0,229,255,.25);border-radius:4px;padding:2px 7px;flex-shrink:0';
    badge.textContent = '#' + idx;

    const info = document.createElement('div');
    info.style.cssText = 'flex:1;min-width:0';
    info.innerHTML = `<div class="saved-model-name">${esc(name || '—')}</div><div class="saved-model-path">${_gpuLabel(gpuName, gpuIndex)}</div>`;

    item.appendChild(badge);
    item.appendChild(info);

    if (isActive) {
      const pill = document.createElement('span');
      pill.style.cssText = 'padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:1px;font-family:var(--mono);flex-shrink:0;background:rgba(57,255,126,.1);color:var(--green);border:1px solid rgba(57,255,126,.25)';
      pill.textContent = 'ACTIVE';
      item.appendChild(pill);
    } else {
      const setBtn = document.createElement('button');
      setBtn.className = 'saved-model-load-btn';
      setBtn.textContent = 'set active';
      setBtn.addEventListener('click', () => setActiveModel(idx));
      item.appendChild(setBtn);
    }

    const delBtn = document.createElement('button');
    delBtn.className = 'saved-model-del-btn';
    delBtn.innerHTML = '&#9167;';
    delBtn.title = 'eject this model';
    delBtn.addEventListener('click', () => ejectModelByIndex(idx, name));
    item.appendChild(delBtn);

    list.appendChild(item);
  });
}

// POST /api/active_model  { index }
async function setActiveModel(idx) {
  try {
    const d = await api('/api/active_model', { index: parseInt(idx) });
    if (d.status === 'error') { toast(d.message || 'switch failed', 'err'); return; }
    toast(`active model → #${idx}`, 'ok');
    loadLoadedModels();
    refreshModelStatus();
  } catch(e) { toast('error: ' + e.message, 'err'); }
}

// POST /api/eject_model  { index }
async function ejectModelByIndex(idx, name) {
  if (!confirm(`Eject model #${idx}${name ? ` (${name})` : ''}?`)) return;
  try {
    const d = await api('/api/eject_model', { index: parseInt(idx) });
    if (d.status === 'error') { toast(d.message || 'eject failed', 'err'); return; }
    toast(`ejected #${idx}`, 'warn');
    loadLoadedModels();
  } catch(e) { toast('error: ' + e.message, 'err'); }
}

function _renderAutoSaveBadge() {
  const on = _autoSaveEnabled;
  document.querySelectorAll('.autosave-badge').forEach(el => {
    el.textContent         = on ? 'ON' : 'OFF';
    el.style.color         = on ? 'var(--green)' : 'var(--muted)';
    el.style.borderColor   = on ? 'rgba(57,255,126,.3)' : 'var(--border)';
  });
  document.querySelectorAll('.autosave-track').forEach(el => {
    el.style.background = on ? 'rgba(57,255,126,.25)' : 'var(--border2)';
  });
  document.querySelectorAll('.autosave-thumb').forEach(el => {
    el.style.background = on ? 'var(--green)' : 'var(--muted)';
    el.style.left       = on ? '19px'         : '3px';
  });
}
async function toggleAutoSave() {
  try {
    const r = await api('/api/saved-models/toggle-autosave', {});
    if (r.status === 'ok') { _autoSaveEnabled = r.autoSave; _renderAutoSaveBadge(); toast(r.message, r.autoSave ? 'ok' : 'warn'); }
    else toast(r.message || 'Toggle failed', 'err');
  } catch(e) { toast('Toggle error: ' + e.message, 'err'); }
}
async function loadModelByPath(path) {
  _setModelStatusUI('loading', '');
  try {
    const r = await api('/api/llm/load-model', { path });
    if (r.status === 'loading' || r.status === 'ok') { toast('Loading model\u2026', 'info'); _startModelPoll(); }
    else if (r.message === 'model_loaded') { toast('Model already loaded', 'warn'); refreshModelStatus(); }
    else { _setModelStatusUI('error', ''); toast(r.message || 'Load failed', 'err'); }
  } catch(e) {
    if (e.message === 'model_loaded') { toast('Model already loaded', 'warn'); refreshModelStatus(); }
    else { _setModelStatusUI('error', ''); toast('Error: ' + e.message, 'err'); }
  }
}
async function deleteSavedModel(name, path) {
  if (!confirm(`Remove "${name}" from saved list?`)) return;
  try {
    const r = await api('/api/saved-models/delete', { path });
    if (r.status === 'ok') { toast(`"${name}" removed`, 'warn'); await loadSavedModels(); }
    else toast(r.message || 'Delete failed', 'err');
  } catch(e) { toast('Error: ' + e.message, 'err'); }
}
async function saveCurrentModelPath() {
  const path = document.getElementById('model-path-input').value.trim();
  if (!path) { toast('Enter a model path first', 'warn'); return; }
  const name = path.split(/[/\\]/).pop().replace(/\.gguf$/i, '');
  try {
    const r = await api('/api/saved-models', { name, path });
    if (r.status === 'ok') { toast(`Saved: ${name}`, 'ok'); loadSavedModels(); }
    else toast(r.message || 'Save failed', 'err');
  } catch(e) { toast('Error: ' + e.message, 'err'); }
}
async function quickSaveModel() {
  const name = document.getElementById('quick-name').value.trim();
  const path = document.getElementById('quick-path').value.trim();
  if (!name) { toast('Enter model name', 'warn'); return; }
  if (!path) { toast('Enter model path', 'warn'); return; }
  if (!path.toLowerCase().endsWith('.gguf')) { toast('Path must end with .gguf', 'warn'); return; }
  try {
    const r = await api('/api/saved-models', { name, path });
    if (r.status === 'ok') {
      toast(`Saved: ${name}`, 'ok');
      document.getElementById('quick-name').value = '';
      document.getElementById('quick-path').value = '';
      loadSavedModels();
    } else toast(r.message || 'Save failed', 'err');
  } catch(e) { toast('Error: ' + e.message, 'err'); }
}

// ─── Model Loader ─────────────────────────────────────────────────
let _modelStatusInterval = null;
// Poll bookkeeping so a load that dies/stalls can't spin forever.
let _modelPollStart      = 0;     // when the current poll began (ms)
let _modelPollStallSince = 0;     // last time progress advanced (ms)
let _modelPollLastProg   = -1;    // highest progress % seen so far
let _modelPollSawProg    = false; // server reported a numeric % at least once
let _modelPollErrCount   = 0;     // consecutive fetch failures
const MODEL_POLL_MS       = 1000;            // poll cadence — 1s for near real-time
const MODEL_POLL_MAX_MS   = 30 * 60 * 1000;  // absolute cap: give up after 30 min
const MODEL_POLL_STALL_MS = 90 * 1000;       // % frozen this long ⇒ stalled
const MODEL_POLL_MAX_ERR  = 4;               // consecutive errors before giving up

function _stopModelPoll() {
  if (_modelStatusInterval) { clearInterval(_modelStatusInterval); _modelStatusInterval = null; }
}
// Single exit path for a failed/stalled/timed-out load: stop polling, surface the
// error in both bars, and resync the authoritative active-model state.
function _modelLoadFailed(msg) {
  _stopModelPoll();
  _setModelStatusUI('error', '');
  _setChatModelBar('error', '');
  toast(msg || 'Model load failed', 'err');
  loadLoadedModels();
}

function _setModelStatusUI(status, modelName, progress, errorMsg) {
  const dot = document.getElementById('model-status-dot');
  const txt = document.getElementById('model-status-txt');
  if (!dot || !txt) return;
  dot.className = 'model-status-dot ' + (status || '');
  txt.className  = 'model-status-txt '  + (status || '');
  const shortName = modelName ? modelName.split(/[/\\]/).pop() : '';
  const pct       = (typeof progress === 'number' && progress > 0)
                      ? ' ' + Math.min(100, Math.round(progress)) + '%'
                      : '';
  const labels    = {
    ready:   '\u2714  ' + (shortName || 'ready'),
    loading: 'loading\u2026' + pct,
    // Show the real reason from llm-server (e.g. out of memory) instead of a
    // generic "check the log". Full text is also on the title for hover.
    error:   '\u26a0 ' + (errorMsg || 'load failed \u2014 check llm_server log'),
    idle:    'no model loaded',
  };
  txt.textContent = labels[status] || (status || 'no model loaded');
  txt.title = (status === 'error' && errorMsg) ? errorMsg : '';

  // Only the transient "loading…" chip comes from model-status; the authoritative
  // ready/idle (active-model) state is owned by renderLoadedModels to avoid races.
  if (status === 'loading') _setChatModelBar('loading', shortName);

  const bar  = document.getElementById('model-progress');
  const fill = document.getElementById('model-progress-fill');
  if (bar && fill) {
    if (status === 'loading') {
      bar.classList.add('show');
      const hasPct = typeof progress === 'number' && progress > 0;
      if (hasPct) {
        bar.classList.remove('indeterminate');
        fill.style.width = Math.min(100, Math.max(0, Math.round(progress))) + '%';
      } else {
        // no numeric progress reported -> show animated indeterminate sweep
        bar.classList.add('indeterminate');
      }
    } else {
      bar.classList.remove('show', 'indeterminate');
      fill.style.width = '0%';
    }
  }
}
// Show/hide the in-chat "current model" chip. Visible while a model is ready or
// loading; hidden when idle/none so it only appears when there's something to show.
function _setChatModelBar(status, shortName) {
  const barEl  = document.getElementById('chat-model-bar');
  const dotEl  = document.getElementById('chat-model-dot');
  const nameEl = document.getElementById('chat-model-name');
  if (!barEl || !nameEl) return;
  if (dotEl) dotEl.className = 'cmb-dot ' + (status || '');
  if (status === 'loading') {
    nameEl.textContent = shortName ? (shortName + ' · loading…') : 'loading…';
    barEl.classList.add('show');
  } else if (status === 'ready' && shortName) {
    nameEl.textContent = shortName;
    barEl.classList.add('show');
  } else if (status === 'error') {
    // Don't just vanish on failure — show a red "load failed" so the user knows
    // the load stopped, then let the next state (idle/ready) replace it.
    nameEl.textContent = 'load failed';
    barEl.classList.add('show');
  } else {
    barEl.classList.remove('show');   // idle / no model → hide
  }
}
function _startModelPoll() {
  if (_modelStatusInterval) return;
  _modelPollStart      = Date.now();
  _modelPollStallSince = Date.now();
  _modelPollLastProg   = -1;
  _modelPollSawProg    = false;
  _modelPollErrCount   = 0;
  _modelStatusInterval = setInterval(async () => {
    // 1) Absolute backstop so an upstream that's stuck on "loading" can't spin forever.
    if (Date.now() - _modelPollStart > MODEL_POLL_MAX_MS) {
      _modelLoadFailed('Model load timed out'); return;
    }

    // 2) Fetch status — tolerate a few transient errors, then give up loudly
    //    (the old code silently killed the poll and left the spinner stuck).
    let d;
    try {
      d = await api('/api/llm/model-status', null, 'GET');
      _modelPollErrCount = 0;
    } catch {
      if (++_modelPollErrCount >= MODEL_POLL_MAX_ERR) {
        _modelLoadFailed('Lost contact with server while loading');
      }
      return;
    }

    _setModelStatusUI(d.status, d.loaded_model, d.progress, d.error || d.message);

    // 3) Still loading: watch for a stalled % (only once the server has actually
    //    reported numbers — an indeterminate load is covered by the cap above).
    if (d.status === 'loading') {
      const prog = (typeof d.progress === 'number' && d.progress >= 0) ? d.progress : -1;
      if (prog >= 0) {
        _modelPollSawProg = true;
        if (prog > _modelPollLastProg) { _modelPollLastProg = prog; _modelPollStallSince = Date.now(); }
      }
      if (_modelPollSawProg && Date.now() - _modelPollStallSince > MODEL_POLL_STALL_MS) {
        _modelLoadFailed('Model load stalled — no progress');
      }
      return;
    }

    // 4) Terminal state reached.
    _stopModelPoll();
    if (d.status === 'ready') {
      toast('Model ready!', 'ok'); loadSavedModels(); loadLoadedModels();
    } else if (d.status === 'error') {
      const emsg = d.error || d.message || 'Model load failed';
      _setModelStatusUI('error', '', 0, emsg);
      _setChatModelBar('error', ''); toast(emsg, 'err'); loadLoadedModels();
    } else {
      loadLoadedModels();   // idle / unknown → resync the chip from active model
    }
  }, MODEL_POLL_MS);
}
async function refreshModelStatus() {
  try {
    const d = await api('/api/llm/model-status', null, 'GET');
    _setModelStatusUI(d.status, d.loaded_model, d.progress, d.error || d.message);
    if (d.status === 'loading') _startModelPoll();
    else if (d.status === 'error') toast(d.error || d.message || 'Model load failed', 'err');
    // Sync the in-chat model chip from the authoritative active model. Without
    // this the chip only ever appeared after a load/eject event, so on a fresh
    // page load (or settings tab) with a model already active it stayed hidden.
    else if (typeof loadLoadedModels === 'function') loadLoadedModels();
  } catch(e) { _setModelStatusUI('error', ''); }
}
async function loadModel() {
  const path = document.getElementById('model-path-input').value.trim();
  if (!path) { toast('Enter model path', 'warn'); return; }
  try {
    _setModelStatusUI('loading', '');
    const r = await api('/api/llm/load-model', { path });
    if (r.status === 'loading' || r.status === 'ok') { toast('Loading model\u2026', 'info'); _startModelPoll(); }
    else if (r.message === 'model_loaded') { toast('Model already loaded', 'warn'); refreshModelStatus(); }
    else { _setModelStatusUI('error', ''); toast(r.message || 'Load failed', 'err'); }
  } catch(e) {
    if (e.message === 'model_loaded') { toast('Model already loaded', 'warn'); refreshModelStatus(); }
    else { _setModelStatusUI('error', ''); toast('Error: ' + e.message, 'err'); }
  }
}

// ─── Cache Prompt ──────────────────────────────────────────────────
let _cachePromptEnabled = false;

function _renderCachePromptUI() {
  const on = _cachePromptEnabled;
  const badge = document.getElementById('cache-prompt-badge');
  const track = document.getElementById('cache-prompt-track');
  const thumb = document.getElementById('cache-prompt-thumb');
  if (!badge) return;
  badge.textContent   = on ? 'ON' : 'OFF';
  badge.style.color   = on ? 'var(--green)' : 'var(--muted)';
  badge.style.borderColor = on ? 'rgba(57,255,126,.3)' : 'var(--border)';
  track.style.background  = on ? 'rgba(57,255,126,.25)' : 'var(--border2)';
  thumb.style.background  = on ? 'var(--green)' : 'var(--muted)';
  thumb.style.left        = on ? '19px' : '3px';
}

async function loadCachePrompt() {
  try {
    const d = await api('/api/llm/cache-prompt', null, 'GET');
    _cachePromptEnabled = d.cache_prompt ?? true;
    _renderCachePromptUI();
  } catch(e) { console.warn('[cache-prompt]', e.message); }
}

async function toggleCachePrompt() {
  const newVal = !_cachePromptEnabled;
  try {
    const r = await api('/api/llm/cache-prompt', { enabled: newVal });
    if (r.status === 'ok') {
      _cachePromptEnabled = r.cache_prompt;
      _renderCachePromptUI();
      toast('Cache prompt ' + (_cachePromptEnabled ? 'ON' : 'OFF'), _cachePromptEnabled ? 'ok' : 'warn');
    } else toast(r.message || 'failed', 'err');
  } catch(e) { toast('error: ' + e.message, 'err'); }
}

// ─── Developer Panel ──────────────────────────────────────────────
let _lastWarnedPct = 0;

async function devRefresh() {
  try {
    const d   = await api('/api/dev/config', null, 'GET');
    const cfg = d.config || {};
    const pct = d.token_pct || 0;

    const bar   = document.getElementById('dev-token-bar');
    const txt   = document.getElementById('dev-token-txt');
    const sub   = document.getElementById('dev-token-sub');
    const badge = document.getElementById('dev-token-badge');
    if (!bar) return;

    txt.textContent   = (d.history_tokens || 0).toLocaleString() + ' tokens';
    sub.textContent   = pct.toFixed(1) + '% of ' + (d.ctx_size || 0).toLocaleString() + ' ctx';
    bar.style.width   = Math.min(pct, 100) + '%';
    bar.style.background = pct >= 90 ? 'var(--red)' : pct >= 70 ? 'var(--amber)' : 'var(--green)';

    badge.textContent = pct >= 90 ? 'FULL' : pct >= 70 ? 'HIGH' : 'OK';
    badge.className   = 'dev-badge ' + (pct >= 90 ? 'dev-badge-err' : pct >= 70 ? 'dev-badge-warn' : 'dev-badge-ok');

    document.getElementById('dev-msg-count').textContent   = d.history_msgs || 0;
    document.getElementById('dev-ctx-display').textContent = (d.ctx_size || 0).toLocaleString();
    document.getElementById('dev-gpu-backend').textContent = d.gpu_backend || '—';
    document.getElementById('dev-gpu-info').textContent    = d.gpu_info    || '—';

    const _configInputIds = ['dev-ctx','dev-batch','dev-cpu-pct','dev-gpu-layers','dev-max-hist','dev-warn-at'];
    const _userEditing = _configInputIds.some(id => document.getElementById(id) === document.activeElement);
    if (!_userEditing) {
      document.getElementById('dev-ctx').value        = cfg.ctx_size    || 8192;
      document.getElementById('dev-batch').value      = cfg.batch_size  || 512;
      document.getElementById('dev-cpu-pct').value    = cfg.cpu_percent || 0;
      document.getElementById('dev-gpu-layers').value = cfg.gpu_layers != null ? cfg.gpu_layers : '';
      document.getElementById('dev-max-hist').value   = cfg.max_history  || 0;
      document.getElementById('dev-warn-at').value    = cfg.token_warn_at || 6000;
      const atEl = document.getElementById('dev-auto-trim');
      if (atEl) atEl.checked = !!cfg.auto_trim;
    }
    document.getElementById('dev-cpu-val').textContent = (cfg.cpu_percent || 0) + '%';
    document.getElementById('dev-gpu-val').textContent = (100 - (cfg.cpu_percent || 0)) + '%';
    const warnBadge = document.getElementById('ctx-warn-badge');
    if (warnBadge) {
      if (pct >= 70) {
        warnBadge.innerHTML = '<svg class="ic" width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.6 14.6 13.4H1.4z"/><path d="M8 6v3.2"/><circle cx="8" cy="11.4" r=".55" fill="currentColor" stroke="none"/></svg> ctx ' + pct.toFixed(0) + '%';
        warnBadge.classList.add('visible');
      }
      else             warnBadge.classList.remove('visible');
    }

    const crossedTo90 = pct >= 90 && _lastWarnedPct < 90;
    const crossedTo70 = pct >= 70 && _lastWarnedPct < 70;
    if      (crossedTo90) toast('Context ' + pct.toFixed(0) + '% full — consider trimming history', 'err');
    else if (crossedTo70) toast('Context ' + pct.toFixed(0) + '% — getting full', 'warn');
    if      (pct < 70)   _lastWarnedPct = 0;
    else if (pct >= 90)  _lastWarnedPct = 90;
    else if (pct >= 70)  _lastWarnedPct = 70;

  } catch(e) {}
}
async function devApply() {
  try {
    const gpuRaw = document.getElementById('dev-gpu-layers').value.trim();
    const body   = {
      ctx_size:      parseInt(document.getElementById('dev-ctx').value),
      batch_size:    parseInt(document.getElementById('dev-batch').value),
      cpu_percent:   parseInt(document.getElementById('dev-cpu-pct').value),
      gpu_layers:    gpuRaw === '' ? null : parseInt(gpuRaw),
      max_history:   parseInt(document.getElementById('dev-max-hist').value) || 0,
      token_warn_at: parseInt(document.getElementById('dev-warn-at').value)  || 6000,
    };
    const r = await api('/api/dev/config', body);
    if (r.status === 'ok') toast(r.message || 'Config applied', 'ok');
    else toast(r.message || 'Error', 'err');
    devRefresh();
  } catch(e) { toast('Error: ' + e.message, 'err'); }
}
async function devEject() {
  if (!confirm('Eject model? llama-server process will be killed.')) return;
  try {
    const r = await api('/api/dev/eject', {});
    if (r.status === 'ok') toast(r.message || 'Model ejected', 'warn');
    else toast(r.message || 'Error', 'err');
    refreshModelStatus(); devRefresh();
  } catch(e) { toast('Error: ' + e.message, 'err'); }
}
async function devTrimHistory() {
  const n     = parseInt(document.getElementById('dev-trim-n').value);
  const label = isNaN(n) || n === 0 ? 'clear ALL history' : 'keep last ' + n + ' messages';
  if (!confirm(label + '?')) return;
  try {
    const body = { keep: isNaN(n) ? 0 : n };
    const r    = await api('/api/dev/trim_history', body);
    if (r.status === 'ok') toast(r.message || 'Trimmed', 'warn');
    else toast(r.message || 'Error', 'err');
    devRefresh();
  } catch(e) { toast('Error: ' + e.message, 'err'); }
}
async function toggleAutoTrim() {
  const newVal = document.getElementById('dev-auto-trim').checked;
  try {
    const r = await api('/api/dev/config', { auto_trim: newVal });
    if (r.status === 'ok') toast('Auto-trim ' + (newVal ? 'enabled' : 'disabled'), newVal ? 'ok' : 'warn');
    else toast(r.message || 'Error', 'err');
  } catch(e) { toast('Error: ' + e.message, 'err'); }
}
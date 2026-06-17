// ══════════════════════════════════════════════════════════════════
//  prompt.js  —  system prompt · prompt library · network config
// ══════════════════════════════════════════════════════════════════

// ─── Prompt Panel (current system prompt view) ────────────────────
async function loadPrompt() {
  try {
    const d = await api('/api/llm/prompt', null, 'GET');
    document.getElementById('prompt-view').textContent      = d.system_prompt || '—';
    document.getElementById('prompt-len-label').textContent = `${d.prompt_length_chars || 0} chars`;
    const badge = document.getElementById('prompt-active-badge');
    if (badge) {
      if (d.prompt_stopped) {
        badge.textContent = 'STOPPED'; badge.className = 'prompt-active-badge stopped';
      } else if (d.active_prompt_name) {
        badge.textContent = d.active_prompt_name; badge.className = 'prompt-active-badge active';
      } else {
        badge.textContent = 'DEFAULT'; badge.className = 'prompt-active-badge default';
      }
    }
  } catch(e) {
    document.getElementById('prompt-view').textContent = 'LLM server unreachable';
  }
}

async function reloadPromptJson() {
  try {
    const r = await api('/api/llm/reload', null, 'POST');
    toast(r.message || 'Reloaded', 'ok');
    await loadPrompt();
  } catch(e) { toast(e.message, 'err'); }
}

// ─── Prompt Library ───────────────────────────────────────────────
let _promptEditName = null;

async function loadPromptNames() {
  const list = document.getElementById('prompt-library-list');
  if (!list) return;
  list.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:14px">Loading…</div>';
  try {
    const d = await api('/api/llm/get-promptn', null, 'GET');
    const prompts = d.prompts || [];
    const active  = d.active;
    const stopped = d.stopped;
    document.getElementById('prompt-count-label').textContent = `${prompts.length} prompt${prompts.length !== 1 ? 's' : ''}`;
    if (!prompts.length) {
      list.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:14px">No saved prompts</div>';
      return;
    }
    list.innerHTML = '';
    prompts.forEach(name => {
      const isActive = !stopped && active && (active === name || active === name.replace(/\.txt$/, ''));
      const item = document.createElement('div');
      item.className = 'prompt-lib-item' + (isActive ? ' active' : '');
      item.dataset.name = name;
      const displayName = name.replace(/\.txt$/, '');
      item.innerHTML = `
        <div class="prompt-lib-name" title="${displayName}">${displayName}</div>
        <div class="prompt-lib-actions">
          ${isActive
            ? '<span class="prompt-active-dot" title="Active"></span>'
            : `<button class="plib-btn use" onclick="usePrompt('${name}')" title="Activate"><svg class="ic" width="10" height="10" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M4 3l9 5-9 5z"/></svg></button>`}
          <button class="plib-btn edit" onclick="openEditPrompt('${name}')" title="Edit"><svg class="ic" width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11.5 1.5l3 3-9 9H2.5v-3z"/><path d="M10.5 2.5l3 3"/></svg></button>
          <button class="plib-btn del"  onclick="deletePrompt('${name}')" title="Delete"><svg class="ic" width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9"/></svg></button>
        </div>`;
      list.appendChild(item);
    });
  } catch(e) {
    list.innerHTML = '<div style="color:var(--red);font-size:11px;text-align:center;padding:14px">Server unreachable</div>';
  }
}

async function usePrompt(name) {
  try {
    const r = await api(`/api/llm/use-prompt?name=${encodeURIComponent(name)}`, null, 'GET');
    toast(r.message || `Activated: ${name}`, 'ok');
    await loadPromptNames();
    await loadPrompt();
  } catch(e) { toast(e.message, 'err'); }
}

async function deletePrompt(name) {
  if (!confirm(`Delete prompt "${name.replace(/\.txt$/, '')}"?`)) return;
  try {
    const r = await api('/api/llm/delete-prompt', { name });
    toast(r.message || 'Deleted', 'ok');
    await loadPromptNames();
    await loadPrompt();
  } catch(e) { toast(e.message, 'err'); }
}

const _USE_APIS_TOKEN = '{{USE_APIS}}';
const _PROMPT_TOKEN   = '{{PROMPT}}';

function _makeChip(label) {
  const s = document.createElement('span');
  s.contentEditable = 'false';
  s.dataset.token = _USE_APIS_TOKEN;
  s.style.cssText = 'display:inline-block;background:rgba(255,179,0,.15);border:1px solid rgba(255,179,0,.45);color:var(--amber);font-size:11px;font-weight:700;letter-spacing:1px;padding:2px 10px;border-radius:4px;font-family:var(--mono);user-select:text;cursor:text;white-space:pre-wrap;vertical-align:middle;margin:0 2px;';
  s.textContent = label || '{{USE_APIS}}';
  return s;
}

function _makePromptChip(label) {
  const s = document.createElement('span');
  s.contentEditable = 'false';
  s.dataset.token = _PROMPT_TOKEN;
  s.style.cssText = 'display:inline-block;background:rgba(57,255,126,.15);border:1px solid rgba(57,255,126,.45);color:var(--green);font-size:11px;font-weight:700;letter-spacing:1px;padding:2px 10px;border-radius:4px;font-family:var(--mono);user-select:none;cursor:default;white-space:nowrap;vertical-align:middle;margin:0 2px;';
  s.textContent = label || '{{PROMPT}}';
  return s;
}

function _loadEditorContent(div, text, apiLabel) {
  div.innerHTML = '';
  const tokens  = [_USE_APIS_TOKEN, _PROMPT_TOKEN];
  const escaped = tokens.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const regex   = new RegExp(escaped.join('|'), 'g');
  let last = 0, match;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > last)
      div.appendChild(document.createTextNode(text.slice(last, match.index)));
    if (match[0] === _USE_APIS_TOKEN)    div.appendChild(_makeChip(apiLabel));
    else if (match[0] === _PROMPT_TOKEN) div.appendChild(_makePromptChip(apiLabel));
    last = match.index + match[0].length;
  }
  if (last < text.length)
    div.appendChild(document.createTextNode(text.slice(last)));
}

function _readEditorContent(div) {
  let out = '';
  div.childNodes.forEach(n => {
    if (n.nodeType === Node.TEXT_NODE) out += n.textContent;
    else if (n.dataset && n.dataset.token === _USE_APIS_TOKEN)  out += _USE_APIS_TOKEN;
    else if (n.dataset && n.dataset.token === _PROMPT_TOKEN)    out += _PROMPT_TOKEN;
    else if (n.nodeName === 'BR') out += '\n';
    else out += n.textContent;
  });
  return out;
}

async function openEditPrompt(name) {
  _promptEditName = name;
  const displayName = name.replace(/\.txt$/, '');
  document.getElementById('prompt-edit-title').textContent = `edit: ${displayName}`;
  document.getElementById('prompt-editor-wrap').style.display = 'flex';
  const div = document.getElementById('prompt-editor-div');
  div.innerHTML = '<span style="color:var(--muted);font-style:italic">Loading…</span>';

  if (div._observer) { div._observer.disconnect(); div._observer = null; }

    div.onkeydown = function(e) {
        if (e.key === 'Delete' || e.key === 'Backspace') {
          const sel = window.getSelection();
          if (!sel || !sel.rangeCount) return;
          const range = sel.getRangeAt(0);
          if (!range.collapsed) {
            e.preventDefault();
            const chipsToRemove = [];
            div.childNodes.forEach(n => {
              if (n.dataset && n.dataset.token && sel.containsNode(n, false)) {
                chipsToRemove.push(n);
              }
            });
            document.execCommand('delete');
            chipsToRemove.forEach(n => { if (n.parentNode) n.parentNode.removeChild(n); });
          }
        }
      };

  try {
    const d = await api(`/api/llm/get-prompt?name=${encodeURIComponent(name)}`, null, 'GET');
    const p = await api('/api/llm/api-docs', null, 'GET').catch(() => null);
    const apiLabel = p ? p.content : '{{USE_APIS}}';
    div._hasChip = (d.content || '').includes(_USE_APIS_TOKEN) || (d.content || '').includes(_PROMPT_TOKEN);
    _loadEditorContent(div, d.content || '', apiLabel);
  } catch(e) {
    div.innerHTML = '<span style="color:var(--red)">Error: ' + e.message + '</span>';
    toast('Could not load prompt', 'err');
  }
}
function closeEditPrompt() {
  _promptEditName = null;
  const div = document.getElementById('prompt-editor-div');
  if (div._observer) { div._observer.disconnect(); div._observer = null; }
  div._hasChip = false;
  document.getElementById('prompt-editor-wrap').style.display = 'none';
  div.innerHTML = '';
}

async function saveEditedPrompt() {
  if (!_promptEditName) return;
  const text = _readEditorContent(document.getElementById('prompt-editor-div'));
  try {
    const r = await api('/api/llm/edit-prompt', { name: _promptEditName, edprompt: text });
    toast(r.message || 'Saved', 'ok');
    closeEditPrompt();
    await loadPromptNames();
    await loadPrompt();
  } catch(e) { toast(e.message, 'err'); }
}

async function stopPrompt() {
  try {
    const r = await api('/api/llm/stop-prompt', {});
    toast(r.message || 'Prompt stopped', 'warn');
    await loadPromptNames();
    await loadPrompt();
  } catch(e) { toast(e.message, 'err'); }
}

async function resetPrompt() {
  try {
    const r = await api('/api/llm/reset-prompt', {});
    toast(r.message || 'Reset to default', 'ok');
    await loadPromptNames();
    await loadPrompt();
  } catch(e) { toast(e.message, 'err'); }
}

async function createNewPrompt() {
  const name     = document.getElementById('new-prompt-name').value.trim();
  const text     = document.getElementById('new-prompt-text').value.trim();
  const use_apis = document.getElementById('new-prompt-apis').checked;
  if (!name) { toast('Name is required', 'warn'); return; }
  if (!text) { toast('Prompt text is required', 'warn'); return; }
  try {
    const r = await api('/api/llm/new-prompt', { name, prompt: text, use_apis });
    toast(r.message || 'Created', 'ok');
    document.getElementById('new-prompt-name').value = '';
    document.getElementById('new-prompt-text').value = '';
    document.getElementById('new-prompt-apis').checked = false;
    document.getElementById('new-prompt-form').style.display = 'none';
    await loadPromptNames();
  } catch(e) { toast(e.message, 'err'); }
}

function toggleNewPromptForm() {
  const f = document.getElementById('new-prompt-form');
  const isHidden = f.style.display === 'none' || !f.style.display;
  f.style.display = isHidden ? 'flex' : 'none';
  if (isHidden) document.getElementById('new-prompt-name').focus();
}

// ─── Network Config ───────────────────────────────────────────────
function loadNetworkConfig() {
  _cfg = Object.assign(_defaultNet(), JSON.parse(localStorage.getItem(NET_KEY) || 'null') || {});
  document.getElementById('net-chat-host').value  = _cfg.chat_host;
  document.getElementById('net-chat-port').value  = _cfg.chat_port;
  document.getElementById('net-llm-host').value   = _cfg.llm_host;
  document.getElementById('net-llm-tcp').value    = _cfg.llm_tcp;
  document.getElementById('net-llm-http').value   = _cfg.llm_http;
  document.getElementById('net-sys-host').value   = _cfg.sys_host;
  document.getElementById('net-sys-port').value   = _cfg.sys_port;
  document.getElementById('net-audio-port').value = _cfg.audio_port;
  _updateHeaderUrl();
}

async function applyNetworkConfig() {
  const newCfg = {
    chat_host:  document.getElementById('net-chat-host').value.trim() || '127.0.0.1',
    chat_port:  parseInt(document.getElementById('net-chat-port').value) || 8900,
    llm_host:   document.getElementById('net-llm-host').value.trim(),
    llm_tcp:    parseInt(document.getElementById('net-llm-tcp').value),
    llm_http:   parseInt(document.getElementById('net-llm-http').value),
    sys_host:   document.getElementById('net-sys-host').value.trim(),
    sys_port:   parseInt(document.getElementById('net-sys-port').value),
    audio_port: parseInt(document.getElementById('net-audio-port').value),
  };
  if (!newCfg.chat_host) { toast('Chat server host cannot be empty', 'warn'); return; }
  const oldB = B;
  _cfg = newCfg; B = _buildBaseUrl(_cfg.chat_host, _cfg.chat_port);
  localStorage.setItem(NET_KEY, JSON.stringify(_cfg)); _updateHeaderUrl();
  const chatChanged = (B !== oldB);
  try {
    const r = await api('/api/network', newCfg);
    if (r.status === 'ok') toast(chatChanged ? `Chat server → ${B}` : 'Network config applied', 'ok');
    else toast('Server said: ' + (r.message || 'error'), 'warn');
  } catch(e) {
    if (chatChanged) toast(`Chat server changed → ${B} (reconnecting…)`, 'info');
    else toast('Could not reach server: ' + e.message, 'err');
  }
  if (chatChanged) { setConn(false); setTimeout(connectSSE, 500); }
}
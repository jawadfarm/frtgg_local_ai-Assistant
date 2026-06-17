// ══════════════════════════════════════════════════════════════════
//  chat.js  —  SSE · streaming · messages · send · commands
// ══════════════════════════════════════════════════════════════════

// ─── Chat state ───────────────────────────────────────────────────
let _expectingNewSession = false;
let _lastSentByUser    = false;
let _queuedIndicatorEl = null;
let _msgIdCounter      = 0;
let _streamBubble      = null, _streamBody     = null;
let _streamThink       = null, _streamThinkBody = null, _streamIsThink = false;
let _streamAccum       = '';
let _streamBodyText    = '';   // live body text (warnings stripped) — re-rendered with markdown each token
let _warnState         = { inside: false, prefixBuf: '', atLineStart: true, blockEl: null };
let _awaitingReloadHistory = false;   // suppress stream until session_history after a reload-stop
let _streamStopped         = false;   // after a stop: ignore stray leaked tokens (no new bubble)

// True when a generation SSE event belongs to the session currently displayed.
// Generation events carry d.session (null for a fresh/unsaved session).
function _eventForDisplayed(d) {
  const evt = (d.session === undefined) ? null : d.session;
  return (evt ?? null) === (_currentSessionName ?? null);
}

// ─── Generation UI ────────────────────────────────────────────────
function _setGeneratingUI(v) {
  _isGenerating = v;
  const bar = document.getElementById('input-bar');
  if (v) bar.classList.add('generating');
  else  { bar.classList.remove('generating'); _removeQueuedIndicator(); }
}
function _removeQueuedIndicator() {
  if (_queuedIndicatorEl) { _queuedIndicatorEl.remove(); _queuedIndicatorEl = null; }
}
function _showQueuedIndicator(text) {
  _removeQueuedIndicator();
  const stream = document.getElementById('chat-stream');
  const div = document.createElement('div');
  div.className = 'queued-indicator';
  div.innerHTML = `<div class="queued-indicator-dot"></div><span>queued: <em>${esc(text.slice(0,60))}${text.length>60?'…':''}</em></span>`;
  stream.appendChild(div); _queuedIndicatorEl = div; scrollChat();
}

// Tear down the live streaming bubble (remove cursor/dots, reset stream state).
function _teardownStreamBubble() {
  if (!_streamBubble) return;
  const cursor = _streamBubble.querySelector('.streaming-cursor');
  if (cursor) cursor.remove();
  if (_streamThink) { const dot = _streamThink.querySelector('.think-streaming-dot'); if (dot) dot.remove(); }
  _streamBubble.classList.remove('streaming');
  _streamBubble = null; _streamBody = null;
  _streamThink  = null; _streamThinkBody = null; _streamIsThink = false; _streamBodyText = '';
  _resetWarnState();
}

// ─── Stop generation ──────────────────────────────────────────────
async function stopGeneration() {
  try {
    const r = await api('/api/llm/stop', {}, 'POST');
    if (r.status === 'stop_success') {
      toast('Generation stopped', 'warn');
      _streamStopped = true;
      _teardownStreamBubble();
      _setGeneratingUI(false);
    } else toast(r.message || 'Stop failed', 'err');
  } catch(e) { toast('Stop error: ' + e.message, 'err'); }
}

// ─── SSE ──────────────────────────────────────────────────────────
function connectSSE() {
  if (_sseSource) { try { _sseSource.close(); } catch {} }
  const url = _authToken
    ? `${B}/stream?token=${encodeURIComponent(_authToken)}`
    : `${B}/stream`;
  const es = new EventSource(url);
  _sseSource = es;
  es.onopen    = () => { setConn(true); if (typeof _maybeAutoLoadSession === 'function') _maybeAutoLoadSession(); };
  es.onmessage = e => { try { handleSSE(JSON.parse(e.data)); } catch {} };
  es.onerror   = () => { setConn(false); setTimeout(connectSSE, 3000); };
}

function handleSSE(d) {
  switch (d.type) {
    case 'prompt_processing':
      // Pre-token stage: llama.cpp is still evaluating the prompt. Show how far
      // it has reached; it's removed as soon as the first token/think arrives.
      if (_awaitingReloadHistory || _streamStopped) break;
      _setGeneratingUI(true); _showPromptProcessing(d.value);
      break;

    case 'ai_token':
      if (_awaitingReloadHistory || _streamStopped || !_eventForDisplayed(d)) break;
      _setGeneratingUI(true); ensureStreamBubble();
      _streamAccum += (d.token || '');
      appendToStreamBody(d.token || ''); scrollChat();
      break;

    case 'ai_think_start':
      if (_awaitingReloadHistory || _streamStopped || !_eventForDisplayed(d)) break;
      _setGeneratingUI(true); ensureStreamBubble();
      ensureThinkBlock(); _streamIsThink = true; scrollChat();
      break;

    case 'ai_think':
      if (_awaitingReloadHistory || _streamStopped || !_eventForDisplayed(d)) break;
      // Lazily build the reasoning block (handles re-attaching to a generation
      // that entered <think> while a different session was displayed).
      _setGeneratingUI(true); ensureStreamBubble(); ensureThinkBlock(); _streamIsThink = true;
      if (_streamThinkBody) {
        const tb = _streamThinkBody;
        const tbStick = (tb.scrollHeight - tb.scrollTop - tb.clientHeight) < 24;
        tb.textContent += (d.token || '');
        if (tbStick) tb.scrollTop = tb.scrollHeight;   // follow reasoning inside its own box
        scrollChat();
      }
      break;

    case 'ai_think_end':
      if (_awaitingReloadHistory || _streamStopped || !_eventForDisplayed(d)) break;
      _streamIsThink = false;
      if (_streamThink) {
        const dot = _streamThink.querySelector('.think-streaming-dot');
        if (dot) dot.remove();
        const toggle = _streamThink.querySelector('.think-toggle');
        if (toggle) { const label = toggle.querySelector('.think-label'); if (label) label.textContent = 'reasoning'; }
      }
      break;

    case 'warning':
      // Server notice on its OWN channel (context full, trimmed, out of memory…)
      // — no longer scraped out of the model's text.
      if (_awaitingReloadHistory || _streamStopped) break;
      _showStreamWarning(d.text || '');
      break;

    case 'ai_message_done':
      _removePromptProcessing();
      if (_awaitingReloadHistory || !_eventForDisplayed(d)) break;
      finalizeStreamBubble(d.text || ''); _setGeneratingUI(false);
      break;

    case 'ai_generation_done':
      _removePromptProcessing();
      _setGeneratingUI(false);
      break;

    case 'ai_stopped':
      _removePromptProcessing();
      _streamStopped = true;              // ignore any stray tokens leaked after the stop
      if (d.reason === 'reload') {        // same-session reload: tear down quietly, wait for session_history
        _awaitingReloadHistory = true;
        _teardownStreamBubble(); _setGeneratingUI(false);
        break;
      }
      if (!_eventForDisplayed(d)) { _setGeneratingUI(false); break; }
      _teardownStreamBubble();
      _setGeneratingUI(false); appendDivider('STOPPED');
      break;

    case 'msg_queued':
      _showQueuedIndicator(d.text || '');
      break;

    case 'pending_msg_sending':
      _removeQueuedIndicator(); _setGeneratingUI(true);
      break;

    case 'ai_message':
      if (!_streamBubble) { appendMsg('ai', d.text, d.ts); executeCommands(d.text); }
      _setGeneratingUI(false);
      break;

    case 'user_message':
      _streamStopped = false;   // a new turn started (arrives after any auto-stop) — allow fresh stream
      if (!_lastSentByUser) appendMsg('user', d.text, d.ts);
      _lastSentByUser = false;
      break;

    case 'session_open':
      // A parallel command session just opened — auto-reveal the Tasks panel so
      // the user sees live commands, status, and remaining time without clicking.
      if (typeof openTasksPanel === 'function') openTasksPanel();
      break;

    case 'cmd_result':
      appendSysapiPlaceholder(d.ts);
      break;

    case 'systemapi_offline':
      appendSysapiOfflineWarning();
      break;

    case 'systemapi_Authentication':
      // user chose "Skip systemapi" this session — ignore further auth events
      if (typeof _systemapiSkipped !== 'undefined' && _systemapiSkipped) break;
      toast('systemapi authentication failed — re-login', 'err');
      appendSysapiAuthWarning();
      // show the systemapi password prompt on-screen (UI), not inside the chat
      if (typeof promptSystemapiLogin === 'function') promptSystemapiLogin();
      break;

    case 'model_loading':
      _setModelStatusUI('loading', '');
      toast('Model loading\u2026', 'info');
      _startModelPoll();
      loadLoadedModels();
      break;

    case 'model_ejected':
      _setModelStatusUI('idle', '');
      toast(d.message || 'Model ejected', 'warn');
      devRefresh();
      loadLoadedModels();
      break;

    case 'active_model_changed':
      refreshModelStatus();
      loadLoadedModels();
      toast(d.message || 'Active model changed', 'info');
      break;

    case 'session_history':
      _awaitingReloadHistory = false; _streamStopped = false;
      if (d.session !== undefined) _currentSessionName = d.session;
      if (_expectingNewSession) { _expectingNewSession = false; break; }
      clearChat(); appendDivider('SESSION LOADED');
      const renderHistory = () => {
        for (const msg of d.history) {
          const ts = msg.ts || d.ts, c = msg.content || '';
          if (msg.role === 'systemapi' || c.startsWith('[SYSTEM RESULT') || c.startsWith('systemapi:') || c.startsWith('systemapi_search:')) {
            appendSysapiPlaceholder(ts); continue;
          }
          if      (msg.role === 'user')                           appendMsg('user', c, ts);
          else if (msg.role === 'assistant' || msg.role === 'ai') appendMsg('ai',   c, ts);
        }
      };
      if (commands.length) renderHistory(); else loadCommands().then(renderHistory);
      setTimeout(_syncMsgIds, 100);
      break;

    case 'session_saved':   toast('Session saved', 'ok');   loadSessionList(); break;
    case 'session_loaded': {
      if (d.session !== undefined) _currentSessionName = d.session;
      const _msg  = d.message || '';
      const _isCP = _msg.toLowerCase().includes('checkpoint');
      toast(_isCP ? _msg : 'Session loaded', _isCP ? 'warn' : 'ok');
      loadSessionList(); _fetchSaveThinkState();
      break;
    }
    case 'session_new':
      _currentSessionName = null; _awaitingReloadHistory = false; _streamStopped = false;
      toast('New session started', 'info'); loadSessionList();
      clearChat(); appendDivider('NEW SESSION');
      _fetchSaveThinkState();
      break;
    case 'session_deleted': toast('Session deleted', 'warn'); loadSessionList(); break;
    case 'session_cleared': toast('Session history cleared', 'warn'); loadSessionList(); break;
    case 'commands_updated': commands = d.commands; renderCommands(); break;
    case 'history_trimmed':
      toast('History trimmed — ' + (d.remaining || 0) + ' msgs remain', 'warn'); break;
    case 'login_toggled':
      toast(d.login_required ? 'login ENABLED' : 'login DISABLED', d.login_required ? 'warn' : 'info'); break;
  }
}

// ─── Prompt-processing stage indicator ───────────────────────────
// Shown before any token streams (llama.cpp is still evaluating the prompt),
// updated with the % reached, and removed the moment real output begins.
function _showPromptProcessing(pct) {
  const stream = document.getElementById('chat-stream');
  if (!stream) return;
  let el = document.getElementById('prompt-proc');
  if (!el) {
    el = document.createElement('div');
    el.id = 'prompt-proc';
    el.className = 'prompt-proc';
    el.innerHTML = '<span class="prompt-proc-dot"></span>'
      + '<span class="prompt-proc-tag">Processing prompt</span>'
      + '<span class="prompt-proc-bar"><span class="prompt-proc-fill"></span></span>'
      + '<span class="prompt-proc-pct"></span>';
    stream.appendChild(el);
  }
  const p = Math.min(100, Math.max(0, Math.round(pct || 0)));
  el.querySelector('.prompt-proc-fill').style.width = p + '%';
  el.querySelector('.prompt-proc-pct').textContent = p + '%';
  scrollChat();
}
function _removePromptProcessing() {
  const el = document.getElementById('prompt-proc');
  if (el) el.remove();
}

// ─── Streaming bubble ─────────────────────────────────────────────
function ensureStreamBubble() {
  _removePromptProcessing();   // first real output → drop the processing stage
  if (_streamBubble) return;
  const stream = document.getElementById('chat-stream');
  const uiId   = _msgIdCounter++;
  const div    = document.createElement('div');
  div.className = 'msg ai streaming'; div.dataset.msgId = uiId;
  div.innerHTML = `
    <div class="msg-header">
      <span class="msg-who">FRTGG</span>
      <span class="msg-ts">${now()}</span>
      <span class="msg-id">#${uiId}</span>
      <button class="msg-edit-btn" onclick="editMsg(${uiId},this)" title="Edit message">&#9998;</button>
      <button class="msg-del-btn" onclick="deleteMsg(${uiId},this)" title="Delete message">&#10005;</button>
    </div>
    <div class="msg-body"></div>`;
  stream.appendChild(div);
  _streamBubble = div; _streamBody = div.querySelector('.msg-body');
  _streamIsThink = false; _streamAccum = ''; _streamBodyText = ''; scrollChat();
}
function ensureThinkBlock() {
  if (_streamThink) return;
  const thinkId = 'think-' + Date.now();
  const block   = document.createElement('div');
  block.className = 'think-block';
  block.innerHTML = `
    <button class="think-toggle" onclick="toggleThink('${thinkId}')">
      <span class="think-arrow" id="arr-${thinkId}">&#9658;</span>
      <span class="think-label">reasoning</span>
      <span class="think-streaming-dot"></span>
    </button>
    <div class="think-body" id="${thinkId}"></div>`;
  _streamBubble.insertBefore(block, _streamBody);
  _streamThink = block; _streamThinkBody = block.querySelector('.think-body');
}
function _resetWarnState() {
  _warnState = { inside: false, prefixBuf: '', atLineStart: true, blockEl: null };
}
function _ensureWarnBlock() {
  if (!_streamBubble || _warnState.blockEl) return;
  const block = document.createElement('div');
  block.className = 'warn-block';
  block.innerHTML = `<span class="warn-block-icon">&#9888;</span><span class="warn-block-text"><span class="warn-block-label">auto-trim</span></span>`;
  _streamBubble.appendChild(block);
  _warnState.blockEl = block.querySelector('.warn-block-text');
  scrollChat();
}
function _appendToBodyText(t) {
  if (!_streamBody || !t) return;
  _streamBodyText += t;
  // Fast path: no markdown markers yet → cheap text-node append (same as before, no flicker).
  if (_streamBodyText.indexOf('`') === -1 && _streamBodyText.indexOf('**') === -1) {
    const old = _streamBody.querySelector('.streaming-cursor'); if (old) old.remove();
    const last = _streamBody.lastChild;
    if (last && last.nodeType === 3) last.data += t;
    else _streamBody.appendChild(document.createTextNode(t));
    const cursor = document.createElement('span'); cursor.className = 'streaming-cursor';
    _streamBody.appendChild(cursor);
    return;
  }
  // Markdown present → live render the whole accumulated body. Completed `code`/**bold**
  // turn into formatting immediately; unclosed markers stay as plain text until they close.
  _streamBody.innerHTML = '';
  _streamBody.appendChild(renderStreamingText(_streamBodyText));
  const cursor = document.createElement('span'); cursor.className = 'streaming-cursor';
  _streamBody.appendChild(cursor);
}
function appendToStreamBody(token) {
  // Warnings now arrive on their own SSE "warning" event (see _showStreamWarning),
  // so the stream body is pure model text — just append it.
  if (!_streamBody || !token) return;
  _appendToBodyText(token);
}
// Render a server warning as a STANDALONE system line in the chat stream —
// never inside the AI bubble — so it clearly reads as an llm-server/system
// notice, not part of the model's reply.
function _showStreamWarning(text) {
  const reason = (text || '').trim();
  if (!reason) return;
  const stream = document.getElementById('chat-stream');
  if (!stream) { toast(reason, 'warn'); return; }
  const row = document.createElement('div');
  row.className = 'sys-warn';
  const tag = document.createElement('span');
  tag.className = 'sys-warn-tag';
  tag.textContent = '⚠ llm-server';
  row.appendChild(tag);
  row.appendChild(document.createTextNode(reason));
  stream.appendChild(row);
  scrollChat();
}
function _extractWarnings(text) {
  if (!text) return { clean: text, warnings: [] };
  const warnings = [];
  const clean = text.replace(/(^|\n)warning:>\s*([^\n]*)(?:\n|$)/g, (_m, p1, p2) => {
    warnings.push((p2 || '').trim());
    return p1;
  });
  return { clean, warnings };
}
function _appendWarnBlockTo(parent, reason) {
  const block = document.createElement('div');
  block.className = 'warn-block';
  block.innerHTML = `<span class="warn-block-icon">&#9888;</span><span class="warn-block-text"><span class="warn-block-label">auto-trim</span></span>`;
  block.querySelector('.warn-block-text').appendChild(document.createTextNode(reason));
  parent.appendChild(block);
}
function finalizeStreamBubble(fullText) {
  if (!_streamBubble) return;
  const cursor = _streamBubble.querySelector('.streaming-cursor'); if (cursor) cursor.remove();
  _streamBubble.classList.remove('streaming');
  const rawText = (fullText && fullText.trim()) ? fullText : _streamAccum;
  const { clean: textToRender, warnings: _finWarns } = _extractWarnings(rawText);
  if (textToRender && (textToRender.includes('`') || textToRender.includes('**')) && _streamBody) {
    _streamBody.innerHTML = '';
    _streamBody.appendChild(renderText(textToRender));
  }
  const cmds = detectCommands(textToRender || fullText);
  if (cmds.length) {
    const cmdDiv = document.createElement('div');
    cmdDiv.innerHTML = renderDetectedCmds(cmds);
    if (cmdDiv.firstElementChild) _streamBubble.appendChild(cmdDiv.firstElementChild);
    executeCommands(textToRender || fullText);
  }
  _streamBubble = null; _streamBody = null;
  _streamThink  = null; _streamThinkBody = null; _streamIsThink = false; _streamAccum = ''; _streamBodyText = '';
  _resetWarnState();
  scrollChat();
}
function toggleThink(id) {
  const body = document.getElementById(id);
  const arr  = document.getElementById('arr-' + id);
  if (!body) return;
  const open = body.classList.toggle('visible');
  if (arr) arr.classList.toggle('open', open);
}

// ─── Message helpers ──────────────────────────────────────────────
function renderText(raw) {
  const parts = raw.split(/(```[\s\S]*?```)/g);
  const frag  = document.createDocumentFragment();
  parts.forEach(part => {
    const fenceMatch = part.match(/^```(\w*)\n?([\s\S]*?)```$/);
    if (fenceMatch) {
      const lang = fenceMatch[1] || '';
      const code = fenceMatch[2].replace(/\n$/, '');
      const isShort = !code.trim().includes('\n');

      if (isShort) {
        // single-line / single-word → click-to-copy chip with hover tooltip
        frag.appendChild(_makeInlineCodeChip(code.trim()));
      } else {
        // multi-line → header bar (lang + Copy button) above the code
        const wrap = document.createElement('div');
        wrap.className = 'code-wrap';

        const bar = document.createElement('div');
        bar.className = 'code-bar';
        const langSpan = document.createElement('span');
        langSpan.className = 'code-lang';
        langSpan.textContent = lang || 'code';
        bar.appendChild(langSpan);
        const copyBtn = document.createElement('button');
        copyBtn.className = 'code-copy-btn';
        copyBtn.type = 'button';
        copyBtn.textContent = 'Copy';
        copyBtn.addEventListener('click', () => copyCodeText(code, copyBtn));
        bar.appendChild(copyBtn);

        const pre = document.createElement('code');
        pre.className = 'code-block';
        pre.appendChild(document.createTextNode(code));

        wrap.appendChild(bar);
        wrap.appendChild(pre);
        frag.appendChild(wrap);
      }
    } else if (part) {
      _appendTextWithTables(frag, part);
    }
  });
  return frag;
}

// ─── GFM tables ───────────────────────────────────────────────────
// Split a text run into table blocks and plain runs. A table is a header row
// (a line containing "|") immediately followed by a separator row of dashes
// (e.g. | :--- | ---: |). Non-table text is passed through to _appendInlineMd.
const _TABLE_SEP_RE = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/;
function _appendTextWithTables(frag, text) {
  const lines = text.split('\n');
  let i = 0, plainBuf = [];
  const flushPlain = () => {
    if (!plainBuf.length) return;
    _appendInlineMd(frag, plainBuf.join('\n'));
    plainBuf = [];
  };
  while (i < lines.length) {
    const header = lines[i];
    const sep    = lines[i + 1];
    // A table needs: a header line with a pipe, then a separator line.
    if (header && header.includes('|') && sep !== undefined && _TABLE_SEP_RE.test(sep)) {
      const headerCells = _splitTableRow(header);
      const aligns      = _splitTableRow(sep).map(c => {
        const l = c.startsWith(':'), r = c.endsWith(':');
        return l && r ? 'center' : r ? 'right' : l ? 'left' : '';
      });
      // Collect data rows until a line that's no longer part of the table.
      const rows = [];
      let j = i + 2;
      while (j < lines.length && lines[j].includes('|') && lines[j].trim() !== '') {
        rows.push(_splitTableRow(lines[j]));
        j++;
      }
      flushPlain();
      frag.appendChild(_buildTable(headerCells, aligns, rows));
      i = j;
      continue;
    }
    plainBuf.push(header);
    i++;
  }
  flushPlain();
}

// Split a "| a | b |" row into trimmed cell strings (honours escaped \| ).
function _splitTableRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|'))   s = s.slice(0, -1);
  return s.split(/(?<!\\)\|/).map(c => c.replace(/\\\|/g, '|').trim());
}

function _buildTable(headerCells, aligns, rows) {
  const wrap  = document.createElement('div');
  wrap.className = 'md-table-wrap';
  const table = document.createElement('table');
  table.className = 'md-table';

  const thead = document.createElement('thead');
  const htr   = document.createElement('tr');
  headerCells.forEach((cell, c) => {
    const th = document.createElement('th');
    if (aligns[c]) th.style.textAlign = aligns[c];
    _appendInlineMd(th, cell);
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  rows.forEach(cells => {
    const tr = document.createElement('tr');
    for (let c = 0; c < headerCells.length; c++) {
      const td = document.createElement('td');
      if (aligns[c]) td.style.textAlign = aligns[c];
      _appendInlineMd(td, cells[c] !== undefined ? cells[c] : '');
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  wrap.appendChild(table);
  return wrap;
}

// Streaming-safe render: same as renderText, but if a ``` fence is still open
// (odd count), render the trailing partial fence as an in-progress code block
// instead of leaking raw backticks into the text.
function renderStreamingText(raw) {
  const fenceCount = (raw.match(/```/g) || []).length;
  if (fenceCount % 2 === 1) {
    const idx    = raw.lastIndexOf('```');
    const before = raw.slice(0, idx);
    const after  = raw.slice(idx + 3);
    const frag   = document.createDocumentFragment();
    if (before) frag.appendChild(renderText(before));
    const nl   = after.indexOf('\n');
    const code = nl === -1 ? '' : after.slice(nl + 1);
    const wrap = document.createElement('div');
    wrap.className = 'code-wrap';
    const pre  = document.createElement('code');
    pre.className = 'code-block';
    pre.appendChild(document.createTextNode(code));
    wrap.appendChild(pre);
    frag.appendChild(wrap);
    return frag;
  }
  return renderText(raw);
}

// Build a click-to-copy inline code chip (shared by short fences and `inline` code).
function _makeInlineCodeChip(code) {
  const chip = document.createElement('code');
  chip.className   = 'code-inline';
  chip.textContent = code;
  chip.dataset.tip = 'Click to copy';
  chip.tabIndex    = 0;
  chip.setAttribute('role', 'button');
  chip.addEventListener('click', () => copyCodeText(code, chip));
  chip.addEventListener('keydown', e => {
    if (e.key === 'Enter' || (e.key === ' ') ||
        ((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C'))) {
      e.preventDefault(); copyCodeText(code, chip);
    }
  });
  return chip;
}

// Render inline markdown in a text run: `code` → copy chip, **bold** → <strong>.
function _appendInlineMd(frag, text) {
  const re = /`([^`\n]+?)`|\*\*([^\n]+?)\*\*/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    if (m[1] !== undefined) {
      frag.appendChild(_makeInlineCodeChip(m[1].trim()));
    } else {
      const strong = document.createElement('strong');
      strong.className = 'md-bold';
      strong.textContent = m[2];
      frag.appendChild(strong);
    }
    last = re.lastIndex;
  }
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
}

// ─── Copy-to-clipboard for code blocks / inline chips ─────────────
function copyCodeText(text, el) {
  const feedback = () => {
    if (!el) return;
    clearTimeout(el._copyTimer);
    if (el.classList.contains('code-copy-btn')) {
      el.innerHTML = '<svg class="ic" width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8.5 6.5 12 13 4.5"/></svg> Copied';
      el.classList.add('copied');
      el._copyTimer = setTimeout(() => { el.textContent = 'Copy'; el.classList.remove('copied'); }, 1200);
    } else {
      el.dataset.tip = 'Copied!';
      el.classList.add('copied');
      el._copyTimer = setTimeout(() => { el.dataset.tip = 'Click to copy'; el.classList.remove('copied'); }, 1200);
    }
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(feedback).catch(() => _fallbackCopyText(text, feedback));
  } else {
    _fallbackCopyText(text, feedback);
  }
}
function _fallbackCopyText(text, cb) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed'; ta.style.top = '-9999px'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  try { document.execCommand('copy'); } catch {}
  ta.remove();
  if (cb) cb();
}

function appendMsg(type, text, ts, whoOverride) {
  const stream = document.getElementById('chat-stream');
  const div    = document.createElement('div');
  const uiId   = _msgIdCounter++;
  div.className = `msg ${type}`; div.dataset.msgId = uiId;
  const whoMap = { user: 'YOU', ai: 'FRTGG', sysapi: 'SYSTEMAPI', 'cmd-result': whoOverride || 'RESULT' };
  const who    = whoOverride || whoMap[type] || type.toUpperCase();
  let _msgWarnings = [];
  if (type === 'ai') {
    const _ex = _extractWarnings(text);
    text = _ex.clean;
    _msgWarnings = _ex.warnings;
  }
  const _textForCmds = text.replace(/<think>[\s\S]*?<\/think>/g, '\n').trim();
  const detectedCmds = (type === 'ai' || type === 'sysapi') ? detectCommands(_textForCmds) : [];
  div.innerHTML = `
    <div class="msg-header">
      <span class="msg-who">${esc(who)}</span>
      <span class="msg-ts">${ts || now()}</span>
      <span class="msg-id">#${uiId}</span>
      <button class="msg-edit-btn" onclick="editMsg(${uiId},this)" title="Edit message">&#9998;</button>
      <button class="msg-del-btn" onclick="deleteMsg(${uiId},this)" title="Delete message">&#10005;</button>
    </div>
    <div class="msg-body"></div>
    ${detectedCmds.length ? renderDetectedCmds(detectedCmds) : ''}`;
  const thinkMatch = text.match(/<think>([\s\S]*?)<\/think>/);
  if (thinkMatch) {
    const thinkContent = thinkMatch[1].trim();
    const cleanText    = text.replace(/<think>[\s\S]*?<\/think>/, '').trim();
    const thinkId      = 'think-' + Date.now() + '-' + uiId;
    const thinkBlock   = document.createElement('div');
    thinkBlock.className = 'think-block';
    thinkBlock.innerHTML = `
      <button class="think-toggle" onclick="toggleThink('${thinkId}')">
        <span class="think-arrow" id="arr-${thinkId}">&#9658;</span>
        <span class="think-label">reasoning</span>
      </button>
      <div class="think-body" id="${thinkId}">${esc(thinkContent)}</div>`;
    div.querySelector('.msg-body').before(thinkBlock);
    div.querySelector('.msg-body').appendChild(renderText(cleanText));
  } else {
    div.querySelector('.msg-body').appendChild(renderText(text));
  }
  for (const w of _msgWarnings) if (w) _appendWarnBlockTo(div, w);
  stream.appendChild(div); scrollChat();
}
function appendSysapiPlaceholder(ts) {
  const stream = document.getElementById('chat-stream');
  const div    = document.createElement('div');
  const uiId   = _msgIdCounter++;
  div.className = 'msg sysapi-placeholder'; div.dataset.msgId = uiId;
  div.innerHTML = `
    <div class="msg-header">
      <span class="msg-who">SYSTEMAPI</span>
      <span class="msg-ts">${ts || now()}</span>
      <span class="msg-id">#${uiId}</span>
      <button class="msg-edit-btn" onclick="editMsg(${uiId},this)" title="Edit message">&#9998;</button>
      <button class="msg-del-btn" onclick="deleteMsg(${uiId},this)" title="Delete message">&#10005;</button>
    </div>
    <div class="msg-body">— system message —</div>`;
  stream.appendChild(div);
}
function appendSysapiOfflineWarning() {
  const stream = document.getElementById('chat-stream');
  // منع التكرار: لو آخر عنصر هو تحذير offline لا تضيف ثاني
  if (stream.lastElementChild && stream.lastElementChild.classList.contains('sysapi-offline-warn')) return;
  const div = document.createElement('div');
  div.className = 'sysapi-offline-warn';
  // ربط التحذير بآخر رسالة — لو الرسالة اتحذفت يتحذف معها
  const msgs = stream.querySelectorAll('.msg[data-msg-id]');
  if (msgs.length) div.dataset.linkedMsgId = msgs[msgs.length - 1].dataset.msgId;
  div.innerHTML = `
    <span class="sysapi-offline-warn-icon">&#9888;</span>
    <span class="sysapi-offline-warn-text">
      <strong>SYSTEMAPI OFFLINE</strong> — commands not executed.
      Check systemapi is running on <code>${esc(_cfg.sys_host)}:${esc(String(_cfg.sys_port))}</code>
    </span>`;
  stream.appendChild(div); scrollChat();
}
// Auth failure (systemapi reachable, but its password changed) — distinct from
// the offline warning. The actual re-login prompt is shown on-screen (UI), not
// inside the chat; this row is just an inline note with the correct hint.
function appendSysapiAuthWarning() {
  const stream = document.getElementById('chat-stream');
  if (stream.lastElementChild && stream.lastElementChild.classList.contains('sysapi-offline-warn')) return;
  const div = document.createElement('div');
  div.className = 'sysapi-offline-warn';
  const msgs = stream.querySelectorAll('.msg[data-msg-id]');
  if (msgs.length) div.dataset.linkedMsgId = msgs[msgs.length - 1].dataset.msgId;
  div.innerHTML = `
    <span class="sysapi-offline-warn-icon">&#9888;</span>
    <span class="sysapi-offline-warn-text">
      <strong>SYSTEMAPI OFFLINE</strong> — commands not executed.
      Check that your SYSTEMAPI password is set correctly.
    </span>`;
  stream.appendChild(div); scrollChat();
}
function clearChat() {
  _streamBubble = null; _streamBody = null;
  _streamThink  = null; _streamThinkBody = null; _streamIsThink = false;
  _queuedIndicatorEl = null; _msgIdCounter = 0; _setGeneratingUI(false);
  document.getElementById('chat-stream').innerHTML = '';
}
function appendDivider(label) {
  const stream = document.getElementById('chat-stream');
  const div    = document.createElement('div');
  div.className = 'chat-divider';
  div.innerHTML = `
    <div class="chat-divider-line"></div>
    <span class="chat-divider-text">${esc(label)}</span>
    <div class="chat-divider-line"></div>`;
  stream.appendChild(div); scrollChat();
}
// Stick-to-bottom: only auto-scroll while the user is already near the bottom.
// If they scrolled up to read history during generation, leave them be.
let _stickToBottom = true;
function _initChatScrollWatch() {
  const s = document.getElementById('chat-stream');
  if (!s || s._scrollWatchBound) return;
  s._scrollWatchBound = true;
  s.addEventListener('scroll', () => {
    const dist = s.scrollHeight - s.scrollTop - s.clientHeight;
    _stickToBottom = dist < 60;   // within 60px of the bottom = "following"
  }, { passive: true });
}
function scrollChat(force) {
  const s = document.getElementById('chat-stream');
  if (!s) return;
  if (force) _stickToBottom = true;
  if (!_stickToBottom) return;
  s.scrollTop = s.scrollHeight;
}

// ─── Delete / sync messages ───────────────────────────────────────
async function deleteMsg(uiId, btnEl) {
  const bubble = document.querySelector(`.msg[data-msg-id="${uiId}"]`);
  if (!bubble) return;
  try {
    const r = await api('/api/llm/delete_msg', { id: uiId });
    if (r.status === 'ok') {
      // Remove offline warnings linked to this message by ID
      document.querySelectorAll(`.sysapi-offline-warn[data-linked-msg-id="${uiId}"]`)
        .forEach(w => w.remove());
      // Also remove any offline warnings immediately adjacent to the deleted bubble
      // (handles cases where the link attribute wasn't set or IDs shifted)
      let sibling = bubble.nextElementSibling;
      while (sibling) {
        if (sibling.classList.contains('sysapi-offline-warn')) {
          const next = sibling.nextElementSibling;
          sibling.remove();
          sibling = next;
        } else if (sibling.classList.contains('sysapi-placeholder')) {
          // skip past sysapi placeholder to find a warning beyond it
          sibling = sibling.nextElementSibling;
        } else {
          break;
        }
      }
      bubble.classList.add('deleting');
      setTimeout(async () => { bubble.remove(); await _syncMsgIds(); }, 310);
      toast(`#${uiId} deleted`, 'warn');
    } else toast(r.message || 'Delete failed', 'err');
  } catch(e) { toast('Delete error: ' + e.message, 'err'); }
}

// Re-render a message body the same way appendMsg does (markdown + <think> block).
function _rerenderMsgBody(bubble, text) {
  const oldThink = bubble.querySelector('.think-block'); if (oldThink) oldThink.remove();
  const body = bubble.querySelector('.msg-body');
  body.innerHTML = '';
  const thinkMatch = text.match(/<think>([\s\S]*?)<\/think>/);
  if (thinkMatch) {
    const thinkContent = thinkMatch[1].trim();
    const cleanText    = text.replace(/<think>[\s\S]*?<\/think>/, '').trim();
    const thinkId      = 'think-' + Date.now();
    const thinkBlock   = document.createElement('div');
    thinkBlock.className = 'think-block';
    thinkBlock.innerHTML = `
      <button class="think-toggle" onclick="toggleThink('${thinkId}')">
        <span class="think-arrow" id="arr-${thinkId}">&#9658;</span>
        <span class="think-label">reasoning</span>
      </button>
      <div class="think-body" id="${thinkId}">${esc(thinkContent)}</div>`;
    body.before(thinkBlock);
    body.appendChild(renderText(cleanText));
  } else {
    body.appendChild(renderText(text));
  }
}

async function editMsg(uiId, btnEl) {
  const bubble = document.querySelector(`.msg[data-msg-id="${uiId}"]`);
  if (!bubble || bubble.classList.contains('editing')) return;

  // Fetch the true raw content from history — covers user / AI and hidden
  // systemapi messages whose content isn't shown in the DOM.
  let raw = '';
  try {
    const d    = await api('/api/llm/history', null, 'GET');
    const item = (d.history || []).find(h => String(h.id) === String(uiId));
    raw = item ? (item.content || '') : '';
  } catch(e) { toast('Could not load message: ' + e.message, 'err'); return; }

  const isPlaceholder = bubble.classList.contains('sysapi-placeholder');
  const body = bubble.querySelector('.msg-body');
  bubble.classList.add('editing');

  const editor = document.createElement('div');
  editor.className = 'msg-editor';
  editor.innerHTML = `
    <textarea class="msg-edit-area" spellcheck="false"></textarea>
    <div class="msg-edit-actions">
      <button type="button" class="msg-edit-save">Save</button>
      <button type="button" class="msg-edit-cancel">Cancel</button>
    </div>`;
  const ta      = editor.querySelector('.msg-edit-area');
  const saveBtn = editor.querySelector('.msg-edit-save');
  ta.value = raw;
  body.style.display = 'none';
  body.after(editor);

  const autosize = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 400) + 'px'; };
  autosize(); ta.addEventListener('input', autosize);
  ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);

  const cleanup = () => { editor.remove(); body.style.display = ''; bubble.classList.remove('editing'); };

  const save = async () => {
    const content = ta.value;
    saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
    try {
      const r = await api('/api/llm/edit_msg', { id: uiId, content });
      if (r.status === 'ok') {
        cleanup();
        if (!isPlaceholder) _rerenderMsgBody(bubble, content);
        toast(`#${uiId} edited`, 'ok');
      } else {
        saveBtn.disabled = false; saveBtn.textContent = 'Save';
        toast(r.message || 'Edit failed', 'err');
      }
    } catch(e) {
      saveBtn.disabled = false; saveBtn.textContent = 'Save';
      toast('Edit error: ' + e.message, 'err');
    }
  };

  saveBtn.addEventListener('click', save);
  editor.querySelector('.msg-edit-cancel').addEventListener('click', cleanup);
  ta.addEventListener('keydown', e => {
    if (e.key === 'Escape') { e.preventDefault(); cleanup(); }
    else if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); save(); }
  });
}

async function _syncMsgIds() {
  try {
    const d = await api('/api/llm/history', null, 'GET');
    const history     = d.history || [];
    const visibleMsgs = document.querySelectorAll('#chat-stream .msg[data-msg-id]');
    visibleMsgs.forEach((el, i) => {
      const oldId  = el.dataset.msgId;
      const realId = history[i] ? history[i].id : i;
      el.dataset.msgId = realId;
      const idLabel = el.querySelector('.msg-id');      if (idLabel) idLabel.textContent = `#${realId}`;
      const btn     = el.querySelector('.msg-del-btn'); if (btn)     btn.setAttribute('onclick', `deleteMsg(${realId},this)`);
      const ebtn    = el.querySelector('.msg-edit-btn'); if (ebtn)   ebtn.setAttribute('onclick', `editMsg(${realId},this)`);
      // Update any warnings that were linked to the old ID
      if (oldId !== String(realId)) {
        document.querySelectorAll(`.sysapi-offline-warn[data-linked-msg-id="${oldId}"]`)
          .forEach(w => { w.dataset.linkedMsgId = realId; });
      }
    });
    _msgIdCounter = visibleMsgs.length;
  } catch(e) { console.error('_syncMsgIds error:', e); }
}

// ─── Commands detection ───────────────────────────────────────────
// FIX: استبدلنا النسخة المكسورة (كان فيها }] بدل }) بالنسخة الصحيحة
// A standalone `timeout=NN` line (also NN/s, NNs). Mirrors _CMD_TIMEOUT_LINE in chat.py.
const TIMEOUT_LINE_RE = /^\s*timeout\s*=\s*\d+\s*\/?\s*s?\s*$/i;

// From index k, skip blank lines; if the landed line is a `timeout=NN` line,
// return { line, idx }, else null. Lets detectCommands fold a trailing timeout
// into a command's `full` (single-line, and braces with the timeout after `}`).
function _timeoutAt(lines, k) {
  let j = k;
  while (j < lines.length && !lines[j].trim()) j++;
  if (j < lines.length && TIMEOUT_LINE_RE.test(lines[j].trim()))
    return { line: lines[j].trim(), idx: j };
  return null;
}

function detectCommands(text) {
  if (!commands.length) return [];

  const lines = text.split('\n');
  const found = [];
  let i = 0;

  while (i < lines.length) {
    const line    = lines[i].trim();
    const lineLow = line.toLowerCase();

    if (!line) { i++; continue; }

    let matched = false;

    for (const cmd of commands) {
      const p = (cmd.prefix || '').toLowerCase();
      if (!p || !lineLow.startsWith(p)) continue;

      // ── END block ──────────────────────────────
      if (cmd.end) {
        const block = [line];
        i++;

        while (i < lines.length) {
          const innerTrimmed = lines[i].trim();
          const innerLineLow = innerTrimmed.toLowerCase();

          if (innerTrimmed.toUpperCase() === 'END') {
            block.push(innerTrimmed);
            i++;
            break;
          }

          const isOtherCmd = commands.some(other => {
            const op = (other.prefix || '').toLowerCase();
            return op && op !== p && innerLineLow.startsWith(op);
          });
          if (isOtherCmd) break;

          block.push(lines[i]); // الأصلية بدون trim لحفظ الـ indentation
          i++;
        }

        found.push({
          prefix:  cmd.prefix,
          color:   cmd.color || '#888',
          label:   cmd.label || cmd.prefix,
          full:    block.join('\n').trim(),
          useEnd:  true,
          hasEnd:  block[block.length - 1].trim().toUpperCase() === 'END',
          timeout: !!cmd.timeout
        });
        matched = true;
        break;
      }

      // ── BRACES block ───────────────────────────
      if (cmd.braces) {
        let combined = line;
        if (combined.includes('{') && !combined.includes('}')) {
          i++;
          while (i < lines.length) {
            combined += '\n' + lines[i];
            if (lines[i].includes('}')) { i++; break; }
            i++;
          }
        } else {
          i++;
        }
        // timeout=NN right after the closing brace belongs to this command
        if (cmd.timeout) {
          const tp = _timeoutAt(lines, i);
          if (tp) { combined += '\n' + tp.line; i = tp.idx + 1; }
        }
        found.push({
          prefix:  cmd.prefix,
          color:   cmd.color || '#888',
          label:   cmd.label || cmd.prefix,
          full:    combined.trim(),
          useEnd:  false,
          hasEnd:  false,
          timeout: !!cmd.timeout
        });
        matched = true;
        break;
      }

      let full = line;
      if (cmd.timeout) {
        const tp = _timeoutAt(lines, i + 1);
        if (tp) { full += '\n' + tp.line; i = tp.idx; }
      }
      found.push({
        prefix:  cmd.prefix,
        color:   cmd.color || '#888',
        label:   cmd.label || cmd.prefix,
        full:    full,
        useEnd:  false,
        hasEnd:  false,
        timeout: !!cmd.timeout
      });
      matched = true;
      i++;
      break;
    }

    if (!matched) i++;
  }

  return found;
}

function renderDetectedCmds(cmds) {
  if (!cmds.length) return '';
  return `<div class="detected-cmds">${cmds.map(c => {
    const rest     = c.full.slice(c.prefix.length);
    const endBadge = c.useEnd
      ? (c.hasEnd ? `<span class="dcmd-end-badge found">END &#10003;</span>` : `<span class="dcmd-end-badge missing">no END</span>`)
      : '';
    return `<div class="dcmd"><span class="dcmd-prefix" style="color:${esc(c.color)};border-color:${esc(c.color)}33">${esc(c.prefix)}</span><span class="dcmd-body">${esc(rest)}</span>${endBadge}</div>`;
  }).join('')}</div>`;
}
// ── Big background-logo glow when AI commands are dispatched ──────────
// Glows in each command's color. 1 cmd → 1 pulse in its color · 2 cmds →
// 2 pulses (cmd1 color then cmd2 color) · 3+ cmds → blend all colors → 1 pulse.
var _LOGO_SRC_HUE = 186;            // hue of the cyan logo art
function _logoHexToRgb(h) {
  h = (h || '').trim().replace('#', '');
  if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
  const n = parseInt(h, 16);
  if (isNaN(n) || h.length < 6) return { r: 0, g: 229, b: 255 };   // fallback cyan
  return { r: (n>>16)&255, g: (n>>8)&255, b: n&255 };
}
function _logoRgbHue(r, g, b) {
  const mx = Math.max(r,g,b), mn = Math.min(r,g,b), d = mx - mn;
  if (d === 0) return _LOGO_SRC_HUE;
  let h;
  if (mx === r)      h = ((g - b) / d) % 6;
  else if (mx === g) h = (b - r) / d + 2;
  else               h = (r - g) / d + 4;
  h *= 60; if (h < 0) h += 360;
  return h;
}
function _logoBlend(colors) {
  let r=0,g=0,b=0;
  colors.forEach(function (c) { const o=_logoHexToRgb(c); r+=o.r; g+=o.g; b+=o.b; });
  const n=colors.length;
  r=Math.round(r/n); g=Math.round(g/n); b=Math.round(b/n);
  // return HEX so _logoHexToRgb can re-parse it (identical colors → same color)
  return '#' + ((1<<24) + (r<<16) + (g<<8) + b).toString(16).slice(1);
}
function _ensureColorBurstStyle() {
  if (document.getElementById('app-logo-cburst-style')) return;
  const st = document.createElement('style');
  st.id = 'app-logo-cburst-style';
  st.textContent =
    '@keyframes logoColorBurst{' +
      '0%{opacity:.5;filter:hue-rotate(0deg) drop-shadow(0 0 45px rgba(0,229,255,.35));transform:scale(1)}' +
      '40%{opacity:1;filter:hue-rotate(var(--burst-rot,0deg)) saturate(1.7) brightness(1.4) drop-shadow(0 0 100px var(--burst-c,#00e5ff)) drop-shadow(0 0 40px var(--burst-c,#00e5ff));transform:scale(var(--burst-scale,1.03))}' +
      '100%{opacity:.5;filter:hue-rotate(0deg) drop-shadow(0 0 45px rgba(0,229,255,.35));transform:scale(1)}}';
  document.head.appendChild(st);
}
function _logoPulse(logo, color, dur, strong) {
  const c   = _logoHexToRgb(color);
  const rot = Math.round(_logoRgbHue(c.r, c.g, c.b) - _LOGO_SRC_HUE);
  logo.style.setProperty('--burst-c', color);
  logo.style.setProperty('--burst-rot', rot + 'deg');
  logo.style.setProperty('--burst-scale', strong ? '1.06' : '1.03');
  logo.classList.remove('glow');
  logo.style.animation = 'none';
  void logo.offsetWidth;            // reflow → restart animation
  logo.style.animation = 'logoColorBurst ' + dur + 's ease 1';
}
function _flashAppLogo(colors) {
  if (!colors || !colors.length) return;
  const logo = document.getElementById('app-bg-logo');
  if (!logo) return;
  _ensureColorBurstStyle();
  logo.style.zIndex = '40';         // lift above translucent panels for the flash
  let totalMs;
  if (colors.length >= 3) {
    const dur = 0.85;
    _logoPulse(logo, _logoBlend(colors), dur, true);   // mix all colors → one pulse
    totalMs = dur * 1000;
  } else {
    const dur = 0.55;
    let i = 0;
    (function next() {
      if (i >= colors.length) return;
      _logoPulse(logo, colors[i], dur, false);          // one pulse per command color
      i++;
      if (i < colors.length) setTimeout(next, dur * 1000);
    })();
    totalMs = dur * colors.length * 1000;
  }
  clearTimeout(logo._pulseT);
  logo._pulseT = setTimeout(function () {
    logo.style.animation = '';
    logo.style.zIndex = '';
    logo.style.removeProperty('--burst-c');
    logo.style.removeProperty('--burst-rot');
    logo.style.removeProperty('--burst-scale');
  }, totalMs + 100);
}

async function executeCommands(text) {
  const cmds = detectCommands(text); if (!cmds.length) return;
  _flashAppLogo(cmds.map(function (c) { return c.color || '#00e5ff'; }));
  try {
    const r = await api('/api/cmds_batch', { commands: cmds.map(c => c.full) });
    // A parallel session was opened — auto-reveal the Tasks panel so the user
    // sees the live commands, status, and remaining time without clicking.
    if (r && r.session_id) openTasksPanel();
  } catch(e) {
    if (e.message && (e.message.includes('503') || e.message.includes('systemapi_offline')))
      appendSysapiOfflineWarning();
    else toast('CMD error: ' + e.message, 'err');
  }
}

// ─── Live command sessions (Tasks panel) ──────────────────────────
// Polls /command_status: 5s normally, 1s while any command is "working".
// Self-stops when the Tasks tab is closed; switchTab() re-starts it.
let _cmdPollTimer = null;

// Reveal + activate the Tasks panel (called when a session opens). Works whether
// or not the header pill is present, and un-collapses the right panel if needed.
function openTasksPanel() {
  const rp = document.getElementById('right-panel');
  if (rp) rp.classList.remove('collapsed');
  const pill = document.querySelector('.h-pill[data-tab="cmdsessions"]');
  if (pill && typeof switchTab === 'function') {
    switchTab('cmdsessions', pill);          // owns active-state + starts polling
  } else {
    document.querySelectorAll('.rpanel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.h-pill').forEach(b => b.classList.remove('active'));
    const panel = document.getElementById('rp-cmdsessions');
    if (panel) panel.classList.add('active');
    startCmdPolling();
  }
}

function startCmdPolling() {
  clearTimeout(_cmdPollTimer);
  pollCommandStatus();
}

function _scheduleCmdPoll(ms) {
  clearTimeout(_cmdPollTimer);
  _cmdPollTimer = setTimeout(pollCommandStatus, ms);
}

async function pollCommandStatus() {
  const panel = document.getElementById('rp-cmdsessions');
  if (!panel || !panel.classList.contains('active')) { _cmdPollTimer = null; return; }  // tab closed
  try {
    const data = await api('/command_status', null, 'GET');
    const sessions = (data && data.sessions) ? data.sessions : (data && data.session_id ? [data] : []);
    renderCmdSessions(sessions);
    const anyActive  = sessions.some(s => !s.complete);
    const anyWorking = sessions.some(s =>
      Object.values(s.commands || {}).some(c => c.status === 'working'));
    // Only keep polling while something is actually running. Once everything is
    // complete we STOP (the panel keeps its last render); a new session re-starts
    // the loop via the session_open SSE event, and Stop/Start buttons refresh once.
    if (anyActive) _scheduleCmdPoll(anyWorking ? 1000 : 2000);
    else { clearTimeout(_cmdPollTimer); _cmdPollTimer = null; }
  } catch (e) {
    _scheduleCmdPoll(5000);
  }
}

function renderCmdSessions(sessions) {
  const el = document.getElementById('cmdsession-list');
  if (!el) return;
  if (!sessions || !sessions.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:20px">No active tasks</div>';
    return;
  }
  el.innerHTML = sessions.map(_renderOneSession).join('');
}

// Color for a command type, taken from the command defs in commands.json
// (the same `color` used by the Commands panel and the logo flash).
function _cmdColor(type) {
  if (!type) return 'var(--cyan)';
  const t = (type + ':').toLowerCase();
  const def = (commands || []).find(c => (c.prefix || '').toLowerCase() === t);
  return (def && def.color) ? def.color : 'var(--cyan)';
}

function _renderOneSession(s) {
  const cmds = s.commands || {};
  const rows = Object.keys(cmds).map(function (cid) {
    const c = cmds[cid];
    const raw  = c.status || 'pending';
    // A "-stoped" suffix marks a command in a paused session — show it yellow.
    const isStopped = raw.indexOf('stoped') !== -1;
    const base = raw.replace('-stoped', '');
    const color = _cmdColor(c.type);
    const badge = `<span class="st-badge st-${esc(base)}${isStopped ? ' st-stoped' : ''}">${esc(raw)}</span>`;
    let extra = '';
    if (base === 'working' && c.Remaining_time !== undefined) {
      const rem = (c.Remaining_time === '--') ? '—' : (c.Remaining_time + 's');
      extra = `<span class="pv">${rem} / ${esc(String(c.timeout || ''))}s</span>`;
    } else if (c.result_preview) {
      extra = `<span class="pv" title="${esc(c.result_preview)}">${esc(c.result_preview)}</span>`;
    }
    return `<div class="task-cmd${isStopped ? ' cmd-stoped' : ''}">`
         + `<span class="cmd-swatch" style="background:${esc(color)}"></span>`
         + `<span class="nm">${esc(cid)}</span>${badge}`
         + `<span class="tp" style="color:${esc(color)}">${esc(c.type || '')}</span>${extra}</div>`;
  }).join('');

  const held    = !!s.held;
  const stopped = !!s.stopped;
  let status;
  if (s.error)         status = `<span class="st-badge st-error">${esc(s.error)}</span>`;
  else if (s.complete) status = held ? '<span class="st-badge st-stoped">held</span>'
                                     : '<span class="st-badge st-done">done</span>';
  else if (stopped)    status = '<span class="st-badge st-stoped">stopped</span>';
  else                 status = '<span class="st-badge st-working">running</span>';

  const sid = esc(s.session_id || '');
  const btns = `<div class="task-btns">`
    + `<button class="btn btn-ghost" style="flex:1;font-size:11px;padding:4px" onclick="stopResult('${sid}')">stop</button>`
    + `<button class="btn btn-cyan"  style="flex:1;font-size:11px;padding:4px" onclick="startResult('${sid}')">start</button>`
    + `</div>`;

  return `<div class="task-card ${(held || stopped) ? 'stopped' : ''}">`
       + `<div class="task-head"><span class="task-id">${sid}</span>${status}</div>`
       + rows + btns + `</div>`;
}

async function stopResult(sid) {
  try { await api('/stop_result?session_id=' + encodeURIComponent(sid), null, 'POST'); pollCommandStatus(); }
  catch (e) { toast('stop: ' + e.message, 'err'); }
}
async function startResult(sid) {
  try { await api('/start_result?session_id=' + encodeURIComponent(sid), null, 'POST'); pollCommandStatus(); }
  catch (e) { toast('start: ' + e.message, 'err'); }
}
async function clearTasks() {
  try {
    const r = await api('/clear_task', {}, 'POST');
    toast(`Cleared ${r.count || 0} task(s)`, 'ok');
    pollCommandStatus();
  } catch (e) { toast('clear: ' + e.message, 'err'); }
}

// ─── Send message ─────────────────────────────────────────────────
async function sendMsg() {
  const el  = document.getElementById('input-field');
  const txt = el.value.trim(); if (!txt) return;
  el.value = ''; el.style.height = 'auto';
  localStorage.removeItem(DRAFT_KEY);   // sent — drop the saved draft
  _lastSentByUser = true; appendMsg('user', txt, now()); scrollChat(true);
  try {
    const r = await api('/api/send', { text: txt });
    if (r.status === 'queued') toast('Queued — waiting for AI to finish', 'warn');
  } catch(e) { toast(`Send error: ${e.message}`, 'err'); }
}

// Input field events are wired in shared.js _bootApp to avoid duplicate listeners

// ─── Commands panel ───────────────────────────────────────────────
async function loadCommands() {
  try { commands = await api('/api/commands', null, 'GET'); renderCommands(); }
  catch(e) { document.getElementById('commands-list').innerHTML = `<div style="color:var(--red);font-size:12px;padding:20px">Error: ${esc(e.message)}</div>`; }
}
function renderCommands() {
  const el = document.getElementById('commands-list');
  if (!commands.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:20px">No commands defined</div>';
    return;
  }
  el.innerHTML = commands.map((c, i) => `
    <div class="cmd-item">
      <div class="cmd-swatch" style="background:${esc(c.color||'#888')}"></div>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:6px">
          <span class="cmd-prefix-txt" style="color:${esc(c.color||'#888')}">${esc(c.prefix)}</span>
          ${c.braces  ? `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(255,179,0,.1);color:var(--amber);border:1px solid rgba(255,179,0,.2);letter-spacing:.5px;font-family:var(--mono)">{}</span>` : ''}
          ${c.end     ? `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(57,255,126,.1);color:var(--green);border:1px solid rgba(57,255,126,.2);letter-spacing:.5px;font-family:var(--mono)">END</span>` : ''}
          ${c.timeout ? `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(41,236,255,.1);color:#29ecff;border:1px solid rgba(41,236,255,.2);letter-spacing:.5px;font-family:var(--mono)">timeout</span>` : ''}
        </div>
        <span class="cmd-label-txt">${esc(c.label||'')}</span>
      </div>
      <button class="cmd-del" onclick="removeCommand(${i})" title="Remove">&#10005;</button>
    </div>`).join('');
}

let _bracesOn = false;
function toggleBraces() {
  _bracesOn = !_bracesOn;
  const track = document.getElementById('braces-track'), thumb = document.getElementById('braces-thumb');
  const badge = document.getElementById('braces-badge'), hint  = document.getElementById('braces-hint');
  if (_bracesOn) {
    track.style.background='rgba(255,179,0,.25)';thumb.style.background='var(--amber)';thumb.style.left='19px';
    badge.textContent='ON';badge.style.color='var(--amber)';badge.style.borderColor='rgba(255,179,0,.3)';hint.style.color='var(--amber)';
  } else {
    track.style.background='var(--border2)';thumb.style.background='var(--muted)';thumb.style.left='3px';
    badge.textContent='OFF';badge.style.color='var(--muted)';badge.style.borderColor='var(--border)';hint.style.color='var(--muted)';
  }
}
let _endOn = false;
function toggleEnd() {
  _endOn = !_endOn;
  const track = document.getElementById('end-track'), thumb = document.getElementById('end-thumb');
  const badge = document.getElementById('end-badge'), hint  = document.getElementById('end-hint');
  if (_endOn) {
    track.style.background='rgba(57,255,126,.25)';thumb.style.background='var(--green)';thumb.style.left='19px';
    badge.textContent='ON';badge.style.color='var(--green)';badge.style.borderColor='rgba(57,255,126,.3)';hint.style.color='var(--green)';
  } else {
    track.style.background='var(--border2)';thumb.style.background='var(--muted)';thumb.style.left='3px';
    badge.textContent='OFF';badge.style.color='var(--muted)';badge.style.borderColor='var(--border)';hint.style.color='var(--muted)';
  }
}
let _timeoutOn = false;
function toggleTimeout() {
  _timeoutOn = !_timeoutOn;
  const track = document.getElementById('timeout-track'), thumb = document.getElementById('timeout-thumb');
  const badge = document.getElementById('timeout-badge'), hint  = document.getElementById('timeout-hint');
  if (_timeoutOn) {
    track.style.background='rgba(41,236,255,.25)';thumb.style.background='#29ecff';thumb.style.left='19px';
    badge.textContent='ON';badge.style.color='#29ecff';badge.style.borderColor='rgba(41,236,255,.3)';hint.style.color='#29ecff';
  } else {
    track.style.background='var(--border2)';thumb.style.background='var(--muted)';thumb.style.left='3px';
    badge.textContent='OFF';badge.style.color='var(--muted)';badge.style.borderColor='var(--border)';hint.style.color='var(--muted)';
  }
}
async function addCommand() {
  const prefix = document.getElementById('new-cmd-prefix').value.trim();
  const label  = document.getElementById('new-cmd-label').value.trim();
  const color  = document.getElementById('new-cmd-color').value;
  if (!prefix) { toast('Prefix required', 'warn'); return; }
  const p = prefix.endsWith(':') ? prefix : prefix + ':';
  if (commands.find(c => c.prefix === p)) { toast('Prefix already exists', 'warn'); return; }
  commands.push({ prefix: p, color, label: label || p, braces: _bracesOn, end: _endOn, timeout: _timeoutOn });
  await saveCommands();
  document.getElementById('new-cmd-prefix').value = '';
  document.getElementById('new-cmd-label').value  = '';
  _bracesOn = false; toggleBraces(); _endOn = false; toggleEnd(); _timeoutOn = false; toggleTimeout();
  toast(`Added: ${p}`, 'ok');
}
async function removeCommand(i) { commands.splice(i, 1); await saveCommands(); toast('Removed', 'warn'); }
async function saveCommands() {
  try { await api('/api/commands', commands); renderCommands(); }
  catch(e) { toast('Save failed: ' + e.message, 'err'); }
}
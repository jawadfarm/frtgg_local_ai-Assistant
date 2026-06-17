// ══════════════════════════════════════════════════════════════════
//  multi-gpus.js — GPU System management
// ══════════════════════════════════════════════════════════════════

let _gpuList  = [];
let _gpuFlow  = {};
let _flowPrev = {};

// ─── Main entry (called on tab switch) ───────────────────────────
async function loadGpuSystem() {
  await Promise.all([loadGpuStatus(), loadGpuFlowStatus()]);
}

// ─── GET /api/gpus/status ────────────────────────────────────────
async function loadGpuStatus() {
  const container = document.getElementById('gpu-cards-container');
  container.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:16px">Detecting GPUs…</div>';
  try {
    const d = await api('/api/gpus/status');
    _gpuList = d.gpus || [];
    renderGpuCards(_gpuList);
    const multi = _gpuList.length >= 2;
    document.getElementById('gpu-subtab-bar').style.display    = multi ? 'flex' : 'none';
    document.getElementById('gpu-action-buttons').style.display = multi ? 'flex' : 'none';
    const placement = document.getElementById('gpu-placement-section');
    if (placement) placement.style.display = multi ? 'block' : 'none';
    if (multi) {
      renderFlowSliders(_gpuList);
      const opts = _gpuList.map(g =>
        `<option value="${g.index}">GPU ${g.index} — ${esc(g.name)}</option>`
      ).join('');
      const sel = document.getElementById('drive-gpu-select');
      if (sel) sel.innerHTML = opts;
      const dsel = document.getElementById('default-gpu-select');
      if (dsel) dsel.innerHTML = opts;
      loadPlacementSettings();
    }
  } catch(e) {
    container.innerHTML = `<div style="color:var(--red);font-size:11px;text-align:center;padding:16px">Error: ${esc(e.message)}</div>`;
  }
}

// ─── GET /api/gpus/flow ──────────────────────────────────────────
async function loadGpuFlowStatus() {
  try {
    const d = await api('/api/gpus/flow');
    _gpuFlow = d.flow || {};
    renderFlowStatus(d);
  } catch(e) {
    const el = document.getElementById('gpu-flow-status');
    if (el) el.innerHTML = `<div style="color:var(--red);font-size:11px">Error: ${esc(e.message)}</div>`;
  }
}

// ─── Placement settings (2+ GPUs only) ──────────────────────────
let _enableFlow = false;

function _renderEnableFlowToggle(on) {
  _enableFlow = on;
  const badge = document.getElementById('enable-flow-badge');
  const track = document.getElementById('enable-flow-track');
  const thumb = document.getElementById('enable-flow-thumb');
  const hint  = document.getElementById('placement-hint');
  if (badge) {
    badge.textContent       = on ? 'ON' : 'OFF';
    badge.style.color       = on ? 'var(--green)' : 'var(--muted)';
    badge.style.borderColor = on ? 'rgba(57,255,126,.3)' : 'var(--border)';
  }
  if (track) track.style.background = on ? 'rgba(57,255,126,.25)' : 'var(--border2)';
  if (thumb) { thumb.style.background = on ? 'var(--green)' : 'var(--muted)'; thumb.style.left = on ? '19px' : '3px'; }
  const dsel = document.getElementById('default-gpu-select');
  if (dsel) dsel.disabled = on;
  if (hint) hint.textContent = on
    ? 'flow ON — new models split across all GPUs by VRAM'
    : 'flow OFF — new models load fully onto the default GPU';
}

// GET /api/multi_gpu_settings
async function loadPlacementSettings() {
  try {
    const d = await api('/api/multi_gpu_settings');
    _renderEnableFlowToggle(!!d.enable_flow);
    const dsel = document.getElementById('default-gpu-select');
    if (dsel && d.default_gpu != null) dsel.value = String(d.default_gpu);
  } catch(e) {
    // gate (400 on <2 GPUs) — section is hidden anyway
  }
}

// POST /api/multi_gpu_settings  { enable_flow }
async function toggleEnableFlow() {
  const next = !_enableFlow;
  try {
    const d = await api('/api/multi_gpu_settings', { enable_flow: next });
    if (d.status === 'error') { toast(d.message || 'failed', 'err'); return; }
    _renderEnableFlowToggle(typeof d.enable_flow === 'boolean' ? d.enable_flow : next);
    toast(`flow ${_enableFlow ? 'enabled' : 'disabled'}`, _enableFlow ? 'ok' : 'warn');
  } catch(e) { toast('error: ' + e.message, 'err'); }
}

// POST /api/default_gpu  { default_gpu }
async function setDefaultGpu(idx) {
  try {
    const d = await api('/api/default_gpu', { default_gpu: parseInt(idx) });
    if (d.status === 'error') { toast(d.message || 'failed', 'err'); return; }
    toast(d.message || `default GPU set to ${idx}`, 'ok');
  } catch(e) { toast('error: ' + e.message, 'err'); }
}

// ─── Render GPU detection cards ──────────────────────────────────
function renderGpuCards(gpus) {
  const el = document.getElementById('gpu-cards-container');
  if (!gpus.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:16px">No NVIDIA GPUs detected</div>';
    return;
  }
  el.innerHTML = gpus.map(g => `
    <div style="display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--s2);margin-bottom:6px">
      <div style="width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);flex-shrink:0"></div>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;color:var(--txtbr);font-weight:600">GPU ${g.index} — ${esc(g.name)}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:1px">${g.vram_gb.toFixed(1)} GB VRAM</div>
      </div>
      <span style="font-size:11px;color:var(--cyan);font-weight:700;letter-spacing:1px">${g.vram_gb.toFixed(0)}G</span>
    </div>`).join('');
}

// ─── Render current flow info ─────────────────────────────────────
function renderFlowStatus(d) {
  const el = document.getElementById('gpu-flow-status');
  if (!el) return;
  const flow  = d.flow  || {};
  const model = d.model || null;
  if (!model && !Object.keys(flow).length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:11px">No model loaded / No flow configured</div>';
    return;
  }
  let html = '';
  if (model) {
    html += `<div class="dev-stat-row">
      <span class="dev-stat-key">Model</span>
      <span class="dev-stat-val" style="color:var(--cyan)">${esc(model)}</span>
    </div>`;
  }
  if (Object.keys(flow).length) {
    html += '<div class="dev-sep"></div>';
    for (const [idx, val] of Object.entries(flow)) {
      const [gpuName, pct] = val;
      html += `<div class="dev-stat-row">
        <span class="dev-stat-key">GPU ${idx} · ${esc(gpuName)}</span>
        <span class="dev-stat-val" style="color:var(--green)">${pct}%</span>
      </div>`;
    }
  }
  el.innerHTML = html;
}

// ─── Render sliders for manual flow ──────────────────────────────
function renderFlowSliders(gpus) {
  const el = document.getElementById('gpu-sliders-container');
  if (!el || !gpus.length) return;
  const defPct  = Math.floor(100 / gpus.length);
  const lastPct = 100 - defPct * (gpus.length - 1);
  _flowPrev = {};
  el.innerHTML = gpus.map((g, i) => {
    const pct = i === gpus.length - 1 ? lastPct : defPct;
    _flowPrev[g.index] = pct;
    return `
      <div>
        <div style="display:flex;justify-content:space-between;margin-bottom:5px">
          <span style="font-size:11px;color:var(--txtbr)">GPU ${g.index} · ${esc(g.name)}, ${g.vram_gb.toFixed(0)} GB</span>
          <span style="font-size:11px;color:var(--cyan);font-weight:700;min-width:38px;text-align:right" id="gpu-pct-label-${g.index}">${pct}%</span>
        </div>
        <input type="range" min="1" max="99" value="${pct}" id="gpu-slider-${g.index}"
          oninput="onGpuSliderInput(${g.index},this.value)"
          style="width:100%;accent-color:var(--cyan);cursor:pointer;margin-bottom:5px">
        <input type="number" class="f-input" min="1" max="99" value="${pct}" id="gpu-input-${g.index}"
          oninput="onGpuInputChange(${g.index},this.value)"
          style="width:72px;font-size:12px;padding:5px 8px">
      </div>`;
  }).join('<div style="height:1px;background:var(--border);margin:6px 0"></div>');
  updateGpuTotal();
}

// ─── Slider / input sync ─────────────────────────────────────────
function onGpuSliderInput(idx, val) {
  const inp = document.getElementById(`gpu-input-${idx}`);
  const lbl = document.getElementById(`gpu-pct-label-${idx}`);
  if (inp) inp.value       = val;
  if (lbl) lbl.textContent = val + '%';
  updateGpuTotal();
}

function onGpuInputChange(idx, val) {
  const slider = document.getElementById(`gpu-slider-${idx}`);
  const lbl    = document.getElementById(`gpu-pct-label-${idx}`);
  if (slider) slider.value    = val;
  if (lbl)    lbl.textContent = (val || 0) + '%';
  updateGpuTotal();
}

function updateGpuTotal() {
  const total = _gpuList.reduce((sum, g) => {
    return sum + (parseInt(document.getElementById(`gpu-input-${g.index}`)?.value) || 0);
  }, 0);
  const ok  = total === 100;
  const lbl = document.getElementById('gpu-total-label');
  if (lbl) { lbl.textContent = `Total: ${total}%`; lbl.style.color = ok ? 'var(--green)' : 'var(--red)'; }
  const applyBtn = document.getElementById('gpu-apply-btn');
  if (applyBtn) applyBtn.disabled = !ok;
}

// ─── POST /api/gpus/flow ─────────────────────────────────────────
async function applyGpuFlow() {
  const body = {};
  for (const g of _gpuList) {
    body[String(g.index)] = document.getElementById(`gpu-input-${g.index}`)?.value || '0';
  }
  const total = Object.values(body).reduce((s, v) => s + parseInt(v), 0);
  if (total !== 100) { toast(`percentages must total 100%, got ${total}%`, 'err'); return; }
  try {
    const d = await api('/api/gpus/flow', body);
    toast(d.message || 'flow applied', d.status === 'error' ? 'err' : 'ok');
    if (d.status !== 'error') {
      _flowPrev = {...body};
      await loadGpuFlowStatus();
    }
  } catch(e) { toast('error: ' + e.message, 'err'); }
}

function resetGpuFlow() {
  for (const g of _gpuList) {
    const prev   = _flowPrev[g.index] ?? 0;
    const inp    = document.getElementById(`gpu-input-${g.index}`);
    const slider = document.getElementById(`gpu-slider-${g.index}`);
    const lbl    = document.getElementById(`gpu-pct-label-${g.index}`);
    if (inp)    inp.value       = prev;
    if (slider) slider.value    = prev;
    if (lbl)    lbl.textContent = prev + '%';
  }
  updateGpuTotal();
}

// ─── POST /api/gpus/auto_flow ────────────────────────────────────
async function autoGpuFlow() {
  if (_gpuList.length < 2) { toast('auto flow requires 2+ GPUs', 'warn'); return; }
  try {
    const d = await api('/api/gpus/auto_flow', {});
    toast(d.message || 'auto flow applied', d.status === 'error' ? 'err' : 'ok');
    if (d.status !== 'error') {
      await loadGpuFlowStatus();
      await loadGpuStatus();
    }
  } catch(e) { toast('error: ' + e.message, 'err'); }
}

// ─── POST /api/model/restart ─────────────────────────────────────
async function restartGpuModel() {
  try {
    const d = await api('/api/model/restart', {});
    toast(d.message || 'model restarting…', d.status === 'error' ? 'err' : 'ok');
  } catch(e) { toast('error: ' + e.message, 'err'); }
}

// ─── Drive section toggle ────────────────────────────────────────
function openDriveSection() {
  const sec = document.getElementById('drive-section');
  if (!sec) return;
  const visible = sec.style.display !== 'none';
  sec.style.display = visible ? 'none' : 'block';
}

// ─── POST /api/model/gpus/drive ──────────────────────────────────
async function driveToGpu() {
  const idx  = parseInt(document.getElementById('drive-gpu-select')?.value);
  const name = document.getElementById('drive-model-name')?.value.trim();
  if (isNaN(idx)) { toast('select a GPU', 'warn'); return; }
  if (!name)      { toast('enter model name', 'warn'); return; }
  try {
    const d = await api('/api/model/gpus/drive', { index_gpu: idx, model_name: name });
    toast(d.message || `driving '${name}' to GPU ${idx}`, d.status === 'error' ? 'err' : 'ok');
    if (d.status !== 'error') {
      document.getElementById('drive-section').style.display = 'none';
      document.getElementById('drive-model-name').value = '';
    }
  } catch(e) { toast('error: ' + e.message, 'err'); }
}

// ─── Subtab switching ────────────────────────────────────────────
function switchGpuSubtab(name, btn) {
  document.querySelectorAll('.gpu-sub-pill').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  const isStatus = name === 'status';
  document.getElementById('gpu-sub-status').style.display    = isStatus ? 'flex' : 'none';
  document.getElementById('gpu-sub-flow').style.display      = isStatus ? 'none' : 'flex';
  document.getElementById('gpu-footer-status').style.display = isStatus ? 'flex' : 'none';
  document.getElementById('gpu-footer-flow').style.display   = isStatus ? 'none' : 'flex';
}

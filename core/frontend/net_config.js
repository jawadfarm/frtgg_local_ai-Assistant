/* ─────────────────────────────────────────────────────────────────────────
 * net_config.js — shared connection settings for the frtgg operator pages
 * (ui_json.html, prompt_manager.html, server_settings.html).
 *
 * NOTE: this file was missing from the project and has been reconstructed
 * from how the three pages consume it. The wire protocol it relies on is the
 * one documented in those pages:
 *
 *   GET http://<llm-host>:<llm-http-port>/server_settings   →  JSON, e.g.
 *   {
 *     "llm-server_TCP":  8111,  "llm-server_HTTP":  8112,
 *     "prompt-api_TCP":  8211,  "prompt-api_HTTP":  8212
 *   }
 *
 * If you still have the ORIGINAL net_config.js, restore it instead — this is a
 * drop-in compatible reconstruction, not necessarily byte-identical.
 *
 * Public API (all attached to window, called bare by the pages):
 *   netLoad()                         → raw saved object {llm_server?, promptapi?}
 *   netGetLlm()                       → {host, port}  (the llm-server HTTP endpoint)
 *   netGetPromptApi()                 → {host, port}  (discovered Prompt API)
 *   netGetPromptApiBase()             → "http://host:port"
 *   netGetPromptApiLabel()            → "host:port"
 *   netSetLlm(host, port)             → persist llm-server endpoint
 *   netRefreshFromLlm()               → async; re-discover Prompt API from saved
 *                                       llm-server. Returns settings or null.
 *   netResolveAndSave(host, port, cb) → async; save llm-server, resolve the
 *                                       Prompt API (direct or via discovery),
 *                                       persist it. Returns {settings, promptapi, mode}.
 * ───────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  const KEY = 'frtgg_net_config';
  const DEFAULT_LLM = { host: '127.0.0.1', port: 8112 };

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; }
    catch { return {}; }
  }
  function save(o) { localStorage.setItem(KEY, JSON.stringify(o)); }

  function getLlm() {
    const s = load().llm_server || {};
    return { host: s.host || DEFAULT_LLM.host, port: parseInt(s.port) || DEFAULT_LLM.port };
  }
  function getPromptApi() {
    const s = load().promptapi || {};
    if (s.host && s.port) return { host: s.host, port: parseInt(s.port) };
    return getLlm(); // sensible fallback before discovery has run
  }
  function getPromptApiBase()  { const p = getPromptApi(); return `http://${p.host}:${p.port}`; }
  function getPromptApiLabel() { const p = getPromptApi(); return `${p.host}:${p.port}`; }

  function setLlm(host, port) {
    const o = load(); o.llm_server = { host: String(host).trim(), port: parseInt(port) }; save(o);
  }
  function setPromptApi(host, port) {
    const o = load(); o.promptapi = { host: String(host).trim(), port: parseInt(port) }; save(o);
  }

  async function fetchJSON(url, ms = 5000) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms);
    try {
      const r = await fetch(url, { signal: ctl.signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } finally { clearTimeout(t); }
  }

  // Is something already serving the Prompt API at this base? A 401/403 still
  // means the server is up (just auth-gated), so treat that as reachable.
  async function reachable(base, ms = 4000) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms);
    try {
      const r = await fetch(`${base}/api/prompt`, { signal: ctl.signal, method: 'GET' });
      return r.ok || r.status === 401 || r.status === 403;
    } catch (e) { return false; }
    finally { clearTimeout(t); }
  }

  async function refreshFromLlm() {
    const llm = load().llm_server;
    if (!llm || !llm.host) return null;
    const settings = await fetchJSON(`http://${llm.host}:${parseInt(llm.port)}/server_settings`);
    const paPort = parseInt(settings['prompt-api_HTTP']);
    if (paPort) setPromptApi(llm.host, paPort);
    return settings;
  }

  async function resolveAndSave(host, port, cb) {
    host = String(host || '').trim();
    port = parseInt(port);
    if (!host || isNaN(port)) throw new Error('enter server ip and port');

    cb && cb(`saving ${host}:${port} …`);
    setLlm(host, port);
    const base = `http://${host}:${port}`;

    // 1) Try the address directly as the Prompt API.
    cb && cb(`probing ${host}:${port} directly …`);
    if (await reachable(base)) {
      setPromptApi(host, port);
      return { settings: null, promptapi: { host, port }, mode: 'direct' };
    }

    // 2) Ask the server for its settings and discover the Prompt API.
    cb && cb('asking server for /server_settings …');
    let settings;
    try {
      settings = await fetchJSON(`${base}/server_settings`);
    } catch (e) {
      throw new Error(`server unreachable at ${host}:${port}`);
    }
    const paPort = parseInt(settings['prompt-api_HTTP']);
    if (!paPort) throw new Error('server_settings missing prompt-api_HTTP');
    setPromptApi(host, paPort);
    cb && cb(`discovered Prompt API at ${host}:${paPort}`);
    return { settings, promptapi: { host, port: paPort }, mode: 'discovered' };
  }

  Object.assign(window, {
    netLoad:              load,
    netGetLlm:            getLlm,
    netGetPromptApi:      getPromptApi,
    netGetPromptApiBase:  getPromptApiBase,
    netGetPromptApiLabel: getPromptApiLabel,
    netSetLlm:            setLlm,
    netSetPromptApi:      setPromptApi,
    netRefreshFromLlm:    refreshFromLlm,
    netResolveAndSave:    resolveAndSave,
  });
})();

"""
prompt_api.py
Merged server (llm-server + promptapi) — single process, four listeners:
  LLM    TCP socket on port 8111   (chat / inject commands)
  LLM    HTTP REST  on port 8112   (token-gated + /v1 OpenAI API)
  Prompt TCP socket on port 8202   (system_prompt.json GUI commands)
  Prompt HTTP REST  on port 8203   (system_prompt.json web bridge — token-gated)
Ports are configurable via llm-server_settings.json / the /server_settings endpoint.

llama.cpp backend:
  - Launches llama.cpp server automatically (llama-server / llama.cpp)
  - Auto-detects GPU backend: NVIDIA CUDA / AMD ROCm / Intel SYCL / Vulkan — uses GPU layers if available
  - Loads any .gguf file by full path via POST /api/llm/load-model
  - reasoning_effort passed natively to capable models (gpt-oss/Harmony); plain
    on/off thinking otherwise. Capability auto-detected from the chat template.

TCP commands (LLM port 8111):
  __reload_prompt__               -> reload system_prompt.json
  json_prompt:<content>           -> inject context
  __clear_inject__                -> clear injected context
  <any other text>                -> send to LLM and speak response

TCP commands (Prompt port 8202 — GUI):
  __get_prompt__
  __set_prompt__:<json>
  __add_prompt__:<key>:<json_section>
  __del_prompt__:<key>

Auth (password + token; HTTP only — TCP ports 8111/8202 stay open).
Unified across BOTH HTTP servers: one token store, one auth.json, one
login_required flag. The auth endpoints below are served on BOTH HTTP ports:
  POST /set_password?password=..  -> first-run bootstrap; only while no password
                                     is set (file missing/corrupt/empty pass)
  POST /login                     -> {"password":".."} -> {"token":".."} (24h TTL)
                                     401 + "No password has been set yet…" if unset
  POST /change_password           -> {"old_password":"..","new_password":".."}
  POST /login_required            -> {"password":"..","login_required": true/false}
  POST /openai_key                -> {"password":"..","new_key":".."}  (set /v1 Bearer key)
  GET  /openai_key?token=..       -> {"openaikey":".."}                (token-gated)
  Every other HTTP endpoint (except /v1/* and the three above) requires the token
  via  X-Auth-Token: <token>  (or ?token= / JSON "token") while login_required is
  true. When login_required is false the token gate is disabled and /login issues
  a token for any password. auth.json: {"pass": "<sha256>", "login_required": bool}

HTTP endpoints:
  POST /api/llm/raw               -> send raw command
  POST /api/llm/reload            -> reload system_prompt.json
  POST /api/llm/stop              -> stop current LLM generation

  POST /api/llm/load-model        -> load a .gguf model  {"path": "C:/models/foo.gguf"}
                                     placement decided by gpu_placement.json; auto-activates
  GET  /api/llm/model-status      -> current model load status

  -- Multi-model (multiple models loaded at once, one llama-server each) --
  GET  /loaded_model              -> {index: [model_name, gpu_name, gpu_index]} for all loaded
  GET  /active_model              -> the model currently active in chat
  POST /active_model?index=N      -> switch the active chat model by index (history unchanged)
  POST /eject_model?index=N       -> eject one loaded model by index
  GET  /default_gpu               -> get default GPU index (gpu_placement.json)        [2+ GPUs only]
  POST /default_gpu               -> set default GPU index  {"default_gpu": N}       [2+ GPUs only]
  GET  /multi_gpu_settings        -> {"enable_flow": bool, "default_gpu": N}         [2+ GPUs only]
  POST /multi_gpu_settings        -> update load placement {"enable_flow":..,"default_gpu":..} [2+ GPUs only]

  GET  /api/llm/saved-models      -> list saved models from saved_models.json
  POST /api/llm/saved-models      -> add new model {"name": "...", "path": "...", "supports_thinking": true/false/null}
  POST /api/llm/saved-models/set-thinking -> set thinking flag {"path": "...", "supports_thinking": true/false}

  GET  /api/llm/prompt            -> view current system prompt
  GET  /api/llm/settings          -> get current LLM settings
  POST /api/llm/settings          -> update LLM settings
  POST /api/llm/settings/reset    -> reset settings to defaults

  GET  /api/llm/history           -> view conversation history
  POST /api/llm/history/clear     -> clear conversation history
  POST /api/llm/delete_msg        -> delete a message by id  {"id": 0}
  POST /api/llm/edit_msg          -> edit a message by id    {"id": 0, "content": "..."}

  GET  /api/llm/sessions          -> list all saved sessions
  POST /api/llm/sessions/save     -> save current session   {"name": "my-session"}
  POST /api/llm/sessions/load     -> load a session         {"name": "my-session"}
  POST /api/llm/sessions/new      -> start fresh session    {"save_current_as": "name"} (optional)
  POST /api/llm/sessions/delete   -> delete a session       {"name": "my-session"}

  POST /api/llm/save-thinking     -> toggle think saving    {"save": true/false, "session_name": "name"}

  GET  /api/dev/config            -> get developer config + token stats
  POST /api/dev/config            -> update developer config
  POST /api/dev/eject             -> kill llama-server and unload model
  POST /api/dev/trim_history      -> trim history {"keep": N}  (0 = clear all)

  -- Prompt management --
  POST /api/llm/stop-prompt       -> disable system prompt entirely
  POST /api/llm/new-prompt        -> create new prompt {"name":"..","prompt":"..","use_apis":bool}
  POST /api/llm/delete-prompt     -> delete a prompt file {"name":".."}
  POST /api/llm/edit-prompt       -> edit existing prompt {"name":"..","edprompt":".."}
  GET  /api/llm/use-prompt?name=  -> activate a saved prompt
  GET  /api/llm/get-prompt?name=  -> get prompt file content
  GET  /api/llm/get-promptn       -> list all saved prompt names

OpenAI-compatible endpoints (standard compliance — stateless passthrough to the
active/loaded llama.cpp instance; supports all OpenAI features incl. streaming,
tools/function calling, response_format, logprobs, embeddings, etc.).
Auth: clients must send  Authorization: Bearer <openaikey>  (auth.json
"openaikey", default "frtgg" — see OPENAI_API_KEY / POST /openai_key):
  GET  /v1/models                 -> list loaded models (OpenAI list shape)
  GET  /v1/models/{id}            -> single model object
  POST /v1/chat/completions       -> chat completions (stream + non-stream)
  POST /v1/completions            -> legacy text completions
  POST /v1/embeddings             -> embeddings
  (any other /v1/* is transparently proxied)

Prompt-API HTTP endpoints (port 8203 — web bridge, token-gated):
  POST /api/login                 -> body: {password} -> {status, token}  (alias of /login)
  GET  /api/prompt                -> returns {"data": {<key>: section, ...}}
  POST /api/prompt/add            -> body: {key, section}
  POST /api/prompt/del            -> body: {key}
  POST /api/prompt/block          -> body: {key, block?}  toggle/set section block flag
  POST /api/prompt/blocks         -> list block flags for all sections
  POST /api/prompt/set            -> body: {data: {...}}

Server settings (served on BOTH HTTP ports):
  GET  /server_settings           -> current listener ports (NO auth required)
                                     {"llm-server_TCP": 8111, "llm-server_HTTP": 8112,
                                      "prompt-api_TCP": 8202, "prompt-api_HTTP": 8203}
  POST /server_settings           -> update ports (any subset of the four keys).
                                     Auth: valid token OR {"password": ".."} in the body
                                     (no auth needed while login_required is false).
                                     Saved to llm-server_settings.json; applied on next restart.
"""

import socket
import struct
import threading
import time
import subprocess
import shutil
import json
import os
import sys
import re as _re
import hashlib
import secrets
import requests
try:
    import psutil
except ImportError:
    psutil = None
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Force UTF-8 console output. On Arabic/legacy Windows the console codepage is
# cp1256, which can't encode the box-drawing banner or Arabic text — printing
# them raises UnicodeEncodeError and kills the server at startup before the
# TCP/HTTP servers ever start. errors="replace" keeps any stray char harmless.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROMPT_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.json")
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
MODELS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved_models.json")
PROMPTS_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_settings.json")
MULTI_GPU_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpu_placement.json")
PASSWORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.json")
SERVER_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm-server_settings.json")
today = time.strftime("%Y-%m-%d", time.localtime())


os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(PROMPTS_DIR,  exist_ok=True)

BIND_HOST = "0.0.0.0"

# ── Listener ports (llm-server_settings.json) ─────────────────────────
DEFAULT_SERVER_SETTINGS = {
    "llm-server_TCP":  8111,
    "llm-server_HTTP": 8112,
    "prompt-api_TCP":  8202,
    "prompt-api_HTTP": 8203,
}

def _load_server_settings() -> dict:
    if not os.path.exists(SERVER_SETTINGS_FILE):
        _save_server_settings(dict(DEFAULT_SERVER_SETTINGS))
        return dict(DEFAULT_SERVER_SETTINGS)
    try:
        with open(SERVER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        merged = dict(DEFAULT_SERVER_SETTINGS)
        if isinstance(saved, dict):
            for key in DEFAULT_SERVER_SETTINGS:
                if key in saved:
                    try:
                        merged[key] = int(saved[key])
                    except (TypeError, ValueError):
                        pass
        return merged
    except (json.JSONDecodeError, ValueError, OSError):
        _save_server_settings(dict(DEFAULT_SERVER_SETTINGS))
        return dict(DEFAULT_SERVER_SETTINGS)

def _save_server_settings(settings: dict):
    try:
        with open(SERVER_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[server-settings] Failed to save llm-server_settings.json: {e}")

_srv_ports = _load_server_settings()
LLM_TCP_PORT     = _srv_ports["llm-server_TCP"]
LLM_HTTP_PORT    = _srv_ports["llm-server_HTTP"]
PROMPT_TCP_PORT  = _srv_ports["prompt-api_TCP"]
PROMPT_HTTP_PORT = _srv_ports["prompt-api_HTTP"]

# Shared secret required on the OpenAI-compatible /v1/* endpoints.
# Clients must send  Authorization: Bearer frtgg
OPENAI_API_KEY = "frtgg"

# ══════════════════════════════════════════════════════════════════
#  PASSWORD / LOGIN  (ported from chat.py — HTTP auth only; TCP is open)
# ══════════════════════════════════════════════════════════════════
def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _load_pw_cfg():
    """Load auth.json. A missing OR corrupted file (bad JSON / not an object)
    degrades gracefully to the unconfigured default so the server stays usable
    and /set_password can bootstrap it."""
    if not os.path.exists(PASSWORD_FILE):
        return {"pass": "", "login_required": True}
    try:
        with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return {"pass": "", "login_required": True}
        return cfg
    except (json.JSONDecodeError, ValueError, OSError):
        return {"pass": "", "login_required": True}

def _save_pw_cfg(cfg):
    with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def _is_login_required():
    return _load_pw_cfg().get("login_required", True)

def _password_is_set():
    """True only when a real password exists. Covers all 'unconfigured' cases —
    missing file, corrupted file, or a missing/empty `pass` field — because
    _load_pw_cfg normalises those to pass=''."""
    return bool(_load_pw_cfg().get("pass"))

_tokens = {}
_tokens_lock = threading.Lock()
TOKEN_TTL = 86400   # 24 hours

def _issue_token():
    tok = secrets.token_hex(32)
    now = time.time()
    with _tokens_lock:
        expired = [t for t, exp in _tokens.items() if exp < now]
        for t in expired:
            del _tokens[t]
        _tokens[tok] = now + TOKEN_TTL
    return tok

def _token_valid(tok):
    with _tokens_lock:
        exp = _tokens.get(tok)
        if exp is None:
            return False
        if time.time() > exp:
            del _tokens[tok]
            return False
        return True

def _get_openai_key():
    """The Bearer key required on /v1/*. Sourced from auth.json's
    "openaikey" field, falling back to the OPENAI_API_KEY default when unset."""
    key = (_load_pw_cfg().get("openaikey") or "").strip()
    return key or OPENAI_API_KEY

# ── Shared auth endpoint logic (served on BOTH HTTP ports) ────────
# Each helper returns (payload_dict, http_status) so the two handler classes
# dispatch to identical logic and produce identical responses.
def _auth_set_password(new_pw):
    """POST /set_password — bootstrap only while no password is configured."""
    if _password_is_set():
        return {"status": "error",
                "message": "password already set; use /change_password"}, 409
    if not new_pw:
        return {"status": "error", "message": "password query param is required"}, 400
    cfg = _load_pw_cfg()
    cfg["pass"] = _sha256(new_pw)
    cfg.setdefault("login_required", True)
    if not cfg.get("openaikey"):
        cfg["openaikey"] = new_pw   # mirror first-run: key defaults to the password
    _save_pw_cfg(cfg)
    return {"status": "ok", "message": "password set"}, 200

def _auth_login(body):
    """POST /login (and the legacy /api/login alias) — password -> token."""
    pw  = (body or {}).get("password", "")
    cfg = _load_pw_cfg()
    if not cfg.get("pass", ""):
        # Unconfigured (missing / corrupted / empty pass): no terminal
        # prompt — tell the caller to bootstrap via /set_password.
        return {"status": "error",
                "message": "No password has been set yet. Please set a password."}, 401
    if not _is_login_required():
        # login disabled: any password is accepted, still hand back a token
        return {"status": "ok", "token": _issue_token(), "login_required": False}, 200
    if _sha256(pw) != cfg["pass"]:
        return {"status": "error", "message": "wrong_password"}, 401
    return {"status": "ok", "token": _issue_token(), "login_required": True}, 200

def _auth_change_password(body):
    old = (body or {}).get("old_password", "")
    new = (body or {}).get("new_password", "")
    if not new:
        return {"status": "error", "message": "new_password is required"}, 400
    cfg = _load_pw_cfg()
    if cfg.get("pass", "") and _sha256(old) != cfg["pass"]:
        return {"status": "error", "message": "wrong_password"}, 401
    cfg["pass"] = _sha256(new)
    _save_pw_cfg(cfg)
    return {"status": "ok", "message": "password changed"}, 200

def _auth_login_required(body):
    pw  = (body or {}).get("password", "")
    raw = (body or {}).get("login_required")
    cfg = _load_pw_cfg()
    if not cfg.get("pass", ""):
        return {"status": "error", "message": "no_password_set"}, 400
    if _sha256(pw) != cfg["pass"]:
        return {"status": "error", "message": "wrong_password"}, 401
    if raw is None:
        return {"status": "error", "message": "login_required (true/false) is required"}, 400
    cfg["login_required"] = bool(raw)
    _save_pw_cfg(cfg)
    return {"status": "ok", "login_required": cfg["login_required"]}, 200

# ── /server_settings (shared by both HTTP handlers) ───────────────
def _server_settings_get():
    """GET /server_settings — public, no auth (a supplied token is ignored)."""
    return _load_server_settings(), 200

def _server_settings_post(body, token):
    """POST /server_settings — needs a valid token OR the password in the body
    (no auth at all while login_required is false). Persists to
    llm-server_settings.json; new ports apply on the next server restart."""
    body = body if isinstance(body, dict) else {}
    if _is_login_required():
        authed = bool(token and _token_valid(token))
        if not authed:
            pw  = body.get("password", "")
            cfg = _load_pw_cfg()
            authed = bool(cfg.get("pass")) and _sha256(pw) == cfg["pass"]
        if not authed:
            return {"status": "error", "message": "unauthorized"}, 401

    updates = {}
    for k, v in body.items():
        if k in ("token", "password"):
            continue
        if k not in DEFAULT_SERVER_SETTINGS:
            return {"status": "error", "message": f"Unknown key: {k}"}, 400
        try:
            port = int(v)
        except (TypeError, ValueError):
            return {"status": "error", "message": f"{k} must be an integer"}, 400
        if not 1 <= port <= 65535:
            return {"status": "error", "message": f"{k} must be 1-65535, got {port}"}, 400
        updates[k] = port

    if not updates:
        return {"status": "error",
                "message": f"at least one of {sorted(DEFAULT_SERVER_SETTINGS)} is required"}, 400

    settings = _load_server_settings()
    settings.update(updates)
    if len(set(settings.values())) != len(settings):
        return {"status": "error", "message": "ports must be distinct"}, 400
    _save_server_settings(settings)
    print(f"[server-settings] Updated: {updates}")
    return {
        "status":   "ok",
        "settings": settings,
        "message":  "saved — restart the server to apply new ports",
    }, 200

# ── llama.cpp server config ───────────────────────────────────────
LLAMACPP_HOST    = "127.0.0.1"
LLAMACPP_PORT    = 8080          # base port; instance N listens on LLAMACPP_PORT + N
LLAMACPP_PORT_BASE = LLAMACPP_PORT
LLAMACPP_BASE    = f"http://{LLAMACPP_HOST}:{LLAMACPP_PORT}"

# VRAM fit estimate (enable_flow=false, single-GPU load).
# Required VRAM ≈ (weights on GPU) + (KV cache for GPU layers) + overhead,
# compared against the GPU's *free* VRAM. Only the layers actually offloaded to
# the GPU count — if the user pushes layers to CPU (dev cfg gpu_layers/cpu_percent)
# the requirement shrinks accordingly, and a full-CPU load skips the check.
MODEL_VRAM_OVERHEAD   = 1.2     # fallback multiplier when gguf metadata can't be read
_KV_CACHE_BYTES       = 2       # KV cache element size (llama.cpp default = f16)
_VRAM_FIXED_OVERHEAD_MB = 400   # compute/CUDA-graph/context buffers
_VRAM_OVERHEAD_FRAC     = 0.05  # extra margin on top of weights+KV
_LAYER_FALLBACK_TOTAL   = 64    # assumed layer count for cpu_percent math when N unknown

# Default contents for gpu_placement.json — controls /api/llm/load-model placement.
DEFAULT_MULTI_GPU = {"enable_flow": False, "default_gpu": 0}


def _load_multi_gpu_settings() -> dict:
    if not os.path.exists(MULTI_GPU_FILE):
        _save_multi_gpu_settings(dict(DEFAULT_MULTI_GPU))
        return dict(DEFAULT_MULTI_GPU)
    try:
        with open(MULTI_GPU_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        merged = dict(DEFAULT_MULTI_GPU)
        if isinstance(saved, dict):
            if "enable_flow" in saved:
                merged["enable_flow"] = bool(saved["enable_flow"])
            if "default_gpu" in saved:
                try:
                    merged["default_gpu"] = int(saved["default_gpu"])
                except (TypeError, ValueError):
                    pass
        return merged
    except (json.JSONDecodeError, ValueError, OSError):
        _save_multi_gpu_settings(dict(DEFAULT_MULTI_GPU))
        return dict(DEFAULT_MULTI_GPU)


def _save_multi_gpu_settings(settings: dict):
    try:
        with open(MULTI_GPU_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[multi-gpu] Failed to save gpu_placement.json: {e}")

def _load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        _save_settings(dict(DEFAULT_SETTINGS))
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        return merged
    except (json.JSONDecodeError, ValueError):
        _save_settings(dict(DEFAULT_SETTINGS))
        return dict(DEFAULT_SETTINGS)

def _save_settings(settings: dict):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE) or '.', exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[settings] Failed to save llm_settings.json: {e}")

def _detect_thinking_support(base: str = LLAMACPP_BASE, model_path: str = "") -> bool:
    """
    Auto-detect thinking support by probing the loaded model.
    Tries multiple methods in order:
      1. Check if <think> / </think> / /think tokenize to a single token
      2. Check model name in /props metadata
      3. Check loaded model filename for known thinking model keywords

    `base` is the llama-server base URL of the specific instance to probe;
    `model_path` is the .gguf path used for the filename heuristic.
    """
    # Method 1: single-token probe for thinking tags
    for probe in ["<think>", "</think>", "/think"]:
        try:
            r = requests.post(
                f"{base}/tokenize",
                json={"content": probe, "add_special": False},
                timeout=5
            )
            tokens = r.json().get("tokens", [])
            if len(tokens) == 1:
                print(f"[thinking] '{probe}' -> single token ({tokens[0]}) -> supported=True")
                return True
        except Exception as e:
            print(f"[thinking] Tokenize probe '{probe}' failed: {e}")

    # Method 2: check model metadata from /props
    try:
        r = requests.get(f"{base}/props", timeout=5)
        props = r.json()
        model_name = str(
            props.get("default_generation_settings", {}).get("model", "")
        ).lower()
        thinking_keywords = ["qwq", "deepseek-r1", "r1", "thinking", "reasoner"]
        if any(kw in model_name for kw in thinking_keywords):
            print(f"[thinking] Model name suggests thinking support: {model_name}")
            return True
    except Exception as e:
        print(f"[thinking] Props probe failed: {e}")

    # Method 3: check loaded model filename
    if not model_path:
        model_path = launcher.loaded_model if 'launcher' in globals() else ""
    model_path = (model_path or "").lower()
    thinking_keywords = ["qwq", "deepseek-r1", "-r1-", "thinking", "reasoner", "r1-distill"]
    if model_path and any(kw in model_path for kw in thinking_keywords):
        print(f"[thinking] Model filename suggests thinking support: {model_path}")
        return True

    print("[thinking] No thinking support detected -> supported=False")
    return False


def _get_thinking_support_for_model(path: str, base: str = LLAMACPP_BASE) -> bool:
    """
    Resolve supports_thinking for a model path:
      1. Look up saved_models.json — if supports_thinking is explicitly set (true/false) use it.
      2. If found but supports_thinking is null/missing → auto-detect and save result.
      3. If not in saved_models.json at all → auto-detect only (don't save).
    """
    try:
        data = _load_models()
        for model in data.get("models", []):
            if model.get("path") == path:
                val = model.get("supports_thinking")
                if val is not None:
                    # User has explicitly set this — respect it
                    print(f"[thinking] From saved_models.json: supports_thinking={val}")
                    return bool(val)
                # Entry exists but no value set → auto-detect and persist
                detected = _detect_thinking_support(base, path)
                model["supports_thinking"] = detected
                _save_models(data)
                print(f"[thinking] Auto-detected & saved to saved_models.json: {detected}")
                return detected
    except Exception as e:
        print(f"[thinking] saved_models.json lookup failed: {e}")

    # Model not in saved list — just auto-detect without saving
    print("[thinking] Model not in saved list, running auto-detect.")
    return _detect_thinking_support(base, path)


def _detect_reasoning_effort_support(base: str = LLAMACPP_BASE) -> bool:
    """
    Auto-detect whether a model supports reasoning-DEPTH control (low/medium/high)
    as opposed to plain on/off thinking.

    A model supports depth control only if its chat template natively understands
    the `reasoning_effort` variable (the OpenAI gpt-oss / Harmony templates render
    "Reasoning: high/medium/low" into the system message). We detect this by
    reading the actual jinja chat template from /props (requires --jinja) and
    checking whether it references `reasoning_effort`.
    """
    try:
        r = requests.get(f"{base}/props", timeout=5)
        props = r.json()
        template = props.get("chat_template", "")
        # Some builds return a dict/list (e.g. tool-use variants); flatten to text.
        if isinstance(template, (dict, list)):
            template = json.dumps(template)
        template = str(template)
        supported = "reasoning_effort" in template
        print(f"[effort] chat_template reasoning_effort support -> {supported}")
        return supported
    except Exception as e:
        print(f"[effort] /props chat_template probe failed: {e}")
        return False


def _get_reasoning_effort_support_for_model(path: str, base: str = LLAMACPP_BASE) -> bool:
    """
    Resolve supports_reasoning_effort for a model path (mirrors
    _get_thinking_support_for_model):
      1. saved_models.json explicit true/false → respect it.
      2. Entry exists but null/missing → auto-detect and persist.
      3. Not in saved_models.json → auto-detect only (don't save).
    """
    try:
        data = _load_models()
        for model in data.get("models", []):
            if model.get("path") == path:
                val = model.get("supports_reasoning_effort")
                if val is not None:
                    print(f"[effort] From saved_models.json: supports_reasoning_effort={val}")
                    return bool(val)
                detected = _detect_reasoning_effort_support(base)
                model["supports_reasoning_effort"] = detected
                _save_models(data)
                print(f"[effort] Auto-detected & saved to saved_models.json: {detected}")
                return detected
    except Exception as e:
        print(f"[effort] saved_models.json lookup failed: {e}")

    print("[effort] Model not in saved list, running auto-detect.")
    return _detect_reasoning_effort_support(base)


LLAMACPP_BINARY_CANDIDATES = [
    # PATH lookup (works on any OS via shutil.which)
    "llama-server",
    "llama.cpp/llama-server",
    "llama.cpp/build/bin/llama-server",
    # Linux / macOS common locations
    "/usr/local/bin/llama-server",
    "/usr/bin/llama-server",
    "/opt/llama.cpp/llama-server",
    "/opt/llama.cpp/build/bin/llama-server",
    os.path.expanduser("~/llama.cpp/llama-server"),
    os.path.expanduser("~/llama.cpp/build/bin/llama-server"),
    os.path.expanduser("~/.local/bin/llama-server"),
    # Windows common locations
    r"C:\llama.cpp\llama-server.exe",
    r"C:\llama.cpp\build\bin\Release\llama-server.exe",
    r"C:\llama.cpp\build\bin\llama-server.exe",
]

LLAMACPP_CTX     = 8192
LLAMACPP_BATCH   = 512

# ── Default LLM Settings ──────────────────────────────────────────
DEFAULT_SETTINGS = {
    "model_path":       "",
    "temperature":      0.6,
    "top_p":            0.9,
    "top_k":            40,
    "min_p":            0.05,
    "presence_penalty": 0.0,
    "repeat_penalty":   1.1,
    "max_tokens":       4096,
    "reasoning_effort": "none",
    "supports_thinking": False,
    "supports_reasoning_effort": False,
    "cache_prompt":     True
}

SETTINGS_BOUNDS = {
    "temperature":      (0.0, 2.0),
    "top_p":            (0.0, 1.0),
    "top_k":            (0, 1000),
    "min_p":            (0.0, 1.0),
    "presence_penalty": (-2.0, 2.0),
    "repeat_penalty":   (0.0, 2.0),
    "max_tokens":       (64, 32768),
}

SETTINGS_TYPES = {
    "temperature":      float,
    "top_p":            float,
    "top_k":            int,
    "min_p":            float,
    "presence_penalty": float,
    "repeat_penalty":   float,
    "max_tokens":       int,
    "reasoning_effort": str,
    "supports_thinking": bool,
    "supports_reasoning_effort": bool,
    "cache_prompt": bool
}

VALID_REASONING = {"none", "low", "medium", "high"}

_API_ROLES = {"user", "assistant", "system", "tool"}

# ── Prompt state ─────────────────────────────────────────────────
_prompt_lock        = threading.Lock()
_prompt_state = {
    "stopped": False,
    "active_name": None,
}

# ══════════════════════════════════════════════════════════════════
#  DEVELOPER CONFIG  (runtime-editable)
# ══════════════════════════════════════════════════════════════════
_dev_cfg_lock = threading.Lock()

_dev_cfg = {
    "ctx_size":           LLAMACPP_CTX,
    "batch_size":         LLAMACPP_BATCH,
    "gpu_layers":         None,
    "cpu_percent":        0,
    "max_history":        0,
    "token_warn_at":      6000,
    "ctx_safety_margin":  0.15,
    "auto_trim":          True,
}

def _get_dev_cfg() -> dict:
    with _dev_cfg_lock:
        return dict(_dev_cfg)


# ── GPU Flow State ────────────────────────────────────────────────
_gpu_flow_lock = threading.Lock()
_gpu_flow: dict = {}
# {gpu_index: percent}  e.g. {0: 60, 1: 40} or {0: 100} (single-GPU)


# ══════════════════════════════════════════════════════════════════
#  TOKEN COUNTING  (exact — the loaded model's own tokenizer)
# ══════════════════════════════════════════════════════════════════
#  Counts are taken from llama-server's /tokenize endpoint, i.e. the
#  EXACT tokenizer of the running model. tiktoken (cl100k_base) is kept
#  only as a fallback for when no model is loaded yet — it mis-counts
#  non-English (Arabic ~2× too high), so it must never gate real budget
#  decisions while a model is live.
import tiktoken as _tiktoken

_TOKENIZER: _tiktoken.Encoding | None = None

# Cache /tokenize results so we don't issue an HTTP round-trip per message
# on every request. Keyed by (model base url, text) so switching models
# invalidates stale counts automatically.
_TOK_CACHE: dict = {}
_TOK_CACHE_LOCK = threading.Lock()
_TOK_CACHE_MAX  = 8192

def _get_tokenizer() -> _tiktoken.Encoding:
    global _TOKENIZER
    if _TOKENIZER is None:
        _TOKENIZER = _tiktoken.get_encoding("cl100k_base")
        print(f"[tiktoken] Fallback encoder loaded: cl100k_base")
    return _TOKENIZER

def _model_token_count(text: str) -> int | None:
    """Exact token count from the loaded model's tokenizer, or None if no
    model is ready / the endpoint fails (caller should then fall back)."""
    if not text:
        return 0
    try:
        if not launcher.is_ready():
            return None
        base = launcher.base
    except Exception:
        return None

    key = (base, text)
    with _TOK_CACHE_LOCK:
        cached = _TOK_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        r = requests.post(
            f"{base}/tokenize",
            json={"content": text, "add_special": False},
            timeout=5,
        )
        r.raise_for_status()
        n = len(r.json().get("tokens", []))
    except Exception as e:
        print(f"[tokenize] /tokenize failed, falling back to tiktoken: {e}")
        return None

    with _TOK_CACHE_LOCK:
        if len(_TOK_CACHE) >= _TOK_CACHE_MAX:
            _TOK_CACHE.clear()
        _TOK_CACHE[key] = n
    return n

def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    # Prefer the model's own (exact) tokenizer; fall back to tiktoken only
    # when no model is loaded yet.
    n = _model_token_count(text)
    if n is not None:
        return n
    return len(_get_tokenizer().encode(text))

def _estimate_msg_tokens(msg: dict) -> int:
    content_tokens = _estimate_tokens(msg.get("content", ""))
    role_tokens    = _estimate_tokens(msg.get("role", ""))
    return content_tokens + role_tokens + 4

def _count_history_tokens() -> int:
    total = _estimate_tokens(llm.system_prompt) if 'llm' in globals() else 0
    if 'llm' in globals():
        for m in llm.history:
            total += _estimate_msg_tokens(m)
    return total


# ══════════════════════════════════════════════════════════════════
#  BUDGET MATH (shared)
# ══════════════════════════════════════════════════════════════════
def _compute_budget(ctx_size: int, max_tokens: int, safety_margin: float) -> dict:
    # The only rule: total conversation tokens ≤ ctx_size.
    # max_tokens is irrelevant to budgeting — it's just the model's generation cap.
    margin       = int(ctx_size * safety_margin)
    input_budget = max(256, ctx_size - margin)
    return {
        "ctx_size":       ctx_size,
        "output_reserve": 0,
        "margin":         margin,
        "input_budget":   input_budget,
    }


# ══════════════════════════════════════════════════════════════════
#  SLIDING WINDOW
# ══════════════════════════════════════════════════════════════════
def _build_sliding_window(
    system_prompt: str,
    history: list,
    pending: dict,
    ctx_size: int,
    max_tokens: int,
    safety_margin: float,
    auto_trim: bool = True,
) -> tuple:
    b = _compute_budget(ctx_size, max_tokens, safety_margin)
    input_budget = b["input_budget"]

    sys_tokens     = _estimate_tokens(system_prompt) + 4
    pending_tokens = _estimate_msg_tokens(pending)
    fixed_cost     = sys_tokens + pending_tokens

    # auto_trim disabled → never drop anything; send the full history as-is.
    if not auto_trim:
        history_tokens  = sum(_estimate_msg_tokens(m) for m in history)
        total_estimated = fixed_cost + history_tokens
        pct             = round(total_estimated / max(ctx_size, 1) * 100, 1)
        messages = (
            [{"role": "system", "content": system_prompt}]
            + [{"role": _to_api_role(m["role"]), "content": m["content"]} for m in history]
            + [{"role": _to_api_role(pending["role"]),  "content": pending["content"]}]
        )
        # Fill the context right up to ctx_size — no trimming. Once the whole
        # conversation would exceed the full window there is no room for a new
        # turn, so flag it: the caller refuses to start a fresh generation and
        # warns. (A reply already streaming is never cut; this only guards the
        # *next* turn.)
        context_full = total_estimated > ctx_size
        warning = None
        if context_full:
            warning = (
                f"context limit reached (~{total_estimated} / {ctx_size} tok); "
                f"auto-trim is OFF — start a new session, clear history, or enable auto-trim"
            )
        print(
            f"[window] auto_trim=OFF | ctx={ctx_size} | "
            f"used~{total_estimated} ({pct}%) | history={len(history)} kept, 0 dropped"
            f"{' | CONTEXT FULL → stop' if context_full else ''}"
        )
        return messages, {
            "included":         len(history),
            "dropped":          0,
            "tokens_estimated": total_estimated,
            "budget":           input_budget,
            "ctx_pct":          pct,
            "warning":          warning,
            "context_full":     context_full,
        }

    remaining = input_budget - fixed_cost

    if remaining < 0:
        print(
            f"[window] WARNING: fixed cost ({fixed_cost} tok) > input budget "
            f"({input_budget} tok). Sending with no history."
        )
        messages = [
            {"role": "system",  "content": system_prompt},
            {"role": _to_api_role(pending["role"]), "content": pending["content"]},
        ]
        return messages, {
            "included":         0,
            "dropped":          len(history),
            "tokens_estimated": fixed_cost,
            "budget":           input_budget,
            "ctx_pct":          round(fixed_cost / max(ctx_size, 1) * 100, 1),
            "warning":          (
                f"message exceeds allowed input budget "
                f"({fixed_cost} > {input_budget} tokens); "
                f"request will be truncated server-side"
            ),
        }

    included   = []
    dropped    = 0
    token_used = 0

    for msg in reversed(history):
        cost = _estimate_msg_tokens(msg)
        if token_used + cost > remaining:
            dropped += 1
        else:
            included.insert(0, msg)
            token_used += cost

    if dropped > 0:
        notice = {
            "role":    "assistant",
            "content": f"[{dropped} earlier message(s) were omitted to fit the context window.]",
        }
        included.insert(0, notice)
        print(f"[window] Dropped {dropped} old message(s) to fit context.")

    messages = (
        [{"role": "system", "content": system_prompt}]
        + [{"role": _to_api_role(m["role"]), "content": m["content"]} for m in included]
        + [{"role": _to_api_role(pending["role"]),  "content": pending["content"]}]
    )

    total_estimated = fixed_cost + token_used
    pct             = round(total_estimated / max(ctx_size, 1) * 100, 1)
    print(
        f"[window] ctx={ctx_size} | budget={input_budget} | "
        f"used~{total_estimated} ({pct}%) | "
        f"history={len(included)} kept, {dropped} dropped"
    )

    return messages, {
        "included": len(included),
        "dropped":  dropped,
        "tokens_estimated": total_estimated,
        "budget":   input_budget,
        "ctx_pct":  pct,
        "warning":  (
            f"trimmed {dropped} old message(s) to fit context"
            if dropped > 0 else None
        ),
    }


# ══════════════════════════════════════════════════════════════════
#  HISTORY TRIMMER
# ══════════════════════════════════════════════════════════════════
def _history_trim_if_needed(history=None, settings=None):
    if 'llm' not in globals():
        return

    _hist     = history  if history  is not None else llm.history
    _settings = settings if settings is not None else llm.settings

    cfg   = _get_dev_cfg()
    max_h = cfg["max_history"]

    if max_h and len(_hist) > max_h:
        removed = len(_hist) - max_h
        del _hist[:len(_hist) - max_h]
        print(f"[trim] Hard cap: removed {removed} oldest message(s).")

    if not cfg.get("auto_trim", True):
        return

    ctx_size       = cfg["ctx_size"]
    safety_margin  = cfg.get("ctx_safety_margin", 0.15)
    max_tokens     = _settings.get("max_tokens", DEFAULT_SETTINGS["max_tokens"])

    b              = _compute_budget(ctx_size, max_tokens, safety_margin)
    sys_tokens     = _estimate_tokens(llm.system_prompt) + 4 if 'llm' in globals() else 0
    # Reserve a small slot for the next pending user message (unknown at trim time).
    pending_reserve = 512
    history_budget = max(256, b["input_budget"] - sys_tokens - pending_reserve)

    total = sum(_estimate_msg_tokens(m) for m in _hist)
    if total <= history_budget:
        return

    removed = 0
    while _hist and total > history_budget:
        oldest = _hist.pop(0)
        total -= _estimate_msg_tokens(oldest)
        removed += 1

    if removed:
        print(f"[trim] Token budget: removed {removed} oldest message(s)  (~{total} tok remaining).")


# ══════════════════════════════════════════════════════════════════
#  TIMESTAMP FILTER
# ══════════════════════════════════════════════════════════════════
def _strip_timestamp(text: str) -> str:
    return _re.sub(r'^\[\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?\]\s*-?\s*', '', text)


# ══════════════════════════════════════════════════════════════════
#  MODELS FILE HELPERS
# ══════════════════════════════════════════════════════════════════
def _load_models() -> dict:
    if not os.path.exists(MODELS_FILE):
        default = {"models": [], "autoSave": False}
        with open(MODELS_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2, ensure_ascii=False)
        return default
    with open(MODELS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    for m in data.get("models", []):
        if "supports_thinking" not in m:
            m["supports_thinking"] = None
        if "supports_reasoning_effort" not in m:
            m["supports_reasoning_effort"] = None
    return data

def _save_models(data: dict):
    with open(MODELS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def _autosave_model_if_enabled(name: str, path: str):
    """
    Auto-save model entry when autoSave is enabled.
    supports_thinking is set to null so it gets auto-detected on next load.
    """
    try:
        data = _load_models()
        if not data.get("autoSave", False):
            return
        existing_paths = [m.get("path") for m in data.get("models", [])]
        if path in existing_paths:
            return
        data.setdefault("models", []).append({
            "name":             name,
            "path":             path,
            "supports_thinking": None,   # null = auto-detect on next load
            "supports_reasoning_effort": None,  # null = auto-detect on next load
        })
        _save_models(data)
        print(f"[models] Auto-saved model '{name}' -> {path}")
    except Exception as e:
        print(f"[models] Auto-save error: {e}")


# ══════════════════════════════════════════════════════════════════
#  PROMPT FILE HELPERS
# ══════════════════════════════════════════════════════════════════
def _prompt_file_path(name: str) -> str:
    safe = name.strip()
    if not safe:
        raise ValueError("Prompt name cannot be empty")
    if safe.lower().endswith(".txt"):
        safe = safe[:-4]
    safe = "".join(c for c in safe if c.isalnum() or c in "-_ ").strip()
    if not safe:
        raise ValueError("Invalid prompt name")
    return os.path.join(PROMPTS_DIR, safe + ".txt")

def _prompt_file_exists(name: str) -> bool:
    try:
        return os.path.isfile(_prompt_file_path(name))
    except ValueError:
        return False

def _read_prompt_file(name: str) -> str:
    path = _prompt_file_path(name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Prompt '{name}' not found")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def _write_prompt_file(name: str, content: str):
    path = _prompt_file_path(name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _delete_prompt_file(name: str):
    path = _prompt_file_path(name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Prompt '{name}' not found")
    os.remove(path)

def _list_prompt_names() -> list:
    names = []
    for fn in sorted(os.listdir(PROMPTS_DIR)):
        if fn.endswith(".txt"):
            names.append(fn[:-4])
    return names


# ══════════════════════════════════════════════════════════════════
#  GPU DETECTION (NVIDIA / AMD / Intel / Vulkan)
# ══════════════════════════════════════════════════════════════════
# Detection spawns up to 5 vendor CLIs per call (nvidia-smi, rocm-smi,
# xpu-smi, sycl-ls, vulkaninfo). Cache for 30 s so HTTP endpoints
# (`/gpus/status`, `/gpus/flow`, etc.) don't fork-bomb on every request.
_GPU_CACHE_TTL = 30.0
_gpu_cache_lock = threading.Lock()
_gpu_cache: dict = {"detect_gpu": (0.0, None), "detect_all_gpus": (0.0, None)}


def _cache_get(key: str):
    ts, val = _gpu_cache[key]
    if val is not None and (time.time() - ts) < _GPU_CACHE_TTL:
        return val
    return None


def _cache_put(key: str, val):
    _gpu_cache[key] = (time.time(), val)


def detect_gpu() -> tuple:
    # Priority: NVIDIA → AMD ROCm → Intel SYCL → Vulkan → CPU
    with _gpu_cache_lock:
        cached = _cache_get("detect_gpu")
        if cached is not None:
            return cached
    result_tuple = _detect_gpu_impl()
    with _gpu_cache_lock:
        _cache_put("detect_gpu", result_tuple)
    return result_tuple


def _detect_gpu_impl() -> tuple:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            info  = "; ".join(lines)
            print(f"[GPU] NVIDIA CUDA detected: {info}")
            return True, 999, info, "cuda"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try AMD ROCm
    rocm_info, _, rocm_backend = _detect_rocm_gpus()
    if rocm_info:
        print(f"[GPU] AMD ROCm detected: {rocm_info}")
        return True, 999, rocm_info, rocm_backend

    # Try Intel SYCL (xpu-smi / sycl-ls)
    intel_info, _, intel_backend = _detect_intel_gpus()
    if intel_info:
        print(f"[GPU] Intel SYCL detected: {intel_info}")
        return True, 999, intel_info, intel_backend

    # Try Vulkan (any vendor)
    vulkan_info, _, vulkan_backend = _detect_vulkan_gpus()
    if vulkan_info:
        print(f"[GPU] Vulkan detected: {vulkan_info}")
        return True, 999, vulkan_info, vulkan_backend

    print("[GPU] No GPU detected — CPU mode.")
    return False, 0, "CPU only", "cpu"


def detect_all_gpus() -> list:
    """Return list of ALL GPUs from ALL vendors combined, each with a vendor field.
    Indices are made globally unique across vendors to avoid collisions."""
    with _gpu_cache_lock:
        cached = _cache_get("detect_all_gpus")
        if cached is not None:
            return [dict(g) for g in cached]  # defensive copy
    result_list = _detect_all_gpus_impl()
    with _gpu_cache_lock:
        _cache_put("detect_all_gpus", [dict(g) for g in result_list])
    return result_list


def _detect_all_gpus_impl() -> list:
    all_gpus = []
    next_index = 0

    # NVIDIA — native_index == global index (nvidia-smi numbering starts at 0)
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",", 2)]
                if len(parts) >= 3:
                    try:
                        native = int(parts[0])
                        vram_mb = int(parts[2])
                        all_gpus.append({
                            "index":        next_index,
                            "native_index": native,   # used in CUDA_VISIBLE_DEVICES
                            "name":         parts[1],
                            "vendor":       "nvidia",
                            "vram_mb":      vram_mb,
                            "vram_gb":      round(vram_mb / 1024, 2),
                        })
                        next_index += 1
                    except ValueError:
                        pass
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    # AMD ROCm — native_index is rocm-smi device ordinal (0-based, independent of NVIDIA)
    _, rocm_gpus, _ = _detect_rocm_gpus()
    for i, gpu in enumerate(rocm_gpus):
        gpu["index"]        = next_index
        gpu["native_index"] = i   # used in ROCR_VISIBLE_DEVICES
        all_gpus.append(gpu)
        next_index += 1

    # Intel SYCL — native_index is level_zero device ordinal (used in ONEAPI_DEVICE_SELECTOR)
    _, intel_gpus, _ = _detect_intel_gpus()
    for i, gpu in enumerate(intel_gpus):
        gpu["index"]        = next_index
        gpu["native_index"] = i
        all_gpus.append(gpu)
        next_index += 1

    # Vulkan — deduplicate against already-detected GPUs
    known_names = {g["name"].lower() for g in all_gpus}
    _, vulkan_gpus, _ = _detect_vulkan_gpus()
    for i, gpu in enumerate(vulkan_gpus):
        if gpu["name"].lower() not in known_names:
            gpu["index"]        = next_index
            gpu["native_index"] = i
            all_gpus.append(gpu)
            next_index += 1

    return all_gpus


def _detect_rocm_gpus() -> tuple:
    """Detect AMD ROCm GPUs. Returns (first_gpu_info, all_gpus_list, backend)."""
    try:
        result = subprocess.run(
            ["rocm-smi", "--query-gpu=gpu_id,gpu_name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            gpus = []
            first_info = ""
            for line in result.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",", 2)]
                if len(parts) >= 3:
                    try:
                        gpu_id = int(parts[0])
                        vram_mb = int(parts[2])
                        gpu_dict = {
                            "index":   gpu_id,
                            "name":    parts[1],
                            "vendor":  "amd",
                            "vram_mb": vram_mb,
                            "vram_gb": round(vram_mb / 1024, 2),
                        }
                        gpus.append(gpu_dict)
                        if not first_info:
                            first_info = f"{parts[1]} ({round(vram_mb / 1024, 1)}GB)"
                    except ValueError:
                        pass
            if gpus and first_info:
                return first_info, gpus, "rocm"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return "", [], "rocm"


def _detect_intel_gpus() -> tuple:
    """Detect Intel GPUs (Arc discrete and integrated Iris/UHD).

    Uses modern Intel-official tooling — not vendor-name heuristics:
      1. xpu-smi  (Intel XPU Manager — analogue of nvidia-smi / rocm-smi).
                  Reports device id, name, and physical VRAM in bytes.
      2. sycl-ls  (Intel oneAPI). Parses lines like:
                  [level_zero:gpu][level_zero:0] Intel(R) Arc(TM) A770 ...
                  VRAM is unknown via sycl-ls, so it is left at 0.

    Returns (first_gpu_info, all_gpus_list, "sycl").
    """
    # ── xpu-smi discovery -j ──────────────────────────────────────
    try:
        result = subprocess.run(
            ["xpu-smi", "discovery", "-j"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = json.loads(result.stdout)
            # xpu-smi schema varies by version; be liberal about key names.
            entries = (raw.get("device_list") or raw.get("devices")
                       or raw.get("device") or [])
            if isinstance(entries, dict):
                entries = [entries]
            _NAME_KEYS = ("device_name", "name", "DeviceName",
                          "device_model", "model")
            _MEM_BYTE_KEYS = ("memory_physical_size_byte",
                              "memory_physical_size_bytes",
                              "physical_memory_bytes")
            _MEM_MB_KEYS = ("memory_physical_size",  # historically MiB
                            "memory_physical_size_mb",
                            "memory_size_mb")
            gpus = []
            first_info = ""
            for i, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                name = ""
                for k in _NAME_KEYS:
                    v = entry.get(k)
                    if v:
                        name = str(v).strip()
                        break
                if not name:
                    continue
                vram_mb = 0
                for k in _MEM_BYTE_KEYS:
                    v = entry.get(k)
                    if v:
                        try:
                            vram_mb = int(v) // (1024 * 1024); break
                        except (TypeError, ValueError):
                            pass
                if not vram_mb:
                    for k in _MEM_MB_KEYS:
                        v = entry.get(k)
                        if v:
                            try:
                                vram_mb = int(v); break
                            except (TypeError, ValueError):
                                pass
                gpus.append({
                    "index":   i,
                    "name":    name,
                    "vendor":  "intel",
                    "vram_mb": vram_mb,
                    "vram_gb": round(vram_mb / 1024, 2),
                })
                if not first_info:
                    first_info = (f"{name} ({round(vram_mb / 1024, 1)}GB)"
                                  if vram_mb else name)
            if gpus and first_info:
                return first_info, gpus, "sycl"
    except (FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError, ValueError):
        pass

    # ── sycl-ls fallback ─────────────────────────────────────────
    try:
        result = subprocess.run(
            ["sycl-ls"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            # Real sycl-ls line: [level_zero:gpu][level_zero:N] <Device Name> <api-ver> [<driver-ver>]
            # Anchor on the trailing [driver] bracket, then strip the api-ver suffix
            # so names like "Intel(R) UHD Graphics 770" aren't truncated at "770".
            pat = _re.compile(
                r"\[level_zero:gpu\]\[level_zero:(\d+)\]\s+(.+?)\s*\[[^\]]+\]\s*$",
                _re.IGNORECASE
            )
            ver_suffix = _re.compile(r"\s+\d+(?:\.\d+)+\s*$")
            gpus = []
            first_info = ""
            seen_ordinals = set()
            for line in result.stdout.splitlines():
                m = pat.search(line)
                if not m:
                    continue
                ordinal = int(m.group(1))
                if ordinal in seen_ordinals:
                    continue
                seen_ordinals.add(ordinal)
                name = ver_suffix.sub("", m.group(2)).strip()
                gpus.append({
                    "index":   ordinal,
                    "name":    name,
                    "vendor":  "intel",
                    "vram_mb": 0,
                    "vram_gb": 0.0,
                })
                if not first_info:
                    first_info = name
            if gpus and first_info:
                return first_info, gpus, "sycl"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return "", [], "sycl"


def _detect_vulkan_gpus() -> tuple:
    """Detect Vulkan-capable GPUs using hardware Vendor IDs — no name guessing.

    PCI Vendor IDs (authoritative):
      0x10DE = NVIDIA
      0x1002 = AMD
      0x8086 = Intel
      0x1414 = Microsoft (skip — software/virtual)
    """
    _VENDOR_ID_MAP = {
        0x10DE: "nvidia",
        0x1002: "amd",
        0x8086: "intel",
    }
    _SKIP_VENDOR_IDS = {0x1414}  # Microsoft Basic Display / Hyper-V

    # ── vulkaninfo --summary: read vendorID + deviceName per GPU ──
    try:
        result = subprocess.run(
            ["vulkaninfo", "--summary"],
            capture_output=True, text=True, timeout=8
        )
        if result.returncode == 0:
            gpus = []
            current: dict = {}

            for line in result.stdout.splitlines():
                s = line.strip()
                if not s or "=" not in s:
                    continue
                key, _, val = s.partition("=")
                key = key.strip()
                val = val.strip()

                if key == "vendorID":
                    try:
                        current["vendor_id"] = int(val, 16)
                    except ValueError:
                        pass
                elif key == "deviceName":
                    current["name"] = val
                elif key == "deviceType":
                    current["device_type"] = val

                # Once we have both fields, flush the current GPU entry
                if "vendor_id" in current and "name" in current:
                    vid = current["vendor_id"]
                    if vid not in _SKIP_VENDOR_IDS:
                        vendor = _VENDOR_ID_MAP.get(vid, f"unknown(0x{vid:04X})")
                        gpus.append({
                            "index":   len(gpus),
                            "name":    current["name"],
                            "vendor":  vendor,
                            "vram_mb": 0,
                            "vram_gb": 0.0,
                        })
                    current = {}

            if gpus:
                return gpus[0]["name"], gpus, "vulkan"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # ── WMI fallback: read PNPDeviceID which contains VEN_XXXX ───
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-WmiObject Win32_VideoController | "
             "Select-Object Name,PNPDeviceID | "
             "ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=8
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = json.loads(result.stdout.strip())
            # ConvertTo-Json returns object or array depending on count
            if isinstance(raw, dict):
                raw = [raw]
            gpus = []
            for entry in raw:
                name    = (entry.get("Name") or "").strip()
                pnp_id  = (entry.get("PNPDeviceID") or "").upper()
                if not name or not pnp_id.startswith("PCI\\"):
                    continue  # Skip non-PCI devices (USB displays, virtual, etc.)

                # Extract VEN_XXXX from PNPDeviceID
                vendor = "unknown"
                for part in pnp_id.split("&"):
                    if part.startswith("VEN_"):
                        try:
                            vid = int(part[4:8], 16)
                            if vid in _SKIP_VENDOR_IDS:
                                vendor = None  # Mark for skip
                                break
                            vendor = _VENDOR_ID_MAP.get(vid, f"unknown(0x{vid:04X})")
                        except ValueError:
                            pass
                        break

                if vendor is None:
                    continue  # Skip Microsoft/virtual adapters

                gpus.append({
                    "index":   len(gpus),
                    "name":    name,
                    "vendor":  vendor,
                    "vram_mb": 0,
                    "vram_gb": 0.0,
                })

            if gpus:
                return gpus[0]["name"], gpus, "vulkan"
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    return "", [], "vulkan"


def _detect_vulkan() -> str:
    """Legacy fallback for Vulkan detection. Returns single GPU info string."""
    info, _, _ = _detect_vulkan_gpus()
    return info


# ══════════════════════════════════════════════════════════════════
#  GPU FLOW HELPERS
# ══════════════════════════════════════════════════════════════════
def _get_vendor_from_flow(flow_dict: dict) -> str:
    """Determine GPU vendor from flow dictionary keys (GPU indices).
    Returns 'nvidia', 'amd', or 'cpu' based on first GPU in flow."""
    if not flow_dict:
        return "cpu"

    all_gpus = detect_all_gpus()
    if not all_gpus:
        return "cpu"

    first_gpu_idx = min(flow_dict.keys())
    for gpu in all_gpus:
        if gpu.get("index") == first_gpu_idx:
            return gpu.get("vendor") or "cpu"

    return "cpu"


def _validate_gpu_flow_vendor(flow_dict: dict) -> str:
    """Validate all GPUs in flow have same vendor. Raises ValueError if mixed.
    Returns the vendor string."""
    if not flow_dict:
        return "cpu"

    all_gpus = detect_all_gpus()
    if not all_gpus:
        return "cpu"

    gpu_map = {g["index"]: g for g in all_gpus}
    vendors_in_flow = {}  # vendor -> list of GPU names
    for gpu_idx in flow_dict.keys():
        gpu = gpu_map.get(gpu_idx)
        if gpu:
            vendor = gpu.get("vendor") or "cpu"
            vendors_in_flow.setdefault(vendor, []).append(gpu["name"])

    if len(vendors_in_flow) > 1:
        parts = ", ".join(
            f"{v.upper()} [{', '.join(names)}]"
            for v, names in vendors_in_flow.items()
        )
        raise ValueError(
            f"Cannot split the model across different GPU platforms ({parts}). "
            f"Use GPUs from the same vendor only."
        )

    return list(vendors_in_flow)[0] if vendors_in_flow else "cpu"


def _calc_auto_flow(model_path: str) -> dict:
    """Auto-calculate GPU split ratios proportional to each GPU's VRAM.
    Requires 2+ GPUs — returns empty dict otherwise."""
    gpus = detect_all_gpus()
    if len(gpus) < 2:
        return {}

    total_vram = sum(g["vram_mb"] for g in gpus)
    if total_vram == 0:
        return {}

    percents = []
    for g in gpus:
        percents.append(round(g["vram_mb"] / total_vram * 100))

    # Fix rounding so sum == 100
    diff = 100 - sum(percents)
    percents[-1] += diff

    return {gpus[i]["index"]: percents[i] for i in range(len(gpus))}


def _reload_current_model_bg(force_flow: dict | None = None):
    """Eject the active model and reload it on its same instance/port.

    If `force_flow` is given it is used verbatim (e.g. from POST /gpus/flow);
    otherwise the placement plan is recomputed from gpu_placement.json + _gpu_flow.
    """
    inst = manager.active()
    path = inst.loaded_model
    if not path:
        return
    if force_flow is not None:
        flow = force_flow
    else:
        flow, err = _compute_load_plan(path)
        if err:
            print(f"[reload] Plan error: {err}")
            return
    inst._kill()
    inst.status       = "idle"
    inst.loaded_model = ""
    ok, msg = inst.load(path, flow=flow)
    if ok:
        supports = _get_thinking_support_for_model(path, inst.base)
        llm.settings["supports_thinking"] = supports
        llm.settings["supports_reasoning_effort"] = _get_reasoning_effort_support_for_model(path, inst.base)
    print(f"[reload] {msg}")


# ══════════════════════════════════════════════════════════════════
#  llama.cpp SERVER LAUNCHER
# ══════════════════════════════════════════════════════════════════
class LlamaCppLauncher:

    def __init__(self, port: int = LLAMACPP_PORT, machine_info: tuple | None = None,
                 binary: str | None = None):
        self._proc:   subprocess.Popen | None = None
        self._lock    = threading.Lock()
        self._binary  = binary if binary is not None else self._find_binary()
        # Machine-level GPU info is shared across instances — detect once on the
        # template launcher and pass it to every model instance.
        if machine_info is not None:
            self.has_gpu, self.gpu_layers, self.gpu_info, self.gpu_backend = machine_info
        else:
            self.has_gpu, self.gpu_layers, self.gpu_info, self.gpu_backend = detect_gpu()
        self.has_cuda = self.has_gpu
        self.port:          int = port
        self.base:          str = f"http://{LLAMACPP_HOST}:{port}"
        self.loaded_model:  str = ""
        self.status:        str = "idle"
        self.load_progress:  int = 0   # 0-100, model-load percent while status == "loading"
        self.gpus:         list = []   # global GPU indices this instance occupies
        self.flow:         dict = {}   # {gpu_index: percent} used at launch
        self.error_message: str = ""   # short, clear reason when status == "error"
        self._error_lines: list = []   # raw llama-server lines that signalled the failure

    def machine_info(self) -> tuple:
        return (self.has_gpu, self.gpu_layers, self.gpu_info, self.gpu_backend)

    def _find_binary(self) -> str | None:
        for candidate in LLAMACPP_BINARY_CANDIDATES:
            found = shutil.which(candidate) or (os.path.isfile(candidate) and candidate)
            if found:
                print(f"[llama.cpp] Binary: {found}")
                return found
        print("[llama.cpp] WARNING: llama-server binary not found.")
        return None

    def _kill(self):
        if self._proc and self._proc.poll() is None:
            print("[llama.cpp] Stopping existing server process…")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def load(self, model_path: str, flow: dict | None = None) -> tuple:
        """Launch this instance's llama-server for `model_path`.

        `flow` is an explicit {gpu_index: percent} placement plan:
          - len 1  → load fully on that single GPU
          - len 2+ → tensor-split across those GPUs
          - None / empty → CPU only (no GPU offload)
        """
        if not self._binary:
            return False, "llama-server binary not found."

        if not os.path.isfile(model_path):
            return False, f"Model file not found: {model_path}"

        if not model_path.lower().endswith(".gguf"):
            return False, f"Expected a .gguf file, got: {model_path}"

        flow = dict(flow) if flow else {}

        with self._lock:
            self._kill()
            self.status         = "loading"
            self.load_progress  = 0
            self.loaded_model   = ""
            self._pending_model = model_path
            self.gpus           = sorted(flow.keys()) if flow else []
            self.flow           = dict(flow)
            self.error_message  = ""
            self._error_lines   = []

            cfg   = _get_dev_cfg()
            ctx   = cfg["ctx_size"]
            batch = cfg["batch_size"]

            # Guard: never request more context than the model was trained for.
            # Beyond ctx_train the model emits garbage (positions outside its RoPE
            # training) and the KV-cache balloons. Clamp to the model's limit and
            # sync dev-config so every token-budget calc uses the REAL window
            # (otherwise budget math would think it has room the model lacks).
            _meta_ct   = _read_gguf_metadata(model_path)
            _ctx_train = _meta_ct.get("ctx_train")
            if _ctx_train and ctx > _ctx_train:
                print(
                    f"[load] ⚠ ctx_size={ctx} exceeds model's trained context "
                    f"({_ctx_train}); clamping to {_ctx_train} to avoid garbage "
                    f"output and KV-cache overflow."
                )
                ctx = _ctx_train
                with _dev_cfg_lock:
                    _dev_cfg["ctx_size"] = _ctx_train

            cmd = [
                self._binary,
                "--model",      model_path,
                "--host",       LLAMACPP_HOST,
                "--port",       str(self.port),
                "--ctx-size",   str(ctx),
                "--batch-size", str(batch),
                "--parallel", "1",
                "--jinja",
                # Disable mmap so weight loading goes through real ReadFile calls.
                # This makes the process read_bytes counter grow by exactly the
                # file size on every load (mmap page-ins aren't counted on
                # Windows and vary with the OS page cache), giving a consistent,
                # accurate load percentage run-to-run.
                "--no-mmap",
            ]

            launch_env = dict(os.environ)

            if flow and self.has_gpu:
                gpu_indices = sorted(flow.keys())

                # Determine vendor and set appropriate environment variable
                try:
                    vendor = _validate_gpu_flow_vendor(flow)
                except ValueError:
                    vendor = "cpu"
                    print("[GPU] WARNING: Mixed GPU vendors in flow; falling back to CPU mode.")

                # Build native device list (vendor-native index, not global)
                all_gpus_map = {g["index"]: g for g in detect_all_gpus()}
                native_ids = [
                    str(all_gpus_map[i].get("native_index", i))
                    for i in gpu_indices if i in all_gpus_map
                ]
                native_list = ",".join(native_ids)

                if vendor == "nvidia":
                    launch_env["CUDA_VISIBLE_DEVICES"] = native_list
                elif vendor == "amd" and self.gpu_backend == "rocm":
                    launch_env["ROCR_VISIBLE_DEVICES"] = native_list
                    launch_env["HIP_VISIBLE_DEVICES"]  = native_list
                elif vendor == "intel" and self.gpu_backend == "sycl":
                    sel = ";".join(f"level_zero:{nid}" for nid in native_ids)
                    launch_env["ONEAPI_DEVICE_SELECTOR"] = sel
                    native_list = sel  # log display
                # Vulkan: no env vars needed

                if len(gpu_indices) == 1:
                    # Honor dev-config partial offload (gpu_layers override / cpu_percent)
                    # on a single GPU; default to offloading every layer.
                    override = cfg["gpu_layers"]
                    cpu_pct  = cfg["cpu_percent"]
                    if override is not None:
                        ngl = str(int(override))
                    elif cpu_pct > 0:
                        ngl = str(max(0, int(64 * (1 - cpu_pct / 100))))
                    else:
                        ngl = "999"
                    cmd += ["--n-gpu-layers", ngl]
                    env_var = ("CUDA_VISIBLE_DEVICES" if vendor == "nvidia"
                               else "ROCR_VISIBLE_DEVICES" if vendor == "amd" and self.gpu_backend == "rocm"
                               else "ONEAPI_DEVICE_SELECTOR" if vendor == "intel" and self.gpu_backend == "sycl"
                               else "Vulkan")
                    print(f"[GPU] Single-GPU flow: GPU {gpu_indices[0]} ({env_var}={native_list}, n-gpu-layers={ngl})")
                else:
                    split_vals = ",".join(str(flow[i]) for i in gpu_indices)
                    cmd += ["--tensor-split", split_vals, "--n-gpu-layers", "999"]
                    print(f"[GPU] Multi-GPU flow: tensor-split={split_vals} on GPUs {gpu_indices} (vendor={vendor})")
            else:
                print("[llama.cpp] CPU mode — no GPU offload")

            print(f"[llama.cpp] Launching: {' '.join(cmd)}")
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    env=launch_env,
                )
            except Exception as e:
                self.status = "error"
                return False, f"Failed to launch llama-server: {e}"

            threading.Thread(target=self._log_reader, daemon=True).start()

            # Real load percentage: track bytes the process reads from the model
            # file (exact denominator = file size). NVIDIA VRAM fill is used as a
            # fallback only if file I/O isn't observable (mmap page-ins on Windows).
            threading.Thread(
                target=self._monitor_load_progress,
                args=(model_path, dict(flow), ctx, dict(cfg)),
                daemon=True,
            ).start()

        ready = self._wait_ready(timeout=120)
        if ready:
            if not self.loaded_model:
                self.loaded_model = model_path
            self.status = "ready"
            self.load_progress = 100
            name = os.path.splitext(os.path.basename(model_path))[0]
            msg  = f"Model '{name}' loaded. GPU={'yes' if self.has_cuda else 'no'}"
            print(f"[llama.cpp] {msg}")
            _autosave_model_if_enabled(name, model_path)
            return True, msg
        else:
            self.status = "error"
            detail = self._build_load_error()
            self.error_message = detail
            print(f"[llama.cpp] Load failed: {detail}")
            self._kill()   # reap the lingering llama.cpp process (frees VRAM)
            return False, detail

    def _build_load_error(self) -> str:
        """One short, clear reason the load failed — no log dump."""
        blob = " ".join(self._error_lines).lower()
        if ("out of memory" in blob or "cudamalloc failed" in blob
                or "failed to allocate" in blob):
            return "Out of memory: model too large for the GPU/RAM. Try a smaller model or context size."
        if self._error_lines:
            return "Failed to load the model."
        if self._proc is not None and self._proc.poll() not in (None, 0):
            return "llama-server crashed while loading the model."
        return "Model did not load in time (120 s)."

    def _wait_ready(self, timeout: int = 120) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.status == "ready":
                return True
            if self.status == "error":
                return False
            if self._proc and self._proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def is_ready(self) -> bool:
        if self._proc is None or self._proc.poll() is not None:
            return False
        return self.status == "ready"

    _READY_KEYWORDS = [
        "server is listening",
        "model loaded",
        "all slots are idle",
        "llama server listening",
    ]

    # Lines that mark a fatal load failure (OOM, buffer/KV allocation, context
    # creation). Collected so _build_load_error can name the real cause.
    _ERROR_KEYWORDS = [
        "cudamalloc failed",
        "out of memory",
        "failed to allocate",
        "failed to initialize the context",
        "failed to create context",
        "error loading model",
        "failed to load model",
        "unable to load model",
        "error: failed to",
    ]

    def _log_reader(self):
        # Read stdout incrementally (not line-buffered) so we can observe the
        # model-load progress dots as they stream. llama.cpp's default progress
        # callback prints one '.' per 1% of tensor loading, as a contiguous run
        # of dots on its own line — counting that run gives the load percent.
        if not self._proc or not self._proc.stdout:
            return
        fd = self._proc.stdout.fileno()
        line = ""              # current line being assembled
        dot_run = 0            # leading run of progress dots on the current line
        line_is_dots = True    # current line so far is only dots/spaces
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                for ch in chunk.decode("utf-8", errors="replace"):
                    if ch in ("\n", "\r"):
                        if line.strip():
                            self._handle_log_line(line)
                        line = ""
                        dot_run = 0
                        line_is_dots = True
                        continue
                    line += ch
                    if ch == ".":
                        if line_is_dots:
                            dot_run += 1
                            if self.status == "loading" and dot_run > self.load_progress:
                                self.load_progress = min(99, dot_run)
                    elif ch != " ":
                        line_is_dots = False
        except Exception:
            pass

    def _handle_log_line(self, line: str):
        line = line.rstrip()
        if not line:
            return
        print(f"[llama.cpp] {line}")
        low = line.lower()
        if any(kw in low for kw in self._READY_KEYWORDS):
            if self.status == "loading":
                self.status        = "ready"
                self.load_progress = 100
                self.loaded_model  = self._pending_model
                name = os.path.basename(self._pending_model)
                print(f"[llama.cpp] Model ready: {name}")
        elif any(kw in low for kw in self._ERROR_KEYWORDS):
            self._error_lines.append(line)
            if len(self._error_lines) > 25:
                self._error_lines = self._error_lines[-25:]
            # Flip state the moment a fatal line prints so _wait_ready returns
            # immediately instead of waiting ~10s for the process to fully exit.
            # Build the detail here too, so the instance always carries the real
            # reason even before load()'s failure branch runs (avoids a window
            # where model-status sees status="error" with no message).
            if self.status == "loading":
                self.status = "error"
                self.error_message = self._build_load_error()
        elif "prompt processing progress" in low:
            # Inference-time prompt eval. Relay the % to the active chat client so
            # the UI can show a "processing prompt" stage before tokens stream.
            conn = _get_active_stream_conn()
            if conn is not None:
                m = _re.search(r"progress\s*=\s*([0-9.]+)", line)
                if m:
                    try:
                        pct = int(round(float(m.group(1)) * 100))
                    except ValueError:
                        pct = -1
                    if pct >= 0:
                        _emit_event(conn, "prompt_processing",
                                    value=min(99, max(0, pct)))

    @staticmethod
    def _nvidia_used_mb(natives: list) -> int | None:
        """Sum of memory.used (MB) across the given native NVIDIA GPU indices."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits", "-i", ",".join(natives)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return sum(int(x.strip())
                           for x in result.stdout.strip().splitlines() if x.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            pass
        return None

    def _monitor_load_progress(self, model_path: str, flow: dict, ctx: int, cfg: dict):
        """Drive self.load_progress (0-99) during load by tracking how many
        bytes of the model file the process has read against the file size.

        With --no-mmap, llama.cpp reads weights via real ReadFile calls, so the
        process read_bytes counter grows by exactly the file size on every load
        (denominator = exact file size). This is consistent run-to-run, unlike
        RSS/working-set which varies with the OS page cache and CUDA buffers.
        Fallback : NVIDIA VRAM fill (estimated denominator) only when read_bytes
                   is unavailable (psutil missing). Stops when status leaves
                   'loading'. Works for CPU and GPU loads.
        """
        try:
            proc = self._proc
            if proc is None:
                return
            ps = None
            if psutil is not None:
                try:
                    ps = psutil.Process(proc.pid)
                except Exception:
                    ps = None

            file_size = os.path.getsize(model_path) if os.path.isfile(model_path) else 0

            # --- optional VRAM fallback setup (NVIDIA only) ---
            natives, expected = None, 0
            if flow and self.has_gpu:
                all_gpus = {g["index"]: g for g in detect_all_gpus()}
                nv = [all_gpus[i] for i in flow.keys()
                      if i in all_gpus and all_gpus[i].get("vendor") == "nvidia"]
                if nv:
                    natives = [str(g.get("native_index", g.get("index", 0))) for g in nv]
                    meta       = _read_gguf_metadata(model_path)
                    gpu_layers = _resolve_gpu_layers(cfg, meta.get("n_layers"))
                    expected   = _estimate_required_vram_mb(model_path, gpu_layers, meta, ctx)

            def _read_bytes():
                try:
                    return ps.io_counters().read_bytes if ps else None
                except Exception:
                    return None

            base_read = _read_bytes()
            base_vram = self._nvidia_used_mb(natives) if natives else None

            deadline = time.time() + 300
            while self.status == "loading" and time.time() < deadline:
                # primary: bytes read so far / model file size
                io_pct = None
                if base_read is not None and file_size > 0:
                    now = _read_bytes()
                    if now is not None and now - base_read > 0:
                        io_pct = (now - base_read) / file_size * 100
                # fallback: VRAM fill (only when read_bytes is unavailable)
                vram_pct = None
                if io_pct is None and natives and expected > 0 and base_vram is not None:
                    used = self._nvidia_used_mb(natives)
                    if used is not None:
                        vram_pct = max(0, used - base_vram) / expected * 100
                pct = io_pct if io_pct is not None else vram_pct
                if pct is not None:
                    p = int(min(99, max(0, pct)))
                    if p > self.load_progress:
                        self.load_progress = p
                time.sleep(0.2)
        except Exception as e:
            print(f"[load-progress] {e}")


# ══════════════════════════════════════════════════════════════════
#  GGUF METADATA + VRAM ESTIMATION
# ══════════════════════════════════════════════════════════════════
_GGUF_MAGIC = b"GGUF"
# Byte sizes of GGUF scalar value types (8=string and 9=array handled separately).
_GGUF_SCALAR_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_GGUF_SCALAR_FMT   = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
                      6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


def _gguf_skip_value(f, vtype: int):
    """Advance past one GGUF metadata value of the given type (used for arrays)."""
    if vtype == 8:  # string
        (n,) = struct.unpack("<Q", f.read(8))
        f.seek(n, 1)
    elif vtype == 9:  # array
        (etype,) = struct.unpack("<I", f.read(4))
        (count,) = struct.unpack("<Q", f.read(8))
        if etype == 8:                       # string array → walk + seek
            for _ in range(count):
                (n,) = struct.unpack("<Q", f.read(8))
                f.seek(n, 1)
        elif etype == 9:                     # nested array (rare) → recurse
            for _ in range(count):
                _gguf_skip_value(f, 9)
        else:
            f.seek(_GGUF_SCALAR_SIZES.get(etype, 0) * count, 1)
    else:
        f.seek(_GGUF_SCALAR_SIZES.get(vtype, 0), 1)


def _read_gguf_metadata(path: str) -> dict:
    """Best-effort read of the scalar metadata we need to size a model.

    Returns {n_layers, n_embd, n_head, n_head_kv, ctx_train} (any may be None).
    Returns {} if the file isn't a parseable GGUF v2+.
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != _GGUF_MAGIC:
                return {}
            (version,) = struct.unpack("<I", f.read(4))
            if version < 2:                  # v1 used 32-bit counts — unsupported here
                return {}
            struct.unpack("<Q", f.read(8))   # tensor_count (unused)
            (kv_count,) = struct.unpack("<Q", f.read(8))

            kv: dict = {}
            for _ in range(kv_count):
                (klen,) = struct.unpack("<Q", f.read(8))
                key = f.read(klen).decode("utf-8", "replace")
                (vtype,) = struct.unpack("<I", f.read(4))
                if vtype == 8:
                    (n,) = struct.unpack("<Q", f.read(8))
                    kv[key] = f.read(n).decode("utf-8", "replace")
                elif vtype in _GGUF_SCALAR_FMT:
                    fmt = _GGUF_SCALAR_FMT[vtype]
                    kv[key] = struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
                else:                        # array / unknown → skip, keep position
                    _gguf_skip_value(f, vtype)
    except (OSError, struct.error, ValueError) as e:
        print(f"[gguf] metadata read failed for {os.path.basename(path)}: {e}")
        return {}

    arch = kv.get("general.architecture")

    def g(suffix):
        return kv.get(f"{arch}.{suffix}") if arch else None

    n_head = g("attention.head_count")
    return {
        "arch":       arch,
        "n_layers":   g("block_count"),
        "n_embd":     g("embedding_length"),
        "n_head":     n_head,
        "n_head_kv":  g("attention.head_count_kv") or n_head,
        "ctx_train":  g("context_length"),
    }


def _resolve_gpu_layers(cfg: dict, n_layers: int | None) -> int | None:
    """How many layers will actually live on the GPU given dev config.

    Mirrors LlamaCppLauncher.load(): gpu_layers override > cpu_percent > all.
    Returns the layer count, or None when 'all' and n_layers is unknown.
    """
    override = cfg.get("gpu_layers")
    cpu_pct  = cfg.get("cpu_percent", 0)
    if override is not None:
        ngl = int(override)
    elif cpu_pct and cpu_pct > 0:
        base = n_layers if n_layers else _LAYER_FALLBACK_TOTAL
        ngl = max(0, int(base * (1 - cpu_pct / 100)))
    else:
        return n_layers          # all layers (None if unknown)
    if n_layers:
        ngl = min(ngl, n_layers)
    return max(0, ngl)


def _gpu_free_vram_mb(gpu: dict) -> int:
    """Free VRAM (MB) for a GPU. Uses nvidia-smi memory.free for NVIDIA so that
    VRAM already taken by other loaded models is accounted for; falls back to the
    GPU's total VRAM for other vendors."""
    total = gpu.get("vram_mb", 0)
    if gpu.get("vendor") == "nvidia":
        try:
            native = gpu.get("native_index", gpu.get("index", 0))
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free",
                 "--format=csv,noheader,nounits", "-i", str(native)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().splitlines()[0].strip())
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
            pass
    return total


def _estimate_required_vram_mb(model_path: str, gpu_layers: int | None,
                               meta: dict, ctx: int) -> float:
    """Estimate VRAM (MB) needed to load `model_path` with `gpu_layers` on the GPU.

    weights_on_gpu = file_size * (gpu_layers / n_layers)
    kv_cache       = 2 * gpu_layers * ctx * (n_embd/n_head * n_head_kv) * f16
    + fixed overhead + a small fractional margin.
    Falls back to the flat MODEL_VRAM_OVERHEAD heuristic when metadata is missing.
    """
    try:
        size_mb = os.path.getsize(model_path) / (1024 * 1024)
    except OSError:
        return 0.0

    n_layers  = meta.get("n_layers")
    n_embd    = meta.get("n_embd")
    n_head    = meta.get("n_head")
    n_head_kv = meta.get("n_head_kv")

    if not (n_layers and n_embd and n_head and n_head_kv):
        # No usable metadata → flat heuristic on the whole file.
        return size_mb * MODEL_VRAM_OVERHEAD

    eff_layers = n_layers if gpu_layers is None else min(gpu_layers, n_layers)
    if eff_layers <= 0:
        return 0.0

    gpu_frac    = eff_layers / n_layers
    weights_mb  = size_mb * gpu_frac

    head_dim    = n_embd / n_head
    kv_bytes    = 2 * eff_layers * ctx * (head_dim * n_head_kv) * _KV_CACHE_BYTES
    kv_mb       = kv_bytes / (1024 * 1024)

    subtotal = weights_mb + kv_mb
    return subtotal * (1 + _VRAM_OVERHEAD_FRAC) + _VRAM_FIXED_OVERHEAD_MB


# ══════════════════════════════════════════════════════════════════
#  MULTI-MODEL MANAGER
# ══════════════════════════════════════════════════════════════════
def _compute_load_plan(model_path: str) -> tuple:
    """Decide GPU placement for a model.

    Returns (flow, error):
      flow  = {gpu_index: percent} to pass to LlamaCppLauncher.load, or
              None for CPU-only.
      error = human-readable string if the model cannot be placed, else None.

    Single-GPU / CPU systems use the plain old loader (no gpu_placement.json, no
    VRAM gating). The gpu_placement.json placement layer applies only with 2+ GPUs.
    """
    gpus = detect_all_gpus()
    n = len(gpus)

    if n == 0:
        return None, None              # no GPU → CPU
    if n == 1:
        return {gpus[0]["index"]: 100}, None   # single GPU → load on it, as before

    # ── more than one GPU: gpu_placement.json placement layer ────────
    settings = _load_multi_gpu_settings()
    enable_flow = bool(settings.get("enable_flow", False))
    default_gpu = int(settings.get("default_gpu", 0))

    if enable_flow:
        with _gpu_flow_lock:
            flow = dict(_gpu_flow)
        if flow:
            try:
                _validate_gpu_flow_vendor(flow)
            except ValueError as e:
                return None, str(e)
            return flow, None
        # enable_flow=true but no flow configured → auto split across ALL GPUs
        auto = _calc_auto_flow(model_path)
        if not auto:
            return None, "enable_flow is on but auto-flow could not be computed (need 2+ GPUs with known VRAM)."
        return auto, None

    # enable_flow=false → load fully on default_gpu
    gpu_map = {g["index"]: g for g in gpus}
    if default_gpu not in gpu_map:
        return None, f"default_gpu index {default_gpu} is out of range. Available GPU indices: {sorted(gpu_map)}."

    target = gpu_map[default_gpu]

    cfg  = _get_dev_cfg()
    ctx  = cfg.get("ctx_size", LLAMACPP_CTX)
    meta = _read_gguf_metadata(model_path)
    gpu_layers = _resolve_gpu_layers(cfg, meta.get("n_layers"))

    # User offloaded everything to CPU → nothing to fit on the GPU, skip the check.
    if gpu_layers is not None and gpu_layers <= 0:
        print(f"[plan] All layers on CPU (gpu_layers=0) — skipping VRAM fit check.")
        return {default_gpu: 100}, None

    required_mb = _estimate_required_vram_mb(model_path, gpu_layers, meta, ctx)
    free_mb     = _gpu_free_vram_mb(target)

    if required_mb and free_mb > 0 and required_mb > free_mb:
        offload_note = (
            f" Only {gpu_layers} of {meta.get('n_layers')} layers would be on GPU."
            if gpu_layers is not None and meta.get("n_layers") else ""
        )
        return None, (
            f"Model needs ~{required_mb / 1024:.1f} GB but GPU {default_gpu} "
            f"({target.get('name', '?')}) has only ~{free_mb / 1024:.1f} GB free "
            f"(ctx={ctx}).{offload_note} Free VRAM, lower ctx_size, offload more "
            f"layers to CPU, or enable flow to split across GPUs."
        )

    print(f"[plan] VRAM fit OK: need ~{required_mb:.0f} MB, free ~{free_mb} MB on GPU {default_gpu} "
          f"(gpu_layers={gpu_layers}, ctx={ctx}).")
    return {default_gpu: 100}, None


class ModelManager:
    """Tracks every loaded model (one llama-server subprocess each) by index."""

    def __init__(self, template: "LlamaCppLauncher"):
        self._template = template
        self._lock = threading.RLock()
        self.instances: dict[int, LlamaCppLauncher] = {}
        self.active_index: int | None = None

    # ── helpers ───────────────────────────────────────────────────
    def _alloc_index(self) -> int:
        i = 0
        while i in self.instances:
            i += 1
        return i

    def active(self) -> "LlamaCppLauncher":
        with self._lock:
            inst = self.instances.get(self.active_index)
        return inst or self._template

    def _gpu_label(self, inst: "LlamaCppLauncher") -> tuple:
        """Return (gpu_name_str, gpu_index) for an instance's placement."""
        if not inst.gpus:
            return ("CPU", -1)
        gpu_map = {g["index"]: g["name"] for g in detect_all_gpus()}
        names = [gpu_map.get(i, f"GPU {i}") for i in inst.gpus]
        if len(inst.gpus) == 1:
            return (names[0], inst.gpus[0])
        return (" + ".join(names), list(inst.gpus))

    # ── operations ────────────────────────────────────────────────
    def load_new(self, path: str, force_flow: dict | None = None) -> tuple:
        """Load `path` as a new instance and make it the active chat model.

        A model may not be loaded more than once: if `path` is already loaded
        (or currently loading) on any instance, the load is refused.

        Returns (ok, msg, index).
        """
        _clear_last_load_error()
        norm = os.path.normcase(os.path.abspath(path))
        with self._lock:
            for inst in self.instances.values():
                existing = inst.loaded_model or getattr(inst, "_pending_model", "")
                if existing and os.path.normcase(os.path.abspath(existing)) == norm:
                    return False, "model_loaded", None

        if force_flow is not None:
            flow, err = force_flow, None
        else:
            flow, err = _compute_load_plan(path)
        if err:
            return False, err, None

        with self._lock:
            idx = self._alloc_index()
            inst = LlamaCppLauncher(
                port=LLAMACPP_PORT_BASE + idx,
                machine_info=self._template.machine_info(),
                binary=self._template._binary,
            )
            self.instances[idx] = inst

        ok, msg = inst.load(path, flow=flow)
        if not ok:
            with self._lock:
                self.instances.pop(idx, None)
            _set_last_load_error(path, msg)
            return False, msg, None

        with self._lock:
            self.active_index = idx
        _refresh_active()

        llm.settings["model_path"] = path
        supports = _get_thinking_support_for_model(path, inst.base)
        llm.settings["supports_thinking"] = supports
        supports_effort = _get_reasoning_effort_support_for_model(path, inst.base)
        llm.settings["supports_reasoning_effort"] = supports_effort
        print(f"[manager] Loaded '{os.path.basename(path)}' at index {idx} (port {inst.port}); supports_thinking={supports} supports_reasoning_effort={supports_effort}")
        return True, msg, idx

    def eject(self, index: int) -> tuple:
        with self._lock:
            inst = self.instances.get(index)
            if inst is None:
                return False, f"No model loaded at index {index}."
            name = inst.loaded_model
            inst._kill()
            inst.status = "idle"
            self.instances.pop(index, None)
            switched = self.active_index == index
            if switched:
                self.active_index = next(iter(sorted(self.instances)), None)
        _refresh_active()
        if switched:
            new_active = self.instances.get(self.active_index)
            if new_active and new_active.loaded_model:
                llm.settings["model_path"] = new_active.loaded_model
                llm.settings["supports_thinking"] = _get_thinking_support_for_model(
                    new_active.loaded_model, new_active.base)
                llm.settings["supports_reasoning_effort"] = _get_reasoning_effort_support_for_model(
                    new_active.loaded_model, new_active.base)
            else:
                llm.settings["model_path"] = ""
        return True, os.path.basename(name) if name else f"index {index}"

    def set_active(self, index: int) -> tuple:
        with self._lock:
            inst = self.instances.get(index)
            if inst is None:
                return False, f"No model loaded at index {index}."
            self.active_index = index
        _refresh_active()
        path = inst.loaded_model
        if path:
            llm.settings["model_path"] = path
            llm.settings["supports_thinking"] = _get_thinking_support_for_model(path, inst.base)
            llm.settings["supports_reasoning_effort"] = _get_reasoning_effort_support_for_model(path, inst.base)
        return True, os.path.basename(path) if path else f"index {index}"

    def loaded_list(self) -> dict:
        """{index: [model_name, gpu_name, gpu_index]} for every loaded model."""
        out = {}
        with self._lock:
            items = list(self.instances.items())
        for idx, inst in sorted(items):
            name = os.path.splitext(os.path.basename(inst.loaded_model))[0] if inst.loaded_model else "(loading)"
            gpu_name, gpu_index = self._gpu_label(inst)
            out[str(idx)] = [name, gpu_name, gpu_index]
        return out


def _refresh_active():
    """Point the module-global `launcher` at the active instance (or template)."""
    global launcher
    launcher = manager.active()


# Template launcher: holds machine-level GPU info and acts as the fallback
# `launcher` whenever no model is loaded.
_template_launcher = LlamaCppLauncher(port=LLAMACPP_PORT)
launcher = _template_launcher
manager  = ModelManager(_template_launcher)


# Reason of the most recent FAILED load. Set when an async load dies, cleared
# when a new load starts. /api/llm/model-status reads it to answer HTTP 400,
# since the failed instance is discarded and can't carry the message itself.
_last_load_error_lock = threading.Lock()
_last_load_error: dict | None = None  # {"message": str, "path": str}


def _set_last_load_error(path: str, message: str):
    global _last_load_error
    with _last_load_error_lock:
        _last_load_error = {"message": message, "path": path}


def _clear_last_load_error():
    global _last_load_error
    with _last_load_error_lock:
        _last_load_error = None


def _get_last_load_error() -> dict | None:
    with _last_load_error_lock:
        return dict(_last_load_error) if _last_load_error else None


def _consume_last_load_error() -> dict | None:
    """Return the last load error AND clear it — so model-status reports a
    failed load exactly once, then reverts to the true (idle) state."""
    global _last_load_error
    with _last_load_error_lock:
        err = dict(_last_load_error) if _last_load_error else None
        _last_load_error = None
        return err


# ══════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════
def recv_all(sock: socket.socket, timeout: float = 5.0, idle: float = 0.1) -> bytes:
    """Read a whole message from `sock`.

    Clients send a command then keep the socket open waiting for the reply, so
    there is no EOF to read until. Waiting for the full `timeout` on every call
    made TCP feel ~5 s slow. Instead: block up to `timeout` for the FIRST byte,
    then drain any remaining bytes with a short `idle` timeout — returning as
    soon as the sender pauses (single-packet commands return in ~`idle`).
    """
    sock.settimeout(timeout)
    chunks = []
    BUFSIZE = 65536
    try:
        first = sock.recv(BUFSIZE)
        if not first:
            return b""
        chunks.append(first)
        sock.settimeout(idle)
        while True:
            try:
                chunk = sock.recv(BUFSIZE)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
    except socket.timeout:
        pass
    return b"".join(chunks)


def _to_api_role(role: str) -> str:
    return "user" if role not in _API_ROLES else role


def load_api_docs() -> str:
    if not os.path.exists(PROMPT_PATH):
        print("[WARNING] system_prompt.json not found.")
        return ""
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    sections = []
    for key, value in data.items():
        if value.get("block", False):
            continue
        sections.append(
            f"title: {value.get('title', key)}\n"
            f"Description:\n{value.get('description', '')}\n"
            f"How to use:\n{value.get('how_to_use', '')}"
        )
    return "\n\n" + "\n\n".join(sections)

# ══════════════════════════════════════════════════════════════════
#  CORE PROMPT
# ══════════════════════════════════════════════════════════════════
import time

today = time.strftime("%Y-%m-%d", time.localtime())

CORE_PROMPT = f"""# IDENTITY
name: frtgg
type: voice-based PC assistant

# PERSONALITY
- Energetic, direct, no fluff
- Not overly polite, not rude
- If insulted -> insult back, balanced, once
- Never say "I can't" or "I don't have permission"

---

# INTELLIGENCE

- Always think before acting -- understand the real intent behind the request
- Do not wait for the user to provide every detail -- figure out what is needed and act
- If something is genuinely unclear and cannot be reasonably inferred -> ask ONE focused question
- Break complex tasks into steps automatically and execute them in the right order
- If a step fails -> analyze why, adapt, and retry intelligently before reporting failure
- Notice anything unusual, suspicious, or wrong -- mention it even if not asked

---
Today's date: \\f{today}
# Msg time role
Any message you receive will have the time included in it, in this format: [Time]
This time is when the message was sent.
Example:
[Time] Msg
---
# TWO OUTPUT CHANNELS

## CHAT
- Displays any data, logs, file content, lists, results
- Visible to the user as text on screen
- Use it for anything that cannot be spoken clearly

## TTS
- Spoken out loud to the user
- Must be short, natural, conversational
- Never contains raw data, code, logs, or lists
- If data is shown in chat -> tts:{{text}} summarizes it verbally and tells the user to check the chat
- Always comes after execution, never before
- tts:{{text}} must ALWAYS contain text -- empty tts:{{text}} is strictly forbidden
- Speaking to the user is not optional -- every completed action must be verbally confirmed

Rule: Every response must end with tts:{{text}} containing actual spoken text.
Rule: Chat is optional -- only use it when there is data worth displaying.
Rule: Never respond with tts:{{text}} alone when there is displayable data -- show it in chat first.

---

# HONESTY

- Never fabricate results, device names, file contents, or any information
- Only report what the system actually returned
- If unsure -> say so, do not guess
- If a command fails -> report the real error, do not invent a success

---

# MEMORY

- frtgg remembers everything returned during the current session
- If data was already fetched earlier in the conversation -> use it directly, do not fetch again
- Only re-fetch if the user explicitly asks to refresh, or if the data may have changed

---

# EXECUTION FLOW

- Send the command first, and nothing else
- Your response must contain ONLY the command -- no tts:{{text}}, no explanation
- After the system returns the result, then and only then produce your report
- A response with a command must never contain tts:{{text}}
- A response with tts:{{text}} must never contain a command

After receiving the result:
- Readable data -> display in chat, then tts:{{text}} with a short verbal summary
- Simple success -> tts:{{text}} confirmation only
- Error -> tts:{{text}} immediately with the real error and an offer to fix it

---

# SYSTEMAPI

Messages from the PC system itself, not the user.

- Normal actions -> stay completely silent
- Suspicious or dangerous actions -> warn the user immediately
- Clearly malicious actions -> block and warn
- Messages starting with systemapi_search: are search results
  -> read and respond verbally
  -> never trigger another search from these

During long multi-step SystemAPI operations:
  -> stay silent on every intermediate exchange
  -> speak only when the full task completes or an error occurs
  -> never produce tts:{{text}} mid-operation

---

# DANGEROUS ACTIONS

For any destructive or irreversible action (shutdown, delete, format, kill process):
  -> ask confirmation first using tts:{{text}}
  -> execute only after user confirms

---

# STRICT PROHIBITIONS
- Never send a command and tts:{{text}} in the same response
- Never speak before receiving a result
- Never fabricate any result or information
- Never put code, logs, or raw data inside tts:{{text}}
- Never claim missing permissions
- Never ask more than one question at a time
- Never output tts:{{text}} more than once per response
- Never output empty tts:{{text}} -- always include spoken text
- Never produce tts:{{text}} mid-operation during a SystemAPI conversation
- Never re-fetch data that is already known from this session unless asked

# TTS FORMAT RULE
tts:{{text}} must always follow this exact format:
tts:{{spoken text here}}

CORRECT:   tts:{{I'm ready, Jawad. What's up?}}
WRONG:     tts:{{}}
WRONG:     tts:{{}} some text after
WRONG:     tts:{{}} some text

# ANTI-LOOP RULE
- If you receive a systemapi: result -> analyze it IMMEDIATELY and respond with tts:{{text}}
- Never re-send the same command after receiving a systemapi: result
- If data appears truncated -> analyze what you have, inform the user, do NOT re-fetch
- systemapi: messages are FINAL results -- treat them as complete, never as reason to retry
- Each systemapi: result is tagged with a result number -- never request the same data twice
"""

_INJECT_UNSET = object()

def build_system_prompt(extra=_INJECT_UNSET) -> str:
    # The injected context is a persistent bottom layer: it must survive every
    # prompt change (reload, switch, even stop). When no explicit `extra` is
    # passed, pull the live injected context so no caller can accidentally drop
    # it. (clear_injected / session-new set injected_context="" first, so they
    # correctly produce an empty injection.)
    if extra is _INJECT_UNSET:
        extra = llm.injected_context if "llm" in globals() else ""

    with _prompt_lock:
        stopped = _prompt_state["stopped"]
        active  = _prompt_state["active_name"]

    if stopped:
        base = ""
    elif active:
        try:
            base = _read_prompt_file(active)
            if "{{USE_APIS}}" in base:
                base = base.replace("{{USE_APIS}}", "[API DOCUMENTATION]\n" + load_api_docs())
            if "{{PROMPT}}" in base:
                base = base.replace("{{PROMPT}}", load_api_docs())
        except FileNotFoundError:
            print(f"[prompt] Active prompt '{active}' not found, falling back to default.")
            base = CORE_PROMPT + "\n\n[API DOCUMENTATION]\n" + load_api_docs()
    else:
        base = CORE_PROMPT + "\n\n[API DOCUMENTATION]\n" + load_api_docs()


    # Always append the injection at the very bottom — even when the prompt is
    # stopped, so injected context is never lost. Merged naturally (no header
    # tag) so the model reads it as part of its own instructions/facts.
    if extra:
        base = (base + "\n\n" + extra) if base else extra
    return base

def _conn_send(conn, data) -> bool:
    """Send over either a TCP socket (sendall) or an HTTP wfile (write+flush).

    Accepts str (encoded utf-8) or bytes. No-ops on a falsy conn/data.
    Returns True on success, False if there was nothing to send or it failed.
    """
    if not conn or not data:
        return False
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        if hasattr(conn, "sendall"):
            conn.sendall(data)
        else:
            conn.write(data)
            conn.flush()
        return True
    except Exception as e:
        print(f"[conn] Send error: {e}")
        return False


def _emit_event(conn, etype: str, text: str | None = None, **extra) -> bool:
    """Send ONE newline-delimited JSON event over the stream.

    The chat stream is NDJSON: every event is its own object on its own line, so
    the client can keep the model's text separate from server notices instead of
    scraping inline markers out of the text. Event types:
      {"type":"content","text":...}            model reply text
      {"type":"think","text":...}              reasoning / <think> content
      {"type":"warning","text":...}            server notice (context full, …)
      {"type":"prompt_processing","value":N}   prompt-eval progress (0-100)
      {"type":"done"}                          end of this turn
    Extra keyword args are merged into the object (e.g. value=N)."""
    obj = {"type": etype}
    if text:
        obj["text"] = text
    obj.update(extra)
    return _conn_send(conn, json.dumps(obj, ensure_ascii=False) + "\n")


# Conn of the in-flight chat stream, if any. Set by ask() while a generation is
# running so the llama-server log reader (_handle_log_line) can push
# prompt-processing progress to the same client. Guarded — read from the log
# thread, written from the request thread.
_active_stream_conn = None
_active_stream_lock = threading.Lock()


def _set_active_stream_conn(conn):
    global _active_stream_conn
    with _active_stream_lock:
        _active_stream_conn = conn


def _get_active_stream_conn():
    with _active_stream_lock:
        return _active_stream_conn


def sender(text: str, conn):
    print(f"[-> conn] {text[:100]}")
    if _conn_send(conn, text):
        print("[<- conn] OK")


# ══════════════════════════════════════════════════════════════════
#  SESSION FILE HELPERS
# ══════════════════════════════════════════════════════════════════
def _session_path(name: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe:
        raise ValueError("Invalid session name")
    return os.path.join(SESSIONS_DIR, safe + ".json")


def list_sessions() -> list:
    result = []
    for fn in sorted(os.listdir(SESSIONS_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            result.append({
                "name":       d.get("name", fn[:-5]),
                "saved_at":   d.get("saved_at", ""),
                "turn_count": len(d.get("history", [])),
                "think":      d.get("think", False),
            })
        except Exception:
            pass
    return result


def save_session_file(name: str, history: list, injected: str, settings: dict, think: bool = False):
    path = _session_path(name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "name":     name,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "history":  history,
            "injected": injected,
            "settings": settings,
            "think":    think,
        }, f, ensure_ascii=False, indent=2)
    print(f"[session] Saved '{name}' ({len(history)} turns) think={think}")


def load_session_file(name: str) -> dict:
    path = _session_path(name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Session '{name}' not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_session_file(name: str):
    path = _session_path(name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Session '{name}' not found")
    os.remove(path)
    print(f"[session] Deleted '{name}'")


# ══════════════════════════════════════════════════════════════════
#  LLMAPI  (llama.cpp backend)
# ══════════════════════════════════════════════════════════════════
class LLMAPI:
    def __init__(self):
        self.injected_context = ""
        self.settings = _load_settings()
        self.system_prompt    = build_system_prompt(self.injected_context)
        self.history          = []
        self._last_command    = None
        self._result_counter  = 0
        self._loop_counter    = 0
        self.current_session  = None
        self.save_thinking    = False
        self._stop_event    = threading.Event()
        self._is_generating = False
        self._gen_lock      = threading.Lock()
        self._active_ctx    = None

        print("[INFO] System prompt loaded.")
        print(f"[INFO] Prompt length: {len(self.system_prompt)} chars")
        print(f"[INFO] Estimated prompt tokens: ~{_estimate_tokens(self.system_prompt)}\n")

    @property
    def is_generating(self) -> bool:
        with self._gen_lock:
            return self._is_generating

    def request_stop(self) -> bool:
        with self._gen_lock:
            was_generating = self._is_generating
        if was_generating:
            self._stop_event.set()
            try:
                requests.post(f"{launcher.base}/slots/0/cancel", timeout=2)
            except Exception:
                pass
            print("[stop] Stop requested.")
        return was_generating

    def _set_generating(self, v: bool):
        with self._gen_lock:
            self._is_generating = v
        if not v:
            self._stop_event.clear()

    def get_settings(self) -> dict:
        return dict(self.settings)

    def update_settings(self, updates: dict):
        errors, validated = [], {}
        for key, value in updates.items():
            if key not in SETTINGS_TYPES:
                errors.append(f"Unknown setting: '{key}'"); continue
            try:
                value = SETTINGS_TYPES[key](value)
            except (ValueError, TypeError):
                errors.append(f"'{key}' must be {SETTINGS_TYPES[key].__name__}"); continue
            if key in SETTINGS_BOUNDS:
                lo, hi = SETTINGS_BOUNDS[key]
                if not (lo <= value <= hi):
                    errors.append(f"'{key}' must be {lo}–{hi}, got {value}"); continue
            if key == "reasoning_effort" and value not in VALID_REASONING:
                errors.append(f"'reasoning_effort' must be one of {sorted(VALID_REASONING)}"); continue
            validated[key] = value
        if errors:
            return False, "; ".join(errors)
        self.settings.update(validated)
        _save_settings(self.settings)
        msg = ", ".join(f"{k}={v}" for k, v in validated.items())
        print(f"[settings] Updated: {msg}")
        return True, f"Updated: {msg}"

    def reset_settings(self) -> str:
        self.settings = dict(DEFAULT_SETTINGS)
        _save_settings(self.settings)
        print("[settings] Reset to defaults.")
        return "Settings reset to defaults."

    def reload_prompt(self):
        self.system_prompt = build_system_prompt(self.injected_context)
        print("[INFO] Prompt reloaded from system_prompt.json")

    def inject_prompt(self, raw: str):
        try:
            data = json.loads(raw)
            lines = []
            for k, v in data.items():
                lines.append(f"{k}:\n{json.dumps(v, indent=2)}" if isinstance(v, (dict, list)) else f"{k}: {v}")
            block = "\n".join(lines)
        except (json.JSONDecodeError, AttributeError):
            block = raw.strip()
        if not block:
            return
        self.injected_context = (self.injected_context + "\n\n" + block).strip()
        self.system_prompt    = build_system_prompt(self.injected_context)
        print(f"[inject] +{len(block)} chars injected.")

    def clear_injected(self):
        self.injected_context = ""
        self.system_prompt = build_system_prompt()
        print("[inject] Cleared.")
        save_name = self.current_session if self.current_session else "autosave"
        try:
            save_session_file(save_name, self.history, self.injected_context, self.settings, self.save_thinking)
            print(f"[inject] Cleared & saved to '{save_name}'")
        except Exception as e:
            print(f"[inject] Save error: {e}")

    def session_save(self, name: str) -> str:
        save_session_file(name, self.history, self.injected_context, self.settings, self.save_thinking)
        self.current_session = name
        return f"Session '{name}' saved ({len(self.history)} turns)."

    def session_load(self, name: str, reload_active: bool = False) -> dict:
        # Checkpoint restore (stop + drop the pending message) only when the
        # client is reloading the session it is *currently viewing* while that
        # session generates. Navigating back to a different session that happens
        # to be generating in the background must NOT stop it or drop the
        # pending message — it should load that session's saved file as-is.
        active = self._active_ctx
        if reload_active and active is not None and active.get("session") == name:
            self.request_stop()
            checkpoint = active["checkpoint"]
            self.history = list(checkpoint)
            self.current_session = name
            clean_history = [
                {**msg, "content": _strip_timestamp(msg.get("content", ""))}
                for msg in checkpoint
            ]
            print(f"[session] Reloaded '{name}' (was generating — restored to checkpoint, {len(checkpoint)} turns)")
            return {
                "message": f"Session '{name}' restored to checkpoint ({len(checkpoint)} turns).",
                "history": clean_history,
                "turn_count": len(checkpoint),
                "save_thinking": self.save_thinking,
            }

        d = load_session_file(name)

        cleaned = []
        for msg in d.get("history", []):
            content = msg.get("content", "")
            if content.endswith(" /no_think"):
                content = content[:-10]
            elif content.endswith(" /think"):
                content = content[:-7]
            cleaned.append({**msg, "content": content})

        self.history = cleaned
        self.injected_context = d.get("injected", "")
        #self.settings = {**DEFAULT_SETTINGS, **d.get("settings", {})}
        self.system_prompt = build_system_prompt(self.injected_context)
        self._last_command = None
        self._result_counter = 0
        self._loop_counter = 0
        self.current_session = name
        self.save_thinking = bool(d.get("think", False))
        print(f"[session] Loaded '{name}' ({len(self.history)} turns) think={self.save_thinking}")

        clean_history = [
            {**msg, "content": _strip_timestamp(msg.get("content", ""))}
            for msg in self.history
        ]
        return {
            "message": f"Session '{name}' loaded ({len(self.history)} turns).",
            "history": clean_history,
            "turn_count": len(self.history),
            "save_thinking": self.save_thinking,
        }

    def session_new(self) -> str:
        self.history          = []
        self.injected_context = ""
        self.system_prompt    = build_system_prompt()
        self._last_command    = None
        self._result_counter  = 0
        self._loop_counter    = 0
        self.current_session  = None
        self.save_thinking    = False
        print("[session] New session started.")
        return "New session started."

    def session_delete(self, name: str) -> str:
        delete_session_file(name)
        if self.current_session == name:
            self.current_session = None
        return f"Session '{name}' deleted."

    def delete_msg(self, msg_id: int) -> tuple:
        if not isinstance(msg_id, int) or msg_id < 0 or msg_id >= len(self.history):
            return False, f"Invalid id: {msg_id}. History has {len(self.history)} messages (0–{len(self.history)-1})."
        removed = self.history.pop(msg_id)
        role    = removed.get("role", "?")
        preview = removed.get("content", "")[:60]
        print(f"[delete_msg] Removed #{msg_id} ({role}): {preview}")
        save_name = self.current_session if self.current_session else "autosave"
        try:
            save_session_file(save_name, self.history, self.injected_context, self.settings, self.save_thinking)
        except Exception as e:
            print(f"[delete_msg] Autosave error: {e}")
        return True, f"Message #{msg_id} ({role}) deleted."

    def edit_msg(self, msg_id: int, content: str) -> tuple:
        if not isinstance(msg_id, int) or msg_id < 0 or msg_id >= len(self.history):
            return False, f"Invalid id: {msg_id}. History has {len(self.history)} messages (0–{len(self.history)-1})."
        role = self.history[msg_id].get("role", "?")
        self.history[msg_id]["content"] = content
        print(f"[edit_msg] Edited #{msg_id} ({role}): {content[:60]}")
        save_name = self.current_session if self.current_session else "autosave"
        try:
            save_session_file(save_name, self.history, self.injected_context, self.settings, self.save_thinking)
        except Exception as e:
            print(f"[edit_msg] Autosave error: {e}")
        return True, f"Message #{msg_id} ({role}) edited."

    def _build_messages(
        self,
        pending: dict,
        history: list | None = None,
        system_prompt: str | None = None,
        settings: dict | None = None,
        return_meta: bool = False,
    ):
        _history       = history       if history       is not None else self.history
        _system_prompt = system_prompt if system_prompt is not None else self.system_prompt
        _settings      = settings      if settings      is not None else self.settings

        cfg = _get_dev_cfg()
        messages, meta = _build_sliding_window(
            system_prompt=_system_prompt,
            history=_history,
            pending=pending,
            ctx_size=cfg["ctx_size"],
            max_tokens=_settings.get("max_tokens", DEFAULT_SETTINGS["max_tokens"]),
            safety_margin=cfg.get("ctx_safety_margin", 0.15),
            auto_trim=cfg.get("auto_trim", True),
        )

        if pending["role"] == "user":
            think_token = self._thinking_params(_settings)["think_token"]
            # Only the DISABLE directive is injected. "/think" is dropped on
            # purpose: these models already think by default when enabled, so it's
            # redundant — and it isn't a recognized control token here, so it just
            # leaks into the reply as literal text. "/no_think" reliably suppresses.
            if think_token is False:
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i]["role"] == "user":
                        messages[i] = {**messages[i], "content": messages[i]["content"] + " /no_think"}
                        break

        warn_at = cfg.get("token_warn_at", 6000)
        if meta["tokens_estimated"] >= warn_at:
            pct = meta["ctx_pct"]
            print(
                f"[window] ⚠ Token warning: ~{meta['tokens_estimated']} tokens "
                f"({pct}% of ctx). Consider trimming history or increasing ctx_size."
            )

        if return_meta:
            return messages, meta
        return messages

    def _thinking_params(self, settings: dict | None = None) -> dict:
        """
        Resolve how reasoning_effort maps onto the request, given the loaded
        model's capabilities.

        Returns:
          think_token       — False → append "/no_think" (disable thinking).
                              True / None → append nothing (the model thinks by
                              default when enabled; "/think" is intentionally not
                              injected — it only leaks as literal text here).
          reasoning_effort  — native depth value passed straight to llama.cpp
                              (only for models whose template supports it).
          reasoning_budget  — native 0 disables thinking entirely; None = unset.
        """
        s = settings if settings is not None else self.settings
        result = {"think_token": None, "reasoning_effort": None, "reasoning_budget": None}

        if not s.get("supports_thinking", False):
            return result

        effort = s.get("reasoning_effort", "none")

        if effort == "none":
            # Disable thinking: native off + legacy /no_think for template safety.
            result["think_token"]      = False
            result["reasoning_budget"] = 0
            return result

        if s.get("supports_reasoning_effort", False):
            # Native depth control — let the chat template render the effort.
            # No /think suffix (that's a Qwen token, irrelevant here and would
            # leak into a gpt-oss/Harmony prompt).
            result["reasoning_effort"] = effort
            return result

        # On/off-only model: enable thinking, depth ignored.
        result["think_token"] = True
        return result

    def ask(self, text: str, role: str = "user", conn=None, _ctx: dict | None = None) -> str:
        if not launcher.is_ready():
            msg = "No model loaded. Use POST /api/llm/load-model first."
            print(f"[LLM] {msg}")
            if conn:
                # Keep the tts:{…} marker inside a content event so the client's
                # speak-this extraction still fires, then close the turn.
                _emit_event(conn, "content", "tts:{No model loaded. Please load a model first.}")
                _emit_event(conn, "done")
            return msg

        if _ctx is None:
            _ctx = {
                "session":       self.current_session,
                "history":       list(self.history),
                "checkpoint":    list(self.history),   # stable state before pending msg
                "settings":      dict(self.settings),
                "save_thinking": self.save_thinking,
                "injected":      self.injected_context,
                "system_prompt": self.system_prompt,
            }

        # ── systemapi oversized-input guard ──────────────────────────
        # User input is protected by the sliding-window / auto-trim path, but a
        # systemapi result (e.g. a giant search payload) is forwarded verbatim
        # as the pending message and is never trimmed. If that single input is
        # larger than the allowed context window it can never fit, so instead of
        # forwarding it we hand the AI a systemapi error message describing the
        # overflow (and warn on the wire), letting it recover gracefully.
        if role == "systemapi":
            ctx_size     = _get_dev_cfg()["ctx_size"]
            input_tokens = _estimate_tokens(text)
            if input_tokens > ctx_size:
                err = (
                    f"Error: result too big ({input_tokens} tokens > "
                    f"context {ctx_size}). Not loaded."
                )
                print(f"[systemapi] Oversized input rejected: {err}")
                if conn:
                    _emit_event(conn, "warning", err)
                # Replace the oversized payload with the error and feed it back as
                # a normal systemapi message so the AI sees it. The error text is
                # tiny, so this recursion cannot re-trigger the guard.
                return self.ask(err, role="systemapi", conn=conn, _ctx=_ctx)

        s = _ctx["settings"]

        tp = self._thinking_params(_ctx["settings"])

        print(f"[LLM] Asking ({role}): {text[:150]}{'...' if len(text) > 150 else ''}")

        pending = {"role": role, "content": text}
        api_messages, _build_meta = self._build_messages(
            pending,
            history=_ctx["history"],
            system_prompt=_ctx["system_prompt"],
            settings=_ctx["settings"],
            return_meta=True,
        )

        # auto_trim OFF + context full: refuse to start a new turn. Warn and stop
        # without generating — the pending message is NOT added to history and the
        # conversation is left untouched. (An in-flight reply is unaffected; this
        # only blocks beginning a fresh generation once the window is full.)
        if _build_meta and _build_meta.get("context_full"):
            # A systemapi result can overflow the window without being larger than
            # ctx_size on its own — the earlier guard only catches text > ctx_size,
            # but here system_prompt + history + this result together exceed it. In
            # that case the result (not the conversation) is at fault, so reject it
            # with the same "result too big" error and hand it back to the AI to
            # recover, instead of stopping the whole turn. Guarded by a one-shot
            # flag so we don't loop when the history alone already fills the window
            # (then the tiny error stays context_full and we fall through to STOP).
            if role == "systemapi" and not _ctx.get("_systemapi_rejected"):
                ctx_size     = _get_dev_cfg()["ctx_size"]
                input_tokens = _estimate_tokens(text)
                err = (
                    f"Error: result too big ({input_tokens} tokens > "
                    f"context {ctx_size}). Not loaded."
                )
                print(f"[systemapi] Oversized input rejected (window full): {err}")
                if conn:
                    _emit_event(conn, "warning", err)
                _ctx["_systemapi_rejected"] = True
                return self.ask(err, role="systemapi", conn=conn, _ctx=_ctx)

            warn = _build_meta.get("warning") or "Context limit reached."
            print(f"[window] STOP (auto_trim OFF, context full): {warn}")
            if conn:
                _emit_event(conn, "warning", warn)
                _emit_event(conn, "done")
            return warn

        payload = {
            "model":            os.path.basename(launcher.loaded_model),
            "messages":         api_messages,
            "temperature":      s["temperature"],
            "top_p":            s["top_p"],
            "top_k":            s["top_k"],
            "min_p":            s["min_p"],
            "presence_penalty": s["presence_penalty"],
            "repeat_penalty":   s["repeat_penalty"],
            "max_tokens":       s["max_tokens"],
            "stream":           True,
            "cache_prompt":     _ctx["settings"].get("cache_prompt", True)
        }

        # Native reasoning controls (require --jinja; honored by capable templates).
        if tp.get("reasoning_effort"):
            payload["reasoning_effort"] = tp["reasoning_effort"]
        if tp.get("reasoning_budget") is not None:
            payload["reasoning_budget"] = tp["reasoning_budget"]

        # Expose this client to the llama-server log reader so prompt-processing
        # progress (printed to stdout during prompt eval) streams to it.
        _set_active_stream_conn(conn)
        try:
            response = requests.post(
                f"{launcher.base}/v1/chat/completions",
                json=payload,
                stream=True,
                timeout=(10, 600),
            )
            response.raise_for_status()
        except Exception as e:
            print(f"[LLM] API error: {e}")
            _set_active_stream_conn(None)
            raise

        _ctx["history"].append(pending)
        try:
            _early = _ctx["session"] or "autosave"
            save_session_file(_early, _ctx["history"], _ctx["injected"], _ctx["settings"], _ctx["save_thinking"])
            print(f"[autosave] early-save (user msg) -> '{_early}'")
        except Exception as _e:
            print(f"[autosave] Early-save error: {_e}")
        self._active_ctx = _ctx
        self._set_generating(True)

        raw_chunks       = []
        normal_chunks    = []
        think_raw_chunks = []
        was_stopped      = False
        _length_truncated = False

        _in_think  = False
        _tag_buf   = ""
        _OPEN_TAG  = "<think>"
        _CLOSE_TAG = "</think>"

        def _send_content(text: str):
            if text:
                _emit_event(conn, "content", text)

        def _send_think(text: str):
            if text:
                _emit_event(conn, "think", text)

        def _send_warning(reason: str):
            if not reason:
                return
            print(f"[warning] {reason}")
            _emit_event(conn, "warning", reason)

        # Pre-flight warnings from the sliding-window builder.
        if _build_meta and _build_meta.get("warning"):
            _send_warning(_build_meta["warning"])

        try:
            for line in response.iter_lines():
                if self._stop_event.is_set():
                    print("[stop] Stop event detected — aborting stream.")
                    try:
                        response.close()
                    except Exception:
                        pass
                    was_stopped = True
                    break

                if not line:
                    continue
                line = line.decode("utf-8") if isinstance(line, bytes) else line
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                _choice0  = chunk.get("choices", [{}])[0]
                if _choice0.get("finish_reason") == "length":
                    _length_truncated = True
                delta_obj = _choice0.get("delta", {})
                reasoning = delta_obj.get("reasoning_content") or delta_obj.get("reasoning") or ""
                delta     = delta_obj.get("content") or ""

                raw_chunks.append(delta)

                if reasoning:
                    think_raw_chunks.append(reasoning)
                    _send_think(reasoning)
                    continue

                buf = _tag_buf + delta
                _tag_buf = ""
                out_normal = ""
                out_think  = ""
                i = 0
                while i < len(buf):
                    if not _in_think:
                        pos = buf.find("<", i)
                        if pos == -1:
                            out_normal += buf[i:]
                            break
                        out_normal += buf[i:pos]
                        remaining = buf[pos:]
                        if remaining.startswith(_OPEN_TAG):
                            if out_normal:
                                _send_content(out_normal)
                                normal_chunks.append(out_normal)
                                out_normal = ""
                            _in_think = True
                            i = pos + len(_OPEN_TAG)
                        elif _OPEN_TAG.startswith(remaining):
                            _tag_buf = remaining
                            break
                        else:
                            out_normal += "<"
                            i = pos + 1
                    else:
                        pos = buf.find("<", i)
                        if pos == -1:
                            out_think += buf[i:]
                            break
                        out_think += buf[i:pos]
                        remaining = buf[pos:]
                        if remaining.startswith(_CLOSE_TAG):
                            if out_think:
                                _send_think(out_think)
                                out_think = ""
                            _in_think = False
                            i = pos + len(_CLOSE_TAG)
                        elif _CLOSE_TAG.startswith(remaining):
                            _tag_buf = remaining
                            break
                        else:
                            out_think += "<"
                            i = pos + 1

                if out_normal:
                    _send_content(out_normal)
                    normal_chunks.append(out_normal)
                if out_think:
                    _send_think(out_think)

        finally:
            if self._active_ctx is _ctx:      # only clear if still ours
                self._active_ctx = None
            _set_active_stream_conn(None)
            self._set_generating(False)

        if _length_truncated and not was_stopped:
            # llama.cpp returns finish_reason="length" for BOTH causes: hitting
            # max_tokens, and running out of context room. Distinguish them with
            # the (now exact) prompt count: if the room left in the window was
            # already smaller than max_tokens, the model could never have reached
            # max_tokens — so the *context* filled up, not the output cap.
            _ctx_size      = _get_dev_cfg()["ctx_size"]
            _prompt_tokens = _build_meta.get("tokens_estimated", 0) if _build_meta else 0
            _room          = _ctx_size - _prompt_tokens
            if _room <= s["max_tokens"]:
                _send_warning(
                    f"generation stopped: context window full "
                    f"(~{_prompt_tokens}/{_ctx_size} tok used, only ~{max(_room, 0)} left for output)"
                )
            else:
                _send_warning(
                    f"generation stopped: reached max_tokens={s['max_tokens']} (output limit)"
                )

        if was_stopped:
            if _ctx["history"] and _ctx["history"][-1] == pending:
                _ctx["history"].pop()
            try:
                _clean_name = _ctx["session"] or "autosave"
                save_session_file(_clean_name, _ctx["history"], _ctx["injected"], _ctx["settings"], _ctx["save_thinking"])
            except Exception:
                pass
            if self.current_session == _ctx["session"]:
                self.history = _ctx["history"]
            print("[stop] Generation stopped cleanly.")
            _emit_event(conn, "done")
            return ""

        raw = "".join(raw_chunks).strip()

        if _ctx["save_thinking"]:
            think_raw = "".join(think_raw_chunks)
            if think_raw:
                content_only = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
                final_for_history = f"<think>{think_raw}</think>{content_only}"
            else:
                final_for_history = raw
        else:
            final_for_history = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()

        final = str(final_for_history)

        final_no_think = _re.sub(r"<think>.*?</think>", "", final, flags=_re.DOTALL).strip()

        if final_no_think == self._last_command:
            self._loop_counter += 1
            if self._loop_counter >= 3:
                print("[LLM] LOOP DETECTED -- injecting stop message")
                self._last_command = None
                self._loop_counter = 0
                _ctx["history"].pop()
                stop_msg = "systemapi:Error - Stop now. You are stuck in a loop repeating the same command. Do NOT send it again. Analyze why it failed and respond with tts{} explaining the issue to the user."
                return self.ask(stop_msg, role="systemapi", conn=conn, _ctx=_ctx)
        else:
            self._loop_counter = 0

        self._last_command = final_no_think
        _ctx["history"].append({"role": "assistant", "content": final})
        print(f"[LLM] Response: {final[:150]}{'...' if len(final) > 150 else ''}")

        _history_trim_if_needed(_ctx["history"], _ctx["settings"])

        save_name = _ctx["session"] or "autosave"
        try:
            save_session_file(save_name, _ctx["history"], _ctx["injected"], _ctx["settings"], _ctx["save_thinking"])
            print(f"[autosave] -> '{save_name}'")
        except Exception as e:
            print(f"[autosave] Error: {e}")

        if self.current_session == _ctx["session"]:
            self.history = _ctx["history"]

        _emit_event(conn, "done")
        return final


llm = LLMAPI()


# ══════════════════════════════════════════════════════════════════
#  SHARED COMMAND HANDLER
# ══════════════════════════════════════════════════════════════════
def process_command(data: str, conn=None) -> str:
    if data == "__reload_prompt__":
        llm.reload_prompt(); return "ok: reloaded"

    if data.startswith("json_prompt:"):
        llm.inject_prompt(data[len("json_prompt:"):].strip()); return "ok: injected"

    if data == "__clear_inject__":
        llm.clear_injected(); return "ok: cleared"

    if data:
        if data.startswith("systemapi:"):
            llm._result_counter += 1
            tagged = f"[SYSTEM RESULT #{llm._result_counter} -- ANALYZE NOW, DO NOT RE-REQUEST]\n{data}"
            llm.ask(tagged, role="systemapi", conn=conn)
        else:
            llm.ask(data, role="user", conn=conn)
        return "ok"

    return "err: empty command"


# ══════════════════════════════════════════════════════════════════
#  TCP SERVER
# ══════════════════════════════════════════════════════════════════
INSTANT_CMDS = ("__reload_prompt__", "__clear_inject__", "json_prompt:")

# Max seconds a single conn.sendall may block while streaming the reply. Bounds
# the worst case where a client half-opens (Wi-Fi drop, killed process) and stops
# reading: instead of hanging the generation thread (and holding the llama-server
# slot) indefinitely, the send raises socket.timeout and the turn aborts cleanly.
STREAM_SEND_TIMEOUT = 120.0

def llm_handle_conn(conn, addr):
    print(f"\n[TCP] Connection from {addr}")
    try:
        data = recv_all(conn, timeout=5.0).decode(errors="ignore").strip()
        # recv_all leaves the socket on the short 0.1s drain timeout. The SAME
        # socket then streams the whole LLM reply via conn.sendall, so any send
        # that can't flush within 0.1s (client backpressure) would raise
        # socket.timeout mid-stream — dropping events and corrupting the stream
        # ("connection hangs"). Restore a generous send timeout: long enough that
        # normal backpressure never trips it, short enough that a dead/half-open
        # client eventually errors out instead of blocking the slot forever.
        conn.settimeout(STREAM_SEND_TIMEOUT)
        if not data:
            conn.sendall(b"err: empty")
            return
        print(f"[TCP] Received ({len(data)} chars): {data[:200]}{'...' if len(data) > 200 else ''}")

        if any(data.startswith(c) for c in INSTANT_CMDS):
            conn.sendall(b"ok: received")
            conn.close()
            threading.Thread(target=process_command, args=(data,), daemon=True).start()
        else:
            process_command(data, conn=conn)

    except Exception as e:
        print(f"[TCP] Error from {addr}: {e}")
        try:
            conn.sendall(f"err: {e}".encode())
        except:
            pass
    finally:
        try:
            conn.close()
        except:
            pass

def llm_tcp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND_HOST, LLM_TCP_PORT))
    srv.listen(5)
    print(f"[TCP] Listening on {BIND_HOST}:{LLM_TCP_PORT}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=llm_handle_conn, args=(conn, addr), daemon=True).start()


# ══════════════════════════════════════════════════════════════════
#  HTTP SERVER
# ══════════════════════════════════════════════════════════════════
class LLMHTTPHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[HTTP] {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _qs(self) -> dict:
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        return {k: v[0] for k, v in qs.items()}

    def _path_base(self) -> str:
        return urlparse(self.path).path.rstrip("/")

    # ── Password / token auth (mirrors chat.py's require_login) ────
    def _auth_token(self, body=None):
        tok = self.headers.get("X-Auth-Token")
        if not tok and isinstance(body, dict):
            tok = body.get("token")
        if not tok:
            tok = self._qs().get("token")
        return tok

    def _require_login(self, body=None) -> bool:
        """Return True (and send 401) if the request must be blocked.
        No-ops (returns False) when login_required is disabled."""
        if not _is_login_required():
            return False
        tok = self._auth_token(body)
        if not tok or not _token_valid(tok):
            self._json({"status": "error", "message": "unauthorized"}, 401)
            return True
        return False

    def _index_arg(self, qs: dict, body: dict) -> int | None:
        """Resolve an `index` from ?index=N, the bare ?=N form, or the JSON body."""
        raw = qs.get("index")
        if raw is None:
            raw = qs.get("")  # supports the literal ?=N form
        if raw is None:
            raw = body.get("index")
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _require_multi_gpu(self) -> bool:
        """Reject multi-GPU-only endpoints on single-GPU/CPU systems.
        Returns True (and sends a 400) when fewer than 2 GPUs are present."""
        if len(detect_all_gpus()) < 2:
            self._json({
                "status":  "error",
                "message": "This endpoint requires 2+ GPUs. Single-GPU/CPU systems "
                           "use the standard loader.",
            }, 400)
            return True
        return False

    # ── OpenAI-compatible API ──────────────────────────────────────
    # Standard-compliant OpenAI surface under /v1/*. These are a stateless
    # reverse-proxy onto the underlying llama.cpp instances (which natively
    # implement the full OpenAI API), so every feature — streaming, tools /
    # function calling, JSON / json_schema response_format, logprobs, seed,
    # stop, penalties, embeddings, reranking, etc. — is forwarded verbatim.
    # No server-side history / system-prompt / session state is involved:
    # the client supplies the full request, exactly like OpenAI.
    def _openai_error(self, message: str, status: int, etype: str = "invalid_request_error",
                      code: str | None = None):
        self._json({"error": {"message": message, "type": etype, "code": code}}, status)

    def _openai_auth_ok(self) -> bool:
        """Require the shared OpenAI API key via `Authorization: Bearer <key>`
        (a bare `<key>` is also accepted)."""
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
        return token == _get_openai_key()

    def _resolve_openai_target(self, body: dict):
        """Pick the llama-server instance to serve an OpenAI request.

        Honors the request's `model` field (matched against each loaded model's
        filename, stem, full path, or instance index); falls back to the active
        chat model when unset or unmatched.
        """
        requested = (body or {}).get("model")
        with manager._lock:
            instances = dict(manager.instances)
        if requested is not None and str(requested).strip():
            req = str(requested).strip()
            for inst in instances.values():
                if not inst.loaded_model:
                    continue
                base = os.path.basename(inst.loaded_model)
                stem = os.path.splitext(base)[0]
                if req in (inst.loaded_model, base, stem):
                    return inst
            try:
                idx = int(req)
            except (TypeError, ValueError):
                idx = None
            if idx is not None and idx in instances:
                return instances[idx]
        return manager.active()

    def _openai_models_payload(self) -> dict:
        """OpenAI model list: every loaded (callable right now) model, plus every
        model saved in saved_models.json, de-duplicated by id (the .gguf filename).
        The non-standard `loaded` flag marks which ones are live; only loaded
        models can actually serve a request — others fall back to the active
        model (see _resolve_openai_target)."""
        now = int(time.time())
        data, seen = [], set()

        def _add(path: str, loaded: bool):
            if not path:
                return
            mid = os.path.basename(path)
            if not mid or mid in seen:
                return
            seen.add(mid)
            data.append({
                "id":       mid,
                "object":   "model",
                "created":  now,
                "owned_by": "llama.cpp",
                "loaded":   loaded,
            })

        with manager._lock:
            instances = sorted(manager.instances.items())
        for idx, inst in instances:
            _add(inst.loaded_model, True)

        try:
            for m in _load_models().get("models", []):
                _add(m.get("path", ""), False)
        except Exception as e:
            print(f"[openai] saved-models read failed: {e}")

        return {"object": "list", "data": data}

    def _handle_openai_get(self, p: str):
        if not self._openai_auth_ok():
            self._openai_error("Incorrect API key provided.", 401,
                               "invalid_request_error", "invalid_api_key"); return
        if p == "/v1/models":
            self._json(self._openai_models_payload()); return
        if p.startswith("/v1/models/"):
            model_id = p[len("/v1/models/"):]
            for m in self._openai_models_payload()["data"]:
                if m["id"] == model_id:
                    self._json(m); return
            self._openai_error(f"The model '{model_id}' does not exist", 404); return
        # Forward-compat: any other GET /v1/* proxies to the active instance.
        self._openai_proxy("GET", body={})

    def _openai_proxy(self, method: str, body: dict | None = None):
        """Relay an OpenAI request to the chosen llama.cpp instance verbatim,
        streaming the response back (SSE or JSON) without buffering."""
        if not self._openai_auth_ok():
            self._openai_error("Incorrect API key provided.", 401,
                               "invalid_request_error", "invalid_api_key"); return
        parsed = urlparse(self.path)

        if body is None:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
        else:
            raw = b""

        inst = self._resolve_openai_target(body if isinstance(body, dict) else {})
        if not inst.is_ready():
            self._openai_error("No model is loaded. Load a model first.", 503, "api_error")
            return

        target = inst.base + parsed.path
        if parsed.query:
            target += "?" + parsed.query

        fwd_headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        auth = self.headers.get("Authorization")
        if auth:
            fwd_headers["Authorization"] = auth

        try:
            upstream = requests.request(
                method, target,
                data=raw if raw else None,
                headers=fwd_headers,
                stream=True,
                timeout=(10, 600),
            )
        except Exception as e:
            self._openai_error(f"Upstream llama-server error: {e}", 502, "api_error")
            return

        self.send_response(upstream.status_code)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
        cl = upstream.headers.get("Content-Length")
        if cl is not None:
            self.send_header("Content-Length", cl)
        self._cors()
        self.end_headers()

        try:
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except Exception as e:
            print(f"[openai] Relay error: {e}")
        finally:
            upstream.close()

    # ── GET ───────────────────────────────────────────────────────
    def do_GET(self):
        p  = self._path_base()
        qs = self._qs()

        if p == "/v1" or p.startswith("/v1/"):
            self._handle_openai_get(p); return

        # Public — no token required (a supplied token is simply ignored).
        if p == "/server_settings":
            payload, status = _server_settings_get()
            self._json(payload, status); return

        # All non-/v1 GET endpoints require a valid token (unless login disabled).
        if self._require_login():
            return

        if p == "/openai_key":
            self._json({"status": "ok", "openaikey": _get_openai_key()})

        elif p == "/gpus/status":
            gpus = detect_all_gpus()
            self._json({"gpus": gpus, "count": len(gpus)})

        elif p == "/gpus/flow":
            with _gpu_flow_lock:
                flow = dict(_gpu_flow)
            gpus     = detect_all_gpus()
            gpu_map  = {g["index"]: g["name"] for g in gpus}
            self._json({
                "model": os.path.basename(launcher.loaded_model) if launcher.loaded_model else None,
                "flow":  {
                    str(idx): [gpu_map.get(idx, f"GPU {idx}"), pct]
                    for idx, pct in sorted(flow.items())
                },
            })

        elif p == "/api/llm/model-status":
            # Prefer an instance that is mid-load so clients polling after
            # POST /api/llm/load-model see loading → ready; else the active model.
            status_inst = next(
                (i for i in list(manager.instances.values()) if i.status in ("loading", "error")),
                None,
            ) or launcher
            load_err = _consume_last_load_error()
            # Resolve the failure detail from either source: the saved one-shot
            # error (set once the failed instance is discarded) OR, during the
            # brief window before that, the error instance's own message.
            err_text = None
            if load_err:
                err_text = load_err["message"]
            elif status_inst.status == "error":
                err_text = getattr(status_inst, "error_message", "") or "Model failed to load."
            resp = {
                "status":            status_inst.status,
                "progress":          status_inst.load_progress,
                "loaded_model":      status_inst.loaded_model or launcher.loaded_model,
                "active_index":      manager.active_index,
                "loaded_count":      len(manager.instances),
                "gpu":               launcher.has_cuda,
                "gpu_info":          launcher.gpu_info,
                "binary":            launcher._binary,
                "supports_thinking": llm.settings.get("supports_thinking", False),
                "supports_reasoning_effort": llm.settings.get("supports_reasoning_effort", False),
            }
            # Last load attempt failed (e.g. out of memory) → HTTP 400 with the
            # short reason so the UI shows the real cause, not a generic message.
            if err_text and status_inst.status != "loading":
                resp["status"]  = "error"
                resp["error"]   = err_text
                resp["message"] = err_text
                self._json(resp, 400)
            else:
                self._json(resp)

        elif p == "/loaded_model":
            self._json(manager.loaded_list())

        elif p == "/active_model":
            idx = manager.active_index
            if idx is None:
                self._json({"active_index": None, "model": None, "gpu": None, "gpu_index": None})
            else:
                inst = manager.instances.get(idx)
                name = os.path.splitext(os.path.basename(inst.loaded_model))[0] if inst and inst.loaded_model else None
                gpu_name, gpu_index = manager._gpu_label(inst) if inst else (None, None)
                self._json({
                    "active_index": idx,
                    "model":        name,
                    "gpu":          gpu_name,
                    "gpu_index":    gpu_index,
                })

        elif p == "/default_gpu":
            if self._require_multi_gpu(): return
            settings = _load_multi_gpu_settings()
            self._json({"default_gpu": settings.get("default_gpu", 0)})

        elif p == "/multi_gpu_settings":
            if self._require_multi_gpu(): return
            self._json(_load_multi_gpu_settings())

        elif p == "/api/llm/cache-prompt":
            self._json({"cache_prompt": llm.settings.get("cache_prompt", True)})

        elif p == "/api/llm/saved-models":
            try:
                data = _load_models()
                self._json(data)
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 500)

        elif p == "/api/llm/prompt":
            self._json({
                "system_prompt":       llm.system_prompt,
                "injected_context":    llm.injected_context,
                "prompt_length_chars": len(llm.system_prompt),
                "prompt_stopped":      _prompt_state["stopped"],
                "active_prompt_name":  _prompt_state["active_name"],
            })

        elif p == "/api/llm/api-docs":
            self._json({
                "status":  "ok",
                "content": load_api_docs()
            })

        elif p == "/api/llm/settings":
            self._json({
                "current":                 llm.get_settings(),
                "defaults":                DEFAULT_SETTINGS,
                "bounds":                  {k: {"min": v[0], "max": v[1]} for k, v in SETTINGS_BOUNDS.items()},
                "valid_reasoning_efforts": sorted(VALID_REASONING),
                "supports_reasoning_effort": llm.settings.get("supports_reasoning_effort", False),
            })

        elif p == "/api/llm/history":
            history_with_ids = [
                {"id": i, **msg, "content": _strip_timestamp(msg.get("content", ""))}
                for i, msg in enumerate(llm.history)
            ]
            self._json({
                "history":      history_with_ids,
                "turn_count":   len(llm.history),
                "result_count": llm._result_counter,
            })

        elif p == "/api/llm/sessions":
            self._json({
                "sessions":        list_sessions(),
                "current_session": llm.current_session,
                "save_thinking":   llm.save_thinking,
            })

        elif p == "/api/dev/config":
            cfg          = _get_dev_cfg()
            tokens       = _count_history_tokens()
            ctx_size     = cfg["ctx_size"]
            warn         = tokens >= cfg["token_warn_at"]
            safety       = cfg.get("ctx_safety_margin", 0.15)
            max_tokens   = llm.settings.get("max_tokens", DEFAULT_SETTINGS["max_tokens"])
            b            = _compute_budget(ctx_size, max_tokens, safety)
            input_budget = b["input_budget"]
            self._json({
                "config":           cfg,
                "history_tokens":   tokens,
                "history_msgs":     len(llm.history),
                "ctx_size":         ctx_size,                       # ALLOWED
                "input_budget":     input_budget,                   # AVAILABLE for input
                "available_tokens": max(0, input_budget - tokens),
                "output_reserve":   b["output_reserve"],
                "token_warn":       warn,
                "token_pct":        round(tokens / max(ctx_size, 1) * 100, 1),       # vs ALLOWED
                "usage_pct":        round(tokens / max(input_budget, 1) * 100, 1),   # vs AVAILABLE (trigger-relevant)
                "input_budget_pct": round(tokens / max(input_budget, 1) * 100, 1),   # legacy alias
                "model_loaded":     launcher.loaded_model,
                "gpu_backend":      launcher.gpu_backend,
                "gpu_info":         launcher.gpu_info,
            })

        elif p == "/api/llm/use-prompt":
            name = qs.get("name", "").strip()
            if not name:
                self._json({"error": "name query param is required"}, 400); return
            if not _prompt_file_exists(name):
                self._json({"status": "error", "message": f"Prompt '{name}' not found"}, 404); return
            with _prompt_lock:
                _prompt_state["active_name"] = name
                _prompt_state["stopped"]     = False
            llm.system_prompt = build_system_prompt(llm.injected_context)
            print(f"[prompt] Activated '{name}'")
            self._json({
                "status":  "ok",
                "message": f"Prompt '{name}' is now active.",
                "active":  name,
            })

        elif p == "/api/llm/get-prompt":
            name = qs.get("name", "").strip()
            if not name:
                self._json({"error": "name query param is required"}, 400); return
            try:
                content = _read_prompt_file(name)
                self._json({"status": "ok", "name": name, "content": content})
            except FileNotFoundError as e:
                self._json({"status": "error", "message": str(e)}, 404)

        elif p == "/api/llm/get-promptn":
            self._json({
                "status":  "ok",
                "prompts": _list_prompt_names(),
                "active":  _prompt_state["active_name"],
                "stopped": _prompt_state["stopped"],
            })

        else:
            self._json({"error": "unknown endpoint"}, 404)

    # ── POST ──────────────────────────────────────────────────────
    def do_POST(self):
        pth = self._path_base()
        if pth == "/v1" or pth.startswith("/v1/"):
            self._openai_proxy("POST"); return

        try:
            body = self._body()
        except Exception as e:
            self._json({"error": f"bad JSON: {e}"}, 400); return

        p  = self._path_base()
        qs = self._qs()

        # ── Auth endpoints (no token required) ────────────────────
        if p == "/set_password":
            # Bootstrap only: usable solely while NO password is configured
            # (file missing / corrupted / empty `pass`). Once set, use
            # /change_password instead.
            payload, status = _auth_set_password(qs.get("password", ""))
            self._json(payload, status); return

        if p == "/login":
            payload, status = _auth_login(body)
            self._json(payload, status); return

        if p == "/change_password":
            payload, status = _auth_change_password(body)
            self._json(payload, status); return

        if p == "/login_required":
            payload, status = _auth_login_required(body)
            self._json(payload, status); return

        if p == "/server_settings":
            # Own auth: valid token OR body password (open while login_required=false).
            payload, status = _server_settings_post(body, self._auth_token(body))
            self._json(payload, status); return

        if p == "/openai_key":
            pw      = body.get("password", "")
            new_key = body.get("new_key", "")
            cfg = _load_pw_cfg()
            if not cfg.get("pass", ""):
                self._json({"status": "error", "message": "no_password_set"}, 400); return
            if _sha256(pw) != cfg["pass"]:
                self._json({"status": "error", "message": "wrong_password"}, 401); return
            if not new_key:
                self._json({"status": "error", "message": "new_key is required"}, 400); return
            cfg["openaikey"] = new_key
            _save_pw_cfg(cfg)
            self._json({"status": "ok", "message": "openai key updated"}); return

        # ── everything below requires a valid token (unless login disabled) ──
        if self._require_login(body):
            return

        # ── Load model ────────────────────────────────────────────
        if p == "/api/llm/load-model":
            path = body.get("path", "").strip()
            if not path:
                self._json({"error": "path is required"}, 400); return
            # llama-server may have been installed/built AFTER this server
            # started; re-detect so a fresh build is picked up without a restart.
            if not manager._template._binary:
                manager._template._binary = manager._template._find_binary()
            if not manager._template._binary:
                self._json({
                    "status":  "error",
                    "message": "llama.cpp (llama-server) binary not found — install or build it, then retry",
                }, 503); return
            if not os.path.isfile(path):
                self._json({"status": "error", "message": f"Model file not found: {path}"}, 404); return

            # A model may not be loaded more than once.
            norm = os.path.normcase(os.path.abspath(path))
            for inst in manager.instances.values():
                existing = inst.loaded_model or getattr(inst, "_pending_model", "")
                if existing and os.path.normcase(os.path.abspath(existing)) == norm:
                    self._json({"status": "error", "message": "model_loaded"}, 409); return

            # Validate placement up front so size/range errors reach the caller.
            flow, err = _compute_load_plan(path)
            if err:
                self._json({"status": "error", "message": err}, 400); return

            def _load_bg():
                ok, msg, idx = manager.load_new(path, force_flow=flow if flow is not None else {})
                print(f"[load-model] {msg}")

            threading.Thread(target=_load_bg, daemon=True).start()
            self._json({
                "status":  "loading",
                "message": f"Loading model: {os.path.basename(path)} — poll GET /api/llm/model-status",
            })
            return

        # ── Saved models: add ─────────────────────────────────────
        elif p == "/api/llm/saved-models":
            name = body.get("name", "").strip()
            path = body.get("path", "").strip()
            if not name:
                self._json({"status": "error", "message": "name is required"}, 400); return
            if not path:
                self._json({"status": "error", "message": "path is required"}, 400); return
            if not os.path.isfile(path):
                self._json({"status": "error", "message": f"Model file not found: {path}"}, 404); return
            if not path.lower().endswith(".gguf"):
                self._json({"status": "error", "message": f"Expected a .gguf file, got: {path}"}, 400); return
            try:
                data = _load_models()
                existing_paths = [m.get("path") for m in data.get("models", [])]
                if path in existing_paths:
                    self._json({"status": "error", "message": "Model with this path already saved"}, 409); return

                # supports_thinking: accept explicit true/false, or null for auto-detect later
                raw_st = body.get("supports_thinking", None)
                if raw_st is None:
                    supports_thinking = None           # will auto-detect on first load
                else:
                    supports_thinking = bool(raw_st)   # honour explicit user choice

                # supports_reasoning_effort: same convention (true/false/null)
                raw_re = body.get("supports_reasoning_effort", None)
                if raw_re is None:
                    supports_reasoning_effort = None   # will auto-detect on first load
                else:
                    supports_reasoning_effort = bool(raw_re)

                data.setdefault("models", []).append({
                    "name":              name,
                    "path":              path,
                    "supports_thinking": supports_thinking,
                    "supports_reasoning_effort": supports_reasoning_effort,
                })
                _save_models(data)
                self._json({
                    "status":  "ok",
                    "message": f"Model '{name}' saved.",
                    "models":  data["models"],
                })
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 500)
            return

        elif p == "/api/llm/sessions/clear":
            name = body.get("name", "").strip()
            if not name:
                self._json({"error": "name is required"}, 400);
                return
            try:
                data = load_session_file(name)
            except FileNotFoundError:
                self._json({"status": "error", "message": f"Session '{name}' not found"}, 404)
                return
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 500)
                return

            # Wipe history but preserve session metadata
            data["history"] = []
            path = _session_path(name)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self._json({"status": "error", "message": f"Could not write session file: {e}"}, 500)
                return

            # If this session is currently active, clear live history too
            if llm.current_session == name:
                llm.history.clear()
                llm._last_command = None
                llm._result_counter = 0
                print(f"[session] Live session '{name}' history cleared.")

            print(f"[session] Session '{name}' history cleared.")
            self._json({
                "status": "ok",
                "message": f"Session '{name}' history cleared.",
                "session_name": name,
            })

        # ── Saved models: set thinking / reasoning-effort flags ────
        elif p == "/api/llm/saved-models/set-thinking":
            """
            Manually override capability flags for a saved model.
            Body: {"path": "...",
                   "supports_thinking": true/false/null,            (optional)
                   "supports_reasoning_effort": true/false/null}    (optional)
            At least one flag must be present. Each accepts true/false/null
            (null = re-auto-detect on next load).
            """
            path = body.get("path", "").strip()

            if not path:
                self._json({"status": "error", "message": "path is required"}, 400); return

            FLAG_KEYS = ("supports_thinking", "supports_reasoning_effort")
            present = {k: body[k] for k in FLAG_KEYS if k in body}
            if not present:
                self._json({
                    "status": "error",
                    "message": "at least one of supports_thinking / supports_reasoning_effort is required (true/false/null)"
                }, 400)
                return
            for k, v in present.items():
                if v not in (True, False, None):
                    self._json({
                        "status": "error",
                        "message": f"{k} must be true, false, or null"
                    }, 400)
                    return

            try:
                data  = _load_models()
                found = False
                for model in data.get("models", []):
                    if model.get("path") == path:
                        model.update(present)
                        found = True
                        break
                if not found:
                    self._json({"status": "error", "message": f"Model not found in saved list: {path}"}, 404); return
                _save_models(data)

                # If this model is the active chat model, update live settings immediately
                if manager.active().loaded_model == path:
                    llm.settings.update(present)
                    print(f"[thinking] Live model updated: {present}")

                print(f"[thinking] Saved to saved_models.json: {path} -> {present}")
                self._json({
                    "status":  "ok",
                    "message": f"Updated {', '.join(present)} for '{os.path.basename(path)}'.",
                    "path":    path,
                    **present,
                })
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 500)
            return

        elif p == "/api/llm/stop":
            llm.request_stop()
            self._json({"status": "stop_success", "message": "Stop signal sent."})
            return

        elif p == "/api/llm/saved-models/delete":
            path = body.get("path", "").strip()
            if not path:
                self._json({"status": "error", "message": "path is required"}, 400)
                return
            try:
                data = _load_models()
                before = len(data.get("models", []))
                data["models"] = [m for m in data.get("models", []) if m.get("path") != path]
                after = len(data["models"])
                if before == after:
                    self._json({"status": "error", "message": "Model not found in saved list"}, 404)
                    return
                _save_models(data)
                self._json({"status": "ok", "message": "Model removed.", "models": data["models"]})
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 500)
            return

        elif p == "/api/llm/saved-models/toggle-autosave":
            try:
                data = _load_models()
                data["autoSave"] = not data.get("autoSave", False)
                _save_models(data)
                self._json({
                    "status":   "ok",
                    "autoSave": data["autoSave"],
                    "message":  f"Auto-save {'enabled' if data['autoSave'] else 'disabled'}"
                })
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 500)
            return

        elif p == "/api/llm/raw":
            cmd = body.get("command", "").strip()
            if not cmd:
                self._json({"error": "command is empty"}, 400); return
            r = process_command(cmd, conn=self.wfile)
            self._json({"status": "ok" if not r.startswith("err:") else "error", "message": r})

        elif p == "/api/llm/reload":
            llm.reload_prompt()
            self._json({"status": "ok", "message": "reloaded"})

        elif p == "/api/llm/settings":
            if not body:
                self._json({"error": "no settings provided"}, 400); return
            ok, msg = llm.update_settings(body)
            self._json({"status": "ok" if ok else "error", "message": msg,
                        **({"current": llm.get_settings()} if ok else {})},
                       200 if ok else 400)

        elif p == "/api/llm/settings/reset":
            self._json({"status": "ok", "message": llm.reset_settings(), "current": llm.get_settings()})

        elif p == "/api/llm/history/clear":
            llm.history.clear()
            llm._last_command   = None
            llm._result_counter = 0
            self._json({"status": "ok", "message": "history cleared"})

        elif p == "/api/llm/delete_msg":
            raw_id = body.get("id")
            if raw_id is None:
                self._json({"error": "id is required"}, 400); return
            try:
                msg_id = int(raw_id)
            except (TypeError, ValueError):
                self._json({"error": "id must be an integer"}, 400); return
            ok, msg = llm.delete_msg(msg_id)
            self._json(
                {"status": "ok" if ok else "error", "message": msg,
                 **({"remaining": len(llm.history)} if ok else {})},
                200 if ok else 400
            )

        elif p == "/api/llm/edit_msg":
            raw_id = body.get("id")
            content = body.get("content")
            if raw_id is None:
                self._json({"error": "id is required"}, 400); return
            if content is None:
                self._json({"error": "content is required"}, 400); return
            if not isinstance(content, str):
                self._json({"error": "content must be a string"}, 400); return
            try:
                msg_id = int(raw_id)
            except (TypeError, ValueError):
                self._json({"error": "id must be an integer"}, 400); return
            ok, msg = llm.edit_msg(msg_id, content)
            self._json(
                {"status": "ok" if ok else "error", "message": msg},
                200 if ok else 400
            )
        elif p == "/api/llm/cache-prompt":
            val = body.get("enabled")
            if val is None:
                self._json({"error": "'enabled' is required (true/false)"}, 400);
                return
            llm.settings["cache_prompt"] = bool(val)
            self._json({"status": "ok", "cache_prompt": llm.settings["cache_prompt"]})

        elif p == "/api/llm/sessions/save":
            name = body.get("name", "").strip()
            if not name:
                self._json({"error": "name is required"}, 400); return
            try:
                self._json({"status": "ok", "message": llm.session_save(name)})
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 400)

        elif p == "/api/llm/sessions/load":
            name = body.get("name", "").strip()
            if not name:
                self._json({"error": "name is required"}, 400); return
            reload_active = bool(body.get("reload_active") or body.get("checkpoint"))
            try:
                result = llm.session_load(name, reload_active=reload_active)
                self._json({"status": "ok", **result})
            except FileNotFoundError as e:
                self._json({"status": "error", "message": str(e)}, 404)
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 400)

        elif p == "/api/llm/sessions/new":
            msgs    = []
            save_as = body.get("save_current_as", "").strip()
            try:
                if save_as:
                    msgs.append(llm.session_save(save_as))
                msgs.append(llm.session_new())
                self._json({"status": "ok", "message": " | ".join(msgs)})
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 400)

        elif p == "/api/llm/sessions/delete":
            name = body.get("name", "").strip()
            if not name:
                self._json({"error": "name is required"}, 400); return
            try:
                self._json({"status": "ok", "message": llm.session_delete(name)})
            except FileNotFoundError as e:
                self._json({"status": "error", "message": str(e)}, 404)
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 400)

        elif p == "/api/llm/save-thinking":
            raw_save = body.get("save")
            if raw_save is None:
                self._json({"error": "'save' field is required (true or false)"}, 400); return

            save_flag    = bool(raw_save)
            session_name = body.get("session_name", "").strip()

            if not session_name:
                self._json({"error": "'session_name' is required"}, 400); return

            try:
                data = load_session_file(session_name)
            except FileNotFoundError:
                self._json({"status": "error", "message": f"Session '{session_name}' not found"}, 404)
                return

            data["think"] = save_flag
            path = _session_path(session_name)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self._json({"status": "error", "message": f"Could not write session file: {e}"}, 500)
                return

            if llm.current_session == session_name:
                llm.save_thinking = save_flag
                print(f"[think] Live session updated: save_thinking={save_flag}")

            print(f"[think] Session '{session_name}' think={save_flag}")
            self._json({
                "status":       "ok",
                "message":      f"Session '{session_name}' think saving set to {save_flag}.",
                "session_name": session_name,
                "think":        save_flag,
            })

        # ── Prompt management ─────────────────────────────────────

        elif p == "/api/llm/stop-prompt":
            with _prompt_lock:
                _prompt_state["stopped"]     = True
                _prompt_state["active_name"] = None
            llm.system_prompt = ""
            print("[prompt] System prompt stopped.")
            self._json({"status": "ok", "message": "System prompt disabled."})

        elif p == "/api/llm/reset-prompt":
            with _prompt_lock:
                _prompt_state["stopped"] = False
                _prompt_state["active_name"] = None
            llm.system_prompt = build_system_prompt(llm.injected_context)
            print("[prompt] Reset to default CORE_PROMPT.")
            self._json({"status": "ok", "message": "Reverted to default system prompt."})

        elif p == "/api/llm/new-prompt":
            name     = body.get("name", "").strip()
            text     = body.get("prompt", "").strip()
            use_apis = bool(body.get("use_apis", False))
            if not name:
                self._json({"error": "name is required"}, 400); return
            if not text:
                self._json({"error": "prompt is required"}, 400); return
            try:
                content = text
                if use_apis:
                    content = text + "\n\n{{USE_APIS}}"
                _write_prompt_file(name, content)
                print(f"[prompt] Created '{name}' (use_apis={use_apis})")
                self._json({
                    "status":   "ok",
                    "message":  f"Prompt '{name}' saved.",
                    "name":     name,
                    "use_apis": use_apis,
                })
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 400)

        elif p == "/api/llm/delete-prompt":
            name = body.get("name", "").strip()
            if not name:
                self._json({"error": "name is required"}, 400); return
            try:
                _delete_prompt_file(name)
                with _prompt_lock:
                    bare = name[:-4] if name.endswith(".txt") else name
                    if _prompt_state["active_name"] == bare or _prompt_state["active_name"] == name:
                        _prompt_state["active_name"] = None
                llm.system_prompt = build_system_prompt(llm.injected_context)
                print(f"[prompt] Deleted '{name}'")
                self._json({"status": "ok", "message": f"Prompt '{name}' deleted."})
            except FileNotFoundError as e:
                self._json({"status": "error", "message": str(e)}, 404)
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 400)

        elif p == "/api/llm/edit-prompt":
            name     = body.get("name", "").strip()
            edprompt = body.get("edprompt", "")
            if not name:
                self._json({"error": "name is required"}, 400); return
            if edprompt is None:
                self._json({"error": "edprompt is required"}, 400); return
            if not _prompt_file_exists(name):
                self._json({"status": "error", "message": f"Prompt '{name}' not found"}, 404); return
            try:
                _write_prompt_file(name, edprompt)
                with _prompt_lock:
                    bare      = name[:-4] if name.endswith(".txt") else name
                    is_active = (_prompt_state["active_name"] in (name, bare))
                if is_active:
                    llm.system_prompt = build_system_prompt(llm.injected_context)
                print(f"[prompt] Edited '{name}'")
                self._json({"status": "ok", "message": f"Prompt '{name}' updated."})
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 400)

        # ── Developer endpoints ───────────────────────────────────
        elif p == "/api/dev/config":
            allowed = {
                "ctx_size", "batch_size", "gpu_layers", "cpu_percent",
                "max_history", "token_warn_at",
                "ctx_safety_margin", "auto_trim",
            }
            errors, validated = [], {}
            for k, v in body.items():
                if k not in allowed:
                    errors.append(f"Unknown key: {k}"); continue
                if k == "gpu_layers":
                    validated[k] = None if v is None else int(v)
                elif k == "cpu_percent":
                    try:
                        pct = float(v)
                        if not 0 <= pct <= 100:
                            errors.append("cpu_percent must be 0–100"); continue
                        validated[k] = round(pct)
                    except Exception:
                        errors.append("cpu_percent must be a number")
                elif k == "ctx_safety_margin":
                    try:
                        margin = float(v)
                        if not 0.0 <= margin <= 0.5:
                            errors.append("ctx_safety_margin must be 0.0–0.5"); continue
                        validated[k] = margin
                    except Exception:
                        errors.append("ctx_safety_margin must be a float")
                elif k == "auto_trim":
                    validated[k] = bool(v)
                else:
                    try:
                        validated[k] = int(v)
                    except Exception:
                        errors.append(f"{k} must be int")
            if errors:
                self._json({"status": "error", "message": "; ".join(errors)}, 400); return
            with _dev_cfg_lock:
                _dev_cfg.update(validated)
            self._json({"status": "ok", "message": "Dev config updated.", "config": _get_dev_cfg()})

        elif p == "/api/dev/eject":
            if manager.active_index is None:
                self._json({"status": "error", "message": "No model loaded"}, 400); return
            ok, name = manager.eject(manager.active_index)
            if not ok:
                self._json({"status": "error", "message": name}, 400); return
            self._json({"status": "ok", "message": f"Model ejected: {name}"})

        elif p == "/eject_model":
            idx = self._index_arg(qs, body)
            if idx is None:
                self._json({"status": "error", "message": "index is required (e.g. /eject_model?index=1)"}, 400); return
            ok, name = manager.eject(idx)
            if not ok:
                self._json({"status": "error", "message": name}, 404); return
            self._json({"status": "ok", "message": f"Ejected model at index {idx}: {name}", "index": idx})

        elif p == "/active_model":
            idx = self._index_arg(qs, body)
            if idx is None:
                self._json({"status": "error", "message": "index is required (e.g. /active_model?index=1)"}, 400); return
            ok, name = manager.set_active(idx)
            if not ok:
                self._json({"status": "error", "message": name}, 404); return
            self._json({"status": "ok", "message": f"Active chat model switched to index {idx}: {name}", "active_index": idx})

        elif p == "/default_gpu":
            if self._require_multi_gpu(): return
            raw = body.get("default_gpu")
            if raw is None:
                self._json({"status": "error", "message": "default_gpu is required"}, 400); return
            try:
                val = int(raw)
            except (TypeError, ValueError):
                self._json({"status": "error", "message": "default_gpu must be an integer"}, 400); return
            available = {g["index"] for g in detect_all_gpus()}
            if available and val not in available:
                self._json({"status": "error", "message": f"GPU index {val} not found. Available: {sorted(available)}"}, 400); return
            settings = _load_multi_gpu_settings()
            settings["default_gpu"] = val
            _save_multi_gpu_settings(settings)
            self._json({"status": "ok", "message": f"default_gpu set to {val}.", "default_gpu": val})

        elif p == "/multi_gpu_settings":
            if self._require_multi_gpu(): return
            settings = _load_multi_gpu_settings()
            if "enable_flow" in body:
                settings["enable_flow"] = bool(body["enable_flow"])
            if "default_gpu" in body:
                try:
                    val = int(body["default_gpu"])
                except (TypeError, ValueError):
                    self._json({"status": "error", "message": "default_gpu must be an integer"}, 400); return
                available = {g["index"] for g in detect_all_gpus()}
                if available and val not in available:
                    self._json({"status": "error", "message": f"GPU index {val} not found. Available: {sorted(available)}"}, 400); return
                settings["default_gpu"] = val
            _save_multi_gpu_settings(settings)
            self._json({"status": "ok", "message": "multi_gpu_settings updated.", **settings})

        elif p == "/api/dev/trim_history":
            keep   = int(body.get("keep", 0))
            before = len(llm.history)
            if keep > 0 and keep < before:
                llm.history = llm.history[-keep:]
            elif keep == 0:
                llm.history = []
            after = len(llm.history)
            self._json({"status": "ok", "message": f"Trimmed {before - after} messages.", "remaining": after})

        # ── GPU Flow endpoints ────────────────────────────────────
        elif p == "/gpus/flow":
            parsed = {}
            for k, v in body.items():
                try:
                    idx = int(k)
                    pct = int(v)
                except (ValueError, TypeError):
                    self._json({"status": "error", "message": f"Invalid key/value: {k}={v}"}, 400); return
                # 0 = exclude this GPU from the split entirely (e.g. it's busy with
                # another model); 100 = dedicate the whole model to this one GPU.
                # Anything outside 0–100 is meaningless for a tensor-split ratio.
                if not (0 <= pct <= 100):
                    self._json({"status": "error", "message": f"Percent for GPU {idx} must be 0–100, got {pct}"}, 400); return
                if pct > 0:
                    parsed[idx] = pct   # drop 0% GPUs so they're left free for other loads

            if not parsed:
                self._json({"status": "error", "message": "At least one GPU must have a percent > 0"}, 400); return

            total = sum(parsed.values())
            if total != 100:
                self._json({"status": "error", "message": f"Percents of the GPUs you use must sum to 100, got {total}"}, 400); return

            available = {g["index"] for g in detect_all_gpus()}
            missing = [i for i in parsed if i not in available]
            if missing:
                self._json({"status": "error", "message": f"GPU indices not found: {missing}"}, 400); return

            # Validate all GPUs in flow have same vendor
            try:
                _validate_gpu_flow_vendor(parsed)
            except ValueError as e:
                self._json({"status": "error", "message": str(e)}, 400); return

            with _gpu_flow_lock:
                _gpu_flow.clear()
                _gpu_flow.update(parsed)

            was_loaded = launcher.is_ready()
            if was_loaded:
                threading.Thread(target=_reload_current_model_bg, kwargs={"force_flow": parsed}, daemon=True).start()
                self._json({
                    "status":  "reloading",
                    "message": "Flow saved. Active model is reloading with new GPU split.",
                    "flow":    parsed,
                })
            else:
                self._json({
                    "status":  "ok",
                    "message": "Flow saved. Will apply on next model load.",
                    "flow":    parsed,
                })

        elif p == "/gpus/auto_flow":
            model_path = body.get("model_path", "").strip() or launcher.loaded_model
            if not model_path:
                self._json({"status": "error", "message": "No model path provided and no model loaded"}, 400); return
            if not os.path.isfile(model_path):
                self._json({"status": "error", "message": f"Model file not found: {model_path}"}, 404); return

            flow = _calc_auto_flow(model_path)
            if not flow:
                gpus = detect_all_gpus()
                if len(gpus) < 2:
                    self._json({"status": "error", "message": f"auto_flow requires 2+ GPUs, found {len(gpus)}"}, 400)
                else:
                    self._json({"status": "error", "message": "Failed to calculate auto flow"}, 500)
                return

            with _gpu_flow_lock:
                _gpu_flow.clear()
                _gpu_flow.update(flow)

            gpus    = detect_all_gpus()
            gpu_map = {g["index"]: g["name"] for g in gpus}
            flow_detail = {
                str(idx): [gpu_map.get(idx, f"GPU {idx}"), pct]
                for idx, pct in sorted(flow.items())
            }

            was_loaded = launcher.is_ready()
            if was_loaded:
                threading.Thread(target=_reload_current_model_bg, kwargs={"force_flow": flow}, daemon=True).start()
                self._json({
                    "status":  "reloading",
                    "message": "Auto-flow applied. Active model is reloading.",
                    "flow":    flow_detail,
                })
            else:
                self._json({
                    "status":  "ok",
                    "message": "Auto-flow saved. Will apply on next model load.",
                    "flow":    flow_detail,
                })

        elif p == "/model/gpus/drive":
            raw_idx    = body.get("index_gpu")
            model_name = body.get("model_name", "").strip()

            if raw_idx is None:
                self._json({"status": "error", "message": "index_gpu is required"}, 400); return
            if not model_name:
                self._json({"status": "error", "message": "model_name is required"}, 400); return

            try:
                gpu_idx = int(raw_idx)
            except (TypeError, ValueError):
                self._json({"status": "error", "message": "index_gpu must be an integer"}, 400); return

            available = {g["index"] for g in detect_all_gpus()}
            if available and gpu_idx not in available:
                self._json({"status": "error", "message": f"GPU index {gpu_idx} not found. Available: {sorted(available)}"}, 400); return

            try:
                data   = _load_models()
                model  = next((m for m in data.get("models", []) if m.get("name") == model_name), None)
            except Exception as e:
                self._json({"status": "error", "message": str(e)}, 500); return

            if not model:
                self._json({"status": "error", "message": f"Model '{model_name}' not found in saved models"}, 404); return

            model_path = model.get("path", "")
            if not os.path.isfile(model_path):
                self._json({"status": "error", "message": f"Model file not found: {model_path}"}, 404); return

            def _drive_bg():
                ok, msg, idx = manager.load_new(model_path, force_flow={gpu_idx: 100})
                print(f"[drive] {msg}")

            threading.Thread(target=_drive_bg, daemon=True).start()
            self._json({
                "status":  "loading",
                "message": f"Driving '{model_name}' to GPU {gpu_idx}. Poll GET /api/llm/model-status.",
                "gpu":     gpu_idx,
                "model":   model_name,
            })

        elif p == "/model/restart":
            inst = manager.active()
            if not inst.loaded_model:
                self._json({"status": "error", "message": "No model is currently loaded"}, 400); return

            model_path = inst.loaded_model
            threading.Thread(target=_reload_current_model_bg,
                             kwargs={"force_flow": dict(inst.flow) if inst.flow else None},
                             daemon=True).start()
            self._json({
                "status":  "restarting",
                "message": f"Restarting '{os.path.basename(model_path)}'. Poll GET /api/llm/model-status.",
                "model":   os.path.basename(model_path),
            })

        else:
            self._json({"error": "unknown endpoint"}, 404)


def llm_http_server():
    # ThreadingHTTPServer, not plain HTTPServer: the plain server is single-
    # threaded, so one slow request (a streaming /v1 chat completion, a model
    # load taking minutes, a slow /tokenize) blocks the whole listener — every
    # other call, including /login and /server_settings, queues behind it and
    # appears to "hang". One thread per request keeps auth/control responsive
    # while a generation runs. daemon_threads so workers don't block shutdown.
    srv = ThreadingHTTPServer((BIND_HOST, LLM_HTTP_PORT), LLMHTTPHandler)
    srv.daemon_threads = True

    print(f"[HTTP] Listening on {BIND_HOST}:{LLM_HTTP_PORT}")
    srv.serve_forever()


# ══════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════
#  PROMPT API  (merged from promptapi.py — system_prompt.json store)
# ══════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════

# ── Prompt file I/O ───────────────────────────────────────────────
_prompt_file_lock = threading.Lock()

def load_prompt() -> dict:
    if not os.path.exists(PROMPT_PATH):
        return {}
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_prompt(data: dict):
    with _prompt_file_lock:
        with open(PROMPT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

# ══════════════════════════════════════════════════════════════════
#  PROMPT TCP SOCKET SERVER
# ══════════════════════════════════════════════════════════════════
def prompt_handle_conn(conn):
    try:
        raw = conn.recv(262144).decode(errors="ignore").strip()

        if raw == "__get_prompt__":
            data = load_prompt()
            payload = json.dumps(data, indent=2, ensure_ascii=False)
            conn.sendall(payload.encode("utf-8"))
            print(f"[TCP GET] Returned {len(data)} sections")
            return

        if raw.startswith("__set_prompt__:"):
            raw_json = raw[len("__set_prompt__:"):].strip()
            try:
                parsed = json.loads(raw_json)
                save_prompt(parsed)
                conn.sendall(b"ok: system_prompt.json saved")
                print(f"[TCP SET] {len(parsed)} sections")
            except Exception as e:
                conn.sendall(f"err: {e}".encode())
            return

        if raw.startswith("__add_prompt__:"):
            rest = raw[len("__add_prompt__:"):].strip()
            sep = rest.find(":")
            if sep == -1:
                conn.sendall(b"err: missing key separator"); return
            key     = rest[:sep].strip()
            sec_raw = rest[sep+1:].strip()
            try:
                section = json.loads(sec_raw)
                data = load_prompt()
                data[key] = section
                save_prompt(data)
                conn.sendall(f"ok: section '{key}' saved".encode())
                print(f"[TCP ADD] '{key}'")
            except Exception as e:
                conn.sendall(f"err: {e}".encode())
            return

        if raw.startswith("__del_prompt__:"):
            key = raw[len("__del_prompt__:"):].strip()
            data = load_prompt()
            if key in data:
                del data[key]
                save_prompt(data)
                conn.sendall(f"ok: section '{key}' deleted".encode())
                print(f"[TCP DEL] '{key}'")
            else:
                conn.sendall(f"err: key '{key}' not found".encode())
            return

        conn.sendall(b"err: unknown command")

    except Exception as e:
        print(f"[TCP ERROR] {e}")
        try:
            conn.sendall(f"err: {e}".encode())
        except:
            pass
    finally:
        conn.close()


def prompt_tcp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND_HOST, PROMPT_TCP_PORT))
    srv.listen(5)
    print(f"[TCP]  PromptAPI socket listening on {BIND_HOST}:{PROMPT_TCP_PORT}")
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=prompt_handle_conn, args=(conn,), daemon=True).start()




class PromptHTTPHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[HTTP] {fmt % args}")

    # ── CORS ──────────────────────────────────────────────────
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    # ── Token check (unified — same store/semantics as the LLM server) ──
    def _check_auth(self, body: dict = None) -> bool:
        """
        يرجع True لو الطلب مصرح له.
        يقرأ الـ token من:
          1. Header  X-Auth-Token
          2. Body    {"token": "..."}
          3. Query   ?token=...
        """
        if not _is_login_required():
            return True

        tok = (
            self.headers.get("X-Auth-Token") or
            (body or {}).get("token") or
            self._query_param("token")
        )
        return bool(tok and _token_valid(tok))

    def _query_param(self, key: str):
        """استخرج query string parameter بسيط."""
        path = self.path
        if "?" not in path:
            return None
        qs = path.split("?", 1)[1]
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k == key:
                    return v
        return None

    def _unauthorized(self):
        self._json_response({"status": "error", "message": "unauthorized"}, 401)

    # ══════════════════════════════════════════════════════════
    #  GET
    # ══════════════════════════════════════════════════════════
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        # Public — no token required (a supplied token is simply ignored).
        if path == "/server_settings":
            payload, status = _server_settings_get()
            self._json_response(payload, status)
        elif path in ("/api/prompt", "/api/prompt/get"):
            if not self._check_auth():
                self._unauthorized(); return
            data = load_prompt()
            self._json_response({"data": data})
        else:
            self._json_response({"error": "not found"}, 404)

    # ══════════════════════════════════════════════════════════
    #  POST
    # ══════════════════════════════════════════════════════════
    def do_POST(self):
        try:
            body = self._read_body()
        except Exception as e:
            self._json_response({"error": f"bad JSON: {e}"}, 400)
            return

        path = self.path.split("?")[0].rstrip("/")



        # ── PUBLIC: auth endpoints (shared logic with the LLM server) ──
        if path in ("/api/login", "/login"):
            payload, status = _auth_login(body)
            if status == 200:
                print(f"[login] token issued")
            self._json_response(payload, status)
            return

        if path == "/set_password":
            payload, status = _auth_set_password(self._query_param("password") or "")
            self._json_response(payload, status)
            return

        if path == "/change_password":
            payload, status = _auth_change_password(body)
            self._json_response(payload, status)
            return

        if path == "/login_required":
            payload, status = _auth_login_required(body)
            self._json_response(payload, status)
            return

        if path == "/server_settings":
            # Own auth: valid token OR body password (open while login_required=false).
            tok = (
                self.headers.get("X-Auth-Token") or
                (body or {}).get("token") or
                self._query_param("token")
            )
            payload, status = _server_settings_post(body, tok)
            self._json_response(payload, status)
            return

        # ── كل ما يلي محمي بالـ token ─────────────────────────
        if not self._check_auth(body):
            self._unauthorized(); return

        # /api/prompt/add
        if path == "/api/prompt/add":
            key     = body.get("key", "").strip()
            section = body.get("section", {})
            if not key:
                self._json_response({"error": "key required"}, 400); return
            data = load_prompt()
            data[key] = section
            save_prompt(data)
            print(f"[HTTP ADD] '{key}'")
            self._json_response({"status": "ok", "key": key})

        # /api/prompt/del
        elif path == "/api/prompt/del":
            key = body.get("key", "").strip()
            if not key:
                self._json_response({"error": "key required"}, 400); return
            data = load_prompt()
            removed = key in data
            data.pop(key, None)
            save_prompt(data)
            print(f"[HTTP DEL] '{key}' removed={removed}")
            self._json_response({"status": "ok", "removed": removed})

        elif path == "/api/prompt/block":
            key = body.get("key", "").strip()
            if not key:
                self._json_response({"error": "key required"}, 400);
                return
            data = load_prompt()
            if key not in data:
                self._json_response({"error": f"key '{key}' not found"}, 404);
                return
            if "block" in body:
                blocked = bool(body["block"])
            else:
                blocked = not data[key].get("block", False)
            data[key]["block"] = blocked
            save_prompt(data)
            print(f"[HTTP BLOCK] '{key}' block={blocked}")
            self._json_response({"status": "ok", "key": key, "block": blocked})

        # /api/prompt/blocks
        elif path == "/api/prompt/blocks":
            data = load_prompt()
            modified = False
            for key, value in data.items():
                if "block" not in value:
                    value["block"] = False
                    modified = True
            if modified:
                save_prompt(data)
            blocks = {key: value.get("block", False) for key, value in data.items()}
            self._json_response({"status": "ok", "blocks": blocks})

        # /api/prompt/set
        elif path == "/api/prompt/set":
            inner = body.get("data", body)
            inner.pop("token", None)
            if not isinstance(inner, dict):
                self._json_response({"error": "expected object"}, 400); return
            save_prompt(inner)
            print(f"[HTTP SET] {len(inner)} sections")
            self._json_response({"status": "ok", "count": len(inner)})

        else:
            self._json_response({"error": "unknown endpoint"}, 404)


def prompt_http_server():
    srv = ThreadingHTTPServer((BIND_HOST, PROMPT_HTTP_PORT), PromptHTTPHandler)
    srv.daemon_threads = True
    print(f"[HTTP] PromptAPI HTTP  listening on {BIND_HOST}:{PROMPT_HTTP_PORT}")
    srv.serve_forever()


# ══════════════════════════════════════════════════════════════════
#  ENTRY
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    _pw_cfg = _load_pw_cfg()
    _ls = "ENABLED"  if _pw_cfg.get("login_required", True) else "DISABLED"
    _hp = "SET"      if _pw_cfg.get("pass")                 else "NOT SET !"
    print(f"""
+======================================================+
|     Merged Server  (LLM llama.cpp + PromptAPI)       |
|         single process -- four listeners             |
|  LLM    TCP  socket -> port {LLM_TCP_PORT}  (open, no password)
|  LLM    HTTP REST   -> port {LLM_HTTP_PORT}  (token-gated)
|  Prompt TCP  socket -> port {PROMPT_TCP_PORT}  (open, no password)
|  Prompt HTTP REST   -> port {PROMPT_HTTP_PORT}  (token-gated)
|  Login: {_ls}   Password: {_hp}
|  Ports: llm-server_settings.json  /  /server_settings
+======================================================+""")
    print("""
+======================================================+
|  Endpoint catalog                                    |
+------------------------------------------------------+
|  POST /set_password?password=    bootstrap (no pw)   |
|  POST /login                     password -> token   |
|  POST /change_password           old + new password  |
|  POST /login_required            toggle auth on/off  |
|  POST /openai_key                set /v1 bearer key  |
|  GET  /openai_key?token=         get /v1 bearer key  |
+------------------------------------------------------+
|  POST /api/llm/load-model        load .gguf model    |
|  GET  /api/llm/model-status      model load status   |
+------------------------------------------------------+
|  GET  /api/llm/saved-models      list saved models   |
|  POST /api/llm/saved-models      add new model       |
|  POST /api/llm/saved-models/     set thinking flag   |
|       set-thinking                                   |
+------------------------------------------------------+
|  GET  /api/llm/prompt            view system prompt  |
|  GET  /api/llm/settings          view settings       |
|  POST /api/llm/settings          update settings     |
|  POST /api/llm/settings/reset    reset to defaults   |
|  GET  /api/llm/history           view history        |
|  POST /api/llm/history/clear     clear history       |
|  POST /api/llm/delete_msg        delete msg by id    |
|  POST /api/llm/edit_msg          edit msg by id      |
|  POST /api/llm/stop              stop generation     |
|  GET  /api/llm/sessions          list sessions       |
|  POST /api/llm/sessions/save     save session        |
|  POST /api/llm/sessions/load     load session        |
|  POST /api/llm/sessions/new      new session         |
|  POST /api/llm/sessions/delete   delete session      |
|  POST /api/llm/save-thinking     toggle think saving |
+------------------------------------------------------+
|  POST /api/llm/stop-prompt       disable sys prompt  |
|  POST /api/llm/new-prompt        create prompt       |
|  POST /api/llm/delete-prompt     delete prompt       |
|  POST /api/llm/edit-prompt       edit prompt         |
|  GET  /api/llm/use-prompt?name=  activate prompt     |
|  GET  /api/llm/get-prompt?name=  get prompt content  |
|  GET  /api/llm/get-promptn       list prompt names   |
+------------------------------------------------------+
|  GET  /api/dev/config            developer config    |
|  POST /api/dev/config            update dev config   |
|  POST /api/dev/eject             eject loaded model  |
|  POST /api/dev/trim_history      trim history        |
+------------------------------------------------------+
|  ── Multi-GPU System ──                              |
|  GET  /gpus/status               list GPUs + VRAM    |
|  GET  /gpus/flow                 current GPU split   |
|  POST /gpus/flow                 set GPU split %     |
|  POST /gpus/auto_flow            auto split by VRAM  |
|  POST /model/gpus/drive          drive model to GPU  |
|  POST /model/restart             eject + reload      |
+------------------------------------------------------+
|  ── Multi-Model (load many at once) ──               |
|  GET  /loaded_model              list loaded models  |
|  GET  /active_model              active chat model    |
|  POST /active_model?index=N      switch chat model   |
|  POST /eject_model?index=N       eject model by idx  |
|  GET  /default_gpu               get default GPU *    |
|  POST /default_gpu               set default GPU *    |
|  GET  /multi_gpu_settings        get load settings * |
|  POST /multi_gpu_settings        set load settings * |
|  (* = requires 2+ GPUs)                              |
+------------------------------------------------------+
|  ── OpenAI-compatible API (/v1) ──                   |
|  GET  /v1/models                 list models         |
|  GET  /v1/models/{id}            one model           |
|  POST /v1/chat/completions       chat (stream too)   |
|  POST /v1/completions            text completion     |
|  POST /v1/embeddings             embeddings          |
+------------------------------------------------------+
|  ── PromptAPI (prompt-api HTTP port) ──              |
|  POST /api/login                 password -> token   |
|  GET  /api/prompt                get system_prompt.json     |
|  POST /api/prompt/add            add section         |
|  POST /api/prompt/del            delete section      |
|  POST /api/prompt/block          toggle block flag   |
|  POST /api/prompt/blocks         list block flags    |
|  POST /api/prompt/set            replace system_prompt.json |
|  (auth endpoints above also served on this port)     |
+------------------------------------------------------+
|  ── Server settings (BOTH HTTP ports) ──             |
|  GET  /server_settings           listener ports      |
|  POST /server_settings           update ports        |
+======================================================+
""")
    threading.Thread(target=llm_tcp_server,     daemon=True).start()
    threading.Thread(target=prompt_tcp_server,  daemon=True).start()
    threading.Thread(target=prompt_http_server, daemon=True).start()
    llm_http_server()

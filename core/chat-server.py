"""
server.py
─────────
Merged single-file server that runs BOTH services in one process:

  * chat-server (Flask app: chat_app)
  * prompt-mgr  (Flask app: prompt_app)

The listening ports are read from chat_ports.json, e.g.:

    {
      "chat-server": 8900,
      "prompt-mgr": 8843
    }

The backend config (llm_server / systemapi / promptapi) lives in the single
merged settings file backend_servers.json (config.json has been merged into it).

The chat app additionally exposes:
  * GET  /server-settings  (public)  — ALL settings: the ports the services are
    CURRENTLY listening on (the in-memory snapshot, not the on-disk file) plus
    the merged backend config (llm_server / systemapi / promptapi).
  * POST /server-settings  (login)   — persists new ports to chat_ports.json
    and restarts the process so the services rebind. The in-memory snapshot is
    left untouched until the restart, so a GET issued before the restart still
    reports the old (live) ports.

Run:
    pip install flask requests
    python server.py
"""

# ==========================================================================
#  SECTION 1 — CHAT-SERVER (Flask app: chat_app)
# ==========================================================================

"""
chat_server.py
  - /api/llm/load-model and /api/llm/model-status
  - STOP fixed: sends HTTP stop to llm_server + sets _generation_done_evt immediately
  - sessions/load: history returned in HTTP response, no separate TCP push needed
  - Developer panel proxy routes: /api/dev/config, /api/dev/eject, /api/dev/trim_history
  - Mobile detection: redirects to chatp.html on mobile devices
  - Saved models proxy: /api/saved-models (GET + POST)
  - /api/saved-models/delete  — real delete from saved_models.json
  - /api/saved-models/toggle-autosave — toggle autoSave flag
  - detect_commands: auto-detects and executes commands from AI response
    if no SSE clients are connected (browser closed/offline)
"""

import json
import os
import socket
import threading
import time
import queue
import logging
import urllib.request
import urllib.error
import hashlib
import secrets
import string
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from datetime import datetime
import re as _re


LLM_TCP_HOST   = "127.0.0.1"
LLM_TCP_PORT   = 8111
LLM_HTTP_HOST  = "127.0.0.1"
LLM_HTTP_PORT  = 8112
LLM_SCHEME     = "http"
SYSTEMAPI_HOST = "127.0.0.1"
SYSTEMAPI_PORT = 8300
# Prompt API — shares the llm-server host; the ports are discovered from the
# llm-server's public GET /server_settings when we connect (see
# _discover_and_save_promptapi). These are only the fallback defaults.
PROMPTAPI_HOST = "127.0.0.1"
PROMPTAPI_TCP  = 8202
PROMPTAPI_HTTP = 8203
LISTEN_PORT    = 8801
BRIDGE_PORT    = 8900
COMMANDS_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "commands.json")
PASSWORD_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.json")
SERVER_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_servers.json")
HTML_DIR = os.path.join("frontend")

chat_app = Flask(__name__)

# Drop Werkzeug's access-log lines for the high-frequency polling endpoints (and
# all OPTIONS preflights). On Windows the console write is synchronous and was a
# real bottleneck once the Tasks panel started polling /command_status.
class _QuietAccessLog(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return not ("/command_status" in msg or "/stopd_result" in msg
                    or '"OPTIONS ' in msg)
logging.getLogger("werkzeug").addFilter(_QuietAccessLog())

# ══════════════════════════════════════════════════════════════════
#  SERVER SETTINGS  (persisted across restarts)
# ══════════════════════════════════════════════════════════════════
def _load_server_settings():
    if not os.path.exists(SERVER_SETTINGS_FILE):
        return {}
    try:
        with open(SERVER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_server_settings():
    # backend_servers.json is the single merged config file (shared with the
    # prompt-mgr app). Persist every section this app knows about — llm_server,
    # systemapi and promptapi — preserving any other keys already on disk.
    # promptapi.port is the Prompt API TCP port; promptapi.http_port is its HTTP
    # port (mirrors llm_server). Both are learned from the llm-server's
    # GET /server_settings on connect (see _discover_and_save_promptapi).
    data = _load_server_settings()
    data["llm_server"] = {
        "host":      LLM_TCP_HOST,
        "port":      LLM_TCP_PORT,
        "http_port": LLM_HTTP_PORT,
    }
    data["systemapi"] = {
        "host": SYSTEMAPI_HOST,
        "port": SYSTEMAPI_PORT,
    }
    data["promptapi"] = {
        "host":      PROMPTAPI_HOST,
        "port":      PROMPTAPI_TCP,
        "http_port": PROMPTAPI_HTTP,
    }
    with open(SERVER_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# Apply persisted settings over the hardcoded defaults (nested merged schema).
_saved = _load_server_settings()
_saved_llm = _saved.get("llm_server", {})
_saved_sys = _saved.get("systemapi", {})
_saved_papi = _saved.get("promptapi", {})
if _saved_llm:
    LLM_TCP_HOST  = _saved_llm.get("host", LLM_TCP_HOST)
    LLM_HTTP_HOST = _saved_llm.get("host", LLM_HTTP_HOST)
    LLM_TCP_PORT  = int(_saved_llm.get("port",      LLM_TCP_PORT))
    LLM_HTTP_PORT = int(_saved_llm.get("http_port", LLM_HTTP_PORT))
if _saved_sys:
    SYSTEMAPI_HOST = _saved_sys.get("host", SYSTEMAPI_HOST)
    SYSTEMAPI_PORT = int(_saved_sys.get("port", SYSTEMAPI_PORT))
if _saved_papi:
    PROMPTAPI_HOST = _saved_papi.get("host", PROMPTAPI_HOST)
    PROMPTAPI_TCP  = int(_saved_papi.get("port",      PROMPTAPI_TCP))
    PROMPTAPI_HTTP = int(_saved_papi.get("http_port", PROMPTAPI_HTTP))


def _discover_and_save_promptapi():
    """Called after we connect to the llm-server. Query its PUBLIC endpoint
    GET /server_settings (no password / token required), which returns e.g.:

        {"llm-server_TCP": 8111, "llm-server_HTTP": 8112,
         "prompt-api_TCP": 8202, "prompt-api_HTTP": 8203}

    Take prompt-api_TCP / prompt-api_HTTP, pair them with the llm-server host
    (the Prompt API runs alongside the llm-server), and persist them into the
    promptapi section of backend_servers.json. Best-effort: a failure is logged
    and the previously saved/default ports are kept."""
    global PROMPTAPI_HOST, PROMPTAPI_TCP, PROMPTAPI_HTTP
    host = LLM_HTTP_HOST if LLM_HTTP_HOST not in ("0.0.0.0", "") else "127.0.0.1"
    url  = f"{LLM_SCHEME}://{host}:{LLM_HTTP_PORT}/server_settings"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as r:
            d = json.loads(r.read())
        PROMPTAPI_HOST = host
        PROMPTAPI_TCP  = int(d["prompt-api_TCP"])
        PROMPTAPI_HTTP = int(d["prompt-api_HTTP"])
    except Exception as e:
        print(f"[promptapi] /server_settings discovery failed: {e}")
        return False
    _save_server_settings()
    print(f"[promptapi] discovered host={PROMPTAPI_HOST} tcp={PROMPTAPI_TCP} http={PROMPTAPI_HTTP}")
    return True

def _strip_timestamp(text: str) -> str:
    return _re.sub(r'^\[\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?\]\s*-?\s*', '', text)

# ══════════════════════════════════════════════════════════════════
#  PASSWORD / LOGIN
# ══════════════════════════════════════════════════════════════════
def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _load_pw_cfg():
    if not os.path.exists(PASSWORD_FILE):
        return {"pass": "", "login_required": True}
    with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_pw_cfg(cfg):
    with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def _is_login_required():
    return _load_pw_cfg().get("login_required", True)

_tokens = {}
_tokens_lock = threading.Lock()
TOKEN_TTL = 86400

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

def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _is_login_required():
            tok = (
                request.headers.get("X-Auth-Token") or
                (request.get_json(silent=True) or {}).get("token") or
                request.args.get("token")
            )
            if not tok or not _token_valid(tok):
                return jsonify({"status": "error", "message": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

# ══════════════════════════════════════════════════════════════════
#  SYSTEMAPI AUTH  (password → token, cached in memory)
# ══════════════════════════════════════════════════════════════════
# chat.py authenticates to systemapi the same way the AI does: it logs in with a
# password, receives a token, and attaches `token:<tok>` to every command. The
# token and the working password are kept in memory only (lost on restart).
_systemapi_token = None          # token cached from systemapi
_systemapi_pass  = None          # plaintext pw known to work with systemapi
_systemapi_lock  = threading.Lock()
_systemapi_login_pending = 0.0   # epoch until which a manual /apis/login is expected (60s window)

def _sys_get_token():
    with _systemapi_lock:
        return _systemapi_token

def _sys_set_token(tok):
    global _systemapi_token
    with _systemapi_lock:
        _systemapi_token = tok or None

def _sys_get_pass():
    with _systemapi_lock:
        return _systemapi_pass

def _sys_set_pass(pw):
    global _systemapi_pass
    with _systemapi_lock:
        _systemapi_pass = pw or None

def _sys_set_pending(epoch):
    global _systemapi_login_pending
    with _systemapi_lock:
        _systemapi_login_pending = epoch

def _systemapi_online(timeout=2):
    try:
        test = socket.create_connection((SYSTEMAPI_HOST, SYSTEMAPI_PORT), timeout=timeout)
        test.close()
        return True
    except Exception:
        return False

def _systemapi_exchange(payload, timeout=120):
    """Send one request to systemapi and read the full reply.
    Returns (reply_text, None) on success, or (None, err) where err is
    'connection_refused' or a string description."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        s.settimeout(timeout)
        s.connect((SYSTEMAPI_HOST, SYSTEMAPI_PORT))
        s.sendall(payload.encode("utf-8"))
        # Half-close the write side (send FIN). After systemapi sends its reply,
        # its next recv on this connection reads EOF and it closes the socket,
        # which gives us an immediate, clean end-of-reply: the read loop below
        # ends the instant the connection closes — with NO idle-drain wait. This
        # is what keeps the exchange instant (the previous version dropped this
        # shutdown and paid a fixed idle-timeout tax on EVERY command, including
        # login). `timeout` still bounds how long we wait for a slow command to
        # produce its reply, and SO_KEEPALIVE guards the silent wait.
        # Trade-off (same as the original fast design): a command that runs
        # silently for >~120s may be reset by the Windows FIN_WAIT_2 timer — fine
        # for all normal commands; systemapi still bounds execution by timeout.
        s.shutdown(socket.SHUT_WR)
        resp = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            resp += chunk
        s.close()
        return resp.decode("utf-8", errors="replace").strip(), None
    except ConnectionRefusedError:
        return None, "connection_refused"
    except Exception as e:
        return None, str(e)

def _systemapi_login(pw):
    """Log in to systemapi with a password. On success, cache the token and the
    working password. Returns (True, None) or (False, reason) where reason is
    'offline' | 'wrong_password' | 'unexpected'."""
    reply, err = _systemapi_exchange(f"password:{pw}\n")
    if err is not None or reply is None:
        return False, "offline"
    for line in reply.splitlines():
        if line.strip().lower().startswith("yourtoken:"):
            tok = line.strip()[len("yourtoken:"):].strip()
            _sys_set_token(tok)
            _sys_set_pass(pw)
            return True, None
    if "password not correct" in reply.lower():
        return False, "wrong_password"
    return False, "unexpected"

def _try_login_stored():
    """Re-login to systemapi using the cached password. The password is kept on
    failure so every later command keeps retrying the login (e.g. after the
    systemapi password changed) instead of giving up after one attempt."""
    pw = _sys_get_pass()
    if not pw:
        return False
    ok, _ = _systemapi_login(pw)
    return ok

# ── Per-command timeout derivation ─────────────────────────────────
# A command's own `timeout=NN` (or JSON "timeout": NN) governs how long systemapi
# runs it. chat.py must keep the socket open at least that long + a buffer, or it
# tears down the connection mid-run (WinError 10053 on the systemapi side). These
# mirror _parse_os_command / _TIMEOUT_LINE in systemapi.py.
_CMD_TIMEOUT_LINE = _re.compile(r'(?im)^\s*timeout\s*=\s*(\d+)\s*/?\s*s?\s*$')
_CMD_TIMEOUT_JSON = _re.compile(r'(?i)"timeout"\s*:\s*(\d+)')

SYSTEMAPI_TIMEOUT_BUFFER  = 5    # seconds added on top of the command's own timeout (grace period)
SYSTEMAPI_DEFAULT_TIMEOUT = 120  # fallback when no explicit timeout is present

def _extract_command_timeout(payload):
    """Largest timeout explicitly declared inside payload, or None if none.

    Covers the brace-block form (timeout=NN) and the JSON form ("timeout": NN).
    Returns None for commands that set no timeout (they keep the default).
    Used as the back-compat fallback for payloads that match no command def."""
    vals = [int(m) for m in _CMD_TIMEOUT_LINE.findall(payload)]
    vals += [int(m) for m in _CMD_TIMEOUT_JSON.findall(payload)]
    vals = [v for v in vals if v > 0]
    return max(vals) if vals else None

# A single standalone "timeout=NN" line (no MULTILINE — matches one line only).
_CMD_TIMEOUT_ONE = _re.compile(r'(?i)^\s*timeout\s*=\s*(\d+)\s*/?\s*s?\s*$')

def _timeout_line_value(line):
    """The NN of a standalone `timeout=NN` line, else None (line is plain text)."""
    m = _CMD_TIMEOUT_ONE.match(line)
    return int(m.group(1)) if m else None

def _match_command_def(payload):
    """The command def whose prefix the payload's first line starts with, or None."""
    first = payload.lstrip().split('\n', 1)[0].strip().lower()
    for cmd in _load_commands():
        prefix = (cmd.get('prefix') or '').lower()
        if prefix and first.startswith(prefix):
            return cmd
    return None

def _line_form_timeout(payload, braces):
    """`timeout=NN` value resolved by the spec's placement rules, else None.

    braces=True : the LAST non-empty line inside {…}; if that line is not a
                  timeout, the FIRST non-empty line after the closing `}`.
    braces=False: the first standalone `timeout=NN` line after the command line."""
    if braces:
        # Match the block's closing `}` to its opening `{` by walking brace DEPTH,
        # so braces inside the command body (e.g. Python f-strings like f'{ip}')
        # are balanced out instead of fooling a naive rfind('}'). If the block is
        # incomplete (no matching close — e.g. the body brace count is unbalanced
        # or the block was truncated), close_i stays -1 and we fall through to the
        # standalone-line scan below.
        open_i  = payload.find('{')
        close_i = -1
        if open_i != -1:
            depth = 0
            for k in range(open_i, len(payload)):
                if payload[k] == '{':
                    depth += 1
                elif payload[k] == '}':
                    depth -= 1
                    if depth == 0:
                        close_i = k
                        break
        if open_i != -1 and close_i > open_i:
            inner_lines = [l for l in payload[open_i + 1:close_i].split('\n') if l.strip()]
            # Scan ALL inner lines for a standalone `timeout=NN` directive, not
            # just the last one: a `kill-after_timeout=...` line may legitimately
            # follow the timeout line inside the block, so the timeout is not
            # necessarily the last inner line. A timeout directive anywhere inside
            # the block is the one assigned to it.
            inner_vals = [v for v in (_timeout_line_value(l) for l in inner_lines) if v]
            if inner_vals:
                return inner_vals[-1]
            # no timeout directive inside → look just past the closing brace
            for l in payload[close_i + 1:].split('\n'):
                if l.strip():
                    return _timeout_line_value(l)
            return None
        # malformed braces → fall through to single-line handling
    for l in payload.split('\n')[1:]:
        v = _timeout_line_value(l)
        if v:
            return v
    return None

def _command_timeout_for(payload):
    """Per-command timeout (seconds) honoring the matched def's `timeout` flag and
    placement rules. None means "no explicit timeout" (caller uses the default).

    - No matching def → back-compat whole-payload scan (_extract_command_timeout).
    - Def with `timeout` falsy → None (any timeout=NN is plain text).
    - Def with `timeout` truthy → line-form value by placement, plus the JSON form
      ("timeout": NN) anywhere (preserves os_command); the largest wins."""
    cmd = _match_command_def(payload)
    if cmd is None:
        return _extract_command_timeout(payload)
    if not cmd.get('timeout'):
        return None
    vals = []
    line_v = _line_form_timeout(payload, bool(cmd.get('braces')))
    if line_v:
        vals.append(line_v)
    vals += [int(m) for m in _CMD_TIMEOUT_JSON.findall(payload)]
    vals = [v for v in vals if v > 0]
    return max(vals) if vals else None

def _systemapi_timeout_for(payload):
    """Socket timeout for sending payload to systemapi: the command's own timeout
    plus a buffer, or the default when no explicit timeout is declared. No upper
    cap — systemapi itself bounds execution at the command's timeout."""
    matched = _match_command_def(payload)
    t = _command_timeout_for(payload)
    final = (t + SYSTEMAPI_TIMEOUT_BUFFER) if t else SYSTEMAPI_DEFAULT_TIMEOUT
    print("=" * 70, flush=True)
    print("[timeout-debug] _systemapi_timeout_for CALLED", flush=True)
    print(f"[timeout-debug]   payload (repr) = {payload!r}", flush=True)
    print(f"[timeout-debug]   matched def    = {matched}", flush=True)
    print(f"[timeout-debug]   command_timeout= {t}  (None means no explicit timeout)", flush=True)
    print(f"[timeout-debug]   buffer         = {SYSTEMAPI_TIMEOUT_BUFFER}", flush=True)
    print(f"[timeout-debug]   => SOCKET TIMEOUT APPLIED = {final}s", flush=True)
    print("=" * 70, flush=True)
    return final

def _is_token_error(reply):
    """True only when systemapi's reply IS the token-rejection message — NOT when a
    command's output merely *contains* that text.

    systemapi sends `Error: token not found` as the ENTIRE reply (raw, no
    `systemapi:` prefix) and runs no command. A real command reply is always
    prefixed `systemapi:\\n...`. A plain `in reply` substring test gives a false
    positive whenever the output itself carries the literal string — e.g. reading
    a source file that contains `"Error: token not found"` (our own auth code
    does) — which made chat.py discard a valid token, re-login, and re-run the
    whole command. Match the stripped reply exactly instead."""
    return reply.strip().lower() == "error: token not found"

def _systemapi_command(cmd, timeout=120):
    """Send a command to systemapi with token auth, re-logging-in with the cached
    password whenever the token is missing/expired. Returns (result, None) or
    (None, err) where err is 'connection_refused' or 'systemapi_Authentication'.
    A login failure yields 'systemapi_Authentication' on EVERY call (it never
    stops retrying), so it self-heals once the right password is supplied."""
    if not _systemapi_online():
        return None, "connection_refused"

    tok = _sys_get_token()
    if not tok:
        if not _try_login_stored():
            return None, "systemapi_Authentication"
        tok = _sys_get_token()

    reply, err = _systemapi_exchange(f"token:{tok}\n{cmd}", timeout=timeout)
    if err == "connection_refused":
        return None, "connection_refused"
    if err is not None or reply is None:
        return None, err or "systemapi_Authentication"

    if _is_token_error(reply):
        # Token expired/invalid: discard it and re-login with the cached password.
        _sys_set_token(None)
        if not _try_login_stored():
            return None, "systemapi_Authentication"
        tok = _sys_get_token()
        reply, err = _systemapi_exchange(f"token:{tok}\n{cmd}", timeout=timeout)
        if err == "connection_refused":
            return None, "connection_refused"
        if err is not None or reply is None:
            return None, err or "systemapi_Authentication"
        if _is_token_error(reply):
            return None, "systemapi_Authentication"

    return reply, None

# ══════════════════════════════════════════════════════════════════
#  PARALLEL COMMAND SESSIONS  (one persistent socket, streamed status)
# ══════════════════════════════════════════════════════════════════
# chat.py mints a session id, sends every command in one Build payload to
# systemapi over a single socket, and a background reader streams per-command
# status frames (working→done/error) into _cmd_sessions. The frontend polls
# /command_status; the completed result is still forwarded to the AI "as is"
# (combined exactly like the old path) unless the user paused it.
_cmd_sessions = {}                 # session_id -> session dict (see _build_session)
_cmd_sessions_lock = threading.Lock()
SESSION_TTL = 600                  # keep a finished, non-held session this long (s)

# ── duplicate-batch guard ──────────────────────────────────────────
# Every open browser tab independently detects the SAME commands in one AI reply
# (the SSE 'ai_message_done' fans out to all tabs) and each POSTs /api/cmds_batch.
# Without a guard the server built one systemapi session PER TAB, so the same
# commands ran 2-3× and the combined result was forwarded to the AI 2-3×.
# Fingerprint the command batch: only the FIRST POST builds a session; any
# duplicate POST that arrives within BATCH_DEDUP_WINDOW seconds returns that same
# session_id instead of starting a new one.
_batch_dedup = {}                  # fingerprint -> (session_id_or_None, epoch)
_batch_dedup_lock = threading.Lock()
BATCH_DEDUP_WINDOW = 3             # seconds an identical batch is treated as a dup

def _batch_fingerprint(commands):
    """Stable hash of a command batch, ignoring blank entries and surrounding
    whitespace so cosmetic differences between tabs still collapse to one key."""
    norm = "\n\x00\n".join((c or "").strip() for c in commands if c and c.strip())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

def _purge_batch_dedup(now):
    """Drop fingerprints older than the window (caller holds _batch_dedup_lock)."""
    for k in [k for k, (_sid, ts) in _batch_dedup.items()
              if now - ts > BATCH_DEDUP_WINDOW]:
        del _batch_dedup[k]

def _new_session_id():
    """5 random lowercase letters + 5 random digits, e.g. 'abcde12345'."""
    letters = "".join(secrets.choice(string.ascii_lowercase) for _ in range(5))
    digits  = "".join(secrets.choice(string.digits) for _ in range(5))
    return letters + digits

def _strip_end(cmd_text):
    """Drop everything from a standalone END line onward."""
    clean = []
    for l in cmd_text.split("\n"):
        if l.strip().upper() == "END":
            break
        clean.append(l)
    return "\n".join(clean).strip()

SESSION_KEEP_FINISHED = 10   # auto-clear finished sessions beyond this many

def _cleanup_sessions():
    now = time.time()
    with _cmd_sessions_lock:
        # TTL: drop old finished (completed/errored, non-held) sessions.
        for sid in list(_cmd_sessions):
            s = _cmd_sessions[sid]
            if (s.get("complete") and not s.get("held")
                    and s.get("finished") and (now - s["finished"]) > SESSION_TTL):
                del _cmd_sessions[sid]
        # Count cap: once more than 10 finished sessions pile up, drop the oldest
        # so the list returns to normal (held sessions are never auto-cleared —
        # they await the user's start/clear decision).
        finished = [(s.get("finished") or 0, sid) for sid, s in _cmd_sessions.items()
                    if s.get("complete") and not s.get("held")]
        if len(finished) > SESSION_KEEP_FINISHED:
            finished.sort()
            for _, sid in finished[:len(finished) - SESSION_KEEP_FINISHED]:
                _cmd_sessions.pop(sid, None)

def _read_one_frame(sock):
    """Read until one newline-delimited JSON frame is complete. Returns
    (frame_dict_or_None, leftover_bytes). None on EOF/timeout/parse failure."""
    buf = b""
    while b"\n" not in buf:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            return None, buf
        if not chunk:
            return None, buf
        buf += chunk
    line, leftover = buf.split(b"\n", 1)
    line = line.strip()
    if not line:
        return None, leftover
    try:
        return json.loads(line.decode("utf-8", "replace")), leftover
    except Exception:
        return None, leftover

def _frame_is_token_error(frame):
    """True when a handshake frame is systemapi's token rejection."""
    return bool(frame
                and str(frame.get("status", "")).lower() == "error"
                and "token not found" in str(frame.get("message", "")).lower())

def _build_session(cmds):
    """Open a parallel command session on systemapi. cmds is a list of command
    strings (the AI's `full` text). Returns the session_id on success, else None
    (offline / not authenticated / connect failure — the caller already saw the
    relevant broadcast)."""
    cmds = [c for c in (cmds or []) if c and c.strip()]
    if not cmds:
        return None
    if not _systemapi_online():
        broadcast("systemapi_offline", {"ts": _ts()})
        return None
    tok = _sys_get_token()
    if not tok:
        if not _try_login_stored():
            broadcast("systemapi_Authentication",
                      {"status": "error", "message": "systemapi_Authentication", "ts": _ts()})
            return None
        tok = _sys_get_token()

    _cleanup_sessions()

    with _cmd_sessions_lock:
        sid = _new_session_id()
        while sid in _cmd_sessions:
            sid = _new_session_id()
        cmd_map, state, order = {}, {}, []
        for idx, raw in enumerate(cmds):
            cid   = f"command{idx + 1}#"
            clean = _strip_end(raw)
            t     = _command_timeout_for(clean) or SYSTEMAPI_DEFAULT_TIMEOUT
            ctype = clean.split(":", 1)[0].strip().lower() if ":" in clean else ""
            cmd_map[cid] = f"timeout={t}:{clean}"   # fold timeout into the wire token
            state[cid]   = {"status": "pending", "type": ctype, "timeout": t,
                            "start": None, "result": None, "raw": clean}
            order.append(cid)
        _cmd_sessions[sid] = {
            "commands": state, "order": order,
            "stopped": False, "complete": False, "held": False,
            "stat_seen": False, "combined": None, "error": None,
            "created": time.time(), "finished": None,
        }

    def _attempt(token):
        """Connect, send the Build, and read the first frame (the Listen ack or an
        error). Returns (sock, frame, leftover_bytes). Raises on connect/send
        failure. IMPORTANT: never shutdown(SHUT_WR) — half-closing puts the socket
        in FIN_WAIT_2, which Windows resets after ~120s while a command runs
        silently. systemapi delimits the Build via its idle-gap reader."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        s.settimeout(5)                       # bounded connect
        s.connect((SYSTEMAPI_HOST, SYSTEMAPI_PORT))
        payload = {"token": token, "status": "Build", "session_id": sid, "commands": cmd_map}
        s.sendall(json.dumps(payload).encode("utf-8"))
        s.settimeout(15)                      # bounded wait for the handshake frame
        fr, leftover = _read_one_frame(s)
        return s, fr, leftover

    def _fail(err, event):
        with _cmd_sessions_lock:
            if sid in _cmd_sessions:
                _cmd_sessions[sid]["error"]    = err
                _cmd_sessions[sid]["complete"] = True
                _cmd_sessions[sid]["finished"] = time.time()
        broadcast(event, {"status": "error", "message": err, "ts": _ts()})

    sock = None
    try:
        sock, frame, leftover = _attempt(tok)
        # Self-heal a stale token (systemapi restarted → its in-memory token is
        # gone): discard it, re-login with the cached password, and resend ONCE —
        # exactly like the legacy _systemapi_command path, so the user is not
        # bounced to the login prompt for a recoverable token expiry.
        if _frame_is_token_error(frame):
            try: sock.close()
            except Exception: pass
            _sys_set_token(None)
            if _try_login_stored():
                tok = _sys_get_token()
                sock, frame, leftover = _attempt(tok)
        if _frame_is_token_error(frame):
            try: sock.close()
            except Exception: pass
            with _cmd_sessions_lock:
                _cmd_sessions.pop(sid, None)
            broadcast("systemapi_Authentication",
                      {"status": "error", "message": "systemapi_Authentication", "ts": _ts()})
            return None
        if not frame or str(frame.get("status", "")).lower() != "listen":
            try: sock.close()
            except Exception: pass
            _fail((frame or {}).get("message", "connection to systemapi lost"),
                  "systemapi_offline")
            return None
    except Exception as e:
        print(f"[session {sid}] connect/send failed: {e}")
        if sock:
            try: sock.close()
            except Exception: pass
        _fail("connection to systemapi lost", "systemapi_offline")
        return None

    # Listen ack received → stream the rest. No per-recv deadline now: a command
    # may run silently for minutes; systemapi sends a terminal frame within its
    # wall-clock timeout and closes the socket at the end (the read loop ends on EOF).
    sock.settimeout(None)
    threading.Thread(target=_session_reader, args=(sock, sid, leftover), daemon=True).start()
    # Tell the browser a session is live so it can auto-open the Tasks panel.
    broadcast("session_open", {"session_id": sid, "ts": _ts()})
    return sid

def _apply_frame(sid, frame):
    """Fold one streamed JSON frame into the session's per-command state."""
    with _cmd_sessions_lock:
        s = _cmd_sessions.get(sid)
        if not s:
            return
        # Top-level acks: Listen / error (auth / duplicate). Per-command frames
        # carry their status nested under a commandN# key, never at top level.
        if "stat" in frame:
            s["stat_seen"] = True
            return
        top = str(frame.get("status", "")).lower()
        if top == "listen":
            s["listen"] = True
            return
        if top == "error":
            s["error"] = frame.get("message", "error")
            return
        for k, v in frame.items():
            if k in ("session_id", "token", "status") or not isinstance(v, dict):
                continue
            cmd = s["commands"].get(k)
            if cmd is None:
                continue
            st = v.get("status")
            if st == "working":
                cmd["status"]  = "working"
                cmd["type"]    = v.get("type", cmd["type"])
                cmd["timeout"] = v.get("timeout", cmd["timeout"])
                if cmd["start"] is None:
                    cmd["start"] = time.time()
            elif st == "done":
                cmd["status"] = "done"
                cmd["result"] = v.get("result", "")
            elif st == "error":
                cmd["status"] = "error"
                cmd["result"] = v.get("message", "")

def _session_reader(sock, sid, initial_buf=b""):
    """Read newline-delimited JSON frames until systemapi closes the socket.
    initial_buf carries any bytes already read past the handshake frame."""
    buf = initial_buf or b""
    try:
        # Process frames that arrived together with the Listen ack.
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line:
                try:
                    _apply_frame(sid, json.loads(line.decode("utf-8", errors="replace")))
                except Exception:
                    pass
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                _apply_frame(sid, frame)
    except Exception as e:
        print(f"[session {sid}] reader error: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass
        _finalize_session(sid)

def _finalize_session(sid):
    """Called once the session socket closes. Builds the combined result and
    either forwards it to the AI 'as is' or holds it when paused. A premature
    EOF (no 'stat' frame) is reported as a lost connection with NO AI forward."""
    with _cmd_sessions_lock:
        s = _cmd_sessions.get(sid)
        if not s or s.get("complete"):
            return
        s["complete"]  = True
        s["finished"]  = time.time()
        if not s.get("stat_seen") and not s.get("error"):
            s["error"] = "connection to systemapi lost"
        # If the session failed, no command can still be "in flight" — flip any
        # pending/working entry to error so the UI stops counting it down.
        if s.get("error"):
            for cid in s["order"]:
                c = s["commands"][cid]
                if c["status"] in ("pending", "working"):
                    c["status"] = "error"
                    if not c.get("result"):
                        c["result"] = s["error"]
        err     = s.get("error")
        stopped = s.get("stopped")
        combined = None
        if not err:
            parts = []
            for cid in s["order"]:
                c = s["commands"][cid]
                if c["result"] is not None and str(c["result"]).strip():
                    parts.append(f"systemapi:\n{c['result']}")
            combined = "\n\n---\n\n".join(parts)
            s["combined"] = combined

    if err:
        evt = "systemapi_offline" if "lost" in err else "systemapi_Authentication"
        broadcast(evt, {"status": "error", "message": err, "session_id": sid, "ts": _ts()})
        return
    if not combined:
        return
    if stopped:
        with _cmd_sessions_lock:
            if sid in _cmd_sessions:
                _cmd_sessions[sid]["held"] = True
        broadcast("cmd_result", {"command": sid, "result": combined, "ts": _ts(), "held": True})
        return
    broadcast("cmd_result", {"command": sid, "result": combined, "ts": _ts()})
    _forward_to_ai_stream(combined)

# ══════════════════════════════════════════════════════════════════
#  LLM-SERVER AUTH  (password → token over HTTP, cached in memory)
# ══════════════════════════════════════════════════════════════════
# Mirrors the systemapi auth above, but over llm-server's HTTP API instead of a
# raw socket. chat.py logs in with a password (POST /login), receives a token,
# and attaches `X-Auth-Token: <tok>` to every proxied HTTP call. Unlike
# systemapi, llm-server CANNOT be skipped — everything depends on it. The token
# and the working password live in memory only (lost on restart); the password
# doubles as the value auto-synced to llm-server via POST /set_password.
_llm_token = None                # token cached from llm-server
_llm_pass  = None                # plaintext pw known to work with llm-server
_llm_lock  = threading.Lock()
_llm_login_pending = 0.0         # epoch until which a manual /apis/login is expected (60s window)

def _llm_get_token():
    with _llm_lock:
        return _llm_token

def _llm_set_token(tok):
    global _llm_token
    with _llm_lock:
        _llm_token = tok or None

def _llm_get_pass():
    with _llm_lock:
        return _llm_pass

def _llm_set_pass(pw):
    global _llm_pass
    with _llm_lock:
        _llm_pass = pw or None

def _llm_set_pending(epoch):
    global _llm_login_pending
    with _llm_lock:
        _llm_login_pending = epoch

def _llm_online(timeout=2):
    try:
        test = socket.create_connection((LLM_HTTP_HOST, LLM_HTTP_PORT), timeout=timeout)
        test.close()
        return True
    except Exception:
        return False

def _llm_raw_post(path, body=None, qs=None, timeout=10):
    """Plain POST to llm-server WITHOUT the auth token (used by /login and
    /set_password, which must not recurse through the token logic). Returns
    (ok, status_code, json_dict). ok is True only for 2xx."""
    url = f"{LLM_SCHEME}://{LLM_HTTP_HOST}:{LLM_HTTP_PORT}{path}"
    if qs:
        from urllib.parse import urlencode
        url += "?" + urlencode(qs)
    data = json.dumps(body or {}).encode()
    req  = urllib.request.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return False, e.code, json.loads(e.read())
        except Exception:
            return False, e.code, {"error": str(e)}
    except Exception as e:
        return False, 0, {"error": str(e)}

def _llm_login(pw):
    """Log in to llm-server with a password. On success, cache the token and the
    working password. Returns (True, None) or (False, reason) where reason is
    'offline' | 'wrong_password' | 'no_password_set' | 'unexpected'."""
    ok, code, d = _llm_raw_post("/login", {"password": pw})
    if code == 0:
        return False, "offline"
    if ok and d.get("status") == "ok" and d.get("token"):
        _llm_set_token(d["token"])
        _llm_set_pass(pw)
        # Now that we're connected, learn + persist the Prompt API ports.
        _discover_and_save_promptapi()
        return True, None
    msg = (d.get("message") or "").lower()
    if "no password has been set" in msg:
        return False, "no_password_set"
    if "wrong_password" in msg or code == 401:
        return False, "wrong_password"
    return False, "unexpected"

def _llm_set_password_remote(pw):
    """POST /set_password?password=pw to bootstrap llm-server's password.
    Returns (ok, json_dict). Only succeeds while llm-server has no password set
    (otherwise llm-server replies 409)."""
    ok, _code, d = _llm_raw_post("/set_password", qs={"password": pw})
    return ok, d

def _try_llm_login_stored():
    """Re-login to llm-server using the cached password. The password is kept on
    failure so every later call keeps retrying (e.g. after the llm-server
    password changed) instead of giving up after one attempt."""
    pw = _llm_get_pass()
    if not pw:
        return False
    ok, _ = _llm_login(pw)
    return ok

# ══════════════════════════════════════════════════════════════════
#  SSE / GENERATION STATE
# ══════════════════════════════════════════════════════════════════
_clients = []
_clients_lock = threading.Lock()
_is_generating = False
_is_generating_lock = threading.Lock()
_generation_done_evt = threading.Event()
_generation_done_evt.set()
_stop_event = threading.Event()

_current_session = None
_current_session_lock = threading.Lock()

def _set_current_session(name):
    global _current_session
    with _current_session_lock:
        _current_session = name if name else None

def _get_current_session():
    with _current_session_lock:
        return _current_session

# Session that owns the in-flight generation (may differ from the displayed
# session if the user loads another session while a response is streaming).
_generating_session = None

def _set_generating_session(name):
    global _generating_session
    _generating_session = name if name else None

def _get_generating_session():
    return _generating_session

def _set_generating(v):
    global _is_generating
    with _is_generating_lock:
        _is_generating = v
    if v:
        _stop_event.clear()
        _generation_done_evt.clear()
    else:
        _generation_done_evt.set()

def _get_generating():
    with _is_generating_lock:
        return _is_generating

def _has_clients():
    with _clients_lock:
        return len(_clients) > 0

def broadcast(event_type, data):
    if event_type in ("ai_think_start", "ai_token"):
        _set_generating(True)
    elif event_type in ("ai_message_done", "ai_stopped"):
        _set_generating(False)
    payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
    with _clients_lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _clients.remove(q)

def _ts():
    now = datetime.now()
    h = now.hour % 12 or 12
    m = now.strftime("%M")
    ampm = "AM" if now.hour < 12 else "PM"
    return f"{h}:{m} {ampm}"

# High-frequency polling endpoints are not logged (they otherwise flood the
# Windows console — slow, serialized I/O — and starve real requests).
_QUIET_PATHS = {"/command_status", "/stopd_result"}

@chat_app.before_request
def log_request():
    if request.method == "OPTIONS" or request.path in _QUIET_PATHS:
        return
    body = request.get_json(silent=True)
    if body:
        print(f"[{_ts()}] {request.method} {request.path} | body: {json.dumps(body, ensure_ascii=False)}")
    else:
        print(f"[{_ts()}] {request.method} {request.path}")

@chat_app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Auth-Token"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    # Cache the CORS preflight so the browser stops sending an OPTIONS before
    # every single request (the X-Auth-Token header forces preflight otherwise —
    # which doubled request volume on the polling endpoints).
    r.headers["Access-Control-Max-Age"] = "600"
    # never cache UI assets (html/js/css) so edits always take effect on reload
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"]        = "no-cache"
    r.headers["Expires"]       = "0"
    return r

@chat_app.route("/", defaults={"p": ""}, methods=["OPTIONS"])
@chat_app.route("/<path:p>", methods=["OPTIONS"])
def handle_options(p): return jsonify({}), 200

# ══════════════════════════════════════════════════════════════════
#  LOGIN ENDPOINTS
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(force=True) or {}
    pw   = body.get("password", "")
    cfg  = _load_pw_cfg()
    if not cfg.get("pass", ""):
        return jsonify({"status": "error", "message": "no_password_set"}), 400
    if _sha256(pw) != cfg["pass"]:
        return jsonify({"status": "error", "message": "wrong_password"}), 401
    token = _issue_token()
    resp = {"status": "ok", "token": token,
            "login_required": cfg.get("login_required", True)}

    # ── systemapi login (password → token) ──────────────────────────
    # `systemapi_pass` is optional: when omitted we reuse the chat password,
    # since both services usually share the same password. systemapi is
    # SKIPPABLE — offline/failed never blocks the chat login.
    sys_pw = body.get("systemapi_pass") or pw
    _sys_set_pass(sys_pw)   # remember candidate so a later command can auto-login
    if not _systemapi_online():
        # systemapi offline: log in without it (as before). The cached password
        # lets the next command auto-login once systemapi comes back.
        resp["systemapi"] = "offline"
    else:
        ok, _why = _systemapi_login(sys_pw)
        if ok:
            _sys_set_pending(0.0)
            resp["systemapi"] = "ok"
        else:
            # chat password correct, but systemapi rejected it: open a 60s window
            # for the client to retry manually (/apis/login) or skip.
            _sys_set_pending(time.time() + 60)
            resp["login_required"] = True
            resp["systemapi"] = "login_failed"
            resp["message"] = "systemapi_login_failed"

    # ── llm-server login (password → token) ─────────────────────────
    # `llmserver_pass` is optional: when omitted we reuse the chat password.
    # Unlike systemapi, llm-server is MANDATORY (everything depends on it), so a
    # failure here is surfaced as a flag the UI uses to block features. We still
    # soft-fail with HTTP 200 + token so the client can recover via /apis/login,
    # /set_password, or /reconnect.
    llm_pw = body.get("llmserver_pass") or pw
    _llm_set_pass(llm_pw)   # remember candidate (also used for /set_password sync)
    if not _llm_online():
        resp["llm-server"] = "offline"
        _llm_set_pending(time.time() + 60)
    else:
        ok, why = _llm_login(llm_pw)
        if ok:
            _llm_set_pending(0.0)
            resp["llm-server"] = "ok"
        elif why == "no_password_set":
            # llm-server has no password yet — the client should bootstrap it via
            # POST /set_password (auto-sync from the in-memory password).
            _llm_set_pending(time.time() + 60)
            resp["login_required"] = True
            resp["llm-server"] = "no_password_set"
            resp["message"] = "No password has been set yet. Please set a password."
        else:
            _llm_set_pending(time.time() + 60)
            resp["login_required"] = True
            resp["llm-server"] = "login_failed"
            resp["message"] = "llm-server_login_failed"

    return jsonify(resp), 200

@chat_app.route("/apis/login", methods=["POST"])
@require_login
def apis_login():
    """Attempt a systemapi and/or llm-server login for an already chat-
    authenticated client. Used to recover from a *_login_failed, to skip
    systemapi, or to re-login after a backend password changes. Either
    `systemapi_pass` or `llmserver_pass` (or both) may be supplied.

    Key difference: systemapi may be skipped (pass `false`); llm-server may NOT
    (it is mandatory — passing `false` is rejected)."""
    body   = request.get_json(force=True) or {}
    sp     = body.get("systemapi_pass", None)
    lp     = body.get("llmserver_pass", None)
    result = {"status": "ok"}
    code   = 200

    handled = False

    # ── systemapi (skippable) ───────────────────────────────────────
    if sp is not None:
        handled = True
        # `false` (bool or string) → continue without systemapi access.
        if sp is False or (isinstance(sp, str) and sp.strip().lower() == "false"):
            _sys_set_token(None)
            _sys_set_pass(None)
            _sys_set_pending(0.0)
            result["systemapi"] = "skipped"
        elif not sp:
            return jsonify({"status": "error", "message": "systemapi_pass required"}), 400
        elif not _systemapi_online():
            result["status"] = "error"
            result["systemapi"] = "offline"
            result["message"] = "systemapi_offline"
            code = 503
        else:
            ok, _why = _systemapi_login(sp)
            if ok:
                _sys_set_pending(0.0)
                result["systemapi"] = "ok"
            else:
                result["status"] = "error"
                result["systemapi"] = "login_failed"
                result["message"] = "failed login to systemapi"
                code = 401

    # ── llm-server (mandatory — cannot be skipped) ──────────────────
    if lp is not None:
        handled = True
        if lp is False or (isinstance(lp, str) and lp.strip().lower() == "false"):
            return jsonify({"status": "error",
                            "message": "llm-server cannot be skipped"}), 400
        if not lp:
            return jsonify({"status": "error", "message": "llmserver_pass required"}), 400
        if not _llm_online():
            result["status"] = "error"
            result["llm-server"] = "offline"
            result["message"] = "llmserver_offline"
            code = 503
        else:
            ok, why = _llm_login(lp)
            if ok:
                _llm_set_pending(0.0)
                result["llm-server"] = "ok"
            elif why == "no_password_set":
                _llm_set_pass(lp)   # remember so POST /set_password can sync it
                result["status"] = "error"
                result["llm-server"] = "no_password_set"
                result["message"] = "No password has been set yet. Please set a password."
                code = 409
            else:
                result["status"] = "error"
                result["llm-server"] = "login_failed"
                result["message"] = "failed login to llm-server"
                code = 401

    if not handled:
        return jsonify({"status": "error",
                        "message": "systemapi_pass or llmserver_pass required"}), 400

    return jsonify(result), code

@chat_app.route("/api/login/stop-start", methods=["POST"])
def login_toggle():
    body = request.get_json(force=True) or {}
    pw   = body.get("password", "")
    cfg  = _load_pw_cfg()
    if not cfg.get("pass", ""):
        return jsonify({"status": "error", "message": "no_password_set"}), 400
    if _sha256(pw) != cfg["pass"]:
        return jsonify({"status": "error", "message": "wrong_password"}), 401
    new_val = not cfg.get("login_required", True)
    cfg["login_required"] = new_val
    _save_pw_cfg(cfg)
    msg = ("login DISABLED" if not new_val else "login ENABLED")
    broadcast("login_toggled", {"login_required": new_val, "ts": _ts()})
    return jsonify({"status": "ok", "login_required": new_val, "message": msg}), 200

@chat_app.route("/set_password", methods=["POST"])
@require_login
def llm_set_password():
    """Bootstrap llm-server's password (only works while llm-server has none set).

    Option A (manual): POST /set_password?password=<new> — uses the supplied pw.
    Option B (auto-sync): POST /set_password (no query) — reuses the password
    chat.py already holds in memory so both sides end up sharing one password.
    """
    pw = request.args.get("password", "")          # Option A
    if not pw:
        pw = _llm_get_pass()                        # Option B (in-memory password)
        if not pw:
            return jsonify({"status": "error",
                            "message": "No password stored in memory"}), 400

    if not _llm_online():
        return jsonify({"status": "error", "message": "llmserver_offline"}), 503

    ok, d = _llm_set_password_remote(pw)
    if ok:
        # Password is now set on llm-server — remember it and grab a token.
        _llm_set_pass(pw)
        _llm_login(pw)
        _llm_set_pending(0.0)
        return jsonify(d), 200
    # Propagate llm-server's own status (e.g. 409 "password already set").
    code = 409 if "already set" in (d.get("message", "").lower()) else 502
    return jsonify(d), code

@chat_app.route("/reconnect", methods=["POST"])
@require_login
def reconnect():
    """Repoint the backends at runtime and persist the new settings.

    Request: {"systemapi": [ip, port], "llm-server": [scheme, ip, port]}
    Either or both components may be supplied. Each is connectivity-tested before
    anything is committed; on success the new settings are saved, on failure an
    error is returned and nothing is changed. For llm-server the single port is
    the auth/HTTP port (LLM_HTTP_PORT); the TCP streaming port is left unchanged.
    """
    global LLM_TCP_HOST, LLM_HTTP_HOST, LLM_HTTP_PORT, LLM_SCHEME
    global SYSTEMAPI_HOST, SYSTEMAPI_PORT
    body = request.get_json(force=True) or {}
    sys_cfg = body.get("systemapi")
    llm_cfg = body.get("llm-server")

    if not sys_cfg and not llm_cfg:
        return jsonify({"status": "error",
                        "message": "systemapi or llm-server required"}), 400

    def _can_connect(host, port, timeout=3):
        try:
            test = socket.create_connection((host, int(port)), timeout=timeout)
            test.close()
            return True
        except Exception:
            return False

    # ── validate everything BEFORE committing any global ────────────
    pending = {}   # what to apply once all provided components pass
    result  = {"status": "ok"}

    if sys_cfg is not None:
        try:
            s_host, s_port = sys_cfg[0], int(sys_cfg[1])
        except (TypeError, ValueError, IndexError):
            return jsonify({"status": "error",
                            "message": "systemapi must be [ip, port]"}), 400
        if not _can_connect(s_host, s_port):
            return jsonify({"status": "error", "systemapi": "unreachable",
                            "message": f"cannot connect to systemapi at {s_host}:{s_port}"}), 502
        pending["sys"] = (s_host, s_port)
        result["systemapi"] = "ok"

    if llm_cfg is not None:
        try:
            l_scheme, l_host, l_port = llm_cfg[0], llm_cfg[1], int(llm_cfg[2])
        except (TypeError, ValueError, IndexError):
            return jsonify({"status": "error",
                            "message": "llm-server must be [scheme, ip, port]"}), 400
        if l_scheme not in ("http", "https"):
            return jsonify({"status": "error",
                            "message": "llm-server scheme must be 'http' or 'https'"}), 400
        if not _can_connect(l_host, l_port):
            return jsonify({"status": "error", "llm-server": "unreachable",
                            "message": f"cannot connect to llm-server at {l_host}:{l_port}"}), 502
        pending["llm"] = (l_scheme, l_host, l_port)
        result["llm-server"] = "ok"

    # ── all provided components reachable → commit + persist ────────
    if "sys" in pending:
        SYSTEMAPI_HOST, SYSTEMAPI_PORT = pending["sys"]
    if "llm" in pending:
        LLM_SCHEME, LLM_HTTP_HOST, LLM_HTTP_PORT = pending["llm"]
        LLM_TCP_HOST = LLM_HTTP_HOST   # streaming host follows the HTTP host

    _save_server_settings()
    # The llm-server may have moved — re-learn + persist the Prompt API ports.
    if "llm" in pending:
        _discover_and_save_promptapi()
    result["message"] = "reconnected"
    return jsonify(result), 200

# ══════════════════════════════════════════════════════════════════
#  UI — mobile detection
# ══════════════════════════════════════════════════════════════════
def _is_mobile():
    ua = request.headers.get("User-Agent", "").lower()
    return bool(_re.search(
        r"mobile|android|iphone|ipad|ipod|blackberry|windows phone|opera mini|silk",
        ua
    ))


@chat_app.route("/")
@chat_app.route("/chat.html")
@chat_app.route("/chat2.html")
def index():
    if _is_mobile():
        return send_from_directory(HTML_DIR, "index2-mobile.html")
    return send_from_directory(HTML_DIR, "index2.html")
@chat_app.route("/<path:filename>.js")
def serve_js(filename):
    return send_from_directory(
        os.path.join("frontend"),
        filename + ".js"
    )
@chat_app.route("/server_settings.html")
def server_settings():
    if _is_mobile():
        return send_from_directory(HTML_DIR, "server_settings.html")
    return send_from_directory(HTML_DIR, "server_settings.html")
# ── static assets (logo, favicon, images) served from HTML_DIR ──
@chat_app.route("/favicon.ico")
def serve_favicon():
    return send_from_directory(HTML_DIR, "favicon.ico")
@chat_app.route("/<path:filename>.png")
def serve_png(filename):
    return send_from_directory(HTML_DIR, filename + ".png")
@chat_app.route("/<path:filename>.ico")
def serve_ico(filename):
    return send_from_directory(HTML_DIR, filename + ".ico")
@chat_app.route("/<path:filename>.svg")
def serve_svg(filename):
    return send_from_directory(HTML_DIR, filename + ".svg")
@chat_app.route("/<path:filename>.jpg")
def serve_jpg(filename):
    return send_from_directory(HTML_DIR, filename + ".jpg")
@chat_app.route("/<path:filename>.webp")
def serve_webp(filename):
    return send_from_directory(HTML_DIR, filename + ".webp")
@chat_app.route("/config")
def index2():
    if _is_mobile():
        return send_from_directory(HTML_DIR, "api _manager.html")
    return send_from_directory(HTML_DIR, "api _manager.html")
@chat_app.route("/prompt_manager.html")
def index3():
    if _is_mobile():
        return send_from_directory(HTML_DIR, "prompt_manager.html")
    return send_from_directory(HTML_DIR, "prompt_manager.html")
# ══════════════════════════════════════════════════════════════════
#  SSE STREAM
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/stream")
@require_login
def stream():
    q = queue.Queue(maxsize=200)
    with _clients_lock:
        _clients.append(q)
    def generate():
        try:
            yield "da  ta: {\"type\":\"connected\"}\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with _clients_lock:
                if q in _clients:
                    _clients.remove(q)
    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ══════════════════════════════════════════════════════════════════
#  TCP STREAM READER
# ══════════════════════════════════════════════════════════════════
def _stream_llm_response(sock, session=None):
    # llm-server speaks NDJSON: one JSON event per line, e.g.
    #   {"type":"content","text":"..."}  reply text
    #   {"type":"think","text":"..."}    reasoning
    #   {"type":"warning","text":"..."}  server notice (context full, trimmed…)
    #   {"type":"done"}                  end of turn
    # We re-broadcast those to the browser as SSE events, synthesising the
    # think start/end transitions and forwarding warnings on their OWN channel
    # so the UI never has to scrape markers out of the model's text.
    buffer = ""          # holds the not-yet-newline-terminated tail
    in_think = False
    think_buf = ""
    normal_buf = ""
    got_done = False
    sock.settimeout(0.2)

    def _handle_event(ev):
        nonlocal in_think, think_buf, normal_buf, got_done
        etype = ev.get("type")
        txt   = ev.get("text", "")
        if etype == "content":
            if in_think:
                broadcast("ai_think_end", {"session": session, "ts": _ts()})
                in_think = False
            if txt:
                normal_buf += txt
                broadcast("ai_token", {"token": txt, "session": session, "ts": _ts()})
        elif etype == "think":
            if not in_think:
                in_think = True
                broadcast("ai_think_start", {"session": session, "ts": _ts()})
            if txt:
                think_buf += txt
                broadcast("ai_think", {"token": txt, "session": session, "ts": _ts()})
        elif etype == "warning":
            # Separate channel — NOT mixed into the reply text.
            broadcast("warning", {"text": txt, "session": session, "ts": _ts()})
        elif etype == "prompt_processing":
            broadcast("prompt_processing",
                      {"value": ev.get("value", 0), "session": session, "ts": _ts()})
        elif etype == "done":
            got_done = True

    while True:
        if _stop_event.is_set():
            try:
                sock.settimeout(0)
                sock.recv(4096)
            except: pass
            try: sock.close()
            except: pass
            break
        try:
            chunk = sock.recv(256)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                # Tolerate a stray non-JSON line (legacy/raw) as plain content.
                normal_buf += line
                broadcast("ai_token", {"token": line, "session": session, "ts": _ts()})
                continue
            _handle_event(ev)
        if got_done:
            break

    if _stop_event.is_set():
        print(f"[stream] STOPPED — discarding residual buffer ({len(buffer)} chars)")
        return ""

    # Flush any trailing event that arrived without a closing newline.
    tail = buffer.strip()
    if tail:
        try:
            _handle_event(json.loads(tail))
        except Exception:
            normal_buf += tail
            broadcast("ai_token", {"token": tail, "session": session, "ts": _ts()})
    if in_think:
        broadcast("ai_think_end", {"session": session, "ts": _ts()})
    final = normal_buf.strip()
    print(f"[stream] DONE — normal_buf={repr(final[:120])} in_think={in_think} done={got_done}")
    broadcast("ai_message_done", {"text": final, "session": session, "ts": _ts()})

    # ── auto-detect commands when no browser is connected ──────────
    if final and not _has_clients():
        cmds = _detect_commands(final)
        if cmds:
            print(f"[auto_cmd] no clients — executing {len(cmds)} command(s) server-side")
            threading.Thread(
                target=_auto_execute_commands,
                args=(cmds,),
                daemon=True
            ).start()

    return final

def _llm_tcp_stream(text, session=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((LLM_TCP_HOST, LLM_TCP_PORT))
    s.sendall(text.encode("utf-8"))
    s.shutdown(socket.SHUT_WR)
    result = _stream_llm_response(s, session)
    s.close()
    return result

def _forward_to_ai_stream(text):
    try:
        if _get_generating():
            print(f"[forward] AI busy — waiting...")
            _generation_done_evt.wait(timeout=120)
        _stop_event.clear()   # a prior reload/stop may have left it set
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((LLM_TCP_HOST, LLM_TCP_PORT))
        s.sendall(text.encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        _stream_llm_response(s, _get_generating_session())
        s.close()
    except Exception as e:
        print(f"[forward_to_ai] Error: {e}")

def _llm_auth_headers(extra=None):
    """Headers for a token-authenticated llm-server call. The X-Auth-Token is
    attached whenever a token is cached; when llm-server has login disabled the
    token is simply ignored on the other side."""
    h = dict(extra or {})
    tok = _llm_get_token()
    if tok:
        h["X-Auth-Token"] = tok
    return h

def _llm_get(path):
    url = f"{LLM_SCHEME}://{LLM_HTTP_HOST}:{LLM_HTTP_PORT}{path}"
    # One automatic retry: on 401 the cached token is stale — re-login with the
    # cached password and try again (self-healing, like _systemapi_command).
    for attempt in (0, 1):
        req = urllib.request.Request(url, method="GET", headers=_llm_auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return True, json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                _llm_set_token(None)
                if _try_llm_login_stored():
                    continue
            # Preserve the actual error body from llm-server (e.g. multi-GPU gating)
            try:
                return False, json.loads(e.read())
            except Exception:
                return False, {"error": str(e)}
        except Exception as e:
            return False, {"error": str(e)}

def _llm_post(path, body=None, timeout=5):
    url  = f"{LLM_SCHEME}://{LLM_HTTP_HOST}:{LLM_HTTP_PORT}{path}"
    data = json.dumps(body or {}).encode()
    for attempt in (0, 1):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers=_llm_auth_headers({"Content-Type": "application/json"}))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return True, json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                _llm_set_token(None)
                if _try_llm_login_stored():
                    continue
            # Preserve the actual error body from llm-server (e.g. validation errors)
            try:
                return False, json.loads(e.read())
            except Exception:
                return False, {"error": str(e)}
        except Exception as e:
            return False, {"error": str(e)}

# ══════════════════════════════════════════════════════════════════
#  STOP HELPER
# ══════════════════════════════════════════════════════════════════
def _do_stop():
    _stop_event.set()
    _set_generating(False)
    broadcast("ai_stopped", {"session": _get_generating_session(), "ts": _ts()})
    try:
        _llm_post("/api/llm/stop")
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════
#  COMMANDS DETECTION + AUTO-EXECUTE
# ══════════════════════════════════════════════════════════════════
def _consume_timeout_line(lines, i):
    """If the next non-empty line after index i is a `timeout=NN` line, return
    (that stripped line, its index); else (None, i). Lets the caller fold a
    trailing `timeout=NN` into a command's `full` so _systemapi_timeout_for sees
    it (single-line commands and braces commands with the timeout after `}`)."""
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines) and _timeout_line_value(lines[j].strip()) is not None:
        return lines[j].strip(), j
    return None, i

def _detect_commands(text: str) -> list:
    """
    Same logic as detectCommands in chat.js:
    - single line: a plain prefix
    - braces: collects from { to }
    - end: collects from the prefix up to an END line
    - timeout: if the command has timeout=true, fold in the following timeout=NN line
    """
    commands = _load_commands()
    if not commands:
        return []

    lines = text.split('\n')
    found = []
    i = 0

    while i < len(lines):
        line     = lines[i].strip()
        line_low = line.lower()

        for cmd in commands:
            prefix  = (cmd.get('prefix') or '').lower()
            if not prefix or not line_low.startswith(prefix):
                continue

            has_timeout = bool(cmd.get('timeout'))

            # ── END block ──────────────────────────────────────────
            if cmd.get('end'):
                block = [line]
                i += 1
                while i < len(lines):
                    block.append(lines[i])
                    if lines[i].strip().upper() == 'END':
                        break
                    i += 1
                found.append({
                    'prefix':  cmd['prefix'],
                    'full':    '\n'.join(block).strip(),
                    'label':   cmd.get('label', cmd['prefix']),
                    'timeout': has_timeout,
                })
                break

            # ── BRACES block ────────────────────────────────────────
            if cmd.get('braces'):
                # Track brace DEPTH (every "{" opens, every "}" closes) instead of
                # stopping at the first line containing "}". A naive "}" check is
                # fooled by braces inside the command body itself — e.g. Python
                # f-strings like f'{ip}' — and truncates the block before its real
                # closing brace, losing the trailing timeout line. This mirrors the
                # depth-based collector in systemapi.api().
                combined = line
                depth = line.count('{') - line.count('}')
                while depth > 0 and i + 1 < len(lines):
                    i += 1
                    combined += '\n' + lines[i]
                    depth += lines[i].count('{') - lines[i].count('}')
                # timeout=NN right after the closing brace belongs to this command
                if has_timeout:
                    tline, ni = _consume_timeout_line(lines, i)
                    if tline is not None:
                        combined += '\n' + tline
                        i = ni
                found.append({
                    'prefix':  cmd['prefix'],
                    'full':    combined.strip(),
                    'label':   cmd.get('label', cmd['prefix']),
                    'timeout': has_timeout,
                })
                break

            # ── single line ────────────────────────
            full = line.strip()
            if has_timeout:
                tline, ni = _consume_timeout_line(lines, i)
                if tline is not None:
                    full += '\n' + tline
                    i = ni
            found.append({
                'prefix':  cmd['prefix'],
                'full':    full,
                'label':   cmd.get('label', cmd['prefix']),
                'timeout': has_timeout,
            })
            break

        i += 1

    return found


def _auto_execute_commands(cmds: list):
    """Auto-run commands detected in the AI's reply when no browser is connected.
    Routes through the same parallel session model as /api/cmds_batch; the session
    reader forwards the combined result back to the AI when complete."""
    if not _systemapi_online():
        broadcast("systemapi_offline", {"ts": _ts()})
        print("[auto_cmd] systemapi offline — aborting")
        return
    _build_session([c['full'] for c in cmds])

# ══════════════════════════════════════════════════════════════════
#  SEND / STOP
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/send", methods=["POST"])
@require_login
def send_msg():
    body = request.get_json(force=True)
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"status": "error", "message": "empty text"}), 400

    def _run():
        try:
            if _get_generating():
                if text.lower().startswith("systemapi:"):
                    _generation_done_evt.wait(timeout=120)
                else:
                    _do_stop()
                    time.sleep(0.15)

            _stop_event.clear()
            _set_generating_session(_get_current_session())   # owns this turn
            t = _ts()
            if text.lower().startswith("systemapi:"):
                stamped = f"[{t}] - {text}"
            else:
                stamped = f"[{t}] {text}"
            broadcast("user_message", {"text": stamped, "ts": t})
            _llm_tcp_stream(stamped, _get_generating_session())
        except Exception as e:
            broadcast("error", {"message": str(e), "ts": _ts()})
            _set_generating(False)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "ok"})

@chat_app.route("/api/llm/stop", methods=["POST"])
@require_login
def llm_stop():
    _do_stop()
    return jsonify({"status": "stop_success", "message": "Stream stopped."}), 200

# ══════════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/cmd", methods=["POST"])
@require_login
def send_cmd():
    body = request.get_json(force=True)
    cmd  = body.get("command", "").strip()
    if not cmd:
        return jsonify({"status": "error", "message": "empty command"}), 400
    def _run():
        try:
            lines = cmd.split("\n")
            clean_lines = []
            for l in lines:
                if l.strip().upper() == "END":
                    break
                clean_lines.append(l)
            payload = "\n".join(clean_lines).strip()
            result, err = _systemapi_command(payload, timeout=_systemapi_timeout_for(payload))
            if err == "connection_refused":
                broadcast("systemapi_offline", {"ts": _ts()})
                return
            if err == "systemapi_Authentication":
                broadcast("systemapi_Authentication", {"status": "error", "message": "systemapi_Authentication", "ts": _ts()})
                return
            if not result: return
            broadcast("cmd_result", {"command": cmd, "result": result, "ts": _ts()})
            _forward_to_ai_stream(result)
        except Exception as e:
            print(f"[cmd] Error: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "ok"})

@chat_app.route("/api/cmds_batch", methods=["POST"])
@require_login
def cmds_batch():
    body     = request.get_json(force=True) or {}
    commands = body.get("commands", [])
    if not commands:
        return jsonify({"status": "error", "message": "no commands"}), 400
    if not _systemapi_online():
        broadcast("systemapi_offline", {"ts": _ts()})
        return jsonify({"status": "error", "message": "systemapi_offline"}), 503

    # ── duplicate-batch guard (multiple tabs POST the same commands) ──
    # Reserve the fingerprint under the lock BEFORE building so concurrent tabs
    # racing in here all see the reservation and collapse onto one session. The
    # first request gets through with a placeholder sid (None); the rest return
    # 'duplicate' immediately and rely on the global /command_status poll to show
    # the session once it exists.
    fp  = _batch_fingerprint(commands)
    now = time.time()
    with _batch_dedup_lock:
        _purge_batch_dedup(now)
        hit = _batch_dedup.get(fp)
        if hit is not None:
            return jsonify({"status": "ok", "session_id": hit[0], "duplicate": True})
        _batch_dedup[fp] = (None, now)   # reserve while we build

    # Run everything through ONE parallel session on systemapi; the background
    # reader streams per-command status and forwards the combined result to the
    # AI when complete. The session_id lets the frontend start polling at once.
    sid = _build_session(commands)
    with _batch_dedup_lock:
        if sid:
            _batch_dedup[fp] = (sid, time.time())   # publish real sid to dups
        else:
            _batch_dedup.pop(fp, None)              # build failed — allow a retry
    if not sid:
        return jsonify({"status": "error", "message": "could not start session"}), 503
    return jsonify({"status": "ok", "session_id": sid})

# ══════════════════════════════════════════════════════════════════
#  SESSION STATUS / STOP / START
# ══════════════════════════════════════════════════════════════════
def _session_snapshot(sid):
    """Build the /command_status view for one session, or None if unknown.
    Remaining_time is computed here (timeout − elapsed) so systemapi never has to
    stream heartbeats."""
    with _cmd_sessions_lock:
        s = _cmd_sessions.get(sid)
        if not s:
            return None
        now = time.time()
        stopped = s.get("stopped", False)
        cmds = {}
        for cid in s["order"]:
            c = s["commands"][cid]
            # When the session is paused, suffix the status (e.g. "working-stoped")
            # so the caller can see which commands they have stopped.
            status = c["status"] + "-stoped" if stopped else c["status"]
            view = {"status": status, "type": c["type"]}
            if c["result"] is not None:
                view["result_preview"] = str(c["result"])[:200]
            if c["status"] == "working":
                view["timeout"] = c["timeout"]
                if c["start"] is not None:
                    view["Remaining_time"] = int(max(0, c["timeout"] - (now - c["start"])))
                else:
                    view["Remaining_time"] = "--"
            cmds[cid] = view
        return {
            "session_id": sid,
            "complete":   s.get("complete", False),
            "stopped":    s.get("stopped", False),
            "held":       s.get("held", False),
            "error":      s.get("error"),
            "commands":   cmds,
        }

def _sid_from_request():
    return request.args.get("session_id") or (request.get_json(silent=True) or {}).get("session_id")

@chat_app.route("/command_status", methods=["GET"])
@require_login
def command_status():
    _cleanup_sessions()
    sid = request.args.get("session_id")
    if sid:
        snap = _session_snapshot(sid)
        if snap is None:
            return jsonify({"status": "error", "message": "session not found"}), 404
        return jsonify(snap)
    with _cmd_sessions_lock:
        ids = list(_cmd_sessions.keys())
    return jsonify({"sessions": [s for s in (_session_snapshot(i) for i in ids) if s]})

@chat_app.route("/stop_result", methods=["POST"])
@require_login
def stop_result():
    sid = _sid_from_request()
    if not sid:
        return jsonify({"status": "error", "message": "missing session_id"}), 400
    with _cmd_sessions_lock:
        s = _cmd_sessions.get(sid)
        if not s:
            return jsonify({"status": "error", "message": "session not found"}), 404
        s["stopped"] = True
        # If it already finished, hold the ready result back from the AI.
        if s.get("complete") and s.get("combined"):
            s["held"] = True
    broadcast("session_stopped", {"session_id": sid, "ts": _ts()})
    return jsonify({"status": "ok", "session_id": sid, "stopped": True})

@chat_app.route("/start_result", methods=["POST"])
@require_login
def start_result():
    sid = _sid_from_request()
    if not sid:
        return jsonify({"status": "error", "message": "missing session_id"}), 400
    with _cmd_sessions_lock:
        s = _cmd_sessions.get(sid)
        if not s:
            return jsonify({"status": "error", "message": "session not found"}), 404
        s["stopped"] = False
        held     = s.get("held")
        combined = s.get("combined")
        if held:
            s["held"] = False
    released = bool(held and combined)
    if released:
        broadcast("cmd_result", {"command": sid, "result": combined, "ts": _ts()})
        threading.Thread(target=_forward_to_ai_stream, args=(combined,), daemon=True).start()
    broadcast("session_started", {"session_id": sid, "ts": _ts()})
    return jsonify({"status": "ok", "session_id": sid, "stopped": False, "released": released})

@chat_app.route("/stopd_result", methods=["GET"])
@require_login
def stopd_result():
    with _cmd_sessions_lock:
        held = [sid for sid, s in _cmd_sessions.items() if s.get("held")]
    if not held:
        return jsonify({"no session stoped": True})
    return jsonify({"session_id": held})

@chat_app.route("/clear_task", methods=["POST"])
@require_login
def clear_task():
    """Clear all finished tasks — sessions that completed or errored. Held
    sessions (stopped, awaiting the user's start decision) are kept."""
    removed = []
    with _cmd_sessions_lock:
        for sid in list(_cmd_sessions):
            s = _cmd_sessions[sid]
            if s.get("complete") and not s.get("held"):
                del _cmd_sessions[sid]
                removed.append(sid)
    return jsonify({"status": "ok", "cleared": removed, "count": len(removed)})

# ══════════════════════════════════════════════════════════════════
#  COMMANDS LIST
# ══════════════════════════════════════════════════════════════════
def _load_commands():
    if not os.path.exists(COMMANDS_FILE):
        default = [
            {"prefix": "audioapi:",    "color": "#29ecff", "label": "Audio API",    "timeout": True},
            {"prefix": "os_command:",  "color": "#4dffaa", "label": "OS Command",   "timeout": True},
            {"prefix": "internetapi:", "color": "#ffc200", "label": "Internet API", "timeout": True},
            {"prefix": "reconweb:",    "color": "#a78bfa", "label": "Recon Web",    "timeout": True},
            {"prefix": "subdomains:",  "color": "#ff6b6b", "label": "Subdomains",   "timeout": True},
            {"prefix": "tts:",         "color": "#ff9f43", "label": "TTS",          "timeout": True},
        ]
        with open(COMMANDS_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        return default
    with open(COMMANDS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_commands(data):
    with open(COMMANDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

@chat_app.route("/api/commands", methods=["GET"])
@require_login
def get_commands():
    return jsonify(_load_commands())

@chat_app.route("/api/commands", methods=["POST"])
@require_login
def set_commands():
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({"status": "error", "message": "Expected a JSON array"}), 400
    _save_commands(data)
    broadcast("commands_updated", {"commands": data, "ts": _ts()})
    return jsonify({"status": "ok", "count": len(data)})

# ══════════════════════════════════════════════════════════════════
#  NETWORK CONFIG
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/network", methods=["GET"])
@require_login
def network_get():
    return jsonify({
        "llm_host":   LLM_TCP_HOST,
        "llm_tcp":    LLM_TCP_PORT,
        "llm_http":   LLM_HTTP_PORT,
        "llm_scheme": LLM_SCHEME,
        "sys_host":   SYSTEMAPI_HOST,
        "sys_port":   SYSTEMAPI_PORT,
        "audio_port": LISTEN_PORT,
    })

@chat_app.route("/api/network", methods=["POST"])
@require_login
def network_set():
    global LLM_TCP_HOST, LLM_TCP_PORT, LLM_HTTP_HOST, LLM_HTTP_PORT, LLM_SCHEME
    global SYSTEMAPI_HOST, SYSTEMAPI_PORT, LISTEN_PORT
    body = request.get_json(force=True) or {}
    try:
        LLM_TCP_HOST   = body.get("llm_host",  LLM_TCP_HOST)
        LLM_HTTP_HOST  = body.get("llm_host",  LLM_HTTP_HOST)
        LLM_TCP_PORT   = int(body.get("llm_tcp",   LLM_TCP_PORT))
        LLM_HTTP_PORT  = int(body.get("llm_http",  LLM_HTTP_PORT))
        LLM_SCHEME     = body.get("llm_scheme", LLM_SCHEME)
        SYSTEMAPI_HOST = body.get("sys_host",  SYSTEMAPI_HOST)
        SYSTEMAPI_PORT = int(body.get("sys_port",  SYSTEMAPI_PORT))
        _save_server_settings()
        return jsonify({"status": "ok", "message": "Network config updated"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

# ══════════════════════════════════════════════════════════════════
#  SESSIONS
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/sessions", methods=["GET"])
@require_login
def sessions_list():
    ok, d = _llm_get("/api/llm/sessions")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/sessions/save", methods=["POST"])
@require_login
def sessions_save():
    body = request.get_json(force=True) or {}
    ok, d = _llm_post("/api/llm/sessions/save", body)
    if ok:
        _set_current_session((body.get("name") or "").strip())
        broadcast("session_saved", {"ts": _ts(), **d})
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/sessions/load", methods=["POST"])
@require_login
def sessions_load():
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()

    # Reloading the session that currently owns the in-flight generation must
    # NOT cancel it (compare against the *generating* session, not the displayed
    # one — they differ when a background reply is streaming). If that session is
    # also the one on screen, leave the live stream completely untouched: skip
    # the reload + history re-broadcast so the streaming bubble keeps rendering
    # the *same* generation instead of restarting it. If it is generating in the
    # background, fall through to load its saved history and re-attach the stream
    # (generation events are session-tagged, so they resume rendering on screen).
    if name and _get_generating() and name == _get_generating_session() \
            and name == _get_current_session():
        return jsonify({
            "status":  "ok",
            "message": f"Session '{name}' is still generating — continuing.",
            "noop":    True,
        }), 200

    ok, d = _llm_post("/api/llm/sessions/load", body)
    if ok:
        _set_current_session(name)
        history = d.get("history", [])
        broadcast("session_history",
                  {"history": [{**msg, "content": _strip_timestamp(msg.get("content", ""))} for msg in history],
                   "session": name, "ts": _ts()})
        broadcast("session_loaded", {"session": name, "ts": _ts(), "message": d.get("message", "")})
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/sessions/new", methods=["POST"])
@require_login
def sessions_new():
    ok, d = _llm_post("/api/llm/sessions/new", request.get_json(force=True) or {})
    if ok:
        _set_current_session(None)
        _set_generating_session(None)
        broadcast("session_new", {"session": None, "ts": _ts(), **d})
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/sessions/delete", methods=["POST"])
@require_login
def sessions_delete():
    ok, d = _llm_post("/api/llm/sessions/delete", request.get_json(force=True))
    if ok: broadcast("session_deleted", {"ts": _ts(), **d})
    return jsonify(d), (200 if ok else 502)

# ══════════════════════════════════════════════════════════════════
#  LLM SETTINGS / PROMPT / HISTORY
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/llm/settings", methods=["GET"])
@require_login
def llm_settings_get():
    ok, d = _llm_get("/api/llm/settings")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/settings", methods=["POST"])
@require_login
def llm_settings_post():
    ok, d = _llm_post("/api/llm/settings", request.get_json(force=True))
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/settings/reset", methods=["POST"])
@require_login
def llm_settings_reset():
    ok, d = _llm_post("/api/llm/settings/reset")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/prompt", methods=["GET"])
@require_login
def llm_prompt():
    ok, d = _llm_get("/api/llm/prompt")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/raw", methods=["POST"])
@require_login
def llm_raw():
    body = request.get_json(force=True)
    ok, d = _llm_post("/api/llm/raw", body)
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/history", methods=["GET"])
@require_login
def llm_history():
    ok, d = _llm_get("/api/llm/history")
    if ok and "history" in d:
        d["history"] = [{**msg, "content": _strip_timestamp(msg.get("content", ""))} for msg in d["history"]]
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/history/clear", methods=["POST"])
@require_login
def llm_history_clear():
    ok, d = _llm_post("/api/llm/history/clear")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/sessions/clear", methods=["POST"])
@require_login
def sessions_clear():
    ok, d = _llm_post("/api/llm/sessions/clear", request.get_json(force=True))
    if ok:
        broadcast("session_cleared", {"ts": _ts(), **d})
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/reload", methods=["POST"])
@require_login
def llm_reload():
    ok, d = _llm_post("/api/llm/reload")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/delete_msg", methods=["POST"])
@require_login
def llm_delete_msg():
    body = request.get_json(force=True) or {}
    ok, d = _llm_post("/api/llm/delete_msg", body)
    if ok and d.get("status") == "ok":
        broadcast("msg_deleted", {"id": body.get("id"), "ts": _ts(), **d})
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/edit_msg", methods=["POST"])
@require_login
def llm_edit_msg():
    body = request.get_json(force=True) or {}
    ok, d = _llm_post("/api/llm/edit_msg", body)
    if ok and d.get("status") == "ok":
        broadcast("msg_edited", {"id": body.get("id"), "content": body.get("content"), "ts": _ts(), **d})
    return jsonify(d), (200 if ok else 502)

# ══════════════════════════════════════════════════════════════════
#  LOAD MODEL + MODEL STATUS
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/llm/load-model", methods=["POST"])
@require_login
def llm_load_model():
    body = request.get_json(force=True) or {}
    path = body.get("path", "").strip()
    if not path:
        return jsonify({"status": "error", "message": "path is required"}), 400
    ok, d = _llm_post("/api/llm/load-model", {"path": path}, timeout=15)
    if ok:
        broadcast("model_loading", {"path": path, "ts": _ts()})
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/model-status", methods=["GET"])
@require_login
def llm_model_status():
    ok, d = _llm_get("/api/llm/model-status")
    # llm-server answers HTTP 400 with a FULL status body when the last load
    # failed (e.g. out of memory) — that's a valid status payload, not a
    # transport failure. Pass it through as 200 so the browser's poll can read
    # d.status === "error" and d.error instead of throwing on a non-2xx code.
    if isinstance(d, dict) and d.get("status"):
        return jsonify(d), 200
    return jsonify(d), (200 if ok else 502)

# ══════════════════════════════════════════════════════════════════
#  SAVED MODELS  (proxy to llm_server)
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/saved-models", methods=["GET"])
@require_login
def saved_models_get():
    ok, d = _llm_get("/api/llm/saved-models")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/saved-models", methods=["POST"])
@require_login
def saved_models_post():
    body = request.get_json(force=True) or {}
    ok, d = _llm_post("/api/llm/saved-models", body)
    return jsonify(d), (200 if ok else (404 if d.get("message", "").startswith("Model file not found") else 502))

@chat_app.route("/api/saved-models/delete", methods=["POST"])
@require_login
def saved_models_delete():
    body = request.get_json(force=True) or {}
    ok, d = _llm_post("/api/llm/saved-models/delete", body)
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/saved-models/toggle-autosave", methods=["POST"])
@require_login
def saved_models_toggle_autosave():
    ok, d = _llm_post("/api/llm/saved-models/toggle-autosave")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/saved-models/set-thinking", methods=["POST"])
@require_login
def saved_models_set_thinking():
    body = request.get_json(force=True) or {}
    ok, d = _llm_post("/api/llm/saved-models/set-thinking", body)
    return jsonify(d), (200 if ok else (404 if "not found" in d.get("message", "").lower() else 502))

# ══════════════════════════════════════════════════════════════════
#  PROMPT CACHE
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/llm/cache-prompt", methods=["GET"])
@require_login
def cache_prompt_get():
    ok, d = _llm_get("/api/llm/cache-prompt")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/cache-prompt", methods=["POST"])
@require_login
def cache_prompt_set():
    ok, d = _llm_post("/api/llm/cache-prompt", request.get_json(force=True))
    return jsonify(d), (200 if ok else 502)

# ══════════════════════════════════════════════════════════════════
#  DEVELOPER PANEL ROUTES
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/dev/config", methods=["GET"])
@require_login
def dev_config_get():
    ok, d = _llm_get("/api/dev/config")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/dev/config", methods=["POST"])
@require_login
def dev_config_post():
    ok, d = _llm_post("/api/dev/config", request.get_json(force=True))
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/dev/eject", methods=["POST"])
@require_login
def dev_eject():
    ok, d = _llm_post("/api/dev/eject")
    if ok:
        broadcast("model_ejected", {"ts": _ts(), "message": d.get("message", "")})
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/dev/trim_history", methods=["POST"])
@require_login
def dev_trim_history():
    ok, d = _llm_post("/api/dev/trim_history", request.get_json(force=True) or {})
    return jsonify(d), (200 if ok else 502)

# ══════════════════════════════════════════════════════════════════
#  GPU SYSTEM
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/gpus/status", methods=["GET"])
@require_login
def gpus_status():
    ok, d = _llm_get("/gpus/status")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/gpus/flow", methods=["GET"])
@require_login
def gpus_flow_get():
    ok, d = _llm_get("/gpus/flow")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/gpus/flow", methods=["POST"])
@require_login
def gpus_flow_post():
    body = request.get_json(force=True) or {}
    ok, d = _llm_post("/gpus/flow", body)
    if ok and d.get("status") == "reloading":
        broadcast("model_loading", {"ts": _ts(), "message": d.get("message", "")})
    if not ok and d.get("status") == "error":
        return jsonify(d), 400
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/gpus/auto_flow", methods=["POST"])
@require_login
def gpus_auto_flow():
    body = request.get_json(force=True) or {}
    ok, d = _llm_post("/gpus/auto_flow", body)
    if ok and d.get("status") == "reloading":
        broadcast("model_loading", {"ts": _ts(), "message": d.get("message", "")})
    if not ok and d.get("status") == "error":
        return jsonify(d), 400
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/model/gpus/drive", methods=["POST"])
@require_login
def model_gpus_drive():
    body = request.get_json(force=True) or {}
    ok, d = _llm_post("/model/gpus/drive", body, timeout=15)
    if ok:
        broadcast("model_loading", {"ts": _ts(), "message": d.get("message", "")})
    if not ok and d.get("status") == "error":
        return jsonify(d), 400
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/model/restart", methods=["POST"])
@require_login
def model_restart():
    ok, d = _llm_post("/model/restart")
    if ok:
        broadcast("model_loading", {"ts": _ts(), "message": d.get("message", "")})
    return jsonify(d), (200 if ok else 502)

# ══════════════════════════════════════════════════════════════════
#  MULTI-MODEL  (load many at once, switch / eject by index)
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/loaded_model", methods=["GET"])
@require_login
def loaded_model_get():
    ok, d = _llm_get("/loaded_model")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/active_model", methods=["GET"])
@require_login
def active_model_get():
    ok, d = _llm_get("/active_model")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/active_model", methods=["POST"])
@require_login
def active_model_post():
    body = request.get_json(silent=True) or {}
    idx  = body.get("index", request.args.get("index"))
    if idx is None or str(idx).strip() == "":
        return jsonify({"status": "error", "message": "index is required"}), 400
    ok, d = _llm_post(f"/active_model?index={idx}")
    if ok:
        broadcast("active_model_changed", {"ts": _ts(), **d})
        return jsonify(d), 200
    if d.get("status") == "error":
        return jsonify(d), (404 if "no model" in d.get("message", "").lower() else 400)
    return jsonify(d), 502

@chat_app.route("/api/eject_model", methods=["POST"])
@require_login
def eject_model_post():
    body = request.get_json(silent=True) or {}
    idx  = body.get("index", request.args.get("index"))
    if idx is None or str(idx).strip() == "":
        return jsonify({"status": "error", "message": "index is required"}), 400
    ok, d = _llm_post(f"/eject_model?index={idx}")
    if ok:
        broadcast("model_ejected", {"ts": _ts(), "message": d.get("message", "")})
        return jsonify(d), 200
    if d.get("status") == "error":
        return jsonify(d), (404 if "no model" in d.get("message", "").lower() else 400)
    return jsonify(d), 502

# ══════════════════════════════════════════════════════════════════
#  MULTI-GPU PLACEMENT CONFIG  (requires 2+ GPUs — llm-server returns 400 otherwise)
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/default_gpu", methods=["GET"])
@require_login
def default_gpu_get():
    ok, d = _llm_get("/default_gpu")
    if not ok and d.get("status") == "error":
        return jsonify(d), 400
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/default_gpu", methods=["POST"])
@require_login
def default_gpu_post():
    ok, d = _llm_post("/default_gpu", request.get_json(force=True) or {})
    if not ok and d.get("status") == "error":
        return jsonify(d), 400
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/multi_gpu_settings", methods=["GET"])
@require_login
def multi_gpu_settings_get():
    ok, d = _llm_get("/multi_gpu_settings")
    if not ok and d.get("status") == "error":
        return jsonify(d), 400
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/multi_gpu_settings", methods=["POST"])
@require_login
def multi_gpu_settings_post():
    ok, d = _llm_post("/multi_gpu_settings", request.get_json(force=True) or {})
    if not ok and d.get("status") == "error":
        return jsonify(d), 400
    return jsonify(d), (200 if ok else 502)

# ══════════════════════════════════════════════════════════════════
#  PROMPT MANAGEMENT
# ══════════════════════════════════════════════════════════════════
@chat_app.route("/api/llm/stop-prompt", methods=["POST"])
@require_login
def llm_stop_prompt():
    ok, d = _llm_post("/api/llm/stop-prompt")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/reset-prompt", methods=["POST"])
@require_login
def llm_reset_prompt():
    ok, d = _llm_post("/api/llm/reset-prompt")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/new-prompt", methods=["POST"])
@require_login
def llm_new_prompt():
    ok, d = _llm_post("/api/llm/new-prompt", request.get_json(force=True) or {})
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/delete-prompt", methods=["POST"])
@require_login
def llm_delete_prompt():
    ok, d = _llm_post("/api/llm/delete-prompt", request.get_json(force=True) or {})
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/edit-prompt", methods=["POST"])
@require_login
def llm_edit_prompt():
    ok, d = _llm_post("/api/llm/edit-prompt", request.get_json(force=True) or {})
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/use-prompt", methods=["GET"])
@require_login
def llm_use_prompt():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name query param is required"}), 400
    ok, d = _llm_get(f"/api/llm/use-prompt?name={name}")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/get-prompt", methods=["GET"])
@require_login
def llm_get_prompt():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name query param is required"}), 400
    ok, d = _llm_get(f"/api/llm/get-prompt?name={name}")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/get-promptn", methods=["GET"])
@require_login
def llm_get_promptn():
    ok, d = _llm_get("/api/llm/get-promptn")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/api-docs", methods=["GET"])
@require_login
def llm_api_docs():
    ok, d = _llm_get("/api/llm/api-docs")
    return jsonify(d), (200 if ok else 502)

@chat_app.route("/api/llm/save-thinking", methods=["POST"])
@require_login
def llm_save_thinking():
    body = request.get_json(force=True) or {}
    ok, d = _llm_post("/api/llm/save-thinking", body)
    return jsonify(d), (200 if ok else 502)

# ══════════════════════════════════════════════════════════════════
#  TCP LISTENER  (AI responses on port 8801)
# ══════════════════════════════════════════════════════════════════
def _ai_listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", LISTEN_PORT))
    srv.listen(10)
    print(f"[listener] Waiting for AI responses on :{LISTEN_PORT}")
    while True:
        try:
            conn, addr = srv.accept()
            chunks = []
            while True:
                chunk = conn.recv(4096)
                if not chunk: break
                chunks.append(chunk)
            conn.close()
            data = b"".join(chunks).decode("utf-8", errors="replace").strip()
            if not data:
                continue
            try:
                pkt = json.loads(data)
                if isinstance(pkt, dict) and pkt.get("__type__") == "session_history":
                    broadcast("session_history", {"history": pkt["history"], "ts": _ts()})
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            clean = data
            while "<think>" in clean and "</think>" in clean:
                start = clean.find("<think>")
                end   = clean.find("</think>") + len("</think>")
                clean = (clean[:start] + clean[end:]).strip()
            if clean:
                broadcast("ai_message", {"text": clean, "ts": _ts()})
        except Exception as e:
            print(f"[listener] Error: {e}")

# ══════════════════════════════════════════════════════════════════
#  ENTRY
# ══════════════════════════════════════════════════════════════════

# ==========================================================================
#  SECTION 2 — PROMPT-MGR (Flask app: prompt_app)
# ==========================================================================

import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_from_directory, Response, make_response

# ── Added for fast HTTP connection pooling ─────────────────────────────────
import requests
from requests.adapters import HTTPAdapter

# Session with keep-alive for promptapi (reuses TCP connections)
_prompt_session = None
_prompt_session_lock = threading.Lock()


def get_prompt_session():
    global _prompt_session
    if _prompt_session is None:
        with _prompt_session_lock:
            if _prompt_session is None:
                _prompt_session = requests.Session()
                adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
                _prompt_session.mount('http://', adapter)
                _prompt_session.mount('https://', adapter)
    return _prompt_session


# ── Mtime-based in-memory cache (avoids repeated JSON parsing from disk) ───
class _MtimeCache:
    """Caches file content; reloads only when the file's mtime changes."""
    __slots__ = ("_lock", "_mtime", "_data")

    def __init__(self):
        self._lock = threading.Lock()
        self._mtime = -1.0
        self._data = None

    def get(self, path: str):
        """Returns (hit: bool, data). Fast path — no file I/O."""
        try:
            mtime = os.path.getmtime(path)
        except FileNotFoundError:
            return False, None
        with self._lock:
            if self._mtime == mtime and self._data is not None:
                return True, self._data
        return False, None

    def set(self, path: str, data):
        try:
            mtime = os.path.getmtime(path)
        except FileNotFoundError:
            mtime = -1.0
        with self._lock:
            self._mtime = mtime
            self._data = data


_services_cache = _MtimeCache()
_config_cache = _MtimeCache()
_prompt_cache = _MtimeCache()
_blockes_cache = _MtimeCache()
# ── End of added code ─────────────────────────────────────────────────────

# ── Config ─────────────────────────────────────────────────────────────────
SERVICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "services.json")
# Config now lives in the single merged settings file shared with the chat app
# (formerly config.json — merged into backend_servers.json).
CONFIG_FILE = SERVER_SETTINGS_FILE
PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.json")
BLOCKES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apis", "file_blocks.json")
UI_FILE = "api _manager.html"

# Pages/assets this bridge is allowed to serve alongside the main UI.
STATIC_PAGES = {"api _manager.html", "prompt_manager.html", "server_settings.html", "net_config.js"}

# Thread pool for network operations (TCP / HTTP)
_net_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="netpool")

# Lock to guard the files against concurrent read/write
_services_lock = threading.RLock()
_config_lock = threading.RLock()
_prompt_lock = threading.RLock()
_blockes_lock = threading.RLock()

# Default config (used if backend_servers.json doesn't exist yet).
# For both llm_server and promptapi: "port" is the TCP port (raw commands) and
# "http_port" is the REST port. The live Prompt API address is discovered via
# the llm-server's GET /server_settings (see _resolve_promptapi); these values
# are the fallback used only when discovery fails.
DEFAULT_CONFIG = {
    "llm_server": {"host": "127.0.0.1", "port": 8111, "http_port": 8112},
    "systemapi": {"host": "127.0.0.1", "port": 8300},
    "promptapi": {"host": "127.0.0.1", "port": 8202, "http_port": 8203},
}

DEFAULT_BLOCKES = {
    "readfile": {"block": False},
    "writefile": {"block": False},
    "delete": {"block": False},
    "addfile": {"block": False},
    "listfile": {"block": False},
    "get_folders": {"block": False},
    "extFiles": {"block": False},
    "size": {"block": False},
    "editline": {"block": False},
    "deleteline": {"block": False},
    "startwithline": {"block": False},
    "edit_with-line":{"block":False},
    "read_from":{"block":False},
}

prompt_app = Flask(__name__)


# ══════════════════════════════════════════════════════════════════════════
#  CORS — full handling of every request, including the OPTIONS preflight
# ══════════════════════════════════════════════════════════════════════════
def _cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


@prompt_app.before_request
def handle_preflight():
    """Handle the OPTIONS preflight before any route (this was the cause of the 500)."""
    if request.method == "OPTIONS":
        res = make_response("", 200)
        return _cors_headers(res)


@prompt_app.after_request
def add_cors(response):
    return _cors_headers(response)


# ── Serve UI ───────────────────────────────────────────────────────────────
@prompt_app.route("/")
def prompt_index():
    return send_from_directory(".", UI_FILE)


@prompt_app.route("/<path:page>")
def serve_page(page):
    """Serve the companion pages (prompt manager, server settings, shared JS).
    Fixed API routes take precedence over this catch-all in werkzeug routing."""
    if page in STATIC_PAGES:
        return send_from_directory(".", page)
    return jsonify({"status": "error", "message": "not found"}), 404


# ══════════════════════════════════════════════════════════════════════════
#  SERVICES
# ══════════════════════════════════════════════════════════════════════════
def load_services() -> dict:
    hit, data = _services_cache.get(SERVICES_FILE)
    if hit:
        return data or {}
    with _services_lock:
        hit, data = _services_cache.get(SERVICES_FILE)
        if hit:
            return data or {}
        if not os.path.exists(SERVICES_FILE):
            return {}
        try:
            with open(SERVICES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[services] load error: {e}")
            data = {}
        _services_cache.set(SERVICES_FILE, data)
        return data


def save_services(data: dict):
    with _services_lock:
        with open(SERVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _services_cache.set(SERVICES_FILE, data)


@prompt_app.route("/api/services", methods=["GET"])
def get_services():
    return jsonify(load_services())


@prompt_app.route("/api/services", methods=["POST"])
def set_services():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "Expected a JSON object"}), 400
    save_services(data)
    return jsonify({"status": "ok", "count": len(data)})


@prompt_app.route("/api/service/add", methods=["POST"])
def service_add():
    body = request.get_json(force=True)
    key = body.get("key", "").strip()
    ip = body.get("ip", "").strip()
    port = body.get("port")

    if not key or not ip or not port:
        return jsonify({"status": "error", "message": "key, ip, port required"}), 400
    if not key.endswith(":"):
        key += ":"

    svcs = load_services()
    if key in svcs:
        return jsonify({"status": "error", "message": f"'{key}' already exists"}), 409

    braces = bool(body.get("braces", False))
    svcs[key] = {"ip": ip, "port": int(port), "Block": False, "Braces": braces}
    save_services(svcs)

    _net_executor.submit(_send_tcp, f"add_service:{key.rstrip(':')}:{ip}:{port}")

    return jsonify({"status": "ok", "key": key, "ip": ip, "port": int(port), "Block": False, "Braces": braces})


@prompt_app.route("/api/service/add/Available_access", methods=["POST"])
def service_add_available_access():
    body = request.get_json(force=True)
    key = body.get("key", "").strip()
    ip = body.get("ip", "").strip()
    port = body.get("port")

    if not key or not ip or not port:
        return jsonify({"status": "error", "message": "key, ip, port required"}), 400
    if not key.endswith(":"):
        key += ":"

    svcs = load_services()
    if key in svcs:
        return jsonify({"status": "error", "message": f"'{key}' already exists"}), 409

    svcs[key] = {"ip": ip, "port": int(port), "Available_access": True}
    save_services(svcs)
    _net_executor.submit(_send_tcp, f"add_service:{key.rstrip(':')}:{ip}:{port}")

    return jsonify({"status": "ok", "key": key, "ip": ip, "port": int(port), "Available_access": True})


@prompt_app.route("/api/service/edit", methods=["POST"])
def service_edit():
    body = request.get_json(force=True)
    key = body.get("key", "").strip()
    ip = body.get("ip", "").strip()
    port = body.get("port")

    if not key.endswith(":"):
        key += ":"

    svcs = load_services()
    if key not in svcs:
        return jsonify({"status": "error", "message": f"'{key}' not found"}), 404

    if ip:   svcs[key]["ip"] = ip
    if port: svcs[key]["port"] = int(port)
    if "braces" in body: svcs[key]["Braces"] = bool(body["braces"])
    save_services(svcs)

    _net_executor.submit(_send_tcp, f"edit_service:{key.rstrip(':')}:{svcs[key]['ip']}:{svcs[key]['port']}")

    return jsonify({"status": "ok", "key": key, **svcs[key]})


@prompt_app.route("/api/service/remove", methods=["POST"])
def service_remove():
    body = request.get_json(force=True)
    key = body.get("key", "").strip()
    if not key.endswith(":"):
        key += ":"

    svcs = load_services()
    if key not in svcs:
        return jsonify({"status": "error", "message": f"'{key}' not found"}), 404

    del svcs[key]
    save_services(svcs)
    _net_executor.submit(_send_tcp, f"remove_service:{key.rstrip(':')}")

    return jsonify({"status": "ok", "removed": key})


@prompt_app.route("/api/service/test", methods=["POST"])
def service_test():
    body = request.get_json(force=True)
    key = body.get("key", "").strip()
    if not key.endswith(":"):
        key += ":"

    svcs = load_services()
    if key not in svcs:
        return jsonify({"status": "error", "message": f"'{key}' not found"}), 404

    svc = svcs[key]
    reachable, msg = _ping_service(svc["ip"], svc["port"])
    return jsonify({"status": "ok" if reachable else "error",
                    "reachable": reachable,
                    "message": msg})


@prompt_app.route("/api/service/block", methods=["POST"])
def service_block():
    body = request.get_json(force=True)
    key = body.get("key", "").strip()
    if not key.endswith(":"):
        key += ":"

    svcs = load_services()
    if key not in svcs:
        return jsonify({"status": "error", "message": f"'{key}' not found"}), 404

    if "block" in body:
        new_state = bool(body["block"])
    else:
        new_state = not svcs[key].get("Block", False)

    svcs[key]["Block"] = new_state
    save_services(svcs)

    action = "blocked" if new_state else "unblocked"
    return jsonify({"status": "ok", "key": key, "Block": new_state, "action": action})


@prompt_app.route("/api/send", methods=["POST"])
def send_command():
    body = request.get_json(force=True)
    cmd = body.get("command", "").strip()
    if not cmd:
        return jsonify({"status": "error", "message": "command is empty"}), 400

    cfg = load_config()
    host = cfg["systemapi"]["host"]
    port = cfg["systemapi"]["port"]
    success, msg = _send_tcp(cmd, host, port)
    return jsonify({"status": "ok" if success else "error",
                    "message": msg,
                    "target": f"{host}:{port}"}), (200 if success else 502)


# ══════════════════════════════════════════════════════════════════════════
#  CONFIG  (backend_servers.json — merged, formerly config.json)
# ══════════════════════════════════════════════════════════════════════════
def load_config() -> dict:
    hit, data = _config_cache.get(CONFIG_FILE)
    if hit:
        return data or DEFAULT_CONFIG.copy()
    with _config_lock:
        hit, data = _config_cache.get(CONFIG_FILE)
        if hit:
            return data or DEFAULT_CONFIG.copy()
        if not os.path.exists(CONFIG_FILE):
            return DEFAULT_CONFIG.copy()
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            # nested keys aren't merged by the loop above — older settings
            # files predate llm_server.http_port
            if isinstance(data.get("llm_server"), dict):
                data["llm_server"].setdefault("http_port", DEFAULT_CONFIG["llm_server"]["http_port"])
        except Exception as e:
            print(f"[config] load error: {e}")
            data = DEFAULT_CONFIG.copy()
        _config_cache.set(CONFIG_FILE, data)
        return data


def save_config(data: dict):
    with _config_lock:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _config_cache.set(CONFIG_FILE, data)


@prompt_app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@prompt_app.route("/api/config", methods=["POST"])
def set_config():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "Expected a JSON object"}), 400

    required = ["llm_server", "systemapi", "promptapi"]
    for key in required:
        if key not in data:
            return jsonify({"status": "error", "message": f"Missing key: {key}"}), 400
        if "host" not in data[key] or "port" not in data[key]:
            return jsonify({"status": "error", "message": f"{key} needs host and port"}), 400

    save_config(data)
    _invalidate_promptapi_cache()
    return jsonify({"status": "ok", "config": data})


# ══════════════════════════════════════════════════════════════════════════
#  PROMPT  (system_prompt.json)
# ══════════════════════════════════════════════════════════════════════════
def load_prompt() -> dict:
    hit, data = _prompt_cache.get(PROMPT_FILE)
    if hit:
        return data or {}
    with _prompt_lock:
        hit, data = _prompt_cache.get(PROMPT_FILE)
        if hit:
            return data or {}
        if not os.path.exists(PROMPT_FILE):
            return {}
        try:
            with open(PROMPT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[prompt] load error: {e}")
            data = {}
        _prompt_cache.set(PROMPT_FILE, data)
        return data


def save_prompt(data: dict):
    with _prompt_lock:
        with open(PROMPT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        _prompt_cache.set(PROMPT_FILE, data)


# ── Prompt API auto-discovery via llm-server GET /server_settings ──────────
_papi_resolve_lock = threading.Lock()
_papi_resolve_cache = {"ts": 0.0, "value": None}
_PAPI_RESOLVE_TTL = 15.0


def _invalidate_promptapi_cache():
    with _papi_resolve_lock:
        _papi_resolve_cache["ts"] = 0.0
        _papi_resolve_cache["value"] = None


def _resolve_promptapi():
    """Resolve the Prompt API (host, port).

    Preferred source: the llm-server's public GET /server_settings — the
    merged server runs both services in one process, so the Prompt API host
    equals the llm host and its port is the response's "prompt-api_HTTP".
    Falls back to backend_servers.json's promptapi entry when discovery fails.
    Result is cached for _PAPI_RESOLVE_TTL seconds."""
    now = time.time()
    with _papi_resolve_lock:
        if _papi_resolve_cache["value"] and (now - _papi_resolve_cache["ts"]) < _PAPI_RESOLVE_TTL:
            return _papi_resolve_cache["value"]

    cfg = load_config()
    llm = cfg.get("llm_server", {})
    host = llm.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    http_port = llm.get("http_port") or DEFAULT_CONFIG["llm_server"]["http_port"]

    resolved = None
    try:
        session = get_prompt_session()
        resp = session.get(f"http://{host}:{http_port}/server_settings", timeout=2)
        if resp.status_code == 200:
            port = int(resp.json()["prompt-api_HTTP"])
            resolved = (host, port)
    except Exception as e:
        print(f"[promptapi] /server_settings discovery failed: {e}")

    if resolved is None:
        # Fallback: the saved promptapi entry. _try_promptapi talks HTTP, so use
        # the HTTP port (http_port); tolerate the older schema where "port" held
        # the HTTP port.
        papi = cfg.get("promptapi", {})
        resolved = (papi.get("host", "127.0.0.1"),
                    papi.get("http_port") or papi.get("port") or 8203)

    with _papi_resolve_lock:
        _papi_resolve_cache["ts"] = time.time()
        _papi_resolve_cache["value"] = resolved
    return resolved


# ── Replaced function: Using requests.Session for connection pooling ───────
def _try_promptapi(method: str, path: str, body=None, timeout=2):
    host, port = _resolve_promptapi()
    url = f"http://{host}:{port}{path}"

    try:
        session = get_prompt_session()
        if method == "GET":
            resp = session.get(url, timeout=timeout)
        else:
            resp = session.post(url, json=body, timeout=timeout)
        return resp.status_code == 200, resp.json() if resp.content else {}
    except Exception as e:
        print(f"[promptapi] {method} {url}: {e}")
        return False, {}


# ── End of replaced function ─────────────────────────────────────────────

@prompt_app.route("/api/prompt/get", methods=["GET"])
def prompt_get():
    ok, data = _try_promptapi("GET", "/api/prompt")
    if ok and data:
        return jsonify(data)
    return jsonify({"data": load_prompt()})


@prompt_app.route("/api/prompt/add", methods=["POST"])
def prompt_add():
    body = request.get_json(force=True)
    key = body.get("key", "").strip()
    section = body.get("section", {})
    if not key:
        return jsonify({"status": "error", "message": "key required"}), 400

    # 1. Save locally immediately (fast)
    data = load_prompt()
    data[key] = section
    save_prompt(data)

    # 2. Send to promptapi in background (async - does not block response)
    _net_executor.submit(_try_promptapi, "POST", "/api/prompt/add",
                         {"key": key, "section": section})

    # 3. Return immediately to user (injection happens asynchronously)
    return jsonify({"status": "ok", "key": key, "sync": "async"})


@prompt_app.route("/api/prompt/del", methods=["POST"])
def prompt_del():
    body = request.get_json(force=True)
    key = body.get("key", "").strip()
    if not key:
        return jsonify({"status": "error", "message": "key required"}), 400

    # Delete locally immediately
    data = load_prompt()
    removed = key in data
    data.pop(key, None)
    save_prompt(data)

    # Delete in background (async - does not block response)
    _net_executor.submit(_try_promptapi, "POST", "/api/prompt/del", {"key": key})

    return jsonify({"status": "ok", "removed": removed})


@prompt_app.route("/api/prompt/set", methods=["POST"])
def prompt_set():
    body = request.get_json(force=True)
    data = body.get("data", body)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "Expected a JSON object"}), 400

    # Save locally immediately
    save_prompt(data)

    # Update in background (async - does not block response)
    _net_executor.submit(_try_promptapi, "POST", "/api/prompt/set", {"data": data})

    return jsonify({"status": "ok", "count": len(data)})

@prompt_app.route("/api/prompt/block", methods=["POST"])
def prompt_block():
    body = request.get_json(force=True)
    key  = body.get("key", "").strip()
    blk  = body.get("block")
    if not key or blk is None:
        return jsonify({"status": "error", "message": "key and block required"}), 400

    data = load_prompt()
    if key not in data:
        return jsonify({"status": "error", "message": f"'{key}' not found"}), 404
    data[key]["block"] = bool(blk)
    save_prompt(data)

    _net_executor.submit(_try_promptapi, "POST", "/api/prompt/block",
                         {"key": key, "block": bool(blk)})

    action = "blocked" if blk else "unblocked"
    return jsonify({"status": "ok", "key": key, "block": bool(blk), "action": action})

# ══════════════════════════════════════════════════════════════════════════
#  LLM
# ══════════════════════════════════════════════════════════════════════════
def _llm_host_port():
    cfg = load_config()
    host = cfg["llm_server"]["host"]
    port = cfg["llm_server"]["port"]
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    return host, port


@prompt_app.route("/api/llm/raw", methods=["POST"])
def prompt_llm_raw():
    body = request.get_json(force=True)
    cmd = body.get("command", "").strip()
    if not cmd:
        return jsonify({"status": "error", "message": "command is empty"}), 400

    host, port = _llm_host_port()
    success, msg = _send_tcp(cmd, host, port)
    return jsonify({"status": "ok" if success else "error",
                    "message": msg,
                    "target": f"{host}:{port}"}), (200 if success else 502)


@prompt_app.route("/api/llm/reload", methods=["POST"])
def prompt_llm_reload():
    host, port = _llm_host_port()
    success, msg = _send_tcp("__reload_prompt__", host, port)
    return jsonify({"status": "ok" if success else "error",
                    "message": msg,
                    "target": f"{host}:{port}"}), (200 if success else 502)


# ══════════════════════════════════════════════════════════════════════════
#  FILEAPI BLOCKS  (file_blocks.json)
# ══════════════════════════════════════════════════════════════════════════
def load_blockes() -> dict:
    hit, data = _blockes_cache.get(BLOCKES_FILE)
    if hit:
        return data or DEFAULT_BLOCKES.copy()
    with _blockes_lock:
        hit, data = _blockes_cache.get(BLOCKES_FILE)
        if hit:
            return data or DEFAULT_BLOCKES.copy()
        if not os.path.exists(BLOCKES_FILE):
            save_blockes(DEFAULT_BLOCKES.copy())
            return DEFAULT_BLOCKES.copy()
        try:
            with open(BLOCKES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_BLOCKES.items():
                data.setdefault(k, v)
        except Exception as e:
            print(f"[blockes] load error: {e}")
            data = DEFAULT_BLOCKES.copy()
        _blockes_cache.set(BLOCKES_FILE, data)
        return data


def save_blockes(data: dict):
    with _blockes_lock:
        with open(BLOCKES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _blockes_cache.set(BLOCKES_FILE, data)


@prompt_app.route("/api/fileapi/blocks", methods=["GET"])
def fileapi_blocks_get():
    return jsonify(load_blockes())


@prompt_app.route("/api/fileapi/blocks", methods=["POST"])
def fileapi_blocks_set():
    body = request.get_json(force=True)
    cmd = body.get("cmd", "").strip()
    blk = body.get("block")

    if not cmd or blk is None:
        return jsonify({"status": "error", "message": "cmd and block required"}), 400

    data = load_blockes()

    if cmd not in data:
        data[cmd] = {"block": False}

    data[cmd]["block"] = bool(blk)
    save_blockes(data)
    action = "blocked" if blk else "unblocked"
    print(f"[fileapi/blocks] '{cmd}' → block={blk}")
    return jsonify({"status": "ok", "cmd": cmd, "block": bool(blk), "action": action})


# ══════════════════════════════════════════════════════════════════════════
#  SYSTEMAPI COMMAND BLOCKS  (proxied to systemapi: blocked_commands.json)
# ══════════════════════════════════════════════════════════════════════════
# systemapi owns the OS-command block list and exposes it over its TCP protocol
# via the get_/add_/remove_command_block control commands (see systemapi.py
# handle_control). These endpoints proxy to it through the authenticated
# _systemapi_command path (token auth + full reply), so the browser never has to
# speak systemapi's raw protocol or manage its auth.
def _parse_command_blocks(reply: str) -> list:
    """Parse systemapi's '{0:cmd, 1:cmd2}' reply (optionally prefixed with a
    'systemapi:' framing line) into an ordered list of command strings. The
    entries are 'N:cmd' joined by ', '; split only before a 'digits:' boundary so
    commands that themselves contain commas/colons survive."""
    if reply is None:
        return []
    text = reply.strip()
    if text.lower().startswith("systemapi:"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    text = text.strip()
    if text.startswith("{"):
        text = text[1:]
    if text.endswith("}"):
        text = text[:-1]
    text = text.strip()
    if not text:
        return []
    blocks = []
    for p in _re.split(r',\s*(?=\d+\s*:)', text):
        m = _re.match(r'\s*\d+\s*:\s*(.*)$', p, _re.DOTALL)
        if m:
            blocks.append(m.group(1).strip())
    return blocks


def _sys_blocks_response(reply, err):
    """Map a _systemapi_command (reply, err) result to a Flask JSON response."""
    if err == "connection_refused":
        return jsonify({"status": "error", "message": "systemapi offline"}), 502
    if err is not None:
        return jsonify({"status": "error",
                        "message": "systemapi authentication required"}), 401
    return jsonify({"status": "ok", "blocks": _parse_command_blocks(reply)})


@prompt_app.route("/api/systemapi/login", methods=["POST"])
def systemapi_login_route():
    """Log the bridge in to systemapi with a password supplied by the API Manager.
    On success the token + password are cached in memory (shared with the rest of
    the process via _systemapi_login), so the block endpoints below authenticate."""
    pw = (request.get_json(force=True) or {}).get("password", "")
    if not pw:
        return jsonify({"status": "error", "message": "password required"}), 400
    if not _systemapi_online():
        return jsonify({"status": "error", "message": "systemapi offline"}), 503
    ok, why = _systemapi_login(pw)
    if ok:
        return jsonify({"status": "ok"})
    if why == "wrong_password":
        return jsonify({"status": "error", "message": "wrong password"}), 401
    return jsonify({"status": "error", "message": f"systemapi login {why}"}), 502


@prompt_app.route("/api/systemapi/blocks", methods=["GET"])
def systemapi_blocks_get():
    reply, err = _systemapi_command("get_command_block", timeout=10)
    return _sys_blocks_response(reply, err)


@prompt_app.route("/api/systemapi/blocks/add", methods=["POST"])
def systemapi_blocks_add():
    cmd = (request.get_json(force=True) or {}).get("command", "").strip()
    if not cmd:
        return jsonify({"status": "error", "message": "command is empty"}), 400
    reply, err = _systemapi_command(f"add_command_block:{cmd}", timeout=10)
    print(f"[systemapi/blocks] add '{cmd}' (err={err})")
    return _sys_blocks_response(reply, err)


@prompt_app.route("/api/systemapi/blocks/remove", methods=["POST"])
def systemapi_blocks_remove():
    body = request.get_json(force=True) or {}
    idx  = body.get("index")
    cmd  = (body.get("command") or "").strip()
    if idx is None:
        # Resolve the typed command text to its current index (systemapi removes
        # by index). Read the live list first, then locate the match.
        cur, err = _systemapi_command("get_command_block", timeout=10)
        if err:
            return _sys_blocks_response(cur, err)
        blocks = _parse_command_blocks(cur)
        if cmd not in blocks:
            return jsonify({"status": "error", "message": f"'{cmd}' is not blocked"}), 404
        idx = blocks.index(cmd)
    reply, err = _systemapi_command(f"remove_command_block:{int(idx)}", timeout=10)
    print(f"[systemapi/blocks] remove index={idx} (err={err})")
    return _sys_blocks_response(reply, err)


# ══════════════════════════════════════════════════════════════════════════
#  TCP helpers
# ══════════════════════════════════════════════════════════════════════════
def _send_tcp(command: str, host: str = None, port: int = None, timeout: float = 4.0):
    if host is None or port is None:
        cfg = load_config()
        host = host or cfg["systemapi"]["host"]
        port = port or cfg["systemapi"]["port"]
    target = f"{host}:{port}"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, int(port)))
            s.sendall(command.encode("utf-8"))
            resp = s.recv(1024).decode(errors="ignore")
        return True, resp or "ok"
    except ConnectionRefusedError:
        return False, f"Server is down — nothing listening on {target} (connection refused)"
    except (socket.timeout, TimeoutError):
        return False, f"Server not responding — {target} timed out after {timeout:.0f}s"
    except socket.gaierror as e:
        return False, f"Cannot resolve host '{host}' ({e})"
    except OSError as e:
        return False, f"Network error reaching {target}: {e}"


def _ping_service(ip: str, port: int, timeout: float = 2.0):
    target = f"{ip}:{port}"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((ip, int(port)))
        return True, f"Connected to {target}"
    except ConnectionRefusedError:
        return False, f"Server is down — nothing listening on {target} (connection refused)"
    except (socket.timeout, TimeoutError):
        return False, f"Server not responding — {target} timed out after {timeout:.0f}s"
    except socket.gaierror as e:
        return False, f"Cannot resolve host '{ip}' ({e})"
    except OSError as e:
        return False, f"Network error reaching {target}: {e}"


# ══════════════════════════════════════════════════════════════════════════
#  Entry
# ══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
#  PORTS CONFIG (chat_ports.json) + /server-settings endpoints
# ═══════════════════════════════════════════════════════════════════
import sys

PORTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_ports.json")
DEFAULT_PORTS = {"chat-server": 8900, "prompt-mgr": 8843}


def _save_ports(ports):
    with open(PORTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"chat-server": int(ports["chat-server"]),
                   "prompt-mgr":  int(ports["prompt-mgr"])}, f, indent=2)


def _load_ports():
    """Read the port assignments from chat_ports.json, falling back to the
    defaults for any missing/invalid entry. Materialises the file on first run."""
    ports = dict(DEFAULT_PORTS)
    try:
        with open(PORTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in DEFAULT_PORTS:
            if k in data:
                ports[k] = int(data[k])
    except FileNotFoundError:
        _save_ports(ports)
    except Exception as e:
        print(f"[ports] load error: {e}")
    return ports


# RUNNING_PORTS is the in-memory snapshot of the ports the services are ACTUALLY
# bound to right now. It is captured once at startup. POST /server-settings
# rewrites chat_ports.json but does NOT mutate RUNNING_PORTS — so GET keeps
# returning the live ports until the process restarts and rebinds from the file.
RUNNING_PORTS = _load_ports()


@chat_app.route("/server-settings", methods=["GET"])
def get_server_settings():
    """Public — return ALL settings:
      * the ports the services are currently listening on — the in-memory
        snapshot, NOT the on-disk chat_ports.json, and
      * the merged backend config (llm_server / systemapi / promptapi) read
        from backend_servers.json.
    """
    cfg = _load_server_settings()
    return jsonify({
        "chat-server": RUNNING_PORTS["chat-server"],
        "prompt-mgr":  RUNNING_PORTS["prompt-mgr"],
        "llm_server":  cfg.get("llm_server", {}),
        "systemapi":   cfg.get("systemapi", {}),
        "promptapi":   cfg.get("promptapi", {}),
    })


@chat_app.route("/server-settings", methods=["POST"])
@require_login
def post_server_settings():
    """Protected — persist new ports to chat_ports.json, then restart so the
    services rebind. RUNNING_PORTS is left unchanged until the restart completes,
    so a GET issued before the restart still reports the old (live) ports."""
    body = request.get_json(force=True) or {}
    new_ports = dict(RUNNING_PORTS)
    for k in DEFAULT_PORTS:
        if k in body:
            try:
                new_ports[k] = int(body[k])
            except (TypeError, ValueError):
                return jsonify({"status": "error",
                                "message": f"'{k}' must be an integer port"}), 400
    _save_ports(new_ports)

    # Restart the whole process shortly after the response is flushed so the new
    # ports take effect (the merged process rebinds both services from the file).
    def _restart():
        time.sleep(1.0)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_restart, daemon=True).start()

    return jsonify({
        "status":  "ok",
        "message": "settings saved — restarting to apply",
        "saved":   {"chat-server": new_ports["chat-server"],
                    "prompt-mgr":  new_ports["prompt-mgr"]},
        "running": {"chat-server": RUNNING_PORTS["chat-server"],
                    "prompt-mgr":  RUNNING_PORTS["prompt-mgr"]},
    })


# ═══════════════════════════════════════════════════════════════════
#  ENTRY — run both services in one process
# ═══════════════════════════════════════════════════════════════════
def _run_prompt_app():
    prompt_app.run(host="0.0.0.0", port=RUNNING_PORTS["prompt-mgr"],
                   debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    print(f"""
+======================================================+
|            Merged Server (chat + prompt-mgr)         |
|  chat-server   ->  http://127.0.0.1:{RUNNING_PORTS['chat-server']}
|  prompt-mgr    ->  http://127.0.0.1:{RUNNING_PORTS['prompt-mgr']}
+======================================================+
""")
    threading.Thread(target=_run_prompt_app, daemon=True).start()
    chat_app.run(host="0.0.0.0", port=RUNNING_PORTS["chat-server"],
                 debug=False, threaded=True, use_reloader=False)

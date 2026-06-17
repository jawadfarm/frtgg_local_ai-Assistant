import os
import re
import sys
import json
import socket
import subprocess
import threading
import concurrent.futures
import time
import uuid
import hashlib
import secrets
import ctypes
from ctypes import wintypes
import ipaddress
import tempfile
import psutil

PORT = 8300

# ── recv helper ───────────────────────────────────────────────────────────
def recv_all(sock: socket.socket, timeout: float = 30.0) -> bytes:
    sock.settimeout(timeout)
    chunks = []
    while True:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        except socket.timeout:
            break
    return b"".join(chunks)


# ── Inbound message reader ──────────────────────────────────────────────────
def recv_message(sock: socket.socket, first_timeout: float = 60.0,
                 idle_timeout: float = 0.3) -> bytes:
    """Read one logical request without requiring the client to half-close.

    Waits up to ``first_timeout`` for the first byte (so an idle persistent
    connection is eventually dropped), then keeps reading until the peer pauses
    for ``idle_timeout`` seconds or closes the connection. This avoids blocking
    for the full socket timeout when a client sends a request and waits for the
    reply on the same open socket.
    """
    sock.settimeout(first_timeout)
    try:
        first = sock.recv(65536)
    except socket.timeout:
        return b""
    if not first:
        return b""
    chunks = [first]
    sock.settimeout(idle_timeout)
    while True:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        except socket.timeout:
            break
    return b"".join(chunks)


# ── Debugger sender ───────────────────────────────────────────────────────
def Dsender(text, ip, port):
    try:
        if not port: port = 8787
        if not ip:   ip   = "127.0.0.1"
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(10)
        client.connect((ip, port))
        client.send(text.encode())
        client.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
        client.close()
    except Exception as e:
        return f"Error: {e}"


def _debug_error(source: str, error):
    try:
        Dsender(f'Error:"{source}","{error}"', "127.0.0.1", 8787)
    except Exception:
        pass


# ── Connection error normalizer ───────────────────────────────────────────
def _is_connection_error(e: Exception) -> bool:
    if isinstance(e, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return True
    if isinstance(e, OSError) and e.errno in (10061, 111, 10060, 10065):
        return True
    return False


# ── Auth token store ──────────────────────────────────────────────────────
# Tokens minted on a successful password login. Memory-only: they never touch
# disk and are cleared automatically when the process exits.
_tokens: set = set()
_tokens_lock = threading.Lock()

def issue_token() -> str:
    token = secrets.token_hex(16)
    with _tokens_lock:
        _tokens.add(token)
    return token

def token_valid(t: str) -> bool:
    with _tokens_lock:
        return t in _tokens


# ── Duplicate prevention ──────────────────────────────────────────────────
_recent_commands: dict = {}
_recent_lock = threading.Lock()

def is_duplicate(raw_text: str) -> bool:
    key = hash(raw_text)
    now = time.time()
    with _recent_lock:
        if key in _recent_commands:
            if now - _recent_commands[key] < 2.0:
                return True
        _recent_commands[key] = now
    return False


# ── Inbound IP trust model ──────────────────────────────────────────────────
# Only inbound connections are restricted. Outbound connections are always
# allowed. Access is decided non-interactively from config: localhost is always
# allowed, the "ip" allow-list is always allowed, LAN addresses follow
# "local_network", and everything else (public) follows "public_network".
LOCALHOST = {"127.0.0.1", "::1", "localhost"}


def is_lan_ip(ip: str) -> bool:
    """Return True for private LAN addresses (10/8, 172.16-31/12, 192.168/16)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private and not addr.is_loopback


def is_allowed(ip: str) -> tuple[bool, str]:
    """Decide whether an inbound IP may connect, based only on config."""
    if ip in LOCALHOST:
        return True, "localhost"
    cfg = load_config()
    if ip in cfg.get("ip", []):
        return True, "trusted_ip"
    if is_lan_ip(ip):
        return bool(cfg.get("local_network", True)), "local_network"
    return bool(cfg.get("public_network", True)), "public_network"


# ── Request authentication parser ──────────────────────────────────────────
def parse_auth(raw_text: str) -> tuple:
    """Split the auth line (and an optional API key) from the command body.

    Scans for the first line starting with ``password:`` or ``token:`` (the
    authentication credential). A *second* ``token:`` line appearing after it is
    treated as an API key, not as another credential — it is pulled out of the
    body and forwarded to downstream services rather than run as a command.

    Returns ``(kind, value, command_body, api_key)`` where ``kind`` is
    "password", "token", or None, ``value`` is the credential, ``command_body``
    is the remaining lines rejoined (with both recognized lines removed), and
    ``api_key`` is the second token value or None.
    """
    lines = raw_text.split("\n")
    kind = value = api_key = None
    auth_idx = None
    drop = set()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        low = stripped.lower()
        if auth_idx is None:
            if low.startswith("password:"):
                kind, value, auth_idx = "password", stripped[len("password:"):], idx
                drop.add(idx)
            elif low.startswith("token:"):
                kind, value, auth_idx = "token", stripped[len("token:"):], idx
                drop.add(idx)
            continue
        # After the auth line: a subsequent token: line is the API key.
        if api_key is None and low.startswith("token:"):
            api_key = stripped[len("token:"):]
            drop.add(idx)
    if auth_idx is None:
        return None, None, raw_text, None
    body = "\n".join(l for i, l in enumerate(lines) if i not in drop)
    return kind, value, body, api_key


# ── Parallel command sessions ──────────────────────────────────────────────
# A session carries many commands on ONE persistent socket. systemapi fans the
# commands out concurrently and streams a status frame per command back to the
# caller (chat.py), which mirrors them for the frontend. See execute_one_command.
SESSION_DEFAULT_TIMEOUT = 120   # per-command wall-clock budget when none is given
SESSION_MAX_WORKERS     = 100   # cap on concurrent commands within one session

_sessions: dict = {}
_sessions_lock = threading.Lock()


def _try_parse_build(text: str) -> "dict | None":
    """Return the parsed Build object when text is a session-build request, else
    None. A build is a JSON object whose "status" is "Build"."""
    t = (text or "").strip()
    if not t.startswith("{"):
        return None
    try:
        obj = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, dict) and str(obj.get("status", "")).lower() == "build":
        return obj
    return None


def _send_frame(conn: socket.socket, lock: threading.Lock, obj: dict):
    """Serialize one newline-delimited JSON frame onto the session socket."""
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with lock:
        conn.sendall(data)


def handle_session(conn: socket.socket, build: dict, client_ip: str):
    """Serve one parallel command session over a single persistent socket.

    Protocol (see plan): validate token, reject a duplicate active session_id,
    ack with {"status":"Listen"}, fan commands out concurrently emitting a
    "working" then a terminal "done"/"error" frame each, then a final
    {"stat":"done - working"} before closing.
    """
    write_lock   = threading.Lock()
    session_id   = str(build.get("session_id") or "").strip()
    token        = build.get("token")
    api_key      = build.get("api_key")
    commands     = build.get("commands") or {}

    # ── Auth gate (same policy as the legacy path) ──────────────────────────
    pw = load_password()
    if bool(pw.get("login_required")) and not token_valid(str(token or "")):
        _send_frame(conn, write_lock,
                    {"status": "error", "message": "token not found",
                     "session_id": session_id})
        conn.close()
        return

    if not session_id:
        _send_frame(conn, write_lock,
                    {"status": "error", "message": "missing session_id"})
        conn.close()
        return

    # ── Duplicate session id ────────────────────────────────────────────────
    with _sessions_lock:
        if session_id in _sessions:
            _send_frame(conn, write_lock,
                        {"status": "error",
                         "message": f"session {session_id} already active",
                         "session_id": session_id})
            conn.close()
            return
        _sessions[session_id] = {"created": time.time()}

    # Normalize commands into an ordered list of (cmd_id, raw_text).
    if isinstance(commands, dict):
        items = [(str(k), v) for k, v in commands.items()]
    elif isinstance(commands, list):
        items = [(f"command{idx + 1}#", v) for idx, v in enumerate(commands)]
    else:
        items = []

    print(f"[systemapi] Session {session_id} from {client_ip}: {len(items)} command(s)")

    try:
        # ── Listen ack ───────────────────────────────────────────────────────
        _send_frame(conn, write_lock,
                    {"token": token, "status": "Listen", "session_id": session_id})

        def run_cmd(cmd_id: str, raw):
            timeout, clean = _split_timeout_prefix(str(raw))
            if timeout is None:
                timeout = SESSION_DEFAULT_TIMEOUT
            ctype = (clean.split(":", 1)[0].strip().lower()
                     if ":" in clean else clean.strip().lower())
            # working frame (start) — chat.py derives Remaining_time from this.
            _send_frame(conn, write_lock,
                        {"session_id": session_id,
                         cmd_id: {"status": "working", "type": ctype, "timeout": timeout}})
            # Run under a wall-clock budget. The inner thread keeps running if it
            # overruns (we cannot safely kill it), but only one terminal frame is
            # ever emitted — the timeout error wins.
            box = {}
            def _work():
                try:
                    box["r"] = execute_one_command(clean, api_key=api_key)
                except Exception as e:
                    _debug_error(f"session_cmd_{cmd_id}", e)
                    box["r"] = {"status": "error", "message": str(e)}
            th = threading.Thread(target=_work, daemon=True)
            th.start()
            th.join(timeout)
            if th.is_alive():
                frame = {"status": "error", "message": f"error time out after {timeout}"}
            else:
                frame = box.get("r") or {"status": "error", "message": "no result"}
            _send_frame(conn, write_lock, {"session_id": session_id, cmd_id: frame})

        if items:
            workers = max(1, min(len(items), SESSION_MAX_WORKERS))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(run_cmd, cid, raw) for cid, raw in items]
                concurrent.futures.wait(futs)

        # ── Session end ───────────────────────────────────────────────────────
        _send_frame(conn, write_lock,
                    {"session_id": session_id, "stat": "done - working"})
    except Exception as e:
        _debug_error("handle_session", e)
        print(f"[systemapi] Session {session_id} error: {e}")
    finally:
        with _sessions_lock:
            _sessions.pop(session_id, None)
        try:
            conn.close()
        except Exception:
            pass
        print(f"[systemapi] Session {session_id} closed")


# ── Socket server ─────────────────────────────────────────────────────────
def socket_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", PORT))
    server.listen(20)
    print(f"Server listening on port {PORT}...")
    while True:
        try:
            conn, addr = server.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except Exception as e:
            _debug_error("socket_server", e)
            print(f"[systemapi] Error in socket_server: {e}")


def handle_client(conn, addr):
    client_ip, client_port = addr
    try:
        print(f"[systemapi] New connection from {addr}")

        # ── Inbound access control before anything else ─────────────────
        allowed, reason = is_allowed(client_ip)
        if not allowed:
            print(f"[systemapi] Access denied: {client_ip}", flush=True)
            _debug_error("access_control", f"denied:{client_ip}")
            try:
                conn.sendall(f"systemapi:\nAccess denied: {client_ip}".encode("utf-8"))
            except Exception:
                pass
            conn.close()
            return

        # ── Session mode? The first message decides ─────────────────────
        # A JSON object with "status":"Build" opens a parallel command session
        # (one persistent socket, streamed per-command status frames). Anything
        # else is a legacy line-based request served by the loop below.
        first_text = recv_message(conn).decode(errors="ignore").strip()
        build = _try_parse_build(first_text)
        if build is not None:
            handle_session(conn, build, client_ip)
            return

        # ── Trusted connection, serve requests until the client disconnects ─
        raw_text = first_text
        while True:
            if not raw_text:
                # Peer closed the connection or stayed idle past the timeout.
                break
            print(f"[systemapi] Message received ({len(raw_text)} chars):")
            print(f"  {raw_text[:300]}{'...' if len(raw_text) > 300 else ''}")
            print(f"{'=' * 60}")

            # Whether this request resolves to a command we actually execute.
            # Auth failures / duplicates / token-only pings reply (or skip) and
            # then fall straight through to read the NEXT request — they must
            # NEVER `continue`, because the read happens at the bottom of the
            # loop. A `continue` would leave raw_text unchanged and reprocess the
            # same message forever (a tight DUPLICATE-spam loop that pegs the CPU
            # and never returns a clean error — exactly what an expired token hit).
            run_command  = False
            command_body = None
            api_key      = None

            if is_duplicate(raw_text):
                print(f"[systemapi] ⚠ DUPLICATE — ignored")
            else:
                # ── Authentication gate ─────────────────────────────────
                # The auth line is always parsed out (so it never reaches api as
                # an unknown command). When login is off, credentials are accepted
                # as-is without verification but still behave the same way.
                pw = load_password()
                login_on = bool(pw.get("login_required"))
                kind, value, command_body, api_key = parse_auth(raw_text)

                if kind == "password":
                    # Login off: the password is not verified, but the caller
                    # still gets a token so the flow is identical either way.
                    if login_on and hash_password(value) != pw.get("pass"):
                        reply_to_caller(conn, "Error: password not correct")
                    else:
                        token = issue_token()
                        # Hand the caller their token first (on its own line),
                        # then the command result follows on the same stream.
                        reply_to_caller(conn, f"yourtoken:{token}\n")
                        run_command = True
                elif kind == "token":
                    if login_on and not token_valid(value):
                        reply_to_caller(conn, "Error: token not found")
                    elif not command_body.strip():
                        # Token only, no command: just confirm it is valid.
                        reply_to_caller(conn, "Token:active")
                    else:
                        run_command = True
                elif login_on:
                    # No credentials supplied while login is required.
                    reply_to_caller(conn, "Error: token not found")
                else:
                    # Login off and no auth line -> run command_body (== raw_text).
                    run_command = True

            if run_command:
                done = threading.Event()
                _t_api = time.time()
                print(f"[hc-debug] -> api() START", flush=True)
                api(command_body, reply_conn=conn, done=done, api_key=api_key)
                print(f"[hc-debug] <- api() RETURNED after {time.time()-_t_api:.1f}s "
                      f"done.is_set()={done.is_set()}", flush=True)
                done.wait(timeout=120)
                print(f"[hc-debug] done.wait() finished at {time.time()-_t_api:.1f}s "
                      f"done.is_set()={done.is_set()} -> looping back to recv_message", flush=True)

            # Read the next request on this persistent connection. ALWAYS reached
            # (even after an auth failure or duplicate), so the loop advances to
            # the next message instead of spinning on the same one.
            raw_text = recv_message(conn).decode(errors="ignore").strip()

    except socket.timeout:
        _debug_error("handle_client", "time_out")
        print(f"[systemapi] Timeout handling client {addr}")
    except Exception as e:
        _debug_error("handle_client", e)
        print(f"[systemapi] Error handling client {addr}: {e}")
    finally:
        conn.close()


# ── Services loader ────────────────────────────────────────────────────────
SERVICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "services.json")
CONFIG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "systemapi_config.json")
PASSWORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.json")
BLOCKS_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocked_commands.json")

# Default configuration. Written to disk only when systemapi_config.json is missing,
# corrupted, or has missing keys. A valid, complete file is never overwritten,
# so the user is free to edit their settings.
DEFAULT_CONFIG = {
    "admin":          False,
    "ip":             [],
    "local_network":  True,
    "public_network": True,
}

def _atomic_write_json(path, data, ensure_ascii=True):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=ensure_ascii)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

def load_services() -> dict:
    if not os.path.exists(SERVICES_FILE):
        return {}
    try:
        with open(SERVICES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _debug_error("load_services", e)
        return {}

def load_config() -> dict:
    # Use the on-disk file as-is when it is valid and has all required keys.
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and all(k in data for k in DEFAULT_CONFIG):
                return data
        except Exception as e:
            _debug_error("load_config", e)
    # Missing, corrupted, or missing keys: create it with defaults, once.
    cfg = dict(DEFAULT_CONFIG)
    save_config(cfg)
    return cfg

def save_config(data: dict):
    try:
        _atomic_write_json(CONFIG_FILE, data)
    except Exception as e:
        _debug_error("save_config", e)

def save_services(data: dict):
    try:
        _atomic_write_json(SERVICES_FILE, data)
    except Exception as e:
        _debug_error("save_services", e)


# ── Password store ─────────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

def load_password() -> dict:
    if not os.path.exists(PASSWORD_FILE):
        return {}
    try:
        with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        _debug_error("load_password", e)
        return {}

def save_password(data: dict):
    try:
        _atomic_write_json(PASSWORD_FILE, data)
    except Exception as e:
        _debug_error("save_password", e)


# ── Command block store ─────────────────────────────────────────────────────
# Blocked os_command words live in blocked_commands.json as {"block": [...]}.
# Missing/corrupt files are recreated empty so the system fails open to an
# editable, well-formed file rather than crashing.
def load_command_blocks() -> list:
    if os.path.exists(BLOCKS_FILE):
        try:
            with open(BLOCKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            block = data.get("block") if isinstance(data, dict) else None
            if isinstance(block, list):
                return [str(b) for b in block]
        except Exception as e:
            _debug_error("load_command_blocks", e)
    save_command_blocks([])
    return []

def save_command_blocks(blocks: list):
    try:
        _atomic_write_json(BLOCKS_FILE, {"block": list(blocks)})
    except Exception as e:
        _debug_error("save_command_blocks", e)


# ── Command Logger ────────────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "command_log.json")
_log_lock = threading.Lock()

def log_command(key: str, value: str) -> str:
    entry = {
        "id":        str(uuid.uuid4()),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "key":       key,
        "value":     value,
    }
    try:
        with _log_lock:
            log = []
            if os.path.exists(LOG_FILE):
                try:
                    with open(LOG_FILE, "r", encoding="utf-8") as f:
                        log = json.load(f)
                except Exception:
                    pass
            log.append(entry)
            _atomic_write_json(LOG_FILE, log, ensure_ascii=False)
    except Exception as e:
        _debug_error("log_command", e)
    print(f"[log] Saved entry id={entry['id']}  key={key[:60]}")
    return entry["id"]


# ── Generic TCP sender ────────────────────────────────────────────────────
def send_to_service(key: str, full_line: str, api_key: str = None) -> str:
    services = load_services()
    svc = services.get(key)
    if not svc:
        msg = f"[services] No service registered for key '{key}'"
        print(msg)
        return msg
    ip   = svc.get("ip")
    port = svc.get("port")
    if api_key:
        full_line = f"token:{api_key}\n{full_line}"
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(30)
        client.connect((ip, int(port)))
        client.sendall((full_line + "\n").encode("utf-8"))  # append newline
        client.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
            if b"\n" in response:
                break
        client.close()
        return response
    except socket.timeout:
        err = f"{key.rstrip(':')}:Not_connected"
        _debug_error(f"send_to_service_{key}", "time_out")
        return err
    except Exception as e:
        if _is_connection_error(e):
            err = f"{key.rstrip(':')}:Not_connected"
            _debug_error(f"send_to_service_{key}", "not_connected")
            return err
        err = f"[{key}] error ({ip}:{port}): {e}"
        _debug_error(f"send_to_service_{key}", e)
        return err


# ── Reply to caller ───────────────────────────────────────────────────────
def reply_to_caller(conn: socket.socket, text: str):
    try:
        conn.sendall(text.encode("utf-8"))
        print(f"[systemapi] Replied to caller ({len(text)} chars)")
    except Exception as e:
        _debug_error("reply_to_caller", e)
        print(f"[systemapi] Reply error: {e}")


# ── Audio sender ──────────────────────────────────────────────────────────
def sender(text: str, api_key: str = None) -> str:
    services = load_services()
    svc  = services.get("audioapi:")
    ip   = svc["ip"]   if svc else "127.0.0.1"
    port = svc["port"] if svc else 8116
    if api_key:
        text = f"token:{api_key}\n{text}"
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(30)
        client.connect((ip, int(port)))
        client.sendall(text.encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        response = recv_all(client, timeout=30.0).decode(errors="ignore")
        client.close()
        return response
    except socket.timeout:
        _debug_error("audioapi", "time_out")
        return "audioapi:Not_connected"
    except Exception as e:
        if _is_connection_error(e):
            _debug_error("audioapi", "not_connected")
            return "audioapi:Not_connected"
        _debug_error("audioapi", e)
        return f"Error: {e}"


# ── FileAPI sender ────────────────────────────────────────────────────────
def send_to_fileapi(commands: str, api_key: str = None) -> str:
    services = load_services()
    svc  = services.get("fileapi:")
    ip   = svc["ip"]   if svc else "127.0.0.1"
    port = svc["port"] if svc else 8910
    if api_key:
        commands = f"token:{api_key}\n{commands}"
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(30)
        client.connect((ip, int(port)))
        client.sendall((commands + "\nEND").encode("utf-8"))
        marker = b"<<EOF>>"
        response = bytearray()
        found = False
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            tail_start = len(response) - (len(marker) - 1)
            response += chunk
            if tail_start < 0:
                tail_start = 0
            if marker in response[tail_start:]:
                found = True
                break
        client.close()
        if found:
            response = response[:response.rfind(marker)]
        return bytes(response).decode("utf-8", errors="replace").strip()
    except socket.timeout:
        _debug_error("fileapi", "time_out")
        return "fileapi:Not_connected"
    except Exception as e:
        if _is_connection_error(e):
            _debug_error("fileapi", "not_connected")
            return "fileapi:Not_connected"
        _debug_error("fileapi", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Search helpers ────────────────────────────────────────────────────────
COMMAND_MAP = {
    "fastsearch":   "fastSearch",
    "normalsearch": "normalSearch",
    "deepsearch":   "deepSearch",
    "fastfetch":    "fastFetch",
    "normalfetch":  "normalFetch",
    "deepfetch":    "deepFetch",
}

def sender_search(raw_command: str, api_key: str = None) -> str:
    try:
        if ":" not in raw_command:
            return json.dumps({"status": "error", "message": "Invalid format. Use command:value"})
        command, value = raw_command.split(":", 1)
        correct_command = COMMAND_MAP.get(command.strip().lower())
        if not correct_command:
            return json.dumps({"status": "error",
                               "message": f"Unknown: '{command}'",
                               "available": list(COMMAND_MAP.values())})
        value = value.strip()
        if not value:
            return json.dumps({"status": "error", "message": "Query cannot be empty"})
        request_obj = ({"command": correct_command, "query": value}
                       if correct_command in ["fastSearch", "normalSearch", "deepSearch"]
                       else {"command": correct_command, "url": value})
        if api_key:
            request_obj["api_key"] = api_key
        services = load_services()
        svc  = services.get("internetapi:")
        ip   = svc["ip"]   if svc else "127.0.0.1"
        port = svc["port"] if svc else 8400
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect((ip, int(port)))
        s.sendall(json.dumps(request_obj).encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        data = recv_all(s, timeout=30.0)
        s.close()
        return data.decode("utf-8")
    except socket.timeout:
        _debug_error("sender_search", "time_out")
        return json.dumps({"status": "error", "message": "internetapi:Not_connected"})
    except ConnectionRefusedError:
        _debug_error("sender_search", "not_connected")
        return json.dumps({"status": "error", "message": "internetapi:Not_connected"})
    except Exception as e:
        if _is_connection_error(e):
            _debug_error("sender_search", "not_connected")
            return json.dumps({"status": "error", "message": "internetapi:Not_connected"})
        _debug_error("sender_search", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── TTS sender ───────────────────────────────────────────────────────────
def send_to_tts(text: str) -> None:
    services = load_services()
    svc  = services.get("tts:")
    ip   = svc["ip"]   if svc else "127.0.0.1"
    port = svc["port"] if svc else 8112
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(10)
        client.connect((ip, int(port)))
        client.sendall(text.encode("utf-8"))
        client.close()
    except socket.timeout:
        _debug_error("tts", "time_out")
    except Exception as e:
        if _is_connection_error(e):
            _debug_error("tts", "not_connected")
        else:
            _debug_error("tts", e)


# ── TTS text extractor ────────────────────────────────────────────────────
def extract_tts_text(raw: str) -> str:
    rest = raw.strip()
    if rest.startswith("{"):
        close = rest.rfind("}")
        return rest[1:close] if close != -1 else rest[1:]
    return rest

# ── os_command parsing & blocking ──────────────────────────────────────────
DEFAULT_OS_TIMEOUT = 5  # seconds; per-command override comes from os_command JSON

# Matches single- or double-quoted spans so quoted text is never treated as a
# command (e.g. python print("del") must not flag "del").
_QUOTED_SPAN = re.compile(r"'[^']*'|\"[^\"]*\"")
# Shell separators that each introduce a new command position.
_CMD_SEPARATORS = re.compile(r"&&|\|\||[&|;\n]")


# A standalone "timeout=NN" line inside a brace block. Accepts an optional
# "s" / "/s" suffix (timeout=30, timeout=30s, timeout=30/s) — all mean seconds.
_TIMEOUT_LINE = re.compile(r"^\s*timeout\s*=\s*(\d+)\s*/?\s*s?\s*$", re.IGNORECASE)

# A standalone "kill-after_timeout = true/false" line inside a brace block.
# Accepts "kill-after_timeout", "killafter_timeout", "kill_after_timeout".
# When true, the process is forcibly terminated once the timeout is reached;
# when false (the default), it is left running but the partial result is still
# returned.
_KILL_AFTER_LINE = re.compile(r"^\s*kill[-_]?after_timeout\s*=\s*(true|false)\s*$", re.IGNORECASE)


def _coerce_timeout(val) -> int:
    """Clamp a timeout to a positive int, falling back to DEFAULT_OS_TIMEOUT."""
    try:
        t = int(val)
    except (TypeError, ValueError):
        return DEFAULT_OS_TIMEOUT
    return t if t > 0 else DEFAULT_OS_TIMEOUT


def _coerce_kill_after(val) -> bool:
    """Coerce a kill-after_timeout value to a bool (default False)."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() == "true"
    return False


def _parse_os_command(raw: str) -> tuple:
    """Parse an os_command value into ``(command, timeout, kill_after)``.

    Accepts three shapes, in priority order:

      1. JSON object (single- or multi-line)::

             {"command": "...", "timeout": 30, "kill-after_timeout": true}

      2. Plain brace block (does NOT need to be one line)::

             {
             <command line(s)>
             timeout=30                  # optional, seconds, default 5
             kill-after_timeout=true     # optional, default false
             }

         Every line that is not ``timeout`` or ``kill-after_timeout`` is treated
         as one command; multiple lines are joined with `` & `` so they run
         sequentially in cmd.exe.

      3. A plain command string (back-compat, e.g. ``notepad``).

    ``timeout`` defaults to DEFAULT_OS_TIMEOUT when absent or invalid.
    ``kill_after`` defaults to False (process is left running on timeout).
    """
    raw = raw.strip()
    if raw.startswith("{"):
        # 1. Strict JSON first (handles the legacy single-line JSON form).
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            cmd = obj.get("command")
            if isinstance(cmd, str) and cmd.strip():
                kill_after = _coerce_kill_after(
                    obj.get("kill-after_timeout", obj.get("kill_after_timeout")))
                return cmd.strip(), _coerce_timeout(obj.get("timeout")), kill_after
        # 2. Plain brace block: drop the outer "{" / "}", lift out the
        #    timeout=NN and kill-after_timeout lines, keep the rest as the body.
        inner = raw[1:]
        close = inner.rfind("}")
        if close != -1:
            inner = inner[:close]
        timeout = DEFAULT_OS_TIMEOUT
        kill_after = False
        cmd_lines = []
        for ln in inner.splitlines():
            m = _TIMEOUT_LINE.match(ln)
            if m:
                timeout = _coerce_timeout(m.group(1))
                continue
            k = _KILL_AFTER_LINE.match(ln)
            if k:
                kill_after = (k.group(1).lower() == "true")
                continue
            if ln.strip():
                cmd_lines.append(ln.strip())
        if cmd_lines:
            return " & ".join(cmd_lines), timeout, kill_after
    # 3. Plain command string.
    return raw, DEFAULT_OS_TIMEOUT, False


def find_blocked_command(cmd: str, blocks: list) -> "str | None":
    """Return the blocked word that appears as a real, unquoted command.

    Quoted spans are blanked first so a blocked word inside quotes is ignored.
    The remainder is split on shell separators (``& && | || ; newline``); a
    block is flagged only when it is the first word of a command segment, which
    defeats bypasses like ``python print(1) & shutdown -s``.
    """
    if not blocks:
        return None
    unquoted = _QUOTED_SPAN.sub(" ", cmd)
    for segment in _CMD_SEPARATORS.split(unquoted):
        seg = segment.strip()
        if not seg:
            continue
        for blocked in blocks:
            b = blocked.strip()
            if not b:
                continue
            if re.match(rf"{re.escape(b)}(\s|$)", seg, re.IGNORECASE):
                return blocked
    return None


LAUNCHER_PREFIXES = (
    "start ", "explorer", "mspaint", "notepad", "calc",
    "taskmgr", "control", "msconfig", "regedit", "mmc",
    "devmgmt.msc", "diskmgmt.msc", "services.msc"
)

def _is_launcher_cmd(cmd: str) -> bool:
    c = cmd.lower().lstrip()
    return any(c.startswith(p) for p in LAUNCHER_PREFIXES)


# ── Privilege management ──────────────────────────────────────────────────
# The application runs with administrator privileges, but os_command must run
# with standard-user privileges unless "admin" is enabled in config.
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# CreateProcessWithTokenW is used to launch de-elevated processes. An elevated
# administrator holds SeImpersonatePrivilege (required by CreateProcessWithTokenW)
# but NOT SeAssignPrimaryTokenPrivilege (required by CreateProcessAsUser), so the
# latter fails with ERROR_PRIVILEGE_NOT_HELD. pywin32 does not expose
# CreateProcessWithTokenW, so we call it via ctypes. It does not inherit handles,
# so command output is captured by redirecting to a temp file inside the command.
_CREATE_NO_WINDOW = 0x08000000
# A de-elevated process launched with CREATE_NO_WINDOW gets NO console of its own.
# Console programs that query the console (PowerShell's output formatter asks the
# console host for its buffer width to lay out results) then block forever waiting
# on a console host that does not exist — the command "hangs until the timeout"
# while producing no output. CREATE_NEW_CONSOLE gives it a real (private) console
# to talk to; STARTF_USESHOWWINDOW + SW_HIDE keep that console window invisible.
# stdout/stderr are still captured via the shell `> file` redirection, independent
# of this console.
_CREATE_NEW_CONSOLE   = 0x00000010
_STARTF_USESHOWWINDOW = 0x00000001
_SW_HIDE              = 0
_WAIT_TIMEOUT = 0x00000102


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_CreateProcessWithTokenW = _advapi32.CreateProcessWithTokenW
_CreateProcessWithTokenW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
    wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
    ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION),
]
_CreateProcessWithTokenW.restype = wintypes.BOOL


# The duplicated shell token is cached and reused across commands. Building it
# scans the process list for explorer.exe, which is slow to do on every call.
_shell_token = None
_shell_token_lock = threading.Lock()


def _build_shell_token():
    """Duplicate explorer.exe's medium-integrity token for de-elevation.

    Returns None when pywin32 is missing or no suitable token can be obtained.
    """
    try:
        import win32api, win32con, win32security
    except ImportError as e:
        _debug_error("deelevate", e)
        return None
    for proc in psutil.process_iter(["name", "pid"]):
        if (proc.info["name"] or "").lower() == "explorer.exe":
            try:
                hproc = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION,
                                             False, proc.info["pid"])
                htoken = win32security.OpenProcessToken(
                    hproc,
                    win32con.TOKEN_DUPLICATE | win32con.TOKEN_QUERY |
                    win32con.TOKEN_ASSIGN_PRIMARY | win32con.TOKEN_ADJUST_DEFAULT |
                    win32con.TOKEN_ADJUST_SESSIONID,
                )
                return win32security.DuplicateTokenEx(
                    htoken,
                    win32security.SecurityImpersonation,
                    win32con.MAXIMUM_ALLOWED,
                    win32security.TokenPrimary,
                )
            except Exception:
                continue
    return None


def _get_shell_token(force_refresh: bool = False):
    """Return a cached shell token, rebuilding it on first use or on demand."""
    global _shell_token
    with _shell_token_lock:
        if force_refresh:
            _shell_token = None
        if _shell_token is None:
            _shell_token = _build_shell_token()
        return _shell_token


def _spawn_deelevated(token, cmd: str, capture: bool, timeout: int = DEFAULT_OS_TIMEOUT,
                      kill_after: bool = False) -> str:
    """Spawn one command de-elevated via CreateProcessWithTokenW.

    Raises on failure. Output is captured by redirecting both streams to a temp
    file inside the command line, because CreateProcessWithTokenW does not pass
    inherited std handles to the new process.
    """
    out_path = None
    try:
        # `< NUL` connects the child's stdin to the null device. The de-elevated
        # process inherits NO handles (CreateProcessWithTokenW), so without this
        # its stdin is invalid: any command that touches stdin — e.g. powershell
        # not run with -NonInteractive, `more`, `set /p`, `sort` with no file —
        # blocks forever waiting for input that never arrives, and the command
        # appears to "hang until the timeout" while producing no output. Feeding
        # NUL hands it an immediate EOF instead so it runs and exits normally.
        if capture:
            # The de-elevated (medium integrity) process creates and writes this
            # file itself via redirection; do not pre-create it.
            out_path = os.path.join(tempfile.gettempdir(),
                                    f"systemapi_{uuid.uuid4().hex}.out")
            full = f'cmd /c {cmd} < NUL > "{out_path}" 2>&1'
        else:
            full = f'cmd /c {cmd} < NUL'

        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(_STARTUPINFOW)
        pi = _PROCESS_INFORMATION()
        ok = _CreateProcessWithTokenW(
            int(token), 0, None, ctypes.create_unicode_buffer(full),
            _CREATE_NO_WINDOW, None, None, ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

        if not capture:
            _kernel32.CloseHandle(pi.hProcess)
            _kernel32.CloseHandle(pi.hThread)
            return "no result"

        wait_result = _kernel32.WaitForSingleObject(pi.hProcess, int(timeout * 1000))
        timed_out = (wait_result == _WAIT_TIMEOUT)
        # Forcibly terminate the (de-elevated) shell process when asked; leave it
        # running otherwise. Either way the partial output file is read below.
        if timed_out and kill_after:
            try:
                _kernel32.TerminateProcess(pi.hProcess, 1)
            except Exception as e:
                _debug_error("spawn_deelevated_kill", e)
        _kernel32.CloseHandle(pi.hProcess)
        _kernel32.CloseHandle(pi.hThread)
        try:
            with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                output = f.read()
        except FileNotFoundError:
            output = ""
        return _format_result(output, timed_out=timed_out,
                              timeout=timeout, killed=(timed_out and kill_after))
    finally:
        if out_path and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass


DEELEVATE_UNAVAILABLE = (
    "cannot run os_command without administrator privileges: de-elevation "
    "unavailable (install pywin32 and ensure explorer.exe is running, or set "
    '"admin": true in systemapi_config.json to allow elevated execution)'
)


def run_command_deelevated(cmd: str, capture: bool = True, timeout: int = DEFAULT_OS_TIMEOUT,
                           kill_after: bool = False) -> str:
    """Run a command as the standard (non-elevated) user via the shell token.

    Reuses a cached token; if the spawn fails (e.g. explorer was restarted and
    the token is stale) it rebuilds the token once and retries. If privileges
    cannot be dropped, it refuses rather than run the command as administrator.
    """
    for attempt in range(2):
        token = _get_shell_token(force_refresh=(attempt == 1))
        if token is None:
            break
        try:
            return _spawn_deelevated(token, cmd, capture, timeout, kill_after)
        except Exception as e:
            _debug_error("deelevate", e)

    # Do not silently run with administrator privileges when policy forbids it.
    _debug_error("deelevate", "de-elevation unavailable; refusing elevated run")
    return DEELEVATE_UNAVAILABLE


def _format_result(output: str, timed_out: bool, timeout: int = DEFAULT_OS_TIMEOUT,
                   killed: bool = False) -> str:
    """Normalize captured command output to the agreed return values.

    No timeout: the actual output, or "no result" when there is none.
    Timeout:    the partial output (if any) followed by a standardized message
                explaining the result is incomplete. The wording differs by
                whether the process was forcibly terminated at the limit.
    """
    output = (output or "").strip()
    if timed_out:
        if killed:
            msg = (f"The process did not complete; however, the specified time "
                   f"limit was reached, and execution was terminated at {timeout}s.")
        else:
            msg = (f"The process did not complete; the specified time limit of "
                   f"{timeout}s was reached. Partial output is shown above; the "
                   f"process was left running.")
        return f"{output}\n{msg}" if output else msg
    return output or "no result"


def _run_command_normal(cmd: str, capture: bool = True, timeout: int = DEFAULT_OS_TIMEOUT,
                        kill_after: bool = False) -> str:
    """Run a command in the current process context (inherits its privileges)."""
    if not capture:
        try:
            subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "no result"
        except Exception as e:
            return f"Launch error: {e}"
    try:
        # stdin=DEVNULL gives the child an immediate EOF instead of an inherited
        # stdin it might block on. Without it, a command that reads input (e.g.
        # powershell not run with -NonInteractive, `more`, `set /p`) hangs until
        # the timeout while producing no output.
        proc = subprocess.Popen(
            cmd, shell=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        collected = []

        def _read(stream, buf=collected):  # buf avoids the closure problem
            try:
                for ln in stream:  # ln instead of line
                    buf.append(ln)
            except Exception:
                pass

        t1 = threading.Thread(target=_read, args=(proc.stdout,), daemon=True)
        t2 = threading.Thread(target=_read, args=(proc.stderr,), daemon=True)
        t1.start()
        t2.start()
        try:
            proc.wait(timeout=timeout)
            t1.join(1)
            t2.join(1)
            return _format_result("".join(collected), timed_out=False)
        except subprocess.TimeoutExpired:
            # Forcibly terminate the shell process when asked; otherwise leave it
            # running and just snapshot whatever output has been captured so far.
            if kill_after:
                try:
                    proc.kill()
                except Exception as e:
                    _debug_error("run_command_normal_kill", e)
            t1.join(0.5)
            t2.join(0.5)
            return _format_result("".join(collected), timed_out=True,
                                  timeout=timeout, killed=kill_after)
    except Exception as e:
        return f"Error: {e}"


def run_os_command(cmd: str, capture: bool = True, timeout: int = DEFAULT_OS_TIMEOUT,
                   kill_after: bool = False) -> str:
    """Execute an os_command in systemapi's own process context.

    Privilege now follows the process itself: systemapi no longer self-elevates,
    so it runs as a standard user by default and commands inherit that. If the
    user deliberately launches systemapi as administrator, commands run elevated.

    The old de-elevation path (CreateProcessWithTokenW) has been removed: it hung
    indefinitely on certain console output (e.g. PowerShell reading a file), and
    it is no longer needed now that command privilege simply matches the process.
    """
    return _run_command_normal(cmd, capture, timeout, kill_after)


# ── Block checker ─────────────────────────────────────────────────────────
def is_blocked(key: str) -> bool:
    return load_services().get(key, {}).get("Block", False) is True


# ── Auth / network control commands ────────────────────────────────────────
def handle_control(line: str) -> "str | None":
    """Handle authenticated control commands for login, password, and network.

    Reached only after the auth gate in handle_client, so these are protected
    whenever login_required is true. Returns the user-facing reply message
    (WITHOUT the "systemapi:\n" prefix) when the line is consumed, or None when
    the line is not a control command. Callers add any transport framing.
    """
    low = line.strip().lower()

    if low.startswith("set_login:"):
        val = line.split(":", 1)[1].strip().lower()
        pw = load_password()
        pw["login_required"] = (val == "true")
        save_password(pw)
        log_command(line, f"login_required set to {pw['login_required']}")
        return f"login_required set to {pw['login_required']}"

    if low.startswith("change_password:"):
        parts = line.split(":", 2)
        if len(parts) < 3:
            return "Usage: change_password:<old>:<new>"
        old, new = parts[1].strip(), parts[2].strip()
        pw = load_password()
        if hash_password(old) != pw.get("pass"):
            return "Error: password not correct"
        pw["pass"] = hash_password(new)
        save_password(pw)
        log_command(line, "password changed")
        return "password changed"

    if low.startswith("set_local_network:") or low.startswith("set_public_network:"):
        prefix, val = line.split(":", 1)
        key = "local_network" if prefix.strip().lower() == "set_local_network" else "public_network"
        cfg = load_config()
        cfg[key] = (val.strip().lower() == "true")
        save_config(cfg)
        log_command(line, f"{key} set to {cfg[key]}")
        return f"{key} set to {cfg[key]}"

    if low == "get_command_block" or low == "get_command_block:":
        blocks = load_command_blocks()
        return "{" + ", ".join(f"{i}:{c}" for i, c in enumerate(blocks)) + "}"

    if low.startswith("add_command_block:"):
        cmd = line.split(":", 1)[1].strip()
        blocks = load_command_blocks()
        if cmd and cmd not in blocks:
            blocks.append(cmd)
            save_command_blocks(blocks)
            log_command(line, f"command block added: {cmd}")
        return "{" + ", ".join(f"{i}:{c}" for i, c in enumerate(blocks)) + "}"

    if low.startswith("remove_command_block:"):
        arg = line.split(":", 1)[1].strip().strip("[]")
        indices = []
        for part in arg.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                indices.append(int(part))
            except ValueError:
                pass
        blocks = load_command_blocks()
        # Remove highest indices first so earlier positions stay valid.
        for idx in sorted(set(indices), reverse=True):
            if 0 <= idx < len(blocks):
                del blocks[idx]
        save_command_blocks(blocks)
        log_command(line, f"command blocks removed at {indices}")
        return "{" + ", ".join(f"{i}:{c}" for i, c in enumerate(blocks)) + "}"

    return None


# ── Service management ────────────────────────────────────────────────────
def handle_service_mgmt(line_lower: str) -> "str | None":
    """Add/remove/edit a registered service. Returns a confirmation message when
    the line is a service-management command, or None when it is not. (The legacy
    socket path ignores the message; the session path returns it as the result.)"""
    if line_lower.startswith("add_service:"):
        parts = line_lower.split(":")
        if len(parts) >= 4:
            key  = parts[1].strip() + ":"
            ip   = parts[2].strip()
            port = int(parts[3].strip())
            svcs = load_services()
            svcs[key] = {"ip": ip, "port": port}
            save_services(svcs)
            log_command(line_lower, f"Service added: {key} → {ip}:{port}")
            return f"Service added: {key} → {ip}:{port}"
        return "Usage: add_service:<key>:<ip>:<port>"

    if line_lower.startswith("remove_service:"):
        key = line_lower.replace("remove_service:", "").strip()
        if not key.endswith(":"): key += ":"
        svcs = load_services()
        if key in svcs:
            del svcs[key]
            save_services(svcs)
            log_command(line_lower, f"Service removed: {key}")
            return f"Service removed: {key}"
        return f"Service not found: {key}"

    if line_lower.startswith("edit_service:"):
        parts = line_lower.split(":")
        if len(parts) >= 4:
            key  = parts[1].strip() + ":"
            ip   = parts[2].strip() if parts[2].strip() != "-" else None
            port = int(parts[3].strip()) if parts[3].strip() not in ("-", "") else None
            svcs = load_services()
            if key in svcs:
                if ip:   svcs[key]["ip"]   = ip
                if port: svcs[key]["port"] = port
                save_services(svcs)
                log_command(line_lower, f"Service updated: {key} → {svcs[key]}")
                return f"Service updated: {key} → {svcs[key]}"
            return f"Service not found: {key}"
        return "Usage: edit_service:<key>:<ip>:<port>"

    return None


# ── FileAPI multiline collector ───────────────────────────────────────────
def collect_fileapi_block(lines: list, start_index: int) -> tuple:
    payload_lines = []
    depth = 0
    i = start_index
    while i < len(lines):
        line_stripped = lines[i].strip()
        line_original = lines[i]

        if line_stripped.lower().startswith("fileapi:"):
            line_original = lines[i][lines[i].lower().find("fileapi:") + len("fileapi:"):]
            line_stripped = line_original.strip()

        if line_stripped.upper() == "END":
            i += 1
            break

        payload_lines.append(line_original)
        depth += line_stripped.count("{") - line_stripped.count("}")
        i += 1
        if depth <= 0 and payload_lines:
            break
    return "\n".join(payload_lines), i

def collect_tts_block(lines: list, start_index: int) -> tuple:
    first_line = lines[start_index].strip()
    content = first_line[len("tts:"):].strip()

    open_count = content.count("{") - content.count("}")
    if open_count <= 0:
        return content, start_index + 1

    i = start_index + 1
    while i < len(lines) and open_count > 0:
        line = lines[i].strip()
        content += "\n" + line
        open_count += line.count("{") - line.count("}")
        i += 1

    return content, i

# ── Per-command timeout prefix ──────────────────────────────────────────────
# A session command may carry an optional leading "timeout=<n>:" token, e.g.
# "timeout=300:internetapi:claude ai". chat.py folds the AI's timeout=NN into
# this form so the session layer knows the wall-clock budget before dispatch.
_SESSION_TIMEOUT_PREFIX = re.compile(r"^\s*timeout\s*=\s*(\d+)\s*/?\s*s?\s*:", re.IGNORECASE)


def _split_timeout_prefix(text: str) -> tuple:
    """Return (timeout_or_None, remaining_text). Strips a leading timeout=<n>:
    token if present; the rest of the command is returned untouched."""
    m = _SESSION_TIMEOUT_PREFIX.match(text)
    if not m:
        return None, text
    return _coerce_timeout(m.group(1)), text[m.end():].lstrip()


def _is_not_connected(result) -> bool:
    """True when a sender's result string signals the downstream service is
    offline. The audioapi / fileapi / generic-service senders return a bare
    "<service>:Not_connected" string on a refused/timed-out connection (see
    sender / send_to_audioapi / send_to_fileapi / send_to_service). Those used to
    be reported to the AI as a successful "done" command; they are failures and
    must be flagged as "error" instead."""
    return isinstance(result, str) and result.strip().lower().endswith(":not_connected")


def execute_one_command(command_text: str, api_key: str = None) -> dict:
    """Execute ONE command (possibly multi-line) synchronously and return a
    result dict: {"status": "done", "result": <str>} on success, or
    {"status": "error", "message": <str>} on failure.

    This is the single-command core shared by the session handler. It reuses the
    same senders as the legacy api() dispatcher but returns the result instead of
    writing it to a socket, so commands can be fanned out concurrently and their
    results streamed back per-command. Result strings carry NO "systemapi:\n"
    framing — the caller adds any transport/AI framing it needs.
    """
    text = (command_text or "").strip()
    if not text:
        return {"status": "done", "result": ""}
    first = text.split("\n", 1)[0].strip()
    low = first.lower()

    # ── control commands (login / password / network / blocks) ──────────────
    ctl = handle_control(first)
    if ctl is not None:
        return {"status": "done", "result": ctl}

    # ── service management (add/remove/edit service) ─────────────────────────
    sm = handle_service_mgmt(low)
    if sm is not None:
        return {"status": "done", "result": sm}

    # ── audio ────────────────────────────────────────────────────────────────
    if low.startswith("audioapi:"):
        if is_blocked("audioapi:"):
            return {"status": "error", "message": "Service 'audioapi' is blocked."}
        msg = sender(first[len("audioapi:"):], api_key=api_key)
        log_command(first, msg)
        if _is_not_connected(msg):
            return {"status": "error", "message": msg}
        return {"status": "done", "result": msg}

    # ── os_command (may be a multi-line brace block) ─────────────────────────
    if low.startswith("os_command:"):
        payload = text[text.lower().find("os_command:") + len("os_command:"):].strip()
        cmd, timeout, kill_after = _parse_os_command(payload)
        blocked = find_blocked_command(cmd, load_command_blocks())
        if blocked:
            result_text = f"{{warning: command '{blocked}' is blocked}}"
            log_command(f"os_command:{payload}", result_text)
            return {"status": "error", "message": result_text}
        output = run_os_command(cmd, capture=not _is_launcher_cmd(cmd),
                                timeout=timeout, kill_after=kill_after)
        log_command(f"os_command:{payload}", output)
        return {"status": "done", "result": output}

    # ── internet search ──────────────────────────────────────────────────────
    if low.startswith("internetapi:"):
        if is_blocked("internetapi:"):
            return {"status": "error", "message": "Service 'internetapi' is blocked."}
        result = sender_search(first[len("internetapi:"):], api_key=api_key)
        try:
            data = json.loads(result)
            if data.get("status") == "error":
                return {"status": "error", "message": data.get("message", "Unknown error")}
            if not data.get("results"):
                formatted = "No results found."
            else:
                formatted = ""
                for r in data["results"]:
                    formatted += (f"{r['position']}. {r['title']}\n"
                                  f"   URL: {r['url']}\n   {r['snippet']}\n\n")
        except json.JSONDecodeError:
            if not result.strip():
                return {"status": "error", "message": "internetapi:Not_connected"}
            formatted = result
        log_command(first, formatted)
        return {"status": "done", "result": formatted}

    # ── tts (fire-and-forget — speaks, but returns NO result to the AI) ───────
    # tts produces no output the AI should see; returning text here makes the AI
    # treat "tts:spoken" as command output and loop. The empty result is dropped
    # from the combined payload (see chat.py _finalize_session), so the AI gets
    # nothing for a tts command — same as the legacy fire-and-forget behavior.
    if low.startswith("tts:"):
        if is_blocked("tts:"):
            return {"status": "error", "message": "Service 'tts' is blocked."}
        tts_text = extract_tts_text(text[text.lower().find("tts:") + len("tts:"):])
        if tts_text.strip():
            log_command(first, tts_text)
            send_to_tts(tts_text)
        return {"status": "done", "result": ""}

    # ── file operations (may be a multi-line block) ──────────────────────────
    if low.startswith("fileapi:"):
        if is_blocked("fileapi:"):
            return {"status": "error", "message": "Service 'fileapi' is blocked."}
        # Strip the "fileapi:" prefix from the start of EVERY command line —
        # a multi-line block repeats the prefix per line (e.g. "fileapi:addfile x"
        # / "fileapi:writefile x {"). Only the leading token is removed; the
        # indentation of file content lines is left completely untouched (NO
        # per-line or whole-payload strip that would flatten Python code).
        payload = re.sub(r"(?im)^[ \t]*fileapi:[ \t]*", "", text)
        # Drop a trailing END marker if the caller left one in — send_to_fileapi
        # appends its own. Removed from the very end only; body is untouched.
        payload = re.sub(r"(?im)\n?[ \t]*END[ \t]*\Z", "", payload)
        result = send_to_fileapi(payload, api_key=api_key)
        log_command(first, result)
        if _is_not_connected(result):
            return {"status": "error", "message": result}
        return {"status": "done", "result": result}

    # ── dynamic services registered in services.json ─────────────────────────
    services = load_services()
    for key in services:
        if low.startswith(key.lower()):
            if is_blocked(key):
                return {"status": "error",
                        "message": f"Service '{key.rstrip(':')}' is blocked."}
            response = send_to_service(key, first, api_key=api_key)
            if isinstance(response, bytes):
                response = response.decode("utf-8", errors="replace")
            log_command(first, response)
            if _is_not_connected(response):
                return {"status": "error", "message": response}
            return {"status": "done", "result": response}

    # ── no handler ────────────────────────────────────────────────────────────
    log_command(first, "(no handler found)")
    return {"status": "error", "message": f"no handler for '{first}'"}


# ── Main dispatcher ───────────────────────────────────────────────────────
def api(command: str, reply_conn: socket.socket = None, done: "threading.Event | None" = None,
        api_key: str = None):
    lines = command.split("\n")
    has_async = False
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        if not line or line.upper() == "END":
            i += 1
            continue

        line_lower = line.lower()
        print(f"\n[api] ┌─ Dispatching ──────────────────────────────────")
        print(f"[api] │  {line[:150]}{'...' if len(line) > 150 else ''}")
        print(f"[api] └────────────────────────────────────────────────")

        ctl = handle_control(line)
        if ctl is not None:
            reply_to_caller(reply_conn, f"systemapi:\n{ctl}")
            i += 1
            continue

        if handle_service_mgmt(line_lower) is not None:
            i += 1
            continue

        if line_lower.startswith("audioapi:"):
            if is_blocked("audioapi:"):
                reply_to_caller(reply_conn, "systemapi:\nService 'audioapi' is blocked.")
                i += 1
                continue
            msg = sender(line[len("audioapi:"):], api_key=api_key)
            log_command(line, msg)
            reply_to_caller(reply_conn, f"systemapi:\n{msg}")
            if done: done.set()
            i += 1
            continue

        if line_lower.startswith("os_command:"):
            payload = line[len("os_command:"):].strip()
            # Multi-line brace block: "os_command:{" opens a block whose closing
            # "}" lands on a later line. Track brace depth (every "{" opens, every
            # "}" closes) and keep pulling in lines until the outer brace closes,
            # so braces inside the command itself — e.g. an f-string's "{subnet}"
            # — are balanced out instead of ending the block early.
            if payload.startswith("{"):
                depth = payload.count("{") - payload.count("}")
                block = [payload]
                while depth > 0 and i + 1 < len(lines):
                    i += 1
                    block.append(lines[i])
                    depth += lines[i].count("{") - lines[i].count("}")
                payload = "\n".join(block)
            log_line = f"os_command:{payload}"
            cmd, timeout, kill_after = _parse_os_command(payload)
            blocked = find_blocked_command(cmd, load_command_blocks())
            if blocked:
                result_text = f"{{warning: command '{blocked}' is blocked}}"
                log_command(log_line, result_text)
                reply_to_caller(reply_conn, f"systemapi_security:\n{result_text}")
            else:
                # Launcher commands run detached without output capture.
                _t_cmd = time.time()
                print(f"[oscmd-debug] run_os_command START cmd={cmd[:60]!r} "
                      f"timeout={timeout} kill_after={kill_after} "
                      f"capture={not _is_launcher_cmd(cmd)}", flush=True)
                output = run_os_command(cmd, capture=not _is_launcher_cmd(cmd),
                                        timeout=timeout, kill_after=kill_after)
                print(f"[oscmd-debug] run_os_command RETURNED after "
                      f"{time.time()-_t_cmd:.1f}s len(output)={len(output)}", flush=True)
                log_command(log_line, output)
                reply_to_caller(reply_conn, f"systemapi:\n{output}")
            if done: done.set()
            i += 1
            continue

        if line_lower.startswith("internetapi:"):
            if is_blocked("internetapi:"):
                reply_to_caller(reply_conn, "systemapi:\nService 'internetapi' is blocked.")
                if done: done.set()
                i += 1
                continue
            rest = line[len("internetapi:"):]
            has_async = True

            def do_search(cmd=rest, original_line=line, conn=reply_conn, _done=done, _key=api_key):
                try:
                    result = sender_search(cmd, api_key=_key)
                    try:
                        data = json.loads(result)
                        if data.get("status") == "error":
                            reply_to_caller(conn, f"systemapi:\n{data.get('message', 'Unknown error')}")
                            return
                        elif not data.get("results"):
                            formatted = "systemapi:\nNo results found."
                        else:
                            formatted = "systemapi:\n"
                            for r in data["results"]:
                                formatted += f"{r['position']}. {r['title']}\n   URL: {r['url']}\n   {r['snippet']}\n\n"
                    except json.JSONDecodeError:
                        formatted = f"systemapi:\n{result}" if result.strip() else "systemapi:\ninternetapi:Not_connected"
                    log_command(original_line, formatted)
                    reply_to_caller(conn, formatted)
                except Exception as e:
                    _debug_error("internetapi", e)
                    reply_to_caller(conn, f"systemapi:\nError: {e}")
                finally:
                    if _done: _done.set()

            threading.Thread(target=do_search, daemon=True).start()
            i += 1
            continue

        if line_lower.startswith("tts:"):
            if is_blocked("tts:"):
                reply_to_caller(reply_conn, "systemapi:\nService 'tts' is blocked.")
                i += 1
                continue
            raw_content, i = collect_tts_block(lines, i)
            tts_text = extract_tts_text(raw_content)
            if tts_text.strip():
                log_command(line, tts_text)
                threading.Thread(target=send_to_tts, args=(tts_text,)).start()
            continue

        if line_lower.startswith("fileapi:"):
            if is_blocked("fileapi:"):
                reply_to_caller(reply_conn, "systemapi:\nService 'fileapi' is blocked.")
                if done: done.set()
                i += 1
                continue
            payload, i = collect_fileapi_block(lines, i)
            has_async = True

            def do_fileapi(cmds=payload, original_line=line, conn=reply_conn, _done=done, _key=api_key):
                try:
                    result = send_to_fileapi(cmds, api_key=_key)
                    log_command(original_line, result)
                    reply_to_caller(conn, f"systemapi:\n{result}")
                except Exception as e:
                    _debug_error("fileapi", e)
                    reply_to_caller(conn, f"systemapi:\nError: {e}")
                finally:
                    if _done: _done.set()

            threading.Thread(target=do_fileapi, daemon=True).start()
            continue

        services = load_services()
        matched  = False
        for key in services:
            if line_lower.startswith(key.lower()):
                if is_blocked(key):
                    reply_to_caller(reply_conn, f"systemapi:\nService '{key.rstrip(':')}' is blocked.")
                    if done: done.set()
                    matched = True
                    break
                has_async = True

                def do_send(k=key, original_line=line, conn=reply_conn, _done=done, _key=api_key):
                    try:
                        response = send_to_service(k, original_line, api_key=_key)
                        log_command(original_line, response)
                        reply_to_caller(conn, f"systemapi:\n{response}")
                    except Exception as e:
                        _debug_error(f"dynamic_service_{k}", e)
                        reply_to_caller(conn, f"systemapi:\nError: {e}")
                    finally:
                        if _done: _done.set()

                threading.Thread(target=do_send, daemon=True).start()
                matched = True
                break
        if line.strip() == "}":
            i += 1
            continue
        if not matched:
            _debug_error("api_no_handler", f"no handler for: {line[:80]}")
            log_command(line, "(no handler found)")
            reply_to_caller(reply_conn, f"systemapi:\nNo handler found for: {line}")
            if done: done.set()

        i += 1

    if done and not has_async:
        done.set()

def ensure_admin():
    """Relaunch elevated via UAC. If the user declines, keep running as a
    standard user instead of exiting."""
    if is_admin():
        return
    try:
        params = " ".join(f'"{a}"' for a in sys.argv)
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1)
        if ret > 32:
            # An elevated instance was started; this one can exit.
            sys.exit(0)
        print("[systemapi] Elevation declined; running as standard user.", flush=True)
    except Exception as e:
        _debug_error("ensure_admin", e)
        print("[systemapi] Could not elevate; running as standard user.", flush=True)


def ensure_password():
    """On first run, prompt the user to choose a password and create
    auth.json. Requires console input, so it must run before the server
    starts serving requests."""
    if os.path.exists(PASSWORD_FILE):
        return
    print("password file not found, please select password:", flush=True)
    while True:
        try:
            p1 = input("password: ")
            p2 = input("repeat password: ")
        except (EOFError, KeyboardInterrupt):
            print("\n[systemapi] No password entered; aborting.", flush=True)
            sys.exit(1)
        if p1 and p1 == p2:
            break
        print("passwords do not match, try again", flush=True)
    save_password({"pass": hash_password(p1), "login_required": True})
    print("[systemapi] auth.json created.", flush=True)


def warm_shell_token():
    """Pre-build the de-elevation token in the background so the first
    os_command does not pay the lookup cost."""
    if is_admin() and not load_config().get("admin", False):
        threading.Thread(target=_get_shell_token, daemon=True).start()


if __name__ == "__main__":
    try:
        # No self-elevation: systemapi runs as a standard user by default, and
        # os_command inherits that privilege. To run commands elevated, launch
        # this script as administrator deliberately (e.g. "Run as administrator").
        ensure_password()  # prompt for a password and create auth.json on first run
        load_config()  # create systemapi_config.json with defaults on first run
        socket_server()
    except KeyboardInterrupt:
        print("\nServer stopped by user")
    except Exception as e:
        _debug_error("main", e)
        print(f"Fatal error: {e}")

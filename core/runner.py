#!/usr/bin/env python3
# ══════════════════════════════════════════════════════════════════════════
#  runner.py — control-plane supervisor for the core servers
# --------------------------------------------------------------------------
#  Discovers and launches the project's servers (llm-server, chat-server and,
#  on Windows, systemapi) with the bundled portable Python, streams their logs,
#  and exposes a small Flask API + web dashboard (frontend/runner/) to start /
#  stop them and to register extra user services via POST /api/add_file.
#
#  Auth, token and network-trust behaviour mirror chat-server.py / systemapi.py
#  so credentials are shared across the stack (SHA-256 password in auth.json →
#  in-memory token, sent as the X-Auth-Token header).
# ══════════════════════════════════════════════════════════════════════════
import os
import sys
import json
import time
import shutil
import socket
import secrets
import hashlib
import getpass
import platform
import ipaddress
import threading
import subprocess
from collections import deque
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, Response

IS_WINDOWS = platform.system() == "Windows"
HERE       = os.path.dirname(os.path.abspath(__file__))


# ══════════════════════════════════════════════════════════════════
#  PATH DISCOVERY   (writes files_runner.json)
# ══════════════════════════════════════════════════════════════════
# The three core scripts runner.py supervises. systemapi is Windows-only.
def required_scripts():
    scripts = ["llm-server.py", "chat-server.py"]
    if IS_WINDOWS:
        scripts.append("systemapi.py")
    return scripts


def _has_core_layout(d):
    """True when *d* looks like the `core/` dir: the core scripts + python/."""
    if not all(os.path.exists(os.path.join(d, s)) for s in required_scripts()):
        return False
    return os.path.isdir(os.path.join(d, "python"))


def _write_files_runner(core_path, apis_path):
    try:
        with open(os.path.join(HERE, "files_runner.json"), "w", encoding="utf-8") as f:
            json.dump({"core_path": core_path, "apis_path": apis_path},
                      f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[runner] WARN could not write files_runner.json: {e}")


def discover_paths():
    """Resolve where the core scripts live.

    - If runner.py sits in the core dir (scripts + python/ alongside it) the
      paths are recorded as "default".
    - Otherwise, when runner.py was moved out of a folder *not* named `core`,
      its immediate subdirectories are searched for a folder named `core`; the
      absolute paths are recorded.
    Returns (core_path, apis_path) as absolute paths for runtime use.
    """
    if _has_core_layout(HERE):
        _write_files_runner("default", "default")
        return HERE, os.path.join(HERE, "apis")

    if os.path.basename(HERE.rstrip("/\\")).lower() != "core":
        try:
            entries = sorted(os.listdir(HERE))
        except OSError:
            entries = []
        for name in entries:
            sub = os.path.join(HERE, name)
            if os.path.isdir(sub) and name.lower() == "core" and _has_core_layout(sub):
                apis = os.path.join(sub, "apis")
                _write_files_runner(sub, apis)
                return sub, apis

    # Fallback: assume the scripts live next to runner.py even if incomplete.
    _write_files_runner("default", "default")
    return HERE, os.path.join(HERE, "apis")


CORE_PATH, APIS_PATH = discover_paths()
FRONTEND_DIR         = os.path.join(CORE_PATH, "frontend")
RUNNER_FRONTEND_DIR  = os.path.join(FRONTEND_DIR, "runner")

AUTH_FILE          = os.path.join(CORE_PATH, "auth.json")
RUNNER_CONFIG_FILE = os.path.join(CORE_PATH, "runner-settings.json")
RUNNER_ADDED_FILE  = os.path.join(CORE_PATH, "runner_added.json")


def bundled_python():
    """Path to the portable Python 3.10 shipped under core/python/."""
    if IS_WINDOWS:
        return os.path.join(CORE_PATH, "python", "python.exe")
    return os.path.join(CORE_PATH, "python", "bin", "python3")


def system_python():
    """The OS-installed Python (used for added files unless 3.10 is requested)."""
    for cand in ("python3", "python"):
        found = shutil.which(cand)
        if found:
            return found
    return sys.executable


# ══════════════════════════════════════════════════════════════════
#  PASSWORD / LOGIN   (same scheme as chat-server.py)
# ══════════════════════════════════════════════════════════════════
def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_pw_cfg():
    if not os.path.exists(AUTH_FILE):
        return {"pass": "", "login_required": True}
    with open(AUTH_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_pw_cfg(cfg):
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _is_login_required():
    try:
        return _load_pw_cfg().get("login_required", True)
    except Exception:
        return True


_tokens = {}
_tokens_lock = threading.Lock()
TOKEN_TTL = 86400  # 24h, matches chat-server


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


def ensure_auth():
    """Validate auth.json on startup; if missing/corrupt/passwordless, prompt
    interactively to set a password (two matching entries → SHA-256)."""
    cfg = None
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = None

    if isinstance(cfg, dict) and cfg.get("pass"):
        return cfg  # valid

    print("\n[runner] auth.json is missing, corrupt, or has no password.")
    print("[runner] Set a password to protect the runner (entered twice).")
    existing = cfg if isinstance(cfg, dict) else {}
    while True:
        try:
            pw1 = getpass.getpass("  New password: ")
            pw2 = getpass.getpass("  Confirm password: ")
        except (EOFError, KeyboardInterrupt):
            print("\n[runner] password setup aborted — exiting.")
            sys.exit(1)
        if not pw1.strip():
            print("  ! password cannot be empty")
            continue
        if pw1 != pw2:
            print("  ! entries did not match, try again")
            continue
        break

    new_cfg = {
        "pass": _sha256(pw1),
        "login_required": True,
        "openaikey": existing.get("openaikey", "frtgg"),
    }
    _save_pw_cfg(new_cfg)
    print("[runner] password saved to auth.json\n")
    return new_cfg


# ══════════════════════════════════════════════════════════════════
#  RUNNER CONFIG  (port + network flags)
# ══════════════════════════════════════════════════════════════════
RUNNER_CONFIG_DEFAULT = {
    "port": 8721,
    "allowed_public_network": False,
    "allowed_private_network": False,
}


def load_runner_config():
    cfg = dict(RUNNER_CONFIG_DEFAULT)
    try:
        with open(RUNNER_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: data[k] for k in RUNNER_CONFIG_DEFAULT if k in data})
    except Exception:
        pass
    return cfg


def save_runner_config(cfg):
    merged = dict(RUNNER_CONFIG_DEFAULT)
    merged.update({k: cfg[k] for k in RUNNER_CONFIG_DEFAULT if k in cfg})
    with open(RUNNER_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    return merged


# ══════════════════════════════════════════════════════════════════
#  NETWORK TRUST  (mirrors systemapi.is_allowed, localhost-default)
# ══════════════════════════════════════════════════════════════════
LOCALHOST = {"127.0.0.1", "::1", "localhost"}


def is_localhost(ip):
    return ip in LOCALHOST


def is_lan_ip(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private and not addr.is_loopback


def network_allowed(ip):
    if is_localhost(ip):
        return True
    cfg = load_runner_config()
    if is_lan_ip(ip):
        return bool(cfg.get("allowed_private_network", False))
    return bool(cfg.get("allowed_public_network", False))


# ══════════════════════════════════════════════════════════════════
#  PROCESS MANAGEMENT
# ══════════════════════════════════════════════════════════════════
class Service:
    """A supervised child process with a bounded, sequence-numbered log buffer."""

    def __init__(self, name, cmd, interpreter, source=None, removable=False,
                 python_flag=None, group="core", port=None, cwd=None):
        self.name        = name
        self.cmd         = cmd          # list[str]
        self.interpreter = interpreter  # human label: "bundled" / "system" / "exe" / ...
        self.source      = source       # script/exe path
        self.removable   = removable    # added files can be removed; core cannot
        self.python_flag = python_flag  # persisted "python" value for added files
        self.group       = group        # "core" / "apis" / "added"
        self.port        = port          # known listening port (for liveness probe)
        self.cwd         = cwd or CORE_PATH
        self.proc        = None
        self._live       = False        # last liveness result (refreshed by monitor)
        self.ports       = []           # actual LISTENing ports (refreshed by monitor)
        self.logs        = deque(maxlen=3000)  # (seq, text)
        self._seq        = 0
        self._lock       = threading.Lock()

    def owned_running(self):
        """True only when this runner launched the process and it is alive."""
        p = self.proc
        return p is not None and p.poll() is None

    def status(self):
        # Running if we own a live process, OR the monitor saw it alive
        # (port listening or a process running this service's file) — covers
        # servers started outside this runner / in a previous session.
        if self.owned_running():
            return "running"
        if self._live:
            return "running"
        return "stopped"

    def pid(self):
        p = self.proc
        if p is not None and p.poll() is None:
            return p.pid
        return None

    def _append(self, text):
        with self._lock:
            self._seq += 1
            self.logs.append((self._seq, text.rstrip("\n")))

    def _reader(self, proc):
        try:
            for line in iter(proc.stdout.readline, ""):
                if line == "":
                    break
                # Echo to runner's own terminal so logs are visible there too.
                sys.stdout.write(f"[{self.name}] {line}")
                sys.stdout.flush()
                self._append(line)
        except Exception as e:
            self._append(f"<reader error: {e}>")
        finally:
            code = proc.poll()
            self._append(f"<process exited, code={code}>")
            # Reflect the exit immediately (the 5s monitor re-checks for any
            # surviving external instance on its next pass).
            self._live = False
            self.ports = []

    def start(self):
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "already_running"
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
            )
        except Exception as e:
            self._append(f"<failed to start: {e}>")
            return False, str(e)
        self.proc = proc
        self._append(f"<started: {' '.join(self.cmd)}  (pid {proc.pid})>")
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        return True, "started"

    def stop(self):
        p = self.proc
        # Case 1: a process this runner launched — terminate it directly.
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=5)
            except Exception as e:
                return False, str(e)
            self._append("<stopped>")
            return True, "stopped"

        # Case 2: not owned by us — kill anything listening on the known port
        # AND any process whose command line runs this service's file (started
        # externally or in a previous session).
        pids = set(_pids_on_port(self.port)) | set(_pids_for_source(self.source))
        pids.discard(os.getpid())
        if not pids:
            return False, "not_running"
        killed, errors = [], []
        for pid in pids:
            ok, msg = _kill_pid(pid)
            (killed if ok else errors).append(pid if ok else f"{pid}:{msg}")
        if killed:
            self._live = False
            self._append(f"<killed external process(es) {killed}>")
            return True, f"killed_external ({len(killed)})"
        return False, "; ".join(errors) or "kill_failed"

    def tail(self, since=0):
        with self._lock:
            lines = [(s, t) for (s, t) in self.logs if s > since]
            last = self.logs[-1][0] if self.logs else since
        return lines, last


_services = {}
_services_lock = threading.Lock()


def _register(service):
    with _services_lock:
        _services[service.name] = service
    return service


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _port_listening(port, host="127.0.0.1", timeout=0.4):
    """True if something is accepting TCP connections on host:port."""
    if not port:
        return False
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _pids_on_port(port):
    """All PIDs LISTENING on *port* (uses psutil). Returns a list (possibly
    empty); a port may have several instances when SO_REUSEADDR is used."""
    if not port:
        return []
    try:
        import psutil
    except Exception:
        return []
    pids = []
    try:
        for c in psutil.net_connections(kind="inet"):
            if (c.status == psutil.CONN_LISTEN and c.laddr
                    and c.laddr.port == int(port) and c.pid
                    and c.pid not in pids):
                pids.append(c.pid)
    except Exception:
        return []
    return pids


def _pids_for_source(source):
    """All PIDs whose command line runs *source* (the service's .py/.exe path).
    Lets the runner detect/kill a service even when its port is unknown."""
    if not source:
        return []
    try:
        import psutil
    except Exception:
        return []
    src = os.path.normcase(os.path.abspath(source))
    me  = os.getpid()
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.info["pid"] == me:
                continue
            cl = " ".join(p.info.get("cmdline") or [])
            if cl and src in os.path.normcase(cl):
                out.append(p.info["pid"])
        except Exception:
            continue
    return out


def _kill_pid(pid):
    """Terminate (then kill) a process by PID via psutil."""
    try:
        import psutil
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        return True, "killed"
    except Exception as e:
        return False, str(e)


def _core_port(name):
    """Resolve the known HTTP/TCP port of a core service from its config."""
    if name == "chat-server":
        return _read_json(os.path.join(CORE_PATH, "chat_ports.json")).get("chat-server", 8900)
    if name == "llm-server":
        s = _read_json(os.path.join(CORE_PATH, "llm-server_settings.json"))
        return (s.get("llm-server_HTTP")
                or _read_json(os.path.join(CORE_PATH, "backend_servers.json"))
                   .get("llm_server", {}).get("http_port", 8112))
    if name == "systemapi":
        return _read_json(os.path.join(CORE_PATH, "backend_servers.json")) \
               .get("systemapi", {}).get("port", 8300)
    return None


def build_core_services():
    interp = bundled_python()
    core = [("llm-server.py", "llm-server"), ("chat-server.py", "chat-server")]
    if IS_WINDOWS:
        core.append(("systemapi.py", "systemapi"))
    for fname, name in core:
        path = os.path.join(CORE_PATH, fname)
        _register(Service(name, [interp, path], "bundled", source=path,
                          removable=False, group="core", port=_core_port(name)))


def _apis_cmd(path):
    """(cmd, label) to launch an apis file, or (None, None) if its type is not
    supported on this OS. Windows: .py/.exe; Linux/macOS: .py/.sh."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        return [bundled_python(), path], "bundled"
    if ext == ".exe" and IS_WINDOWS:
        return [path], "exe"
    if ext == ".sh" and not IS_WINDOWS:
        return ["bash", path], "sh"
    return None, None


def build_apis_services():
    """Auto-discover every runnable file in core/apis as an `apis`-group
    service (.py everywhere, .exe on Windows, .sh on Linux/macOS)."""
    if not os.path.isdir(APIS_PATH):
        return
    for fname in sorted(os.listdir(APIS_PATH)):
        path = os.path.join(APIS_PATH, fname)
        if not os.path.isfile(path):
            continue
        cmd, label = _apis_cmd(path)
        if cmd is None:
            continue
        name = _unique_name(os.path.splitext(fname)[0])
        # No guessed port for apis (it collided with core ports, e.g. tts vs
        # llm on 8112); liveness + kill use the process command line instead.
        _register(Service(name, cmd, label, source=path, removable=False,
                          group="apis", port=None, cwd=APIS_PATH))


# ── Liveness monitor (refreshes status + listening ports every 5s) ──
def _build_proc_maps():
    """One snapshot of the process table. Returns:
        proc_index : list of (pid, normalized cmdline)  [excludes the runner]
        children   : dict ppid -> [child pids]          [whole tree]
        listen     : dict pid  -> set of LISTENing ports
    """
    proc_index, children, listen = [], {}, {}
    try:
        import psutil
    except Exception:
        return proc_index, children, listen
    me = os.getpid()
    # Listening ports grouped by owning pid.
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.pid:
                listen.setdefault(conn.pid, set()).add(conn.laddr.port)
    except Exception:
        pass
    # Process index + parent→children tree (used to catch child-held ports).
    for p in psutil.process_iter(["pid", "ppid", "cmdline"]):
        try:
            pid  = p.info["pid"]
            ppid = p.info.get("ppid")
            if ppid is not None:
                children.setdefault(ppid, []).append(pid)
            if pid == me:
                continue
            cl = " ".join(p.info.get("cmdline") or [])
            if cl:
                proc_index.append((pid, os.path.normcase(cl)))
        except Exception:
            continue
    return proc_index, children, listen


def _descendants(pid, children):
    """All transitive child pids of *pid*."""
    out, stack = set(), [pid]
    while stack:
        for ch in children.get(stack.pop(), []):
            if ch not in out:
                out.add(ch)
                stack.append(ch)
    return out


def refresh_liveness():
    """Re-probe every service now: liveness (command line) + the actual ports
    the running process tree is LISTENing on."""
    proc_index, children, listen = _build_proc_maps()
    with _services_lock:
        svcs = list(_services.values())
    for s in svcs:
        # Collect the root pids belonging to this service.
        roots = set()
        if s.owned_running():
            roots.add(s.proc.pid)
        if s.source:
            src  = os.path.normcase(os.path.abspath(s.source))
            base = os.path.normcase(os.path.basename(src))
            for pid, cl in proc_index:
                if src in cl or base in cl:
                    roots.add(pid)
        # Expand to the whole process tree so child-held ports are included.
        all_pids = set(roots)
        for r in roots:
            all_pids |= _descendants(r, children)
        ports = set()
        for pid in all_pids:
            ports |= listen.get(pid, set())
        s._live  = bool(roots)
        s.ports  = sorted(ports)


def _monitor_loop():
    while True:
        refresh_liveness()
        time.sleep(5)


def start_monitor():
    threading.Thread(target=_monitor_loop, daemon=True).start()


def rediscover_apis():
    """Pick up files added to / removed from core/apis since startup."""
    if not os.path.isdir(APIS_PATH):
        return
    with _services_lock:
        existing = {os.path.normcase(os.path.abspath(s.source)): s
                    for s in _services.values() if s.group == "apis" and s.source}
    present = set()
    for fname in sorted(os.listdir(APIS_PATH)):
        path = os.path.join(APIS_PATH, fname)
        if not os.path.isfile(path):
            continue
        cmd, label = _apis_cmd(path)
        if cmd is None:
            continue
        norm = os.path.normcase(os.path.abspath(path))
        present.add(norm)
        if norm in existing:
            continue  # already registered
        name = _unique_name(os.path.splitext(fname)[0])
        # No guessed port for apis (it collided with core ports, e.g. tts vs
        # llm on 8112); liveness + kill use the process command line instead.
        _register(Service(name, cmd, label, source=path, removable=False,
                          group="apis", port=None, cwd=APIS_PATH))
    # Drop apis services whose file is gone and that aren't running.
    with _services_lock:
        for norm, s in list(existing.items()):
            if norm not in present and not s.owned_running() and not s._live:
                _services.pop(s.name, None)


# ── Added-file persistence ─────────────────────────────────────────
def _load_added():
    try:
        with open(RUNNER_ADDED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_added(entries):
    with open(RUNNER_ADDED_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def _added_extensions():
    return {".exe", ".py"} if IS_WINDOWS else {".py", ".sh"}


def _unique_name(base):
    name = base
    i = 2
    with _services_lock:
        while name in _services:
            name = f"{base}-{i}"
            i += 1
    return name


def _build_added_service(filepath, python_flag, name=None):
    """Construct (not register) a Service for an added file. Raises ValueError
    on validation failure."""
    filepath = os.path.abspath(filepath)
    if not os.path.isfile(filepath):
        raise ValueError("file_not_found")
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in _added_extensions():
        allowed = ", ".join(sorted(_added_extensions()))
        raise ValueError(f"bad_extension (allowed: {allowed})")

    if ext == ".py":
        if str(python_flag) == "3.10":
            cmd, label = [bundled_python(), filepath], "bundled 3.10"
        else:
            cmd, label = [system_python(), filepath], "system"
    elif ext == ".exe":
        cmd, label = [filepath], "exe"
    elif ext == ".sh":
        cmd, label = ["bash", filepath], "sh"
    else:  # unreachable
        raise ValueError("bad_extension")

    svc_name = name or _unique_name(os.path.splitext(os.path.basename(filepath))[0])
    return Service(svc_name, cmd, label, source=filepath, removable=True,
                   python_flag=python_flag, group="added",
                   cwd=os.path.dirname(filepath))


def reload_added_services():
    """Recreate + auto-start every persisted added file on startup."""
    cleaned = []
    for entry in _load_added():
        try:
            svc = _build_added_service(entry.get("file", ""),
                                       entry.get("python"),
                                       name=entry.get("name"))
        except ValueError as e:
            print(f"[runner] skip added file {entry!r}: {e}")
            continue
        _register(svc)
        svc.start()
        cleaned.append({"file": svc.source, "python": svc.python_flag, "name": svc.name})
    if cleaned:
        _save_added(cleaned)


# ══════════════════════════════════════════════════════════════════
#  FLASK APP
# ══════════════════════════════════════════════════════════════════
runner_app = Flask(__name__)


def _ts():
    return time.strftime("%H:%M:%S")


@runner_app.before_request
def _gate():
    ip = request.remote_addr or ""
    if not network_allowed(ip):
        return jsonify({"status": "error", "message": "forbidden_network"}), 403


@runner_app.after_request
def _cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Auth-Token"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    r.headers["Access-Control-Max-Age"]       = "600"
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"]        = "no-cache"
    r.headers["Expires"]       = "0"
    return r


@runner_app.route("/", defaults={"p": ""}, methods=["OPTIONS"])
@runner_app.route("/<path:p>", methods=["OPTIONS"])
def _options(p):
    return jsonify({}), 200


# ── Auth endpoints ─────────────────────────────────────────────────
@runner_app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(force=True, silent=True) or {}
    pw   = body.get("password", "")
    cfg  = _load_pw_cfg()
    if not cfg.get("pass", ""):
        return jsonify({"status": "error", "message": "no_password_set"}), 400
    if _sha256(pw) != cfg["pass"]:
        return jsonify({"status": "error", "message": "wrong_password"}), 401
    return jsonify({"status": "ok", "token": _issue_token(),
                    "login_required": cfg.get("login_required", True)}), 200


@runner_app.route("/api/change_password", methods=["POST"])
@require_login
def change_password():
    body    = request.get_json(force=True, silent=True) or {}
    new_pw  = body.get("new_password", "")
    if not new_pw.strip():
        return jsonify({"status": "error", "message": "empty_password"}), 400

    cfg = _load_pw_cfg()
    if not is_localhost(request.remote_addr or ""):
        # From outside localhost the old password is required.
        old_pw = body.get("old_password", "")
        if not cfg.get("pass"):
            return jsonify({"status": "error", "message": "no_password_set"}), 400
        if _sha256(old_pw) != cfg["pass"]:
            return jsonify({"status": "error", "message": "wrong_password"}), 401

    cfg["pass"] = _sha256(new_pw)
    cfg.setdefault("login_required", True)
    _save_pw_cfg(cfg)
    # Invalidate existing tokens so old sessions must re-auth.
    with _tokens_lock:
        _tokens.clear()
    return jsonify({"status": "ok"}), 200


# ── Service control ────────────────────────────────────────────────
def _service_view(svc):
    return {
        "name": svc.name,
        "status": svc.status(),
        "owned": svc.owned_running(),
        "pid": svc.pid(),
        "interpreter": svc.interpreter,
        "source": svc.source,
        "removable": svc.removable,
        "group": svc.group,
        "port": svc.port,
        "ports": svc.ports,
    }


@runner_app.route("/api/services", methods=["GET"])
@require_login
def list_services():
    with _services_lock:
        items = [_service_view(s) for s in _services.values()]
    return jsonify({"status": "ok", "services": items}), 200


@runner_app.route("/api/start", methods=["POST"])
@require_login
def start_service():
    name = (request.get_json(force=True, silent=True) or {}).get("name", "")
    with _services_lock:
        svc = _services.get(name)
    if not svc:
        return jsonify({"status": "error", "message": "unknown_service"}), 404
    ok, msg = svc.start()
    return jsonify({"status": "ok" if ok else "error", "message": msg}), 200


@runner_app.route("/api/stop", methods=["POST"])
@require_login
def stop_service():
    name = (request.get_json(force=True, silent=True) or {}).get("name", "")
    with _services_lock:
        svc = _services.get(name)
    if not svc:
        return jsonify({"status": "error", "message": "unknown_service"}), 404
    ok, msg = svc.stop()
    return jsonify({"status": "ok" if ok else "error", "message": msg}), 200


@runner_app.route("/api/start_all", methods=["POST"])
@require_login
def start_all():
    with _services_lock:
        svcs = list(_services.values())
    for s in svcs:
        s.start()
    return jsonify({"status": "ok"}), 200


@runner_app.route("/api/stop_all", methods=["POST"])
@require_login
def stop_all():
    with _services_lock:
        svcs = list(_services.values())
    for s in svcs:
        s.stop()
    return jsonify({"status": "ok"}), 200


@runner_app.route("/api/start_group", methods=["POST"])
@require_login
def start_group():
    group = (request.get_json(force=True, silent=True) or {}).get("group", "")
    with _services_lock:
        svcs = [s for s in _services.values() if s.group == group]
    for s in svcs:
        s.start()
    return jsonify({"status": "ok", "count": len(svcs)}), 200


@runner_app.route("/api/stop_group", methods=["POST"])
@require_login
def stop_group():
    group = (request.get_json(force=True, silent=True) or {}).get("group", "")
    with _services_lock:
        svcs = [s for s in _services.values() if s.group == group]
    for s in svcs:
        s.stop()
    return jsonify({"status": "ok", "count": len(svcs)}), 200


@runner_app.route("/api/refresh", methods=["POST"])
@require_login
def refresh_services():
    """Force a full re-detection now: re-scan core/apis for new/removed files
    and immediately re-probe liveness for every service."""
    rediscover_apis()
    refresh_liveness()
    with _services_lock:
        items = [_service_view(s) for s in _services.values()]
    return jsonify({"status": "ok", "services": items}), 200


@runner_app.route("/api/events", methods=["GET"])
@require_login
def events():
    """Server-Sent Events stream: pushes status snapshots whenever any service
    changes, and log lines for the watched service (?name=) in real time.
    EventSource can't set headers, so auth uses ?token= (handled by
    require_login). """
    name = request.args.get("name", "")

    def gen():
        last_seq = 0
        last_status_key = None
        ticks = 0
        try:
            while True:
                # ── new log lines for the watched service ──
                with _services_lock:
                    svc = _services.get(name)
                if svc is not None:
                    lines, last_seq = svc.tail(last_seq)
                    for s, t in lines:
                        yield ("event: log\ndata: "
                               + json.dumps({"seq": s, "text": t}) + "\n\n")
                # ── status snapshot, only when it changed ──
                with _services_lock:
                    snap = [_service_view(s) for s in _services.values()]
                key = json.dumps([(x["name"], x["status"], x["pid"], x["ports"]) for x in snap])
                if key != last_status_key:
                    last_status_key = key
                    yield "event: status\ndata: " + json.dumps(snap) + "\n\n"
                # ── periodic heartbeat to keep the connection open ──
                ticks += 1
                if ticks % 30 == 0:
                    yield ": ping\n\n"
                time.sleep(0.4)
        except GeneratorExit:
            return

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"]     = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@runner_app.route("/api/logs", methods=["GET"])
@require_login
def logs():
    name  = request.args.get("name", "")
    since = request.args.get("since", "0")
    try:
        since = int(since)
    except ValueError:
        since = 0
    with _services_lock:
        svc = _services.get(name)
    if not svc:
        return jsonify({"status": "error", "message": "unknown_service"}), 404
    lines, last = svc.tail(since)
    return jsonify({"status": "ok", "since": last,
                    "lines": [{"seq": s, "text": t} for (s, t) in lines]}), 200


# ── Add file ───────────────────────────────────────────────────────
@runner_app.route("/api/add_file", methods=["POST"])
@require_login
def add_file():
    body        = request.get_json(force=True, silent=True) or {}
    filepath    = (body.get("file") or "").strip()
    python_flag = body.get("python")
    if not filepath:
        return jsonify({"status": "error", "message": "missing_file"}), 400
    try:
        svc = _build_added_service(filepath, python_flag)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    _register(svc)
    svc.start()

    entries = _load_added()
    entries = [e for e in entries if e.get("name") != svc.name]
    entries.append({"file": svc.source, "python": svc.python_flag, "name": svc.name})
    _save_added(entries)

    return jsonify({"status": "ok", "service": _service_view(svc)}), 200


@runner_app.route("/api/remove_file", methods=["POST"])
@require_login
def remove_file():
    name = (request.get_json(force=True, silent=True) or {}).get("name", "")
    with _services_lock:
        svc = _services.get(name)
    if not svc or not svc.removable:
        return jsonify({"status": "error", "message": "not_removable"}), 400
    svc.stop()
    with _services_lock:
        _services.pop(name, None)
    _save_added([e for e in _load_added() if e.get("name") != name])
    return jsonify({"status": "ok"}), 200


# ── Settings ───────────────────────────────────────────────────────
@runner_app.route("/api/config", methods=["GET"])
@require_login
def get_config():
    return jsonify({"status": "ok", "config": load_runner_config()}), 200


@runner_app.route("/api/config", methods=["POST"])
@require_login
def set_config():
    body = request.get_json(force=True, silent=True) or {}
    cfg = load_runner_config()
    for key in ("allowed_public_network", "allowed_private_network"):
        if key in body:
            cfg[key] = bool(body[key])
    saved = save_runner_config(cfg)
    return jsonify({"status": "ok", "config": saved}), 200


# ── Static frontend ────────────────────────────────────────────────
@runner_app.route("/", methods=["GET"])
def _index():
    return send_from_directory(RUNNER_FRONTEND_DIR, "index.html")


@runner_app.route("/<path:p>", methods=["GET"])
def _static(p):
    # Serve runner assets first, then fall back to shared frontend assets
    # (logo.png, favicon.*) that live one level up in frontend/.
    if os.path.isfile(os.path.join(RUNNER_FRONTEND_DIR, p)):
        return send_from_directory(RUNNER_FRONTEND_DIR, p)
    if os.path.isfile(os.path.join(FRONTEND_DIR, p)):
        return send_from_directory(FRONTEND_DIR, p)
    return jsonify({"status": "error", "message": "not_found"}), 404


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    print("══════════════════════════════════════════════════════")
    print(" runner.py — core server supervisor")
    print(f" core_path : {CORE_PATH}")
    print(f" apis_path : {APIS_PATH}")
    print(f" python    : {bundled_python()}")
    print("══════════════════════════════════════════════════════")

    ensure_auth()
    build_core_services()
    build_apis_services()
    reload_added_services()
    start_monitor()  # refresh port-based liveness every 5s

    with _services_lock:
        print("[runner] services: " + ", ".join(_services.keys()))

    cfg  = load_runner_config()
    if not os.path.exists(RUNNER_CONFIG_FILE):
        # Seed the settings file so the port / network flags are editable.
        save_runner_config(cfg)
    port = int(cfg.get("port", 8721))
    print(f"[runner] listening on http://0.0.0.0:{port}  "
          f"(localhost only unless network flags enabled)")
    runner_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

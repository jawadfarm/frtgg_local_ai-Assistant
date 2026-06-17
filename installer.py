#!/usr/bin/env python3
"""
installer.py
────────────
Bootstraps the runtime environment the whole project runs on. Unlike
installer_llama.py (which downloads llama.cpp), this script:

  1. Provisions a self-contained Python 3.10 under  core/python
  2. Installs the dependency sets (core, systemapi [Windows], tts [optional])
  3. Seeds the JSON config files the servers expect, with defaults pulled
     straight from the code
  4. Prompts for a password -> stores its SHA256 in core/auth.json and sets
     the OpenAI bearer key to the same password (mirrors llm-server's
     first-run behaviour)

Python 3.10 is required because apis/tts-copilt.py / requirements-tts.txt only
run on 3.10. Works on Windows and Linux. This script itself uses ONLY the
standard library, so it runs under whatever Python you already have.

Usage:
    python installer.py            # install (idempotent — skips what exists)
    python installer.py --force    # redo download + overwrite all config files
"""

import argparse
import getpass
import hashlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

# ── Paths ──────────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(ROOT, "core")
PY_DIR = os.path.join(CORE, "python")

# ── Python 3.10 portable build (python-build-standalone, install_only) ───────
# To refresh: pick a newer release at
#   https://github.com/astral-sh/python-build-standalone/releases
# and update RELEASE_TAG / PY_VERSION. The asset name pattern is stable.
RELEASE_TAG = "20260610"
PY_VERSION = "3.10.20"
_BASE_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{RELEASE_TAG}/cpython-{PY_VERSION}+{RELEASE_TAG}-"
)

# Maps (system, machine) -> python-build-standalone target triple.
_TRIPLES = {
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
    ("Windows", "x86_64"): "x86_64-pc-windows-msvc",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("Linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
}

IS_WINDOWS = platform.system() == "Windows"

# ── Requirements files (relative to repo root) ───────────────────────────────
REQ_CORE = "requirements.txt"
REQ_SYSTEMAPI = "requirements-systemapi.txt"  # Windows only
REQ_TTS = "requirements-tts.txt"  # heavy, optional

# ── Config defaults (sourced from the code) ──────────────────────────────────
# services.json — systemapi routing map. Keys carry a trailing colon
# (core/systemapi.py: services.get("fileapi:"), etc.). audioapi excluded.
# reconweb -> apis/farm_client.py (listens on 8716, forwards web-recon/fetch
# commands to the Farm Harvest server on 8901). Routed generically via
# core/systemapi.py send_to_service("reconweb:").
SERVICES = {
    "fileapi:": {"ip": "127.0.0.1", "port": 8910},
    "internetapi:": {"ip": "127.0.0.1", "port": 8400},
    "tts:": {"ip": "127.0.0.1", "port": 8112},
    "reconweb:": {"ip": "127.0.0.1", "port": 8716},
}
# systemapi_config.json — core/systemapi.py defaults.
SYSTEMAPI_CONFIG = {
    "admin": False,
    "ip": [],
    "local_network": True,
    "public_network": True,
}
# backend_servers.json — core/chat-server.py DEFAULT_CONFIG.
BACKEND_SERVERS = {
    "llm_server": {"host": "127.0.0.1", "port": 8111, "http_port": 8112},
    "systemapi": {"host": "127.0.0.1", "port": 8300},
    "promptapi": {"host": "127.0.0.1", "port": 8202, "http_port": 8203},
}
# commands.json — command-prefix defs the frontend renders and chat-server
# matches (core/chat-server.py _load_commands / _match_command_def). A JSON
# array; each entry: prefix, color, label, braces, optional end/timeout.
COMMANDS = [
    {"prefix": "internetapi:", "color": "#ffb300", "label": "Internet API", "braces": False},
    {"prefix": "reconweb:", "color": "#b388ff", "label": "Recon Web", "braces": False},
    {"prefix": "fileapi:", "color": "#00e5ff", "label": "fileapi", "braces": True, "end": True},
    {"prefix": "tts:", "color": "#c66f0c", "label": "Text To speak.", "braces": True, "end": False},
    {"prefix": "os_command:", "color": "#ffbb00", "label": "os command", "braces": True, "end": False, "timeout": True},
]


# ── Small helpers ────────────────────────────────────────────────────────────
def log(msg):
    print(f"[installer] {msg}", flush=True)


def fail(msg, code=1):
    print(f"[installer] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def python_exe():
    """Path to the interpreter inside core/python (regardless of OS)."""
    if IS_WINDOWS:
        return os.path.join(PY_DIR, "python.exe")
    return os.path.join(PY_DIR, "bin", "python3")


def installed_version(exe):
    """Return the 'X.Y.Z' version string the interpreter reports, or None."""
    if not os.path.exists(exe):
        return None
    try:
        out = subprocess.run(
            [exe, "-c", "import platform;print(platform.python_version())"],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def write_json(path, data, force):
    """Write pretty JSON; skip (unless force) if the file already exists."""
    name = os.path.basename(path)
    if os.path.exists(path) and not force:
        log(f"  skip {name} (already exists — use --force to overwrite)")
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log(f"  wrote {name}")
    return True


# ── Step 1: acquire Python 3.10 ──────────────────────────────────────────────
def acquire_python(force):
    exe = python_exe()
    have = installed_version(exe)
    if have and have.startswith("3.10.") and not force:
        log(f"Python {have} already present at {PY_DIR} — skipping download.")
        return exe

    key = (platform.system(), platform.machine())
    triple = _TRIPLES.get(key)
    if not triple:
        fail(f"unsupported platform {key}; no python-build-standalone target. "
             f"Supported: {sorted(_TRIPLES)}")
    url = f"{_BASE_URL}{triple}-install_only.tar.gz"

    if force and os.path.isdir(PY_DIR):
        log(f"--force: removing existing {PY_DIR}")
        shutil.rmtree(PY_DIR, ignore_errors=True)

    os.makedirs(CORE, exist_ok=True)
    log(f"Downloading Python {PY_VERSION} ({triple})")
    log(f"  {url}")

    ctx = ssl.create_default_context()
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "installer.py"})
        with urllib.request.urlopen(req, context=ctx) as resp, open(tmp_path, "wb") as out:
            shutil.copyfileobj(resp, out)
        log(f"  downloaded {os.path.getsize(tmp_path) // (1024 * 1024)} MB; extracting…")
        # Archive has a top-level 'python/' dir -> extract into CORE => core/python/
        with tarfile.open(tmp_path, "r:gz") as tar:
            _safe_extract(tar, CORE)
    except Exception as e:
        fail(f"failed to download/extract Python: {e}")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    have = installed_version(exe)
    if not have or not have.startswith("3.10."):
        fail(f"extraction finished but {exe} does not report 3.10.x (got {have!r})")
    log(f"Python {have} ready at {PY_DIR}")
    return exe


def _safe_extract(tar, dest):
    """Extract guarding against path traversal (../ and absolute members)."""
    dest_abs = os.path.abspath(dest)
    for member in tar.getmembers():
        target = os.path.abspath(os.path.join(dest, member.name))
        if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
            raise RuntimeError(f"unsafe path in archive: {member.name}")
    tar.extractall(dest)


# ── Helper to detect NVIDIA GPU ─────────────────────────────────────────────
def _has_nvidia_gpu():
    """Return True if nvidia-smi runs successfully and lists a GPU."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0 and len(result.stdout.strip()) > 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


# ── Install PyTorch for TTS (GPU‑aware) ─────────────────────────────────────
def _install_torch_tts(exe):
    """Install torch/torchvision/torchaudio suitable for the detected GPU."""
    # Uninstall any existing torch packages to avoid conflicts
    log("Removing any existing PyTorch packages…")
    subprocess.run([exe, "-m", "pip", "uninstall", "-y",
                    "torch", "torchvision", "torchaudio"], check=False)

    if _has_nvidia_gpu():
        log("NVIDIA GPU detected → installing CUDA 12.4 build (cu124)…")
        pip_install(
            exe,
            "--index-url", "https://download.pytorch.org/whl/cu124",
            "torch==2.6.0+cu124",
            "torchvision==0.21.0+cu124",
            "torchaudio==2.6.0+cu124"
        )
    else:
        log("No NVIDIA GPU detected → installing CPU build…")
        pip_install(
            exe,
            "torch==2.6.0",
            "torchvision==0.21.0",
            "torchaudio==2.6.0"
        )
        # For AMD / Intel GPUs on Windows, add DirectML acceleration
        if IS_WINDOWS:
            log("Windows detected → adding torch-directml for AMD/Intel GPU support.")
            pip_install(exe, "torch-directml")


def _install_non_torch_requirements(exe, requirements_path):
    """Install packages from a requirements file, skipping torch lines."""
    with open(requirements_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    filtered = []
    for line in lines:
        stripped = line.strip()
        # Skip comments, blank lines, and anything that starts with torch / torchvision / torchaudio
        if (not stripped
                or stripped.startswith("#")
                or stripped.startswith("torch==")
                or stripped.startswith("torchvision==")
                or stripped.startswith("torchaudio==")
                or stripped.startswith("torch-directml")):
            continue
        filtered.append(line.rstrip("\n"))
    if not filtered:
        log("  (no extra packages to install from requirements file)")
        return

    # Write a temporary file with the filtered content
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     delete=False, encoding="utf-8") as tmp:
        tmp.write("\n".join(filtered))
        tmp_path = tmp.name
    try:
        pip_install(exe, "-r", tmp_path)
    finally:
        os.unlink(tmp_path)


# ── Step 2: install dependencies ─────────────────────────────────────────────
def pip_install(exe, *args):
    # -u + PYTHONUNBUFFERED so pip's progress streams live instead of arriving
    # in one buffered burst at the end.
    cmd = [exe, "-u", "-m", "pip", "install", *args]
    log("  $ " + " ".join(cmd))
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.run(cmd, env=env)
    if proc.returncode != 0:
        fail(f"pip command failed (exit {proc.returncode}): {' '.join(args)}")


def install_dependencies(exe):
    installed = []
    log("Upgrading pip…")
    pip_install(exe, "--upgrade", "pip")

    core_req = os.path.join(ROOT, REQ_CORE)
    if not os.path.exists(core_req):
        fail(f"{REQ_CORE} not found at repo root")
    log("Installing core dependencies (requirements.txt)…")
    pip_install(exe, "-r", core_req)
    installed.append(REQ_CORE)

    if IS_WINDOWS:
        sys_req = os.path.join(ROOT, REQ_SYSTEMAPI)
        if os.path.exists(sys_req):
            log("Installing systemapi dependencies (Windows)…")
            pip_install(exe, "-r", sys_req)
            installed.append(REQ_SYSTEMAPI)
            _pywin32_postinstall(exe)
        else:
            log(f"  {REQ_SYSTEMAPI} not found — skipping")
    else:
        log("Non-Windows OS — skipping requirements-systemapi.txt (pywin32).")

    if _ask_yes_no("Install TTS dependencies (Coqui TTS + torch, several GB)?", default=False):
        tts_req = os.path.join(ROOT, REQ_TTS)
        if os.path.exists(tts_req):
            log("Installing non-torch TTS dependencies from requirements-tts.txt…")
            _install_non_torch_requirements(exe, tts_req)
            log("Installing PyTorch for TTS (GPU detection)…")
            _install_torch_tts(exe)
            installed.append(REQ_TTS)
        else:
            log(f"  {REQ_TTS} not found — skipping")
    else:
        log("Skipping TTS dependencies.")

    return installed


def _pywin32_postinstall(exe):
    """Run pywin32's post-install (best effort)."""
    script = os.path.join(PY_DIR, "Scripts", "pywin32_postinstall.py")
    if not os.path.exists(script):
        return
    try:
        log("  running pywin32 post-install…")
        subprocess.run([exe, script, "-install"], check=False)
    except Exception as e:
        log(f"  pywin32 post-install skipped: {e}")


def install_flask_system():
    """Install Flask into the SYSTEM Python (sys.executable — the interpreter
    you launched installer.py with), NOT core/python. runner.py runs under the
    system Python and needs Flask available there."""
    log("Installing Flask into the system Python (sys.executable)…")
    cmd = [sys.executable, "-m", "pip", "install", "flask"]
    log("  $ " + " ".join(cmd))
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.run(cmd, env=env)
    if proc.returncode != 0:
        fail(f"failed to install flask into system Python (exit {proc.returncode})")


def run_llama_installer():
    """Run core/llama-installer.py (downloads llama.cpp) once everything else
    is in place. Uses the system Python and runs from inside core/ (mirrors the
    documented `cd core && python llama-installer.py`)."""
    script = os.path.join(CORE, "llama-installer.py")
    if not os.path.exists(script):
        log(f"  {script} not found — skipping llama install")
        return
    log("Running llama installer (core/llama-installer.py)…")
    proc = subprocess.run([sys.executable, script], cwd=CORE)
    if proc.returncode != 0:
        fail(f"llama-installer.py failed (exit {proc.returncode})")


# ── Step 3: seed config files ────────────────────────────────────────────────
def seed_configs(force):
    log("Seeding config files in core/ …")
    results = {}
    results["services.json"] = write_json(
        os.path.join(CORE, "services.json"), SERVICES, force)
    results["systemapi_config.json"] = write_json(
        os.path.join(CORE, "systemapi_config.json"), SYSTEMAPI_CONFIG, force)
    results["backend_servers.json"] = write_json(
        os.path.join(CORE, "backend_servers.json"), BACKEND_SERVERS, force)
    results["commands.json"] = write_json(
        os.path.join(CORE, "commands.json"), COMMANDS, force)
    return results


# ── Step 4: password -> auth.json ────────────────────────────────────────────
def setup_auth(force):
    auth_path = os.path.join(CORE, "auth.json")
    if os.path.exists(auth_path) and not force:
        log("auth.json already exists — leaving it untouched "
            "(use --force to reset the password / key).")
        return False

    log("Set the master password (used by chat-server, llm-server, systemapi).")
    while True:
        pw = getpass.getpass("  Password: ")
        if not pw:
            print("  Password cannot be empty.")
            continue
        confirm = getpass.getpass("  Confirm password: ")
        if pw != confirm:
            print("  Passwords do not match — try again.")
            continue
        break

    # OpenAI / v1 bearer key: defaults to the password (mirrors
    # core/llm-server.py _set_password_logic -> cfg["openaikey"] = new_pw).
    key = getpass.getpass(
        "  OpenAI/Bearer key [Enter to reuse the password]: ") or pw

    cfg = {
        "pass": hashlib.sha256(pw.encode("utf-8")).hexdigest(),
        "login_required": True,
        "openaikey": key,
    }
    with open(auth_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    log("  wrote auth.json (password hashed with SHA256).")
    return True


def _ask_yes_no(question, default=False):
    suffix = " [y/N]: " if not default else " [Y/n]: "
    try:
        ans = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


# ── Orchestration ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Provision core/python and seed config.")
    parser.add_argument("--force", action="store_true",
                        help="re-download Python and overwrite existing config/auth files")
    args = parser.parse_args()

    print("=" * 60)
    print("  Project environment installer")
    print("=" * 60)

    exe = acquire_python(args.force)
    installed = install_dependencies(exe)
    install_flask_system()
    configs = seed_configs(args.force)
    auth_written = setup_auth(args.force)

    # Everything downloaded/installed — now fetch llama.cpp.
    run_llama_installer()

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  Done.")
    print("=" * 60)
    print(f"Interpreter : {exe}")
    print(f"Requirements: {', '.join(installed)}")
    created = [n for n, w in configs.items() if w]
    skipped = [n for n, w in configs.items() if not w]
    if created:
        print(f"Config new  : {', '.join(created)}")
    if skipped:
        print(f"Config kept : {', '.join(skipped)}")
    print(f"auth.json   : {'written' if auth_written else 'kept existing'}")
    print("\nRun servers with the provisioned interpreter, e.g.:")
    print(f"  {exe} {os.path.join('core', 'chat-server.py')}")
    print(f"  {exe} {os.path.join('core', 'llm-server.py')}")
    if IS_WINDOWS:
        print(f"  {exe} {os.path.join('core', 'systemapi.py')}")


if __name__ == "__main__":
    main()

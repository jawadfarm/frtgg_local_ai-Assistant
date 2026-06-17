"""
installer.py
============
llama.cpp Auto-Installer — Cross-Platform
Supports: Windows, Linux, macOS

GPU Backends:
  Windows:
    NVIDIA  -> CUDA build  (auto-detects CUDA 12 or 13)
             if CUDA runtime already installed -> skip cudart zip
             if missing -> ask user: download toolkit OR include cudart zip
    AMD     -> Vulkan build (or HIP Radeon)
    Intel   -> Vulkan / SYCL build
    No GPU  -> CPU / AVX2
  Linux:
    NVIDIA  -> Vulkan build  (no prebuilt CUDA for Linux; Vulkan runs great on NVIDIA)
    AMD     -> ROCm 7.2 build
    Intel   -> SYCL FP32 build (or Vulkan fallback)
    No GPU  -> CPU / AVX2
  macOS:
    Apple Silicon (M1/M2/M3) -> Metal build  (macOS-arm64)
    Intel Mac                -> CPU build     (macOS-x64)

Run with:  python installer.py
"""

from __future__ import annotations

import os
import sys
import json
import shutil
import zipfile
import tarfile
import subprocess
import urllib.request
import urllib.error
import platform as _platform
import re
from pathlib import Path

# ═════════════════════════════════════════════════════════════════
# Platform Detection
# ═════════════════════════════════════════════════════════════════
IS_WINDOWS: bool = sys.platform == "win32"
IS_LINUX:   bool = sys.platform.startswith("linux")
IS_MACOS:   bool = sys.platform == "darwin"

ARCH: str = _platform.machine().lower().replace("amd64", "x64").replace("x86_64", "x64").replace("aarch64", "arm64")

# ═════════════════════════════════════════════════════════════════
# Config
# ═════════════════════════════════════════════════════════════════
if IS_WINDOWS:
    INSTALL_DIR = Path(r"C:\llama.cpp")
    TMP_DIR     = Path(os.environ.get("TEMP", r"C:\Temp"))
elif IS_MACOS:
    INSTALL_DIR = Path.home() / "llama.cpp"
    TMP_DIR = Path(os.environ.get("TMPDIR", "/tmp"))
else:  # Linux
    INSTALL_DIR = Path.home() / "llama.cpp"
    TMP_DIR = Path(os.environ.get("TMPDIR", "/tmp"))

GITHUB_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

# ═════════════════════════════════════════════════════════════════
# Asset naming patterns per platform
# ═════════════════════════════════════════════════════════════════
ASSET_PATTERNS = {
    # --- Windows ---
    ("win", "cpu"):           "bin-win-cpu-x64.zip",
    ("win", "vulkan"):        "bin-win-vulkan-x64.zip",
    ("win", "sycl"):          "bin-win-sycl-x64.zip",
    ("win", "hip"):           "bin-win-hip-radeon-x64.zip",
    # ("win", "cuda") handled dynamically — see below

    # --- Linux Ubuntu ---
    ("linux", "cpu"):         "bin-ubuntu-x64.tar.gz",
    ("linux", "cpu-arm64"):   "bin-ubuntu-arm64.tar.gz",
    ("linux", "cpu-s390x"):   "bin-ubuntu-s390x.tar.gz",
    ("linux", "vulkan"):      "bin-ubuntu-vulkan-x64.tar.gz",
    ("linux", "vulkan-arm64"):"bin-ubuntu-vulkan-arm64.tar.gz",
    ("linux", "rocm"):        "bin-ubuntu-rocm-7.2-x64.tar.gz",
    ("linux", "sycl-fp32"):   "bin-ubuntu-sycl-fp32-x64.tar.gz",
    ("linux", "sycl-fp16"):   "bin-ubuntu-sycl-fp16-x64.tar.gz",
    ("linux", "openvino"):    "bin-ubuntu-openvino",

    # --- macOS ---
    ("macos", "arm64"):       "bin-macos-arm64.tar.gz",
    ("macos", "x64"):         "bin-macos-x64.tar.gz",
}

# CUDA for Windows — version injected at runtime
cuda_suffix_template = "bin-win-cuda-{ver}-x64.zip"
cudart_suffix_template = "cudart-llama-bin-win-cuda-{ver}-x64.zip"
CUDA_DEFAULT_VER = "12"
CUDA_DOWNLOAD_URL = "https://developer.nvidia.com/cuda-downloads"

# ═════════════════════════════════════════════════════════════════
# ANSI Colors
# ═════════════════════════════════════════════════════════════════
if IS_WINDOWS:
    os.system("")

R   = "\033[91m"
G   = "\033[92m"
Y   = "\033[93m"
B   = "\033[94m"
C   = "\033[96m"
W   = "\033[97m"
DIM = "\033[2m"
X   = "\033[0m"
BOLD= "\033[1m"

def hdr(text):
    w = 58
    print(f"\n{B}{'═'*w}{X}")
    print(f"{B}  {BOLD}{text}{X}")
    print(f"{B}{'═'*w}{X}")

def ok(text):   print(f"  {G}✔{X}  {text}")
def info(text): print(f"  {C}→{X}  {text}")
def warn(text): print(f"  {Y}⚠{X}  {text}")
def err(text):  print(f"  {R}✘{X}  {text}")
def blank():    print()

def step(n, total, text):
    print(f"\n{DIM}[{n}/{total}]{X} {BOLD}{text}{X}")


# ═════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════
def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _run(cmd: list[str], timeout: int = 8) -> str:
    """Run a command, return stdout+stderr or empty string on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _which(tool: str) -> bool:
    """Check if a CLI tool is available on PATH."""
    if IS_WINDOWS:
        return shutil.which(tool + ".exe") is not None
    return shutil.which(tool) is not None


# ═════════════════════════════════════════════════════════════════
# GPU DETECTION  (Platform-specific)
# ═════════════════════════════════════════════════════════════════
def detect_cuda_major_version() -> str | None:
    """Try to detect installed CUDA major version (e.g. '12' or '13')."""
    # 1. nvcc --version
    out = _run(["nvcc", "--version"], timeout=5)
    m = re.search(r"release\s+(\d+)\.\d+", out)
    if m:
        return m.group(1)

    # 2. nvidia-smi
    out = _run(["nvidia-smi"], timeout=6)
    m = re.search(r"CUDA Version:\s*(\d+)\.\d+", out)
    if m:
        return m.group(1)

    # 3. Scan for cudart64_*.dll (Windows) or libcudart.so.* (Linux)
    if IS_WINDOWS:
        sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        for folder in [sys32] + [Path(p) for p in os.environ.get("PATH", "").split(";")]:
            try:
                for dll in Path(folder).glob("cudart64_*.dll"):
                    m = re.search(r"cudart64_(\d+)", dll.name)
                    if m:
                        major = m.group(1)
                        return major[:2] if len(major) >= 2 else major
            except (OSError, ValueError):
                pass
    else:
        # Linux / macOS — look for libcudart.so
        for folder in [Path("/usr/local/cuda/lib64"), Path("/usr/lib/x86_64-linux-gnu")]:
            try:
                for so in folder.glob("libcudart.so.*"):
                    m = re.search(r"libcudart\.so\.(\d+)\.\d+", so.name)
                    if m:
                        return m.group(1)
            except (OSError, ValueError):
                pass
    return None


def _cuda_runtime_present() -> bool:
    """True if any CUDA runtime is detected."""
    if detect_cuda_major_version() is not None:
        return True
    if IS_WINDOWS:
        sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        if list(sys32.glob("cudart64_*.dll")):
            return True
    else:
        for p in [Path("/usr/local/cuda/lib64/libcudart.so"),
                  Path("/usr/lib/x86_64-linux-gnu/libcudart.so")]:
            if p.exists() or p.is_symlink():
                return True
    return False


def ask_cuda_missing() -> bool:
    """CUDA runtime not found on Windows. Ask user what to do."""
    blank()
    print(f"  {Y}{'─'*54}{X}")
    warn(f"CUDA runtime {W}(cudart64_12.dll / cudart64_13.dll){Y} not found.")
    blank()
    print(f"  {BOLD}Options:{X}")
    print(f"  {C}[1]{X}  Download CUDA Toolkit from NVIDIA  {DIM}(~3 GB, re-run after){X}")
    print(f"  {C}[2]{X}  Include CUDA runtime DLLs in this install  {DIM}(portable, ~370 MB extra){X}")
    print(f"  {Y}{'─'*54}{X}")
    blank()
    while True:
        choice = input(f"  {BOLD}Your choice (1/2): {X}").strip()
        if choice == "1":
            info(f"Opening: {CUDA_DOWNLOAD_URL}")
            try:
                import webbrowser
                webbrowser.open(CUDA_DOWNLOAD_URL)
                ok("Browser opened.")
            except Exception:
                warn("Could not open browser. Visit manually:")
                print(f"  {C}{CUDA_DOWNLOAD_URL}{X}")
            blank()
            warn("Install CUDA Toolkit, then re-run installer.py")
            blank()
            input(f"  {DIM}Press Enter to exit…{X}  ")
            sys.exit(0)
        elif choice == "2":
            ok("Will include CUDA runtime DLLs zip in installation.")
            return True
        else:
            print(f"  {R}Enter 1 or 2.{X}")


def pick_cuda_version(available_cuda_assets: list[str]) -> str:
    """Pick the CUDA build version matching the user's system."""
    major = detect_cuda_major_version()
    if major:
        for asset_ver in available_cuda_assets:
            if re.match(rf"^{re.escape(major)}[\.\-]?", asset_ver):
                info(f"Detected CUDA {major}.x — selecting build: cuda-{asset_ver}")
                return asset_ver
        warn(f"Detected CUDA {major}.x but no exact match in release assets: {available_cuda_assets}")
        warn(f"Falling back to: cuda-{available_cuda_assets[0]}")
        return available_cuda_assets[0]
    return available_cuda_assets[0] if available_cuda_assets else CUDA_DEFAULT_VER


# ── Windows GPU Detection ──────────────────────────────────────
def _detect_gpu_windows():
    """Returns (backend, gpu_name, include_cudart, cuda_ver)"""
    include_cudart = False
    cuda_ver       = CUDA_DEFAULT_VER

    # NVIDIA
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], timeout=6)
    if out.strip():
        name = out.strip().splitlines()[0].split(",")[0].strip()
        ok(f"NVIDIA GPU: {W}{name}{X}")
        if _cuda_runtime_present():
            major = detect_cuda_major_version()
            cuda_ver = major if major else CUDA_DEFAULT_VER
            ok(f"CUDA runtime found  {DIM}(major version: {cuda_ver}){X}")
        else:
            include_cudart = ask_cuda_missing()
        info("Backend: CUDA")
        return "cuda", name, include_cudart, cuda_ver

    # AMD / Intel via WMI
    gpu_name = _wmi_gpu()
    if gpu_name and "microsoft basic" not in gpu_name.lower():
        ok(f"GPU: {W}{gpu_name}{X}")
        n = gpu_name.lower()
        if "amd" in n or "radeon" in n:
            info("AMD GPU → Backend: Vulkan")
            return "vulkan", gpu_name, False, cuda_ver
        elif "intel" in n:
            info("Intel GPU → Backend: Vulkan")
            return "vulkan", gpu_name, False, cuda_ver
        else:
            info("Unknown vendor → Backend: Vulkan")
            return "vulkan", gpu_name, False, cuda_ver

    warn("No discrete GPU detected")
    info("Backend: CPU / AVX2")
    return "cpu", "CPU only", False, cuda_ver


def _wmi_gpu() -> str:
    out = _run(
        ["powershell", "-NoProfile", "-Command",
         "Get-WmiObject Win32_VideoController | Select-Object -ExpandProperty Name"],
        timeout=8
    )
    if out:
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        for line in lines:
            if "microsoft basic" not in line.lower():
                return line
        return lines[0] if lines else ""
    return ""


# ── Linux GPU Detection ────────────────────────────────────────
def _detect_gpu_linux():
    """Returns (backend, gpu_name, include_cudart, cuda_ver)"""
    include_cudart = False
    cuda_ver       = CUDA_DEFAULT_VER

    # 1. NVIDIA — check nvidia-smi
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], timeout=6)
    if out.strip():
        name = out.strip().splitlines()[0].split(",")[0].strip()
        ok(f"NVIDIA GPU: {W}{name}{X}")
        info("Backend: Vulkan  {DIM}(no prebuilt CUDA for Linux; Vulkan runs great on NVIDIA){X}")
        return "vulkan", name, False, cuda_ver

    # 2. AMD — check rocminfo first, then lspci
    out = _run(["rocminfo"], timeout=6)
    if "AMD" in out or "gfx" in out.lower():
        # Extract GPU name from rocminfo
        m = re.search(r"Name:\s+(gfx\w+)", out)
        name = m.group(1) if m else "AMD GPU"
        ok(f"AMD GPU: {W}{name}{X}")
        info("Backend: ROCm 7.2")
        return "rocm", name, False, cuda_ver

    out = _run(["lspci"], timeout=6)
    if out:
        for line in out.splitlines():
            l = line.lower()
            if ("amd" in l or "ati" in l or "radeon" in l) and "vga" in l or "3d" in l:
                name = line.split(":", 2)[-1].strip() if ":" in line else line
                ok(f"AMD GPU: {W}{name}{X}")
                info("Backend: ROCm 7.2")
                return "rocm", name, False, cuda_ver

    # 3. Intel — check sycl-ls, then lspci, then vulkaninfo
    out = _run(["sycl-ls"], timeout=6)
    if out.strip() and "intel" in out.lower():
        ok(f"Intel GPU detected via SYCL{X}")
        info("Backend: SYCL FP32")
        return "sycl-fp32", "Intel GPU", False, cuda_ver

    if out:
        for line in out.splitlines():
            if "intel" in line.lower():
                ok(f"Intel GPU: {W}{line.strip()}{X}")
                info("Backend: SYCL FP32")
                return "sycl-fp32", "Intel GPU", False, cuda_ver

    # Check lspci for Intel GPU
    if out:
        for line in out.splitlines():
            l = line.lower()
            if "intel" in l and ("vga" in l or "3d" in l or "graphics" in l):
                name = line.split(":", 2)[-1].strip() if ":" in line else line
                ok(f"Intel GPU: {W}{name}{X}")
                info("Backend: SYCL FP32")
                return "sycl-fp32", name, False, cuda_ver

    # 4. No discrete GPU found
    warn("No discrete GPU detected")
    info("Backend: CPU / AVX2")
    return "cpu", "CPU only", False, cuda_ver


# ── macOS GPU Detection ────────────────────────────────────────
def _detect_gpu_macos():
    """Returns (backend, gpu_name, include_cudart, cuda_ver)"""
    include_cudart = False
    cuda_ver       = CUDA_DEFAULT_VER

    # Detect architecture
    arch = _platform.machine().lower()
    if arch in ("arm64", "aarch64"):
        ok(f"Apple Silicon detected  {DIM}(architecture: {arch}){X}")
        info("Backend: Metal (GPU-accelerated)")
        return "arm64", "Apple Silicon", False, cuda_ver
    else:
        ok(f"Intel Mac detected  {DIM}(architecture: {arch}){X}")
        info("Backend: CPU")
        return "x64", "Intel Mac", False, cuda_ver


def detect_gpu():
    """Dispatch to platform-specific GPU detection."""
    blank()
    if IS_WINDOWS:
        return _detect_gpu_windows()
    elif IS_LINUX:
        return _detect_gpu_linux()
    elif IS_MACOS:
        return _detect_gpu_macos()
    else:
        err(f"Unsupported platform: {sys.platform}")
        sys.exit(1)


# ═════════════════════════════════════════════════════════════════
# Runtime Checks
# ═════════════════════════════════════════════════════════════════
def check_vulkan_runtime():
    """Warn if Vulkan runtime is missing."""
    if IS_WINDOWS:
        dll = Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "vulkan-1.dll")
        if dll.exists():
            ok("Vulkan runtime found (vulkan-1.dll)")
        else:
            warn("vulkan-1.dll not found — if llama-server fails, install Vulkan SDK:")
            print(f"  {C}https://vulkan.lunarg.com/sdk/home#windows{X}")
    else:
        # Linux / macOS
        if _which("vulkaninfo"):
            ok("Vulkan runtime found (vulkaninfo)")
        else:
            warn("vulkaninfo not found — if llama-server fails, install Vulkan:")
            if IS_LINUX:
                print(f"  {C}sudo apt install vulkan-tools libvulkan1{X}  {DIM}(Debian/Ubuntu){X}")
                print(f"  {C}sudo pacman -S vulkan-tools vulkan-icd-loader{X}  {DIM}(Arch){X}")
            elif IS_MACOS:
                print(f"  {C}brew install vulkan-tools{X}  {DIM}(macOS with Homebrew){X}")


def check_rocm_runtime():
    """Warn if ROCm runtime is missing on Linux."""
    if not IS_LINUX:
        return
    if _which("rocminfo") or _which("rocm-smi"):
        ok("ROCm runtime found")
    else:
        warn("ROCm runtime not found — if llama-server fails, install ROCm:")
        print(f"  {C}https://rocm.docs.amd.com/projects/install-on-linux/en/latest/{X}")


def check_sycl_runtime():
    """Warn if Intel SYCL runtime is missing on Linux."""
    if not IS_LINUX:
        return
    if _which("sycl-ls"):
        ok("Intel SYCL runtime found")
    else:
        warn("Intel SYCL runtime not found — if llama-server fails, install oneAPI:")
        print(f"  {C}https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit.html{X}")


# ═════════════════════════════════════════════════════════════════
# GITHUB RELEASE
# ═════════════════════════════════════════════════════════════════
def get_latest_release(backend: str, include_cudart: bool, cuda_major: str):
    """
    Fetch latest release from GitHub API and pick the right asset.
    Returns (tag, main_url, main_name, cudart_url, cudart_name, resolved_cuda_ver)
    """
    info("Fetching latest release info from GitHub…")
    req = urllib.request.Request(
        GITHUB_API,
        headers={"User-Agent": "llama-cpp-installer/2.0",
                 "Accept":     "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach GitHub API: {e}")

    tag    = data["tag_name"]
    assets = data.get("assets", [])
    resolved_cuda_ver = cuda_major

    if IS_WINDOWS and backend == "cuda":
        # Discover all CUDA build variants
        cuda_ver_pattern = re.compile(r"^llama-.*bin-win-cuda-([\d.]+)-x64\.zip$")
        available_cuda_vers = []
        for a in assets:
            m = cuda_ver_pattern.match(a["name"])
            if m:
                available_cuda_vers.append(m.group(1))

        if not available_cuda_vers:
            names = [a["name"] for a in assets]
            raise RuntimeError(
                f"No CUDA build assets found in release {tag}.\n"
                "Available:\n" + "\n".join(f"  {n}" for n in names)
            )

        info(f"Available CUDA builds in {tag}: {', '.join(available_cuda_vers)}")
        resolved_cuda_ver = pick_cuda_version(available_cuda_vers)
        suffix = cuda_suffix_template.format(ver=resolved_cuda_ver)
        matches = [a for a in assets if a["name"].startswith("llama-") and suffix in a["name"]]
        if not matches:
            raise RuntimeError(f"Could not find CUDA asset matching '{suffix}' in release {tag}.")

    elif IS_WINDOWS:
        pattern = ASSET_PATTERNS.get(("win", backend))
        if not pattern:
            raise RuntimeError(f"Unknown Windows backend: {backend}")
        matches = [a for a in assets if pattern in a["name"]]
        if not matches:
            names = [a["name"] for a in assets]
            raise RuntimeError(
                f"No matching asset ('{pattern}') found in release {tag}.\n"
                "Available:\n" + "\n".join(f"  {n}" for n in names)
            )

    elif IS_LINUX:
        # Determine asset key
        if backend == "cpu":
            if ARCH == "s390x":
                asset_key = ("linux", "cpu-s390x")
            elif ARCH == "arm64":
                asset_key = ("linux", "cpu-arm64")
            else:
                asset_key = ("linux", "cpu")
        elif backend == "vulkan":
            if ARCH == "arm64":
                asset_key = ("linux", "vulkan-arm64")
            else:
                asset_key = ("linux", "vulkan")
        elif backend in ("rocm", "sycl-fp32", "sycl-fp16", "openvino"):
            asset_key = ("linux", backend)
        else:
            # Fallback to CPU
            asset_key = ("linux", "cpu")

        pattern = ASSET_PATTERNS.get(asset_key)
        if not pattern:
            raise RuntimeError(f"Unknown Linux backend/arch combo: {backend}/{ARCH}")

        matches = [a for a in assets if pattern in a["name"]]
        if not matches:
            names = [a["name"] for a in assets]
            raise RuntimeError(
                f"No matching asset ('{pattern}') found in release {tag}.\n"
                "Available:\n" + "\n".join(f"  {n}" for n in names)
            )

    elif IS_MACOS:
        if backend == "arm64":
            asset_key = ("macos", "arm64")
        elif backend == "x64":
            asset_key = ("macos", "x64")
        else:
            asset_key = ("macos", "x64")  # Fallback

        pattern = ASSET_PATTERNS.get(asset_key)
        if not pattern:
            raise RuntimeError(f"Unknown macOS backend: {backend}")

        matches = [a for a in assets if pattern in a["name"]]
        if not matches:
            names = [a["name"] for a in assets]
            raise RuntimeError(
                f"No matching asset ('{pattern}') found in release {tag}.\n"
                "Available:\n" + "\n".join(f"  {n}" for n in names)
            )

    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")

    asset = matches[0]
    ok(f"Release : {W}{tag}{X}")
    ok(f"Main    : {W}{asset['name']}{X}  ({_human_size(asset['size'])})")

    cudart_url, cudart_name = "", ""
    if include_cudart and IS_WINDOWS and backend == "cuda":
        cudart_suffix = cudart_suffix_template.format(ver=resolved_cuda_ver)
        cm = [a for a in assets if cudart_suffix in a["name"]]
        if cm:
            ok(f"DLLs    : {W}{cm[0]['name']}{X}  ({_human_size(cm[0]['size'])})")
            cudart_url  = cm[0]["browser_download_url"]
            cudart_name = cm[0]["name"]
        else:
            warn(f"cudart zip ('{cudart_suffix}') not found in release — skipping")

    return tag, asset["browser_download_url"], asset["name"], cudart_url, cudart_name, resolved_cuda_ver


# ═════════════════════════════════════════════════════════════════
# DOWNLOAD
# ═════════════════════════════════════════════════════════════════
def download_file(url: str, dest: Path, label: str = ""):
    if label:
        info(label)
    req = urllib.request.Request(url, headers={"User-Agent": "llama-cpp-installer/2.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        total      = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        bar_w      = 40
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct  = downloaded / total
                    done = int(bar_w * pct)
                    bar  = "█" * done + "░" * (bar_w - done)
                    print(f"\r  {C}[{bar}]{X} {_human_size(downloaded)} / {_human_size(total)}  ",
                          end="", flush=True)
    print()
    ok(f"Done: {_human_size(dest.stat().st_size)}")


# ═════════════════════════════════════════════════════════════════
# EXTRACT
# ═════════════════════════════════════════════════════════════════
def extract_archive(archive_path: Path, dest_dir: Path, label: str = ""):
    """
    Extract archive directly into dest_dir, stripping any top-level subfolder.
    Supports both .zip and .tar.gz
    """
    if label:
        info(label)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if archive_path.suffix == ".zip" or archive_path.name.endswith(".zip"):
        _extract_zip(archive_path, dest_dir)
    elif archive_path.suffix == ".gz" or ".tar.gz" in archive_path.name:
        _extract_tar(archive_path, dest_dir)
    else:
        raise RuntimeError(f"Unknown archive format: {archive_path}")


def _extract_zip(zip_path: Path, dest_dir: Path):
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.namelist()
        total   = len(members)

        # Detect common top-level prefix
        prefix = ""
        dirs = [m for m in members if m.endswith("/")]
        if dirs:
            first = dirs[0]
            if all(m.startswith(first) or m == first for m in members):
                prefix = first

        for i, member in enumerate(members, 1):
            rel = member[len(prefix):] if prefix and member.startswith(prefix) else member
            if not rel or rel.endswith("/"):
                continue
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            print(f"\r  Extracting {i}/{total}…", end="", flush=True)
    print()
    ok(f"Extracted to {dest_dir}{os.sep}")


def _extract_tar(tar_path: Path, dest_dir: Path):
    with tarfile.open(tar_path, "r:gz") as tf:
        members = tf.getmembers()
        total   = len(members)

        # Detect common top-level prefix
        prefix = ""
        dirs = [m for m in members if m.isdir()]
        if dirs:
            first = dirs[0].name + "/"
            if all((m.name + "/").startswith(first) or (m.name + "/") == first for m in members):
                prefix = first

        for i, member in enumerate(members, 1):
            if not member.isfile():
                continue
            rel = member.name[len(prefix):] if prefix and member.name.startswith(prefix) else member.name
            if not rel or rel.endswith("/"):
                continue
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with tf.extractfile(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            print(f"\r  Extracting {i}/{total}…", end="", flush=True)
    print()
    ok(f"Extracted to {dest_dir}{os.sep}")


# ═════════════════════════════════════════════════════════════════
# PATH Management  (Platform-specific)
# ═════════════════════════════════════════════════════════════════
def add_to_path(directory: Path):
    """Add directory to PATH — platform specific."""
    if IS_WINDOWS:
        _add_to_path_windows(directory)
    else:
        _add_to_path_unix(directory)


def _add_to_path_windows(directory: Path):
    import ctypes
    import winreg
    dir_str = str(directory)
    if dir_str.lower() in os.environ.get("PATH", "").lower():
        ok("Already in PATH")
        return
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Environment",
            0, winreg.KEY_READ | winreg.KEY_WRITE
        )
        try:
            existing, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            existing = ""
        if dir_str.lower() not in existing.lower():
            new_path = f"{existing};{dir_str}" if existing else dir_str
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
            ok(f"Added to user PATH: {dir_str}")
            info("Restart terminal to apply PATH change")
        else:
            ok("Already in user PATH")
        winreg.CloseKey(key)
        try:
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 2, 5000, None)
        except Exception:
            pass
    except Exception as e:
        warn(f"Could not update PATH: {e}")
        warn(f"Add manually: {dir_str}")


def _add_to_path_unix(directory: Path):
    """Add to shell profile files on Linux/macOS."""
    dir_str = str(directory)

    # Check if already in current PATH
    if dir_str in os.environ.get("PATH", "").split(":"):
        ok("Already in PATH")
        return

    # Detect which shell profiles to update
    profiles = []
    home = Path.home()

    if IS_MACOS:
        # macOS default is zsh since Catalina
        profiles = [home / ".zshrc", home / ".bash_profile"]
    else:
        # Linux
        shell = os.environ.get("SHELL", "").lower()
        if "zsh" in shell:
            profiles = [home / ".zshrc", home / ".bashrc"]
        elif "bash" in shell:
            profiles = [home / ".bashrc", home / ".bash_profile"]
        else:
            profiles = [home / ".profile"]

    added = False
    for profile in profiles:
        if not profile.exists():
            continue
        content = profile.read_text(encoding="utf-8", errors="ignore")
        if dir_str in content:
            continue
        with open(profile, "a", encoding="utf-8") as f:
            f.write(f"\n# llama.cpp PATH\nexport PATH=\"{dir_str}:$PATH\"\n")
        ok(f"Added to {profile.name}")
        added = True

    if not added:
        # Create the most appropriate profile
        if IS_MACOS:
            profile = home / ".zshrc"
        else:
            profile = home / ".bashrc"
        with open(profile, "a", encoding="utf-8") as f:
            f.write(f"\n# llama.cpp PATH\nexport PATH=\"{dir_str}:$PATH\"\n")
        ok(f"Added to {profile.name}")

    info("Reload your shell or run:  source ~/.bashrc  (or ~/.zshrc)")


# ═════════════════════════════════════════════════════════════════
# VERIFY
# ═════════════════════════════════════════════════════════════════
def verify_binary(install_dir: Path) -> bool:
    exe_name = "llama-server.exe" if IS_WINDOWS else "llama-server"
    exe = install_dir / exe_name
    if not exe.exists():
        found = list(install_dir.rglob(exe_name))
        if found:
            exe = found[0]
        else:
            err(f"{exe_name} not found!")
            info("Executables in install dir:")
            pattern = "*.exe" if IS_WINDOWS else "*"
            for p in sorted(install_dir.rglob(pattern))[:20]:
                print(f"    {p.relative_to(install_dir)}")
            return False
    try:
        r     = subprocess.run([str(exe), "--version"],
                               capture_output=True, text=True, timeout=10)
        lines = (r.stdout + r.stderr).strip().splitlines()
        vstr  = lines[0] if lines else "(unknown)"
        ok(f"llama-server works!  {DIM}{vstr}{X}")
        return True
    except Exception as e:
        warn(f"Could not run {exe_name}: {e}")
        if IS_WINDOWS:
            warn("May need Visual C++ Redistributable:")
            print(f"  {C}https://aka.ms/vs/17/release/vc_redist.x64.exe{X}")
        elif IS_LINUX:
            warn("May need to install runtime libraries:")
            print(f"  {C}sudo apt install libgomp1 libvulkan1{X}  {DIM}(Debian/Ubuntu){X}")
        elif IS_MACOS:
            warn("May need Xcode Command Line Tools:")
            print(f"  {C}xcode-select --install{X}")
        return False


# ═════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════
def main():
    # ── Banner ──────────────────────────────────────────────────
    plat_label = "Windows" if IS_WINDOWS else ("macOS" if IS_MACOS else "Linux")
    print(f"""
{B}╔══════════════════════════════════════════════════════╗
║        llama.cpp  Auto-Installer  ({plat_label:^8})        ║
║        github.com/ggml-org/llama.cpp                 ║
╚══════════════════════════════════════════════════════╝{X}""")

    info(f"Platform : {plat_label}")
    info(f"Arch     : {ARCH}")
    blank()

    STEPS = 5

    # ── Step 1: GPU Detection ──────────────────────────────────
    step(1, STEPS, "Detecting GPU…")
    backend, gpu_name, include_cudart, cuda_major = detect_gpu()

    if backend in ("vulkan", "rocm", "sycl-fp32", "sycl-fp16"):
        if backend == "vulkan":
            check_vulkan_runtime()
        elif backend == "rocm":
            check_rocm_runtime()
        elif backend in ("sycl-fp32", "sycl-fp16"):
            check_sycl_runtime()

    # ── Step 2: Fetch release info ─────────────────────────────
    step(2, STEPS, "Fetching latest llama.cpp release…")
    try:
        tag, main_url, main_name, cudart_url, cudart_name, resolved_cuda_ver = \
            get_latest_release(backend, include_cudart, cuda_major)
    except RuntimeError as e:
        err(str(e))
        sys.exit(1)

    # ── Step 3: Download ───────────────────────────────────────
    step(3, STEPS, "Downloading…")

    tmp_main = TMP_DIR / main_name
    try:
        download_file(main_url, tmp_main, label=f"Main package: {main_name}")
    except Exception as e:
        err(f"Download failed: {e}")
        sys.exit(1)

    tmp_cudart = None
    if cudart_url:
        tmp_cudart = TMP_DIR / cudart_name
        try:
            download_file(cudart_url, tmp_cudart, label=f"CUDA DLLs: {cudart_name}")
        except Exception as e:
            warn(f"cudart download failed: {e} — continuing without it")
            tmp_cudart = None

    # ── Step 4: Extract ────────────────────────────────────────
    step(4, STEPS, f"Installing to {INSTALL_DIR}…")

    if INSTALL_DIR.exists():
        warn(f"Removing existing: {INSTALL_DIR}")
        shutil.rmtree(INSTALL_DIR)
    INSTALL_DIR.mkdir(parents=True)

    try:
        extract_archive(tmp_main, INSTALL_DIR, label="Extracting main package…")
    except Exception as e:
        err(f"Extraction failed: {e}")
        sys.exit(1)
    finally:
        try:
            tmp_main.unlink()
        except Exception:
            pass

    if tmp_cudart and tmp_cudart.exists():
        try:
            extract_archive(tmp_cudart, INSTALL_DIR, label="Extracting CUDA DLLs…")
        except Exception as e:
            warn(f"cudart extraction error: {e}")
        finally:
            try:
                tmp_cudart.unlink()
                info("Temp cudart archive cleaned up")
            except Exception:
                pass

    # ── Step 5: PATH + verify ──────────────────────────────────
    step(5, STEPS, "Finalizing…")
    add_to_path(INSTALL_DIR)
    binary_ok = verify_binary(INSTALL_DIR)

    # ── Summary ────────────────────────────────────────────────
    hdr("Installation Complete")
    print(f"  {G}{'Platform':<18}{X} {plat_label}")
    print(f"  {G}{'GPU':<18}{X} {gpu_name}")
    print(f"  {G}{'Backend':<18}{X} {backend.upper()}")
    if IS_WINDOWS and backend == "cuda":
        print(f"  {G}{'CUDA version':<18}{X} {resolved_cuda_ver}")
    print(f"  {G}{'CUDA DLLs':<18}{X} {'included' if include_cudart else 'system / not needed'}")
    print(f"  {G}{'Release':<18}{X} {tag}")
    print(f"  {G}{'Install path':<18}{X} {INSTALL_DIR}")
    print(f"  {G}{'Binary':<18}{X} {'✔ working' if binary_ok else '⚠ needs check'}")
    blank()

    if binary_ok:
        print(f"  {BOLD}llama-server is ready!{X}")
        if IS_WINDOWS:
            print(f"""
  {DIM}Quick start:{X}
  {C}llama-server.exe -m C:\\models\\yourmodel.gguf --host 0.0.0.0 --port 8080{X}
""")
        else:
            print(f"""
  {DIM}Quick start:{X}
  {C}llama-server -m ~/models/yourmodel.gguf --host 0.0.0.0 --port 8080{X}
""")
    else:
        print(f"""
  {Y}Installed but could not verify llama-server.{X}
  Check the runtime dependencies listed above.
""")

    if not IS_WINDOWS:
        blank()
        info("To use right now, run:")
        print(f"  {C}export PATH=\"{INSTALL_DIR}:$PATH\"{X}")
        print(f"  {C}llama-server --version{X}")

    try:
        input(f"\n  {DIM}Press Enter to exit…{X}  ")
    except EOFError:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {Y}Cancelled.{X}\n")
        sys.exit(0)
    except Exception as e:
        print(f"\n  {R}Unexpected error: {e}{X}\n")
        import traceback
        traceback.print_exc()
        try:
            input("  Press Enter to exit…  ")
        except EOFError:
            pass
        sys.exit(1)

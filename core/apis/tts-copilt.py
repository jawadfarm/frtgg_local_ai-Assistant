
import os


import socket
import threading
import queue
import warnings
import contextlib
import ipaddress
import json
import torch
import numpy as np
import sounddevice as sd
from TTS.api import TTS
import sys
import soundfile as sf
import time
import re
import logging
import logging.handlers
import os

warnings.filterwarnings("ignore")

# ===============================
# allow XTTS classes for torch.load (PyTorch >= 2.6 defaults weights_only=True)
# safe because the XTTS v2 checkpoint comes from the official Coqui source
# ===============================
from TTS.tts.configs.xtts_config import XttsConfig
from TTS.tts.models.xtts import XttsAudioConfig, XttsArgs
from TTS.config.shared_configs import BaseDatasetConfig

torch.serialization.add_safe_globals([XttsConfig, XttsAudioConfig, XttsArgs, BaseDatasetConfig])

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ===============================
# logging setup
# ===============================
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "tts-server.log")

_fmt = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_fmt)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_fmt)

log = logging.getLogger("tts")
log.setLevel(logging.DEBUG)
log.addHandler(_file_handler)
log.addHandler(_console_handler)

log.info("=" * 60)
log.info("TTS Server starting up")
log.info(f"Log file: {LOG_FILE}")

# ===============================
# settings (tts-settings.json)
# port + which networks are allowed to connect
# ===============================
SETTINGS_FILE = os.path.join(BASE_DIR, "tts-settings.json")

DEFAULT_SETTINGS = {
    "port": 8113,
    "Allowed_local-network": True,
    "Allowed_public-network": False,
}


def _get_setting(data: dict, key: str, default):
    for variant in (key, key.replace("-", "_"), key.replace("_", "-")):
        if variant in data:
            return data[variant]
    return default


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=4)
        log.info(f"Created default settings file: {SETTINGS_FILE}")
        return dict(DEFAULT_SETTINGS)

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Could not read settings ({e}); falling back to defaults")
        return dict(DEFAULT_SETTINGS)

    return {
        "port": int(_get_setting(data, "port", DEFAULT_SETTINGS["port"])),
        "Allowed_local-network": bool(
            _get_setting(data, "Allowed_local-network", DEFAULT_SETTINGS["Allowed_local-network"])
        ),
        "Allowed_public-network": bool(
            _get_setting(data, "Allowed_public-network", DEFAULT_SETTINGS["Allowed_public-network"])
        ),
    }


SETTINGS = load_settings()
PORT = SETTINGS["port"]
ALLOW_LOCAL = SETTINGS["Allowed_local-network"]
ALLOW_PUBLIC = SETTINGS["Allowed_public-network"]
log.info(f"Settings | port={PORT} local={ALLOW_LOCAL} public={ALLOW_PUBLIC}")

# ===============================
# device detection (multi-vendor)
# ===============================
def detect_device():
    if torch.cuda.is_available():
        if getattr(torch.version, "hip", None):
            return "cuda", "cuda", "AMD ROCm"
        return "cuda", "cuda", "NVIDIA CUDA"

    if hasattr(torch, "xpu") and torch.xpu.is_available():
        return "xpu", "xpu", "Intel XPU"

    try:
        import torch_directml
        if torch_directml.is_available():
            return torch_directml.device(), "dml", "DirectML (AMD/Intel)"
    except Exception:
        pass

    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps", "mps", "Apple MPS"

    return "cpu", "cpu", "CPU"


# ===============================
# speaker
# ===============================
SPEAKER_FILE = os.path.join(BASE_DIR, "voice.wav")

# ===============================
# model state
# ===============================
MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
MIN_FREE_VRAM_GB = 3.0

tts = None
gpt_cond_latent = None
speaker_embedding = None
DEVICE = "cpu"
DEVICE_TYPE = "cpu"
VENDOR = "CPU"
current_gpu = None
_autocast = contextlib.nullcontext()
model_lock = threading.Lock()


def make_autocast(device_type: str):
    if device_type == "cuda":
        return torch.amp.autocast("cuda")
    if device_type == "xpu":
        return torch.amp.autocast("xpu")
    return contextlib.nullcontext()


def apply_cuda_opts():
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.backends.cudnn.benchmark = True


def list_gpus() -> list[dict]:
    gpus = []
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            try:
                free, total = torch.cuda.mem_get_info(i)
            except Exception:
                free = total = 0
            gpus.append({
                "index": i,
                "name": torch.cuda.get_device_properties(i).name,
                "vram_total_gb": round(total / 1024 ** 3, 2),
                "vram_free_gb": round(free / 1024 ** 3, 2),
            })
    return gpus


def default_gpu_index():
    gpus = list_gpus()
    if not gpus:
        return None
    return max(gpus, key=lambda g: g["vram_free_gb"])["index"]


def _extract_latents_and_warmup():
    global gpt_cond_latent, speaker_embedding

    log.info("Extracting speaker latents...")
    gpt_cond_latent, speaker_embedding = tts.synthesizer.tts_model.get_conditioning_latents(
        audio_path=[SPEAKER_FILE]
    )
    if gpt_cond_latent.dim() == 2:
        gpt_cond_latent = gpt_cond_latent.unsqueeze(0)
    if speaker_embedding.dim() == 1:
        speaker_embedding = speaker_embedding.unsqueeze(0)
    log.info(f"gpt_cond_latent shape: {gpt_cond_latent.shape}")
    log.info(f"speaker_embedding shape: {speaker_embedding.shape}")

    log.info("Warming up (3 passes)...")
    for i in range(3):
        with torch.inference_mode(), _autocast:
            _ = tts.synthesizer.tts_model.inference(
                text="hello world" if i < 2 else "warming up the gpu for realtime synthesis",
                language="en",
                gpt_cond_latent=gpt_cond_latent,
                speaker_embedding=speaker_embedding,
                temperature=0.3,
                top_p=0.7,
                top_k=20,
            )
    log.info("Warmup done — model ready")


def load_model(gpu_index) -> str:
    global tts, DEVICE, DEVICE_TYPE, VENDOR, current_gpu, _autocast

    with model_lock, inference_lock:
        gpus = list_gpus()

        if gpus:
            if gpu_index is None:
                gpu_index = default_gpu_index()

            valid = {g["index"] for g in gpus}
            if gpu_index not in valid:
                return f"error: invalid gpu index {gpu_index} (have {sorted(valid)})"

            free_gb = next(g["vram_free_gb"] for g in gpus if g["index"] == gpu_index)
            if free_gb < MIN_FREE_VRAM_GB:
                log.warning(f"GPU {gpu_index}: only {free_gb:.1f}GB free, need >= {MIN_FREE_VRAM_GB:.0f}GB")
                return f"no vram (free={free_gb:.1f}GB, need>={MIN_FREE_VRAM_GB:.0f}GB)"

            device = f"cuda:{gpu_index}"
            device_type = "cuda"
            vendor = "AMD ROCm" if getattr(torch.version, "hip", None) else "NVIDIA CUDA"
        else:
            device, device_type, vendor = detect_device()
            gpu_index = None

        if tts is not None:
            del tts
            tts = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        log.info(f"Loading model on {device} ({vendor})")

        if device_type == "cuda" and gpu_index is not None:
            torch.cuda.set_device(gpu_index)
            apply_cuda_opts()

        DEVICE, DEVICE_TYPE, VENDOR, current_gpu = device, device_type, vendor, gpu_index
        _autocast = make_autocast(device_type)

        try:
            tts = TTS(MODEL_NAME).to(device)
            _extract_latents_and_warmup()
        except torch.cuda.OutOfMemoryError:
            tts = None
            torch.cuda.empty_cache()
            log.error(f"GPU {gpu_index}: out of memory while loading")
            return "no vram (out of memory during load)"
        except Exception as e:
            tts = None
            log.error(f"Model load failed: {e}")
            return f"error: {e}"

        where = f"gpu {gpu_index}" if gpu_index is not None else device_type
        return f"ok loaded on {where}"


def ensure_model_loaded() -> bool:
    if tts is not None:
        return True
    load_model(default_gpu_index())
    return tts is not None

# ===============================
# queues & events
# ===============================
audio_queue = queue.Queue(maxsize=12)
request_queue = queue.Queue()
interrupt_event = threading.Event()
outputting = threading.Event()
inference_lock = threading.Lock()

# ===============================
# audio player thread
# ===============================
SAMPLE_RATE = 24000  # XTTS v2 always outputs 24 kHz


def audio_player():
    while True:
        try:
            wav = audio_queue.get(timeout=0.2)
        except queue.Empty:
            outputting.clear()
            continue

        if wav is None or interrupt_event.is_set():
            continue

        outputting.set()
        sd.play(wav, SAMPLE_RATE, blocking=True)

        if audio_queue.empty():
            outputting.clear()

# ===============================
# text cleaner
# ===============================
def clean_text(text: str) -> str:
    text = text.replace("I've", "I have")
    text = text.replace("I'm", "I am")
    text = text.replace("don't", "do not")
    text = text.replace("can't", "cannot")

    if "\u0010" in text:
        text = text.split("\u0010")[-1] if "\u0010" in text else text.split("\n")[-1]

    return text.strip()

# ===============================
# sentence splitter + merge
# ===============================
def split_sentences(text: str) -> list[str]:
    raw = re.split(r'(?<=[.!?؟…;:—])\s+|(?<=\.\.\.)\s+', text)
    raw = [c.strip() for c in raw if c.strip()]

    if not raw:
        return [text]

    expanded = []
    for chunk in raw:
        words = chunk.split()
        if len(words) > 12:
            for i in range(0, len(words), 12):
                sub = " ".join(words[i:i+12])
                if sub:
                    expanded.append(sub)
        else:
            expanded.append(chunk)

    result = []
    i = 0
    while i < len(expanded):
        current = expanded[i]
        current_words = len(current.split())

        if current_words < 4 and i + 1 < len(expanded):
            next_chunk = expanded[i + 1]
            next_words = len(next_chunk.split())
            combined_words = current_words + next_words

            if combined_words < 8:
                result.append(current + " " + next_chunk)
                i += 2
            else:
                result.append(current)
                i += 1
        else:
            result.append(current)
            i += 1

    return result if result else [text]

# ===============================
# generate one chunk
# ===============================
def generate_chunk(sentence: str) -> np.ndarray | None:
    try:
        with inference_lock, torch.inference_mode(), _autocast:
            out = tts.synthesizer.tts_model.inference(
                text=sentence,
                language="en",
                gpt_cond_latent=gpt_cond_latent,
                speaker_embedding=speaker_embedding,
                temperature=0.3,
                top_p=0.7,
                speed=1.0,
                top_k=20,
            )
        wav = out["wav"] if isinstance(out, dict) else out
        wav = np.asarray(wav, dtype=np.float32)

        peak = np.max(np.abs(wav))
        if peak > 1e-9:
            target_rms = 0.05
            current_rms = np.sqrt(np.mean(wav**2))
            if current_rms > 1e-9:
                scale = target_rms / current_rms
                wav = np.clip(wav * scale, -0.95, 0.95)

        return wav
    except Exception as e:
        log.error(f"TTS inference error: {e}")
        return None

# ===============================
# TTS handler
# ===============================
def handle_tts(text: str):
    if tts is None:
        log.warning("TTS requested but no model is loaded — ignoring")
        return

    text = clean_text(text).replace("\n", "")
    if not text:
        return

    log.info(f"TTS start | text={text!r}")

    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            break

    sentences = split_sentences(text)
    log.debug(f"TTS split  | {len(sentences)} chunk(s): {sentences}")
    if not sentences:
        return

    t0 = time.perf_counter()
    current_wav = generate_chunk(sentences[0])
    elapsed = (time.perf_counter() - t0) * 1000
    if current_wav is not None:
        dur = len(current_wav) / SAMPLE_RATE * 1000
        log.debug(f"TTS chunk  | [0] gen={elapsed:.0f}ms  dur={dur:.0f}ms  RTF={elapsed/max(dur,1):.2f}")

    next_result = [None]
    gen_thread = None

    try:
        for i in range(len(sentences)):
            if interrupt_event.is_set():
                break

            if i > 0:
                if gen_thread:
                    gen_thread.join()
                current_wav = next_result[0]
                next_result = [None]

            if current_wav is None:
                continue

            if interrupt_event.is_set():
                break

            try:
                audio_queue.put(current_wav, timeout=1.0)
            except queue.Full:
                log.warning("TTS queue full, skipping chunk")
                continue

            if i + 1 < len(sentences) and not interrupt_event.is_set():
                next_result = [None]

                def _gen(s=sentences[i + 1], res=next_result, idx=i + 1):
                    t0 = time.perf_counter()
                    res[0] = generate_chunk(s)
                    elapsed = (time.perf_counter() - t0) * 1000
                    if res[0] is not None:
                        dur = len(res[0]) / SAMPLE_RATE * 1000
                        log.debug(f"TTS chunk  | [{idx}] gen={elapsed:.0f}ms  dur={dur:.0f}ms  RTF={elapsed/max(dur,1):.2f}")

                gen_thread = threading.Thread(target=_gen, daemon=True)
                gen_thread.start()
            else:
                gen_thread = None
    finally:
        if gen_thread:
            gen_thread.join()

    if interrupt_event.is_set():
        log.info("TTS end    | interrupted before all chunks finished")
    else:
        log.info("TTS end    | all chunks queued successfully")

# ===============================
# TTS worker
# ===============================
def tts_worker():
    while True:
        text = request_queue.get()
        interrupt_event.clear()
        if not ensure_model_loaded():
            log.error("No model could be loaded — dropping request")
            continue
        handle_tts(text)

# ===============================
# recv full
# ===============================
def recv_full(conn) -> str:
    conn.settimeout(10.0)
    data = b""
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
    except (socket.timeout, ConnectionResetError):
        pass
    return data.decode(errors="ignore").strip()

# ===============================
# helpers
# ===============================
def _drain(q: queue.Queue):
    while not q.empty():
        try:
            q.get_nowait()
        except queue.Empty:
            break


def client_allowed(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if ip.is_loopback:
        return True
    if ip.is_private or ip.is_link_local:
        return ALLOW_LOCAL
    return ALLOW_PUBLIC

# ===============================
# server
# ===============================
_req_counter = 0
_req_lock = threading.Lock()


def socket_server():
    global _req_counter

    bind_host = "0.0.0.0" if (ALLOW_LOCAL or ALLOW_PUBLIC) else "127.0.0.1"

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((bind_host, PORT))
    server.listen(5)
    log.info(f"Server listening on {bind_host}:{PORT}")

    while True:
        conn, addr = server.accept()
        client_ip = addr[0]
        client_port = addr[1]

        if not client_allowed(client_ip):
            log.warning(f"Rejected connection from {client_ip}:{client_port} (not allowed by settings)")
            conn.close()
            continue

        with _req_lock:
            _req_counter += 1
            req_id = _req_counter

        recv_start = time.perf_counter()
        text = recv_full(conn)
        recv_ms = (time.perf_counter() - recv_start) * 1000

        stripped = text.strip()
        log.info(
            f"REQ #{req_id:05d} | "
            f"from={client_ip}:{client_port} | "
            f"recv={recv_ms:.1f}ms | "
            f"len={len(text)} | "
            f"text={text!r:.120}"
        )

        if stripped == "Command_TTS:get_gpus":
            resp = json.dumps({"gpus": list_gpus()})
            conn.sendall(resp.encode("utf-8"))
            conn.close()
            log.info(f"REQ #{req_id:05d} | command=get_gpus -> {resp}")
            continue

        run_match = re.fullmatch(r"Command_TTS:run\[(\d+)\]", stripped)
        if run_match:
            idx = int(run_match.group(1))
            log.info(f"REQ #{req_id:05d} | command=run[{idx}] — switching GPU")
            interrupt_event.set()
            sd.stop()
            _drain(audio_queue)
            _drain(request_queue)
            result = load_model(idx)
            interrupt_event.clear()
            conn.sendall(result.encode("utf-8"))
            conn.close()
            log.info(f"REQ #{req_id:05d} | run result: {result}")
            continue

        conn.sendall(b"ok")
        conn.close()

        text = stripped.replace("frtgg", "fartigg")
        if not text:
            log.warning(f"REQ #{req_id:05d} | empty request — ignored")
            continue

        if text.lower() == "exit":
            log.info(f"REQ #{req_id:05d} | command=EXIT — shutting down")
            interrupt_event.set()
            sd.stop()
            _drain(audio_queue)

            if tts is not None:
                with inference_lock, torch.inference_mode(), _autocast:
                    out = tts.synthesizer.tts_model.inference(
                        text="goodbye",
                        language="en",
                        gpt_cond_latent=gpt_cond_latent,
                        speaker_embedding=speaker_embedding,
                    )
                wav = out["wav"] if isinstance(out, dict) else out
                wav = np.asarray(wav, dtype=np.float32)
                wav /= np.max(np.abs(wav)) + 1e-9
                sd.play(wav, SAMPLE_RATE, blocking=True)

            log.info("Server stopped cleanly")
            server.close()
            sys.exit(0)

        if outputting.is_set():
            log.info(f"REQ #{req_id:05d} | action=INTERRUPT+REPLACE (audio was playing)")
            interrupt_event.set()
            sd.stop()
            _drain(audio_queue)
            _drain(request_queue)
            request_queue.put(text)
        else:
            log.info(f"REQ #{req_id:05d} | action=QUEUED (queue_size≈{request_queue.qsize()+1})")
            request_queue.put(text)

# ===============================
# start
# ===============================
log.info("Starting audio_player thread")
threading.Thread(target=audio_player, daemon=True).start()
log.info("Starting tts_worker thread")
threading.Thread(target=tts_worker, daemon=True).start()
socket_server()

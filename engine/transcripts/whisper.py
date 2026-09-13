"""Local transcription with whisper.cpp.

Arch ships `whisper-cpp` (the `whisper-cli` binary) and, separately, the
`ggml-vulkan` backend that ggml loads at runtime when present — on this
class of machine that is the difference between real time and twenty times
faster. Models are downloaded on first use into the cache.

The runner is deliberately plain: convert the audio to 16 kHz mono WAV with
ffmpeg, run whisper-cli over it in ten-minute chunks under nice/ionice, and
hand each chunk's segments back as they land so the transcript view can fill
in while the rest is still being worked out. Everything here blocks; the
manager drives it from a thread.
"""

import glob
import json
import os
import shutil
import subprocess
import tempfile
import time

from .. import http, log
from .canonical import segment

LOG = log.get("whisper")

MODEL_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
MODELS = {
    # name: (file, approx bytes, needs GPU to be pleasant)
    "large-v3-turbo": ("ggml-large-v3-turbo-q5_0.bin", 574041195, True),
    "small": ("ggml-small-q5_1.bin", 190085487, False),
    "base": ("ggml-base-q5_1.bin", 59707625, False),
    "tiny": ("ggml-tiny-q5_1.bin", 32166155, False),
}
GGML_MAGIC = (b"lmgg", b"ggml", b"GGUF", b"tjgg", b"lmgg")
CHUNK_SECONDS = 600
OVERLAP_SECONDS = 4
VRAM_FOR_TURBO = 1.5 * 1024 ** 3
DEFAULT_MAX_LEN = 60


class TranscribeError(Exception):
    pass


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- detection

def detect(models_dir, settings=None):
    binary = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
    vulkan = bool(glob.glob("/usr/lib/libggml-vulkan*.so*")) and bool(glob.glob("/dev/dri/renderD*"))
    vram = 0
    for path in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
        try:
            with open(path) as handle:
                vram = max(vram, int(handle.read().strip()))
        except (OSError, ValueError):
            pass
    cpus = os.cpu_count() or 2
    device_pref = str(getattr(settings, "whisperDevice", "auto") or "auto")
    gpu = vulkan and device_pref != "cpu"
    model_pref = str(getattr(settings, "whisperModel", "auto") or "auto")
    if model_pref in MODELS:
        model = model_pref
    elif gpu and vram >= VRAM_FOR_TURBO:
        model = "large-v3-turbo"
    elif cpus >= 8:
        model = "small"
    else:
        model = "base"
    model_file = MODELS[model][0]
    present = os.path.exists(os.path.join(models_dir, model_file))
    return {
        "available": binary is not None,
        "binary": binary or "",
        "gpu": gpu,
        "vulkan": vulkan,
        "vram": vram,
        "cpus": cpus,
        "model": model,
        "modelFile": model_file,
        "modelPresent": present,
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }


def estimate_seconds(duration, info):
    """Rough wall-clock guess for the UI: GPU turbo ~15x, CPU small ~1.5x,
    CPU base ~4x real time on a four-core desktop."""
    duration = float(duration or 0)
    if info.get("gpu"):
        factor = 15.0 if info.get("model") == "large-v3-turbo" else 30.0
    else:
        factor = {"tiny": 8.0, "base": 4.0, "small": 1.5, "large-v3-turbo": 0.3}.get(info.get("model"), 2.0)
    return int(duration / factor) + 15


# ---------------------------------------------------------------- models

def model_path(models_dir, model):
    return os.path.join(models_dir, MODELS[model][0])


def model_ok(path, model):
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    expected = MODELS[model][1]
    if size < expected * 0.9:
        return False
    with open(path, "rb") as handle:
        magic = handle.read(4)
    return magic in GGML_MAGIC


def download_model(models_dir, model, progress=None, cancel=None):
    """Blocking, resumable download of a model file. `progress(done, total)`
    is called at most every half second."""
    file_name, expected, _ = MODELS[model]
    target = os.path.join(models_dir, file_name)
    if model_ok(target, model):
        return target
    os.makedirs(models_dir, exist_ok=True)
    part = target + ".part"
    done = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"Range": "bytes=%d-" % done} if done else {}
    response = http.open_stream(MODEL_BASE_URL + file_name, timeout=60, headers=headers)
    try:
        status = getattr(response, "status", 200)
        mode = "ab" if status == 206 else "wb"
        if mode == "wb":
            done = 0
        total_header = response.headers.get("Content-Length")
        total = (done + int(total_header)) if total_header and total_header.isdigit() and mode == "ab" else (int(total_header) if total_header and total_header.isdigit() else expected)
        last = 0.0
        with open(part, mode) as handle:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if progress and now - last > 0.5:
                    last = now
                    progress(done, total)
    finally:
        response.close()
    os.replace(part, target)
    if not model_ok(target, model):
        os.unlink(target)
        raise TranscribeError("the downloaded model file looks wrong; try again")
    if progress:
        progress(total, total)
    return target


# ---------------------------------------------------------------- pipeline

def _nice(argv):
    prefix = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "3"]
    return prefix + argv


def convert_to_wav(source, wav_path, cancel=None):
    """ffmpeg -> 16 kHz mono s16le WAV, the only input whisper.cpp wants.
    Returns the duration in seconds."""
    argv = _nice(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", source, "-vn", "-ac", "1", "-ar", "16000",
                  "-c:a", "pcm_s16le", "-f", "wav", wav_path])
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _wait(process, cancel)
    if process.returncode != 0:
        err = (process.stderr.read() if process.stderr else b"").decode("utf-8", "replace").strip()
        raise TranscribeError("ffmpeg could not read the audio: %s" % (err.splitlines()[-1] if err else "unknown error"))
    try:
        size = os.path.getsize(wav_path)
    except OSError:
        raise TranscribeError("ffmpeg produced no audio")
    return max(0.0, (size - 44) / 32000.0)


def _wait(process, cancel):
    while True:
        try:
            process.wait(timeout=0.25)
            return
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise Cancelled()


def transcribe_chunk(binary, model_file, wav_path, offset_seconds, duration_seconds, language, gpu, threads, cancel=None):
    """One whisper-cli run over [offset, offset+duration]. Returns
    (segments in absolute seconds, detected language)."""
    with tempfile.TemporaryDirectory(prefix="whisper-") as tmp:
        base = os.path.join(tmp, "out")
        argv = [binary, "-m", model_file, "-f", wav_path, "-ot", str(int(offset_seconds * 1000)),
                "-d", str(int(duration_seconds * 1000)), "-l", language or "auto", "-oj", "-of", base,
                "-ml", str(DEFAULT_MAX_LEN), "-sow", "-np", "-t", str(max(1, threads))]
        if not gpu:
            argv.append("-ng")
        process = subprocess.Popen(_nice(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _wait(process, cancel)
        if process.returncode != 0:
            err = (process.stderr.read() if process.stderr else b"").decode("utf-8", "replace").strip()
            raise TranscribeError("whisper-cli failed: %s" % (err.splitlines()[-1][:200] if err else "exit %d" % process.returncode))
        try:
            with open(base + ".json", "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as error:
            raise TranscribeError("whisper-cli wrote no result: %s" % error)
    detected = ""
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        detected = str(result.get("language") or "")
    segments = []
    for item in payload.get("transcription", []) if isinstance(payload, dict) else []:
        offsets = item.get("offsets") or {}
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(offsets.get("from", 0)) / 1000.0
            end = float(offsets.get("to", 0)) / 1000.0
        except (TypeError, ValueError):
            continue
        if _looks_like_hallucination(text):
            continue
        segments.append(segment(start, end, text))
    return segments, detected


_JUNK = ("[music]", "[blank_audio]", "(music)", "[silence]", "[applause]", "♪", "[ __ ]")


def _looks_like_hallucination(text):
    lowered = text.lower().strip()
    if lowered in _JUNK or all(part in _JUNK for part in lowered.split()):
        return True
    # whisper loops a phrase on long silences; three identical words in a row is one.
    words = lowered.split()
    if len(words) >= 6 and len(set(words)) <= 2:
        return True
    return False


def plan_chunks(duration, chunk_seconds=CHUNK_SECONDS):
    """[(offset, length)] covering `duration` with a small overlap so a word
    cut at a boundary is caught by the next chunk."""
    chunks = []
    offset = 0.0
    while offset < duration:
        length = min(chunk_seconds + OVERLAP_SECONDS, duration - offset)
        chunks.append((offset, length))
        offset += chunk_seconds
    return chunks or [(0.0, duration)]


def merge_chunk(existing, new_segments, chunk_offset, chunk_seconds=CHUNK_SECONDS):
    """Append a chunk's segments, dropping the ones that belong to the
    previous chunk's window (the overlap)."""
    cutoff = chunk_offset if chunk_offset > 0 else -1.0
    last_end = existing[-1]["endTime"] if existing and existing[-1].get("endTime") else -1.0
    kept = []
    for seg in new_segments:
        if seg["startTime"] < cutoff - 0.05 and chunk_offset > 0:
            continue
        if seg["startTime"] < last_end - 0.5:
            continue
        kept.append(seg)
    return existing + kept

"""Local transcription with whisper.cpp.

Arch ships `whisper-cpp` (the `whisper-cli` binary) and, separately, the
`ggml-vulkan` backend that ggml loads at runtime when present — on this
class of machine that is the difference between real time and twenty times
faster. Models are downloaded on first use into the cache from one pinned
revision of ggerganov/whisper.cpp and verified against a SHA-256 recorded
here before whisper-cli ever opens them.

The runner is deliberately plain: convert the audio to 16 kHz mono WAV with
ffmpeg, run whisper-cli over it in ten-minute chunks under nice/ionice, and
hand each chunk's segments back as they land so the transcript view can fill
in while the rest is still being worked out. Everything here blocks; the
manager drives it from a thread.
"""

import collections
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time

from .. import fsio, http, log
from .canonical import segment

LOG = log.get("whisper")

Model = collections.namedtuple("Model", "file size sha256 gpu")

# One immutable revision of https://huggingface.co/ggerganov/whisper.cpp; the
# sizes and digests are the LFS objects at that commit. Bump all three
# together, never the URL alone.
MODEL_REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"
MODEL_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/"
MODELS = {
    "large-v3-turbo": Model("ggml-large-v3-turbo-q5_0.bin", 574041195,
                            "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2", True),
    "small": Model("ggml-small-q5_1.bin", 190085487,
                   "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb", False),
    "base": Model("ggml-base-q5_1.bin", 59707625,
                  "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898", False),
    "tiny": Model("ggml-tiny-q5_1.bin", 32152673,
                  "818710568da3ca15689e31a743197b520007872ff9576237bda97bd1b469c3d7", False),
}
MODEL_STALL_SECONDS = 90
MODEL_TOTAL_DEADLINE = 2 * 3600
MODEL_SPACE_SLACK = 1.1
HASH_CHUNK = 1 << 20
CHUNK_SECONDS = 600
OVERLAP_SECONDS = 4
VRAM_FOR_TURBO = 1.5 * 1024 ** 3
DEFAULT_MAX_LEN = 60
# Language codes whisper.cpp accepts with -l (its whisper_lang_id table).
LANGUAGES = frozenset("""
en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms cs ro da hu ta no th ur hr bg lt la mi ml cy
sk te fa lv bn sr az sl kn et mk br eu is hy ne mn bs kk sq sw gl mr pa si km sn yo so af oc ka be tg sd gu am yi lo
uz fo ht ps tk nn mt sa lb my bo tl mg as tt haw ln ha ba jw su yue
""".split())


def normalize_language(value):
    """A feed's language tag reduced to a code whisper knows, else 'auto'."""
    code = str(value or "").strip().lower().replace("_", "-").split("-")[0]
    if code == "iw":
        code = "he"
    return code if code in LANGUAGES else "auto"


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
    entry = MODELS[model]
    present = _size_matches(os.path.join(models_dir, entry.file), entry.size)
    return {
        "available": binary is not None,
        "binary": binary or "",
        "gpu": gpu,
        "vulkan": vulkan,
        "vram": vram,
        "cpus": cpus,
        "model": model,
        "modelFile": entry.file,
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
    return os.path.join(models_dir, MODELS[model].file)


def model_url(entry):
    return MODEL_BASE_URL + MODEL_REVISION + "/" + entry.file


def _size_matches(path, size):
    try:
        return os.stat(path).st_size == size
    except OSError:
        return False


def _sha256_fd(handle, cancel=None):
    digest = hashlib.sha256()
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        block = handle.read(HASH_CHUNK)
        if not block:
            return digest.hexdigest()
        digest.update(block)


def verify_model(path, entry, cancel=None):
    """True when `path` is our own regular file of exactly the pinned size
    and SHA-256. Blocking: about a second per 500 MB on an SSD."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != entry.size:
            return False
        return _sha256_fd(handle, cancel) == entry.sha256


class _Restart(Exception):
    """The server's answer does not fit the partial file: start over."""


class _Discard(Exception):
    """The transfer can never complete correctly: drop the partial file."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _check_space(directory, size):
    try:
        usage = shutil.disk_usage(directory)
    except OSError:
        return
    if usage.free < int(size * MODEL_SPACE_SLACK):
        raise TranscribeError("not enough free space for the model (%d MB left, %d MB needed)"
                              % (usage.free // (1024 * 1024), size // (1024 * 1024)))


def _probe(url, entry):
    """Fail before the transfer when the server already says the object is
    not the one pinned here. Hugging Face exposes the LFS size and digest on
    its redirect; a host that does not is simply not pre-checked."""
    headers = http.probe_headers(url)
    etag = str(headers.get("x-linked-etag") or "").strip().strip('"').lower()
    size = str(headers.get("x-linked-size") or "").strip()
    if (etag and etag != entry.sha256) or (size.isdigit() and int(size) != entry.size):
        raise TranscribeError("the model on the server no longer matches the pinned digest; not downloading")


def _sidecar_path(target):
    return target + ".download.json"


def _read_sidecar(models_dir, entry, sidecar):
    """The recorded temp name of an unfinished download, if it still fits
    the pinned table; anything else is forgotten."""
    try:
        with open(sidecar, "rb") as handle:
            data = json.loads(handle.read(4096))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    temp = str(data.get("temp") or "")
    pattern = re.escape(entry.file) + r"\.[0-9a-f]{16}\.part"
    if (data.get("size") != entry.size or data.get("sha256") != entry.sha256
            or data.get("revision") != MODEL_REVISION or not re.fullmatch(pattern, temp)):
        return None
    return os.path.join(models_dir, temp)


def _sweep_orphans(models_dir, entry, keep):
    """Drop partial files of this model that no valid sidecar claims."""
    pattern = re.escape(entry.file) + r"\.[0-9a-f]{16}\.part"
    try:
        names = os.listdir(models_dir)
    except OSError:
        return
    for name in names:
        path = os.path.join(models_dir, name)
        if re.fullmatch(pattern, name) and path != keep:
            _unlink_quiet(path)


def _open_resume(models_dir, entry, sidecar):
    temp = _read_sidecar(models_dir, entry, sidecar)
    _sweep_orphans(models_dir, entry, temp)
    if temp is None:
        _unlink_quiet(sidecar)
        return None
    try:
        fd = fsio.open_nofollow(temp, os.O_WRONLY | os.O_APPEND)
    except OSError:
        _unlink_quiet(sidecar)
        return None
    info = os.fstat(fd)
    if info.st_nlink != 1 or not 0 < info.st_size < entry.size:
        os.close(fd)
        _unlink_quiet(temp)
        _unlink_quiet(sidecar)
        return None
    return fd, temp, info.st_size


def _open_fresh(models_dir, entry, sidecar):
    fd, temp = fsio.open_new(models_dir, entry.file + ".", ".part")
    fsio.atomic_write(sidecar, json.dumps({
        "temp": os.path.basename(temp), "size": entry.size, "sha256": entry.sha256, "revision": MODEL_REVISION,
    }))
    return fd, temp, 0


def _content_range(value):
    """('bytes S-E/T') -> (S, T); (None, None) when absent or malformed."""
    match = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*", str(value or ""))
    if not match:
        return None, None
    start, _end, total = match.groups()
    return int(start), (int(total) if total != "*" else None)


def _int_header(value):
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def download_model(models_dir, model, progress=None, cancel=None):
    """Blocking, resumable download of a model file, verified against the
    pinned size and SHA-256 before it is installed. `progress(done, total,
    phase)` is called at most every half second; phases are "downloading",
    "verifying" and "done". Raises TranscribeError, Cancelled or
    http.FetchError (the latter leaves a resumable partial file behind)."""
    entry = MODELS[model]
    target = model_path(models_dir, model)
    sidecar = _sidecar_path(target)
    url = model_url(entry)
    if verify_model(target, entry, cancel):
        return target
    # A file of the wrong size or digest is never kept, whoever put it there.
    _unlink_quiet(target)
    os.makedirs(models_dir, exist_ok=True)
    _unlink_quiet(target + ".part")      # the predictable temp older releases used
    _check_space(models_dir, entry.size)
    _probe(url, entry)
    for _attempt in range(2):
        resumed = _open_resume(models_dir, entry, sidecar)
        fd, temp, done = resumed or _open_fresh(models_dir, entry, sidecar)
        response = None
        try:
            headers = {"Range": "bytes=%d-" % done} if done else None
            response = http.open_stream(url, timeout=60, headers=headers)
            status = getattr(response, "status", 200)
            if done and status == 206:
                start, total = _content_range(response.headers.get("Content-Range"))
                if start != done or total != entry.size:
                    raise _Restart()
            elif status == 200:
                if done:
                    # The server ignored the range: reuse the temp from byte 0.
                    os.ftruncate(fd, 0)
                    os.lseek(fd, 0, os.SEEK_SET)
                    done = 0
            else:
                raise http.FetchError("http", "server answered %d" % status, status=status)
            length = _int_header(response.headers.get("Content-Length"))
            if length is not None and length != entry.size - done:
                kind = "too-large" if length > entry.size - done else "http"
                raise _Discard(kind, "the server offers %d bytes but the pinned model is %d" % (length + done, entry.size))
            started = time.monotonic()
            last_progress = rate_at = alive_at = started
            rate_bytes = done
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = http.read_chunk(response)
                if not chunk:
                    break
                done += len(chunk)
                if done > entry.size:
                    raise _Discard("too-large", "the server sent more than the pinned model size")
                _write_all(fd, chunk)
                now = time.monotonic()
                if now - started > MODEL_TOTAL_DEADLINE:
                    raise http.FetchError("network", "the model download took more than two hours")
                if now - rate_at >= 1.0:
                    rate = (done - rate_bytes) / (now - rate_at)
                    rate_at, rate_bytes = now, done
                    if rate >= 256:
                        alive_at = now
                    elif now - alive_at > MODEL_STALL_SECONDS:
                        raise http.FetchError("network", "the server stopped sending")
                if progress and now - last_progress > 0.5:
                    last_progress = now
                    progress(done, entry.size, "downloading")
            if done != entry.size:
                raise http.FetchError("network", "connection dropped at %d of %d bytes" % (done, entry.size))
            os.fsync(fd)
            os.close(fd)
            fd = None
            if progress:
                progress(entry.size, entry.size, "verifying")
            with open(temp, "rb") as handle:
                digest = _sha256_fd(handle, cancel)
            if digest != entry.sha256:
                raise _Discard("integrity", "the downloaded model failed its integrity check")
            os.replace(temp, target)
            _unlink_quiet(sidecar)
            fsio._fsync_dir(models_dir)
            if progress:
                progress(entry.size, entry.size, "done")
            return target
        except _Restart:
            _close_quiet(fd)
            _unlink_quiet(temp)
            _unlink_quiet(sidecar)
            continue
        except _Discard as error:
            _close_quiet(fd)
            _unlink_quiet(temp)
            _unlink_quiet(sidecar)
            if error.kind == "integrity":
                raise TranscribeError(error.message)
            raise http.FetchError(error.kind, error.message)
        except BaseException:
            # Network errors and cancellation keep the partial file for a resume.
            _close_quiet(fd)
            raise
        finally:
            if response is not None:
                response.close()
    raise TranscribeError("the server's copy of the model keeps changing; try again later")


def _close_quiet(fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------- pipeline

def _nice(argv):
    prefix = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "3"]
    return prefix + argv


_CHILDREN = set()
_CHILDREN_LOCK = threading.Lock()


def _run(argv, errlog, cancel, deadline_seconds):
    """Run a tool in its own process group, registered so a restart can
    reap it, and wait for it under a cancel flag and a wall-clock limit."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errlog,
                               start_new_session=True, close_fds=True)
    with _CHILDREN_LOCK:
        _CHILDREN.add(process)
    try:
        _wait(process, cancel, time.monotonic() + deadline_seconds)
    finally:
        with _CHILDREN_LOCK:
            _CHILDREN.discard(process)
    return process


def _kill_group(process):
    # start_new_session makes the child its own group leader (pgid == pid),
    # so this also reaches anything nice/ionice or the tool itself forked.
    for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except OSError:
            pass
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def kill_children():
    """Stop every ffmpeg/whisper-cli this process started; called before the
    daemon re-executes itself so nothing keeps running under the new image."""
    with _CHILDREN_LOCK:
        procs = list(_CHILDREN)
    for process in procs:
        _kill_group(process)


def convert_to_wav(source, wav_path, cancel=None, deadline_seconds=3600):
    """ffmpeg -> 16 kHz mono s16le WAV, the only input whisper.cpp wants.
    Returns the duration in seconds."""
    argv = _nice(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", source, "-vn", "-ac", "1", "-ar", "16000",
                  "-c:a", "pcm_s16le", "-f", "wav", wav_path])
    # stderr goes to a file, never a pipe: _wait does not drain, and a damaged
    # file makes ffmpeg print one line per bad frame.
    with tempfile.TemporaryFile(prefix="ffmpeg-stderr-") as errlog:
        process = _run(argv, errlog, cancel, deadline_seconds)
        if process.returncode != 0:
            errlog.seek(0)
            err = errlog.read()[-4096:].decode("utf-8", "replace").strip()
            raise TranscribeError("ffmpeg could not read the audio: %s" % (err.splitlines()[-1][:200] if err else "exit %d" % process.returncode))
    try:
        size = os.path.getsize(wav_path)
    except OSError:
        raise TranscribeError("ffmpeg produced no audio")
    return max(0.0, (size - 44) / 32000.0)


def _wait(process, cancel, deadline):
    # Polls; never hand the child a PIPE, nothing here drains it.
    while True:
        try:
            process.wait(timeout=0.25)
            return
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set():
            _kill_group(process)
            raise Cancelled()
        if time.monotonic() > deadline:
            _kill_group(process)
            raise TranscribeError("%s ran past its time limit and was stopped" % os.path.basename(str(process.args[0])))


def transcribe_chunk(binary, model_file, wav_path, offset_seconds, duration_seconds, language, gpu, threads,
                     cancel=None, deadline_seconds=1800):
    """One whisper-cli run over [offset, offset+duration]. Returns
    (segments in absolute seconds, detected language)."""
    with tempfile.TemporaryDirectory(prefix="whisper-") as tmp:
        base = os.path.join(tmp, "out")
        argv = [binary, "-m", model_file, "-f", wav_path, "-ot", str(int(offset_seconds * 1000)),
                "-d", str(int(duration_seconds * 1000)), "-l", language or "auto", "-oj", "-of", base,
                "-ml", str(DEFAULT_MAX_LEN), "-sow", "-np", "-t", str(max(1, threads))]
        if not gpu:
            argv.append("-ng")
        with open(os.path.join(tmp, "stderr"), "w+b") as errlog:
            process = _run(_nice(argv), errlog, cancel, deadline_seconds)
            if process.returncode != 0:
                errlog.seek(0)
                err = errlog.read()[-4096:].decode("utf-8", "replace").strip()
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

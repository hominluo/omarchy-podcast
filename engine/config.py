"""Where things live, and what the user asked for.

Three layers of configuration, kept deliberately separate:

  * `Paths`: XDG-derived directories. Nothing here is ever written inside the
    plugin folder (see podcastd.py for why).
  * `Settings`: the user-facing knobs. Their defaults are the manifest's
    `barWidget.defaults`, so the manifest is the single source of truth and
    the shell pushes the effective values with the `configure` command.
  * Credentials: secrets the Settings view sends with `set-credentials`. They
    are written to a 0600 file outside shell.json, which is plain-text and
    routinely pasted into bug reports.
"""

import json
import os
import socket
import stat

from . import PLUGIN_ID, fsio

MANIFEST_NAME = "manifest.json"
CREDENTIALS_NAME = "credentials.json"

# Kept in step with manifest.json by tests/test_config.py. This copy only
# matters when the manifest cannot be read, which should never happen, but a
# daemon that refuses to start over a missing default is worse than one that
# runs on a stale copy.
FALLBACK_DEFAULTS = {
    "showTitle": True,
    "maxLabelWidth": 160,
    "barProgress": False,
    "wheelAction": "seek",
    "skipBack": 15,
    "skipForward": 30,
    "defaultSpeed": 1.0,
    "continuousPlayback": True,
    "sleepTimerDefault": 30,
    "searchProvider": "itunes",
    "searchCountry": "auto",
    "downloadDir": "~/Music/Podcasts",
    "autoDownload": "latest",
    "keepDownloads": 5,
    "deletePlayedAfterDays": 7,
    "initialInboxCount": 1,
    "notifyNewEpisodes": False,
    "refreshIntervalMin": 30,
    "transcriptionPolicy": "on-demand",
    "whisperDevice": "auto",
    "whisperModel": "auto",
    "syncProvider": "none",
    "syncServer": "https://gpodder.net",
    "syncUsername": "",
    "syncDeviceId": "",
    "syncIntervalMin": 10,
}


def _xdg(var, fallback):
    value = os.environ.get(var)
    if value:
        return value
    return os.path.join(os.path.expanduser("~"), fallback)


def private_dir(path):
    """True when `path` is a directory of ours that nobody else can enter:
    not a symlink, owned by this uid, no group/other bits."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not (info.st_mode & 0o077)


def runtime_base():
    """The per-user runtime directory the socket may live in, or "" when
    there is none. A shared temp directory is never a substitute: another
    user could sit on the socket name first."""
    value = os.environ.get("XDG_RUNTIME_DIR")
    if value:
        return value
    candidate = "/run/user/%d" % os.getuid()
    return candidate if private_dir(candidate) else ""


class Paths:
    """Every directory and well-known file the daemon touches."""

    def __init__(self, plugin_dir, home=None):
        self.plugin_dir = os.path.abspath(plugin_dir)
        base = runtime_base()
        self.runtime_dir = os.path.join(base, "omarchy-podcast") if base else ""
        self.config_dir = os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "omarchy", "podcast")
        self.state_dir = os.path.join(_xdg("XDG_STATE_HOME", ".local/state"), "omarchy", "podcast")
        self.cache_dir = os.path.join(_xdg("XDG_CACHE_HOME", ".cache"), "omarchy", "podcast")

        # All "" when there is no runtime dir: nothing may resolve to a
        # relative name in the current directory.
        under = (lambda name: os.path.join(self.runtime_dir, name)) if self.runtime_dir else (lambda name: "")
        self.socket_path = under("daemon.sock")
        self.lock_path = under("daemon.lock")
        self.info_path = under("daemon.json")
        self.mpv_socket_path = under("mpv.sock")
        self.mpv_pid_path = under("mpv.pid")

        self.db_path = os.path.join(self.state_dir, "podcasts.db")
        self.log_path = os.path.join(self.state_dir, "daemon.log")
        self.credentials_path = os.path.join(self.config_dir, CREDENTIALS_NAME)
        self.manifest_path = os.path.join(self.plugin_dir, MANIFEST_NAME)

        self.artwork_dir = os.path.join(self.cache_dir, "artwork")
        self.transcripts_dir = os.path.join(self.cache_dir, "transcripts")
        self.chapters_dir = os.path.join(self.cache_dir, "chapters")
        self.audio_dir = os.path.join(self.cache_dir, "audio")
        self.models_dir = os.path.join(self.cache_dir, "models")
        self.thumbs_dir = os.path.join(self.cache_dir, "thumbs")
        self.pycache_dir = os.path.join(self.cache_dir, "pycache")
        self.skipsilence_script = os.path.join(self.plugin_dir, "mpv", "skipsilence.lua")

    def ensure(self):
        if not self.runtime_dir:
            raise RuntimeError("no private runtime directory: XDG_RUNTIME_DIR is unset and /run/user/%d is not ours" % os.getuid())
        os.makedirs(self.runtime_dir, mode=0o700, exist_ok=True)
        # makedirs(exist_ok=True) is happy with a symlink or a stranger's
        # directory at that name; the daemon is not.
        if not private_dir(self.runtime_dir):
            raise RuntimeError("%s is not a private directory owned by this user; refusing to start" % self.runtime_dir)
        for path in (
            self.config_dir, self.state_dir, self.cache_dir, self.artwork_dir,
            self.transcripts_dir, self.chapters_dir, self.audio_dir, self.models_dir, self.thumbs_dir,
        ):
            os.makedirs(path, exist_ok=True)


def load_manifest(paths):
    try:
        with open(paths.manifest_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def manifest_defaults(paths):
    manifest = load_manifest(paths)
    widget = manifest.get("barWidget") if isinstance(manifest, dict) else None
    defaults = widget.get("defaults") if isinstance(widget, dict) else None
    if isinstance(defaults, dict) and defaults:
        merged = dict(FALLBACK_DEFAULTS)
        merged.update(defaults)
        return merged
    return dict(FALLBACK_DEFAULTS)


def manifest_version(paths):
    manifest = load_manifest(paths)
    return str(manifest.get("version", "")) if isinstance(manifest, dict) else ""


class Settings:
    """Effective user settings: manifest defaults overlaid with whatever the
    shell last pushed. Reads go through attribute access so a typo is an
    AttributeError in tests rather than a silently-wrong default."""

    def __init__(self, defaults):
        self._defaults = dict(defaults)
        self._values = {}
        self.locale = ""

    def update(self, values, locale=None):
        """Replace the configured overlay. Returns True when anything changed."""
        clean = {}
        if isinstance(values, dict):
            for key, value in values.items():
                if key == "id":
                    continue
                if key not in self._defaults:
                    # Unknown keys are tolerated (an older daemon under a newer
                    # plugin) but never trusted for anything.
                    continue
                clean[key] = _coerce(value, self._defaults[key])
        changed = clean != self._values
        self._values = clean
        if locale is not None and str(locale) != self.locale:
            self.locale = str(locale)
            changed = True
        return changed

    def get(self, key):
        if key in self._values:
            return self._values[key]
        return self._defaults[key]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self.get(name)
        except KeyError:
            raise AttributeError(name) from None

    def as_dict(self):
        merged = dict(self._defaults)
        merged.update(self._values)
        return merged

    @property
    def download_dir(self):
        return os.path.expanduser(str(self.get("downloadDir") or "~/Music/Podcasts"))

    @property
    def country(self):
        code = str(self.get("searchCountry") or "auto").strip()
        if code and code.lower() != "auto":
            return code.upper()   # validated where it is used, so a typo can be named
        return country_from_locale(self.locale or os.environ.get("LANG", ""))

    @property
    def language(self):
        return language_from_locale(self.locale or os.environ.get("LANG", ""))

    @property
    def sync_device_id(self):
        configured = str(self.get("syncDeviceId") or "").strip()
        if configured:
            return configured
        return "omarchy-" + (socket.gethostname().split(".")[0] or "desktop")


def _coerce(value, default):
    """Pull a pushed setting into the type of its default. shell.json is JSON,
    but the bar's `set` command and hand edits both produce strings."""
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if value is None:
        return default
    return str(value)


def country_from_locale(locale_string):
    """`zh_CN.UTF-8` -> `CN`, `en` -> `US`. Apple's catalogue is per-country
    and answers nothing for an unknown code, so the fallback is a real one."""
    value = str(locale_string or "").split(".")[0].split("@")[0].replace("-", "_")
    parts = value.split("_")
    if len(parts) >= 2 and len(parts[1]) == 2 and parts[1].isalpha():
        return parts[1].upper()
    return "US"


def language_from_locale(locale_string):
    value = str(locale_string or "").split(".")[0].split("@")[0].replace("-", "_")
    lang = value.split("_")[0].lower()
    if len(lang) in (2, 3) and lang.isalpha() and lang not in ("c", "posix"):
        return lang
    return "en"


def load_credentials(paths):
    try:
        with open(paths.credentials_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_credentials(paths, data):
    """Atomic 0600 write: an unpredictable temp beside the file, then rename."""
    fsio.atomic_write(paths.credentials_path, json.dumps(data, indent=2, sort_keys=True) + "\n")


__all__ = [
    "PLUGIN_ID", "Paths", "Settings", "manifest_defaults", "manifest_version",
    "load_manifest", "load_credentials", "save_credentials",
    "country_from_locale", "language_from_locale",
]

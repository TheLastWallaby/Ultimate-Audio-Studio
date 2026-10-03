"""Application configuration, global constants, paths, and shared utilities."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from app.core.config import Settings, clear_settings_cache, get_settings
from app.core.file_utils import atomic_save_json, replace_with_retry, sanitize_filename
from app.core.process_utils import find_system_binary, is_valid_binary, run_ffmpeg
from app.core.time_utils import format_time
from app.models import AppSettings
from app.platform_utils import CREATE_NO_WINDOW

__all__ = [
    "APP_VERSION",
    "AUDIO_EXTS",
    "BASE_PATH",
    "COVER_CACHE_DIR",
    "CREATE_NO_WINDOW",
    "DEFAULT_PLAYLIST_NAME",
    "ERROR_LOG_PATH",
    "EXE_EXT",
    "GITHUB_OWNER",
    "GITHUB_REPO",
    "MUSIC_DIR",
    "PLAYLISTS_PATH",
    "PREVIEW_CACHE_DIR",
    "RELEASES_API_URL",
    "SETTINGS_PATH",
    "Settings",
    "SettingsManager",
    "YT_CACHE_DIR",
    "YOUTUBE_RE",
    "atomic_save_json",
    "cleanup_old_executables",
    "cleanup_temp_caches",
    "clear_settings_cache",
    "ffmpeg_path",
    "ffprobe_path",
    "find_system_binary",
    "format_time",
    "get_settings",
    "get_update_token",
    "is_valid_binary",
    "log_error",
    "run_ffmpeg",
    "sanitize_filename",
    "save_update_token",
    "settings_mgr",
    "setup_logging",
]

logger = logging.getLogger(__name__)

# Frozen builds must use bundled CA certs or YouTube downloads fail SSL checks.
_app_settings = get_settings()
if _app_settings.ssl.ssl_cert_file:
    os.environ.setdefault("SSL_CERT_FILE", str(_app_settings.ssl.ssl_cert_file))
if _app_settings.ssl.requests_ca_bundle:
    os.environ.setdefault("REQUESTS_CA_BUNDLE", str(_app_settings.ssl.requests_ca_bundle))

if getattr(sys, "frozen", False):
    BASE_PATH = getattr(sys, "_MEIPASS", "")
else:
    BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if BASE_PATH and BASE_PATH not in os.environ.get("PATH", ""):
    os.environ["PATH"] = BASE_PATH + os.pathsep + os.environ.get("PATH", "")

EXE_EXT = ".exe" if os.name == "nt" else ""


def _locate_media_tool(name: str) -> str:
    """Path of a bundled FFmpeg tool, else a genuine system copy, else the (missing) bundled path.

    The packaged .exe always ships verified binaries (build_exe.ps1 rejects shims), so they are
    trusted without running ``<tool> -version``: probing both tools cost two subprocess launches
    on every start-up.
    """
    candidate = os.path.join(BASE_PATH, f"{name}{EXE_EXT}")
    if getattr(sys, "frozen", False) and os.path.isfile(candidate):
        return candidate
    if is_valid_binary(candidate):
        return candidate
    return find_system_binary(f"{name}{EXE_EXT}") or candidate


ffmpeg_path = _locate_media_tool("ffmpeg")
ffprobe_path = _locate_media_tool("ffprobe")

if ffmpeg_path and os.path.exists(ffmpeg_path):
    f_dir = os.path.dirname(os.path.abspath(ffmpeg_path))
    if f_dir and f_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f_dir + os.pathsep + os.environ.get("PATH", "")

MUSIC_DIR = str(_app_settings.paths.music_dir)
PLAYLISTS_PATH = str(_app_settings.paths.playlists_path)
SETTINGS_PATH = str(_app_settings.paths.settings_path)
ERROR_LOG_PATH = str(_app_settings.paths.error_log_path)
YT_CACHE_DIR = str(_app_settings.paths.yt_cache_dir)
COVER_CACHE_DIR = str(_app_settings.paths.cover_cache_dir)
PREVIEW_CACHE_DIR = str(_app_settings.paths.preview_cache_dir)
DEFAULT_PLAYLIST_NAME = "My Playlist"
# Everything FFmpeg can decode that people commonly have. Windows Media Player rips CDs to WMA by
# default; formats pygame cannot play directly are converted to WAV before playback (AudioEngine).
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".wma", ".aac", ".opus", ".aiff", ".aif")
YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)/", re.I)

# Application version and update configuration
APP_VERSION = _app_settings.app.app_version
GITHUB_OWNER = _app_settings.github.owner
GITHUB_REPO = _app_settings.github.repo
RELEASES_API_URL = str(_app_settings.github.releases_api_url)


# A locked settings file is tried this many times before the app starts without it.
_SETTINGS_READ_ATTEMPTS = 3


class SettingsManager:
    """Thread-safe store of the user's settings, kept in a JSON file.

    A settings file that cannot be read is never written over. A damaged one is set aside as
    ``<name>.damaged-<time>.json`` (``damaged_copy``, for the window to mention), so a chosen
    Library folder can still be recovered from it. One that cannot be opened at all (locked by
    another program, no permission) is left alone: settings are not saved for the rest of the
    session (``read_failed``).
    """

    def __init__(self, settings_path: str | Path = SETTINGS_PATH) -> None:
        self.settings_path = settings_path
        self._lock = threading.Lock()
        self._cached_settings: AppSettings | None = None
        self.read_failed = False
        self.damaged_copy: Path | None = None

    def get_settings(self) -> AppSettings:
        """The settings, read from disk once; the defaults when the file is missing or unreadable."""
        with self._lock:
            if self._cached_settings is None:
                self._cached_settings = self._load_from_disk()
            return self._cached_settings

    def reload(self) -> AppSettings:
        """Read the settings file again."""
        with self._lock:
            self._cached_settings = self._load_from_disk()
            return self._cached_settings

    def _read_bytes(self, path: Path) -> bytes | None:
        """The file's contents, or None when it does not exist; retries while it is briefly locked."""
        for attempt in range(_SETTINGS_READ_ATTEMPTS):
            try:
                return path.read_bytes()
            except FileNotFoundError:
                return None
            except PermissionError:
                # OneDrive and antivirus scanners hold a file for a moment after it changes.
                if attempt == _SETTINGS_READ_ATTEMPTS - 1:
                    raise
                time.sleep(0.1 * (attempt + 1))
        return None

    def _load_from_disk(self) -> AppSettings:
        """Read the settings file; never raises, and never leaves a damaged file where a save would replace it."""
        path = Path(self.settings_path)
        self.read_failed = False
        try:
            raw = self._read_bytes(path)
        except OSError as err:
            logger.warning("The settings file %s could not be opened: %s", path, err)
            self.read_failed = True
            return AppSettings()
        if raw is None:
            return AppSettings()
        try:
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("the file does not hold a set of settings")
            return AppSettings.from_dict(data)
        except (ValueError, TypeError) as err:  # bad UTF-8 and bad JSON are ValueErrors too
            logger.error("The settings file %s is damaged: %s", path, err)
        kept = path.with_name(f"{path.stem}.damaged-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
        try:
            replace_with_retry(path, kept)
        except OSError as err:
            logger.error("Could not set the damaged settings file %s aside: %s", path, err)
            self.read_failed = True
            return AppSettings()
        self.damaged_copy = kept
        return AppSettings()

    def update_settings(self, **kwargs: object) -> AppSettings:
        """Change some settings and save them all; raises OSError when the file must not be written."""
        with self._lock:
            if self._cached_settings is None:
                self._cached_settings = self._load_from_disk()
            if self.read_failed:
                # The window started from the defaults, so saving now would replace the user's real
                # settings (their Library folder above all) with them.
                raise OSError(f"{Path(self.settings_path).name} could not be read, so it is left as it is")
            current = self._cached_settings
            for k, v in kwargs.items():
                if hasattr(current, k) and k != "extra":
                    setattr(current, k, v)
                else:
                    current.extra[k] = v
            atomic_save_json(self.settings_path, current.to_dict())
            return current

    def get_token(self) -> str:
        settings = self.get_settings()
        if settings.github_update_token:
            return settings.github_update_token
        return ""

    def save_token(self, token: str) -> bool:
        tok = str(token or "").strip()
        try:
            self.update_settings(github_update_token=tok)
            return True
        except Exception as e:
            log_error(f"Failed to save update token: {e}")
            return False


settings_mgr = SettingsManager()


def get_update_token() -> str:
    """Retrieve optional GitHub token for updates (unneeded for public repository)."""
    return settings_mgr.get_token()


def save_update_token(token: str) -> bool:
    """Save an optional GitHub update token to the user's settings file."""
    return settings_mgr.save_token(token)


def cleanup_old_executables() -> bool:
    """Delete the previous version's executable that an update left next to the app; True when none is left.

    Right after an update the previous version is still running from that file (it waits to see
    the new one start), and Windows will not delete it until that process has ended: False then.
    """
    if not getattr(sys, "frozen", False):
        return True
    old_exe = Path(sys.executable + ".old")
    try:
        old_exe.unlink(missing_ok=True)
    except OSError as err:
        logger.debug("The previous version %s is still in use: %s", old_exe.name, err)
        return False
    return True


def cleanup_temp_caches() -> None:
    """Clean up temporary cover art and preview cache files."""
    temp_dir = os.path.realpath(tempfile.gettempdir())
    cwd = os.path.realpath(os.getcwd())
    for folder in (COVER_CACHE_DIR, PREVIEW_CACHE_DIR):
        try:
            real_folder = os.path.realpath(folder)
            if not folder or real_folder in ("", cwd, os.path.realpath(os.path.expanduser("~"))):
                continue
            if not real_folder.startswith(temp_dir):
                continue
            if os.path.exists(folder):
                shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass


_LOG_MAX_BYTES = 1024 * 1024
_LOG_BACKUP_COUNT = 3
_log_setup_lock = threading.Lock()
_log_handler_installed = False


def setup_logging() -> logging.Logger:
    """Attach a size-capped rotating file handler to the ``app`` logger namespace (idempotent)."""
    global _log_handler_installed
    app_logger = logging.getLogger("app")
    with _log_setup_lock:
        if _log_handler_installed:
            return app_logger
        _log_handler_installed = True
        try:
            Path(ERROR_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                ERROR_LOG_PATH,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            handler.setLevel(logging.INFO)
            app_logger.addHandler(handler)
            app_logger.setLevel(logging.INFO)
        except OSError:
            # Logging must never take the app down (e.g. read-only Music folder).
            pass
    return app_logger


def log_error(text: str) -> None:
    """Record an error in the rotating user error log (kept for existing call sites)."""
    setup_logging().error(text)

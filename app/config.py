"""Application configuration, global constants, paths, and shared utilities."""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from typing import Any

from app.core.config import Settings, clear_settings_cache, get_settings
from app.core.file_utils import atomic_save_json, sanitize_filename
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
]

# Bootstrap audioop for Python 3.13+ before importing pydub
try:
    import audioop
except ImportError:
    try:
        import audioop_lts as audioop  # type: ignore[no-redef]

        sys.modules["audioop"] = audioop
    except ImportError:
        pass

from pydub import AudioSegment  # noqa: F401

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


candidate_ffmpeg = os.path.join(BASE_PATH, f"ffmpeg{EXE_EXT}")
if is_valid_binary(candidate_ffmpeg):
    ffmpeg_path = candidate_ffmpeg
else:
    alt = find_system_binary(f"ffmpeg{EXE_EXT}")
    ffmpeg_path = alt if alt else candidate_ffmpeg

candidate_ffprobe = os.path.join(BASE_PATH, f"ffprobe{EXE_EXT}")
if is_valid_binary(candidate_ffprobe):
    ffprobe_path = candidate_ffprobe
else:
    alt = find_system_binary(f"ffprobe{EXE_EXT}")
    ffprobe_path = alt if alt else candidate_ffprobe

if ffmpeg_path and os.path.exists(ffmpeg_path):
    f_dir = os.path.dirname(os.path.abspath(ffmpeg_path))
    if f_dir and f_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f_dir + os.pathsep + os.environ.get("PATH", "")

AudioSegment.converter = ffmpeg_path
AudioSegment.ffprobe = ffprobe_path
try:
    import pydub.utils

    pydub.utils.get_prober_name = lambda: ffprobe_path
except Exception:
    pass

MUSIC_DIR = str(_app_settings.paths.music_dir)
PLAYLISTS_PATH = str(_app_settings.paths.playlists_path)
SETTINGS_PATH = str(_app_settings.paths.settings_path)
ERROR_LOG_PATH = str(_app_settings.paths.error_log_path)
YT_CACHE_DIR = str(_app_settings.paths.yt_cache_dir)
COVER_CACHE_DIR = str(_app_settings.paths.cover_cache_dir)
PREVIEW_CACHE_DIR = str(_app_settings.paths.preview_cache_dir)
DEFAULT_PLAYLIST_NAME = "My Playlist"
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".flac")
YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)/", re.I)

# Application version and update configuration
APP_VERSION = _app_settings.app.app_version
GITHUB_OWNER = _app_settings.github.owner
GITHUB_REPO = _app_settings.github.repo
RELEASES_API_URL = str(_app_settings.github.releases_api_url)


class SettingsManager:
    """Thread-safe centralized repository for application settings and update credentials."""

    def __init__(self, settings_path: str = SETTINGS_PATH):
        self.settings_path = settings_path
        self._lock = threading.Lock()
        self._cached_settings: AppSettings | None = None

    def get_settings(self) -> AppSettings:
        with self._lock:
            if self._cached_settings is None:
                self._cached_settings = self._load_from_disk()
            return self._cached_settings

    def reload(self) -> AppSettings:
        with self._lock:
            self._cached_settings = self._load_from_disk()
            return self._cached_settings

    def _load_from_disk(self) -> AppSettings:
        if os.path.exists(self.settings_path):
            try:
                with open(self.settings_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return AppSettings.from_dict(data)
            except Exception as e:
                log_error(f"SettingsManager load error: {e}")
        return AppSettings()

    def update_settings(self, **kwargs: Any) -> AppSettings:
        with self._lock:
            current = self._cached_settings or self._load_from_disk()
            for k, v in kwargs.items():
                if hasattr(current, k) and k != "extra":
                    setattr(current, k, v)
                else:
                    current.extra[k] = v
            self._cached_settings = current
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


def cleanup_old_executables() -> None:
    """Clean up leftover .old executable from previous in-place auto-update on startup."""
    if not getattr(sys, "frozen", False):
        return
    try:
        old_exe = sys.executable + ".old"
        if os.path.exists(old_exe):
            try:
                os.remove(old_exe)
            except Exception:
                pass
    except Exception:
        pass


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


def log_error(text: str) -> None:
    """Append error message to user error log file."""
    try:
        os.makedirs(MUSIC_DIR, exist_ok=True)
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except Exception:
        pass

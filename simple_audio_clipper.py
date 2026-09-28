"""Ultimate Audio Studio: Entry point and backward-compatible facade.

This script delegates directly to the modularized `app` package.
It preserves full compatibility with existing build scripts (build_exe.ps1)
and PyInstaller spec definitions (simple_audio_clipper.spec).
"""

import os
import sys

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Ensure subprocess silencing is active immediately
from app.config import (
    AUDIO_EXTS,
    COVER_CACHE_DIR,
    DEFAULT_PLAYLIST_NAME,
    ERROR_LOG_PATH,
    MUSIC_DIR,
    PLAYLISTS_PATH,
    PREVIEW_CACHE_DIR,
    SETTINGS_PATH,
    YT_CACHE_DIR,
    ffmpeg_path,
    ffprobe_path,
    format_time,
    log_error,
    sanitize_filename,
)
from app.core.metadata import extract_album_art, probe_audio_duration, read_track_metadata
from app.core.waveform import extract_waveform_peaks
from app.main import UltimateAudioStudio, main
from app.platform_utils import CREATE_NO_WINDOW, already_running, enable_windows_dpi
from app.ui.components import ToolTip, create_button
from app.ui.theme import (
    BG_CARD,
    BG_INPUT,
    BG_ROOT,
    BG_SUB_CARD,
    BORDER_MAIN,
    COLOR_ACCENT,
    COLOR_DOWNLOAD,
    COLOR_EXPORT,
    COLOR_PAUSE,
    COLOR_PLAY,
    COLOR_STOP,
    FONT_APP_TITLE,
    FONT_BODY,
    FONT_BODY_BOLD,
    FONT_STEP_BADGE,
)

__all__ = [
    "AUDIO_EXTS",
    "BG_CARD",
    "BG_INPUT",
    "BG_ROOT",
    "BG_SUB_CARD",
    "BORDER_MAIN",
    "COLOR_ACCENT",
    "COLOR_DOWNLOAD",
    "COLOR_EXPORT",
    "COLOR_PAUSE",
    "COLOR_PLAY",
    "COLOR_STOP",
    "COVER_CACHE_DIR",
    "CREATE_NO_WINDOW",
    "DEFAULT_PLAYLIST_NAME",
    "ERROR_LOG_PATH",
    "FONT_APP_TITLE",
    "FONT_BODY",
    "FONT_BODY_BOLD",
    "FONT_STEP_BADGE",
    "MUSIC_DIR",
    "PLAYLISTS_PATH",
    "PREVIEW_CACHE_DIR",
    "SETTINGS_PATH",
    "YT_CACHE_DIR",
    "ToolTip",
    "UltimateAudioStudio",
    "already_running",
    "create_button",
    "enable_windows_dpi",
    "extract_album_art",
    "extract_waveform_peaks",
    "ffmpeg_path",
    "ffprobe_path",
    "format_time",
    "log_error",
    "main",
    "probe_audio_duration",
    "read_track_metadata",
    "sanitize_filename",
]

if __name__ == "__main__":
    main()

"""Ultimate Audio Studio: Entry point and backward-compatible facade.

This script delegates directly to the modularized `app` package.
It preserves full compatibility with existing build scripts (build_exe.ps1)
and PyInstaller spec definitions (simple_audio_clipper.spec).
"""

import sys
import os

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Ensure subprocess silencing is active immediately
from app.platform_utils import CREATE_NO_WINDOW, already_running, enable_windows_dpi
from app.config import (
    ffmpeg_path, ffprobe_path, MUSIC_DIR, PLAYLISTS_PATH,
    SETTINGS_PATH, ERROR_LOG_PATH, YT_CACHE_DIR, COVER_CACHE_DIR,
    PREVIEW_CACHE_DIR, DEFAULT_PLAYLIST_NAME, AUDIO_EXTS,
    format_time, log_error, sanitize_filename
)
from app.core.metadata import probe_audio_duration, extract_album_art, read_track_metadata
from app.core.waveform import extract_waveform_peaks
from app.ui.theme import (
    FONT_APP_TITLE, FONT_STEP_BADGE, FONT_BODY, FONT_BODY_BOLD,
    BG_ROOT, BG_CARD, BG_SUB_CARD, BG_INPUT, BORDER_MAIN,
    COLOR_PLAY, COLOR_PAUSE, COLOR_STOP, COLOR_ACCENT,
    COLOR_DOWNLOAD, COLOR_EXPORT
)
from app.ui.components import ToolTip, create_button
from app.main import UltimateAudioStudio, main

if __name__ == "__main__":
    main()

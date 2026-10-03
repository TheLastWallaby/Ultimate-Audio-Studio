"""Temporary CI diagnostic: time the parts of opening and closing the main window.

Opening the window takes about 8 s per test on GitHub's Windows runner, against well under a second
on a developer PC. This prints how long the mixer, Tk and the window take there, and a profile of
the window's start-up, so the fix can target the real cause. Remove once that is fixed.
"""

from __future__ import annotations

import cProfile
import os
import pstats
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_home = Path(tempfile.mkdtemp(prefix="uas-profile-"))
_music = _home / "Music"
_music.mkdir()
# Same isolation as tests/conftest.py, set before anything imports ``app``.
os.environ.update(
    {
        "UAS_MUSIC_DIR": str(_music),
        "UAS_PLAYLISTS_PATH": str(_music / "audio_studio_playlists.json"),
        "UAS_SETTINGS_PATH": str(_music / "audio_studio_settings.json"),
        "UAS_ERROR_LOG_PATH": str(_music / "audio_studio_error.txt"),
        "UAS_YT_CACHE_DIR": str(_music / ".audio_studio_cache"),
    }
)
sys.path.insert(0, str(ROOT))


def _timed(label: str, start: float) -> None:
    print(f"{label}: {time.monotonic() - start:.2f} s", flush=True)


def main() -> None:
    """Print the timings and the profile."""
    start = time.monotonic()
    import pygame

    _timed("import pygame", start)

    for attempt in (1, 2):
        start = time.monotonic()
        try:
            pygame.mixer.pre_init(44100, -16, 2, 8192)
            pygame.mixer.init()
            outcome = f"ok {pygame.mixer.get_init()}"
        except pygame.error as err:
            outcome = f"failed: {err}"
        _timed(f"pygame.mixer.init #{attempt} ({outcome})", start)
        start = time.monotonic()
        pygame.quit()
        _timed(f"pygame.quit #{attempt}", start)

    import tkinter as tk

    from app.main import UltimateAudioStudio

    profile = cProfile.Profile()
    for attempt in (1, 2):
        start = time.monotonic()
        root = tk.Tk()
        root.withdraw()
        _timed(f"tk.Tk #{attempt}", start)
        start = time.monotonic()
        profile.enable()
        window = UltimateAudioStudio(root)
        profile.disable()
        _timed(f"UltimateAudioStudio(root) #{attempt}", start)
        start = time.monotonic()
        window.on_close()
        _timed(f"on_close #{attempt}", start)

    pstats.Stats(profile).sort_stats("cumulative").print_stats(25)


if __name__ == "__main__":
    main()

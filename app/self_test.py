"""Start-up self-test for the packaged executable (``Ultimate Audio Studio.exe --self-test``).

Releases reach every user automatically (including unattended yt-dlp releases), so CI runs the
*built* .exe with this flag before publishing. It exercises what source-level tests cannot see in a
frozen build: bundled FFmpeg/FFprobe/Deno, hidden imports, Tk, the audio pipeline, and SSL certs.
The exit code is 0 when every check passes; a report is written to ``--self-test-report <path>``.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import tkinter as tk
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

SELF_TEST_FLAG = "--self-test"
REPORT_FLAG = "--self-test-report"


def _check_media_tools() -> None:
    from app.config import ffmpeg_path, ffprobe_path, is_valid_binary

    for name, path in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)):
        if not is_valid_binary(path):
            raise RuntimeError(f"{name} is missing or not a working binary: {path}")


def _check_deno() -> None:
    from app.services.downloader import find_deno_runtime

    if not find_deno_runtime():
        raise RuntimeError("Deno JavaScript runtime (needed by yt-dlp for YouTube) was not found")


def _check_yt_dlp() -> None:
    import yt_dlp

    from app.services.downloader import _js_runtime_opts

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **_js_runtime_opts()}):
        pass


def _check_ssl_certs() -> None:
    import certifi

    if not Path(certifi.where()).is_file():
        raise RuntimeError(f"CA certificate bundle missing: {certifi.where()}")


def _check_audio_pipeline() -> None:
    """Encode a short tone with FFmpeg, then read its tags and waveform as the app would."""
    from app.config import run_ffmpeg
    from app.core.metadata import read_track_metadata
    from app.core.waveform import analyze_audio

    with tempfile.TemporaryDirectory(prefix="uas_selftest_") as td:
        mp3 = str(Path(td) / "tone.mp3")
        res = run_ffmpeg(["-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libmp3lame", mp3])
        if res.returncode != 0 or not Path(mp3).is_file():
            raise RuntimeError(f"FFmpeg could not encode MP3: {res.stderr[-400:]}")
        meta = read_track_metadata(mp3)
        if not 1.5 <= meta.duration <= 2.5:
            raise RuntimeError(f"unexpected duration for 2 s test tone: {meta.duration}")
        if not analyze_audio(mp3, n_bars=20).peaks:
            raise RuntimeError("waveform analysis returned no peaks")


def _check_tk_and_mixer() -> None:
    import pygame

    from app.ui.theme import init_fonts

    root = tk.Tk()
    try:
        root.withdraw()
        init_fonts(root)
    finally:
        root.destroy()
    # CI machines have no sound card; the dummy driver still proves SDL_mixer loads.
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    pygame.mixer.init()
    pygame.mixer.quit()


CHECKS: tuple[tuple[str, Callable[[], None]], ...] = (
    ("FFmpeg and FFprobe", _check_media_tools),
    ("Deno runtime", _check_deno),
    ("yt-dlp", _check_yt_dlp),
    ("SSL certificates", _check_ssl_certs),
    ("Audio pipeline", _check_audio_pipeline),
    ("Tk and audio mixer", _check_tk_and_mixer),
)


def run_self_test() -> list[str]:
    """Run every check; returns one line per check (prefixed PASS/FAIL)."""
    lines = []
    for name, check in CHECKS:
        try:
            check()
            lines.append(f"PASS {name}")
        except Exception as e:
            lines.append(f"FAIL {name}: {type(e).__name__}: {e}")
    return lines


def report_path(argv: list[str]) -> Path | None:
    """Path given after ``--self-test-report``, if any."""
    if REPORT_FLAG in argv:
        idx = argv.index(REPORT_FLAG)
        if idx + 1 < len(argv):
            return Path(argv[idx + 1])
    return None


def main(argv: list[str] | None = None) -> int:
    """Run the self-test, write the report, and return the process exit code."""
    args = sys.argv if argv is None else argv
    lines = run_self_test()
    failed = [line for line in lines if line.startswith("FAIL")]
    lines.append(f"RESULT {'FAIL' if failed else 'PASS'} ({len(lines) - len(failed)}/{len(lines)} checks passed)")
    text = "\n".join(lines) + "\n"
    target = report_path(args)
    if target is not None:
        target.write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        sys.stdout.write(text)
    for line in failed:
        logger.error("self-test %s", line)
    return 1 if failed else 0

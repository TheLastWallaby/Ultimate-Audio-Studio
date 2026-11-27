"""Writing a song's title tag, so the name the user gives a song is the name shown for it everywhere.

The Library list, the player and car stereos all show the title stored inside the song, not its
file name. Renaming only the file therefore changed nothing the user could see.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from app.config import run_ffmpeg
from app.core.file_utils import fsync_file, replace_with_retry

# The copy is written under a name without an audio extension (so the Library never lists it), and
# FFmpeg picks its output format from the extension: each format is named here instead.
_MUXERS = {
    ".mp3": "mp3",
    ".m4a": "ipod",
    ".flac": "flac",
    ".ogg": "ogg",
    ".opus": "opus",
    ".wav": "wav",
    ".wma": "asf",
    ".aiff": "aiff",
    ".aif": "aiff",
}
# Only the tags change, and the sound makes up nearly all of a song file: a copy much smaller than the
# song has lost some of it. (Durations are not compared: for an MP3 without a length header they are
# only estimated, and the copy gains such a header.)
_MIN_SIZE_RATIO = 0.9
# Copying streams reads the file once at disk speed: even a long song on a slow USB stick is quick.
_TIMEOUT_SEC = 120


class TitleWriteError(Exception):
    """The title could not be written. The song itself is unchanged."""


def can_write_title(song: str | Path) -> bool:
    """True for the formats whose title tag this module can write."""
    return Path(song).suffix.lower() in _MUXERS


def write_title(song: str | Path, title: str) -> None:
    """Store ``title`` as the title tag of ``song`` without touching its sound (worker threads only).

    FFmpeg copies the audio and cover picture unchanged into a new file next to the song, with the
    new title. The copy replaces the song only after it is checked: not empty, and about as large
    as the song. Until then the song is untouched, so any failure leaves it exactly as it was. Raises
    ``TitleWriteError``.
    """
    song = Path(song)
    muxer = _MUXERS.get(song.suffix.lower())
    if muxer is None:
        raise TitleWriteError(f"Titles cannot be written into {song.suffix} files")
    # A ".partial" name of this process: a crash leaves it to remove_stale_work_files.
    work = song.with_name(f"{song.name}.{os.getpid()}.partial")
    args = ["-y", "-i", str(song), "-map", "0:a", "-map", "0:v?", "-c", "copy", "-map_metadata", "0"]
    if muxer == "mp3":
        args += ["-id3v2_version", "3"]
    args += ["-metadata", f"title={title}", "-f", muxer, str(work)]
    try:
        result = run_ffmpeg(args, timeout=_TIMEOUT_SEC)
        if result.returncode != 0:
            raise TitleWriteError(f"FFmpeg could not copy {song.name}: {str(result.stderr)[-300:]}")
        _check_copy(song, work)
        fsync_file(work)
        replace_with_retry(work, song)
    except OSError as err:
        raise TitleWriteError(f"{song.name} could not be updated: {err}") from err
    finally:
        with contextlib.suppress(OSError):  # gone after a successful swap
            work.unlink()


def _check_copy(song: Path, copy: Path) -> None:
    """Raise ``TitleWriteError`` unless ``copy`` holds all of ``song``'s sound."""
    copy_size = copy.stat().st_size if copy.is_file() else 0
    if copy_size == 0 or copy_size < song.stat().st_size * _MIN_SIZE_RATIO:
        raise TitleWriteError(f"The new copy of {song.name} came out too small ({copy_size} bytes)")

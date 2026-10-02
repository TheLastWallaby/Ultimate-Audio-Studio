"""Audio clipping, volume boosting, fading, and audition slice generation."""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from pydub import AudioSegment

from app.config import PREVIEW_CACHE_DIR, ffmpeg_path, log_error, run_ffmpeg
from app.core.file_utils import copy_file_atomic
from app.core.file_utils import replace_with_retry as _replace_with_retry
from app.core.process_utils import ffmpeg_timed_out
from app.services.ffmpeg_args import MP3_VBR_QUALITY, clip_filter_chain, fade_duration, mp3_output_args

try:
    from send2trash import send2trash as _send2trash
except ImportError:  # pragma: no cover - send2trash is a hard dependency; kept for source runs without it
    _send2trash = None

__all__ = [
    "MP3_VBR_QUALITY",
    "ORIGINAL_BACKUP_SUFFIX",
    "clip_audio_worker",
    "create_audition_slice",
    "has_original_backup",
    "original_backup_path",
    "restore_original",
]

logger = logging.getLogger(__name__)

ORIGINAL_BACKUP_SUFFIX = ".original.bak"
_CLIP_TIMEOUT_MIN_SEC = 120.0
_CLIP_TIMEOUT_MAX_SEC = 1800.0
# Largest song the in-memory fallback is tried on (pydub holds the decoded audio in RAM).
_FALLBACK_MAX_BYTES = 50 * 1024 * 1024


def original_backup_path(song_path: str) -> str:
    """Where the untrimmed song is kept after a clip was saved over it."""
    return song_path + ORIGINAL_BACKUP_SUFFIX


def has_original_backup(song_path: str | None) -> bool:
    """True when a clip replaced this song and its original can still be restored."""
    return bool(song_path) and os.path.isfile(original_backup_path(str(song_path)))


def _ensure_original_backup(song: Path) -> Path:
    """Keep a complete copy of the untrimmed song before a clip replaces it; returns the backup.

    The first original is kept across re-trims, and an interrupted copy is never mistaken for a
    backup (``copy_file_atomic``). Raises OSError when no complete backup exists afterwards; the
    caller must then leave the song untouched.
    """
    backup = Path(original_backup_path(str(song)))
    if backup.is_file() and backup.stat().st_size > 0:
        return backup
    try:
        copy_file_atomic(song, backup)
    except OSError as err:
        raise OSError(f"The original song could not be backed up, so it was left unchanged ({err})") from err
    return backup


def restore_original(song_path: str) -> None:
    """Put the untrimmed original back in place of the trimmed song.

    The trimmed version goes to the Recycle Bin (under its own name, so it can be recovered).
    Raises OSError when the backup is missing or a file is locked.
    """
    backup = original_backup_path(song_path)
    if not os.path.isfile(backup):
        raise FileNotFoundError(f"No original backup found for {os.path.basename(song_path)}")
    if os.path.exists(song_path) and _send2trash is not None:
        _send2trash(song_path)
    _replace_with_retry(backup, song_path)


def create_audition_slice(
    filepath: str,
    s_time: float,
    e_time: float,
    gain_db: float = 0.0,
    soften: bool = False,
    fade_sec: float = 1.5,
) -> str | None:
    """Generate a temporary rendered preview slice with volume boost and fade applied for 'Test Clip'."""
    if not filepath or not os.path.exists(filepath) or not os.path.exists(ffmpeg_path):
        return None
    try:
        os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
        clip_dur = max(0.05, e_time - s_time)
        slice_file = os.path.join(PREVIEW_CACHE_DIR, f"audition_{os.getpid()}_{int(time.time() * 1000)}.wav")
        args = [
            "-y",
            "-accurate_seek",
            "-ss",
            f"{s_time:.3f}",
            "-i",
            filepath,
            "-t",
            f"{clip_dur:.3f}",
            "-af",
            clip_filter_chain(clip_dur, gain_db, soften, fade_sec),
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            slice_file,
        ]
        res = run_ffmpeg(args, timeout=10)
        if res.returncode == 0 and os.path.exists(slice_file) and os.path.getsize(slice_file) > 0:
            return slice_file
    except Exception as ex:
        log_error(f"create_audition_slice failed: {ex}")
    return None


def _clip_args(filepath: str, s_time: float, dur: float, audio_filter: str, wav: bool, cover: str) -> list[str]:
    if wav:
        args = ["-y", "-accurate_seek", "-ss", f"{s_time:.3f}", "-i", filepath, "-t", f"{dur:.3f}"]
        return args + ["-af", audio_filter, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le"]
    # Place -ss after -i for MP3 so stream 0:v (attached cover art at t=0) is preserved
    args = ["-y", "-i", filepath, "-ss", f"{s_time:.3f}", "-t", f"{dur:.3f}", "-af", audio_filter]
    return args + mp3_output_args("copy" if cover == "copy" else "mjpeg")


def _has_content(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _clip_timeout_sec(clip_end_sec: float) -> int:
    """Seconds FFmpeg may take to cut a clip that ends ``clip_end_sec`` into the song.

    The MP3 path decodes the song from its start up to the end of the clip. Even a slow PC does that
    several times faster than the music plays, so "as long as it would take to listen to it, plus a
    minute" never stops a healthy run, however long the clip is.
    """
    return int(min(_CLIP_TIMEOUT_MAX_SEC, max(_CLIP_TIMEOUT_MIN_SEC, 60.0 + clip_end_sec)))


def clip_audio_worker(
    filepath: str | Path,
    s_time: float,
    e_time: float,
    save_name: str | Path,
    soften: bool = False,
    gain_db: float = 0.0,
    is_self_overwrite: bool = False,
    on_success: Callable[[str, str, bool], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    fade_sec: float = 1.5,
) -> None:
    """Export sample-accurate clipped audio file with backup protection and Windows lock retries.

    When the clip replaces the song it was cut from, the song is only replaced once a complete
    backup of the original exists; otherwise the save fails and the song is left as it was.
    """
    source = str(filepath)
    target = Path(save_name)
    save_dir = target.absolute().parent
    save_dir.mkdir(parents=True, exist_ok=True)
    wav = target.suffix.lower() == ".wav"
    tmp_ext = ".wav" if wav else ".mp3"
    tmp_save = save_dir / f".clip_tmp_{os.getpid()}_{int(time.time() * 1000)}{tmp_ext}"

    try:
        dur = max(0.01, e_time - s_time)
        audio_filter = clip_filter_chain(dur, gain_db, soften, fade_sec)
        timeout = _clip_timeout_sec(e_time)

        result = run_ffmpeg(
            _clip_args(source, s_time, dur, audio_filter, wav, "copy") + [str(tmp_save)], timeout=timeout
        )
        if result.returncode != 0 and not wav and not ffmpeg_timed_out(result):
            # Retry transcoding video stream to mjpeg in case source art was PNG
            with contextlib.suppress(OSError):
                tmp_save.unlink()
            result = run_ffmpeg(
                _clip_args(source, s_time, dur, audio_filter, wav, "mjpeg") + [str(tmp_save)], timeout=timeout
            )
        if result.returncode != 0 or not _has_content(tmp_save):
            with contextlib.suppress(OSError):
                tmp_save.unlink()
            # The fallback decodes the whole song into memory, so it is kept for what it can help
            # with: a normal-sized song that FFmpeg rejected. A run that timed out would only take
            # even longer this way, and a long recording would not fit in memory.
            if ffmpeg_timed_out(result):
                raise RuntimeError(str(result.stderr))
            if Path(source).stat().st_size > _FALLBACK_MAX_BYTES:
                raise RuntimeError(f"FFmpeg could not clip {Path(source).name}: {str(result.stderr)[-300:]}")
            audio = AudioSegment.from_file(source)
            clipped = audio[s_time * 1000 : e_time * 1000]
            if abs(gain_db) > 0.05:
                clipped = clipped + gain_db
            fade_dur = fade_duration(dur, soften, fade_sec)
            if soften and fade_dur > 0.01:
                fade_ms = int(fade_dur * 1000)
                clipped = clipped.fade_in(fade_ms).fade_out(fade_ms)
            if wav:
                clipped.export(str(tmp_save), format="wav")
            else:
                clipped.export(str(tmp_save), format="mp3", parameters=["-q:a", MP3_VBR_QUALITY])

        if not _has_content(tmp_save):
            raise RuntimeError("Audio clipping produced an empty file.")

        if is_self_overwrite:
            time.sleep(0.05)
            # The original is the one thing that cannot be made again: no complete backup, no replace.
            _ensure_original_backup(target)

        # Resilient file replace against Windows file indexing / antivirus locks
        _replace_with_retry(str(tmp_save), str(target))

        if on_success:
            on_success(target.name, str(save_name), is_self_overwrite)
    except Exception as e:  # last-resort guard: a worker must always report back to the window
        with contextlib.suppress(OSError):
            tmp_save.unlink()
        logger.error("clip_audio_worker: %s", e)
        if on_error:
            on_error(str(e))

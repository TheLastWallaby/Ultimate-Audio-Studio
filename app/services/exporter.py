"""Export services for USB flash drives (with loudness normalization and M3U playlists) and CD burning."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

from pydub import AudioSegment

from app.config import AUDIO_EXTS, log_error, run_ffmpeg, sanitize_filename
from app.core.cache_manager import cache_mgr
from app.core.process_utils import CancelToken
from app.services.ffmpeg_args import mp3_output_args

logger = logging.getLogger(__name__)

# EBU R128-style targets used for "Make all songs equally loud".
LOUDNORM_I = -16.0
LOUDNORM_TP = -1.5
LOUDNORM_LRA = 11.0
_LOUDNORM_BASE = f"loudnorm=I={LOUDNORM_I:g}:TP={LOUDNORM_TP:g}:LRA={LOUDNORM_LRA:g}"
_LOUDNORM_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
# Tracks already within this many LU of the target (and with safe peaks) are copied, not re-encoded.
_ALREADY_LEVEL_TOLERANCE_LU = 1.0
# Encoding a long track with loudnorm can take minutes on slow PCs.
_ENCODE_TIMEOUT_SEC = 900

# Files this app writes into an export folder: "07 - Song.mp3" / "107 - Song.mp3" and "00_Playlist.m3u(8)".
_EXPORTED_TRACK_RE = re.compile(r"^\d{2,3} - .+$")
_EXPORTED_PLAYLIST_RE = re.compile(r"^00_.+\.m3u8?$", re.IGNORECASE)

ProgressFn = Callable[[float], None]
StatusFn = Callable[[str], None]
StopFn = Callable[[], bool]


def track_number_width(total: int) -> int:
    """Digits used to number exported files, so players that sort by name keep playlist order.

    With 100+ songs, two digits would sort "100 - ..." before "11 - ...".
    """
    return 3 if total >= 100 else 2


def find_previous_export(folder: str) -> list[str]:
    """Files an earlier export of this app left in ``folder`` (numbered songs and its M3U lists).

    Anything else the user put there is never touched.
    """
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = []
    for name in names:
        full = os.path.join(folder, name)
        if not os.path.isfile(full):
            continue
        is_track = _EXPORTED_TRACK_RE.match(name) and name.lower().endswith(AUDIO_EXTS)
        if is_track or _EXPORTED_PLAYLIST_RE.match(name):
            found.append(full)
    return sorted(found)


def remove_previous_export(folder: str) -> list[str]:
    """Delete an earlier export's files from ``folder``; returns the names that could not be removed.

    USB drives have no Recycle Bin, and these are copies of songs that stay in the Library.
    """
    failed = []
    for path in find_previous_export(folder):
        try:
            os.remove(path)
        except OSError as e:
            log_error(f"remove_previous_export {path}: {e}")
            failed.append(os.path.basename(path))
    return failed


def measure_loudnorm(filepath: str, cancel_event: CancelToken | None = None) -> dict[str, str] | None:
    """Run (or reuse the cached result of) FFmpeg's loudnorm analysis pass for two-pass normalization."""
    cached = cache_mgr.get_loudnorm_stats(filepath)
    if cached and all(k in cached for k in _LOUDNORM_KEYS):
        return cached
    res = run_ffmpeg(
        ["-hide_banner", "-i", filepath, "-vn", "-af", f"{_LOUDNORM_BASE}:print_format=json", "-f", "null", "-"],
        timeout=_ENCODE_TIMEOUT_SEC,
        cancel_event=cancel_event,
    )
    if res.returncode != 0:
        return None
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", res.stderr or "")
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
        stats = {k: str(raw[k]) for k in _LOUDNORM_KEYS}
        # Silent or undecodable input reports -inf; those values cannot drive a linear second pass.
        if not all(math.isfinite(float(v)) for v in stats.values()):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    cache_mgr.set_loudnorm_stats(filepath, stats)
    return stats


def loudnorm_filter(stats: dict[str, str] | None) -> str:
    """Build the loudnorm filter: linear two-pass when measurements exist, else dynamic single-pass."""
    if not stats:
        return _LOUDNORM_BASE
    return (
        f"{_LOUDNORM_BASE}:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
        f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
        f":offset={stats['target_offset']}:linear=true:print_format=none"
    )


def is_already_level(stats: dict[str, str] | None) -> bool:
    """True when a track already sits at the target loudness with peak headroom, so it can be copied."""
    if not stats:
        return False
    try:
        return (
            abs(float(stats["input_i"]) - LOUDNORM_I) <= _ALREADY_LEVEL_TOLERANCE_LU
            and float(stats["input_tp"]) <= LOUDNORM_TP + 0.5
        )
    except (KeyError, ValueError):
        return False


def _fsync_file(path: str) -> None:
    """Write a file's cached data through to the drive (works without administrator rights).

    Flushing the whole volume needs admin rights, so it silently did nothing for normal accounts.
    """
    try:
        with open(path, "rb+") as f:
            os.fsync(f.fileno())
    except OSError as e:
        log_error(f"fsync {path}: {e}")


def _encode_mp3(
    filepath: str, dest_file: str, stats: dict[str, str] | None, normalize: bool, cancel_event: CancelToken | None
) -> bool:
    """Encode to MP3 with FFmpeg, retrying with a re-encoded cover picture; True when a file was made."""
    for cover in ("copy", "mjpeg"):
        args = ["-y", "-i", filepath]
        if normalize:
            args += ["-af", loudnorm_filter(stats)]
        args += mp3_output_args("copy" if cover == "copy" else "mjpeg", resample_44k=True) + [dest_file]
        result = run_ffmpeg(args, timeout=_ENCODE_TIMEOUT_SEC, cancel_event=cancel_event)
        if result.returncode == 0 and os.path.exists(dest_file) and os.path.getsize(dest_file) > 0:
            return True
        with contextlib.suppress(OSError):
            os.remove(dest_file)
        if cancel_event is not None and cancel_event.is_set():
            return False
    return False


def _write_m3u(dest_folder: str, playlist_name: str, tracks: list[tuple[str, str, float]]) -> None:
    """M3U (UTF-8 with BOM, for car stereos) and M3U8 playlists listing the exported files in order."""
    try:
        pl_clean = sanitize_filename(playlist_name or "Playlist")
        for ext_name, enc in ((".m3u", "utf-8-sig"), (".m3u8", "utf-8")):
            m3u_path = os.path.join(dest_folder, f"00_{pl_clean}{ext_name}")
            with open(m3u_path, "w", encoding=enc) as f:
                f.write("#EXTM3U\n")
                for fname, title, dur in tracks:
                    d_int = int(dur) if dur > 0 else -1
                    f.write(f"#EXTINF:{d_int},{title}\n")
                    f.write(f"{fname}\n")
                f.flush()
                os.fsync(f.fileno())
    except Exception as m3u_err:
        log_error(f"failed writing m3u: {m3u_err}")


def usb_export_worker(
    dest_folder: str,
    playlist_name: str,
    files_to_export: Sequence[str],
    normalize: bool = False,
    on_progress: ProgressFn | None = None,
    on_status: StatusFn | None = None,
    on_success: Callable[[int, int, list[str]], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    is_shutting_down_fn: StopFn | None = None,
    duration_fn: Callable[[str], float] | None = None,
    cancel_event: CancelToken | None = None,
    on_cancelled: Callable[[int, int], None] | None = None,
    clear_existing: bool = False,
) -> None:
    """Export playlist tracks to USB with numbering, FAT32-safe filenames, optional loudnorm, and M3U playlist file.

    ``clear_existing`` first removes the files of an earlier export, so songs that were removed or
    reordered since do not linger on the drive; an export stopped before it began removes nothing.
    When ``cancel_event`` is set, the current FFmpeg run is stopped, its half-written file removed,
    and ``on_cancelled(done, total)`` is called.
    """
    total = len(files_to_export)
    width = track_number_width(total)
    dest_dir = Path(dest_folder)
    success_count = 0
    skipped: list[str] = []
    exported_tracks: list[tuple[str, str, float]] = []

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def _stopping() -> bool:
        return bool(is_shutting_down_fn and is_shutting_down_fn()) or _cancelled()

    try:
        # Checked first: a job stopped while it was still queued must leave the drive as it was.
        if clear_existing and not _stopping():
            if on_status:
                on_status("Removing the songs from the previous export...")
            for name in remove_previous_export(dest_folder):
                skipped.append(f"{name} (old file could not be removed)")

        for idx, filepath in enumerate(files_to_export, 1):
            if _stopping():
                break
            if on_progress:
                on_progress(((idx - 1) / total) * 100)

            source = Path(filepath) if filepath else None
            if source is None or not source.exists():
                skipped.append((source.name if source else f"Track {idx}") + " (file not found)")
                continue

            clean_base = sanitize_filename(source.name)
            clean_stem = sanitize_filename(source.stem)
            is_mp3 = source.suffix.lower() == ".mp3"
            track_normalize = normalize

            try:
                stats = None
                if track_normalize:
                    if on_status:
                        on_status(f"Measuring loudness of USB track {idx} of {total}...")
                    stats = measure_loudnorm(filepath, cancel_event=cancel_event)
                    if _stopping():
                        break
                    if is_mp3 and is_already_level(stats):
                        track_normalize = False  # already at target: a straight copy is faster and lossless

                # Car stereos require standard MP3. If not already MP3 or if normalize is enabled, transcode.
                if track_normalize or not is_mp3:
                    dest_file = dest_dir / f"{idx:0{width}d} - {clean_stem}.mp3"
                    action_desc = "Normalizing & converting" if track_normalize else "Converting to MP3 for"
                    if on_status:
                        on_status(f"{action_desc} USB track {idx} of {total}...")
                    encoded = _encode_mp3(filepath, str(dest_file), stats, track_normalize, cancel_event)
                    if _stopping():
                        with contextlib.suppress(OSError):
                            dest_file.unlink()
                        break
                    if not encoded:
                        if is_mp3:
                            shutil.copy2(source, dest_file)
                        else:
                            audio = AudioSegment.from_file(filepath)
                            if track_normalize:
                                with contextlib.suppress(Exception):
                                    from pydub.effects import normalize as pydub_norm

                                    audio = pydub_norm(audio)
                            audio.export(str(dest_file), format="mp3")
                else:
                    dest_file = dest_dir / f"{idx:0{width}d} - {clean_base}"
                    if on_status:
                        on_status(f"Copying USB track {idx} of {total}...")
                    shutil.copy2(source, dest_file)

                _fsync_file(str(dest_file))
                success_count += 1
                dur = duration_fn(filepath) if duration_fn else 0.0
                track_title = clean_stem if (track_normalize or not is_mp3) else clean_base
                exported_tracks.append((dest_file.name, track_title, dur))
            except Exception as track_err:  # one bad song must not end the whole export
                logger.error("usb export track %s: %s", filepath, track_err)
                skipped.append(f"{source.name} ({track_err})")

        shutting_down = bool(is_shutting_down_fn and is_shutting_down_fn())
        # Even after Stop, list the songs that were copied so the drive plays them in order.
        if exported_tracks and not shutting_down:
            _write_m3u(dest_folder, playlist_name, exported_tracks)

        if _cancelled():
            if on_cancelled:
                on_cancelled(success_count, total)
            return
        if on_progress:
            on_progress(100.0)
        if on_success:
            on_success(success_count, total, skipped)
    except Exception as e:  # last-resort guard: a worker must always report back to the window
        logger.error("usb_export_worker: %s", e)
        if on_error:
            on_error(f"Could not complete USB export:\n{e}")


def flush_usb_drive(dest_folder: str) -> None:
    """Write every file in ``dest_folder`` through to the drive before it is unplugged."""
    for entry in Path(dest_folder).iterdir() if os.path.isdir(dest_folder) else ():
        if entry.is_file():
            _fsync_file(str(entry))


def cd_export_worker(
    cd_folder: str,
    files_to_export: Sequence[str],
    normalize: bool = False,
    on_progress: ProgressFn | None = None,
    on_status: StatusFn | None = None,
    on_success: Callable[[str, int, int, list[str]], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    is_shutting_down_fn: StopFn | None = None,
    cancel_event: CancelToken | None = None,
    on_cancelled: Callable[[int, int], None] | None = None,
    clear_existing: bool = False,
) -> None:
    """Export tracks to Desktop burn folder as standard Red Book 44.1kHz 16-bit stereo PCM WAV files.

    ``clear_existing`` first removes the tracks of an earlier export (and nothing else in the
    folder); an export stopped before it began removes nothing.
    """
    total = len(files_to_export)
    width = track_number_width(total)
    cd_dir = Path(cd_folder)
    success_count = 0
    skipped: list[str] = []

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def _stopping() -> bool:
        return bool(is_shutting_down_fn and is_shutting_down_fn()) or _cancelled()

    try:
        # Checked first: a job stopped while it was still queued must leave the folder as it was.
        if clear_existing and not _stopping():
            if on_status:
                on_status("Removing the tracks from the previous CD export...")
            for name in remove_previous_export(cd_folder):
                skipped.append(f"{name} (old file could not be removed)")

        for idx, filepath in enumerate(files_to_export, 1):
            if _stopping():
                break
            if on_progress:
                on_progress(((idx - 1) / total) * 100)

            source = Path(filepath) if filepath else None
            if source is None or not source.exists():
                skipped.append((source.name if source else f"Track {idx}") + " (file not found)")
                continue

            if on_status:
                norm_str = " (normalizing volume)..." if normalize else "..."
                on_status(f"Preparing CD track {idx} of {total}{norm_str}")

            wav_path = cd_dir / f"{idx:0{width}d} - {sanitize_filename(source.stem)}.wav"

            try:
                args = ["-y", "-i", filepath]
                if normalize:
                    args += ["-af", loudnorm_filter(measure_loudnorm(filepath, cancel_event=cancel_event))]
                args += ["-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(wav_path)]
                result = run_ffmpeg(args, timeout=_ENCODE_TIMEOUT_SEC, cancel_event=cancel_event)
                if _stopping():
                    with contextlib.suppress(OSError):
                        wav_path.unlink()
                    break
                if result.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size == 0:
                    audio = AudioSegment.from_file(filepath)
                    if normalize:
                        with contextlib.suppress(Exception):
                            from pydub.effects import normalize as pydub_norm

                            audio = pydub_norm(audio)
                    audio = audio.set_frame_rate(44100).set_channels(2).set_sample_width(2)
                    audio.export(str(wav_path), format="wav")
                success_count += 1
            except Exception as track_err:  # one bad song must not end the whole export
                logger.error("cd export track %s: %s", filepath, track_err)
                skipped.append(f"{source.stem} ({track_err})")

        if _cancelled():
            if on_cancelled:
                on_cancelled(success_count, total)
            return
        if on_progress:
            on_progress(100.0)
        if on_success:
            on_success(cd_folder, success_count, total, skipped)
    except Exception as e:  # last-resort guard: a worker must always report back to the window
        logger.error("cd_export_worker: %s", e)
        if on_error:
            on_error(f"Could not prepare CD files:\n{e}")

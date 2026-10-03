"""Export services for USB flash drives (with loudness normalization and M3U playlists) and CD burning."""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import math
import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config import AUDIO_EXTS, log_error, run_ffmpeg, sanitize_filename
from app.core.cache_manager import cache_mgr
from app.core.errors import friendly_error
from app.core.file_utils import copy_file_atomic, replace_with_retry
from app.core.process_utils import CancelToken
from app.services.ffmpeg_args import mp3_audio_only_args, mp3_output_args

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
# A track still being written ("07 - Song.mp3.partial"); one is left behind when a drive is pulled.
_PARTIAL_SUFFIX = ".partial"
# With less free space than this, no song fits on the drive any more.
_MIN_FREE_BYTES = 1024 * 1024

# Why an export stopped before the end: its drive was removed, or it is full ("" when it did not stop).
DriveProblem = Literal["", "removed", "full"]
_EXPORTED_PLAYLIST_RE = re.compile(r"^00_.+\.m3u8?$", re.IGNORECASE)
# What FFmpeg prints when the disk it writes to fills up.
_NO_SPACE_RE = re.compile(r"no space left on device", re.IGNORECASE)

ProgressFn = Callable[[float], None]
StatusFn = Callable[[str], None]
StopFn = Callable[[], bool]


@dataclass(slots=True, frozen=True)
class ExportReport:
    """What an export did, so the window can report it truthfully."""

    total: int  # songs in the playlist
    exported: int  # songs now in the export folder
    skipped: tuple[str, ...] = ()  # not exported (or an old file not removed), each with its reason
    not_leveled: tuple[str, ...] = ()  # exported, but without the volume levelling that was asked for
    stopped: DriveProblem = ""  # set when the drive gave out, and the remaining songs were not tried


ReportFn = Callable[[ExportReport], None]


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
        is_track = _EXPORTED_TRACK_RE.match(name) and name.lower().endswith((*AUDIO_EXTS, _PARTIAL_SUFFIX))
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
    filepath: str | Path,
    dest_file: str | Path,
    stats: dict[str, str] | None,
    normalize: bool,
    cancel_event: CancelToken | None,
) -> bool:
    """Encode to MP3 with FFmpeg, retrying with a re-encoded cover picture; True when a file was made.

    FFmpeg writes under a ``.partial`` name that is renamed when complete, so a drive pulled out
    mid-song never leaves a cut-off track that a car stereo would play.
    """
    for cover in ("copy", "mjpeg"):
        args = ["-y", "-i", str(filepath)]
        if normalize:
            args += ["-af", loudnorm_filter(stats)]
        args += mp3_output_args("copy" if cover == "copy" else "mjpeg", resample_44k=True)
        if _ffmpeg_to(args, Path(dest_file), "mp3", cancel_event):
            return True
        if cancel_event is not None and cancel_event.is_set():
            return False
    return False


def _ffmpeg_to(args: list[str], dest_file: Path, fmt: str, cancel_event: CancelToken | None) -> bool:
    """Run FFmpeg with ``args`` into ``dest_file`` (format ``fmt``); True when a complete file is in place.

    FFmpeg writes under a ``.partial`` name that is renamed only after a clean finish, so a failed,
    stopped or timed-out run never leaves a cut-off track under the real name. Raises
    OSError(ENOSPC) when FFmpeg ran out of disk space: that is the drive's problem, not the song's,
    and trying the song again another way (or the next song) would only fail the same way.
    """
    partial = dest_file.with_name(dest_file.name + _PARTIAL_SUFFIX)
    # The format is named because ".partial" does not tell FFmpeg which one to write.
    result = run_ffmpeg([*args, "-f", fmt, str(partial)], timeout=_ENCODE_TIMEOUT_SEC, cancel_event=cancel_event)
    if result.returncode != 0 and _NO_SPACE_RE.search(str(result.stderr)):
        with contextlib.suppress(OSError):
            partial.unlink()
        raise OSError(errno.ENOSPC, f"No space left to write {dest_file.name}")
    try:
        if result.returncode == 0 and partial.stat().st_size > 0:
            replace_with_retry(partial, dest_file)
            return True
        logger.warning("FFmpeg could not make %s (code %s)", dest_file.name, result.returncode)
    except OSError as err:
        logger.warning("Track %s could not be put in place: %s", dest_file.name, err)
    with contextlib.suppress(OSError):  # a leftover is removed by the next export (find_previous_export)
        partial.unlink()
    return False


def _drive_problem(folder: Path, err: BaseException | None = None) -> DriveProblem:
    """Why nothing more can be written to ``folder``: its drive was removed or is full ("" if neither).

    Checked after a song fails, so one unplugged or full drive ends the export with one plain
    message instead of every remaining song being measured, encoded and failed in turn.
    """
    if isinstance(err, OSError) and err.errno == errno.ENOSPC:
        return "full"
    try:
        if not folder.is_dir():
            return "removed"
        free = shutil.disk_usage(folder).free
    except OSError:
        return "removed"
    return "full" if free < _MIN_FREE_BYTES else ""


class _NotConvertible(Exception):
    """FFmpeg could not turn this song into a playable track, even without levelling or its picture."""


def _skip_reason(err: BaseException) -> str:
    """A short plain-language reason for the export report ("disk is full"), never raw error text."""
    if isinstance(err, _NotConvertible):
        return "could not be converted; the file may be damaged"
    return friendly_error(err, "export").title.lower()


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
    on_success: ReportFn | None = None,
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
    and ``on_cancelled(done, total)`` is called. A song whose volume could not be levelled is still
    exported, at its original volume, and listed in the report's ``not_leveled``.
    """
    total = len(files_to_export)
    width = track_number_width(total)
    dest_dir = Path(dest_folder)
    success_count = 0
    skipped: list[str] = []
    not_leveled: list[str] = []
    exported_tracks: list[tuple[str, str, float]] = []
    stopped: DriveProblem = ""

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def _stopping() -> bool:
        return bool(is_shutting_down_fn and is_shutting_down_fn()) or _cancelled()

    try:
        # Checked first: a job stopped while it was still queued must leave the drive as it was.
        if not _stopping():
            # The folder is made here and not by the window, so that a locked or unplugged drive ends
            # as a reported error instead of an exception that leaves the window stuck on "Exporting...".
            dest_dir.mkdir(parents=True, exist_ok=True)
        if clear_existing and not _stopping():
            if on_status:
                on_status("Removing the songs from the previous export...")
            for name in remove_previous_export(dest_folder):
                skipped.append(f"{name} (old file could not be removed)")

        for idx, filepath in enumerate(files_to_export, 1):
            if _stopping():
                break
            stopped = _drive_problem(dest_dir)
            if stopped:
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
                    encoded = _encode_mp3(filepath, dest_file, stats, track_normalize, cancel_event)
                    if _stopping():
                        with contextlib.suppress(OSError):
                            dest_file.unlink()
                        break
                    if not encoded:
                        stopped = _drive_problem(dest_dir)
                        if stopped:
                            break
                        # FFmpeg failed: still deliver the song, but never call that a levelled export.
                        if is_mp3:
                            copy_file_atomic(source, dest_file)
                        elif not _ffmpeg_to(
                            ["-y", "-i", filepath, *mp3_audio_only_args(resample_44k=True)],
                            dest_file,
                            "mp3",
                            cancel_event,
                        ):
                            raise _NotConvertible(f"{source.name} could not be converted to MP3")
                        if _stopping():
                            break
                        if track_normalize:
                            not_leveled.append(source.name)
                else:
                    dest_file = dest_dir / f"{idx:0{width}d} - {clean_base}"
                    if on_status:
                        on_status(f"Copying USB track {idx} of {total}...")
                    copy_file_atomic(source, dest_file)

                _fsync_file(str(dest_file))
                success_count += 1
                dur = duration_fn(filepath) if duration_fn else 0.0
                track_title = clean_stem if (track_normalize or not is_mp3) else clean_base
                exported_tracks.append((dest_file.name, track_title, dur))
            except Exception as track_err:  # one bad song must not end the whole export
                logger.error("usb export track %s: %s", filepath, track_err)
                stopped = _drive_problem(dest_dir, track_err)
                if stopped:
                    break
                skipped.append(f"{source.name} ({_skip_reason(track_err)})")

        shutting_down = bool(is_shutting_down_fn and is_shutting_down_fn())
        # Even after Stop, list the songs that were copied so the drive plays them in order.
        if exported_tracks and not shutting_down and stopped != "removed":
            _write_m3u(dest_folder, playlist_name, exported_tracks)

        if _cancelled():
            if on_cancelled:
                on_cancelled(success_count, total)
            return
        if on_progress:
            on_progress(100.0)
        if on_success:
            on_success(ExportReport(total, success_count, tuple(skipped), tuple(not_leveled), stopped))
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
    on_success: ReportFn | None = None,
    on_error: Callable[[str], None] | None = None,
    is_shutting_down_fn: StopFn | None = None,
    cancel_event: CancelToken | None = None,
    on_cancelled: Callable[[int, int], None] | None = None,
    clear_existing: bool = False,
) -> None:
    """Export tracks to Desktop burn folder as standard Red Book 44.1kHz 16-bit stereo PCM WAV files.

    ``clear_existing`` first removes the tracks of an earlier export (and nothing else in the
    folder); an export stopped before it began removes nothing. A song whose volume could not be
    levelled is still exported, at its original volume, and listed in the report's ``not_leveled``.
    """
    total = len(files_to_export)
    width = track_number_width(total)
    cd_dir = Path(cd_folder)
    success_count = 0
    skipped: list[str] = []
    not_leveled: list[str] = []
    stopped: DriveProblem = ""

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
                cd_audio = ["-vn", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le"]
                level = (
                    ["-af", loudnorm_filter(measure_loudnorm(filepath, cancel_event=cancel_event))] if normalize else []
                )
                made = _ffmpeg_to(["-y", "-i", filepath, *level, *cd_audio], wav_path, "wav", cancel_event)
                if _stopping():
                    break
                if not made and normalize:
                    # Levelling failed: still deliver the track, but never call that a levelled export.
                    made = _ffmpeg_to(["-y", "-i", filepath, *cd_audio], wav_path, "wav", cancel_event)
                    if _stopping():
                        break
                    if made:
                        not_leveled.append(source.name)
                if not made:
                    raise _NotConvertible(f"{source.name} could not be converted for the CD")
                success_count += 1
            except Exception as track_err:  # one bad song must not end the whole export
                logger.error("cd export track %s: %s", filepath, track_err)
                stopped = _drive_problem(cd_dir, track_err)
                if stopped:
                    break
                skipped.append(f"{source.stem} ({_skip_reason(track_err)})")

        if _cancelled():
            if on_cancelled:
                on_cancelled(success_count, total)
            return
        if on_progress:
            on_progress(100.0)
        if on_success:
            on_success(ExportReport(total, success_count, tuple(skipped), tuple(not_leveled), stopped))
    except Exception as e:  # last-resort guard: a worker must always report back to the window
        logger.error("cd_export_worker: %s", e)
        if on_error:
            on_error(f"Could not prepare CD files:\n{e}")

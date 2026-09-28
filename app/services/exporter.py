"""Export services for USB flash drives (with loudness normalization and M3U playlists) and CD burning."""

import json
import math
import os
import re
import shutil
from collections.abc import Callable, Sequence

from pydub import AudioSegment

from app.config import log_error, run_ffmpeg, sanitize_filename
from app.core.cache_manager import cache_mgr

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


def measure_loudnorm(filepath: str) -> dict[str, str] | None:
    """Run (or reuse the cached result of) FFmpeg's loudnorm analysis pass for two-pass normalization."""
    cached = cache_mgr.get_loudnorm_stats(filepath)
    if cached and all(k in cached for k in _LOUDNORM_KEYS):
        return cached
    res = run_ffmpeg(
        ["-hide_banner", "-i", filepath, "-vn", "-af", f"{_LOUDNORM_BASE}:print_format=json", "-f", "null", "-"],
        timeout=_ENCODE_TIMEOUT_SEC,
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


def usb_export_worker(
    dest_folder: str,
    playlist_name: str,
    files_to_export: Sequence[str],
    normalize: bool = False,
    on_progress: Callable[[float], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    on_success: Callable[[int, int, list[str]], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    is_shutting_down_fn: Callable[[], bool] | None = None,
    duration_fn: Callable[[str], float] | None = None,
) -> None:
    """Export playlist tracks to USB with numbering, FAT32-safe filenames, optional loudnorm, and M3U playlist file."""
    total = len(files_to_export)
    success_count = 0
    skipped = []
    exported_tracks = []

    normalize_requested = normalize
    try:
        for idx, filepath in enumerate(files_to_export, 1):
            if is_shutting_down_fn and is_shutting_down_fn():
                break
            normalize = normalize_requested
            pct = (idx / total) * 100
            if on_progress:
                on_progress(pct)

            if not filepath or not os.path.exists(filepath):
                skipped.append(os.path.basename(filepath or f"Track {idx}") + " (file not found)")
                continue

            base_name = os.path.basename(filepath)
            stem, ext = os.path.splitext(base_name)
            clean_base = sanitize_filename(base_name)
            clean_stem = sanitize_filename(stem)
            is_mp3 = ext.lower() == ".mp3"

            try:
                stats = None
                if normalize:
                    if on_status:
                        on_status(f"Measuring loudness of USB track {idx} of {total}...")
                    stats = measure_loudnorm(filepath)
                    if is_mp3 and is_already_level(stats):
                        normalize = False  # already at target: a straight copy is faster and lossless

                # Car stereos require standard MP3. If not already MP3 or if normalize is enabled, transcode.
                if normalize or not is_mp3:
                    norm_name = f"{idx:02d} - {clean_stem}.mp3"
                    dest_file = os.path.join(dest_folder, norm_name)
                    action_desc = "Normalizing & converting" if normalize else "Converting to MP3 for"
                    if on_status:
                        on_status(f"{action_desc} USB track {idx} of {total}...")
                    args = ["-y", "-i", filepath]
                    if normalize:
                        args += ["-af", loudnorm_filter(stats)]
                    args += [
                        "-map",
                        "0:a",
                        "-map",
                        "0:v?",
                        "-c:v",
                        "copy",
                        "-disposition:v:0",
                        "attached_pic",
                        "-map_metadata",
                        "0",
                        "-id3v2_version",
                        "3",
                        "-c:a",
                        "libmp3lame",
                        "-q:a",
                        "2",
                        "-ar",
                        "44100",
                        dest_file,
                    ]
                    result = run_ffmpeg(args, timeout=_ENCODE_TIMEOUT_SEC)
                    if result.returncode != 0 or not os.path.exists(dest_file) or os.path.getsize(dest_file) == 0:
                        # Retry with mjpeg video conversion to handle PNG/M4A cover art
                        if os.path.exists(dest_file):
                            try:
                                os.remove(dest_file)
                            except Exception:
                                pass
                        args_mjpeg = ["-y", "-i", filepath]
                        if normalize:
                            args_mjpeg += ["-af", loudnorm_filter(stats)]
                        args_mjpeg += [
                            "-map",
                            "0:a",
                            "-map",
                            "0:v?",
                            "-c:v:0",
                            "mjpeg",
                            "-disposition:v:0",
                            "attached_pic",
                            "-map_metadata",
                            "0",
                            "-id3v2_version",
                            "3",
                            "-c:a",
                            "libmp3lame",
                            "-q:a",
                            "2",
                            "-ar",
                            "44100",
                            dest_file,
                        ]
                        result = run_ffmpeg(args_mjpeg, timeout=_ENCODE_TIMEOUT_SEC)

                    if result.returncode != 0 or not os.path.exists(dest_file) or os.path.getsize(dest_file) == 0:
                        if is_mp3:
                            shutil.copy2(filepath, dest_file)
                        else:
                            audio = AudioSegment.from_file(filepath)
                            if normalize:
                                try:
                                    from pydub.effects import normalize as pydub_norm

                                    audio = pydub_norm(audio)
                                except Exception:
                                    pass
                            audio.export(dest_file, format="mp3")
                else:
                    new_name = f"{idx:02d} - {clean_base}"
                    dest_file = os.path.join(dest_folder, new_name)
                    if on_status:
                        on_status(f"Copying USB track {idx} of {total}...")
                    shutil.copy2(filepath, dest_file)

                success_count += 1
                dur = duration_fn(filepath) if duration_fn else 0.0
                track_title = clean_stem if (normalize or not is_mp3) else clean_base
                exported_tracks.append((os.path.basename(dest_file), track_title, dur))
            except Exception as track_err:
                log_error(f"usb export track {filepath}: {track_err}")
                skipped.append(f"{base_name} ({track_err})")

        # Generate M3U playlist files for car stereos and media players (UTF-8 BOM for car stereo compatibility)
        if exported_tracks and not (is_shutting_down_fn and is_shutting_down_fn()):
            try:
                pl_clean = sanitize_filename(playlist_name or "Playlist")
                for ext_name, enc in ((".m3u", "utf-8-sig"), (".m3u8", "utf-8")):
                    m3u_file = f"00_{pl_clean}{ext_name}"
                    m3u_path = os.path.join(dest_folder, m3u_file)
                    with open(m3u_path, "w", encoding=enc) as f:
                        f.write("#EXTM3U\n")
                        for fname, title, dur in exported_tracks:
                            d_int = int(dur) if dur > 0 else -1
                            f.write(f"#EXTINF:{d_int},{title}\n")
                            f.write(f"{fname}\n")
            except Exception as m3u_err:
                log_error(f"failed writing m3u: {m3u_err}")

        # Flush file and disk write buffers to physical media before reporting success
        if not (is_shutting_down_fn and is_shutting_down_fn()):
            flush_usb_drive(dest_folder)

        if on_success:
            on_success(success_count, total, skipped)
    except Exception as e:
        log_error(f"usb_export_worker: {e}")
        if on_error:
            on_error(f"Could not complete USB export:\n{e}")


def flush_usb_drive(dest_folder):
    """Flush file and disk write buffers to physical media to ensure safe drive removal."""
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            drive_letter = os.path.splitdrive(dest_folder)[0]
            if drive_letter:
                h = kernel32.CreateFileW(f"\\\\.\\{drive_letter}", 0x40000000 | 0x80000000, 1 | 2, None, 3, 0, None)
                if h and h != -1 and h != 0xFFFFFFFFFFFFFFFF:
                    try:
                        kernel32.FlushFileBuffers(h)
                    finally:
                        kernel32.CloseHandle(h)
        elif hasattr(os, "sync"):
            os.sync()
    except Exception as flush_err:
        log_error(f"flush_usb_drive: {flush_err}")


def cd_export_worker(
    cd_folder,
    files_to_export,
    normalize=False,
    on_progress=None,
    on_status=None,
    on_success=None,
    on_error=None,
    is_shutting_down_fn=None,
):
    """Export tracks to Desktop burn folder as standard Red Book 44.1kHz 16-bit stereo PCM WAV files."""
    total = len(files_to_export)
    success_count = 0
    skipped = []

    try:
        for idx, filepath in enumerate(files_to_export, 1):
            if is_shutting_down_fn and is_shutting_down_fn():
                break
            pct = (idx / total) * 100
            if on_progress:
                on_progress(pct)

            if not filepath or not os.path.exists(filepath):
                skipped.append(os.path.basename(filepath or f"Track {idx}") + " (file not found)")
                continue

            if on_status:
                norm_str = " (normalizing volume)..." if normalize else "..."
                on_status(f"Preparing CD track {idx} of {total}{norm_str}")

            base = os.path.splitext(os.path.basename(filepath))[0]
            clean_base = sanitize_filename(base)
            wav_path = os.path.join(cd_folder, f"{idx:02d} - {clean_base}.wav")

            try:
                args = ["-y", "-i", filepath]
                if normalize:
                    args += ["-af", loudnorm_filter(measure_loudnorm(filepath))]
                args += ["-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", wav_path]
                result = run_ffmpeg(args, timeout=_ENCODE_TIMEOUT_SEC)
                if result.returncode != 0 or not os.path.exists(wav_path) or os.path.getsize(wav_path) == 0:
                    audio = AudioSegment.from_file(filepath)
                    if normalize:
                        try:
                            from pydub.effects import normalize as pydub_norm

                            audio = pydub_norm(audio)
                        except Exception:
                            pass
                    audio = audio.set_frame_rate(44100).set_channels(2).set_sample_width(2)
                    audio.export(wav_path, format="wav")
                success_count += 1
            except Exception as track_err:
                log_error(f"cd export track {filepath}: {track_err}")
                skipped.append(f"{base} ({track_err})")

        if on_success:
            on_success(cd_folder, success_count, total, skipped)
    except Exception as e:
        log_error(f"cd_export_worker: {e}")
        if on_error:
            on_error(f"Could not prepare CD files:\n{e}")

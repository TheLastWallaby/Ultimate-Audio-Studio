"""Export services for USB flash drives (with loudness normalization and M3U playlists) and CD burning."""

import os
import shutil
from collections.abc import Callable, Sequence

from pydub import AudioSegment

from app.config import log_error, run_ffmpeg, sanitize_filename


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

            base_name = os.path.basename(filepath)
            stem, ext = os.path.splitext(base_name)
            clean_base = sanitize_filename(base_name)
            clean_stem = sanitize_filename(stem)
            is_mp3 = ext.lower() == ".mp3"

            try:
                # Car stereos require standard MP3. If not already MP3 or if normalize is enabled, transcode.
                if normalize or not is_mp3:
                    norm_name = f"{idx:02d} - {clean_stem}.mp3"
                    dest_file = os.path.join(dest_folder, norm_name)
                    action_desc = "Normalizing & converting" if normalize else "Converting to MP3 for"
                    if on_status:
                        on_status(f"{action_desc} USB track {idx} of {total}...")
                    args = ["-y", "-i", filepath]
                    if normalize:
                        args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
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
                        dest_file,
                    ]
                    result = run_ffmpeg(args)
                    if result.returncode != 0 or not os.path.exists(dest_file) or os.path.getsize(dest_file) == 0:
                        # Retry with mjpeg video conversion to handle PNG/M4A cover art
                        if os.path.exists(dest_file):
                            try:
                                os.remove(dest_file)
                            except Exception:
                                pass
                        args_mjpeg = ["-y", "-i", filepath]
                        if normalize:
                            args_mjpeg += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
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
                            dest_file,
                        ]
                        result = run_ffmpeg(args_mjpeg)

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
                    args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
                args += ["-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", wav_path]
                result = run_ffmpeg(args)
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

"""Audio clipping, volume boosting, fading, and audition slice generation."""

import os
import shutil
import time

from pydub import AudioSegment

from app.config import PREVIEW_CACHE_DIR, ffmpeg_path, log_error, run_ffmpeg


def create_audition_slice(filepath, s_time, e_time, gain_db=0.0, soften=False, fade_sec=1.5):
    """Generate a temporary rendered preview slice with volume boost and fade applied for 'Test Clip'."""
    if not filepath or not os.path.exists(filepath) or not os.path.exists(ffmpeg_path):
        return None
    try:
        os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
        clip_dur = max(0.05, e_time - s_time)
        fade_dur = min(float(fade_sec or 1.5), clip_dur / 2.0) if soften else 0.0
        filter_parts = ["asetpts=PTS-STARTPTS"]
        if gain_db > 0.05:
            filter_parts.append(f"volume={gain_db:.1f}dB,alimiter=limit=0.95:attack=5:release=50")
        elif gain_db < -0.05:
            filter_parts.append(f"volume={gain_db:.1f}dB")
        if soften and fade_dur > 0.01:
            out_start = max(0.0, clip_dur - fade_dur)
            filter_parts.append(f"afade=t=in:st=0:d={fade_dur:.3f},afade=t=out:st={out_start:.3f}:d={fade_dur:.3f}")

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
            ",".join(filter_parts),
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


def clip_audio_worker(
    filepath,
    s_time,
    e_time,
    save_name,
    soften=False,
    gain_db=0.0,
    is_self_overwrite=False,
    on_success=None,
    on_error=None,
    fade_sec=1.5,
):
    """Export sample-accurate clipped audio file with backup protection and Windows lock retries."""
    save_dir = os.path.dirname(os.path.abspath(save_name))
    os.makedirs(save_dir, exist_ok=True)
    wav = save_name.lower().endswith(".wav")
    tmp_ext = ".wav" if wav else ".mp3"
    tmp_save = os.path.join(save_dir, f".clip_tmp_{os.getpid()}_{int(time.time() * 1000)}{tmp_ext}")

    try:
        dur = max(0.01, e_time - s_time)
        fade_dur = min(float(fade_sec or 1.5), dur / 2.0) if soften else 0.0

        filter_parts = ["asetpts=PTS-STARTPTS"]
        if gain_db > 0.05:
            filter_parts.append(f"volume={gain_db:.1f}dB,alimiter=limit=0.95:attack=5:release=50")
        elif gain_db < -0.05:
            filter_parts.append(f"volume={gain_db:.1f}dB")
        if soften and fade_dur > 0.01:
            out_start = max(0.0, dur - fade_dur)
            filter_parts.append(f"afade=t=in:st=0:d={fade_dur:.3f},afade=t=out:st={out_start:.3f}:d={fade_dur:.3f}")

        if wav:
            args = ["-y", "-accurate_seek", "-ss", f"{s_time:.3f}", "-i", filepath, "-t", f"{dur:.3f}"]
            args += ["-af", ",".join(filter_parts)]
            args += ["-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le"]
        else:
            # Place -ss after -i for MP3 so stream 0:v (attached cover art at t=0) is preserved
            args = ["-y", "-i", filepath, "-ss", f"{s_time:.3f}", "-t", f"{dur:.3f}"]
            args += ["-af", ",".join(filter_parts)]
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
                "-b:a",
                "320k",
            ]
        args.append(tmp_save)
        result = run_ffmpeg(args)

        if result.returncode != 0 and not wav:
            # Retry transcoding video stream to mjpeg in case source art was PNG
            if os.path.exists(tmp_save):
                try:
                    os.remove(tmp_save)
                except Exception:
                    pass
            args_retry = ["-y", "-i", filepath, "-ss", f"{s_time:.3f}", "-t", f"{dur:.3f}"]
            args_retry += ["-af", ",".join(filter_parts)]
            args_retry += [
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
                "-b:a",
                "320k",
                tmp_save,
            ]
            result = run_ffmpeg(args_retry)
        if result.returncode != 0 or not os.path.exists(tmp_save) or os.path.getsize(tmp_save) == 0:
            if os.path.exists(tmp_save):
                try:
                    os.remove(tmp_save)
                except Exception:
                    pass
            audio = AudioSegment.from_file(filepath)
            clipped = audio[s_time * 1000 : e_time * 1000]
            if abs(gain_db) > 0.05:
                clipped = clipped + gain_db
            if soften and fade_dur > 0.01:
                fade_ms = int(fade_dur * 1000)
                clipped = clipped.fade_in(fade_ms).fade_out(fade_ms)
            if wav:
                clipped.export(tmp_save, format="wav")
            else:
                clipped.export(tmp_save, format="mp3", bitrate="320k")

        if not os.path.exists(tmp_save) or os.path.getsize(tmp_save) == 0:
            raise RuntimeError("Audio clipping produced an empty file.")

        if is_self_overwrite:
            time.sleep(0.05)
            # Safeguard original file with automatic backup
            try:
                bak_path = save_name + ".original.bak"
                if not os.path.exists(bak_path):
                    shutil.copy2(save_name, bak_path)
            except Exception as bak_err:
                log_error(f"backup original failed: {bak_err}")

        # Resilient file replace retry loop against Windows file indexing / antivirus locks
        replaced = False
        for attempt in range(5):
            try:
                os.replace(tmp_save, save_name)
                replaced = True
                break
            except PermissionError:
                time.sleep(0.08 * (attempt + 1))
        if not replaced:
            os.replace(tmp_save, save_name)

        if on_success:
            on_success(os.path.basename(save_name), save_name, is_self_overwrite)
    except Exception as e:
        if os.path.exists(tmp_save):
            try:
                os.remove(tmp_save)
            except Exception:
                pass
        log_error(f"clip_audio_worker: {e}")
        if on_error:
            on_error(str(e))

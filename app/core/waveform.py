"""Waveform peak extraction and coordinate translation logic."""

from __future__ import annotations

import array
import os
import subprocess
import threading
from collections.abc import Callable, Sequence

from pydub import AudioSegment

from app.config import CREATE_NO_WINDOW, ffmpeg_path, log_error


def _samples_to_peaks(samples: Sequence[int], n_bars: int) -> list[float]:
    """Normalize raw 16-bit PCM samples into n_bars normalized float peaks [0.0, 1.0]."""
    if not samples:
        return []
    n_samples = len(samples)
    peaks = []
    for i in range(n_bars):
        start = int(i * n_samples / n_bars)
        end = int((i + 1) * n_samples / n_bars)
        if start < end:
            chunk = samples[start:end]
            peaks.append(max(max(chunk), -min(chunk)))
        else:
            peaks.append(0)
    max_p = max(peaks) if peaks else 0
    if max_p <= 0:
        return []
    return [float(p) / float(max_p) for p in peaks]


def extract_waveform_peaks(
    audio_path: str,
    n_bars: int = 220,
    cancel_event: threading.Event | None = None,
    on_process_spawned: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> list[float]:
    """Fast downsampled audio peak extraction for waveform visualization with accurate binning and timeout.

    Returns a list of float peak values between 0.0 and 1.0, or [] if extraction fails.
    """
    if not audio_path or not os.path.exists(audio_path):
        return []
    if cancel_event and cancel_event.is_set():
        return []

    # 1. Fast direct extraction via ffmpeg raw PCM pipe
    proc = None
    try:
        cmd = [
            ffmpeg_path,
            "-nostdin",
            "-threads",
            "2",
            "-i",
            audio_path,
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            "1000",
            "-f",
            "s16le",
            "-",
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        if on_process_spawned:
            try:
                on_process_spawned(proc)
            except Exception:
                pass

        try:
            stdout_data, stderr_data = proc.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_data, stderr_data = proc.communicate()
            log_error(f"extract_waveform_peaks: ffmpeg pipe timed out on {audio_path}")

        if cancel_event and cancel_event.is_set():
            return []

        if proc.returncode == 0 and stdout_data:
            raw_bytes = stdout_data
            even_len = len(raw_bytes) - (len(raw_bytes) % 2)
            if even_len > 0:
                samples = array.array("h", raw_bytes[:even_len])
                norm_peaks = _samples_to_peaks(samples, n_bars)
                if norm_peaks:
                    return norm_peaks
        else:
            err_msg = stderr_data.decode("utf-8", errors="ignore")[:300] if stderr_data else ""
            if not (cancel_event and cancel_event.is_set()):
                log_error(f"extract_waveform_peaks (ffmpeg pipe rc={proc.returncode}): {err_msg}")
    except Exception as e:
        if not (cancel_event and cancel_event.is_set()):
            log_error(f"extract_waveform_peaks (ffmpeg pipe): {e}")

    # 2. In-process Pygame Sound extraction (zero external dependencies, memory safe)
    try:
        if os.path.exists(audio_path) and os.path.getsize(audio_path) < 20 * 1024 * 1024:
            import pygame

            if not pygame.mixer.get_init():
                pygame.mixer.init()
            snd = pygame.mixer.Sound(audio_path)
            raw = snd.get_raw()
            del snd
            even_len = len(raw) - (len(raw) % 2)
            if even_len > 0:
                samples = array.array("h", raw[:even_len])
                norm_peaks = _samples_to_peaks(samples, n_bars)
                if norm_peaks:
                    return norm_peaks
    except Exception as e:
        log_error(f"extract_waveform_peaks (pygame fallback): {e}")

    # 3. Resilient temp WAV extraction via ffmpeg + standard library wave
    import tempfile
    import wave

    tmp_wav = None
    try:
        fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        cmd = [
            ffmpeg_path,
            "-nostdin",
            "-y",
            "-i",
            audio_path,
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            "1000",
            "-f",
            "wav",
            tmp_wav,
        ]
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            timeout=25,
        )
        if res.returncode == 0 and os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 44:
            with wave.open(tmp_wav, "rb") as wf:
                raw_bytes = wf.readframes(wf.getnframes())
            even_len = len(raw_bytes) - (len(raw_bytes) % 2)
            if even_len > 0:
                samples = array.array("h", raw_bytes[:even_len])
                norm_peaks = _samples_to_peaks(samples, n_bars)
                if norm_peaks:
                    return norm_peaks
        elif res.returncode != 0:
            err_msg = res.stderr.decode("utf-8", errors="ignore")[:300] if res.stderr else ""
            log_error(f"extract_waveform_peaks (temp wav rc={res.returncode}): {err_msg}")
    except Exception as e:
        log_error(f"extract_waveform_peaks (temp wav fallback): {e}")
    finally:
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    # 4. Resilient fallback to pydub (with file size safety check)
    try:
        if os.path.exists(audio_path) and os.path.getsize(audio_path) < 50 * 1024 * 1024:
            audio = AudioSegment.from_file(audio_path)
            low_res = audio.set_frame_rate(1000).set_channels(1)
            samples = low_res.get_array_of_samples()
            norm_peaks = _samples_to_peaks(samples, n_bars)
            if norm_peaks:
                return norm_peaks
    except Exception as e:
        log_error(f"extract_waveform_peaks (pydub fallback): {e}")

    return []


def get_waveform_bounds(track_duration, clip_start, clip_end, zoomed=False):
    """Calculate the visible start and end seconds on the waveform canvas."""
    if not zoomed or track_duration <= 0:
        return 0.0, max(track_duration, 1.0)
    start_b = max(0.0, clip_start - 5.0)
    end_b = min(track_duration, clip_end + 5.0)
    if end_b <= start_b:
        end_b = min(track_duration, start_b + 10.0)
    return start_b, max(end_b, start_b + 0.1)


def time_to_waveform_x(t, w, start_bound, end_bound):
    """Translate a time in seconds to an x-pixel coordinate within width w."""
    span = max(0.1, end_bound - start_bound)
    clamped_t = max(start_bound, min(end_bound, t))
    return ((clamped_t - start_bound) / span) * w


def time_from_waveform_x(px, w, start_bound, end_bound):
    """Translate an x-pixel coordinate within width w to a time in seconds."""
    span = max(0.1, end_bound - start_bound)
    ratio = max(0.0, min(1.0, px / max(1.0, float(w))))
    return start_bound + (ratio * span)

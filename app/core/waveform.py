"""Waveform peak extraction and coordinate translation logic."""

from __future__ import annotations

import array
import math
import operator
import os
import subprocess
import threading
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass

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


ANALYSIS_RATE_HZ = 4000
_BLOCK_SECONDS = 0.4
_ABSOLUTE_GATE_DB = -70.0
_RELATIVE_GATE_DB = -10.0


@dataclass(slots=True, frozen=True)
class AudioAnalysis:
    """Waveform peaks (normalized 0..1 for drawing) plus the track's un-normalized loudness."""

    peaks: list[float]
    loudness_db: float | None


def _mean_square_db(mean_square: float) -> float:
    return 10.0 * math.log10(mean_square) if mean_square > 0 else -200.0


def measure_loudness_db(samples: Sequence[int], block_size: int) -> float | None:
    """Estimate programme loudness in dBFS from raw 16-bit PCM using gated block RMS.

    Mirrors EBU R128 gating (400 ms blocks, -70 dB absolute gate, -10 dB relative gate)
    without K-weighting, so quiet intros and silences do not drag the estimate down.
    Unlike the drawing peaks, this is NOT normalized, so loud and quiet tracks differ.
    """
    n = len(samples)
    if n == 0 or block_size <= 0:
        return None
    full_scale_sq = 32768.0 * 32768.0
    blocks: list[float] = []
    for start in range(0, n, block_size):
        chunk = samples[start : start + block_size]
        if len(chunk) < block_size // 2:
            break
        blocks.append(sum(map(operator.mul, chunk, chunk)) / (len(chunk) * full_scale_sq))
    gated = [ms for ms in blocks if _mean_square_db(ms) > _ABSOLUTE_GATE_DB]
    if not gated:
        return None
    rel_threshold = _mean_square_db(sum(gated) / len(gated)) + _RELATIVE_GATE_DB
    loud = [ms for ms in gated if _mean_square_db(ms) > rel_threshold] or gated
    return round(_mean_square_db(sum(loud) / len(loud)), 2)


def _analysis_from_samples(samples: Sequence[int], n_bars: int, rate: int, channels: int = 1) -> AudioAnalysis:
    return AudioAnalysis(
        peaks=_samples_to_peaks(samples, n_bars),
        loudness_db=measure_loudness_db(samples, int(rate * channels * _BLOCK_SECONDS)),
    )


def _analysis_from_wav_file(audio_path: str, n_bars: int, cancel_event: threading.Event | None) -> AudioAnalysis | None:
    """Analyse an uncompressed WAV file with the standard library: no FFmpeg, no audio device.

    Uses the first channel, as 16-bit samples, at about ANALYSIS_RATE_HZ, read a second at a time so
    a long recording never sits in memory. Returns None for anything ``wave`` cannot read (other
    formats, compressed or floating-point WAV) or when cancelled.
    """
    samples = array.array("h")
    try:
        with wave.open(audio_path, "rb") as wf:
            channels, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
            if channels < 1 or width < 1 or rate < 1:
                return None
            step = max(1, rate // ANALYSIS_RATE_HZ)
            stride = channels * width * step
            while True:
                if cancel_event and cancel_event.is_set():
                    return None
                block = wf.readframes(rate)
                if not block:
                    break
                for offset in range(0, len(block) - width + 1, stride):
                    if width == 1:  # 8-bit WAV is unsigned
                        samples.append((block[offset] - 128) << 8)
                    else:  # little-endian signed; the top two bytes give the 16-bit value
                        samples.append(
                            int.from_bytes(block[offset + width - 2 : offset + width], "little", signed=True)
                        )
    except (OSError, EOFError, wave.Error) as err:
        log_error(f"analyze_audio (wav reader): {err}")
        return None
    if not samples:
        return None
    return _analysis_from_samples(samples, n_bars, rate // step)


def analyze_audio(
    audio_path: str,
    n_bars: int = 220,
    cancel_event: threading.Event | None = None,
    on_process_spawned: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> AudioAnalysis:
    """Decode a track once to produce waveform peaks and a loudness estimate.

    Returns AudioAnalysis([], None) if the file cannot be decoded.
    """
    empty = AudioAnalysis([], None)
    if not audio_path or not os.path.exists(audio_path):
        return empty
    if cancel_event and cancel_event.is_set():
        return empty

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
            str(ANALYSIS_RATE_HZ),
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
            log_error(f"analyze_audio: ffmpeg pipe timed out on {audio_path}")

        if cancel_event and cancel_event.is_set():
            return empty

        if proc.returncode == 0 and stdout_data:
            raw_bytes = stdout_data
            even_len = len(raw_bytes) - (len(raw_bytes) % 2)
            if even_len > 0:
                samples = array.array("h", raw_bytes[:even_len])
                result = _analysis_from_samples(samples, n_bars, ANALYSIS_RATE_HZ)
                if result.peaks:
                    return result
        else:
            err_msg = stderr_data.decode("utf-8", errors="ignore")[:300] if stderr_data else ""
            if not (cancel_event and cancel_event.is_set()):
                log_error(f"analyze_audio (ffmpeg pipe rc={proc.returncode}): {err_msg}")
    except Exception as e:
        if not (cancel_event and cancel_event.is_set()):
            log_error(f"analyze_audio (ffmpeg pipe): {e}")

    # 2. A WAV file can be read directly; this needs no FFmpeg and no audio device (the pygame
    #    step below fails on a PC without sound output, such as a build server).
    if audio_path.lower().endswith((".wav", ".wave")):
        from_wav = _analysis_from_wav_file(audio_path, n_bars, cancel_event)
        if from_wav is not None and from_wav.peaks:
            return from_wav

    # 3. In-process Pygame Sound extraction (zero external dependencies, memory safe)
    try:
        if os.path.exists(audio_path) and os.path.getsize(audio_path) < 20 * 1024 * 1024:
            import pygame

            if not pygame.mixer.get_init():
                pygame.mixer.init()
            mixer_rate, _fmt, mixer_channels = pygame.mixer.get_init()
            snd = pygame.mixer.Sound(audio_path)
            raw = snd.get_raw()
            del snd
            even_len = len(raw) - (len(raw) % 2)
            if even_len > 0:
                samples = array.array("h", raw[:even_len])
                result = _analysis_from_samples(samples, n_bars, mixer_rate, mixer_channels)
                if result.peaks:
                    return result
    except Exception as e:
        log_error(f"analyze_audio (pygame fallback): {e}")

    # 4. Resilient temp WAV extraction via ffmpeg + standard library wave
    import tempfile

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
            str(ANALYSIS_RATE_HZ),
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
                result = _analysis_from_samples(samples, n_bars, ANALYSIS_RATE_HZ)
                if result.peaks:
                    return result
        elif res.returncode != 0:
            err_msg = res.stderr.decode("utf-8", errors="ignore")[:300] if res.stderr else ""
            log_error(f"analyze_audio (temp wav rc={res.returncode}): {err_msg}")
    except Exception as e:
        log_error(f"analyze_audio (temp wav fallback): {e}")
    finally:
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    # A pydub step used to follow here. It needed FFmpeg for everything except WAV, which step 2
    # now reads directly, and it ran without a time limit; a song nothing can read has no waveform.
    return empty


def extract_waveform_peaks(
    audio_path: str,
    n_bars: int = 220,
    cancel_event: threading.Event | None = None,
    on_process_spawned: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> list[float]:
    """Fast downsampled audio peak extraction for waveform visualization with accurate binning and timeout.

    Returns a list of float peak values between 0.0 and 1.0, or [] if extraction fails.
    """
    return analyze_audio(audio_path, n_bars, cancel_event, on_process_spawned).peaks


def get_waveform_bounds(
    track_duration: float, clip_start: float, clip_end: float, zoomed: bool = False
) -> tuple[float, float]:
    """Calculate the visible start and end seconds on the waveform canvas."""
    if not zoomed or track_duration <= 0:
        return 0.0, max(track_duration, 1.0)
    start_b = max(0.0, clip_start - 5.0)
    end_b = min(track_duration, clip_end + 5.0)
    if end_b <= start_b:
        end_b = min(track_duration, start_b + 10.0)
    return start_b, max(end_b, start_b + 0.1)


def time_to_waveform_x(t: float, w: float, start_bound: float, end_bound: float) -> float:
    """Translate a time in seconds to an x-pixel coordinate within width w."""
    span = max(0.1, end_bound - start_bound)
    clamped_t = max(start_bound, min(end_bound, t))
    return ((clamped_t - start_bound) / span) * w


def time_from_waveform_x(px: float, w: float, start_bound: float, end_bound: float) -> float:
    """Translate an x-pixel coordinate within width w to a time in seconds."""
    span = max(0.1, end_bound - start_bound)
    ratio = max(0.0, min(1.0, px / max(1.0, float(w))))
    return start_bound + (ratio * span)

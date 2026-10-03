"""Pygame mixer wrapper, high-precision timeline tracking, and playable audio caching."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

import pygame

from app.config import PREVIEW_CACHE_DIR, log_error, run_ffmpeg
from app.core.metadata import read_track_metadata

logger = logging.getLogger(__name__)

# FFmpeg converts to WAV many times faster than the music plays, so half the song's length (plus a
# minute) is plenty even on a slow PC. A song whose length is unknown gets the generous fixed limit.
_CONVERT_TIMEOUT_MIN_SEC = 60
_CONVERT_TIMEOUT_MAX_SEC = 1800
_CONVERT_TIMEOUT_UNKNOWN_SEC = 600


def _conversion_timeout_sec(filepath: str) -> int:
    """Seconds FFmpeg may take to convert ``filepath`` to WAV, scaled to the song's length."""
    duration = read_track_metadata(filepath, probe_fallback=False).duration
    if duration <= 0:
        return _CONVERT_TIMEOUT_UNKNOWN_SEC
    return int(min(_CONVERT_TIMEOUT_MAX_SEC, max(_CONVERT_TIMEOUT_MIN_SEC, 60 + duration / 2)))


def _has_content(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


class AudioEngine:
    """Manages audio playback via Pygame mixer with monotonic clock timeline tracking and conversion cache.

    The end of a song is detected by the caller polling :meth:`is_busy` and the clock: pygame's
    end event needs the pygame *display* subsystem, which this Tkinter app never initializes.
    """

    NATIVE_EXTS = (".mp3", ".wav", ".ogg", ".flac")

    def __init__(self, max_playable_cache: int = 8, use_volume_curve: bool = False) -> None:
        self._playable_cache: OrderedDict[str, str] = OrderedDict()
        self._max_playable_cache = max_playable_cache
        self.play_start_offset = 0.0
        self.play_clock_origin: float | None = None
        self._master_volume = 0.8
        self._current_gain_factor = 1.0
        self.auto_level_enabled = False
        self.use_volume_curve = use_volume_curve
        # AGENTS.md invariant: 8192-sample buffer avoids crackling on slow machines.
        self.buffer_samples = 8192
        self._cache_lock = threading.Lock()
        # One lock per converted file: loading a song converts it ahead of time and Play converts it
        # too, and the second run must wait for the first instead of reading its half-written file.
        self._convert_locks: dict[str, threading.Lock] = {}
        self._init_mixer()

    def _init_mixer(self) -> None:
        """Single mixer pre_init to avoid audio driver deadlocks on Windows."""
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.pre_init(44100, -16, 2, self.buffer_samples)
                pygame.mixer.init()
                pygame.mixer.music.set_volume(0.8)
        except Exception as e:
            log_error(f"AudioEngine._init_mixer: {e}")

    def release_audio_file(self) -> None:
        """Safely stops and unloads pygame mixer handles to prevent Windows file locking."""
        with contextlib.suppress(Exception):
            pygame.mixer.music.stop()
            if hasattr(pygame.mixer.music, "unload"):
                pygame.mixer.music.unload()

    def needs_conversion(self, filepath: str | None) -> bool:
        """True when ``filepath`` must be converted before pygame can play it, and is not converted yet."""
        if not filepath or not os.path.exists(filepath):
            return False
        if os.path.splitext(filepath)[1].lower() in self.NATIVE_EXTS:
            return False
        try:
            return not _has_content(self._converted_wav_path(filepath))
        except OSError:  # the song vanished meanwhile: there is nothing to convert
            return False

    @staticmethod
    def _converted_wav_path(filepath: str) -> Path:
        """Where the WAV made from ``filepath`` is kept.

        The name changes whenever the song is edited or replaced, so a song swapped for another
        under the same name never plays the old one's WAV. Raises OSError when the song is gone.
        """
        source = Path(filepath)
        info = source.stat()
        key = hashlib.md5(str(source.absolute()).encode("utf-8", errors="ignore"), usedforsecurity=False).hexdigest()
        return Path(PREVIEW_CACHE_DIR) / f"{key}_{info.st_mtime_ns}_{info.st_size}.wav"

    def _conversion_lock(self, wav: Path) -> threading.Lock:
        with self._cache_lock:
            return self._convert_locks.setdefault(str(wav).casefold(), threading.Lock())

    def get_playable_audio_path(self, filepath: str) -> str:
        """Returns a path directly playable by pygame.mixer.music, converting unsupported formats (e.g. M4A) to cached WAV if needed.

        Thread-safe: may be called from a worker thread to prepare a file before playback. Returns
        ``filepath`` itself when the conversion failed.
        """
        if not filepath or not os.path.exists(filepath):
            return filepath
        if os.path.splitext(filepath)[1].lower() in self.NATIVE_EXTS:
            return filepath
        try:
            wav = self._converted_wav_path(filepath)
        except OSError:
            return filepath
        with self._conversion_lock(wav):
            if not _has_content(wav) and not self._convert_to_wav(filepath, wav):
                return filepath
        self._remember_playable(filepath, str(wav))
        return str(wav)

    @staticmethod
    def _convert_to_wav(filepath: str, wav: Path) -> bool:
        """Convert ``filepath`` to ``wav`` with FFmpeg; True when a complete WAV is in place.

        FFmpeg writes under a temporary name that is renamed only after a clean finish, so a run
        that times out or fails never leaves a half-written WAV that a later call would play.
        """
        partial = wav.with_name(f"{wav.name}.{os.getpid()}.{threading.get_ident()}.partial")
        try:
            wav.parent.mkdir(parents=True, exist_ok=True)
            args = ["-y", "-i", filepath, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", "-f", "wav", str(partial)]
            result = run_ffmpeg(args, timeout=_conversion_timeout_sec(filepath))
            if result.returncode == 0 and _has_content(partial):
                os.replace(partial, wav)
                return True
            logger.warning(
                "Converting %s for playback failed (code %s): %s",
                Path(filepath).name,
                result.returncode,
                str(result.stderr)[-300:],
            )
        except OSError as err:
            logger.warning("Converting %s for playback failed: %s", Path(filepath).name, err)
        finally:
            # Gone after a successful rename; a leftover is never read, so a failure here is harmless.
            with contextlib.suppress(OSError):
                partial.unlink()
        return False

    def prepare_for_playback(self, filepath: str) -> bool:
        """Make ``filepath`` playable, converting it if needed; False when the conversion failed.

        Slow (it can run FFmpeg for many seconds), so call it from a worker thread. Only start
        playback when it returns True: ``load_and_play`` on a file that is still unconverted runs
        the same failing conversion again, on the calling thread.
        """
        self.get_playable_audio_path(filepath)
        return not self.needs_conversion(filepath)

    def _remember_playable(self, filepath: str, cached_wav: str) -> None:
        """Record a converted WAV in the bounded LRU cache, deleting evicted files."""
        evicted = []
        with self._cache_lock:
            if filepath in self._playable_cache:
                previous = self._playable_cache[filepath]
                if previous != cached_wav:  # the song was replaced: its old WAV is no longer used
                    evicted.append(previous)
                self._playable_cache.move_to_end(filepath)
            else:
                while len(self._playable_cache) >= self._max_playable_cache:
                    _k, old_path = self._playable_cache.popitem(last=False)
                    if old_path and old_path != cached_wav:
                        evicted.append(old_path)
            self._playable_cache[filepath] = cached_wav
        for old_path in evicted:
            with contextlib.suppress(OSError):
                os.remove(old_path)

    def start_clock(self, start_pos: float) -> None:
        """Set the reference origin for playback timeline tracking."""
        self.play_start_offset = float(start_pos)
        self.play_clock_origin = time.monotonic()

    def pause_clock(self) -> None:
        """Record elapsed playback time and pause clock origin."""
        self.play_start_offset = self.current_play_seconds()
        self.play_clock_origin = None

    def seek_clock(self, new_pos: float, is_playing: bool = True) -> None:
        """Update clock after scrubbing or seeking."""
        self.play_start_offset = float(new_pos)
        if is_playing:
            self.play_clock_origin = time.monotonic()
        else:
            self.play_clock_origin = None

    def current_play_seconds(self) -> float:
        """Calculate elapsed seconds using monotonic clock offsets instead of drifting get_pos()."""
        if self.play_clock_origin is not None:
            return self.play_start_offset + (time.monotonic() - self.play_clock_origin)
        return self.play_start_offset

    def load_and_play(self, filepath: str, start_sec: float = 0.0) -> None:
        """Load and start playback from start_sec."""
        playable = self.get_playable_audio_path(filepath)
        pygame.mixer.music.load(playable)
        pygame.mixer.music.play(start=start_sec)
        self.start_clock(start_sec)

    def pause(self) -> None:
        """Pause playback."""
        self.pause_clock()
        pygame.mixer.music.pause()

    def unpause(self) -> None:
        """Unpause playback."""
        pygame.mixer.music.unpause()
        self.start_clock(self.play_start_offset)

    def stop(self) -> None:
        """Stop playback and reset clock origin."""
        self.release_audio_file()
        self.play_clock_origin = None

    @property
    def volume(self) -> float:
        return self._master_volume

    @property
    def effective_volume(self) -> float:
        scaled = (self._master_volume**2) if getattr(self, "use_volume_curve", False) else self._master_volume
        return min(1.0, scaled * (self._current_gain_factor if self.auto_level_enabled else 1.0))

    def set_volume(self, vol: float, use_curve: bool | None = None) -> None:
        """Set master volume between 0.0 and 1.0 with perceptual logarithmic taper (vol^2) and auto-level scaling."""
        try:
            self._master_volume = max(0.0, min(1.0, float(vol)))
            if use_curve is not None:
                self.use_volume_curve = bool(use_curve)
            scaled = (self._master_volume**2) if getattr(self, "use_volume_curve", False) else self._master_volume
            effective = scaled * (self._current_gain_factor if self.auto_level_enabled else 1.0)
            pygame.mixer.music.set_volume(max(0.0, min(1.0, effective)))
        except Exception:
            pass

    def set_auto_level(self, enabled: bool) -> None:
        """Toggle player-side loudness leveling during playback."""
        self.auto_level_enabled = bool(enabled)
        self.set_volume(self._master_volume)

    def set_track_gain(self, gain_factor: float) -> None:
        """Apply dynamic gain scaling factor for current track when auto-level is enabled."""
        self._current_gain_factor = max(0.1, min(2.0, float(gain_factor)))
        if self.auto_level_enabled:
            self.set_volume(self._master_volume)

    def is_busy(self) -> bool:
        """Check if mixer is currently playing."""
        try:
            return bool(pygame.mixer.music.get_busy())
        except Exception:
            return False

"""Pygame mixer wrapper, high-precision timeline tracking, and playable audio caching."""

import hashlib
import os
import subprocess
import time
from collections import OrderedDict

import pygame

from app.config import CREATE_NO_WINDOW, PREVIEW_CACHE_DIR, ffmpeg_path, log_error

SONG_END_EVENT = (pygame.USEREVENT + 1) if hasattr(pygame, "USEREVENT") else 24


class AudioEngine:
    """Manages audio playback via Pygame mixer with monotonic clock timeline tracking and conversion cache."""

    def __init__(self, max_playable_cache=8, use_volume_curve=False):
        self._playable_cache = OrderedDict()
        self._max_playable_cache = max_playable_cache
        self.play_start_offset = 0.0
        self.play_clock_origin = None
        self._master_volume = 0.8
        self._current_gain_factor = 1.0
        self.auto_level_enabled = False
        self.use_volume_curve = use_volume_curve
        self.buffer_samples = 2048
        self._init_mixer()

    def _init_mixer(self):
        """Single mixer pre_init to avoid audio driver deadlocks on Windows."""
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.pre_init(44100, -16, 2, 2048)
                pygame.mixer.init()
                pygame.mixer.music.set_volume(0.8)
                self.set_endevent(SONG_END_EVENT)
        except Exception as e:
            log_error(f"AudioEngine._init_mixer: {e}")

    def release_audio_file(self):
        """Safely stops and unloads pygame mixer handles to prevent Windows file locking."""
        try:
            pygame.mixer.music.stop()
            if hasattr(pygame.mixer.music, "unload"):
                pygame.mixer.music.unload()
        except Exception:
            pass

    def get_playable_audio_path(self, filepath):
        """Returns a path directly playable by pygame.mixer.music, converting unsupported formats (e.g. M4A) to cached WAV if needed."""
        if not filepath or not os.path.exists(filepath):
            return filepath
        ext = os.path.splitext(filepath)[1].lower()
        if ext in (".mp3", ".wav", ".ogg", ".flac"):
            return filepath
        if filepath in self._playable_cache:
            cached = self._playable_cache[filepath]
            if cached and os.path.exists(cached) and os.path.getsize(cached) > 0:
                self._playable_cache.move_to_end(filepath)
                return cached
        try:
            os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
            file_hash = hashlib.md5(
                os.path.abspath(filepath).encode("utf-8", errors="ignore"), usedforsecurity=False
            ).hexdigest()
            mtime = int(os.path.getmtime(filepath))
            cached_wav = os.path.join(PREVIEW_CACHE_DIR, f"{file_hash}_{mtime}.wav")
            if os.path.exists(cached_wav) and os.path.getsize(cached_wav) > 0:
                while len(self._playable_cache) >= self._max_playable_cache:
                    _k, old_path = self._playable_cache.popitem(last=False)
                    if old_path and os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except Exception:
                            pass
                self._playable_cache[filepath] = cached_wav
                return cached_wav

            cmd = [
                ffmpeg_path,
                "-nostdin",
                "-y",
                "-i",
                filepath,
                "-ar",
                "44100",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                cached_wav,
            ]
            res = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=30
            )
            if res.returncode == 0 and os.path.exists(cached_wav) and os.path.getsize(cached_wav) > 0:
                while len(self._playable_cache) >= self._max_playable_cache:
                    _k, old_path = self._playable_cache.popitem(last=False)
                    if old_path and os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except Exception:
                            pass
                self._playable_cache[filepath] = cached_wav
                return cached_wav
        except Exception as e:
            log_error(f"AudioEngine.get_playable_audio_path: {e}")
        return filepath

    def start_clock(self, start_pos):
        """Set the reference origin for playback timeline tracking."""
        self.play_start_offset = float(start_pos)
        self.play_clock_origin = time.monotonic()

    def pause_clock(self):
        """Record elapsed playback time and pause clock origin."""
        self.play_start_offset = self.current_play_seconds()
        self.play_clock_origin = None

    def seek_clock(self, new_pos, is_playing=True):
        """Update clock after scrubbing or seeking."""
        self.play_start_offset = float(new_pos)
        if is_playing:
            self.play_clock_origin = time.monotonic()
        else:
            self.play_clock_origin = None

    def current_play_seconds(self):
        """Calculate elapsed seconds using monotonic clock offsets instead of drifting get_pos()."""
        if self.play_clock_origin is not None:
            return self.play_start_offset + (time.monotonic() - self.play_clock_origin)
        return self.play_start_offset

    def load_and_play(self, filepath, start_sec=0.0):
        """Load and start playback from start_sec."""
        playable = self.get_playable_audio_path(filepath)
        pygame.mixer.music.load(playable)
        pygame.mixer.music.play(start=start_sec)
        self.start_clock(start_sec)

    def pause(self):
        """Pause playback."""
        self.pause_clock()
        pygame.mixer.music.pause()

    def unpause(self):
        """Unpause playback."""
        pygame.mixer.music.unpause()
        self.start_clock(self.play_start_offset)

    def stop(self):
        """Stop playback and reset clock origin."""
        self.release_audio_file()
        self.play_clock_origin = None

    def set_endevent(self, event_id=SONG_END_EVENT):
        """Register an event ID to be posted when music stream finishes."""
        try:
            pygame.mixer.music.set_endevent(event_id)
        except Exception:
            pass

    def clear_endevent(self):
        """Clear the registered music end event."""
        try:
            pygame.mixer.music.set_endevent()
        except Exception:
            pass

    @property
    def volume(self):
        return self._master_volume

    @property
    def effective_volume(self):
        scaled = (self._master_volume**2) if getattr(self, "use_volume_curve", False) else self._master_volume
        return min(1.0, scaled * (self._current_gain_factor if self.auto_level_enabled else 1.0))

    def set_volume(self, vol, use_curve=None):
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

    def set_auto_level(self, enabled):
        """Toggle player-side loudness leveling during playback."""
        self.auto_level_enabled = bool(enabled)
        self.set_volume(self._master_volume)

    def set_track_gain(self, gain_factor):
        """Apply dynamic gain scaling factor for current track when auto-level is enabled."""
        self._current_gain_factor = max(0.1, min(2.0, float(gain_factor)))
        if self.auto_level_enabled:
            self.set_volume(self._master_volume)

    def is_busy(self):
        """Check if mixer is currently playing."""
        try:
            return pygame.mixer.music.get_busy()
        except Exception:
            return False

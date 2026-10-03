"""Playback controller managing audio transport, clock tracking, native end events, and auto-leveling."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence

import pygame

from app.config import ffmpeg_path, log_error
from app.core.audio_engine import AudioEngine
from app.services.clipper import create_audition_slice

logger = logging.getLogger(__name__)

# A seek during a clip preview lands at least this far before the clip's end, so it still plays.
_CLIP_SEEK_END_MARGIN_SEC = 0.05


class PlaybackController:
    """Manages audio playback transport, timeline synchronization, volume, and audition clips."""

    def __init__(self, app: object, audio_engine: AudioEngine) -> None:
        self.app = app
        self.audio_engine = audio_engine
        if self.audio_engine:
            self.audio_engine.use_volume_curve = True
        self.is_playing_main = False
        self.is_playing_playlist = False
        self.is_paused = False
        self.previewing_clip = False
        self.loop_preview = False
        self.clip_start_time = 0.0
        self.clip_end_time = 0.0
        self.current_preview_filepath: str | None = None
        self.play_guard_until = 0.0
        # True when the mixer no longer holds the paused track at the paused position (the user
        # scrubbed, or another player such as the search preview used the shared pygame mixer).
        self._scrubbed_while_paused = False
        self.paused_position = 0.0
        self._audition_slice_file: str | None = None
        self._is_audition_slice = False
        self._unmuted_volume = 80.0

    def set_volume(self, vol: float, use_curve: bool = True) -> None:
        """Set volume on audio engine with perceptual curve."""
        if self.audio_engine:
            self.audio_engine.set_volume(vol, use_curve=use_curve)

    def play_track(self, filepath: str, start_sec: float = 0.0, is_playlist: bool = False) -> None:
        """Start playback of a track from start_sec."""
        self.stop(user=False)
        self.audio_engine.load_and_play(filepath, start_sec)
        self.audio_engine.start_clock(start_sec)
        self.play_guard_until = time.monotonic() + 0.45
        self.is_paused = False
        self._scrubbed_while_paused = False
        if is_playlist:
            self.is_playing_playlist = True
            self.is_playing_main = False
        else:
            self.is_playing_main = True
            self.is_playing_playlist = False

    def pause(self) -> None:
        """Pause playback or resume if already paused."""
        if self.is_paused:
            self.unpause()
            return
        if not (self.is_playing_main or self.is_playing_playlist):
            return
        self.audio_engine.pause()
        self.paused_position = self.audio_engine.play_start_offset
        self.is_paused = True
        self._scrubbed_while_paused = False

    def unpause(self, current_track_path: str | None = None) -> None:
        """Resume playback from paused position."""
        if not self.is_paused:
            return
        if not self._scrubbed_while_paused:
            try:
                self.audio_engine.unpause()
                self.is_paused = False
                self.play_guard_until = time.monotonic() + 0.45
                return
            except pygame.error as err:
                logger.info("Unpause failed (%s); reloading at the paused position", err)
        # If scrubbed while paused, the mixer was borrowed, or unpause failed: reload from saved offset
        target_pos = self.paused_position
        if self._playing_audition_slice() and self._audition_slice_file:
            # Reloading the song itself would drop the preview's boost and fade.
            pygame.mixer.music.load(self._audition_slice_file)
            pygame.mixer.music.play(start=target_pos - self.clip_start_time)
            self.audio_engine.start_clock(target_pos)
        elif current_track_path:
            self.audio_engine.load_and_play(current_track_path, target_pos)
            self.audio_engine.start_clock(target_pos)
        self.is_paused = False
        self._scrubbed_while_paused = False
        self.play_guard_until = time.monotonic() + 0.45

    def stop(self, user: bool = False) -> None:
        """Stop playback and cleanup any temporary audition slice."""
        self.cleanup_audition_slice()
        self.audio_engine.stop()
        self.is_playing_main = False
        self.is_playing_playlist = False
        self.is_paused = False
        self.previewing_clip = False
        self._scrubbed_while_paused = False
        if user:
            self.audio_engine.play_start_offset = 0.0

    def _playing_audition_slice(self) -> bool:
        """True while Test Clip plays a rendered slice (boost or fade), which holds only the clip."""
        return self.previewing_clip and self._is_audition_slice and bool(self._audition_slice_file)

    def seek(self, seconds: float, track_duration: float = 0.0) -> float:
        """Seek playback to ``seconds`` into the song; returns the position it actually went to.

        While Test Clip plays a rendered slice, the slice starts at the clip's start, so the song
        position is moved into the clip and made relative to it. Seeking the slice to a song
        position past its short length used to stop the preview.
        """
        seek_sec = max(0.0, min(track_duration or seconds, seconds))
        in_slice = self._playing_audition_slice()
        if in_slice:
            last = max(self.clip_start_time, self.clip_end_time - _CLIP_SEEK_END_MARGIN_SEC)
            seek_sec = max(self.clip_start_time, min(last, seek_sec))
        file_pos = seek_sec - self.clip_start_time if in_slice else seek_sec
        if (self.is_playing_main or self.is_playing_playlist) and not self.is_paused:
            try:
                pygame.mixer.music.play(start=file_pos)
                self.audio_engine.start_clock(seek_sec)
                self.play_guard_until = time.monotonic() + 0.45
            except pygame.error as err:
                logger.info("Seeking with play(start=...) failed (%s); trying set_pos", err)
                try:
                    pygame.mixer.music.rewind()
                    pygame.mixer.music.set_pos(file_pos)
                    self.audio_engine.start_clock(seek_sec)
                except pygame.error as err2:
                    logger.warning("This audio cannot be sought to %.1f s: %s", seek_sec, err2)
        else:
            self.audio_engine.seek_clock(seek_sec, is_playing=False)
            if self.is_paused:
                self.paused_position = seek_sec
                self._scrubbed_while_paused = True
        return seek_sec

    def mark_mixer_taken(self) -> None:
        """Record that another player used the shared mixer, so resuming must reload the track."""
        if self.is_paused:
            self._scrubbed_while_paused = True

    def skip_by(self, delta_seconds: float, track_duration: float = 0.0) -> float:
        """Skip playback position by delta_seconds (+10s or -10s)."""
        curr = self.current_play_seconds()
        new_pos = max(0.0, min(track_duration, curr + delta_seconds))
        return self.seek(new_pos, track_duration)

    def current_play_seconds(self) -> float:
        """Get elapsed playback position from high-precision monotonic clock."""
        return self.audio_engine.current_play_seconds()

    def test_clip(
        self,
        filepath: str,
        s_time: float,
        e_time: float,
        gain_db: float = 0.0,
        soften: bool = False,
        fade_sec: float = 1.5,
        loop: bool = False,
    ) -> None:
        """Audition a clipped section with gain boost, soft limiter, and smooth fade."""
        preview_path = self.prepare_audition(filepath, s_time, e_time, gain_db, soften, fade_sec)
        self.start_audition(filepath, s_time, e_time, preview_path, loop=loop)

    @staticmethod
    def prepare_audition(
        filepath: str, s_time: float, e_time: float, gain_db: float = 0.0, soften: bool = False, fade_sec: float = 1.5
    ) -> str | None:
        """Render the boosted/faded preview slice with FFmpeg (safe to call from a worker thread).

        Returns the temporary WAV path, or None when no processing is needed or rendering failed.
        """
        if (abs(gain_db) > 0.05 or soften) and os.path.exists(ffmpeg_path):
            return create_audition_slice(filepath, s_time, e_time, gain_db=gain_db, soften=soften, fade_sec=fade_sec)
        return None

    @staticmethod
    def discard_audition(preview_path: str | None) -> None:
        """Delete a rendered preview slice that will not be played."""
        if preview_path and os.path.exists(preview_path):
            try:
                os.remove(preview_path)
            except OSError:
                pass

    def start_audition(
        self, filepath: str, s_time: float, e_time: float, preview_path: str | None = None, loop: bool = False
    ) -> None:
        """Start clip playback on the UI thread, from a prepared slice or directly from the track."""
        self.stop(user=False)
        self.loop_preview = bool(loop)
        self.clip_start_time = s_time
        self.clip_end_time = e_time
        self.current_preview_filepath = filepath

        if preview_path:
            pygame.mixer.music.load(preview_path)
            pygame.mixer.music.play()
            self._is_audition_slice = True
            self._audition_slice_file = preview_path
        else:
            self._is_audition_slice = False
            self._audition_slice_file = None
            self.audio_engine.load_and_play(filepath, s_time)

        self.audio_engine.start_clock(s_time)
        self.is_playing_main = True
        self.is_playing_playlist = False
        self.previewing_clip = True
        self.clip_end_time = e_time
        self.play_guard_until = time.monotonic() + 0.45

    def restart_clip_loop(self) -> bool:
        """Seamlessly loop back to clip start if loop preview mode is active."""
        if not self.previewing_clip or not self.loop_preview:
            return False
        try:
            if self._is_audition_slice and self._audition_slice_file and os.path.exists(self._audition_slice_file):
                pygame.mixer.music.play()
                self.audio_engine.start_clock(self.clip_start_time)
                self.play_guard_until = time.monotonic() + 0.35
                return True
            elif self.current_preview_filepath:
                self.audio_engine.load_and_play(self.current_preview_filepath, self.clip_start_time)
                self.audio_engine.start_clock(self.clip_start_time)
                self.play_guard_until = time.monotonic() + 0.35
                return True
        except Exception as e:
            log_error(f"restart_clip_loop: {e}")
        return False

    def cleanup_audition_slice(self) -> None:
        """Remove any temporary audition WAV slice."""
        if self._audition_slice_file:
            f = self._audition_slice_file
            self._audition_slice_file = None
            self._is_audition_slice = False
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass

    # Gated-RMS level most tracks are pulled toward. Chosen low enough that loud modern masters are
    # turned down while quiet older recordings can still be raised within the mixer's headroom.
    AUTO_LEVEL_TARGET_DB = -16.0
    AUTO_LEVEL_MAX_CUT_DB = -12.0
    AUTO_LEVEL_MAX_BOOST_DB = 6.0

    def compute_auto_level_gain(self, loudness_db: float | None) -> float:
        """Return the linear gain factor that moves a track's measured loudness toward the target."""
        if loudness_db is None:
            return 1.0
        try:
            gain_db = self.AUTO_LEVEL_TARGET_DB - float(loudness_db)
        except (TypeError, ValueError):
            return 1.0
        gain_db = max(self.AUTO_LEVEL_MAX_CUT_DB, min(self.AUTO_LEVEL_MAX_BOOST_DB, gain_db))
        return 10 ** (gain_db / 20.0)

    def update_auto_level(self, loudness_db: float | None) -> None:
        """Apply auto-level gain for the current track (unity gain when disabled or unmeasured)."""
        if not self.audio_engine.auto_level_enabled or loudness_db is None:
            self.audio_engine.set_track_gain(1.0)
            return
        self.audio_engine.set_track_gain(self.compute_auto_level_gain(loudness_db))

    @property
    def play_start_offset(self) -> float:
        return self.audio_engine.play_start_offset

    @play_start_offset.setter
    def play_start_offset(self, val: float) -> None:
        self.audio_engine.play_start_offset = float(val)

    @property
    def play_clock_origin(self) -> float | None:
        return self.audio_engine.play_clock_origin

    @play_clock_origin.setter
    def play_clock_origin(self, val: float | None) -> None:
        self.audio_engine.play_clock_origin = val

    @staticmethod
    def nudge_start(delta: float, clip_start: float, clip_end: float) -> float:
        """Calculate and return new clip start clamped between 0 and clip_end - 0.05."""
        return max(0.0, min(float(clip_end) - 0.05, float(clip_start) + float(delta)))

    @staticmethod
    def nudge_end(delta: float, clip_start: float, clip_end: float, track_duration: float = 999999.0) -> float:
        """Calculate and return new clip end clamped between clip_start + 0.05 and track_duration."""
        max_limit = float(track_duration) if track_duration > 0 else 999999.0
        return max(float(clip_start) + 0.05, min(max_limit, float(clip_end) + float(delta)))

    def calculate_vu_level(self, current_seconds: float, duration: float, peaks: Sequence[float]) -> int:
        """Calculate 0 to 5 LED VU activity level based on current peak amplitude and playback state."""
        if not (self.is_playing_main or self.is_playing_playlist) or self.is_paused:
            return 0
        if not peaks or duration <= 0:
            t = int(time.time() * 8) % 4
            return 2 if t in (0, 2) else 3
        try:
            n = len(peaks)
            idx = int((current_seconds / max(0.01, duration)) * n)
            idx = max(0, min(n - 1, idx))
            val = peaks[idx]
            if val <= 0.04:
                return 1
            elif val <= 0.25:
                return 2
            elif val <= 0.50:
                return 3
            elif val <= 0.75:
                return 4
            else:
                return 5
        except Exception:
            return 2

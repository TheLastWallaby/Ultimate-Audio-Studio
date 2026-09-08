"""Playback controller managing audio transport, clock tracking, native end events, and auto-leveling."""

import os
import time
import pygame
from app.config import format_time, ffmpeg_path, log_error
from app.core.audio_engine import SONG_END_EVENT
from app.services.clipper import create_audition_slice


class PlaybackController:
    """Manages audio playback transport, timeline synchronization, volume, and audition clips."""

    def __init__(self, app, audio_engine):
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
        self.current_preview_filepath = None
        self.play_guard_until = 0.0
        self._scrubbed_while_paused = False
        self._audition_slice_file = None
        self._is_audition_slice = False
        self._unmuted_volume = 80.0

    def set_volume(self, vol, use_curve=True):
        """Set volume on audio engine with perceptual curve."""
        if self.audio_engine:
            self.audio_engine.set_volume(vol, use_curve=use_curve)

    def play_track(self, filepath, start_sec=0.0, is_playlist=False):
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

    def pause(self):
        """Pause playback or resume if already paused."""
        if self.is_paused:
            return self.unpause()
        if not (self.is_playing_main or self.is_playing_playlist):
            return
        self.audio_engine.pause()
        self.is_paused = True
        self._scrubbed_while_paused = False

    def unpause(self, current_track_path=None):
        """Resume playback from paused position."""
        if not self.is_paused:
            return
        if not self._scrubbed_while_paused:
            try:
                self.audio_engine.unpause()
                self.is_paused = False
                self.play_guard_until = time.monotonic() + 0.45
                return
            except Exception:
                pass
        # If scrubbed while paused or unpause failed, reload from saved offset
        target_pos = self.audio_engine.play_start_offset
        if current_track_path:
            self.audio_engine.load_and_play(current_track_path, target_pos)
            self.audio_engine.start_clock(target_pos)
        self.is_paused = False
        self._scrubbed_while_paused = False
        self.play_guard_until = time.monotonic() + 0.45

    def stop(self, user=False):
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

    def seek(self, seconds, track_duration=0.0):
        """Seek playback to specified seconds position."""
        seek_sec = max(0.0, min(track_duration or seconds, seconds))
        if (self.is_playing_main or self.is_playing_playlist) and not self.is_paused:
            try:
                pygame.mixer.music.play(start=seek_sec)
                self.audio_engine.start_clock(seek_sec)
                self.play_guard_until = time.monotonic() + 0.45
            except Exception:
                try:
                    pygame.mixer.music.rewind()
                    pygame.mixer.music.set_pos(seek_sec)
                    self.audio_engine.start_clock(seek_sec)
                except Exception:
                    pass
        else:
            self.audio_engine.seek_clock(seek_sec, is_playing=False)
            if self.is_paused:
                self._scrubbed_while_paused = True

    def skip_by(self, delta_seconds, track_duration=0.0):
        """Skip playback position by delta_seconds (+10s or -10s)."""
        curr = self.current_play_seconds()
        new_pos = max(0.0, min(track_duration, curr + delta_seconds))
        self.seek(new_pos, track_duration)
        return new_pos

    def current_play_seconds(self):
        """Get elapsed playback position from high-precision monotonic clock."""
        return self.audio_engine.current_play_seconds()

    def check_native_end_event(self):
        """Check Pygame event queue for SONG_END_EVENT fired by mixer."""
        try:
            for event in pygame.event.get():
                if event.type == SONG_END_EVENT:
                    return True
        except Exception:
            pass
        return False

    def test_clip(self, filepath, s_time, e_time, gain_db=0.0, soften=False, fade_sec=1.5, loop=False):
        """Audition a clipped section with gain boost, soft limiter, and smooth fade."""
        self.stop(user=False)
        self.loop_preview = bool(loop)
        self.clip_start_time = s_time
        self.clip_end_time = e_time
        self.current_preview_filepath = filepath
        needs_slice = (abs(gain_db) > 0.05 or soften)
        preview_path = None
        if needs_slice and os.path.exists(ffmpeg_path):
            preview_path = create_audition_slice(filepath, s_time, e_time, gain_db=gain_db, soften=soften, fade_sec=fade_sec)

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

    def restart_clip_loop(self):
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

    def cleanup_audition_slice(self):
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

    def compute_auto_level_gain(self, peaks):
        """Compute auto-level gain factor from peaks."""
        if not peaks:
            return 1.0
        try:
            max_p = max(peaks) if peaks else 0.0
            if max_p > 0.05:
                return max(0.35, min(1.35, 0.70 / max_p))
        except Exception:
            pass
        return 1.0

    def update_auto_level_for_peaks(self, peaks):
        """Update audio engine track gain from peaks."""
        self.apply_track_auto_level(peaks)

    def apply_track_auto_level(self, peaks):
        """Adjust track gain factor based on peak amplitude to balance playback loudness."""
        if not self.audio_engine.auto_level_enabled or not peaks:
            self.audio_engine.set_track_gain(1.0)
            return
        factor = self.compute_auto_level_gain(peaks)
        self.audio_engine.set_track_gain(factor)

    @property
    def play_start_offset(self):
        return self.audio_engine.play_start_offset

    @play_start_offset.setter
    def play_start_offset(self, val):
        self.audio_engine.play_start_offset = float(val)

    @property
    def play_clock_origin(self):
        return self.audio_engine.play_clock_origin

    @play_clock_origin.setter
    def play_clock_origin(self, val):
        self.audio_engine.play_clock_origin = val

    @staticmethod
    def nudge_start(delta, clip_start, clip_end):
        """Calculate and return new clip start clamped between 0 and clip_end - 0.05."""
        return max(0.0, min(float(clip_end) - 0.05, float(clip_start) + float(delta)))

    @staticmethod
    def nudge_end(delta, clip_start, clip_end, track_duration=999999.0):
        """Calculate and return new clip end clamped between clip_start + 0.05 and track_duration."""
        max_limit = float(track_duration) if track_duration > 0 else 999999.0
        return max(float(clip_start) + 0.05, min(max_limit, float(clip_end) + float(delta)))

    def calculate_vu_level(self, current_seconds, duration, peaks):
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

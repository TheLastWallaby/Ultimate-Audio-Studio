"""Step 2 transport: track loading, waveform/art workers, play/pause/stop, seeking."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import subprocess
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path

import pygame

from app.config import COVER_CACHE_DIR, format_time, log_error
from app.core.cache_manager import cache_mgr
from app.core.metadata import extract_album_art, read_track_metadata
from app.core.task_manager import task_mgr
from app.core.waveform import analyze_audio
from app.ui import dialogs
from app.ui.components import draw_placeholder_cover
from app.ui.error_dialog import show_friendly_error
from app.ui.features.base import AppBase
from app.ui.theme import BORDER_MAIN, COLOR_ACCENT, COLOR_PAUSE, COLOR_PLAY

logger = logging.getLogger(__name__)

MONITOR_INTERVAL_MS = 40
# After a failed tick the monitor slows down, so a fault that keeps happening cannot flood the log.
MONITOR_RETRY_MS = 1000
# The mixer says when a song has ended. The clock is only a fallback for a mixer that never does,
# and only for a song whose length is known: an unknown length is 0, which the clock has always passed.
END_OF_SONG_GRACE_SEC = 2.0
# PLAY this close to the end of a song (it finished, or the slider was dragged there) starts it again.
REPLAY_FROM_START_SEC = 0.5


class PlayerMixin(AppBase):
    """Step 2 transport: track loading, waveform/art workers, play/pause/stop, seeking."""

    def _release_audio_file(self) -> None:
        """Safely stops and unloads pygame mixer handles to prevent Windows file locking."""
        self.audio_engine.release_audio_file()

    def _draw_placeholder_cover(self) -> None:
        if hasattr(self, "canvas_cover"):
            draw_placeholder_cover(self.canvas_cover)

    def _start_worker_threads(self) -> None:
        threading.Thread(target=self._waveform_worker_loop, daemon=True).start()
        threading.Thread(target=self._art_worker_loop, daemon=True).start()

    def _waveform_worker_loop(self) -> None:
        while True:
            try:
                item = self._waveform_queue.get()
                if item is None or getattr(self, "_is_shutting_down", False):
                    self._waveform_queue.task_done()
                    break
                if len(item) == 3:
                    filepath, req_id, cancel_evt = item
                else:
                    filepath, req_id = item
                    cancel_evt = None
                if (
                    req_id != self._waveform_req_id
                    or getattr(self, "_is_shutting_down", False)
                    or (cancel_evt and cancel_evt.is_set())
                ):
                    self._waveform_queue.task_done()
                    continue

                def _on_spawn(p: subprocess.Popen[bytes]) -> None:
                    self._active_waveform_proc = p

                analysis = analyze_audio(filepath, n_bars=220, cancel_event=cancel_evt, on_process_spawned=_on_spawn)
                peaks, loudness = analysis.peaks, analysis.loudness_db
                self._active_waveform_proc = None

                if cancel_evt and cancel_evt.is_set():
                    self._waveform_queue.task_done()
                    continue

                if peaks and any(val > 0.001 for val in peaks):
                    cache_mgr.set_peaks(filepath, peaks)
                if loudness is not None:
                    cache_mgr.set_loudness(filepath, loudness)
                if req_id == self._waveform_req_id and not getattr(self, "_is_shutting_down", False):

                    def on_done(
                        p: list[float] = peaks, f: str = filepath, r: int = req_id, ld: float | None = loudness
                    ) -> None:
                        if r == self._waveform_req_id and not getattr(self, "_is_shutting_down", False):
                            self._waveform_loading = False
                            if self.selected_file_path == f:
                                self._current_peaks = p if (p and any(val > 0.001 for val in p)) else []
                                self._current_loudness = ld
                                if hasattr(self, "playback_ctrl"):
                                    self.playback_ctrl.update_auto_level(ld)
                                self._render_waveform(full_redraw=True)

                    self._safe_after(0, on_done)
                self._waveform_queue.task_done()
            except Exception as e:
                self._active_waveform_proc = None
                log_error(f"_waveform_worker_loop: {e}")

    def _art_worker_loop(self) -> None:
        while True:
            try:
                item = self._art_queue.get()
                if item is None or getattr(self, "_is_shutting_down", False):
                    self._art_queue.task_done()
                    break
                filepath, req_id, size = item
                if req_id != self._art_req_id or getattr(self, "_is_shutting_down", False):
                    self._art_queue.task_done()
                    continue

                file_hash = hashlib.md5(
                    os.path.abspath(filepath).encode("utf-8", errors="ignore"), usedforsecurity=False
                ).hexdigest()
                out_png = os.path.join(COVER_CACHE_DIR, f"{file_hash}_{size}_art.png")
                success = extract_album_art(filepath, out_png, size)

                def on_art_done(s: bool = success, out: str = out_png, f: str = filepath, r: int = req_id) -> None:
                    if r != self._art_req_id or getattr(self, "_is_shutting_down", False):
                        return
                    img = None
                    if s and os.path.exists(out):
                        try:
                            img = tk.PhotoImage(file=out)
                        except Exception:
                            img = None
                    while len(self._art_cache) >= self._max_art_cache:
                        self._art_cache.popitem(last=False)
                    self._art_cache[f] = img
                    if self.selected_file_path == f:
                        if img:
                            self._current_cover_img = img
                            self.canvas_cover.delete("all")
                            self.canvas_cover.create_image(0, 0, anchor="nw", image=img)
                        else:
                            self._draw_placeholder_cover()

                self._safe_after(0, on_art_done)
                self._art_queue.task_done()
            except Exception as e:
                log_error(f"_art_worker_loop: {e}")

    def _load_album_art(self, filepath: str) -> None:
        self._art_req_id += 1
        current_req = self._art_req_id

        if filepath in self._art_cache:
            img = self._art_cache[filepath]
            self._art_cache.move_to_end(filepath)
            if img:
                self._current_cover_img = img
                self.canvas_cover.delete("all")
                self.canvas_cover.create_image(0, 0, anchor="nw", image=img)
                return
            else:
                self._draw_placeholder_cover()
                return

        self._art_queue.put((filepath, current_req, int(self.canvas_cover.cget("width"))))

    def _load_waveform(self, filepath: str) -> None:
        self._waveform_req_id += 1
        current_req = self._waveform_req_id

        # Eagerly cancel any active FFmpeg extraction
        if self._active_waveform_proc:
            try:
                self._active_waveform_proc.terminate()
            except Exception:
                pass
            self._active_waveform_proc = None
        self._active_waveform_cancel.set()
        self._active_waveform_cancel = threading.Event()

        # Drain obsolete items from queue
        while not self._waveform_queue.empty():
            try:
                self._waveform_queue.get_nowait()
                self._waveform_queue.task_done()
            except Exception:
                break

        # Check persistent cache first (entries from older versions lack loudness: re-analyze those)
        disk_peaks = cache_mgr.get_peaks(filepath)
        disk_loudness = cache_mgr.get_loudness(filepath)
        if disk_peaks and any(val > 0.001 for val in disk_peaks) and disk_loudness is not None:
            self._current_peaks = disk_peaks
            self._current_loudness = disk_loudness
            self._waveform_loading = False
            if hasattr(self, "playback_ctrl"):
                self.playback_ctrl.update_auto_level(disk_loudness)
            self._render_waveform(full_redraw=True)
            return

        self._current_peaks = []
        self._current_loudness = None
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.update_auto_level(None)
        self._waveform_loading = True
        self._render_waveform(full_redraw=True)
        self._waveform_queue.put((filepath, current_req, self._active_waveform_cancel))

    def toggle_waveform_zoom(self) -> None:
        if hasattr(self, "waveform_view"):
            self.waveform_view.toggle_zoom()

    def _render_waveform(self, full_redraw: bool = False) -> None:
        if hasattr(self, "waveform_view"):
            self.waveform_view.render(full_redraw=full_redraw)

    def _prog_label(self, current: float) -> str:
        return f"{format_time(current)} / {format_time(self.track_duration)}"

    def skip_by(self, seconds: float) -> None:
        if not self.selected_file_path:
            return
        new_pos = self.playback_ctrl.skip_by(seconds, self.track_duration)
        self._updating_ui = True
        self.scale_progress.set(new_pos)
        self.lbl_prog_time.config(text=self._prog_label(new_pos))
        self._updating_ui = False
        self._render_waveform()
        self.play_start_offset = new_pos
        if self.is_paused:
            self._scrubbed_while_paused = True

    def on_volume_change(self, val: str | float) -> None:
        v = float(val)
        self.audio_engine.set_volume(v / 100.0)
        if hasattr(self, "lbl_vol_pct") and self.lbl_vol_pct.winfo_exists():
            self.lbl_vol_pct.config(text=f"{int(v)}%")
        if hasattr(self, "btn_mute") and self.btn_mute.winfo_exists():
            self.btn_mute.config(text="🔇" if v <= 0.01 else "🔊")
        if v > 0.01:
            self._unmuted_volume = v
        self._schedule_settings_save()

    @property
    def is_muted(self) -> bool:
        if hasattr(self, "scale_volume") and self.scale_volume.winfo_exists():
            return float(self.scale_volume.get()) <= 0.01
        return False

    def toggle_mute(self) -> None:
        if not hasattr(self, "scale_volume"):
            return
        curr = float(self.scale_volume.get())
        if curr > 0.01:
            self._unmuted_volume = curr
            self.scale_volume.set(0)
            self.on_volume_change(0)
            self.set_status("Audio muted.", icon="🔇")
        else:
            restore = getattr(self, "_unmuted_volume", 80)
            if restore <= 0.01:
                restore = 80
            self.scale_volume.set(restore)
            self.on_volume_change(restore)
            self.set_status(f"Audio unmuted ({int(restore)}%).", icon="🔊")

    def on_toggle_auto_level(self) -> None:
        enabled = bool(self.auto_level_playback.get())
        self.audio_engine.set_auto_level(enabled)
        if enabled:
            if hasattr(self, "playback_ctrl"):
                self.playback_ctrl.update_auto_level(self._current_loudness)
            self.set_status(
                "Auto-level playback enabled: loud and quiet songs will play at a similar volume.", icon="🔊"
            )
        else:
            self.audio_engine.set_track_gain(1.0)
            self.set_status("Auto-level playback disabled.", icon="ℹ️")
        self._schedule_settings_save()

    def _current_play_seconds(self) -> float:
        return float(min(self.track_duration or 10**9, self.audio_engine.current_play_seconds()))

    def _start_clock(self, start_pos: float) -> None:
        self.play_start_offset = start_pos
        self.audio_engine.start_clock(start_pos)
        self.play_clock_origin = self.audio_engine.play_clock_origin
        self.play_guard_until = time.monotonic() + 0.45
        self.is_paused = False

    def _when_playable(
        self, path: str, start_fn: Callable[[], None], busy_text: str = "Preparing this song for playback..."
    ) -> None:
        """Run start_fn once path can be played, converting (e.g. M4A -> WAV) on a worker if needed.

        The conversion can take many seconds, so it never runs on the Tkinter thread. If the user
        stops or picks another song meanwhile, the stale start is dropped. When the conversion
        fails, the user is told and start_fn is not run.
        """
        if not self.audio_engine.needs_conversion(path):
            self._pending_play_token = None
            start_fn()
            return
        token = object()
        self._pending_play_token = token
        self.set_busy(True, busy_text)

        def _worker() -> None:
            prepared = self.audio_engine.prepare_for_playback(path)
            self._safe_after(0, _ready, prepared)

        def _ready(prepared: bool) -> None:
            if self._pending_play_token is not token:
                return
            self._pending_play_token = None
            if not prepared:
                # Starting now would repeat the failed conversion on this thread and freeze the window.
                self.set_busy(False, "This song could not be played.")
                show_friendly_error(
                    self.root, f"FFmpeg could not convert '{Path(path).name}' for playback.", "playback"
                )
                return
            self.set_busy(False)
            start_fn()

        task_mgr.submit_task(_worker)

    def _load_track_ui(self, path: str, title: str | None = None) -> bool:
        self.selected_file_path = path
        base_name = title or os.path.basename(path)
        artist_name = "Unknown Artist"

        # Never probe on the UI thread: ffprobe/ffmpeg (for files whose header has no length) can take
        # seconds. Tags come from the fast header read; a missing duration is probed on a worker.
        meta = self._cached_metadata(path, probe=False)
        if not meta.get("duration") and os.path.isfile(path):
            header = read_track_metadata(path, probe_fallback=False)
            if header.duration > 0:
                cache_mgr.set_metadata(path, header)
            meta = header.to_dict()
        dur = float(meta.get("duration") or 0.0)
        if meta.get("title"):
            base_name = meta["title"]
        if meta.get("artist"):
            artist_name = meta["artist"]

        if not os.path.exists(path):
            dialogs.show_warning(self.root, "Error", f"Audio file not found:\n{path}")
            return False

        self.lbl_selected.config(text=base_name)
        self.lbl_selected_artist.config(text=artist_name)
        self.track_duration = dur
        self.clip_start_sec = 0.0
        self.clip_end_sec = dur

        self._updating_ui = True
        self.scale_progress.config(to=dur)
        self.scale_progress.set(0)
        self.lbl_start_time.config(text=f"Start: {format_time(0)}")
        self.lbl_prog_time.config(text=f"{format_time(0)} / {format_time(dur)}")
        self.lbl_end_time.config(text=f"End: {format_time(dur)}")
        self._updating_ui = False
        self._update_clip_length_label()

        self._load_album_art(path)
        self._load_waveform(path)
        self._update_restore_original_button()
        if dur <= 0:
            self._probe_duration_later(path)
        if self.audio_engine.needs_conversion(path):
            # Convert ahead of time so pressing Play is instant.
            task_mgr.submit_task(self.audio_engine.get_playable_audio_path, path)
        return True

    def _probe_duration_later(self, path: str) -> None:
        """Measure a song's length with ffprobe on a worker, then fill in the timeline."""

        def _worker() -> None:
            dur = self._cached_duration(path)
            self._safe_after(0, self._apply_track_duration, path, dur)

        task_mgr.submit_task(_worker)

    def _apply_track_duration(self, path: str, dur: float) -> None:
        if self.selected_file_path != path or dur <= 0 or self.track_duration > 0:
            return
        self.track_duration = dur
        if self.clip_end_sec <= 0:
            self.clip_end_sec = dur
        self._updating_ui = True
        self.scale_progress.config(to=dur)
        self.lbl_prog_time.config(text=self._prog_label(float(self.scale_progress.get())))
        self.lbl_end_time.config(text=f"End: {format_time(self.clip_end_sec)}")
        self._updating_ui = False
        self._update_clip_length_label()
        self._render_waveform(full_redraw=True)

    def _mark_progress_drag(self, dragging: bool) -> None:
        self._progress_dragging = dragging
        if dragging:
            self.root.bind("<ButtonRelease-1>", self._on_progress_release)

    def on_progress_drag(self, val: str | float) -> None:
        if self._updating_ui:
            return
        v = float(val)
        self.lbl_prog_time.config(text=self._prog_label(v))
        self._render_waveform()

    def _on_progress_release(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        try:
            self.root.unbind("<ButtonRelease-1>")
        except Exception:
            pass
        was_dragging = self._progress_dragging
        self._progress_dragging = False
        v = float(self.scale_progress.get())
        self.lbl_prog_time.config(text=self._prog_label(v))
        self._render_waveform()
        if not was_dragging:
            return
        if (self.is_playing_main or self.is_playing_playlist) and not self.is_paused:
            self._seek_playback(v)
        else:
            # Controller records the paused position so Resume continues from the new spot.
            self.playback_ctrl.seek(v, self.track_duration)

    def _seek_playback(self, seconds: float) -> None:
        if not self.selected_file_path:
            return
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.seek(seconds, self.track_duration)
        else:
            seek_sec = max(0.0, min(self.track_duration or seconds, seconds))
            try:
                pygame.mixer.music.play(start=seek_sec)
                self._start_clock(seek_sec)
            except Exception:
                try:
                    pygame.mixer.music.rewind()
                    pygame.mixer.music.set_pos(seek_sec)
                    self._start_clock(seek_sec)
                except Exception:
                    self.set_status("Seeking to exact position not supported for this audio stream.")
        self._render_waveform()

    def _draw_vu_meter(self, level: int = 0) -> None:
        if not hasattr(self, "canvas_vu") or not self.canvas_vu.winfo_exists():
            return
        c = self.canvas_vu
        c.delete("all")
        colors_on = ["#22c55e", "#22c55e", "#22c55e", "#f59e0b", "#ef4444"]
        dim_color = "#cbd5e1"
        k = int(c.cget("width")) / 80.0  # designed at 80x14; the canvas grows with Text Size
        for i in range(5):
            x1 = (2 + i * 16) * k
            x2 = x1 + 12 * k
            color = colors_on[i] if (i < level) else dim_color
            c.create_rectangle(x1, 2 * k, x2, 12 * k, fill=color, outline="", width=0)

    def _set_card_playing_state(self, state: str) -> None:
        if hasattr(self, "f_track_card"):
            if state == "playing":
                self.f_track_card.configure(highlightbackground=COLOR_PLAY, highlightthickness=2)
                self.lbl_selected.config(fg=COLOR_PLAY)
                self.lbl_track_state.config(text="▶ PLAYING", bg=COLOR_PLAY)
            elif state == "paused":
                self.f_track_card.configure(highlightbackground=COLOR_PAUSE, highlightthickness=2)
                self.lbl_selected.config(fg=COLOR_PAUSE)
                self.lbl_track_state.config(text="⏸ PAUSED", bg=COLOR_PAUSE)
                self._draw_vu_meter(0)
            else:
                self.f_track_card.configure(highlightbackground=BORDER_MAIN, highlightthickness=2)
                self.lbl_selected.config(fg=COLOR_ACCENT)
                self.lbl_track_state.config(text="⏹ READY", bg="#64748b")
                self._draw_vu_meter(0)

    def play_main(self) -> None:
        """PLAY: switch to a song clicked meanwhile, resume a paused one, or play from the slider."""
        if self._selection_debounce_timer:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._selection_debounce_timer)
            self._selection_debounce_timer = None
        if self._pending_library_song:
            # A Library song was clicked while this one played: PLAY means "play what I clicked".
            self.stop_audio()
            self._load_pending_selection()
        if not self.selected_file_path:
            dialogs.show_warning(self.root, "No Song", "Click a song in the Library first.")
            return
        if self.is_paused:
            self.pause_audio()
            return
        self.stop_audio()
        path = self.selected_file_path
        start_pos = float(self.scale_progress.get())
        if self.track_duration > 0 and start_pos >= self.track_duration - REPLAY_FROM_START_SEC:
            start_pos = 0.0  # starting at the very end would play nothing

        def _start() -> None:
            if self.selected_file_path != path:
                return
            try:
                self.playback_ctrl.play_track(path, start_pos, is_playlist=False)
            except (pygame.error, OSError) as err:
                logger.error("Could not play %s: %s", Path(path).name, err)
                show_friendly_error(self.root, err, "playback")
                return
            self._set_card_playing_state("playing")
            self.set_status(f"Playing: {Path(path).name}", icon="▶")

        self._when_playable(path, _start)

    def stop_pressed(self) -> None:
        """STOP button: stop, go back to the start, and load a Library song that was clicked meanwhile."""
        self.stop_audio(user=True)
        if self._load_pending_selection() and self.selected_file_path:
            self.set_status(
                f"Stopped. '{Path(self.selected_file_path).name}' is ready: press PLAY to listen.", icon="⏹"
            )

    def pause_audio(self) -> None:
        if not self.selected_file_path:
            return
        if self.is_paused:
            try:
                if hasattr(self, "playback_ctrl"):
                    self.playback_ctrl.unpause(self.selected_file_path)
                else:
                    self.audio_engine.unpause()
                    self.is_paused = False
                self._set_card_playing_state("playing")
                self.set_status(f"Playing: {os.path.basename(self.selected_file_path)}", icon="▶")
            except Exception as e:
                log_error(f"resume: {e}")
                show_friendly_error(self.root, e, "playback")
            return
        if not (self.is_playing_main or self.is_playing_playlist):
            return
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.pause()
        else:
            self.audio_engine.pause()
            self.is_paused = True
        self._set_card_playing_state("paused")
        self.set_status("Paused. Press PLAY to continue.", icon="⏸")

    def stop_audio(self, user: bool = False) -> None:
        """Stop playback (``user``: the STOP button, which also returns the slider to the start)."""
        self._cancel_pending_skip()
        if self._pending_play_token is not None:
            self._pending_play_token = None
            self.set_busy(False)
        # The controller owns playback state: it stops the mixer (releasing the file), clears the
        # playing/paused/preview flags and the clock, and deletes any Test Clip preview slice.
        self.playback_ctrl.stop(user=user)
        self._set_card_playing_state("stopped")
        if user:
            self._updating_ui = True
            self.scale_progress.set(0)
            self.lbl_prog_time.config(text=f"00:00 / {format_time(self.track_duration)}")
            self._updating_ui = False
            self.set_status("Stopped.", icon="⏹")
            self.refresh_playlist_listbox()
        self._render_waveform()

    def _finish_clip_preview(self) -> None:
        """A Test Clip preview reached its end: loop it when Loop is ticked, otherwise stop."""
        if self.loop_clip.get():
            self._restart_clip_loop()
            return
        self.stop_audio(user=False)
        self.set_status("Finished previewing clip.")

    def _song_finished(self) -> None:
        """The audio ran out: loop the clip, play the next playlist song, or go back to the start."""
        if self.is_paused:
            return
        if self.previewing_clip:
            self._finish_clip_preview()
        elif self.is_playing_playlist:
            self.play_next_in_playlist()
        elif self.is_playing_main:
            # Like the STOP button, this returns the slider to the start: PLAY begins at the slider,
            # so leaving it at the end made the next PLAY do nothing.
            self.stop_audio(user=True)
            if self._load_pending_selection() and self.selected_file_path:
                ready = Path(self.selected_file_path).name
                self.set_status(f"Playback finished. '{ready}' is ready: press PLAY to listen.")
            else:
                self.set_status("Playback finished. Press PLAY to hear it again.")

    def _clock_passed_end(self) -> bool:
        """True when the clock is well past the end of a song whose length is known (see END_OF_SONG_GRACE_SEC)."""
        if self.track_duration <= 0:
            return False
        return self.audio_engine.current_play_seconds() >= self.track_duration + END_OF_SONG_GRACE_SEC

    def _monitor_tick(self) -> None:
        """Move the timeline and the level meter, and notice the end of the song or clip preview."""
        if not (self.is_playing_main or self.is_playing_playlist) or self.is_paused:
            return
        curr_pos = self._current_play_seconds()
        if not self._progress_dragging:
            self._updating_ui = True
            self.scale_progress.set(curr_pos)
            self.lbl_prog_time.config(text=self._prog_label(curr_pos))
            self._updating_ui = False
            self._render_waveform()
        self._draw_vu_meter(self.playback_ctrl.calculate_vu_level(curr_pos, self.track_duration, self._current_peaks))

        if self.previewing_clip and curr_pos >= self.clip_end_time - 0.05:
            self._finish_clip_preview()
        elif time.monotonic() < self.play_guard_until:
            return  # just loaded or moved: the mixer does not report busy yet
        elif not self.audio_engine.is_busy() or self._clock_passed_end():
            self._song_finished()

    def monitor_audio(self) -> None:
        """Run the playback monitor every 40 ms for as long as the window is open."""
        if self._is_shutting_down:
            return
        delay = MONITOR_INTERVAL_MS
        try:
            self._monitor_tick()
        except Exception:  # last-resort guard: one failed tick must not freeze the timeline for good
            if not self._is_shutting_down:
                logger.exception("The playback monitor failed; it will try again shortly")
            delay = MONITOR_RETRY_MS
        if not self._is_shutting_down:
            with contextlib.suppress(tk.TclError):
                self._monitor_timer = self.root.after(delay, self.monitor_audio)

"""Step 2 transport: track loading, waveform/art workers, play/pause/stop, seeking."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox

import pygame

from app.config import COVER_CACHE_DIR, format_time, log_error
from app.core.cache_manager import cache_mgr
from app.core.metadata import extract_album_art
from app.core.task_manager import task_mgr
from app.core.waveform import analyze_audio
from app.ui.components import draw_placeholder_cover
from app.ui.error_dialog import show_friendly_error
from app.ui.features.base import AppBase
from app.ui.theme import BORDER_MAIN, COLOR_ACCENT, COLOR_PAUSE, COLOR_PLAY


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
                filepath, req_id = item
                if req_id != self._art_req_id or getattr(self, "_is_shutting_down", False):
                    self._art_queue.task_done()
                    continue

                file_hash = hashlib.md5(
                    os.path.abspath(filepath).encode("utf-8", errors="ignore"), usedforsecurity=False
                ).hexdigest()
                out_png = os.path.join(COVER_CACHE_DIR, f"{file_hash}_art.png")
                success = extract_album_art(filepath, out_png)

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

        self._art_queue.put((filepath, current_req))

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
        stops or picks another song meanwhile, the stale start is dropped.
        """
        if not self.audio_engine.needs_conversion(path):
            self._pending_play_token = None
            start_fn()
            return
        token = object()
        self._pending_play_token = token
        self.set_busy(True, busy_text)

        def _worker() -> None:
            self.audio_engine.get_playable_audio_path(path)
            self._safe_after(0, _ready)

        def _ready() -> None:
            if self._pending_play_token is not token:
                return
            self._pending_play_token = None
            self.set_busy(False)
            start_fn()

        task_mgr.submit_task(_worker)

    def _load_track_ui(self, path: str, title: str | None = None) -> bool:
        self.selected_file_path = path
        base_name = title or os.path.basename(path)
        artist_name = "Unknown Artist"

        meta = self._cached_metadata(path)
        dur = meta.get("duration", 0.0)
        if meta.get("title"):
            base_name = meta["title"]
        if meta.get("artist"):
            artist_name = meta["artist"]

        if not os.path.exists(path):
            messagebox.showerror("Error", f"Audio file not found:\n{path}")
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
        if self.audio_engine.needs_conversion(path):
            # Convert ahead of time so pressing Play is instant.
            task_mgr.submit_task(self.audio_engine.get_playable_audio_path, path)
        return True

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
        for i in range(5):
            x1 = 2 + i * 16
            x2 = x1 + 12
            color = colors_on[i] if (i < level) else dim_color
            c.create_rectangle(x1, 2, x2, 12, fill=color, outline="", width=0)

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
        if self._selection_debounce_timer:
            try:
                self.root.after_cancel(self._selection_debounce_timer)
            except Exception:
                pass
            self._selection_debounce_timer = None
        if not self.selected_file_path:
            messagebox.showwarning("No Song", "Click a song in the Library first.")
            return
        if self.is_paused:
            self.pause_audio()
            return
        self.stop_audio()
        path = self.selected_file_path
        start_pos = float(self.scale_progress.get())

        def _start() -> None:
            if self.selected_file_path != path:
                return
            try:
                self.playback_ctrl.play_track(path, start_pos, is_playlist=False)
                self._set_card_playing_state("playing")
                self.set_status(f"Playing: {os.path.basename(path)}", icon="▶")
            except Exception as e:
                log_error(f"play_main: {e}")
                show_friendly_error(self.root, e, "playback")

        self._when_playable(path, _start)

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
        if self._pending_play_token is not None:
            self._pending_play_token = None
            self.set_busy(False)
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.stop(user=user)
        self._release_audio_file()
        aud_file = getattr(self, "_audition_slice_file", None)
        if aud_file:
            self._audition_slice_file = None
            self._is_audition_slice = False
            try:
                if os.path.exists(aud_file):
                    os.remove(aud_file)
            except Exception:
                pass
        self.is_playing_main = False
        self.is_playing_playlist = False
        self.is_paused = False
        self.previewing_clip = False
        self.play_clock_origin = None
        self._set_card_playing_state("stopped")
        if user:
            self._updating_ui = True
            self.scale_progress.set(0)
            self.lbl_prog_time.config(text=f"00:00 / {format_time(self.track_duration)}")
            self._updating_ui = False
            self.play_start_offset = 0
            self.set_status("Stopped.", icon="⏹")
            self.refresh_playlist_listbox()
        self._render_waveform()

    def _song_finished(self) -> None:
        if self.is_paused:
            return
        if self.previewing_clip and hasattr(self, "loop_clip") and self.loop_clip.get():
            self._restart_clip_loop()
            return
        if self.is_playing_playlist:
            self.play_next_in_playlist()
        elif self.is_playing_main:
            self.is_playing_main = False
            self.play_clock_origin = None
            self._set_card_playing_state("stopped")
            self.set_status("Playback finished.")
            self._render_waveform()

    def monitor_audio(self) -> None:
        if not getattr(self, "_is_shutting_down", False):
            if (self.is_playing_main or self.is_playing_playlist) and not self.is_paused:
                curr_pos = self._current_play_seconds()
                if not self._progress_dragging:
                    self._updating_ui = True
                    self.scale_progress.set(curr_pos)
                    self.lbl_prog_time.config(text=self._prog_label(curr_pos))
                    self._updating_ui = False
                    self._render_waveform()

                if hasattr(self, "playback_ctrl"):
                    vu_lvl = self.playback_ctrl.calculate_vu_level(curr_pos, self.track_duration, self._current_peaks)
                    self._draw_vu_meter(vu_lvl)

                if self.previewing_clip and curr_pos >= (self.clip_end_time - 0.05):
                    if hasattr(self, "loop_clip") and self.loop_clip.get():
                        self._restart_clip_loop()
                    else:
                        self.stop_audio(user=False)
                        self.set_status("Finished previewing clip.")
                        self._render_waveform()
                else:
                    now = time.monotonic()
                    ended = False
                    if hasattr(self, "playback_ctrl") and self.playback_ctrl.check_native_end_event():
                        ended = True
                    elif now >= getattr(self, "play_guard_until", 0):
                        if not pygame.mixer.music.get_busy() or curr_pos >= (self.track_duration - 0.05):
                            ended = True
                    if ended:
                        self._song_finished()

            if hasattr(self, "root") and self.root and self.root.winfo_exists():
                self._monitor_timer = self.root.after(40, self.monitor_audio)

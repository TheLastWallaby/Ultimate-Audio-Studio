"""Step 2 clipping: start/end markers, nudges, gain, fades, Test Clip and Save Clip."""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, simpledialog

from app.config import format_time, log_error
from app.core.cache_manager import cache_mgr
from app.core.task_manager import task_mgr
from app.core.time_utils import parse_time
from app.services.clipper import clip_audio_worker, has_original_backup, restore_original
from app.ui import dialogs
from app.ui.error_dialog import show_friendly_error
from app.ui.features.base import AppBase
from app.ui.theme import COLOR_DOWNLOAD, TEXT_DARK


class ClipEditorMixin(AppBase):
    """Step 2 clipping: start/end markers, nudges, gain, fades, Test Clip and Save Clip."""

    def on_gain_change(self, val: str | float) -> None:
        v = float(val)
        sign = "+" if v > 0 else ""
        self.lbl_gain.config(text=f"Volume Boost: {sign}{v:.1f} dB")
        if abs(v) > 0.05:
            self.lbl_gain.config(fg=COLOR_DOWNLOAD)
        else:
            self.lbl_gain.config(fg=TEXT_DARK)

    def reset_gain(self) -> None:
        self.scale_gain.set(0.0)
        self.lbl_gain.config(text="Volume Boost: 0 dB", fg=TEXT_DARK)

    def nudge_clip_start(self, delta: float) -> None:
        if not self.selected_file_path:
            return
        new_val = self.playback_ctrl.nudge_start(delta, self.clip_start_sec, self.clip_end_sec)
        self.clip_start_sec = new_val
        self._updating_ui = True
        is_frac = not float(new_val).is_integer()
        self.lbl_start_time.config(text=f"Start: {format_time(new_val, include_fractional=is_frac)}")
        self._updating_ui = False
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip start set to {format_time(new_val, include_fractional=is_frac)}.")

    def nudge_clip_end(self, delta: float) -> None:
        if not self.selected_file_path:
            return
        max_dur = self.track_duration if self.track_duration > 0 else 999999
        new_val = self.playback_ctrl.nudge_end(delta, self.clip_start_sec, self.clip_end_sec, max_dur)
        self.clip_end_sec = new_val
        self._updating_ui = True
        is_frac = not float(new_val).is_integer()
        self.lbl_end_time.config(text=f"End: {format_time(new_val, include_fractional=is_frac)}")
        self._updating_ui = False
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip end set to {format_time(new_val, include_fractional=is_frac)}.")

    def _update_clip_length_label(self) -> None:
        dur = max(0.0, self.clip_end_sec - self.clip_start_sec)
        is_frac = (
            not float(dur).is_integer()
            or not float(self.clip_start_sec).is_integer()
            or not float(self.clip_end_sec).is_integer()
        )
        self.lbl_clip_len.config(text=f"Clip: {format_time(dur, include_fractional=is_frac)}")

    def set_start_here(self) -> None:
        if not self.selected_file_path:
            self.set_status("Select a song in the Library first to set clip start.")
            return
        curr = max(0.0, min(self.track_duration, float(self.scale_progress.get())))
        self.clip_start_sec = curr
        if self.clip_start_sec >= self.clip_end_sec:
            self.clip_end_sec = self.track_duration
            self.lbl_end_time.config(text=f"End: {format_time(self.clip_end_sec)}")
        is_frac = not float(curr).is_integer()
        self.lbl_start_time.config(text=f"Start: {format_time(curr, include_fractional=is_frac)}")
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip start set to {format_time(curr, include_fractional=is_frac)}.")

    def set_end_here(self) -> None:
        if not self.selected_file_path:
            self.set_status("Select a song in the Library first to set clip end.")
            return
        curr = max(0.0, min(self.track_duration, float(self.scale_progress.get())))
        self.clip_end_sec = curr
        if self.clip_end_sec <= self.clip_start_sec:
            self.clip_start_sec = 0.0
            self.lbl_start_time.config(text="Start: 00:00")
        is_frac = not float(curr).is_integer()
        self.lbl_end_time.config(text=f"End: {format_time(curr, include_fractional=is_frac)}")
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip end set to {format_time(curr, include_fractional=is_frac)}.")

    def _parse_time_input(self, text: str | None) -> float | None:
        """Parse MM:SS, M:SS, MM:SS.s, HH:MM:SS or raw seconds; None when invalid or negative."""
        return parse_time(text)

    def edit_start_time(self) -> None:
        if not self.selected_file_path:
            self.set_status("Select a song in the Library first to edit clip start.")
            return
        curr_str = format_time(self.clip_start_sec, include_fractional=not float(self.clip_start_sec).is_integer())
        inp = simpledialog.askstring(
            "Set Clip Start",
            f"Enter new Start time for clip (e.g. 01:23, 01:23.5, or 83.5):\nMax allowed: {format_time(self.clip_end_sec, include_fractional=not float(self.clip_end_sec).is_integer())}",
            initialvalue=curr_str,
            parent=self.root,
        )
        if inp is None:
            return
        val = self._parse_time_input(inp)
        if val is None:
            dialogs.show_warning(self.root, "Invalid Time", "Please enter a valid time (e.g. '01:30' or '90').")
            return
        if val >= self.clip_end_sec:
            dialogs.show_warning(
                self.root,
                "Invalid Range",
                f"Clip Start must be before Clip End ({format_time(self.clip_end_sec, include_fractional=not float(self.clip_end_sec).is_integer())}).",
            )
            return
        self.clip_start_sec = max(0.0, val)
        is_frac = not float(self.clip_start_sec).is_integer()
        self.lbl_start_time.config(text=f"Start: {format_time(self.clip_start_sec, include_fractional=is_frac)}")
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip start set to {format_time(self.clip_start_sec, include_fractional=is_frac)}.")

    def edit_end_time(self) -> None:
        if not self.selected_file_path:
            self.set_status("Select a song in the Library first to edit clip end.")
            return
        curr_str = format_time(self.clip_end_sec, include_fractional=not float(self.clip_end_sec).is_integer())
        inp = simpledialog.askstring(
            "Set Clip End",
            f"Enter new End time for clip (e.g. 02:45, 02:45.5, or 165.5):\nSong total length: {format_time(self.track_duration)}",
            initialvalue=curr_str,
            parent=self.root,
        )
        if inp is None:
            return
        val = self._parse_time_input(inp)
        if val is None:
            dialogs.show_warning(self.root, "Invalid Time", "Please enter a valid time (e.g. '02:45' or '165').")
            return
        if val <= self.clip_start_sec:
            dialogs.show_warning(
                self.root,
                "Invalid Range",
                f"Clip End must be after Clip Start ({format_time(self.clip_start_sec, include_fractional=not float(self.clip_start_sec).is_integer())}).",
            )
            return
        max_limit = self.track_duration if self.track_duration > 0 else 999999
        self.clip_end_sec = min(max_limit, val)
        is_frac = not float(self.clip_end_sec).is_integer()
        self.lbl_end_time.config(text=f"End: {format_time(self.clip_end_sec, include_fractional=is_frac)}")
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip end set to {format_time(self.clip_end_sec, include_fractional=is_frac)}.")

    def _get_fade_sec(self) -> float:
        choice = self.fade_choice_var.get() if hasattr(self, "fade_choice_var") else ""
        if "0.5s" in choice:
            return 0.5
        elif "3.0s" in choice:
            return 3.0
        return 1.5

    def test_clip(self) -> None:
        if not self.selected_file_path:
            dialogs.show_warning(self.root, "No Song", "Click a song in the Library first.")
            return
        s_time = self.clip_start_sec
        e_time = self.clip_end_sec
        if s_time >= e_time:
            dialogs.show_warning(self.root, "Invalid Range", "Clip End must be after Clip Start.")
            return
        self.stop_audio()

        soften = bool(self.soften_clip.get()) if hasattr(self, "soften_clip") else False
        gain_db = float(self.scale_gain.get()) if hasattr(self, "scale_gain") else 0.0
        fade_sec = self._get_fade_sec()
        loop = bool(self.loop_clip.get()) if hasattr(self, "loop_clip") else False

        path = self.selected_file_path
        token = object()
        self._pending_play_token = token
        self.set_busy(True, "Preparing your clip preview...")

        def _prepare() -> None:
            # Both steps can run FFmpeg for seconds, so they stay off the Tkinter thread.
            prepared = self.audio_engine.prepare_for_playback(path)
            slice_path = self.playback_ctrl.prepare_audition(path, s_time, e_time, gain_db, soften, fade_sec)
            self._safe_after(0, _start, slice_path, prepared)

        def _start(slice_path: str | None, prepared: bool) -> None:
            if self._pending_play_token is not token or self.selected_file_path != path:
                self.playback_ctrl.discard_audition(slice_path)
                return
            self._pending_play_token = None
            if slice_path is None and not prepared:
                # Without a rendered slice the song itself is played, which would repeat the failed
                # conversion on this thread and freeze the window.
                self.set_busy(False, "This clip could not be played.")
                show_friendly_error(
                    self.root, f"FFmpeg could not convert '{Path(path).name}' for playback.", "playback"
                )
                return
            self.set_busy(False)
            self._start_test_clip(path, s_time, e_time, slice_path, gain_db, soften, fade_sec, loop)

        task_mgr.submit_task(_prepare)

    def _start_test_clip(
        self,
        path: str,
        s_time: float,
        e_time: float,
        slice_path: str | None,
        gain_db: float,
        soften: bool,
        fade_sec: float,
        loop: bool,
    ) -> None:
        try:
            # Sets is_playing_main, previewing_clip and clip_end_time, and owns the preview slice file.
            self.playback_ctrl.start_audition(path, s_time, e_time, slice_path, loop=loop)
            self._set_card_playing_state("playing")
            self._updating_ui = True
            self.scale_progress.set(s_time)
            self._updating_ui = False
            self._render_waveform()

            notes = []
            if abs(gain_db) > 0.05:
                notes.append(f"{gain_db:+.1f}dB boost")
            if soften:
                notes.append(f"{fade_sec:.1f}s smooth fade")
            if loop:
                notes.append("loop on")
            note_str = f" ({', '.join(notes)})" if notes else ""
            self.set_status(f"Previewing clip from {format_time(s_time)} to {format_time(e_time)}{note_str}.", icon="▶")
        except Exception as e:
            log_error(f"test_clip: {e}")
            show_friendly_error(self.root, e, "playback")

    def _restart_clip_loop(self) -> bool:
        s_time = self.clip_start_sec
        if self.playback_ctrl.restart_clip_loop():
            self._updating_ui = True
            self.scale_progress.set(s_time)
            self._updating_ui = False
            self.set_status(
                f"Looping clip preview ({format_time(s_time)} - {format_time(self.clip_end_sec)})...", icon="🔁"
            )
            return True
        else:
            self.stop_audio(user=False)
            return False

    def save_clip(self) -> None:
        if not self.selected_file_path:
            dialogs.show_warning(self.root, "No Song", "Click a song in the Library first.")
            return
        s_time = self.clip_start_sec
        e_time = self.clip_end_sec
        if s_time >= e_time:
            dialogs.show_warning(self.root, "Invalid Range", "Clip End must be after Clip Start.")
            return

        dur = e_time - s_time
        base, _ext = os.path.splitext(os.path.basename(self.selected_file_path))
        def_name = f"{base}_clip_{int(dur)}s.mp3"

        save_name = filedialog.asksaveasfilename(
            initialdir=self.library_folder,
            initialfile=def_name,
            defaultextension=".mp3",
            filetypes=[("MP3 Audio (*.mp3)", "*.mp3")],
            title="Save Your Clip",
            parent=self.root,
        )
        if not save_name:
            return

        if not save_name.lower().endswith(".mp3"):
            save_name += ".mp3"

        is_self_overwrite = os.path.abspath(save_name).lower() == os.path.abspath(self.selected_file_path).lower()
        if is_self_overwrite:
            confirm = dialogs.ask_yes_no(
                self.root,
                "Replace the Original Song?",
                f"You are about to replace the original song file:\n\n'{os.path.basename(save_name)}'\n\n"
                "A backup of the original is kept automatically, and you can put it back later "
                "with 'Restore Original Song'.",
                yes="Replace with my clip",
                no="Cancel",
                default_yes=False,
                icon=dialogs.ICON_WARNING,
            )
            if not confirm:
                return

        self.stop_audio()
        self._release_audio_file()

        soften = bool(self.soften_clip.get()) if hasattr(self, "soften_clip") else False
        gain_db = float(self.scale_gain.get()) if hasattr(self, "scale_gain") else 0.0
        fade_sec = self._get_fade_sec()

        self._saving_clip = True
        self.btn_save_clip.config(text="Saving...", state=tk.DISABLED)
        self.set_busy(True, "Trimming and saving your clip...")

        def _on_succ(name: str, full_path: str, was_self_ovw: bool) -> None:
            self._safe_after(0, self._save_success, name, full_path, was_self_ovw)

        def _on_err(err: str) -> None:
            self._safe_after(0, self._save_error, err)

        task_mgr.submit_task(
            clip_audio_worker,
            self.selected_file_path,
            s_time,
            e_time,
            save_name,
            soften,
            gain_db,
            is_self_overwrite,
            _on_succ,
            _on_err,
            fade_sec=fade_sec,
        )

    def _save_success(self, name: str, full_path: str, was_self_overwrite: bool) -> None:
        self._saving_clip = False
        self.btn_save_clip.config(text="💾 Save Clip", state=tk.NORMAL)
        self.set_busy(False)
        cache_mgr.invalidate(full_path)
        if hasattr(self, "library_ctrl"):
            self.library_ctrl.invalidate_search_index(full_path)
        self._art_cache.pop(full_path, None)
        if was_self_overwrite:
            self.refresh_library(select_name=name)
            if self.selected_file_path == full_path:
                self._load_track_ui(full_path, name)
            self.notify_success(
                "Clip saved! The original song was kept as a backup: 'Restore Original Song' puts it back.",
                icon="💾",
            )
        elif self._reveal_new_song(name):
            self.notify_success("Clip saved and loaded: press PLAY to hear it.", icon="💾")
        else:
            self.notify_success("Clip saved! It is marked in green in your Library on the left.", icon="💾")

    def _save_error(self, err: str) -> None:
        self._saving_clip = False
        self.btn_save_clip.config(text="💾 Save Clip", state=tk.NORMAL)
        self.set_busy(False, "Could not save clip.")
        show_friendly_error(self.root, err, "save_clip")

    def _update_restore_original_button(self) -> None:
        """Show 'Restore Original Song' only for a song that a clip was saved over."""
        if not hasattr(self, "btn_restore_original"):
            return
        if has_original_backup(self.selected_file_path):
            if not self.btn_restore_original.winfo_ismapped():
                self.btn_restore_original.pack(fill=tk.X, pady=(4, 0))
        else:
            self.btn_restore_original.pack_forget()

    def restore_original_song(self) -> None:
        """Put the untrimmed song back (the trimmed version goes to the Recycle Bin)."""
        path = self.selected_file_path
        if not path or not has_original_backup(path):
            self._update_restore_original_button()
            return
        name = os.path.basename(path)
        if not dialogs.ask_yes_no(
            self.root,
            "Restore the Original Song?",
            f"This puts back the full, untrimmed '{name}' as it was before you saved a clip over it.\n\n"
            "The trimmed version is moved to the Recycle Bin.",
            yes="Restore the original",
            no="Cancel",
        ):
            return
        self.stop_audio(user=True)
        self._release_audio_file()
        try:
            restore_original(path)
        except OSError as e:
            log_error(f"restore_original_song: {e}")
            show_friendly_error(self.root, e, "generic")
            return
        cache_mgr.invalidate(path)
        self.library_ctrl.invalidate_search_index(path)
        self._art_cache.pop(path, None)
        self.refresh_library(select_name=name)
        self._load_track_ui(path, name)
        self.notify_success(f"The original '{name}' is back in your Library.", icon="↩️")

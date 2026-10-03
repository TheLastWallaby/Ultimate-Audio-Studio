"""Step 3 playlists: create/rename/delete, add/remove/reorder songs, playlist playback."""

from __future__ import annotations

import contextlib
import logging
import os
import tkinter as tk
from pathlib import Path

import pygame

from app.config import PLAYLISTS_PATH, log_error, sanitize_filename
from app.controllers.playlist_controller import PlaylistLoadResult
from app.core.audio_engine import NoAudioDeviceError
from app.ui import dialogs
from app.ui.components import listbox_nearest, listbox_selection
from app.ui.error_dialog import show_friendly_error
from app.ui.features.base import AppBase

logger = logging.getLogger(__name__)
# Pause before moving past a playlist song whose file is gone, so the status message can be read.
MISSING_SONG_SKIP_MS = 350


class PlaylistMixin(AppBase):
    """Step 3 playlists: create/rename/delete, add/remove/reorder songs, playlist playback."""

    # True while saving keeps failing, so the warning is shown once and not after every change.
    _playlist_save_failing = False
    # Tk ``after`` id of the pending move past a missing playlist song (cancelled by any stop).
    _pl_skip_timer: str | None = None

    def load_playlists(self) -> None:
        result = self.playlist_ctrl.load(PLAYLISTS_PATH)
        saved_active = getattr(self, "_saved_active_playlist", "")
        if saved_active in self.playlists:
            self.active_playlist_name = saved_active
        self._relink_playlists()
        self.refresh_playlist_dropdown()
        self.refresh_playlist_listbox()
        if result.needs_notice:
            # After the window is up: a dialog during start-up would appear before the app does.
            self.root.after(800, self._report_playlist_load, result)

    def _report_playlist_load(self, result: PlaylistLoadResult) -> None:
        """Tell the user that their playlists file was unreadable and what the app did about it."""
        if getattr(self, "_is_shutting_down", False):
            return
        if result.restored_from_backup:
            dialogs.show_warning(
                self.root,
                "Playlists Restored",
                "Your saved playlists could not be opened, so they were restored from an earlier copy.\n\n"
                "Changes you made the last time you used the app may be missing. Please check your "
                "playlists and add any missing songs again.",
            )
            return
        kept = (
            f"\n\nThe file that could not be opened was kept as '{result.damaged_copy.name}' in your "
            f"'{result.damaged_copy.parent.name}' folder, in case someone can help you recover it."
            if result.damaged_copy
            else ""
        )
        dialogs.show_warning(
            self.root,
            "Playlists Could Not Be Opened",
            "Your saved playlists could not be opened, so the app started with an empty playlist.\n\n"
            "Your songs are safe: they are still in your Library, and you can add them to a playlist "
            f"again.{kept}",
        )

    def _relink_playlists(self) -> int:
        """Reconnect playlist songs that moved into the current Library folder (same file name)."""
        relinked = self.playlist_ctrl.relink_missing(self.library_folder)
        if relinked:
            self.save_playlists()
            self.set_status(f"Reconnected {relinked} playlist song(s) found in your Library folder.")
        return int(relinked)

    def _resync_playlist_index(self) -> None:
        """After songs were removed or restored outside the playlist tools, point playlist_index back at
        the song that is playing, so Next/Previous and the ▶ marker stay correct."""
        if not self.is_playing_playlist or not self.selected_file_path:
            return
        tracks = self.playlist_ctrl.get_active_tracks(self.active_playlist_name)
        if self.selected_file_path in tracks:
            self.playlist_index = tracks.index(self.selected_file_path)

    def save_playlists(self) -> None:
        """Save the playlists, warning the user (once per run of failures) when that did not work."""
        if self.playlist_ctrl.save(PLAYLISTS_PATH):
            self._playlist_save_failing = False
            return
        already_warned = self._playlist_save_failing
        self._playlist_save_failing = True
        if already_warned or getattr(self, "_is_shutting_down", False):
            return
        dialogs.show_warning(
            self.root,
            "Playlists Not Saved",
            "Your playlist changes could not be saved on this computer.\n\n"
            "• Check that your Music folder is available and the disk is not full.\n"
            "• The app tries again each time you change a playlist, and when you close it.\n\n"
            "If saving still fails when the app is closed, these changes will be lost.",
        )

    def refresh_playlist_dropdown(self) -> None:
        names = self.playlist_ctrl.get_playlist_names()
        self.cmb_playlists["values"] = names
        if self.active_playlist_name in names:
            self.playlist_var.set(self.active_playlist_name)
        elif names:
            self.active_playlist_name = names[0]
            self.playlist_ctrl.active_playlist_name = names[0]
            self.playlist_var.set(names[0])

    def refresh_playlist_listbox(self) -> None:
        """Draw the active playlist from memory (no disk access: it is redrawn on every drag step)."""
        self.listbox_pl.delete(0, tk.END)
        self.playlist_files = self.playlist_ctrl.get_active_tracks(self.active_playlist_name)
        rows = self._song_rows_for(self.playlist_files)
        for idx, (path, row) in enumerate(zip(self.playlist_files, rows, strict=True), 1):
            prefix = "▶ " if (self.is_playing_playlist and idx - 1 == self.playlist_index) else f"{idx:02d}. "
            text = f"⚠️ [Missing] {Path(path).name}" if row.missing else row.text
            self.listbox_pl.insert(tk.END, f"{prefix}{text}")

    def on_playlist_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        name = self.playlist_var.get()
        if name in self.playlists:
            self.active_playlist_name = name
            self.playlist_ctrl.active_playlist_name = name
            if self.is_playing_playlist:
                self.stop_audio()
            self.refresh_playlist_listbox()
            self._schedule_settings_save()
            self.set_status(f"Switched to playlist: {name}")

    def create_playlist(self) -> None:
        """Ask for a name and start a new, empty playlist."""
        answer = dialogs.ask_text(self.root, "New Playlist", "Type a name for the new playlist:", ok="Create playlist")
        if answer is None:
            return
        name = sanitize_filename(answer.text).strip()
        if not name:
            dialogs.show_warning(self.root, "Invalid Name", "Please enter a valid playlist name.")
            return
        if not self.playlist_ctrl.create_playlist(name):
            dialogs.show_warning(self.root, "Already Exists", f"A playlist named '{name}' already exists.")
            return
        if self.is_playing_playlist:
            # The new, empty playlist is now the active one: playback would otherwise go on showing
            # "PLAYING" in silence after this song, or play a song of the new list by its old position.
            self.stop_audio(user=True)
        self.save_playlists()
        self.refresh_playlist_dropdown()
        self.refresh_playlist_listbox()
        self._schedule_settings_save()
        self.set_status(f"Created new playlist: {name}")

    def rename_playlist(self) -> None:
        """Ask for a new name for the playlist that is showing."""
        current = self.active_playlist_name
        answer = dialogs.ask_text(
            self.root,
            "Rename Playlist",
            f"Type a new name for the playlist '{current}':",
            initial=current,
            ok="Rename playlist",
        )
        if answer is None or answer.text == current:
            return
        name = sanitize_filename(answer.text).strip()
        if not name:
            dialogs.show_warning(self.root, "Invalid Name", "Please enter a valid playlist name.")
            return
        if not self.playlist_ctrl.rename_playlist(current, name):
            dialogs.show_warning(self.root, "Already Exists", f"A playlist named '{name}' already exists.")
            return
        self.save_playlists()
        self.refresh_playlist_dropdown()
        self.refresh_playlist_listbox()
        self._schedule_settings_save()
        self.set_status(f"Renamed playlist to: {name}")

    def delete_playlist(self) -> None:
        current = self.active_playlist_name
        if len(self.playlists) <= 1:
            dialogs.show_warning(self.root, "Cannot Delete", "You must keep at least one playlist.")
            return
        ok = dialogs.ask_yes_no(
            self.root,
            "Delete Playlist",
            f"Delete the playlist '{current}'?\n\nYour songs stay in your Library; only the list is removed.",
            yes="Delete playlist",
            no="Keep it",
            danger=True,
            default_yes=False,
        )
        if not ok:
            return
        if self.is_playing_playlist:
            # Playback would otherwise continue into whichever playlist becomes active next.
            self.stop_audio(user=True)
        if not self.playlist_ctrl.delete_playlist(current):
            dialogs.show_warning(self.root, "Cannot Delete", "Could not delete playlist.")
            return
        self.save_playlists()
        self.refresh_playlist_dropdown()
        self.refresh_playlist_listbox()
        self._schedule_settings_save()
        self.set_status(f"Deleted playlist '{current}'.")

    def add_to_playlist(self) -> None:
        sel = listbox_selection(self.listbox_lib) if hasattr(self, "listbox_lib") else ()
        if sel:
            selected_paths = []
            for idx in sel:
                if 0 <= idx < len(self.visible_files):
                    selected_paths.append(os.path.join(self.library_folder, self.visible_files[idx]))
        elif self.selected_file_path:
            selected_paths = [self.selected_file_path]
        else:
            dialogs.show_warning(self.root, "No Song", "Click or select songs in the Library first.")
            return

        playlist = self.active_playlist_name
        added = [path for path in selected_paths if self.playlist_ctrl.add_track(playlist, path)]
        skipped = len(selected_paths) - len(added)
        if added:
            self.save_playlists()
            self.refresh_playlist_listbox()
            self.listbox_pl.see(tk.END)
        if not added and len(selected_paths) == 1:
            self.set_status(f"'{os.path.basename(selected_paths[0])}' is already in '{playlist}'.", icon="ℹ️")
        elif not added:
            self.set_status(f"All {skipped} selected songs are already in '{playlist}'.", icon="ℹ️")
        elif len(added) == 1 and not skipped:
            self.set_status(f"Added '{os.path.basename(added[0])}' to '{playlist}'.")
        else:
            note = f" ({skipped} already there)" if skipped else ""
            self.set_status(f"Added {len(added)} songs to '{playlist}'{note}.")

    def pl_remove(self) -> None:
        """Take the selected song out of the playlist (the song itself stays in the Library), with Undo."""
        sel = listbox_selection(self.listbox_pl)
        if not sel or sel[0] >= len(self.playlist_files):
            return
        idx = sel[0]
        was_playing = self.is_playing_playlist and idx == self.playlist_index
        if was_playing:
            self.stop_audio()
        removed = self.playlist_ctrl.remove_track_with_undo(self.active_playlist_name, idx)
        if removed:
            self.save_playlists()
            self.refresh_playlist_listbox()
            tracks = self.playlists.get(self.active_playlist_name, [])
            if tracks:
                new_sel = min(idx, len(tracks) - 1)
                self.listbox_pl.selection_set(new_sel)
            self.show_undo(f"Removed '{Path(removed).name}' from playlist.", callback=self._undo_remove_track)

    def _undo_remove_track(self) -> None:
        try:
            restored = self.playlist_ctrl.undo_remove()
            if restored:
                self.save_playlists()
                self.refresh_playlist_listbox()
                self.set_status(f"Restored '{os.path.basename(restored[2])}' to playlist.", icon="↩️")
        except Exception as e:
            log_error(f"_undo_remove_track: {e}")

    def pl_move_up(self) -> None:
        sel = listbox_selection(self.listbox_pl)
        if not sel or sel[0] <= 0:
            return
        idx = sel[0]
        new_idx = self.playlist_ctrl.move_up(self.active_playlist_name, idx)
        self.save_playlists()
        self.refresh_playlist_listbox()
        self.listbox_pl.selection_set(new_idx)
        self.listbox_pl.see(new_idx)

    def pl_move_down(self) -> None:
        sel = listbox_selection(self.listbox_pl)
        tracks = self.playlist_ctrl.get_active_tracks(self.active_playlist_name)
        if not sel or sel[0] >= len(tracks) - 1:
            return
        idx = sel[0]
        new_idx = self.playlist_ctrl.move_down(self.active_playlist_name, idx)
        self.save_playlists()
        self.refresh_playlist_listbox()
        self.listbox_pl.selection_set(new_idx)
        self.listbox_pl.see(new_idx)

    def _pl_drag_start(self, event: tk.Event[tk.Listbox]) -> None:
        """Remember which song the mouse went down on, for drag-to-reorder."""
        idx = listbox_nearest(self.listbox_pl, event.y)
        self._pl_drag_index = idx if 0 <= idx < len(self.playlist_files) else None
        self._pl_drag_moved = False

    def _pl_drag_motion(self, event: tk.Event[tk.Listbox]) -> None:
        """Move the dragged song to the row under the mouse."""
        src = getattr(self, "_pl_drag_index", None)
        if src is None:
            return
        dst = listbox_nearest(self.listbox_pl, event.y)
        if dst == src or not (0 <= dst < len(self.playlist_files)):
            return
        new_idx = self.playlist_ctrl.move_track(self.active_playlist_name, src, dst)
        self._pl_drag_index = new_idx
        self._pl_drag_moved = True
        self.listbox_pl.config(cursor="sb_v_double_arrow")
        self.refresh_playlist_listbox()
        self.listbox_pl.selection_clear(0, tk.END)
        self.listbox_pl.selection_set(new_idx)
        self.listbox_pl.see(new_idx)

    def _pl_drag_end(self, _event: tk.Event[tk.Listbox] | None = None) -> None:
        """Save the new order once the mouse button is released."""
        moved = getattr(self, "_pl_drag_moved", False)
        self._pl_drag_index = None
        self._pl_drag_moved = False
        self.listbox_pl.config(cursor="")
        if moved:
            self.save_playlists()
            self.set_status("Playlist order changed.")

    def on_playlist_double_click(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        sel = listbox_selection(self.listbox_pl)
        if not sel or sel[0] >= len(self.playlist_files):
            return
        self.playlist_index = sel[0]
        self._play_current_pl_track()

    def play_playlist(self) -> None:
        if not self.playlist_files:
            dialogs.show_warning(self.root, "Empty Playlist", "Add some songs to this playlist first.")
            return
        sel = listbox_selection(self.listbox_pl)
        self.playlist_index = sel[0] if sel else 0
        self._play_current_pl_track()

    def play_prev_in_playlist(self) -> None:
        if not self.playlist_files:
            return
        # Like a CD player: past the first few seconds, Previous restarts the current song.
        if (self.is_playing_playlist or self.is_paused) and self._current_play_seconds() > 3.0:
            self._play_current_pl_track()
            return
        prev_idx = self.playlist_ctrl.get_prev_index(
            self.active_playlist_name, self.playlist_index, repeat=bool(self.repeat_playlist.get())
        )
        if prev_idx is not None:
            self.playlist_index = prev_idx
            self._play_current_pl_track()

    def play_next_in_playlist(self) -> None:
        """Play the next playlist song; after the last one, stop and go back to the start."""
        if not self.playlist_files:
            return
        next_idx = self.playlist_ctrl.get_next_index(
            self.active_playlist_name, self.playlist_index, repeat=bool(self.repeat_playlist.get())
        )
        if next_idx is not None:
            self.playlist_index = next_idx
            self._play_current_pl_track()
        else:
            self.stop_audio(user=True)
            self.set_status("Finished playlist.")
            self._load_pending_selection()

    def _play_current_pl_track(self, skipped: int = 0) -> None:
        """Play the playlist song at ``playlist_index``.

        ``skipped`` counts the songs passed on the way because they are missing or cannot be played.
        """
        self._pl_skip_timer = None
        if not self.playlist_files or not (0 <= self.playlist_index < len(self.playlist_files)):
            return
        path = self.playlist_files[self.playlist_index]
        if not Path(path).exists():
            self._skip_pl_track(path, skipped + 1)
            return
        self.stop_audio()
        if not self._load_track_ui(path):
            return
        index = self.playlist_index

        def _unplayable() -> None:
            if self.selected_file_path == path:
                self._skip_pl_track(path, skipped + 1, unplayable=True)

        def _start() -> None:
            if self.selected_file_path != path:
                return
            try:
                self.playback_ctrl.play_track(path, 0.0, is_playlist=True)
            except NoAudioDeviceError as err:
                # Nothing can be heard at all: skipping would only run through every song in silence.
                logger.error("No sound output for the playlist song %s: %s", Path(path).name, err)
                show_friendly_error(self.root, err, "playback")
                return
            except (pygame.error, OSError) as err:
                # One damaged song must not end a playlist that is playing unattended.
                logger.error("Could not play the playlist song %s: %s", Path(path).name, err)
                _unplayable()
                return
            self._set_card_playing_state("playing")
            self.refresh_playlist_listbox()
            self.set_status(f"Playlist ({index + 1}/{len(self.playlist_files)}): {Path(path).name}", icon="▶")

        self._when_playable(path, _start, on_failed=_unplayable)

    def _skip_pl_track(self, path: str, skipped: int, unplayable: bool = False) -> None:
        """Move past a playlist song that is gone or (``unplayable``) cannot be played.

        Stops once every song has been tried: with Repeat on, a playlist whose songs are all missing
        (a USB drive that was unplugged) used to be skipped through forever, and Stop could not end it.
        """
        total = len(self.playlist_files)
        if unplayable:
            self._skip_unplayable_pl_track(path, skipped)
            return
        self.set_status(f"Skipping missing song: {Path(path).name}", icon="⚠️")
        next_index = self.playlist_index + 1
        if next_index >= total and self.repeat_playlist.get():
            next_index = 0
        if next_index < total and skipped < total:
            self.playlist_index = next_index
            self._pl_skip_timer = self.root.after(MISSING_SONG_SKIP_MS, self._play_current_pl_track, skipped)
            return
        self.stop_audio(user=True)
        self._warm_library_metadata()  # the lists learn on a worker which songs are gone, and mark them
        plug_in = "If your music is on a USB drive or memory card, plug it in and try again."
        if skipped >= total:
            self.set_status("None of the songs in this playlist could be found.", icon="⚠️")
            dialogs.show_warning(
                self.root,
                "Songs Not Found",
                f"None of the songs in the playlist '{self.active_playlist_name}' could be found.\n\n"
                f"{plug_in}\n\nOtherwise, remove the missing songs (marked ⚠️) from the playlist.",
            )
            return
        self.set_status(f"Song not found: {Path(path).name}. The playlist stopped.", icon="⚠️")
        dialogs.show_warning(
            self.root,
            "Song Not Found",
            f"The song '{Path(path).name}' could not be found, so the playlist stopped.\n\n"
            f"{plug_in}\n\nOtherwise, remove it from the playlist (it is marked ⚠️).",
        )

    def _skip_unplayable_pl_track(self, path: str, skipped: int) -> None:
        """Move past a playlist song that would not play; say so when it was the last one to try."""
        total = len(self.playlist_files)
        name = Path(path).name
        next_index = self.playlist_index + 1
        if next_index >= total and self.repeat_playlist.get():
            next_index = 0
        if next_index < total and skipped < total:
            self.set_status(f"Skipping a song that could not be played: {name}", icon="⚠️")
            self.playlist_index = next_index
            self._pl_skip_timer = self.root.after(MISSING_SONG_SKIP_MS, self._play_current_pl_track, skipped)
            return
        self.stop_audio(user=True)
        self.set_status(f"'{name}' could not be played. The playlist stopped.", icon="⚠️")
        dialogs.show_warning(
            self.root,
            "Song Could Not Be Played",
            f"The song '{name}' could not be played, so the playlist stopped.\n\n"
            "The file may be damaged or in an unusual format.\n\n"
            "• Remove it from the playlist, or download the song again.",
        )

    def _cancel_pending_skip(self) -> None:
        """Forget a pending move past a missing song, so it cannot start the playlist after a stop."""
        timer, self._pl_skip_timer = self._pl_skip_timer, None
        if timer is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(timer)

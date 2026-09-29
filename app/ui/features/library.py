"""Step 1 library list: scanning, search, import, rename and delete with undo."""

from __future__ import annotations

import os
import time
import tkinter as tk
from collections.abc import Mapping
from tkinter import filedialog, simpledialog
from typing import Any

from app.config import AUDIO_EXTS, format_time, log_error, sanitize_filename
from app.core.cache_manager import cache_mgr
from app.core.errors import friendly_error
from app.core.metadata import read_track_metadata
from app.core.task_manager import task_mgr
from app.ui import dialogs
from app.ui.components import listbox_selection
from app.ui.error_dialog import show_error, show_friendly_error
from app.ui.features.base import AppBase

# Background of a just-added library row that was not selected (music was playing at the time).
NEW_SONG_ROW_BG = "#dcfce7"


class LibraryMixin(AppBase):
    """Step 1 library list: scanning, search, import, rename and delete with undo."""

    def _handle_dropped_files(self, paths: list[str]) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        self._import_paths(paths, source="dropped")

    def _import_paths(self, paths: list[str], source: str = "selected") -> None:
        """Copy audio files/folders into the Library, asking before replacing existing songs."""
        planned, dest_exists = self.library_ctrl.build_import_plan(paths, self.library_folder)
        if not planned and not dest_exists:
            dialogs.show_info(
                self.root,
                "No Audio Files",
                f"No compatible audio songs ({', '.join(AUDIO_EXTS)}) were found in the {source} files.",
            )
            return
        if not planned and dest_exists:
            self.set_status(f"The {source} audio files are already in your Library.")
            return

        if dest_exists:
            replace = dialogs.ask_yes_no(
                self.root,
                "Songs Already in Your Library",
                f"{len(dest_exists)} of the {source} song(s) are already in your Library.",
                yes="Replace them",
                no="Keep the ones I have",
                default_yes=False,
            )
            if not replace:
                planned = [(s, d) for (s, d) in planned if not os.path.exists(d)]

        if not planned:
            return

        self._importing = True
        self.set_busy(True, f"Adding {len(planned)} song(s) to your Library...")
        self.library_ctrl.import_external_files(
            planned,
            is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
            on_done=lambda cnt: self._safe_after(0, self._on_copy_external_done, cnt),
        )

    def refresh_library(self, select_name: str | None = None, preserve_view: bool = False) -> None:
        """Show the folder's songs immediately; tags and durations fill in from a background scan.

        preserve_view keeps the current selection and scroll position (for background refreshes).
        """
        self.library_files = self.library_ctrl.scan_files(self.library_folder)
        self.apply_library_filter(select_name, preserve_view=preserve_view)
        self._warm_library_metadata()

    def _reveal_new_song(self, filename: str) -> bool:
        """Show a song that was just added (download, saved clip) and return True if it was loaded.

        Selecting a row in code does not load it into the player, so PLAY used to play the previous
        song while the new one was highlighted. When the player is idle the new song is highlighted
        *and* loaded; while something is playing or paused that is left alone, and the new row is only
        tinted green (not selected), so the highlight never disagrees with what PLAY will do.
        """
        path = os.path.join(self.library_folder, filename)
        player_in_use = self.is_playing_main or self.is_playing_playlist or self.is_paused
        if not player_in_use and os.path.isfile(path):
            self.refresh_library(select_name=filename)
            self.stop_audio()
            return self._load_track_ui(path, filename)
        self._fresh_songs.add(filename)
        self.refresh_library()
        if filename in self.visible_files:
            self.listbox_lib.see(self.visible_files.index(filename))
        return False

    def _warm_library_metadata(self) -> None:
        """Read tags/durations for songs not yet cached on a worker thread, then refresh the lists once."""
        paths = [os.path.join(self.library_folder, f) for f in self.library_files]
        paths += [f for f in getattr(self, "playlist_files", []) if f not in paths]
        missing = [p for p in paths if cache_mgr.get_metadata(p) is None]
        if not missing:
            return
        self._library_meta_gen += 1
        gen = self._library_meta_gen
        if len(missing) > 20:
            self.set_status(f"Reading song details for {len(missing)} songs...", icon="⏳")

        def _worker() -> None:
            for path in missing:
                if getattr(self, "_is_shutting_down", False) or gen != self._library_meta_gen:
                    return
                try:
                    cache_mgr.set_metadata(path, read_track_metadata(path))
                except Exception as e:
                    log_error(f"metadata warm-up {path}: {e}")
            self._safe_after(0, self._on_library_metadata_ready, gen, len(missing))

        task_mgr.submit_task(_worker)

    def _on_library_metadata_ready(self, gen: int, count: int) -> None:
        if gen != self._library_meta_gen or getattr(self, "_is_shutting_down", False):
            return
        self.library_ctrl.invalidate_search_index()
        self.apply_library_filter(preserve_view=True)
        pl_selection = listbox_selection(self.listbox_pl)
        pl_view = self.listbox_pl.yview()[0]
        self.refresh_playlist_listbox()
        for idx in pl_selection:
            self.listbox_pl.selection_set(idx)
        self.listbox_pl.yview_moveto(pl_view)
        if count > 20:
            self.set_status("Ready. Select a song on the left to play or trim.")

    def _display_name(self, path: str, filename: str | None = None) -> str:
        """Friendly row text: 'Title — Artist' from tags when present, else the file name without extension."""
        meta = self._cached_metadata(path, probe=False)
        stem = os.path.splitext(filename or os.path.basename(path))[0]
        title = (meta.get("title") or "").strip() or stem
        artist = (meta.get("artist") or "").strip()
        if artist.endswith(" - Topic"):
            artist = artist[: -len(" - Topic")]
        if artist and artist.lower() not in title.lower():
            return f"{title} — {artist}"
        return title

    def apply_library_filter(self, select_name: str | None = None, preserve_view: bool = False) -> None:
        query = self.entry_search.get().strip().lower() if hasattr(self, "entry_search") else ""
        prev_selected = set()
        prev_view = None
        if preserve_view:
            prev_selected = {
                self.visible_files[i] for i in listbox_selection(self.listbox_lib) if i < len(self.visible_files)
            }
            prev_view = self.listbox_lib.yview()[0]
        if hasattr(self, "library_ctrl"):
            self.visible_files = self.library_ctrl.filter_files(
                self.library_files,
                self.library_folder,
                query,
                get_metadata_fn=lambda path: self._cached_metadata(path, probe=False),
            )
        else:
            self.visible_files = list(self.library_files)

        self.listbox_lib.delete(0, tk.END)
        select_idx = None
        for idx, f in enumerate(self.visible_files):
            f_path = os.path.join(self.library_folder, f)
            dur = self._cached_duration(f_path, probe=False)
            dur_str = f" [{format_time(dur)}]" if dur > 0 else ""
            self.listbox_lib.insert(tk.END, f"{self._display_name(f_path, f)}{dur_str}")
            if f in self._fresh_songs:
                self.listbox_lib.itemconfig(idx, background=NEW_SONG_ROW_BG)
            if select_name and f == select_name:
                select_idx = idx
            elif f in prev_selected:
                self.listbox_lib.selection_set(idx)

        if prev_view is not None:
            self.listbox_lib.yview_moveto(prev_view)
        if select_idx is not None:
            self.listbox_lib.selection_set(select_idx)
            self.listbox_lib.see(select_idx)

    def on_search_key_release(self, event: tk.Event[tk.Misc] | None = None) -> None:
        timer = getattr(self, "_search_debounce_timer", None)
        if timer is not None:
            try:
                self.root.after_cancel(timer)
            except Exception:
                pass
        self._search_debounce_timer = self.root.after(150, self.apply_library_filter)

    def clear_search(self) -> None:
        timer = getattr(self, "_search_debounce_timer", None)
        if timer is not None:
            try:
                self.root.after_cancel(timer)
            except Exception:
                pass
            self._search_debounce_timer = None
        if hasattr(self, "entry_search"):
            self.entry_search.delete(0, tk.END)
            self.apply_library_filter()

    def _cached_duration(self, path: str | None, probe: bool = True) -> float:
        """Track duration from cache; with probe=False never touches the file (safe for list redraws)."""
        if not path or not os.path.exists(path):
            return 0.0
        dur = cache_mgr.get_duration(path)
        if dur is not None:
            return float(dur)
        if not probe:
            return 0.0
        meta = read_track_metadata(path)
        dur = meta.get("duration", 0.0)
        cache_mgr.set_duration(path, dur)
        return float(dur)

    def _cached_metadata(self, path: str | None, probe: bool = True) -> Mapping[str, Any]:
        """Track tags from cache; with probe=False returns blanks instead of reading the file."""
        if not path or not os.path.exists(path):
            return {"title": "", "artist": "", "duration": 0.0}
        cached_meta = cache_mgr.get_metadata(path)
        if cached_meta is not None:
            return cached_meta
        if not probe:
            return {"title": "", "artist": "", "duration": 0.0}
        data = read_track_metadata(path)
        cache_mgr.set_metadata(path, data)
        return data.to_dict() if hasattr(data, "to_dict") else dict(data)

    def change_folder(self) -> None:
        f = filedialog.askdirectory(
            initialdir=self.library_folder, title="Choose Your Music Library Folder", parent=self.root
        )
        if f and os.path.isdir(f):
            self.library_folder = f
            self._save_settings()
            task_mgr.submit_task(self.library_ctrl.recover_stranded_deletes, f)
            self.refresh_library()
            self.set_status(f"Music folder changed to: {f}")
            if self._relink_playlists():
                self.refresh_playlist_listbox()

    def open_library_folder(self) -> None:
        try:
            os.startfile(self.library_folder)
        except Exception as e:
            dialogs.show_warning(self.root, "Error", f"Could not open folder:\n{e}")

    def add_external_file(self) -> None:
        files = filedialog.askopenfilenames(
            title="Select Music Files to Import",
            filetypes=[("Audio Files", ";".join(f"*{ext}" for ext in AUDIO_EXTS)), ("All Files", "*.*")],
            parent=self.root,
        )
        if not files:
            return
        # Same plan/confirmation as drag-and-drop, so existing songs are never silently overwritten.
        self._import_paths(list(files), source="selected")

    def _on_copy_external_done(self, copied: int) -> None:
        self._importing = False
        self.set_busy(False)
        self.refresh_library()
        self.notify_success(f"Added {copied} song(s) to your Library.")

    def _watch_library(self) -> None:
        if not getattr(self, "_is_shutting_down", False):
            try:
                # Compare names, not just the count, so renames done in File Explorer show up too.
                on_disk = self.library_ctrl.scan_files(self.library_folder)
                if on_disk != self.library_files:
                    self.refresh_library(preserve_view=True)
            except Exception:
                pass
            if hasattr(self, "root") and self.root and self.root.winfo_exists():
                self._timer_watch_library = self.root.after(12000, self._watch_library)

    def rename_library_file(self) -> None:
        sel = listbox_selection(self.listbox_lib)
        if not sel or sel[0] >= len(self.visible_files):
            dialogs.show_warning(self.root, "Select a Song", "Please click a song in the library listbox first.")
            return
        old_name = self.visible_files[sel[0]]
        old_path = os.path.join(self.library_folder, old_name)
        stem, ext = os.path.splitext(old_name)

        new_stem = simpledialog.askstring(
            "Rename Song", "Enter new name for this song:", initialvalue=stem, parent=self.root
        )
        if not new_stem or new_stem.strip() == stem:
            return
        new_name = sanitize_filename(new_stem) + ext
        new_path = os.path.join(self.library_folder, new_name)

        if os.path.exists(new_path) and new_path.lower() != old_path.lower():
            dialogs.show_warning(self.root, "File Exists", f"A file named '{new_name}' already exists in your library.")
            return

        was_playing_renamed = self.selected_file_path == old_path and (self.is_playing_main or self.is_playing_playlist)
        if was_playing_renamed:
            self.stop_audio()

        self._release_audio_file()
        time.sleep(0.05)

        try:
            new_path = self.library_ctrl.rename_file(old_path, new_name, self.playlists)
            self.save_playlists()
            self.refresh_playlist_listbox()
            self.refresh_library(select_name=new_name)
            if self.selected_file_path == old_path:
                self._load_track_ui(new_path, new_name)
            self.set_status(f"Renamed song to: {new_name}")
        except Exception as e:
            log_error(f"rename_library_file: {e}")
            show_friendly_error(self.root, e, "generic")

    def delete_library_file(self) -> None:
        """Move every selected song to the Recycle Bin (one confirmation, one Undo)."""
        sel = [i for i in listbox_selection(self.listbox_lib) if i < len(self.visible_files)]
        if not sel:
            dialogs.show_warning(self.root, "Select a Song", "Please click a song in the library listbox first.")
            return
        names = [self.visible_files[i] for i in sel]
        paths = [os.path.join(self.library_folder, name) for name in names]

        if len(names) == 1:
            title = "Delete Song"
            question = f"Are you sure you want to delete '{names[0]}'?\n\nIt will be moved"
        else:
            title = "Delete Songs"
            listed = "\n".join(f"• {name}" for name in names[:8])
            more = f"\n... and {len(names) - 8} more." if len(names) > 8 else ""
            question = (
                f"Are you sure you want to delete these {len(names)} songs?\n\n{listed}{more}\n\nThey will be moved"
            )
        if not dialogs.ask_yes_no(
            self.root,
            title,
            f"{question} safely to your Windows Recycle Bin.",
            yes="Delete" if len(names) == 1 else f"Delete {len(names)} songs",
            no="Keep",
            danger=True,
            default_yes=False,
        ):
            return

        if self.selected_file_path in paths:
            self.stop_audio(user=True)
            self.selected_file_path = None
            self.lbl_selected.config(text="No song selected")
            self.lbl_selected_artist.config(text="Click a song on the left to start")
            self._draw_placeholder_cover()
            self._current_peaks = []
            self._render_waveform(full_redraw=True)
            self._update_restore_original_button()

        self._release_audio_file()
        time.sleep(0.05)

        try:
            staged, failed = self.library_ctrl.stage_delete_many(paths, self.playlists)
        except Exception as e:
            log_error(f"delete_library_file: {e}")
            show_friendly_error(self.root, e, "generic")
            return
        self._resync_playlist_index()
        self.save_playlists()
        self.refresh_playlist_listbox()
        self.refresh_library()
        if staged:
            what = f"'{staged[0]}'" if len(staged) == 1 else f"{len(staged)} songs"
            self.show_undo(f"Moved {what} to Recycle Bin.", callback=self._undo_delete_file, timeout_sec=8)
        if failed:
            fe = friendly_error(failed[0][1], "generic")
            listed = "\n".join(f"• {name}" for name, _err in failed[:8])
            show_error(
                self.root,
                fe.title,
                f"These song(s) could not be deleted:\n{listed}\n\n{fe.message}",
                details="\n".join(f"{name}: {err}" for name, err in failed),
            )

    def _undo_delete_file(self) -> None:
        try:
            if self.library_ctrl.undo_delete(self.playlists):
                self._resync_playlist_index()
                self.save_playlists()
                self.refresh_playlist_listbox()
                self.refresh_library()
                self.set_status("Restored deleted song(s).", icon="↩️")
        except Exception as e:
            log_error(f"_undo_delete_file: {e}")

    def on_library_select(self, event: tk.Event[tk.Misc] | None = None) -> None:
        sel = listbox_selection(self.listbox_lib)
        if not sel or sel[0] >= len(self.visible_files):
            return
        filename = self.visible_files[sel[0]]
        new_path = os.path.join(self.library_folder, filename)
        if filename in self._fresh_songs:
            self._fresh_songs.discard(filename)
            self.listbox_lib.itemconfig(sel[0], background="")
        if new_path == self.selected_file_path:
            return

        if self._selection_debounce_timer:
            try:
                self.root.after_cancel(self._selection_debounce_timer)
            except Exception:
                pass
            self._selection_debounce_timer = None

        def _do_select() -> None:
            self.stop_audio()
            if self._load_track_ui(new_path, filename):
                self.set_status(f"Selected: {filename}. Press PLAY, or use the Waveform to trim.")

        self._selection_debounce_timer = self.root.after(100, _do_select)

    def _on_library_double_click(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if self._selection_debounce_timer:
            try:
                self.root.after_cancel(self._selection_debounce_timer)
            except Exception:
                pass
            self._selection_debounce_timer = None
        sel = listbox_selection(self.listbox_lib)
        if not sel or sel[0] >= len(self.visible_files):
            return
        filename = self.visible_files[sel[0]]
        new_path = os.path.join(self.library_folder, filename)
        if self.selected_file_path != new_path:
            self.stop_audio()
            if not self._load_track_ui(new_path, filename):
                return
        self.play_main()

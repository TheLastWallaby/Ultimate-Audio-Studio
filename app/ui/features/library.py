"""Step 1 library list: scanning, search, import, rename and delete with undo."""

from __future__ import annotations

import contextlib
import logging
import os
import time
import tkinter as tk
from collections.abc import Mapping, Sequence
from pathlib import Path
from tkinter import filedialog
from typing import Any

from app.config import AUDIO_EXTS, sanitize_filename
from app.controllers.library_controller import ImportResult
from app.core.cache_manager import cache_mgr
from app.core.errors import friendly_error
from app.core.metadata import read_track_metadata
from app.core.task_manager import task_mgr
from app.models import SongRow
from app.platform_utils import has_recycle_bin
from app.ui import dialogs
from app.ui.components import listbox_selection
from app.ui.error_dialog import show_error, show_friendly_error
from app.ui.features.base import NEW_SONG_ROW_BG, UNDO_SECONDS, AppBase

logger = logging.getLogger(__name__)

# An import of more songs than this, or of more data, is confirmed first: a whole Music folder dropped
# by accident would otherwise be copied (and fill the disk) without a word.
LARGE_IMPORT_SONGS = 200
LARGE_IMPORT_BYTES = 2 * 1024**3
LIBRARY_WATCH_MS = 12000
# Up to this many rows whose details are not in memory yet are looked up at once (a few disk checks
# each, no file is opened). Above it the rows show their file names until the background reader has
# been through them, so a long list is never drawn at the speed of the disk.
SYNC_ROW_LIMIT = 50


def _row_label(title: str, artist: str, filename: str) -> str:
    """'Title — Artist' from the tags when present, else the file name without its extension."""
    title = title.strip() or Path(filename).stem
    artist = artist.strip()
    if artist.endswith(" - Topic"):  # YouTube's auto-generated artist channels
        artist = artist[: -len(" - Topic")]
    if artist and artist.lower() not in title.lower():
        return f"{title} — {artist}"
    return title


def _placeholder_row(path: str) -> SongRow:
    """The row for a song whose details have not been read yet: its file name."""
    song = Path(path)
    return SongRow(label=song.stem, searchable=song.name.lower())


def _row_from_details(path: str, details: Mapping[str, Any]) -> SongRow:
    name = Path(path).name
    title, artist = str(details.get("title") or ""), str(details.get("artist") or "")
    return SongRow(
        label=_row_label(title, artist, name),
        seconds=float(details.get("duration") or 0.0),
        searchable=f"{name} {title} {artist}".lower(),
    )


def _remembered_row(path: str) -> SongRow | None:
    """The row for ``path`` from the details cache (a few disk checks); None when they were never read."""
    # The cache first: it answers None for a song that is gone as well as for one never read, so a
    # song that vanishes while this runs is reported missing and never handed on to the tag reader.
    details = cache_mgr.get_metadata(path)
    if details is not None:
        return _row_from_details(path, details)
    song = Path(path)
    if not song.is_file():
        return SongRow(label=song.stem, searchable=song.name.lower(), missing=True)
    return None


def read_song_row(path: str) -> SongRow:
    """The row for ``path``, reading the song's tags when they are not cached yet (worker threads only)."""
    row = _remembered_row(path)
    if row is not None:
        return row
    details = read_track_metadata(path)
    cache_mgr.set_metadata(path, details)
    return _row_from_details(path, details.to_dict())


def _size_text(size_bytes: int) -> str:
    """A size the way people say it: "350 MB", "4.2 GB"."""
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.1f} GB"
    return f"{max(1, round(size_bytes / 1024**2))} MB"


def _same_song(a: str | None, b: str | None) -> bool:
    """True when both paths name the same file (Windows compares paths without regard to case)."""
    if not a or not b:
        return False
    return str(Path(a).absolute()).casefold() == str(Path(b).absolute()).casefold()


class LibraryMixin(AppBase):
    """Step 1 library list: scanning, search, import, rename and delete with undo."""

    # A Library song that was clicked while another one was playing or paused. A click never cuts
    # the music off; this song is loaded once PLAY or STOP is pressed, or the music ends.
    _pending_library_song: str | None = None
    # The chosen Library folder when it was offline at start-up (a USB drive, memory card or network
    # share). The window then shows the default folder, but this one is what gets saved.
    _unavailable_library_folder: str | None = None
    # True while the Library watcher is reading the folder on a worker (one read at a time).
    _library_scan_running = False

    def _known_rows(self) -> dict[str, SongRow]:
        """The rows read so far, by path (as the lists spell it)."""
        rows: dict[str, SongRow] | None = vars(self).get("_song_rows")
        if rows is None:
            rows = self._song_rows = {}
        return rows

    def _song_rows_for(self, paths: Sequence[str]) -> list[SongRow]:
        """Rows for drawing a list now, from memory.

        A few rows that are not in memory yet are looked up at once (``SYNC_ROW_LIMIT``); when there
        are many, they show their file names until the background reader has filled them in.
        """
        known = self._known_rows()
        unknown = [path for path in paths if path not in known]
        if len(unknown) <= SYNC_ROW_LIMIT:
            for path in unknown:
                row = _remembered_row(path)
                if row is not None:
                    known[path] = row
        return [known.get(path) or _placeholder_row(path) for path in paths]

    def _library_row_path(self, filename: str) -> str:
        """Path of a Library row, in the exact form the player and the playlists keep it in.

        ``os.path.join`` is deliberate: older code compares these strings with ``==``, and a
        ``Path`` would spell a folder chosen with the folder dialog ("C:/Music") differently.
        """
        return os.path.join(self.library_folder, filename)

    def _cancel_selection_debounce(self) -> None:
        """Drop a click that has not been acted on yet (a newer click or a double-click replaces it)."""
        if self._selection_debounce_timer:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._selection_debounce_timer)
            self._selection_debounce_timer = None

    def _load_pending_selection(self) -> bool:
        """Load the Library song that was clicked while another was in the player; True when loaded."""
        path, self._pending_library_song = self._pending_library_song, None
        if not path or _same_song(path, self.selected_file_path) or not Path(path).is_file():
            return False
        return self._load_track_ui(path, Path(path).name)

    def _handle_dropped_files(self, paths: list[str]) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        self._import_paths(paths, source="dropped")

    def _import_paths(self, paths: list[str], source: str = "selected") -> None:
        """Copy audio files/folders into the Library, asking before replacing existing songs.

        A dropped folder can hold thousands of files (or sit on a CD or another computer), so it is
        searched on a worker; the questions are asked once the songs in it are known.
        """
        if self._importing:
            self.set_status("Still adding songs to your Library. Add these again when it has finished.", icon="⏳")
            return
        self._importing = True
        self.set_busy(True, f"Looking for songs in the {source} files...")
        library = self.library_folder

        def _worker() -> None:
            try:
                planned, dest_exists = self.library_ctrl.build_import_plan(paths, library)
                total_bytes = sum(Path(src).stat().st_size for src, _dest in planned)
            except OSError as err:
                logger.error("Could not read the %s files: %s", source, err)
                self._safe_after(0, self._import_plan_failed, err)
                return
            self._safe_after(0, self._confirm_import, planned, dest_exists, source, total_bytes)

        task_mgr.submit_task(_worker)

    def _import_plan_failed(self, err: OSError) -> None:
        """The files to import could not be read (a CD or drive removed meanwhile)."""
        self._importing = False
        self.set_busy(False, "No songs were added to your Library.")
        show_friendly_error(self.root, err, "import")

    def _confirm_import(
        self, planned: list[tuple[str, str]], dest_exists: list[str], source: str, total_bytes: int
    ) -> None:
        """Ask the import questions (all of them before anything is copied), then start copying."""
        self._importing = False
        self.set_busy(False)
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

        if (len(planned) > LARGE_IMPORT_SONGS or total_bytes > LARGE_IMPORT_BYTES) and not dialogs.ask_yes_no(
            self.root,
            "Add All These Songs?",
            f"The {source} files hold {len(planned)} songs ({_size_text(total_bytes)}).\n\n"
            "They are copied into your Library; the originals stay where they are.",
            yes=f"Add {len(planned)} songs",
            no="Cancel",
            default_yes=False,
        ):
            self.set_status("No songs were added to your Library.")
            return

        already_there = {name.casefold() for name in dest_exists}
        if dest_exists:
            gone = (
                "go to the Recycle Bin"
                if has_recycle_bin(self.library_folder)
                else "are deleted for good (this drive has no Recycle Bin)"
            )
            replace = dialogs.ask_yes_no(
                self.root,
                "Songs Already in Your Library",
                f"{len(dest_exists)} of the {source} song(s) are already in your Library.\n\n"
                f"If you replace them, the copies you have now {gone}.",
                yes="Replace them",
                no="Keep the ones I have",
                default_yes=False,
            )
            if not replace:
                planned = [(s, d) for (s, d) in planned if Path(d).name.casefold() not in already_there]

        if not planned:
            return

        # The song in the player is held open, and Windows will not move an open file to the Recycle Bin.
        if any(
            _same_song(dest, self.selected_file_path)
            for _src, dest in planned
            if Path(dest).name.casefold() in already_there
        ):
            self.stop_audio()
            self._release_audio_file()

        self._importing = True
        self.set_busy(True, f"Adding {len(planned)} song(s) to your Library...")
        self.library_ctrl.import_external_files(
            planned,
            is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
            on_done=lambda result: self._safe_after(0, self._on_copy_external_done, result),
        )

    def refresh_library(
        self, select_name: str | None = None, preserve_view: bool = False, files: list[str] | None = None
    ) -> None:
        """Show the folder's songs immediately; tags and durations fill in from a background scan.

        preserve_view keeps the current selection and scroll position (for background refreshes).
        ``files`` is the folder's song list when the caller has just read it (the Library watcher).
        """
        self.library_files = self.library_ctrl.scan_files(self.library_folder) if files is None else files
        self.apply_library_filter(select_name, preserve_view=preserve_view)
        self._warm_library_metadata()

    def _reveal_new_song(self, filename: str, keep_trim_work: bool = False) -> bool:
        """Show a song that was just added (download, saved clip) and return True if it was loaded.

        Selecting a row in code does not load it into the player, so PLAY used to play the previous
        song while the new one was highlighted. When the player is idle the new song is highlighted
        *and* loaded; while something is playing or paused that is left alone, and the new row is only
        tinted green (not selected), so the highlight never disagrees with what PLAY will do.

        ``keep_trim_work`` also leaves the player alone while the loaded song has clip marks or a
        Volume Boost set: a download that finishes meanwhile must not throw that work away.
        """
        path = self._library_row_path(filename)
        player_in_use = self.is_playing_main or self.is_playing_playlist or self.is_paused
        if keep_trim_work and self._has_trim_work():
            player_in_use = True
        if not player_in_use and Path(path).is_file():
            self.refresh_library(select_name=filename)
            self.stop_audio()
            return self._load_track_ui(path, filename)
        self._fresh_songs.add(filename)
        self.refresh_library(preserve_view=True)
        if filename in self.visible_files:
            self.listbox_lib.see(self.visible_files.index(filename))
        return False

    def _warm_library_metadata(self) -> None:
        """Read every listed song's details on a worker, then redraw the lists if anything changed.

        This is the only place the lists' details come from the disk: tags of songs that were never
        read, and whether each song is still there. Drawing a list uses what was read here.
        """
        paths = [self._library_row_path(name) for name in self.library_files]
        listed = set(paths)
        paths += [path for path in self.playlist_files if path not in listed]
        known = self._known_rows()
        unread = sum(1 for path in paths if path not in known)
        self._library_meta_gen += 1
        gen = self._library_meta_gen
        announce = unread > 20
        if announce:
            self.set_status(f"Reading song details for {unread} songs...", icon="⏳")

        def _worker() -> None:
            rows: dict[str, SongRow] = {}
            for path in paths:
                if self._is_shutting_down or gen != self._library_meta_gen:
                    return
                try:
                    rows[path] = read_song_row(path)
                except OSError as err:  # the drive went away mid-read: the row keeps its file name
                    logger.warning("Could not read the details of %s: %s", Path(path).name, err)
                    rows[path] = _placeholder_row(path)
            self._safe_after(0, self._on_library_metadata_ready, gen, rows, announce)

        task_mgr.submit_task(_worker)

    def _on_library_metadata_ready(self, gen: int, rows: dict[str, SongRow], announce: bool) -> None:
        """Keep the rows the worker read; redraw both lists when they differ from what is showing."""
        if gen != self._library_meta_gen or self._is_shutting_down:
            return
        changed = rows != self._known_rows()
        self._song_rows = rows
        if changed:
            self.apply_library_filter(preserve_view=True)
            pl_selection = listbox_selection(self.listbox_pl)
            pl_view = self.listbox_pl.yview()[0]
            self.refresh_playlist_listbox()
            for idx in pl_selection:
                self.listbox_pl.selection_set(idx)
            self.listbox_pl.yview_moveto(pl_view)
        if announce:
            self.set_status("Ready. Select a song on the left to play or trim.")

    def _display_name(self, path: str, filename: str | None = None) -> str:
        """Friendly row text: 'Title — Artist' from tags when present, else the file name without extension."""
        meta = self._cached_metadata(path, probe=False)
        return _row_label(str(meta.get("title") or ""), str(meta.get("artist") or ""), filename or Path(path).name)

    def apply_library_filter(self, select_name: str | None = None, preserve_view: bool = False) -> None:
        """Draw the Library list for what is typed in the search box, from memory (no disk access)."""
        query = self.entry_search.get().strip().lower() if hasattr(self, "entry_search") else ""
        prev_selected: set[str] = set()
        prev_view = None
        if preserve_view:
            prev_selected = {
                self.visible_files[i] for i in listbox_selection(self.listbox_lib) if i < len(self.visible_files)
            }
            prev_view = self.listbox_lib.yview()[0]
        paths = [self._library_row_path(f) for f in self.library_files]
        rows = dict(zip(self.library_files, self._song_rows_for(paths), strict=True))
        self.visible_files = [f for f in self.library_files if query in rows[f].searchable]

        self.listbox_lib.delete(0, tk.END)
        select_idx = None
        for idx, f in enumerate(self.visible_files):
            self.listbox_lib.insert(tk.END, rows[f].text)
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
        self._show_library_hint()

    def _show_library_hint(self) -> None:
        """Say what to do next over an empty Library list (no songs yet, or none match the search)."""
        if not hasattr(self, "lbl_lib_empty"):
            return
        if self.visible_files:
            self.lbl_lib_empty.place_forget()
            return
        if self.library_files:
            typed = self.entry_search.get().strip()
            text = f"No songs match '{typed}'.\n\nClick ✕ next to the search box to show all your songs."
        else:
            text = (
                "Your Library is empty.\n\n"
                "Type a song name at the top and click 'Download MP3',\n"
                "or click 'Add Music from PC' below."
            )
        self.lbl_lib_empty.config(text=text)
        self.lbl_lib_empty.place(relx=0.5, rely=0.4, anchor="center")

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
        """Let the user pick the Library folder; this is the only way a saved choice is replaced."""
        f = filedialog.askdirectory(
            initialdir=self.library_folder, title="Choose Your Music Library Folder", parent=self.root
        )
        if f and os.path.isdir(f):
            self.library_folder = f
            self._unavailable_library_folder = None
            self._save_settings()
            task_mgr.submit_task(self.library_ctrl.recover_stranded_deletes, f)
            self.refresh_library()
            self.set_status(f"Music folder changed to: {f}")
            if self._relink_playlists():
                self.refresh_playlist_listbox()

    def open_library_folder(self) -> None:
        """Show the Library folder in File Explorer (the 📁 Folder button)."""
        try:
            os.startfile(self.library_folder)
        except OSError as err:
            logger.warning("Could not open the Library folder %s: %s", self.library_folder, err)
            dialogs.show_warning(
                self.root,
                "Folder Could Not Be Opened",
                f"Your music folder could not be opened:\n{self.library_folder}\n\n"
                "If it is on a USB drive or memory card, check that it is plugged in, then try again.",
            )

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

    def _on_copy_external_done(self, result: ImportResult) -> None:
        """Report an import truthfully: what was added, what was renamed, and what could not be copied."""
        self._importing = False
        self.set_busy(False)
        for name in result.replaced:
            # A replaced song keeps its name, so details and pictures remembered for it are now wrong.
            path = self._library_row_path(name)
            cache_mgr.invalidate(path)
            self.library_ctrl.invalidate_search_index(path)
            self._art_cache.pop(path, None)
        self.refresh_library(preserve_view=True)
        reloaded = next(
            (n for n in result.replaced if _same_song(self._library_row_path(n), self.selected_file_path)), None
        )
        if reloaded:
            self._load_track_ui(self._library_row_path(reloaded), reloaded)
        if result.copied:
            message = f"Added {len(result.copied)} song(s) to your Library."
            if len(result.renamed) == 1:
                message += f" One had the same name as another song, so it was added as '{result.renamed[0]}'."
            elif result.renamed:
                message += (
                    f" {len(result.renamed)} had the same name as another song, so they were added with a "
                    f"number, like '{result.renamed[0]}'."
                )
            bin_note = "is in the Recycle Bin" if has_recycle_bin(self.library_folder) else "was deleted"
            if len(result.replaced) == 1:
                message += f" The older copy of '{result.replaced[0]}' {bin_note}."
            elif result.replaced:
                were = "are in the Recycle Bin" if bin_note.startswith("is") else "were deleted"
                message += f" The {len(result.replaced)} older copies they replaced {were}."
            self.notify_success(message)
        else:
            self.set_status("No songs were added to your Library.", icon="⚠️")
        if result.failed:
            listed = "\n".join(f"• {name}" for name in result.failed[:6])
            if len(result.failed) > 6:
                listed += f"\n... and {len(result.failed) - 6} more."
            dialogs.show_warning(
                self.root,
                "Some Songs Could Not Be Added",
                f"{len(result.failed)} song(s) could not be copied into your Library:\n\n{listed}\n\n"
                "• If they are on a CD, USB drive or phone, check that it is still connected.\n"
                "• Check that this computer has free disk space.\n"
                "• Then add them again.",
            )

    def _watch_library(self) -> None:
        """Every few seconds, notice songs added, renamed or removed in File Explorer.

        The folder is read on a worker: a Library on a network folder or a sleeping USB drive can take
        seconds to answer, and the window must not freeze meanwhile.
        """
        if self._is_shutting_down:
            return
        if not self._library_scan_running:
            self._library_scan_running = True
            folder = self.library_folder

            def _worker() -> None:
                on_disk = self.library_ctrl.scan_files(folder)
                self._safe_after(0, self._library_scanned, folder, on_disk)

            if task_mgr.submit_task(_worker) is None:
                self._library_scan_running = False
        with contextlib.suppress(tk.TclError):
            self._timer_watch_library = self.root.after(LIBRARY_WATCH_MS, self._watch_library)

    def _library_scanned(self, folder: str, on_disk: list[str]) -> None:
        """Show what the watcher found, unless the Library folder was changed meanwhile."""
        self._library_scan_running = False
        # Compare names, not just the count, so renames done in File Explorer show up too.
        if folder == self.library_folder and on_disk != self.library_files:
            self.refresh_library(preserve_view=True, files=on_disk)

    def rename_library_file(self) -> None:
        """Rename the selected song; its playlist entries and its trim backup follow it."""
        sel = listbox_selection(self.listbox_lib)
        if not sel or sel[0] >= len(self.visible_files):
            dialogs.show_warning(self.root, "Select a Song", "Please click a song in your Library first.")
            return
        old_name = self.visible_files[sel[0]]
        old_path = self._library_row_path(old_name)
        old = Path(old_name)

        answer = dialogs.ask_text(
            self.root, "Rename Song", "Type a new name for this song:", initial=old.stem, ok="Rename song"
        )
        if answer is None or answer.text == old.stem:
            return
        new_name = sanitize_filename(answer.text) + old.suffix
        new_path = self._library_row_path(new_name)

        if Path(new_path).exists() and not _same_song(new_path, old_path):
            dialogs.show_warning(
                self.root,
                "Name Already Used",
                f"A song named '{new_name}' is already in your Library.\n\nPlease choose a different name.",
            )
            return

        # Only the song in the player is held open by it. Stopping for any other song would cut the
        # music off (and make a playlist skip ahead) for no reason.
        is_loaded = _same_song(self.selected_file_path, old_path)
        if is_loaded:
            self.stop_audio()
            time.sleep(0.05)  # let Windows release the file before it is renamed

        try:
            new_path = self.library_ctrl.rename_file(old_path, new_name, self.playlists)
        except OSError as err:
            logger.error("Could not rename %s: %s", old_name, err)
            show_friendly_error(self.root, err, "generic")
            return
        if _same_song(self._pending_library_song, old_path):
            self._pending_library_song = new_path
        self.save_playlists()
        self.refresh_playlist_listbox()
        self.refresh_library(select_name=new_name)
        if is_loaded:
            self._load_track_ui(new_path, new_name)
        self.set_status(f"Renamed song to: {new_name}")

    def delete_library_file(self) -> None:
        """Move every selected song to the Recycle Bin (one confirmation, one Undo)."""
        sel = [i for i in listbox_selection(self.listbox_lib) if i < len(self.visible_files)]
        if not sel:
            dialogs.show_warning(self.root, "Select a Song", "Please click a song in your Library first.")
            return
        names = [self.visible_files[i] for i in sel]
        paths = [self._library_row_path(name) for name in names]
        one = len(names) == 1
        # USB sticks, memory cards and network folders have no Recycle Bin: there, deleting is for good.
        recyclable = has_recycle_bin(self.library_folder)

        if one:
            title = "Delete Song"
            question = f"Are you sure you want to delete '{names[0]}'?"
        else:
            title = "Delete Songs"
            listed = "\n".join(f"• {name}" for name in names[:8])
            more = f"\n... and {len(names) - 8} more." if len(names) > 8 else ""
            question = f"Are you sure you want to delete these {len(names)} songs?\n\n{listed}{more}"
        if recyclable:
            where = f"{'It' if one else 'They'} will be moved safely to your Windows Recycle Bin."
        else:
            where = (
                f"This drive has no Recycle Bin, so {'it is' if one else 'they are'} deleted for good. "
                f"You can still press Undo for {UNDO_SECONDS:.0f} seconds afterwards."
            )
        if not dialogs.ask_yes_no(
            self.root,
            title,
            f"{question}\n\n{where}",
            yes="Delete" if len(names) == 1 else f"Delete {len(names)} songs",
            no="Keep",
            danger=True,
            default_yes=False,
        ):
            return

        # Only the song in the player is held open by it; music from any other song keeps playing.
        if any(_same_song(self.selected_file_path, path) for path in paths):
            self.stop_audio(user=True)
            self.selected_file_path = None
            self.lbl_selected.config(text="No song selected")
            self.lbl_selected_artist.config(text="Click a song on the left to start")
            self._draw_placeholder_cover()
            self._current_peaks = []
            self._render_waveform(full_redraw=True)
            self._update_restore_original_button()
            time.sleep(0.05)  # let Windows release the file before it is moved
        if any(_same_song(self._pending_library_song, path) for path in paths):
            self._pending_library_song = None

        try:
            staged, failed = self.library_ctrl.stage_delete_many(paths, self.playlists)
        except OSError as err:
            logger.error("Could not delete the selected songs: %s", err)
            show_friendly_error(self.root, err, "generic")
            return
        self._resync_playlist_index()
        self.save_playlists()
        self.refresh_playlist_listbox()
        # The list stays where it was scrolled to: deleting several songs one by one from the middle
        # of a long Library must not mean scrolling back down after each.
        self.refresh_library(preserve_view=True)
        if staged:
            what = f"'{staged[0]}'" if len(staged) == 1 else f"{len(staged)} songs"
            them = "it" if len(staged) == 1 else "them"
            done = (
                f"Moved {what} to Recycle Bin." if recyclable else f"Deleted {what}. Press Undo to bring {them} back."
            )
            self.show_undo(done, callback=self._undo_delete_file)
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
        """Undo button after a delete: bring the song(s) back and say so (or offer to try again)."""
        result = self.library_ctrl.undo_delete(self.playlists)
        if not result:
            self.show_undo("The song(s) could not be put back. Press Undo to try again.", self._undo_delete_file)
            return
        self._resync_playlist_index()
        self.save_playlists()
        self.refresh_playlist_listbox()
        self.refresh_library(preserve_view=True)
        message = "Restored deleted song(s)."
        if result.renamed:
            message += (
                f" A newer song has taken the old name, so one came back as '{result.renamed[0]}'."
                if len(result.renamed) == 1
                else f" Newer songs have taken {len(result.renamed)} of the old names, so those came back "
                f"with a number, like '{result.renamed[0]}'."
            )
        self.set_status(message, icon="↩️")

    def on_library_select(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        """A Library row was clicked: load it into the player, unless a song is playing or paused.

        Songs are also clicked to add them to a playlist or to delete them, so a click must never
        cut the music off. While the player is in use the clicked song is only remembered; it is
        loaded when PLAY or STOP is pressed or the music ends (double-click plays it at once).
        """
        sel = listbox_selection(self.listbox_lib)
        if len(sel) > 1:
            # Several songs are being picked (Ctrl or Shift) to add to a playlist or to delete. The
            # player keeps its song: loading the topmost one would throw away the clip marks.
            self._cancel_selection_debounce()
            return
        if not sel or sel[0] >= len(self.visible_files):
            return
        filename = self.visible_files[sel[0]]
        new_path = self._library_row_path(filename)
        if filename in self._fresh_songs:
            self._fresh_songs.discard(filename)
            self.listbox_lib.itemconfig(sel[0], background="")
        self._cancel_selection_debounce()
        if new_path == self.selected_file_path:
            self._pending_library_song = None
            return

        if self.is_playing_main or self.is_playing_playlist or self.is_paused:
            self._pending_library_song = new_path
            self.set_status(
                f"Selected: {self._display_name(new_path, filename)}. The current song keeps playing; "
                "press PLAY to switch to this one."
            )
            return
        self._pending_library_song = None

        def _do_select() -> None:
            self.stop_audio()
            if self._load_track_ui(new_path, filename):
                self.set_status(f"Selected: {filename}. Press PLAY, or use the Waveform to trim.")

        self._selection_debounce_timer = self.root.after(100, _do_select)

    def _on_library_double_click(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        """Play the double-clicked song now, replacing whatever is playing."""
        self._cancel_selection_debounce()
        sel = listbox_selection(self.listbox_lib)
        if not sel or sel[0] >= len(self.visible_files):
            return
        filename = self.visible_files[sel[0]]
        new_path = self._library_row_path(filename)
        self._pending_library_song = None
        if self.selected_file_path != new_path:
            self.stop_audio()
            if not self._load_track_ui(new_path, filename):
                return
        self.play_main()

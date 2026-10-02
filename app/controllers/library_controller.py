"""Library controller handling scanning, search filtering, renaming, file import, and safe deletion with undo."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import shutil
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import AUDIO_EXTS, log_error
from app.core.file_utils import copy_file_atomic
from app.core.task_manager import task_mgr
from app.services.clipper import original_backup_path

try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None

logger = logging.getLogger(__name__)

MetadataFn = Callable[[str], Mapping[str, Any]]

UNDO_DIR_NAME = ".undo_trash"


@dataclass(slots=True, frozen=True)
class ImportResult:
    """What an import did, by file name, so the window can report it truthfully."""

    copied: tuple[str, ...] = ()  # songs now in the Library (under these names)
    renamed: tuple[str, ...] = ()  # the copied songs that were given a new name to avoid a clash
    failed: tuple[str, ...] = ()  # songs that could not be copied


def _path_key(path: Path) -> str:
    """A path as Windows compares it (absolute, case-insensitive)."""
    return str(path.absolute()).casefold()


def _free_name(dest: Path, taken_names: set[str]) -> Path:
    """``dest`` renamed to the first "Name (2).ext"-style name that neither exists nor is planned."""
    number = 2
    while True:
        candidate = dest.with_name(f"{dest.stem} ({number}){dest.suffix}")
        if candidate.name.casefold() not in taken_names and not candidate.exists():
            return candidate
        number += 1


def _hide_path(path: str) -> None:
    """Mark a folder hidden on Windows so the undo area does not clutter File Explorer."""
    if os.name == "nt":
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # FILE_ATTRIBUTE_HIDDEN


def _trash_staged_file(staging_path: str, orig_path: str | None) -> None:
    """Move a staged file back to its original location, then to the Recycle Bin.

    Restoring it from the Recycle Bin later then returns it to the user's music folder under its
    real name. If the original name has been reused meanwhile, the staged copy is trashed in place.
    """
    if not os.path.exists(staging_path):
        return
    target = staging_path
    if orig_path and not os.path.exists(orig_path):
        try:
            shutil.move(staging_path, orig_path)
            target = orig_path
        except OSError as e:
            log_error(f"restore-before-trash failed for {staging_path}: {e}")
    try:
        if _send2trash:
            _send2trash(target)
        else:
            os.remove(target)
    except Exception as e:
        log_error(f"flush_pending_trash: {e}")
    with contextlib.suppress(OSError):
        os.rmdir(os.path.dirname(staging_path))


class LibraryController:
    """Manages audio files in the music library, search queries, renaming, and safe deletion with undo."""

    def __init__(self, app: object) -> None:
        self.app = app
        # Each entry: (orig_path, staging_path, filename, [(playlist_name, [indices])])
        self._pending_deletes: list[tuple[str, str, str, list[tuple[str, list[int]]]]] = []
        self._search_index: dict[str, str] = {}

    def scan_files(self, folder: str) -> list[str]:
        """Return sorted list of audio filenames in folder."""
        if not folder or not os.path.exists(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except Exception:
                return []
        try:
            return sorted([f for f in os.listdir(folder) if f.lower().endswith(AUDIO_EXTS)], key=lambda s: s.lower())
        except Exception as e:
            log_error(f"LibraryController.scan_files: {e}")
            return []

    def scan_and_filter(
        self, folder: str, query: str = "", get_metadata_fn: MetadataFn | None = None
    ) -> tuple[list[str], list[str]]:
        """Scan folder for audio files and filter by query."""
        files = self.scan_files(folder)
        visible = self.filter_files(files, folder, query, get_metadata_fn=get_metadata_fn)
        return files, visible

    @staticmethod
    def build_import_plan(
        paths: Sequence[str | Path], library_folder: str | Path
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Plan the copies for dropped or selected files/folders.

        Returns the ``(source, destination)`` pairs and the names that are already in the Library.
        Two different files with the same name are both imported: the later one gets a free
        "Name (2).mp3"-style name instead of overwriting the first.
        """
        library = Path(library_folder)
        sources: list[Path] = []
        for raw in paths:
            path = Path(raw)
            if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
                sources.append(path)
            elif path.is_dir():
                sources.extend(f for f in sorted(path.rglob("*")) if f.is_file() and f.suffix.lower() in AUDIO_EXTS)

        planned: list[tuple[str, str]] = []
        dest_exists: list[str] = []
        seen_sources: set[str] = set()
        taken_names: set[str] = set()
        for src in sources:
            src_key = _path_key(src)
            if src_key in seen_sources:  # the same file dropped twice (a folder and a file inside it)
                continue
            seen_sources.add(src_key)
            dest = library / src.name
            if _path_key(dest) == src_key:  # already in the Library folder
                continue
            if dest.name.casefold() in taken_names:
                dest = _free_name(dest, taken_names)
            taken_names.add(dest.name.casefold())
            planned.append((str(src), str(dest)))
            if dest.exists():
                dest_exists.append(dest.name)
        return planned, dest_exists

    def update_search_index(self, folder: str, files: Sequence[str], get_metadata_fn: MetadataFn | None = None) -> None:
        """Pre-index searchable strings for library files."""
        for f in files:
            f_path = os.path.join(folder, f)
            if f_path not in self._search_index:
                meta = get_metadata_fn(f_path) if get_metadata_fn else {}
                title = meta.get("title", "") if meta else ""
                artist = meta.get("artist", "") if meta else ""
                self._search_index[f_path] = f"{f} {title} {artist}".lower()

    def invalidate_search_index(self, filepath: str | None = None) -> None:
        """Invalidate search index for single file or entire library."""
        if filepath:
            self._search_index.pop(filepath, None)
            norm = os.path.abspath(filepath).lower()
            keys_to_del = [k for k in self._search_index if os.path.abspath(k).lower() == norm]
            for k in keys_to_del:
                self._search_index.pop(k, None)
        else:
            self._search_index.clear()

    def filter_files(
        self, files: Sequence[str], folder: str, query: str, get_metadata_fn: MetadataFn | None = None
    ) -> list[str]:
        """Filter files by search query matching filename, title, or artist via pre-indexed lookup."""
        q = (query or "").strip().lower()
        if not q:
            return list(files)
        matches = []
        for f in files:
            f_path = os.path.join(folder, f)
            searchable = self._search_index.get(f_path)
            if searchable is None:
                meta = get_metadata_fn(f_path) if get_metadata_fn else {}
                title = meta.get("title", "") if meta else ""
                artist = meta.get("artist", "") if meta else ""
                searchable = f"{f} {title} {artist}".lower()
                self._search_index[f_path] = searchable
            if q in searchable:
                matches.append(f)
        return matches

    def rename_file(self, old_path: str, new_name: str, playlists: dict[str, list[str]]) -> str:
        """Rename file on disk and update all playlists referencing it."""
        folder = os.path.dirname(old_path)
        new_path = os.path.join(folder, new_name)
        if os.path.exists(new_path) and new_path.lower() != old_path.lower():
            raise FileExistsError(f"A file named '{new_name}' already exists.")

        os.replace(old_path, new_path)
        # A trimmed song's untrimmed backup follows the song, so "Restore Original Song" keeps working.
        old_backup = original_backup_path(old_path)
        if os.path.isfile(old_backup):
            try:
                os.replace(old_backup, original_backup_path(new_path))
            except OSError as e:
                log_error(f"rename backup {old_backup}: {e}")
        self.invalidate_search_index(old_path)
        self.invalidate_search_index(new_path)
        # Update references in playlists
        for _pl_name, tracks in playlists.items():
            for idx, t in enumerate(tracks):
                if os.path.abspath(t).lower() == os.path.abspath(old_path).lower():
                    tracks[idx] = new_path
        return new_path

    def stage_delete_with_undo(self, filepath: str, playlists: dict[str, list[str]]) -> str:
        """Stage one file for deletion (see ``stage_delete_many``); returns its file name."""
        self.flush_pending_trash()
        return self._stage_one_delete(filepath, playlists)

    def stage_delete_many(
        self, filepaths: list[str], playlists: dict[str, list[str]]
    ) -> tuple[list[str], list[tuple[str, OSError]]]:
        """Stage several files for deletion as one undoable batch.

        Returns the staged file names and ``(file name, error)`` for files that could not be moved
        (for example because another program has them open); those stay in the Library untouched.
        """
        self.flush_pending_trash()
        staged: list[str] = []
        failed: list[tuple[str, OSError]] = []
        for filepath in filepaths:
            try:
                staged.append(self._stage_one_delete(filepath, playlists))
            except OSError as e:
                log_error(f"stage delete {filepath}: {e}")
                failed.append((os.path.basename(filepath), e))
        return staged, failed

    def _stage_one_delete(self, filepath: str, playlists: dict[str, list[str]]) -> str:
        """Move a file into a hidden undo area and take it out of the playlists (restorable by undo).

        The file keeps its real name inside a unique sub-folder, and is moved back to its original
        location before going to the Recycle Bin, so a later 'Restore' from the Bin works normally.
        """
        folder = os.path.dirname(filepath)
        filename = os.path.basename(filepath)
        staging_dir = os.path.join(folder, UNDO_DIR_NAME, uuid.uuid4().hex)
        os.makedirs(staging_dir, exist_ok=True)
        _hide_path(os.path.join(folder, UNDO_DIR_NAME))
        staging_path = os.path.join(staging_dir, filename)

        # Move first: if the file is locked, playlists must stay as they were.
        try:
            shutil.move(filepath, staging_path)
        except OSError:
            with contextlib.suppress(OSError):
                os.rmdir(staging_dir)
            raise

        # The untrimmed backup of a trimmed song goes (and comes back on Undo) with it.
        backup = original_backup_path(filepath)
        if os.path.isfile(backup):
            try:
                shutil.move(backup, original_backup_path(staging_path))
            except OSError as e:
                log_error(f"stage backup {backup}: {e}")

        # Remember playlists where this track was present
        target = os.path.abspath(filepath).lower()
        affected_playlists = []
        for pl_name, tracks in playlists.items():
            indices = [i for i, t in enumerate(tracks) if os.path.abspath(t).lower() == target]
            if indices:
                affected_playlists.append((pl_name, indices))
                playlists[pl_name] = [t for t in tracks if os.path.abspath(t).lower() != target]

        self.invalidate_search_index(filepath)
        self._pending_deletes.append((filepath, staging_path, filename, affected_playlists))
        return filename

    def undo_delete(self, playlists: dict[str, list[str]]) -> bool:
        """Restore the staged deleted file(s) back to the library and playlists."""
        if not self._pending_deletes:
            return False
        pending, self._pending_deletes = self._pending_deletes, []
        restored = False
        # Newest first, so playlist positions are restored in the reverse order they were removed.
        for orig_path, staging_path, _filename, affected_playlists in reversed(pending):
            try:
                if os.path.exists(staging_path):
                    shutil.move(staging_path, orig_path)
                    staged_backup = original_backup_path(staging_path)
                    if os.path.exists(staged_backup):
                        shutil.move(staged_backup, original_backup_path(orig_path))
                    with contextlib.suppress(OSError):
                        os.rmdir(os.path.dirname(staging_path))
                self.invalidate_search_index(orig_path)
                for pl_name, indices in affected_playlists:
                    if pl_name in playlists:
                        for idx in indices:
                            if idx <= len(playlists[pl_name]):
                                playlists[pl_name].insert(idx, orig_path)
                            else:
                                playlists[pl_name].append(orig_path)
                restored = True
            except Exception as e:
                log_error(f"undo_delete: {e}")
        return restored

    def flush_pending_trash(self) -> None:
        """Commit pending staged deletes to the Windows Recycle Bin."""
        pending, self._pending_deletes = self._pending_deletes, []
        for orig_path, staging_path, _fname, _aff in pending:
            _trash_staged_file(original_backup_path(staging_path), original_backup_path(orig_path))
            _trash_staged_file(staging_path, orig_path)

    @staticmethod
    def recover_stranded_deletes(folder: str | None) -> int:
        """Send files left in the undo area by a crash or forced exit to the Recycle Bin.

        Returns the number of files handled. Legacy '<name>.undo' files are supported too.
        """
        undo_root = os.path.join(folder or "", UNDO_DIR_NAME)
        if not folder or not os.path.isdir(undo_root):
            return 0
        handled = 0
        for root_dir, _dirs, files in os.walk(undo_root, topdown=False):
            for name in files:
                original_name = name[: -len(".undo")] if name.endswith(".undo") else name
                _trash_staged_file(os.path.join(root_dir, name), os.path.join(folder, original_name))
                handled += 1
            with contextlib.suppress(OSError):
                os.rmdir(root_dir)
        return handled

    def import_external_files(
        self,
        planned_copies: Sequence[tuple[str, str]],
        is_shutting_down_fn: Callable[[], bool] | None,
        on_done: Callable[[ImportResult], None] | None,
    ) -> None:
        """Copy external files into the Library folder on a worker thread and report what happened.

        Each file is copied under a temporary name and renamed when complete, so a source that
        vanishes halfway never leaves a cut-off song (or damages one that was being replaced).
        """

        def _worker() -> None:
            copied: list[str] = []
            renamed: list[str] = []
            failed: list[str] = []
            for src, dest in planned_copies:
                if is_shutting_down_fn and is_shutting_down_fn():
                    break
                src_name, dest_name = Path(src).name, Path(dest).name
                try:
                    copy_file_atomic(src, dest)
                except OSError as err:
                    logger.error("Import of %s failed: %s", src, err)
                    failed.append(src_name)
                    continue
                copied.append(dest_name)
                if dest_name != src_name:
                    renamed.append(dest_name)
            if on_done:
                on_done(ImportResult(tuple(copied), tuple(renamed), tuple(failed)))

        task_mgr.submit_task(_worker)

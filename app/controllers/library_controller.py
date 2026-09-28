"""Library controller handling scanning, search filtering, renaming, file import, and safe deletion with undo."""

import contextlib
import ctypes
import os
import shutil
import uuid

from app.config import AUDIO_EXTS, log_error
from app.core.task_manager import task_mgr

try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None

UNDO_DIR_NAME = ".undo_trash"


def _hide_path(path):
    """Mark a folder hidden on Windows so the undo area does not clutter File Explorer."""
    if os.name == "nt":
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # FILE_ATTRIBUTE_HIDDEN


def _trash_staged_file(staging_path, orig_path):
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

    def __init__(self, app):
        self.app = app
        # Each entry: (orig_path, staging_path, filename, [(playlist_name, [indices])])
        self._pending_deletes: list[tuple[str, str, str, list[tuple[str, list[int]]]]] = []
        self._search_index = {}

    def scan_files(self, folder):
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

    def scan_and_filter(self, folder, query="", get_metadata_fn=None):
        """Scan folder for audio files and filter by query."""
        files = self.scan_files(folder)
        visible = self.filter_files(files, folder, query, get_metadata_fn=get_metadata_fn)
        return files, visible

    @staticmethod
    def build_import_plan(paths, library_folder):
        """Build planned copies for dropped or selected files/folders."""
        audio_files = []
        for p in paths:
            if os.path.isfile(p) and p.lower().endswith(AUDIO_EXTS):
                audio_files.append(p)
            elif os.path.isdir(p):
                for root_dir, _, files in os.walk(p):
                    for f in files:
                        if f.lower().endswith(AUDIO_EXTS):
                            audio_files.append(os.path.join(root_dir, f))
        if not audio_files:
            return [], []

        planned = []
        dest_exists = []
        for src in audio_files:
            dest = os.path.join(library_folder, os.path.basename(src))
            if os.path.abspath(src).lower() == os.path.abspath(dest).lower():
                continue
            planned.append((src, dest))
            if os.path.exists(dest):
                dest_exists.append(os.path.basename(dest))
        return planned, dest_exists

    def update_search_index(self, folder, files, get_metadata_fn=None):
        """Pre-index searchable strings for library files."""
        for f in files:
            f_path = os.path.join(folder, f)
            if f_path not in self._search_index:
                meta = get_metadata_fn(f_path) if get_metadata_fn else {}
                title = meta.get("title", "") if meta else ""
                artist = meta.get("artist", "") if meta else ""
                self._search_index[f_path] = f"{f} {title} {artist}".lower()

    def invalidate_search_index(self, filepath=None):
        """Invalidate search index for single file or entire library."""
        if filepath:
            self._search_index.pop(filepath, None)
            norm = os.path.abspath(filepath).lower()
            keys_to_del = [k for k in self._search_index if os.path.abspath(k).lower() == norm]
            for k in keys_to_del:
                self._search_index.pop(k, None)
        else:
            self._search_index.clear()

    def filter_files(self, files, folder, query, get_metadata_fn=None):
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

    def rename_file(self, old_path, new_name, playlists):
        """Rename file on disk and update all playlists referencing it."""
        folder = os.path.dirname(old_path)
        new_path = os.path.join(folder, new_name)
        if os.path.exists(new_path) and new_path.lower() != old_path.lower():
            raise FileExistsError(f"A file named '{new_name}' already exists.")

        os.replace(old_path, new_path)
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
            _trash_staged_file(staging_path, orig_path)

    @staticmethod
    def recover_stranded_deletes(folder):
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

    def import_external_files(self, planned_copies, is_shutting_down_fn, on_done):
        """Copy external files into library folder in background thread."""

        def _worker():
            copied = 0
            for src, dest in planned_copies:
                if is_shutting_down_fn and is_shutting_down_fn():
                    break
                try:
                    shutil.copy2(src, dest)
                    copied += 1
                except Exception as e:
                    log_error(f"import_external_files: {e}")
            if on_done:
                on_done(copied)

        task_mgr.submit_task(_worker)

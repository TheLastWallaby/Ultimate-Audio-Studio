"""Library controller handling scanning, search filtering, renaming, file import, and safe deletion with undo."""

import os
import shutil

from app.config import AUDIO_EXTS, log_error
from app.core.task_manager import task_mgr

try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None


class LibraryController:
    """Manages audio files in the music library, search queries, renaming, and safe deletion with undo."""

    def __init__(self, app):
        self.app = app
        self._pending_delete = None  # (orig_path, staging_path, filename, affected_playlists)
        self._search_index = {}

    def scan_files(self, folder):
        """Return sorted list of audio filenames in folder."""
        if not folder or not os.path.exists(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except Exception:
                return []
        try:
            return sorted(
                [f for f in os.listdir(folder) if f.lower().endswith(AUDIO_EXTS)],
                key=lambda s: s.lower()
            )
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

    def stage_delete_with_undo(self, filepath, playlists):
        """Stage file deletion by moving to a hidden undo directory so user can undo immediately."""
        self.flush_pending_trash()
        folder = os.path.dirname(filepath)
        filename = os.path.basename(filepath)
        staging_dir = os.path.join(folder, ".undo_trash")
        os.makedirs(staging_dir, exist_ok=True)
        staging_path = os.path.join(staging_dir, f"{filename}.undo")

        # Remember playlists where this track was present
        affected_playlists = []
        for pl_name, tracks in playlists.items():
            indices = [i for i, t in enumerate(tracks) if os.path.abspath(t).lower() == os.path.abspath(filepath).lower()]
            if indices:
                affected_playlists.append((pl_name, indices))
                playlists[pl_name] = [t for t in tracks if os.path.abspath(t).lower() != os.path.abspath(filepath).lower()]

        shutil.move(filepath, staging_path)
        self.invalidate_search_index(filepath)
        self._pending_delete = (filepath, staging_path, filename, affected_playlists)
        return filename

    def undo_delete(self, playlists):
        """Restore staged deleted file back to library and playlists."""
        if not self._pending_delete:
            return False
        orig_path, staging_path, filename, affected_playlists = self._pending_delete
        self._pending_delete = None
        try:
            if os.path.exists(staging_path):
                shutil.move(staging_path, orig_path)
            self.invalidate_search_index(orig_path)
            # Restore into playlists
            for pl_name, indices in affected_playlists:
                if pl_name in playlists:
                    for idx in indices:
                        if idx <= len(playlists[pl_name]):
                            playlists[pl_name].insert(idx, orig_path)
                        else:
                            playlists[pl_name].append(orig_path)
            return True
        except Exception as e:
            log_error(f"undo_delete: {e}")
            return False

    def flush_pending_trash(self):
        """Commit pending staged delete to Windows Recycle Bin."""
        if not self._pending_delete:
            return
        _orig, staging_path, _fname, _aff = self._pending_delete
        self._pending_delete = None
        if os.path.exists(staging_path):
            try:
                if _send2trash:
                    _send2trash(staging_path)
                else:
                    os.remove(staging_path)
            except Exception as e:
                log_error(f"flush_pending_trash: {e}")

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

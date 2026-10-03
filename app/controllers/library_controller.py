"""Library controller handling scanning, search filtering, renaming, file import, and safe deletion with undo."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import AUDIO_EXTS, log_error
from app.core.file_utils import copy_file_atomic, replace_with_retry, unused_path
from app.core.task_manager import task_mgr
from app.services.clipper import discard_orphan_backup, find_orphan_backups, original_backup_path

try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None

logger = logging.getLogger(__name__)

MetadataFn = Callable[[str], Mapping[str, Any]]

UNDO_DIR_NAME = ".undo_trash"
# Temporary files this app makes in the Library folder while it copies, clips or restores a song. Each
# name carries the id of the process that made it, so no file of the user's can match.
_WORK_FILE_RE = re.compile(
    r"^(?:\.clip_tmp_(?P<clip_pid>\d+)_\d+\.(?:mp3|wav)"
    r"|(?P<song>.+)\.(?P<pid>\d+)\.(?P<kind>partial|incoming|restoring))$"
)
# A complete copy of a song that was about to be renamed into place ("Song.mp3.<pid>.restoring").
_READY_COPY_KINDS = ("incoming", "restoring")


@dataclass(slots=True, frozen=True)
class ImportResult:
    """What an import did, by file name, so the window can report it truthfully."""

    copied: tuple[str, ...] = ()  # songs now in the Library (under these names)
    renamed: tuple[str, ...] = ()  # the copied songs that were given a new name to avoid a clash
    failed: tuple[str, ...] = ()  # songs that could not be copied
    replaced: tuple[str, ...] = ()  # the copied songs that replaced one already there (now in the Recycle Bin)


@dataclass(slots=True, frozen=True)
class UndoResult:
    """What Undo brought back, by file name; false when nothing came back."""

    restored: tuple[str, ...] = ()  # songs back in the Library (under these names)
    renamed: tuple[str, ...] = ()  # the restored songs that got a new name because theirs was taken meanwhile

    def __bool__(self) -> bool:
        return bool(self.restored)


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


def _finish_interrupted_swap(ready: Path, song: Path) -> bool:
    """Put the complete copy ``ready`` in place of ``song``, which an earlier run had already moved away.

    Restoring an original and replacing a song on import both send the old song to the Recycle Bin
    and then rename a finished copy into its place. A run that ended between the two steps left
    the Library without the song; removing the copy as well (and then its backup, as an orphan)
    would leave it with neither version. True when the song is back; raises OSError.
    """
    if song.exists():
        return False
    # The backup belongs to the song that is gone: left in place, it would pass for the original of
    # the one put back here.
    discard_orphan_backup(song)
    replace_with_retry(ready, song)
    return True


def remove_stale_work_files(folder: Path) -> int:
    """Tidy the app's own temporary files that an earlier run left in ``folder``.

    A run that was ended mid-save (a crash, the power going) leaves its half-made copy or clip
    behind; a leftover clip even shows up in the Library as a song. They are removed, except for a
    complete copy whose song is gone: that one takes the song's place (``_finish_interrupted_swap``).
    Files of this run are kept: one of them may be in use right now. Returns the number of files
    handled.
    """
    try:
        entries = list(folder.iterdir())
    except OSError:
        return 0
    handled = 0
    for entry in entries:
        match = _WORK_FILE_RE.match(entry.name)
        if match is None or int(match.group("clip_pid") or match.group("pid")) == os.getpid():
            continue
        try:
            if not entry.is_file():
                continue
            if match.group("kind") in _READY_COPY_KINDS and _finish_interrupted_swap(
                entry, folder / match.group("song")
            ):
                logger.info("Put %s back in the Library after an interrupted save", match.group("song"))
            else:
                entry.unlink()
            handled += 1
        except OSError as err:
            logger.warning("Could not tidy the leftover file %s: %s", entry.name, err)
    return handled


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


# One staged delete: (original path, staging path, file name, [(playlist name, [positions])]).
PendingDelete = tuple[str, str, str, list[tuple[str, list[int]]]]


def _trash_pending(pending: Sequence[PendingDelete]) -> None:
    """Send staged deletes (and the trim backups staged with them) to the Recycle Bin."""
    for orig_path, staging_path, _name, _playlists in pending:
        _trash_staged_file(original_backup_path(staging_path), original_backup_path(orig_path))
        _trash_staged_file(staging_path, orig_path)


def _replace_song(src: Path, dest: Path) -> None:
    """Put ``src`` in place of the Library song ``dest``; the old song goes to the Recycle Bin.

    The new copy is complete before the old song is touched, so a failed copy leaves it as it was.
    A backup kept from trimming the old song belongs to the old song: it is recycled too, or else
    'Restore Original' would put the old song back over the new one. Raises OSError when the new
    song could not be put in place; the old song is then still in the Library or the Recycle Bin.
    """
    if _send2trash is None:
        raise OSError("the Recycle Bin cannot be used, so the old song was left in place")
    # Not an audio file name, so the Library list never shows the copy while it is being made.
    incoming = dest.with_name(f"{dest.name}.{os.getpid()}.incoming")
    try:
        copy_file_atomic(src, incoming)
        _send2trash(str(dest))
        replace_with_retry(incoming, dest)
    finally:
        with contextlib.suppress(FileNotFoundError):
            incoming.unlink()

    old_backup = Path(original_backup_path(str(dest)))
    if not old_backup.exists():
        return
    try:
        _send2trash(str(old_backup))
        return
    except OSError as err:
        logger.warning("Could not recycle %s: %s", old_backup.name, err)
    # Renamed so it can no longer be "restored" over the new song; the old original stays on disk.
    kept = old_backup.with_name(f"{old_backup.name}.replaced-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        replace_with_retry(old_backup, kept)
    except OSError as err:
        # The new song is in place, so the import itself succeeded and is reported as such.
        logger.error("Could not set the old backup %s aside: %s", old_backup.name, err)


class LibraryController:
    """Manages audio files in the music library, search queries, renaming, and safe deletion with undo."""

    def __init__(self, app: object) -> None:
        self.app = app
        self._pending_deletes: list[PendingDelete] = []
        self._search_index: dict[str, str] = {}

    def scan_files(self, folder: str | Path) -> list[str]:
        """The audio file names in ``folder``, sorted; empty when the folder cannot be read.

        The app's own unfinished files are left out: a clip is cut under a temporary ``.mp3`` name
        in this folder, and it would otherwise show as a song until the save has finished.
        """
        library = Path(folder)
        if not str(folder):
            return []
        try:
            library.mkdir(parents=True, exist_ok=True)
            names = [entry.name for entry in library.iterdir()]
        except OSError as err:
            logger.warning("The Library folder %s could not be read: %s", library, err)
            return []
        songs = [name for name in names if name.lower().endswith(AUDIO_EXTS) and not _WORK_FILE_RE.match(name)]
        return sorted(songs, key=str.lower)

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
        """Rename a song on disk and in every playlist; returns its new path. Raises OSError."""
        old = Path(old_path)
        # Same folder spelling as before (these paths are compared as text); only the name changes.
        new_path = old_path[: len(old_path) - len(old.name)] + new_name
        new = Path(new_path)
        old_key = _path_key(old)
        if new.exists() and _path_key(new) != old_key:
            raise FileExistsError(f"A file named '{new_name}' already exists.")

        discard_orphan_backup(new)
        os.replace(old, new)
        # A trimmed song's untrimmed backup follows the song, so "Restore Original Song" keeps working.
        old_backup = Path(original_backup_path(old_path))
        if old_backup.is_file():
            try:
                os.replace(old_backup, original_backup_path(new_path))
            except OSError as err:
                # The song itself is renamed; only 'Restore Original Song' is lost for it.
                logger.warning("The trim backup %s could not follow its song: %s", old_backup.name, err)
        self.invalidate_search_index(old_path)
        self.invalidate_search_index(new_path)
        for tracks in playlists.values():
            for idx, track in enumerate(tracks):
                if _path_key(Path(track)) == old_key:
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
        self.flush_pending_trash(background=True)
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

    def undo_delete(self, playlists: dict[str, list[str]]) -> UndoResult:
        """Put the staged deleted file(s) back in the Library and the playlists.

        A song whose name was taken meanwhile (a download with the same title) comes back as
        "Name (2).mp3": moving it onto the new song would replace that one. A song that could not be
        moved back stays staged, so Undo can be tried again.
        """
        pending, self._pending_deletes = self._pending_deletes, []
        restored: list[str] = []
        renamed: list[str] = []
        still_staged = []
        # Newest first, so playlist positions are restored in the reverse order they were removed.
        for entry in reversed(pending):
            orig_path, staging_path, _filename, affected_playlists = entry
            staged = Path(staging_path)
            target = orig_path
            try:
                if staged.exists():
                    free = unused_path(orig_path)
                    if free.name != Path(orig_path).name:
                        # Same folder spelling as before; only the file name changes.
                        target = orig_path[: len(orig_path) - len(Path(orig_path).name)] + free.name
                        renamed.append(free.name)
                    discard_orphan_backup(target)
                    replace_with_retry(staged, target)
                    staged_backup = Path(original_backup_path(staging_path))
                    if staged_backup.exists():
                        replace_with_retry(staged_backup, original_backup_path(target))
                    with contextlib.suppress(OSError):
                        staged.parent.rmdir()
            except OSError as err:
                logger.error("Undo could not put %s back: %s", Path(orig_path).name, err)
                still_staged.append(entry)
                continue
            self.invalidate_search_index(target)
            for pl_name, indices in affected_playlists:
                if pl_name in playlists:
                    for idx in indices:
                        if idx <= len(playlists[pl_name]):
                            playlists[pl_name].insert(idx, target)
                        else:
                            playlists[pl_name].append(target)
            restored.append(Path(target).name)
        self._pending_deletes = list(reversed(still_staged))
        return UndoResult(tuple(restored), tuple(renamed))

    def flush_pending_trash(self, background: bool = False) -> None:
        """Commit pending staged deletes to the Windows Recycle Bin.

        ``background`` does the moving on a worker, for calls from the window's thread: the Recycle
        Bin can take seconds for a big file or a slow drive. Without it (closing, restarting) the
        files are in the Recycle Bin when this returns.
        """
        pending, self._pending_deletes = self._pending_deletes, []
        if not pending:
            return
        if background and task_mgr.submit_task(_trash_pending, pending) is not None:
            return
        _trash_pending(pending)

    @staticmethod
    def recover_stranded_deletes(folder: str | Path | None) -> int:
        """Tidy the Library folder after a crash or forced exit; returns the number of files handled.

        Files left in the undo area go to the Recycle Bin (legacy '<name>.undo' files too), the
        app's own unfinished temporary files are removed (``remove_stale_work_files``), and trim
        backups whose song is gone are set aside (``discard_orphan_backup``).
        """
        if not folder:
            return 0
        library = Path(folder)
        handled = remove_stale_work_files(library)
        for song in find_orphan_backups(library):
            try:
                handled += discard_orphan_backup(song)
            except OSError as err:
                logger.warning("Could not set the leftover backup of %s aside: %s", song.name, err)
        undo_root = library / UNDO_DIR_NAME
        if not undo_root.is_dir():
            return handled
        # os.walk, not Path.walk: that one needs Python 3.12.
        for root_dir, _dirs, files in os.walk(undo_root, topdown=False):
            for name in files:
                original_name = name[: -len(".undo")] if name.endswith(".undo") else name
                _trash_staged_file(str(Path(root_dir) / name), str(library / original_name))
                handled += 1
            with contextlib.suppress(OSError):
                Path(root_dir).rmdir()
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
        A song that is already in the Library is replaced only after the old one is in the
        Recycle Bin, where the user can get it back.
        """

        def _worker() -> None:
            copied: list[str] = []
            renamed: list[str] = []
            failed: list[str] = []
            replaced: list[str] = []
            for src, dest in planned_copies:
                if is_shutting_down_fn and is_shutting_down_fn():
                    break
                src_path, dest_path = Path(src), Path(dest)
                replacing = dest_path.exists()
                try:
                    if replacing:
                        _replace_song(src_path, dest_path)
                    else:
                        discard_orphan_backup(dest_path)
                        copy_file_atomic(src_path, dest_path)
                except OSError as err:
                    logger.error("Import of %s failed: %s", src_path, err)
                    failed.append(src_path.name)
                    continue
                copied.append(dest_path.name)
                if dest_path.name != src_path.name:
                    renamed.append(dest_path.name)
                if replacing:
                    replaced.append(dest_path.name)
            if on_done:
                on_done(ImportResult(tuple(copied), tuple(renamed), tuple(failed), tuple(replaced)))

        task_mgr.submit_task(_worker)

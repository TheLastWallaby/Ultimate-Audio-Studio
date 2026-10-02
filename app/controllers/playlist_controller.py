"""Playlist controller managing playlist collections, reordering, and track removal with undo."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import DEFAULT_PLAYLIST_NAME, atomic_save_json
from app.core.file_utils import copy_file_atomic

logger = logging.getLogger(__name__)

LAST_GOOD_SUFFIX = ".bak"


@dataclass(slots=True, frozen=True)
class PlaylistLoadResult:
    """What happened when the playlists file was read, so the window can tell the user."""

    unreadable: bool = False  # the file was there but could not be read
    restored_from_backup: bool = False  # the playlists come from the last copy that loaded correctly
    damaged_copy: Path | None = None  # where the unreadable file was kept for recovery

    @property
    def needs_notice(self) -> bool:
        return self.unreadable or self.restored_from_backup


def _same_file(a: str, b: str) -> bool:
    """Compare two song paths the way Windows does (absolute, case-insensitive)."""
    return str(Path(a).absolute()).casefold() == str(Path(b).absolute()).casefold()


def last_good_path(playlists_file: Path) -> Path:
    """Where the copy of the playlists file that last loaded correctly is kept."""
    return playlists_file.with_name(playlists_file.name + LAST_GOOD_SUFFIX)


def _read_playlists(path: Path) -> dict[str, list[str]]:
    """Playlists stored in ``path``. Raises OSError or ValueError when the file cannot be used."""
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} does not hold a collection of playlists")
    # Drop malformed entries instead of crashing later on non-list values.
    return {
        str(name): [t for t in tracks if isinstance(t, str) and t]
        for name, tracks in data.items()
        if isinstance(tracks, list)
    }


def _set_aside(path: Path) -> Path | None:
    """Move an unreadable playlists file out of the way, so the next save cannot overwrite it."""
    kept = path.with_name(f"{path.stem}.damaged-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
    try:
        path.replace(kept)
    except OSError as err:
        logger.error("Could not set the unreadable playlists file %s aside: %s", path, err)
        return None
    return kept


def _keep_as_last_good(path: Path) -> None:
    """Copy a playlists file that just loaded correctly, as the fallback for a later damaged one."""
    try:
        copy_file_atomic(path, last_good_path(path))
    except OSError as err:
        logger.warning("Could not keep a spare copy of the playlists file: %s", err)


class PlaylistController:
    """Manages playlist state, file serialization, track reordering, and single-click removal undo.

    ``playlist_index`` always keeps pointing at the same song of the active playlist when songs are
    removed, restored, or moved, so the ▶ marker and Previous/Next stay on the song that is playing.
    """

    def __init__(self, app: object) -> None:
        self.app = app
        self.playlists: dict[str, list[str]] = {DEFAULT_PLAYLIST_NAME: []}
        self.active_playlist_name = DEFAULT_PLAYLIST_NAME
        self.playlist_index = 0
        self._last_removed: tuple[str, int, str] | None = None  # (playlist_name, index, track_path)

    def load(self, filepath: str | Path) -> PlaylistLoadResult:
        """Load playlists from the JSON file, falling back to the last copy that loaded correctly.

        Playlists cannot be made again, so an unreadable file is never left where the next save
        would overwrite it: it is moved aside under a ``.damaged-<time>`` name. A file that loads
        correctly is copied as the fallback for next time.
        """
        path = Path(filepath)
        loaded: dict[str, list[str]] | None = None
        unreadable = False
        damaged_copy: Path | None = None
        if path.exists():
            try:
                loaded = _read_playlists(path)
            except (OSError, ValueError) as err:
                logger.error("The playlists file %s could not be read: %s", path, err)
                unreadable = True
                damaged_copy = _set_aside(path)
            else:
                _keep_as_last_good(path)

        restored = False
        backup = last_good_path(path)
        if loaded is None and backup.exists():
            try:
                loaded = _read_playlists(backup)
                restored = True
            except (OSError, ValueError) as err:
                logger.error("The spare playlists file %s could not be read either: %s", backup, err)

        self.playlists = loaded or {DEFAULT_PLAYLIST_NAME: []}
        if DEFAULT_PLAYLIST_NAME not in self.playlists:
            self.playlists[DEFAULT_PLAYLIST_NAME] = []
        self.active_playlist_name = next(iter(self.playlists))
        return PlaylistLoadResult(unreadable=unreadable, restored_from_backup=restored, damaged_copy=damaged_copy)

    def save(self, filepath: str | Path) -> bool:
        """Persist playlists to the JSON file atomically; False (after logging) when that failed."""
        try:
            atomic_save_json(filepath, self.playlists)
        except (OSError, TypeError, ValueError) as err:
            logger.error("The playlists could not be saved to %s: %s", filepath, err)
            return False
        return True

    def create_playlist(self, name: str) -> bool:
        """Create new empty playlist."""
        if name in self.playlists:
            return False
        self.playlists[name] = []
        self.active_playlist_name = name
        return True

    def rename_playlist(self, old_name: str, new_name: str) -> bool:
        """Rename an existing playlist."""
        if old_name not in self.playlists or new_name in self.playlists:
            return False
        self.playlists[new_name] = self.playlists.pop(old_name)
        self.active_playlist_name = new_name
        return True

    def delete_playlist(self, name: str) -> bool:
        """Delete playlist while retaining at least one playlist."""
        if len(self.playlists) <= 1 or name not in self.playlists:
            return False
        del self.playlists[name]
        self.active_playlist_name = list(self.playlists.keys())[0]
        self.playlist_index = 0
        return True

    def contains(self, playlist_name: str, track_path: str) -> bool:
        """True when the playlist already has this song (same file, ignoring path case)."""
        return any(_same_file(t, track_path) for t in self.playlists.get(playlist_name, []))

    def add_track(self, playlist_name: str, track_path: str) -> bool:
        """Add track to the playlist; returns False (and adds nothing) if it is already there."""
        tracks = self.playlists.setdefault(playlist_name, [])
        if self.contains(playlist_name, track_path):
            return False
        tracks.append(track_path)
        return True

    def move_track(self, playlist_name: str, src: int, dst: int) -> int:
        """Move the track at ``src`` so it ends up at ``dst``; returns its new index."""
        tracks = self.playlists.get(playlist_name, [])
        if not (0 <= src < len(tracks)):
            return src
        dst = max(0, min(len(tracks) - 1, dst))
        if dst == src:
            return src
        tracks.insert(dst, tracks.pop(src))
        if playlist_name == self.active_playlist_name:
            if self.playlist_index == src:
                self.playlist_index = dst
            elif src < self.playlist_index <= dst:
                self.playlist_index -= 1
            elif dst <= self.playlist_index < src:
                self.playlist_index += 1
        return dst

    def move_up(self, playlist_name: str, index: int) -> int:
        """Move track at index up by 1 position."""
        tracks = self.playlists.get(playlist_name, [])
        if index <= 0 or index >= len(tracks):
            return index
        return self.move_track(playlist_name, index, index - 1)

    def move_down(self, playlist_name: str, index: int) -> int:
        """Move track at index down by 1 position."""
        tracks = self.playlists.get(playlist_name, [])
        if index < 0 or index >= len(tracks) - 1:
            return index
        return self.move_track(playlist_name, index, index + 1)

    def remove_track_with_undo(self, playlist_name: str, index: int) -> str | None:
        """Remove track from playlist and stage for undo."""
        tracks = self.playlists.get(playlist_name, [])
        if 0 <= index < len(tracks):
            removed_track = tracks.pop(index)
            self._last_removed = (playlist_name, index, removed_track)
            if playlist_name == self.active_playlist_name and index < self.playlist_index:
                self.playlist_index -= 1
            return removed_track
        return None

    def undo_remove(self) -> tuple[str, int, str] | None:
        """Restore last removed track to its original playlist index."""
        if not self._last_removed:
            return None
        pl_name, idx, track_path = self._last_removed
        self._last_removed = None
        if pl_name in self.playlists:
            tracks = self.playlists[pl_name]
            if idx <= len(tracks):
                tracks.insert(idx, track_path)
            else:
                idx = len(tracks)
                tracks.append(track_path)
            if pl_name == self.active_playlist_name and idx <= self.playlist_index:
                self.playlist_index += 1
            return pl_name, idx, track_path
        return None

    def get_next_index(self, playlist_name: str, current_index: int, repeat: bool = False) -> int | None:
        """Calculate next track index in playlist with optional loop."""
        tracks = self.playlists.get(playlist_name, [])
        if not tracks:
            return None
        if current_index + 1 < len(tracks):
            return current_index + 1
        elif repeat:
            return 0
        return None

    def get_active_tracks(self, playlist_name: str | None = None) -> list[str]:
        """Return list of track filepaths in active or specified playlist."""
        name = playlist_name or self.active_playlist_name
        return self.playlists.get(name, [])

    def get_current_track(self) -> str | None:
        """Return current track filepath in active playlist if index is valid."""
        tracks = self.get_active_tracks()
        if 0 <= self.playlist_index < len(tracks):
            return tracks[self.playlist_index]
        return None

    def get_playlist_names(self) -> list[str]:
        """Return list of all playlist names."""
        return list(self.playlists.keys())

    def get_prev_index(self, playlist_name: str, current_index: int, repeat: bool = False) -> int | None:
        """Calculate previous track index; at the first track, stay there unless repeat wraps to the end."""
        tracks = self.playlists.get(playlist_name, [])
        if not tracks:
            return None
        if current_index > 0:
            return min(current_index - 1, len(tracks) - 1)
        return len(tracks) - 1 if repeat else 0

    def relink_missing(self, library_folder: str) -> int:
        """Point playlist entries whose file is gone at a same-named file in library_folder.

        Returns the number of entries relinked (e.g. after the library folder was moved or changed).
        """
        if not library_folder or not os.path.isdir(library_folder):
            return 0
        try:
            by_name = {f.lower(): f for f in os.listdir(library_folder)}
        except OSError:
            return 0
        relinked = 0
        for tracks in self.playlists.values():
            for idx, path in enumerate(tracks):
                if not path or os.path.exists(path):
                    continue
                match = by_name.get(os.path.basename(path).lower())
                if match:
                    tracks[idx] = os.path.join(library_folder, match)
                    relinked += 1
        return relinked

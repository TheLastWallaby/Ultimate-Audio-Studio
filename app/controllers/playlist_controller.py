"""Playlist controller managing playlist collections, reordering, and track removal with undo."""

import os
import json
from app.config import DEFAULT_PLAYLIST_NAME, atomic_save_json, log_error


class PlaylistController:
    """Manages playlist state, file serialization, track reordering, and single-click removal undo."""

    def __init__(self, app):
        self.app = app
        self.playlists = {DEFAULT_PLAYLIST_NAME: []}
        self.active_playlist_name = DEFAULT_PLAYLIST_NAME
        self.playlist_index = 0
        self._last_removed = None  # (playlist_name, index, track_path)

    def load(self, filepath):
        """Load playlists from JSON file."""
        try:
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    self.playlists = data
                    if DEFAULT_PLAYLIST_NAME not in self.playlists:
                        self.playlists[DEFAULT_PLAYLIST_NAME] = []
                    self.active_playlist_name = list(self.playlists.keys())[0]
                    return
        except Exception as e:
            log_error(f"PlaylistController.load: {e}")
        self.playlists = {DEFAULT_PLAYLIST_NAME: []}
        self.active_playlist_name = DEFAULT_PLAYLIST_NAME

    def save(self, filepath):
        """Persist playlists to JSON file atomically."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
            atomic_save_json(filepath, self.playlists)
        except Exception as e:
            log_error(f"PlaylistController.save: {e}")

    def create_playlist(self, name):
        """Create new empty playlist."""
        if name in self.playlists:
            return False
        self.playlists[name] = []
        self.active_playlist_name = name
        return True

    def rename_playlist(self, old_name, new_name):
        """Rename an existing playlist."""
        if old_name not in self.playlists or new_name in self.playlists:
            return False
        self.playlists[new_name] = self.playlists.pop(old_name)
        self.active_playlist_name = new_name
        return True

    def delete_playlist(self, name):
        """Delete playlist while retaining at least one playlist."""
        if len(self.playlists) <= 1 or name not in self.playlists:
            return False
        del self.playlists[name]
        self.active_playlist_name = list(self.playlists.keys())[0]
        return True

    def add_track(self, playlist_name, track_path):
        """Add track to specified playlist."""
        tracks = self.playlists.setdefault(playlist_name, [])
        tracks.append(track_path)

    def move_up(self, playlist_name, index):
        """Move track at index up by 1 position."""
        tracks = self.playlists.get(playlist_name, [])
        if index <= 0 or index >= len(tracks):
            return index
        tracks[index - 1], tracks[index] = tracks[index], tracks[index - 1]
        return index - 1

    def move_down(self, playlist_name, index):
        """Move track at index down by 1 position."""
        tracks = self.playlists.get(playlist_name, [])
        if index < 0 or index >= len(tracks) - 1:
            return index
        tracks[index], tracks[index + 1] = tracks[index + 1], tracks[index]
        return index + 1

    def remove_track_with_undo(self, playlist_name, index):
        """Remove track from playlist and stage for undo."""
        tracks = self.playlists.get(playlist_name, [])
        if 0 <= index < len(tracks):
            removed_track = tracks.pop(index)
            self._last_removed = (playlist_name, index, removed_track)
            return removed_track
        return None

    def undo_remove(self):
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
                tracks.append(track_path)
            return pl_name, idx, track_path
        return None

    def get_next_index(self, playlist_name, current_index, repeat=False):
        """Calculate next track index in playlist with optional loop."""
        tracks = self.playlists.get(playlist_name, [])
        if not tracks:
            return None
        if current_index + 1 < len(tracks):
            return current_index + 1
        elif repeat:
            return 0
        return None

    def get_active_tracks(self, playlist_name=None):
        """Return list of track filepaths in active or specified playlist."""
        name = playlist_name or self.active_playlist_name
        return self.playlists.get(name, [])

    def get_current_track(self):
        """Return current track filepath in active playlist if index is valid."""
        tracks = self.get_active_tracks()
        if 0 <= self.playlist_index < len(tracks):
            return tracks[self.playlist_index]
        return None

    def get_playlist_names(self):
        """Return list of all playlist names."""
        return list(self.playlists.keys())

    def get_prev_index(self, playlist_name, current_index):
        """Calculate previous track index in playlist."""
        tracks = self.playlists.get(playlist_name, [])
        if not tracks:
            return None
        return (current_index - 1) % len(tracks)

"""Persistent disk and in-memory caching for audio metadata, durations, and waveform peaks."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from app.config import YT_CACHE_DIR, atomic_save_json, log_error

CACHE_FILE_NAME = "persistent_audio_cache.json"
SAVE_DEBOUNCE_SEC = 2.0
DEFAULT_MAX_META_ENTRIES = 20000


class CacheManager:
    """Manages thread-safe persistent caching for track metadata, duration, and waveform peaks.

    Keys are keyed by file path, mtime, and file size so any edit or replacement
    automatically invalidates stale cached data.

    Setters never write to disk on the calling thread: they schedule a debounced
    background save, so the Tkinter main loop is not blocked by JSON serialization.
    Call ``flush()`` on shutdown to persist pending changes.
    """

    def __init__(
        self,
        cache_dir: str | None = None,
        max_mem_entries: int = 1024,
        max_meta_entries: int | None = None,
    ) -> None:
        self.cache_dir = cache_dir or YT_CACHE_DIR
        self.cache_file = os.path.join(self.cache_dir, CACHE_FILE_NAME)
        # Peaks are large (hundreds of floats) and only needed for the selected track;
        # metadata is tiny and needed for every library row, so it gets a much larger cap.
        self.max_mem_entries = max_mem_entries
        self.max_meta_entries = max(max_meta_entries or DEFAULT_MAX_META_ENTRIES, max_mem_entries)
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._meta_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._peaks_cache: OrderedDict[str, list[float]] = OrderedDict()
        # Songs whose waveform could not be made in this session. Not saved: the cause may be gone
        # at the next start (a busy PC, a drive that was slow to answer).
        self._no_waveform: set[str] = set()
        self._dirty = False
        self._last_save_time = 0.0
        self._save_timer: threading.Timer | None = None
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        with self._lock:
            if not os.path.exists(self.cache_file):
                return
            try:
                with open(self.cache_file, encoding="utf-8") as f:
                    data = json.load(f)
                meta = data.get("meta", {})
                peaks = data.get("peaks", {})
                for k, v in meta.items():
                    self._meta_cache[k] = v
                for k, v in peaks.items():
                    self._peaks_cache[k] = v
            except Exception as e:
                log_error(f"CacheManager._load_from_disk: {e}")

    def save_to_disk(self, force: bool = False) -> None:
        """Atomically persist cache contents to disk (synchronous; prefer ``_schedule_save``)."""
        with self._lock:
            if not self._dirty and not force:
                return
            now = time.time()
            if not force and (now - self._last_save_time < SAVE_DEBOUNCE_SEC):
                return
            data = {
                "version": 1,
                "saved_at": int(now),
                "meta": dict(self._meta_cache),
                "peaks": dict(self._peaks_cache),
            }
            self._dirty = False
            self._last_save_time = now
        # Serialize outside the data lock so readers on the UI thread are never blocked by I/O.
        with self._write_lock:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                atomic_save_json(self.cache_file, data)
            except Exception as e:
                with self._lock:
                    self._dirty = True
                log_error(f"CacheManager.save_to_disk: {e}")

    def _schedule_save(self) -> None:
        """Mark dirty and persist on a background timer, coalescing bursts of updates."""
        with self._lock:
            self._dirty = True
            if self._save_timer is not None:
                return
            timer = threading.Timer(SAVE_DEBOUNCE_SEC, self._background_save)
            timer.daemon = True
            self._save_timer = timer
        timer.start()

    def _background_save(self) -> None:
        with self._lock:
            self._save_timer = None
        # A vanished cache directory means it was deliberately removed; don't resurrect it.
        if os.path.isdir(self.cache_dir):
            self.save_to_disk(force=True)

    def flush(self) -> None:
        """Cancel any pending background save and write outstanding changes now (call on exit)."""
        with self._lock:
            timer, self._save_timer = self._save_timer, None
            dirty = self._dirty
        if timer is not None:
            timer.cancel()
        if dirty:
            self.save_to_disk(force=True)

    @staticmethod
    def _make_key(filepath: str | None) -> str | None:
        if not filepath or not os.path.exists(filepath):
            return None
        try:
            mtime = int(os.path.getmtime(filepath))
            size = os.path.getsize(filepath)
            norm_path = os.path.abspath(filepath).lower()
            return f"{norm_path}::{mtime}::{size}"
        except Exception:
            return None

    def get_metadata(self, filepath: str) -> dict[str, Any] | None:
        with self._lock:
            key = self._make_key(filepath)
            if key and key in self._meta_cache:
                self._meta_cache.move_to_end(key)
                return dict(self._meta_cache[key])
            return None

    def set_metadata(self, filepath: str, data: Mapping[str, Any] | Any) -> None:
        """Store metadata fields, merging with any existing fields (e.g. loudness) for the file."""
        with self._lock:
            key = self._make_key(filepath)
            if not key or not data:
                return
            new_fields = dict(data.to_dict()) if hasattr(data, "to_dict") and callable(data.to_dict) else dict(data)
            merged = {**self._meta_cache.pop(key, {}), **new_fields}
            while len(self._meta_cache) >= self.max_meta_entries:
                self._meta_cache.popitem(last=False)
            self._meta_cache[key] = merged
        self._schedule_save()

    def get_duration(self, filepath: str) -> float | None:
        with self._lock:
            meta = self.get_metadata(filepath)
            if meta and "duration" in meta:
                try:
                    return float(meta["duration"])
                except (ValueError, TypeError):
                    pass
            return None

    def set_duration(self, filepath: str, duration: float) -> None:
        self.set_metadata(filepath, {"duration": float(duration)})

    def get_loudness(self, filepath: str) -> float | None:
        """Return the cached gated-RMS loudness (dBFS) measured during waveform analysis."""
        meta = self.get_metadata(filepath)
        value = meta.get("loudness_db") if meta else None
        return float(value) if isinstance(value, (int, float)) else None

    def set_loudness(self, filepath: str, loudness_db: float) -> None:
        self.set_metadata(filepath, {"loudness_db": float(loudness_db)})

    def get_loudnorm_stats(self, filepath: str) -> dict[str, str] | None:
        """Return cached FFmpeg loudnorm first-pass measurements for two-pass normalization."""
        meta = self.get_metadata(filepath)
        stats = meta.get("loudnorm") if meta else None
        return dict(stats) if isinstance(stats, dict) else None

    def set_loudnorm_stats(self, filepath: str, stats: Mapping[str, str]) -> None:
        self.set_metadata(filepath, {"loudnorm": dict(stats)})

    def get_peaks(self, filepath: str) -> list[float] | None:
        with self._lock:
            key = self._make_key(filepath)
            if key and key in self._peaks_cache:
                self._peaks_cache.move_to_end(key)
                return list(self._peaks_cache[key])
            return None

    def set_peaks(self, filepath: str, peaks: list[float]) -> None:
        with self._lock:
            key = self._make_key(filepath)
            if not key or not peaks:
                return
            while len(self._peaks_cache) >= self.max_mem_entries:
                self._peaks_cache.popitem(last=False)
            self._peaks_cache[key] = list(peaks)
        self._schedule_save()

    def set_no_waveform(self, filepath: str) -> None:
        """Remember, until the app closes or the file changes, that no waveform could be made of it."""
        with self._lock:
            key = self._make_key(filepath)
            if key:
                self._no_waveform.add(key)

    def has_no_waveform(self, filepath: str) -> bool:
        """True when making this file's waveform already failed in this session."""
        with self._lock:
            return self._make_key(filepath) in self._no_waveform

    def invalidate(self, filepath: str) -> None:
        """Remove any entries associated with filepath regardless of mtime."""
        if not filepath:
            return
        removed = False
        with self._lock:
            norm = os.path.abspath(filepath).lower()
            prefix = f"{norm}::"
            meta_keys = [k for k in list(self._meta_cache.keys()) if k.startswith(prefix) or k == norm]
            for k in meta_keys:
                del self._meta_cache[k]
                removed = True
            peaks_keys = [k for k in list(self._peaks_cache.keys()) if k.startswith(prefix) or k == norm]
            for k in peaks_keys:
                del self._peaks_cache[k]
                removed = True
            self._no_waveform = {k for k in self._no_waveform if not k.startswith(prefix)}
        if removed:
            self._schedule_save()


# Global singleton instance
cache_mgr = CacheManager()

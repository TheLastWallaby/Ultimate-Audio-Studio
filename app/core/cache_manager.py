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


class CacheManager:
    """Manages thread-safe persistent caching for track metadata, duration, and waveform peaks.

    Keys are keyed by file path, mtime, and file size so any edit or replacement
    automatically invalidates stale cached data.
    """

    def __init__(self, cache_dir: str | None = None, max_mem_entries: int = 1024) -> None:
        self.cache_dir = cache_dir or YT_CACHE_DIR
        self.cache_file = os.path.join(self.cache_dir, CACHE_FILE_NAME)
        self.max_mem_entries = max_mem_entries
        self._lock = threading.RLock()
        self._meta_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._peaks_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._dirty = False
        self._last_save_time = 0.0
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
        """Atomically persist cache contents to disk."""
        with self._lock:
            if not self._dirty and not force:
                return
            now = time.time()
            if not force and (now - self._last_save_time < 2.0):
                return
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                data = {
                    "version": 1,
                    "saved_at": int(now),
                    "meta": dict(self._meta_cache),
                    "peaks": dict(self._peaks_cache),
                }
                atomic_save_json(self.cache_file, data)
                self._dirty = False
                self._last_save_time = now
            except Exception as e:
                log_error(f"CacheManager.save_to_disk: {e}")

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
        with self._lock:
            key = self._make_key(filepath)
            if not key or not data:
                return
            while len(self._meta_cache) >= self.max_mem_entries:
                self._meta_cache.popitem(last=False)
            if hasattr(data, "to_dict") and callable(data.to_dict):
                self._meta_cache[key] = dict(data.to_dict())
            else:
                self._meta_cache[key] = dict(data)
            self._dirty = True
            self.save_to_disk()

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
        with self._lock:
            meta = self.get_metadata(filepath) or {}
            meta["duration"] = float(duration)
            self.set_metadata(filepath, meta)

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
            self._dirty = True
            self.save_to_disk()

    def invalidate(self, filepath: str) -> None:
        """Remove any entries associated with filepath regardless of mtime."""
        if not filepath:
            return
        with self._lock:
            norm = os.path.abspath(filepath).lower()
            prefix = f"{norm}::"
            meta_keys = [k for k in list(self._meta_cache.keys()) if k.startswith(prefix) or k == norm]
            for k in meta_keys:
                del self._meta_cache[k]
                self._dirty = True
            peaks_keys = [k for k in list(self._peaks_cache.keys()) if k.startswith(prefix) or k == norm]
            for k in peaks_keys:
                del self._peaks_cache[k]
                self._dirty = True
            if self._dirty:
                self.save_to_disk(force=True)


# Global singleton instance
cache_mgr = CacheManager()

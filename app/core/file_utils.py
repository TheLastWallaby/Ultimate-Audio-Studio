"""Filesystem I/O, atomic file operations, filename sanitization, and cache hashing."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any


def atomic_save_json(filepath: str | Path, data: Any) -> None:
    """Atomically write JSON data to avoid corruption during crashes or power cuts."""
    p = Path(filepath).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = p.with_name(f"{p.name}.{os.getpid()}_{int(time.time() * 1000)}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(p)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def sanitize_filename(name: str | None, max_len: int = 120) -> str:
    """Sanitize filename to prevent illegal character errors on FAT32/exFAT and NTFS, clamped to max_len."""
    clean = re.sub(r'[\x00-\x1f\\/*?:"<>|]', "_", str(name or "")).strip(". ")
    if not clean:
        return "AudioTrack"
    # Check for Windows DOS reserved device names
    base_check = clean.split(".")[0].upper()
    dos_reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
    if base_check in dos_reserved:
        clean = f"Track_{clean}"
    if len(clean) > max_len:
        clean = clean[:max_len].strip(". ")
    return clean or "AudioTrack"


def hash_file_key(filepath: str | Path) -> str:
    """Generate MD5 cache key for a local file path."""
    norm_path = str(Path(filepath).resolve()).encode("utf-8", errors="ignore")
    return hashlib.md5(norm_path, usedforsecurity=False).hexdigest()


def hash_url_key(url: str, length: int = 16) -> str:
    """Generate MD5 cache key slice for a remote URL."""
    norm_url = str(url).strip().encode("utf-8", errors="ignore")
    digest = hashlib.md5(norm_url, usedforsecurity=False).hexdigest()
    return digest[:length] if length > 0 else digest

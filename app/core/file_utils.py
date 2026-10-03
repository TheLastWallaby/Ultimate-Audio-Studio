"""Filesystem I/O, atomic file operations, filename sanitization, and cache hashing."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any


def replace_with_retry(src: str | Path, dest: str | Path) -> None:
    """``os.replace`` that retries while Windows search indexing or antivirus briefly holds a file."""
    for attempt in range(5):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            time.sleep(0.08 * (attempt + 1))
    os.replace(src, dest)


def fsync_file(path: str | Path) -> None:
    """Write a file's cached data through to the disk; raises OSError.

    Windows records a rename on disk before the data of the renamed file, so after a power cut a
    file swapped into place without this can be empty, with the file it replaced already gone.
    """
    with open(path, "rb+") as f:
        os.fsync(f.fileno())


def copy_file_atomic(src: str | Path, dest: str | Path) -> None:
    """Copy ``src`` to ``dest`` so that ``dest`` ends up either complete or exactly as it was.

    The copy is written next to ``dest`` under a temporary name, checked against the size of the
    source, written through to the disk, and only then renamed into place. A source that disappears
    halfway (an unplugged drive, a full disk) or a power cut therefore never leaves a cut-off file
    under the real name. Raises OSError.
    """
    src, dest = Path(src), Path(dest)
    partial = dest.with_name(f"{dest.name}.{os.getpid()}.partial")
    try:
        shutil.copy2(src, partial)
        if partial.stat().st_size != src.stat().st_size:
            raise OSError(f"the copy of {src.name} is incomplete")
        fsync_file(partial)
        replace_with_retry(partial, dest)
    except OSError:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise


def unused_path(dest: str | Path) -> Path:
    """``dest``, or the first "Name (2).ext"-style name next to it that is not taken yet."""
    dest = Path(dest)
    candidate, number = dest, 2
    while candidate.exists():
        candidate = dest.with_name(f"{dest.stem} ({number}){dest.suffix}")
        number += 1
    return candidate


def atomic_save_json(filepath: str | Path, data: Any) -> None:
    """Atomically write JSON data to avoid corruption during crashes or power cuts (see ``fsync_file``).

    The swap is retried while OneDrive or an antivirus scan briefly holds the file that is replaced.
    """
    p = Path(filepath).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = p.with_name(f"{p.name}.{os.getpid()}_{int(time.time() * 1000)}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp_path, p)
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

"""Subprocess execution and external media tool (FFmpeg/FFprobe) discovery."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings
from app.platform_utils import CREATE_NO_WINDOW

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ProcessResult:
    """Immutable result from a subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str


def is_valid_binary(path: str | Path | None, probe_arg: str = "-version") -> bool:
    """Check if a path points to a genuine, working standalone binary (not a broken shim/stub)."""
    if not path:
        return False
    p = Path(path)
    if not p.is_file():
        return False
    try:
        # A genuine FFmpeg/FFprobe binary is typically 30MB-120MB; shims are < 1MB
        if os.name == "nt" and p.stat().st_size < 5 * 1024 * 1024:
            return False
        res = subprocess.run(
            [str(p), probe_arg],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=3,
        )
        return res.returncode == 0
    except Exception as e:
        logger.debug("is_valid_binary probe failed for %s: %s", path, e)
        return False


def find_system_binary(binary_name: str) -> str | None:
    """Actively locate a genuine standalone binary if the bundled one is missing or an invalid shim."""
    search_dirs = [
        Path.home() / "Downloads" / "installer_files" / "ffmpeg" / "bin",
        Path(r"C:\ProgramData\chocolatey\lib\ffmpeg"),
        Path(r"C:\ffmpeg\bin"),
    ]
    local_app_data = get_settings().system.local_app_data
    if local_app_data:
        search_dirs.append(local_app_data / "Microsoft" / "WinGet" / "Packages")

    variants = [binary_name]
    if binary_name.lower().endswith(".exe"):
        variants.append(binary_name[:-4])
    else:
        variants.append(f"{binary_name}.exe")

    for d in search_dirs:
        if not d.is_dir():
            continue
        try:
            for root, _, files in os.walk(d):
                for v in variants:
                    if v in files:
                        candidate = Path(root) / v
                        if is_valid_binary(candidate):
                            return str(candidate)
        except Exception as e:
            logger.debug("Error searching directory %s for %s: %s", d, binary_name, e)

    for v in variants:
        found = shutil.which(v)
        if found and is_valid_binary(found):
            return str(Path(found))
    return None


class CancelToken(Protocol):
    """Anything with ``is_set()`` (normally a ``threading.Event``)."""

    def is_set(self) -> bool: ...


CANCELLED_STDERR = "FFmpeg was stopped because the task was cancelled."
_CANCEL_POLL_SEC = 0.25


def run_ffmpeg(
    args: list[str],
    timeout: int = 60,
    ffmpeg_bin: str | Path | None = None,
    cancel_event: CancelToken | None = None,
) -> subprocess.CompletedProcess[str] | ProcessResult:
    """Run an FFmpeg command with standard silencing and timeout handling.

    With ``cancel_event``, FFmpeg is killed within a fraction of a second once the event is set (a
    loudness-normalized encode can otherwise run for minutes after the user pressed Stop); the
    result then has returncode -1 and ``CANCELLED_STDERR``.
    """
    if ffmpeg_bin is None:
        from app.config import ffmpeg_path

        ffmpeg_bin = ffmpeg_path

    cmd = [str(ffmpeg_bin), "-nostdin"] + args
    if cancel_event is not None:
        return _run_cancellable(cmd, timeout, cancel_event)
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        msg = f"run_ffmpeg timed out after {timeout}s: {' '.join(str(a) for a in args[:6])}"
        logger.error(msg)
        return ProcessResult(
            returncode=-1,
            stdout="",
            stderr=f"FFmpeg execution timed out after {timeout} seconds.",
        )
    except Exception as e:
        logger.error("run_ffmpeg exception: %s", e)
        return ProcessResult(
            returncode=-1,
            stdout="",
            stderr=str(e),
        )


def _run_cancellable(cmd: list[str], timeout: int, cancel_event: CancelToken) -> ProcessResult:
    """Run ``cmd`` like ``subprocess.run`` but poll ``cancel_event`` while it runs."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        logger.error("run_ffmpeg exception: %s", e)
        return ProcessResult(returncode=-1, stdout="", stderr=str(e))
    with proc:
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, err = proc.communicate(timeout=_CANCEL_POLL_SEC)
                return ProcessResult(returncode=proc.returncode, stdout=out or "", stderr=err or "")
            except subprocess.TimeoutExpired:
                cancelled = cancel_event.is_set()
                if not cancelled and time.monotonic() < deadline:
                    continue
                proc.kill()
                proc.communicate()
                if cancelled:
                    return ProcessResult(returncode=-1, stdout="", stderr=CANCELLED_STDERR)
                logger.error("run_ffmpeg timed out after %ss: %s", timeout, " ".join(cmd[1:7]))
                return ProcessResult(
                    returncode=-1, stdout="", stderr=f"FFmpeg execution timed out after {timeout} seconds."
                )

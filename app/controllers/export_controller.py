"""Export controller managing USB flash drive validation/export and CD staging/export."""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable, Sequence

from app.config import log_error, sanitize_filename
from app.core.task_manager import task_mgr
from app.platform_utils import get_desktop_dir, safely_eject_usb_drive
from app.services.exporter import cd_export_worker, find_previous_export, usb_export_worker

DurationFn = Callable[[str], float]


class ExportController:
    """Manages pre-flight checks and background workers for USB and CD audio exports.

    One export runs at a time; :meth:`cancel` stops it (the running FFmpeg is killed at once).
    """

    def __init__(self, app: object) -> None:
        self.app = app
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Stop the running export after cleaning up the song it was working on."""
        self._cancel_event.set()

    def _new_job(self) -> threading.Event:
        self._cancel_event = threading.Event()
        return self._cancel_event

    def get_cd_burn_folder(self) -> str:
        """Locate or create standard CD burn folder on user's true Windows Desktop."""
        desktop = get_desktop_dir()
        cd_folder = os.path.join(desktop, "My_CD_Burn_Folder")
        os.makedirs(cd_folder, exist_ok=True)
        return cd_folder

    @staticmethod
    def usb_playlist_folder(drive_root: str, playlist_name: str) -> str:
        """Folder on the drive that holds this playlist's songs."""
        return os.path.join(drive_root, sanitize_filename(playlist_name or "Playlist"))

    @staticmethod
    def previous_export_files(folder: str) -> list[str]:
        """Songs and playlist files an earlier export left in ``folder``."""
        return find_previous_export(folder)

    @staticmethod
    def is_ntfs(fs_type: str | None) -> bool:
        """Check if filesystem type is NTFS."""
        return (fs_type or "").upper() == "NTFS"

    def estimate_playlist_bytes(self, playlist_files: Sequence[str], duration_fn: DurationFn | None = None) -> int:
        """Estimate total disk bytes needed for playlist export with 15MB safety overhead."""
        est_bytes = 0
        for f in playlist_files:
            if os.path.exists(f):
                est_bytes += max(os.path.getsize(f), int((duration_fn(f) if duration_fn else 0.0) * 40000))
            else:
                est_bytes += int((duration_fn(f) if duration_fn else 0.0) * 40000)
        return max(est_bytes + 15 * 1024 * 1024, 20 * 1024 * 1024)

    def check_usb_space(self, dest_folder: str, required_bytes: int, freed_bytes: int = 0) -> tuple[bool, int]:
        """Check if destination has room, counting ``freed_bytes`` that will be deleted first."""
        try:
            free = shutil.disk_usage(dest_folder).free + max(0, freed_bytes)
            return (free >= required_bytes), free
        except Exception as e:
            log_error(f"check_usb_space: {e}")
            return True, 0

    def get_playlist_duration(self, playlist_files: Sequence[str], duration_fn: DurationFn | None) -> float:
        """Calculate total seconds for all files in playlist."""
        return sum(duration_fn(f) if duration_fn else 0.0 for f in playlist_files)

    def start_usb_export(
        self,
        dest_folder: str,
        playlist_name: str,
        playlist_files: Sequence[str],
        normalize: bool,
        on_progress: Callable[[float], None],
        on_status: Callable[[str], None],
        on_success: Callable[[int, int, list[str]], None],
        on_error: Callable[[str], None],
        is_shutting_down_fn: Callable[[], bool],
        duration_fn: DurationFn,
        on_cancelled: Callable[[int, int], None] | None = None,
        clear_existing: bool = False,
    ) -> None:
        """Launch background USB export worker into the playlist's folder on the drive."""
        pl_dest = self.usb_playlist_folder(dest_folder, playlist_name)
        os.makedirs(pl_dest, exist_ok=True)
        task_mgr.submit_task(
            usb_export_worker,
            pl_dest,
            playlist_name,
            list(playlist_files),
            normalize,
            on_progress,
            on_status,
            on_success,
            on_error,
            is_shutting_down_fn,
            duration_fn,
            cancel_event=self._new_job(),
            on_cancelled=on_cancelled,
            clear_existing=clear_existing,
        )

    def start_cd_export(
        self,
        cd_folder: str,
        playlist_files: Sequence[str],
        normalize: bool,
        on_progress: Callable[[float], None],
        on_status: Callable[[str], None],
        on_success: Callable[[str, int, int, list[str]], None],
        on_error: Callable[[str], None],
        is_shutting_down_fn: Callable[[], bool],
        on_cancelled: Callable[[int, int], None] | None = None,
        clear_existing: bool = False,
    ) -> None:
        """Launch background CD WAV export worker; ``clear_existing`` removes an earlier export's tracks first."""
        task_mgr.submit_task(
            cd_export_worker,
            cd_folder,
            list(playlist_files),
            normalize,
            on_progress,
            on_status,
            on_success,
            on_error,
            is_shutting_down_fn,
            cancel_event=self._new_job(),
            on_cancelled=on_cancelled,
            clear_existing=clear_existing,
        )

    def eject_usb_drive(self, drive_root: str) -> tuple[bool, str]:
        """Safely flush buffers and eject USB drive."""
        ok, msg = safely_eject_usb_drive(drive_root)
        return bool(ok), str(msg)

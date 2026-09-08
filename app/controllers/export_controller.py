"""Export controller managing USB flash drive validation/export and CD staging/export."""

import os
import shutil

from app.config import log_error, sanitize_filename
from app.core.task_manager import task_mgr
from app.platform_utils import get_desktop_dir, safely_eject_usb_drive
from app.services.exporter import cd_export_worker, usb_export_worker


class ExportController:
    """Manages pre-flight checks and background workers for USB and CD audio exports."""

    def __init__(self, app):
        self.app = app

    def get_cd_burn_folder(self):
        """Locate or create standard CD burn folder on user's true Windows Desktop."""
        desktop = get_desktop_dir()
        cd_folder = os.path.join(desktop, "My_CD_Burn_Folder")
        os.makedirs(cd_folder, exist_ok=True)
        return cd_folder

    def get_cd_existing_files(self, cd_folder):
        """Return non-hidden files currently in CD burn folder."""
        if not os.path.exists(cd_folder):
            return []
        try:
            return [f for f in os.listdir(cd_folder) if not f.startswith(".")]
        except Exception:
            return []

    def clear_cd_folder(self, cd_folder):
        """Delete files from previous CD export."""
        for f in self.get_cd_existing_files(cd_folder):
            try:
                f_path = os.path.join(cd_folder, f)
                if os.path.isfile(f_path):
                    os.remove(f_path)
            except Exception:
                pass

    @staticmethod
    def is_ntfs(fs_type):
        """Check if filesystem type is NTFS."""
        return (fs_type or "").upper() == "NTFS"

    def estimate_playlist_bytes(self, playlist_files, duration_fn=None):
        """Estimate total disk bytes needed for playlist export with 15MB safety overhead."""
        est_bytes = 0
        for f in playlist_files:
            if os.path.exists(f):
                est_bytes += max(os.path.getsize(f), int((duration_fn(f) if duration_fn else 0.0) * 40000))
            else:
                est_bytes += int((duration_fn(f) if duration_fn else 0.0) * 40000)
        return max(est_bytes + 15 * 1024 * 1024, 20 * 1024 * 1024)

    def check_usb_space(self, dest_folder, required_bytes):
        """Check if destination folder has sufficient free disk space."""
        try:
            usage = shutil.disk_usage(dest_folder)
            return (usage.free >= required_bytes), usage.free
        except Exception as e:
            log_error(f"check_usb_space: {e}")
            return True, 0

    def get_playlist_duration(self, playlist_files, duration_fn):
        """Calculate total seconds for all files in playlist."""
        return sum(duration_fn(f) if duration_fn else 0.0 for f in playlist_files)

    def start_usb_export(
        self,
        dest_folder,
        playlist_name,
        playlist_files,
        normalize,
        on_progress,
        on_status,
        on_success,
        on_error,
        is_shutting_down_fn,
        duration_fn,
    ):
        """Launch background USB export worker."""
        clean_pl = sanitize_filename(playlist_name or "Playlist")
        pl_dest = os.path.join(dest_folder, clean_pl)
        os.makedirs(pl_dest, exist_ok=True)

        task_mgr.submit_task(
            usb_export_worker,
            pl_dest, playlist_name, playlist_files, normalize,
            on_progress, on_status, on_success, on_error,
            is_shutting_down_fn, duration_fn
        )

    def start_cd_export(
        self,
        cd_folder,
        playlist_files,
        normalize,
        on_progress,
        on_status,
        on_success,
        on_error,
        is_shutting_down_fn,
    ):
        """Launch background CD WAV export worker."""
        task_mgr.submit_task(
            cd_export_worker,
            cd_folder, playlist_files, normalize,
            on_progress, on_status, on_success, on_error,
            is_shutting_down_fn
        )

    def eject_usb_drive(self, drive_root):
        """Safely flush buffers and eject USB drive."""
        return safely_eject_usb_drive(drive_root)

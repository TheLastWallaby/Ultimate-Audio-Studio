"""Step 3 export: USB drive detection/eject and USB/CD playlist export."""

from __future__ import annotations

import os
import tkinter as tk

from app.config import log_error, sanitize_filename
from app.core.cache_manager import cache_mgr
from app.core.task_manager import task_mgr
from app.platform_utils import find_windows_media_player, list_removable_drives
from app.ui import dialogs
from app.ui.error_dialog import show_friendly_error
from app.ui.features.base import AppBase

EXPORT_BUTTON_TEXT = "⚡ Export Playlist Now"


class ExportMixin(AppBase):
    """Step 3 export: USB drive detection/eject and USB/CD playlist export."""

    def _on_usb_hotplug(self) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        if self._timer_hotplug_debounce:
            try:
                self.root.after_cancel(self._timer_hotplug_debounce)
            except Exception:
                pass

        def _do_hotplug() -> None:
            if getattr(self, "_is_shutting_down", False):
                return
            old_keys = set(self._usb_map.keys())
            self.refresh_usb_drives()
            new_keys = set(self._usb_map.keys())
            added = new_keys - old_keys
            if added:
                new_drive = list(added)[0]
                self.set_status(f"USB Flash Drive detected: {new_drive}", icon="💾")
            elif old_keys - new_keys:
                self.set_status("USB Flash Drive unplugged.", icon="ℹ️")

        self._timer_hotplug_debounce = self.root.after(800, _do_hotplug)

    def refresh_usb_drives(self) -> None:
        drives = list_removable_drives()
        self._usb_map = {label: path for path, label, _ in drives}
        self._usb_fs_map = {label: fs for _, label, fs in drives}
        options = list(self._usb_map.keys())
        self.cmb_usb["values"] = options
        if options:
            self.cmb_usb.current(0)
            self.set_status(f"Found {len(options)} USB flash drive(s).")
        else:
            self.usb_choice.set("No USB drives detected")
            self.set_status("No USB flash drives found. Plug in a USB drive and click 🔄.")
        if hasattr(self, "btn_usb_eject") and self.btn_usb_eject:
            self.btn_usb_eject.config(state=tk.NORMAL if options else tk.DISABLED)

    def eject_selected_usb(self) -> None:
        choice = self.usb_choice.get()
        if not choice or choice not in self._usb_map:
            dialogs.show_info(self.root, "No Drive", "Please select a connected USB drive to safely eject.")
            return
        drive_path = self._usb_map[choice]
        ok, msg = self.export_ctrl.eject_usb_drive(drive_path)
        self.refresh_usb_drives()
        if ok:
            dialogs.show_info(
                self.root,
                "Safe to Remove Hardware",
                f"The USB drive ({choice}) was safely ejected.\n\nYou may now unplug it.",
            )
            self.set_status(f"USB drive {choice} safely ejected.")
        else:
            dialogs.show_warning(
                self.root,
                "Ejection Failed",
                f"Could not eject drive ({choice}):\n{msg}\n\nPlease ensure no open files or Explorer windows are accessing it.",
            )

    def export_playlist(self) -> None:
        """Check the songs' lengths off the UI thread when needed, then ask the export questions."""
        if not self.playlist_files:
            dialogs.show_warning(self.root, "Empty Playlist", "Add some songs to this playlist before exporting.")
            return
        files = list(self.playlist_files)
        # Durations drive the disk-space and 80-minute checks; reading an uncached one can start
        # ffprobe, which must not freeze the window. Usually they were read in the background already.
        uncached = [f for f in files if os.path.isfile(f) and cache_mgr.get_duration(f) is None]
        if not uncached:
            self._export_after_preflight(files)
            return
        self.btn_export.config(text="Checking songs...", state=tk.DISABLED)
        self.set_busy(True, f"Checking {len(uncached)} song(s) before exporting...")

        def _worker() -> None:
            for path in uncached:
                if getattr(self, "_is_shutting_down", False):
                    return
                self._cached_duration(path)
            self._safe_after(0, _ready)

        def _ready() -> None:
            self.btn_export.config(text=EXPORT_BUTTON_TEXT, state=tk.NORMAL)
            self.set_busy(False)
            self._export_after_preflight(files)

        task_mgr.submit_task(_worker)

    def _cached_duration_only(self, path: str) -> float:
        return self._cached_duration(path, probe=False)

    def _export_after_preflight(self, files: list[str]) -> None:
        normalize = bool(self.even_volume.get())
        if self.export_var.get() == "USB":
            self._export_to_usb(files, normalize)
        else:
            self._export_to_cd(files, normalize)

    def _export_to_usb(self, files: list[str], normalize: bool) -> None:
        choice = self.usb_choice.get()
        if not choice or choice not in self._usb_map:
            dialogs.show_warning(
                self.root, "No USB Drive", "Please insert a USB flash drive and select it from the list."
            )
            return

        drive_root = self._usb_map[choice]
        if not os.path.exists(drive_root):
            dialogs.show_warning(
                self.root, "Drive Missing", "The selected USB drive can no longer be found. Please plug it in again."
            )
            return

        fs_type = self._usb_fs_map.get(choice, "")
        if self.export_ctrl.is_ntfs(fs_type) and not dialogs.ask_yes_no(
            self.root,
            "This Drive May Not Play in a Car",
            f"The selected drive ({choice}) is formatted as NTFS.\n\n"
            "Many car stereos and older stereos only read FAT32 or exFAT drives, and will show "
            "'No Device' or 'Read Error' for this one.",
            yes="Export anyway",
            no="Cancel",
            default_yes=False,
            icon=dialogs.ICON_WARNING,
        ):
            return

        # An earlier export of this playlist: its numbered files would otherwise stay on the drive, so
        # songs removed or moved since keep playing (and in the wrong order).
        playlist_folder = self.export_ctrl.usb_playlist_folder(drive_root, self.active_playlist_name)
        previous = self.export_ctrl.previous_export_files(playlist_folder)
        clear_existing = False
        freed_bytes = 0
        if previous:
            songs = sum(1 for p in previous if not p.lower().endswith((".m3u", ".m3u8")))
            answer = dialogs.ask_choice(
                self.root,
                "Playlist Already on This Drive",
                f"The drive already has {songs} song(s) from an earlier export of "
                f"'{self.active_playlist_name}'.\n\n"
                "Replacing them makes the drive match your playlist exactly. Keeping them can leave "
                "songs you have removed or moved since.",
                [
                    dialogs.DialogButton("Replace the old songs", "replace", "primary"),
                    dialogs.DialogButton("Keep them and add these", "keep"),
                    dialogs.DialogButton("Cancel", "cancel"),
                ],
                cancel_value="cancel",
            )
            if answer == "cancel":
                return
            clear_existing = answer == "replace"
            if clear_existing:
                freed_bytes = sum(os.path.getsize(p) for p in previous if os.path.isfile(p))

        # Pre-flight disk space validation
        est_bytes = self.export_ctrl.estimate_playlist_bytes(files, self._cached_duration_only)
        has_space, free_bytes = self.export_ctrl.check_usb_space(drive_root, est_bytes, freed_bytes)
        if not has_space:
            free_mb = free_bytes / (1024 * 1024)
            needed_mb = est_bytes / (1024 * 1024)
            dialogs.show_warning(
                self.root,
                "Not Enough Space on the USB Drive",
                f"The selected USB drive only has {free_mb:.0f} MB of free space.\n\n"
                f"This playlist needs about {needed_mb:.0f} MB.\n\n"
                "Please delete files from the USB flash drive or use a drive with more free space.",
            )
            return

        self._begin_export("Exporting...", f"Exporting {len(files)} songs to the USB flash drive...")
        self.export_ctrl.start_usb_export(
            drive_root,
            self.active_playlist_name,
            files,
            normalize,
            on_progress=lambda pct: self._safe_after(0, self._update_export_progress, pct),
            on_status=lambda text: self._safe_after(0, self.set_status, text),
            on_success=lambda sc, tot, sk: self._safe_after(0, self._usb_export_success, sc, tot, sk),
            on_error=lambda err: self._safe_after(0, self._export_error, err),
            is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
            duration_fn=self._cached_duration,
            on_cancelled=lambda done, tot: self._safe_after(0, self._export_cancelled, done, tot, "USB"),
            clear_existing=clear_existing,
        )

    def _export_to_cd(self, files: list[str], normalize: bool) -> None:
        cd_folder = self.export_ctrl.get_cd_burn_folder()
        # Only tracks this app made count (and are ever removed); other files in the folder are the user's.
        previous = self.export_ctrl.previous_export_files(cd_folder)
        clear_existing = False
        if previous:
            answer = dialogs.ask_choice(
                self.root,
                "Previous CD Files Found",
                f"The CD folder ('My_CD_Burn_Folder' on your Desktop) still has {len(previous)} "
                "track(s) from an earlier export.",
                [
                    dialogs.DialogButton("Remove them and start fresh", "clear", "primary"),
                    dialogs.DialogButton("Keep them and add these", "keep"),
                    dialogs.DialogButton("Cancel", "cancel"),
                ],
                cancel_value="cancel",
            )
            if answer == "cancel":
                return
            # Nothing is removed here: the export job does it, once every question has been answered.
            clear_existing = answer == "clear"

        # 80-minute CD capacity validation
        total_sec = self.export_ctrl.get_playlist_duration(files, self._cached_duration_only)
        if total_sec > 80 * 60 and not dialogs.ask_yes_no(
            self.root,
            "Playlist Is Longer Than One CD",
            "An audio CD holds 80 minutes of music.\n\n"
            f"This playlist is {int(total_sec // 60)} minutes long, so some songs will not fit on one blank CD.",
            yes="Prepare all songs anyway",
            no="Cancel",
            icon=dialogs.ICON_WARNING,
        ):
            return

        self._begin_export("Preparing CD...", f"Preparing {len(files)} CD audio tracks...")
        self.export_ctrl.start_cd_export(
            cd_folder,
            files,
            normalize,
            on_progress=lambda pct: self._safe_after(0, self._update_export_progress, pct),
            on_status=lambda text: self._safe_after(0, self.set_status, text),
            on_success=lambda fld, sc, tot, sk: self._safe_after(0, self._cd_export_success, fld, sc, tot, sk),
            on_error=lambda err: self._safe_after(0, self._export_error, err),
            is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
            on_cancelled=lambda done, tot: self._safe_after(0, self._export_cancelled, done, tot, "CD"),
            clear_existing=clear_existing,
        )

    def _begin_export(self, button_text: str, status: str) -> None:
        self._exporting = True
        self.btn_export.config(text=button_text, state=tk.DISABLED)
        self.prog_export.pack(fill=tk.X, pady=(4, 2), before=self.btn_export)
        self.prog_export["value"] = 0
        self.btn_cancel_export.config(text="⏹ Stop Export", state=tk.NORMAL)
        self.btn_cancel_export.pack(fill=tk.X, pady=(1, 2), after=self.btn_export)
        self.set_busy(True, status)

    def _end_export(self, status: str) -> None:
        self._exporting = False
        self.btn_export.config(text=EXPORT_BUTTON_TEXT, state=tk.NORMAL)
        self.prog_export.pack_forget()
        self.btn_cancel_export.pack_forget()
        self.set_busy(False, status)

    def cancel_export(self) -> None:
        if not self._exporting:
            return
        self.export_ctrl.cancel()
        self.btn_cancel_export.config(text="Stopping...", state=tk.DISABLED)
        self.set_status("Stopping the export...", icon="⏳")

    def _export_cancelled(self, done: int, total: int, target: str) -> None:
        if target == "USB":
            self._end_export(
                f"Export stopped. {done} of {total} songs were copied to the USB drive. "
                "Eject the drive before unplugging it."
            )
        else:
            self._end_export(f"Export stopped. {done} of {total} CD tracks were prepared.")

    def _update_export_progress(self, pct: float) -> None:
        if hasattr(self, "prog_export"):
            self.prog_export["value"] = pct

    def _usb_export_success(self, success_count: int, total: int, skipped: list[str]) -> None:
        self._end_export("USB export finished. Eject the drive before unplugging it.")
        clean_pl = sanitize_filename(self.active_playlist_name or "Playlist")
        if skipped:
            msg = (
                f"Exported {success_count} of {total} song(s) to the USB flash drive (with '00_{clean_pl}.m3u' "
                f"playlist).\n\n{len(skipped)} song(s) could not be processed:\n"
            )
            msg += "\n".join(f"• {s}" for s in skipped[:6])
            if len(skipped) > 6:
                msg += f"\n... and {len(skipped) - 6} more."
            dialogs.show_warning(self.root, "Export Finished with Some Problems", msg)
            title = "Eject USB Drive"
            question = "Would you like to safely eject the USB flash drive now so you can unplug it?"
        else:
            title = "Export Finished"
            question = (
                f"Your playlist was copied to the USB flash drive, with a '00_{clean_pl}.m3u' playlist file "
                "for car stereos and media players.\n\nBefore unplugging it, the drive should be ejected."
            )
        if dialogs.ask_yes_no(self.root, title, question, yes="Eject the drive now", no="Not now", icon="✅"):
            self.eject_selected_usb()

    def _cd_export_success(self, cd_folder: str, success_count: int, total: int, skipped: list[str]) -> None:
        self._end_export("CD files are ready on your Desktop in 'My_CD_Burn_Folder'.")
        warn_text = ""
        if skipped:
            warn_text = f"\n\nNote: {len(skipped)} song(s) were skipped:\n" + "\n".join(f"• {s}" for s in skipped[:5])

        # File Explorer's 'Send to / Burn to disc' makes a DATA disc that most CD players and car
        # stereos cannot play. An audio CD must be burned with Windows Media Player's Burn list.
        wmp = find_windows_media_player()
        steps = (
            "To make a CD that plays in any CD player or car stereo:\n"
            "1. Insert a blank CD-R.\n"
            "2. In Windows Media Player, click the 'Burn' tab (top right).\n"
            "3. Click the Burn options button and choose 'Audio CD'.\n"
            "4. Drag all the songs from the CD folder into the Burn list.\n"
            "5. Click 'Start burn'.\n\n"
            "Please do not use File Explorer's 'Send to' for this: it makes a data disc that most CD players cannot play."
        )
        msg = f"Prepared {success_count} of {total} CD track(s) in 'My_CD_Burn_Folder' on your Desktop.{warn_text}\n\n{steps}"
        if wmp:
            if dialogs.ask_yes_no(
                self.root,
                "Ready to Burn",
                msg,
                yes="Open Media Player and the CD folder",
                no="Not now",
                icon="💿",
            ):
                try:
                    os.startfile(cd_folder)
                    os.startfile(wmp)
                except OSError as e:
                    log_error(f"open CD burn tools: {e}")
        else:
            dialogs.show_info(
                self.root,
                "Ready to Burn",
                msg + "\n\nWindows Media Player is not installed on this PC. You can add it in "
                "Settings > Apps > Optional features > 'Windows Media Player Legacy'.",
                button="Open the CD folder",
                icon="💿",
            )
            try:
                os.startfile(cd_folder)
            except OSError:
                pass

    def _export_error(self, err: str) -> None:
        self._end_export("Export failed.")
        show_friendly_error(self.root, err, "export")

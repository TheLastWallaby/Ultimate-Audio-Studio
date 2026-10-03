"""Step 3 export: USB drive detection/eject and USB/CD playlist export."""

from __future__ import annotations

import contextlib
import logging
import os
import tkinter as tk
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from app.config import sanitize_filename
from app.controllers.export_controller import UsbTarget
from app.core.cache_manager import cache_mgr
from app.core.task_manager import task_mgr
from app.models import DriveInfo
from app.platform_utils import find_windows_media_player, list_removable_drives
from app.services.exporter import ExportReport
from app.ui import dialogs
from app.ui.error_dialog import show_friendly_error
from app.ui.features.base import AppBase

logger = logging.getLogger(__name__)

EXPORT_BUTTON_TEXT = "⚡ Export Playlist Now"

# What a drive scan reports in the status bar: everything, only a drive that came or went, or nothing.
DriveAnnounce = Literal["always", "changes", "quiet"]


def _listed(names: Sequence[str], limit: int = 6) -> str:
    """Bullet list of the first ``limit`` names, with a count of the rest."""
    lines = "\n".join(f"• {name}" for name in names[:limit])
    return lines + (f"\n... and {len(names) - limit} more." if len(names) > limit else "")


def export_problems(report: ExportReport, drive: str = "The USB drive") -> str:
    """Plain-language list of everything an export did not do as asked; empty when it did it all.

    ``drive`` names where the songs went, for the message about a drive that gave out.
    """
    parts = []
    if report.stopped == "removed":
        parts.append(
            f"{drive} was unplugged or stopped working, so the export stopped after "
            f"{report.exported} of {report.total} song(s).\n\n"
            "• Plug the drive in again, then export the playlist again."
        )
    elif report.stopped == "full":
        parts.append(
            f"{drive} is full, so the export stopped after {report.exported} of {report.total} song(s).\n\n"
            "• Delete files you no longer need from it, or use a bigger drive, then export the playlist again."
        )
    if report.skipped:
        parts.append(f"{len(report.skipped)} song(s) could not be processed:\n{_listed(report.skipped)}")
    if report.not_leveled:
        parts.append(
            f"{len(report.not_leveled)} song(s) were exported at their original volume, because their "
            f"volume could not be evened out:\n{_listed(report.not_leveled)}"
        )
    return "\n\n".join(parts)


class ExportMixin(AppBase):
    """Step 3 export: USB drive detection/eject and USB/CD playlist export."""

    # Root of the USB drive an export is writing to right now; it must not be ejected meanwhile.
    _export_drive_root: str | None = None
    # True while a drive is being ejected on a worker; a second eject, or an export, must wait.
    _ejecting = False
    # Numbers the drive scans, so the answer of an older, slower scan is not shown over a newer one.
    _usb_scan_id = 0

    def _on_usb_hotplug(self) -> None:
        """Re-read the drives after Windows reports a device change (debounced: one change sends several)."""
        if getattr(self, "_is_shutting_down", False):
            return
        if self._timer_hotplug_debounce:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._timer_hotplug_debounce)

        def _do_hotplug() -> None:
            self._timer_hotplug_debounce = None
            if not getattr(self, "_is_shutting_down", False):
                # Headphones and Bluetooth devices send the same message; only a USB drive that came
                # or went is worth a word in the status bar.
                self.refresh_usb_drives(announce="changes")

        self._timer_hotplug_debounce = self.root.after(800, _do_hotplug)

    def refresh_usb_drives(self, announce: DriveAnnounce = "always") -> None:
        """Re-read the plugged-in USB drives on a worker; the list updates when they have been read.

        Reading a drive can take seconds (a waking hard disk, a slow card reader), so it never runs
        on the window's thread. ``announce`` says what the status bar reports: everything (the 🔄
        button), only a drive that came or went (device changes), or nothing (start-up).
        """
        self._usb_scan_id += 1
        scan_id = self._usb_scan_id

        def _worker() -> None:
            drives = list_removable_drives()
            if not getattr(self, "_is_shutting_down", False):
                self._safe_after(0, self._show_usb_drives, scan_id, drives, announce)

        task_mgr.submit_task(_worker)

    def _show_usb_drives(self, scan_id: int, drives: Sequence[DriveInfo], announce: DriveAnnounce) -> None:
        """Fill the drive list, keeping the chosen drive selected while it is still plugged in."""
        if scan_id != self._usb_scan_id or getattr(self, "_is_shutting_down", False):
            return  # a newer scan is on its way
        previous = self.usb_choice.get()
        old_labels = set(self._usb_map)
        self._usb_map = {label: path for path, label, _ in drives}
        self._usb_fs_map = {label: fs for _, label, fs in drives}
        options = list(self._usb_map)
        self.cmb_usb["values"] = options
        if options:
            # Plugging in a second drive must not switch the export (or the eject) to it unasked.
            self.cmb_usb.current(options.index(previous) if previous in options else 0)
        else:
            self.usb_choice.set("No USB drives detected")
        if hasattr(self, "btn_usb_eject") and self.btn_usb_eject and not self._ejecting:
            self.btn_usb_eject.config(state=tk.NORMAL if options else tk.DISABLED)

        added = [label for label in options if label not in old_labels]
        removed = old_labels - set(options)
        if (added or removed) and announce != "quiet":
            # Songs on that drive are there again (or gone): the lists re-check their "[Missing]" marks.
            self._warm_library_metadata()
        if announce == "quiet" or self._busy:
            return  # never cover up the progress of a download or an export
        if added and announce == "changes":
            self.set_status(f"USB Flash Drive detected: {added[0]}", icon="💾")
        elif removed and announce == "changes":
            self.set_status("USB Flash Drive unplugged.", icon="ℹ️")
        elif announce == "always":
            if options:
                self.set_status(f"Found {len(options)} USB flash drive(s).")
            else:
                self.set_status("No USB flash drives found. Plug in a USB drive and click 🔄.")

    def on_export_target_changed(self) -> None:
        """USB or CD was chosen: the drive list and Eject are shown only for USB, where they apply."""
        if self.export_var.get() == "USB":
            self.f_usb.pack(fill=tk.X, pady=2, before=self.chk_even_volume)
        else:
            self.f_usb.pack_forget()

    def eject_selected_usb(self) -> None:
        """Eject the drive chosen in the list (the Eject button)."""
        choice = self.usb_choice.get()
        if not choice or choice not in self._usb_map:
            dialogs.show_info(self.root, "No Drive", "Please select a connected USB drive to safely eject.")
            return
        self._eject_drive(choice, self._usb_map[choice])

    def _eject_drive(self, choice: str, drive_path: str) -> None:
        """Safely eject one specific drive on a worker, then tell the user whether it can be unplugged.

        Windows can take several seconds to let a drive go, so the window stays responsive and says
        "Ejecting..." meanwhile.
        """
        exporting_to = self._export_drive_root
        if exporting_to is not None and Path(drive_path) == Path(exporting_to):
            dialogs.show_info(
                self.root,
                "Export Still Running",
                "Songs are still being copied to this USB drive, so it cannot be ejected yet.\n\n"
                "Wait for the export to finish, or click 'Stop Export' first.",
            )
            return
        if self._ejecting:
            return
        self._ejecting = True
        if hasattr(self, "btn_usb_eject") and self.btn_usb_eject:
            self.btn_usb_eject.config(state=tk.DISABLED)
        self.set_busy(True, f"Ejecting the USB drive ({choice})... Please wait before unplugging it.")

        def _worker() -> None:
            ok, msg = self.export_ctrl.eject_usb_drive(drive_path)
            self._safe_after(0, self._eject_finished, choice, ok, msg)

        task_mgr.submit_task(_worker)

    def _eject_finished(self, choice: str, ok: bool, msg: str) -> None:
        """Report the eject, and read the drives again (the ejected one is gone from the list)."""
        self._ejecting = False
        self.set_busy(False)
        self.refresh_usb_drives(announce="quiet")
        if ok:
            self.set_status(f"USB drive {choice} safely ejected.")
            dialogs.show_info(
                self.root,
                "Safe to Remove Hardware",
                f"The USB drive ({choice}) was safely ejected.\n\nYou may now unplug it.",
            )
        else:
            self.set_status(f"USB drive {choice} was not ejected.", icon="⚠️")
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
        """Check the chosen drive on a worker, then ask the export questions (``_usb_target_checked``)."""
        choice = self.usb_choice.get()
        if not choice or choice not in self._usb_map:
            dialogs.show_warning(
                self.root, "No USB Drive", "Please insert a USB flash drive and select it from the list."
            )
            return

        if self._ejecting:
            dialogs.show_info(
                self.root, "Drive Is Being Ejected", "Wait until the USB drive has been ejected, then try again."
            )
            return

        drive_root = self._usb_map[choice]
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

        # Reading the drive (is it there, what is on it, how much room is left) can take seconds on
        # a drive that has gone to sleep, so it is not done on the window's thread.
        playlist = self.active_playlist_name
        self.btn_export.config(text="Checking the drive...", state=tk.DISABLED)
        self.set_busy(True, "Checking the USB drive...")

        def _worker() -> None:
            target = self.export_ctrl.inspect_usb_target(drive_root, playlist)
            self._safe_after(0, self._usb_target_checked, files, normalize, choice, drive_root, playlist, target)

        if task_mgr.submit_task(_worker) is None:  # the app is closing
            self.btn_export.config(text=EXPORT_BUTTON_TEXT, state=tk.NORMAL)
            self.set_busy(False)

    def _usb_target_checked(
        self, files: list[str], normalize: bool, choice: str, drive_root: str, playlist: str, target: UsbTarget
    ) -> None:
        """Ask every remaining question about the drive (nothing on it is touched yet), then start."""
        self.btn_export.config(text=EXPORT_BUTTON_TEXT, state=tk.NORMAL)
        self.set_busy(False)
        if not target.connected:
            dialogs.show_warning(
                self.root, "Drive Missing", "The selected USB drive can no longer be found. Please plug it in again."
            )
            return

        # An earlier export of this playlist: its numbered files would otherwise stay on the drive, so
        # songs removed or moved since keep playing (and in the wrong order).
        clear_existing = False
        if target.previous:
            songs = sum(1 for p in target.previous if not p.lower().endswith((".m3u", ".m3u8")))
            answer = dialogs.ask_choice(
                self.root,
                "Playlist Already on This Drive",
                f"The drive already has {songs} song(s) from an earlier export of '{playlist}'.\n\n"
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

        est_bytes = self.export_ctrl.estimate_playlist_bytes(files, self._cached_duration_only, normalize)
        if target.free_bytes is not None:
            free_bytes = target.free_bytes + (target.previous_bytes if clear_existing else 0)
            if free_bytes < est_bytes:
                dialogs.show_warning(
                    self.root,
                    "Not Enough Space on the USB Drive",
                    f"The selected USB drive only has {free_bytes / 1024**2:.0f} MB of free space.\n\n"
                    f"This playlist needs about {est_bytes / 1024**2:.0f} MB.\n\n"
                    "Please delete files from the USB flash drive or use a drive with more free space.",
                )
                return

        self._export_drive_root = drive_root
        self._begin_export("Exporting...", f"Exporting {len(files)} songs to the USB flash drive...")
        self.export_ctrl.start_usb_export(
            drive_root,
            playlist,
            files,
            normalize,
            on_progress=lambda pct: self._safe_after(0, self._update_export_progress, pct),
            on_status=lambda text: self._safe_after(0, self.set_status, text),
            # The drive and the playlist are fixed here: by the time the export ends, others may be selected.
            on_success=lambda report: self._safe_after(
                0, self._usb_export_success, report, choice, drive_root, playlist
            ),
            on_error=lambda err: self._safe_after(0, self._export_error, err),
            is_shutting_down_fn=lambda: self._is_shutting_down,
            duration_fn=self._cached_duration,
            on_cancelled=lambda done, tot: self._safe_after(0, self._export_cancelled, done, tot, "USB"),
            clear_existing=clear_existing,
        )

    def _export_to_cd(self, files: list[str], normalize: bool) -> None:
        """Ask the CD export questions (nothing is removed or written before they are answered), then start."""
        try:
            cd_folder = self.export_ctrl.get_cd_burn_folder()
        except OSError as err:
            logger.error("The CD folder could not be made: %s", err)
            show_friendly_error(self.root, err, "export")
            return
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

        # CD tracks are uncompressed, about 10 MB a minute: a full CD needs some 800 MB on this computer.
        needed = self.export_ctrl.estimate_cd_bytes(files, self._cached_duration_only)
        freed = sum(Path(p).stat().st_size for p in previous if Path(p).is_file()) if clear_existing else 0
        has_space, free_bytes = self.export_ctrl.check_usb_space(cd_folder, needed, freed)
        if not has_space:
            dialogs.show_warning(
                self.root,
                "Not Enough Space on This Computer",
                f"The CD tracks need about {needed / 1024**2:.0f} MB, but this computer's disk only has "
                f"{free_bytes / 1024**2:.0f} MB of free space.\n\n"
                "• Empty the Recycle Bin, or delete files you no longer need.\n"
                "• Then export the playlist again.",
            )
            return

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
            on_success=lambda report: self._safe_after(0, self._cd_export_success, cd_folder, report),
            on_error=lambda err: self._safe_after(0, self._export_error, err),
            is_shutting_down_fn=lambda: self._is_shutting_down,
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
        self._export_drive_root = None
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

    def _usb_export_success(
        self, report: ExportReport, drive_label: str, drive_root: str, playlist: str | None = None
    ) -> None:
        """Report a finished USB export of ``playlist`` and offer to eject the drive it was written to."""
        problems = export_problems(report)
        if report.stopped == "removed":
            self._end_export("USB export stopped: the drive was unplugged.")
            dialogs.show_warning(self.root, "Export Stopped", problems)  # there is nothing left to eject
            return
        if report.stopped == "full":
            self._end_export("USB export stopped: the drive is full. Eject the drive before unplugging it.")
        else:
            self._end_export("USB export finished. Eject the drive before unplugging it.")
        clean_pl = sanitize_filename(playlist or self.active_playlist_name or "Playlist")
        if problems:
            msg = (
                f"Exported {report.exported} of {report.total} song(s) to the USB flash drive (with "
                f"'00_{clean_pl}.m3u' playlist).\n\n{problems}"
            )
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
            self._eject_drive(drive_label, drive_root)

    def _cd_export_success(self, cd_folder: str, report: ExportReport) -> None:
        """Report the prepared CD tracks and hand over to Windows Media Player for burning."""
        self._end_export("CD files are ready on your Desktop in 'My_CD_Burn_Folder'.")
        problems = export_problems(report, drive="This computer's disk")
        warn_text = f"\n\nNote: {problems}" if problems else ""

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
        msg = f"Prepared {report.exported} of {report.total} CD track(s) in 'My_CD_Burn_Folder' on your Desktop.{warn_text}\n\n{steps}"
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
                    logger.error("Could not open the CD burn tools: %s", e)
        else:
            dialogs.show_info(
                self.root,
                "Ready to Burn",
                msg + "\n\nWindows Media Player is not installed on this PC. You can add it in "
                "Settings > Apps > Optional features > 'Windows Media Player Legacy'.",
                button="Open the CD folder",
                icon="💿",
            )
            with contextlib.suppress(OSError):  # the folder path is in the message above
                os.startfile(cd_folder)

    def _export_error(self, err: str) -> None:
        self._end_export("Export failed.")
        show_friendly_error(self.root, err, "export")

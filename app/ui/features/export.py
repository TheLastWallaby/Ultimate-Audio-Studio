"""Step 3 export: USB drive detection/eject and USB/CD playlist export."""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox

from app.config import log_error, sanitize_filename
from app.platform_utils import find_windows_media_player, list_removable_drives
from app.ui.error_dialog import show_friendly_error
from app.ui.features.base import AppBase


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
            messagebox.showinfo("No Drive", "Please select a connected USB drive to safely eject.")
            return
        drive_path = self._usb_map[choice]
        ok, msg = self.export_ctrl.eject_usb_drive(drive_path)
        self.refresh_usb_drives()
        if ok:
            messagebox.showinfo(
                "Safe to Remove Hardware", f"The USB drive ({choice}) was safely ejected.\n\nYou may now unplug it."
            )
            self.set_status(f"USB drive {choice} safely ejected.")
        else:
            messagebox.showwarning(
                "Ejection Failed",
                f"Could not eject drive ({choice}):\n{msg}\n\nPlease ensure no open files or Explorer windows are accessing it.",
            )

    def export_playlist(self) -> None:
        if not self.playlist_files:
            messagebox.showwarning("Empty Playlist", "Add some songs to this playlist before exporting.")
            return

        dest_type = self.export_var.get()
        normalize = self.even_volume.get()

        if dest_type == "USB":
            choice = self.usb_choice.get()
            if not choice or choice not in self._usb_map:
                messagebox.showwarning("No USB Drive", "Please insert a USB flash drive and select it from the list.")
                return

            dest_folder = self._usb_map[choice]
            if not os.path.exists(dest_folder):
                messagebox.showerror("Drive Missing", "Selected USB drive is no longer accessible. Please re-insert.")
                return

            fs_type = self._usb_fs_map.get(choice, "")
            if self.export_ctrl.is_ntfs(fs_type):
                warn = messagebox.askyesno(
                    "NTFS Filesystem Warning",
                    f"The selected drive ({choice}) is formatted as NTFS.\n\n"
                    "Many car stereos and older stereos ONLY recognize FAT32 or exFAT drives, and will show 'No Device' or 'Read Error'.\n\n"
                    "Do you want to continue exporting anyway?",
                )
                if not warn:
                    return

            # Pre-flight disk space validation
            est_bytes = self.export_ctrl.estimate_playlist_bytes(self.playlist_files, self._cached_duration)
            has_space, free_bytes = self.export_ctrl.check_usb_space(dest_folder, est_bytes)
            if not has_space:
                free_mb = free_bytes / (1024 * 1024)
                needed_mb = est_bytes / (1024 * 1024)
                messagebox.showwarning(
                    "Insufficient USB Disk Space",
                    f"The selected USB drive only has {free_mb:.0f} MB of free space.\n\n"
                    f"This playlist requires approximately {needed_mb:.0f} MB.\n\n"
                    "Please delete files from your USB flash drive or use a drive with more free space.",
                )
                return

            self._exporting = True
            self.btn_export.config(text="Exporting...", state=tk.DISABLED)
            self.prog_export.pack(fill=tk.X, pady=(4, 2))
            self.prog_export["value"] = 0
            self.set_busy(True, f"Exporting {len(self.playlist_files)} songs to USB flash drive...")

            self.export_ctrl.start_usb_export(
                dest_folder,
                self.active_playlist_name,
                self.playlist_files,
                normalize,
                on_progress=lambda pct: self._safe_after(0, lambda: self._update_export_progress(pct)),
                on_status=lambda text: self._safe_after(0, lambda: self.set_status(text)),
                on_success=lambda sc, tot, sk: self._safe_after(0, self._usb_export_success, sc, tot, sk),
                on_error=lambda err: self._safe_after(0, self._export_error, err),
                is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
                duration_fn=self._cached_duration,
            )
        else:
            # CD Burn Folder Export
            cd_folder = self.export_ctrl.get_cd_burn_folder()
            existing_cd_files = self.export_ctrl.get_cd_existing_files(cd_folder)
            if existing_cd_files:
                clear_old = messagebox.askyesnocancel(
                    "Clear Previous CD Files?",
                    f"The CD burn folder ('My_CD_Burn_Folder') already contains {len(existing_cd_files)} file(s) from a previous export.\n\n"
                    "Click 'Yes' to remove old files and start fresh.\n"
                    "Click 'No' to keep old files and add these songs.\n"
                    "Click 'Cancel' to stop.",
                )
                if clear_old is None:
                    return
                elif clear_old:
                    self.export_ctrl.clear_cd_folder(cd_folder)

            # 80-minute CD capacity validation
            total_sec = self.export_ctrl.get_playlist_duration(self.playlist_files, self._cached_duration)
            if total_sec > 80 * 60:
                mins = int(total_sec // 60)
                ok = messagebox.askyesno(
                    "Playlist Exceeds 80 Minutes",
                    f"Standard audio CDs hold 80 minutes of music.\n\n"
                    f"Your playlist is currently {mins} minutes long, so some songs might not fit on one blank CD.\n\n"
                    "Do you still want to prepare all tracks?",
                )
                if not ok:
                    return

            self._exporting = True
            self.btn_export.config(text="Preparing CD...", state=tk.DISABLED)
            self.prog_export.pack(fill=tk.X, pady=(4, 2))
            self.prog_export["value"] = 0
            self.set_busy(True, f"Preparing {len(self.playlist_files)} CD audio tracks...")

            self.export_ctrl.start_cd_export(
                cd_folder,
                self.playlist_files,
                normalize,
                on_progress=lambda pct: self._safe_after(0, lambda: self._update_export_progress(pct)),
                on_status=lambda text: self._safe_after(0, lambda: self.set_status(text)),
                on_success=lambda fld, sc, tot, sk: self._safe_after(0, self._cd_export_success, fld, sc, tot, sk),
                on_error=lambda err: self._safe_after(0, self._export_error, err),
                is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
            )

    def _update_export_progress(self, pct: float) -> None:
        if hasattr(self, "prog_export"):
            self.prog_export["value"] = pct

    def _usb_export_success(self, success_count: int, total: int, skipped: list[str]) -> None:
        self._exporting = False
        self.btn_export.config(text="⚡ Export Playlist Now", state=tk.NORMAL)
        self.prog_export.pack_forget()
        self.set_busy(False, "USB export finished. Eject the drive before unplugging it.")
        pl_name = self.active_playlist_name or "Playlist"
        clean_pl = sanitize_filename(pl_name)
        if skipped:
            msg = f"Exported {success_count} of {total} song(s) to the USB flash drive (with '00_{clean_pl}.m3u' playlist).\n\n{len(skipped)} song(s) could not be processed:\n"
            msg += "\n".join(f"• {s}" for s in skipped[:6])
            if len(skipped) > 6:
                msg += f"\n... and {len(skipped) - 6} more."
            messagebox.showwarning("Export Completed with Warnings", msg)
            question = "Would you like to safely eject the USB flash drive now so you can unplug it?"
        else:
            question = (
                f"Your playlist was copied to the USB flash drive, with a '00_{clean_pl}.m3u' playlist file "
                "for car stereos and media players.\n\n"
                "Before unplugging it, the drive should be ejected.\n\nEject the USB flash drive now?"
            )
        if messagebox.askyesno("Export Successful" if not skipped else "Eject USB Drive", question):
            self.eject_selected_usb()

    def _cd_export_success(self, cd_folder: str, success_count: int, total: int, skipped: list[str]) -> None:
        self._exporting = False
        self.btn_export.config(text="⚡ Export Playlist Now", state=tk.NORMAL)
        self.prog_export.pack_forget()
        self.set_busy(False, "CD files are ready on your Desktop in 'My_CD_Burn_Folder'.")
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
            if messagebox.askyesno("Ready to Burn", msg + "\n\nOpen Windows Media Player and the CD folder now?"):
                try:
                    os.startfile(cd_folder)
                    os.startfile(wmp)
                except OSError as e:
                    log_error(f"open CD burn tools: {e}")
        else:
            messagebox.showinfo(
                "Ready to Burn",
                msg + "\n\nWindows Media Player is not installed on this PC. You can add it in "
                "Settings > Apps > Optional features > 'Windows Media Player Legacy'.",
            )
            try:
                os.startfile(cd_folder)
            except OSError:
                pass

    def _export_error(self, err: str) -> None:
        self._exporting = False
        self.btn_export.config(text="⚡ Export Playlist Now", state=tk.NORMAL)
        if hasattr(self, "prog_export"):
            self.prog_export.pack_forget()
        self.set_busy(False, "Export failed.")
        show_friendly_error(self.root, err, "export")

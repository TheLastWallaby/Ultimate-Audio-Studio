"""Update checks and the update badge/dialog in the status bar."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox

from app.config import APP_VERSION
from app.models import ReleaseInfo
from app.ui.features.base import AppBase


class UpdatesMixin(AppBase):
    """Update checks and the update badge/dialog in the status bar."""

    def _check_for_updates_on_launch(self) -> None:
        """Silently check for updates in a background thread after launch."""
        if getattr(self, "_is_shutting_down", False):
            return
        self.update_ctrl.check_on_launch(
            on_update_found=lambda rel: self._safe_after(0, self._handle_update_found, rel)
        )

    def _handle_update_found(self, release_info: ReleaseInfo) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        self._available_update = release_info
        tag = release_info.get("tag_name", "New")
        if hasattr(self, "btn_update_badge") and self.btn_update_badge.winfo_exists():
            self.btn_update_badge.config(text=f"⭐ Update to {tag}")
            self.btn_update_badge.pack(side=tk.RIGHT, padx=(8, 0))
        # If running as a frozen executable, offer the update; installing always needs a click
        # and waits for downloads/exports to finish (see busy_reason / prepare_for_restart).
        if getattr(sys, "frozen", False):
            self._open_update_dialog(auto_start=False)

    def check_for_updates_manual(self) -> None:
        """User-initiated update check with explicit UI feedback."""
        if getattr(self, "_is_checking_updates_manual", False) or getattr(self, "_is_shutting_down", False):
            return
        self._is_checking_updates_manual = True
        self.set_status("Checking for application updates...", icon="🔄")
        if hasattr(self, "btn_version_check") and self.btn_version_check.winfo_exists():
            self.btn_version_check.config(text=f"v{APP_VERSION} (Checking...)", state=tk.DISABLED)

        self.update_ctrl.check_manual(
            on_result=lambda has_update, release_info, err: self._safe_after(
                0, self._handle_manual_update_result, has_update, release_info, err
            )
        )

    def _handle_manual_update_result(self, has_update: bool, release_info: ReleaseInfo | None, err: str | None) -> None:
        self._is_checking_updates_manual = False
        if hasattr(self, "btn_version_check") and self.btn_version_check.winfo_exists():
            self.btn_version_check.config(text=f"v{APP_VERSION} • Check for Updates", state=tk.NORMAL)

        if getattr(self, "_is_shutting_down", False):
            return

        if has_update and release_info:
            tag = release_info.get("tag_name", "New")
            self.set_status(f"Update available: {tag}", icon="⭐")
            self._available_update = release_info
            if hasattr(self, "btn_update_badge") and self.btn_update_badge.winfo_exists():
                self.btn_update_badge.config(text=f"⭐ Update to {tag}")
                self.btn_update_badge.pack(side=tk.RIGHT, padx=(8, 0))
            self._open_update_dialog(auto_start=False)
        elif err:
            self.set_status("Update check failed.", icon="⚠️")
            messagebox.showwarning("Update Check", f"{err}")
        else:
            self.set_status(f"Ultimate Audio Studio is up to date (v{APP_VERSION}).", icon="✅")
            messagebox.showinfo(
                "Update Check", f"You are running the latest version of Ultimate Audio Studio (v{APP_VERSION})."
            )

    def _open_update_dialog(self, auto_start: bool = False) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        self.update_ctrl.open_dialog(self.root, release_info=self._available_update, auto_start=auto_start)

"""Update checks and the update badge/dialog in the status bar."""

from __future__ import annotations

import tkinter as tk

from app.config import APP_VERSION
from app.models import ReleaseInfo
from app.services.updater import PendingUpdate
from app.ui import dialogs
from app.ui.features.base import AppBase


class UpdatesMixin(AppBase):
    """Update checks and the update badge/dialog in the status bar.

    A new release found at start-up is downloaded quietly and installed the next time the app opens;
    the only visible sign is the status-bar badge, so nothing pops up while someone is starting work.
    """

    def _check_for_updates_on_launch(self) -> None:
        """Silently check for updates in a background thread after launch."""
        if getattr(self, "_is_shutting_down", False):
            return
        self.update_ctrl.check_on_launch(
            on_update_found=lambda rel: self._safe_after(0, self._handle_update_found, rel)
        )

    def _show_update_badge(self, text: str) -> None:
        if hasattr(self, "btn_update_badge") and self.btn_update_badge.winfo_exists():
            self.btn_update_badge.config(text=text)
            self.btn_update_badge.pack(side=tk.RIGHT, padx=(8, 0))

    def _handle_update_found(self, release_info: ReleaseInfo) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        self._available_update = release_info
        self._show_update_badge(f"⭐ Update to {release_info.tag_name}")
        if self.update_ctrl.should_stage_automatically(release_info):
            self.update_ctrl.stage_in_background(
                release_info, on_done=lambda pending, _err: self._safe_after(0, self._on_update_staged, pending)
            )

    def _on_update_staged(self, pending: PendingUpdate | None) -> None:
        if getattr(self, "_is_shutting_down", False) or pending is None:
            return
        self._show_update_badge(f"⭐ {pending.tag} ready: installs next time you open the app")
        self.set_status(
            f"A new version ({pending.tag}) has been downloaded. It will be installed the next time you open the app.",
            icon="⭐",
        )

    def _remember_pending_update(self, pending: PendingUpdate) -> None:
        self.update_ctrl.pending_update = pending
        self._on_update_staged(pending)

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
            self.set_status(f"Update available: {release_info.tag_name}", icon="⭐")
            self._available_update = release_info
            self._show_update_badge(f"⭐ Update to {release_info.tag_name}")
            self._open_update_dialog()
        elif err:
            self.set_status("Update check failed.", icon="⚠️")
            dialogs.show_warning(
                self.root,
                "Could Not Check for Updates",
                f"{err}\n\nCheck your internet connection and try again later.",
            )
        else:
            self.set_status(f"Ultimate Audio Studio is up to date (v{APP_VERSION}).", icon="✅")
            dialogs.show_info(
                self.root,
                "You're Up to Date",
                f"You are running the latest version of Ultimate Audio Studio (v{APP_VERSION}).",
            )

    def _open_update_dialog(self) -> None:
        if getattr(self, "_is_shutting_down", False):
            return
        self.update_ctrl.open_dialog(
            self.root, release_info=self._available_update, on_pending=self._remember_pending_update
        )

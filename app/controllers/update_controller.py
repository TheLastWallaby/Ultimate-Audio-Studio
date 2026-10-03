"""Update controller managing launch/manual update checks, background staging, and the update dialog."""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from collections.abc import Callable

from app.config import APP_VERSION, log_error
from app.core.task_manager import network_task_mgr as task_mgr
from app.models import ReleaseInfo
from app.services.updater import PendingUpdate, check_latest_release, failed_update_tag, stage_update

ManualResultCallback = Callable[[bool, ReleaseInfo | None, str | None], None]
StagedCallback = Callable[[PendingUpdate | None, str], None]


class UpdateController:
    """Coordinates automatic and user-initiated GitHub update checks.

    In the packaged app a newer release is downloaded quietly in the background ("staged") and
    installed the next time the app starts, so an update never interrupts the user or pops up a
    window at start-up.
    """

    def __init__(self, app: object) -> None:
        self.app = app
        self.available_update: ReleaseInfo | None = None
        self.pending_update: PendingUpdate | None = None
        self.is_checking_manual = False
        self._is_staging = False
        self._stage_cancel = threading.Event()

    @staticmethod
    def can_install() -> bool:
        """True in the packaged .exe; source checkouts are updated with git instead."""
        return bool(getattr(sys, "frozen", False))

    def check_on_launch(self, on_update_found: Callable[[ReleaseInfo], None] | None) -> None:
        """Silently check for updates in a background thread after launch."""

        def _worker() -> None:
            try:
                has_update, release_info, err = check_latest_release(current_ver=APP_VERSION, return_error=True)
                if has_update and release_info:
                    self.available_update = release_info
                    if on_update_found:
                        on_update_found(release_info)
                elif err:
                    log_error(f"Launch update check: {err}")
            except Exception as e:
                log_error(f"Launch update check exception: {e}")

        task_mgr.submit_task(_worker)

    def check_manual(self, on_result: ManualResultCallback | None) -> None:
        """Initiate user-requested update check."""
        if self.is_checking_manual:
            return
        self.is_checking_manual = True

        def _worker() -> None:
            has_update, release_info, err = check_latest_release(current_ver=APP_VERSION, return_error=True)
            self.is_checking_manual = False
            if has_update and release_info:
                self.available_update = release_info
            if on_result:
                on_result(has_update, release_info, err)

        task_mgr.submit_task(_worker)

    def should_stage_automatically(self, release_info: ReleaseInfo) -> bool:
        """Download in the background unless this release already failed to start on this PC."""
        return self.can_install() and release_info.tag_name != failed_update_tag()

    def stage_in_background(self, release_info: ReleaseInfo, on_done: StagedCallback) -> None:
        """Download and verify the release on a worker thread; ``on_done(pending, error)`` runs there too."""
        if self._is_staging:
            return
        self._is_staging = True

        def _worker() -> None:
            try:
                pending, err = stage_update(release_info, cancel_event=self._stage_cancel)
            except Exception as e:
                pending, err = None, str(e)
            self._is_staging = False
            if pending is not None:
                self.pending_update = pending
            elif err:
                log_error(f"Background update download failed: {err}")
            on_done(pending, err)

        task_mgr.submit_task(_worker)

    def cancel_staging(self) -> None:
        """Stop a background download (e.g. when the app closes)."""
        self._stage_cancel.set()

    def open_dialog(
        self,
        parent_win: tk.Misc,
        release_info: ReleaseInfo | None = None,
        on_pending: Callable[[PendingUpdate], None] | None = None,
    ) -> None:
        """Open the modal UpdateDialog."""
        info = release_info or self.available_update
        if not info:
            return
        from app.ui.update_dialog import UpdateDialog

        pending = self.pending_update if self.pending_update and self.pending_update.tag == info.tag_name else None
        try:
            UpdateDialog(
                parent_win,
                info,
                pending=pending,
                busy_reason_fn=getattr(self.app, "busy_reason", None),
                prepare_restart_fn=getattr(self.app, "prepare_for_restart", None),
                on_pending=on_pending,
            )
        except Exception as e:
            log_error(f"Failed to open UpdateDialog: {e}")

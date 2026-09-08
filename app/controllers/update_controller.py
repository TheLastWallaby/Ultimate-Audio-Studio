"""Update controller managing launch/manual update checks and update dialogs."""

from app.config import APP_VERSION, log_error
from app.core.task_manager import task_mgr
from app.services.updater import check_latest_release
from app.ui.update_dialog import UpdateDialog


class UpdateController:
    """Coordinates automatic and user-initiated GitHub update checks."""

    def __init__(self, app):
        self.app = app
        self.available_update = None
        self.is_checking_manual = False

    def check_on_launch(self, on_update_found):
        """Silently check for updates in a background thread after launch."""
        def _worker():
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

    def check_manual(self, on_result):
        """Initiate user-requested update check."""
        if self.is_checking_manual:
            return
        self.is_checking_manual = True

        def _worker():
            has_update, release_info, err = check_latest_release(current_ver=APP_VERSION, return_error=True)
            self.is_checking_manual = False
            if has_update and release_info:
                self.available_update = release_info
            if on_result:
                on_result(has_update, release_info, err)

        task_mgr.submit_task(_worker)

    def open_dialog(self, parent_win, release_info=None, auto_start=False):
        """Open the modal UpdateDialog."""
        info = release_info or self.available_update
        if not info:
            return
        try:
            UpdateDialog(parent_win, info, auto_start=auto_start)
        except Exception as e:
            log_error(f"Failed to open UpdateDialog: {e}")


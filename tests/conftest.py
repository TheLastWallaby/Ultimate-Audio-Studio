"""Pytest configuration and global test environment bootstrap."""

import atexit
import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Ensure Windows Conda Tkinter reliably discovers Tcl/Tk runtime libraries
if sys.platform == "win32":
    try:
        import tkinter as _tk_test

        _r = _tk_test.Tk()
        _r.destroy()
    except Exception:
        _tcl_dir = os.path.normpath(os.path.join(sys.prefix, "Library", "lib", "tcl8.6"))
        _tk_dir = os.path.normpath(os.path.join(sys.prefix, "Library", "lib", "tk8.6"))
        if os.path.isdir(_tcl_dir):
            os.environ["TCL_LIBRARY"] = _tcl_dir
        if os.path.isdir(_tk_dir):
            os.environ["TK_LIBRARY"] = _tk_dir

# Point the app at a throwaway "Music" folder before anything imports ``app``: the tests open the
# real window, which reads and rewrites the playlists, settings, error log and Library found there.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="uas-tests-"))
_TEST_MUSIC = _TEST_HOME / "Music"
_TEST_MUSIC.mkdir()
os.environ.update(
    {
        "UAS_MUSIC_DIR": str(_TEST_MUSIC),
        "UAS_PLAYLISTS_PATH": str(_TEST_MUSIC / "audio_studio_playlists.json"),
        "UAS_SETTINGS_PATH": str(_TEST_MUSIC / "audio_studio_settings.json"),
        "UAS_ERROR_LOG_PATH": str(_TEST_MUSIC / "audio_studio_error.txt"),
        "UAS_YT_CACHE_DIR": str(_TEST_MUSIC / ".audio_studio_cache"),
    }
)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _working_task_manager() -> None:
    """Give every test a running worker pool.

    Closing a window shuts the shared pool down for the rest of the process; without this, background
    work in every later test would be dropped without an error and the test would prove nothing.
    """
    from app.core.task_manager import task_mgr

    task_mgr.restart()


@pytest.fixture
def studio(tmp_path: Path) -> Iterator[Any]:
    """The real main window, hidden, with a playlists file of its own; closed again after the test."""
    import tkinter as tk
    from unittest.mock import patch

    from app.main import UltimateAudioStudio

    with patch("app.ui.features.playlists.PLAYLISTS_PATH", str(tmp_path / "studio_playlists.json")):
        root = tk.Tk()
        root.withdraw()
        window = UltimateAudioStudio(root)
        try:
            yield window
        finally:
            window.on_close()


@pytest.fixture(autouse=True)
def _no_blocking_dialogs(request: pytest.FixtureRequest) -> Iterator[None]:
    """Answer every themed dialog with its cancel value unless a test patches it or opts out.

    The app's dialogs are modal (``wait_window``), so an unexpected one would hang the test run.
    Error dialogs are closed the same way. Tests of the dialogs themselves use
    ``@pytest.mark.real_dialogs``.
    """
    if request.node.get_closest_marker("real_dialogs"):
        yield
        return
    from unittest.mock import patch

    from app.ui import error_dialog

    def _cancel(*_args: object, cancel_value: str | None = None, **_kwargs: object) -> str | None:
        return cancel_value

    def _dismissed(*_args: object, **_kwargs: object) -> None:
        return None

    real_show_error = error_dialog.show_error
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("app.ui.dialogs.ask_choice", side_effect=_cancel))
        # ``from app.ui.error_dialog import show_error`` binds the function in the importing module,
        # so every module that holds it is patched, not only the one that defines it.
        for name, module in list(sys.modules.items()):
            if name.startswith("app.") and getattr(module, "show_error", None) is real_show_error:
                stack.enter_context(patch.object(module, "show_error", _dismissed))
        yield

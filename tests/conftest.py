"""Pytest configuration and global test environment bootstrap."""

import atexit
import contextlib
import functools
import os
import shutil
import sys
import tempfile
import time
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

# SDL's silent "dummy" audio driver, set before anything imports pygame. The tests never need to be
# heard, and on a PC without an audio device (the GitHub runners) pygame.mixer.init() waits 8 s
# before it gives up, once for every window a test opens. setdefault: a run can still choose a
# real driver through the environment.
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

TK_STARTUP_ATTEMPTS = 3


def _retry_tk_startup() -> None:
    """Make ``tk.Tk()`` try again when Tcl fails to start with "Can't find a usable init.tcl".

    On the Windows CI runners that error appears now and then while a window is being created,
    before any test code runs, and the same test passes on the next run. The suite opens well over
    a hundred windows, so one such hiccup would otherwise fail a whole CI run (and block a release).
    Any other start-up error, or one that keeps happening, is raised as usual.
    """
    import tkinter as tk

    real_init = tk.Tk.__init__

    @functools.wraps(real_init)
    def _init_with_retry(self: tk.Tk, *args: Any, **kwargs: Any) -> None:
        for attempt in range(1, TK_STARTUP_ATTEMPTS + 1):
            try:
                real_init(self, *args, **kwargs)
                return
            except tk.TclError as err:
                if "init.tcl" not in str(err) or attempt == TK_STARTUP_ATTEMPTS:
                    raise
                time.sleep(0.2 * attempt)

    tk.Tk.__init__ = _init_with_retry  # type: ignore[method-assign]


_retry_tk_startup()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _working_task_manager() -> None:
    """Give every test running worker pools (the shared one, and the one for internet work).

    Closing a window shuts the shared pools down for the rest of the process; without this, background
    work in every later test would be dropped without an error and the test would prove nothing.
    """
    from app.core.task_manager import network_task_mgr, task_mgr

    task_mgr.restart()
    network_task_mgr.restart()


@pytest.fixture(autouse=True)
def _fresh_settings_and_playlists() -> None:
    """Start every test from the default settings and an empty playlist, like a first start.

    A window saves its settings and playlists when it closes, and many tests point the Library at a
    temporary folder that is deleted afterwards. The app keeps a saved folder that is missing (it may
    be an unplugged drive), so without this every later window would start without its Library and
    schedule a "Music Folder Not Found" warning, and open playlists left by other tests.
    """
    from app.config import PLAYLISTS_PATH, SETTINGS_PATH, settings_mgr
    from app.controllers.playlist_controller import last_good_path

    # The spare copy goes too, or the app would "restore" the playlists from it and say so.
    for leftover in (Path(SETTINGS_PATH), Path(PLAYLISTS_PATH), last_good_path(Path(PLAYLISTS_PATH))):
        with contextlib.suppress(FileNotFoundError):
            leftover.unlink()
    settings_mgr.reload()
    settings_mgr.damaged_copy = None


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
        stack.enter_context(patch("app.ui.dialogs.ask_text", return_value=None))  # None is its cancel value
        # ``from app.ui.error_dialog import show_error`` binds the function in the importing module,
        # so every module that holds it is patched, not only the one that defines it.
        for name, module in list(sys.modules.items()):
            if name.startswith("app.") and getattr(module, "show_error", None) is real_show_error:
                stack.enter_context(patch.object(module, "show_error", _dismissed))
        yield

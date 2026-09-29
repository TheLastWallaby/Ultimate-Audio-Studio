"""Pytest configuration and global test environment bootstrap."""

import os
import sys
from collections.abc import Iterator

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


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_blocking_dialogs(request: pytest.FixtureRequest) -> Iterator[None]:
    """Answer every themed dialog with its cancel value unless a test patches it or opts out.

    The app's dialogs are modal (``wait_window``), so an unexpected one would hang the test run.
    Tests of the dialogs themselves use ``@pytest.mark.real_dialogs``.
    """
    if request.node.get_closest_marker("real_dialogs"):
        yield
        return
    from unittest.mock import patch

    def _cancel(*_args: object, cancel_value: str | None = None, **_kwargs: object) -> str | None:
        return cancel_value

    with patch("app.ui.dialogs.ask_choice", side_effect=_cancel):
        yield

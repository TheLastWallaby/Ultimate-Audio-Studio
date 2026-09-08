"""Pytest configuration and global test environment bootstrap."""

import os
import sys

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

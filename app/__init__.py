"""Ultimate Audio Studio Application Package."""

__version__ = "1.1.2"

import os
import sys

# Ensure Windows Conda Tkinter reliably discovers Tcl/Tk runtime libraries
if sys.platform == "win32":
    _tcl_dir = os.path.normpath(os.path.join(sys.prefix, "Library", "lib", "tcl8.6"))
    _tk_dir = os.path.normpath(os.path.join(sys.prefix, "Library", "lib", "tk8.6"))
    if os.path.isdir(_tcl_dir):
        os.environ.setdefault("TCL_LIBRARY", _tcl_dir)
    if os.path.isdir(_tk_dir):
        os.environ.setdefault("TK_LIBRARY", _tk_dir)

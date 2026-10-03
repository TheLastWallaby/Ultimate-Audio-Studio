"""Senior-friendly error dialog: plain-language explanation plus buttons to open or copy technical details."""

from __future__ import annotations

import contextlib
import logging
import os
import tkinter as tk

from app.config import ERROR_LOG_PATH
from app.core.errors import ErrorContext, friendly_error
from app.ui.components import create_button
from app.ui.dialogs import center_over, fit_to_screen, wrap_width
from app.ui.theme import (
    BG_CARD,
    COLOR_ACCENT,
    COLOR_ACCENT_HV,
    COLOR_BTN_NEUTRAL,
    COLOR_BTN_NEUTRAL_HV,
    COLOR_STOP,
    FONT_APP_TITLE,
    FONT_BODY,
    FONT_BTN_MAIN,
    FONT_BTN_SUB,
    TEXT_DARK,
)

logger = logging.getLogger(__name__)

__all__ = ["show_error", "show_friendly_error"]


def show_error(parent: tk.Misc, title: str, message: str, details: str = "") -> None:
    """Show a modal error dialog with 'Open Error Log' and (when details exist) 'Copy Details' buttons.

    A message too long for the screen scrolls, so the buttons are always in reach (``fit_to_screen``).
    """
    if details:
        logger.error("%s: %s", title, details)
    top = parent.winfo_toplevel()
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=BG_CARD)
    win.resizable(False, False)
    with contextlib.suppress(tk.TclError):
        win.transient(top)

    body = tk.Frame(win, bg=BG_CARD, padx=20, pady=16)
    body.pack(fill=tk.BOTH, expand=True)
    tk.Label(body, text=f"⚠️  {title}", font=FONT_APP_TITLE, fg=COLOR_STOP, bg=BG_CARD, anchor="w").pack(
        fill=tk.X, pady=(0, 10)
    )
    message_label = tk.Label(
        body, text=message, font=FONT_BODY, fg=TEXT_DARK, bg=BG_CARD, justify="left", wraplength=wrap_width(win)
    )
    message_label.pack(anchor="w", pady=(0, 14))

    btns = tk.Frame(body, bg=BG_CARD)
    btns.pack(fill=tk.X)

    def _close() -> None:
        with contextlib.suppress(tk.TclError):
            win.grab_release()
        win.destroy()

    def _open_log() -> None:
        try:
            os.startfile(ERROR_LOG_PATH)  # nosec B606
        except OSError:
            show_status.config(text="The error log is empty or could not be opened.")

    def _copy() -> None:
        win.clipboard_clear()
        win.clipboard_append(f"{title}\n{message}\n\nDetails: {details}")
        show_status.config(text="Details copied. You can paste them into an email for help.")

    ok_btn = create_button(
        btns, "OK", _close, bg=COLOR_ACCENT, fg="#ffffff", hover_bg=COLOR_ACCENT_HV, font=FONT_BTN_MAIN, padx=24, pady=6
    )
    ok_btn.pack(side=tk.RIGHT)
    create_button(
        btns,
        "Open Error Log",
        _open_log,
        bg=COLOR_BTN_NEUTRAL,
        hover_bg=COLOR_BTN_NEUTRAL_HV,
        font=FONT_BTN_SUB,
        pady=6,
    ).pack(side=tk.LEFT)
    if details:
        create_button(
            btns, "Copy Details", _copy, bg=COLOR_BTN_NEUTRAL, hover_bg=COLOR_BTN_NEUTRAL_HV, font=FONT_BTN_SUB, pady=6
        ).pack(side=tk.LEFT, padx=(8, 0))
    show_status = tk.Label(body, text="", font=FONT_BODY, fg=TEXT_DARK, bg=BG_CARD, anchor="w")
    show_status.pack(fill=tk.X, pady=(8, 0))

    win.protocol("WM_DELETE_WINDOW", _close)
    win.bind("<Escape>", lambda _e: _close())
    fit_to_screen(win, message_label)
    center_over(win, top)
    ok_btn.focus_set()
    with contextlib.suppress(tk.TclError):
        win.grab_set()
    win.wait_window()


def show_friendly_error(parent: tk.Misc, raw_error: object, context: ErrorContext = "generic") -> None:
    """Translate a raw error into plain language and show it with the technical details attached."""
    fe = friendly_error(raw_error, context)
    show_error(parent, fe.title, fe.message, details=str(raw_error or ""))

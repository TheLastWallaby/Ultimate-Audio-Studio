"""Senior-friendly replacements for ``tkinter.messagebox``.

Native Windows message boxes ignore the app's Text Size setting (they always use the ~9pt system
font) and can only offer Yes/No/Cancel, which forces people to map a question onto those words.
These dialogs use the app's named fonts, so they grow with Text Size, and every button says what
it does ("Download all 23 songs", "Just this song").

Call sites use ``dialogs.ask_yes_no(...)`` (module attribute access) so tests can patch
``app.ui.dialogs.<function>``.
"""

from __future__ import annotations

import contextlib
import functools
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.ui.components import create_button
from app.ui.theme import (
    BG_CARD,
    COLOR_ACCENT,
    COLOR_ACCENT_HV,
    COLOR_BTN_NEUTRAL,
    COLOR_BTN_NEUTRAL_HV,
    COLOR_STOP,
    COLOR_STOP_HV,
    FONT_APP_TITLE,
    FONT_BODY,
    FONT_BTN_MAIN,
    TEXT_DARK,
)

__all__ = ["DialogButton", "ask_choice", "ask_yes_no", "show_info", "show_warning"]

ButtonKind = Literal["primary", "neutral", "danger"]

ICON_INFO = "ℹ️"
ICON_WARNING = "⚠️"
ICON_QUESTION = "❓"

_BUTTON_COLORS: dict[ButtonKind, tuple[str, str, str]] = {
    "primary": (COLOR_ACCENT, "#ffffff", COLOR_ACCENT_HV),
    "danger": (COLOR_STOP, "#ffffff", COLOR_STOP_HV),
    "neutral": (COLOR_BTN_NEUTRAL, TEXT_DARK, COLOR_BTN_NEUTRAL_HV),
}
# Wrap the message after roughly this many average characters (scales with the Text Size fonts).
_WRAP_CHARS = 52


@dataclass(slots=True, frozen=True)
class DialogButton:
    """One choice in a dialog: ``label`` is shown, ``value`` is returned when it is clicked."""

    label: str
    value: str
    kind: ButtonKind = "neutral"


def ask_choice(
    parent: tk.Misc,
    title: str,
    message: str,
    buttons: Sequence[DialogButton],
    *,
    icon: str = ICON_QUESTION,
    default: str | None = None,
    cancel_value: str | None = None,
) -> str | None:
    """Show a modal dialog and return the ``value`` of the clicked button.

    Closing the window or pressing Escape returns ``cancel_value``; Enter clicks ``default``
    (the first button when not given). Two buttons sit side by side; three or more are stacked
    full-width so every label stays readable at large text sizes.
    """
    top = parent.winfo_toplevel()
    win = tk.Toplevel(top)
    win.title(title)
    win.configure(bg=BG_CARD)
    win.resizable(False, False)
    # A dialog made transient to a hidden window (e.g. at start-up) would be hidden too.
    if top.winfo_viewable():
        with contextlib.suppress(tk.TclError):
            win.transient(top)

    result: list[str | None] = [cancel_value]
    done = [False]

    def _finish(value: str | None) -> None:
        # Enter on a focused button also reaches the window's <Return> binding: the first call wins.
        if done[0]:
            return
        done[0] = True
        result[0] = value
        with contextlib.suppress(tk.TclError):
            win.grab_release()
        win.destroy()

    body = tk.Frame(win, bg=BG_CARD, padx=22, pady=18)
    body.pack(fill=tk.BOTH, expand=True)
    tk.Label(body, text=f"{icon}  {title}", font=FONT_APP_TITLE, fg=TEXT_DARK, bg=BG_CARD, anchor="w").pack(
        fill=tk.X, pady=(0, 10)
    )
    wrap = tkfont.nametofont(FONT_BODY, root=win).measure("0") * _WRAP_CHARS
    tk.Label(body, text=message, font=FONT_BODY, fg=TEXT_DARK, bg=BG_CARD, justify="left", wraplength=wrap).pack(
        anchor="w", pady=(0, 16)
    )

    row = tk.Frame(body, bg=BG_CARD)
    row.pack(fill=tk.X)
    stacked = len(buttons) > 2
    default_value = default if default is not None else (buttons[0].value if buttons else cancel_value)
    focus_button: tk.Button | None = None
    # Side by side, the first button is packed rightmost so the main action sits bottom-right.
    for spec in buttons:
        bg, fg, hover = _BUTTON_COLORS[spec.kind]
        btn = create_button(
            row,
            spec.label,
            functools.partial(_finish, spec.value),
            bg=bg,
            fg=fg,
            hover_bg=hover,
            font=FONT_BTN_MAIN,
            padx=18,
            pady=8,
        )
        if stacked:
            btn.pack(fill=tk.X, pady=(0, 6))
        else:
            btn.pack(side=tk.RIGHT, padx=(8, 0))
        if spec.value == default_value:
            focus_button = btn

    win.protocol("WM_DELETE_WINDOW", lambda: _finish(cancel_value))
    win.bind("<Escape>", lambda _e: _finish(cancel_value))
    win.bind("<Return>", lambda _e: _finish(default_value))

    _center_over(win, top)
    if focus_button is not None:
        focus_button.focus_set()
    with contextlib.suppress(tk.TclError):
        win.grab_set()
    win.lift()
    win.wait_window()
    return result[0]


def ask_yes_no(
    parent: tk.Misc,
    title: str,
    message: str,
    *,
    yes: str,
    no: str,
    danger: bool = False,
    default_yes: bool = True,
    icon: str = ICON_QUESTION,
) -> bool:
    """Two-button question with action labels; True when ``yes`` is clicked (closing counts as ``no``)."""
    answer = ask_choice(
        parent,
        title,
        message,
        [DialogButton(yes, "yes", "danger" if danger else "primary"), DialogButton(no, "no")],
        icon=icon,
        default="yes" if default_yes else "no",
        cancel_value="no",
    )
    return answer == "yes"


def show_info(parent: tk.Misc, title: str, message: str, *, button: str = "OK", icon: str = ICON_INFO) -> None:
    """Information message with a single button."""
    ask_choice(parent, title, message, [DialogButton(button, "ok", "primary")], icon=icon, cancel_value="ok")


def show_warning(parent: tk.Misc, title: str, message: str, *, button: str = "OK") -> None:
    """Warning message with a single button."""
    show_info(parent, title, message, button=button, icon=ICON_WARNING)


def _center_over(win: tk.Toplevel, top: tk.Misc) -> None:
    """Place the dialog over the app window (a third of the way down), kept fully on screen."""
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    if top.winfo_viewable():
        x = top.winfo_rootx() + max(0, (top.winfo_width() - w) // 2)
        y = top.winfo_rooty() + max(0, (top.winfo_height() - h) // 3)
    else:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 3
    x = max(0, min(x, win.winfo_screenwidth() - w))
    y = max(0, min(y, win.winfo_screenheight() - h - 40))
    win.geometry(f"+{x}+{y}")

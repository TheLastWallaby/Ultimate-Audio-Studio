"""Senior-friendly replacements for ``tkinter.messagebox``.

Native Windows message boxes ignore the app's Text Size setting (they always use the ~9pt system
font) and can only offer Yes/No/Cancel, which forces people to map a question onto those words.
These dialogs use the app's named fonts, so they grow with Text Size, and every button says what
it does ("Download all 23 songs", "Just this song"). ``ask_text`` does the same for the small
"type a name" prompts, which ``tkinter.simpledialog`` shows in the system font with OK/Cancel.

Call sites use ``dialogs.ask_yes_no(...)`` (module attribute access) so tests can patch
``app.ui.dialogs.<function>``.
"""

from __future__ import annotations

import contextlib
import functools
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.ui.components import create_button
from app.ui.theme import (
    BG_CARD,
    BG_INPUT,
    BORDER_FOCUS,
    BORDER_MAIN,
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

__all__ = [
    "TEXT_OK",
    "DialogButton",
    "TextAnswer",
    "ask_choice",
    "ask_text",
    "ask_yes_no",
    "center_over",
    "fit_to_screen",
    "show_info",
    "show_warning",
    "wrap_width",
]

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
# Height of the screen a dialog leaves free for its own title bar and the taskbar (at 100% scaling).
_SCREEN_MARGIN_PX = 110
# A message that has to scroll still shows at least this many lines.
_MIN_MESSAGE_LINES = 4


@dataclass(slots=True, frozen=True)
class DialogButton:
    """One choice in a dialog: ``label`` is shown, ``value`` is returned when it is clicked."""

    label: str
    value: str
    kind: ButtonKind = "neutral"


# ``TextAnswer.value`` when the main button of ``ask_text`` was clicked.
TEXT_OK = "ok"


@dataclass(slots=True, frozen=True)
class TextAnswer:
    """What ``ask_text`` returned: the typed ``text`` (trimmed) and the ``value`` of the clicked button."""

    text: str
    value: str = TEXT_OK


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
    win, top, body, message_label = _open_dialog(parent, title, message, icon)
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

    default_value = default if default is not None else (buttons[0].value if buttons else cancel_value)
    placed = _place_buttons(body, [(spec, functools.partial(_finish, spec.value)) for spec in buttons])
    focus_button = next((btn for spec, btn in zip(buttons, placed, strict=True) if spec.value == default_value), None)

    win.protocol("WM_DELETE_WINDOW", lambda: _finish(cancel_value))
    win.bind("<Escape>", lambda _e: _finish(cancel_value))
    win.bind("<Return>", lambda _e: _finish(default_value))

    _run_modal(win, top, focus_button, message_label)
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


def ask_text(
    parent: tk.Misc,
    title: str,
    message: str,
    *,
    ok: str,
    initial: str = "",
    cancel: str = "Cancel",
    extra: Sequence[DialogButton] = (),
    icon: str = ICON_QUESTION,
) -> TextAnswer | None:
    """Ask for a short text such as a name or a time; None when the dialog was cancelled.

    ``ok`` is the main button and needs some text: clicked with an empty box, it asks for the text
    and the dialog stays open. ``extra`` buttons are other ways to answer that do not use the text
    (for example "Replace the original song"); the answer's ``value`` says which button was clicked.
    Enter clicks ``ok``; Escape or closing the window cancels.
    """
    win, top, body, message_label = _open_dialog(parent, title, message, icon)
    result: list[TextAnswer | None] = [None]
    done = [False]

    entry = tk.Entry(
        body,
        font=FONT_BODY,
        bg=BG_INPUT,
        fg=TEXT_DARK,
        insertbackground=TEXT_DARK,
        relief=tk.FLAT,
        highlightthickness=2,
        highlightbackground=BORDER_MAIN,
        highlightcolor=BORDER_FOCUS,
    )
    entry.insert(0, initial)
    entry.select_range(0, tk.END)  # typing replaces the suggestion, so nothing has to be deleted first
    entry.pack(fill=tk.X, ipady=4)
    hint = tk.Label(body, text="", font=FONT_BODY, fg=COLOR_STOP, bg=BG_CARD, anchor="w")
    hint.pack(fill=tk.X, pady=(2, 10))

    def _finish(answer: TextAnswer | None) -> None:
        # Enter on a focused button also reaches the window's <Return> binding: the first call wins.
        if done[0]:
            return
        done[0] = True
        result[0] = answer
        with contextlib.suppress(tk.TclError):
            win.grab_release()
        win.destroy()

    def _submit(value: str) -> None:
        if done[0]:
            return
        text = entry.get().strip()
        if value == TEXT_OK and not text:
            hint.config(text="Please type something in the box first.")
            entry.focus_set()
            return
        _finish(TextAnswer(text, value))

    specs = [DialogButton(ok, TEXT_OK, "primary"), *extra]
    actions = [(spec, functools.partial(_submit, spec.value)) for spec in specs]
    actions.append((DialogButton(cancel, ""), functools.partial(_finish, None)))
    _place_buttons(body, actions)

    win.protocol("WM_DELETE_WINDOW", lambda: _finish(None))
    win.bind("<Escape>", lambda _e: _finish(None))
    win.bind("<Return>", lambda _e: _submit(TEXT_OK))

    _run_modal(win, top, entry, message_label)
    return result[0]


def wrap_width(win: tk.Misc) -> int:
    """Pixels after which a dialog's message wraps: it grows with Text Size and display scaling."""
    return tkfont.nametofont(FONT_BODY, root=win).measure("0") * _WRAP_CHARS


def _open_dialog(
    parent: tk.Misc, title: str, message: str, icon: str
) -> tuple[tk.Toplevel, tk.Misc, tk.Frame, tk.Label]:
    """Create a dialog window with its heading and message.

    Returns (window, app window, content frame, message label).
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

    body = tk.Frame(win, bg=BG_CARD, padx=22, pady=18)
    body.pack(fill=tk.BOTH, expand=True)
    tk.Label(body, text=f"{icon}  {title}", font=FONT_APP_TITLE, fg=TEXT_DARK, bg=BG_CARD, anchor="w").pack(
        fill=tk.X, pady=(0, 10)
    )
    message_label = tk.Label(
        body, text=message, font=FONT_BODY, fg=TEXT_DARK, bg=BG_CARD, justify="left", wraplength=wrap_width(win)
    )
    message_label.pack(anchor="w", pady=(0, 16))
    return win, top, body, message_label


def _place_buttons(body: tk.Frame, actions: Sequence[tuple[DialogButton, Callable[[], None]]]) -> list[tk.Button]:
    """Add the buttons under the dialog's content, in the order given; returns them in that order.

    Two buttons sit side by side, the first one rightmost so the main action is bottom-right. Three
    or more are stacked full-width so every label stays readable at large text sizes.
    """
    row = tk.Frame(body, bg=BG_CARD)
    row.pack(fill=tk.X)
    stacked = len(actions) > 2
    placed = []
    for spec, command in actions:
        bg, fg, hover = _BUTTON_COLORS[spec.kind]
        btn = create_button(row, spec.label, command, bg=bg, fg=fg, hover_bg=hover, font=FONT_BTN_MAIN, padx=18, pady=8)
        if stacked:
            btn.pack(fill=tk.X, pady=(0, 6))
        else:
            btn.pack(side=tk.RIGHT, padx=(8, 0))
        placed.append(btn)
    return placed


def fit_to_screen(win: tk.Toplevel, message: tk.Label) -> None:
    """Make a dialog that is taller than the screen scroll its message, so its buttons stay in reach.

    A long list of songs, a big Text Size and a small screen together made a dialog taller than
    the screen: its buttons were below the bottom edge, where they cannot be clicked. The message
    is then shown in a box of its own with a scroll bar, as tall as the screen has room for.
    """
    win.update_idletasks()
    scale = max(1.0, win.winfo_fpixels("1i") / 96.0)
    available = win.winfo_screenheight() - round(_SCREEN_MARGIN_PX * scale)
    excess = win.winfo_reqheight() - available
    if excess <= 0:
        return
    line_px = max(1, tkfont.nametofont(FONT_BODY, root=win).metrics("linespace"))
    lines = max(_MIN_MESSAGE_LINES, (message.winfo_reqheight() - excess) // line_px)
    frame = tk.Frame(message.master, bg=BG_CARD)
    scrollbar = tk.Scrollbar(frame, orient=tk.VERTICAL)
    box = tk.Text(
        frame,
        font=FONT_BODY,
        fg=TEXT_DARK,
        bg=BG_CARD,
        wrap=tk.WORD,
        width=_WRAP_CHARS,
        height=lines,
        relief=tk.FLAT,
        borderwidth=0,
        highlightthickness=0,
        cursor="arrow",
        yscrollcommand=scrollbar.set,
    )
    box.insert("1.0", str(message.cget("text")))
    box.config(state=tk.DISABLED)  # read-only; it still scrolls with the wheel and the bar
    scrollbar.config(command=box.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    frame.pack(fill=tk.X, pady=(0, 16), before=message)
    message.destroy()
    # The box has a few pixels of its own around the text: take lines off until everything fits.
    win.update_idletasks()
    while win.winfo_reqheight() > available and lines > _MIN_MESSAGE_LINES:
        lines -= 1
        box.config(height=lines)
        win.update_idletasks()


def _run_modal(win: tk.Toplevel, top: tk.Misc, focus: tk.Misc | None, message: tk.Label) -> None:
    """Show the dialog over the app window and wait until it is closed."""
    fit_to_screen(win, message)
    center_over(win, top)
    if focus is not None:
        focus.focus_set()
    with contextlib.suppress(tk.TclError):
        win.grab_set()
    win.lift()
    win.wait_window()


def center_over(win: tk.Toplevel, top: tk.Misc) -> None:
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

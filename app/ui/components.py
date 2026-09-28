"""Reusable UI components: ToolTips, tactile buttons, scrolled listbox, and vinyl graphics."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any

from app.ui.theme import BG_INPUT, COLOR_BTN_NEUTRAL, COLOR_BTN_NEUTRAL_HV, FONT_BODY_BOLD, FONT_TOOLTIP, TEXT_DARK

# Button fills that are too close to the panel colour to read as buttons on their own;
# these get a permanent dark 1px outline.
_LOW_CONTRAST_FILLS = {COLOR_BTN_NEUTRAL.lower(), "#ffffff", "#f8fafc", "#f1f5f9", "#e0f2fe", "#dbeafe", "#fee2e2"}


class ToolTip:
    """Accessible hover tooltip with hover debounce and automatic boundary dismissal."""

    def __init__(self, widget, text, delay=350):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self._timer = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<FocusOut>", self._hide, add="+")
        widget.bind("<Unmap>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")
        try:
            top = widget.winfo_toplevel()
            if top and top != widget:
                top.bind("<FocusOut>", self._hide, add="+")
                top.bind("<Deactivate>", self._hide, add="+")
        except Exception:
            pass

    def _schedule(self, event=None):
        self._cancel()
        self._timer = self.widget.after(self.delay, lambda: self._show(event))

    def _cancel(self):
        if self._timer:
            try:
                self.widget.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None

    def _show(self, event=None):
        self._cancel()
        if self.tip or not self.widget.winfo_exists() or not self.widget.winfo_viewable():
            return
        try:
            screen_w = self.widget.winfo_screenwidth()
            screen_h = self.widget.winfo_screenheight()
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            if x + 360 > screen_w:
                x = max(10, screen_w - 365)
            if y + 80 > screen_h:
                y = max(10, self.widget.winfo_rooty() - 40)
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(
                self.tip,
                text=self.text,
                font=FONT_TOOLTIP,
                bg="#0f172a",
                fg="#ffffff",
                padx=10,
                pady=5,
                relief=tk.FLAT,
                borderwidth=0,
                wraplength=340,
                justify="left",
            ).pack()
        except Exception:
            if self.tip:
                try:
                    self.tip.destroy()
                except Exception:
                    pass
                self.tip = None

    def _hide(self, _event=None):
        self._cancel()
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


def create_button(
    parent: tk.Misc,
    text: str,
    command: Callable[[], Any],
    bg: str = COLOR_BTN_NEUTRAL,
    fg: str = TEXT_DARK,
    hover_bg: str | None = COLOR_BTN_NEUTRAL_HV,
    font: Any = FONT_BODY_BOLD,
    pady: int = 4,
    padx: int = 6,
    **kwargs: Any,
) -> tk.Button:
    """Helper to create polished, tactile buttons with hover feedback.

    Every button can take keyboard focus (Tab; Windows draws a dotted focus rectangle) and be
    activated with Space or Enter. Pale buttons carry a dark outline so they are recognisable
    as buttons (WCAG 1.4.11): Tk on Windows ignores highlight rings on buttons, so a SOLID
    relief border is used instead.
    """
    outlined = str(bg).lower() in _LOW_CONTRAST_FILLS
    btn = tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg,
        fg=fg,
        activebackground=hover_bg or bg,
        activeforeground=fg,
        font=font,
        relief=tk.SOLID if outlined else tk.FLAT,
        borderwidth=1 if outlined else 0,
        cursor="hand2",
        padx=padx,
        pady=pady,
        highlightthickness=0,
        takefocus=1,
        **kwargs,
    )
    if hover_bg:
        btn.bind("<Enter>", lambda e: btn.config(bg=hover_bg))
        btn.bind("<Leave>", lambda e: btn.config(bg=bg))
    btn.bind("<Return>", lambda e: btn.invoke())
    return btn


class ScrollableFrame(tk.Frame):
    """A panel whose content (built into ``self.body``) scrolls when the window is too small.

    When everything fits, ``body`` is stretched to the visible area so expanding children
    (e.g. song lists) still fill the space; scrollbars only appear when they are needed.
    """

    def __init__(self, parent: tk.Misc, bg: str, **frame_kwargs: Any) -> None:
        super().__init__(parent, bg=bg, **frame_kwargs)
        # Tiny requested size: the grid stretches it; the default 10cm request would force wide columns.
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, borderwidth=0, width=50, height=50)
        self.vbar = tk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.hbar = tk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vbar.set, xscrollcommand=self.hbar.set)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self._pending: str | None = None
        self.body.bind("<Configure>", self._schedule_relayout, add="+")
        self.canvas.bind("<Configure>", self._schedule_relayout, add="+")
        _install_wheel_router(self)

    def _schedule_relayout(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if self._pending is None:
            self._pending = self.after_idle(self._relayout)

    def _relayout(self) -> None:
        self._pending = None
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        rw, rh = self.body.winfo_reqwidth(), self.body.winfo_reqheight()
        w, h = max(cw, rw), max(ch, rh)
        self.canvas.itemconfigure(self._window, width=w, height=h)
        self.canvas.configure(scrollregion=(0, 0, w, h))
        self._toggle(self.vbar, rh > ch, row=0, column=1, sticky="ns")
        self._toggle(self.hbar, rw > cw, row=1, column=0, sticky="ew")

    @staticmethod
    def _toggle(bar: tk.Scrollbar, show: bool, **grid_kwargs: Any) -> None:
        if show and not bar.winfo_ismapped():
            bar.grid(**grid_kwargs)
        elif not show and bar.winfo_ismapped():
            bar.grid_remove()

    def can_scroll(self) -> bool:
        return self.body.winfo_reqheight() > self.canvas.winfo_height()

    def scroll_units(self, units: int) -> None:
        self.canvas.yview_scroll(units, "units")


# Widgets that handle the mouse wheel themselves; scrolling over them must not move the panel.
_SELF_SCROLLING = ("Listbox", "Text", "Scale", "TCombobox", "Treeview")
_wheel_routers: set[str] = set()


def _install_wheel_router(frame: ScrollableFrame) -> None:
    """Route mouse-wheel events over a ScrollableFrame to it (one app-wide binding per Tk root)."""
    root = frame.winfo_toplevel()
    key = str(root)
    if key in _wheel_routers:
        return
    _wheel_routers.add(key)

    def _on_wheel(event: tk.Event[tk.Misc]) -> str | None:
        widget: tk.Misc | None = frame.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget.winfo_class() in _SELF_SCROLLING:
                return None
            if isinstance(widget, ScrollableFrame):
                if widget.can_scroll():
                    widget.scroll_units(-1 if event.delta > 0 else 1)
                return "break"
            widget = widget.master
        return None

    root.bind_all("<MouseWheel>", _on_wheel, add="+")


def scrolled_listbox(parent: tk.Misc, **kwargs: Any) -> tuple[tk.Frame, tk.Listbox]:
    """Factory helper creating a Listbox with an attached Vertical Scrollbar in a container frame.

    The default height is small (6 rows) so the list shrinks on small screens; it still expands
    to fill any spare space when packed with ``expand=True``.
    """
    kwargs.setdefault("height", 6)
    kwargs.setdefault("activestyle", "dotbox")
    frame = tk.Frame(parent, bg=kwargs.get("bg", BG_INPUT))
    scrollbar = tk.Scrollbar(frame, orient=tk.VERTICAL)
    listbox = tk.Listbox(frame, yscrollcommand=scrollbar.set, **kwargs)
    scrollbar.config(command=listbox.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    return frame, listbox


def draw_placeholder_cover(canvas):
    """Draw a stylized retro vinyl record placeholder on a 60x60 canvas."""
    canvas.delete("all")
    canvas.create_oval(4, 4, 56, 56, fill="#1e293b", outline="#334155", width=2)
    canvas.create_oval(14, 14, 46, 46, outline="#475569", width=1)
    canvas.create_oval(20, 20, 40, 40, fill="#f59e0b", outline="#d97706", width=1)
    canvas.create_oval(28, 28, 32, 32, fill="#0f172a", outline="")

"""Reusable UI components: ToolTips, tactile buttons, scrolled listbox, and vinyl graphics."""

import tkinter as tk
from app.ui.theme import (
    FONT_TOOLTIP, FONT_BODY_BOLD, BG_INPUT,
    COLOR_BTN_NEUTRAL, COLOR_BTN_NEUTRAL_HV, TEXT_DARK
)


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
                self.tip, text=self.text, font=FONT_TOOLTIP,
                bg="#0f172a", fg="#ffffff", padx=10, pady=5,
                relief=tk.FLAT, borderwidth=0, wraplength=340, justify="left"
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
    parent, text, command,
    bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, hover_bg=COLOR_BTN_NEUTRAL_HV,
    font=FONT_BODY_BOLD, pady=4, padx=6, **kwargs
):
    """Helper to create polished, tactile buttons with hover feedback."""
    btn = tk.Button(
        parent, text=text, command=command, bg=bg, fg=fg,
        activebackground=hover_bg or bg, activeforeground=fg,
        font=font, relief=tk.FLAT, borderwidth=0, cursor="hand2",
        padx=padx, pady=pady, highlightthickness=0, **kwargs
    )
    if hover_bg:
        btn.bind("<Enter>", lambda e: btn.config(bg=hover_bg))
        btn.bind("<Leave>", lambda e: btn.config(bg=bg))
    return btn


def scrolled_listbox(parent, **kwargs):
    """Factory helper creating a Listbox with an attached Vertical Scrollbar in a container frame."""
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

"""Update dialog: release notes, optional download with progress, and "Restart and update now"."""

from __future__ import annotations

import contextlib
import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import Any

from app.config import APP_VERSION, log_error
from app.models import ReleaseInfo
from app.platform_utils import already_running
from app.services.updater import PendingUpdate, install_update, stage_update
from app.ui import dialogs
from app.ui.components import create_button
from app.ui.theme import (
    BG_CARD,
    BG_ROOT,
    BG_SUB_CARD,
    BORDER_MAIN,
    COLOR_ACCENT,
    COLOR_ACCENT_HV,
    COLOR_BTN_NEUTRAL,
    COLOR_BTN_NEUTRAL_HV,
    FONT_APP_TITLE,
    FONT_BODY,
    FONT_BODY_BOLD,
    FONT_BTN_MAIN,
    FONT_BTN_SUB,
    TEXT_DARK,
    TEXT_MEDIUM,
)

_BUSY_RETRY_MS = 3000


class UpdateDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        release_info: ReleaseInfo,
        pending: PendingUpdate | None = None,
        busy_reason_fn: Callable[[], str | None] | None = None,
        prepare_restart_fn: Callable[[], None] | None = None,
        on_pending: Callable[[PendingUpdate], None] | None = None,
    ) -> None:
        """Show release notes and offer to install now or later.

        When ``pending`` is given the update is already downloaded and verified; otherwise "install
        now" downloads it first. busy_reason_fn() describes work in progress (download, export...) or
        returns None; the restart waits until that work has finished. prepare_restart_fn() saves
        settings and playlists before the executable is swapped.
        """
        super().__init__(parent)
        self.parent_top = parent.winfo_toplevel()
        self.release_info = release_info
        self.pending = pending
        self.busy_reason_fn = busy_reason_fn
        self.prepare_restart_fn = prepare_restart_fn
        self.on_pending = on_pending
        self._cancel_event = threading.Event()
        self._is_downloading = False
        self._closed = False
        self._ui_queue: queue.Queue[tuple[Callable[..., Any], tuple[Any, ...]]] = queue.Queue()
        self._drain_timer: str | None = None
        self._can_install = bool(getattr(sys, "frozen", False))

        self.title("Application Update Available")
        self.configure(bg=BG_ROOT)
        self.resizable(False, False)
        self.transient(self.parent_top)

        self._drain_ui_queue()
        self._build_ui()

        # Size to content (fonts follow the user's text-size setting) and center on parent
        self.update_idletasks()
        w = max(540, self.winfo_reqwidth())
        h = max(420, self.winfo_reqheight())
        x = max(50, self.parent_top.winfo_rootx() + (self.parent_top.winfo_width() - w) // 2)
        y = max(50, self.parent_top.winfo_rooty() + (self.parent_top.winfo_height() - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda _e: self._on_cancel())

        with contextlib.suppress(tk.TclError):
            self.grab_set()

    def _card(self, parent: tk.Misc, **pack: Any) -> tk.Frame:
        card = tk.Frame(
            parent,
            bg=BG_CARD,
            relief=tk.SOLID,
            borderwidth=1,
            highlightbackground=BORDER_MAIN,
            highlightthickness=1,
            padx=14,
            pady=10,
        )
        card.pack(**pack)
        return card

    def _initial_status(self) -> tuple[str, str]:
        """(status text, main button label) for the current state."""
        if not self._can_install:
            return "Running in development mode (source code). Updates can be pulled with git pull.", ""
        if self.pending is not None:
            return (
                "The new version has been downloaded. It will be installed automatically the next time "
                "you open the app, or you can restart now.",
                "Restart and update now",
            )
        size_mb = self.release_info.asset_size / (1024 * 1024)
        size_note = f" (about {size_mb:.0f} MB)" if size_mb >= 1 else ""
        return f"Ready to download the new version{size_note}.", "Download and install now"

    def _build_ui(self) -> None:
        container = tk.Frame(self, bg=BG_ROOT, padx=20, pady=16)
        container.pack(fill=tk.BOTH, expand=True)

        card_header = self._card(container, fill=tk.X, pady=(0, 10))
        card_header.config(padx=16, pady=12)
        tk.Label(
            card_header, text="⭐ A New Version Is Available", font=FONT_APP_TITLE, bg=BG_CARD, fg=TEXT_DARK, anchor="w"
        ).pack(fill=tk.X)
        tk.Label(
            card_header,
            text=f"Your version: {APP_VERSION}  ➔  New version: {self.release_info.tag_name}",
            font=FONT_BODY_BOLD,
            bg=BG_CARD,
            fg=COLOR_ACCENT,
            anchor="w",
        ).pack(fill=tk.X, pady=(4, 0))

        card_notes = self._card(container, fill=tk.BOTH, expand=True, pady=(0, 10))
        tk.Label(
            card_notes, text="What's New in this Version:", font=FONT_BODY_BOLD, bg=BG_CARD, fg=TEXT_DARK, anchor="w"
        ).pack(fill=tk.X, pady=(0, 6))
        txt_frame = tk.Frame(
            card_notes,
            bg=BG_SUB_CARD,
            relief=tk.SOLID,
            borderwidth=1,
            highlightbackground=BORDER_MAIN,
            highlightthickness=1,
        )
        txt_frame.pack(fill=tk.BOTH, expand=True)
        scrollbar = tk.Scrollbar(txt_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_notes = tk.Text(
            txt_frame,
            font=FONT_BODY,
            bg=BG_SUB_CARD,
            fg=TEXT_MEDIUM,
            wrap=tk.WORD,
            yscrollcommand=scrollbar.set,
            relief=tk.FLAT,
            padx=8,
            pady=8,
            height=6,
        )
        self.txt_notes.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.txt_notes.yview)
        self.txt_notes.insert("1.0", (self.release_info.body or "Performance enhancements and bug fixes.").strip())
        self.txt_notes.config(state=tk.DISABLED)

        card_prog = self._card(container, fill=tk.X, pady=(0, 12))
        status_text, action_text = self._initial_status()
        wrap = max(420, self.winfo_reqwidth())
        self.lbl_status = tk.Label(
            card_prog, text=status_text, font=FONT_BODY, bg=BG_CARD, fg=TEXT_DARK, anchor="w", justify="left"
        )
        self.lbl_status.config(wraplength=wrap)
        self.lbl_status.pack(fill=tk.X, pady=(0, 6))
        self.progressbar = ttk.Progressbar(card_prog, mode="determinate", maximum=100)
        if self.pending is None and self._can_install:
            self.progressbar.pack(fill=tk.X, ipady=3)

        f_btns = tk.Frame(container, bg=BG_ROOT)
        f_btns.pack(fill=tk.X)
        self.btn_action = create_button(
            f_btns,
            text=action_text or "Close",
            command=self._on_action if self._can_install else self._on_cancel,
            bg=COLOR_ACCENT,
            hover_bg=COLOR_ACCENT_HV,
            fg="#ffffff",
            font=FONT_BTN_MAIN,
            pady=6,
        )
        self.btn_action.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.btn_cancel = create_button(
            f_btns,
            text="Later",
            command=self._on_cancel,
            bg=COLOR_BTN_NEUTRAL,
            hover_bg=COLOR_BTN_NEUTRAL_HV,
            fg=TEXT_DARK,
            font=FONT_BTN_SUB,
            pady=6,
        )
        if self._can_install:
            self.btn_cancel.pack(side=tk.RIGHT, padx=(8, 0))
        self.btn_action.focus_set()

    # --- Worker-thread plumbing ---

    def _safe_dispatch(self, callback: Callable[..., Any], *args: Any) -> None:
        """Safely schedule a callback on Tkinter main thread via queue."""
        if not self._closed:
            self._ui_queue.put((callback, args))

    def _drain_ui_queue(self) -> None:
        """Drain queued background callbacks on the main GUI thread."""
        if self._closed:
            return
        while True:
            try:
                cb, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                cb(*args)
            except Exception as e:
                log_error(f"UpdateDialog._drain_ui_queue: {e}")
            finally:
                self._ui_queue.task_done()
        if not self._closed:
            with contextlib.suppress(tk.TclError):
                if self.winfo_exists():
                    self._drain_timer = self.after(25, self._drain_ui_queue)

    # --- Actions ---

    def _on_action(self) -> None:
        if self.pending is not None:
            self._restart_now()
        else:
            self._start_download()

    def _start_download(self) -> None:
        if self._is_downloading:
            return
        self._is_downloading = True
        self.btn_action.config(state=tk.DISABLED, text="Downloading...")
        self.btn_cancel.config(text="Cancel")
        # When the app is already fetching this version in the background, that download is waited
        # for instead of starting a second one, so the bar may stay empty until it is done.
        self.lbl_status.config(text="Downloading the new version... This can take a few minutes.")
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self) -> None:
        def _progress(pct: float, downloaded: int, total: int) -> None:
            mb_down = downloaded / (1024 * 1024)
            mb_tot = total / (1024 * 1024) if total > 0 else 0
            self._safe_dispatch(
                self._update_progress_ui, pct, f"Downloading: {pct:.0f}%  ({mb_down:.1f} of {mb_tot:.1f} MB)"
            )

        pending, err = stage_update(self.release_info, progress_callback=_progress, cancel_event=self._cancel_event)
        if pending is not None:
            self._safe_dispatch(self._on_download_complete, pending)
        elif not self._cancel_event.is_set():
            self._safe_dispatch(self._on_download_failed, err)

    def _update_progress_ui(self, pct: float, text: str) -> None:
        self.progressbar["value"] = pct
        self.lbl_status.config(text=text)

    def _on_download_complete(self, pending: PendingUpdate) -> None:
        self._is_downloading = False
        self.pending = pending
        if self.on_pending:
            self.on_pending(pending)
        self.progressbar["value"] = 100
        self._restart_now()

    def _on_download_failed(self, error_msg: str) -> None:
        self._is_downloading = False
        self.btn_action.config(state=tk.NORMAL, text="Try Again")
        self.btn_cancel.config(state=tk.NORMAL, text="Close")
        self.lbl_status.config(text=f"The update could not be downloaded: {error_msg}")
        log_error(f"Update download failed: {error_msg}")

    def _restart_now(self) -> None:
        if self.pending is None or self._closed:
            return
        busy = self.busy_reason_fn() if self.busy_reason_fn else None
        if busy:
            # Never restart in the middle of a download/export; retry once the work is finished.
            self.btn_action.config(state=tk.DISABLED, text="Waiting...")
            self.lbl_status.config(text=f"The update will install as soon as {busy} finishes...")
            self.after(_BUSY_RETRY_MS, self._restart_now)
            return
        self.btn_action.config(state=tk.DISABLED, text="Restarting...")
        self.btn_cancel.config(state=tk.DISABLED)
        self.lbl_status.config(text="Installing the update and restarting...")
        self.update_idletasks()
        if self.prepare_restart_fn:
            try:
                self.prepare_restart_fn()
            except Exception as e:
                log_error(f"Update prepare_restart failed: {e}")

        def _hide_windows() -> None:
            # The old version waits here until the new one is up; nothing should look frozen meanwhile.
            with contextlib.suppress(tk.TclError):
                self.withdraw()
                self.parent_top.withdraw()
                self.parent_top.update_idletasks()

        result = install_update(self.pending.exe_path, self.pending.tag, on_launched=_hide_windows)
        # Still running: nothing changed, or the new version failed and the old one was restored.
        already_running()  # take the single-instance lock back (released for the new version)
        with contextlib.suppress(tk.TclError):
            self.parent_top.deiconify()
        self._close()
        dialogs.show_warning(self.parent_top, "Update Not Installed", result.message)

    def _close(self) -> None:
        self._closed = True
        if self._drain_timer:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._drain_timer)
        with contextlib.suppress(tk.TclError):
            self.grab_release()
        self.destroy()

    def _on_cancel(self) -> None:
        if self._is_downloading:
            self._cancel_event.set()
        self._close()

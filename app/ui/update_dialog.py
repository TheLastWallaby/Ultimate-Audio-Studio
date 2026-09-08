import os
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from app.config import APP_VERSION, log_error
from app.ui.theme import (
    FONT_APP_TITLE, FONT_BTN_MAIN, FONT_BTN_SUB, FONT_BODY, FONT_BODY_BOLD,
    BG_ROOT, BG_CARD, BG_SUB_CARD, BG_INPUT, BORDER_MAIN,
    TEXT_DARK, TEXT_MEDIUM, TEXT_MUTED,
    COLOR_ACCENT, COLOR_ACCENT_HV, COLOR_BTN_NEUTRAL, COLOR_BTN_NEUTRAL_HV
)
from app.ui.components import create_button
from app.services.updater import download_release_asset, apply_update_and_restart

class UpdateDialog(tk.Toplevel):
    def __init__(self, parent, release_info, auto_start=False):
        super().__init__(parent)
        self.parent = parent
        self.release_info = release_info
        self.auto_start = auto_start
        self._cancel_event = threading.Event()
        self._is_downloading = False
        self._temp_exe = None
        self._closed = False
        self._ui_queue = queue.Queue()
        self._drain_timer = None

        self.title('Application Update Available')
        self.configure(bg=BG_ROOT)
        self.resizable(False, False)
        self.transient(parent)

        self._drain_ui_queue()

        # Center on parent
        w, h = 540, 480
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        x = max(50, px + (pw - w) // 2)
        y = max(50, py + (ph - h) // 2)
        self.geometry(f'{w}x{h}+{x}+{y}')

        self._build_ui()
        self.protocol('WM_DELETE_WINDOW', self._on_cancel)

        try:
            self.grab_set()
        except Exception:
            pass

        if self.auto_start and getattr(sys, "frozen", False):
            self.after(500, self._start_download)

    def _build_ui(self):
        container = tk.Frame(self, bg=BG_ROOT, padx=20, pady=16)
        container.pack(fill=tk.BOTH, expand=True)

        # Header card
        card_header = tk.Frame(container, bg=BG_CARD, relief=tk.SOLID, borderwidth=1,
                               highlightbackground=BORDER_MAIN, highlightthickness=1, padx=16, pady=12)
        card_header.pack(fill=tk.X, pady=(0, 10))

        lbl_title = tk.Label(
            card_header, text='⭐ A New Update is Available!',
            font=FONT_APP_TITLE, bg=BG_CARD, fg=TEXT_DARK, anchor='w'
        )
        lbl_title.pack(fill=tk.X)

        tag = self.release_info.get('tag_name', 'New')
        lbl_sub = tk.Label(
            card_header,
            text=f'Current Version: {APP_VERSION}  ➔  New Version: {tag}',
            font=FONT_BODY_BOLD, bg=BG_CARD, fg=COLOR_ACCENT, anchor='w'
        )
        lbl_sub.pack(fill=tk.X, pady=(4, 0))

        # Release notes card
        card_notes = tk.Frame(container, bg=BG_CARD, relief=tk.SOLID, borderwidth=1,
                              highlightbackground=BORDER_MAIN, highlightthickness=1, padx=14, pady=10)
        card_notes.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        lbl_notes_hdr = tk.Label(
            card_notes, text='What\'s New in this Version:',
            font=FONT_BODY_BOLD, bg=BG_CARD, fg=TEXT_DARK, anchor='w'
        )
        lbl_notes_hdr.pack(fill=tk.X, pady=(0, 6))

        txt_frame = tk.Frame(card_notes, bg=BG_SUB_CARD, relief=tk.SOLID, borderwidth=1,
                             highlightbackground=BORDER_MAIN, highlightthickness=1)
        txt_frame.pack(fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(txt_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.txt_notes = tk.Text(
            txt_frame, font=FONT_BODY, bg=BG_SUB_CARD, fg=TEXT_MEDIUM,
            wrap=tk.WORD, yscrollcommand=scrollbar.set, relief=tk.FLAT,
            padx=8, pady=8, height=6
        )
        self.txt_notes.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.txt_notes.yview)

        notes_body = self.release_info.get('body', 'Performance enhancements and bug fixes.').strip()
        self.txt_notes.insert('1.0', notes_body)
        self.txt_notes.config(state=tk.DISABLED)

        # Progress card
        self.card_prog = tk.Frame(container, bg=BG_CARD, relief=tk.SOLID, borderwidth=1,
                                  highlightbackground=BORDER_MAIN, highlightthickness=1, padx=14, pady=10)
        self.card_prog.pack(fill=tk.X, pady=(0, 12))

        is_frozen = getattr(sys, "frozen", False)
        status_init = 'Ready to update.' if is_frozen else 'Running in development mode (source code). Updates can be pulled with git pull.'
        btn_text = 'Download & Install Update' if is_frozen else 'Dev Mode (Use git pull)'

        self.lbl_status = tk.Label(
            self.card_prog, text=status_init,
            font=FONT_BODY, bg=BG_CARD, fg=TEXT_DARK, anchor='w'
        )
        self.lbl_status.pack(fill=tk.X, pady=(0, 6))

        self.progressbar = ttk.Progressbar(self.card_prog, mode='determinate', maximum=100)
        self.progressbar.pack(fill=tk.X, ipady=3)

        # Action Buttons
        f_btns = tk.Frame(container, bg=BG_ROOT)
        f_btns.pack(fill=tk.X)

        self.btn_action = create_button(
            f_btns, text=btn_text,
            command=self._start_download,
            bg=COLOR_ACCENT if is_frozen else COLOR_BTN_NEUTRAL,
            hover_bg=COLOR_ACCENT_HV if is_frozen else COLOR_BTN_NEUTRAL_HV,
            fg='#ffffff' if is_frozen else TEXT_MUTED,
            font=FONT_BTN_MAIN, height=36
        )
        if not is_frozen:
            self.btn_action.config(state=tk.DISABLED)
        self.btn_action.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.btn_cancel = create_button(
            f_btns, text='Close' if not is_frozen else 'Remind Me Later',
            command=self._on_cancel,
            bg=COLOR_BTN_NEUTRAL, hover_bg=COLOR_BTN_NEUTRAL_HV, fg=TEXT_DARK,
            font=FONT_BTN_SUB, height=36
        )
        self.btn_cancel.pack(side=tk.RIGHT, padx=(8, 0))

    def _start_download(self):
        if not getattr(sys, "frozen", False):
            messagebox.showinfo("Development Mode", "Running in development mode (source code). Updates can be pulled with git pull.")
            return
        if self._is_downloading:
            return
        self._is_downloading = True
        self.btn_action.config(state=tk.DISABLED, text='Downloading...')
        self.btn_cancel.config(text='Cancel')
        self.lbl_status.config(text='Connecting to download server...')

        threading.Thread(target=self._download_worker, daemon=True).start()

    def _safe_dispatch(self, callback, *args):
        """Safely schedule a callback on Tkinter main thread via queue."""
        if not self._closed:
            self._ui_queue.put((callback, args))

    def _drain_ui_queue(self):
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
            try:
                if self.winfo_exists():
                    self._drain_timer = self.after(25, self._drain_ui_queue)
            except Exception:
                pass

    def _download_worker(self):
        asset_id = self.release_info.get('asset_id')
        if not asset_id:
            self._safe_dispatch(self._on_download_failed, 'No downloadable asset found for this release.')
            return

        def _progress(pct, downloaded, total):
            mb_down = downloaded / (1024 * 1024)
            mb_tot = total / (1024 * 1024) if total > 0 else 0
            pct_text = f'Downloading: {pct:.1f}%  ({mb_down:.1f} MB / {mb_tot:.1f} MB)'
            self._safe_dispatch(self._update_progress_ui, pct, pct_text)

        ok, result = download_release_asset(
            asset_id=asset_id,
            progress_callback=_progress,
            cancel_event=self._cancel_event
        )

        if ok:
            self._temp_exe = result
            self._safe_dispatch(self._on_download_complete)
        else:
            if not self._cancel_event.is_set():
                self._safe_dispatch(self._on_download_failed, result)

    def _update_progress_ui(self, pct, text):
        if not self.winfo_exists():
            return
        self.progressbar['value'] = pct
        self.lbl_status.config(text=text)

    def _on_download_complete(self):
        if not self.winfo_exists():
            return
        self.progressbar['value'] = 100
        self.lbl_status.config(text='Download complete! Preparing to restart...')
        self.btn_action.config(text='Restarting...', state=tk.DISABLED)
        self.btn_cancel.config(state=tk.DISABLED)

        # Allow user to see 100% completion for 1 second, then swap and restart
        self.after(1000, self._apply_update)

    def _apply_update(self):
        if not self._temp_exe or not os.path.exists(self._temp_exe):
            messagebox.showerror('Update Error', 'Could not locate downloaded update file.')
            self.destroy()
            return

        ok, msg = apply_update_and_restart(self._temp_exe)
        if not ok:
            messagebox.showwarning('Update Notice', f'{msg}')
            self.destroy()

    def _on_download_failed(self, error_msg):
        if not self.winfo_exists():
            return
        self._is_downloading = False
        self.btn_action.config(state=tk.NORMAL, text='Retry Download')
        self.btn_cancel.config(state=tk.NORMAL, text='Close')
        self.lbl_status.config(text=f'Update failed: {error_msg}')
        log_error(f'Update download failed: {error_msg}')

    def _on_cancel(self):
        self._closed = True
        if self._drain_timer:
            try:
                self.after_cancel(self._drain_timer)
            except Exception:
                pass
        if self._is_downloading:
            self._cancel_event.set()
        self.destroy()

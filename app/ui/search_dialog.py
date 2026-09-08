"""Search results selection dialog allowing users to preview and pick which YouTube result to download."""

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk
import pygame

from app.config import format_time, log_error
from app.services.downloader import fetch_preview_worker
from app.ui.theme import (
    FONT_APP_TITLE, FONT_SECTION_HDR, FONT_BODY, FONT_BODY_BOLD,
    FONT_BTN_MAIN, FONT_BTN_SUB, FONT_FAMILY,
    BG_CARD, BG_SUB_CARD, BG_INPUT, BORDER_MAIN, TEXT_DARK, TEXT_MUTED,
    COLOR_DOWNLOAD, COLOR_DOWNLOAD_HV, COLOR_BTN_NEUTRAL, COLOR_BTN_NEUTRAL_HV,
    COLOR_PLAY, COLOR_PLAY_HV, COLOR_PAUSE, COLOR_PAUSE_HV, COLOR_STOP, COLOR_STOP_HV, COLOR_ACCENT
)
from app.ui.components import create_button


class SearchChoiceDialog:
    """Modal dialog presenting multiple YouTube search results with preview playback, title, artist, and duration."""

    def __init__(self, parent, query, results, on_select, on_cancel=None, audio_engine=None, on_preview_play=None):
        self.parent = parent
        self.query = query
        self.results = results or []
        self.on_select = on_select
        self.on_cancel = on_cancel
        self.audio_engine = audio_engine
        self.on_preview_play = on_preview_play
        self.chosen_item = None

        # Preview state tracking
        self._preview_cancel_event = threading.Event()
        self._preview_request_id = 0
        self._is_previewing = False
        self._is_loading_preview = False
        self._preview_active_url = None
        self._preview_poll_job = None
        self._preview_duration = 30.0
        self._closed = False
        self._ui_queue = queue.Queue()
        self._drain_timer = None

        self.win = tk.Toplevel(parent)
        self.win.title("Choose Version to Download")
        self.win.geometry("760x560")
        self.win.minsize(640, 460)
        self.win.configure(bg=BG_CARD)
        self.win.transient(parent)

        self._drain_ui_queue()

        # Center over parent
        self._center_window()

        self._build_ui()
        self.win.protocol("WM_DELETE_WINDOW", self._do_cancel)

        # Grab focus and select first row
        try:
            self.win.grab_set()
            self.tree.focus_set()
            if self.results:
                first_id = self.tree.get_children()[0]
                self.tree.selection_set(first_id)
                self._on_tree_select()
        except Exception:
            pass

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
                log_error(f"SearchChoiceDialog._drain_ui_queue: {e}")
            finally:
                self._ui_queue.task_done()
        if not self._closed:
            try:
                if self.win.winfo_exists():
                    self._drain_timer = self.win.after(25, self._drain_ui_queue)
            except Exception:
                pass

    def _center_window(self):
        self.win.update_idletasks()
        try:
            pw = self.parent.winfo_width()
            ph = self.parent.winfo_height()
            px = self.parent.winfo_rootx()
            py = self.parent.winfo_rooty()
            w, h = 760, 560
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 2)
            self.win.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _build_ui(self):
        container = tk.Frame(self.win, bg=BG_CARD, padx=16, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        # Header Badge & Title
        f_top = tk.Frame(container, bg=BG_CARD)
        f_top.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        tk.Label(
            f_top, text="🎵 Multiple Matches Found",
            font=FONT_APP_TITLE, fg=TEXT_DARK, bg=BG_CARD
        ).pack(anchor="w")

        tk.Label(
            f_top,
            text=f'Select the version of "{self.query}" you would like to download:',
            font=FONT_BODY, fg=TEXT_MUTED, bg=BG_CARD
        ).pack(anchor="w", pady=(2, 0))

        # Bottom Controls - Pack to BOTTOM first so action buttons are always visible
        self.f_btns = tk.Frame(container, bg=BG_CARD)
        self.f_btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))

        tk.Label(
            self.f_btns, text="💡 Tip: Double-click a song to download.",
            font=FONT_BODY, fg=TEXT_MUTED, bg=BG_CARD
        ).pack(side=tk.LEFT, anchor="c")

        self.btn_more = create_button(
            self.f_btns, "➕ Show More Results", self._load_more_results,
            bg=COLOR_BTN_NEUTRAL, fg=COLOR_ACCENT, hover_bg=COLOR_BTN_NEUTRAL_HV,
            font=FONT_BTN_SUB, padx=10, pady=5
        )
        self.btn_more.pack(side=tk.LEFT, padx=(10, 0))

        self.btn_cancel = create_button(
            self.f_btns, "Cancel", self._do_cancel,
            bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, hover_bg=COLOR_BTN_NEUTRAL_HV,
            font=FONT_BTN_SUB, padx=14, pady=5
        )
        self.btn_cancel.pack(side=tk.RIGHT, padx=(6, 0))

        self.btn_download = create_button(
            self.f_btns, "⬇ Download Selected", self._do_select,
            bg=COLOR_DOWNLOAD, fg="#ffffff", hover_bg=COLOR_DOWNLOAD_HV,
            font=FONT_BTN_MAIN, padx=16, pady=5
        )
        self.btn_download.pack(side=tk.RIGHT)

        # Preview Control Panel - Pack to BOTTOM above f_btns
        self.f_prev = tk.Frame(container, bg=BG_SUB_CARD, highlightthickness=1, highlightbackground=BORDER_MAIN, padx=12, pady=8)
        self.f_prev.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 8))

        f_prev_top = tk.Frame(self.f_prev, bg=BG_SUB_CARD)
        f_prev_top.pack(fill=tk.X)

        self.btn_preview_play = create_button(
            f_prev_top, "▶ Play Preview", self._toggle_preview,
            bg=COLOR_PLAY, fg="#ffffff", hover_bg=COLOR_PLAY_HV,
            font=FONT_BTN_MAIN, padx=12, pady=4
        )
        self.btn_preview_play.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_preview_stop = create_button(
            f_prev_top, "⏹ Stop", self._stop_preview,
            bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, hover_bg=COLOR_BTN_NEUTRAL_HV,
            font=FONT_BTN_SUB, padx=10, pady=4, state=tk.DISABLED
        )
        self.btn_preview_stop.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_preview_download = create_button(
            f_prev_top, "⬇ Download", self._do_select,
            bg=COLOR_DOWNLOAD, fg="#ffffff", hover_bg=COLOR_DOWNLOAD_HV,
            font=FONT_BTN_SUB, padx=10, pady=4
        )
        self.btn_preview_download.pack(side=tk.LEFT, padx=(0, 10))

        self.lbl_preview_time = tk.Label(
            f_prev_top, text="0:00 / 0:30",
            font=FONT_BODY_BOLD, fg=TEXT_MUTED, bg=BG_SUB_CARD
        )
        self.lbl_preview_time.pack(side=tk.RIGHT, padx=(6, 0))

        self.lbl_preview_status = tk.Label(
            f_prev_top, text="Select a song and click 'Play Preview' to listen (30-sec sample)",
            font=FONT_BODY, fg=TEXT_MUTED, bg=BG_SUB_CARD, anchor="w"
        )
        self.lbl_preview_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.prog_preview = ttk.Progressbar(
            self.f_prev, orient=tk.HORIZONTAL, mode="determinate", maximum=self._preview_duration
        )
        self.prog_preview.pack(fill=tk.X, pady=(6, 0))

        # Results Frame - Expand in remaining middle space
        f_table = tk.Frame(container, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER_MAIN)
        f_table.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(4, 6))

        # Styled Treeview
        style = ttk.Style(self.win)
        style.theme_use("clam")
        style.configure(
            "Search.Treeview",
            background=BG_INPUT,
            foreground=TEXT_DARK,
            fieldbackground=BG_INPUT,
            font=FONT_BODY,
            rowheight=30
        )
        style.configure(
            "Search.Treeview.Heading",
            background=BG_SUB_CARD,
            foreground=TEXT_DARK,
            font=FONT_BODY_BOLD,
            relief="flat",
            padding=4
        )
        style.map(
            "Search.Treeview",
            background=[("selected", "#0284c7")],
            foreground=[("selected", "#ffffff")]
        )

        columns = ("title", "artist", "duration")
        self.tree = ttk.Treeview(
            f_table, columns=columns, show="headings",
            selectmode="browse", style="Search.Treeview",
            height=6
        )
        self.tree.heading("title", text="Song Title / Description")
        self.tree.heading("artist", text="Artist / Channel")
        self.tree.heading("duration", text="Length")

        self.tree.column("title", width=400, anchor="w")
        self.tree.column("artist", width=180, anchor="w")
        self.tree.column("duration", width=75, anchor="center")

        sb = ttk.Scrollbar(f_table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-Button-1>", lambda e: self._do_select())
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Populate rows
        for idx, item in enumerate(self.results):
            self.tree.insert(
                "", tk.END, iid=str(idx),
                values=(item.get("title", ""), item.get("uploader", ""), item.get("duration_str", "--:--"))
            )

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
            if 0 <= idx < len(self.results):
                item = self.results[idx]
                if self._is_previewing or self._is_loading_preview:
                    if item.get("url") != self._preview_active_url:
                        self._stop_preview(reset_status=False)
                        self.lbl_preview_status.config(
                            text=f"Selected: {item.get('title', 'Song')} — Click 'Play Preview' to listen",
                            fg=TEXT_DARK
                        )
                else:
                    self.lbl_preview_status.config(
                        text=f"Selected: {item.get('title', 'Song')} — Click 'Play Preview' to listen",
                        fg=TEXT_DARK
                    )
        except Exception:
            pass

    def _toggle_preview(self):
        if self._is_previewing or self._is_loading_preview:
            self._stop_preview()
            return

        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
            if not (0 <= idx < len(self.results)):
                return
            item = self.results[idx]
            self._start_preview(item)
        except Exception:
            pass

    def _start_preview(self, item):
        url = item.get("url")
        if not url:
            return

        if self.on_preview_play:
            try:
                self.on_preview_play()
            except Exception:
                pass

        self._stop_preview(reset_status=False)
        self._is_loading_preview = True
        self._preview_active_url = url
        self._preview_cancel_event.clear()
        self._preview_request_id += 1
        req_id = self._preview_request_id

        title = item.get("title", "Song")
        self.lbl_preview_status.config(text=f"⏳ Loading 30s preview for \"{title}\"...", fg=COLOR_ACCENT)
        self.lbl_preview_time.config(text="0:00 / 0:30", fg=COLOR_ACCENT)
        self.prog_preview["value"] = 0
        self._style_play_button("⏳ Loading...", COLOR_PAUSE, COLOR_PAUSE_HV, state=tk.NORMAL)
        self.btn_preview_stop.config(state=tk.NORMAL)

        def _on_succ(filepath):
            self._safe_dispatch(self._on_preview_ready, req_id, item, filepath)

        def _on_err(err):
            self._safe_dispatch(self._on_preview_failed, req_id, err)

        threading.Thread(
            target=fetch_preview_worker,
            args=(url, int(self._preview_duration), self._preview_cancel_event, _on_succ, _on_err),
            daemon=True
        ).start()

    def _style_play_button(self, text, bg, hover_bg, state=tk.NORMAL):
        try:
            self.btn_preview_play.config(
                text=text,
                bg=bg,
                activebackground=hover_bg or bg,
                state=state
            )
            self.btn_preview_play.bind("<Enter>", lambda e: self.btn_preview_play.config(bg=hover_bg))
            self.btn_preview_play.bind("<Leave>", lambda e: self.btn_preview_play.config(bg=bg))
        except Exception:
            pass

    def _on_preview_ready(self, req_id, item, filepath):
        if self._closed or req_id != self._preview_request_id:
            return
        self._is_loading_preview = False
        self._is_previewing = True

        title = item.get("title", "Song")
        self.lbl_preview_status.config(text=f"🔊 Playing preview: {title}", fg=COLOR_PLAY)
        self._style_play_button("⏹ Stop Preview", COLOR_STOP, COLOR_STOP_HV, state=tk.NORMAL)
        self.btn_preview_stop.config(state=tk.NORMAL)

        try:
            if self.audio_engine:
                self.audio_engine.load_and_play(filepath, start_sec=0.0)
            else:
                if not pygame.mixer.get_init():
                    pygame.mixer.init()
                pygame.mixer.music.load(filepath)
                pygame.mixer.music.play()
        except Exception as e:
            log_error(f"SearchChoiceDialog._on_preview_ready: {e}")
            self._on_preview_failed(req_id, str(e))
            return

        self._start_timeline_poll()

    def _on_preview_failed(self, req_id, err):
        if self._closed or req_id != self._preview_request_id:
            return
        self._is_loading_preview = False
        self._is_previewing = False
        self._style_play_button("▶ Play Preview", COLOR_PLAY, COLOR_PLAY_HV, state=tk.NORMAL)
        self.btn_preview_stop.config(state=tk.DISABLED)
        self.lbl_preview_status.config(text="⚠️ Preview unavailable for this track (you can still download)", fg=COLOR_STOP)
        self.lbl_preview_time.config(text="--:--", fg=TEXT_MUTED)
        self.prog_preview["value"] = 0

    def _start_timeline_poll(self):
        self._stop_timeline_poll()
        self._poll_timeline()

    def _stop_timeline_poll(self):
        if self._preview_poll_job:
            try:
                self.win.after_cancel(self._preview_poll_job)
            except Exception:
                pass
            self._preview_poll_job = None

    def _poll_timeline(self):
        if self._closed or not self._is_previewing:
            return

        elapsed = 0.0
        is_busy = False
        try:
            if self.audio_engine:
                elapsed = self.audio_engine.current_play_seconds()
                is_busy = self.audio_engine.is_busy()
            else:
                is_busy = pygame.mixer.music.get_busy()
        except Exception:
            pass

        if not is_busy or elapsed >= self._preview_duration:
            self._stop_preview(reset_status=False)
            self.lbl_preview_status.config(text="Preview finished.", fg=TEXT_MUTED)
            self.prog_preview["value"] = 0
            self.lbl_preview_time.config(text=f"0:00 / {format_time(self._preview_duration)}", fg=TEXT_MUTED)
            return

        self.prog_preview["value"] = min(self._preview_duration, elapsed)
        self.lbl_preview_time.config(
            text=f"{format_time(elapsed)} / {format_time(self._preview_duration)}",
            fg=TEXT_DARK
        )

        if not self._closed and self.win.winfo_exists():
            self._preview_poll_job = self.win.after(100, self._poll_timeline)

    def _stop_preview(self, reset_status=True):
        self._preview_cancel_event.set()
        self._preview_request_id += 1
        self._stop_timeline_poll()
        self._is_previewing = False
        self._is_loading_preview = False
        self._preview_active_url = None

        try:
            if self.audio_engine:
                self.audio_engine.stop()
            else:
                if pygame.mixer.get_init():
                    pygame.mixer.music.stop()
                    if hasattr(pygame.mixer.music, "unload"):
                        pygame.mixer.music.unload()
        except Exception:
            pass

        try:
            if self.win.winfo_exists():
                self._style_play_button("▶ Play Preview", COLOR_PLAY, COLOR_PLAY_HV, state=tk.NORMAL)
                self.btn_preview_stop.config(state=tk.DISABLED)
                self.prog_preview["value"] = 0
                self.lbl_preview_time.config(text=f"0:00 / {format_time(self._preview_duration)}", fg=TEXT_MUTED)
                if reset_status:
                    self.lbl_preview_status.config(
                        text="Select a song and click 'Play Preview' to listen (30-sec sample)",
                        fg=TEXT_MUTED
                    )
        except Exception:
            pass

    def _do_select(self):
        self._closed = True
        self._stop_preview(reset_status=False)
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.results):
            chosen = self.results[idx]
            self.chosen_item = chosen
            try:
                self.win.grab_release()
                self.win.destroy()
            except Exception:
                pass
            if self.on_select:
                self.on_select(chosen)

    def _do_cancel(self):
        self._closed = True
        self._stop_preview(reset_status=False)
        try:
            self.win.grab_release()
            self.win.destroy()
        except Exception:
            pass
        if self.on_cancel:
            self.on_cancel()

    def _load_more_results(self):
        if getattr(self, "_loading_more", False) or self._closed:
            return
        self._loading_more = True
        self.btn_more.config(text="Searching...", state=tk.DISABLED)
        self.lbl_preview_status.config(text="Searching for more matches on YouTube...", fg=TEXT_DARK)
        next_count = len(self.results) + 8

        def _worker():
            try:
                from app.services.downloader import search_youtube
                fetched = search_youtube(self.query, max_results=next_count)
                if self._closed:
                    return

                def _apply():
                    self._loading_more = False
                    if self._closed:
                        return
                    self.btn_more.config(text="➕ Show More Results", state=tk.NORMAL)
                    existing_urls = {r.get("url") for r in self.results}
                    new_entries = [it for it in fetched if it.get("url") not in existing_urls]
                    if new_entries:
                        start_pos = len(self.results)
                        for i, item in enumerate(new_entries):
                            self.tree.insert(
                                "", tk.END, iid=str(start_pos + i),
                                values=(item.get("title", ""), item.get("uploader", ""), item.get("duration_str", "--:--"))
                            )
                            self.results.append(item)
                        self.lbl_preview_status.config(
                            text=f"Added {len(new_entries)} more match(es). Double-click or click Download to choose.",
                            fg=TEXT_DARK
                        )
                    else:
                        self.btn_more.config(text="No More Results", state=tk.DISABLED)
                        self.lbl_preview_status.config(text="No additional matches found on YouTube.", fg=TEXT_MUTED)

                self._safe_dispatch(_apply)
            except Exception as e:
                log_error(f"_load_more_results: {e}")
                def _err():
                    self._loading_more = False
                    if not self._closed:
                        self.btn_more.config(text="➕ Show More Results", state=tk.NORMAL)
                        self.lbl_preview_status.config(text="Could not load more results. Try again later.", fg=TEXT_MUTED)
                self._safe_dispatch(_err)

        threading.Thread(target=_worker, daemon=True).start()


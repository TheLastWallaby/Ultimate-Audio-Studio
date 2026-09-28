"""Main Application Coordinator and Window Controller for Ultimate Audio Studio."""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
import queue
import re
import sys
import threading
import tkinter as tk
import traceback
from collections import OrderedDict
from collections.abc import Callable
from tkinter import messagebox, ttk
from types import TracebackType
from typing import Any

import pygame

from app.config import (
    APP_VERSION,
    DEFAULT_PLAYLIST_NAME,
    MUSIC_DIR,
    cleanup_old_executables,
    cleanup_temp_caches,
    ffmpeg_path,
    log_error,
    settings_mgr,
)
from app.controllers import (
    DownloadController,
    ExportController,
    LibraryController,
    PlaybackController,
    PlaylistController,
    UpdateController,
)
from app.core.audio_engine import AudioEngine
from app.core.cache_manager import cache_mgr
from app.core.task_manager import task_mgr
from app.platform_utils import Win32DragDropHandler, already_running, enable_windows_dpi
from app.ui.components import ScrollableFrame, ToolTip, create_button
from app.ui.error_dialog import show_error
from app.ui.features.base import UiCallback
from app.ui.features.clip_editor import ClipEditorMixin
from app.ui.features.download import DownloadMixin
from app.ui.features.export import ExportMixin
from app.ui.features.library import LibraryMixin
from app.ui.features.player import PlayerMixin
from app.ui.features.playlists import PlaylistMixin
from app.ui.features.updates import UpdatesMixin
from app.ui.theme import (
    BG_CARD,
    BG_INPUT,
    BG_ROOT,
    BG_SUB_CARD,
    BORDER_MAIN,
    COLOR_ACCENT,
    COLOR_ACCENT_HV,
    COLOR_BTN_NEUTRAL,
    COLOR_DOWNLOAD,
    COLOR_EXPORT,
    COLOR_PLAY,
    DEFAULT_TEXT_SIZE,
    FONT_APP_TITLE,
    FONT_BODY,
    FONT_BODY_BOLD,
    FONT_BTN_MAIN,
    FONT_STATUS_BAR,
    TEXT_DARK,
    TEXT_SIZES,
    apply_text_size,
    init_fonts,
    next_text_size,
)
from app.ui.views.step1_library import build_step1_view
from app.ui.views.step2_player import build_step2_view
from app.ui.views.step3_export import build_step3_view
from app.ui.waveform_view import WaveformView

STATUS_BAR_BG = "#0f172a"
STATUS_FLASH_MS = 5000


class UltimateAudioStudio(
    LibraryMixin,
    DownloadMixin,
    PlayerMixin,
    ClipEditorMixin,
    PlaylistMixin,
    ExportMixin,
    UpdatesMixin,
):
    """Main window: builds the layout and owns settings, status bar, undo, and shutdown.

    Feature behaviour lives in the mixins under ``app/ui/features``; playback and playlist state
    is owned by the controllers and exposed here through ``AppBase``'s ``ControllerState`` descriptors.
    """

    _saved_geometry: str | None

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"Ultimate Audio Studio v{APP_VERSION}")

        # Responsive geometry. Sizes are in physical pixels, so scale the design size by the
        # Windows display scaling (125%/150%...) and start maximized when it does not fit.
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        ui_scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        want_w, want_h = int(1360 * ui_scale), int(820 * ui_scale)
        target_w = min(want_w, max(800, screen_w - 60))
        target_h = min(want_h, max(560, screen_h - 100))
        self.root.geometry(f"{target_w}x{target_h}")
        self.root.minsize(min(int(900 * ui_scale), target_w), min(int(560 * ui_scale), target_h))
        self._start_maximized = want_w > screen_w - 60 or want_h > screen_h - 100
        self.root.configure(bg=BG_ROOT)
        self.root.option_add("*Font", FONT_BODY)

        self.audio_engine = AudioEngine()

        self._is_shutting_down = False
        self.default_lib_path = os.path.join(MUSIC_DIR, "MyAudioDownloads")
        os.makedirs(self.default_lib_path, exist_ok=True)
        cleanup_temp_caches()
        cleanup_old_executables()
        self._available_update = None

        self.library_folder = self.default_lib_path
        self.library_files = []
        self.visible_files = []
        self._art_cache = OrderedDict()
        self._max_art_cache = 64
        self._current_peaks = []
        self._waveform_loading = False
        self._bar_colors: list[str] = []
        self._current_cover_img = None
        self._last_waveform_sig = None
        self._selection_debounce_timer = None
        self._search_debounce_timer = None
        self._waveform_req_id = 0
        self._art_req_id = 0
        self._last_dl_ui_time = 0.0
        self._active_waveform_cancel = threading.Event()
        self._active_waveform_proc = None
        self._waveform_queue = queue.Queue()
        self._art_queue = queue.Queue()
        self._ui_callback_queue = queue.Queue()
        self._scrubbed_while_paused = False
        self._resize_timer = None
        self.loop_clip = tk.BooleanVar(value=False)
        self._start_worker_threads()
        self.library_ctrl = LibraryController(self)
        self.playback_ctrl = PlaybackController(self, self.audio_engine)
        self.playlist_ctrl = PlaylistController(self)
        self.export_ctrl = ExportController(self)
        self.download_ctrl = DownloadController(self)
        self.update_ctrl = UpdateController(self)
        self._undo_callback = None
        self._undo_timer: str | None = None
        self._current_loudness = None
        self._pending_play_token = None
        self._library_meta_gen = 0
        self._exporting = False
        self._saving_clip = False
        self._importing = False
        self._timer_settings_save: str | None = None

        self.selected_file_path = None
        self.track_duration = 0.0
        self.clip_start_sec = 0.0
        self.clip_end_sec = 0.0

        self.playlists = {DEFAULT_PLAYLIST_NAME: []}
        self.active_playlist_name = DEFAULT_PLAYLIST_NAME
        self.playlist_files = []

        self.is_playing_main = False
        self.is_playing_playlist = False
        self.is_paused = False
        self.playlist_index = 0
        self.previewing_clip = False
        self.clip_end_time = 0
        self.play_start_offset = 0
        self.play_clock_origin = None
        self.play_guard_until = 0
        self._updating_ui = False
        self._progress_dragging = False
        self._busy = False
        self._usb_map = {}
        self._usb_fs_map = {}
        self.waveform_zoomed = False
        self._dragging_marker = None
        self._is_audition_slice = False
        self._audition_slice_file = None

        self._load_settings()
        init_fonts(self.root, self.text_size)
        self.repeat_playlist = tk.BooleanVar(value=getattr(self, "_saved_repeat", False))
        self.soften_clip = tk.BooleanVar(value=getattr(self, "_saved_soften", True))
        self.fade_choice_var = tk.StringVar(value=getattr(self, "_saved_fade_choice", "1.5s (Standard)"))
        self.even_volume = tk.BooleanVar(value=getattr(self, "_saved_even", True))
        self.auto_level_playback = tk.BooleanVar(value=getattr(self, "_saved_auto_level_playback", False))
        self.audio_engine.set_auto_level(self.auto_level_playback.get())
        self.usb_choice = tk.StringVar(value="")
        self._unmuted_volume = getattr(self, "_saved_volume", 80)
        self._timer_check_ffmpeg = None
        self._timer_update_check = None
        self._timer_watch_library = None
        self._timer_hotplug_debounce = None
        self._timer_status_flash: str | None = None

        if getattr(self, "_saved_geometry", None):
            try:
                self.root.geometry(self._saved_geometry)
            except Exception:
                pass
        elif self._start_maximized:
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass

        self._build_layout()
        self.build_column_1()
        self.build_column_2()
        self.build_column_3()
        self._balance_columns()
        self.refresh_usb_drives()
        self._setup_drag_and_drop()
        self._install_exception_hooks()
        self._watch_settings_changes()

        task_mgr.submit_task(self.library_ctrl.recover_stranded_deletes, self.library_folder)
        self.load_playlists()
        self.refresh_library()
        self.set_status("Ready. Select a song on the left to play or trim.")
        self._timer_check_ffmpeg = self.root.after(300, self._check_ffmpeg)
        self._timer_update_check = self.root.after(1500, self._check_for_updates_on_launch)
        self._timer_watch_library = self.root.after(12000, self._watch_library)
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self._drain_ui_callbacks()
        self.monitor_audio()

    def _drain_ui_callbacks(self) -> None:
        """Drain and execute queued worker callbacks on Tkinter main thread."""
        if getattr(self, "_is_shutting_down", False):
            return
        while True:
            try:
                item = self._ui_callback_queue.get_nowait()
            except queue.Empty:
                break
            try:
                cb, args = item
                cb(*args)
            except Exception as e:
                import traceback

                log_error(f"_drain_ui_callbacks: {e}\n{traceback.format_exc()}")
            finally:
                self._ui_callback_queue.task_done()
        if not getattr(self, "_is_shutting_down", False):
            try:
                if hasattr(self, "root") and self.root and self.root.winfo_exists():
                    self._drain_timer = self.root.after(25, self._drain_ui_callbacks)
            except Exception:
                pass

    def _safe_after(self, delay: int, callback: UiCallback, *args: Any) -> None:
        """Safely schedule a callback on Tkinter main thread, guarding against Python 3.13 cross-thread errors."""
        if getattr(self, "_is_shutting_down", False):
            return
        if threading.current_thread() is threading.main_thread():
            if delay <= 0:
                callback(*args)
            else:
                try:
                    if hasattr(self, "root") and self.root and self.root.winfo_exists():
                        self.root.after(delay, callback, *args)
                except Exception:
                    callback(*args)
        else:
            if delay <= 0:
                self._ui_callback_queue.put((callback, args))
            else:

                def _delayed_dispatch() -> None:
                    try:
                        if hasattr(self, "root") and self.root and self.root.winfo_exists():
                            self.root.after(delay, callback, *args)
                        else:
                            callback(*args)
                    except Exception:
                        callback(*args)

                self._ui_callback_queue.put((_delayed_dispatch, ()))

    def _install_exception_hooks(self) -> None:
        def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
            if getattr(self, "_is_shutting_down", False):
                return
            if issubclass(exc_type, (KeyboardInterrupt, tk.TclError)):
                return
            details = "".join(traceback.format_exception(exc_type, exc, tb))
            log_error(details)
            try:
                show_error(
                    self.root,
                    "Something went wrong",
                    "The app hit an unexpected problem.\n\n"
                    "You can keep using it. If something looks wrong, close and reopen the app.",
                    details=f"{exc_type.__name__}: {exc}",
                )
            except Exception:
                pass

        self.root.report_callback_exception = hook

    def _load_settings(self) -> None:
        try:
            s = settings_mgr.get_settings()
            if s.library_folder and os.path.isdir(s.library_folder):
                self.library_folder = s.library_folder
            self._saved_volume = s.volume
            self._saved_repeat = s.repeat_playlist
            self._saved_soften = s.soften_clip
            self._saved_fade_choice = s.fade_choice
            self._saved_even = s.even_volume
            self._saved_auto_level_playback = s.auto_level_playback
            self.text_size = s.text_size if s.text_size in TEXT_SIZES else DEFAULT_TEXT_SIZE
            self._saved_active_playlist = s.active_playlist
            geo = s.geometry
            if geo:
                m = re.match(r"^(\d+)x(\d+)", str(geo))
                if m and int(m.group(1)) >= 1020 and int(m.group(2)) >= 600:
                    self._saved_geometry = geo
                else:
                    self._saved_geometry = None
            else:
                self._saved_geometry = None
        except Exception:
            self._saved_volume = 80
            self._saved_repeat = False
            self._saved_soften = True
            self._saved_fade_choice = "1.5s (Standard)"
            self._saved_even = True
            self._saved_auto_level_playback = False
            self._saved_geometry = None
            self.text_size = DEFAULT_TEXT_SIZE
            self._saved_active_playlist = ""

    def _schedule_settings_save(self, *_args: object) -> None:
        """Persist settings shortly after any change (debounced), so a crash loses nothing."""
        if getattr(self, "_is_shutting_down", False):
            return
        if self._timer_settings_save:
            try:
                self.root.after_cancel(self._timer_settings_save)
            except Exception:
                pass
        self._timer_settings_save = self.root.after(1000, self._save_settings)

    def _watch_settings_changes(self) -> None:
        for var in (self.repeat_playlist, self.soften_clip, self.fade_choice_var, self.even_volume):
            var.trace_add("write", self._schedule_settings_save)
        self.root.bind("<Configure>", self._on_root_configure, add="+")

    def _on_root_configure(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is self.root:
            self._schedule_settings_save()

    def _save_settings(self) -> None:
        # Cancel any pending debounced save; when called directly (e.g. from on_close) that timer
        # would otherwise fire later against a destroyed window.
        timer, self._timer_settings_save = self._timer_settings_save, None
        if timer is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(timer)
        try:
            volume = 80
            if hasattr(self, "scale_volume"):
                volume = int(self.scale_volume.get())

            settings_mgr.update_settings(
                library_folder=self.library_folder,
                volume=volume,
                repeat_playlist=bool(self.repeat_playlist.get()) if hasattr(self, "repeat_playlist") else False,
                soften_clip=bool(self.soften_clip.get()) if hasattr(self, "soften_clip") else True,
                fade_choice=self.fade_choice_var.get() if hasattr(self, "fade_choice_var") else "1.5s (Standard)",
                even_volume=bool(self.even_volume.get()) if hasattr(self, "even_volume") else True,
                auto_level_playback=bool(self.auto_level_playback.get())
                if hasattr(self, "auto_level_playback")
                else False,
                geometry=self.root.geometry(),
                text_size=getattr(self, "text_size", DEFAULT_TEXT_SIZE),
                active_playlist=self.active_playlist_name or "",
            )
        except Exception as e:
            log_error(f"_save_settings: {e}")

    def _setup_drag_and_drop(self) -> None:
        self._dnd_handler = Win32DragDropHandler(
            self.root,
            self._handle_dropped_files,
            is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
            device_change_callback=self._on_usb_hotplug,
        )
        self._dnd_handler.setup()

    def _teardown_drag_and_drop(self) -> None:
        if hasattr(self, "_dnd_handler") and self._dnd_handler:
            self._dnd_handler.teardown()

    def request_close(self) -> None:
        """Window close button: warn before stopping a download, export, clip save, or import."""
        reason = self.busy_reason()
        if reason and not messagebox.askyesno(
            "Still Working",
            f"Ultimate Audio Studio is still busy with {reason}.\n\n"
            "If you close now, it will be stopped and may be left unfinished "
            "(for example, a USB drive with only some of the songs).\n\n"
            "Close anyway?",
            icon=messagebox.WARNING,
            default=messagebox.NO,
            parent=self.root,
        ):
            self.set_status(f"Still working on {reason}. You can close the app when it has finished.", icon="⏳")
            return
        self.on_close()

    def on_close(self) -> None:
        self._is_shutting_down = True
        self.download_ctrl.cancel()
        task_mgr.shutdown(wait=False, cancel_futures=True)
        self._teardown_drag_and_drop()
        if hasattr(self, "_waveform_queue"):
            try:
                self._waveform_queue.put_nowait(None)
            except Exception:
                pass
        if hasattr(self, "_art_queue"):
            try:
                self._art_queue.put_nowait(None)
            except Exception:
                pass
        self._save_settings()
        self.save_playlists()
        if hasattr(self, "library_ctrl"):
            try:
                self.library_ctrl.flush_pending_trash()
            except Exception:
                pass
        cache_mgr.flush()
        self._release_audio_file()
        try:
            pygame.quit()
        except Exception:
            pass
        for timer_attr in (
            "_drain_timer",
            "_monitor_timer",
            "_selection_debounce_timer",
            "_resize_timer",
            "_timer_check_ffmpeg",
            "_timer_update_check",
            "_timer_watch_library",
            "_timer_hotplug_debounce",
            "_timer_settings_save",
            "_search_debounce_timer",
            "_undo_timer",
            "_timer_status_flash",
        ):
            tid = getattr(self, timer_attr, None)
            if tid:
                try:
                    self.root.after_cancel(tid)
                except Exception:
                    pass
                setattr(self, timer_attr, None)

        if hasattr(self, "waveform_view") and hasattr(self.waveform_view, "_resize_timer"):
            wtid = getattr(self.waveform_view, "_resize_timer", None)
            if wtid:
                try:
                    self.root.after_cancel(wtid)
                except Exception:
                    pass
                self.waveform_view._resize_timer = None

        cleanup_temp_caches()
        self.root.destroy()

    def busy_reason(self) -> str | None:
        """Describe work that must not be interrupted (e.g. by an update restart), or None."""
        if self.download_ctrl.is_downloading:
            return "the current download"
        if self._exporting:
            return "the playlist export"
        if self._saving_clip:
            return "saving your clip"
        if self._importing:
            return "adding songs to your Library"
        return None

    def prepare_for_restart(self) -> None:
        """Persist everything before the updater swaps the executable and exits the process."""
        self.stop_audio()
        self._save_settings()
        self.save_playlists()
        try:
            self.library_ctrl.flush_pending_trash()
        except Exception as e:
            log_error(f"prepare_for_restart flush_pending_trash: {e}")
        cache_mgr.flush()

    def _balance_columns(self) -> None:
        """Share the window width in proportion to what each column needs, so the widest column
        (usually the player) does not need horizontal scrolling while the others have spare room."""
        self.root.update_idletasks()
        for idx, col in enumerate((self.col1, self.col2, self.col3)):
            self._columns_body.columnconfigure(idx, weight=max(1, col.body.winfo_reqwidth()), uniform="col")

    def cycle_text_size(self) -> None:
        """Switch Normal -> Large -> Extra Large and resize the whole app immediately."""
        self.text_size = next_text_size(self.text_size)
        apply_text_size(self.root, self.text_size)
        self._balance_columns()
        if hasattr(self, "btn_text_size"):
            self.btn_text_size.config(text=f"🔠 Text Size: {self.text_size}")
        self.set_status(f"Text size set to {self.text_size}.", icon="🔠")
        self._schedule_settings_save()

    def _build_layout(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG_CARD, foreground=TEXT_DARK, font=FONT_BODY)
        style.configure(
            "TCombobox",
            fieldbackground=BG_INPUT,
            background=COLOR_BTN_NEUTRAL,
            foreground=TEXT_DARK,
            bordercolor=BORDER_MAIN,
            padding=5,
        )
        style.map("TCombobox", fieldbackground=[("readonly", BG_INPUT)], foreground=[("readonly", TEXT_DARK)])
        style.configure(
            "Download.Horizontal.TProgressbar",
            troughcolor="#e2e8f0",
            background=COLOR_DOWNLOAD,
            bordercolor=BORDER_MAIN,
            lightcolor=COLOR_DOWNLOAD,
            darkcolor=COLOR_DOWNLOAD,
        )
        style.configure(
            "Export.Horizontal.TProgressbar",
            troughcolor="#e2e8f0",
            background=COLOR_EXPORT,
            bordercolor=BORDER_MAIN,
            lightcolor=COLOR_EXPORT,
            darkcolor=COLOR_EXPORT,
        )
        style.configure("TCheckbutton", background=BG_CARD, foreground=TEXT_DARK, font=FONT_BODY_BOLD)
        style.map("TCheckbutton", background=[("active", BG_CARD)], foreground=[("active", TEXT_DARK)])
        style.configure("TRadiobutton", background=BG_CARD, foreground=TEXT_DARK, font=FONT_BODY_BOLD)
        style.map("TRadiobutton", background=[("active", BG_CARD)], foreground=[("active", TEXT_DARK)])

        self.root.option_add("*TCombobox*Listbox.background", BG_INPUT)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT_DARK)
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLOR_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", FONT_BODY)
        self.root.option_add("*Checkbutton.background", BG_CARD)
        self.root.option_add("*Checkbutton.foreground", TEXT_DARK)
        self.root.option_add("*Checkbutton.activebackground", BG_CARD)
        self.root.option_add("*Checkbutton.activeforeground", TEXT_DARK)
        self.root.option_add("*Checkbutton.selectColor", "#ffffff")
        self.root.option_add("*Radiobutton.background", BG_CARD)
        self.root.option_add("*Radiobutton.foreground", TEXT_DARK)
        self.root.option_add("*Radiobutton.activebackground", BG_CARD)
        self.root.option_add("*Radiobutton.activeforeground", TEXT_DARK)
        self.root.option_add("*Radiobutton.selectColor", "#ffffff")

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        body = tk.Frame(self.root, bg=BG_ROOT)
        body.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 6))
        # Column widths are balanced to their content in _balance_columns() once they are built.
        self._columns_body = body
        for idx in range(3):
            body.columnconfigure(idx, weight=1, uniform="col")
        body.rowconfigure(0, weight=1)

        # Each column scrolls when the window is too small for it (small laptops, 125-150% scaling,
        # or a larger text size), so no button can ever be cut off.
        col_opts = dict(
            relief=tk.SOLID, borderwidth=1, highlightbackground=BORDER_MAIN, highlightthickness=1, padx=14, pady=14
        )
        self.col1 = ScrollableFrame(body, bg=BG_CARD, **col_opts)
        self.col2 = ScrollableFrame(body, bg=BG_CARD, **col_opts)
        self.col3 = ScrollableFrame(body, bg=BG_CARD, **col_opts)
        self.col1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.col2.grid(row=0, column=1, sticky="nsew", padx=4)
        self.col3.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        # Bottom Status Bar
        f_status = tk.Frame(self.root, bg=STATUS_BAR_BG, padx=16, pady=8)
        f_status.grid(row=1, column=0, sticky="ew")
        self._status_bar = f_status

        self.lbl_status_icon = tk.Label(f_status, text="ℹ️", font=FONT_STATUS_BAR, bg=STATUS_BAR_BG, fg="#38bdf8")
        self.lbl_status_icon.pack(side=tk.LEFT, padx=(0, 8))

        self.status = tk.Label(f_status, text="", font=FONT_STATUS_BAR, anchor="w", bg=STATUS_BAR_BG, fg="#f8fafc")
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_version_check = tk.Button(
            f_status,
            text=f"v{APP_VERSION} • Check for Updates",
            font=FONT_STATUS_BAR,
            bg="#1e293b",
            fg="#94a3b8",
            activebackground="#334155",
            activeforeground="#f8fafc",
            relief=tk.FLAT,
            padx=8,
            pady=2,
            cursor="hand2",
            command=self.check_for_updates_manual,
        )
        self.btn_version_check.pack(side=tk.RIGHT, padx=(8, 0))

        self.btn_text_size = tk.Button(
            f_status,
            text=f"🔠 Text Size: {self.text_size}",
            font=FONT_STATUS_BAR,
            bg="#1e293b",
            fg="#f8fafc",
            activebackground="#334155",
            activeforeground="#f8fafc",
            relief=tk.FLAT,
            padx=8,
            pady=2,
            cursor="hand2",
            command=self.cycle_text_size,
        )
        self.btn_text_size.pack(side=tk.RIGHT, padx=(8, 0))
        ToolTip(self.btn_text_size, "Make all text in the app bigger or smaller")

        self.btn_update_badge = tk.Button(
            f_status,
            text="⭐ Update Available",
            font=FONT_STATUS_BAR,
            bg=COLOR_ACCENT,
            fg="#ffffff",
            activebackground=COLOR_ACCENT_HV,
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=10,
            pady=2,
            cursor="hand2",
            command=self._open_update_dialog,
        )

        self.btn_undo = tk.Button(
            f_status,
            text="↩️ Undo",
            font=FONT_STATUS_BAR,
            bg="#f59e0b",
            fg="#0f172a",
            activebackground="#d97706",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=10,
            pady=2,
            cursor="hand2",
            command=self._on_undo_click,
        )

    def show_undo(self, message: str, callback: Callable[[], None], timeout_sec: float = 8) -> None:
        """Display an Undo button in the status bar for accidental deletions/removals."""
        self._undo_callback = callback
        if self._undo_timer:
            try:
                self.root.after_cancel(self._undo_timer)
            except Exception:
                pass
            self._undo_timer = None

        if hasattr(self, "btn_undo"):
            self.btn_undo.pack(side=tk.RIGHT, padx=(8, 4))
        self.set_status(message, icon="🗑️")

        def _expire() -> None:
            self.hide_undo(flush=True)

        self._undo_timer = self.root.after(int(timeout_sec * 1000), _expire)

    def hide_undo(self, flush: bool = False) -> None:
        """Hide the Undo button and optionally flush pending deletions to disk/trash."""
        if self._undo_timer:
            try:
                self.root.after_cancel(self._undo_timer)
            except Exception:
                pass
            self._undo_timer = None
        if hasattr(self, "btn_undo") and self.btn_undo.winfo_ismapped():
            self.btn_undo.pack_forget()
        self._undo_callback = None
        if flush and hasattr(self, "library_ctrl"):
            self.library_ctrl.flush_pending_trash()

    def _on_undo_click(self) -> None:
        """Trigger the active undo callback."""
        cb = self._undo_callback
        self.hide_undo(flush=False)
        if cb:
            cb()

    def set_status(self, text: str, icon: str = "ℹ️") -> None:
        self.status.config(text=text)
        self.lbl_status_icon.config(text=icon)

    def notify_success(self, text: str, icon: str = "✅") -> None:
        """Confirm finished work in the status bar, briefly shown in green so it is noticed without a pop-up."""
        self.set_status(text, icon=icon)
        flashed = (self._status_bar, self.status, self.lbl_status_icon)
        for widget in flashed:
            widget.config(bg=COLOR_PLAY)
        if self._timer_status_flash:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._timer_status_flash)

        def _restore() -> None:
            self._timer_status_flash = None
            for widget in flashed:
                with contextlib.suppress(tk.TclError):
                    widget.config(bg=STATUS_BAR_BG)

        self._timer_status_flash = self.root.after(STATUS_FLASH_MS, _restore)

    def set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        self.root.config(cursor="watch" if busy else "")
        if status:
            self.set_status(status, icon="⏳" if busy else "ℹ️")

    def _check_ffmpeg(self) -> None:
        if not os.path.exists(ffmpeg_path):
            messagebox.showwarning(
                "Missing Helper File",
                "The app could not find ffmpeg.exe.\n\n"
                "Downloading, clipping, and CD export will not work until that file is included.",
            )

    def build_column_1(self) -> None:
        build_step1_view(self.col1.body, self)

    def build_column_2(self) -> None:
        build_step2_view(self.col2.body, self)
        self.waveform_view = WaveformView(self.canvas_waveform, self)

    def build_column_3(self) -> None:
        build_step3_view(self.col3.body, self)

    def show_help(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Ultimate Audio Studio - Help Guide")
        win.geometry("640x600")
        win.minsize(480, 400)
        win.configure(bg=BG_CARD)

        f_content = tk.Frame(win, bg=BG_CARD, padx=16, pady=16)
        f_content.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            f_content, text="How to Use Ultimate Audio Studio", font=FONT_APP_TITLE, fg=COLOR_ACCENT, bg=BG_CARD
        ).pack(anchor="w", pady=(0, 10))

        help_text = (
            "STEP 1: GETTING MUSIC\n"
            "• Search or Paste: Type any song and artist, or paste a YouTube link, then click 'Download MP3'.\n"
            "• Add Music from PC: Click 'Add Music from PC' or drag & drop audio files directly into the window.\n"
            "• Search Library: Use the search bar to filter songs by title or artist.\n\n"
            "STEP 2: PLAYING & CLIPPING\n"
            "• Play / Pause: Click the green PLAY button.\n"
            "• Interactive Markers: Drag the Blue Start marker or Red End marker directly on the waveform.\n"
            "• Zoom Clip: Click 'Zoom Clip' to focus in on your selected clip range for fine-tuning.\n"
            "• Test Clip: Audition your clip range with live volume boost and smooth fade.\n"
            "• Save Clip: Saves your trimmed clip safely into your library with automatic original backup.\n\n"
            "STEP 3: PLAYLISTS & EXPORT\n"
            "• Playlists: Organize favorite songs with 'New Playlist'. Change the order by dragging a song "
            "up or down in the list, or with ▲ Up and ▼ Down.\n"
            "• USB Flash Drive: Plug in a USB flash drive, choose it from the list, and click 'Export Playlist Now'. "
            "Songs are normalized and an M3U playlist file is created automatically for car stereos! "
            "Click 'Eject' before unplugging the drive.\n"
            "• CD Burn: Creates CD-ready audio tracks on your Desktop in 'My_CD_Burn_Folder'. Burn them with "
            "Windows Media Player's 'Burn' tab set to 'Audio CD' (not File Explorer's 'Send to').\n\n"
            "HANDY EXTRAS\n"
            "• Text Size: Click '🔠 Text Size' at the bottom of the window to make all text bigger.\n"
            "• Keyboard: Press Tab to move between buttons (a dotted outline shows where you are), then Enter to click."
        )

        txt = tk.Text(
            f_content,
            font=FONT_BODY,
            wrap="word",
            bg=BG_SUB_CARD,
            fg=TEXT_DARK,
            padx=10,
            pady=10,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=BORDER_MAIN,
        )
        txt.insert("1.0", help_text)
        txt.config(state=tk.DISABLED)
        txt.pack(fill=tk.BOTH, expand=True, pady=(0, 12))

        create_button(
            f_content, "Close Guide", win.destroy, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_MAIN, pady=5
        ).pack(anchor="e")


def _close_splash() -> None:
    """Close the start-up splash screen shown by the packaged .exe (no-op when run from source)."""
    if not getattr(sys, "frozen", False):
        return
    try:
        importlib.import_module("pyi_splash").close()
    except (ImportError, RuntimeError) as e:  # built without a splash, or it is already closed
        logging.getLogger(__name__).debug("splash close skipped: %s", e)


def main() -> None:
    """Application entry point."""
    if already_running():
        _close_splash()
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Already Running", "Ultimate Audio Studio is already running.")
        sys.exit(0)

    enable_windows_dpi()
    root = tk.Tk()
    _app = UltimateAudioStudio(root)  # keep a reference for the lifetime of the main loop
    root.after_idle(_close_splash)  # once the window is drawn, so there is no blank gap
    root.mainloop()


if __name__ == "__main__":
    main()

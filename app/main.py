"""Main Application Coordinator and Window Controller for Ultimate Audio Studio."""

import os
import sys
import time
import json
import re
import queue
import threading
import hashlib
import traceback
from collections import OrderedDict
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import pygame

try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None

from app.config import (
    ffmpeg_path, ffprobe_path, BASE_PATH, MUSIC_DIR, PLAYLISTS_PATH,
    SETTINGS_PATH, ERROR_LOG_PATH, YT_CACHE_DIR, COVER_CACHE_DIR,
    PREVIEW_CACHE_DIR, DEFAULT_PLAYLIST_NAME, AUDIO_EXTS, YOUTUBE_RE,
    cleanup_temp_caches, format_time, log_error, atomic_save_json,
    sanitize_filename, run_ffmpeg, APP_VERSION, cleanup_old_executables,
    settings_mgr
)
from app.platform_utils import (
    enable_windows_dpi, already_running, list_removable_drives,
    Win32DragDropHandler, get_desktop_dir
)
from app.core.audio_engine import AudioEngine, SONG_END_EVENT
from app.core.cache_manager import cache_mgr
from app.core.task_manager import task_mgr
from app.controllers import (
    LibraryController, PlaybackController, PlaylistController,
    ExportController, DownloadController, UpdateController
)
from app.core.metadata import probe_audio_duration, extract_album_art, read_track_metadata
from app.core.waveform import extract_waveform_peaks
from app.ui.search_dialog import SearchChoiceDialog
from app.services.clipper import clip_audio_worker
from app.ui.theme import (
    FONT_FAMILY, FONT_APP_TITLE, FONT_STEP_BADGE, FONT_SECTION_HDR,
    FONT_BODY, FONT_BODY_BOLD, FONT_TIME_LARGE, FONT_HERO_TITLE,
    FONT_HERO_ARTIST, FONT_BTN_MAIN, FONT_BTN_SUB, FONT_STATUS_BAR,
    BG_ROOT, BG_CARD, BG_SUB_CARD, BG_INPUT, BORDER_MAIN,
    TEXT_DARK, TEXT_MEDIUM, TEXT_MUTED,
    COLOR_PLAY, COLOR_PLAY_HV, COLOR_PAUSE, COLOR_PAUSE_HV,
    COLOR_STOP, COLOR_STOP_HV, COLOR_ACCENT, COLOR_ACCENT_HV,
    COLOR_EXPORT, COLOR_EXPORT_HV, COLOR_DOWNLOAD, COLOR_DOWNLOAD_HV,
    COLOR_BTN_NEUTRAL, COLOR_BTN_NEUTRAL_HV,
    COLOR_DANGER_BG, COLOR_DANGER_TEXT, COLOR_DANGER_HV
)
from app.ui.components import ToolTip, create_button, scrolled_listbox, draw_placeholder_cover
from app.ui.waveform_view import WaveformView
from app.ui.views.step1_library import build_step1_view
from app.ui.views.step2_player import build_step2_view
from app.ui.views.step3_export import build_step3_view


class UltimateAudioStudio:
    """Master application coordinator managing state, UI views, background workers, and playback."""

    def __init__(self, root):
        self.root = root
        self.root.title(f"Ultimate Audio Studio v{APP_VERSION}")

        # Responsive geometry
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        target_w = min(1360, max(1040, screen_w - 60))
        target_h = min(820, max(620, screen_h - 100))
        self.root.geometry(f"{target_w}x{target_h}")
        self.root.minsize(1020, 600)
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
        self._bar_colors = []
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
        self._undo_timer = None

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
        self._download_cancel = self.download_ctrl.cancel_event
        self._usb_map = {}
        self._usb_fs_map = {}
        self.waveform_zoomed = False
        self._dragging_marker = None
        self._is_audition_slice = False
        self._audition_slice_file = None

        self._load_settings()
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

        if getattr(self, "_saved_geometry", None):
            try:
                self.root.geometry(self._saved_geometry)
            except Exception:
                pass

        self._build_layout()
        self.build_column_1()
        self.build_column_2()
        self.build_column_3()
        self.refresh_usb_drives()
        self._setup_drag_and_drop()
        self._install_exception_hooks()

        self.load_playlists()
        self.refresh_library()
        self.set_status("Ready. Select a song on the left to play or trim.")
        self._timer_check_ffmpeg = self.root.after(300, self._check_ffmpeg)
        self._timer_update_check = self.root.after(1500, self._check_for_updates_on_launch)
        self._timer_watch_library = self.root.after(12000, self._watch_library)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._drain_ui_callbacks()
        self.monitor_audio()

    # Consolidated Controller State Properties (Single Source of Truth)
    @property
    def is_playing_main(self):
        return self.playback_ctrl.is_playing_main if hasattr(self, "playback_ctrl") else False

    @is_playing_main.setter
    def is_playing_main(self, val):
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.is_playing_main = bool(val)

    @property
    def is_playing_playlist(self):
        return self.playback_ctrl.is_playing_playlist if hasattr(self, "playback_ctrl") else False

    @is_playing_playlist.setter
    def is_playing_playlist(self, val):
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.is_playing_playlist = bool(val)

    @property
    def is_paused(self):
        return self.playback_ctrl.is_paused if hasattr(self, "playback_ctrl") else False

    @is_paused.setter
    def is_paused(self, val):
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.is_paused = bool(val)

    @property
    def previewing_clip(self):
        return self.playback_ctrl.previewing_clip if hasattr(self, "playback_ctrl") else False

    @previewing_clip.setter
    def previewing_clip(self, val):
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.previewing_clip = bool(val)

    @property
    def clip_end_time(self):
        return self.playback_ctrl.clip_end_time if hasattr(self, "playback_ctrl") else 0.0

    @clip_end_time.setter
    def clip_end_time(self, val):
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.clip_end_time = float(val)

    @property
    def play_start_offset(self):
        return self.playback_ctrl.play_start_offset if hasattr(self, "playback_ctrl") else 0.0

    @play_start_offset.setter
    def play_start_offset(self, val):
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.play_start_offset = float(val)

    @property
    def play_clock_origin(self):
        return self.playback_ctrl.play_clock_origin if hasattr(self, "playback_ctrl") else None

    @play_clock_origin.setter
    def play_clock_origin(self, val):
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.play_clock_origin = val

    @property
    def play_guard_until(self):
        return self.playback_ctrl.play_guard_until if hasattr(self, "playback_ctrl") else 0.0

    @play_guard_until.setter
    def play_guard_until(self, val):
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.play_guard_until = float(val)

    @property
    def playlists(self):
        return self.playlist_ctrl.playlists if hasattr(self, "playlist_ctrl") else {DEFAULT_PLAYLIST_NAME: []}

    @playlists.setter
    def playlists(self, val):
        if hasattr(self, "playlist_ctrl"):
            self.playlist_ctrl.playlists = val

    @property
    def active_playlist_name(self):
        return self.playlist_ctrl.active_playlist_name if hasattr(self, "playlist_ctrl") else DEFAULT_PLAYLIST_NAME

    @active_playlist_name.setter
    def active_playlist_name(self, val):
        if hasattr(self, "playlist_ctrl"):
            self.playlist_ctrl.active_playlist_name = str(val)

    @property
    def playlist_index(self):
        return self.playlist_ctrl.playlist_index if hasattr(self, "playlist_ctrl") else 0

    @playlist_index.setter
    def playlist_index(self, val):
        if hasattr(self, "playlist_ctrl"):
            self.playlist_ctrl.playlist_index = int(val)

    def _drain_ui_callbacks(self):
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

    def _safe_after(self, delay, callback, *args):
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
                def _delayed_dispatch():
                    try:
                        if hasattr(self, "root") and self.root and self.root.winfo_exists():
                            self.root.after(delay, callback, *args)
                        else:
                            callback(*args)
                    except Exception:
                        callback(*args)
                self._ui_callback_queue.put((_delayed_dispatch, ()))

    def _release_audio_file(self):
        """Safely stops and unloads pygame mixer handles to prevent Windows file locking."""
        self.audio_engine.release_audio_file()

    def _install_exception_hooks(self):
        def hook(exc_type, exc, tb):
            if getattr(self, "_is_shutting_down", False):
                return
            if issubclass(exc_type, (KeyboardInterrupt, tk.TclError)):
                return
            details = "".join(traceback.format_exception(exc_type, exc, tb))
            log_error(details)
            try:
                messagebox.showerror("Something went wrong", "The app hit an unexpected problem.\n\nDetails were saved to error log.")
            except Exception:
                pass
        self.root.report_callback_exception = hook

    def _load_settings(self):
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

    def _save_settings(self):
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
                auto_level_playback=bool(self.auto_level_playback.get()) if hasattr(self, "auto_level_playback") else False,
                geometry=self.root.geometry(),
            )
        except Exception:
            pass

    def _setup_drag_and_drop(self):
        self._dnd_handler = Win32DragDropHandler(
            self.root, self._handle_dropped_files,
            is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
            device_change_callback=self._on_usb_hotplug
        )
        self._dnd_handler.setup()

    def _on_usb_hotplug(self):
        if getattr(self, "_is_shutting_down", False):
            return
        if self._timer_hotplug_debounce:
            try:
                self.root.after_cancel(self._timer_hotplug_debounce)
            except Exception:
                pass
        def _do_hotplug():
            if getattr(self, "_is_shutting_down", False):
                return
            old_keys = set(self._usb_map.keys())
            self.refresh_usb_drives()
            new_keys = set(self._usb_map.keys())
            added = new_keys - old_keys
            if added:
                new_drive = list(added)[0]
                self.set_status(f"USB Flash Drive detected: {new_drive}", icon="💾")
            elif old_keys - new_keys:
                self.set_status("USB Flash Drive unplugged.", icon="ℹ️")
        self._timer_hotplug_debounce = self.root.after(800, _do_hotplug)

    def _teardown_drag_and_drop(self):
        if hasattr(self, "_dnd_handler") and self._dnd_handler:
            self._dnd_handler.teardown()

    def _handle_dropped_files(self, paths):
        if getattr(self, "_is_shutting_down", False):
            return
        planned, dest_exists = self.library_ctrl.build_import_plan(paths, self.library_folder)
        if not planned and not dest_exists:
            messagebox.showinfo(
                "No Audio Files",
                "No compatible audio songs (.mp3, .wav, .m4a, .ogg, .flac) were found in the dropped files."
            )
            return
        if not planned and dest_exists:
            self.set_status("Dropped audio files are already in your Library.")
            return

        if dest_exists:
            replace = messagebox.askyesno(
                "Replace Files?",
                f"{len(dest_exists)} of the dropped song(s) already exist in your Library.\n\nDo you want to replace them?"
            )
            if not replace:
                planned = [(s, d) for (s, d) in planned if not os.path.exists(d)]

        if not planned:
            return

        self.set_busy(True, f"Adding {len(planned)} dropped song(s) to Library...")
        self.library_ctrl.import_external_files(
            planned,
            is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
            on_done=lambda cnt: self._safe_after(0, self._on_copy_external_done, cnt)
        )

    def on_close(self):
        self._is_shutting_down = True
        self._download_cancel.set()
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
        self._release_audio_file()
        try:
            pygame.quit()
        except Exception:
            pass
        for timer_attr in (
            "_drain_timer", "_monitor_timer", "_selection_debounce_timer",
            "_resize_timer", "_timer_check_ffmpeg", "_timer_update_check",
            "_timer_watch_library", "_timer_hotplug_debounce"
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

    def _build_layout(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG_CARD, foreground=TEXT_DARK, font=FONT_BODY)
        style.configure("TCombobox", fieldbackground=BG_INPUT, background=COLOR_BTN_NEUTRAL, foreground=TEXT_DARK, bordercolor=BORDER_MAIN, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", BG_INPUT)], foreground=[("readonly", TEXT_DARK)])
        style.configure("Download.Horizontal.TProgressbar", troughcolor="#e2e8f0", background=COLOR_DOWNLOAD, bordercolor=BORDER_MAIN, lightcolor=COLOR_DOWNLOAD, darkcolor=COLOR_DOWNLOAD)
        style.configure("Export.Horizontal.TProgressbar", troughcolor="#e2e8f0", background=COLOR_EXPORT, bordercolor=BORDER_MAIN, lightcolor=COLOR_EXPORT, darkcolor=COLOR_EXPORT)
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
        body.columnconfigure(0, weight=1, uniform="col")
        body.columnconfigure(1, weight=1, uniform="col")
        body.columnconfigure(2, weight=1, uniform="col")
        body.rowconfigure(0, weight=1)

        self.col1 = tk.Frame(body, bg=BG_CARD, relief=tk.SOLID, borderwidth=1, highlightbackground=BORDER_MAIN, highlightthickness=1, padx=14, pady=14)
        self.col2 = tk.Frame(body, bg=BG_CARD, relief=tk.SOLID, borderwidth=1, highlightbackground=BORDER_MAIN, highlightthickness=1, padx=14, pady=14)
        self.col3 = tk.Frame(body, bg=BG_CARD, relief=tk.SOLID, borderwidth=1, highlightbackground=BORDER_MAIN, highlightthickness=1, padx=14, pady=14)
        self.col1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.col2.grid(row=0, column=1, sticky="nsew", padx=4)
        self.col3.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        # Bottom Status Bar
        f_status = tk.Frame(self.root, bg="#0f172a", padx=16, pady=8)
        f_status.grid(row=1, column=0, sticky="ew")

        self.lbl_status_icon = tk.Label(f_status, text="ℹ️", font=FONT_STATUS_BAR, bg="#0f172a", fg="#38bdf8")
        self.lbl_status_icon.pack(side=tk.LEFT, padx=(0, 8))

        self.status = tk.Label(
            f_status, text="", font=FONT_STATUS_BAR, anchor="w",
            bg="#0f172a", fg="#f8fafc"
        )
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_version_check = tk.Button(
            f_status, text=f"v{APP_VERSION} • Check for Updates",
            font=FONT_STATUS_BAR, bg="#1e293b", fg="#94a3b8",
            activebackground="#334155", activeforeground="#f8fafc",
            relief=tk.FLAT, padx=8, pady=2, cursor="hand2",
            command=self.check_for_updates_manual
        )
        self.btn_version_check.pack(side=tk.RIGHT, padx=(8, 0))

        self.btn_update_badge = tk.Button(
            f_status, text="⭐ Update Available",
            font=FONT_STATUS_BAR, bg=COLOR_ACCENT, fg="#ffffff",
            activebackground=COLOR_ACCENT_HV, activeforeground="#ffffff",
            relief=tk.FLAT, padx=10, pady=2, cursor="hand2",
            command=self._open_update_dialog
        )

        self.btn_undo = tk.Button(
            f_status, text="↩️ Undo",
            font=FONT_STATUS_BAR, bg="#f59e0b", fg="#0f172a",
            activebackground="#d97706", activeforeground="#ffffff",
            relief=tk.FLAT, padx=10, pady=2, cursor="hand2",
            command=self._on_undo_click
        )

    def show_undo(self, message, callback, timeout_sec=8):
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

        def _expire():
            self.hide_undo(flush=True)

        self._undo_timer = self.root.after(int(timeout_sec * 1000), _expire)

    def hide_undo(self, flush=False):
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

    def _on_undo_click(self):
        """Trigger the active undo callback."""
        cb = self._undo_callback
        self.hide_undo(flush=False)
        if cb:
            cb()

    def _check_for_updates_on_launch(self):
        """Silently check for updates in a background thread after launch."""
        if getattr(self, "_is_shutting_down", False):
            return
        self.update_ctrl.check_on_launch(
            on_update_found=lambda rel: self._safe_after(0, self._handle_update_found, rel)
        )

    def _handle_update_found(self, release_info):
        if getattr(self, "_is_shutting_down", False):
            return
        self._available_update = release_info
        tag = release_info.get("tag_name", "New")
        if hasattr(self, "btn_update_badge") and self.btn_update_badge.winfo_exists():
            self.btn_update_badge.config(text=f"⭐ Update to {tag}")
            self.btn_update_badge.pack(side=tk.RIGHT, padx=(8, 0))
        # If running as a frozen executable, open the update dialog
        if getattr(sys, "frozen", False):
            self._open_update_dialog(auto_start=True)

    def check_for_updates_manual(self):
        """User-initiated update check with explicit UI feedback."""
        if getattr(self, "_is_checking_updates_manual", False) or getattr(self, "_is_shutting_down", False):
            return
        self._is_checking_updates_manual = True
        self.set_status("Checking for application updates...", icon="🔄")
        if hasattr(self, "btn_version_check") and self.btn_version_check.winfo_exists():
            self.btn_version_check.config(text=f"v{APP_VERSION} (Checking...)", state=tk.DISABLED)

        self.update_ctrl.check_manual(
            on_result=lambda has_update, release_info, err: self._safe_after(
                0, self._handle_manual_update_result, has_update, release_info, err
            )
        )

    def _handle_manual_update_result(self, has_update, release_info, err):
        self._is_checking_updates_manual = False
        if hasattr(self, "btn_version_check") and self.btn_version_check.winfo_exists():
            self.btn_version_check.config(text=f"v{APP_VERSION} • Check for Updates", state=tk.NORMAL)

        if getattr(self, "_is_shutting_down", False):
            return

        if has_update and release_info:
            tag = release_info.get("tag_name", "New")
            self.set_status(f"Update available: {tag}", icon="⭐")
            self._available_update = release_info
            if hasattr(self, "btn_update_badge") and self.btn_update_badge.winfo_exists():
                self.btn_update_badge.config(text=f"⭐ Update to {tag}")
                self.btn_update_badge.pack(side=tk.RIGHT, padx=(8, 0))
            self._open_update_dialog(auto_start=False)
        elif err:
            self.set_status("Update check failed.", icon="⚠️")
            messagebox.showwarning("Update Check", f"{err}")
        else:
            self.set_status(f"Ultimate Audio Studio is up to date (v{APP_VERSION}).", icon="✅")
            messagebox.showinfo("Update Check", f"You are running the latest version of Ultimate Audio Studio (v{APP_VERSION}).")

    def _open_update_dialog(self, auto_start=False):
        if getattr(self, "_is_shutting_down", False):
            return
        self.update_ctrl.open_dialog(self.root, release_info=self._available_update, auto_start=auto_start)

    def set_status(self, text, icon="ℹ️"):
        self.status.config(text=text)
        self.lbl_status_icon.config(text=icon)

    def set_busy(self, busy, status=None):
        self._busy = busy
        self.root.config(cursor="watch" if busy else "")
        if status:
            self.set_status(status, icon="⏳" if busy else "ℹ️")

    def _check_ffmpeg(self):
        if not os.path.exists(ffmpeg_path):
            messagebox.showwarning(
                "Missing Helper File",
                "The app could not find ffmpeg.exe.\n\n"
                "Downloading, clipping, and CD export will not work until that file is included."
            )

    def build_column_1(self):
        build_step1_view(self.col1, self)

    def build_column_2(self):
        build_step2_view(self.col2, self)
        self.waveform_view = WaveformView(self.canvas_waveform, self)

    def build_column_3(self):
        build_step3_view(self.col3, self)

    def _draw_placeholder_cover(self):
        if hasattr(self, "canvas_cover"):
            draw_placeholder_cover(self.canvas_cover)

    def _start_worker_threads(self):
        threading.Thread(target=self._waveform_worker_loop, daemon=True).start()
        threading.Thread(target=self._art_worker_loop, daemon=True).start()

    def _waveform_worker_loop(self):
        while True:
            try:
                item = self._waveform_queue.get()
                if item is None or getattr(self, "_is_shutting_down", False):
                    self._waveform_queue.task_done()
                    break
                if len(item) == 3:
                    filepath, req_id, cancel_evt = item
                else:
                    filepath, req_id = item
                    cancel_evt = None
                if req_id != self._waveform_req_id or getattr(self, "_is_shutting_down", False) or (cancel_evt and cancel_evt.is_set()):
                    self._waveform_queue.task_done()
                    continue

                def _on_spawn(p):
                    self._active_waveform_proc = p

                peaks = extract_waveform_peaks(filepath, n_bars=220, cancel_event=cancel_evt, on_process_spawned=_on_spawn)
                self._active_waveform_proc = None

                if cancel_evt and cancel_evt.is_set():
                    self._waveform_queue.task_done()
                    continue

                if peaks and any(val > 0.001 for val in peaks):
                    cache_mgr.set_peaks(filepath, peaks)
                if req_id == self._waveform_req_id and not getattr(self, "_is_shutting_down", False):
                    def on_done(p=peaks, f=filepath, r=req_id):
                        if r == self._waveform_req_id and not getattr(self, "_is_shutting_down", False):
                            self._waveform_loading = False
                            if self.selected_file_path == f:
                                self._current_peaks = p if (p and any(val > 0.001 for val in p)) else []
                                if hasattr(self, "playback_ctrl"):
                                    self.playback_ctrl.update_auto_level_for_peaks(self._current_peaks)
                                self._render_waveform(full_redraw=True)
                    self._safe_after(0, on_done)
                self._waveform_queue.task_done()
            except Exception as e:
                self._active_waveform_proc = None
                log_error(f"_waveform_worker_loop: {e}")

    def _art_worker_loop(self):
        while True:
            try:
                item = self._art_queue.get()
                if item is None or getattr(self, "_is_shutting_down", False):
                    self._art_queue.task_done()
                    break
                filepath, req_id = item
                if req_id != self._art_req_id or getattr(self, "_is_shutting_down", False):
                    self._art_queue.task_done()
                    continue

                file_hash = hashlib.md5(os.path.abspath(filepath).encode("utf-8", errors="ignore"), usedforsecurity=False).hexdigest()
                out_png = os.path.join(COVER_CACHE_DIR, f"{file_hash}_art.png")
                success = extract_album_art(filepath, out_png)

                def on_art_done(s=success, out=out_png, f=filepath, r=req_id):
                    if r != self._art_req_id or getattr(self, "_is_shutting_down", False):
                        return
                    img = None
                    if s and os.path.exists(out):
                        try:
                            img = tk.PhotoImage(file=out)
                        except Exception:
                            img = None
                    while len(self._art_cache) >= self._max_art_cache:
                        self._art_cache.popitem(last=False)
                    self._art_cache[f] = img
                    if self.selected_file_path == f:
                        if img:
                            self._current_cover_img = img
                            self.canvas_cover.delete("all")
                            self.canvas_cover.create_image(0, 0, anchor="nw", image=img)
                        else:
                            self._draw_placeholder_cover()
                self._safe_after(0, on_art_done)
                self._art_queue.task_done()
            except Exception as e:
                log_error(f"_art_worker_loop: {e}")

    def _load_album_art(self, filepath):
        self._art_req_id += 1
        current_req = self._art_req_id

        if filepath in self._art_cache:
            img = self._art_cache[filepath]
            self._art_cache.move_to_end(filepath)
            if img:
                self._current_cover_img = img
                self.canvas_cover.delete("all")
                self.canvas_cover.create_image(0, 0, anchor="nw", image=img)
                return
            else:
                self._draw_placeholder_cover()
                return

        self._art_queue.put((filepath, current_req))

    def _load_waveform(self, filepath):
        self._waveform_req_id += 1
        current_req = self._waveform_req_id

        # Eagerly cancel any active FFmpeg extraction
        if self._active_waveform_proc:
            try:
                self._active_waveform_proc.terminate()
            except Exception:
                pass
            self._active_waveform_proc = None
        self._active_waveform_cancel.set()
        self._active_waveform_cancel = threading.Event()

        # Drain obsolete items from queue
        while not self._waveform_queue.empty():
            try:
                self._waveform_queue.get_nowait()
                self._waveform_queue.task_done()
            except Exception:
                break

        # Check persistent cache first
        disk_peaks = cache_mgr.get_peaks(filepath)
        if disk_peaks and any(val > 0.001 for val in disk_peaks):
            self._current_peaks = disk_peaks
            self._waveform_loading = False
            if hasattr(self, "playback_ctrl"):
                self.playback_ctrl.update_auto_level_for_peaks(disk_peaks)
            self._render_waveform(full_redraw=True)
            return

        self._current_peaks = []
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.update_auto_level_for_peaks([])
        self._waveform_loading = True
        self._render_waveform(full_redraw=True)
        self._waveform_queue.put((filepath, current_req, self._active_waveform_cancel))

    def toggle_waveform_zoom(self):
        if hasattr(self, "waveform_view"):
            self.waveform_view.toggle_zoom()

    def _render_waveform(self, full_redraw=False):
        if hasattr(self, "waveform_view"):
            self.waveform_view.render(full_redraw=full_redraw)

    def on_gain_change(self, val):
        v = float(val)
        sign = "+" if v > 0 else ""
        self.lbl_gain.config(text=f"Volume Boost: {sign}{v:.1f} dB")
        if abs(v) > 0.05:
            self.lbl_gain.config(fg=COLOR_DOWNLOAD)
        else:
            self.lbl_gain.config(fg=TEXT_DARK)

    def reset_gain(self):
        self.scale_gain.set(0.0)
        self.lbl_gain.config(text="Volume Boost: 0 dB", fg=TEXT_DARK)

    def nudge_clip_start(self, delta):
        if not self.selected_file_path:
            return
        new_val = self.playback_ctrl.nudge_start(delta, self.clip_start_sec, self.clip_end_sec)
        self.clip_start_sec = new_val
        self._updating_ui = True
        is_frac = not float(new_val).is_integer()
        self.lbl_start_time.config(text=f"Start: {format_time(new_val, include_fractional=is_frac)}")
        self._updating_ui = False
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip start set to {format_time(new_val, include_fractional=is_frac)}.")

    def nudge_clip_end(self, delta):
        if not self.selected_file_path:
            return
        max_dur = self.track_duration if self.track_duration > 0 else 999999
        new_val = self.playback_ctrl.nudge_end(delta, self.clip_start_sec, self.clip_end_sec, max_dur)
        self.clip_end_sec = new_val
        self._updating_ui = True
        is_frac = not float(new_val).is_integer()
        self.lbl_end_time.config(text=f"End: {format_time(new_val, include_fractional=is_frac)}")
        self._updating_ui = False
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip end set to {format_time(new_val, include_fractional=is_frac)}.")

    def load_playlists(self):
        self.playlist_ctrl.load(PLAYLISTS_PATH)
        self.refresh_playlist_dropdown()
        self.refresh_playlist_listbox()

    def save_playlists(self):
        self.playlist_ctrl.save(PLAYLISTS_PATH)

    def refresh_playlist_dropdown(self):
        names = self.playlist_ctrl.get_playlist_names()
        self.cmb_playlists["values"] = names
        if self.active_playlist_name in names:
            self.playlist_var.set(self.active_playlist_name)
        elif names:
            self.active_playlist_name = names[0]
            self.playlist_ctrl.active_playlist_name = names[0]
            self.playlist_var.set(names[0])

    def refresh_playlist_listbox(self):
        self.listbox_pl.delete(0, tk.END)
        self.playlist_files = self.playlist_ctrl.get_active_tracks(self.active_playlist_name)
        for idx, f in enumerate(self.playlist_files, 1):
            name = os.path.basename(f)
            prefix = "▶ " if (self.is_playing_playlist and idx - 1 == self.playlist_index) else f"{idx:02d}. "
            if not os.path.exists(f):
                self.listbox_pl.insert(tk.END, f"{prefix}⚠️ [Missing] {name}")
                continue
            dur = self._cached_duration(f)
            dur_str = f" [{format_time(dur)}]" if dur > 0 else ""
            self.listbox_pl.insert(tk.END, f"{prefix}{name}{dur_str}")

    def on_playlist_selected(self, _event=None):
        name = self.playlist_var.get()
        if name in self.playlists:
            self.active_playlist_name = name
            self.playlist_ctrl.active_playlist_name = name
            if self.is_playing_playlist:
                self.stop_audio()
            self.refresh_playlist_listbox()
            self.set_status(f"Switched to playlist: {name}")

    def create_playlist(self):
        name = simpledialog.askstring("New Playlist", "Enter a name for the new playlist:", parent=self.root)
        if not name:
            return
        name = sanitize_filename(name).strip()
        if not name:
            return messagebox.showwarning("Invalid Name", "Please enter a valid playlist name.")
        if not self.playlist_ctrl.create_playlist(name):
            return messagebox.showwarning("Already Exists", f"A playlist named '{name}' already exists.")
        self.save_playlists()
        self.refresh_playlist_dropdown()
        self.refresh_playlist_listbox()
        self.set_status(f"Created new playlist: {name}")

    def rename_playlist(self):
        current = self.active_playlist_name
        name = simpledialog.askstring("Rename Playlist", f"Rename '{current}' to:", initialvalue=current, parent=self.root)
        if not name or name == current:
            return
        name = sanitize_filename(name).strip()
        if not name:
            return messagebox.showwarning("Invalid Name", "Please enter a valid playlist name.")
        if not self.playlist_ctrl.rename_playlist(current, name):
            return messagebox.showwarning("Already Exists", f"A playlist named '{name}' already exists.")
        self.save_playlists()
        self.refresh_playlist_dropdown()
        self.refresh_playlist_listbox()
        self.set_status(f"Renamed playlist to: {name}")

    def delete_playlist(self):
        current = self.active_playlist_name
        if len(self.playlists) <= 1:
            return messagebox.showwarning("Cannot Delete", "You must keep at least one playlist.")
        ok = messagebox.askyesno("Delete Playlist", f"Delete playlist '{current}'?\n\n(This will not delete your audio files.)")
        if not ok:
            return
        if not self.playlist_ctrl.delete_playlist(current):
            return messagebox.showwarning("Cannot Delete", "Could not delete playlist.")
        self.save_playlists()
        self.refresh_playlist_dropdown()
        self.refresh_playlist_listbox()
        self.set_status(f"Deleted playlist '{current}'.")

    def refresh_library(self, select_name=None):
        self.library_files = self.library_ctrl.scan_files(self.library_folder)
        self.library_ctrl.update_search_index(
            self.library_folder, self.library_files, get_metadata_fn=self._cached_metadata
        )
        self.apply_library_filter(select_name)

    def apply_library_filter(self, select_name=None):
        query = self.entry_search.get().strip().lower() if hasattr(self, "entry_search") else ""
        if hasattr(self, "library_ctrl"):
            self.visible_files = self.library_ctrl.filter_files(
                self.library_files, self.library_folder, query, get_metadata_fn=self._cached_metadata
            )
        else:
            self.visible_files = list(self.library_files)

        self.listbox_lib.delete(0, tk.END)
        select_idx = None
        for idx, f in enumerate(self.visible_files):
            f_path = os.path.join(self.library_folder, f)
            dur = self._cached_duration(f_path)
            dur_str = f" [{format_time(dur)}]" if dur > 0 else ""
            self.listbox_lib.insert(tk.END, f"{f}{dur_str}")
            if select_name and f == select_name:
                select_idx = idx

        if select_idx is not None:
            self.listbox_lib.selection_set(select_idx)
            self.listbox_lib.see(select_idx)

    def on_search_key_release(self, event=None):
        if getattr(self, "_search_debounce_timer", None) is not None:
            try:
                self.root.after_cancel(self._search_debounce_timer)
            except Exception:
                pass
        self._search_debounce_timer = self.root.after(150, self.apply_library_filter)

    def clear_search(self):
        if getattr(self, "_search_debounce_timer", None) is not None:
            try:
                self.root.after_cancel(self._search_debounce_timer)
            except Exception:
                pass
            self._search_debounce_timer = None
        if hasattr(self, "entry_search"):
            self.entry_search.delete(0, tk.END)
            self.apply_library_filter()

    def _cached_duration(self, path):
        if not path or not os.path.exists(path):
            return 0.0
        dur = cache_mgr.get_duration(path)
        if dur is not None:
            return dur
        meta = read_track_metadata(path)
        dur = meta.get("duration", 0.0)
        cache_mgr.set_duration(path, dur)
        return dur

    def _cached_metadata(self, path):
        if not path or not os.path.exists(path):
            return {"title": "", "artist": "", "duration": 0.0}
        cached_meta = cache_mgr.get_metadata(path)
        if cached_meta is not None:
            return cached_meta
        data = read_track_metadata(path)
        cache_mgr.set_metadata(path, data)
        return data.to_dict() if hasattr(data, "to_dict") else dict(data)

    def change_folder(self):
        f = filedialog.askdirectory(initialdir=self.library_folder, title="Choose Your Music Library Folder", parent=self.root)
        if f and os.path.isdir(f):
            self.library_folder = f
            self._save_settings()
            self.refresh_library()
            self.set_status(f"Music folder changed to: {f}")

    def open_library_folder(self):
        try:
            os.startfile(self.library_folder)
        except Exception as e:
            messagebox.showerror("Error", f"Could not open folder:\n{e}")

    def add_external_file(self):
        files = filedialog.askopenfilenames(
            title="Select Music Files to Import",
            filetypes=[("Audio Files", "*.mp3;*.wav;*.m4a;*.ogg;*.flac"), ("All Files", "*.*")],
            parent=self.root
        )
        if not files:
            return
        planned = []
        for src in files:
            dest = os.path.join(self.library_folder, os.path.basename(src))
            if os.path.abspath(src).lower() != os.path.abspath(dest).lower():
                planned.append((src, dest))

        if not planned:
            return self.set_status("Selected files are already in your Library.")

        self.set_busy(True, f"Copying {len(planned)} song(s) into Library...")
        task_mgr.submit_task(self._copy_external_worker, planned)

    def _copy_external_worker(self, planned):
        copied = 0
        for src, dest in planned:
            if getattr(self, "_is_shutting_down", False):
                break
            try:
                import shutil
                shutil.copy2(src, dest)
                copied += 1
            except Exception as e:
                log_error(f"copy_external: {e}")

        def _done():
            self.set_busy(False)
            self.refresh_library()
            self.set_status(f"Added {copied} song(s) to your Library.")
            messagebox.showinfo("Import Complete", f"Successfully added {copied} song(s) to your Library!")
        self._safe_after(0, _done)

    def paste_youtube_link(self):
        try:
            txt = self.root.clipboard_get().strip()
            self.entry_url.delete(0, tk.END)
            self.entry_url.insert(0, txt)
            if YOUTUBE_RE.search(txt):
                self.set_status("YouTube link pasted! Click 'Download MP3' to get the song.")
            else:
                self.set_status("Text pasted into download field.")
        except Exception:
            self.set_status("Clipboard is empty or does not contain text.")

    def _on_url_focus(self, _event=None):
        try:
            txt = self.root.clipboard_get().strip()
            if YOUTUBE_RE.search(txt) and not self.entry_url.get().strip():
                self.entry_url.delete(0, tk.END)
                self.entry_url.insert(0, txt)
                self.set_status("Detected YouTube link from clipboard! Click 'Download MP3'.")
        except Exception:
            pass

    def cancel_download(self):
        self.download_ctrl.cancel()
        self.btn_download.config(text="⬇ Download MP3", state=tk.NORMAL)
        self.btn_cancel_dl.config(state=tk.DISABLED)
        self.prog_download.pack_forget()
        self.lbl_dl_metrics.pack_forget()
        self.set_busy(False, "Cancelling download...")

    def _watch_library(self):
        if not getattr(self, "_is_shutting_down", False):
            try:
                current_count = len(self.library_files)
                on_disk = len([f for f in os.listdir(self.library_folder) if f.lower().endswith(AUDIO_EXTS)])
                if on_disk != current_count:
                    self.refresh_library()
            except Exception:
                pass
            if hasattr(self, "root") and self.root and self.root.winfo_exists():
                self._timer_watch_library = self.root.after(12000, self._watch_library)

    def _update_clip_length_label(self):
        dur = max(0.0, self.clip_end_sec - self.clip_start_sec)
        is_frac = not float(dur).is_integer() or not float(self.clip_start_sec).is_integer() or not float(self.clip_end_sec).is_integer()
        self.lbl_clip_len.config(text=f"Clip: {format_time(dur, include_fractional=is_frac)}")

    def _prog_label(self, current):
        return f"{format_time(current)} / {format_time(self.track_duration)}"

    def skip_by(self, seconds):
        if not self.selected_file_path:
            return
        new_pos = self.playback_ctrl.skip_by(seconds, self.track_duration)
        self._updating_ui = True
        self.scale_progress.set(new_pos)
        self.lbl_prog_time.config(text=self._prog_label(new_pos))
        self._updating_ui = False
        self._render_waveform()
        self.play_start_offset = new_pos
        if self.is_paused:
            self._scrubbed_while_paused = True

    def refresh_usb_drives(self):
        drives = list_removable_drives()
        self._usb_map = {label: path for path, label, _ in drives}
        self._usb_fs_map = {label: fs for _, label, fs in drives}
        options = list(self._usb_map.keys())
        self.cmb_usb["values"] = options
        if options:
            self.cmb_usb.current(0)
            self.set_status(f"Found {len(options)} USB flash drive(s).")
        else:
            self.usb_choice.set("No USB drives detected")
            self.set_status("No USB flash drives found. Plug in a USB drive and click 🔄.")
        if hasattr(self, "btn_usb_eject") and self.btn_usb_eject:
            self.btn_usb_eject.config(state=tk.NORMAL if options else tk.DISABLED)

    def eject_selected_usb(self):
        choice = self.usb_choice.get()
        if not choice or choice not in self._usb_map:
            messagebox.showinfo("No Drive", "Please select a connected USB drive to safely eject.")
            return
        drive_path = self._usb_map[choice]
        ok, msg = self.export_ctrl.eject_usb_drive(drive_path)
        self.refresh_usb_drives()
        if ok:
            messagebox.showinfo("Safe to Remove Hardware", f"The USB drive ({choice}) was safely ejected.\n\nYou may now unplug it.")
            self.set_status(f"USB drive {choice} safely ejected.")
        else:
            messagebox.showwarning("Ejection Failed", f"Could not eject drive ({choice}):\n{msg}\n\nPlease ensure no open files or Explorer windows are accessing it.")

    def rename_library_file(self):
        sel = self.listbox_lib.curselection()
        if not sel or sel[0] >= len(self.visible_files):
            return messagebox.showwarning("Select a Song", "Please click a song in the library listbox first.")
        old_name = self.visible_files[sel[0]]
        old_path = os.path.join(self.library_folder, old_name)
        stem, ext = os.path.splitext(old_name)

        new_stem = simpledialog.askstring("Rename Song", "Enter new name for this song:", initialvalue=stem, parent=self.root)
        if not new_stem or new_stem.strip() == stem:
            return
        new_name = sanitize_filename(new_stem) + ext
        new_path = os.path.join(self.library_folder, new_name)

        if os.path.exists(new_path) and new_path.lower() != old_path.lower():
            return messagebox.showwarning("File Exists", f"A file named '{new_name}' already exists in your library.")

        was_playing_renamed = (self.selected_file_path == old_path and (self.is_playing_main or self.is_playing_playlist))
        if was_playing_renamed:
            self.stop_audio()

        self._release_audio_file()
        time.sleep(0.05)

        try:
            new_path = self.library_ctrl.rename_file(old_path, new_name, self.playlists)
            self.save_playlists()
            self.refresh_playlist_listbox()
            self.refresh_library(select_name=new_name)
            if self.selected_file_path == old_path:
                self._load_track_ui(new_path, new_name)
            self.set_status(f"Renamed song to: {new_name}")
        except Exception as e:
            log_error(f"rename_library_file: {e}")
            messagebox.showerror("Rename Failed", f"Could not rename this file:\n{e}")

    def delete_library_file(self):
        sel = self.listbox_lib.curselection()
        if not sel or sel[0] >= len(self.visible_files):
            return messagebox.showwarning("Select a Song", "Please click a song in the library listbox first.")
        name = self.visible_files[sel[0]]
        path = os.path.join(self.library_folder, name)

        ok = messagebox.askyesno(
            "Delete Song",
            f"Are you sure you want to delete '{name}'?\n\nIt will be moved safely to your Windows Recycle Bin."
        )
        if not ok:
            return

        if self.selected_file_path == path:
            self.stop_audio(user=True)
            self.selected_file_path = None
            self.lbl_selected.config(text="No song selected")
            self.lbl_selected_artist.config(text="Click a song on the left to start")
            self._draw_placeholder_cover()
            self._current_peaks = []
            self._render_waveform(full_redraw=True)

        self._release_audio_file()
        time.sleep(0.05)

        try:
            self.library_ctrl.stage_delete_with_undo(path, self.playlists)
            self.save_playlists()
            self.refresh_playlist_listbox()
            self.refresh_library()
            self.show_undo(
                f"Moved '{name}' to Recycle Bin.",
                callback=self._undo_delete_file,
                timeout_sec=8
            )
        except Exception as e:
            log_error(f"delete_library_file: {e}")
            messagebox.showerror("Delete Failed", f"Could not delete this file:\n{e}")

    def _undo_delete_file(self):
        try:
            if self.library_ctrl.undo_delete(self.playlists):
                self.save_playlists()
                self.refresh_playlist_listbox()
                self.refresh_library()
                self.set_status("Restored deleted song.", icon="↩️")
        except Exception as e:
            log_error(f"_undo_delete_file: {e}")

    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title("Ultimate Audio Studio - Help Guide")
        win.geometry("560x520")
        win.minsize(480, 400)
        win.configure(bg=BG_CARD)

        f_content = tk.Frame(win, bg=BG_CARD, padx=16, pady=16)
        f_content.pack(fill=tk.BOTH, expand=True)

        tk.Label(f_content, text="How to Use Ultimate Audio Studio", font=FONT_APP_TITLE, fg=COLOR_ACCENT, bg=BG_CARD).pack(anchor="w", pady=(0, 10))

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
            "• Playlists: Organize favorite songs with 'New Playlist' and reorder tracks using ▲ Up and ▼ Down.\n"
            "• USB Flash Drive: Plug in a USB flash drive, choose it from the list, and click 'Export Playlist Now'. "
            "Songs are normalized and an M3U playlist file is created automatically for car stereos!\n"
            "• CD Burn: Creates CD-ready WAV audio tracks on your Desktop in 'My_CD_Burn_Folder'."
        )

        txt = tk.Text(f_content, font=FONT_BODY, wrap="word", bg=BG_SUB_CARD, fg=TEXT_DARK, padx=10, pady=10, relief=tk.FLAT, highlightthickness=1, highlightbackground=BORDER_MAIN)
        txt.insert("1.0", help_text)
        txt.config(state=tk.DISABLED)
        txt.pack(fill=tk.BOTH, expand=True, pady=(0, 12))

        create_button(f_content, "Close Guide", win.destroy, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_MAIN, pady=5).pack(anchor="e")

    def _update_download_progress(self, pct, speed="", eta="", finished=False):
        if finished:
            self.prog_download["value"] = 100
            self.lbl_dl_metrics.config(text="Converting to MP3 & embedding cover art...")
        else:
            self.prog_download["value"] = pct
            parts = [f"{pct:.1f}%"]
            if speed:
                parts.append(speed)
            if eta:
                parts.append(f"ETA {eta}")
            self.lbl_dl_metrics.config(text=" • ".join(parts))

    def start_download(self):
        query = self.entry_url.get().strip()
        if not query:
            return messagebox.showwarning("Nothing to Download", "Type a song name or paste a YouTube link first.")
        if not os.path.exists(ffmpeg_path):
            return messagebox.showerror("Cannot Download", "ffmpeg.exe is missing, so audio cannot be converted.")

        target_url, is_search = self.download_ctrl.resolve_query(query)
        if not is_search:
            if self.download_ctrl.is_playlist(target_url):
                choice = messagebox.askyesnocancel(
                    "YouTube Playlist Detected",
                    "This link points to a YouTube playlist.\n\n"
                    "• Click 'Yes' to download all songs in this playlist\n"
                    "• Click 'No' to download only this single video\n"
                    "• Click 'Cancel' to abort"
                )
                if choice is None:
                    return
                elif choice is True:
                    self._start_playlist_download(target_url)
                    return
            # Direct URL: download immediately without search dialog
            self._start_download_url(target_url)
            return

        # Search query: fetch matching options first and let user select
        self.btn_download.config(text="Searching...", state=tk.DISABLED)
        self.btn_cancel_dl.config(state=tk.NORMAL)
        self.set_busy(True, f'Searching YouTube for "{query}"...')

        self.download_ctrl.start_search(
            query, 6,
            on_success=lambda results: self._safe_after(0, self._handle_search_results, query, results),
            on_error=lambda err: self._safe_after(0, self._handle_search_error, err)
        )

    def _start_playlist_download(self, url):
        self.btn_download.config(text="Scanning...", state=tk.DISABLED)
        self.btn_cancel_dl.config(state=tk.NORMAL)
        self.set_busy(True, "Scanning playlist tracks...")
        self.prog_download.pack(fill=tk.X, pady=(4, 2))
        self.prog_download["value"] = 0
        self.lbl_dl_metrics.pack(fill=tk.X)
        self.lbl_dl_metrics.config(text="Fetching playlist info...")

        def _worker():
            info = self.download_ctrl.probe_playlist(url)
            if self.download_ctrl.cancel_event.is_set():
                self._safe_after(0, self._download_cancelled)
                return
            if not info or not info.get("entries"):
                self._safe_after(0, self._download_error, "Could not find any downloadable tracks in playlist.")
                return

            entries = info["entries"]
            title = info.get("title", "Playlist")
            total = len(entries)
            self._safe_after(0, lambda: self.set_status(f"Starting batch download of {total} tracks from '{title}'..."))

            def _on_start(idx, tot, track_title):
                self._safe_after(0, lambda: self.set_status(f"Downloading [{idx}/{tot}]: {track_title}", icon="⬇"))
                self._safe_after(0, lambda: self.lbl_dl_metrics.config(text=f"Track {idx}/{tot}: {track_title[:35]}"))

            def _on_prog(idx, tot, pct, speed, eta):
                def _ui():
                    overall_pct = ((idx - 1) + (pct / 100.0)) / max(tot, 1) * 100.0
                    self.prog_download["value"] = overall_pct
                    parts = [f"Track {idx}/{tot}"]
                    if speed: parts.append(speed)
                    if eta: parts.append(f"ETA {eta}")
                    self.lbl_dl_metrics.config(text=" • ".join(parts))
                self._safe_after(0, _ui)

            def _on_fin(idx, tot, fname):
                self._safe_after(0, lambda: self.refresh_library(select_name=fname))

            def _on_complete(downloaded_files, tot):
                self._safe_after(0, self._playlist_download_success, downloaded_files, tot)

            def _on_canc():
                self._safe_after(0, self._download_cancelled)

            def _on_err(err):
                self._safe_after(0, self._download_error, err)

            self.download_ctrl.start_playlist_download(
                entries, self.library_folder,
                on_track_start=_on_start,
                on_track_progress=_on_prog,
                on_track_finished=_on_fin,
                on_batch_complete=_on_complete,
                on_cancelled=_on_canc,
                on_error=_on_err
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _playlist_download_success(self, downloaded_files, total):
        self.entry_url.delete(0, tk.END)
        self.btn_download.config(text="⬇ Download MP3", state=tk.NORMAL)
        self.btn_cancel_dl.config(state=tk.DISABLED)
        self.prog_download.pack_forget()
        self.lbl_dl_metrics.pack_forget()
        self.set_busy(False)
        self.refresh_library()
        count = len(downloaded_files)
        self.set_status(f"Playlist download complete: {count}/{total} songs saved to Library.", icon="✓")
        messagebox.showinfo(
            "Playlist Download Complete",
            f"Successfully downloaded {count} of {total} songs from the playlist into your Music Library!"
        )

    def _handle_search_results(self, query, results):
        self.btn_download.config(text="⬇ Download MP3", state=tk.NORMAL)
        self.btn_cancel_dl.config(state=tk.DISABLED)
        self.set_busy(False)
        if not results:
            messagebox.showinfo(
                "No Matches Found",
                f'No results found on YouTube for "{query}".\n\n'
                "Try including both the artist and song title, or paste a direct YouTube link."
            )
            return

        SearchChoiceDialog(
            self.root,
            query,
            results,
            on_select=lambda item: self._start_download_url(item["url"], item.get("title")),
            on_cancel=lambda: self.set_status("Ready"),
            audio_engine=self.audio_engine,
            on_preview_play=self._on_search_preview_play
        )

    def _on_search_preview_play(self):
        """Pause active playback in main window when preview starts, marking reload needed."""
        if (self.is_playing_main or self.is_playing_playlist) and not self.is_paused:
            self.pause_audio()
            self._scrubbed_while_paused = True

    def _handle_search_error(self, err):
        self.btn_download.config(text="⬇ Download MP3", state=tk.NORMAL)
        self.btn_cancel_dl.config(state=tk.DISABLED)
        self.set_busy(False, "Search failed.")
        messagebox.showerror(
            "Search Failed",
            "Could not search YouTube.\n\n"
            "Please check that you are online and try again.\n\n"
            f"Details: {err}"
        )

    def _start_download_url(self, target_url, display_title=None):
        status_text = f'Downloading "{display_title}"...' if display_title else "Downloading from YouTube..."
        metric_text = "Connecting to YouTube..."

        self.btn_download.config(text="Downloading...", state=tk.DISABLED)
        self.btn_cancel_dl.config(state=tk.NORMAL)

        self.prog_download.pack(fill=tk.X, pady=(4, 2))
        self.prog_download["value"] = 0
        self.lbl_dl_metrics.pack(fill=tk.X)
        self.lbl_dl_metrics.config(text=metric_text)

        self.set_busy(True, status_text)

        self.download_ctrl.start_download(
            target_url,
            self.library_folder,
            on_progress=lambda pct, speed, eta, finished: self._safe_after(
                0, lambda: self._update_download_progress(pct, speed, eta, finished)
            ),
            on_success=lambda fname: self._safe_after(0, self._download_success, fname),
            on_cancelled=lambda: self._safe_after(0, self._download_cancelled),
            on_error=lambda err: self._safe_after(0, self._download_error, err),
        )

    def _download_success(self, filename):
        self.entry_url.delete(0, tk.END)
        self.btn_download.config(text="⬇ Download MP3", state=tk.NORMAL)
        self.btn_cancel_dl.config(state=tk.DISABLED)
        self.prog_download.pack_forget()
        self.lbl_dl_metrics.pack_forget()
        self.set_busy(False)
        self.refresh_library(select_name=filename)
        self.set_status("Download complete! The song is ready in your Library on the left.")
        messagebox.showinfo("Download Complete", "Song downloaded with album art and tags! It is now in your Library.")

    def _download_cancelled(self):
        self.download_ctrl.cleanup_partial(self.library_folder)
        self.btn_download.config(text="⬇ Download MP3", state=tk.NORMAL)
        self.btn_cancel_dl.config(state=tk.DISABLED)
        self.prog_download.pack_forget()
        self.lbl_dl_metrics.pack_forget()
        self.set_busy(False, "Download stopped.")

    def _download_error(self, error):
        self.download_ctrl.cleanup_partial(self.library_folder)
        self.btn_download.config(text="⬇ Download MP3", state=tk.NORMAL)
        self.btn_cancel_dl.config(state=tk.DISABLED)
        self.prog_download.pack_forget()
        self.lbl_dl_metrics.pack_forget()
        self.set_busy(False, "Download failed.")
        messagebox.showerror(
            "Download Failed",
            "The video could not be downloaded.\n\n"
            "Please check that you are online and that the link is valid.\n\n"
            f"Details: {error}"
        )

    def on_volume_change(self, val):
        v = float(val)
        self.audio_engine.set_volume(v / 100.0)
        if hasattr(self, "lbl_vol_pct") and self.lbl_vol_pct.winfo_exists():
            self.lbl_vol_pct.config(text=f"{int(v)}%")
        if hasattr(self, "btn_mute") and self.btn_mute.winfo_exists():
            self.btn_mute.config(text="🔇" if v <= 0.01 else "🔊")
        if v > 0.01:
            self._unmuted_volume = v

    @property
    def is_muted(self):
        if hasattr(self, "scale_volume") and self.scale_volume.winfo_exists():
            return float(self.scale_volume.get()) <= 0.01
        return False

    def toggle_mute(self):
        if not hasattr(self, "scale_volume"):
            return
        curr = float(self.scale_volume.get())
        if curr > 0.01:
            self._unmuted_volume = curr
            self.scale_volume.set(0)
            self.on_volume_change(0)
            self.set_status("Audio muted.", icon="🔇")
        else:
            restore = getattr(self, "_unmuted_volume", 80)
            if restore <= 0.01:
                restore = 80
            self.scale_volume.set(restore)
            self.on_volume_change(restore)
            self.set_status(f"Audio unmuted ({int(restore)}%).", icon="🔊")

    def on_toggle_auto_level(self):
        enabled = bool(self.auto_level_playback.get())
        self.audio_engine.set_auto_level(enabled)
        if enabled:
            if hasattr(self, "playback_ctrl"):
                self.playback_ctrl.update_auto_level_for_peaks(self._current_peaks)
            self.set_status("Auto-level playback enabled: quiet tracks will be boosted smoothly.", icon="🔊")
        else:
            self.audio_engine.set_track_gain(1.0)
            self.set_status("Auto-level playback disabled.", icon="ℹ️")
        self._save_settings()

    def _current_play_seconds(self):
        return min(self.track_duration or 10**9, self.audio_engine.current_play_seconds())

    def _start_clock(self, start_pos):
        self.play_start_offset = start_pos
        self.audio_engine.start_clock(start_pos)
        self.play_clock_origin = self.audio_engine.play_clock_origin
        self.play_guard_until = time.monotonic() + 0.45
        self.is_paused = False

    def _load_track_ui(self, path, title=None):
        self.selected_file_path = path
        base_name = title or os.path.basename(path)
        artist_name = "Unknown Artist"

        meta = self._cached_metadata(path)
        dur = meta.get("duration", 0.0)
        if meta.get("title"):
            base_name = meta["title"]
        if meta.get("artist"):
            artist_name = meta["artist"]

        if not os.path.exists(path):
            messagebox.showerror("Error", f"Audio file not found:\n{path}")
            return False

        self.lbl_selected.config(text=base_name)
        self.lbl_selected_artist.config(text=artist_name)
        self.track_duration = dur
        self.clip_start_sec = 0.0
        self.clip_end_sec = dur

        self._updating_ui = True
        self.scale_progress.config(to=dur)
        self.scale_progress.set(0)
        self.lbl_start_time.config(text=f"Start: {format_time(0)}")
        self.lbl_prog_time.config(text=f"{format_time(0)} / {format_time(dur)}")
        self.lbl_end_time.config(text=f"End: {format_time(dur)}")
        self._updating_ui = False
        self._update_clip_length_label()

        self._load_album_art(path)
        self._load_waveform(path)
        return True

    def on_library_select(self, event):
        sel = self.listbox_lib.curselection()
        if not sel or sel[0] >= len(self.visible_files):
            return
        filename = self.visible_files[sel[0]]
        new_path = os.path.join(self.library_folder, filename)
        if new_path == self.selected_file_path:
            return

        if self._selection_debounce_timer:
            try:
                self.root.after_cancel(self._selection_debounce_timer)
            except Exception:
                pass
            self._selection_debounce_timer = None

        def _do_select():
            self.stop_audio()
            if self._load_track_ui(new_path, filename):
                self.set_status(f"Selected: {filename}. Press PLAY, or use the Waveform to trim.")

        self._selection_debounce_timer = self.root.after(100, _do_select)

    def _on_library_double_click(self, _event=None):
        if self._selection_debounce_timer:
            try:
                self.root.after_cancel(self._selection_debounce_timer)
            except Exception:
                pass
            self._selection_debounce_timer = None
        sel = self.listbox_lib.curselection()
        if not sel or sel[0] >= len(self.visible_files):
            return
        filename = self.visible_files[sel[0]]
        new_path = os.path.join(self.library_folder, filename)
        if self.selected_file_path != new_path:
            self.stop_audio()
            if not self._load_track_ui(new_path, filename):
                return
        self.play_main()

    def _mark_progress_drag(self, dragging):
        self._progress_dragging = dragging
        if dragging:
            self.root.bind("<ButtonRelease-1>", self._on_progress_release)

    def on_progress_drag(self, val):
        if self._updating_ui:
            return
        v = float(val)
        self.lbl_prog_time.config(text=self._prog_label(v))
        self._render_waveform()

    def _on_progress_release(self, _event=None):
        try:
            self.root.unbind("<ButtonRelease-1>")
        except Exception:
            pass
        was_dragging = self._progress_dragging
        self._progress_dragging = False
        v = float(self.scale_progress.get())
        self.lbl_prog_time.config(text=self._prog_label(v))
        self._render_waveform()
        if not was_dragging:
            return
        if (self.is_playing_main or self.is_playing_playlist) and not self.is_paused:
            self._seek_playback(v)
        else:
            self.play_start_offset = v
            self.audio_engine.seek_clock(v, is_playing=False)
            if self.is_paused:
                self._scrubbed_while_paused = True

    def _seek_playback(self, seconds):
        if not self.selected_file_path:
            return
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.seek(seconds, self.track_duration)
        else:
            seek_sec = max(0.0, min(self.track_duration or seconds, seconds))
            try:
                pygame.mixer.music.play(start=seek_sec)
                self._start_clock(seek_sec)
            except Exception:
                try:
                    pygame.mixer.music.rewind()
                    pygame.mixer.music.set_pos(seek_sec)
                    self._start_clock(seek_sec)
                except Exception:
                    self.set_status("Seeking to exact position not supported for this audio stream.")
        self._render_waveform()

    def set_start_here(self):
        if not self.selected_file_path:
            self.set_status("Select a song in the Library first to set clip start.")
            return
        curr = max(0.0, min(self.track_duration, float(self.scale_progress.get())))
        self.clip_start_sec = curr
        if self.clip_start_sec >= self.clip_end_sec:
            self.clip_end_sec = self.track_duration
            self.lbl_end_time.config(text=f"End: {format_time(self.clip_end_sec)}")
        is_frac = not float(curr).is_integer()
        self.lbl_start_time.config(text=f"Start: {format_time(curr, include_fractional=is_frac)}")
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip start set to {format_time(curr, include_fractional=is_frac)}.")

    def set_end_here(self):
        if not self.selected_file_path:
            self.set_status("Select a song in the Library first to set clip end.")
            return
        curr = max(0.0, min(self.track_duration, float(self.scale_progress.get())))
        self.clip_end_sec = curr
        if self.clip_end_sec <= self.clip_start_sec:
            self.clip_start_sec = 0.0
            self.lbl_start_time.config(text="Start: 00:00")
        is_frac = not float(curr).is_integer()
        self.lbl_end_time.config(text=f"End: {format_time(curr, include_fractional=is_frac)}")
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip end set to {format_time(curr, include_fractional=is_frac)}.")

    def _parse_time_input(self, text):
        """Parse time input string in format MM:SS, M:SS, MM:SS.s, or raw seconds float.
        Returns float seconds if valid, or None if invalid or negative.
        """
        if not text or not str(text).strip():
            return None
        raw = str(text).strip()
        if ":" in raw:
            parts = raw.split(":")
            try:
                if len(parts) == 2:
                    mins = float(parts[0])
                    secs = float(parts[1])
                    if mins < 0 or secs < 0 or secs >= 60:
                        return None
                    return mins * 60.0 + secs
                elif len(parts) == 3:
                    hrs = float(parts[0])
                    mins = float(parts[1])
                    secs = float(parts[2])
                    if hrs < 0 or mins < 0 or secs < 0 or mins >= 60 or secs >= 60:
                        return None
                    return hrs * 3600.0 + mins * 60.0 + secs
            except ValueError:
                return None
        else:
            try:
                val = float(raw)
                return val if val >= 0 else None
            except ValueError:
                return None
        return None

    def edit_start_time(self):
        if not self.selected_file_path:
            self.set_status("Select a song in the Library first to edit clip start.")
            return
        curr_str = format_time(self.clip_start_sec, include_fractional=not float(self.clip_start_sec).is_integer())
        inp = simpledialog.askstring(
            "Set Clip Start",
            f"Enter new Start time for clip (e.g. 01:23, 01:23.5, or 83.5):\nMax allowed: {format_time(self.clip_end_sec, include_fractional=not float(self.clip_end_sec).is_integer())}",
            initialvalue=curr_str, parent=self.root
        )
        if inp is None:
            return
        val = self._parse_time_input(inp)
        if val is None:
            messagebox.showwarning("Invalid Time", "Please enter a valid time (e.g. '01:30' or '90').")
            return
        if val >= self.clip_end_sec:
            messagebox.showwarning("Invalid Range", f"Clip Start must be before Clip End ({format_time(self.clip_end_sec, include_fractional=not float(self.clip_end_sec).is_integer())}).")
            return
        self.clip_start_sec = max(0.0, val)
        is_frac = not float(self.clip_start_sec).is_integer()
        self.lbl_start_time.config(text=f"Start: {format_time(self.clip_start_sec, include_fractional=is_frac)}")
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip start set to {format_time(self.clip_start_sec, include_fractional=is_frac)}.")

    def edit_end_time(self):
        if not self.selected_file_path:
            self.set_status("Select a song in the Library first to edit clip end.")
            return
        curr_str = format_time(self.clip_end_sec, include_fractional=not float(self.clip_end_sec).is_integer())
        inp = simpledialog.askstring(
            "Set Clip End",
            f"Enter new End time for clip (e.g. 02:45, 02:45.5, or 165.5):\nSong total length: {format_time(self.track_duration)}",
            initialvalue=curr_str, parent=self.root
        )
        if inp is None:
            return
        val = self._parse_time_input(inp)
        if val is None:
            messagebox.showwarning("Invalid Time", "Please enter a valid time (e.g. '02:45' or '165').")
            return
        if val <= self.clip_start_sec:
            messagebox.showwarning("Invalid Range", f"Clip End must be after Clip Start ({format_time(self.clip_start_sec, include_fractional=not float(self.clip_start_sec).is_integer())}).")
            return
        max_limit = self.track_duration if self.track_duration > 0 else 999999
        self.clip_end_sec = min(max_limit, val)
        is_frac = not float(self.clip_end_sec).is_integer()
        self.lbl_end_time.config(text=f"End: {format_time(self.clip_end_sec, include_fractional=is_frac)}")
        self._update_clip_length_label()
        self._render_waveform()
        self.set_status(f"Clip end set to {format_time(self.clip_end_sec, include_fractional=is_frac)}.")

    def _draw_vu_meter(self, level=0):
        if not hasattr(self, "canvas_vu") or not self.canvas_vu.winfo_exists():
            return
        c = self.canvas_vu
        c.delete("all")
        colors_on = ["#22c55e", "#22c55e", "#22c55e", "#f59e0b", "#ef4444"]
        dim_color = "#cbd5e1"
        for i in range(5):
            x1 = 2 + i * 16
            x2 = x1 + 12
            color = colors_on[i] if (i < level) else dim_color
            c.create_rectangle(x1, 2, x2, 12, fill=color, outline="", width=0)

    def _set_card_playing_state(self, state):
        if hasattr(self, "f_track_card"):
            if state == "playing":
                self.f_track_card.configure(highlightbackground=COLOR_PLAY, highlightthickness=2)
                self.lbl_selected.config(fg=COLOR_PLAY)
                self.lbl_track_state.config(text="▶ PLAYING", bg=COLOR_PLAY)
            elif state == "paused":
                self.f_track_card.configure(highlightbackground=COLOR_PAUSE, highlightthickness=2)
                self.lbl_selected.config(fg=COLOR_PAUSE)
                self.lbl_track_state.config(text="⏸ PAUSED", bg=COLOR_PAUSE)
                self._draw_vu_meter(0)
            else:
                self.f_track_card.configure(highlightbackground=BORDER_MAIN, highlightthickness=2)
                self.lbl_selected.config(fg=COLOR_ACCENT)
                self.lbl_track_state.config(text="⏹ READY", bg="#64748b")
                self._draw_vu_meter(0)

    def play_main(self):
        if self._selection_debounce_timer:
            try:
                self.root.after_cancel(self._selection_debounce_timer)
            except Exception:
                pass
            self._selection_debounce_timer = None
        if not self.selected_file_path:
            return messagebox.showwarning("No Song", "Click a song in the Library first.")
        if self.is_paused:
            self.pause_audio()
            return
        self.stop_audio()
        try:
            start_pos = float(self.scale_progress.get())
            if hasattr(self, "playback_ctrl"):
                self.playback_ctrl.play_track(self.selected_file_path, start_pos, is_playlist=False)
            else:
                self.audio_engine.load_and_play(self.selected_file_path, start_pos)
                self._start_clock(start_pos)
                self.is_playing_main = True
            self._set_card_playing_state("playing")
            self.set_status(f"Playing: {os.path.basename(self.selected_file_path)}", icon="▶")
        except Exception as e:
            messagebox.showerror("Playback Error", f"Could not play this file:\n{e}")

    def pause_audio(self):
        if not self.selected_file_path:
            return
        if self.is_paused:
            try:
                if hasattr(self, "playback_ctrl"):
                    self.playback_ctrl.unpause(self.selected_file_path)
                else:
                    self.audio_engine.unpause()
                    self.is_paused = False
                self._set_card_playing_state("playing")
                self.set_status(f"Playing: {os.path.basename(self.selected_file_path)}", icon="▶")
            except Exception as e:
                messagebox.showerror("Playback Error", f"Could not resume playback:\n{e}")
            return
        if not (self.is_playing_main or self.is_playing_playlist):
            return
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.pause()
        else:
            self.audio_engine.pause()
            self.is_paused = True
        self._set_card_playing_state("paused")
        self.set_status("Paused. Press PLAY to continue.", icon="⏸")

    def _get_fade_sec(self):
        choice = self.fade_choice_var.get() if hasattr(self, "fade_choice_var") else ""
        if "0.5s" in choice:
            return 0.5
        elif "3.0s" in choice:
            return 3.0
        return 1.5

    def test_clip(self):
        if not self.selected_file_path:
            return messagebox.showwarning("No Song", "Click a song in the Library first.")
        s_time = self.clip_start_sec
        e_time = self.clip_end_sec
        if s_time >= e_time:
            return messagebox.showwarning("Invalid Range", "Clip End must be after Clip Start.")
        self.stop_audio()

        soften = bool(self.soften_clip.get()) if hasattr(self, "soften_clip") else False
        gain_db = float(self.scale_gain.get()) if hasattr(self, "scale_gain") else 0.0
        fade_sec = self._get_fade_sec()
        loop = bool(self.loop_clip.get()) if hasattr(self, "loop_clip") else False

        try:
            self.playback_ctrl.test_clip(
                self.selected_file_path, s_time, e_time,
                gain_db=gain_db, soften=soften, fade_sec=fade_sec, loop=loop
            )
            self._is_audition_slice = self.playback_ctrl._is_audition_slice
            self._audition_slice_file = self.playback_ctrl._audition_slice_file
            self.is_playing_main = True
            self.previewing_clip = True
            self.clip_end_time = e_time
            self._set_card_playing_state("playing")
            self._updating_ui = True
            self.scale_progress.set(s_time)
            self._updating_ui = False
            self._render_waveform()

            notes = []
            if abs(gain_db) > 0.05:
                notes.append(f"{gain_db:+.1f}dB boost")
            if soften:
                notes.append(f"{fade_sec:.1f}s smooth fade")
            if loop:
                notes.append("loop on")
            note_str = f" ({', '.join(notes)})" if notes else ""
            self.set_status(f"Previewing clip from {format_time(s_time)} to {format_time(e_time)}{note_str}.", icon="▶")
        except Exception as e:
            messagebox.showerror("Playback Error", f"Could not preview this clip:\n{e}")

    def _restart_clip_loop(self):
        s_time = self.clip_start_sec
        if self.playback_ctrl.restart_clip_loop():
            self._updating_ui = True
            self.scale_progress.set(s_time)
            self._updating_ui = False
            self.set_status(f"Looping clip preview ({format_time(s_time)} - {format_time(self.clip_end_sec)})...", icon="🔁")
            return True
        else:
            self.stop_audio(user=False)
            return False

    def stop_audio(self, user=False):
        if hasattr(self, "playback_ctrl"):
            self.playback_ctrl.stop(user=user)
        self._release_audio_file()
        if getattr(self, "_audition_slice_file", None):
            aud_file = self._audition_slice_file
            self._audition_slice_file = None
            self._is_audition_slice = False
            try:
                if os.path.exists(aud_file):
                    os.remove(aud_file)
            except Exception:
                pass
        self.is_playing_main = False
        self.is_playing_playlist = False
        self.is_paused = False
        self.previewing_clip = False
        self.play_clock_origin = None
        self._set_card_playing_state("stopped")
        if user:
            self._updating_ui = True
            self.scale_progress.set(0)
            self.lbl_prog_time.config(text=f"00:00 / {format_time(self.track_duration)}")
            self._updating_ui = False
            self.play_start_offset = 0
            self.set_status("Stopped.", icon="⏹")
            self.refresh_playlist_listbox()
        self._render_waveform()

    def _song_finished(self):
        if self.is_paused:
            return
        if self.previewing_clip and hasattr(self, "loop_clip") and self.loop_clip.get():
            self._restart_clip_loop()
            return
        if self.is_playing_playlist:
            self.play_next_in_playlist()
        elif self.is_playing_main:
            self.is_playing_main = False
            self.play_clock_origin = None
            self._set_card_playing_state("stopped")
            self.set_status("Playback finished.")
            self._render_waveform()

    def monitor_audio(self):
        if not getattr(self, "_is_shutting_down", False):
            if (self.is_playing_main or self.is_playing_playlist) and not self.is_paused:
                curr_pos = self._current_play_seconds()
                if not self._progress_dragging:
                    self._updating_ui = True
                    self.scale_progress.set(curr_pos)
                    self.lbl_prog_time.config(text=self._prog_label(curr_pos))
                    self._updating_ui = False
                    self._render_waveform()

                if hasattr(self, "playback_ctrl"):
                    vu_lvl = self.playback_ctrl.calculate_vu_level(curr_pos, self.track_duration, self._current_peaks)
                    self._draw_vu_meter(vu_lvl)

                if self.previewing_clip and curr_pos >= (self.clip_end_time - 0.05):
                    if hasattr(self, "loop_clip") and self.loop_clip.get():
                        self._restart_clip_loop()
                    else:
                        self.stop_audio(user=False)
                        self.set_status("Finished previewing clip.")
                        self._render_waveform()
                else:
                    now = time.monotonic()
                    ended = False
                    if hasattr(self, "playback_ctrl") and self.playback_ctrl.check_native_end_event():
                        ended = True
                    elif now >= getattr(self, "play_guard_until", 0):
                        if not pygame.mixer.music.get_busy() or curr_pos >= (self.track_duration - 0.05):
                            ended = True
                    if ended:
                        self._song_finished()

            if hasattr(self, "root") and self.root and self.root.winfo_exists():
                self._monitor_timer = self.root.after(40, self.monitor_audio)

    def save_clip(self):
        if not self.selected_file_path:
            return messagebox.showwarning("No Song", "Click a song in the Library first.")
        s_time = self.clip_start_sec
        e_time = self.clip_end_sec
        if s_time >= e_time:
            return messagebox.showwarning("Invalid Range", "Clip End must be after Clip Start.")

        dur = e_time - s_time
        base, _ext = os.path.splitext(os.path.basename(self.selected_file_path))
        def_name = f"{base}_clip_{int(dur)}s.mp3"

        save_name = filedialog.asksaveasfilename(
            initialdir=self.library_folder,
            initialfile=def_name,
            defaultextension=".mp3",
            filetypes=[("High-Quality MP3 Audio (*.mp3)", "*.mp3")],
            title="Save Clipped Audio (320 kbps MP3)",
            parent=self.root
        )
        if not save_name:
            return

        if not save_name.lower().endswith(".mp3"):
            save_name += ".mp3"

        is_self_overwrite = (os.path.abspath(save_name).lower() == os.path.abspath(self.selected_file_path).lower())
        if is_self_overwrite:
            confirm = messagebox.askyesno(
                "Confirm Overwrite",
                f"You are about to overwrite the original song file:\n\n'{os.path.basename(save_name)}'\n\n"
                "A backup of the original will be saved automatically as:\n"
                f"'{os.path.basename(save_name)}.original.bak'\n\nDo you want to proceed?"
            )
            if not confirm:
                return

        self.stop_audio()
        self._release_audio_file()

        soften = bool(self.soften_clip.get()) if hasattr(self, "soften_clip") else False
        gain_db = float(self.scale_gain.get()) if hasattr(self, "scale_gain") else 0.0
        fade_sec = self._get_fade_sec()

        self.btn_save_clip.config(text="Saving...", state=tk.DISABLED)
        self.set_busy(True, "Trimming audio and saving 320 kbps MP3 clip...")

        def _on_succ(name, full_path, was_self_ovw):
            self._safe_after(0, self._save_success, name, full_path, was_self_ovw)

        def _on_err(err):
            self._safe_after(0, self._save_error, err)

        task_mgr.submit_task(
            clip_audio_worker,
            self.selected_file_path, s_time, e_time, save_name, soften, gain_db, is_self_overwrite, _on_succ, _on_err,
            fade_sec=fade_sec
        )

    def _save_success(self, name, full_path, was_self_overwrite):
        self.btn_save_clip.config(text="💾 Save Clip", state=tk.NORMAL)
        self.set_busy(False)
        cache_mgr.invalidate(full_path)
        if hasattr(self, "library_ctrl"):
            self.library_ctrl.invalidate_search_index(full_path)
        self._art_cache.pop(full_path, None)
        self.refresh_library(select_name=name)
        if was_self_overwrite and self.selected_file_path == full_path:
            self._load_track_ui(full_path, name)
        if was_self_overwrite:
            bak_name = name + ".original.bak"
            self.set_status(f"Clip saved! Original backed up as '{bak_name}'.", icon="💾")
            messagebox.showinfo(
                "Clip Saved",
                f"Your trimmed song has been saved as 320 kbps MP3!\n\nA backup of the original was safely created as:\n{bak_name}"
            )
        else:
            self.set_status("Clip saved to your Library.", icon="💾")
            messagebox.showinfo("Clip Saved", "Your trimmed song has been saved as 320 kbps MP3 to your Library!")

    def _save_error(self, err):
        self.btn_save_clip.config(text="💾 Save Clip", state=tk.NORMAL)
        self.set_busy(False, "Could not save clip.")
        messagebox.showerror("Error", f"Failed to save clip:\n{err}")

    def add_to_playlist(self):
        sel = self.listbox_lib.curselection() if hasattr(self, "listbox_lib") else ()
        if sel:
            selected_paths = []
            for idx in sel:
                if 0 <= idx < len(self.visible_files):
                    selected_paths.append(os.path.join(self.library_folder, self.visible_files[idx]))
        elif self.selected_file_path:
            selected_paths = [self.selected_file_path]
        else:
            return messagebox.showwarning("No Song", "Click or select songs in the Library first.")

        for path in selected_paths:
            self.playlist_ctrl.add_track(self.active_playlist_name, path)
        self.save_playlists()
        self.refresh_playlist_listbox()
        if len(selected_paths) == 1:
            self.set_status(f"Added '{os.path.basename(selected_paths[0])}' to '{self.active_playlist_name}'.")
        else:
            self.set_status(f"Added {len(selected_paths)} songs to '{self.active_playlist_name}'.")

    def pl_remove(self):
        sel = self.listbox_pl.curselection()
        if not sel or sel[0] >= len(self.playlist_files):
            return
        idx = sel[0]
        was_playing = (self.is_playing_playlist and idx == self.playlist_index)
        if was_playing:
            self.stop_audio()
        removed = self.playlist_ctrl.remove_track_with_undo(self.active_playlist_name, idx)
        if removed:
            self.save_playlists()
            self.refresh_playlist_listbox()
            tracks = self.playlists.get(self.active_playlist_name, [])
            if tracks:
                new_sel = min(idx, len(tracks) - 1)
                self.listbox_pl.selection_set(new_sel)
            fname = os.path.basename(removed)
            self.show_undo(
                f"Removed '{fname}' from playlist.",
                callback=self._undo_remove_track,
                timeout_sec=8
            )

    def _undo_remove_track(self):
        try:
            restored = self.playlist_ctrl.undo_remove()
            if restored:
                self.save_playlists()
                self.refresh_playlist_listbox()
                self.set_status(f"Restored '{os.path.basename(restored[2])}' to playlist.", icon="↩️")
        except Exception as e:
            log_error(f"_undo_remove_track: {e}")

    def pl_move_up(self):
        sel = self.listbox_pl.curselection()
        if not sel or sel[0] <= 0:
            return
        idx = sel[0]
        new_idx = self.playlist_ctrl.move_up(self.active_playlist_name, idx)
        if self.is_playing_playlist:
            if self.playlist_index == idx:
                self.playlist_index = new_idx
            elif self.playlist_index == new_idx:
                self.playlist_index = idx
        self.save_playlists()
        self.refresh_playlist_listbox()
        self.listbox_pl.selection_set(new_idx)
        self.listbox_pl.see(new_idx)

    def pl_move_down(self):
        sel = self.listbox_pl.curselection()
        tracks = self.playlist_ctrl.get_active_tracks(self.active_playlist_name)
        if not sel or sel[0] >= len(tracks) - 1:
            return
        idx = sel[0]
        new_idx = self.playlist_ctrl.move_down(self.active_playlist_name, idx)
        if self.is_playing_playlist:
            if self.playlist_index == idx:
                self.playlist_index = new_idx
            elif self.playlist_index == new_idx:
                self.playlist_index = idx
        self.save_playlists()
        self.refresh_playlist_listbox()
        self.listbox_pl.selection_set(new_idx)
        self.listbox_pl.see(new_idx)

    def on_playlist_double_click(self, _event=None):
        sel = self.listbox_pl.curselection()
        if not sel or sel[0] >= len(self.playlist_files):
            return
        self.playlist_index = sel[0]
        self._play_current_pl_track()

    def play_playlist(self):
        if not self.playlist_files:
            return messagebox.showwarning("Empty Playlist", "Add some songs to this playlist first.")
        sel = self.listbox_pl.curselection()
        self.playlist_index = sel[0] if sel else 0
        self._play_current_pl_track()

    def play_prev_in_playlist(self):
        if not self.playlist_files:
            return
        prev_idx = self.playlist_ctrl.get_prev_index(self.active_playlist_name, self.playlist_index)
        if prev_idx is not None:
            self.playlist_index = prev_idx
            self._play_current_pl_track()

    def play_next_in_playlist(self):
        if not self.playlist_files:
            return
        next_idx = self.playlist_ctrl.get_next_index(self.active_playlist_name, self.playlist_index, repeat=self.repeat_playlist.get())
        if next_idx is not None:
            self.playlist_index = next_idx
            self._play_current_pl_track()
        else:
            self.stop_audio(user=True)
            self.set_status("Finished playlist.")

    def _play_current_pl_track(self):
        if not self.playlist_files or not (0 <= self.playlist_index < len(self.playlist_files)):
            return
        path = self.playlist_files[self.playlist_index]
        if not os.path.exists(path):
            self.set_status(f"Skipping missing song: {os.path.basename(path)}", icon="⚠️")
            if self.playlist_index + 1 < len(self.playlist_files):
                self.playlist_index += 1
                self.root.after(350, self._play_current_pl_track)
            elif self.repeat_playlist.get() and len(self.playlist_files) > 1:
                self.playlist_index = 0
                self.root.after(350, self._play_current_pl_track)
            else:
                self.stop_audio(user=True)
                messagebox.showwarning("File Missing", f"Audio track not found:\n{path}\n\nPlease verify or remove it from the playlist.")
            return
        self.stop_audio()
        if not self._load_track_ui(path):
            return
        try:
            if hasattr(self, "playback_ctrl"):
                self.playback_ctrl.play_track(path, 0.0, is_playlist=True)
            else:
                self.audio_engine.load_and_play(path, 0.0)
                self._start_clock(0.0)
                self.is_playing_playlist = True
            self._set_card_playing_state("playing")
            self.refresh_playlist_listbox()
            self.set_status(f"Playlist ({self.playlist_index+1}/{len(self.playlist_files)}): {os.path.basename(path)}", icon="▶")
        except Exception as e:
            messagebox.showerror("Playback Error", f"Could not play playlist song:\n{e}")

    def export_playlist(self):
        if not self.playlist_files:
            return messagebox.showwarning("Empty Playlist", "Add some songs to this playlist before exporting.")

        dest_type = self.export_var.get()
        normalize = self.even_volume.get()

        if dest_type == "USB":
            choice = self.usb_choice.get()
            if not choice or choice not in self._usb_map:
                return messagebox.showwarning("No USB Drive", "Please insert a USB flash drive and select it from the list.")

            dest_folder = self._usb_map[choice]
            if not os.path.exists(dest_folder):
                return messagebox.showerror("Drive Missing", "Selected USB drive is no longer accessible. Please re-insert.")

            fs_type = self._usb_fs_map.get(choice, "")
            if self.export_ctrl.is_ntfs(fs_type):
                warn = messagebox.askyesno(
                    "NTFS Filesystem Warning",
                    f"The selected drive ({choice}) is formatted as NTFS.\n\n"
                    "Many car stereos and older stereos ONLY recognize FAT32 or exFAT drives, and will show 'No Device' or 'Read Error'.\n\n"
                    "Do you want to continue exporting anyway?"
                )
                if not warn:
                    return

            # Pre-flight disk space validation
            est_bytes = self.export_ctrl.estimate_playlist_bytes(self.playlist_files, self._cached_duration)
            has_space, free_bytes = self.export_ctrl.check_usb_space(dest_folder, est_bytes)
            if not has_space:
                free_mb = free_bytes / (1024 * 1024)
                needed_mb = est_bytes / (1024 * 1024)
                messagebox.showwarning(
                    "Insufficient USB Disk Space",
                    f"The selected USB drive only has {free_mb:.0f} MB of free space.\n\n"
                    f"This playlist requires approximately {needed_mb:.0f} MB.\n\n"
                    "Please delete files from your USB flash drive or use a drive with more free space."
                )
                return

            self.btn_export.config(text="Exporting...", state=tk.DISABLED)
            self.prog_export.pack(fill=tk.X, pady=(4, 2))
            self.prog_export["value"] = 0
            self.set_busy(True, f"Exporting {len(self.playlist_files)} songs to USB flash drive...")

            self.export_ctrl.start_usb_export(
                dest_folder,
                self.active_playlist_name,
                self.playlist_files,
                normalize,
                on_progress=lambda pct: self._safe_after(0, lambda: self._update_export_progress(pct)),
                on_status=lambda text: self._safe_after(0, lambda: self.set_status(text)),
                on_success=lambda sc, tot, sk: self._safe_after(0, self._usb_export_success, sc, tot, sk),
                on_error=lambda err: self._safe_after(0, self._export_error, err),
                is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
                duration_fn=self._cached_duration,
            )
        else:
            # CD Burn Folder Export
            cd_folder = self.export_ctrl.get_cd_burn_folder()
            existing_cd_files = self.export_ctrl.get_cd_existing_files(cd_folder)
            if existing_cd_files:
                clear_old = messagebox.askyesnocancel(
                    "Clear Previous CD Files?",
                    f"The CD burn folder ('My_CD_Burn_Folder') already contains {len(existing_cd_files)} file(s) from a previous export.\n\n"
                    "Click 'Yes' to remove old files and start fresh.\n"
                    "Click 'No' to keep old files and add these songs.\n"
                    "Click 'Cancel' to stop."
                )
                if clear_old is None:
                    return
                elif clear_old:
                    self.export_ctrl.clear_cd_folder(cd_folder)

            # 80-minute CD capacity validation
            total_sec = self.export_ctrl.get_playlist_duration(self.playlist_files, self._cached_duration)
            if total_sec > 80 * 60:
                mins = int(total_sec // 60)
                ok = messagebox.askyesno(
                    "Playlist Exceeds 80 Minutes",
                    f"Standard audio CDs hold 80 minutes of music.\n\n"
                    f"Your playlist is currently {mins} minutes long, so some songs might not fit on one blank CD.\n\n"
                    "Do you still want to prepare all tracks?"
                )
                if not ok:
                    return

            self.btn_export.config(text="Preparing CD...", state=tk.DISABLED)
            self.prog_export.pack(fill=tk.X, pady=(4, 2))
            self.prog_export["value"] = 0
            self.set_busy(True, f"Preparing {len(self.playlist_files)} CD audio tracks...")

            self.export_ctrl.start_cd_export(
                cd_folder,
                self.playlist_files,
                normalize,
                on_progress=lambda pct: self._safe_after(0, lambda: self._update_export_progress(pct)),
                on_status=lambda text: self._safe_after(0, lambda: self.set_status(text)),
                on_success=lambda fld, sc, tot, sk: self._safe_after(0, self._cd_export_success, fld, sc, tot, sk),
                on_error=lambda err: self._safe_after(0, self._export_error, err),
                is_shutting_down_fn=lambda: getattr(self, "_is_shutting_down", False),
            )

    def _update_export_progress(self, pct):
        if hasattr(self, "prog_export"):
            self.prog_export["value"] = pct

    def _usb_export_success(self, success_count, total, skipped):
        self.btn_export.config(text="⚡ Export Playlist Now", state=tk.NORMAL)
        self.prog_export.pack_forget()
        self.set_busy(False, "USB export finished! Files safely written and flushed to drive.")
        pl_name = self.active_playlist_name or "Playlist"
        clean_pl = sanitize_filename(pl_name)
        if skipped:
            msg = f"Exported {success_count} of {total} song(s) to USB flash drive (with '00_{clean_pl}.m3u' playlist).\n\n{len(skipped)} song(s) could not be processed:\n"
            msg += "\n".join(f"• {s}" for s in skipped[:6])
            if len(skipped) > 6:
                msg += f"\n... and {len(skipped) - 6} more."
            messagebox.showwarning("Export Completed with Warnings", msg)
        else:
            messagebox.showinfo(
                "Export Successful",
                f"Playlist copied to USB flash drive successfully!\n\nAn '00_{clean_pl}.m3u' playlist file was created for car stereos and media players.\n\nAll file buffers have been written and flushed to the disk. You can safely remove the flash drive."
            )
        if messagebox.askyesno("Safe USB Ejection", "Export complete! Would you like to safely eject the USB flash drive now so you can unplug it?"):
            self.eject_selected_usb()

    def _cd_export_success(self, cd_folder, success_count, total, skipped):
        self.btn_export.config(text="⚡ Export Playlist Now", state=tk.NORMAL)
        self.prog_export.pack_forget()
        self.set_busy(False, "CD files are ready on your Desktop in 'My_CD_Burn_Folder'.")
        warn_text = ""
        if skipped:
            warn_text = f"\n\nNote: {len(skipped)} song(s) were skipped:\n" + "\n".join(f"• {s}" for s in skipped[:5])

        msg = (
            f"Prepared {success_count} of {total} audio CD track(s) on your Desktop in 'My_CD_Burn_Folder'.{warn_text}\n\n"
            "1. Insert a blank CD.\n"
            "2. Select all files in that folder.\n"
            "3. Right-click and choose 'Send to' -> Your CD Drive."
        )
        if skipped:
            messagebox.showwarning("Ready to Burn (with skipped files)", msg)
        else:
            messagebox.showinfo("Ready to Burn", msg)
        try:
            os.startfile(cd_folder)
        except Exception:
            pass

    def _export_error(self, err):
        self.btn_export.config(text="⚡ Export Playlist Now", state=tk.NORMAL)
        if hasattr(self, "prog_export"):
            self.prog_export.pack_forget()
        self.set_busy(False, "Export failed.")
        messagebox.showerror("Export Failed", err)


def main():
    """Application entry point."""
    if already_running():
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Already Running", "Ultimate Audio Studio is already running.")
        sys.exit(0)

    enable_windows_dpi()
    root = tk.Tk()
    app = UltimateAudioStudio(root)
    root.mainloop()


if __name__ == "__main__":
    main()

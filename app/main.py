"""Main Application Coordinator and Window Controller for Ultimate Audio Studio."""

from __future__ import annotations

import contextlib
import faulthandler
import importlib
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk
from types import TracebackType
from typing import Any, TextIO

import pygame

from app import self_test
from app.config import (
    APP_VERSION,
    DEFAULT_PLAYLIST_NAME,
    ERROR_LOG_PATH,
    MUSIC_DIR,
    cleanup_old_executables,
    cleanup_temp_caches,
    ffmpeg_path,
    log_error,
    settings_mgr,
    setup_logging,
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
from app.core.task_manager import network_task_mgr, task_mgr
from app.platform_utils import (
    Win32DragDropHandler,
    activate_existing_window,
    already_running,
    enable_windows_dpi,
    is_on_a_monitor,
    release_instance_mutex,
    send_fatal_errors_to,
    set_keep_awake,
    signal_named_event,
    terminate_child_processes,
)
from app.self_test import SELF_TEST_FLAG
from app.services.updater import (
    UPDATE_OK_EVENT_NAME,
    clear_pending_update,
    failed_update_tag,
    install_update,
    load_pending_update,
)
from app.ui import dialogs
from app.ui.components import ScrollableFrame, ToolTip, create_button
from app.ui.error_dialog import show_error
from app.ui.features.base import UNDO_SECONDS, UiCallback
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

logger = logging.getLogger(__name__)

STATUS_BAR_BG = "#0f172a"
# How often the window looks for results that worker threads have queued for it.
UI_QUEUE_POLL_MS = 25
# The window title starts with this; a second start finds the open window by it.
WINDOW_TITLE_PREFIX = "Ultimate Audio Studio v"
STATUS_FLASH_MS = 5000

# A saved window size below this is not restored (the three columns would not fit).
MIN_RESTORED_WIDTH, MIN_RESTORED_HEIGHT = 1020, 600
# The part of the title bar that must be on a screen for the window to be seen and dragged:
# this far in from both sides, and this tall.
TITLE_BAR_INSET, TITLE_BAR_HEIGHT = 80, 40
_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?")


def _same_folder(a: str | Path, b: str | Path) -> bool:
    """True when both name the same folder (Windows compares paths without regard to case)."""
    return str(Path(a).absolute()).casefold() == str(Path(b).absolute()).casefold()


def restorable_geometry(geometry: str | None) -> str | None:
    """The saved window geometry if the window would be usable there, else None (use the default).

    A window last closed on a second monitor or a TV that is no longer connected would otherwise
    open off-screen, where it can neither be seen nor dragged back: the app would seem not to start.
    """
    match = _GEOMETRY_RE.match(str(geometry or ""))
    if match is None:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width < MIN_RESTORED_WIDTH or height < MIN_RESTORED_HEIGHT:
        return None
    if match.group(3) is None:
        return f"{width}x{height}"  # no position saved: Windows places the window
    left, top = int(match.group(3)), int(match.group(4))
    if not is_on_a_monitor(left + TITLE_BAR_INSET, top, left + width - TITLE_BAR_INSET, top + TITLE_BAR_HEIGHT):
        return None
    return str(geometry)


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
    # The Help Guide window while it is open, so the Help button shows it instead of opening another.
    _help_window: tk.Toplevel | None = None

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{WINDOW_TITLE_PREFIX}{APP_VERSION}")

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
        self._fresh_songs: set[str] = set()
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
        self.refresh_usb_drives(announce="quiet")
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
        """Run the callbacks that workers queued, on the Tkinter thread, and come back shortly.

        The next run is scheduled before any callback runs. A callback that opens a dialog waits
        inside that dialog until it is closed; scheduled afterwards, nothing from any other worker
        (export progress, a finished download, the next playlist song being ready) arrived until then.
        """
        if self._is_shutting_down:
            return
        with contextlib.suppress(tk.TclError):  # the window is already gone
            self._drain_timer = self.root.after(UI_QUEUE_POLL_MS, self._drain_ui_callbacks)
        while not self._is_shutting_down:
            try:
                callback, args = self._ui_callback_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except Exception:  # last-resort guard: one failed update must not stop the ones behind it
                logger.exception("A queued window update failed")
            finally:
                self._ui_callback_queue.task_done()

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
        """Copy the saved preferences into the attributes the window is built from."""
        s = settings_mgr.get_settings()  # never raises: an unreadable file gives the defaults
        if s.library_folder and Path(s.library_folder).is_dir():
            self.library_folder = s.library_folder
        elif s.library_folder and not _same_folder(s.library_folder, self.default_lib_path):
            # On a drive or share that is not connected now: use the default folder for this session,
            # but keep the user's choice so it is back once the drive is.
            self._unavailable_library_folder = s.library_folder
            self.root.after(800, self._report_unavailable_library_folder)
        self._saved_volume = s.volume
        self._saved_repeat = s.repeat_playlist
        self._saved_soften = s.soften_clip
        self._saved_fade_choice = s.fade_choice
        self._saved_even = s.even_volume
        self._saved_auto_level_playback = s.auto_level_playback
        self.text_size = s.text_size if s.text_size in TEXT_SIZES else DEFAULT_TEXT_SIZE
        self._saved_active_playlist = s.active_playlist
        self._saved_geometry = restorable_geometry(s.geometry)
        if settings_mgr.damaged_copy is not None or settings_mgr.read_failed:
            # After the window is up: a dialog during start-up would appear before the app does.
            self.root.after(800, self._report_settings_problem)

    def _report_unavailable_library_folder(self) -> None:
        """Tell the user that their chosen music folder is not there, and how to get it back."""
        missing = self._unavailable_library_folder
        if missing is None or getattr(self, "_is_shutting_down", False):
            return
        dialogs.show_warning(
            self.root,
            "Music Folder Not Found",
            f"Your music folder could not be found:\n{missing}\n\n"
            "If it is on a USB drive, a memory card or another computer, connect it, then close "
            "Ultimate Audio Studio and open it again.\n\n"
            f"Until then, the app uses the '{Path(self.library_folder).name}' folder. To use a different "
            "folder from now on, click the 'Change...' button at the top of your Library.",
        )

    def _report_settings_problem(self) -> None:
        """Tell the user that their saved settings could not be used, and what to do about it."""
        if getattr(self, "_is_shutting_down", False):
            return
        kept, settings_mgr.damaged_copy = settings_mgr.damaged_copy, None  # told once per damaged file
        if kept is not None:
            dialogs.show_warning(
                self.root,
                "Settings Could Not Be Opened",
                "Your saved settings could not be opened, so the app started with its standard settings.\n\n"
                "Your songs and playlists are not affected. If you had chosen your own music folder, "
                "choose it again with the 'Change...' button at the top of your Library.\n\n"
                f"The file that could not be opened was kept as '{kept.name}' in your '{kept.parent.name}' "
                "folder, in case someone can help you recover it.",
            )
        elif settings_mgr.read_failed:
            dialogs.show_warning(
                self.root,
                "Settings Could Not Be Opened",
                "Another program is using your saved settings, so the app started with its standard "
                "settings. Your saved settings are kept as they are.\n\n"
                "Close Ultimate Audio Studio and open it again to get your settings back.",
            )

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
        """Save the window's preferences now (also cancels a pending debounced save)."""
        # When called directly (e.g. from on_close) the pending timer would otherwise fire later
        # against a destroyed window.
        timer, self._timer_settings_save = self._timer_settings_save, None
        if timer is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(timer)
        if settings_mgr.read_failed:
            return  # the saved settings could not be read; saving would replace them with the defaults
        try:
            volume = 80
            if hasattr(self, "scale_volume"):
                volume = int(self.scale_volume.get())

            settings_mgr.update_settings(
                # A chosen folder that is offline stays chosen; only 'Change...' replaces it.
                library_folder=self._unavailable_library_folder or self.library_folder,
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
        except Exception:  # last-resort guard: a failed save must not break the window
            logging.getLogger(__name__).exception("Saving the settings failed")

    def _setup_drag_and_drop(self) -> None:
        """Receive dropped files and device changes from Windows.

        The handler calls back inside the window procedure, where tkinter must not be used (it ends
        the process). Both callbacks therefore only queue the news; ``_drain_ui_callbacks`` acts on it.
        """

        def _dropped(paths: list[str]) -> None:
            self._ui_callback_queue.put((self._handle_dropped_files, (paths,)))

        def _device_changed() -> None:
            self._ui_callback_queue.put((self._on_usb_hotplug, ()))

        self._dnd_handler = Win32DragDropHandler(
            self.root,
            _dropped,
            is_shutting_down_fn=lambda: self._is_shutting_down,
            device_change_callback=_device_changed,
        )
        self._dnd_handler.setup()

    def _teardown_drag_and_drop(self) -> None:
        if hasattr(self, "_dnd_handler") and self._dnd_handler:
            self._dnd_handler.teardown()

    def request_close(self) -> None:
        """Window close button: warn before stopping a download, export, clip save, or import."""
        reason = self.busy_reason()
        if reason and not dialogs.ask_yes_no(
            self.root,
            "Still Working",
            f"Ultimate Audio Studio is still busy with {reason}.\n\n"
            "If you close now, it will be stopped and may be left unfinished "
            "(for example, a USB drive with only some of the songs).",
            yes="Close and stop it",
            no="Keep working",
            danger=True,
            default_yes=False,
            icon=dialogs.ICON_WARNING,
        ):
            self.set_status(f"Still working on {reason}. You can close the app when it has finished.", icon="⏳")
            return
        self.on_close()

    def on_close(self) -> None:
        self._is_shutting_down = True
        self.download_ctrl.cancel()
        self.export_ctrl.cancel()
        self.update_ctrl.cancel_staging()
        task_mgr.shutdown(wait=False, cancel_futures=True)
        network_task_mgr.shutdown(wait=False, cancel_futures=True)
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
            "_pl_skip_timer",
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
        if self._restoring_original:
            return "restoring the original song"
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
        # Canvas drawings do not follow widget sizes by themselves: redraw them at the new size.
        self._art_cache.clear()
        if self.selected_file_path:
            self._load_album_art(self.selected_file_path)
        else:
            self._draw_placeholder_cover()
        self._draw_vu_meter(0)
        self._render_waveform(full_redraw=True)
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

    def show_undo(self, message: str, callback: Callable[[], None], timeout_sec: float = UNDO_SECONDS) -> None:
        """Offer Undo in the status bar for ``timeout_sec`` seconds after a deletion or removal."""
        self._undo_callback = callback
        if self._undo_timer:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._undo_timer)
            self._undo_timer = None

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
            self.library_ctrl.flush_pending_trash(background=True)

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
        # Keep Windows from sleeping mid-download/export (a finished search must not end that early).
        set_keep_awake(busy or self.busy_reason() is not None)
        if status:
            self.set_status(status, icon="⏳" if busy else "ℹ️")

    def _check_ffmpeg(self) -> None:
        if not os.path.exists(ffmpeg_path):
            dialogs.show_warning(
                self.root,
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
        """Open the Help Guide, or bring it to the front when it is already open."""
        existing = self._help_window
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return
        win = tk.Toplevel(self.root)
        self._help_window = win
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


_CRASH_LOG_MAX_BYTES = 512 * 1024
# faulthandler writes into this file at crash time, so it must stay open for the life of the process.
_crash_log_file: TextIO | None = None


def _install_crash_logging() -> None:
    """Record crashes that the normal error log cannot see.

    ``faulthandler`` dumps every thread's stack when native code (SDL/pygame, the ctypes window
    procedure, FFmpeg bindings) kills the process; the packaged app has no console, so without a
    file the crash would leave no trace. Uncaught exceptions in plain threads go to the error log.
    """
    global _crash_log_file
    crash_log = Path(ERROR_LOG_PATH).with_name("audio_studio_crash.txt")
    try:
        crash_log.parent.mkdir(parents=True, exist_ok=True)
        too_big = crash_log.is_file() and crash_log.stat().st_size > _CRASH_LOG_MAX_BYTES
        _crash_log_file = open(crash_log, "w" if too_big else "a", encoding="utf-8")
        faulthandler.enable(file=_crash_log_file, all_threads=True)
        # faulthandler does not see a fatal interpreter error: Python reports that one on stderr,
        # which the packaged app does not have.
        send_fatal_errors_to(crash_log)
    except OSError as e:
        logging.getLogger(__name__).warning("crash log unavailable: %s", e)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "unknown"
        details = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        log_error(f"Uncaught exception in thread {name}:\n{details}")

    threading.excepthook = _thread_hook


def _close_splash() -> None:
    """Close the start-up splash screen shown by the packaged .exe (no-op when run from source)."""
    if not getattr(sys, "frozen", False):
        return
    try:
        importlib.import_module("pyi_splash").close()
    except (ImportError, RuntimeError) as e:  # built without a splash, or it is already closed
        logging.getLogger(__name__).debug("splash close skipped: %s", e)


def _show_already_running() -> None:
    """A second start shows the window that is already open; the message is only a fallback."""
    _close_splash()
    if activate_existing_window(WINDOW_TITLE_PREFIX):
        return
    root = tk.Tk()
    root.withdraw()
    init_fonts(root)
    dialogs.show_info(
        root,
        "Already Open",
        "Ultimate Audio Studio is already open.\n\nLook for it on the taskbar at the bottom of the screen.",
    )
    root.destroy()


def _install_pending_update_at_startup() -> str | None:
    """Install a release downloaded during an earlier session, before any window opens.

    On success this process exits (the new version takes over). Returns a message for the user when
    the new version could not be installed or failed to start (this version then keeps running),
    else None. Either way that release is not tried again at the next start.
    """
    if not getattr(sys, "frozen", False):
        return None
    pending = load_pending_update()
    if pending is None:
        return None
    if pending.tag == failed_update_tag():
        clear_pending_update()
        return None
    result = install_update(pending.exe_path, pending.tag)
    already_running()  # take the single-instance lock back (it was released for the new version)
    return result.message


def _exit_process() -> None:
    """End the process once the window has closed (settings, playlists and caches are already saved).

    Helper programs are killed and the process exits at once, so a long FFmpeg encode or a network
    read cannot keep an invisible copy of the app running and blocking the next start.
    """
    stopped = terminate_child_processes()
    if stopped:
        logging.getLogger(__name__).info("Stopped %d helper process(es) at exit", stopped)
    release_instance_mutex()
    logging.shutdown()
    if _crash_log_file is not None:
        with contextlib.suppress(OSError):
            _crash_log_file.flush()
    os._exit(0)


# The previous version ends within moments of this one signalling that it started; until then
# Windows keeps its file locked. A few tries, a few seconds apart, are plenty.
_PREVIOUS_VERSION_FIRST_TRY_MS = 3000
_PREVIOUS_VERSION_TRIES = 10
_PREVIOUS_VERSION_WAIT_SEC = 3.0


def _remove_previous_version() -> None:
    """Delete the previous version's executable once it has stopped running (worker thread).

    Without this it stayed next to the app, usually on the Desktop, until the next start.
    """
    for attempt in range(_PREVIOUS_VERSION_TRIES):
        if cleanup_old_executables():
            return
        if attempt < _PREVIOUS_VERSION_TRIES - 1:
            time.sleep(_PREVIOUS_VERSION_WAIT_SEC)
    logging.getLogger(__name__).info("The previous version is still in use; it is removed at the next start")


def main() -> None:
    """Application entry point."""
    setup_logging()  # before anything logs: module loggers only reach the error log through this handler
    _install_crash_logging()
    if SELF_TEST_FLAG in sys.argv:
        _close_splash()
        sys.exit(self_test.main())
    if already_running():
        _show_already_running()
        sys.exit(0)

    rollback_message = _install_pending_update_at_startup()

    enable_windows_dpi()
    root = tk.Tk()
    _app = UltimateAudioStudio(root)  # keep a reference for the lifetime of the main loop
    # Tell a previous version waiting in install_update() that this one started properly.
    signal_named_event(UPDATE_OK_EVENT_NAME)
    root.after(_PREVIOUS_VERSION_FIRST_TRY_MS, lambda: task_mgr.submit_task(_remove_previous_version))
    root.after_idle(_close_splash)  # once the window is drawn, so there is no blank gap
    if rollback_message:
        root.after(800, lambda: dialogs.show_warning(root, "Update Not Installed", rollback_message))
    root.mainloop()
    _exit_process()


if __name__ == "__main__":
    main()

"""Shared base for the main window's feature mixins: typed state declarations and controller delegation.

``UltimateAudioStudio`` is assembled from one mixin per feature area (library, download, player,
clip editor, playlists, export, updates). Every mixin derives from :class:`AppBase`, which declares
the widgets and state they share so each module type-checks on its own. Nothing here holds values
at runtime except the :class:`ControllerState` descriptors.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from tkinter import ttk
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from app.config import DEFAULT_PLAYLIST_NAME

if TYPE_CHECKING:
    import subprocess

    from app.controllers import (
        DownloadController,
        ExportController,
        LibraryController,
        PlaybackController,
        PlaylistController,
        UpdateController,
    )
    from app.core.audio_engine import AudioEngine
    from app.models import ReleaseInfo, SongRow
    from app.platform_utils import Win32DragDropHandler
    from app.ui.components import ScrollableFrame
    from app.ui.waveform_view import WaveformView

__all__ = ["NEW_SONG_ROW_BG", "UNDO_SECONDS", "AppBase", "ControllerState", "UiCallback"]

T = TypeVar("T")
UiCallback = Callable[..., object]
# How long Undo stays in the status bar. Long enough to read the message, find the button and click
# it without hurry; the deleted song waits in the undo area meanwhile.
UNDO_SECONDS = 30.0
# Background of a Library row that was just added, or pointed out, without being selected.
NEW_SONG_ROW_BG = "#dcfce7"


class ControllerState(Generic[T]):
    """Expose one attribute of a controller as an app attribute (the controller is the single source of truth).

    Before the controller exists (early in ``__init__``) reads return ``default()`` and writes are ignored,
    matching the previous hand-written properties.
    """

    def __init__(self, controller_attr: str, default: Callable[[], T], convert: Callable[[Any], T]) -> None:
        self._controller_attr = controller_attr
        self._default = default
        self._convert = convert
        self._name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name

    def __get__(self, obj: object, objtype: type | None = None) -> T:
        controller = vars(obj).get(self._controller_attr) if obj is not None else None
        if controller is None:
            return self._default()
        value: T = getattr(controller, self._name)
        return value

    def __set__(self, obj: object, value: T) -> None:
        controller = vars(obj).get(self._controller_attr)
        if controller is not None:
            setattr(controller, self._name, self._convert(value))


def _identity(value: T) -> T:
    return value


class AppBase:
    """Declarations shared by all main-window mixins (see module docstring)."""

    # --- State owned by PlaybackController ---
    is_playing_main = ControllerState[bool]("playback_ctrl", lambda: False, bool)
    is_playing_playlist = ControllerState[bool]("playback_ctrl", lambda: False, bool)
    is_paused = ControllerState[bool]("playback_ctrl", lambda: False, bool)
    previewing_clip = ControllerState[bool]("playback_ctrl", lambda: False, bool)
    clip_end_time = ControllerState[float]("playback_ctrl", lambda: 0.0, float)
    play_start_offset = ControllerState[float]("playback_ctrl", lambda: 0.0, float)
    play_clock_origin = ControllerState[float | None]("playback_ctrl", lambda: None, _identity)
    play_guard_until = ControllerState[float]("playback_ctrl", lambda: 0.0, float)
    # True when Resume must reload the track (the user scrubbed, or another player used the mixer).
    _scrubbed_while_paused = ControllerState[bool]("playback_ctrl", lambda: False, bool)

    # --- State owned by PlaylistController ---
    playlists = ControllerState[dict[str, list[str]]]("playlist_ctrl", lambda: {DEFAULT_PLAYLIST_NAME: []}, _identity)
    active_playlist_name = ControllerState[str]("playlist_ctrl", lambda: DEFAULT_PLAYLIST_NAME, str)
    playlist_index = ControllerState[int]("playlist_ctrl", lambda: 0, int)

    if TYPE_CHECKING:
        # --- Core objects ---
        root: tk.Tk
        audio_engine: AudioEngine
        library_ctrl: LibraryController
        playback_ctrl: PlaybackController
        playlist_ctrl: PlaylistController
        export_ctrl: ExportController
        download_ctrl: DownloadController
        update_ctrl: UpdateController
        waveform_view: WaveformView
        _dnd_handler: Win32DragDropHandler | None

        # --- Library / track state ---
        default_lib_path: str
        library_folder: str
        library_files: list[str]
        visible_files: list[str]
        playlist_files: list[str]
        selected_file_path: str | None
        track_duration: float
        clip_start_sec: float
        clip_end_sec: float
        text_size: str
        waveform_zoomed: bool
        _dragging_marker: str | None
        _current_peaks: list[float]
        _current_loudness: float | None
        _current_cover_img: tk.PhotoImage | None
        _waveform_loading: bool
        _art_cache: OrderedDict[str, tk.PhotoImage | None]
        _max_art_cache: int
        _waveform_req_id: int
        _art_req_id: int
        _active_waveform_cancel: threading.Event
        _active_waveform_proc: subprocess.Popen[bytes] | None
        _waveform_queue: queue.Queue[tuple[str, int, threading.Event] | None]
        _art_queue: queue.Queue[tuple[str, int, int] | None]
        _ui_callback_queue: queue.Queue[tuple[UiCallback, tuple[Any, ...]]]
        _library_meta_gen: int
        _fresh_songs: set[str]
        _song_rows: dict[str, SongRow]
        _pending_library_song: str | None
        _pending_play_token: object | None
        _updating_ui: bool
        _progress_dragging: bool
        _busy: bool
        _exporting: bool
        _saving_clip: bool
        _importing: bool
        _restoring_original: bool
        _is_shutting_down: bool
        _is_checking_updates_manual: bool
        _available_update: ReleaseInfo | None
        _update_wanted_now: bool
        _usb_map: dict[str, str]
        _usb_fs_map: dict[str, str]
        _unmuted_volume: float
        _undo_callback: Callable[[], None] | None

        # --- Timers (Tk ``after`` ids) ---
        _selection_debounce_timer: str | None
        _search_debounce_timer: str | None
        _timer_watch_library: str | None
        _timer_update_check: str | None
        _timer_hotplug_debounce: str | None
        _monitor_timer: str | None
        _pl_skip_timer: str | None

        # --- Tk variables ---
        loop_clip: tk.BooleanVar
        repeat_playlist: tk.BooleanVar
        soften_clip: tk.BooleanVar
        even_volume: tk.BooleanVar
        auto_level_playback: tk.BooleanVar
        fade_choice_var: tk.StringVar
        usb_choice: tk.StringVar
        playlist_var: tk.StringVar
        export_var: tk.StringVar

        # --- Widgets (created by app/ui/views/*) ---
        col1: ScrollableFrame
        col2: ScrollableFrame
        col3: ScrollableFrame
        status: tk.Label
        lbl_status_icon: tk.Label
        btn_version_check: tk.Button
        btn_text_size: tk.Button
        btn_update_badge: tk.Button
        btn_undo: tk.Button
        entry_url: tk.Entry
        entry_search: tk.Entry
        btn_download: tk.Button
        btn_cancel_dl: tk.Button
        btn_load_ext: tk.Button
        prog_download: ttk.Progressbar
        lbl_dl_metrics: tk.Label
        listbox_lib: tk.Listbox
        lbl_lib_empty: tk.Label
        f_track_card: tk.Frame
        canvas_cover: tk.Canvas
        canvas_vu: tk.Canvas
        canvas_waveform: tk.Canvas
        lbl_selected: tk.Label
        lbl_selected_artist: tk.Label
        lbl_track_state: tk.Label
        btn_mute: tk.Button
        scale_volume: tk.Scale
        lbl_vol_pct: tk.Label
        chk_auto_level: tk.Checkbutton
        btn_zoom: tk.Button
        lbl_prog_time: tk.Label
        scale_progress: tk.Scale
        lbl_start_time: tk.Label
        lbl_end_time: tk.Label
        btn_edit_start: tk.Button
        btn_edit_end: tk.Button
        lbl_clip_len: tk.Label
        cmb_fade_dur: ttk.Combobox
        lbl_gain: tk.Label
        scale_gain: tk.Scale
        chk_loop_clip: tk.Checkbutton
        btn_save_clip: tk.Button
        f_restore_original: tk.Frame
        btn_restore_original: tk.Button
        cmb_playlists: ttk.Combobox
        listbox_pl: tk.Listbox
        cmb_usb: ttk.Combobox
        btn_usb_eject: tk.Button
        prog_export: ttk.Progressbar
        btn_export: tk.Button
        btn_cancel_export: tk.Button

        # --- Methods called across feature modules (defined in main.py or another mixin) ---
        def set_status(self, text: str, icon: str = "ℹ️") -> None: ...
        def set_busy(self, busy: bool, status: str | None = None) -> None: ...
        def notify_success(self, text: str, icon: str = "✅") -> None: ...
        def show_undo(self, message: str, callback: Callable[[], None], timeout_sec: float = UNDO_SECONDS) -> None: ...
        def _safe_after(self, delay: int, callback: UiCallback, *args: Any) -> None: ...
        def _save_settings(self) -> None: ...
        def _schedule_settings_save(self, *_args: object) -> None: ...
        def refresh_library(
            self, select_name: str | None = None, preserve_view: bool = False, files: list[str] | None = None
        ) -> None: ...
        def _cached_duration(self, path: str | None, probe: bool = True) -> float: ...
        def _cached_metadata(self, path: str | None, probe: bool = True) -> Mapping[str, Any]: ...
        def _display_name(self, path: str, filename: str | None = None) -> str: ...
        def _song_rows_for(self, paths: Sequence[str]) -> list[SongRow]: ...
        def _warm_library_metadata(self) -> None: ...
        def _library_row_path(self, filename: str) -> str: ...
        def play_main(self) -> None: ...
        def pause_audio(self) -> None: ...
        def stop_audio(self, user: bool = False) -> None: ...
        def _current_play_seconds(self) -> float: ...
        def _prog_label(self, current: float) -> str: ...
        def _seek_playback(self, seconds: float) -> None: ...
        def _draw_placeholder_cover(self) -> None: ...
        def _load_album_art(self, filepath: str) -> None: ...
        def _draw_vu_meter(self, level: int = 0) -> None: ...
        def _load_track_ui(self, path: str, title: str | None = None) -> bool: ...
        def _reveal_new_song(self, filename: str, keep_trim_work: bool = False) -> bool: ...
        def _has_trim_work(self) -> bool: ...
        def clear_search(self) -> None: ...
        def _load_pending_selection(self) -> bool: ...
        def _check_for_updates_on_launch(self) -> None: ...
        def _open_update_dialog(self) -> None: ...
        def _update_restore_original_button(self) -> None: ...
        def _release_audio_file(self) -> None: ...
        def _render_waveform(self, full_redraw: bool = False) -> None: ...
        def _set_card_playing_state(self, state: str) -> None: ...
        def _when_playable(
            self, path: str, start_fn: Callable[[], None], busy_text: str = "Preparing this song for playback..."
        ) -> None: ...
        def _update_clip_length_label(self) -> None: ...
        def reset_gain(self) -> None: ...
        def _restart_clip_loop(self) -> bool: ...
        def save_playlists(self) -> None: ...
        def refresh_playlist_listbox(self) -> None: ...
        def _relink_playlists(self) -> int: ...
        def _resync_playlist_index(self) -> None: ...
        def play_next_in_playlist(self) -> None: ...
        def _cancel_pending_skip(self) -> None: ...

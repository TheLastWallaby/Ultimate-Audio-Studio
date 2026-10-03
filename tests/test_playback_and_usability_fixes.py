"""Regression tests: music that stops by itself, PLAY after a song ends, export lock-up, themed prompts,
Undo time, update hints, off-screen window, long clips, and updates that cannot be installed."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import subprocess
import sys
import time
import tkinter as tk
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import app.main as main_module
from app.config import ffmpeg_path, is_valid_binary, settings_mgr
from app.controllers.playback_controller import PlaybackController
from app.core.audio_engine import AudioEngine
from app.core.cache_manager import cache_mgr
from app.core.errors import friendly_error
from app.core.process_utils import ProcessResult, ffmpeg_timed_out, run_ffmpeg
from app.main import UltimateAudioStudio, restorable_geometry
from app.models import ReleaseInfo, SearchResult
from app.platform_utils import is_on_a_monitor, kill_process_tree
from app.services import clipper, exporter, updater
from app.services.clipper import clip_audio_worker
from app.ui import dialogs
from app.ui.dialogs import DialogButton, TextAnswer
from app.ui.features import player as player_feature
from app.ui.features.base import UNDO_SECONDS
from app.ui.features.clip_editor import CLIP_REPLACE_ORIGINAL
from app.ui.features.export import EXPORT_BUTTON_TEXT
from app.ui.search_dialog import SearchChoiceDialog
from app.ui.theme import init_fonts

HAVE_FFMPEG = is_valid_binary(ffmpeg_path)
ON_WINDOWS = os.name == "nt"


def _pump(window: UltimateAudioStudio, until: Callable[[], bool], timeout: float = 5.0) -> None:
    """Run the window's event loop until ``until()`` is true, so timers and worker results arrive."""
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)


def _pump_for(window: UltimateAudioStudio, seconds: float) -> None:
    _pump(window, lambda: False, timeout=seconds)


def _library(window: UltimateAudioStudio, folder: Path, *names: str) -> list[str]:
    """Give the window a Library of small stand-in songs; returns their paths as the window spells them.

    Their details are put in the cache and the waveform and cover readers are switched off, so no
    background worker opens the files: a test may then rename or delete them straight away.
    """
    folder.mkdir(parents=True, exist_ok=True)
    window.library_folder = str(folder)
    window._load_waveform = lambda _path: None  # type: ignore[method-assign]
    window._load_album_art = lambda _path: None  # type: ignore[method-assign]
    paths = []
    for name in names:
        (folder / name).write_bytes(b"ID3" + bytes(64))
        path = window._library_row_path(name)
        cache_mgr.set_metadata(path, {"title": "", "artist": "", "duration": 100.0})
        paths.append(path)
    window.refresh_library()
    return paths


def _select_row(window: UltimateAudioStudio, name: str) -> None:
    """Highlight one Library row the way a click does (without the click's event)."""
    window.listbox_lib.selection_clear(0, tk.END)
    window.listbox_lib.selection_set(window.visible_files.index(name))


@contextlib.contextmanager
def _music_playing(window: UltimateAudioStudio, path: str) -> Iterator[None]:
    """Pretend ``path`` is playing: the test machine may have no sound device to really play it on."""
    assert window._load_track_ui(path, Path(path).name)
    window.playback_ctrl.is_playing_main = True
    window.audio_engine.seek_clock(5.0, is_playing=False)
    with patch.object(window.audio_engine, "is_busy", return_value=True):
        try:
            yield
        finally:
            window.playback_ctrl.is_playing_main = False
            window.playback_ctrl.is_playing_playlist = False
            window.playback_ctrl.is_paused = False


# --- 1. Music must not stop because of something unrelated -------------------------------------------


@pytest.fixture
def root() -> Iterator[tk.Tk]:
    window = tk.Tk()
    window.withdraw()
    init_fonts(window)
    try:
        yield window
    finally:
        window.destroy()


def _playing_engine() -> MagicMock:
    """A stand-in for the shared audio engine, in the middle of playing something."""
    engine = MagicMock()
    engine.is_busy.return_value = True
    engine.current_play_seconds.return_value = 1.0
    return engine


@contextlib.contextmanager
def _search_dialog(root: tk.Tk, engine: MagicMock) -> Iterator[SearchChoiceDialog]:
    results = [SearchResult("id1", "Song", "Artist", 200.0, "03:20", "https://example.invalid/1")]
    dialog = SearchChoiceDialog(root, "song", results, on_select=lambda _item: None, audio_engine=engine)
    try:
        yield dialog
    finally:
        # The closed dialog's last queue timer would otherwise fire on a window that is gone.
        for timer in (dialog._drain_timer, dialog._preview_poll_job):
            if timer:
                root.after_cancel(timer)


def test_cancelling_the_search_dialog_leaves_the_music_playing(root: tk.Tk) -> None:
    engine = _playing_engine()
    with _search_dialog(root, engine) as dialog:
        dialog._do_cancel()

    engine.stop.assert_not_called()
    engine.release_audio_file.assert_not_called()


def test_choosing_a_search_result_leaves_the_music_playing(root: tk.Tk) -> None:
    engine = _playing_engine()
    with _search_dialog(root, engine) as dialog:
        dialog._do_select()

    engine.stop.assert_not_called()
    engine.release_audio_file.assert_not_called()


def _window_size(window: tk.Toplevel) -> tuple[int, int]:
    width, height = window.geometry().split("+")[0].split("x")
    return int(width), int(height)


@pytest.mark.parametrize("text_size", ["Normal", "Extra Large"])
def test_search_dialog_is_big_enough_for_its_contents(root: tk.Tk, text_size: str) -> None:
    init_fonts(root, text_size)
    # A window only has a real size once it is on screen, and the dialog follows its parent there.
    root.geometry("+-3000+-3000")
    root.deiconify()
    root.update()
    with _search_dialog(root, _playing_engine()) as dialog:
        win = dialog.win
        win.update()
        width, height = _window_size(win)
        # Everything fits, unless the screen itself is smaller than the contents.
        assert width >= min(win.winfo_reqwidth(), win.winfo_screenwidth() - 40)
        assert height >= min(win.winfo_reqheight(), win.winfo_screenheight() - 80)
        # Roomier than the old fixed 760x560, which cut off the buttons on the right.
        assert width >= min(900, win.winfo_screenwidth() - 40)
        assert height >= min(660, win.winfo_screenheight() - 80)
        dialog._do_cancel()


def test_search_dialog_stops_the_preview_it_started(root: tk.Tk) -> None:
    engine = _playing_engine()
    with _search_dialog(root, engine) as dialog:
        dialog._on_preview_ready(dialog._preview_request_id, dialog.results[0], "preview.mp3")
        engine.play_preview.assert_called_once_with("preview.mp3")

        dialog._do_cancel()

    engine.release_audio_file.assert_called_once()
    # The song player's own clock and stop are left alone: a paused song keeps its position.
    engine.load_and_play.assert_not_called()
    engine.stop.assert_not_called()


def test_deleting_another_song_leaves_the_music_playing(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    playing, _other = _library(studio, tmp_path / "lib", "playing.mp3", "other.mp3")
    with _music_playing(studio, playing), patch.object(studio.audio_engine, "release_audio_file") as release:
        _select_row(studio, "other.mp3")
        with patch("app.ui.dialogs.ask_yes_no", return_value=True):
            studio.delete_library_file()

        release.assert_not_called()
        assert studio.is_playing_main
        assert studio.selected_file_path == playing
    assert not (tmp_path / "lib" / "other.mp3").exists()
    assert (tmp_path / "lib" / "playing.mp3").exists()


def test_deleting_the_song_in_the_player_stops_it_first(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (playing,) = _library(studio, tmp_path / "lib", "playing.mp3")
    with _music_playing(studio, playing):
        _select_row(studio, "playing.mp3")
        with patch("app.ui.dialogs.ask_yes_no", return_value=True):
            studio.delete_library_file()

        assert not studio.is_playing_main
        assert studio.selected_file_path is None
    assert not (tmp_path / "lib" / "playing.mp3").exists()


def test_renaming_another_song_leaves_the_music_playing(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    playing, _other = _library(studio, tmp_path / "lib", "playing.mp3", "other.mp3")
    with _music_playing(studio, playing), patch.object(studio.audio_engine, "release_audio_file") as release:
        _select_row(studio, "other.mp3")
        with patch("app.ui.dialogs.ask_text", return_value=TextAnswer("renamed")):
            studio.rename_library_file()

        release.assert_not_called()
        assert studio.is_playing_main
    assert sorted(p.name for p in (tmp_path / "lib").glob("*.mp3")) == ["playing.mp3", "renamed.mp3"]


def test_rename_that_windows_refuses_changes_nothing(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    studio.playlists["My Playlist"].append(song)
    _select_row(studio, "song.mp3")
    with (
        patch("app.ui.dialogs.ask_text", return_value=TextAnswer("renamed")),
        patch("app.controllers.library_controller.os.replace", side_effect=PermissionError(13, "in use")),
    ):
        studio.rename_library_file()

    assert sorted(p.name for p in (tmp_path / "lib").glob("*.mp3")) == ["song.mp3"]
    assert studio.playlists["My Playlist"] == [song]


# --- 2. PLAY after a song has finished ---------------------------------------------------------------


def test_play_after_a_song_finished_starts_from_the_beginning(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    with _music_playing(studio, song):
        studio.scale_progress.set(99.9)  # where the slider is when a 100-second song ends
        studio._song_finished()

        assert not studio.is_playing_main
        assert float(studio.scale_progress.get()) == 0.0
    with patch.object(studio.playback_ctrl, "play_track") as play:
        studio.play_main()
    play.assert_called_once_with(song, 0.0, is_playlist=False)


def test_play_with_the_slider_at_the_very_end_starts_over(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    assert studio._load_track_ui(song, "song.mp3")
    studio.scale_progress.set(99.8)

    with patch.object(studio.playback_ctrl, "play_track") as play:
        studio.play_main()

    play.assert_called_once_with(song, 0.0, is_playlist=False)


def test_play_from_the_middle_still_starts_at_the_slider(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    assert studio._load_track_ui(song, "song.mp3")
    studio.scale_progress.set(40.0)

    with patch.object(studio.playback_ctrl, "play_track") as play:
        studio.play_main()

    play.assert_called_once_with(song, 40.0, is_playlist=False)


# --- 4. A song whose length is unknown ---------------------------------------------------------------


def test_song_of_unknown_length_plays_until_the_mixer_says_it_ended(
    studio: UltimateAudioStudio, tmp_path: Path
) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    with _music_playing(studio, song):
        studio.track_duration = 0.0  # neither the file header nor ffprobe gave a length
        studio.play_guard_until = 0.0

        studio._monitor_tick()
        assert studio.is_playing_main  # the clock is past "0 seconds", but the music is still playing

        with patch.object(studio.audio_engine, "is_busy", return_value=False):
            studio._monitor_tick()
        assert not studio.is_playing_main


def test_clock_only_ends_a_song_well_past_its_known_length(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    with _music_playing(studio, song):
        studio.play_guard_until = 0.0
        studio.audio_engine.seek_clock(100.5, is_playing=False)  # just past the 100-second length
        studio._monitor_tick()
        assert studio.is_playing_main  # the mixer is still busy with the last notes

        studio.audio_engine.seek_clock(100.0 + player_feature.END_OF_SONG_GRACE_SEC + 0.1, is_playing=False)
        studio._monitor_tick()
        # The song's real length is measured first (on a worker); it is no longer than the header said.
        _pump(studio, lambda: not studio.is_playing_main)
        assert not studio.is_playing_main  # a mixer that never reports the end cannot hang the player


# --- 5. One failed monitor tick --------------------------------------------------------------------


def test_playback_monitor_keeps_running_after_a_failed_tick(
    studio: UltimateAudioStudio, caplog: pytest.LogCaptureFixture
) -> None:
    ticks: list[int] = []

    def _tick() -> None:
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("one bad tick")

    studio.root.after_cancel(studio._monitor_timer)  # the test runs the monitor itself, as its only chain
    with (
        patch.object(studio, "_monitor_tick", side_effect=_tick),
        patch.object(player_feature, "MONITOR_RETRY_MS", 10),
        caplog.at_level(logging.ERROR, logger="app.ui.features.player"),
    ):
        studio.monitor_audio()  # must not raise
        _pump(studio, lambda: len(ticks) >= 3)

    assert len(ticks) >= 3
    assert "playback monitor failed" in caplog.text


# --- 3. USB export to a drive that cannot be written -------------------------------------------------


def test_usb_export_makes_the_playlist_folder_itself(tmp_path: Path) -> None:
    song = tmp_path / "src" / "song0.mp3"
    song.parent.mkdir()
    song.write_bytes(b"ID3" + bytes(64))
    dest = tmp_path / "drive" / "Trip"
    reports: list[exporter.ExportReport] = []

    exporter.usb_export_worker(str(dest), "Trip", [str(song)], on_success=reports.append)

    assert [r.exported for r in reports] == [1]
    assert (dest / "01 - song0.mp3").read_bytes() == song.read_bytes()


def test_usb_export_reports_a_drive_it_cannot_write_to(tmp_path: Path) -> None:
    song = tmp_path / "song0.mp3"
    song.write_bytes(b"ID3" + bytes(64))
    (tmp_path / "drive").write_bytes(b"")  # a file where the drive should be: no folder can be made in it
    reports: list[exporter.ExportReport] = []
    errors: list[str] = []

    exporter.usb_export_worker(
        str(tmp_path / "drive" / "Trip"), "Trip", [str(song)], on_success=reports.append, on_error=errors.append
    )

    assert reports == []
    assert len(errors) == 1


def test_window_recovers_when_the_usb_drive_cannot_be_written(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    drive = tmp_path / "usb"
    drive.mkdir()
    # The playlist's folder name is taken by a file, so the folder cannot be created (as on a locked drive).
    (drive / studio.active_playlist_name).write_bytes(b"")
    label = "USB Drive: TEST (X:\\) [FAT32]"
    studio._usb_map, studio._usb_fs_map = {label: str(drive)}, {label: "FAT32"}
    studio.usb_choice.set(label)

    studio._export_to_usb([song], False)
    _pump(studio, lambda: "failed" in studio.status.cget("text"))  # the drive is read on a worker first

    assert not studio._exporting
    assert studio.busy_reason() is None
    assert studio.btn_export.cget("text") == EXPORT_BUTTON_TEXT
    assert str(studio.btn_export.cget("state")) == tk.NORMAL
    assert "failed" in studio.status.cget("text")


# --- 15. Clicking a Library song while music plays -----------------------------------------------------


def test_clicking_a_library_song_does_not_stop_the_music(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    playing, other = _library(studio, tmp_path / "lib", "playing.mp3", "other.mp3")
    with _music_playing(studio, playing), patch.object(studio, "stop_audio") as stop:
        _select_row(studio, "other.mp3")
        studio.on_library_select()
        _pump_for(studio, 0.3)  # longer than the click's delay

        stop.assert_not_called()
        assert studio.is_playing_main
        assert studio.selected_file_path == playing
        assert studio._pending_library_song == other
        assert "keeps playing" in studio.status.cget("text")


def test_play_switches_to_the_song_clicked_meanwhile(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    playing, other = _library(studio, tmp_path / "lib", "playing.mp3", "other.mp3")
    with _music_playing(studio, playing):
        _select_row(studio, "other.mp3")
        studio.on_library_select()
        with patch.object(studio.playback_ctrl, "play_track") as play:
            studio.play_main()

    play.assert_called_once_with(other, 0.0, is_playlist=False)
    assert studio.selected_file_path == other
    assert studio._pending_library_song is None


def test_stop_loads_the_song_clicked_meanwhile(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    playing, other = _library(studio, tmp_path / "lib", "playing.mp3", "other.mp3")
    with _music_playing(studio, playing):
        _select_row(studio, "other.mp3")
        studio.on_library_select()

        studio.stop_pressed()

        assert not studio.is_playing_main
        assert studio.selected_file_path == other
        assert "other.mp3" in studio.status.cget("text")


def test_finished_song_makes_way_for_the_song_clicked_meanwhile(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    playing, other = _library(studio, tmp_path / "lib", "playing.mp3", "other.mp3")
    with _music_playing(studio, playing):
        _select_row(studio, "other.mp3")
        studio.on_library_select()

        studio._song_finished()

        assert studio.selected_file_path == other
        assert float(studio.scale_progress.get()) == 0.0


def test_clicked_song_that_has_gone_is_forgotten(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    playing, _other = _library(studio, tmp_path / "lib", "playing.mp3", "other.mp3")
    with _music_playing(studio, playing):
        _select_row(studio, "other.mp3")
        studio.on_library_select()
        (tmp_path / "lib" / "other.mp3").unlink()  # removed in File Explorer meanwhile

        with patch.object(studio.playback_ctrl, "play_track") as play:
            studio.play_main()

    assert play.call_args.args[0] == playing
    assert studio._pending_library_song is None


def test_clicking_a_library_song_loads_it_when_nothing_is_playing(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    first, other = _library(studio, tmp_path / "lib", "first.mp3", "other.mp3")
    assert studio._load_track_ui(first, "first.mp3")
    _select_row(studio, "other.mp3")

    studio.on_library_select()
    _pump(studio, lambda: studio.selected_file_path == other)

    assert studio.selected_file_path == other
    assert studio._pending_library_song is None


# --- 16. Themed prompts instead of the system ones ----------------------------------------------------


def _descendants(widget: tk.Misc) -> list[tk.Misc]:
    found: list[tk.Misc] = []
    for child in widget.winfo_children():
        found.append(child)
        found.extend(_descendants(child))
    return found


def _in_dialog(root: tk.Tk, *steps: Callable[[tk.Toplevel], None]) -> None:
    """Once a dialog is open, run ``steps`` on it one after another; close it after 5 s whatever happens."""

    def _dialog() -> tk.Toplevel | None:
        return next((w for w in root.winfo_children() if isinstance(w, tk.Toplevel)), None)

    def _run(index: int) -> None:
        dialog = _dialog()
        if dialog is None:
            return
        steps[index](dialog)
        if index + 1 < len(steps):
            root.after(100, _run, index + 1)

    def _give_up() -> None:
        dialog = _dialog()
        if dialog is not None:
            dialog.destroy()

    root.after(150, _run, 0)
    root.after(5000, _give_up)


def _type(text: str) -> Callable[[tk.Toplevel], None]:
    def _step(dialog: tk.Toplevel) -> None:
        entry = next(w for w in _descendants(dialog) if isinstance(w, tk.Entry))
        entry.delete(0, tk.END)
        entry.insert(0, text)

    return _step


def _click(label: str) -> Callable[[tk.Toplevel], None]:
    def _step(dialog: tk.Toplevel) -> None:
        next(w for w in _descendants(dialog) if isinstance(w, tk.Button) and w.cget("text") == label).invoke()

    return _step


def _labels(dialog: tk.Toplevel) -> list[str]:
    return [str(w.cget("text")) for w in _descendants(dialog) if isinstance(w, tk.Label)]


@pytest.mark.real_dialogs
def test_text_prompt_returns_what_was_typed(root: tk.Tk) -> None:
    _in_dialog(root, _type("  Road Trip  "), _click("Create playlist"))

    answer = dialogs.ask_text(root, "New Playlist", "Type a name:", ok="Create playlist")

    assert answer == TextAnswer("Road Trip", dialogs.TEXT_OK)


@pytest.mark.real_dialogs
def test_text_prompt_starts_with_the_suggested_text(root: tk.Tk) -> None:
    _in_dialog(root, _click("Rename song"))

    answer = dialogs.ask_text(root, "Rename Song", "Type a new name:", initial="Old Name", ok="Rename song")

    assert answer == TextAnswer("Old Name")


@pytest.mark.real_dialogs
def test_text_prompt_asks_again_when_the_box_is_empty(root: tk.Tk) -> None:
    hints: list[list[str]] = []
    _in_dialog(root, _type("   "), _click("Create playlist"), lambda d: hints.append(_labels(d)), _click("Cancel"))

    answer = dialogs.ask_text(root, "New Playlist", "Type a name:", ok="Create playlist")

    assert answer is None  # it stayed open after the empty try, and was then cancelled
    assert any("type something" in text for text in hints[0])


@pytest.mark.real_dialogs
def test_text_prompt_extra_button_does_not_need_any_text(root: tk.Tk) -> None:
    _in_dialog(root, _type(""), _click("Replace the original song"))

    answer = dialogs.ask_text(
        root,
        "Save Your Clip",
        "Type a name:",
        ok="Save as a new song",
        extra=[DialogButton("Replace the original song", "replace")],
    )

    assert answer == TextAnswer("", "replace")


@pytest.mark.real_dialogs
def test_closing_the_text_prompt_cancels_it(root: tk.Tk) -> None:
    _in_dialog(root, lambda d: d.tk.call(d.tk.call("wm", "protocol", str(d), "WM_DELETE_WINDOW")))

    assert dialogs.ask_text(root, "New Playlist", "Type a name:", initial="Mix", ok="Create playlist") is None


def test_no_system_text_prompts_or_save_dialogs_remain() -> None:
    app_dir = Path(main_module.__file__).parent
    offenders = [
        path.name
        for path in app_dir.rglob("*.py")
        if "simpledialog" in path.read_text(encoding="utf-8") or "asksaveasfilename" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [path.name for path in app_dir.rglob("dialogs.py")]  # only its docstring mentions them


def test_new_playlist_is_created_from_the_themed_prompt(studio: UltimateAudioStudio) -> None:
    with patch("app.ui.dialogs.ask_text", return_value=TextAnswer("Road Trip")) as ask:
        studio.create_playlist()

    assert ask.call_args.kwargs["ok"] == "Create playlist"
    assert "Road Trip" in studio.playlists
    assert studio.active_playlist_name == "Road Trip"


def test_cancelled_playlist_prompt_creates_nothing(studio: UltimateAudioStudio) -> None:
    before = dict(studio.playlists)

    studio.create_playlist()  # the prompt answers None (cancelled) in tests

    assert studio.playlists == before


def test_clip_start_can_be_typed(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    assert studio._load_track_ui(song, "song.mp3")

    with patch("app.ui.dialogs.ask_text", return_value=TextAnswer("00:05")):
        studio.edit_start_time()
    assert studio.clip_start_sec == 5.0

    with (
        patch("app.ui.dialogs.ask_text", return_value=TextAnswer("soon")),
        patch("app.ui.dialogs.show_warning") as warn,
    ):
        studio.edit_start_time()
    warn.assert_called_once()
    assert studio.clip_start_sec == 5.0  # an unreadable time changes nothing


def _saved_clip_job(studio: UltimateAudioStudio, answer: TextAnswer | None) -> tuple[MagicMock, MagicMock]:
    """Press Save Clip with ``answer`` given to the name prompt; returns the prompt and the worker hand-off."""
    with (
        patch("app.ui.dialogs.ask_text", return_value=answer) as ask,
        patch("app.ui.features.clip_editor.task_mgr.submit_task") as submit,
    ):
        studio.save_clip()
    studio._saving_clip = False
    studio.set_busy(False)
    return ask, submit


def test_clip_is_saved_into_the_library_under_the_typed_name(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    assert studio._load_track_ui(song, "song.mp3")

    ask, submit = _saved_clip_job(studio, TextAnswer("My Clip.mp3"))

    assert ask.call_args.kwargs["initial"] == "song_clip_100s"
    worker, source, _start, _end, save_name, _soften, _gain, is_self_overwrite = submit.call_args.args[:8]
    assert worker is clip_audio_worker
    assert source == song
    assert Path(save_name) == tmp_path / "lib" / "My Clip.mp3"
    assert is_self_overwrite is False


def test_clip_never_takes_the_name_of_another_song(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    song, _other = _library(studio, tmp_path / "lib", "song.mp3", "other.mp3")
    assert studio._load_track_ui(song, "song.mp3")

    _ask, submit = _saved_clip_job(studio, TextAnswer("other"))

    save_name, is_self_overwrite = submit.call_args.args[4], submit.call_args.args[7]
    assert Path(save_name) == tmp_path / "lib" / "other (2).mp3"
    assert is_self_overwrite is False
    assert (tmp_path / "lib" / "other.mp3").read_bytes() == b"ID3" + bytes(64)


def test_replacing_the_original_is_its_own_button(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    assert studio._load_track_ui(song, "song.mp3")

    ask, submit = _saved_clip_job(studio, TextAnswer("ignored", CLIP_REPLACE_ORIGINAL))

    assert [button.label for button in ask.call_args.kwargs["extra"]] == ["Replace the original song"]
    save_name, is_self_overwrite = submit.call_args.args[4], submit.call_args.args[7]
    assert save_name == song  # the very path the player and the playlists use
    assert is_self_overwrite is True


def test_only_an_mp3_can_be_replaced_by_its_clip(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.wav")
    assert studio._load_track_ui(song, "song.wav")

    ask, _submit = _saved_clip_job(studio, None)

    assert ask.call_args.kwargs["extra"] == []


def test_cancelled_clip_prompt_saves_nothing(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    assert studio._load_track_ui(song, "song.mp3")

    with patch("app.ui.features.clip_editor.task_mgr.submit_task") as submit:
        studio.save_clip()  # the prompt answers None (cancelled) in tests

    submit.assert_not_called()
    assert not studio._saving_clip
    assert str(studio.btn_save_clip.cget("state")) == tk.NORMAL


# --- 17. Undo stays long enough to be used ----------------------------------------------------------


def test_undo_is_offered_for_half_a_minute_after_a_delete(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    _library(studio, tmp_path / "lib", "song.mp3")
    _select_row(studio, "song.mp3")
    with (
        patch("app.ui.dialogs.ask_yes_no", return_value=True),
        patch.object(studio.root, "after", wraps=studio.root.after) as after,
    ):
        studio.delete_library_file()

    assert UNDO_SECONDS >= 30
    assert int(UNDO_SECONDS * 1000) in [call.args[0] for call in after.call_args_list]
    assert studio.btn_undo.winfo_manager()  # the Undo button is showing

    studio._on_undo_click()
    assert (tmp_path / "lib" / "song.mp3").exists()


# --- 18. Download failures that an update fixes ------------------------------------------------------

YOUTUBE_CHANGED = "ERROR: [youtube] abc: Requested format is not available. Use --list-formats for a list"
OFFLINE = "<urlopen error [Errno 11001] getaddrinfo failed>"


def test_youtube_changes_are_explained_as_needing_an_update() -> None:
    for raw in (
        YOUTUBE_CHANGED,
        "ERROR: [youtube] abc: Unable to extract player response; please report this issue on https://github.com/",
        "WARNING: [youtube] abc: nsig extraction failed: Some formats may be missing",
    ):
        error = friendly_error(raw, "download")
        assert error.title == "This App Needs an Update", raw
        assert "Check for Updates" in error.message
        assert error.suggests_update


def test_other_failures_do_not_point_at_an_update() -> None:
    assert not friendly_error(OFFLINE, "download").suggests_update
    assert not friendly_error("Private video", "download").suggests_update
    assert not friendly_error("something odd", "export").suggests_update
    assert friendly_error("something odd", "download").suggests_update  # unknown download failure: worth a look


def test_write_protected_drive_is_explained_in_plain_words() -> None:
    error = friendly_error(
        "Could not complete USB export:\n[WinError 19] The media is write protected: 'E:\\\\Trip'", "export"
    )

    assert error.title == "Drive Is Locked"
    assert "lock switch" in error.message


def test_download_failure_from_a_youtube_change_looks_for_an_update(studio: UltimateAudioStudio) -> None:
    with patch.object(studio.update_ctrl, "check_on_launch") as check:
        studio._download_error(YOUTUBE_CHANGED)
        check.assert_called_once()

        check.reset_mock()
        studio._download_error(OFFLINE)
        check.assert_not_called()


def test_no_second_update_check_once_an_update_was_found(studio: UltimateAudioStudio) -> None:
    studio._available_update = ReleaseInfo("v9.9.9", "v9.9.9", "", "", 1, "a.exe", 2_000_000, "", "", "", "")
    with patch.object(studio.update_ctrl, "check_on_launch") as check:
        studio._download_error(YOUTUBE_CHANGED)

    check.assert_not_called()


# --- 11. A window saved on a screen that is gone -----------------------------------------------------


def test_window_position_outside_every_screen_is_not_restored() -> None:
    with patch.object(main_module, "is_on_a_monitor", return_value=False) as on_screen:
        assert restorable_geometry("1360x820+9000+200") is None

    left, top, right, bottom = on_screen.call_args.args
    assert (left, top) == (9000 + main_module.TITLE_BAR_INSET, 200)  # the title bar is what must be reachable
    assert (right, bottom) == (9000 + 1360 - main_module.TITLE_BAR_INSET, 200 + main_module.TITLE_BAR_HEIGHT)


def test_window_position_on_a_screen_is_restored() -> None:
    with patch.object(main_module, "is_on_a_monitor", return_value=True):
        assert restorable_geometry("1360x820+100+50") == "1360x820+100+50"
        assert restorable_geometry("1920x1017+-8+-8") == "1920x1017+-8+-8"  # how Windows reports a maximized window


def test_unusable_saved_sizes_are_not_restored() -> None:
    with patch.object(main_module, "is_on_a_monitor", return_value=True):
        assert restorable_geometry("800x500+10+10") is None  # too small for the three columns
        assert restorable_geometry("") is None
        assert restorable_geometry(None) is None
        assert restorable_geometry("not a geometry") is None
        assert restorable_geometry("1360x820") == "1360x820"  # no position: Windows chooses one


@pytest.mark.skipif(not ON_WINDOWS, reason="monitors are looked up through the Windows API")
def test_monitor_lookup_tells_on_screen_from_off_screen() -> None:
    assert is_on_a_monitor(100, 100, 500, 140)
    assert not is_on_a_monitor(90_000, 90_000, 91_000, 90_040)


@pytest.mark.skipif(not ON_WINDOWS, reason="monitors are looked up through the Windows API")
def test_window_saved_on_a_missing_screen_opens_where_it_can_be_seen() -> None:
    settings_mgr.update_settings(geometry="1360x820+90000+200")
    root = tk.Tk()
    root.withdraw()
    window = UltimateAudioStudio(root)
    try:
        root.update_idletasks()
        assert "+90000" not in root.geometry()
    finally:
        window.on_close()
        settings_mgr.update_settings(geometry=None)


# --- 13. Long clips --------------------------------------------------------------------------------

TIMED_OUT = ProcessResult(returncode=-1, stdout="", stderr="FFmpeg execution timed out after 120 seconds.")


def test_clip_time_limit_grows_with_the_clip() -> None:
    assert clipper._clip_timeout_sec(30.0) == 120  # short clips keep a comfortable minimum
    assert clipper._clip_timeout_sec(600.0) == 660  # ten minutes into the song: listening time plus a minute
    assert clipper._clip_timeout_sec(7200.0) == 1800  # capped


def test_long_clip_gets_the_longer_time_limit(tmp_path: Path) -> None:
    song = tmp_path / "album.mp3"
    song.write_bytes(b"original")

    def _ffmpeg(args: list[str], **_kwargs: object) -> ProcessResult:
        Path(args[-1]).write_bytes(b"trimmed")
        return ProcessResult(returncode=0, stdout="", stderr="")

    with patch("app.services.clipper.run_ffmpeg", side_effect=_ffmpeg) as ffmpeg:
        clip_audio_worker(str(song), 0.0, 900.0, str(tmp_path / "clip.mp3"))

    assert ffmpeg.call_args.kwargs["timeout"] == 960
    assert (tmp_path / "clip.mp3").read_bytes() == b"trimmed"


def test_clip_that_timed_out_is_not_tried_again_the_slow_ways(tmp_path: Path) -> None:
    song = tmp_path / "album.mp3"
    song.write_bytes(b"original")
    errors: list[str] = []
    with patch("app.services.clipper.run_ffmpeg", return_value=TIMED_OUT) as ffmpeg:
        clip_audio_worker(str(song), 0.0, 60.0, str(song), is_self_overwrite=True, on_error=errors.append)

    assert ffmpeg.call_count == 1  # no second FFmpeg run that would time out again
    assert len(errors) == 1
    assert song.read_bytes() == b"original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["album.mp3"]  # no backup, no leftover clip


def test_clip_ffmpeg_cannot_make_is_reported_after_the_last_try(tmp_path: Path) -> None:
    song = tmp_path / "album.mp3"
    song.write_bytes(b"x" * 64)
    errors: list[str] = []
    failed = ProcessResult(returncode=1, stdout="", stderr="Invalid data found when processing input")
    with patch("app.services.clipper.run_ffmpeg", return_value=failed) as ffmpeg:
        clip_audio_worker(str(song), 0.0, 60.0, str(tmp_path / "clip.mp3"), on_error=errors.append)

    # Cover copied, cover re-encoded, then no cover; each run has a time limit (no in-memory decode).
    assert ffmpeg.call_count == 3
    assert all(call.kwargs["timeout"] > 0 for call in ffmpeg.call_args_list)
    assert "0:v?" not in ffmpeg.call_args_list[-1].args[0]
    assert len(errors) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["album.mp3"]


def test_clip_is_saved_without_the_cover_when_the_picture_breaks_it(tmp_path: Path) -> None:
    song = tmp_path / "album.mp3"
    song.write_bytes(b"x" * 64)
    saved: list[str] = []

    def _picture_breaks_it(args: list[str], **_kwargs: object) -> ProcessResult:
        if "0:v?" in args:
            return ProcessResult(returncode=1, stdout="", stderr="Could not write header (bad picture)")
        Path(args[-1]).write_bytes(b"clip without cover")
        return ProcessResult(returncode=0, stdout="", stderr="")

    with patch("app.services.clipper.run_ffmpeg", side_effect=_picture_breaks_it):
        clip_audio_worker(
            str(song), 0.0, 60.0, str(tmp_path / "clip.mp3"), on_success=lambda name, *_a: saved.append(name)
        )

    assert saved == ["clip.mp3"]
    assert (tmp_path / "clip.mp3").read_bytes() == b"clip without cover"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs a working ffmpeg")
def test_a_real_timeout_is_recognised() -> None:
    # -re makes FFmpeg run in real time, so this would take 60 s without the timeout.
    timed_out = run_ffmpeg(["-re", "-f", "lavfi", "-i", "sine=duration=60", "-f", "null", "-"], timeout=1)
    finished = run_ffmpeg(["-version"])

    assert ffmpeg_timed_out(timed_out)
    assert not ffmpeg_timed_out(finished)


# --- 14. Updates that cannot be installed ------------------------------------------------------------


def _no_exit(_code: int) -> Any:
    raise AssertionError("install_update must not exit when the new version did not start")


@contextlib.contextmanager
def _packaged_app(folder: Path, outcome: str) -> Iterator[dict[str, Any]]:
    """Run ``install_update`` as the packaged app would: OLD is running, NEW is the download."""
    current = folder / "app.exe"
    current.write_bytes(b"OLD")
    new = folder / "download.exe"
    new.write_bytes(b"NEW")
    with (
        patch.object(sys, "frozen", True, create=True),
        patch.object(sys, "executable", str(current)),
        patch.object(updater, "create_named_event", return_value=1),
        patch.object(updater, "close_handle"),
        patch.object(updater, "release_instance_mutex"),
        patch.object(updater, "launch_update_process", return_value=MagicMock()) as launch,
        patch.object(updater, "wait_for_event_or_exit", return_value=outcome),
        patch.object(updater, "clear_pending_update"),
        patch.object(updater, "kill_process_tree") as kill,
        patch.object(updater.settings_mgr, "update_settings") as remember,
    ):
        yield {"current": current, "new": new, "launch": launch, "kill": kill, "remember": remember}


def test_new_version_that_never_opens_is_stopped_and_rolled_back(tmp_path: Path) -> None:
    with _packaged_app(tmp_path, "timeout") as app:
        result = updater.install_update(app["new"], "v9.9.9", exit_fn=_no_exit)

        app["kill"].assert_called_once_with(app["launch"].return_value)
        app["remember"].assert_called_once_with(failed_update_tag="v9.9.9")
    assert result.rolled_back
    assert app["current"].read_bytes() == b"OLD"
    assert "current version was kept" in result.message


def test_update_that_cannot_be_put_in_place_is_remembered_and_explained(tmp_path: Path) -> None:
    with _packaged_app(tmp_path, "signaled") as app, patch("shutil.move", side_effect=PermissionError(13, "denied")):
        result = updater.install_update(app["new"], "v9.9.9", exit_fn=_no_exit)

        app["launch"].assert_not_called()
        app["remember"].assert_called_once_with(failed_update_tag="v9.9.9")  # not tried again at every start
    assert app["current"].read_bytes() == b"OLD"
    assert not Path(str(app["current"]) + ".old").exists()
    assert "current version was kept" in result.message
    assert "denied" not in result.message and "Errno" not in result.message  # plain words only


def test_rollback_that_fails_is_not_reported_as_a_success(tmp_path: Path) -> None:
    with _packaged_app(tmp_path, "exited") as app, patch.object(updater, "_restore_previous", return_value=False):
        result = updater.install_update(app["new"], "v9.9.9", exit_fn=_no_exit)

    assert not result.rolled_back
    assert "could not be put back" in result.message
    assert "app.exe.old" in result.message  # where the working version is


def test_startup_tells_the_user_about_an_update_that_was_not_installed() -> None:
    pending = updater.PendingUpdate("v9.9.9", Path("staged.exe"), "0" * 64)
    with (
        patch.object(sys, "frozen", True, create=True),
        patch.object(main_module, "load_pending_update", return_value=pending),
        patch.object(main_module, "failed_update_tag", return_value=""),
        patch.object(main_module, "install_update", return_value=updater.InstallResult("It was not installed.")),
        patch.object(main_module, "already_running"),
    ):
        assert main_module._install_pending_update_at_startup() == "It was not installed."


def test_startup_does_not_retry_an_update_that_already_failed() -> None:
    pending = updater.PendingUpdate("v9.9.9", Path("staged.exe"), "0" * 64)
    with (
        patch.object(sys, "frozen", True, create=True),
        patch.object(main_module, "load_pending_update", return_value=pending),
        patch.object(main_module, "failed_update_tag", return_value="v9.9.9"),
        patch.object(main_module, "clear_pending_update") as clear,
        patch.object(main_module, "install_update") as install,
    ):
        assert main_module._install_pending_update_at_startup() is None

    install.assert_not_called()
    clear.assert_called_once()


def _has_exited(pid: int, timeout_ms: int = 5000) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return True
    try:
        return bool(kernel32.WaitForSingleObject(ctypes.c_void_p(handle), timeout_ms) == 0)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


@pytest.mark.skipif(not ON_WINDOWS, reason="process trees are stopped through Windows' taskkill")
def test_stopping_a_process_also_stops_the_process_it_started() -> None:
    # Like the packaged app: the process that was started starts a second one and waits.
    parent_code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE)
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline())

        kill_process_tree(parent)

        assert parent.poll() is not None
        assert _has_exited(child_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
        if parent.stdout is not None:
            parent.stdout.close()


# --- Seeking while Test Clip plays a rendered slice ---------------------------------------------------


@pytest.fixture
def slice_preview(tmp_path: Path) -> Iterator[tuple[PlaybackController, MagicMock]]:
    """A clip preview of 60-80 s playing from a rendered slice, with pygame's music player replaced."""
    slice_file = tmp_path / "audition.wav"
    slice_file.write_bytes(b"RIFF")
    music = MagicMock()
    with patch("app.controllers.playback_controller.pygame.mixer.music", music):
        ctrl = PlaybackController(None, AudioEngine())
        ctrl.start_audition(str(tmp_path / "song.mp3"), 60.0, 80.0, str(slice_file))
        music.reset_mock()
        yield ctrl, music
        ctrl.cleanup_audition_slice()


def test_seek_during_a_slice_preview_moves_within_the_slice(
    slice_preview: tuple[PlaybackController, MagicMock],
) -> None:
    ctrl, music = slice_preview

    landed = ctrl.seek(70.0, track_duration=200.0)

    assert landed == 70.0
    music.play.assert_called_once_with(start=10.0)  # 10 s into the slice, which starts at the clip's start
    assert 69.9 <= ctrl.current_play_seconds() < 70.5  # the timeline still shows song time


@pytest.mark.parametrize(("target", "landed"), [(5.0, 60.0), (150.0, 79.95)])
def test_seek_outside_the_clip_stays_in_the_preview(
    slice_preview: tuple[PlaybackController, MagicMock], target: float, landed: float
) -> None:
    ctrl, music = slice_preview

    assert ctrl.seek(target, track_duration=200.0) == pytest.approx(landed)
    assert music.play.call_args.kwargs["start"] == pytest.approx(landed - 60.0)


def test_skip_during_a_slice_preview_reports_where_it_went(
    slice_preview: tuple[PlaybackController, MagicMock],
) -> None:
    ctrl, _music = slice_preview
    ctrl.audio_engine.start_clock(75.0)

    assert ctrl.skip_by(10.0, track_duration=200.0) == pytest.approx(79.95)


def test_resume_after_scrubbing_a_paused_slice_preview_keeps_the_slice(
    slice_preview: tuple[PlaybackController, MagicMock],
) -> None:
    ctrl, music = slice_preview
    ctrl.pause()
    ctrl.seek(65.0, track_duration=200.0)

    ctrl.unpause(current_track_path="song.mp3")

    music.load.assert_called_once_with(ctrl._audition_slice_file)
    assert music.play.call_args.kwargs["start"] == pytest.approx(5.0)


def test_seek_in_a_plain_song_uses_the_song_position(tmp_path: Path) -> None:
    music = MagicMock()
    with patch("app.controllers.playback_controller.pygame.mixer.music", music):
        ctrl = PlaybackController(None, AudioEngine())
        ctrl.is_playing_main = True
        assert ctrl.seek(70.0, track_duration=200.0) == 70.0

    music.play.assert_called_once_with(start=70.0)


@pytest.mark.parametrize(
    ("slice_made", "expected", "absent"), [(True, "smooth fade", "could not"), (False, "could not", "smooth fade")]
)
def test_preview_status_mentions_effects_only_when_they_are_heard(
    studio: UltimateAudioStudio, tmp_path: Path, slice_made: bool, expected: str, absent: str
) -> None:
    slice_file = tmp_path / "audition.wav"
    slice_file.write_bytes(b"RIFF")
    with patch.object(studio.playback_ctrl, "start_audition"):
        studio._start_test_clip(
            str(tmp_path / "song.mp3"), 10.0, 20.0, str(slice_file) if slice_made else None, 0.0, True, 1.5, False
        )

    status = studio.status.cget("text")
    assert expected in status
    assert absent not in status


# --- Waveforms on a PC without FFmpeg or sound output (like the GitHub runners) -----------------------


@contextlib.contextmanager
def _no_ffmpeg_and_no_sound_device() -> Iterator[None]:
    import pygame

    with (
        patch("app.core.waveform.ffmpeg_path", "nonexistent_ffmpeg_binary.exe"),
        patch("pygame.mixer.get_init", return_value=None),
        patch("pygame.mixer.init", side_effect=pygame.error("No available audio device")),
    ):
        yield


def _tone_wav(path: Path, channels: int, width: int, rate: int = 44100, seconds: float = 1.0) -> Path:
    """A 440 Hz tone at about half of full scale, in every channel."""
    import math
    import wave

    frames = bytearray()
    full_scale = 2 ** (8 * width - 1) - 1
    for i in range(int(rate * seconds)):
        value = int(0.5 * full_scale * math.sin(2 * math.pi * 440 * i / rate))
        sample = (value + 128).to_bytes(1, "little") if width == 1 else value.to_bytes(width, "little", signed=True)
        frames.extend(sample * channels)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(bytes(frames))
    return path


@pytest.mark.parametrize(("channels", "width"), [(1, 2), (2, 2), (2, 3), (1, 1)])
def test_wav_waveform_needs_neither_ffmpeg_nor_a_sound_device(tmp_path: Path, channels: int, width: int) -> None:
    from app.core.waveform import analyze_audio

    song = _tone_wav(tmp_path / "tone.wav", channels, width)

    with _no_ffmpeg_and_no_sound_device():
        result = analyze_audio(str(song), n_bars=20)

    assert len(result.peaks) == 20
    assert min(result.peaks) > 0.5  # a steady tone: every bar is loud
    assert result.loudness_db is not None and -12.0 < result.loudness_db < -6.0  # half scale, about -9 dBFS


def test_unreadable_wav_gives_no_waveform_without_an_error(tmp_path: Path) -> None:
    from app.core.waveform import analyze_audio

    song = tmp_path / "broken.wav"
    song.write_bytes(b"RIFF\x00\x00\x00\x00not really a wave file")

    with _no_ffmpeg_and_no_sound_device():
        result = analyze_audio(str(song), n_bars=20)

    assert result.peaks == []

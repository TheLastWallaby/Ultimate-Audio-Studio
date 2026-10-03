"""Regression tests: exports that find no songs, truthful CD/USB reports, and background-work reliability."""

from __future__ import annotations

import json
import os
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import platform_utils
from app.controllers import download_controller, update_controller
from app.controllers.download_controller import DownloadController
from app.controllers.library_controller import LibraryController
from app.core import waveform
from app.core.cache_manager import CacheManager, cache_mgr
from app.core.file_utils import atomic_save_json
from app.core.task_manager import network_task_mgr, task_mgr
from app.core.waveform import AudioAnalysis
from app.main import UltimateAudioStudio
from app.models import SongRow, TrackMetadata
from app.services import exporter
from app.services.clipper import clip_audio_worker
from app.services.exporter import ExportReport
from app.ui import search_dialog
from app.ui.features import player as player_feature
from app.ui.features.playlists import playlist_length_text

ON_WINDOWS = os.name == "nt"


def _pump(window: UltimateAudioStudio, until: Callable[[], bool], timeout: float = 5.0) -> None:
    """Run the window's event loop until ``until()`` is true, so timers and worker results arrive."""
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)


def _songs(window: UltimateAudioStudio, folder: Path, *names: str, seconds: float = 100.0) -> list[str]:
    """Give the window a Library of stand-in songs of ``seconds`` each; returns their paths.

    Their details are put in the cache and the waveform and cover readers are switched off, so no
    background worker opens the files.
    """
    folder.mkdir(parents=True, exist_ok=True)
    window.library_folder = str(folder)
    window._load_waveform = lambda _path: None  # type: ignore[method-assign]
    window._load_album_art = lambda _path: None  # type: ignore[method-assign]
    paths = []
    for name in names:
        (folder / name).write_bytes(b"ID3" + bytes(64))
        path = window._library_row_path(name)
        cache_mgr.set_metadata(path, {"title": "", "artist": "", "duration": seconds})
        paths.append(path)
    window.refresh_library()
    return paths


def _set_playlist(window: UltimateAudioStudio, paths: list[str]) -> None:
    window.playlists[window.active_playlist_name] = list(paths)
    window.refresh_playlist_listbox()


def _titles(dialog: MagicMock) -> list[str]:
    """Titles of the dialogs shown (on a PC without FFmpeg the window adds a warning of its own)."""
    return [call.args[1] for call in dialog.call_args_list]


# --- 1. An export that finds none of its songs must leave the previous export alone -----------------


def test_usb_export_without_any_song_keeps_the_previous_export(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    old = drive / "01 - Old Song.mp3"
    old.write_bytes(b"old music")
    done: list[ExportReport] = []

    exporter.usb_export_worker(
        str(drive), "Trip", [str(tmp_path / "unplugged" / "gone.mp3")], on_success=done.append, clear_existing=True
    )

    assert old.read_bytes() == b"old music"  # removing it would have left the drive empty
    assert done == [ExportReport(total=1, exported=0, skipped=("gone.mp3 (file not found)",))]


def test_usb_export_with_a_song_still_replaces_the_previous_export(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "01 - Old Song.mp3").write_bytes(b"old music")
    song = tmp_path / "library" / "new.mp3"
    song.parent.mkdir()
    song.write_bytes(b"ID3" + bytes(64))

    exporter.usb_export_worker(str(drive), "Trip", [str(song)], clear_existing=True)

    assert sorted(p.name for p in drive.iterdir() if p.suffix == ".mp3") == ["01 - new.mp3"]


def test_cd_export_without_any_song_keeps_the_previous_tracks(tmp_path: Path) -> None:
    cd = tmp_path / "cd"
    cd.mkdir()
    old = cd / "01 - Old Song.wav"
    old.write_bytes(b"old track")

    exporter.cd_export_worker(str(cd), [str(tmp_path / "gone.mp3")], clear_existing=True)

    assert old.exists()


def test_export_of_a_playlist_whose_songs_are_all_missing_asks_nothing_about_the_drive(
    studio: UltimateAudioStudio, tmp_path: Path
) -> None:
    _set_playlist(studio, [str(tmp_path / "unplugged" / "a.mp3"), str(tmp_path / "unplugged" / "b.mp3")])

    with (
        patch("app.ui.dialogs.show_warning") as warning,
        patch.object(studio, "_export_after_preflight") as export,
    ):
        studio.export_playlist()
        _pump(studio, lambda: "Songs Not Found" in _titles(warning))

    assert "Songs Not Found" in _titles(warning)
    export.assert_not_called()
    assert str(studio.btn_export.cget("state")) == tk.NORMAL  # the button is not left on "Checking songs..."


def test_export_names_the_missing_songs_and_exports_only_the_ones_found(
    studio: UltimateAudioStudio, tmp_path: Path
) -> None:
    (found,) = _songs(studio, tmp_path / "lib", "found.mp3")
    _set_playlist(studio, [found, str(tmp_path / "unplugged" / "gone.mp3")])

    with (
        patch("app.ui.dialogs.ask_yes_no", return_value=True) as ask,
        patch.object(studio, "_export_after_preflight") as export,
    ):
        studio.export_playlist()
        _pump(studio, lambda: export.called)

    assert "gone.mp3" in ask.call_args.args[2]
    export.assert_called_once_with([found])


def test_export_with_missing_songs_can_be_cancelled_before_anything_happens(
    studio: UltimateAudioStudio, tmp_path: Path
) -> None:
    (found,) = _songs(studio, tmp_path / "lib", "found.mp3")
    _set_playlist(studio, [found, str(tmp_path / "unplugged" / "gone.mp3")])

    with (
        patch("app.ui.dialogs.ask_yes_no", return_value=False) as ask,
        patch.object(studio, "_export_after_preflight") as export,
    ):
        studio.export_playlist()
        _pump(studio, lambda: ask.called)

    export.assert_not_called()


# --- 2. "Ready to burn" and "export finished" only when that is true ---------------------------------


def test_cd_export_that_made_no_track_does_not_offer_to_burn(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    report = ExportReport(total=2, exported=0, skipped=("a (file not found)", "b (file not found)"))

    with (
        patch("app.ui.dialogs.show_warning") as warning,
        patch("app.ui.dialogs.show_info") as info,
        patch("app.ui.dialogs.ask_yes_no") as ask,
    ):
        studio._cd_export_success(str(tmp_path), report)

    assert _titles(warning) == ["CD Tracks Not Prepared"]
    info.assert_not_called()
    ask.assert_not_called()
    assert "ready" not in studio.status.cget("text").lower()


def test_cd_export_stopped_by_a_full_disk_does_not_offer_to_burn(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    report = ExportReport(total=5, exported=2, stopped="full")

    with (
        patch("app.ui.dialogs.show_warning") as warning,
        patch("app.ui.dialogs.show_info") as info,
        patch("app.ui.dialogs.ask_yes_no") as ask,
    ):
        studio._cd_export_success(str(tmp_path), report)

    assert _titles(warning) == ["Export Stopped"]
    assert "2 track(s) prepared so far" in warning.call_args.args[2]
    info.assert_not_called()
    ask.assert_not_called()


def test_usb_export_that_copied_no_song_is_not_called_finished(studio: UltimateAudioStudio) -> None:
    report = ExportReport(total=1, exported=0, skipped=("song.wma (could not be converted)",))

    with patch("app.ui.dialogs.show_warning") as warning, patch("app.ui.dialogs.ask_yes_no") as ask:
        studio._usb_export_success(report, "USB E", "E:\\", "Trip")

    assert _titles(warning) == ["No Songs Were Exported"]
    ask.assert_not_called()  # there is nothing new on the drive to eject it for
    assert "finished" not in studio.status.cget("text").lower()


# --- 3. Save Clip always reports back -----------------------------------------------------------------


def test_clip_save_into_a_folder_that_cannot_be_made_reports_the_error(tmp_path: Path) -> None:
    errors: list[str] = []
    saved: list[str] = []

    with patch("pathlib.Path.mkdir", side_effect=OSError("the drive is not connected")):
        clip_audio_worker(
            str(tmp_path / "song.mp3"),
            0.0,
            5.0,
            str(tmp_path / "gone" / "clip.mp3"),
            on_success=lambda name, _path, _own: saved.append(name),
            on_error=errors.append,
        )

    assert saved == []
    assert len(errors) == 1  # without it the window stayed on "Saving..." for good
    assert "not connected" in errors[0]


# --- 4. Worker results keep arriving while a dialog is open -------------------------------------------


def test_worker_results_arrive_while_an_earlier_one_waits_in_a_dialog(studio: UltimateAudioStudio) -> None:
    dialog_closed = tk.BooleanVar(master=studio.root, value=False)
    arrived: list[str] = []
    arrived_while_open: list[list[str]] = []

    def _dialog() -> None:  # what a result that opens a dialog does: wait until it is closed
        studio.root.wait_variable(dialog_closed)

    def _close_dialog() -> None:
        arrived_while_open.append(list(arrived))
        dialog_closed.set(True)

    studio._ui_callback_queue.put((_dialog, ()))
    studio._ui_callback_queue.put((arrived.append, ("export progress",)))
    studio.root.after(400, _close_dialog)
    _pump(studio, lambda: bool(arrived_while_open))

    assert arrived_while_open == [["export progress"]]


def test_a_failing_queued_update_does_not_stop_the_ones_behind_it(studio: UltimateAudioStudio) -> None:
    arrived: list[str] = []

    def _broken() -> None:
        raise RuntimeError("one bad update")

    studio._ui_callback_queue.put((_broken, ()))
    studio._ui_callback_queue.put((arrived.append, ("next",)))
    _pump(studio, lambda: bool(arrived))

    assert arrived == ["next"]


# --- 5. Internet work has a pool of its own -----------------------------------------------------------


def test_internet_work_uses_its_own_pool() -> None:
    for module in (download_controller, update_controller, search_dialog):
        assert module.task_mgr is network_task_mgr
    assert network_task_mgr is not task_mgr

    with patch.object(network_task_mgr, "submit_task") as submit:
        DownloadController(None).start_search("a song", 6, on_success=MagicMock(), on_error=MagicMock())
    submit.assert_called_once()


def test_stuck_internet_jobs_do_not_hold_up_work_on_the_users_files() -> None:
    release = threading.Event()
    try:
        for _ in range(network_task_mgr.max_workers + 2):  # more hung searches than the pool has threads
            network_task_mgr.submit_task(release.wait, 10)

        future = task_mgr.submit_task(lambda: "clip saved")

        assert future is not None
        assert future.result(timeout=3) == "clip saved"
    finally:
        release.set()


# --- 6. A song longer than its header says is not cut off ---------------------------------------------


def test_song_longer_than_its_header_says_plays_on(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _songs(studio, tmp_path / "lib", "song.mp3")
    assert studio._load_track_ui(song, "song.mp3")
    studio.playback_ctrl.is_playing_main = True
    studio.play_guard_until = 0.0
    studio.audio_engine.seek_clock(100.0 + player_feature.END_OF_SONG_GRACE_SEC + 0.1, is_playing=False)

    with (
        patch.object(studio.audio_engine, "is_busy", return_value=True),
        patch.object(player_feature, "probe_audio_duration", return_value=180.0) as probe,
    ):
        try:
            studio._monitor_tick()
            _pump(studio, lambda: studio.track_duration == 180.0)
            studio._monitor_tick()

            assert studio.track_duration == 180.0
            assert studio.is_playing_main  # it used to be stopped here, 80 seconds early
            assert float(studio.scale_progress.cget("to")) == 180.0
            assert studio.clip_end_sec == 180.0  # the clip still ends with the song
            assert cache_mgr.get_duration(song) == 180.0
            probe.assert_called_once()  # measured once, not at every tick
        finally:
            studio.playback_ctrl.is_playing_main = False


# --- 7. A clip that is still being saved is not a song ------------------------------------------------


def test_library_does_not_list_a_clip_that_is_still_being_saved(tmp_path: Path) -> None:
    for name in ("Song.mp3", f".clip_tmp_{os.getpid()}_1700000000000.mp3", "clip_tmp_notes.mp3", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")

    assert LibraryController(None).scan_files(str(tmp_path)) == ["clip_tmp_notes.mp3", "Song.mp3"]


def test_library_scan_of_an_unreadable_folder_is_empty(tmp_path: Path) -> None:
    not_a_folder = tmp_path / "file.txt"
    not_a_folder.write_text("x", encoding="utf-8")

    assert LibraryController(None).scan_files(str(not_a_folder)) == []


# --- 8. Saving waits out a briefly locked file ---------------------------------------------------------


def test_saving_json_retries_while_the_file_is_briefly_locked(tmp_path: Path) -> None:
    target = tmp_path / "playlists.json"
    target.write_text("{}", encoding="utf-8")
    real_replace = os.replace
    attempts: list[int] = []

    def _locked_once(src: str | Path, dest: str | Path) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError(13, "The process cannot access the file")
        real_replace(src, dest)

    with patch("app.core.file_utils.os.replace", side_effect=_locked_once):
        atomic_save_json(target, {"My Playlist": ["a.mp3"]})

    assert json.loads(target.read_text(encoding="utf-8")) == {"My Playlist": ["a.mp3"]}
    assert len(attempts) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["playlists.json"]  # no temporary file left behind


# --- 9. The next playlist song is made playable ahead of time ------------------------------------------


def test_next_playlist_song_is_converted_while_this_one_plays(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    first, second, third = (str(tmp_path / name) for name in ("one.mp3", "two.wma", "three.mp3"))
    _set_playlist(studio, [first, second, third])

    with patch("app.ui.features.playlists.task_mgr.submit_task") as submit:
        studio.playlist_index = 0
        studio._prepare_next_pl_track()
        submit.assert_called_once_with(studio.audio_engine.get_playable_audio_path, second)

        submit.reset_mock()
        studio.playlist_index = 1  # the next one is an MP3: nothing to convert
        studio._prepare_next_pl_track()
        studio.playlist_index = 2  # the last song, and Repeat is off: nothing comes next
        studio._prepare_next_pl_track()
        submit.assert_not_called()


# --- 11. The playlist says how long it is ---------------------------------------------------------------


def test_playlist_length_line() -> None:
    def rows(*seconds: float) -> list[SongRow]:
        return [SongRow(label="Song", seconds=s) for s in seconds]

    assert playlist_length_text([], for_cd=True) == ("", False)
    assert playlist_length_text(rows(215.0), for_cd=False) == ("1 song · 4 min", False)
    assert playlist_length_text(rows(1500.0, 1500.0), for_cd=True) == (
        "2 songs · 50 min of the 80 min one CD holds",
        False,
    )
    text, too_long = playlist_length_text(rows(3000.0, 2700.0), for_cd=True)
    assert too_long
    assert text == "⚠️ 2 songs · 1 h 35 min: more than the 80 min one CD holds"
    assert playlist_length_text(rows(3000.0, 2700.0), for_cd=False) == ("2 songs · 1 h 35 min", False)


def test_playlist_length_line_says_which_songs_are_not_counted() -> None:
    rows = [SongRow("A", seconds=120.0), SongRow("B"), SongRow("C", seconds=300.0, missing=True)]

    assert playlist_length_text(rows, for_cd=False) == ("3 songs · 2 min (2 not counted: length not known)", False)


def test_playlist_length_is_shown_and_measured_against_a_cd(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    paths = _songs(studio, tmp_path / "lib", "a.mp3", "b.mp3", seconds=2700.0)
    _set_playlist(studio, paths)
    assert studio.lbl_pl_total.cget("text") == "2 songs · 1 h 30 min"

    studio.export_var.set("CD")
    studio.on_export_target_changed()

    assert studio.lbl_pl_total.cget("text").startswith("⚠️ 2 songs · 1 h 30 min: more than the 80 min")


# --- 13. What the player holds stays what it shows -----------------------------------------------------


def test_a_song_that_is_gone_does_not_replace_the_one_in_the_player(
    studio: UltimateAudioStudio, tmp_path: Path
) -> None:
    (song,) = _songs(studio, tmp_path / "lib", "song.mp3")
    assert studio._load_track_ui(song, "song.mp3")

    with patch("app.ui.dialogs.show_warning") as warning:
        assert not studio._load_track_ui(str(tmp_path / "lib" / "deleted.mp3"), "deleted.mp3")

    assert "Song Not Found" in _titles(warning)
    assert studio.selected_file_path == song  # PLAY still plays the song the player shows


def test_selecting_several_songs_keeps_the_player_and_its_clip_marks(
    studio: UltimateAudioStudio, tmp_path: Path
) -> None:
    first, second = _songs(studio, tmp_path / "lib", "a first.mp3", "b second.mp3")
    assert studio._load_track_ui(second, "b second.mp3")
    studio.clip_start_sec = 20.0

    # Ctrl-click on the song above the loaded one, to add both to a playlist.
    studio.listbox_lib.selection_set(0, 1)
    studio.on_library_select()
    _pump(studio, lambda: False, timeout=0.4)  # a single click loads its song after 100 ms

    assert studio.selected_file_path == second
    assert studio.clip_start_sec == 20.0
    assert first != second


# --- 14. Waveforms of long recordings ------------------------------------------------------------------


@pytest.mark.parametrize(("seconds", "limit"), [(0.0, 60), (240.0, 27), (3600.0, 195), (4 * 3600.0, 300)])
def test_waveform_time_limit_grows_with_the_recording(seconds: float, limit: int) -> None:
    with patch.object(waveform, "read_track_metadata", return_value=TrackMetadata(duration=seconds)):
        assert waveform.analysis_timeout_sec("song.mp3") == limit


def test_cache_remembers_a_failed_waveform_until_the_file_changes(tmp_path: Path) -> None:
    cache = CacheManager(cache_dir=str(tmp_path / "cache"))
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3" + bytes(64))
    assert not cache.has_no_waveform(str(song))

    cache.set_no_waveform(str(song))
    assert cache.has_no_waveform(str(song))

    song.write_bytes(b"ID3" + bytes(128))  # replaced by another recording
    assert not cache.has_no_waveform(str(song))

    cache.set_no_waveform(str(song))
    cache.invalidate(str(song))
    assert not cache.has_no_waveform(str(song))
    assert not cache.has_no_waveform(str(tmp_path / "gone.mp3"))


def test_a_song_without_a_waveform_is_not_analysed_again(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    song = tmp_path / "long recording.mp3"
    song.write_bytes(b"ID3" + bytes(64))
    path = str(song)

    with patch.object(player_feature, "analyze_audio", return_value=AudioAnalysis([], None)) as analyse:
        studio._load_waveform(path)
        _pump(studio, lambda: cache_mgr.has_no_waveform(path))
        assert analyse.call_count == 1

        studio._load_waveform(path)  # the song is clicked again
        _pump(studio, lambda: False, timeout=0.3)

        assert analyse.call_count == 1
        assert not studio._waveform_loading  # the canvas says "unavailable", not "Analyzing..." for ever
    cache_mgr.invalidate(path)


# --- Only one copy of the app -----------------------------------------------------------------------------


@pytest.mark.skipif(not ON_WINDOWS, reason="the single-instance lock is a Windows mutex")
def test_a_second_start_finds_the_copy_that_is_already_open() -> None:
    handles: list[int | None] = []

    def _check() -> bool:
        found = platform_utils.already_running()
        handles.append(platform_utils._instance_mutex)
        return found

    try:
        _check()  # this process now holds the lock (unless the real app is open: then it does)
        assert _check()
        # The answer is the error code ctypes kept for this call, not one asked from Windows later.
        with patch("app.platform_utils.ctypes.get_last_error", return_value=0):
            assert not _check()
    finally:
        for handle in handles:
            platform_utils.close_handle(handle)
        platform_utils._instance_mutex = None

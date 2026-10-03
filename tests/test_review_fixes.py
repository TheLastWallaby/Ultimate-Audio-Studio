"""Regression tests for the fixes from the October 2026 code review (bugs, data safety, usability)."""

from __future__ import annotations

import contextlib
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pygame
import pytest

from app.core.audio_engine import AudioEngine, NoAudioDeviceError
from app.core.cache_manager import cache_mgr
from app.core.errors import friendly_error
from app.ui.features.library import NEW_SONG_ROW_BG


def _pump(window: Any, until: Callable[[], bool], timeout: float = 5.0) -> None:
    """Run the window's event loop until ``until()`` is true, so worker results reach the UI thread."""
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)


def _library(window: Any, folder: Path, *names: str) -> list[str]:
    """Give the window a Library of small stand-in songs (details cached, no waveform or cover readers)."""
    folder.mkdir(parents=True, exist_ok=True)
    window.library_folder = str(folder)
    window._load_waveform = lambda _path: None
    window._load_album_art = lambda _path: None
    paths = []
    for name in names:
        (folder / name).write_bytes(b"ID3" + bytes(64))
        path = window._library_row_path(name)
        cache_mgr.set_metadata(path, {"title": "", "artist": "", "duration": 100.0})
        paths.append(path)
    window.refresh_library()
    return paths


def _select_row(window: Any, name: str) -> None:
    """Highlight one Library row the way a click does (without the click's event)."""
    window.listbox_lib.selection_clear(0, tk.END)
    window.listbox_lib.selection_set(window.visible_files.index(name))


# --- No sound device when the app started ---------------------------------------------------------------


def test_play_opens_speakers_that_were_not_ready_when_the_app_started(tmp_path: Path) -> None:
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3" + bytes(64))
    attempts: list[int] = []
    running: list[bool] = []

    def _init() -> None:
        attempts.append(1)
        if len(attempts) < 3:  # at start-up and at the first PLAY the speakers are still off
            raise pygame.error("No available audio device")
        running.append(True)

    with (
        patch("pygame.mixer.get_init", side_effect=lambda: (44100, -16, 2) if running else None),
        patch("pygame.mixer.init", side_effect=_init),
        patch("pygame.mixer.pre_init"),
        patch("pygame.mixer.quit") as quit_mixer,
        patch("pygame.mixer.music") as music,
    ):
        engine = AudioEngine()
        with pytest.raises(NoAudioDeviceError) as failed:
            engine.load_and_play(str(song))
        music.load.assert_not_called()

        engine.load_and_play(str(song))  # the speakers are on now
        engine.load_and_play(str(song))

    assert len(attempts) == 3  # a running mixer is never started again
    quit_mixer.assert_not_called()
    assert music.load.call_count == 2
    message = friendly_error(failed.value, "playback")
    assert message.title == "No Speakers or Headphones Found"
    assert "damaged" not in message.message


# --- A failed search or playlist read is not "no matches" -----------------------------------------------

OFFLINE = "ERROR: Unable to download API page: <urlopen error [Errno 11001] getaddrinfo failed>"


class _RunNow:
    """Stand-in for task_mgr that runs submitted work at once, on the calling thread."""

    def submit_task(self, fn: Callable[..., object], *args: object, **kwargs: object) -> None:
        fn(*args, **kwargs)


def test_an_offline_search_reports_an_error_not_an_empty_result() -> None:
    from yt_dlp.utils import DownloadError

    from app.controllers.download_controller import DownloadController

    results: list[object] = []
    errors: list[str] = []
    with (
        patch("yt_dlp.YoutubeDL") as ydl,
        patch("app.controllers.download_controller.task_mgr", _RunNow()),
    ):
        ydl.return_value.__enter__.return_value.extract_info.side_effect = DownloadError(OFFLINE)
        DownloadController(None).start_search("Moon River", 6, results.append, errors.append)

    assert results == []
    assert len(errors) == 1
    assert friendly_error(errors[0], "search").title == "No Internet Connection"


def test_an_unreachable_playlist_is_explained_and_not_called_private(studio: Any) -> None:
    from yt_dlp.utils import DownloadError

    url = "https://www.youtube.com/watch?v=abc&list=PL1"
    with (
        patch("yt_dlp.YoutubeDL") as ydl,
        patch("app.controllers.download_controller.task_mgr", _RunNow()),
        patch("app.ui.dialogs.ask_yes_no") as ask,
        patch("app.ui.features.download.show_friendly_error") as shown,
        patch.object(studio, "_check_for_updates_on_launch"),
    ):
        ydl.return_value.__enter__.return_value.extract_info.side_effect = DownloadError(OFFLINE)
        studio._check_playlist(url)
        _pump(studio, lambda: shown.called)

    ask.assert_not_called()  # the "private or empty" question
    shown.assert_called_once()
    assert friendly_error(shown.call_args[0][1], "download").title == "No Internet Connection"
    assert str(studio.btn_download.cget("state")) == "normal"


def test_a_playlist_that_fails_while_downloading_reports_the_error() -> None:
    from yt_dlp.utils import DownloadError

    from app.controllers.download_controller import DownloadController

    errors: list[str] = []
    controller = DownloadController(None)
    with (
        patch("yt_dlp.YoutubeDL") as ydl,
        patch("app.controllers.download_controller.task_mgr", _RunNow()),
        patch("app.controllers.download_controller.cleanup_partial_downloads"),
    ):
        ydl.return_value.__enter__.return_value.extract_info.side_effect = DownloadError(OFFLINE)
        controller.start_playlist(
            "https://www.youtube.com/playlist?list=PL1",
            "lib",
            *[lambda *_a: None] * 7,
            on_error=errors.append,
        )

    assert errors and "getaddrinfo" in errors[0]
    assert controller.is_downloading is False


# --- Drives without a Recycle Bin -----------------------------------------------------------------------


def test_a_fixed_disk_has_a_recycle_bin_and_a_usb_stick_has_none(tmp_path: Path) -> None:
    import ctypes

    from app.platform_utils import has_recycle_bin

    assert has_recycle_bin(tmp_path)  # the test folder is on the PC's own disk
    drive_removable = 2
    with patch.object(ctypes.windll.kernel32, "GetDriveTypeW", return_value=drive_removable):
        assert not has_recycle_bin(r"E:\Music\song.mp3")


def test_deleting_on_a_drive_without_a_recycle_bin_says_it_is_for_good(studio: Any, tmp_path: Path) -> None:
    _library(studio, tmp_path / "lib", "song.mp3", "other.mp3")
    _select_row(studio, "song.mp3")
    with (
        patch("app.ui.features.library.has_recycle_bin", return_value=False),
        patch("app.ui.dialogs.ask_yes_no", return_value=True) as ask,
    ):
        studio.delete_library_file()

    question = ask.call_args[0][2]
    assert "Recycle Bin" in question and "for good" in question and "moved safely" not in question
    assert "Recycle Bin" not in studio.status.cget("text")
    assert "Undo" in studio.status.cget("text")
    studio.hide_undo(flush=False)


def test_deleting_on_a_normal_disk_still_mentions_the_recycle_bin(studio: Any, tmp_path: Path) -> None:
    _library(studio, tmp_path / "lib", "song.mp3")
    _select_row(studio, "song.mp3")
    with patch("app.ui.dialogs.ask_yes_no", return_value=True) as ask:
        studio.delete_library_file()

    assert "moved safely to your Windows Recycle Bin" in ask.call_args[0][2]
    assert studio.status.cget("text") == "Moved 'song.mp3' to Recycle Bin."
    studio.hide_undo(flush=False)


# --- A playlist download leaves the Library selection alone ---------------------------------------------


def test_songs_finished_by_a_playlist_download_do_not_take_the_selection(studio: Any, tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _library(studio, lib, "chosen.mp3", "other.mp3")
    _select_row(studio, "chosen.mp3")
    with patch.object(studio.download_ctrl, "start_playlist") as start:
        studio._start_playlist_download("https://www.youtube.com/playlist?list=PL1")
    callbacks = start.call_args.kwargs
    (lib / "new song.mp3").write_bytes(b"ID3" + bytes(64))

    callbacks["on_track_finished"](1, 2, "new song.mp3")

    selected = [studio.visible_files[i] for i in studio.listbox_lib.curselection()]
    assert selected == ["chosen.mp3"]
    row = studio.visible_files.index("new song.mp3")
    assert studio.listbox_lib.itemcget(row, "background") == NEW_SONG_ROW_BG

    studio.add_to_playlist()
    assert [Path(p).name for p in studio.playlist_files] == ["chosen.mp3"]

    callbacks["on_batch_complete"](["new song.mp3"], 1)
    assert [studio.visible_files[i] for i in studio.listbox_lib.curselection()] == ["chosen.mp3"]


# --- Stop a download, then search at once ---------------------------------------------------------------


class _Queued:
    """Stand-in for task_mgr that keeps submitted work until the test runs it."""

    def __init__(self) -> None:
        self.jobs: list[Callable[[], object]] = []

    def submit_task(self, fn: Callable[..., object], *args: object, **kwargs: object) -> None:
        self.jobs.append(lambda: fn(*args, **kwargs))


def test_a_search_right_after_stopping_a_download_does_not_leave_the_app_downloading() -> None:
    from yt_dlp.utils import DownloadCancelled

    from app.controllers.download_controller import DownloadController

    controller = DownloadController(None)
    tasks = _Queued()
    with (
        patch("app.controllers.download_controller.task_mgr", tasks),
        patch("app.controllers.download_controller.cleanup_partial_downloads"),
        patch("app.services.downloader.yt_dlp.YoutubeDL") as ydl,
        patch("app.controllers.download_controller.search_youtube_worker"),
    ):
        ydl.return_value.__enter__.return_value.extract_info.side_effect = DownloadCancelled("Stopped by user")
        controller.start_download("https://youtu.be/abc", "lib", *[lambda *_a: None] * 4)
        controller.cancel()  # Stop, while the download is still winding down
        controller.start_search("Moon River", 6, lambda _r: None, lambda _e: None)
        tasks.jobs[0]()  # the stopped download finishes winding down only now

    assert controller.is_downloading is False


# --- A playlist whose songs are missing -----------------------------------------------------------------


def _set_playlist(window: Any, paths: list[str]) -> None:
    window.playlist_ctrl.playlists[window.active_playlist_name] = list(paths)
    window.refresh_playlist_listbox()


def test_a_playlist_with_every_song_missing_stops_even_with_repeat_on(studio: Any, tmp_path: Path) -> None:
    _set_playlist(studio, [str(tmp_path / f"gone{i}.mp3") for i in range(3)])
    studio.repeat_playlist.set(True)
    with patch("app.ui.dialogs.show_warning") as warned:
        studio.play_playlist()
        _pump(studio, lambda: False, timeout=2.0)  # long enough for the three songs to be passed twice

    warned.assert_called_once()
    assert warned.call_args[0][1] == "Songs Not Found"
    assert studio._pl_skip_timer is None
    assert not studio.is_playing_playlist


def test_stop_cancels_a_pending_skip_past_a_missing_song(studio: Any, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "real.mp3")
    _set_playlist(studio, [str(tmp_path / "gone.mp3"), song])
    with patch.object(studio.playback_ctrl, "play_track") as play:
        studio.play_playlist()
        assert studio._pl_skip_timer is not None
        studio.stop_pressed()
        _pump(studio, lambda: False, timeout=0.8)

    play.assert_not_called()
    assert studio.selected_file_path != song


# --- A new playlist while another one plays -------------------------------------------------------------


def test_creating_a_playlist_stops_the_playlist_that_was_playing(studio: Any, tmp_path: Path) -> None:
    from app.ui.dialogs import TextAnswer

    songs = _library(studio, tmp_path / "lib", "a.mp3", "b.mp3")
    _set_playlist(studio, songs)
    studio.playlist_index = 0
    studio.playback_ctrl.is_playing_playlist = True
    with patch("app.ui.dialogs.ask_text", return_value=TextAnswer("Party")):
        studio.create_playlist()

    assert studio.active_playlist_name == "Party"
    assert not studio.is_playing_playlist
    with patch.object(studio.playback_ctrl, "play_track") as play:
        studio._song_finished()  # what the monitor does when the old song runs out
    play.assert_not_called()


# --- Relinking playlist songs ---------------------------------------------------------------------------


def _unused_drive_letter() -> str:
    for letter in "ZYXWVUTSRQPONMLKJIHGFED":
        if not Path(f"{letter}:\\").exists():
            return letter
    pytest.skip("every drive letter is in use")


def test_songs_on_an_unplugged_drive_are_not_relinked_to_namesakes(tmp_path: Path) -> None:
    from app.controllers.playlist_controller import PlaylistController

    library = tmp_path / "Music"
    library.mkdir()
    (library / "01 Track 1.wma").write_bytes(b"other song")
    on_usb = f"{_unused_drive_letter()}:\\My Music\\01 Track 1.wma"
    moved = str(tmp_path / "old folder" / "01 Track 1.wma")  # this drive is here: the folder was moved
    controller = PlaylistController(None)
    controller.playlists = {"Car": [on_usb], "Kitchen": [moved]}

    assert controller.relink_missing(str(library)) == 1

    assert controller.playlists["Car"] == [on_usb]
    assert controller.playlists["Kitchen"] == [str(library / "01 Track 1.wma")]


# --- Written through to the disk before the swap --------------------------------------------------------


def _record_order(module: str, events: list[str]) -> contextlib.ExitStack:
    """Patch ``os.fsync`` and ``os.replace`` (as seen by ``module``) to note the order they run in."""
    import os

    real_fsync, real_replace = os.fsync, os.replace

    def _fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def _replace(src: Any, dst: Any) -> None:
        events.append("replace")
        real_replace(src, dst)

    stack = contextlib.ExitStack()
    stack.enter_context(patch(f"{module}.os.fsync", side_effect=_fsync))
    stack.enter_context(patch(f"{module}.os.replace", side_effect=_replace))
    return stack


def test_a_copied_song_is_on_the_disk_before_it_is_renamed_into_place(tmp_path: Path) -> None:
    from app.core.file_utils import copy_file_atomic

    src = tmp_path / "song.mp3"
    src.write_bytes(b"ID3" + bytes(4096))
    events: list[str] = []
    with _record_order("app.core.file_utils", events):
        copy_file_atomic(src, tmp_path / "copy.mp3")

    assert events == ["fsync", "replace"]
    assert (tmp_path / "copy.mp3").read_bytes() == src.read_bytes()


def test_saved_settings_and_playlists_are_on_the_disk_before_the_swap(tmp_path: Path) -> None:
    from app.core.file_utils import atomic_save_json

    events: list[str] = []
    with _record_order("app.core.file_utils", events):
        atomic_save_json(tmp_path / "playlists.json", {"My Playlist": ["a.mp3"]})

    assert events == ["fsync", "replace"]


def test_a_clip_is_on_the_disk_before_it_replaces_the_song(tmp_path: Path) -> None:
    from app.core.process_utils import ProcessResult
    from app.services import clipper

    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3" + bytes(4096))
    events: list[str] = []

    def _ffmpeg(args: list[str], **_kwargs: object) -> ProcessResult:
        Path(args[-1]).write_bytes(b"ID3 clip")
        return ProcessResult(0, "", "")

    done: list[str] = []
    with (
        patch.object(clipper, "run_ffmpeg", side_effect=_ffmpeg),
        patch.object(clipper, "fsync_file", side_effect=lambda p: events.append(f"fsync {Path(p).suffix}")),
        patch.object(clipper, "_replace_with_retry", side_effect=lambda s, d: events.append("replace")),
    ):
        clipper.clip_audio_worker(song, 1.0, 2.0, song, is_self_overwrite=True, on_success=lambda *a: done.append("ok"))

    assert events == ["fsync .mp3", "replace"]
    assert done == ["ok"]

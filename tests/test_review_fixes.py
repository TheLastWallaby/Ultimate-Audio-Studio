"""Regression tests for the fixes from the October 2026 code review (bugs, data safety, usability)."""

from __future__ import annotations

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

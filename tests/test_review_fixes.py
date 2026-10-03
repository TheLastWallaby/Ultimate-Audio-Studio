"""Regression tests for the fixes from the October 2026 code review (bugs, data safety, usability)."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pygame
import pytest

from app.core.audio_engine import AudioEngine, NoAudioDeviceError
from app.core.errors import friendly_error


def _pump(window: Any, until: Callable[[], bool], timeout: float = 5.0) -> None:
    """Run the window's event loop until ``until()`` is true, so worker results reach the UI thread."""
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)


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

"""Regression tests for the fixes from the October 2026 code review (bugs, data safety, usability)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pygame
import pytest

from app.core.audio_engine import AudioEngine, NoAudioDeviceError
from app.core.errors import friendly_error

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

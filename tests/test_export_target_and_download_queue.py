"""Regression tests: the export's playlist, crash recovery, tall dialogs, and the download queue."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from app.controllers.library_controller import LibraryController
from app.core.cache_manager import cache_mgr
from app.main import UltimateAudioStudio
from app.services import clipper


def _pump(window: UltimateAudioStudio, until: Callable[[], bool], timeout: float = 5.0) -> None:
    """Run the window's event loop until ``until()`` is true, so timers and worker results arrive."""
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)


def _song(folder: Path, name: str = "song.mp3", seconds: float = 100.0) -> str:
    """A stand-in song whose length is already known, so no worker has to open the file."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"ID3" + bytes(64))
    cache_mgr.set_metadata(str(path), {"title": "", "artist": "", "duration": seconds})
    return str(path)


def _usb_drive(window: UltimateAudioStudio, drive: Path) -> None:
    """Make ``drive`` the chosen USB drive (the drive scan started with the window is dropped)."""
    drive.mkdir(parents=True, exist_ok=True)
    window._usb_scan_id += 1
    window._usb_map = {"USB": str(drive)}
    window._usb_fs_map = {"USB": "FAT32"}
    window.usb_choice.set("USB")
    window.export_var.set("USB")


# --- 1. An export goes to the playlist that was showing when Export was clicked ---------------------


def test_export_keeps_its_playlist_when_another_one_is_chosen_during_the_check(
    studio: UltimateAudioStudio, tmp_path: Path
) -> None:
    studio.playlists["Car"] = [_song(tmp_path / "lib")]
    studio.playlists["Other"] = []
    studio.active_playlist_name = "Car"
    studio.refresh_playlist_dropdown()
    studio.refresh_playlist_listbox()
    _usb_drive(studio, tmp_path / "usb")

    with patch.object(studio.export_ctrl, "start_usb_export") as start:
        studio.export_playlist()
        studio.playlist_var.set("Other")  # chosen while "Checking songs..." is still running
        studio.on_playlist_selected()
        _pump(studio, lambda: start.called)

    assert start.called
    assert start.call_args.args[1] == "Car"


def test_missing_songs_are_reported_for_the_playlist_that_was_exported(
    studio: UltimateAudioStudio, tmp_path: Path
) -> None:
    studio.playlists["Car"] = [str(tmp_path / "unplugged" / "gone.mp3")]
    studio.playlists["Other"] = []
    studio.active_playlist_name = "Car"
    studio.refresh_playlist_dropdown()
    studio.refresh_playlist_listbox()

    with patch("app.ui.dialogs.show_warning") as warning:
        studio.export_playlist()
        studio.playlist_var.set("Other")
        studio.on_playlist_selected()
        _pump(studio, lambda: any(call.args[1] == "Songs Not Found" for call in warning.call_args_list))

    messages = [call.args[2] for call in warning.call_args_list if call.args[1] == "Songs Not Found"]
    assert messages and "'Car'" in messages[0]


# --- 2. A save that was interrupted between its two steps is finished at the next start -------------


def _other_run() -> int:
    """A process id that is not this one's: the leftovers of this run are never touched."""
    return 4242 if os.getpid() != 4242 else 4243


@pytest.mark.parametrize("kind", ["restoring", "incoming"])
def test_startup_puts_back_a_song_whose_swap_was_interrupted(tmp_path: Path, kind: str) -> None:
    # The old song is already in the Recycle Bin; its finished replacement was never renamed into place.
    ready = tmp_path / f"Song.mp3.{_other_run()}.{kind}"
    ready.write_bytes(b"the complete copy")
    backup = tmp_path / "Song.mp3.original.bak"
    backup.write_bytes(b"backup of the song that is gone")
    recycled: list[str] = []

    def recycle_bin(path: str) -> None:
        recycled.append(Path(path).name)
        Path(path).unlink()

    with patch.object(clipper, "_send2trash", recycle_bin):
        handled = LibraryController.recover_stranded_deletes(tmp_path)

    assert (tmp_path / "Song.mp3").read_bytes() == b"the complete copy"
    assert not ready.exists()
    # The backup belonged to the song that is gone, so it cannot be "restored" over this one.
    assert recycled == ["Song.mp3.original.bak"]
    assert handled == 1


def test_startup_removes_a_ready_copy_whose_song_is_still_there(tmp_path: Path) -> None:
    song = tmp_path / "Song.mp3"
    song.write_bytes(b"the song, untouched")
    backup = tmp_path / "Song.mp3.original.bak"
    backup.write_bytes(b"its original")
    ready = tmp_path / f"Song.mp3.{_other_run()}.restoring"
    ready.write_bytes(b"a copy made before the run ended")

    assert LibraryController.recover_stranded_deletes(tmp_path) == 1

    assert song.read_bytes() == b"the song, untouched"
    assert backup.read_bytes() == b"its original"
    assert not ready.exists()


def test_startup_never_takes_a_half_made_copy_for_a_song(tmp_path: Path) -> None:
    partial = tmp_path / f"Song.mp3.{_other_run()}.restoring.{_other_run()}.partial"
    partial.write_bytes(b"cut off")

    LibraryController.recover_stranded_deletes(tmp_path)

    assert not partial.exists()
    assert not (tmp_path / "Song.mp3").exists()


def test_a_copy_that_cannot_be_put_back_is_kept_for_the_next_start(tmp_path: Path) -> None:
    ready = tmp_path / f"Song.mp3.{_other_run()}.restoring"
    ready.write_bytes(b"the complete copy")

    with patch("app.controllers.library_controller.replace_with_retry", side_effect=PermissionError("locked")):
        assert LibraryController.recover_stranded_deletes(tmp_path) == 0

    assert ready.read_bytes() == b"the complete copy"

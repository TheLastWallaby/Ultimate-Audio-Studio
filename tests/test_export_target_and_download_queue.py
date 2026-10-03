"""Regression tests: the export's playlist, crash recovery, tall dialogs, and the download queue."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from app.core.cache_manager import cache_mgr
from app.main import UltimateAudioStudio


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

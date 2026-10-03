"""Regression tests: the export's playlist, crash recovery, tall dialogs, and the download queue."""

from __future__ import annotations

import io
import os
import time
import tkinter as tk
import urllib.request
from collections.abc import Callable, Iterator
from http.client import HTTPMessage
from pathlib import Path
from unittest.mock import patch

import pytest

from app.controllers.library_controller import LibraryController
from app.controllers.playback_controller import PlaybackController
from app.core.audio_engine import AudioEngine
from app.core.cache_manager import cache_mgr
from app.core.errors import ErrorContext, friendly_error
from app.main import UltimateAudioStudio
from app.services import clipper, exporter
from app.services.exporter import ExportReport
from app.services.updater import _GitHubAssetRedirectHandler
from app.ui import dialogs, error_dialog
from app.ui.theme import init_fonts


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


# --- 3. A dialog taller than the screen scrolls its message; its buttons stay on the screen ---------

# What a dialog must leave free of the screen's height for its title bar and the taskbar (at 100%).
SCREEN_MARGIN_PX = 110
LONG_MESSAGE = "\n".join(f"• Song number {n} could not be converted; the file may be damaged" for n in range(1, 201))


@pytest.fixture
def root() -> Iterator[tk.Tk]:
    """A hidden window with the app's fonts at the biggest Text Size."""
    window = tk.Tk()
    window.withdraw()
    init_fonts(window, "Extra Large")
    yield window
    window.destroy()


def _descendants(widget: tk.Misc) -> list[tk.Misc]:
    found = []
    for child in widget.winfo_children():
        found.append(child)
        found.extend(_descendants(child))
    return found


def _room_on_screen(window: tk.Misc) -> int:
    scale = max(1.0, window.winfo_fpixels("1i") / 96.0)
    return window.winfo_screenheight() - round(SCREEN_MARGIN_PX * scale)


def _inspect_then_click(root: tk.Tk, label: str, seen: dict[str, object]) -> None:
    """Once the dialog is up: note its height and what it shows, then click the button ``label``."""

    def _run() -> None:
        dialog = next((w for w in root.winfo_children() if isinstance(w, tk.Toplevel)), None)
        if dialog is None:
            root.after(50, _run)
            return
        try:
            dialog.update_idletasks()
            widgets = _descendants(dialog)
            seen["height"] = dialog.winfo_reqheight()
            seen["room"] = _room_on_screen(dialog)
            seen["text"] = "".join(w.get("1.0", tk.END) for w in widgets if isinstance(w, tk.Text))
            seen["scrolls"] = any(isinstance(w, tk.Scrollbar) for w in widgets)
            next(w for w in widgets if isinstance(w, tk.Button) and w.cget("text") == label).invoke()
        finally:
            if dialog.winfo_exists():  # never leave the test waiting on a dialog nobody will close
                dialog.destroy()

    root.after(100, _run)


@pytest.mark.real_dialogs
def test_a_dialog_taller_than_the_screen_scrolls_its_message(root: tk.Tk) -> None:
    seen: dict[str, object] = {}
    _inspect_then_click(root, "Open the CD folder", seen)

    answer = dialogs.ask_yes_no(root, "Ready to Burn", LONG_MESSAGE, yes="Open the CD folder", no="Not now")

    assert answer is True  # the button could be reached
    assert isinstance(seen["height"], int) and isinstance(seen["room"], int)
    assert seen["height"] <= seen["room"]
    assert seen["scrolls"]
    assert "Song number 1 " in str(seen["text"]) and "Song number 200 " in str(seen["text"])  # nothing was cut


@pytest.mark.real_dialogs
def test_a_long_error_message_scrolls_too(root: tk.Tk) -> None:
    seen: dict[str, object] = {}
    _inspect_then_click(root, "OK", seen)

    error_dialog.show_error(root, "Some Songs Could Not Be Deleted", LONG_MESSAGE)

    assert isinstance(seen["height"], int) and isinstance(seen["room"], int)
    assert seen["height"] <= seen["room"]
    assert "Song number 200 " in str(seen["text"])


@pytest.mark.real_dialogs
def test_a_dialog_that_fits_keeps_its_plain_message(root: tk.Tk) -> None:
    seen: dict[str, object] = {}
    _inspect_then_click(root, "OK", seen)

    dialogs.show_info(root, "Export Finished", "Your playlist was copied to the USB flash drive.")

    assert not seen["scrolls"]
    assert seen["text"] == ""


# --- 4. A song with a very long name keeps its extension on the USB drive ---------------------------


def test_usb_export_keeps_the_extension_of_a_song_with_a_very_long_name(tmp_path: Path) -> None:
    name = "Beethoven - Symphony No 9 in D minor Op 125 Choral - IV Presto Allegro assai - " + "x" * 50 + ".mp3"
    song = tmp_path / "lib" / name
    song.parent.mkdir()
    song.write_bytes(b"ID3" + bytes(64))
    drive_folder = tmp_path / "usb" / "Car"
    reports: list[ExportReport] = []

    exporter.usb_export_worker(str(drive_folder), "Car", [str(song)], normalize=False, on_success=reports.append)

    tracks = [p.name for p in drive_folder.iterdir() if not p.name.startswith("00_")]
    assert reports[0].exported == 1
    assert len(tracks) == 1 and tracks[0].startswith("01 - Beethoven") and tracks[0].endswith(".mp3")
    # The next export of this playlist finds it, so "Replace the old songs" can remove it.
    assert str(drive_folder / tracks[0]) in exporter.find_previous_export(str(drive_folder))
    assert tracks[0] in (drive_folder / "00_Car.m3u8").read_text(encoding="utf-8")


# --- 5. A word in a song's name never turns a file error into a message about YouTube --------------


@pytest.mark.parametrize("context", ["export", "save_clip", "import", "playback", "generic"])
@pytest.mark.parametrize(
    "song", ["Private Video Blues", "Timeout", "Forbidden Love", "This Live Stream", "Video Unavailable"]
)
def test_file_errors_are_not_explained_as_youtube_problems(context: ErrorContext, song: str) -> None:
    raw = f"[WinError 3] The system cannot find the path specified: 'E:\\\\Car\\\\03 - {song}.mp3'"

    assert friendly_error(raw, context).title == friendly_error("something odd", context).title


def test_file_errors_keep_their_own_explanations() -> None:
    raw = "[WinError 32] The process cannot access the file: 'C:\\\\Music\\\\Private Video Blues.mp3'"

    assert friendly_error(raw, "export").title == "File Is In Use"
    assert friendly_error("[Errno 28] No space left on device: 'Timeout.mp3'", "save_clip").title == "Disk Is Full"


def test_downloads_and_searches_are_still_explained() -> None:
    assert friendly_error("ERROR: Private video", "download").title == "Private Video"
    assert friendly_error("<urlopen error [Errno 11001] getaddrinfo failed>", "search").title == "No Internet Connection"
    assert friendly_error("HTTP Error 403: Forbidden", "download").suggests_update


# --- 6. +10s and -10s work on a song whose length is not known yet ---------------------------------


def test_skipping_in_a_song_of_unknown_length_does_not_restart_it() -> None:
    ctrl = PlaybackController(object(), AudioEngine())
    ctrl.audio_engine.play_start_offset = 50.0  # paused 50 seconds in; the length (0) is not known yet

    assert ctrl.skip_by(10.0, 0.0) == pytest.approx(60.0)
    assert ctrl.skip_by(-10.0, 0.0) == pytest.approx(50.0)
    assert ctrl.skip_by(-80.0, 0.0) == 0.0  # never before the start


def test_skipping_still_stops_at_the_end_of_a_song_of_known_length() -> None:
    ctrl = PlaybackController(object(), AudioEngine())
    ctrl.audio_engine.play_start_offset = 95.0

    assert ctrl.skip_by(10.0, 100.0) == pytest.approx(100.0)


# --- 7. The GitHub token is only ever sent to github.com ------------------------------------------


def _redirected(url: str) -> urllib.request.Request | None:
    request = urllib.request.Request("https://api.github.com/asset", headers={"Authorization": "Bearer secret"})
    return _GitHubAssetRedirectHandler().redirect_request(request, io.BytesIO(), 302, "Found", HTTPMessage(), url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.example.net/file.exe",  # "github.com" is in the name, but it is not GitHub
        "https://notgithub.com/file.exe",
        "https://example.net/github.com/file.exe",
        "https://objects.githubusercontent.com/file.exe",
        "https://release-assets.s3.amazonaws.com/file.exe",
    ],
)
def test_token_is_dropped_on_a_redirect_to_any_other_host(url: str) -> None:
    redirected = _redirected(url)

    assert redirected is not None
    sent = {**redirected.headers, **redirected.unredirected_hdrs}
    assert "authorization" not in {name.lower() for name in sent}


@pytest.mark.parametrize("url", ["https://github.com/file.exe", "https://API.GitHub.com/file.exe"])
def test_token_is_kept_on_a_redirect_within_github(url: str) -> None:
    redirected = _redirected(url)

    assert redirected is not None
    assert redirected.headers.get("Authorization") == "Bearer secret"

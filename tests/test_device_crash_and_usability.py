"""Regression tests: the window hook that crashed the app, drag and drop, the crash log, PLAY during a
playlist, trim work lost to a download, leftover trim backups, a full disk during export, file work on
the window's thread, the search preview's clock, duplicate downloads, empty lists and the Help window."""

from __future__ import annotations

import ctypes
import errno
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable, Iterator
from ctypes import wintypes
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.controllers import library_controller
from app.controllers.export_controller import ExportController
from app.controllers.library_controller import LibraryController
from app.core.audio_engine import AudioEngine
from app.core.cache_manager import cache_mgr
from app.core.process_utils import ProcessResult
from app.main import UltimateAudioStudio
from app.models import SearchResult, TrackMetadata
from app.platform_utils import Win32DragDropHandler, _orig_popen
from app.services import clipper, downloader, exporter
from app.services.clipper import has_original_backup, original_backup_path
from app.ui.search_dialog import SearchChoiceDialog
from app.ui.theme import init_fonts

ON_WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not ON_WINDOWS, reason="uses the Windows message loop")

WM_DEVICECHANGE = 0x0219
WM_DROPFILES = 0x0233
DBT_DEVNODES_CHANGED = 7


def _pump(root: tk.Misc, until: Callable[[], bool], timeout: float = 5.0) -> None:
    """Run the event loop until ``until()`` is true, so timers and worker results arrive."""
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)


def _library(window: UltimateAudioStudio, folder: Path, *names: str) -> list[str]:
    """Give the window a Library of stand-in songs (details cached, no waveform or cover reader)."""
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


@pytest.fixture
def root() -> Iterator[tk.Tk]:
    window = tk.Tk()
    window.withdraw()
    init_fonts(window)
    try:
        yield window
    finally:
        window.destroy()


# --- 1 and 2. The window hook: device changes and dropped files ------------------------------------


def _send(hwnd: int, message: int, wparam: int) -> None:
    """Deliver a window message the way Windows does (the window procedure runs before this returns)."""
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW(hwnd, message, wparam, 0)


def _drop_handle(*paths: str) -> int:
    """A WM_DROPFILES handle like the one File Explorer makes: a 64-bit pointer on 64-bit Windows."""
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    names = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    header = (20).to_bytes(4, "little") + bytes(12) + (1).to_bytes(4, "little")  # DROPFILES, wide names
    handle = int(kernel32.GlobalAlloc(0x42, len(header) + len(names)))
    memory = kernel32.GlobalLock(handle)
    ctypes.memmove(memory, header + names, len(header) + len(names))
    kernel32.GlobalUnlock(handle)
    return handle


@pytest.fixture
def hooked(root: tk.Tk) -> Iterator[tuple[Win32DragDropHandler, list[list[str]], list[int], MagicMock]]:
    """A window with the app's hook installed: (handler, dropped files, device changes, root.after)."""
    dropped: list[list[str]] = []
    changes: list[int] = []
    handler = Win32DragDropHandler(root, dropped.append, device_change_callback=lambda: changes.append(1))
    handler.setup()
    with patch.object(root, "after") as after:
        try:
            yield handler, dropped, changes, after
        finally:
            handler.teardown()


@windows_only
def test_device_change_never_calls_tkinter_inside_the_window_procedure(
    hooked: tuple[Win32DragDropHandler, list[list[str]], list[int], MagicMock],
) -> None:
    handler, _dropped, changes, after = hooked
    assert handler._drop_target_hwnd

    _send(handler._drop_target_hwnd, WM_DEVICECHANGE, DBT_DEVNODES_CHANGED)

    assert changes == [1]
    # A tkinter call made there ends the process at the next timer ("PyEval_RestoreThread").
    after.assert_not_called()


@windows_only
def test_dropped_files_reach_the_callback(
    hooked: tuple[Win32DragDropHandler, list[list[str]], list[int], MagicMock],
) -> None:
    handler, dropped, _changes, after = hooked
    assert handler._drop_target_hwnd
    names = [r"C:\Music\One Song.mp3", "C:\\Music\\" + "long name " * 60 + ".mp3"]

    _send(handler._drop_target_hwnd, WM_DROPFILES, _drop_handle(*names))

    assert dropped == [names]
    after.assert_not_called()


@windows_only
def test_closing_window_ignores_drops_and_device_changes(root: tk.Tk) -> None:
    dropped: list[list[str]] = []
    changes: list[int] = []
    handler = Win32DragDropHandler(
        root, dropped.append, is_shutting_down_fn=lambda: True, device_change_callback=lambda: changes.append(1)
    )
    handler.setup()
    try:
        assert handler._drop_target_hwnd
        _send(handler._drop_target_hwnd, WM_DROPFILES, _drop_handle(r"C:\Music\Song.mp3"))
        _send(handler._drop_target_hwnd, WM_DEVICECHANGE, DBT_DEVNODES_CHANGED)
    finally:
        handler.teardown()

    assert (dropped, changes) == ([], [])


@windows_only
def test_main_window_imports_dropped_files_and_rereads_drives(studio: UltimateAudioStudio) -> None:
    handler = studio._dnd_handler
    assert handler is not None and handler._drop_target_hwnd
    with (
        patch.object(studio, "_import_paths") as imported,
        patch.object(studio, "_on_usb_hotplug") as hotplug,
    ):
        _send(handler._drop_target_hwnd, WM_DROPFILES, _drop_handle(r"C:\Music\Song.mp3"))
        _send(handler._drop_target_hwnd, WM_DEVICECHANGE, DBT_DEVNODES_CHANGED)
        # Nothing happens inside the window procedure: the window's own loop picks both up.
        imported.assert_not_called()
        hotplug.assert_not_called()
        _pump(studio.root, lambda: imported.called and hotplug.called)

    imported.assert_called_once_with([r"C:\Music\Song.mp3"], source="dropped")
    hotplug.assert_called_once_with()


# --- 3. A fatal interpreter error is written to the crash log --------------------------------------


@windows_only
def test_fatal_error_reaches_the_crash_log_of_a_windowed_app(tmp_path: Path) -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pytest.skip("no pythonw.exe next to this interpreter")
    log = tmp_path / "crash.txt"
    script = (
        "import ctypes, sys\n"
        "from app.platform_utils import send_fatal_errors_to\n"
        "send_fatal_errors_to(sys.argv[1])\n"
        "ctypes.pythonapi.Py_FatalError.argtypes = [ctypes.c_char_p]\n"
        "ctypes.pythonapi.Py_FatalError(b'crash for the test')\n"
    )
    repo = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": repo}

    # Started without any standard handles, like the packaged app from a Desktop icon: Python then
    # has no stderr, and without the redirect the error goes nowhere. (The app's own Popen wrapper
    # would hand the child this test run's stderr.)
    child = _orig_popen(
        [str(pythonw), "-c", script, str(log)],
        env=env,
        cwd=repo,
        creationflags=subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    child.wait(timeout=60)

    text = log.read_text(encoding="utf-8", errors="replace")
    assert "Fatal Python error" in text
    assert "crash for the test" in text


@windows_only
def test_a_console_stderr_is_left_alone(tmp_path: Path) -> None:
    from app.platform_utils import send_fatal_errors_to

    log = tmp_path / "crash.txt"
    crt = ctypes.CDLL("ucrtbase")
    crt.__acrt_iob_func.restype = ctypes.c_void_p
    crt._fileno.argtypes = [ctypes.c_void_p]
    if crt._fileno(crt.__acrt_iob_func(2)) < 0:
        pytest.skip("this test run has no stderr")

    assert send_fatal_errors_to(log) is False
    assert not log.exists()


# --- 4. PLAY while a playlist is playing ------------------------------------------------------------


def test_play_during_a_playlist_song_keeps_the_playlist_going(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    assert studio._load_track_ui(song, "song.mp3")
    studio.playback_ctrl.is_playing_playlist = True
    try:
        with (
            patch.object(studio.playback_ctrl, "play_track") as play_track,
            patch.object(studio.audio_engine, "release_audio_file") as release,
        ):
            studio.play_main()

        play_track.assert_not_called()
        release.assert_not_called()
        assert studio.is_playing_playlist
        assert not studio.is_playing_main
    finally:
        studio.playback_ctrl.is_playing_playlist = False


def test_play_during_a_clip_preview_still_plays_the_song(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    assert studio._load_track_ui(song, "song.mp3")
    studio.playback_ctrl.is_playing_main = True
    studio.playback_ctrl.previewing_clip = True

    with patch.object(studio.playback_ctrl, "play_track") as play_track:
        studio.play_main()

    play_track.assert_called_once()


# --- 5. A download that finishes while a song is being trimmed -------------------------------------


def test_finished_download_leaves_a_song_that_is_being_trimmed(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    trimming, _new = _library(studio, tmp_path / "lib", "trimming.mp3", "new.mp3")
    assert studio._load_track_ui(trimming, "trimming.mp3")
    studio.clip_start_sec, studio.clip_end_sec = 12.0, 40.0

    studio._download_success("new.mp3")

    assert studio.selected_file_path == trimming
    assert (studio.clip_start_sec, studio.clip_end_sec) == (12.0, 40.0)
    assert "new.mp3" in studio._fresh_songs  # pointed out in green instead
    assert "green" in studio.status.cget("text")


def test_finished_download_keeps_a_volume_boost_that_was_set(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    boosted, _new = _library(studio, tmp_path / "lib", "boosted.mp3", "new.mp3")
    assert studio._load_track_ui(boosted, "boosted.mp3")
    studio.scale_gain.set(6.0)

    studio._download_success("new.mp3")

    assert studio.selected_file_path == boosted
    assert float(studio.scale_gain.get()) == 6.0


def test_finished_download_is_loaded_when_nothing_would_be_lost(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    untouched, new = _library(studio, tmp_path / "lib", "untouched.mp3", "new.mp3")
    assert studio._load_track_ui(untouched, "untouched.mp3")

    studio._download_success("new.mp3")

    assert studio.selected_file_path == new


# --- 6. A trim backup left behind by a song that is gone -------------------------------------------


def _orphan(folder: Path, name: str = "Song.mp3") -> tuple[Path, Path]:
    """A trim backup whose song was deleted in File Explorer; returns (song path, backup path)."""
    folder.mkdir(parents=True, exist_ok=True)
    song = folder / name
    backup = Path(original_backup_path(str(song)))
    backup.write_bytes(b"the old recording, untrimmed")
    return song, backup


@pytest.fixture
def no_recycle_bin() -> Iterator[None]:
    """Keep the tests out of the real Recycle Bin: leftovers are renamed, as on a USB drive."""
    with patch.object(clipper, "has_recycle_bin", return_value=False):
        yield


def _kept_aside(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.orphaned-*"))


def test_leftover_backup_is_set_aside_not_deleted(tmp_path: Path, no_recycle_bin: None) -> None:
    song, backup = _orphan(tmp_path)

    assert clipper.discard_orphan_backup(song) is True

    assert not backup.exists()
    (kept,) = _kept_aside(tmp_path)
    assert kept.read_bytes() == b"the old recording, untrimmed"


def test_leftover_backup_goes_to_the_recycle_bin_where_there_is_one(tmp_path: Path) -> None:
    song, backup = _orphan(tmp_path)
    recycled: list[str] = []

    def _recycle(path: str) -> None:
        recycled.append(path)
        Path(path).unlink()

    with patch.object(clipper, "has_recycle_bin", return_value=True), patch.object(clipper, "_send2trash", _recycle):
        assert clipper.discard_orphan_backup(song) is True

    assert recycled == [str(backup)]
    assert _kept_aside(tmp_path) == []


def test_backup_of_a_song_that_is_there_is_kept(tmp_path: Path, no_recycle_bin: None) -> None:
    song, backup = _orphan(tmp_path)
    song.write_bytes(b"trimmed")

    assert clipper.discard_orphan_backup(song) is False

    assert backup.read_bytes() == b"the old recording, untrimmed"


def test_leftover_backup_that_cannot_be_moved_is_reported(tmp_path: Path, no_recycle_bin: None) -> None:
    song, _backup = _orphan(tmp_path)

    with patch.object(clipper, "_replace_with_retry", side_effect=PermissionError("in use")), pytest.raises(OSError):
        clipper.discard_orphan_backup(song)


def test_start_up_tidy_sets_leftover_backups_aside(tmp_path: Path, no_recycle_bin: None) -> None:
    _song, orphan = _orphan(tmp_path, "Gone.mp3")
    kept_song, kept_backup = _orphan(tmp_path, "Here.mp3")
    kept_song.write_bytes(b"trimmed")

    LibraryController.recover_stranded_deletes(tmp_path)

    assert not orphan.exists()
    assert kept_backup.exists()


def _fake_download(library: Path, on_duplicate: Callable[[str], None] | None = None) -> tuple[list[str], list[str]]:
    """Run the download worker with a stand-in for yt-dlp that "downloads" Song.mp3."""

    class _FakeYoutubeDL:
        def __init__(self, opts: dict[str, object]) -> None:
            self.folder = Path(str(opts["outtmpl"])).parent

        def __enter__(self) -> _FakeYoutubeDL:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def add_post_processor(self, *_args: object, **_kwargs: object) -> None:
            return None

        def extract_info(self, _url: str, download: bool = True) -> dict[str, object]:
            (self.folder / "Song.mp3").write_bytes(b"downloaded")
            return {"title": "Song"}

    succeeded: list[str] = []
    errors: list[str] = []
    with patch.object(downloader.yt_dlp, "YoutubeDL", _FakeYoutubeDL):
        downloader.download_audio_worker(
            "https://youtu.be/x",
            str(library),
            threading.Event(),
            lambda *_a: None,
            succeeded.append,
            lambda: None,
            errors.append,
            on_duplicate=on_duplicate,
        )
    return succeeded, errors


def test_downloaded_song_does_not_inherit_a_leftover_backup(tmp_path: Path, no_recycle_bin: None) -> None:
    song, _backup = _orphan(tmp_path / "library")

    succeeded, errors = _fake_download(tmp_path / "library")

    assert (succeeded, errors) == (["Song.mp3"], [])
    assert song.read_bytes() == b"downloaded"
    assert not has_original_backup(str(song))  # 'Restore Original Song' would put the old recording back


def test_renamed_song_does_not_inherit_a_leftover_backup(tmp_path: Path, no_recycle_bin: None) -> None:
    taken, _backup = _orphan(tmp_path)
    other = tmp_path / "Other.mp3"
    other.write_bytes(b"another song")

    new_path = LibraryController(None).rename_file(str(other), "Song.mp3", {"Mix": [str(other)]})

    assert Path(new_path) == taken
    assert taken.read_bytes() == b"another song"
    assert not has_original_backup(new_path)


def test_rename_still_takes_the_songs_own_backup_along(tmp_path: Path, no_recycle_bin: None) -> None:
    song = tmp_path / "Old.mp3"
    song.write_bytes(b"trimmed")
    Path(original_backup_path(str(song))).write_bytes(b"original")
    playlists = {"Mix": [str(song).upper()]}

    new_path = LibraryController(None).rename_file(str(song), "New.mp3", playlists)

    assert Path(original_backup_path(new_path)).read_bytes() == b"original"
    assert playlists == {"Mix": [new_path]}


def test_rename_onto_another_song_is_refused(tmp_path: Path) -> None:
    song, other = tmp_path / "A.mp3", tmp_path / "B.mp3"
    song.write_bytes(b"a")
    other.write_bytes(b"b")

    with pytest.raises(FileExistsError):
        LibraryController(None).rename_file(str(song), "B.mp3", {})

    assert (song.read_bytes(), other.read_bytes()) == (b"a", b"b")


# --- 7. A disk that fills up during an export -------------------------------------------------------

_NO_SPACE = ProcessResult(1, "", "av_interleaved_write_frame(): No space left on device")


def test_ffmpeg_out_of_space_is_reported_as_a_full_disk(tmp_path: Path) -> None:
    dest = tmp_path / "01 - Song.mp3"

    def _fills_the_disk(args: list[str], **_kwargs: object) -> ProcessResult:
        Path(args[-1]).write_bytes(b"half a song")
        return _NO_SPACE

    with patch.object(exporter, "run_ffmpeg", _fills_the_disk), pytest.raises(OSError) as raised:
        exporter._ffmpeg_to(["-i", "x"], dest, "mp3", None)

    assert raised.value.errno == errno.ENOSPC
    assert list(tmp_path.iterdir()) == []  # the half-written track is not left behind


def test_cd_export_stops_at_a_full_disk_instead_of_blaming_every_song(tmp_path: Path) -> None:
    songs = []
    for index in range(3):
        song = tmp_path / f"song{index}.mp3"
        song.write_bytes(b"ID3" + bytes(64))
        songs.append(str(song))
    cd_folder = tmp_path / "cd"
    cd_folder.mkdir()
    reports: list[exporter.ExportReport] = []

    with patch.object(exporter, "run_ffmpeg", return_value=_NO_SPACE) as ffmpeg:
        exporter.cd_export_worker(str(cd_folder), songs, on_success=reports.append)

    (report,) = reports
    assert report.stopped == "full"
    assert report.skipped == ()  # no song is called damaged
    assert ffmpeg.call_count == 1  # the other songs are not tried on a disk that is full


def test_cd_export_is_not_started_without_room_for_the_tracks(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    with (
        patch.object(studio.export_ctrl, "get_cd_burn_folder", return_value=str(tmp_path)),
        patch.object(studio.export_ctrl, "check_usb_space", return_value=(False, 5 * 1024**2)) as space,
        patch("app.ui.dialogs.show_warning") as warning,
        patch.object(studio.export_ctrl, "start_cd_export") as start,
    ):
        studio._export_to_cd([song], False)

    start.assert_not_called()
    assert warning.call_args[0][1] == "Not Enough Space on This Computer"
    # 100 seconds of CD audio (176 400 bytes a second) and 15 MB to spare.
    assert space.call_args[0][1] == 100 * 176_400 + 15 * 1024**2
    assert not studio._exporting


def test_cd_folder_that_cannot_be_made_is_explained(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    with (
        patch.object(studio.export_ctrl, "get_cd_burn_folder", side_effect=PermissionError(13, "Access is denied")),
        patch("app.ui.features.export.show_friendly_error") as explained,
        patch.object(studio.export_ctrl, "start_cd_export") as start,
    ):
        studio._export_to_cd([song], False)

    start.assert_not_called()
    assert explained.call_args[0][2] == "export"


# --- 8. Drive and file work stays off the window's thread ------------------------------------------


def test_usb_drive_is_inspected_for_an_earlier_export_and_its_free_space(tmp_path: Path) -> None:
    controller = ExportController(None)
    folder = Path(controller.usb_playlist_folder(str(tmp_path), "Trip"))
    folder.mkdir()
    (folder / "01 - Old.mp3").write_bytes(bytes(300))
    (folder / "holiday.jpg").write_bytes(bytes(50))  # the user's own file: not part of an export

    target = controller.inspect_usb_target(str(tmp_path), "Trip")

    assert target.connected
    assert [Path(name).name for name in target.previous] == ["01 - Old.mp3"]
    assert target.previous_bytes == 300
    assert target.free_bytes is not None and target.free_bytes > 0


def test_unplugged_usb_drive_is_reported_as_gone(tmp_path: Path) -> None:
    target = ExportController(None).inspect_usb_target(str(tmp_path / "unplugged"), "Trip")

    assert not target.connected


def _choose_drive(studio: UltimateAudioStudio, drive: Path) -> None:
    label = "USB Drive: TEST (X:\\) [FAT32]"
    studio._usb_map, studio._usb_fs_map = {label: str(drive)}, {label: "FAT32"}
    studio.usb_choice.set(label)


def test_usb_drive_is_read_on_a_worker_before_the_export_questions(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    drive = tmp_path / "usb"
    drive.mkdir()
    _choose_drive(studio, drive)
    threads: list[threading.Thread] = []
    inspect = studio.export_ctrl.inspect_usb_target

    def _inspect(drive_root: str, playlist: str) -> object:
        threads.append(threading.current_thread())
        return inspect(drive_root, playlist)

    with (
        patch.object(studio.export_ctrl, "inspect_usb_target", _inspect),
        patch.object(studio.export_ctrl, "start_usb_export") as start,
    ):
        studio._export_to_usb([song], False)
        assert str(studio.btn_export.cget("state")) == tk.DISABLED  # no second export meanwhile
        _pump(studio.root, lambda: start.called)
        studio._end_export("done")

    assert threads and threads[0] is not threading.main_thread()
    assert start.call_args.kwargs["clear_existing"] is False


def test_usb_drive_unplugged_before_the_export_is_explained(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    _choose_drive(studio, tmp_path / "unplugged")

    with (
        patch("app.ui.dialogs.show_warning") as warning,
        patch.object(studio.export_ctrl, "start_usb_export") as start,
    ):
        studio._export_to_usb([song], False)
        _pump(studio.root, lambda: warning.called)

    start.assert_not_called()
    assert warning.call_args[0][1] == "Drive Missing"
    assert str(studio.btn_export.cget("state")) == tk.NORMAL
    assert studio.busy_reason() is None


def test_usb_export_without_room_counts_the_old_songs_it_replaces(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    from app.controllers.export_controller import UsbTarget

    (song,) = _library(studio, tmp_path / "lib", "song.mp3")
    needed = studio.export_ctrl.estimate_playlist_bytes([song], studio._cached_duration_only, False)
    old = (str(tmp_path / "01 - Old.mp3"),)
    target = UsbTarget(True, old, previous_bytes=needed, free_bytes=0)

    with (
        patch("app.ui.dialogs.ask_choice", return_value="keep"),
        patch("app.ui.dialogs.show_warning") as warning,
        patch.object(studio.export_ctrl, "start_usb_export") as start,
    ):
        studio._usb_target_checked([song], False, "USB", str(tmp_path), "Trip", target)
    start.assert_not_called()
    assert warning.call_args[0][1] == "Not Enough Space on the USB Drive"

    with (
        patch("app.ui.dialogs.ask_choice", return_value="replace"),
        patch.object(studio.export_ctrl, "start_usb_export") as start,
    ):
        studio._usb_target_checked([song], False, "USB", str(tmp_path), "Trip", target)
        studio._end_export("done")
    assert start.call_args.kwargs["clear_existing"] is True


def test_recycle_bin_work_for_deleted_songs_runs_on_a_worker(tmp_path: Path) -> None:
    controller = LibraryController(None)
    controller._pending_deletes.append((str(tmp_path / "a.mp3"), str(tmp_path / "staged" / "a.mp3"), "a.mp3", []))
    done = threading.Event()
    threads: list[threading.Thread] = []

    def _trash(_staged: str, _original: str | None) -> None:
        threads.append(threading.current_thread())
        done.set()

    with patch.object(library_controller, "_trash_staged_file", _trash):
        controller.flush_pending_trash(background=True)
        assert done.wait(5.0)

    assert controller._pending_deletes == []
    assert all(thread is not threading.main_thread() for thread in threads)


def test_recycle_bin_work_is_finished_before_the_app_closes(tmp_path: Path) -> None:
    controller = LibraryController(None)
    controller._pending_deletes.append((str(tmp_path / "a.mp3"), str(tmp_path / "staged" / "a.mp3"), "a.mp3", []))
    threads: list[threading.Thread] = []

    with patch.object(library_controller, "_trash_staged_file", lambda *_a: threads.append(threading.current_thread())):
        controller.flush_pending_trash()

    assert threads and all(thread is threading.main_thread() for thread in threads)


def _trimmed_song(studio: UltimateAudioStudio, folder: Path) -> str:
    (song,) = _library(studio, folder, "song.mp3")
    Path(original_backup_path(song)).write_bytes(b"original")
    assert studio._load_track_ui(song, "song.mp3")
    return song


def test_restore_original_runs_on_a_worker(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    song = _trimmed_song(studio, tmp_path / "lib")
    threads: list[threading.Thread] = []

    with (
        patch("app.ui.dialogs.ask_yes_no", return_value=True),
        patch("app.ui.features.clip_editor.restore_original", lambda _path: threads.append(threading.current_thread())),
    ):
        studio.restore_original_song()
        assert studio.busy_reason() == "restoring the original song"  # closing or updating waits for it
        assert str(studio.btn_restore_original.cget("state")) == tk.DISABLED
        _pump(studio.root, lambda: not studio._restoring_original)

    assert threads and threads[0] is not threading.main_thread()
    assert studio.busy_reason() is None
    assert studio.selected_file_path == song
    assert "is back in your Library" in studio.status.cget("text")


def test_restore_original_that_fails_is_explained(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    _trimmed_song(studio, tmp_path / "lib")

    with (
        patch("app.ui.dialogs.ask_yes_no", return_value=True),
        patch("app.ui.features.clip_editor.restore_original", side_effect=PermissionError(13, "Access is denied")),
        patch("app.ui.features.clip_editor.show_friendly_error") as explained,
    ):
        studio.restore_original_song()
        _pump(studio.root, lambda: explained.called)

    assert not studio._restoring_original
    assert studio.busy_reason() is None
    assert str(studio.btn_restore_original.cget("state")) == tk.NORMAL
    assert "could not be restored" in studio.status.cget("text")


def test_restore_original_cancelled_changes_nothing(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    _trimmed_song(studio, tmp_path / "lib")

    with (
        patch("app.ui.dialogs.ask_yes_no", return_value=False),
        patch("app.ui.features.clip_editor.restore_original") as restore,
    ):
        studio.restore_original_song()

    restore.assert_not_called()
    assert not studio._restoring_original


# --- 9. The search preview and the paused song's position ------------------------------------------


def test_search_preview_leaves_the_paused_songs_position_alone() -> None:
    with patch("app.core.audio_engine.pygame") as fake_pygame:
        fake_pygame.mixer.get_init.return_value = (44100, -16, 2)
        engine = AudioEngine()
        engine.seek_clock(90.0, is_playing=False)  # a song paused a minute and a half in

        engine.play_preview("preview.mp3")

        fake_pygame.mixer.music.load.assert_called_once_with("preview.mp3")
        fake_pygame.mixer.music.play.assert_called_once_with()
    assert engine.current_play_seconds() == 90.0  # +10s and Previous still start from there


def test_each_preview_gets_its_own_cancel_signal(root: tk.Tk) -> None:
    results = [
        SearchResult("id1", "Song", "Artist", 200.0, "03:20", "https://example.invalid/1"),
        SearchResult("id2", "Song", "Artist", 200.0, "03:20", "https://example.invalid/2"),
    ]
    dialog = SearchChoiceDialog(root, "song", results, on_select=lambda _item: None, audio_engine=MagicMock())
    try:
        with patch("app.ui.search_dialog.task_mgr.submit_task") as submit:
            dialog._start_preview(results[0])
            dialog._start_preview(results[1])
    finally:
        dialog._do_cancel()

    first, second = (call.args[3] for call in submit.call_args_list)
    assert first is not second
    assert first.is_set()  # the preview that was replaced stays stopped
    assert second.is_set()  # closing the dialog stops the last one


# --- 10. A song that is downloaded a second time ----------------------------------------------------


def _lengths(by_name: dict[str, float]) -> Callable[..., TrackMetadata]:
    return lambda path, probe_fallback=True: TrackMetadata(duration=by_name.get(Path(path).name, 0.0))


def test_same_name_and_length_is_the_same_song(tmp_path: Path) -> None:
    library, work = tmp_path / "library", tmp_path / "work"
    library.mkdir()
    work.mkdir()
    for folder in (library, work):
        (folder / "Song.mp3").write_bytes(b"x")
    (library / "Song (2).mp3").write_bytes(b"x")
    new = work / "Song.mp3"

    with patch.object(downloader, "read_track_metadata", _lengths({"Song.mp3": 200.0, "Song (2).mp3": 95.0})):
        assert downloader.find_same_song(library, new) == library / "Song.mp3"
    # Another recording under the same title (a live version) is a new song.
    with patch.object(
        downloader,
        "read_track_metadata",
        lambda path, probe_fallback=True: TrackMetadata(duration=200.0 if Path(path).parent == work else 95.0),
    ):
        assert downloader.find_same_song(library, new) is None
    # A length that cannot be read proves nothing: the song is kept.
    with patch.object(downloader, "read_track_metadata", _lengths({})):
        assert downloader.find_same_song(library, new) is None


def test_song_downloaded_again_is_not_added_a_second_time(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "Song.mp3").write_bytes(b"the copy I have")
    duplicates: list[str] = []

    with patch.object(downloader, "read_track_metadata", _lengths({"Song.mp3": 200.0})):
        succeeded, errors = _fake_download(library, on_duplicate=duplicates.append)

    assert (succeeded, errors, duplicates) == ([], [], ["Song.mp3"])
    assert sorted(p.name for p in library.iterdir()) == ["Song.mp3"]
    assert (library / "Song.mp3").read_bytes() == b"the copy I have"


def test_playlist_download_reports_songs_that_were_already_there(tmp_path: Path) -> None:
    entries = [
        {"id": "a", "title": "Have", "url": "https://youtu.be/a"},
        {"id": "b", "title": "New", "url": "https://youtu.be/b"},
    ]
    finished: list[str] = []
    already: list[str] = []
    failed: list[str] = []

    def _download(
        url: str,
        _lib: str,
        _cancel: object,
        _prog: object,
        on_success: Callable[[str], None],
        *_rest: object,
        on_duplicate: Callable[[str], None],
    ) -> None:
        if url.endswith("/a"):
            on_duplicate("Have.mp3")
        else:
            on_success("New.mp3")

    with patch.object(downloader, "download_audio_worker", _download):
        downloader.download_playlist_worker(
            entries,
            str(tmp_path),
            threading.Event(),
            on_track_finished=lambda _i, _t, name: finished.append(name),
            on_track_failed=lambda _i, _t, title, _err: failed.append(title),
            on_track_duplicate=lambda _i, _t, name: already.append(name),
        )

    assert (finished, already, failed) == (["New.mp3"], ["Have.mp3"], [])


def test_window_points_out_the_song_that_is_already_there(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    _library(studio, tmp_path / "lib", "Other.mp3", "Song.mp3")
    studio.entry_search.insert(0, "other")
    studio.apply_library_filter()
    assert studio.visible_files == ["Other.mp3"]

    studio._download_duplicate("Song.mp3")

    assert "Song.mp3" in studio.visible_files  # the search no longer hides it
    assert "already in your Library" in studio.status.cget("text")
    assert studio.busy_reason() is None
    assert str(studio.btn_download.cget("state")) == tk.NORMAL


def test_playlist_summary_says_what_was_already_there(studio: UltimateAudioStudio) -> None:
    studio._playlist_download_success(["New.mp3"], 3, [], already_there=2)
    assert "2 song(s) were already in your Library" in studio.status.cget("text")

    studio._playlist_download_success([], 2, [], already_there=2)
    assert "nothing was added" in studio.status.cget("text")


# --- 11. Empty lists say what to do -----------------------------------------------------------------


def _hint(studio: UltimateAudioStudio) -> str:
    """The text shown over the Library list ("" when the list has songs)."""
    studio.root.update_idletasks()
    return str(studio.lbl_lib_empty.cget("text")) if studio.lbl_lib_empty.winfo_manager() else ""


def test_empty_library_says_how_to_get_music(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    _library(studio, tmp_path / "lib")

    assert "Your Library is empty" in _hint(studio)
    assert "Download MP3" in _hint(studio)


def test_search_without_matches_says_so(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    _library(studio, tmp_path / "lib", "Song.mp3")
    assert _hint(studio) == ""

    studio.entry_search.insert(0, "zzz")
    studio.apply_library_filter()
    assert "No songs match 'zzz'" in _hint(studio)

    studio.clear_search()
    assert _hint(studio) == ""


# --- 12. The Help Guide opens once ------------------------------------------------------------------


def test_help_button_shows_the_open_guide_instead_of_another(studio: UltimateAudioStudio) -> None:
    def _guides() -> list[tk.Toplevel]:
        return [w for w in studio.root.winfo_children() if isinstance(w, tk.Toplevel) and "Help" in w.title()]

    studio.show_help()
    studio.show_help()
    assert len(_guides()) == 1

    _guides()[0].destroy()  # closed by the user: the button opens it again
    studio.show_help()
    assert len(_guides()) == 1

"""Regression tests: test foundations, safe import, failed conversions, eject binding, truthful export reports."""

from __future__ import annotations

import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import ERROR_LOG_PATH, MUSIC_DIR, PLAYLISTS_PATH, SETTINGS_PATH
from app.controllers.library_controller import ImportResult, LibraryController
from app.core.audio_engine import AudioEngine
from app.core.file_utils import copy_file_atomic
from app.core.task_manager import TaskManager, task_mgr
from app.main import UltimateAudioStudio
from app.services import exporter
from app.services.exporter import ExportReport
from app.ui.features.export import export_problems


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def _pump(window: UltimateAudioStudio, until: Callable[[], bool], timeout: float = 5.0) -> None:
    """Run the window's event loop until ``until()`` is true, so worker results reach the UI thread."""
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)


def _make_song(folder: Path, name: str = "song0.mp3", content: bytes = b"ID3" + bytes(64)) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    song = folder / name
    song.write_bytes(content)
    return song


# --- Test foundations ---------------------------------------------------------------------------------


def test_worker_pool_can_be_restarted_after_shutdown() -> None:
    pool = TaskManager(max_workers=2)
    pool.shutdown()
    assert pool.submit_task(lambda: 1) is None  # dropped without an error: the trap the fixture closes

    pool.restart()
    future = pool.submit_task(lambda: 41 + 1)

    assert future is not None
    assert future.result(timeout=5) == 42
    pool.shutdown(wait=True)


def test_closing_a_window_shuts_the_shared_worker_pool_down() -> None:
    root = tk.Tk()
    root.withdraw()
    UltimateAudioStudio(root).on_close()

    assert task_mgr.submit_task(lambda: "ran") is None  # left shut down on purpose for the next test


def test_the_next_test_still_gets_a_running_worker_pool() -> None:
    # Runs straight after the test above, which closed a window: only the autouse fixture in
    # conftest.py can have brought the pool back.
    future = task_mgr.submit_task(lambda: "ran")

    assert future is not None
    assert future.result(timeout=5) == "ran"


def test_error_dialogs_do_not_block_a_test() -> None:
    import app.main as main_module
    from app.ui.features import export as export_feature

    root = tk.Tk()
    root.withdraw()
    # If the fixture ever stops covering these, close the dialog instead of hanging the whole run.
    root.after(3000, lambda: [w.destroy() for w in root.winfo_children() if isinstance(w, tk.Toplevel)])
    try:
        export_feature.show_friendly_error(root, "disk full", "export")
        main_module.show_error(root, "Title", "Message")
        assert not [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    finally:
        root.destroy()


def test_tests_never_use_the_real_music_folder() -> None:
    real_music = Path.home() / "Music"
    for path in (MUSIC_DIR, PLAYLISTS_PATH, SETTINGS_PATH, ERROR_LOG_PATH):
        assert not Path(path).is_relative_to(real_music), path


# --- Import -------------------------------------------------------------------------------------------


def _run_import(planned: list[tuple[str, str]]) -> ImportResult:
    """Run the import on its worker and wait for the result."""
    results: list[ImportResult] = []
    done = threading.Event()

    def _on_done(result: ImportResult) -> None:
        results.append(result)
        done.set()

    LibraryController(None).import_external_files(planned, is_shutting_down_fn=None, on_done=_on_done)
    assert done.wait(timeout=10), "the import never reported back"
    return results[0]


def test_two_songs_with_the_same_name_are_both_imported(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    first = _make_song(tmp_path / "album a", "Song.mp3", b"first")
    second = _make_song(tmp_path / "album b", "Song.mp3", b"second")

    planned, already_there = LibraryController.build_import_plan([str(first), str(second)], str(library))
    result = _run_import(planned)

    assert already_there == []
    assert result == ImportResult(copied=("Song.mp3", "Song (2).mp3"), renamed=("Song (2).mp3",))
    assert (library / "Song.mp3").read_bytes() == b"first"
    assert (library / "Song (2).mp3").read_bytes() == b"second"


def test_new_name_skips_names_the_library_already_uses(tmp_path: Path) -> None:
    library = tmp_path / "library"
    _make_song(library, "Song (2).mp3", b"mine")
    first = _make_song(tmp_path / "a", "Song.mp3")
    second = _make_song(tmp_path / "b", "song.MP3")  # Windows treats this as the same name

    planned, _ = LibraryController.build_import_plan([str(first), str(second)], str(library))

    assert [Path(dest).name for _, dest in planned] == ["Song.mp3", "song (3).MP3"]


def test_a_file_dropped_twice_is_imported_once(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    song = _make_song(tmp_path / "album", "Song.mp3")

    planned, _ = LibraryController.build_import_plan([str(song.parent), str(song)], str(library))

    assert [Path(dest).name for _, dest in planned] == ["Song.mp3"]


def test_failed_copy_is_reported_and_leaves_nothing_behind(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    good = _make_song(tmp_path / "usb", "Good.mp3", b"good")
    bad = _make_song(tmp_path / "usb", "Bad.mp3", b"bad song")
    real_copy = __import__("shutil").copy2

    def _drive_removed_mid_copy(src: str | Path, dst: str | Path, **kwargs: object) -> object:
        if Path(src).name == "Bad.mp3":
            Path(dst).write_bytes(b"ba")
            raise OSError("The device is not ready")
        return real_copy(src, dst, **kwargs)

    planned, _ = LibraryController.build_import_plan([str(bad), str(good)], str(library))
    with patch("shutil.copy2", side_effect=_drive_removed_mid_copy):
        result = _run_import(planned)

    assert result == ImportResult(copied=("Good.mp3",), failed=("Bad.mp3",))
    assert _names(library) == ["Good.mp3"]


def test_replacing_a_library_song_keeps_it_when_the_copy_fails(tmp_path: Path) -> None:
    library = tmp_path / "library"
    mine = _make_song(library, "Song.mp3", b"my copy")
    incoming = _make_song(tmp_path / "usb", "Song.mp3", b"the new copy")

    def _cut_off(_src: str | Path, dst: str | Path, **_kwargs: object) -> None:
        Path(dst).write_bytes(b"the n")
        raise OSError("The device is not ready")

    with patch("shutil.copy2", side_effect=_cut_off):
        result = _run_import([(str(incoming), str(mine))])

    assert result == ImportResult(failed=("Song.mp3",))
    assert mine.read_bytes() == b"my copy"
    assert _names(library) == ["Song.mp3"]


def test_short_copy_is_rejected(tmp_path: Path) -> None:
    src = _make_song(tmp_path, "Song.mp3", b"complete")
    dest = tmp_path / "copy.mp3"

    def _short(_src: str | Path, dst: str | Path, **_kwargs: object) -> None:
        Path(dst).write_bytes(b"comp")

    with patch("shutil.copy2", side_effect=_short):
        try:
            copy_file_atomic(src, dest)
        except OSError as err:
            assert "incomplete" in str(err)
        else:
            raise AssertionError("a short copy was accepted")
    assert _names(tmp_path) == ["Song.mp3"]


def test_import_tells_the_user_what_was_renamed_and_what_failed(studio: UltimateAudioStudio) -> None:
    result = ImportResult(copied=("A.mp3", "Song (2).mp3"), renamed=("Song (2).mp3",), failed=("Bad.mp3",))
    with patch("app.ui.dialogs.show_warning") as warn:
        studio._on_copy_external_done(result)

    assert "Added 2 song(s)" in studio.status.cget("text")
    assert "Song (2).mp3" in studio.status.cget("text")
    assert warn.call_args[0][1] == "Some Songs Could Not Be Added"
    assert "Bad.mp3" in warn.call_args[0][2]
    assert not studio._importing


# --- Songs that must be converted before they can be played ------------------------------------------


def test_prepare_for_playback_reports_whether_the_conversion_worked(tmp_path: Path) -> None:
    song = _make_song(tmp_path, "song.m4a", b"not really audio")
    engine = AudioEngine()

    with (
        patch("app.core.audio_engine.PREVIEW_CACHE_DIR", str(tmp_path / "cache")),
        patch("app.core.audio_engine.subprocess.run", return_value=SimpleNamespace(returncode=1)),
    ):
        assert engine.prepare_for_playback(str(song)) is False

    def _converts(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        Path(cmd[-1]).write_bytes(b"RIFF")
        return SimpleNamespace(returncode=0)

    with (
        patch("app.core.audio_engine.PREVIEW_CACHE_DIR", str(tmp_path / "cache")),
        patch("app.core.audio_engine.subprocess.run", side_effect=_converts),
    ):
        assert engine.prepare_for_playback(str(song)) is True


def test_failed_conversion_is_reported_and_playback_is_not_started(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    song = _make_song(tmp_path, "song.m4a", b"not really audio")
    start = MagicMock()

    with (
        patch.object(studio.audio_engine, "prepare_for_playback", return_value=False),
        patch("app.ui.features.player.show_friendly_error") as shown,
    ):
        studio._when_playable(str(song), start)
        _pump(studio, lambda: shown.called or start.called)

    # Starting playback would run the failed conversion again, on the window's own thread.
    start.assert_not_called()
    shown.assert_called_once()
    assert "song.m4a" in shown.call_args[0][1]
    assert shown.call_args[0][2] == "playback"
    assert not studio._busy


def test_converted_song_starts_playing(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    song = _make_song(tmp_path, "song.m4a", b"not really audio")
    start = MagicMock()

    with (
        patch.object(studio.audio_engine, "prepare_for_playback", return_value=True),
        patch("app.ui.features.player.show_friendly_error") as shown,
    ):
        studio._when_playable(str(song), start)
        _pump(studio, lambda: start.called)

    start.assert_called_once()
    shown.assert_not_called()


def test_clip_preview_reports_a_failed_conversion(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    song = _make_song(tmp_path, "song.m4a", b"not really audio")
    studio.selected_file_path = str(song)
    studio.clip_start_sec = 0.0
    studio.clip_end_sec = 5.0

    with (
        patch.object(studio.audio_engine, "prepare_for_playback", return_value=False),
        patch.object(studio.playback_ctrl, "prepare_audition", return_value=None),
        patch.object(studio, "_start_test_clip") as start,
        patch("app.ui.features.clip_editor.show_friendly_error") as shown,
    ):
        studio.test_clip()
        _pump(studio, lambda: shown.called or start.called)

    start.assert_not_called()
    shown.assert_called_once()
    assert not studio._busy


# --- USB drive selection and eject --------------------------------------------------------------------


def test_refreshing_the_drive_list_keeps_the_chosen_drive(studio: UltimateAudioStudio) -> None:
    two = [("E:\\", "USB E", "FAT32"), ("F:\\", "USB F", "FAT32")]
    three = [("D:\\", "USB D", "FAT32"), *two]

    with patch("app.ui.features.export.list_removable_drives", return_value=two):
        studio.refresh_usb_drives()
    studio.usb_choice.set("USB F")
    with patch("app.ui.features.export.list_removable_drives", return_value=three):
        studio.refresh_usb_drives()  # a third drive is plugged in
    assert studio.usb_choice.get() == "USB F"

    with patch("app.ui.features.export.list_removable_drives", return_value=three[:2]):
        studio.refresh_usb_drives()  # the chosen drive is unplugged
    assert studio.usb_choice.get() == "USB D"


def test_finished_export_ejects_the_drive_it_was_written_to(studio: UltimateAudioStudio) -> None:
    studio._usb_map = {"USB E": "E:\\", "USB F": "F:\\"}
    studio.usb_choice.set("USB F")  # another drive was selected while the export ran

    with (
        patch("app.ui.dialogs.ask_choice", return_value="yes"),
        patch("app.ui.features.export.list_removable_drives", return_value=[]),
        patch.object(studio.export_ctrl, "eject_usb_drive", return_value=(True, "")) as eject,
    ):
        studio._usb_export_success(ExportReport(total=1, exported=1), "USB E", "E:\\")

    eject.assert_called_once_with("E:\\")


def test_drive_cannot_be_ejected_while_songs_are_copied_to_it(studio: UltimateAudioStudio) -> None:
    drives = [("E:\\", "USB E", "FAT32")]
    studio._usb_map = {"USB E": "E:\\"}
    studio.usb_choice.set("USB E")
    studio._export_drive_root = "E:\\"

    with (
        patch("app.ui.dialogs.show_info") as info,
        patch("app.ui.features.export.list_removable_drives", return_value=drives),
        patch.object(studio.export_ctrl, "eject_usb_drive", return_value=(True, "")) as eject,
    ):
        studio.eject_selected_usb()
        eject.assert_not_called()
        assert info.call_args[0][1] == "Export Still Running"

        studio._end_export("done")
        studio.eject_selected_usb()
        eject.assert_called_once_with("E:\\")


# --- Export reports -----------------------------------------------------------------------------------


def test_usb_export_says_when_a_song_could_not_be_levelled(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    song = _make_song(tmp_path / "library")
    done: list[ExportReport] = []

    with (
        patch("app.services.exporter.measure_loudnorm", return_value=None),
        patch("app.services.exporter._encode_mp3", return_value=False),  # FFmpeg could not level it
    ):
        exporter.usb_export_worker(str(drive), "Trip", [str(song)], normalize=True, on_success=done.append)

    assert done == [ExportReport(total=1, exported=1, not_leveled=("song0.mp3",))]
    assert (drive / "01 - song0.mp3").read_bytes() == song.read_bytes()  # still delivered, unchanged


def test_usb_export_without_levelling_has_nothing_to_confess(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    done: list[ExportReport] = []

    exporter.usb_export_worker(str(drive), "Trip", [str(_make_song(tmp_path / "library"))], on_success=done.append)

    assert done == [ExportReport(total=1, exported=1)]


def test_missing_song_is_reported_as_skipped_not_exported(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    done: list[ExportReport] = []

    exporter.usb_export_worker(str(drive), "Trip", [str(tmp_path / "gone.mp3")], on_success=done.append)

    assert done == [ExportReport(total=1, exported=0, skipped=("gone.mp3 (file not found)",))]


def test_cd_export_says_when_a_track_could_not_be_levelled(tmp_path: Path) -> None:
    cd = tmp_path / "cd"
    cd.mkdir()
    song = _make_song(tmp_path / "library")
    done: list[ExportReport] = []

    with (
        patch("app.services.exporter.measure_loudnorm", return_value=None),
        patch("app.services.exporter.run_ffmpeg", return_value=SimpleNamespace(returncode=1, stdout="", stderr="")),
        patch("app.services.exporter.AudioSegment"),  # the fallback conversion
    ):
        exporter.cd_export_worker(str(cd), [str(song)], normalize=True, on_success=done.append)

    assert done == [ExportReport(total=1, exported=1, not_leveled=("song0.mp3",))]


def test_export_summary_separates_skipped_songs_from_unlevelled_ones() -> None:
    assert export_problems(ExportReport(total=3, exported=3)) == ""

    text = export_problems(
        ExportReport(total=3, exported=2, skipped=("gone.mp3 (file not found)",), not_leveled=("quiet.mp3",))
    )

    assert "1 song(s) could not be processed:\n• gone.mp3 (file not found)" in text
    assert "1 song(s) were exported at their original volume" in text
    assert "• quiet.mp3" in text


def test_finished_usb_export_shows_the_songs_left_at_their_original_volume(studio: UltimateAudioStudio) -> None:
    with patch("app.ui.dialogs.show_warning") as warn:  # the eject question is cancelled by the fixture
        studio._usb_export_success(ExportReport(total=2, exported=2, not_leveled=("quiet.mp3",)), "USB E", "E:\\")

    assert warn.call_args[0][1] == "Export Finished with Some Problems"
    assert "Exported 2 of 2 song(s)" in warn.call_args[0][2]
    assert "quiet.mp3" in warn.call_args[0][2]

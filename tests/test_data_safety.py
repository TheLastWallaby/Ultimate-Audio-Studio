"""Regression tests for the rules in AGENTS.md "The user's files": backups, playlist recovery, safe Cancel."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import DEFAULT_PLAYLIST_NAME
from app.controllers.library_controller import ImportResult, LibraryController
from app.controllers.playlist_controller import PlaylistController, PlaylistLoadResult, last_good_path
from app.main import UltimateAudioStudio
from app.services import exporter
from app.services.clipper import clip_audio_worker, has_original_backup, original_backup_path


def _fake_ffmpeg(payload: bytes = b"trimmed") -> Callable[..., SimpleNamespace]:
    """Stand in for FFmpeg: write ``payload`` to the output file (the last argument) and succeed."""

    def _run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        Path(args[-1]).write_bytes(payload)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return _run


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


# --- Saving a clip over the song it was cut from ---------------------------------------------------


def _trim_in_place(song: Path) -> tuple[list[str], list[str]]:
    """Save a clip over ``song``; returns the saved paths and the reported errors."""
    saved: list[str] = []
    errors: list[str] = []
    with patch("app.services.clipper.run_ffmpeg", side_effect=_fake_ffmpeg()):
        clip_audio_worker(
            str(song),
            0.0,
            1.0,
            str(song),
            is_self_overwrite=True,
            on_success=lambda _name, path, _replaced: saved.append(path),
            on_error=errors.append,
        )
    return saved, errors


def test_clip_keeps_a_complete_backup_before_replacing_the_song(tmp_path: Path) -> None:
    song = tmp_path / "song.mp3"
    song.write_bytes(b"original")

    saved, errors = _trim_in_place(song)

    assert (saved, errors) == ([str(song)], [])
    assert song.read_bytes() == b"trimmed"
    assert Path(original_backup_path(str(song))).read_bytes() == b"original"
    assert _names(tmp_path) == ["song.mp3", "song.mp3.original.bak"]


def test_failed_backup_leaves_the_original_song_untouched(tmp_path: Path) -> None:
    song = tmp_path / "song.mp3"
    song.write_bytes(b"original")

    with patch("shutil.copy2", side_effect=OSError(28, "No space left on device")):
        saved, errors = _trim_in_place(song)

    assert saved == []
    assert len(errors) == 1
    assert "could not be backed up" in errors[0]
    assert song.read_bytes() == b"original"
    assert _names(tmp_path) == ["song.mp3"]  # no half-made backup and no leftover clip


def test_interrupted_backup_is_never_mistaken_for_a_backup(tmp_path: Path) -> None:
    song = tmp_path / "song.mp3"
    song.write_bytes(b"original")

    def _drive_removed_mid_copy(src: str | Path, dst: str | Path, **_kwargs: object) -> None:
        Path(dst).write_bytes(Path(src).read_bytes()[:3])
        raise OSError("The device is not ready")

    with patch("shutil.copy2", side_effect=_drive_removed_mid_copy):
        saved, errors = _trim_in_place(song)

    assert saved == []
    assert len(errors) == 1
    assert song.read_bytes() == b"original"
    assert not has_original_backup(str(song))
    assert _names(tmp_path) == ["song.mp3"]


def test_short_backup_copy_stops_the_replace(tmp_path: Path) -> None:
    song = tmp_path / "song.mp3"
    song.write_bytes(b"original")

    def _silently_short_copy(src: str | Path, dst: str | Path, **_kwargs: object) -> None:
        Path(dst).write_bytes(Path(src).read_bytes()[:3])

    with patch("shutil.copy2", side_effect=_silently_short_copy):
        saved, errors = _trim_in_place(song)

    assert saved == []
    assert len(errors) == 1
    assert song.read_bytes() == b"original"
    assert not has_original_backup(str(song))


def test_first_original_is_kept_when_a_song_is_trimmed_again(tmp_path: Path) -> None:
    song = tmp_path / "song.mp3"
    song.write_bytes(b"trimmed once")
    backup = Path(original_backup_path(str(song)))
    backup.write_bytes(b"the real original")

    saved, errors = _trim_in_place(song)

    assert (saved, errors) == ([str(song)], [])
    assert backup.read_bytes() == b"the real original"


def test_empty_backup_left_by_an_older_version_is_replaced(tmp_path: Path) -> None:
    song = tmp_path / "song.mp3"
    song.write_bytes(b"original")
    backup = Path(original_backup_path(str(song)))
    backup.write_bytes(b"")

    saved, errors = _trim_in_place(song)

    assert (saved, errors) == ([str(song)], [])
    assert backup.read_bytes() == b"original"


# --- Playlists file ---------------------------------------------------------------------------------


def test_first_start_has_default_playlist_and_nothing_to_report(tmp_path: Path) -> None:
    path = tmp_path / "playlists.json"
    ctrl = PlaylistController(None)

    assert ctrl.load(path) == PlaylistLoadResult()
    assert ctrl.playlists == {DEFAULT_PLAYLIST_NAME: []}
    assert _names(tmp_path) == []


def test_unreadable_playlists_file_is_set_aside_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "playlists.json"
    cut_off = '{"Road Trip": ["C:/Music/a.mp3"'
    path.write_text(cut_off, encoding="utf-8")
    ctrl = PlaylistController(None)

    result = ctrl.load(path)

    assert result.unreadable
    assert not result.restored_from_backup
    assert result.damaged_copy is not None
    assert result.damaged_copy.parent == tmp_path
    assert result.damaged_copy.read_text(encoding="utf-8") == cut_off
    assert ctrl.playlists == {DEFAULT_PLAYLIST_NAME: []}

    assert ctrl.save(path)
    assert result.damaged_copy.read_text(encoding="utf-8") == cut_off  # the save went to a new file
    assert json.loads(path.read_text(encoding="utf-8")) == {DEFAULT_PLAYLIST_NAME: []}


def test_json_that_is_not_a_playlist_collection_counts_as_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "playlists.json"
    path.write_text("[]", encoding="utf-8")

    result = PlaylistController(None).load(path)

    assert result.unreadable
    assert result.damaged_copy is not None


def test_damaged_playlists_are_restored_from_the_last_good_copy(tmp_path: Path) -> None:
    path = tmp_path / "playlists.json"
    good = {"Road Trip": ["C:/Music/a.mp3", "C:/Music/b.mp3"], DEFAULT_PLAYLIST_NAME: []}
    path.write_text(json.dumps(good), encoding="utf-8")
    assert PlaylistController(None).load(path) == PlaylistLoadResult()  # a good load keeps a spare copy
    assert last_good_path(path).is_file()

    path.write_bytes(b"")  # what a power cut during a save leaves behind
    ctrl = PlaylistController(None)
    result = ctrl.load(path)

    assert result.unreadable
    assert result.restored_from_backup
    assert ctrl.playlists == good
    assert ctrl.active_playlist_name == "Road Trip"


def test_missing_playlists_file_is_restored_from_the_last_good_copy(tmp_path: Path) -> None:
    # The damaged file was set aside and the app closed before saving again.
    path = tmp_path / "playlists.json"
    good = {DEFAULT_PLAYLIST_NAME: ["C:/Music/a.mp3"]}
    last_good_path(path).write_text(json.dumps(good), encoding="utf-8")
    ctrl = PlaylistController(None)

    result = ctrl.load(path)

    assert result == PlaylistLoadResult(restored_from_backup=True)
    assert ctrl.playlists == good


def test_failed_playlist_save_is_reported_to_the_caller(tmp_path: Path) -> None:
    ctrl = PlaylistController(None)
    with patch("app.controllers.playlist_controller.atomic_save_json", side_effect=OSError(28, "No space left")):
        assert ctrl.save(tmp_path / "playlists.json") is False
    assert ctrl.save(tmp_path / "playlists.json") is True


def test_failed_playlist_save_warns_the_user_once(studio: UltimateAudioStudio) -> None:
    with (
        patch.object(studio.playlist_ctrl, "save", return_value=False),
        patch("app.ui.dialogs.show_warning") as warn,
    ):
        studio.save_playlists()
        studio.save_playlists()
    assert warn.call_count == 1
    assert "could not be saved" in warn.call_args[0][2]

    studio.save_playlists()  # saving works again, so a later failure is news again
    with (
        patch.object(studio.playlist_ctrl, "save", return_value=False),
        patch("app.ui.dialogs.show_warning") as warn,
    ):
        studio.save_playlists()
    assert warn.call_count == 1


def test_unreadable_playlists_are_reported_after_the_window_opens(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    result = PlaylistLoadResult(unreadable=True, damaged_copy=tmp_path / "playlists.damaged-20260101-120000.json")
    with (
        patch.object(studio.playlist_ctrl, "load", return_value=result),
        patch.object(studio.root, "after") as after,
    ):
        studio.load_playlists()
    after.assert_called_once_with(800, studio._report_playlist_load, result)

    with patch("app.ui.dialogs.show_warning") as warn:
        studio._report_playlist_load(result)
    message = warn.call_args[0][2]
    assert "playlists.damaged-20260101-120000.json" in message
    assert "Your songs are safe" in message


def test_restored_playlists_are_reported(studio: UltimateAudioStudio) -> None:
    with patch("app.ui.dialogs.show_warning") as warn:
        studio._report_playlist_load(PlaylistLoadResult(unreadable=True, restored_from_backup=True))
    assert warn.call_args[0][1] == "Playlists Restored"


# --- Export: Cancel leaves earlier exports alone ------------------------------------------------------


def _make_song(folder: Path, name: str = "song0.mp3") -> str:
    song = folder / name
    song.write_bytes(b"ID3" + bytes(64))
    return str(song)


def test_usb_export_stopped_before_it_began_keeps_the_previous_songs(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    for name in ("01 - Old.mp3", "00_Trip.m3u"):
        (drive / name).write_bytes(b"x")
    stop = threading.Event()
    stop.set()
    cancelled: list[tuple[int, int]] = []

    exporter.usb_export_worker(
        str(drive),
        "Trip",
        [_make_song(tmp_path)],
        cancel_event=stop,
        on_cancelled=lambda done, total: cancelled.append((done, total)),
        clear_existing=True,
    )

    assert cancelled == [(0, 1)]
    assert _names(drive) == ["00_Trip.m3u", "01 - Old.mp3"]


def test_cd_export_stopped_before_it_began_keeps_the_previous_tracks(tmp_path: Path) -> None:
    cd = tmp_path / "cd"
    cd.mkdir()
    (cd / "01 - Old.wav").write_bytes(b"x")
    stop = threading.Event()
    stop.set()
    cancelled: list[tuple[int, int]] = []

    exporter.cd_export_worker(
        str(cd),
        [_make_song(tmp_path)],
        cancel_event=stop,
        on_cancelled=lambda done, total: cancelled.append((done, total)),
        clear_existing=True,
    )

    assert cancelled == [(0, 1)]
    assert _names(cd) == ["01 - Old.wav"]


def test_cd_export_removes_only_tracks_this_app_made(tmp_path: Path) -> None:
    cd = tmp_path / "cd"
    cd.mkdir()
    for name in ("01 - Old.wav", "02 - Removed.wav", "Family Recording.wav", "my notes.txt"):
        (cd / name).write_bytes(b"x")
    done: list[exporter.ExportReport] = []

    with patch("app.services.exporter.run_ffmpeg", side_effect=_fake_ffmpeg(b"RIFF")):
        exporter.cd_export_worker(str(cd), [_make_song(tmp_path)], on_success=done.append, clear_existing=True)

    assert done == [exporter.ExportReport(total=1, exported=1)]
    assert _names(cd) == ["01 - song0.wav", "Family Recording.wav", "my notes.txt"]


def test_cancelling_the_cd_length_question_removes_nothing(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    cd = tmp_path / "cd"
    cd.mkdir()
    (cd / "01 - Old.wav").write_bytes(b"x")
    answers = iter(["clear", "no"])  # "Remove them and start fresh", then Cancel at the 80-minute question

    with (
        patch.object(studio.export_ctrl, "get_cd_burn_folder", return_value=str(cd)),
        patch.object(studio.export_ctrl, "get_playlist_duration", return_value=81 * 60.0),
        patch("app.ui.dialogs.ask_choice", side_effect=lambda *_a, **_k: next(answers)),
        patch.object(studio.export_ctrl, "start_cd_export") as start,
    ):
        studio._export_to_cd([_make_song(tmp_path)], False)

    start.assert_not_called()
    assert list(answers) == []  # both questions were asked
    assert _names(cd) == ["01 - Old.wav"]


def test_cd_export_hands_the_removal_to_the_export_job(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    cd = tmp_path / "cd"
    cd.mkdir()
    (cd / "01 - Old.wav").write_bytes(b"x")
    (cd / "my notes.txt").write_bytes(b"x")

    with (
        patch.object(studio.export_ctrl, "get_cd_burn_folder", return_value=str(cd)),
        patch.object(studio.export_ctrl, "get_playlist_duration", return_value=600.0),
        patch("app.ui.dialogs.ask_choice", return_value="clear") as ask,
        patch.object(studio.export_ctrl, "start_cd_export") as start,
    ):
        studio._export_to_cd([_make_song(tmp_path)], False)
        studio._end_export("done")

    assert "1 track(s)" in ask.call_args[0][2]  # the user's own file is not counted
    assert start.call_args.kwargs["clear_existing"] is True
    assert _names(cd) == ["01 - Old.wav", "my notes.txt"]  # the window itself deletes nothing


# --- Replacing a Library song with an imported one -------------------------------------------------


class _FakeRecycleBin:
    """Stands in for send2trash: moves files into a folder, so a test can check what was recycled."""

    def __init__(self, folder: Path, fail_for: str = "") -> None:
        self.folder = folder
        self.fail_for = fail_for
        folder.mkdir(parents=True, exist_ok=True)

    def __call__(self, path: str) -> None:
        if self.fail_for and Path(path).name == self.fail_for:
            raise OSError(32, "The process cannot access the file because it is being used by another process")
        Path(path).replace(self.folder / Path(path).name)


def _import_now(planned: list[tuple[str, str]]) -> ImportResult:
    results: list[ImportResult] = []
    done = threading.Event()

    def _on_done(result: ImportResult) -> None:
        results.append(result)
        done.set()

    LibraryController(None).import_external_files(planned, is_shutting_down_fn=None, on_done=_on_done)
    assert done.wait(timeout=10), "the import never reported back"
    return results[0]


def test_replaced_song_goes_to_the_recycle_bin(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    mine = library / "Song.mp3"
    mine.write_bytes(b"my copy")
    incoming = tmp_path / "usb" / "Song.mp3"
    incoming.parent.mkdir()
    incoming.write_bytes(b"the new copy")
    recycle_bin = _FakeRecycleBin(tmp_path / "bin")

    with patch("app.controllers.library_controller._send2trash", recycle_bin):
        result = _import_now([(str(incoming), str(mine))])

    assert result == ImportResult(copied=("Song.mp3",), replaced=("Song.mp3",))
    assert mine.read_bytes() == b"the new copy"
    assert (recycle_bin.folder / "Song.mp3").read_bytes() == b"my copy"
    assert _names(library) == ["Song.mp3"]


def test_backup_of_the_replaced_song_is_recycled_too(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    mine = library / "Song.mp3"
    mine.write_bytes(b"my trimmed copy")
    Path(original_backup_path(str(mine))).write_bytes(b"my untrimmed copy")
    incoming = tmp_path / "usb" / "Song.mp3"
    incoming.parent.mkdir()
    incoming.write_bytes(b"the new copy")
    recycle_bin = _FakeRecycleBin(tmp_path / "bin")

    with patch("app.controllers.library_controller._send2trash", recycle_bin):
        _import_now([(str(incoming), str(mine))])

    # Otherwise 'Restore Original' would put the old song back over the new one.
    assert not has_original_backup(str(mine))
    assert _names(recycle_bin.folder) == ["Song.mp3", "Song.mp3.original.bak"]


def test_backup_that_cannot_be_recycled_is_set_aside(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    mine = library / "Song.mp3"
    mine.write_bytes(b"my trimmed copy")
    Path(original_backup_path(str(mine))).write_bytes(b"my untrimmed copy")
    incoming = tmp_path / "usb" / "Song.mp3"
    incoming.parent.mkdir()
    incoming.write_bytes(b"the new copy")
    recycle_bin = _FakeRecycleBin(tmp_path / "bin", fail_for="Song.mp3.original.bak")

    with patch("app.controllers.library_controller._send2trash", recycle_bin):
        result = _import_now([(str(incoming), str(mine))])

    assert result.replaced == ("Song.mp3",)
    assert not has_original_backup(str(mine))
    kept = [p for p in library.iterdir() if p.name.startswith("Song.mp3.original.bak.replaced-")]
    assert [p.read_bytes() for p in kept] == [b"my untrimmed copy"]


def test_song_that_cannot_be_recycled_is_left_as_it_was(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    mine = library / "Song.mp3"
    mine.write_bytes(b"my copy")
    incoming = tmp_path / "usb" / "Song.mp3"
    incoming.parent.mkdir()
    incoming.write_bytes(b"the new copy")
    recycle_bin = _FakeRecycleBin(tmp_path / "bin", fail_for="Song.mp3")

    with patch("app.controllers.library_controller._send2trash", recycle_bin):
        result = _import_now([(str(incoming), str(mine))])

    assert result == ImportResult(failed=("Song.mp3",))
    assert mine.read_bytes() == b"my copy"
    assert _names(library) == ["Song.mp3"]  # no half-made copy left behind


def test_replacing_the_song_in_the_player_releases_and_reloads_it(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    mine = library / "Song.mp3"
    mine.write_bytes(b"ID3" + bytes(64))
    incoming = tmp_path / "usb" / "Song.mp3"
    incoming.parent.mkdir()
    incoming.write_bytes(b"ID3" + bytes(128))
    studio.library_folder = str(library)
    studio.refresh_library()
    studio._load_track_ui(str(mine), mine.name)
    recycle_bin = _FakeRecycleBin(tmp_path / "bin")

    with (
        patch("app.controllers.library_controller._send2trash", recycle_bin),
        patch("app.ui.dialogs.ask_yes_no", return_value=True),
        patch.object(studio, "_release_audio_file", wraps=studio._release_audio_file) as release,
        patch.object(studio, "_load_track_ui", wraps=studio._load_track_ui) as reload,
    ):
        studio._import_paths([str(incoming)])
        deadline = time.monotonic() + 5
        while studio._importing and time.monotonic() < deadline:
            studio.root.update()
            time.sleep(0.01)

    release.assert_called()
    reload.assert_called_once_with(str(mine), "Song.mp3")
    assert mine.read_bytes() == b"ID3" + bytes(128)
    assert "Recycle Bin" in studio.status.cget("text")

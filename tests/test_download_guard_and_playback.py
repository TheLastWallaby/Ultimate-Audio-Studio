"""Download-box guard, list selections, playing a song that is being replaced, and smaller usability fixes.

None of these tests needs FFmpeg or a sound device: FFmpeg runs and playback are replaced by fakes.
"""

from __future__ import annotations

import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pygame
import pytest

from app.config import DEFAULT_PLAYLIST_NAME
from app.controllers.download_controller import DownloadController
from app.controllers.playlist_controller import PlaylistController
from app.core.audio_engine import NoAudioDeviceError
from app.models import DriveInfo, ReleaseInfo, SearchResult
from app.services import clipper, updater
from app.services.clipper import clip_audio_worker, original_backup_path
from app.ui import dialogs
from app.ui.features.clip_editor import CLIP_REPLACE_ORIGINAL
from app.ui.features.download import LONG_DOWNLOAD_SEC, _length_in_words


def _pump(root: tk.Misc, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


def _result(seconds: float = 200.0) -> SearchResult:
    return SearchResult("id", "Song", "Artist", seconds, "03:20", "https://www.youtube.com/watch?v=x")


def _song(folder: Path, name: str = "Song.mp3") -> Path:
    """A file with a song's name; playback and FFmpeg are faked, so its contents do not matter."""
    path = folder / name
    path.write_bytes(b"not really audio" * 64)
    return path


# --- 1. Enter in the YouTube box while a job is running ---------------------------------------------


def test_enter_twice_while_searching_opens_one_results_window(studio: Any) -> None:
    opened: list[str] = []

    def slow_search(_query: str, max_results: int = 10) -> list[SearchResult]:
        time.sleep(0.3)
        return [_result()]

    with (
        patch("app.services.downloader.search_youtube", side_effect=slow_search),
        patch("app.ui.features.download.SearchChoiceDialog", side_effect=lambda *a, **_k: opened.append(a[1])),
        patch("app.ui.features.download.ffmpeg_path", __file__),  # ffmpeg.exe is absent on CI
    ):
        studio.entry_url.insert(0, "some song")
        studio.start_download()
        studio.start_download()  # Enter again: the key binding does not look at the disabled button
        _pump(studio.root, 1.2)

    assert opened == ["some song"]


def test_enter_during_a_download_leaves_it_running_and_stoppable(studio: Any) -> None:
    release = threading.Event()
    jobs: list[threading.Event] = []
    reported: list[str] = []

    def fake_worker(_url: str, _folder: str, cancel: threading.Event, *callbacks: Any, **_kw: Any) -> None:
        jobs.append(cancel)
        release.wait(5)
        callbacks[1]("Song.mp3")  # on_success

    with (
        patch("app.controllers.download_controller.download_audio_worker", side_effect=fake_worker),
        patch("app.services.downloader.search_youtube", return_value=[]) as search,
        patch.object(studio, "_download_success", side_effect=reported.append),
        patch("app.ui.features.download.ffmpeg_path", __file__),
    ):
        studio._start_download_url("https://www.youtube.com/watch?v=abc")
        _pump(studio.root, 0.3)
        studio.entry_url.delete(0, tk.END)
        studio.entry_url.insert(0, "another song")
        studio.start_download()  # Enter while the first download is running
        _pump(studio.root, 0.3)

        assert not search.called
        assert studio.download_ctrl.is_downloading
        assert studio.busy_reason() == "the current download"
        assert "Still working" in studio.status.cget("text")
        release.set()
        _pump(studio.root, 0.5)

    assert reported == ["Song.mp3"]
    assert not jobs[0].is_set()


def test_starting_a_job_stops_the_one_it_replaces() -> None:
    controller = DownloadController(None)
    first = controller.reset_cancel()
    controller.is_downloading = True
    second = controller.reset_cancel()

    assert first.is_set()
    assert not second.is_set()
    assert controller.is_downloading is False


def test_a_replaced_search_never_delivers_its_results() -> None:
    delivered: list[list[SearchResult]] = []
    first_may_finish = threading.Event()
    calls = 0

    def search(_query: str, max_results: int = 10) -> list[SearchResult]:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_may_finish.wait(5)
        return [_result()]

    controller = DownloadController(None)
    with patch("app.services.downloader.search_youtube", side_effect=search):
        controller.start_search("one", 6, delivered.append, lambda _err: None)
        time.sleep(0.1)
        controller.start_search("two", 6, delivered.append, lambda _err: None)
        time.sleep(0.2)
        first_may_finish.set()
        time.sleep(0.3)

    assert len(delivered) == 1


# --- 2. Selections in the two lists ---------------------------------------------------------------


def test_library_selection_survives_a_click_in_the_playlist_and_a_dialog(studio: Any) -> None:
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        studio.listbox_lib.insert(tk.END, name)
        studio.listbox_pl.insert(tk.END, name)
    studio.listbox_lib.selection_set(0, 1)
    studio.listbox_pl.selection_set(2)
    entry = tk.Entry(studio.root)
    entry.insert(0, "Suggested name")
    entry.select_range(0, tk.END)  # what ask_text does with its suggestion
    studio.root.update()

    assert studio.listbox_lib.curselection() == (0, 1)
    assert studio.listbox_pl.curselection() == (2,)


# --- 3. Playing a song while its file is being replaced --------------------------------------------


def test_a_song_being_replaced_is_not_started(studio: Any, tmp_path: Path) -> None:
    song, other = _song(tmp_path), _song(tmp_path, "Other.mp3")
    started: list[str] = []
    studio._replacing_song = str(song)

    studio._when_playable(str(song), lambda: started.append("song"))
    assert started == []
    assert "still being saved" in studio.status.cget("text")

    studio._when_playable(str(other), lambda: started.append("other"))
    assert started == ["other"]


def test_save_over_the_original_marks_the_song_until_the_save_ends(studio: Any, tmp_path: Path) -> None:
    song = _song(tmp_path)
    studio.selected_file_path = str(song)
    studio.clip_start_sec, studio.clip_end_sec = 0.0, 5.0
    answer = dialogs.TextAnswer("", CLIP_REPLACE_ORIGINAL)
    with (
        patch("app.ui.dialogs.ask_text", return_value=answer),
        patch("app.ui.features.clip_editor.task_mgr.submit_task") as submit,
    ):
        studio.save_clip()

    assert submit.called
    assert studio._replacing_song == str(song)
    studio._save_error("Access is denied")
    assert studio._replacing_song is None
    assert studio._saving_clip is False


def test_save_as_a_new_song_does_not_block_playing_the_original(studio: Any, tmp_path: Path) -> None:
    song = _song(tmp_path)
    studio.library_folder = str(tmp_path)
    studio.selected_file_path = str(song)
    studio.clip_start_sec, studio.clip_end_sec = 0.0, 5.0
    with (
        patch("app.ui.dialogs.ask_text", return_value=dialogs.TextAnswer("My clip")),
        patch("app.ui.features.clip_editor.task_mgr.submit_task"),
    ):
        studio.save_clip()

    assert studio._replacing_song is None


def _fake_ffmpeg(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    Path(args[-1]).write_bytes(b"clip")
    return subprocess.CompletedProcess(args, 0, "", "")


def test_a_failed_save_over_the_original_leaves_no_backup_behind(tmp_path: Path) -> None:
    song = _song(tmp_path)
    before = song.read_bytes()
    errors: list[str] = []
    with (
        patch.object(clipper, "run_ffmpeg", side_effect=_fake_ffmpeg),
        patch.object(clipper, "_replace_with_retry", side_effect=PermissionError(13, "Access is denied")),
    ):
        clip_audio_worker(str(song), 1.0, 3.0, str(song), is_self_overwrite=True, on_error=errors.append)

    assert len(errors) == 1
    assert song.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Song.mp3"]


def test_a_failed_save_keeps_a_backup_from_an_earlier_trim(tmp_path: Path) -> None:
    song = _song(tmp_path)
    backup = Path(original_backup_path(str(song)))
    backup.write_bytes(b"the untrimmed original")
    with (
        patch.object(clipper, "run_ffmpeg", side_effect=_fake_ffmpeg),
        patch.object(clipper, "_replace_with_retry", side_effect=PermissionError(13, "Access is denied")),
    ):
        clip_audio_worker(str(song), 1.0, 3.0, str(song), is_self_overwrite=True, on_error=lambda _e: None)

    assert backup.read_bytes() == b"the untrimmed original"


# --- 4. The default playlist ------------------------------------------------------------------------


def test_a_renamed_default_playlist_does_not_come_back(tmp_path: Path) -> None:
    path = tmp_path / "playlists.json"
    first = PlaylistController(None)
    first.load(path)
    first.add_track(DEFAULT_PLAYLIST_NAME, "C:/Music/a.mp3")
    assert first.rename_playlist(DEFAULT_PLAYLIST_NAME, "Car")
    assert first.save(path)

    second = PlaylistController(None)
    second.load(path)

    assert second.playlists == {"Car": ["C:/Music/a.mp3"]}
    assert second.active_playlist_name == "Car"


def test_an_empty_playlists_file_still_gets_the_default_playlist(tmp_path: Path) -> None:
    path = tmp_path / "playlists.json"
    path.write_text("{}", encoding="utf-8")
    controller = PlaylistController(None)
    controller.load(path)

    assert controller.playlists == {DEFAULT_PLAYLIST_NAME: []}


# --- 5. The clock of a freshly loaded song --------------------------------------------------------


def test_loading_a_song_puts_the_clock_back_to_the_start(studio: Any, tmp_path: Path) -> None:
    song = _song(tmp_path)
    studio.play_start_offset = 125.0  # where a Test Clip of the previous song ended

    assert studio._load_track_ui(str(song), song.name)

    assert studio.play_start_offset == 0.0
    assert studio.playback_ctrl.current_play_seconds() == 0.0


# --- 7. Music paused by a search preview ------------------------------------------------------------


def test_music_paused_by_a_preview_plays_on_when_the_results_close(studio: Any, tmp_path: Path) -> None:
    studio.selected_file_path = str(_song(tmp_path))
    studio.is_playing_main = True
    with patch.object(studio, "pause_audio") as pause:
        studio._on_search_preview_play()
        assert pause.call_count == 1
        studio.is_paused = True  # what pause_audio does
        studio._search_cancelled()
        assert pause.call_count == 2  # called on a paused song, it resumes


def test_music_the_user_paused_stays_paused_after_a_preview(studio: Any, tmp_path: Path) -> None:
    studio.selected_file_path = str(_song(tmp_path))
    studio.is_playing_main = True
    studio.is_paused = True
    with patch.object(studio, "pause_audio") as pause:
        studio._on_search_preview_play()
        studio._search_cancelled()

    assert not pause.called


# --- 8. A playlist song that cannot be played -------------------------------------------------------


def test_a_playlist_moves_past_a_song_that_cannot_be_played(studio: Any, tmp_path: Path) -> None:
    bad, good = _song(tmp_path, "Bad.mp3"), _song(tmp_path, "Good.mp3")
    studio.playlists[studio.active_playlist_name] = [str(bad), str(good)]
    studio.refresh_playlist_listbox()

    def play(path: str, _start: float = 0.0, is_playlist: bool = False) -> None:
        if path == str(bad):
            raise pygame.error("Unrecognized audio format")

    with patch.object(studio.playback_ctrl, "play_track", side_effect=play) as play_track:
        studio.play_playlist()
        _pump(studio.root, 0.8)

    assert [call.args[0] for call in play_track.call_args_list] == [str(bad), str(good)]
    assert studio.playlist_index == 1


def test_a_playlist_of_only_unplayable_songs_stops_and_says_so(studio: Any, tmp_path: Path) -> None:
    bad = _song(tmp_path, "Bad.mp3")
    studio.playlists[studio.active_playlist_name] = [str(bad)]
    studio.refresh_playlist_listbox()
    studio.repeat_playlist.set(True)  # must not go round forever
    with (
        patch.object(studio.playback_ctrl, "play_track", side_effect=pygame.error("bad file")) as play_track,
        patch("app.ui.dialogs.show_warning") as warned,
    ):
        studio.play_playlist()
        _pump(studio.root, 0.8)

    assert play_track.call_count == 1
    assert "Song Could Not Be Played" in [call.args[1] for call in warned.call_args_list]


def test_no_sound_device_does_not_skip_through_the_playlist(studio: Any, tmp_path: Path) -> None:
    songs = [str(_song(tmp_path, name)) for name in ("One.mp3", "Two.mp3")]
    studio.playlists[studio.active_playlist_name] = songs
    studio.refresh_playlist_listbox()
    with patch.object(studio.playback_ctrl, "play_track", side_effect=NoAudioDeviceError("no device")) as play_track:
        studio.play_playlist()
        _pump(studio.root, 0.8)

    assert play_track.call_count == 1
    assert studio.playlist_index == 0


# --- 10. Very long search results ------------------------------------------------------------------


def test_a_very_long_search_result_is_confirmed_before_downloading(studio: Any) -> None:
    long_one = _result(seconds=10 * 3600)
    with patch.object(studio, "_start_download_url") as start:
        with patch("app.ui.dialogs.ask_yes_no", return_value=False) as asked:
            studio._search_result_chosen(long_one)
        assert "10 hours" in asked.call_args.args[2]
        assert not start.called

        with patch("app.ui.dialogs.ask_yes_no", return_value=True):
            studio._search_result_chosen(long_one)
        start.assert_called_once_with(long_one.url, long_one.title)


def test_an_ordinary_song_downloads_without_a_question(studio: Any) -> None:
    with (
        patch.object(studio, "_start_download_url") as start,
        patch("app.ui.dialogs.ask_yes_no") as asked,
    ):
        studio._search_result_chosen(_result(seconds=LONG_DOWNLOAD_SEC))

    assert start.called
    assert not asked.called


@pytest.mark.parametrize(
    ("seconds", "words"),
    [
        (25 * 60, "25 minutes"),
        (3600, "1 hour"),
        (3900, "1 hour 5 minutes"),
        (36000, "10 hours"),
        (61 * 60, "1 hour 1 minute"),
    ],
)
def test_length_in_words(seconds: float, words: str) -> None:
    assert _length_in_words(seconds) == words


# --- 11. Smaller items ------------------------------------------------------------------------------


def test_a_drive_that_comes_or_goes_rechecks_the_missing_marks(studio: Any) -> None:
    drive = DriveInfo("E:\\", "USB Drive: MUSIC (E:\\) [FAT32]", "FAT32")
    with patch.object(studio, "_warm_library_metadata") as warm:
        studio._usb_scan_id += 1
        studio._show_usb_drives(studio._usb_scan_id, [drive], "changes")
        assert warm.call_count == 1
        studio._usb_scan_id += 1
        studio._show_usb_drives(studio._usb_scan_id, [drive], "changes")  # nothing changed
        assert warm.call_count == 1
        studio._usb_scan_id += 1
        studio._show_usb_drives(studio._usb_scan_id, [], "quiet")  # start-up and after an eject
        assert warm.call_count == 1


def test_the_usb_drive_list_is_only_shown_for_a_usb_export(studio: Any) -> None:
    studio.export_var.set("CD")
    studio.on_export_target_changed()
    assert studio.f_usb.winfo_manager() == ""

    studio.export_var.set("USB")
    studio.on_export_target_changed()
    assert studio.f_usb.winfo_manager() == "pack"
    # Back in its place: above the "equally loud" tick box, not at the bottom of the card.
    order = studio.f_usb.master.pack_slaves()
    assert order.index(studio.f_usb) < order.index(studio.chk_even_volume)


def test_a_library_folder_that_will_not_open_is_explained_in_plain_words(studio: Any) -> None:
    with (
        patch("app.ui.features.library.os.startfile", side_effect=OSError(2, "The system cannot find the path")),
        patch("app.ui.dialogs.show_warning") as warned,
    ):
        studio.open_library_folder()

    title, message = warned.call_args.args[1], warned.call_args.args[2]
    assert title == "Folder Could Not Be Opened"
    assert "cannot find the path" not in message
    assert "plugged in" in message


def _release() -> ReleaseInfo:
    return ReleaseInfo("v99.0.0", "v99.0.0", "notes", "", 1, "app.exe", 0, "", "", "")


def test_two_update_downloads_at_once_fetch_the_file_only_once(tmp_path: Path) -> None:
    downloads = 0

    def fake_download(_asset_id: int, dest_path: str, **_kwargs: Any) -> tuple[bool, str]:
        nonlocal downloads
        downloads += 1
        time.sleep(0.3)
        Path(dest_path).write_bytes(b"MZ" + b"\0" * 64)
        return True, dest_path

    results: list[tuple[updater.PendingUpdate | None, str]] = []
    with (
        patch.object(updater, "PENDING_DIR", tmp_path / "staging"),
        patch.object(updater, "_PENDING_FILE", tmp_path / "staging" / "pending.json"),
        patch.object(updater, "download_release_asset", side_effect=fake_download),
    ):
        threads = [threading.Thread(target=lambda: results.append(updater.stage_update(_release()))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

    assert downloads == 1
    assert [pending.tag for pending, _err in results if pending] == ["v99.0.0", "v99.0.0"]


def test_waiting_for_another_update_download_can_be_cancelled() -> None:
    cancel = threading.Event()
    cancel.set()
    download = MagicMock()
    with updater._stage_lock, patch.object(updater, "download_release_asset", download):
        pending, reason = updater.stage_update(_release(), cancel_event=cancel)

    assert pending is None
    assert reason == "Download cancelled"
    assert not download.called

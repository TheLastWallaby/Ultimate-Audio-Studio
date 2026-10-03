"""Regression tests for close-while-busy, download jobs, paused seeking, playlists, delete, and CI helpers."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from app.controllers.download_controller import DownloadController
from app.controllers.library_controller import LibraryController
from app.controllers.playlist_controller import PlaylistController
from app.core.cache_manager import cache_mgr
from app.main import STATUS_BAR_BG, UltimateAudioStudio
from app.services.downloader import download_playlist_worker
from app.ui.theme import COLOR_PLAY


def _make_app() -> UltimateAudioStudio:
    root = tk.Tk()
    root.withdraw()
    return UltimateAudioStudio(root)


class TestCloseWhileBusy(unittest.TestCase):
    def test_close_is_confirmed_while_exporting(self) -> None:
        app = _make_app()
        try:
            app._exporting = True
            with (
                patch("app.ui.dialogs.ask_yes_no", return_value=False) as ask,
                patch.object(app, "on_close") as close,
            ):
                app.request_close()
            ask.assert_called_once()
            self.assertIn("the playlist export", ask.call_args[0][2])
            close.assert_not_called()
            self.assertIn("Still working", app.status.cget("text"))
        finally:
            app._exporting = False
            app.on_close()

    def test_close_is_immediate_when_idle(self) -> None:
        app = _make_app()
        with patch("app.ui.dialogs.ask_yes_no") as ask, patch.object(app, "on_close") as close:
            app.request_close()
        ask.assert_not_called()
        close.assert_called_once()
        app.on_close()


class _InlineTasks:
    """Stand-in for task_mgr that records submitted work instead of running it on a thread."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []

    def submit_task(self, fn: Any, *args: Any, **_kwargs: Any) -> None:
        self.calls.append((fn, args))


class TestDownloadJobs(unittest.TestCase):
    def test_playlist_after_a_stopped_download_is_not_reported_as_stopped(self) -> None:
        """Regression: the cancel flag of a stopped download used to be left set for the playlist scan."""
        dl = DownloadController(None)
        dl.cancel()  # user pressed Stop on an earlier download
        tasks = _InlineTasks()
        probed: list[tuple[str, int]] = []
        cancelled: list[bool] = []
        info = {"title": "Mix", "entries": [{"id": "a"}, {"id": "b"}]}
        with (
            patch("app.controllers.download_controller.task_mgr", tasks),
            patch("app.controllers.download_controller.probe_playlist_info", return_value=info),
            patch("app.controllers.download_controller.download_playlist_worker") as worker,
        ):
            dl.start_playlist(
                "https://www.youtube.com/playlist?list=PL1",
                "lib",
                on_probed=lambda title, total: probed.append((title, total)),
                on_track_start=MagicMock(),
                on_track_progress=MagicMock(),
                on_track_finished=MagicMock(),
                on_track_failed=MagicMock(),
                on_batch_complete=MagicMock(),
                on_cancelled=lambda: cancelled.append(True),
                on_error=MagicMock(),
            )
            self.assertTrue(dl.is_downloading, "the scan already counts as downloading (blocks update restarts)")
            fn, _args = tasks.calls[0]
            fn()
        self.assertEqual(probed, [("Mix", 2)])
        self.assertEqual(cancelled, [])
        worker.assert_called_once()

    def test_callbacks_from_a_replaced_job_are_dropped(self) -> None:
        """Regression: Stop then Download again used to revive the first worker and reset the new job's UI."""
        dl = DownloadController(None)
        tasks = _InlineTasks()
        first_success: list[str] = []
        second_success: list[str] = []
        with (
            patch("app.controllers.download_controller.task_mgr", tasks),
            patch("app.controllers.download_controller.cleanup_partial_downloads") as cleanup,
        ):
            dl.start_download("u1", "lib", MagicMock(), first_success.append, MagicMock(), MagicMock())
            first_job = dl.cancel_event
            dl.cancel()
            dl.start_download("u2", "lib", MagicMock(), second_success.append, MagicMock(), MagicMock())
            self.assertIsNot(dl.cancel_event, first_job)
            self.assertFalse(dl.cancel_event.is_set())
            # The first worker winds down and reports back: nothing may reach the UI or touch shared state.
            _fn, first_args = tasks.calls[0]
            _url, _lib, job, _progress, on_success, on_cancelled, _on_error = first_args
            self.assertIs(job, first_job)
            on_cancelled()
            on_success("old.mp3")
            cleanup.assert_not_called()
        self.assertTrue(dl.is_downloading)
        self.assertEqual(first_success, [])
        self.assertEqual(second_success, [])


class TestPlaylistWorkerFailures(unittest.TestCase):
    def test_failed_tracks_are_reported_with_their_title(self) -> None:
        failed: list[tuple[int, str, str]] = []
        completed: list[list[str]] = []

        def fake_download(url: str, _lib: str, _cancel: Any, _prog: Any, succ: Any, _canc: Any, fail: Any) -> None:
            if url.endswith("bad"):
                fail("ERROR: Private video")
            else:
                succ("good.mp3")

        with tempfile.TemporaryDirectory() as td, patch("app.services.downloader.download_audio_worker", fake_download):
            download_playlist_worker(
                [
                    {"url": "https://y/good", "title": "Good"},
                    {"url": "https://y/bad", "title": "Bad"},
                    {"title": "No link"},
                ],
                td,
                threading.Event(),
                on_batch_complete=lambda files, _tot: completed.append(files),
                on_track_failed=lambda idx, _tot, title, err: failed.append((idx, title, err)),
            )
        self.assertEqual(completed, [["good.mp3"]])
        self.assertEqual([(i, t) for i, t, _e in failed], [(2, "Bad"), (3, "No link")])
        self.assertIn("Private video", failed[0][2])


class TestPausedWaveformSeek(unittest.TestCase):
    def test_click_on_waveform_while_paused_moves_the_resume_position(self) -> None:
        """Regression: Resume used to jump back to where the song was paused."""
        app = _make_app()
        try:
            app.selected_file_path = __file__
            app.track_duration = 200.0
            app.clip_start_sec, app.clip_end_sec = 0.0, 200.0
            app.playback_ctrl.is_playing_main = True
            app.playback_ctrl.is_paused = True
            app.playback_ctrl.paused_position = 10.0
            view = app.waveform_view
            width = max(1, view.canvas.winfo_width())
            x = width * 0.5  # away from the start/end markers at the edges
            view.on_click(SimpleNamespace(x=x, y=10))
            expected = view.time_from_x(x, width)
            self.assertAlmostEqual(app.playback_ctrl.paused_position, expected)
            self.assertTrue(app.playback_ctrl._scrubbed_while_paused)
        finally:
            app.playback_ctrl.is_playing_main = False
            app.playback_ctrl.is_paused = False
            app.on_close()


class TestPlaylistIndexTracking(unittest.TestCase):
    def _controller(self, n: int = 5, playing: int = 3) -> PlaylistController:
        pl = PlaylistController(None)
        pl.playlists = {"P": [f"s{i}.mp3" for i in range(n)]}
        pl.active_playlist_name = "P"
        pl.playlist_index = playing
        return pl

    def test_remove_above_playing_song_keeps_index_on_it(self) -> None:
        pl = self._controller()
        pl.remove_track_with_undo("P", 1)
        self.assertEqual(pl.get_current_track(), "s3.mp3")
        pl.undo_remove()
        self.assertEqual(pl.get_current_track(), "s3.mp3")

    def test_remove_below_playing_song_does_not_move_index(self) -> None:
        pl = self._controller()
        pl.remove_track_with_undo("P", 4)
        self.assertEqual(pl.get_current_track(), "s3.mp3")

    def test_drag_moves_keep_index_on_playing_song(self) -> None:
        for src, dst in ((0, 4), (4, 0), (3, 0), (1, 2), (2, 1)):
            pl = self._controller()
            playing = pl.get_current_track()
            self.assertEqual(pl.move_track("P", src, dst), dst)
            self.assertEqual(pl.get_current_track(), playing, f"move {src}->{dst}")

    def test_duplicates_are_not_added(self) -> None:
        pl = self._controller(n=1, playing=0)
        self.assertFalse(pl.add_track("P", "s0.mp3"))
        self.assertFalse(pl.add_track("P", "S0.MP3"))
        self.assertTrue(pl.add_track("P", "new.mp3"))
        self.assertEqual(len(pl.playlists["P"]), 2)

    def test_deleting_playlist_resets_index(self) -> None:
        pl = self._controller()
        pl.playlists["Other"] = ["x.mp3"]
        pl.delete_playlist("P")
        self.assertEqual(pl.playlist_index, 0)


class TestMultiDelete(unittest.TestCase):
    def test_batch_delete_and_undo_restore_files_and_playlist_positions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            songs = [os.path.join(td, f"{name}.mp3") for name in ("a", "b", "c")]
            for song in songs:
                Path(song).write_text("audio")
            playlists = {"P": [songs[0], songs[1], songs[2]]}
            lib = LibraryController(None)
            staged, failed = lib.stage_delete_many([songs[0], songs[2]], playlists)
            self.assertEqual((staged, failed), (["a.mp3", "c.mp3"], []))
            self.assertEqual(playlists["P"], [songs[1]])
            self.assertTrue(lib.undo_delete(playlists))
            self.assertEqual(playlists["P"], songs)
            self.assertTrue(all(os.path.exists(s) for s in songs))

    def test_locked_file_is_reported_and_stays_in_playlists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            song = os.path.join(td, "locked.mp3")
            Path(song).write_text("audio")
            playlists = {"P": [song]}
            lib = LibraryController(None)
            with patch("app.controllers.library_controller.shutil.move", side_effect=PermissionError(32, "in use")):
                staged, failed = lib.stage_delete_many([song], playlists)
            self.assertEqual(staged, [])
            self.assertEqual([name for name, _e in failed], ["locked.mp3"])
            self.assertEqual(playlists["P"], [song])
            self.assertTrue(os.path.exists(song))


class TestMainWindowHelpers(unittest.TestCase):
    def test_library_watcher_notices_renames(self) -> None:
        app = _make_app()
        known = {"title": "", "artist": "", "duration": 100.0}
        try:
            with (
                tempfile.TemporaryDirectory() as td,
                patch("app.ui.features.library.read_track_metadata") as read_details,
            ):
                Path(td, "old name.mp3").write_text("x")
                app.library_folder = td
                # With the details already known, no background reader (tinytag, then ffprobe) holds
                # the file open while it is renamed or while the folder is deleted (WinError 32).
                cache_mgr.set_metadata(os.path.join(app.library_folder, "old name.mp3"), known)
                app.refresh_library()
                os.rename(os.path.join(td, "old name.mp3"), os.path.join(td, "new name.mp3"))
                cache_mgr.set_metadata(os.path.join(app.library_folder, "new name.mp3"), known)
                app._watch_library()
                deadline = time.monotonic() + 5  # the folder is read on a worker
                while app.library_files != ["new name.mp3"] and time.monotonic() < deadline:
                    app.root.update()
                    time.sleep(0.01)
                self.assertEqual(app.library_files, ["new name.mp3"])
            renamed = {"old name.mp3", "new name.mp3"}
            self.assertEqual([c for c in read_details.call_args_list if Path(c.args[0]).name in renamed], [])
        finally:
            app.on_close()

    def test_save_settings_cancels_pending_debounced_save(self) -> None:
        app = _make_app()
        try:
            app._schedule_settings_save()
            pending = app._timer_settings_save
            self.assertIsNotNone(pending)
            app._save_settings()
            self.assertIsNone(app._timer_settings_save)
            self.assertNotIn(pending, app.root.tk.call("after", "info"))
        finally:
            app.on_close()

    def test_notify_success_flashes_status_bar_then_restores_it(self) -> None:
        app = _make_app()
        try:
            with patch("app.main.STATUS_FLASH_MS", 1):
                app.notify_success("Done!")
            self.assertEqual(app.status.cget("text"), "Done!")
            self.assertEqual(app.status.cget("bg"), COLOR_PLAY)
            deadline = time.monotonic() + 2.0
            while app._timer_status_flash is not None and time.monotonic() < deadline:
                app.root.update()
            self.assertEqual(app.status.cget("bg"), STATUS_BAR_BG)
            self.assertEqual(app.status.cget("text"), "Done!")
        finally:
            app.on_close()


class TestBumpVersionScript(unittest.TestCase):
    def test_next_patch(self) -> None:
        path = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "bump_version.py"
        spec = importlib.util.spec_from_file_location("bump_version", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.next_patch("1.1.4"), "1.1.5")
        self.assertEqual(module.next_patch("2.9.9"), "2.9.10")


if __name__ == "__main__":
    unittest.main()

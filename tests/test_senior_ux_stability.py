"""Tests for the senior-UX and stability improvements: dialogs, downloads, export, updates, lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app import self_test
from app.config import ffmpeg_path, is_valid_binary
from app.core.process_utils import CANCELLED_STDERR, run_ffmpeg
from app.main import UltimateAudioStudio
from app.models import ReleaseInfo
from app.services import exporter, updater
from app.services.clipper import has_original_backup, original_backup_path, restore_original
from app.services.downloader import TitleCleanerPP, clean_song_title, has_video_id, is_playlist_url
from app.ui import dialogs
from app.ui.theme import init_fonts, register_scaled, rescale_widgets, scaled_px

HAVE_FFMPEG = is_valid_binary(ffmpeg_path)


def _make_app() -> UltimateAudioStudio:
    root = tk.Tk()
    root.withdraw()
    return UltimateAudioStudio(root)


class TestDialogs(unittest.TestCase):
    """The themed dialogs return the clicked button's value and follow Enter/Escape."""

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        init_fonts(self.root)
        self._timers: list[str] = []

    def tearDown(self) -> None:
        for timer in self._timers:
            self.root.after_cancel(timer)
        self.root.destroy()

    def _answer(self, action: str) -> None:
        """Once the dialog is up, click a button by its label, press the focused button ("focused"),
        or use the window's close button ("close").

        Key events are not generated: they go to the focused window, and a test window in the
        background has none. A fallback closes any dialog after 5 s so a test can never hang.
        """

        def _dialog() -> tk.Toplevel | None:
            return next((w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)), None)

        def _act() -> None:
            dialog = _dialog()
            assert dialog is not None, "dialog did not open"
            if action == "focused":
                # The widget Tk gives the keyboard focus to (the test window itself is in the background).
                focused = dialog.focus_lastfor()
                assert isinstance(focused, tk.Button), f"no button has the keyboard focus: {focused!r}"
                focused.invoke()
            elif action == "close":
                dialog.tk.call(dialog.tk.call("wm", "protocol", str(dialog), "WM_DELETE_WINDOW"))
            else:
                button = next(w for w in _descendants(dialog) if isinstance(w, tk.Button) and w.cget("text") == action)
                button.invoke()

        def _fallback() -> None:
            dialog = _dialog()
            if dialog is not None:
                dialog.destroy()

        self._timers += [self.root.after(150, _act), self.root.after(5000, _fallback)]

    @pytest.mark.real_dialogs
    def test_clicked_button_value_is_returned(self) -> None:
        self._answer("Just this one song")
        buttons = [
            dialogs.DialogButton("Download all 3 songs", "all", "primary"),
            dialogs.DialogButton("Just this one song", "one"),
            dialogs.DialogButton("Cancel", "cancel"),
        ]
        self.assertEqual(dialogs.ask_choice(self.root, "T", "M", buttons, cancel_value="cancel"), "one")

    @pytest.mark.real_dialogs
    def test_closing_the_window_returns_cancel_value(self) -> None:
        self._answer("close")
        self.assertFalse(dialogs.ask_yes_no(self.root, "T", "M", yes="Delete", no="Keep"))

    @pytest.mark.real_dialogs
    def test_safe_choice_has_the_keyboard_focus(self) -> None:
        # A destructive question defaults to the safe answer, so Enter never deletes by accident.
        self._answer("focused")
        self.assertFalse(dialogs.ask_yes_no(self.root, "T", "M", yes="Delete", no="Keep", default_yes=False))


def _descendants(widget: tk.Misc) -> list[tk.Misc]:
    out: list[tk.Misc] = []
    for child in widget.winfo_children():
        out.append(child)
        out.extend(_descendants(child))
    return out


class TestPlaylistLinks(unittest.TestCase):
    def test_youtube_mix_with_video_is_a_single_song(self) -> None:
        self.assertFalse(is_playlist_url("https://www.youtube.com/watch?v=abc&list=RDabc&start_radio=1"))
        self.assertFalse(is_playlist_url("https://youtu.be/abc?list=RDMMabc"))
        self.assertFalse(is_playlist_url("https://music.youtube.com/watch?v=x&list=RDAMVMx"))

    def test_real_playlists_are_still_offered(self) -> None:
        self.assertTrue(is_playlist_url("https://www.youtube.com/playlist?list=PL12345"))
        self.assertTrue(is_playlist_url("https://www.youtube.com/watch?v=abc&list=PL12345"))
        self.assertTrue(is_playlist_url("https://www.youtube.com/watch?v=abc&list=RDCLAK5uy_curated"))

    def test_has_video_id(self) -> None:
        self.assertTrue(has_video_id("https://youtu.be/abc?list=PL1"))
        self.assertTrue(has_video_id("https://www.youtube.com/shorts/abc"))
        self.assertFalse(has_video_id("https://www.youtube.com/playlist?list=PL1"))

    def test_playlist_question_shows_count_and_verb_buttons(self) -> None:
        app = _make_app()
        try:
            info = {"title": "Road Trip", "count": 3, "entries": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
            url = "https://www.youtube.com/watch?v=a&list=PL1"
            with (
                patch("app.ui.dialogs.ask_choice", return_value="one") as ask,
                patch.object(app, "_start_download_url") as single,
                patch.object(app, "_start_playlist_download") as whole,
            ):
                app._on_playlist_checked(url, info)
            labels = [b.label for b in ask.call_args[0][3]]
            self.assertEqual(labels, ["Download all 3 songs", "Just this one song", "Cancel"])
            self.assertIn("Road Trip", ask.call_args[0][2])
            single.assert_called_once_with(url)
            whole.assert_not_called()
        finally:
            app.on_close()


class TestTitleCleaning(unittest.TestCase):
    def test_noise_is_removed(self) -> None:
        self.assertEqual(clean_song_title("Frank Sinatra - My Way (Official Audio)"), "Frank Sinatra - My Way")
        self.assertEqual(clean_song_title("Adele - Hello [4K] (Lyrics)"), "Adele - Hello")
        self.assertEqual(clean_song_title("Song Title | Official Music Video"), "Song Title")
        self.assertEqual(clean_song_title("Beatles - Let It Be (Remastered 2009)"), "Beatles - Let It Be")

    def test_meaningful_brackets_are_kept(self) -> None:
        self.assertEqual(
            clean_song_title("Eagles - Hotel California (Live 1977)"), "Eagles - Hotel California (Live 1977)"
        )
        self.assertEqual(clean_song_title("Hallelujah (Acoustic)"), "Hallelujah (Acoustic)")

    def test_title_never_becomes_empty(self) -> None:
        self.assertEqual(clean_song_title("(Official Video)"), "(Official Video)")

    def test_postprocessor_splits_artist_only_when_youtube_gave_none(self) -> None:
        pp = TitleCleanerPP()
        _, info = pp.run({"title": "Adele – Hello (Official Music Video)"})
        self.assertEqual((info["title"], info["artist"], info["track"]), ("Adele – Hello", "Adele", "Hello"))
        _, info = pp.run({"title": "Hello (Official)", "artist": "Adele"})
        self.assertEqual(info, {"title": "Hello", "artist": "Adele"})


class TestUsbExport(unittest.TestCase):
    def _make_songs(self, folder: str, count: int) -> list[str]:
        paths = []
        for i in range(count):
            path = os.path.join(folder, f"song{i}.mp3")
            Path(path).write_bytes(b"ID3" + bytes(64))
            paths.append(path)
        return paths

    def test_three_digit_numbers_for_long_playlists(self) -> None:
        self.assertEqual(exporter.track_number_width(99), 2)
        self.assertEqual(exporter.track_number_width(100), 3)

    def test_replacing_a_previous_export_keeps_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dest:
            for name in ("01 - Old.mp3", "02 - Removed.mp3", "00_Trip.m3u", "00_Trip.m3u8"):
                Path(dest, name).write_bytes(b"x")
            Path(dest, "my notes.txt").write_text("keep me", encoding="utf-8")
            Path(dest, "Holiday Photo.jpg").write_bytes(b"x")
            songs = self._make_songs(src, 1)
            done: list[tuple[int, int, list[str]]] = []
            exporter.usb_export_worker(dest, "Trip", songs, on_success=lambda *a: done.append(a), clear_existing=True)
            self.assertEqual(done[0][:2], (1, 1))
            self.assertEqual(
                sorted(os.listdir(dest)),
                sorted(["01 - song0.mp3", "00_Trip.m3u", "00_Trip.m3u8", "my notes.txt", "Holiday Photo.jpg"]),
            )

    def test_find_previous_export_ignores_user_files(self) -> None:
        with tempfile.TemporaryDirectory() as dest:
            Path(dest, "01 - A.mp3").write_bytes(b"x")
            Path(dest, "Notes 01 - x.txt").write_bytes(b"x")
            Path(dest, "12 Days.mp3").write_bytes(b"x")
            found = [os.path.basename(p) for p in exporter.find_previous_export(dest)]
            self.assertEqual(found, ["01 - A.mp3"])

    def test_stop_before_start_reports_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dest:
            songs = self._make_songs(src, 3)
            stop = threading.Event()
            stop.set()
            cancelled: list[tuple[int, int]] = []
            succeeded: list[object] = []
            exporter.usb_export_worker(
                dest,
                "Trip",
                songs,
                on_success=lambda *a: succeeded.append(a),
                cancel_event=stop,
                on_cancelled=lambda done, total: cancelled.append((done, total)),
            )
            self.assertEqual(cancelled, [(0, 3)])
            self.assertEqual(succeeded, [])

    def test_export_asks_to_replace_previous_songs(self) -> None:
        app = _make_app()
        try:
            with tempfile.TemporaryDirectory() as drive, tempfile.TemporaryDirectory() as src:
                app.active_playlist_name = "Trip"
                folder = app.export_ctrl.usb_playlist_folder(drive, "Trip")
                os.makedirs(folder)
                Path(folder, "01 - Old.mp3").write_bytes(b"x")
                label = f"USB Drive: TEST ({drive}) [FAT32]"
                app._usb_map = {label: drive}
                app._usb_fs_map = {label: "FAT32"}
                app.usb_choice.set(label)
                app.export_var.set("USB")
                files = self._make_songs(src, 1)
                with (
                    patch("app.ui.dialogs.ask_choice", return_value="replace") as ask,
                    patch.object(app.export_ctrl, "start_usb_export") as start,
                ):
                    app._export_after_preflight(files)
                self.assertIn("earlier export", ask.call_args[0][2])
                self.assertTrue(start.call_args.kwargs["clear_existing"])
                self.assertTrue(app.btn_cancel_export.winfo_manager())  # Stop Export is offered
                app._end_export("done")
        finally:
            app.on_close()


@unittest.skipUnless(HAVE_FFMPEG, "needs a working ffmpeg")
class TestCancellableFfmpeg(unittest.TestCase):
    def test_cancel_stops_ffmpeg_quickly(self) -> None:
        stop = threading.Event()
        threading.Timer(0.4, stop.set).start()
        started = time.monotonic()
        # -re makes FFmpeg run in real time, so this would take 60 s without the cancel.
        res = run_ffmpeg(["-re", "-f", "lavfi", "-i", "sine=duration=60", "-f", "null", "-"], cancel_event=stop)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(res.returncode, -1)
        self.assertEqual(res.stderr, CANCELLED_STDERR)

    def test_completed_run_returns_output(self) -> None:
        res = run_ffmpeg(["-version"], cancel_event=threading.Event())
        self.assertEqual(res.returncode, 0)
        self.assertIn("ffmpeg", res.stdout.lower())


@unittest.skipUnless(os.name == "nt", "child tracking is part of the Windows Popen wrapper")
class TestChildProcesses(unittest.TestCase):
    def test_running_helpers_are_killed(self) -> None:
        from app.platform_utils import terminate_child_processes

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertGreaterEqual(terminate_child_processes(block_new=False), 1)
            proc.wait(timeout=10)
            self.assertIsNotNone(proc.returncode)
        finally:
            if proc.poll() is None:
                proc.kill()


class TestRestoreOriginal(unittest.TestCase):
    def test_restore_puts_original_back_and_trashes_trim(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            song = os.path.join(td, "song.mp3")
            Path(song).write_bytes(b"trimmed")
            Path(original_backup_path(song)).write_bytes(b"original")
            self.assertTrue(has_original_backup(song))
            with patch("app.services.clipper._send2trash") as trash:
                trash.side_effect = os.remove
                restore_original(song)
            self.assertEqual(Path(song).read_bytes(), b"original")
            self.assertFalse(has_original_backup(song))
            trash.assert_called_once_with(song)

    def test_rename_and_delete_undo_carry_the_backup(self) -> None:
        from app.controllers.library_controller import LibraryController

        with tempfile.TemporaryDirectory() as td:
            song = os.path.join(td, "a.mp3")
            Path(song).write_bytes(b"trim")
            Path(original_backup_path(song)).write_bytes(b"orig")
            ctrl = LibraryController(None)
            playlists: dict[str, list[str]] = {"P": [song]}
            renamed = ctrl.rename_file(song, "b.mp3", playlists)
            self.assertTrue(has_original_backup(renamed))
            self.assertFalse(os.path.exists(original_backup_path(song)))

            ctrl.stage_delete_with_undo(renamed, playlists)
            self.assertFalse(os.path.exists(original_backup_path(renamed)))
            self.assertTrue(ctrl.undo_delete(playlists))
            self.assertTrue(has_original_backup(renamed))


class TestNewSongIsLoaded(unittest.TestCase):
    def test_download_loads_song_when_player_is_idle(self) -> None:
        app = _make_app()
        try:
            with tempfile.TemporaryDirectory() as td:
                app.library_folder = td
                Path(td, "New Song.mp3").write_bytes(b"ID3" + bytes(64))
                app._download_success("New Song.mp3")
                self.assertEqual(app.selected_file_path, os.path.join(td, "New Song.mp3"))
                self.assertIn("press PLAY", app.status.cget("text"))
        finally:
            app.on_close()

    def test_download_only_marks_row_while_music_plays(self) -> None:
        app = _make_app()
        try:
            with tempfile.TemporaryDirectory() as td:
                app.library_folder = td
                Path(td, "Old.mp3").write_bytes(b"ID3" + bytes(64))
                Path(td, "New.mp3").write_bytes(b"ID3" + bytes(64))
                app.selected_file_path = os.path.join(td, "Old.mp3")
                app.playback_ctrl.is_playing_main = True
                with patch.object(app, "stop_audio") as stop:
                    app._download_success("New.mp3")
                stop.assert_not_called()
                self.assertEqual(app.selected_file_path, os.path.join(td, "Old.mp3"))
                idx = app.visible_files.index("New.mp3")
                self.assertEqual(app.listbox_lib.itemcget(idx, "background"), "#dcfce7")
                self.assertEqual(app.listbox_lib.curselection(), ())
                app.playback_ctrl.is_playing_main = False
        finally:
            app.on_close()


class TestScaling(unittest.TestCase):
    def test_registered_sizes_follow_text_size(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            init_fonts(root, "Normal")
            scale = tk.Scale(root)
            register_scaled(scale, width=20)
            normal = int(scale.cget("width"))
            self.assertEqual(normal, scaled_px(scale, 20))
            init_fonts(root, "Extra Large")
            rescale_widgets()
            self.assertGreater(int(scale.cget("width")), normal)
        finally:
            init_fonts(root, "Normal")
            root.destroy()


class TestUpdater(unittest.TestCase):
    def _release(self, tag: str = "v9.9.9") -> ReleaseInfo:
        return ReleaseInfo(tag, tag, "", "", 1, "a.exe", 2_000_000, "", "", "", "")

    def test_staged_update_is_verified_when_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.object(updater, "PENDING_DIR", Path(td)):
            pending_file = Path(td) / "pending.json"
            with patch.object(updater, "_PENDING_FILE", pending_file):
                exe = Path(td) / "new.exe"
                exe.write_bytes(b"MZ" + bytes(100))
                digest = hashlib.sha256(exe.read_bytes()).hexdigest()
                pending_file.write_text(json.dumps({"tag": "v9.9.9", "exe_path": str(exe), "sha256": digest}))
                self.assertIsNotNone(updater.load_pending_update("1.0.0"))
                exe.write_bytes(b"MZ tampered")
                self.assertIsNone(updater.load_pending_update("1.0.0"))
                self.assertFalse(Path(td).exists())  # stale staging data is removed

    def test_install_restores_previous_version_when_new_one_dies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            current = Path(td) / "app.exe"
            current.write_bytes(b"OLD")
            new = Path(td) / "download.exe"
            new.write_bytes(b"NEW")
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(current)),
                patch.object(updater, "create_named_event", return_value=1),
                patch.object(updater, "close_handle"),
                patch.object(updater, "release_instance_mutex"),
                patch.object(updater, "launch_update_process", return_value=MagicMock()),
                patch.object(updater, "wait_for_event_or_exit", return_value="exited"),
                patch.object(updater, "clear_pending_update"),
                patch.object(updater.settings_mgr, "update_settings") as remember,
            ):
                result = updater.install_update(new, "v9.9.9", exit_fn=_no_exit)
            self.assertTrue(result.rolled_back)
            self.assertEqual(current.read_bytes(), b"OLD")
            remember.assert_called_once_with(failed_update_tag="v9.9.9")

    def test_install_exits_once_new_version_signals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            current = Path(td) / "app.exe"
            current.write_bytes(b"OLD")
            new = Path(td) / "download.exe"
            new.write_bytes(b"NEW")
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(current)),
                patch.object(updater, "create_named_event", return_value=1),
                patch.object(updater, "close_handle"),
                patch.object(updater, "release_instance_mutex"),
                patch.object(updater, "launch_update_process", return_value=MagicMock()),
                patch.object(updater, "wait_for_event_or_exit", return_value="signaled"),
                patch.object(updater, "clear_pending_update"),
                self.assertRaises(SystemExit),
            ):
                updater.install_update(new, "v9.9.9", exit_fn=sys.exit)
            self.assertEqual(current.read_bytes(), b"NEW")
            self.assertEqual(Path(str(current) + ".old").read_bytes(), b"OLD")

    def test_found_update_is_staged_quietly_without_a_dialog(self) -> None:
        app = _make_app()
        try:
            with (
                patch.object(app.update_ctrl, "should_stage_automatically", return_value=True),
                patch.object(app.update_ctrl, "stage_in_background") as stage,
                patch.object(app, "_open_update_dialog") as dialog,
            ):
                app._handle_update_found(self._release())
            stage.assert_called_once()
            dialog.assert_not_called()
        finally:
            app.on_close()


def _no_exit(_code: int) -> Any:
    raise AssertionError("install_update must not exit when the new version failed")


class TestSelfTest(unittest.TestCase):
    def test_report_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "r.txt"
            with patch.object(self_test, "CHECKS", (("ok", lambda: None), ("bad", _fail))):
                code = self_test.main(["x", self_test.REPORT_FLAG, str(report)])
            text = report.read_text(encoding="utf-8")
            self.assertEqual(code, 1)
            self.assertIn("PASS ok", text)
            self.assertIn("FAIL bad: RuntimeError: broken", text)


def _fail() -> None:
    raise RuntimeError("broken")


if __name__ == "__main__":
    unittest.main()

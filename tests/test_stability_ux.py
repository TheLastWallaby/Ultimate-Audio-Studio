"""Regression tests for the stability / UX improvements (auto-level, updates, exports, library safety)."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.controllers.library_controller import UNDO_DIR_NAME, LibraryController
from app.controllers.playback_controller import PlaybackController
from app.controllers.playlist_controller import PlaylistController
from app.core.cache_manager import CacheManager
from app.core.errors import friendly_error
from app.core.process_utils import ProcessResult
from app.core.waveform import measure_loudness_db


class TestAutoLevel(unittest.TestCase):
    def test_loudness_is_not_normalized(self) -> None:
        """Quiet and loud material must measure differently (the old peak-based gain was always 0.7)."""
        quiet = [int(0.05 * 32767 * ((-1) ** i)) for i in range(8000)]
        loud = [int(0.9 * 32767 * ((-1) ** i)) for i in range(8000)]
        q_db = measure_loudness_db(quiet, 1600)
        l_db = measure_loudness_db(loud, 1600)
        assert q_db is not None and l_db is not None
        self.assertAlmostEqual(q_db, -26.0, delta=0.5)
        self.assertAlmostEqual(l_db, -0.9, delta=0.5)

    def test_silence_has_no_loudness(self) -> None:
        self.assertIsNone(measure_loudness_db([0] * 8000, 1600))
        self.assertIsNone(measure_loudness_db([], 1600))

    def test_gain_moves_toward_target_and_is_clamped(self) -> None:
        pb = PlaybackController(None, MagicMock())
        self.assertGreater(pb.compute_auto_level_gain(-30.0), 1.0)
        self.assertLess(pb.compute_auto_level_gain(-6.0), 1.0)
        self.assertAlmostEqual(pb.compute_auto_level_gain(-16.0), 1.0)
        self.assertAlmostEqual(pb.compute_auto_level_gain(-80.0), 10 ** (6 / 20))
        self.assertAlmostEqual(pb.compute_auto_level_gain(20.0), 10 ** (-12 / 20))
        self.assertEqual(pb.compute_auto_level_gain(None), 1.0)


class TestPauseResumeState(unittest.TestCase):
    def test_scrub_while_paused_resumes_at_new_position(self) -> None:
        engine = MagicMock()
        engine.play_start_offset = 12.0
        pb = PlaybackController(None, engine)
        pb.is_playing_main = True
        pb.pause()
        self.assertEqual(pb.paused_position, 12.0)
        pb.seek(30.0, track_duration=60.0)
        self.assertTrue(pb._scrubbed_while_paused)
        pb.unpause("song.mp3")
        engine.load_and_play.assert_called_with("song.mp3", 30.0)

    def test_borrowed_mixer_forces_reload_at_paused_position(self) -> None:
        engine = MagicMock()
        engine.play_start_offset = 42.0
        pb = PlaybackController(None, engine)
        pb.is_playing_main = True
        pb.pause()
        engine.play_start_offset = 3.0  # a search preview reused the shared clock
        pb.mark_mixer_taken()
        pb.unpause("song.mp3")
        engine.unpause.assert_not_called()
        engine.load_and_play.assert_called_with("song.mp3", 42.0)


class TestFriendlyErrors(unittest.TestCase):
    def test_known_errors_map_to_plain_language(self) -> None:
        cases = {
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access": "Private Video",
            "Sign in to confirm your age. This video may be inappropriate": "Age-Restricted Video",
            "Sign in to confirm you're not a bot": "YouTube Is Blocking Downloads",
            "<urlopen error [Errno 11001] getaddrinfo failed>": "No Internet Connection",
            "[Errno 28] No space left on device": "Disk Is Full",
            "[WinError 32] The process cannot access the file because it is being used by another process": "File Is In Use",
            "FFmpeg execution timed out after 60 seconds.": "Audio Tool Problem",
        }
        for raw, title in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(friendly_error(raw, "download").title, title)

    def test_unknown_error_uses_context_default(self) -> None:
        self.assertEqual(friendly_error("weird", "export").title, "Export Failed")
        self.assertEqual(friendly_error("weird", "search").title, "Search Failed")


class TestCacheManager(unittest.TestCase):
    def test_loudness_survives_metadata_updates_and_flush(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            song = os.path.join(td, "song.mp3")
            Path(song).write_bytes(b"ID3" + b"\x00" * 100)
            cm = CacheManager(cache_dir=td)
            cm.set_loudness(song, -14.5)
            cm.set_metadata(song, {"title": "T", "artist": "A", "duration": 10.0})
            self.assertEqual(cm.get_loudness(song), -14.5)
            cm.flush()
            self.assertEqual(CacheManager(cache_dir=td).get_loudness(song), -14.5)

    def test_setters_do_not_write_on_calling_thread(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            song = os.path.join(td, "song.mp3")
            Path(song).write_bytes(b"x" * 10)
            cm = CacheManager(cache_dir=td)
            with patch("app.core.cache_manager.atomic_save_json") as save:
                cm.set_duration(song, 5.0)
                save.assert_not_called()
                cm.flush()
                save.assert_called_once()

    def test_metadata_cap_is_larger_than_peaks_cap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cm = CacheManager(cache_dir=td, max_mem_entries=2)
            self.assertGreater(cm.max_meta_entries, 1024)


class TestLibrarySafety(unittest.TestCase):
    def test_trash_restores_original_location_first(self) -> None:
        """Recycle Bin must receive the song under its real name at its real location."""
        with tempfile.TemporaryDirectory() as td:
            song = os.path.join(td, "My Song.mp3")
            Path(song).write_text("audio")
            lib = LibraryController(None)
            lib.stage_delete_with_undo(song, {})
            self.assertFalse(os.path.exists(song))
            trashed: list[str] = []
            with patch(
                "app.controllers.library_controller._send2trash",
                side_effect=lambda p: (trashed.append(p), os.remove(p)),
            ):
                lib.flush_pending_trash()
            self.assertEqual(trashed, [song])
            self.assertEqual(os.listdir(os.path.join(td, UNDO_DIR_NAME)), [])

    def test_stranded_deletes_are_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            stranded_dir = Path(td) / UNDO_DIR_NAME / "abc123"
            stranded_dir.mkdir(parents=True)
            (stranded_dir / "Crash Song.mp3").write_text("x")
            (Path(td) / UNDO_DIR_NAME / "Legacy.mp3.undo").write_text("y")
            trashed: list[str] = []
            with patch(
                "app.controllers.library_controller._send2trash",
                side_effect=lambda p: (trashed.append(p), os.remove(p)),
            ):
                self.assertEqual(LibraryController.recover_stranded_deletes(td), 2)
            self.assertCountEqual(trashed, [os.path.join(td, "Crash Song.mp3"), os.path.join(td, "Legacy.mp3")])
            self.assertFalse((Path(td) / UNDO_DIR_NAME).exists())


class TestPlaylists(unittest.TestCase):
    def test_relink_missing_by_file_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            Path(td, "Moved Song.mp3").write_text("x")
            pc = PlaylistController(None)
            pc.playlists = {"Mix": [r"C:\old_library_gone\Moved Song.mp3", r"C:\old_library_gone\Gone.mp3"]}
            self.assertEqual(pc.relink_missing(td), 1)
            self.assertEqual(pc.playlists["Mix"][0], os.path.join(td, "Moved Song.mp3"))
            self.assertEqual(pc.playlists["Mix"][1], r"C:\old_library_gone\Gone.mp3")

    def test_previous_does_not_wrap_unless_repeat(self) -> None:
        pc = PlaylistController(None)
        pc.playlists = {"P": ["a", "b", "c"]}
        self.assertEqual(pc.get_prev_index("P", 2), 1)
        self.assertEqual(pc.get_prev_index("P", 0), 0)
        self.assertEqual(pc.get_prev_index("P", 0, repeat=True), 2)

    def test_malformed_playlist_file_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pl.json")
            Path(path).write_text('{"Good": ["a.mp3", 5, null], "Bad": "not a list"}', encoding="utf-8")
            pc = PlaylistController(None)
            pc.load(path)
            self.assertEqual(pc.playlists["Good"], ["a.mp3"])
            self.assertNotIn("Bad", pc.playlists)


class TestTwoPassLoudnorm(unittest.TestCase):
    STDERR = """[Parsed_loudnorm_0 @ 000001]
{
	"input_i" : "-23.50",
	"input_tp" : "-4.20",
	"input_lra" : "6.10",
	"input_thresh" : "-33.90",
	"output_i" : "-16.02",
	"output_tp" : "-1.50",
	"output_lra" : "5.00",
	"output_thresh" : "-26.40",
	"normalization_type" : "dynamic",
	"target_offset" : "0.02"
}"""

    def test_measure_parses_and_caches(self) -> None:
        from app.services import exporter

        with tempfile.TemporaryDirectory() as td:
            song = os.path.join(td, "s.mp3")
            Path(song).write_bytes(b"x" * 50)
            cm = CacheManager(cache_dir=td)
            with (
                patch.object(exporter, "cache_mgr", cm),
                patch.object(exporter, "run_ffmpeg", return_value=ProcessResult(0, "", self.STDERR)) as run,
            ):
                stats = exporter.measure_loudnorm(song)
                self.assertEqual(stats["input_i"], "-23.50")
                exporter.measure_loudnorm(song)
                run.assert_called_once()  # second call served from cache
            flt = exporter.loudnorm_filter(stats)
            self.assertIn("measured_I=-23.50", flt)
            self.assertIn("linear=true", flt)
            self.assertFalse(exporter.is_already_level(stats))

    def test_already_level_tracks_are_copied(self) -> None:
        from app.services import exporter

        self.assertTrue(exporter.is_already_level({"input_i": "-16.4", "input_tp": "-1.8"}))
        self.assertFalse(exporter.is_already_level({"input_i": "-16.4", "input_tp": "0.5"}))
        self.assertEqual(exporter.loudnorm_filter(None), "loudnorm=I=-16:TP=-1.5:LRA=11")


class TestDownloadResult(unittest.TestCase):
    def test_existing_mp3_is_not_reported_as_new_download(self) -> None:
        """A failed conversion must not 'succeed' with whatever MP3 was already in the folder."""
        from app.services import downloader

        with tempfile.TemporaryDirectory() as td:
            Path(td, "Old Song.mp3").write_text("x")
            ydl = MagicMock()
            ydl.__enter__.return_value = ydl
            ydl.extract_info.return_value = {"requested_downloads": [{"filepath": os.path.join(td, "New.webm")}]}
            success, errors = MagicMock(), MagicMock()
            with patch.object(downloader.yt_dlp, "YoutubeDL", return_value=ydl):
                downloader.download_audio_worker(
                    "https://youtu.be/x", td, threading.Event(), MagicMock(), success, MagicMock(), errors
                )
            success.assert_not_called()
            errors.assert_called_once()


class TestUpdaterIntegrity(unittest.TestCase):
    def _fake_response(self, payload: bytes) -> MagicMock:
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.headers = {"Content-Length": str(len(payload))}
        resp.read.side_effect = io.BytesIO(payload).read
        return resp

    def test_checksum_mismatch_is_rejected(self) -> None:
        from app.services import updater

        payload = b"MZ" + b"\x00" * (2 * 1024 * 1024)
        opener = MagicMock()
        opener.open.return_value = self._fake_response(payload)
        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(updater.urllib.request, "build_opener", return_value=opener),
        ):
            dest = os.path.join(td, "update.exe")
            ok, msg = updater.download_release_asset(1, token="", dest_path=dest, expected_digest="sha256:" + "0" * 64)
            self.assertFalse(ok)
            self.assertIn("checksum", msg)
            self.assertFalse(os.path.exists(dest))

    def test_matching_checksum_is_accepted(self) -> None:
        from app.services import updater

        payload = b"MZ" + b"\x00" * (2 * 1024 * 1024)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        opener = MagicMock()
        opener.open.return_value = self._fake_response(payload)
        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(updater.urllib.request, "build_opener", return_value=opener),
        ):
            dest = os.path.join(td, "update.exe")
            ok, result = updater.download_release_asset(1, token="", dest_path=dest, expected_digest=digest)
            self.assertTrue(ok)
            self.assertEqual(result, dest)

    def test_release_digest_is_captured(self) -> None:
        from app.services import updater

        api = {
            "tag_name": "v99.0.0",
            "assets": [{"name": "Ultimate Audio Studio.exe", "id": 7, "size": 5, "digest": "sha256:" + "a" * 64}],
        }
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = __import__("json").dumps(api).encode()
        with patch.object(updater.urllib.request, "urlopen", return_value=resp):
            has_update, info = updater.check_latest_release(current_ver="1.0.0", token="")
        self.assertTrue(has_update)
        self.assertEqual(info.asset_digest, "sha256:" + "a" * 64)


if __name__ == "__main__":
    unittest.main()

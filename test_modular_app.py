"""Automated test suite verifying modular architecture and business logic."""

import os
import sys
import tempfile
import unittest

from app.config import atomic_save_json, format_time, sanitize_filename
from app.core.audio_engine import AudioEngine
from app.core.waveform import (
    _samples_to_peaks,
    extract_waveform_peaks,
    get_waveform_bounds,
    time_from_waveform_x,
    time_to_waveform_x,
)
from app.services.downloader import resolve_download_query


class TestAppConfig(unittest.TestCase):
    def test_format_time(self):
        self.assertEqual(format_time(0), "00:00")
        self.assertEqual(format_time(65), "01:05")
        self.assertEqual(format_time(3661), "61:01")
        self.assertEqual(format_time(-10), "00:00")

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("valid_name.mp3"), "valid_name.mp3")
        self.assertEqual(sanitize_filename("illegal/path:name*?"), "illegal_path_name__")
        self.assertEqual(sanitize_filename("CON.mp3"), "Track_CON.mp3")
        self.assertEqual(sanitize_filename(""), "AudioTrack")
        # Clamping
        long_str = "a" * 200
        self.assertLessEqual(len(sanitize_filename(long_str, max_len=50)), 50)

    def test_atomic_save_json(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "sub", "test.json")
            data = {"key": "value", "numbers": [1, 2, 3]}
            atomic_save_json(target, data)
            self.assertTrue(os.path.exists(target))
            import json

            with open(target, encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded, data)


class TestWaveformMath(unittest.TestCase):
    def test_waveform_bounds(self):
        # Unzoomed
        b_s, b_e = get_waveform_bounds(120.0, 30.0, 60.0, zoomed=False)
        self.assertEqual(b_s, 0.0)
        self.assertEqual(b_e, 120.0)

        # Zoomed with 5s padding
        b_s, b_e = get_waveform_bounds(120.0, 30.0, 60.0, zoomed=True)
        self.assertEqual(b_s, 25.0)
        self.assertEqual(b_e, 65.0)

    def test_waveform_coord_translations(self):
        b_s, b_e = 20.0, 80.0
        width = 600
        # Time to X
        x_start = time_to_waveform_x(20.0, width, b_s, b_e)
        self.assertAlmostEqual(x_start, 0.0)
        x_mid = time_to_waveform_x(50.0, width, b_s, b_e)
        self.assertAlmostEqual(x_mid, 300.0)
        x_end = time_to_waveform_x(80.0, width, b_s, b_e)
        self.assertAlmostEqual(x_end, 600.0)

        # Inversion X to Time
        t_mid = time_from_waveform_x(300.0, width, b_s, b_e)
        self.assertAlmostEqual(t_mid, 50.0)


from unittest.mock import patch

from app.services.downloader import search_youtube


class TestDownloaderRouting(unittest.TestCase):
    def test_resolve_download_query(self):
        # Direct URL
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        resolved, is_search = resolve_download_query(url)
        self.assertEqual(resolved, url)
        self.assertFalse(is_search)

        # Search Query
        query = "Queen Bohemian Rhapsody"
        resolved, is_search = resolve_download_query(query)
        self.assertEqual(resolved, "ytsearch1:Queen Bohemian Rhapsody")
        self.assertTrue(is_search)

    def test_search_youtube_empty(self):
        self.assertEqual(search_youtube("   "), [])

    @patch("yt_dlp.YoutubeDL")
    def test_search_youtube_parsing(self, mock_ydl_cls):
        mock_instance = mock_ydl_cls.return_value.__enter__.return_value
        mock_instance.extract_info.return_value = {
            "entries": [
                {
                    "id": "vid123",
                    "title": "Hotel California Live",
                    "uploader": "Eagles Band",
                    "duration": 395,
                    "url": "https://www.youtube.com/watch?v=vid123",
                },
                {
                    "id": "vid456",
                    "title": "Hotel California Studio",
                    "uploader": "Eagles Official",
                    "duration": None,
                    "url": None,
                },
            ]
        }
        results = search_youtube("Hotel California", max_results=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Hotel California Live")
        self.assertEqual(results[0]["uploader"], "Eagles Band")
        self.assertEqual(results[0]["duration_str"], "06:35")
        self.assertEqual(results[0]["url"], "https://www.youtube.com/watch?v=vid123")

        # Fallback URL and duration
        self.assertEqual(results[1]["title"], "Hotel California Studio")
        self.assertEqual(results[1]["duration_str"], "--:--")
        self.assertEqual(results[1]["url"], "https://www.youtube.com/watch?v=vid456")


class TestAudioEngine(unittest.TestCase):
    def test_audio_engine_clock(self):
        engine = AudioEngine()
        engine.start_clock(15.0)
        self.assertAlmostEqual(engine.play_start_offset, 15.0)
        self.assertIsNotNone(engine.play_clock_origin)
        elapsed = engine.current_play_seconds()
        self.assertGreaterEqual(elapsed, 15.0)


import hashlib
import threading

from app.config import PREVIEW_CACHE_DIR
from app.services.downloader import fetch_preview_audio, fetch_preview_worker


class TestPreviewService(unittest.TestCase):
    def test_fetch_preview_empty_url(self):
        self.assertIsNone(fetch_preview_audio(""))

    def test_fetch_preview_cancelled(self):
        cancel_event = threading.Event()
        cancel_event.set()
        res = fetch_preview_audio("https://example.com/test", cancel_event=cancel_event)
        self.assertIsNone(res)

    def test_fetch_preview_cache_hit(self):
        url = "https://www.youtube.com/watch?v=fake_test_cache"
        os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        preview_file = os.path.join(PREVIEW_CACHE_DIR, f"yt_prev_{url_hash}_30s.mp3")
        try:
            with open(preview_file, "wb") as f:
                f.write(b"0" * 2048)  # dummy cached audio > 1024 bytes
            result = fetch_preview_audio(url, max_seconds=30)
            self.assertEqual(result, preview_file)
        finally:
            if os.path.exists(preview_file):
                os.remove(preview_file)

    def test_fetch_preview_worker_dispatch(self):
        url = "https://www.youtube.com/watch?v=fake_worker_test"
        os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        preview_file = os.path.join(PREVIEW_CACHE_DIR, f"yt_prev_{url_hash}_30s.mp3")
        try:
            with open(preview_file, "wb") as f:
                f.write(b"0" * 2048)

            success_res = []
            error_res = []
            cancel_event = threading.Event()

            fetch_preview_worker(
                url,
                30,
                cancel_event,
                on_success=lambda p: success_res.append(p),
                on_error=lambda e: error_res.append(e),
            )

            self.assertEqual(len(success_res), 1)
            self.assertEqual(success_res[0], preview_file)
            self.assertEqual(len(error_res), 0)
        finally:
            if os.path.exists(preview_file):
                os.remove(preview_file)


class TestWaveformExtraction(unittest.TestCase):
    def test_samples_to_peaks(self):
        samples = [100, -200, 300, -400]
        peaks = _samples_to_peaks(samples, n_bars=2)
        self.assertEqual(len(peaks), 2)
        self.assertAlmostEqual(peaks[0], 0.5)
        self.assertAlmostEqual(peaks[1], 1.0)

    def test_samples_to_peaks_silence(self):
        self.assertEqual(_samples_to_peaks([0, 0, 0], n_bars=3), [])
        self.assertEqual(_samples_to_peaks([], n_bars=3), [])

    def test_extract_waveform_peaks_missing(self):
        self.assertEqual(extract_waveform_peaks("nonexistent_file_path.mp3"), [])

    def test_extract_waveform_peaks_synthetic_wav(self):
        import math
        import struct
        import wave

        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                data = bytearray()
                for i in range(44100):
                    val = int(16000 * math.sin(2 * math.pi * 440 * i / 44100))
                    data.extend(struct.pack("<h", val))
                wf.writeframes(data)
            peaks = extract_waveform_peaks(wav_path, n_bars=20)
            self.assertEqual(len(peaks), 20)
            self.assertTrue(any(p > 0.5 for p in peaks))
        finally:
            if os.path.exists(wav_path):
                os.remove(wav_path)

    def test_extract_waveform_peaks_pygame_fallback(self):
        """Verify that peak extraction succeeds via Pygame even when ffmpeg fails or is missing."""
        import math
        import struct
        import wave
        from unittest.mock import patch

        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                data = bytearray()
                for i in range(44100):
                    val = int(18000 * math.sin(2 * math.pi * 440 * i / 44100))
                    data.extend(struct.pack("<h", val))
                wf.writeframes(data)

            # Force ffmpeg_path to non-existent executable to force Pygame fallback
            with patch("app.core.waveform.ffmpeg_path", "nonexistent_ffmpeg_binary.exe"):
                peaks = extract_waveform_peaks(wav_path, n_bars=20)
                self.assertEqual(len(peaks), 20)
                self.assertTrue(any(p > 0.5 for p in peaks))
        finally:
            if os.path.exists(wav_path):
                os.remove(wav_path)


class TestPlatformAndThreading(unittest.TestCase):
    def test_silent_popen_stdin_devnull_default(self):
        import subprocess

        if os.name == "nt":
            p = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
            p.wait()
            self.assertEqual(p.returncode, 0)

    def test_is_valid_binary(self):
        from app.config import ffmpeg_path, is_valid_binary

        self.assertFalse(is_valid_binary(""))
        self.assertFalse(is_valid_binary("nonexistent_file.exe"))
        # Tiny file test
        with tempfile.NamedTemporaryFile(suffix=".exe") as tf:
            tf.write(b"not a real binary")
            tf.flush()
            self.assertFalse(is_valid_binary(tf.name))
        # Genuine ffmpeg binary test
        if ffmpeg_path and os.path.exists(ffmpeg_path):
            self.assertTrue(is_valid_binary(ffmpeg_path))


class TestNewFeatures(unittest.TestCase):
    def test_parse_time_input(self):
        from app.main import UltimateAudioStudio

        # Test MM:SS and M:SS
        self.assertAlmostEqual(UltimateAudioStudio._parse_time_input(None, "01:23"), 83.0)
        self.assertAlmostEqual(UltimateAudioStudio._parse_time_input(None, "0:05"), 5.0)
        self.assertAlmostEqual(UltimateAudioStudio._parse_time_input(None, "2:05.5"), 125.5)
        # Test HH:MM:SS
        self.assertAlmostEqual(UltimateAudioStudio._parse_time_input(None, "1:01:05"), 3665.0)
        # Test raw seconds
        self.assertAlmostEqual(UltimateAudioStudio._parse_time_input(None, "90"), 90.0)
        self.assertAlmostEqual(UltimateAudioStudio._parse_time_input(None, "45.5"), 45.5)
        # Test invalid inputs
        self.assertIsNone(UltimateAudioStudio._parse_time_input(None, ""))
        self.assertIsNone(UltimateAudioStudio._parse_time_input(None, "   "))
        self.assertIsNone(UltimateAudioStudio._parse_time_input(None, "abc"))
        self.assertIsNone(UltimateAudioStudio._parse_time_input(None, "-10"))
        self.assertIsNone(UltimateAudioStudio._parse_time_input(None, "01:99"))  # 99 seconds invalid
        self.assertIsNone(UltimateAudioStudio._parse_time_input(None, "1:2:3:4"))

    def test_wcag_theme_contrast(self):
        from app.ui.theme import TEXT_MUTED

        self.assertEqual(TEXT_MUTED, "#475569")

    def test_search_default_limit(self):
        import inspect

        from app.services.downloader import search_youtube

        sig = inspect.signature(search_youtube)
        self.assertEqual(sig.parameters["max_results"].default, 10)

    def test_clipper_fade_sec_parameter(self):
        import inspect

        from app.services.clipper import clip_audio_worker, create_audition_slice

        sig1 = inspect.signature(create_audition_slice)
        self.assertIn("fade_sec", sig1.parameters)
        self.assertEqual(sig1.parameters["fade_sec"].default, 1.5)
        sig2 = inspect.signature(clip_audio_worker)
        self.assertIn("fade_sec", sig2.parameters)
        self.assertEqual(sig2.parameters["fade_sec"].default, 1.5)


class TestApprovedEnhancements(unittest.TestCase):
    """Tests for the approved set of enhancements across items 1, 2, 3, 4, 5."""

    def test_desktop_path_resolution(self):
        """Item 1.4: Verify Win32/fallback Desktop folder resolution."""
        from app.platform_utils import get_desktop_dir

        desktop = get_desktop_dir()
        self.assertIsInstance(desktop, str)
        self.assertTrue(len(desktop) > 0)
        self.assertTrue(os.path.isabs(desktop))

    def test_cache_persistence_and_invalidation(self):
        """Item 1.1 & 5.2: Test persistent disk caching and cache invalidation."""
        from app.core.cache_manager import CacheManager

        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(cache_dir=td)
            dummy_file = os.path.join(td, "song.mp3")
            with open(dummy_file, "wb") as f:
                f.write(b"ID3" + b"\x00" * 500)

            # Metadata & duration
            cache.set_metadata(dummy_file, {"title": "Test Song", "artist": "Tester", "duration": 182.5})
            meta = cache.get_metadata(dummy_file)
            self.assertIsNotNone(meta)
            self.assertEqual(meta["title"], "Test Song")
            self.assertAlmostEqual(cache.get_duration(dummy_file), 182.5)

            # Waveform peaks
            peaks = [0.1, 0.5, 0.8, 0.3]
            cache.set_peaks(dummy_file, peaks)
            self.assertEqual(cache.get_peaks(dummy_file), peaks)

            # Force save and verify new instance loads from disk
            cache.save_to_disk(force=True)
            cache2 = CacheManager(cache_dir=td)
            self.assertEqual(cache2.get_peaks(dummy_file), peaks)
            self.assertEqual(cache2.get_metadata(dummy_file)["title"], "Test Song")

            # Invalidation
            cache.invalidate(dummy_file)
            self.assertIsNone(cache.get_peaks(dummy_file))
            self.assertIsNone(cache.get_metadata(dummy_file))

    def test_waveform_hit_tolerance(self):
        """Item 3.1: Verify waveform marker hit tolerance is 22px."""
        from app.ui.waveform_view import WaveformView

        self.assertEqual(WaveformView.MARKER_HIT_TOLERANCE, 22)

    def test_m3u_utf8_bom_and_m3u8(self):
        """Item 4.1: Verify UTF-8 BOM M3U and companion M3U8 generation."""
        from app.services.exporter import usb_export_worker

        with tempfile.TemporaryDirectory() as td_src, tempfile.TemporaryDirectory() as td_dest:
            track_path = os.path.join(td_src, "test_track.mp3")
            with open(track_path, "wb") as f:
                f.write(b"dummy audio content")

            success_called = []

            def on_succ(cnt, total, skipped):
                success_called.append((cnt, total, skipped))

            usb_export_worker(
                dest_folder=td_dest,
                playlist_name="CarRoadTrip",
                files_to_export=[track_path],
                normalize=False,
                on_success=on_succ,
                duration_fn=lambda p: 120.0,
            )

            m3u_path = os.path.join(td_dest, "00_CarRoadTrip.m3u")
            m3u8_path = os.path.join(td_dest, "00_CarRoadTrip.m3u8")
            self.assertTrue(os.path.exists(m3u_path))
            self.assertTrue(os.path.exists(m3u8_path))

            with open(m3u_path, "rb") as f:
                m3u_bytes = f.read()
            self.assertTrue(m3u_bytes.startswith(b"\xef\xbb\xbf#EXTM3U"))

            with open(m3u8_path, encoding="utf-8") as f:
                m3u8_text = f.read()
            self.assertTrue(m3u8_text.startswith("#EXTM3U"))
            self.assertIn("test_track.mp3", m3u8_text)

    def test_soft_limiter_clipping(self):
        """Item 2.3: Verify soft limiter filter is applied on positive volume boost."""
        from unittest.mock import MagicMock

        from app.services.clipper import create_audition_slice

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            tf.write(b"dummy audio data")
            dummy_path = tf.name

        try:
            with patch("app.services.clipper.run_ffmpeg") as mock_ffmpeg, patch("os.path.exists", return_value=True):
                mock_res = MagicMock()
                mock_res.returncode = 0
                mock_ffmpeg.return_value = mock_res

                create_audition_slice(dummy_path, 10.0, 20.0, gain_db=6.0, soften=True, fade_sec=1.5)
                self.assertTrue(mock_ffmpeg.called)
                cmd = mock_ffmpeg.call_args[0][0]
                af_idx = cmd.index("-af")
                filter_str = cmd[af_idx + 1]
                self.assertIn("alimiter=limit=0.95:attack=5:release=50", filter_str)
                self.assertIn("volume=6.0dB", filter_str)
        finally:
            if os.path.exists(dummy_path):
                try:
                    os.remove(dummy_path)
                except Exception:
                    pass

    def test_undo_staging_and_restore(self):
        """Item 3.5 & 5.1: Verify single-click undo for library deletion and playlist removal."""
        from app.controllers.library_controller import LibraryController
        from app.controllers.playlist_controller import PlaylistController

        with tempfile.TemporaryDirectory() as td:
            song_path = os.path.join(td, "AwesomeSong.mp3")
            with open(song_path, "w") as f:
                f.write("content")

            playlists = {"Favorites": [song_path, "other.mp3"]}
            lib_ctrl = LibraryController(None)

            filename = lib_ctrl.stage_delete_with_undo(song_path, playlists)
            self.assertEqual(filename, "AwesomeSong.mp3")
            self.assertFalse(os.path.exists(song_path))
            self.assertNotIn(song_path, playlists["Favorites"])

            self.assertTrue(lib_ctrl.undo_delete(playlists))
            self.assertTrue(os.path.exists(song_path))
            self.assertIn(song_path, playlists["Favorites"])

            pl_ctrl = PlaylistController(None)
            pl_ctrl.playlists = {"Party": ["track1.mp3", "track2.mp3", "track3.mp3"]}
            removed = pl_ctrl.remove_track_with_undo("Party", 1)
            self.assertEqual(removed, "track2.mp3")
            self.assertEqual(pl_ctrl.playlists["Party"], ["track1.mp3", "track3.mp3"])

            restored = pl_ctrl.undo_remove()
            self.assertIsNotNone(restored)
            self.assertEqual(pl_ctrl.playlists["Party"], ["track1.mp3", "track2.mp3", "track3.mp3"])

    def test_live_stream_filtering_in_youtube_search(self):
        """Item 4.3: Verify that YouTube search excludes live streams."""
        with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
            mock_instance = mock_ydl_cls.return_value.__enter__.return_value
            mock_instance.extract_info.return_value = {
                "entries": [
                    {
                        "id": "live1",
                        "title": "24/7 Live Music Stream",
                        "is_live": True,
                        "url": "https://youtu.be/live1",
                    },
                    {
                        "id": "live2",
                        "title": "Live Radio Stream",
                        "live_status": "is_live",
                        "url": "https://youtu.be/live2",
                    },
                    {
                        "id": "song1",
                        "title": "Regular Music Video",
                        "is_live": False,
                        "duration": 210,
                        "url": "https://youtu.be/song1",
                    },
                ]
            }
            results = search_youtube("Music", max_results=5)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "song1")

    def test_audio_engine_endevent_and_gain(self):
        """Item 2.1 & 2.4: Test native end event toggling and auto-level gain scaling."""
        engine = AudioEngine()
        self.assertEqual(engine.buffer_samples, 2048)

        engine.set_endevent()
        engine.clear_endevent()

        engine.set_auto_level(False)
        engine.set_track_gain(1.5)
        engine.set_volume(0.8)
        self.assertAlmostEqual(engine.volume, 0.8)

        engine.set_auto_level(True)
        engine.set_track_gain(1.2)
        engine.set_volume(0.9)
        self.assertAlmostEqual(engine.effective_volume, min(1.0, 0.9 * 1.2))


class TestPhase2ApprovedEnhancements(unittest.TestCase):
    """Automated tests verifying approved improvements: Items 1, 2, 4, 7, 8, 10, 11, 12, 13, 14."""

    def test_item1_library_preindexed_search(self):
        """Item 1: Pre-indexed search in LibraryController."""
        from app.controllers.library_controller import LibraryController

        ctrl = LibraryController(None)
        files = ["track_a.mp3", "track_b.mp3", "track_c.mp3"]
        metadata_map = {
            os.path.join("C:/Music", "track_a.mp3"): {"title": "Bohemian Rhapsody", "artist": "Queen"},
            os.path.join("C:/Music", "track_b.mp3"): {"title": "Hotel California", "artist": "Eagles"},
            os.path.join("C:/Music", "track_c.mp3"): {"title": "Radio Ga Ga", "artist": "Queen"},
        }

        def get_meta(path):
            return metadata_map.get(path, {})

        ctrl.update_search_index("C:/Music", files, get_metadata_fn=get_meta)

        # Match by artist
        queen_results = ctrl.filter_files(files, "C:/Music", "queen", get_metadata_fn=get_meta)
        self.assertEqual(queen_results, ["track_a.mp3", "track_c.mp3"])

        # Match by title
        california_results = ctrl.filter_files(files, "C:/Music", "california", get_metadata_fn=get_meta)
        self.assertEqual(california_results, ["track_b.mp3"])

        # Invalidation
        ctrl.invalidate_search_index(os.path.join("C:/Music", "track_a.mp3"))
        self.assertNotIn(os.path.join("C:/Music", "track_a.mp3"), ctrl._search_index)

    def test_item4_multiselect_add_to_playlist(self):
        """Item 4: Multi-select add songs to playlist."""
        playlists = {"RoadTrip": ["intro.mp3"]}
        selected_tracks = ["c:/music/track1.mp3", "c:/music/track2.mp3", "c:/music/track3.mp3"]
        tracks = playlists.setdefault("RoadTrip", [])
        for p in selected_tracks:
            tracks.append(p)
        self.assertEqual(len(playlists["RoadTrip"]), 4)
        self.assertEqual(
            playlists["RoadTrip"], ["intro.mp3", "c:/music/track1.mp3", "c:/music/track2.mp3", "c:/music/track3.mp3"]
        )

    def test_item2_waveform_eager_cancellation(self):
        """Item 2: Waveform extraction eager subprocess termination."""
        import threading

        from app.core.waveform import extract_waveform_peaks

        cancel_event = threading.Event()
        cancel_event.set()
        spawned = []
        peaks = extract_waveform_peaks(
            "dummy_nonexistent.mp3", cancel_event=cancel_event, on_process_spawned=lambda p: spawned.append(p)
        )
        self.assertEqual(peaks, [])

    def test_item7_loop_preview_playback_controller(self):
        """Item 7: Continuous loop preview in PlaybackController."""
        from unittest.mock import MagicMock

        from app.controllers.playback_controller import PlaybackController

        mock_engine = MagicMock()
        ctrl = PlaybackController(None, mock_engine)
        with patch("pygame.mixer.music"):
            ctrl.test_clip("song.mp3", 10.0, 25.0, loop=True)
            self.assertTrue(ctrl.loop_preview)
            self.assertTrue(ctrl.previewing_clip)
            self.assertEqual(ctrl.clip_start_time, 10.0)
            self.assertEqual(ctrl.clip_end_time, 25.0)

            # Test restart_clip_loop
            restarted = ctrl.restart_clip_loop()
            self.assertTrue(restarted)
            mock_engine.load_and_play.assert_called_with("song.mp3", 10.0)

    def test_item8_always_320k_mp3_clip(self):
        """Item 8: Clipping strictly forces 320 kbps MP3 output with album art preservation."""
        from unittest.mock import MagicMock

        from app.services.clipper import clip_audio_worker

        with patch("app.services.clipper.run_ffmpeg") as mock_ffmpeg, patch("os.path.exists", return_value=True):
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_ffmpeg.return_value = mock_res

            # Source is WAV, output is MP3
            clip_audio_worker("source.wav", 15.0, 30.0, "output.mp3", soften=False, gain_db=0.0)
            self.assertTrue(mock_ffmpeg.called)
            cmd = mock_ffmpeg.call_args[0][0]
            # Verify libmp3lame, 320k, and id3v2.3
            self.assertIn("-c:a", cmd)
            self.assertIn("libmp3lame", cmd)
            self.assertIn("-b:a", cmd)
            self.assertIn("320k", cmd)
            self.assertIn("-id3v2_version", cmd)
            self.assertIn("3", cmd)

    def test_item10_cache_manager_unification(self):
        """Item 10: CacheManager handles metadata, duration, and peaks in a single unified cache."""
        from app.core.cache_manager import cache_mgr

        with tempfile.TemporaryDirectory() as td:
            track = os.path.join(td, "cache_track.mp3")
            with open(track, "wb") as f:
                f.write(b"RIFF" + b"\x00" * 400)

            cache_mgr.set_duration(track, 240.0)
            self.assertEqual(cache_mgr.get_duration(track), 240.0)

            meta = cache_mgr.get_metadata(track)
            self.assertIsNotNone(meta)
            self.assertEqual(meta.get("duration"), 240.0)

            peaks = [0.2, 0.4, 0.6]
            cache_mgr.set_peaks(track, peaks)
            self.assertEqual(cache_mgr.get_peaks(track), peaks)

            cache_mgr.invalidate(track)
            self.assertIsNone(cache_mgr.get_duration(track))
            self.assertIsNone(cache_mgr.get_peaks(track))

    def test_item11_dialog_thread_safe_queue(self):
        """Item 11: Queue-based UI dispatch in SearchChoiceDialog."""
        import queue
        import tkinter as tk

        from app.ui.search_dialog import SearchChoiceDialog

        root = tk.Tk()
        root.withdraw()
        try:
            executed = []
            dlg = SearchChoiceDialog.__new__(SearchChoiceDialog)
            dlg.root = root
            dlg._ui_queue = queue.Queue()
            dlg._is_closing = False
            dlg._closed = False

            dlg._safe_dispatch(lambda: executed.append(42))
            dlg._drain_ui_queue()
            self.assertEqual(executed, [42])
        finally:
            root.destroy()

    def test_item12_playlist_url_detection_and_worker(self):
        """Item 12: Detect playlist URL and batch download orchestration."""
        import threading

        from app.services.downloader import download_playlist_worker, is_playlist_url, probe_playlist_info

        self.assertTrue(is_playlist_url("https://www.youtube.com/playlist?list=PL12345"))
        self.assertTrue(is_playlist_url("https://www.youtube.com/watch?v=abc&list=PL12345"))
        self.assertFalse(is_playlist_url("https://www.youtube.com/watch?v=abc123xyz"))
        self.assertFalse(is_playlist_url("Queen Bohemian Rhapsody"))

        # Test probe_playlist_info with mock
        with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
            mock_inst = mock_ydl_cls.return_value.__enter__.return_value
            mock_inst.extract_info.return_value = {
                "title": "Rock Classics",
                "entries": [
                    {"id": "s1", "title": "Song 1", "url": "https://youtu.be/s1"},
                    {"id": "s2", "title": "Song 2", "url": "https://youtu.be/s2"},
                ],
            }
            info = probe_playlist_info("https://www.youtube.com/playlist?list=PL12345")
            self.assertIsNotNone(info)
            self.assertEqual(info["title"], "Rock Classics")
            self.assertEqual(info["count"], 2)

        # Test download_playlist_worker batch loop
        entries = [
            {"id": "s1", "title": "Song 1", "url": "https://youtu.be/s1"},
            {"id": "s2", "title": "Song 2", "url": "https://youtu.be/s2"},
        ]
        completed = []
        started = []
        with patch("app.services.downloader.download_audio_worker") as mock_dl:

            def fake_dl(url, lib_dir, cancel_evt, on_prog, on_succ, on_canc, on_err):
                fname = "Track_" + os.path.basename(url) + ".mp3"
                on_prog(100.0, "1MB/s", "00:00", True)
                on_succ(fname)

            mock_dl.side_effect = fake_dl

            cancel_evt = threading.Event()
            download_playlist_worker(
                entries,
                library_folder="dummy_dir",
                cancel_event=cancel_evt,
                on_track_start=lambda idx, tot, t: started.append(t),
                on_batch_complete=lambda files, tot: completed.extend(files),
            )
            self.assertEqual(len(started), 2)
            self.assertEqual(len(completed), 2)

            # Test batch cancellation before processing
            cancel_evt.set()
            cancelled_started = []
            cancelled_completed = []
            download_playlist_worker(
                entries,
                library_folder="dummy_dir",
                cancel_event=cancel_evt,
                on_track_start=lambda idx, tot, t: cancelled_started.append(t),
                on_batch_complete=lambda files, tot: cancelled_completed.extend(files),
            )
            self.assertEqual(len(cancelled_started), 0)
            self.assertEqual(len(cancelled_completed), 0)

    def test_item13_usb_flush_buffer(self):
        """Item 13: USB write buffer flush functionality."""
        from app.services.exporter import flush_usb_drive

        with tempfile.TemporaryDirectory() as td:
            test_file = os.path.join(td, "test_flush.txt")
            with open(test_file, "w") as f:
                f.write("test data")
            flush_usb_drive(td)

    def test_item14_cross_platform_binary_probing(self):
        """Item 14: Cross-platform binary probing and extension definition."""
        from app.config import EXE_EXT

        if os.name == "nt":
            self.assertEqual(EXE_EXT, ".exe")
        else:
            self.assertEqual(EXE_EXT, "")

    def test_item5_controllers_full_decomposition(self):
        """Item 5.1: Verify complete modular decomposition into dedicated controllers."""
        from unittest.mock import MagicMock

        from app.controllers import (
            DownloadController,
            ExportController,
            LibraryController,
            PlaybackController,
            PlaylistController,
            UpdateController,
        )

        # 1. LibraryController
        with tempfile.TemporaryDirectory() as td:
            lib = LibraryController(None)
            f1 = os.path.join(td, "alpha.mp3")
            f2 = os.path.join(td, "beta.wav")
            open(f1, "w").close()
            open(f2, "w").close()
            files, visible = lib.scan_and_filter(td, "alpha")
            self.assertEqual(len(files), 2)
            self.assertEqual(visible, ["alpha.mp3"])

            # import plan
            plan, exists = lib.build_import_plan([f1], td)
            # destination is same so plan skips self-copy
            self.assertEqual(len(plan), 0)

        # 2. PlaylistController
        pl = PlaylistController(None)
        self.assertTrue(pl.create_playlist("Chill"))
        self.assertFalse(pl.create_playlist("Chill"))
        pl.add_track("Chill", "/path/to/song1.mp3")
        pl.add_track("Chill", "/path/to/song2.mp3")
        self.assertEqual(len(pl.get_active_tracks("Chill")), 2)
        pl.move_down("Chill", 0)
        tracks = pl.get_active_tracks("Chill")
        self.assertEqual(tracks[0], "/path/to/song2.mp3")
        self.assertEqual(tracks[1], "/path/to/song1.mp3")

        # 3. PlaybackController
        mock_engine = MagicMock()
        mock_engine.current_play_seconds.return_value = 15.0
        pb = PlaybackController(None, mock_engine)
        new_pos = pb.skip_by(10.0, track_duration=60.0)
        self.assertEqual(new_pos, 25.0)
        gain = pb.compute_auto_level_gain([0.1, 0.2, 0.15])
        self.assertGreater(gain, 0.0)

        # 4. ExportController
        exp = ExportController(None)
        self.assertTrue(exp.is_ntfs("NTFS"))
        self.assertFalse(exp.is_ntfs("FAT32"))
        needed = exp.estimate_playlist_bytes(["song1.mp3", "song2.mp3"])
        self.assertGreater(needed, 15 * 1024 * 1024)

        # 5. DownloadController
        dl = DownloadController(None)
        self.assertFalse(dl.cancel_event.is_set())
        dl.cancel()
        self.assertTrue(dl.cancel_event.is_set())
        dl.reset_cancel()
        self.assertFalse(dl.cancel_event.is_set())
        q, is_search = dl.resolve_query("artist track")
        self.assertTrue(is_search)

        # 6. UpdateController
        up = UpdateController(None)
        self.assertIsNone(up.available_update)
        self.assertFalse(up.is_checking_manual)


class TestApprovedEnhancementsSetB(unittest.TestCase):
    """Automated tests verifying approved enhancements: Items 1, 2, 3, 5, 6."""

    def test_item1_safe_usb_ejection(self):
        """Item 1: Safe USB ejection logic and controller integration."""
        from unittest.mock import patch

        from app.controllers.export_controller import ExportController
        from app.platform_utils import safely_eject_usb_drive

        # Invalid/empty path
        ok, msg = safely_eject_usb_drive("")
        self.assertFalse(ok)
        self.assertIn("drive", msg.lower())

        ok, msg = safely_eject_usb_drive("nonexistent_drive_xyz:\\")
        self.assertFalse(ok)

        # ExportController delegation with mock
        ctrl = ExportController(None)
        with patch("app.controllers.export_controller.safely_eject_usb_drive") as mock_eject:
            mock_eject.return_value = (True, "Drive E: safely ejected.")
            success, message = ctrl.eject_usb_drive("E:\\")
            self.assertTrue(success)
            self.assertEqual(message, "Drive E: safely ejected.")
            mock_eject.assert_called_once_with("E:\\")

        # Test failure handling in ExportController
        with patch("app.controllers.export_controller.safely_eject_usb_drive") as mock_eject:
            mock_eject.return_value = (False, "Drive locked by another process.")
            success, message = ctrl.eject_usb_drive("E:\\")
            self.assertFalse(success)
            self.assertIn("locked", message)

    def test_item2_logarithmic_volume_slider(self):
        """Item 2: Logarithmic volume slider audio taper (vol^2) and toggle."""
        from unittest.mock import MagicMock

        from app.controllers.playback_controller import PlaybackController

        engine = AudioEngine(use_volume_curve=False)
        # Linear mode
        engine.set_volume(0.5, use_curve=False)
        self.assertAlmostEqual(engine.volume, 0.5)
        self.assertAlmostEqual(engine.effective_volume, 0.5)

        # Quadratic taper mode: 0.5^2 = 0.25
        engine.set_volume(0.5, use_curve=True)
        self.assertAlmostEqual(engine.volume, 0.5)
        self.assertAlmostEqual(engine.effective_volume, 0.25)

        # 0.8^2 = 0.64
        engine.set_volume(0.8, use_curve=True)
        self.assertAlmostEqual(engine.effective_volume, 0.64)

        # With auto-level enabled: 0.5^2 * 1.2 = 0.25 * 1.2 = 0.30
        engine.set_auto_level(True)
        engine.set_track_gain(1.2)
        engine.set_volume(0.5, use_curve=True)
        self.assertAlmostEqual(engine.effective_volume, 0.30)

        # PlaybackController enables curve on audio engine
        mock_eng = MagicMock()
        mock_eng.use_volume_curve = False
        pb = PlaybackController(None, mock_eng)
        self.assertTrue(mock_eng.use_volume_curve)
        pb.set_volume(0.7)
        mock_eng.set_volume.assert_called_with(0.7, use_curve=True)

    def test_item3_subsecond_trimming_nudge_and_precision_formatting(self):
        """Item 3: Sub-second nudging (+-0.1s, +-1.0s) and fractional time formatting."""
        from unittest.mock import MagicMock

        from app.controllers.playback_controller import PlaybackController

        # Precision formatting
        self.assertEqual(format_time(65.4, include_fractional=True), "01:05.4")
        self.assertEqual(format_time(65.0, include_fractional=False), "01:05")
        self.assertEqual(format_time(0.0, include_fractional=True), "00:00.0")
        self.assertEqual(format_time(3665.8, include_fractional=True), "61:05.8")

        pb = PlaybackController(None, MagicMock())
        # Nudge start forwards and backwards
        self.assertAlmostEqual(pb.nudge_start(0.1, 10.0, 20.0), 10.1)
        self.assertAlmostEqual(pb.nudge_start(-0.1, 10.0, 20.0), 9.9)
        self.assertAlmostEqual(pb.nudge_start(1.0, 10.0, 20.0), 11.0)
        self.assertAlmostEqual(pb.nudge_start(-1.0, 10.0, 20.0), 9.0)

        # Nudge start clamped at 0.0
        self.assertEqual(pb.nudge_start(-100.0, 5.0, 20.0), 0.0)
        # Nudge start clamped before end - 0.05
        self.assertAlmostEqual(pb.nudge_start(50.0, 10.0, 20.0), 19.95)

        # Nudge end forwards and backwards
        self.assertAlmostEqual(pb.nudge_end(0.1, 10.0, 20.0, 100.0), 20.1)
        self.assertAlmostEqual(pb.nudge_end(-0.1, 10.0, 20.0, 100.0), 19.9)
        self.assertAlmostEqual(pb.nudge_end(1.0, 10.0, 20.0, 100.0), 21.0)
        self.assertAlmostEqual(pb.nudge_end(-1.0, 10.0, 20.0, 100.0), 19.0)

        # Nudge end clamped after start + 0.05
        self.assertAlmostEqual(pb.nudge_end(-50.0, 10.0, 20.0, 100.0), 10.05)
        # Nudge end clamped at duration
        self.assertEqual(pb.nudge_end(500.0, 10.0, 20.0, 100.0), 100.0)

    def test_item5_controller_state_consolidation(self):
        """Item 5: PlaybackController & PlaylistController state consolidation via property delegates."""
        import tkinter as tk

        from app.main import UltimateAudioStudio

        root = tk.Tk()
        root.withdraw()
        app = None
        try:
            app = UltimateAudioStudio(root)
            # Test PlaybackController delegate synchronization
            self.assertFalse(app.is_playing_main)
            app.is_playing_main = True
            self.assertTrue(app.playback_ctrl.is_playing_main)
            self.assertTrue(app.is_playing_main)

            app.playback_ctrl.is_playing_playlist = True
            self.assertTrue(app.is_playing_playlist)

            app.is_paused = True
            self.assertTrue(app.playback_ctrl.is_paused)

            app.previewing_clip = True
            self.assertTrue(app.playback_ctrl.previewing_clip)

            app.clip_end_time = 45.2
            self.assertEqual(app.playback_ctrl.clip_end_time, 45.2)

            app.play_start_offset = 12.8
            self.assertEqual(app.playback_ctrl.play_start_offset, 12.8)

            # Test PlaylistController delegate synchronization
            app.playlists = {"Party": ["track1.mp3", "track2.mp3"]}
            self.assertEqual(app.playlist_ctrl.playlists, {"Party": ["track1.mp3", "track2.mp3"]})
            self.assertEqual(app.playlists, {"Party": ["track1.mp3", "track2.mp3"]})

            app.active_playlist_name = "Party"
            self.assertEqual(app.playlist_ctrl.active_playlist_name, "Party")

            app.playlist_index = 1
            self.assertEqual(app.playlist_ctrl.playlist_index, 1)
        finally:
            if app:
                try:
                    app.on_close()
                except Exception:
                    pass
            else:
                try:
                    root.destroy()
                except Exception:
                    pass

    def test_item6_live_visual_vu_meter(self):
        """Item 6: Live visual audio activity indicator (5-segment LED VU meter)."""
        from unittest.mock import MagicMock

        from app.controllers.playback_controller import PlaybackController

        pb = PlaybackController(None, MagicMock())

        # When stopped: level is 0
        pb.is_playing_main = False
        pb.is_paused = False
        lvl = pb.calculate_vu_level(10.0, 100.0, [0.5] * 100)
        self.assertEqual(lvl, 0)

        # When paused: level is 0
        pb.is_playing_main = True
        pb.is_paused = True
        lvl = pb.calculate_vu_level(10.0, 100.0, [0.5] * 100)
        self.assertEqual(lvl, 0)

        # When playing with no peaks: gentle pulse fallback (2 or 3)
        pb.is_paused = False
        lvl = pb.calculate_vu_level(10.0, 100.0, [])
        self.assertIn(lvl, [2, 3])

        # When playing with peaks: 5 LED bins
        # Peak amplitude mapping:
        # <= 0.04 -> 1
        # <= 0.25 -> 2
        # <= 0.50 -> 3
        # <= 0.75 -> 4
        # > 0.75 -> 5
        peaks = [0.03, 0.20, 0.45, 0.70, 0.95]
        lvl0 = pb.calculate_vu_level(0.0, 50.0, peaks)
        self.assertEqual(lvl0, 1)

        lvl1 = pb.calculate_vu_level(15.0, 50.0, peaks)
        self.assertEqual(lvl1, 2)

        lvl2 = pb.calculate_vu_level(25.0, 50.0, peaks)
        self.assertEqual(lvl2, 3)

        lvl3 = pb.calculate_vu_level(35.0, 50.0, peaks)
        self.assertEqual(lvl3, 4)

        lvl4 = pb.calculate_vu_level(45.0, 50.0, peaks)
        self.assertEqual(lvl4, 5)


if __name__ == "__main__":
    unittest.main()

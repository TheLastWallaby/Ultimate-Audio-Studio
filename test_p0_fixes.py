"""Automated regression tests verifying Phase 1 P0 fixes: USB export fs_type and CacheManager thread safety."""

import os
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from app.core.cache_manager import CacheManager
from app.main import UltimateAudioStudio


class TestCacheManagerThreadSafety(unittest.TestCase):
    def test_concurrent_read_write_invalidate(self):
        with tempfile.TemporaryDirectory() as td:
            cm = CacheManager(cache_dir=td, max_mem_entries=100)
            errors = []

            def worker_writer(thread_id):
                try:
                    for i in range(50):
                        fake_path = os.path.join(td, f"file_{thread_id}_{i}.mp3")
                        # Create dummy file so mtime/size works
                        with open(fake_path, "w") as f:
                            f.write("dummy")
                        cm.set_metadata(fake_path, {"title": f"Song {i}", "duration": 120.5})
                        cm.set_peaks(fake_path, [0.1 * (i % 10), 0.5, 0.9])
                        cm.get_duration(fake_path)
                        cm.get_peaks(fake_path)
                        if i % 10 == 0:
                            cm.invalidate(fake_path)
                except Exception as e:
                    errors.append(e)

            def worker_reader():
                try:
                    for _ in range(100):
                        cm.get_metadata(os.path.join(td, "file_0_0.mp3"))
                        cm.save_to_disk(force=False)
                except Exception as e:
                    errors.append(e)

            threads = []
            for t_id in range(6):
                t = threading.Thread(target=worker_writer, args=(t_id,))
                threads.append(t)
            for _ in range(4):
                t = threading.Thread(target=worker_reader)
                threads.append(t)

            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(errors), 0, f"Thread-safety errors detected: {errors}")


class TestUSBExportFsTypeResolution(unittest.TestCase):
    @patch("tkinter.messagebox.askyesno", return_value=False)
    def test_export_playlist_ntfs_check_no_nameerror(self, mock_ask):
        """Verify that export_playlist does not crash with NameError when evaluating fs_type."""
        root = tk.Tk()
        root.withdraw()
        app = UltimateAudioStudio(root)
        root.update()

        # Set up a fake USB drive in the app's maps
        test_drive = "E:\\"
        app._usb_map = {"USB Drive: TEST (E:\\) [NTFS]": test_drive}
        app._usb_fs_map = {"USB Drive: TEST (E:\\) [NTFS]": "NTFS"}
        app.usb_choice.set("USB Drive: TEST (E:\\) [NTFS]")
        app.export_var.set("USB")
        app.playlist_files = ["song1.mp3"]

        with patch("os.path.exists", return_value=True):
            # Calling export_playlist should evaluate is_ntfs(fs_type) safely and trigger the askyesno warning
            app.export_playlist()

        # Verify askyesno was called (meaning fs_type was checked without raising NameError)
        mock_ask.assert_called_once()
        app.on_close()


class TestModelMappingProtocolAndCacheMetadata(unittest.TestCase):
    """Verify dataclass mapping protocol, dict() conversion, and download success UI flow."""

    def test_track_metadata_mapping_protocol(self):
        from app.models import DriveInfo, ReleaseInfo, SearchResult, TrackMetadata

        tm = TrackMetadata(title="Title A", artist="Artist B", duration=120.5)
        # Verify dict() conversion does not attempt sequence indexing with int
        d = dict(tm)
        self.assertEqual(d, {"title": "Title A", "artist": "Artist B", "duration": 120.5})
        self.assertEqual(tm["title"], "Title A")
        self.assertEqual(tm["artist"], "Artist B")
        self.assertEqual(tm["duration"], 120.5)
        self.assertEqual(list(tm.keys()), ["title", "artist", "duration"])
        self.assertEqual(len(tm), 3)

        with self.assertRaises(KeyError):
            _ = tm[0]  # type: ignore[index]
        with self.assertRaises(KeyError):
            _ = tm["invalid_field"]

        sr = SearchResult(
            id="abc",
            title="Song",
            uploader="Band",
            duration_sec=180.0,
            duration_str="3:00",
            url="https://youtube.com/watch?v=abc",
        )
        self.assertEqual(dict(sr)["id"], "abc")
        self.assertEqual(len(sr), 6)
        with self.assertRaises(KeyError):
            _ = sr[0]  # type: ignore[index]

        rel = ReleaseInfo(
            tag_name="v1.0",
            name="Rel",
            body="notes",
            published_at="2026-01-01",
            asset_id=1,
            asset_name="app.exe",
            asset_size=100,
            asset_api_url="http://api",
            browser_download_url="http://dl",
            html_url="http://html",
        )
        self.assertEqual(dict(rel)["tag_name"], "v1.0")
        self.assertEqual(len(rel), 10)

        drv = DriveInfo(root="D:\\", display_label="USB", fs_type="FAT32")
        self.assertEqual(dict(drv)["root"], "D:\\")
        self.assertEqual(drv[0], "D:\\")
        self.assertEqual(drv["root"], "D:\\")

    def test_cache_manager_accepts_track_metadata(self):
        from app.models import TrackMetadata

        with tempfile.TemporaryDirectory() as td:
            cm = CacheManager(cache_dir=td)
            fake_file = os.path.join(td, "test_track.mp3")
            with open(fake_file, "w") as f:
                f.write("content")
            meta = TrackMetadata(title="Song", artist="Artist", duration=200.0)
            cm.set_metadata(fake_file, meta)
            cached = cm.get_metadata(fake_file)
            self.assertIsNotNone(cached)
            self.assertEqual(cached["title"], "Song")
            self.assertEqual(cached["artist"], "Artist")
            self.assertEqual(cached["duration"], 200.0)

    @patch("tkinter.messagebox.showinfo")
    def test_download_success_clears_busy_and_updates_library(self, mock_info):
        root = tk.Tk()
        root.withdraw()
        app = UltimateAudioStudio(root)
        try:
            # Simulate active download state
            app.set_busy(True, "Downloading...")
            app.prog_download.pack(fill=tk.X)
            app.lbl_dl_metrics.pack(fill=tk.X)
            app.btn_download.config(text="Downloading...", state=tk.DISABLED)
            app.btn_cancel_dl.config(state=tk.NORMAL)

            # Call _download_success
            app._download_success("Disturbed - Ten Thousand Fists [Official Audio].mp3")

            # Verify UI was properly restored
            self.assertFalse(app._busy)
            self.assertEqual(app.btn_download.cget("text"), "⬇ Download MP3")
            self.assertEqual(app.btn_download.cget("state"), tk.NORMAL)
            self.assertEqual(app.btn_cancel_dl.cget("state"), tk.DISABLED)
            self.assertFalse(app.prog_download.winfo_ismapped())
            self.assertFalse(app.lbl_dl_metrics.winfo_ismapped())
            mock_info.assert_called_once()
        finally:
            app.on_close()

    @patch("tkinter.messagebox.showinfo")
    def test_clip_save_success_updates_library(self, mock_info):
        root = tk.Tk()
        root.withdraw()
        app = UltimateAudioStudio(root)
        try:
            app.btn_save_clip.config(text="Saving...", state=tk.DISABLED)
            app.set_busy(True, "Trimming...")

            test_file = os.path.join(app.library_folder, "test_clip_song.mp3")
            with open(test_file, "w") as f:
                f.write("audio_data")

            app._save_success("test_clip_song.mp3", test_file, was_self_overwrite=False)

            self.assertFalse(app._busy)
            self.assertEqual(app.btn_save_clip.cget("text"), "💾 Save Clip")
            self.assertEqual(app.btn_save_clip.cget("state"), tk.NORMAL)
            self.assertIn("test_clip_song.mp3", app.library_files)
            mock_info.assert_called_once()
        finally:
            if os.path.exists(test_file):
                try:
                    os.remove(test_file)
                except Exception:
                    pass
            app.on_close()


if __name__ == "__main__":
    unittest.main()

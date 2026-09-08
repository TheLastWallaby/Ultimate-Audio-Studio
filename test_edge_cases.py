"""Integration tests for edge cases and real-world failure scenarios.

Covers:
1. Network drop / timeout during YouTube search and preview fetching
2. Missing or moved audio files during playlist navigation
3. Disk full / I/O errors during USB export
4. Process timeout and execution failures in core process utilities
5. Corrupted / 0-byte audio handling in waveform extraction
"""

import errno
import json
import os
import tempfile
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

from app.core.file_utils import atomic_save_json
from app.core.process_utils import run_ffmpeg
from app.core.waveform import extract_waveform_peaks
from app.main import UltimateAudioStudio
from app.services.downloader import search_youtube
from app.services.exporter import usb_export_worker


class TestEdgeCasesAndFailureScenarios(unittest.TestCase):
    """Test suite hardening the application against real-world errors and failure modes."""

    def test_search_youtube_network_drop(self) -> None:
        """Verify search_youtube handles network timeout/drop gracefully by returning []."""
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_inst = MagicMock()
            mock_inst.__enter__.return_value = mock_inst
            mock_inst.extract_info.side_effect = TimeoutError("Connection timed out")
            mock_ydl.return_value = mock_inst

            results = search_youtube("test query")
            self.assertEqual(results, [])

    def test_fetch_preview_network_error(self) -> None:
        """Verify fetch_preview_worker captures network failure and calls on_error callback."""
        from app.services.downloader import fetch_preview_worker

        error_messages: list[str] = []
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_inst = MagicMock()
            mock_inst.__enter__.return_value = mock_inst
            mock_inst.download.side_effect = ConnectionResetError("Remote connection closed")
            mock_ydl.return_value = mock_inst

            fetch_preview_worker(
                url="https://www.youtube.com/watch?v=fake_id_123",
                max_seconds=15,
                cancel_event=None,
                on_success=lambda p: None,
                on_error=lambda err: error_messages.append(err),
            )
            self.assertEqual(len(error_messages), 1)
            self.assertIn("Remote connection closed", error_messages[0])

    def test_missing_playlist_file_playback_handling(self) -> None:
        """Verify _play_current_pl_track skips non-existent file and updates status."""
        root = tk.Tk()
        root.withdraw()
        app = None
        try:
            app = UltimateAudioStudio(root)
            non_existent_file = r"C:\fake_folder_never_existed\ghost_track.mp3"
            app.playlist_files = [non_existent_file]
            app.playlist_index = 0

            with patch.object(app, "set_status") as mock_status, patch("tkinter.messagebox.showwarning") as mock_warn:
                app._play_current_pl_track()

                mock_status.assert_called()
                status_calls = [c[0][0] for c in mock_status.call_args_list if c[0]]
                self.assertTrue(any("Skipping missing song" in s and "ghost_track.mp3" in s for s in status_calls))
                mock_warn.assert_called_once()
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

    def test_usb_export_disk_full_error(self) -> None:
        """Verify usb_export_worker handles ENOSPC (No space left on device) gracefully."""
        with tempfile.TemporaryDirectory() as td:
            src_file = os.path.join(td, "track1.mp3")
            with open(src_file, "wb") as f:
                f.write(b"fake mp3 audio content")

            dest_folder = os.path.join(td, "export_dest")
            os.makedirs(dest_folder, exist_ok=True)

            status_messages: list[str] = []
            completed_results: list[tuple[int, int, list[str]]] = []

            with patch("shutil.copy2", side_effect=OSError(errno.ENOSPC, "No space left on device")):
                usb_export_worker(
                    dest_folder=dest_folder,
                    playlist_name="Test Playlist",
                    files_to_export=[src_file],
                    normalize=False,
                    on_status=lambda s: status_messages.append(s),
                    on_success=lambda s, t, sk: completed_results.append((s, t, sk)),
                )

            self.assertEqual(len(completed_results), 1)
            success_count, total_count, skipped = completed_results[0]
            self.assertEqual(success_count, 0)
            self.assertEqual(total_count, 1)
            self.assertEqual(len(skipped), 1)
            self.assertIn("No space left on device", skipped[0])

    def test_process_utils_ffmpeg_nonzero_exit(self) -> None:
        """Verify run_ffmpeg captures non-zero return codes and stderr without crashing."""
        result = run_ffmpeg(["-invalid_unknown_flag_12345"])
        self.assertNotEqual(result.returncode, 0)

    def test_waveform_empty_or_corrupted_file(self) -> None:
        """Verify extract_waveform_peaks gracefully handles empty 0-byte audio files."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(b"")
            corrupt_path = tf.name

        try:
            peaks = extract_waveform_peaks(corrupt_path)
            self.assertIsInstance(peaks, list)
        finally:
            if os.path.exists(corrupt_path):
                os.remove(corrupt_path)

    def test_samples_to_peaks_array_and_list_support(self) -> None:
        """Verify _samples_to_peaks works seamlessly with both array.array and list on all Python versions."""
        import array

        from app.core.waveform import _samples_to_peaks

        # Test with array.array('h')
        arr = array.array("h", [1000, -2000, 3000, -4000, 2000, -1000])
        peaks_arr = _samples_to_peaks(arr, n_bars=3)
        self.assertEqual(len(peaks_arr), 3)
        self.assertAlmostEqual(max(peaks_arr), 1.0)

        # Test with list[int]
        lst = [1000, -2000, 3000, -4000, 2000, -1000]
        peaks_lst = _samples_to_peaks(lst, n_bars=3)
        self.assertEqual(peaks_lst, peaks_arr)

        # Test empty
        self.assertEqual(_samples_to_peaks([], n_bars=10), [])

    def test_atomic_save_json_creates_valid_file(self) -> None:
        """Verify atomic_save_json handles data integrity and replaces existing file safely."""
        with tempfile.TemporaryDirectory() as td:
            target_path = os.path.join(td, "nested", "target.json")
            data1 = {"version": "1.0.0", "items": [1, 2, 3]}
            atomic_save_json(target_path, data1)

            self.assertTrue(os.path.exists(target_path))

            data2 = {"version": "2.0.0", "items": [4, 5, 6]}
            atomic_save_json(target_path, data2)

            with open(target_path, encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded, data2)


if __name__ == "__main__":
    unittest.main()

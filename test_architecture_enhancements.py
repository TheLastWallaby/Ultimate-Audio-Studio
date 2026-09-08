"""Automated unit tests verifying TaskManager, platform security, and models."""

import time
import unittest

from app.core.task_manager import TaskManager
from app.models import DriveInfo, ReleaseInfo, SearchResult, TrackMetadata
from app.platform_utils import safely_eject_usb_drive


class TestTaskManager(unittest.TestCase):
    def test_submit_and_complete(self):
        tm = TaskManager(max_workers=2)
        results = []

        def sample_work(x, y):
            return x + y

        fut = tm.submit_task(sample_work, 10, 20, on_success=lambda res: results.append(res))
        self.assertIsNotNone(fut)
        if fut:
            fut.result(timeout=2.0)
        time.sleep(0.05)
        self.assertEqual(results, [30])
        tm.shutdown(wait=True)

    def test_error_capture(self):
        tm = TaskManager(max_workers=2)
        errors = []

        def faulty_work():
            raise ValueError("Intentional failure")

        fut = tm.submit_task(faulty_work, on_error=lambda err: errors.append(err))
        self.assertIsNotNone(fut)
        time.sleep(0.1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        tm.shutdown(wait=True)

    def test_shutdown_cancels_pending(self):
        tm = TaskManager(max_workers=1)
        # Block worker 1
        _ = tm.submit_task(time.sleep, 0.05)
        # Submit task 2 which will wait in queue
        _ = tm.submit_task(time.sleep, 0.1)
        tm.shutdown(wait=True, cancel_futures=True)
        # Verify subsequent submissions return None
        rejected_fut = tm.submit_task(time.sleep, 0.05)
        self.assertIsNone(rejected_fut)
        self.assertIsNone(rejected_fut)


class TestPlatformSecurity(unittest.TestCase):
    def test_eject_usb_drive_input_validation(self):
        # Empty input
        ok, msg = safely_eject_usb_drive("")
        self.assertFalse(ok)
        self.assertIn("no drive", msg.lower())

        # Malicious PowerShell command injection attempt
        malicious_input = 'D:; Remove-Item -Path C:\\ -Recurse -Force'
        ok, msg = safely_eject_usb_drive(malicious_input)
        self.assertFalse(ok)
        self.assertIn("invalid drive", msg.lower())

        # Invalid drive letters
        ok, msg = safely_eject_usb_drive("1:\\")
        self.assertFalse(ok)
        self.assertIn("invalid drive", msg.lower())

        ok, msg = safely_eject_usb_drive("ZZ:\\")
        self.assertFalse(ok)
        self.assertIn("invalid drive", msg.lower())


class TestModels(unittest.TestCase):
    def test_track_metadata_dataclass(self):
        meta = TrackMetadata(title="Bohemian Rhapsody", artist="Queen", duration=354.0)
        self.assertEqual(meta.title, "Bohemian Rhapsody")
        self.assertEqual(meta.artist, "Queen")
        self.assertEqual(meta.duration, 354.0)

    def test_search_result_dataclass(self):
        sr = SearchResult(
            id="vid123", title="Song Title", uploader="Artist",
            duration_sec=180.0, duration_str="03:00", url="https://youtube.com/watch?v=vid123"
        )
        self.assertEqual(sr.id, "vid123")
        self.assertEqual(sr.duration_str, "03:00")

    def test_release_info_dataclass(self):
        rel = ReleaseInfo(
            tag_name="v1.0.9",
            name="Release v1.0.9",
            body="Release notes",
            published_at="2026-09-06T17:00:00Z",
            asset_id=12345,
            asset_name="Ultimate Audio Studio.exe",
            asset_size=50000000,
            asset_api_url="https://api.github.com/repos/owner/repo/releases/assets/12345",
            browser_download_url="https://github.com/owner/repo/releases/download/v1.0.9/Ultimate.Audio.Studio.exe",
            html_url="https://github.com/owner/repo/releases/tag/v1.0.9",
        )
        self.assertEqual(rel.tag_name, "v1.0.9")
        self.assertEqual(rel.asset_name, "Ultimate Audio Studio.exe")
        self.assertEqual(rel.asset_id, 12345)

    def test_drive_info_dataclass(self):
        di = DriveInfo(root="E:\\", display_label="USB Drive: MUSIC (E:\\) [FAT32]", fs_type="FAT32")
        self.assertEqual(di.root, "E:\\")
        self.assertEqual(di.fs_type, "FAT32")


if __name__ == "__main__":
    unittest.main()

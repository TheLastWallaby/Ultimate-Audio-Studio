import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from app.config import SettingsManager, get_update_token
from app.platform_utils import already_running, clean_pyi_env, launch_detached_gui, release_instance_mutex
from app.services.updater import (
    _GitHubAssetRedirectHandler,
    check_latest_release,
    is_newer_version,
    parse_version,
)


class TestUpdaterService(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(parse_version("v1.0.0"), (1, 0, 0))
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("2.15.4"), (2, 15, 4))
        self.assertEqual(parse_version("v1.0"), (1, 0, 0))
        self.assertEqual(parse_version("2"), (2, 0, 0))
        self.assertEqual(parse_version(""), (0, 0, 0))

    def test_is_newer_version(self):
        self.assertTrue(is_newer_version("v1.0.1", "1.0.0"))
        self.assertTrue(is_newer_version("v1.10.0", "1.9.0"))
        self.assertTrue(is_newer_version("v2.0.0", "1.99.99"))
        self.assertTrue(is_newer_version("v1.0.1", "1.0"))
        self.assertFalse(is_newer_version("v1.0.0", "1.0"))
        self.assertFalse(is_newer_version("v1.0.0", "1.0.0"))
        self.assertFalse(is_newer_version("v0.9.0", "1.0.0"))
        self.assertFalse(is_newer_version("1.0.0", "1.0.1"))

    def test_redirect_handler_s3_auth_stripping(self):
        handler = _GitHubAssetRedirectHandler()
        req = urllib.request.Request("https://api.github.com/asset", headers={"Authorization": "Bearer test-token"})

        # S3 Redirect
        s3_url = "https://github-production-release-asset-2e65be.s3.amazonaws.com/12345/file.exe"
        new_req = handler.redirect_request(req, None, 302, "Found", {}, s3_url)
        self.assertIsNotNone(new_req)
        self.assertNotIn("Authorization", new_req.headers)
        self.assertNotIn("authorization", [k.lower() for k in new_req.headers.keys()])

    def test_get_update_token(self):
        tok = get_update_token()
        self.assertIsInstance(tok, str)

    def test_save_update_token(self):
        with tempfile.TemporaryDirectory() as td:
            custom_settings_path = os.path.join(td, "test_settings.json")
            mgr = SettingsManager(settings_path=custom_settings_path)
            test_tok = "custom_test_token_12345"
            ok = mgr.save_token(test_tok)
            self.assertTrue(ok)
            self.assertEqual(mgr.get_token(), test_tok)
            self.assertTrue(os.path.exists(custom_settings_path))
            with open(custom_settings_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("github_update_token"), test_tok)

    def test_release_instance_mutex(self):
        import app.platform_utils as pu

        # Calling release_instance_mutex when None should be a safe no-op
        pu._instance_mutex = None
        release_instance_mutex()
        self.assertIsNone(pu._instance_mutex)

        # Calling already_running acquires/checks and sets _instance_mutex
        already_running()
        self.assertIsNotNone(pu._instance_mutex)

        # Calling release_instance_mutex closes the handle and resets it to None
        release_instance_mutex()
        self.assertIsNone(pu._instance_mutex)

    def test_check_latest_release_error_reporting(self):
        # Mock a 404 HTTPError without token
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError("https://api.github.com/test", 404, "Not Found", {}, None)
            has_update, info, err = check_latest_release(current_ver="1.0.0", token="", return_error=True)
            self.assertFalse(has_update)
            self.assertIsNone(info)
            self.assertIn("404 Not Found", err)

    def test_check_latest_release_backward_compatibility(self):
        # Unpacking 2 elements should work without error
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = Exception("network disconnected")
            has_update, info = check_latest_release(current_ver="1.0.0")
            self.assertFalse(has_update)
            self.assertIsNone(info)

    def test_settings_preservation(self):
        from app.config import atomic_save_json

        fd, tmp_settings = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            atomic_save_json(tmp_settings, {"volume": 60, "custom_pref": 42})
            with open(tmp_settings, encoding="utf-8") as f:
                existing = json.load(f)
            existing.update({"volume": 75, "library_folder": "C:\\Music"})
            atomic_save_json(tmp_settings, existing)

            with open(tmp_settings, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["custom_pref"], 42)
            self.assertEqual(saved["volume"], 75)
        finally:
            if os.path.exists(tmp_settings):
                os.remove(tmp_settings)

    def test_clean_pyi_env(self):
        # Set various PyInstaller runtime variables in os.environ
        os.environ["_PYI_APPLICATION_HOME_DIR"] = "C:\\test\\mei"
        os.environ["_PYI_ARCHIVE_FILE"] = "C:\\test\\app.exe"
        os.environ["_MEIPASS2"] = "C:\\test\\mei2"
        os.environ["PYI_PARENT_PROCESS_LEVEL"] = "1"

        clean_dict = clean_pyi_env()

        for k in ["_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_MEIPASS2", "PYI_PARENT_PROCESS_LEVEL"]:
            self.assertNotIn(k, os.environ)
            self.assertNotIn(k, clean_dict)

    def test_launch_detached_gui_sanitizes_env(self):
        with patch("os.path.exists", return_value=True), patch("os.startfile", create=True) as mock_startfile:
            os.environ["_PYI_ARCHIVE_FILE"] = "test.exe"
            os.environ["_MEIPASS2"] = "C:\\test"
            launch_detached_gui("test.exe")
            mock_startfile.assert_called_once_with("test.exe")
            self.assertNotIn("_PYI_ARCHIVE_FILE", os.environ)
            self.assertNotIn("_MEIPASS2", os.environ)


if __name__ == "__main__":
    unittest.main()

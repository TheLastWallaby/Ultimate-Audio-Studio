"""Unit tests for centralized Pydantic v2 settings module."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.config import (
    APP_VERSION,
    GITHUB_OWNER,
    GITHUB_REPO,
    RELEASES_API_URL,
    SettingsManager,
)
from app.core.config import (
    AppConfig,
    BinarySettings,
    GitHubSettings,
    PathSettings,
    Settings,
    SSLSettings,
    SystemSettings,
    clear_settings_cache,
    get_settings,
)


class TestCentralizedSettings(unittest.TestCase):
    """Test suite for centralized Pydantic v2 configuration architecture."""

    def setUp(self) -> None:
        clear_settings_cache()

    def tearDown(self) -> None:
        clear_settings_cache()

    def test_default_settings_instantiation(self) -> None:
        """Verify default settings instantiation matches production defaults."""
        settings = Settings()
        self.assertIsInstance(settings.app, AppConfig)
        self.assertEqual(settings.app.app_name, "Ultimate Audio Studio")
        self.assertEqual(settings.app.app_version, "1.1.3")
        self.assertEqual(settings.app.environment, "production")
        self.assertFalse(settings.app.debug)

        self.assertIsInstance(settings.github, GitHubSettings)
        self.assertEqual(settings.github.owner, "TheLastWallaby")
        self.assertEqual(settings.github.repo, "Ultimate-Audio-Studio")
        self.assertEqual(
            str(settings.github.releases_api_url),
            "https://api.github.com/repos/TheLastWallaby/Ultimate-Audio-Studio/releases/latest",
        )

        self.assertIsInstance(settings.paths, PathSettings)
        self.assertIsInstance(settings.paths.music_dir, Path)
        self.assertIn("Music", str(settings.paths.music_dir))
        self.assertTrue(str(settings.paths.playlists_path).endswith("audio_studio_playlists.json"))
        self.assertTrue(str(settings.paths.settings_path).endswith("audio_studio_settings.json"))
        self.assertTrue(str(settings.paths.error_log_path).endswith("audio_studio_error.txt"))
        self.assertTrue(str(settings.paths.yt_cache_dir).endswith(".audio_studio_cache"))
        self.assertTrue(str(settings.paths.cover_cache_dir).endswith("audio_studio_art"))
        self.assertTrue(str(settings.paths.preview_cache_dir).endswith("audio_studio_preview"))

        self.assertIsInstance(settings.ssl, SSLSettings)
        self.assertIsInstance(settings.ssl.ssl_cert_file, Path)
        self.assertIsInstance(settings.ssl.requests_ca_bundle, Path)

        self.assertIsInstance(settings.binaries, BinarySettings)
        self.assertIsNone(settings.binaries.ffmpeg_path)
        self.assertIsNone(settings.binaries.ffprobe_path)

        self.assertIsInstance(settings.system, SystemSettings)
        self.assertTrue(len(settings.system.system_drive) >= 1)

    def test_convenience_properties(self) -> None:
        """Verify top-level convenience property accessors."""
        settings = Settings()
        self.assertEqual(settings.app_version, settings.app.app_version)
        self.assertEqual(settings.debug, settings.app.debug)
        self.assertEqual(settings.environment, settings.app.environment)
        self.assertEqual(settings.github_update_token, "")
        self.assertEqual(settings.releases_api_url, str(settings.github.releases_api_url))
        self.assertEqual(settings.music_dir, settings.paths.music_dir)
        self.assertEqual(settings.playlists_path, settings.paths.playlists_path)
        self.assertEqual(settings.settings_path, settings.paths.settings_path)
        self.assertEqual(settings.error_log_path, settings.paths.error_log_path)
        self.assertEqual(settings.yt_cache_dir, settings.paths.yt_cache_dir)
        self.assertEqual(settings.cover_cache_dir, settings.paths.cover_cache_dir)
        self.assertEqual(settings.preview_cache_dir, settings.paths.preview_cache_dir)

    def test_immutability_enforcement(self) -> None:
        """Verify settings models are frozen and reject runtime mutations."""
        settings = Settings()
        with self.assertRaises(ValidationError):
            settings.app.debug = True  # type: ignore[misc]

        with self.assertRaises(ValidationError):
            settings.github.owner = "OtherOwner"  # type: ignore[misc]

    def test_cached_singleton_behavior(self) -> None:
        """Verify get_settings returns identical cached instance until clear_settings_cache."""
        s1 = get_settings()
        s2 = get_settings()
        self.assertIs(s1, s2)

        clear_settings_cache()
        s3 = get_settings()
        self.assertIsNot(s1, s3)

    def test_environment_variable_flat_aliases(self) -> None:
        """Verify flat environment variables are routed into nested models."""
        env_vars = {
            "APP_ENV": "development",
            "DEBUG": "true",
            "GITHUB_OWNER": "CustomOwner",
            "GITHUB_REPO": "CustomRepo",
            "RELEASES_API_URL": "https://api.github.com/repos/CustomOwner/CustomRepo/releases/latest",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            settings = Settings()
            self.assertEqual(settings.app.environment, "development")
            self.assertTrue(settings.app.debug)
            self.assertEqual(settings.github.owner, "CustomOwner")
            self.assertEqual(settings.github.repo, "CustomRepo")
            self.assertEqual(settings.github_update_token, "")

    def test_environment_variable_nested_delimiter(self) -> None:
        """Verify double-underscore nested environment variables."""
        env_vars = {
            "APP__ENVIRONMENT": "staging",
            "APP__DEBUG": "true",
            "GITHUB__OWNER": "NestedOwner",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            settings = Settings()
            self.assertEqual(settings.app.environment, "staging")
            self.assertTrue(settings.app.debug)
            self.assertEqual(settings.github.owner, "NestedOwner")
            self.assertEqual(settings.github_update_token, "")

    def test_invalid_url_scheme_validation(self) -> None:
        """Verify insecure http:// protocol is rejected by releases_api_url validator."""
        with self.assertRaises(ValidationError):
            GitHubSettings(releases_api_url="http://api.github.com/releases")  # type: ignore[arg-type]

    def test_invalid_environment_literal(self) -> None:
        """Verify invalid environment names are rejected by Literal validator."""
        with self.assertRaises(ValidationError):
            AppConfig(environment="invalid_env")  # type: ignore[arg-type]

    def test_path_safeguards_never_empty(self) -> None:
        """Verify PathSettings never resolves to empty string or current directory."""
        paths = PathSettings(
            music_dir="",  # type: ignore[arg-type]
            playlists_path="",  # type: ignore[arg-type]
            cover_cache_dir="",  # type: ignore[arg-type]
            preview_cache_dir="",  # type: ignore[arg-type]
        )
        self.assertNotEqual(str(paths.music_dir), "")
        self.assertNotEqual(str(paths.music_dir), ".")
        self.assertNotEqual(str(paths.cover_cache_dir), "")
        self.assertNotEqual(str(paths.cover_cache_dir), ".")
        self.assertNotEqual(str(paths.preview_cache_dir), "")
        self.assertNotEqual(str(paths.preview_cache_dir), ".")

    def test_app_config_backward_compatibility(self) -> None:
        """Verify app.config constants and functions reflect centralized settings."""
        settings = get_settings()
        self.assertEqual(APP_VERSION, settings.app_version)
        self.assertEqual(GITHUB_OWNER, settings.github.owner)
        self.assertEqual(GITHUB_REPO, settings.github.repo)
        self.assertEqual(RELEASES_API_URL, settings.releases_api_url)

    def test_settings_manager_token_persistence(self) -> None:
        """Verify SettingsManager properly saves and retrieves custom tokens from settings file."""
        with tempfile.TemporaryDirectory() as td:
            custom_path = str(Path(td) / "test_settings.json")
            mgr = SettingsManager(settings_path=custom_path)
            self.assertEqual(mgr.get_token(), "")

            save_tok = "custom_saved_token_9876543210"
            self.assertTrue(mgr.save_token(save_tok))
            self.assertEqual(mgr.get_token(), save_tok)


if __name__ == "__main__":
    unittest.main()

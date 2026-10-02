"""Centralized application configuration and validated environment settings using Pydantic v2."""

from __future__ import annotations

import functools
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal

import certifi
from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    HttpUrl,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

logger = logging.getLogger(__name__)

__all__ = [
    "AppConfig",
    "BinarySettings",
    "GitHubSettings",
    "PathSettings",
    "SSLSettings",
    "Settings",
    "SystemSettings",
    "clear_settings_cache",
    "get_settings",
]


def _get_default_base_path() -> Path:
    """Determine the default base application directory (supports PyInstaller frozen builds)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        return Path(meipass) if meipass else Path.cwd()
    return Path(__file__).resolve().parent.parent.parent


def _get_default_system_drive() -> str:
    """Determine host Windows system drive letter."""
    return os.environ.get("SystemDrive", "C:").upper()


def _default_music_dir() -> Path:
    """Default directory for user music library."""
    return Path.home() / "Music"


def _default_temp_dir() -> Path:
    """Default directory for application temporary caches."""
    return Path(tempfile.gettempdir())


class AppConfig(BaseModel):
    """Core application metadata, lifecycle environment, and debugging options."""

    model_config = SettingsConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    app_name: str = Field(default="Ultimate Audio Studio", description="Display application name")
    app_version: str = Field(
        default="1.2.3",
        validation_alias=AliasChoices("app_version"),
        description="Application semantic version string",
    )
    environment: Literal["development", "production", "test", "staging"] = Field(
        default="production",
        validation_alias=AliasChoices("UAS_APP_ENV", "environment"),
        description="Operating runtime environment",
    )
    debug: bool = Field(
        default=False,
        validation_alias=AliasChoices("UAS_DEBUG", "debug"),
        description="Enable debug features and detailed diagnostic logging",
    )


class GitHubSettings(BaseModel):
    """GitHub repository details and public release endpoints for updates."""

    model_config = SettingsConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    owner: str = Field(
        default="TheLastWallaby",
        validation_alias=AliasChoices("UAS_GITHUB_OWNER", "owner"),
        description="GitHub organization or user owner of the repository",
    )
    repo: str = Field(
        default="Ultimate-Audio-Studio",
        validation_alias=AliasChoices("UAS_GITHUB_REPO", "repo"),
        description="GitHub repository name",
    )
    releases_api_url: HttpUrl = Field(
        default=HttpUrl("https://api.github.com/repos/TheLastWallaby/Ultimate-Audio-Studio/releases/latest"),
        validation_alias=AliasChoices("UAS_RELEASES_API_URL", "releases_api_url"),
        description="GitHub Releases API endpoint URL",
    )

    @field_validator("releases_api_url")
    @classmethod
    def validate_https(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme != "https":
            raise ValueError("releases_api_url must use the secure https:// protocol scheme")
        return v


class PathSettings(BaseModel):
    """Filesystem storage directories, cache locations, and data file paths."""

    model_config = SettingsConfigDict(frozen=True, extra="ignore", arbitrary_types_allowed=True, populate_by_name=True)

    base_path: Path = Field(
        default_factory=_get_default_base_path,
        validation_alias=AliasChoices("UAS_BASE_PATH", "base_path"),
        description="Root application install or frozen bundle directory",
    )
    music_dir: Path = Field(
        default_factory=_default_music_dir,
        validation_alias=AliasChoices("UAS_MUSIC_DIR", "music_dir"),
        description="Root music directory for user audio files and application data",
    )
    playlists_path: Path = Field(
        default_factory=lambda: _default_music_dir() / "audio_studio_playlists.json",
        validation_alias=AliasChoices("UAS_PLAYLISTS_PATH", "playlists_path"),
        description="Path to user playlists JSON document",
    )
    settings_path: Path = Field(
        default_factory=lambda: _default_music_dir() / "audio_studio_settings.json",
        validation_alias=AliasChoices("UAS_SETTINGS_PATH", "settings_path"),
        description="Path to user GUI preferences JSON document",
    )
    error_log_path: Path = Field(
        default_factory=lambda: _default_music_dir() / "audio_studio_error.txt",
        validation_alias=AliasChoices("UAS_ERROR_LOG_PATH", "error_log_path"),
        description="Path to text error log file",
    )
    yt_cache_dir: Path = Field(
        default_factory=lambda: _default_music_dir() / ".audio_studio_cache",
        validation_alias=AliasChoices("UAS_YT_CACHE_DIR", "yt_cache_dir"),
        description="Directory for persistent YouTube metadata cache",
    )
    cover_cache_dir: Path = Field(
        default_factory=lambda: _default_temp_dir() / "audio_studio_art",
        validation_alias=AliasChoices("UAS_COVER_CACHE_DIR", "cover_cache_dir"),
        description="Temporary directory for cover art cache",
    )
    preview_cache_dir: Path = Field(
        default_factory=lambda: _default_temp_dir() / "audio_studio_preview",
        validation_alias=AliasChoices("UAS_PREVIEW_CACHE_DIR", "preview_cache_dir"),
        description="Temporary directory for audio preview clip cache",
    )

    @field_validator(
        "base_path",
        "music_dir",
        "playlists_path",
        "settings_path",
        "error_log_path",
        "yt_cache_dir",
        "cover_cache_dir",
        "preview_cache_dir",
        mode="before",
    )
    @classmethod
    def parse_path(cls, v: Any, info: ValidationInfo) -> Any:
        defaults = {
            "base_path": _get_default_base_path(),
            "music_dir": _default_music_dir(),
            "playlists_path": _default_music_dir() / "audio_studio_playlists.json",
            "settings_path": _default_music_dir() / "audio_studio_settings.json",
            "error_log_path": _default_music_dir() / "audio_studio_error.txt",
            "yt_cache_dir": _default_music_dir() / ".audio_studio_cache",
            "cover_cache_dir": _default_temp_dir() / "audio_studio_art",
            "preview_cache_dir": _default_temp_dir() / "audio_studio_preview",
        }
        fallback = defaults.get(info.field_name, Path.cwd()) if info.field_name else Path.cwd()
        if v is None or (isinstance(v, str) and not v.strip()):
            return fallback
        if isinstance(v, (str, Path)):
            p = Path(str(v).strip())
            return fallback if str(p) in ("", ".") else p
        return v

    @model_validator(mode="after")
    def resolve_paths(self) -> PathSettings:
        music = self.music_dir if str(self.music_dir) not in ("", ".") else _default_music_dir()
        temp_dir = _default_temp_dir()

        object.__setattr__(self, "music_dir", music)
        if str(self.playlists_path) in ("", "."):
            object.__setattr__(self, "playlists_path", music / "audio_studio_playlists.json")
        if str(self.settings_path) in ("", "."):
            object.__setattr__(self, "settings_path", music / "audio_studio_settings.json")
        if str(self.error_log_path) in ("", "."):
            object.__setattr__(self, "error_log_path", music / "audio_studio_error.txt")
        if str(self.yt_cache_dir) in ("", "."):
            object.__setattr__(self, "yt_cache_dir", music / ".audio_studio_cache")
        if str(self.cover_cache_dir) in ("", "."):
            object.__setattr__(self, "cover_cache_dir", temp_dir / "audio_studio_art")
        if str(self.preview_cache_dir) in ("", "."):
            object.__setattr__(self, "preview_cache_dir", temp_dir / "audio_studio_preview")
        return self


class SSLSettings(BaseModel):
    """SSL/TLS certificate bundles for secure network downloads and YouTube operations."""

    model_config = SettingsConfigDict(frozen=True, extra="ignore", arbitrary_types_allowed=True, populate_by_name=True)

    ssl_cert_file: Path = Field(
        default_factory=lambda: Path(certifi.where()),
        validation_alias=AliasChoices("SSL_CERT_FILE", "ssl_cert_file"),
        description="Path to CA certificate bundle for Python SSL/urllib",
    )
    requests_ca_bundle: Path = Field(
        default_factory=lambda: Path(certifi.where()),
        validation_alias=AliasChoices("REQUESTS_CA_BUNDLE", "requests_ca_bundle"),
        description="Path to CA certificate bundle for requests and yt-dlp",
    )

    @field_validator("ssl_cert_file", "requests_ca_bundle", mode="before")
    @classmethod
    def parse_path(cls, v: Any) -> Path:
        if isinstance(v, Path):
            return v
        if isinstance(v, str) and v.strip():
            return Path(v.strip())
        return Path(certifi.where())


class BinarySettings(BaseModel):
    """Optional overrides for external media utilities (ffmpeg, ffprobe)."""

    model_config = SettingsConfigDict(frozen=True, extra="ignore", arbitrary_types_allowed=True, populate_by_name=True)

    ffmpeg_path: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("UAS_FFMPEG_PATH", "ffmpeg_path"),
        description="Explicit override path to ffmpeg binary",
    )
    ffprobe_path: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("UAS_FFPROBE_PATH", "ffprobe_path"),
        description="Explicit override path to ffprobe binary",
    )

    @field_validator("ffmpeg_path", "ffprobe_path", mode="before")
    @classmethod
    def parse_path(cls, v: Any) -> Path | None:
        if isinstance(v, Path):
            return v
        if isinstance(v, str):
            stripped = v.strip()
            return Path(stripped) if stripped else None
        return None


class SystemSettings(BaseModel):
    """Host operating system runtime environment variables and library paths."""

    model_config = SettingsConfigDict(frozen=True, extra="ignore", arbitrary_types_allowed=True, populate_by_name=True)

    local_app_data: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("LOCALAPPDATA", "local_app_data"),
        description="Windows LOCALAPPDATA directory for WinGet package discovery",
    )
    system_drive: str = Field(
        default_factory=_get_default_system_drive,
        validation_alias=AliasChoices("SystemDrive", "system_drive"),
        description="Host primary system drive letter",
    )
    tcl_library: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("TCL_LIBRARY", "tcl_library"),
        description="Path to Tcl runtime library directory",
    )
    tk_library: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("TK_LIBRARY", "tk_library"),
        description="Path to Tk runtime library directory",
    )

    @field_validator("local_app_data", "tcl_library", "tk_library", mode="before")
    @classmethod
    def parse_path(cls, v: Any) -> Path | None:
        if isinstance(v, Path):
            return v
        if isinstance(v, str):
            stripped = v.strip()
            return Path(stripped) if stripped else None
        return None


class Settings(BaseSettings):
    """Centralized, immutable application settings for Ultimate Audio Studio."""

    model_config = SettingsConfigDict(
        env_prefix="UAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
        frozen=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Ignore stray ``.env`` files in packaged builds and all env input in safe-fallback mode."""
        if not _env_overrides_enabled():
            return (init_settings,)
        if getattr(sys, "frozen", False):
            return (init_settings, env_settings)
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

    app: AppConfig = Field(default_factory=AppConfig)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    ssl: SSLSettings = Field(default_factory=SSLSettings)
    binaries: BinarySettings = Field(default_factory=BinarySettings)
    system: SystemSettings = Field(default_factory=SystemSettings)

    @model_validator(mode="before")
    @classmethod
    def assemble_nested_configs(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        res = dict(data)

        # Mapping of flat environment variable / data keys to nested models
        mapping: dict[str, list[tuple[str, str | None, Any]]] = {
            "app": [
                ("app_version", None, "1.2.3"),
                ("environment", "UAS_APP_ENV", "production"),
                ("debug", "UAS_DEBUG", False),
            ],
            "github": [
                ("owner", "UAS_GITHUB_OWNER", "TheLastWallaby"),
                ("repo", "UAS_GITHUB_REPO", "Ultimate-Audio-Studio"),
                ("releases_api_url", "UAS_RELEASES_API_URL", None),
            ],
            "paths": [
                ("base_path", "UAS_BASE_PATH", None),
                ("music_dir", "UAS_MUSIC_DIR", None),
                ("playlists_path", "UAS_PLAYLISTS_PATH", None),
                ("settings_path", "UAS_SETTINGS_PATH", None),
                ("error_log_path", "UAS_ERROR_LOG_PATH", None),
                ("yt_cache_dir", "UAS_YT_CACHE_DIR", None),
                ("cover_cache_dir", "UAS_COVER_CACHE_DIR", None),
                ("preview_cache_dir", "UAS_PREVIEW_CACHE_DIR", None),
            ],
            "ssl": [
                ("ssl_cert_file", "SSL_CERT_FILE", None),
                ("requests_ca_bundle", "REQUESTS_CA_BUNDLE", None),
            ],
            "binaries": [
                ("ffmpeg_path", "UAS_FFMPEG_PATH", None),
                ("ffprobe_path", "UAS_FFPROBE_PATH", None),
            ],
            "system": [
                ("local_app_data", "LOCALAPPDATA", None),
                ("system_drive", "SystemDrive", None),
                ("tcl_library", "TCL_LIBRARY", None),
                ("tk_library", "TK_LIBRARY", None),
            ],
        }

        for section, fields in mapping.items():
            existing = res.get(section)
            if isinstance(existing, BaseModel):
                section_data = existing.model_dump()
            elif isinstance(existing, dict):
                section_data = dict(existing)
            else:
                section_data = {}

            for field_name, env_key, _ in fields:
                variants = (field_name,) if env_key is None else (field_name, env_key.lower(), env_key)
                for key_variant in variants:
                    if key_variant in res and (section_data.get(field_name) is None):
                        val = res.pop(key_variant)
                        if val is not None and (not isinstance(val, str) or val.strip()):
                            section_data[field_name] = val
                        break
                if env_key is None or not _env_overrides_enabled():
                    continue
                if section_data.get(field_name) is None and env_key in os.environ:
                    env_val = os.environ[env_key].strip()
                    if env_val:
                        section_data[field_name] = env_val

            if section_data:
                res[section] = section_data

        return res

    # Convenience accessors
    @property
    def app_version(self) -> str:
        return self.app.app_version

    @property
    def debug(self) -> bool:
        return self.app.debug

    @property
    def environment(self) -> str:
        return self.app.environment

    @property
    def github_update_token(self) -> str:
        return ""

    @property
    def releases_api_url(self) -> str:
        return str(self.github.releases_api_url)

    @property
    def music_dir(self) -> Path:
        return self.paths.music_dir

    @property
    def playlists_path(self) -> Path:
        return self.paths.playlists_path

    @property
    def settings_path(self) -> Path:
        return self.paths.settings_path

    @property
    def error_log_path(self) -> Path:
        return self.paths.error_log_path

    @property
    def yt_cache_dir(self) -> Path:
        return self.paths.yt_cache_dir

    @property
    def cover_cache_dir(self) -> Path:
        return self.paths.cover_cache_dir

    @property
    def preview_cache_dir(self) -> Path:
        return self.paths.preview_cache_dir


_env_disabled = False


def _env_overrides_enabled() -> bool:
    return not _env_disabled


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retrieve cached, immutable global application settings instance.

    An invalid ``UAS_*`` environment value must never stop the app from opening, so on a
    validation error the settings are rebuilt from built-in defaults and the problem is logged.
    """
    global _env_disabled
    try:
        return Settings()
    except ValidationError as err:
        logger.warning("Ignoring invalid environment configuration, using defaults: %s", err)
        _env_disabled = True
        try:
            return Settings()
        finally:
            _env_disabled = False


def clear_settings_cache() -> None:
    """Clear cached settings instance (primarily for unit test isolation)."""
    get_settings.cache_clear()

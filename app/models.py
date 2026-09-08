"""Strongly-typed immutable data transfer objects and models for Ultimate Audio Studio."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class TrackMetadata:
    """Metadata extracted from an audio file."""

    title: str = ""
    artist: str = ""
    duration: float = 0.0

    def __getitem__(self, item: str) -> Any:
        if not isinstance(item, str) or not hasattr(self, item):
            raise KeyError(item)
        return getattr(self, item)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self) -> tuple[str, ...]:
        return ("title", "artist", "duration")

    def __iter__(self) -> Iterator[str]:
        yield from self.keys()

    def __len__(self) -> int:
        return 3

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "artist": self.artist, "duration": self.duration}


@dataclass(slots=True, frozen=True)
class SearchResult:
    """Represents a single search result from YouTube."""

    id: str
    title: str
    uploader: str
    duration_sec: float | None
    duration_str: str
    url: str

    def __getitem__(self, item: str) -> Any:
        if not isinstance(item, str) or not hasattr(self, item):
            raise KeyError(item)
        return getattr(self, item)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self) -> tuple[str, ...]:
        return ("id", "title", "uploader", "duration_sec", "duration_str", "url")

    def __iter__(self) -> Iterator[str]:
        yield from self.keys()

    def __len__(self) -> int:
        return 6

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "uploader": self.uploader,
            "duration_sec": self.duration_sec,
            "duration_str": self.duration_str,
            "url": self.url,
        }


@dataclass(slots=True, frozen=True)
class ReleaseInfo:
    """GitHub release asset information for application updates."""

    tag_name: str
    name: str
    body: str
    published_at: str
    asset_id: int | None
    asset_name: str
    asset_size: int
    asset_api_url: str
    browser_download_url: str
    html_url: str

    def __getitem__(self, item: str) -> Any:
        if not isinstance(item, str) or not hasattr(self, item):
            raise KeyError(item)
        return getattr(self, item)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self) -> tuple[str, ...]:
        return (
            "tag_name",
            "name",
            "body",
            "published_at",
            "asset_id",
            "asset_name",
            "asset_size",
            "asset_api_url",
            "browser_download_url",
            "html_url",
        )

    def __iter__(self) -> Iterator[str]:
        yield from self.keys()

    def __len__(self) -> int:
        return 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag_name": self.tag_name,
            "name": self.name,
            "body": self.body,
            "published_at": self.published_at,
            "asset_id": self.asset_id,
            "asset_name": self.asset_name,
            "asset_size": self.asset_size,
            "asset_api_url": self.asset_api_url,
            "browser_download_url": self.browser_download_url,
            "html_url": self.html_url,
        }


@dataclass(slots=True, frozen=True)
class DriveInfo:
    """Information regarding a connected storage volume / USB drive."""

    root: str
    display_label: str
    fs_type: str

    def __getitem__(self, item: str | int) -> Any:
        if isinstance(item, int):
            return (self.root, self.display_label, self.fs_type)[item]
        if not isinstance(item, str) or not hasattr(self, item):
            raise KeyError(item)
        return getattr(self, item)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self) -> tuple[str, ...]:
        return ("root", "display_label", "fs_type")

    def __iter__(self) -> Iterator[str]:
        yield self.root
        yield self.display_label
        yield self.fs_type

    def __len__(self) -> int:
        return 3

    def to_dict(self) -> dict[str, Any]:
        return {"root": self.root, "display_label": self.display_label, "fs_type": self.fs_type}


@dataclass(slots=True)
class AppSettings:
    """Strongly-typed application settings and user preferences."""

    library_folder: str = ""
    volume: int = 80
    repeat_playlist: bool = False
    soften_clip: bool = True
    fade_choice: str = "1.5s (Standard)"
    even_volume: bool = True
    auto_level_playback: bool = False
    geometry: str | None = None
    github_update_token: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "library_folder": self.library_folder,
            "volume": self.volume,
            "repeat_playlist": self.repeat_playlist,
            "soften_clip": self.soften_clip,
            "fade_choice": self.fade_choice,
            "even_volume": self.even_volume,
            "auto_level_playback": self.auto_level_playback,
            "geometry": self.geometry,
            "github_update_token": self.github_update_token,
        }
        if self.extra:
            data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppSettings:
        known_keys = {
            "library_folder",
            "volume",
            "repeat_playlist",
            "soften_clip",
            "fade_choice",
            "even_volume",
            "auto_level_playback",
            "geometry",
            "github_update_token",
        }
        extra = {k: v for k, v in data.items() if k not in known_keys}
        return cls(
            library_folder=str(data.get("library_folder") or ""),
            volume=int(data.get("volume", 80)),
            repeat_playlist=bool(data.get("repeat_playlist", False)),
            soften_clip=bool(data.get("soften_clip", True)),
            fade_choice=str(data.get("fade_choice", "1.5s (Standard)")),
            even_volume=bool(data.get("even_volume", True)),
            auto_level_playback=bool(data.get("auto_level_playback", False)),
            geometry=data.get("geometry"),
            github_update_token=str(data.get("github_update_token") or ""),
            extra=extra,
        )

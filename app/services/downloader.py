"""Background yt-dlp downloader worker with search routing, progress reporting, and cancellation."""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from app.config import BASE_PATH, PREVIEW_CACHE_DIR, YOUTUBE_RE, YT_CACHE_DIR, ffmpeg_path, format_time, log_error
from app.core.file_utils import copy_file_atomic, unused_path
from app.models import SearchResult

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str, str, bool], None]


@functools.lru_cache(maxsize=1)
def find_deno_runtime() -> str | None:
    """Locate the Deno JavaScript runtime yt-dlp needs to solve YouTube's player challenges.

    Packaged builds ship ``deno.exe`` next to ``ffmpeg.exe``; source runs use the ``deno``
    PyPI package installed by the ``yt-dlp[deno]`` extra.
    """
    bundled = Path(BASE_PATH) / ("deno.exe" if os.name == "nt" else "deno")
    if bundled.is_file():
        return str(bundled)
    try:
        import deno

        return str(deno.find_deno_bin())
    except (ImportError, FileNotFoundError):
        return None


def _js_runtime_opts() -> dict[str, Any]:
    """yt-dlp options enabling the Deno runtime when available (yt-dlp falls back to PATH lookup)."""
    deno_bin = find_deno_runtime()
    return {"js_runtimes": {"deno": {"path": deno_bin}}} if deno_bin else {}


# Words that mark a bracketed part of a YouTube title as video noise rather than part of the song name,
# e.g. "(Official Music Video)", "[HD]", "(Lyrics)", "[Remastered 2009]". "(Live)", "(Acoustic)" stay.
_NOISE_WORDS = (
    r"official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|8k|1080p|720p|remaster(?:ed)?|"
    r"m/?v|explicit|clean version|high quality|full song|with lyrics"
)
_NOISE_BRACKET_RE = re.compile(rf"\s*[\(\[【]([^\)\]】]*\b(?:{_NOISE_WORDS})\b[^\)\]】]*)[\)\]】]", re.IGNORECASE)
_NOISE_SUFFIX_RE = re.compile(
    r"\s*[|\-–—]\s*(?:official\b.*|lyrics?|lyric video|audio|video|hd|hq|4k)\s*$", re.IGNORECASE
)


_ARTIST_SEPARATOR_RE = re.compile(r"\s[-–—]\s")


def clean_song_title(title: str) -> str:
    """Drop video noise from a YouTube title: "Song (Official Video) [4K]" -> "Song"."""
    cleaned = _NOISE_BRACKET_RE.sub("", title or "")
    cleaned = _NOISE_SUFFIX_RE.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -|–—")
    return cleaned or (title or "").strip()


class TitleCleanerPP(yt_dlp.postprocessor.PostProcessor):  # type: ignore[misc]
    """yt-dlp pre-process step: clean the title before it becomes the file name and the MP3 tags.

    When YouTube gives no artist (most music videos), an "Artist - Song" title is split so the song
    shows as "Song — Artist" in the Library instead of carrying the channel name as its artist.
    """

    def run(self, info: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        title = info.get("title")
        if isinstance(title, str):
            info["title"] = clean_song_title(title)
        if isinstance(info.get("track"), str):
            info["track"] = clean_song_title(info["track"])
        cleaned = str(info.get("title") or "")
        parts = _ARTIST_SEPARATOR_RE.split(cleaned, maxsplit=1)
        if not info.get("artist") and not info.get("artists") and not info.get("track") and len(parts) == 2:
            artist, track = parts[0].strip(), parts[1].strip()
            if artist and track:
                info["artist"] = artist
                info["track"] = track
        return [], info


def resolve_download_query(query: str) -> tuple[str, bool]:
    """Detect whether input is a direct URL or a search phrase."""
    q = query.strip()
    if not YOUTUBE_RE.search(q) and not q.lower().startswith(("http://", "https://")):
        return f"ytsearch1:{q}", True
    return q, False


def _url_list_and_video(url: str) -> tuple[str | None, str | None, str]:
    """Return (playlist id, video id, path) found in a YouTube URL (None when absent)."""
    parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
    params = urllib.parse.parse_qs(parsed.query)
    list_id = (params.get("list") or [""])[0] or None
    video_id = (params.get("v") or [""])[0] or None
    path = parsed.path or ""
    if parsed.netloc.lower().endswith("youtu.be") and path.strip("/"):
        video_id = path.strip("/").split("/")[0]
    elif path.startswith(("/shorts/", "/live/")):
        video_id = path.split("/")[2] or video_id
    return list_id, video_id, path


def has_video_id(url: str) -> bool:
    """True when a YouTube URL points at one particular video (even if it also names a playlist)."""
    return bool(_url_list_and_video(url.strip())[1])


def is_playlist_url(query: str) -> bool:
    """Check if query is a YouTube URL that should be offered as a playlist download.

    A "Mix" (``list=RD...``) is YouTube's endless auto-generated radio list: links copied while
    listening to a song often carry one, and they are treated as that single song.
    """
    q = query.strip()
    if not (YOUTUBE_RE.search(q) or q.lower().startswith(("http://", "https://"))):
        return False
    list_id, video_id, path = _url_list_and_video(q)
    if not list_id:
        return "/playlist" in path
    # RDCLAK... are fixed, curated YouTube Music playlists rather than endless mixes.
    is_mix = list_id.startswith("RD") and not list_id.startswith("RDCLAK")
    return not (is_mix and video_id)


def probe_playlist_info(url: str) -> dict[str, Any] | None:
    """Read a playlist's title and songs without downloading; None when it has no songs.

    Raises (``yt_dlp.utils.DownloadError``, ``OSError``) when YouTube could not be reached or read,
    so the caller can say why instead of calling the playlist empty.
    """
    ydl_opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": YT_CACHE_DIR,
        "socket_timeout": 15,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return None
    valid_entries = [e for e in list(info.get("entries") or []) if e]
    if not valid_entries:
        return None
    return {
        "title": info.get("title") or "YouTube Playlist",
        "count": len(valid_entries),
        "entries": valid_entries,
    }


def search_youtube(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search YouTube for matching tracks without downloading; an empty list means no matches.

    Raises (``yt_dlp.utils.DownloadError``, ``OSError``) when the search itself failed: being offline
    or a YouTube change must not be reported as "no matches".
    """
    q = query.strip()
    if not q:
        return []
    search_query = f"ytsearch{max_results}:{q}"
    ydl_opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": YT_CACHE_DIR,
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(search_query, download=False)
    entries = list(info.get("entries") or []) if (info and isinstance(info, dict)) else []

    results = []
    for entry in entries:
        if not entry:
            continue
        # Filter out active non-downloadable live streams
        if entry.get("is_live") or entry.get("live_status") == "is_live":
            continue
        vid_id = entry.get("id")
        url = entry.get("url")
        if not url or not url.startswith("http"):
            if vid_id:
                url = f"https://www.youtube.com/watch?v={vid_id}"
            else:
                continue
        dur_sec = entry.get("duration")
        dur_str = format_time(dur_sec) if (dur_sec and dur_sec > 0) else "--:--"
        results.append(
            SearchResult(
                id=vid_id or "",
                title=entry.get("title") or "Unknown Title",
                uploader=entry.get("uploader") or entry.get("channel") or "Unknown Artist",
                duration_sec=dur_sec,
                duration_str=dur_str,
                url=url,
            )
        )
    return results


def search_youtube_worker(
    query: str,
    max_results: int,
    cancel_event: threading.Event | None,
    on_success: Callable[[list[SearchResult]], None],
    on_error: Callable[[str], None],
) -> None:
    """Execute search in a worker thread."""
    try:
        if cancel_event and cancel_event.is_set():
            return
        results = search_youtube(query, max_results=max_results)
        if cancel_event and cancel_event.is_set():
            return
        on_success(results)
    except Exception as e:
        log_error(f"search_youtube_worker: {e}")
        on_error(str(e))


def _cleanup_temp_preview(out_base: str) -> None:
    """Remove temporary files created during preview extraction."""
    try:
        parent = os.path.dirname(out_base)
        prefix = os.path.basename(out_base)
        if os.path.exists(parent):
            for f in os.listdir(parent):
                if f.startswith(prefix):
                    try:
                        os.remove(os.path.join(parent, f))
                    except Exception:
                        pass
    except Exception:
        pass


def fetch_preview_audio(url: str, max_seconds: int = 30, cancel_event: threading.Event | None = None) -> str | None:
    """Fetch or generate a short audio preview MP3 for a YouTube URL, caching in PREVIEW_CACHE_DIR."""
    if not url:
        return None

    os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
    url_hash = hashlib.md5(url.encode("utf-8", errors="ignore"), usedforsecurity=False).hexdigest()[:16]
    preview_file = os.path.join(PREVIEW_CACHE_DIR, f"yt_prev_{url_hash}_{max_seconds}s.mp3")

    if os.path.exists(preview_file) and os.path.getsize(preview_file) > 1024:
        return preview_file

    if cancel_event and cancel_event.is_set():
        return None

    out_base = os.path.join(PREVIEW_CACHE_DIR, f"temp_prev_{url_hash}_{int(time.time() * 1000)}")
    outtmpl = out_base + ".%(ext)s"

    def _hook(_d: dict[str, Any]) -> None:
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Preview cancelled")

    ffmpeg_dir = os.path.dirname(os.path.abspath(ffmpeg_path)) or os.path.abspath(ffmpeg_path)

    ydl_opts = {
        "format": "ba/b",
        "outtmpl": outtmpl,
        "ffmpeg_location": ffmpeg_dir,
        "download_ranges": yt_dlp.utils.download_range_func([], [[0, max_seconds]]),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
        "noplaylist": True,
        "windowsfilenames": True,
        "cachedir": YT_CACHE_DIR,
        "socket_timeout": 15,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        **_js_runtime_opts(),
        "progress_hooks": [_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Preview cancelled")

        expected_mp3 = out_base + ".mp3"
        if os.path.exists(expected_mp3) and os.path.getsize(expected_mp3) > 0:
            try:
                if os.path.exists(preview_file):
                    os.remove(preview_file)
                os.replace(expected_mp3, preview_file)
                return preview_file
            except Exception:
                return expected_mp3

        # Check if another mp3 filename matching out_base was produced
        for f in os.listdir(PREVIEW_CACHE_DIR):
            if f.startswith(os.path.basename(out_base)) and f.endswith(".mp3"):
                found_mp3 = os.path.join(PREVIEW_CACHE_DIR, f)
                try:
                    os.replace(found_mp3, preview_file)
                    return preview_file
                except Exception:
                    return found_mp3

        return None
    except DownloadCancelled:
        _cleanup_temp_preview(out_base)
        return None
    except Exception as e:
        _cleanup_temp_preview(out_base)
        raise e


def fetch_preview_worker(
    url: str,
    max_seconds: int = 15,
    cancel_event: threading.Event | None = None,
    on_success: Callable[[str], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> None:
    """Execute preview fetch in a background thread."""
    try:
        if cancel_event and cancel_event.is_set():
            return
        preview_path = fetch_preview_audio(url, max_seconds=max_seconds, cancel_event=cancel_event)
        if cancel_event and cancel_event.is_set():
            return
        if preview_path and os.path.exists(preview_path):
            if on_success:
                on_success(preview_path)
        elif on_error:
            on_error("Preview file could not be generated.")
    except DownloadCancelled:
        pass
    except Exception as e:
        log_error(f"fetch_preview_worker: {e}")
        if on_error:
            on_error(str(e))


def _finished_mp3(work_dir: Path) -> Path | None:
    """The MP3 a download left in its work folder (the newest, should there be more than one)."""
    mp3s = sorted(work_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    return mp3s[0] if mp3s else None


def download_audio_worker(
    target_url: str,
    library_folder: str,
    cancel_event: threading.Event,
    on_progress: ProgressCallback,
    on_success: Callable[[str], None],
    on_cancelled: Callable[[], None],
    on_error: Callable[[str], None],
) -> None:
    """Download one song as an MP3 into the Library on a worker thread.

    yt-dlp works in a folder of its own, and the finished MP3 is then copied into the Library
    under a free name ("Song (2).mp3" when "Song.mp3" is there). Downloading straight into the
    Library would replace a song with the same title: yt-dlp's MP3 conversion overwrites it.
    """
    library = Path(library_folder)
    try:
        library.mkdir(parents=True, exist_ok=True)
        downloads_dir = Path(YT_CACHE_DIR) / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="song-", dir=downloads_dir))
    except OSError as err:
        logger.error("download_audio_worker could not prepare its folders: %s", err)
        on_error(str(err))
        return

    last_ui_time = [0.0]

    def _hook(d: dict[str, Any]) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled("Stopped by user")
        status = d.get("status")
        if status == "downloading":
            now = time.monotonic()
            if now - last_ui_time[0] < 0.06:
                return
            last_ui_time[0] = now

            pct = 0.0
            downloaded = d.get("downloaded_bytes")
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if downloaded and total:
                pct = (float(downloaded) / float(total)) * 100.0
            else:
                pct_raw = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_percent_str") or "0%")).replace("%", "").strip()
                try:
                    pct = float(pct_raw)
                except ValueError:
                    pct = 0.0
            speed = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_speed_str") or "")).strip()
            eta = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_eta_str") or "")).strip()
            on_progress(pct, speed, eta, False)
        elif status == "finished":
            on_progress(100.0, "", "", True)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(work_dir / "%(title).180B.%(ext)s"),
        "ffmpeg_location": os.path.dirname(os.path.abspath(ffmpeg_path)) or os.path.abspath(ffmpeg_path),
        "writethumbnail": True,
        "postprocessors": [
            # VBR V2 (~190 kbps): transparent for YouTube's ~130-160 kbps sources without 320k bloat.
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "2"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ],
        "noplaylist": True,
        "windowsfilenames": True,
        "cachedir": YT_CACHE_DIR,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        **_js_runtime_opts(),
        "progress_hooks": [_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.add_post_processor(TitleCleanerPP(), when="pre_process")
            ydl.extract_info(target_url, download=True)
        if cancel_event.is_set():
            raise DownloadCancelled("Stopped by user")
        song = _finished_mp3(work_dir)
        if song is None:
            raise FileNotFoundError("Download finished but the converted MP3 file could not be found.")
        dest = unused_path(library / song.name)
        copy_file_atomic(song, dest)
        on_success(dest.name)
    except DownloadCancelled:
        on_cancelled()
    except Exception as e:  # last-resort guard: a worker must always report back to the window
        logger.error("download_audio_worker: %s", e)
        on_error(str(e))
    finally:
        try:
            shutil.rmtree(work_dir)
        except OSError as err:
            logger.warning("Could not remove the download folder %s: %s", work_dir, err)


def download_playlist_worker(
    entries: list[dict[str, Any]],
    library_folder: str,
    cancel_event: threading.Event,
    on_track_start: Callable[[int, int, str], None] | None = None,
    on_track_progress: Callable[[int, int, float, str, str], None] | None = None,
    on_track_finished: Callable[[int, int, str], None] | None = None,
    on_batch_complete: Callable[[list[str], int], None] | None = None,
    on_cancelled: Callable[[], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    on_track_failed: Callable[[int, int, str, str], None] | None = None,
) -> None:
    """Sequentially download each track in a playlist with per-track and overall progress reporting.

    A track that cannot be downloaded is reported through ``on_track_failed(idx, total, title, error)``
    and the batch continues with the next track.
    """
    os.makedirs(library_folder, exist_ok=True)
    os.makedirs(YT_CACHE_DIR, exist_ok=True)
    total_tracks = len(entries)
    downloaded_files: list[str] = []

    try:
        for idx, entry in enumerate(entries, 1):
            if cancel_event.is_set():
                if on_cancelled:
                    on_cancelled()
                return

            vid_id = entry.get("id")
            url = entry.get("url") or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else None)
            track_title = entry.get("title") or f"Track {idx}"
            if not url:
                if on_track_failed:
                    on_track_failed(idx, total_tracks, track_title, "This playlist entry has no video link.")
                continue

            if on_track_start:
                on_track_start(idx, total_tracks, track_title)

            track_file: list[str | None] = [None]
            track_error: list[str | None] = [None]

            def _prog(pct: float, spd: str, eta: str, _fin: bool, current_idx: int = idx) -> None:
                if on_track_progress:
                    on_track_progress(current_idx, total_tracks, pct, spd, eta)

            def _succ(fname: str, tf: list[str | None] = track_file) -> None:
                tf[0] = fname

            def _canc() -> None:
                pass

            def _fail(err: str, current_idx: int = idx, te: list[str | None] = track_error) -> None:
                log_error(f"download_playlist_worker track {current_idx} failed: {err}")
                te[0] = err

            download_audio_worker(url, library_folder, cancel_event, _prog, _succ, _canc, _fail)

            if cancel_event.is_set():
                if on_cancelled:
                    on_cancelled()
                return

            finished_file = track_file[0]
            if finished_file:
                downloaded_files.append(finished_file)
                if on_track_finished:
                    on_track_finished(idx, total_tracks, finished_file)
            elif on_track_failed:
                on_track_failed(idx, total_tracks, track_title, track_error[0] or "Unknown error")

        if on_batch_complete:
            on_batch_complete(downloaded_files, total_tracks)
    except Exception as e:
        log_error(f"download_playlist_worker: {e}")
        if on_error:
            on_error(str(e))

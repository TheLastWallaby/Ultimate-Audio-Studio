"""Background yt-dlp downloader worker with search routing, progress reporting, and cancellation."""

import hashlib
import os
import re
import threading
import time
from collections.abc import Callable

import yt_dlp

from app.config import PREVIEW_CACHE_DIR, YOUTUBE_RE, YT_CACHE_DIR, ffmpeg_path, format_time, log_error
from app.models import SearchResult

try:
    from yt_dlp.utils import DownloadCancelled
except Exception:

    class DownloadCancelled(Exception):
        pass


def resolve_download_query(query):
    """Detect whether input is a direct URL or a search phrase."""
    q = query.strip()
    if not YOUTUBE_RE.search(q) and not q.lower().startswith(("http://", "https://")):
        return f"ytsearch1:{q}", True
    return q, False


def is_playlist_url(query):
    """Check if query is a YouTube URL referencing a playlist."""
    q = query.strip()
    if not (YOUTUBE_RE.search(q) or q.lower().startswith(("http://", "https://"))):
        return False
    return "list=" in q or "/playlist" in q


def probe_playlist_info(url):
    """Retrieve playlist title and item count using flat metadata extraction."""
    ydl_opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": YT_CACHE_DIR,
        "socket_timeout": 15,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None
            entries = list(info.get("entries") or [])
            valid_entries = [e for e in entries if e]
            if not valid_entries:
                return None
            title = info.get("title") or "YouTube Playlist"
            return {
                "title": title,
                "count": len(valid_entries),
                "entries": valid_entries,
            }
    except Exception as e:
        log_error(f"probe_playlist_info: {e}")
        return None


def search_youtube(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search YouTube for matching tracks using yt-dlp metadata extraction without downloading."""
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
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = list(info.get("entries") or []) if (info and isinstance(info, dict)) else []
    except Exception as e:
        log_error(f"search_youtube query '{query}': {e}")
        return []

    results = []
    for e in entries:
        if not e:
            continue
        # Filter out active non-downloadable live streams
        if e.get("is_live") or e.get("live_status") == "is_live":
            continue
        vid_id = e.get("id")
        url = e.get("url")
        if not url or not url.startswith("http"):
            if vid_id:
                url = f"https://www.youtube.com/watch?v={vid_id}"
            else:
                continue
        dur_sec = e.get("duration")
        dur_str = format_time(dur_sec) if (dur_sec and dur_sec > 0) else "--:--"
        results.append(
            SearchResult(
                id=vid_id or "",
                title=e.get("title") or "Unknown Title",
                uploader=e.get("uploader") or e.get("channel") or "Unknown Artist",
                duration_sec=dur_sec,
                duration_str=dur_str,
                url=url,
            )
        )
    return results


def search_youtube_worker(query, max_results, cancel_event, on_success, on_error):
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


def _cleanup_temp_preview(out_base):
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


def fetch_preview_audio(url, max_seconds=30, cancel_event=None):
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

    def _hook(d):
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
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "mweb", "ios"],
            }
        },
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
            on_success(preview_path)
        else:
            on_error("Preview file could not be generated.")
    except DownloadCancelled:
        pass
    except Exception as e:
        log_error(f"fetch_preview_worker: {e}")
        on_error(str(e))


def cleanup_partial_downloads(library_folder):
    """Remove stranded .part and .temp files from incomplete downloads."""
    try:
        if os.path.exists(library_folder):
            for f in os.listdir(library_folder):
                if f.endswith((".part", ".ytdl", ".temp")) or ".temp." in f:
                    f_path = os.path.join(library_folder, f)
                    try:
                        if os.path.isfile(f_path):
                            os.remove(f_path)
                    except Exception:
                        pass
    except Exception:
        pass


def download_audio_worker(target_url, library_folder, cancel_event, on_progress, on_success, on_cancelled, on_error):
    """Execute download in a worker thread using yt-dlp."""
    os.makedirs(library_folder, exist_ok=True)
    os.makedirs(YT_CACHE_DIR, exist_ok=True)
    before_files = set(os.listdir(library_folder)) if os.path.exists(library_folder) else set()

    last_ui_time = [0.0]

    def _hook(d):
        if cancel_event.is_set():
            raise DownloadCancelled("Stopped by user")
        status = d.get("status")
        if status == "downloading":
            now = time.time()
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
                except Exception:
                    pct = 0.0
            speed = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_speed_str") or "")).strip()
            eta = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_eta_str") or "")).strip()
            on_progress(pct, speed, eta, False)
        elif status == "finished":
            on_progress(100.0, "", "", True)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(library_folder, "%(title).180B.%(ext)s"),
        "ffmpeg_location": os.path.dirname(os.path.abspath(ffmpeg_path)) or os.path.abspath(ffmpeg_path),
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
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
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "mweb", "ios"],
            }
        },
        "progress_hooks": [_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=True)
            if cancel_event.is_set():
                raise DownloadCancelled("Stopped by user")
            entries = list(info.get("entries") or []) if (info and isinstance(info, dict)) else []
            if entries:
                entry = entries[0]
                prepared = (
                    entry.get("requested_downloads") and entry["requested_downloads"][0].get("filepath")
                ) or ydl.prepare_filename(entry)
            elif info and info.get("requested_downloads"):
                prepared = info["requested_downloads"][0].get("filepath") or info["requested_downloads"][0].get(
                    "filename"
                )
            else:
                prepared = ydl.prepare_filename(info) if info else None

        final_name = None
        if prepared:
            base = os.path.splitext(os.path.basename(prepared))[0]
            candidate = base + ".mp3"
            if os.path.exists(os.path.join(library_folder, candidate)):
                final_name = candidate

        # If candidate wasn't directly matched, detect newly created or most recent MP3
        if not final_name and os.path.exists(library_folder):
            after_files = set(os.listdir(library_folder))
            new_mp3s = [f for f in (after_files - before_files) if f.lower().endswith(".mp3")]
            if new_mp3s:
                new_mp3s.sort(key=lambda x: os.path.getmtime(os.path.join(library_folder, x)), reverse=True)
                final_name = new_mp3s[0]
            else:
                all_mp3s = [f for f in os.listdir(library_folder) if f.lower().endswith(".mp3")]
                if all_mp3s:
                    all_mp3s.sort(key=lambda x: os.path.getmtime(os.path.join(library_folder, x)), reverse=True)
                    final_name = all_mp3s[0]

        on_success(final_name)
    except DownloadCancelled:
        cleanup_partial_downloads(library_folder)
        on_cancelled()
    except Exception as e:
        cleanup_partial_downloads(library_folder)
        log_error(f"download_audio_worker: {e}")
        on_error(str(e))


def download_playlist_worker(
    entries,
    library_folder,
    cancel_event,
    on_track_start=None,
    on_track_progress=None,
    on_track_finished=None,
    on_batch_complete=None,
    on_cancelled=None,
    on_error=None,
):
    """Sequentially download each track in a playlist with per-track and overall progress reporting."""
    os.makedirs(library_folder, exist_ok=True)
    os.makedirs(YT_CACHE_DIR, exist_ok=True)
    total_tracks = len(entries)
    downloaded_files = []

    try:
        for idx, entry in enumerate(entries, 1):
            if cancel_event.is_set():
                if on_cancelled:
                    on_cancelled()
                return

            vid_id = entry.get("id")
            url = entry.get("url") or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else None)
            if not url:
                continue

            track_title = entry.get("title") or f"Track {idx}"
            if on_track_start:
                on_track_start(idx, total_tracks, track_title)

            track_file = [None]

            def _prog(pct, spd, eta, _fin, current_idx=idx):
                if on_track_progress:
                    on_track_progress(current_idx, total_tracks, pct, spd, eta)

            def _succ(fname, tf=track_file):
                tf[0] = fname

            def _canc():
                pass

            def _fail(err, current_idx=idx):
                log_error(f"download_playlist_worker track {current_idx} failed: {err}")

            download_audio_worker(url, library_folder, cancel_event, _prog, _succ, _canc, _fail)

            if cancel_event.is_set():
                if on_cancelled:
                    on_cancelled()
                return

            if track_file[0]:
                downloaded_files.append(track_file[0])
                if on_track_finished:
                    on_track_finished(idx, total_tracks, track_file[0])

        if on_batch_complete:
            on_batch_complete(downloaded_files, total_tracks)
    except Exception as e:
        log_error(f"download_playlist_worker: {e}")
        if on_error:
            on_error(str(e))

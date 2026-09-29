"""Download controller managing YouTube download workers, search dialogs, and progress tracking."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from app.core.task_manager import task_mgr
from app.models import SearchResult
from app.services.downloader import (
    cleanup_partial_downloads,
    download_audio_worker,
    download_playlist_worker,
    has_video_id,
    is_playlist_url,
    probe_playlist_info,
    resolve_download_query,
    search_youtube_worker,
)


class DownloadController:
    """Manages YouTube download lifecycle, background worker threads, search queries, and cancellation.

    Every search or download is a *job* with its own cancel event. Stopping a job sets only that
    event, so starting the next job can never revive a worker that is still winding down, and every
    callback from a job that has since been replaced is dropped (progress, results, the
    ``is_downloading`` flag, and partial-file cleanup that could hit the new job's files).
    """

    def __init__(self, app: object) -> None:
        self.app = app
        self.is_downloading = False
        self._download_cancel = threading.Event()

    @property
    def cancel_event(self) -> threading.Event:
        """Return the cancel event of the current (most recent) job."""
        return self._download_cancel

    def cancel(self) -> None:
        """Cancel the active download or search worker."""
        self._download_cancel.set()

    def reset_cancel(self) -> threading.Event:
        """Start a new job: give it a fresh cancel event (earlier jobs keep their own, possibly set, event)."""
        self._download_cancel = threading.Event()
        return self._download_cancel

    def _is_current(self, job: threading.Event) -> bool:
        return job is self._download_cancel

    def _for_job(self, job: threading.Event, callback: Callable[..., None]) -> Callable[..., None]:
        """Wrap a worker callback so it only runs while ``job`` is still the current job."""

        def _guarded(*args: Any) -> None:
            if self._is_current(job):
                callback(*args)

        return _guarded

    def resolve_query(self, query: str) -> tuple[str, bool]:
        """Resolve raw query or direct URL."""
        return resolve_download_query(query)

    def is_playlist(self, url: str) -> bool:
        """Check if URL points to a YouTube playlist (a Mix link with a video counts as one song)."""
        return is_playlist_url(url)

    def has_video(self, url: str) -> bool:
        """Check if URL names a single video (so "just this song" can be offered)."""
        return has_video_id(url)

    def probe_playlist(self, url: str, on_done: Callable[[dict[str, Any] | None], None]) -> None:
        """Read a playlist's title and songs in the background, as a job Stop can cancel.

        ``on_done(info)`` gets ``{"title", "count", "entries"}`` or None when it could not be read.
        """
        job = self.reset_cancel()

        def _worker() -> None:
            info = probe_playlist_info(url)
            if not job.is_set():
                self._for_job(job, on_done)(info)

        task_mgr.submit_task(_worker)

    def cleanup_partial(self, library_folder: str) -> None:
        """Clean up incomplete or temporary download artifacts."""
        cleanup_partial_downloads(library_folder)

    def start_search(
        self,
        query: str,
        max_results: int,
        on_success: Callable[[list[SearchResult]], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Start YouTube search worker thread (a stopped search never delivers its results)."""
        job = self.reset_cancel()
        task_mgr.submit_task(search_youtube_worker, query, max_results, job, on_success, on_error)

    def start_download(
        self,
        target_url: str,
        library_folder: str,
        on_progress: Callable[[float, str, str, bool], None],
        on_success: Callable[[str], None],
        on_cancelled: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Execute audio download in background thread."""
        self.is_downloading = True
        job = self.reset_cancel()

        def _worker_success(fname: str) -> None:
            self.is_downloading = False
            on_success(fname)

        def _worker_cancelled() -> None:
            self.is_downloading = False
            cleanup_partial_downloads(library_folder)
            on_cancelled()

        def _worker_error(err: str) -> None:
            self.is_downloading = False
            cleanup_partial_downloads(library_folder)
            on_error(err)

        task_mgr.submit_task(
            download_audio_worker,
            target_url,
            library_folder,
            job,
            self._for_job(job, on_progress),
            self._for_job(job, _worker_success),
            self._for_job(job, _worker_cancelled),
            self._for_job(job, _worker_error),
        )

    def start_playlist(
        self,
        url: str,
        library_folder: str,
        on_probed: Callable[[str, int], None],
        on_track_start: Callable[[int, int, str], None],
        on_track_progress: Callable[[int, int, float, str, str], None],
        on_track_finished: Callable[[int, int, str], None],
        on_track_failed: Callable[[int, int, str, str], None],
        on_batch_complete: Callable[[list[str], int], None],
        on_cancelled: Callable[[], None],
        on_error: Callable[[str], None],
        probed: dict[str, Any] | None = None,
    ) -> None:
        """Scan a YouTube playlist, then download every track, as one cancellable job.

        The scan belongs to the job, so Stop works while scanning and ``is_downloading`` is already
        true (an update restart is refused) before the first track starts. Pass ``probed`` (from
        :meth:`probe_playlist`) to skip scanning again.
        """
        self.is_downloading = True
        job = self.reset_cancel()

        def _cancelled() -> None:
            self.is_downloading = False
            cleanup_partial_downloads(library_folder)
            on_cancelled()

        def _error(err: str) -> None:
            self.is_downloading = False
            cleanup_partial_downloads(library_folder)
            on_error(err)

        def _batch_complete(downloaded_files: list[str], total: int) -> None:
            self.is_downloading = False
            on_batch_complete(downloaded_files, total)

        def _worker() -> None:
            info = probed if probed is not None else probe_playlist_info(url)
            if job.is_set():
                self._for_job(job, _cancelled)()
                return
            if not info or not info.get("entries"):
                self._for_job(job, _error)("Could not find any downloadable tracks in playlist.")
                return
            entries = info["entries"]
            self._for_job(job, on_probed)(str(info.get("title") or "Playlist"), len(entries))
            download_playlist_worker(
                entries,
                library_folder,
                job,
                on_track_start=self._for_job(job, on_track_start),
                on_track_progress=self._for_job(job, on_track_progress),
                on_track_finished=self._for_job(job, on_track_finished),
                on_batch_complete=self._for_job(job, _batch_complete),
                on_cancelled=self._for_job(job, _cancelled),
                on_error=self._for_job(job, _error),
                on_track_failed=self._for_job(job, on_track_failed),
            )

        task_mgr.submit_task(_worker)

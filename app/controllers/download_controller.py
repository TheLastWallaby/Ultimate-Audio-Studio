"""Download controller managing YouTube download workers, search dialogs, and progress tracking."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from app.core.task_manager import network_task_mgr as task_mgr
from app.models import SearchResult
from app.services.downloader import (
    download_audio_worker,
    download_playlist_worker,
    has_video_id,
    is_playlist_url,
    probe_playlist_info,
    resolve_download_query,
    search_youtube_worker,
)

logger = logging.getLogger(__name__)


class DownloadController:
    """Manages YouTube download lifecycle, background worker threads, search queries, and cancellation.

    Every search or download is a *job* with its own cancel event. Stopping a job sets only that
    event, so starting the next job can never revive a worker that is still winding down, and every
    callback from a job that has since been replaced is dropped (progress, results, the
    ``is_downloading`` flag, and partial-file cleanup that could hit the new job's files). Starting
    a job stops the one it replaces: a job that can no longer report back must not keep running.
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
        """Start a new job: stop the one it replaces and give the new one a fresh cancel event.

        A replaced job can no longer report back, so it cannot clear ``is_downloading`` either: left
        set, the app would warn "still downloading" at every close and keep the PC awake for good.
        Left running, it would go on downloading with no Stop button and no word when it ends.
        """
        self._download_cancel.set()
        self.is_downloading = False
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

    def probe_playlist(self, url: str, on_done: Callable[[dict[str, Any] | None, str | None], None]) -> None:
        """Read a playlist's title and songs in the background, as a job Stop can cancel.

        ``on_done(info, error)`` gets ``{"title", "count", "entries"}`` (None when the playlist has no
        songs), or None and the reason when YouTube could not be reached or read.
        """
        job = self.reset_cancel()

        def _worker() -> None:
            info: dict[str, Any] | None = None
            error: str | None = None
            try:
                info = probe_playlist_info(url)
            except Exception as err:  # last-resort guard: yt-dlp raises many kinds; the window must hear back
                logger.warning("Reading the playlist %s failed: %s", url, err)
                error = str(err)
            if not job.is_set():
                self._for_job(job, on_done)(info, error)

        task_mgr.submit_task(_worker)

    def start_search(
        self,
        query: str,
        max_results: int,
        on_success: Callable[[list[SearchResult]], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Start YouTube search worker thread (a stopped or replaced search never delivers its results)."""
        job = self.reset_cancel()
        task_mgr.submit_task(
            search_youtube_worker,
            query,
            max_results,
            job,
            self._for_job(job, on_success),
            self._for_job(job, on_error),
        )

    def start_download(
        self,
        target_url: str,
        library_folder: str,
        on_progress: Callable[[float, str, str, bool], None],
        on_success: Callable[[str], None],
        on_cancelled: Callable[[], None],
        on_error: Callable[[str], None],
        on_duplicate: Callable[[str], None] | None = None,
    ) -> None:
        """Download one song on a worker; ``on_duplicate(name)`` when it is already in the Library."""
        job = self.reset_cancel()
        self.is_downloading = True

        def _worker_success(fname: str) -> None:
            self.is_downloading = False
            on_success(fname)

        # A download works in a folder of its own, which its worker removes: nothing is left in the
        # Library to clean up after a stop or a failure.
        def _worker_cancelled() -> None:
            self.is_downloading = False
            on_cancelled()

        def _worker_error(err: str) -> None:
            self.is_downloading = False
            on_error(err)

        def _worker_duplicate(name: str) -> None:
            self.is_downloading = False
            if on_duplicate:
                on_duplicate(name)

        task_mgr.submit_task(
            download_audio_worker,
            target_url,
            library_folder,
            job,
            self._for_job(job, on_progress),
            self._for_job(job, _worker_success),
            self._for_job(job, _worker_cancelled),
            self._for_job(job, _worker_error),
            on_duplicate=self._for_job(job, _worker_duplicate) if on_duplicate else None,
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
        on_track_duplicate: Callable[[int, int, str], None] | None = None,
    ) -> None:
        """Scan a YouTube playlist, then download every track, as one cancellable job.

        The scan belongs to the job, so Stop works while scanning and ``is_downloading`` is already
        true (an update restart is refused) before the first track starts. Pass ``probed`` (from
        :meth:`probe_playlist`) to skip scanning again.
        """
        job = self.reset_cancel()
        self.is_downloading = True

        def _cancelled() -> None:
            self.is_downloading = False
            on_cancelled()

        def _error(err: str) -> None:
            self.is_downloading = False
            on_error(err)

        def _batch_complete(downloaded_files: list[str], total: int) -> None:
            self.is_downloading = False
            on_batch_complete(downloaded_files, total)

        def _worker() -> None:
            try:
                info = probed if probed is not None else probe_playlist_info(url)
            except Exception as err:  # last-resort guard: yt-dlp raises many kinds; the window must hear back
                logger.warning("Reading the playlist %s failed: %s", url, err)
                self._for_job(job, _error)(str(err))
                return
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
                on_track_duplicate=self._for_job(job, on_track_duplicate) if on_track_duplicate else None,
            )

        task_mgr.submit_task(_worker)

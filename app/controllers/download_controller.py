"""Download controller managing YouTube download workers, search dialogs, and progress tracking."""

import threading

from app.core.task_manager import task_mgr
from app.services.downloader import (
    cleanup_partial_downloads,
    download_audio_worker,
    download_playlist_worker,
    is_playlist_url,
    probe_playlist_info,
    resolve_download_query,
    search_youtube_worker,
)


class DownloadController:
    """Manages YouTube download lifecycle, background worker threads, search queries, and cancellation."""

    def __init__(self, app):
        self.app = app
        self.is_downloading = False
        self._download_cancel = threading.Event()

    @property
    def cancel_event(self):
        """Return the cancellation threading event."""
        return self._download_cancel

    def cancel(self):
        """Cancel the active download or search worker."""
        self._download_cancel.set()

    def reset_cancel(self):
        """Reset the cancellation event for a new operation."""
        self._download_cancel.clear()

    def resolve_query(self, query):
        """Resolve raw query or direct URL."""
        return resolve_download_query(query)

    def is_playlist(self, url):
        """Check if URL points to a YouTube playlist."""
        return is_playlist_url(url)

    def probe_playlist(self, url):
        """Extract metadata and track entries from a YouTube playlist URL."""
        return probe_playlist_info(url)

    def cleanup_partial(self, library_folder):
        """Clean up incomplete or temporary download artifacts."""
        cleanup_partial_downloads(library_folder)

    def start_search(self, query, max_results, on_success, on_error):
        """Start YouTube search worker thread."""
        self._download_cancel.clear()
        task_mgr.submit_task(
            search_youtube_worker,
            query, max_results, self._download_cancel, on_success, on_error
        )

    def start_download(
        self,
        target_url,
        library_folder,
        on_progress,
        on_success,
        on_cancelled,
        on_error,
    ):
        """Execute audio download in background thread."""
        self.is_downloading = True
        self._download_cancel.clear()

        def _worker_success(fname):
            self.is_downloading = False
            if on_success:
                on_success(fname)

        def _worker_cancelled():
            self.is_downloading = False
            cleanup_partial_downloads(library_folder)
            if on_cancelled:
                on_cancelled()

        def _worker_error(err):
            self.is_downloading = False
            cleanup_partial_downloads(library_folder)
            if on_error:
                on_error(err)

        task_mgr.submit_task(
            download_audio_worker,
            target_url,
            library_folder,
            self._download_cancel,
            on_progress,
            _worker_success,
            _worker_cancelled,
            _worker_error
        )

    def start_playlist_download(
        self,
        entries,
        library_folder,
        on_track_start,
        on_track_progress,
        on_track_finished,
        on_batch_complete,
        on_cancelled,
        on_error,
    ):
        """Execute batch playlist download in background thread."""
        self.is_downloading = True
        self._download_cancel.clear()

        def _batch_complete(downloaded_files, total):
            self.is_downloading = False
            if on_batch_complete:
                on_batch_complete(downloaded_files, total)

        def _cancelled():
            self.is_downloading = False
            cleanup_partial_downloads(library_folder)
            if on_cancelled:
                on_cancelled()

        def _error(err):
            self.is_downloading = False
            cleanup_partial_downloads(library_folder)
            if on_error:
                on_error(err)

        task_mgr.submit_task(
            download_playlist_worker,
            entries,
            library_folder,
            self._download_cancel,
            on_track_start,
            on_track_progress,
            on_track_finished,
            _batch_complete,
            _cancelled,
            _error
        )

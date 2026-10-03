"""Step 1 YouTube box: search, single and playlist downloads, progress and cancel."""

from __future__ import annotations

import os
import tkinter as tk
from typing import Any

from app.config import YOUTUBE_RE, ffmpeg_path
from app.core.errors import friendly_error, is_recognised
from app.models import SearchResult
from app.ui import dialogs
from app.ui.error_dialog import show_friendly_error
from app.ui.features.base import AppBase
from app.ui.search_dialog import SearchChoiceDialog


class DownloadMixin(AppBase):
    """Step 1 YouTube box: search, single and playlist downloads, progress and cancel."""

    def paste_youtube_link(self) -> None:
        try:
            txt = self.root.clipboard_get().strip()
            self.entry_url.delete(0, tk.END)
            self.entry_url.insert(0, txt)
            if YOUTUBE_RE.search(txt):
                self.set_status("YouTube link pasted! Click 'Download MP3' to get the song.")
            else:
                self.set_status("Text pasted into download field.")
        except Exception:
            self.set_status("Clipboard is empty or does not contain text.")

    def _on_url_focus(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        try:
            txt = self.root.clipboard_get().strip()
            if YOUTUBE_RE.search(txt) and not self.entry_url.get().strip():
                self.entry_url.delete(0, tk.END)
                self.entry_url.insert(0, txt)
                self.set_status("Detected YouTube link from clipboard! Click 'Download MP3'.")
        except Exception:
            pass

    def cancel_download(self) -> None:
        # The stopped job may take a moment to wind down; the controller drops anything it reports
        # after a newer job starts, so the Download button can be offered again straight away.
        was_downloading = self.download_ctrl.is_downloading
        self.download_ctrl.cancel()
        self._reset_download_ui()
        self.set_busy(False, "Download stopped." if was_downloading else "Search stopped.")

    def _reset_download_ui(self) -> None:
        """Return the YouTube box to its idle state (Download enabled, Stop disabled, no progress)."""
        self.btn_download.config(text="⬇ Download MP3", state=tk.NORMAL)
        self.btn_cancel_dl.config(state=tk.DISABLED)
        self.prog_download.pack_forget()
        self.lbl_dl_metrics.pack_forget()

    def _update_download_progress(self, pct: float, speed: str = "", eta: str = "", finished: bool = False) -> None:
        if finished:
            self.prog_download["value"] = 100
            self.lbl_dl_metrics.config(text="Converting to MP3 & embedding cover art...")
        else:
            self.prog_download["value"] = pct
            parts = [f"{pct:.1f}%"]
            if speed:
                parts.append(speed)
            if eta:
                parts.append(f"ETA {eta}")
            self.lbl_dl_metrics.config(text=" • ".join(parts))

    def start_download(self) -> None:
        query = self.entry_url.get().strip()
        if not query:
            dialogs.show_warning(self.root, "Nothing to Download", "Type a song name or paste a YouTube link first.")
            return
        if not os.path.exists(ffmpeg_path):
            dialogs.show_warning(self.root, "Cannot Download", "ffmpeg.exe is missing, so audio cannot be converted.")
            return

        target_url, is_search = self.download_ctrl.resolve_query(query)
        if not is_search:
            if self.download_ctrl.is_playlist(target_url):
                self._check_playlist(target_url)
                return
            # Direct URL: download immediately without search dialog
            self._start_download_url(target_url)
            return

        # Search query: fetch matching options first and let user select
        self.btn_download.config(text="Searching...", state=tk.DISABLED)
        self.btn_cancel_dl.config(state=tk.NORMAL)
        self.set_busy(True, f'Searching YouTube for "{query}"...')

        self.download_ctrl.start_search(
            query,
            6,
            on_success=lambda results: self._safe_after(0, self._handle_search_results, query, results),
            on_error=lambda err: self._safe_after(0, self._handle_search_error, err),
        )

    def _check_playlist(self, url: str) -> None:
        """Read the playlist first, so the question can say how many songs it would download."""
        self.btn_download.config(text="Checking...", state=tk.DISABLED)
        self.btn_cancel_dl.config(state=tk.NORMAL)
        self.set_busy(True, "Checking the YouTube playlist...")
        self.download_ctrl.probe_playlist(
            url, on_done=lambda info, error: self._safe_after(0, self._on_playlist_checked, url, info, error)
        )

    def _on_playlist_checked(self, url: str, info: dict[str, Any] | None, error: str | None = None) -> None:
        """Offer the playlist's songs, or explain why it could not be read (offline, YouTube changed...)."""
        self._reset_download_ui()
        self.set_busy(False)
        if error and is_recognised(error):
            # Being offline is not "private or empty": say what is wrong, and look for a fix.
            self.set_status("The YouTube playlist could not be read.", icon="⚠️")
            self._look_for_update_after_failure([error])
            show_friendly_error(self.root, error, "download")
            return
        single_ok = self.download_ctrl.has_video(url)
        if not info or not info.get("entries"):
            if single_ok and dialogs.ask_yes_no(
                self.root,
                "Playlist Could Not Be Read",
                "This link belongs to a YouTube playlist, but its list of songs could not be read "
                "(it may be private or empty).",
                yes="Download just this song",
                no="Cancel",
                icon=dialogs.ICON_WARNING,
            ):
                self._start_download_url(url)
            elif not single_ok:
                show_friendly_error(self.root, "Could not find any downloadable tracks in playlist.", "download")
            return

        count = int(info.get("count") or len(info["entries"]))
        title = str(info.get("title") or "YouTube Playlist")
        note = "\n\nThis may take a while; you can keep using the app meanwhile." if count > 25 else ""
        buttons = [dialogs.DialogButton(f"Download all {count} songs", "all", "primary")]
        if single_ok:
            buttons.append(dialogs.DialogButton("Just this one song", "one"))
        buttons.append(dialogs.DialogButton("Cancel", "cancel"))
        choice = dialogs.ask_choice(
            self.root,
            "YouTube Playlist",
            f'This link is the playlist "{title}" with {count} songs.{note}',
            buttons,
            cancel_value="cancel",
        )
        if choice == "all":
            self._start_playlist_download(url, info)
        elif choice == "one":
            self._start_download_url(url)
        else:
            self.set_status("Ready")

    def _start_playlist_download(self, url: str, probed: dict[str, Any] | None = None) -> None:
        self.btn_download.config(text="Scanning...", state=tk.DISABLED)
        self.btn_cancel_dl.config(state=tk.NORMAL)
        self.set_busy(True, "Scanning playlist tracks...")
        self.prog_download.pack(fill=tk.X, pady=(4, 2))
        self.prog_download["value"] = 0
        self.lbl_dl_metrics.pack(fill=tk.X)
        self.lbl_dl_metrics.config(text="Fetching playlist info...")
        failures: list[tuple[str, str]] = []

        def _on_probed(title: str, total: int) -> None:
            self._safe_after(0, self.set_status, f"Starting batch download of {total} tracks from '{title}'...")

        def _on_start(idx: int, tot: int, track_title: str) -> None:
            def _ui() -> None:
                self.set_status(f"Downloading [{idx}/{tot}]: {track_title}", icon="⬇")
                self.lbl_dl_metrics.config(text=f"Track {idx}/{tot}: {track_title[:35]}")

            self._safe_after(0, _ui)

        def _on_prog(idx: int, tot: int, pct: float, speed: str, eta: str) -> None:
            def _ui() -> None:
                self.prog_download["value"] = ((idx - 1) + (pct / 100.0)) / max(tot, 1) * 100.0
                parts = [f"Track {idx}/{tot}"]
                if speed:
                    parts.append(speed)
                if eta:
                    parts.append(f"ETA {eta}")
                self.lbl_dl_metrics.config(text=" • ".join(parts))

            self._safe_after(0, _ui)

        def _on_fin(_idx: int, _tot: int, fname: str) -> None:
            self._safe_after(0, self.refresh_library, fname)

        def _on_failed(_idx: int, _tot: int, track_title: str, err: str) -> None:
            failures.append((track_title, err))

        self.download_ctrl.start_playlist(
            url,
            self.library_folder,
            on_probed=_on_probed,
            on_track_start=_on_start,
            on_track_progress=_on_prog,
            on_track_finished=_on_fin,
            on_track_failed=_on_failed,
            on_batch_complete=lambda files, tot: self._safe_after(
                0, self._playlist_download_success, files, tot, list(failures)
            ),
            on_cancelled=lambda: self._safe_after(0, self._download_cancelled),
            on_error=lambda err: self._safe_after(0, self._download_error, err),
            probed=probed,
        )

    def _playlist_download_success(
        self, downloaded_files: list[str], total: int, failures: list[tuple[str, str]] | None = None
    ) -> None:
        """Report a finished playlist download truthfully: what was saved and what could not be."""
        self.entry_url.delete(0, tk.END)
        self._reset_download_ui()
        self.set_busy(False)
        self.refresh_library()
        count = len(downloaded_files)
        if not failures:
            self.notify_success(f"Playlist download complete: all {count} songs are now in your Library.")
            return
        self.set_status(f"Playlist download finished: {count} of {total} songs saved to Library.", icon="⚠️")
        self._look_for_update_after_failure([err for _title, err in failures])
        shown = failures[:8]
        lines = [f"• {title[:60]} — {friendly_error(err, 'download').title}" for title, err in shown]
        if len(failures) > len(shown):
            lines.append(f"... and {len(failures) - len(shown)} more.")
        failed_list = "\n".join(lines)
        dialogs.show_warning(
            self.root,
            "Some Songs Could Not Be Downloaded",
            f"{count} of {total} songs were saved to your Library.\n\n"
            f"These {len(failures)} song(s) could not be downloaded:\n{failed_list}\n\n"
            "You can try them again later, or search for a different version of each song.",
        )

    def _handle_search_results(self, query: str, results: list[SearchResult]) -> None:
        self._reset_download_ui()
        self.set_busy(False)
        if not results:
            dialogs.show_info(
                self.root,
                "No Matches Found",
                f'No results found on YouTube for "{query}".\n\n'
                "Try including both the artist and song title, or paste a direct YouTube link.",
            )
            return

        SearchChoiceDialog(
            self.root,
            query,
            results,
            on_select=lambda item: self._start_download_url(item["url"], item.get("title")),
            on_cancel=lambda: self.set_status("Ready"),
            audio_engine=self.audio_engine,
            on_preview_play=self._on_search_preview_play,
        )

    def _on_search_preview_play(self) -> None:
        """Pause main playback when a search preview starts; the preview replaces the mixer's track,
        so resuming must reload the song at the paused position."""
        if (self.is_playing_main or self.is_playing_playlist) and not self.is_paused:
            self.pause_audio()
        self.playback_ctrl.mark_mixer_taken()

    def _handle_search_error(self, err: str) -> None:
        """Explain a failed search, and look for a fix when a newer version is the likely cure."""
        self._reset_download_ui()
        self.set_busy(False, "Search failed.")
        self._look_for_update_after_failure([err])
        show_friendly_error(self.root, err, "search")

    def _start_download_url(self, target_url: str, display_title: str | None = None) -> None:
        status_text = f'Downloading "{display_title}"...' if display_title else "Downloading from YouTube..."
        metric_text = "Connecting to YouTube..."

        self.btn_download.config(text="Downloading...", state=tk.DISABLED)
        self.btn_cancel_dl.config(state=tk.NORMAL)

        self.prog_download.pack(fill=tk.X, pady=(4, 2))
        self.prog_download["value"] = 0
        self.lbl_dl_metrics.pack(fill=tk.X)
        self.lbl_dl_metrics.config(text=metric_text)

        self.set_busy(True, status_text)

        self.download_ctrl.start_download(
            target_url,
            self.library_folder,
            on_progress=lambda pct, speed, eta, finished: self._safe_after(
                0, lambda: self._update_download_progress(pct, speed, eta, finished)
            ),
            on_success=lambda fname: self._safe_after(0, self._download_success, fname),
            on_cancelled=lambda: self._safe_after(0, self._download_cancelled),
            on_error=lambda err: self._safe_after(0, self._download_error, err),
        )

    def _download_success(self, filename: str) -> None:
        self.entry_url.delete(0, tk.END)
        self._reset_download_ui()
        self.set_busy(False)
        if self._reveal_new_song(filename):
            self.notify_success("Download complete! The new song is ready: press PLAY to listen.")
        else:
            self.notify_success("Download complete! The new song is marked in green in your Library on the left.")

    def _download_cancelled(self) -> None:
        self.download_ctrl.cleanup_partial(self.library_folder)
        self._reset_download_ui()
        self.set_busy(False, "Download stopped.")

    def _download_error(self, error: str) -> None:
        """Explain a failed download, and look for a fix when a newer version is the likely cure."""
        self.download_ctrl.cleanup_partial(self.library_folder)
        self._reset_download_ui()
        self.set_busy(False, "Download failed.")
        self._look_for_update_after_failure([error])
        show_friendly_error(self.root, error, "download")

    def _look_for_update_after_failure(self, errors: list[str]) -> None:
        """Check for a new version in the background when a download failed in a way an update fixes.

        YouTube changes several times a year and each change needs a new release. Finding it now
        means the fix is downloaded while the user reads the message, and installed at the next start.
        """
        if self._available_update is not None:  # already found: its badge is showing
            return
        if any(friendly_error(err, "download").suggests_update for err in errors):
            self._check_for_updates_on_launch()

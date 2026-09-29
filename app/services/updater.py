"""Auto-update service: check GitHub Releases, stage a verified download, and install it safely.

Install flow (Windows, packaged .exe only):
  1. A newer release is downloaded in the background and *staged* (file + ``pending.json`` with its
     SHA-256) without interrupting the user.
  2. It is installed at the next start (or immediately via "Restart and update now"): the running
     .exe is renamed to ``.old``, the new one moved into place and launched.
  3. The old process waits until the new one signals that its window is up. If the new process
     exits without signalling, the previous version is put back and started again, and that
     release is not installed automatically again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any, Literal, NoReturn, overload

from app.config import (
    APP_VERSION,
    GITHUB_OWNER,
    GITHUB_REPO,
    RELEASES_API_URL,
    get_update_token,
    log_error,
    settings_mgr,
)
from app.core.time_utils import is_newer_version, parse_version
from app.models import ReleaseInfo
from app.platform_utils import (
    close_handle,
    create_named_event,
    launch_update_process,
    release_instance_mutex,
    wait_for_event_or_exit,
)

__all__ = [
    "UPDATE_OK_EVENT_NAME",
    "InstallResult",
    "PendingUpdate",
    "check_latest_release",
    "clear_pending_update",
    "download_release_asset",
    "failed_update_tag",
    "install_update",
    "is_newer_version",
    "load_pending_update",
    "parse_version",
    "stage_update",
]

# Set by a newly installed version once its window is up (see app.main.main).
UPDATE_OK_EVENT_NAME = "Local\\UltimateAudioStudioUpdateStarted"
# A slow PC unpacking the one-file .exe can take a while; past this the new version is assumed fine.
_START_TIMEOUT_SEC = 120.0
_MIN_EXE_BYTES = 1024 * 1024
PENDING_DIR = Path(tempfile.gettempdir()) / "audio_studio_update"
_PENDING_FILE = PENDING_DIR / "pending.json"

ProgressCallback = Callable[[float, int, int], None]


class _GitHubAssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            parsed = urllib.parse.urlparse(newurl)
            if "amazonaws.com" in parsed.netloc or "github.com" not in parsed.netloc:
                if hasattr(new_req, "headers"):
                    new_req.headers = {k: v for k, v in new_req.headers.items() if k.lower() != "authorization"}
                if hasattr(new_req, "unredirected_hdrs"):
                    new_req.unredirected_hdrs = {
                        k: v for k, v in new_req.unredirected_hdrs.items() if k.lower() != "authorization"
                    }
        return new_req


@overload
def check_latest_release(
    current_ver: str = ..., token: str | None = ..., timeout: float = ..., *, return_error: Literal[True]
) -> tuple[bool, ReleaseInfo | None, str | None]: ...
@overload
def check_latest_release(
    current_ver: str = ..., token: str | None = ..., timeout: float = ..., return_error: Literal[False] = ...
) -> tuple[bool, ReleaseInfo | None]: ...
def check_latest_release(
    current_ver: str = APP_VERSION, token: str | None = None, timeout: float = 6, return_error: bool = False
) -> tuple[bool, ReleaseInfo | None, str | None] | tuple[bool, ReleaseInfo | None]:
    if token is None:
        token = get_update_token()

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Ultimate-Audio-Studio-Updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def _result(
        ok: bool, info: ReleaseInfo | None, err: str | None
    ) -> tuple[bool, ReleaseInfo | None, str | None] | tuple[bool, ReleaseInfo | None]:
        return (ok, info, err) if return_error else (ok, info)

    try:
        if not RELEASES_API_URL.startswith("https://"):
            raise ValueError("Insecure update URL scheme")
        req = urllib.request.Request(RELEASES_API_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))

        tag_name = data.get("tag_name", "")
        if not tag_name or not is_newer_version(tag_name, current_ver):
            return _result(False, None, None)

        chosen_asset = next((a for a in data.get("assets", []) if a.get("name", "").lower().endswith(".exe")), None)
        if not chosen_asset:
            err = f"Release {tag_name} has no downloadable Windows executable (.exe)."
            log_error(err)
            return _result(False, None, err)

        info = ReleaseInfo(
            tag_name=tag_name,
            name=data.get("name") or tag_name,
            body=data.get("body") or "New improvements and bug fixes.",
            published_at=data.get("published_at", ""),
            asset_id=chosen_asset.get("id"),
            asset_name=chosen_asset.get("name", ""),
            asset_size=chosen_asset.get("size", 0),
            asset_api_url=chosen_asset.get("url", ""),
            browser_download_url=chosen_asset.get("browser_download_url", ""),
            html_url=data.get("html_url", ""),
            asset_digest=str(chosen_asset.get("digest") or ""),
        )
        return _result(True, info, None)

    except urllib.error.HTTPError as e:
        if e.code == 404:
            err = "Update check failed: 404 Not Found (no releases found or repository inaccessible)."
        elif e.code in (401, 403):
            err = f"GitHub API error {e.code}: {e.reason}."
        else:
            err = f"Update check HTTP error {e.code}: {e.reason}"
        log_error(err)
        return _result(False, None, err)
    except Exception as e:
        err = f"Update check error: {e}"
        log_error(err)
        return _result(False, None, err)


def _parse_sha256_digest(digest: object) -> str | None:
    """Return the lowercase hex from a GitHub ``sha256:<hex>`` digest string, or None."""
    if not digest or not isinstance(digest, str):
        return None
    algo, _, value = digest.strip().partition(":")
    value = value.strip().lower()
    if algo.lower() != "sha256" or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        return None
    return value


def _remove_quietly(path: str | Path) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def download_release_asset(
    asset_id: int | str,
    token: str | None = None,
    dest_path: str | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Any = None,
    expected_digest: str | None = None,
) -> tuple[bool, str]:
    """Stream a release asset to disk, verifying its SHA-256 against GitHub's published digest when given.

    Returns ``(True, path)`` on success, else ``(False, reason)``.
    """
    if token is None:
        token = get_update_token()

    if not dest_path:
        dest_path = os.path.join(tempfile.gettempdir(), f"Ultimate_Audio_Studio_update_{os.getpid()}.exe")

    asset_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/assets/{asset_id}"
    headers = {"Accept": "application/octet-stream", "User-Agent": "Ultimate-Audio-Studio-Updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    opener = urllib.request.build_opener(_GitHubAssetRedirectHandler())
    expected_sha256 = _parse_sha256_digest(expected_digest)
    hasher = hashlib.sha256()

    try:
        req = urllib.request.Request(asset_url, headers=headers)
        with opener.open(req, timeout=30) as resp:
            total_bytes = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            chunk_size = 65536
            cancelled = False

            with open(dest_path, "wb") as f_out:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f_out.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)

                    if progress_callback:
                        pct = (downloaded / total_bytes * 100.0) if total_bytes > 0 else 0.0
                        with contextlib.suppress(Exception):
                            progress_callback(pct, downloaded, total_bytes)
            if cancelled:
                _remove_quietly(dest_path)
                return False, "Download cancelled"

        if not os.path.exists(dest_path) or os.path.getsize(dest_path) < _MIN_EXE_BYTES:
            _remove_quietly(dest_path)
            return False, "Downloaded file is incomplete or too small"

        with open(dest_path, "rb") as f_check:
            header = f_check.read(2)
        if header != b"MZ":
            _remove_quietly(dest_path)
            return False, "Downloaded file is not a valid Windows executable"

        if expected_sha256:
            actual = hasher.hexdigest()
            if actual != expected_sha256:
                log_error(f"Update checksum mismatch: expected {expected_sha256}, got {actual}")
                _remove_quietly(dest_path)
                return False, "The downloaded update was damaged (checksum mismatch). Please try again later."
        else:
            log_error("Update asset has no published SHA-256 digest; relying on HTTPS integrity only.")

        return True, dest_path

    except Exception as e:
        log_error(f"Asset download error: {e}")
        _remove_quietly(dest_path)
        return False, str(e)


# --- Staged ("pending") updates -------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PendingUpdate:
    """A downloaded, verified update waiting to be installed."""

    tag: str
    exe_path: Path
    sha256: str


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def stage_update(
    release_info: ReleaseInfo,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Any = None,
) -> tuple[PendingUpdate | None, str]:
    """Download a release into the staging folder and record it as pending (worker thread only).

    Returns ``(pending, "")`` or ``(None, reason)``. An already-staged copy of the same release is reused.
    """
    existing = load_pending_update()
    if existing is not None and existing.tag == release_info.tag_name:
        return existing, ""
    if release_info.asset_id is None:
        return None, "No downloadable file was found for this release."
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    safe_tag = "".join(c for c in release_info.tag_name if c.isalnum() or c in ".-_") or "update"
    dest = PENDING_DIR / f"Ultimate Audio Studio {safe_tag}.exe"
    ok, result = download_release_asset(
        release_info.asset_id,
        dest_path=str(dest),
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        expected_digest=release_info.asset_digest,
    )
    if not ok:
        return None, result
    pending = PendingUpdate(tag=release_info.tag_name, exe_path=dest, sha256=_file_sha256(dest))
    try:
        _PENDING_FILE.write_text(
            json.dumps({"tag": pending.tag, "exe_path": str(pending.exe_path), "sha256": pending.sha256}),
            encoding="utf-8",
        )
    except OSError as e:
        log_error(f"stage_update: could not record pending update: {e}")
        return None, str(e)
    return pending, ""


def load_pending_update(current_ver: str = APP_VERSION) -> PendingUpdate | None:
    """The staged update if it is still newer than this version and its file is intact, else None.

    Stale or damaged staging data is removed.
    """
    try:
        data = json.loads(_PENDING_FILE.read_text(encoding="utf-8"))
        pending = PendingUpdate(tag=str(data["tag"]), exe_path=Path(data["exe_path"]), sha256=str(data["sha256"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        intact = pending.exe_path.is_file() and _file_sha256(pending.exe_path) == pending.sha256
    except OSError:
        intact = False
    if not intact or not is_newer_version(pending.tag, current_ver):
        clear_pending_update()
        return None
    return pending


def clear_pending_update() -> None:
    """Delete the staging folder (downloaded update and its record)."""
    shutil.rmtree(PENDING_DIR, ignore_errors=True)


# --- Installing ------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class InstallResult:
    """Outcome of an install that did not end this process (success exits instead of returning)."""

    message: str
    rolled_back: bool = False


def _restore_previous(current_exe: str, old_exe: str) -> bool:
    """Put the previous executable back after the new one failed (retrying while Windows releases it)."""
    for _attempt in range(20):
        try:
            if os.path.exists(current_exe):
                os.remove(current_exe)
            os.rename(old_exe, current_exe)
            return True
        except OSError:
            time.sleep(0.25)
    log_error(f"Could not restore previous version from {old_exe}")
    return False


def install_update(
    new_exe_path: str | Path,
    tag: str,
    *,
    on_launched: Callable[[], None] | None = None,
    exit_fn: Callable[[int], NoReturn] = os._exit,
    start_timeout_sec: float = _START_TIMEOUT_SEC,
) -> InstallResult:
    """Swap in the new executable, start it, and exit once it reports that it started.

    Returns (instead of exiting) when nothing was changed or the previous version had to be restored;
    the caller should then keep running and, after a rollback, re-take the single-instance lock.
    """
    if not getattr(sys, "frozen", False):
        return InstallResult("Running in development mode (source code). Updates can be pulled with git pull.")

    current_exe = sys.executable
    old_exe = current_exe + ".old"
    new_exe = str(new_exe_path)
    if not os.path.exists(new_exe):
        return InstallResult("The downloaded update file was not found.")

    _remove_quietly(old_exe)
    try:
        os.rename(current_exe, old_exe)
    except OSError as e:
        log_error(f"install_update: cannot rename running executable: {e}")
        return InstallResult(f"The update could not be installed: {e}")
    try:
        shutil.move(new_exe, current_exe)
    except OSError as e:
        log_error(f"install_update: cannot move new executable into place: {e}")
        _restore_previous(current_exe, old_exe)
        return InstallResult(f"The update could not be installed: {e}")

    event = create_named_event(UPDATE_OK_EVENT_NAME)
    # The new version must not find this instance's lock and think the app is already open.
    release_instance_mutex()
    try:
        proc = launch_update_process(current_exe)
    except OSError as e:
        log_error(f"install_update: cannot start new version: {e}")
        close_handle(event)
        _restore_previous(current_exe, old_exe)
        return InstallResult(f"The new version could not be started: {e}", rolled_back=True)
    if on_launched:
        on_launched()

    if event:
        outcome = wait_for_event_or_exit(event, proc, start_timeout_sec)
    else:
        # Without the event, at least catch a version that dies straight away.
        try:
            proc.wait(timeout=20)
            outcome = "exited"
        except subprocess.TimeoutExpired:
            outcome = "timeout"
    close_handle(event)
    if outcome != "exited":
        clear_pending_update()
        exit_fn(0)

    log_error(f"Update {tag} exited before its window opened; restoring the previous version.")
    restored = _restore_previous(current_exe, old_exe)
    clear_pending_update()
    with contextlib.suppress(Exception):
        settings_mgr.update_settings(failed_update_tag=tag)
    return InstallResult(
        f"Version {tag} did not start correctly on this computer, so your current version was kept.",
        rolled_back=restored,
    )


def failed_update_tag() -> str:
    """Tag of a release whose install was rolled back (it is not installed automatically again)."""
    return str(settings_mgr.get_settings().extra.get("failed_update_tag") or "")

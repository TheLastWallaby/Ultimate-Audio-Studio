# Auto-update service for Ultimate Audio Studio
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from app.config import APP_VERSION, GITHUB_OWNER, GITHUB_REPO, RELEASES_API_URL, get_update_token, log_error
from app.core.time_utils import is_newer_version, parse_version
from app.models import ReleaseInfo
from app.platform_utils import clean_pyi_env, launch_detached_gui, release_instance_mutex

__all__ = [
    "apply_update_and_restart",
    "check_latest_release",
    "download_release_asset",
    "is_newer_version",
    "parse_version",
]


class _GitHubAssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
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


def check_latest_release(current_ver=APP_VERSION, token=None, timeout=6, return_error=False):
    if token is None:
        token = get_update_token()

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Ultimate-Audio-Studio-Updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        if not RELEASES_API_URL.startswith("https://"):
            raise ValueError("Insecure update URL scheme")
        req = urllib.request.Request(RELEASES_API_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))

        tag_name = data.get("tag_name", "")
        if not tag_name or not is_newer_version(tag_name, current_ver):
            return (False, None, None) if return_error else (False, None)

        assets = data.get("assets", [])
        chosen_asset = None
        for asset in assets:
            name = asset.get("name", "").lower()
            if name.endswith(".exe"):
                chosen_asset = asset
                break

        if not chosen_asset:
            err = f"Release {tag_name} has no downloadable Windows executable (.exe)."
            log_error(err)
            return (False, None, err) if return_error else (False, None)

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
        )
        return (True, info, None) if return_error else (True, info)

    except urllib.error.HTTPError as e:
        if e.code == 404:
            err = "Update check failed: 404 Not Found (no releases found or repository inaccessible)."
        elif e.code in (401, 403):
            err = f"GitHub API error {e.code}: {e.reason}."
        else:
            err = f"Update check HTTP error {e.code}: {e.reason}"
        log_error(err)
        return (False, None, err) if return_error else (False, None)
    except Exception as e:
        err = f"Update check error: {e}"
        log_error(err)
        return (False, None, err) if return_error else (False, None)


def download_release_asset(asset_id, token=None, dest_path=None, progress_callback=None, cancel_event=None):
    if token is None:
        token = get_update_token()

    if not dest_path:
        dest_path = os.path.join(tempfile.gettempdir(), f"Ultimate_Audio_Studio_update_{os.getpid()}.exe")

    asset_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/assets/{asset_id}"
    headers = {"Accept": "application/octet-stream", "User-Agent": "Ultimate-Audio-Studio-Updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    opener = urllib.request.build_opener(_GitHubAssetRedirectHandler())

    try:
        req = urllib.request.Request(asset_url, headers=headers)
        with opener.open(req, timeout=30) as resp:
            total_bytes = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            chunk_size = 65536

            with open(dest_path, "wb") as f_out:
                while True:
                    if cancel_event and cancel_event.is_set():
                        f_out.close()
                        try:
                            if os.path.exists(dest_path):
                                os.remove(dest_path)
                        except Exception:
                            pass
                        return False, "Download cancelled"

                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f_out.write(chunk)
                    downloaded += len(chunk)

                    if progress_callback:
                        pct = (downloaded / total_bytes * 100.0) if total_bytes > 0 else 0.0
                        try:
                            progress_callback(pct, downloaded, total_bytes)
                        except Exception:
                            pass

        if not os.path.exists(dest_path) or os.path.getsize(dest_path) < 1024 * 1024:
            try:
                os.remove(dest_path)
            except Exception:
                pass
            return False, "Downloaded file is incomplete or too small"

        with open(dest_path, "rb") as f_check:
            header = f_check.read(2)
            if header != b"MZ":
                f_check.close()
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
                return False, "Downloaded file is not a valid Windows executable"

        return True, dest_path

    except Exception as e:
        log_error(f"Asset download error: {e}")
        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
        except Exception:
            pass
        return False, str(e)


def apply_update_and_restart(new_exe_path):
    if not getattr(sys, "frozen", False):
        return False, "Running in development mode (source code). Updates can be pulled with git pull."

    current_exe = sys.executable
    if not os.path.exists(new_exe_path):
        return False, "Downloaded update file not found."

    old_exe = current_exe + ".old"
    try:
        if os.path.exists(old_exe):
            try:
                os.remove(old_exe)
            except Exception:
                pass

        os.rename(current_exe, old_exe)
        shutil.move(new_exe_path, current_exe)

        # Release single-instance mutex so newly launched process doesn't detect itself as already running
        release_instance_mutex()

        # Sanitize environment so child process is not flagged as a PyInstaller worker subprocess
        clean_pyi_env()

        # Launch replacement GUI process cleanly without hidden window flags
        launch_detached_gui(current_exe)

        # Allow Windows shell brief moment for process spawn handover, then cleanly terminate
        import time

        time.sleep(0.3)
        os._exit(0)
    except Exception as e:
        log_error(f"apply_update_and_restart failed: {e}")
        if not os.path.exists(current_exe) and os.path.exists(old_exe):
            try:
                os.rename(old_exe, current_exe)
            except Exception:
                pass
        return False, f"Failed to swap executable: {e}"

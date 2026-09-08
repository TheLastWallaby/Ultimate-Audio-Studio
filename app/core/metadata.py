"""Audio metadata extraction, duration probing, and cover art processing."""

import os
import re
import subprocess

from tinytag import TinyTag

from app.config import CREATE_NO_WINDOW, ffmpeg_path, ffprobe_path
from app.models import TrackMetadata


def probe_audio_duration(filepath):
    """Probe audio duration in seconds via ffprobe or ffmpeg when tag readers return 0."""
    if not filepath or not os.path.exists(filepath):
        return 0.0
    if os.path.exists(ffprobe_path):
        try:
            cmd = [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                filepath,
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=CREATE_NO_WINDOW,
                timeout=6,
            )
            if res.returncode == 0 and res.stdout.strip():
                val = float(res.stdout.strip())
                if val > 0:
                    return val
        except Exception:
            pass
    if os.path.exists(ffmpeg_path):
        try:
            cmd = [ffmpeg_path, "-nostdin", "-i", filepath]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=CREATE_NO_WINDOW,
                timeout=6,
            )
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr or "")
            if m:
                hrs, mins, secs = float(m.group(1)), float(m.group(2)), float(m.group(3))
                return hrs * 3600 + mins * 60 + secs
        except Exception:
            pass
    return 0.0


def extract_album_art(audio_path, out_png_path):
    """Extract embedded cover art to a 60x60 PNG thumbnail."""
    try:
        os.makedirs(os.path.dirname(out_png_path), exist_ok=True)
        cmd = [
            ffmpeg_path,
            "-nostdin",
            "-y",
            "-i",
            audio_path,
            "-an",
            "-vf",
            "scale=60:60:force_original_aspect_ratio=increase,crop=60:60",
            "-frames:v",
            "1",
            "-update",
            "1",
            out_png_path,
        ]
        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
        return res.returncode == 0 and os.path.exists(out_png_path) and os.path.getsize(out_png_path) > 0
    except Exception:
        return False


def read_track_metadata(filepath: str) -> TrackMetadata:
    """Read metadata tags (title, artist, duration) using TinyTag with fallback."""
    title = ""
    artist = ""
    duration = 0.0
    try:
        tag = TinyTag.get(filepath)
        title = (tag.title or "").strip()
        artist = (tag.artist or "").strip()
        duration = float(tag.duration or 0.0)
    except Exception:
        pass
    if duration <= 0:
        duration = probe_audio_duration(filepath)
    return TrackMetadata(
        title=title,
        artist=artist,
        duration=duration,
    )

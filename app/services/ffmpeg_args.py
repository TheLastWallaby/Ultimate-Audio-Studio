"""FFmpeg argument builders shared by clip saving, clip previews, and USB export."""

from __future__ import annotations

from typing import Literal

# LAME VBR V2 (~190 kbps), the same quality as downloads: a YouTube-sourced song gains nothing from
# 320 kbps CBR except a ~1.7x larger file.
MP3_VBR_QUALITY = "2"

CoverMode = Literal["copy", "mjpeg"]


def mp3_output_args(cover: CoverMode = "copy", resample_44k: bool = False) -> list[str]:
    """Output options for an MP3 that keeps the tags and the embedded cover picture.

    ``cover="copy"`` keeps the picture as-is; ``"mjpeg"`` re-encodes it, the retry used when the
    source picture is a PNG (common in M4A files), which MP3 cannot carry by stream copy.
    """
    args = ["-map", "0:a", "-map", "0:v?"]
    args += ["-c:v", "copy"] if cover == "copy" else ["-c:v:0", "mjpeg"]
    args += [
        "-disposition:v:0",
        "attached_pic",
        "-map_metadata",
        "0",
        "-id3v2_version",
        "3",
        "-c:a",
        "libmp3lame",
        "-q:a",
        MP3_VBR_QUALITY,
    ]
    if resample_44k:
        args += ["-ar", "44100"]
    return args


def clip_filter_chain(clip_dur: float, gain_db: float = 0.0, soften: bool = False, fade_sec: float = 1.5) -> str:
    """Audio filter for a clip: reset timestamps, optional gain (with a limiter when boosting) and fades."""
    fade_dur = fade_duration(clip_dur, soften, fade_sec)
    parts = ["asetpts=PTS-STARTPTS"]
    if gain_db > 0.05:
        parts.append(f"volume={gain_db:.1f}dB,alimiter=limit=0.95:attack=5:release=50")
    elif gain_db < -0.05:
        parts.append(f"volume={gain_db:.1f}dB")
    if soften and fade_dur > 0.01:
        out_start = max(0.0, clip_dur - fade_dur)
        parts.append(f"afade=t=in:st=0:d={fade_dur:.3f},afade=t=out:st={out_start:.3f}:d={fade_dur:.3f}")
    return ",".join(parts)


def fade_duration(clip_dur: float, soften: bool, fade_sec: float) -> float:
    """Length of each fade actually applied (never more than half the clip)."""
    return min(float(fade_sec or 1.5), clip_dur / 2.0) if soften else 0.0

"""Time formatting, parsing, and semantic versioning utilities."""

from __future__ import annotations

import re


def format_time(seconds: float | int | None, include_fractional: bool = False) -> str:
    """Format seconds into MM:SS string, optionally including tenths of a second (MM:SS.s)."""
    secs_val = max(0.0, float(seconds or 0.0))
    mins = int(secs_val // 60)
    secs = int(secs_val % 60)
    if include_fractional:
        tenths = int(round((secs_val % 1.0) * 10))
        if tenths >= 10:
            secs += 1
            tenths = 0
            if secs >= 60:
                mins += 1
                secs = 0
        return f"{mins:02d}:{secs:02d}.{tenths}"
    return f"{mins:02d}:{secs:02d}"


def parse_time(text: str | None) -> float | None:
    """Parse time input string in format MM:SS, M:SS, MM:SS.s, HH:MM:SS, or raw seconds float.

    Returns float seconds if valid, or None if invalid or negative.
    """
    if not text or not str(text).strip():
        return None
    raw = str(text).strip()
    if ":" in raw:
        parts = raw.split(":")
        try:
            if len(parts) == 2:
                mins = float(parts[0])
                secs = float(parts[1])
                if mins < 0 or secs < 0 or secs >= 60:
                    return None
                return mins * 60.0 + secs
            elif len(parts) == 3:
                hrs = float(parts[0])
                mins = float(parts[1])
                secs = float(parts[2])
                if hrs < 0 or mins < 0 or secs < 0 or mins >= 60 or secs >= 60:
                    return None
                return hrs * 3600.0 + mins * 60.0 + secs
        except ValueError:
            return None
    else:
        try:
            val = float(raw)
            return val if val >= 0 else None
        except ValueError:
            return None
    return None


def parse_version(v_str: str | None) -> tuple[int, int, int]:
    """Parse semantic version string (e.g. 'v1.0.9') into a 3-integer tuple."""
    if not v_str:
        return (0, 0, 0)
    cleaned = str(v_str).strip().lstrip("vV")
    parts = [int(p) for p in re.findall(r"\d+", cleaned)[:3]]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def is_newer_version(latest_tag: str, current_ver: str) -> bool:
    """Compare two semantic version strings to determine if latest_tag is strictly newer."""
    return parse_version(latest_tag) > parse_version(current_ver)

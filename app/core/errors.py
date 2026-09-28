"""Translate raw technical errors (yt-dlp, FFmpeg, OS) into plain-language messages with recovery steps."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

__all__ = ["FriendlyError", "friendly_error"]

ErrorContext = Literal["download", "search", "export", "save_clip", "playback", "import", "generic"]


@dataclass(slots=True, frozen=True)
class FriendlyError:
    """A user-facing explanation of a failure."""

    title: str
    message: str


_OFFLINE_STEPS = (
    "• Check that your computer is connected to the internet.\n"
    "• Open a web page to confirm the connection works.\n"
    "• Then try again."
)

# (pattern, title, message) evaluated in order; first match wins.
_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"private video|video is private", re.I),
        "Private Video",
        "This video is private, so it cannot be downloaded.\n\nPlease choose a different version of the song.",
    ),
    (
        re.compile(r"confirm your age|age[- ]restricted|inappropriate for some users", re.I),
        "Age-Restricted Video",
        "YouTube only allows signed-in adults to play this video, so it cannot be downloaded.\n\n"
        "Please choose a different version of the song.",
    ),
    (
        re.compile(r"not a bot|sign in to confirm", re.I),
        "YouTube Is Blocking Downloads",
        "YouTube is temporarily refusing downloads from this computer.\n\n"
        "• Wait an hour and try again.\n"
        "• Click 'Check for Updates' at the bottom right: a newer version of this app may fix it.",
    ),
    (
        re.compile(r"not (?:made )?available in your country|geo.?restrict|blocked it in your country", re.I),
        "Not Available in Your Country",
        "The owner of this video does not allow it to be played in your country.\n\n"
        "Please choose a different version of the song.",
    ),
    (
        re.compile(r"video unavailable|has been removed|no longer available|does not exist|is not a valid url", re.I),
        "Video Not Available",
        "This video is no longer available on YouTube, or the link is not correct.\n\n"
        "• Check the link, or type the song name and artist instead.",
    ),
    (
        re.compile(r"live event|premieres in|is_live|this live stream", re.I),
        "Live Video",
        "Live broadcasts cannot be downloaded.\n\nPlease choose a normal (recorded) version of the song.",
    ),
    (
        re.compile(r"no space left|errno 28|disk (?:is )?full|not enough space|winerror 112", re.I),
        "Disk Is Full",
        "There is not enough free space to save the file.\n\n"
        "• Delete some files you no longer need, or choose a different drive, then try again.",
    ),
    (
        re.compile(r"winerror 32|being used by another process|permission denied|access is denied|errno 13", re.I),
        "File Is In Use",
        "Windows would not let the app change this file because another program is using it.\n\n"
        "• Close other music players or File Explorer windows showing this folder, then try again.",
    ),
    (
        re.compile(r"ffmpeg|ffprobe", re.I),
        "Audio Tool Problem",
        "The built-in audio tool could not process this song.\n\n"
        "• The file may be damaged. Try a different song or download it again.",
    ),
    (
        re.compile(
            r"getaddrinfo|name resolution|failed to resolve|timed out|timeout|connection (?:refused|reset|aborted)"
            r"|network is unreachable|unable to download (?:webpage|api page)|urlopen error|no internet",
            re.I,
        ),
        "No Internet Connection",
        f"The app could not reach the internet.\n\n{_OFFLINE_STEPS}",
    ),
    (
        re.compile(r"http error 403|forbidden|http error 429|too many requests", re.I),
        "YouTube Refused the Download",
        "YouTube refused the download right now.\n\n"
        "• Wait a few minutes and try again.\n"
        "• Click 'Check for Updates' at the bottom right: a newer version of this app may fix it.",
    ),
)

_GENERIC: dict[ErrorContext, FriendlyError] = {
    "download": FriendlyError(
        "Download Failed",
        f"The song could not be downloaded.\n\n{_OFFLINE_STEPS}\n• Or try a different version of the song.",
    ),
    "search": FriendlyError("Search Failed", f"Could not search YouTube.\n\n{_OFFLINE_STEPS}"),
    "export": FriendlyError(
        "Export Failed",
        "The playlist could not be copied.\n\n"
        "• Make sure the USB drive is still plugged in and has free space, then try again.",
    ),
    "save_clip": FriendlyError(
        "Could Not Save Clip",
        "The trimmed song could not be saved.\n\n• Try again, or save it with a different name.",
    ),
    "playback": FriendlyError(
        "Cannot Play This Song",
        "This song could not be played. The file may be damaged or in an unusual format.\n\n"
        "• Try another song, or download this one again.",
    ),
    "import": FriendlyError("Could Not Add Songs", "Some songs could not be copied into your Library."),
    "generic": FriendlyError("Something Went Wrong", "The app hit an unexpected problem."),
}


def friendly_error(raw: object, context: ErrorContext = "generic") -> FriendlyError:
    """Map a raw exception or error string to a plain-language title and recovery message."""
    text = str(raw or "")
    for pattern, title, message in _RULES:
        if pattern.search(text):
            return FriendlyError(title, message)
    return _GENERIC.get(context, _GENERIC["generic"])

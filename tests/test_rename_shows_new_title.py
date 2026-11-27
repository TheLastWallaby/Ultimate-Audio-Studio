"""A renamed song, and a clip saved under a new name, are listed under that name.

The Library list shows the title stored inside a song. Renaming changed only the file name, so the
list kept showing the old title, and a clip was listed under the full song's title.
"""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import ffmpeg_path, is_valid_binary
from app.core.cache_manager import cache_mgr
from app.core.metadata import read_track_metadata
from app.core.process_utils import ProcessResult, run_ffmpeg
from app.main import UltimateAudioStudio
from app.services import tagger
from app.services.clipper import _clip_args, clip_audio_worker
from app.services.tagger import TitleWriteError, write_title
from app.ui.dialogs import TextAnswer

HAVE_FFMPEG = is_valid_binary(ffmpeg_path)
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs a working ffmpeg")


def _make_song(path: Path, title: str, seconds: int = 5) -> Path:
    """A real MP3 with a title tag, made by FFmpeg."""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-y", "-f", "lavfi", "-i", f"sine=duration={seconds}", "-metadata", f"title={title}"]
    result = run_ffmpeg(args + ["-c:a", "libmp3lame", "-q:a", "5", str(path)], timeout=60)
    assert result.returncode == 0, result.stderr
    return path


def _pump(window: UltimateAudioStudio, until: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)


def _rows(window: UltimateAudioStudio) -> list[str]:
    return [str(row) for row in window.listbox_lib.get(0, tk.END)]


def _library_with(window: UltimateAudioStudio, folder: Path, song: Path) -> None:
    """Show ``folder`` in the window and wait until the song's row shows its title tag."""
    window.library_folder = str(folder)
    window._load_waveform = lambda _path: None  # type: ignore[method-assign]
    window._load_album_art = lambda _path: None  # type: ignore[method-assign]
    window.refresh_library()
    title = read_track_metadata(str(song)).title
    _pump(window, lambda: any(row.startswith(title) for row in _rows(window)))
    window.listbox_lib.selection_clear(0, tk.END)
    window.listbox_lib.selection_set(window.visible_files.index(song.name))


# --- The service ---------------------------------------------------------------------------------------


@needs_ffmpeg
def test_write_title_changes_the_title_and_keeps_the_sound(tmp_path: Path) -> None:
    song = _make_song(tmp_path / "song.mp3", "Old Title")
    before = read_track_metadata(str(song)).duration

    write_title(song, "New Name")

    details = read_track_metadata(str(song))
    assert details.title == "New Name"
    assert abs(details.duration - before) < 0.1
    assert [p.name for p in tmp_path.iterdir()] == ["song.mp3"]  # no work file left behind


def test_a_failed_ffmpeg_run_leaves_the_song_as_it_was(tmp_path: Path) -> None:
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3" + bytes(5000))
    failed = ProcessResult(returncode=1, stdout="", stderr="Invalid data found")

    with patch.object(tagger, "run_ffmpeg", return_value=failed), pytest.raises(TitleWriteError):
        write_title(song, "New Name")

    assert song.read_bytes() == b"ID3" + bytes(5000)
    assert [p.name for p in tmp_path.iterdir()] == ["song.mp3"]


def test_a_cut_off_copy_never_replaces_the_song(tmp_path: Path) -> None:
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3" + bytes(5000))

    def _short_copy(args: list[str], timeout: int = 60) -> ProcessResult:
        Path(args[-1]).write_bytes(b"ID3" + bytes(100))  # FFmpeg "succeeded" but wrote a fraction
        return ProcessResult(returncode=0, stdout="", stderr="")

    with patch.object(tagger, "run_ffmpeg", side_effect=_short_copy), pytest.raises(TitleWriteError):
        write_title(song, "New Name")

    assert song.read_bytes() == b"ID3" + bytes(5000)
    assert [p.name for p in tmp_path.iterdir()] == ["song.mp3"]


def test_a_format_without_tags_is_refused(tmp_path: Path) -> None:
    song = tmp_path / "song.aac"
    song.write_bytes(bytes(5000))

    assert not tagger.can_write_title(song)
    with pytest.raises(TitleWriteError):
        write_title(song, "New Name")
    assert song.read_bytes() == bytes(5000)


# --- Save Clip -----------------------------------------------------------------------------------------


def test_a_clip_keeps_the_song_title_unless_given_one() -> None:
    plain = _clip_args("song.mp3", 0.0, 10.0, "anull", wav=False, cover="copy")
    named = _clip_args("song.mp3", 0.0, 10.0, "anull", wav=False, cover="none", title="Cafe Scene")

    assert "title=Cafe Scene" not in plain
    assert named[named.index("-metadata") + 1] == "title=Cafe Scene"


@needs_ffmpeg
def test_a_clip_saved_as_a_new_song_carries_its_new_name(tmp_path: Path) -> None:
    song = _make_song(tmp_path / "Full Song.mp3", "Full Song Title")
    clip = tmp_path / "Cafe Scene.mp3"
    errors: list[str] = []

    clip_audio_worker(str(song), 1.0, 3.0, str(clip), title="Cafe Scene", on_error=errors.append)

    assert errors == []
    assert read_track_metadata(str(clip)).title == "Cafe Scene"
    assert read_track_metadata(str(song)).title == "Full Song Title"


# --- Rename in the window ------------------------------------------------------------------------------


@needs_ffmpeg
def test_renamed_song_is_listed_under_its_new_name(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    library = tmp_path / "lib"
    song = _make_song(library / "Old Title.mp3", "Old Title")
    _library_with(studio, library, song)

    with patch("app.ui.dialogs.ask_text", return_value=TextAnswer("Cafe Scene 1 min")):
        studio.rename_library_file()
    _pump(studio, lambda: _rows(studio) == ["Cafe Scene 1 min [00:05]"])

    assert _rows(studio) == ["Cafe Scene 1 min [00:05]"]
    assert sorted(p.name for p in library.iterdir()) == ["Cafe Scene 1 min.mp3"]
    assert read_track_metadata(str(library / "Cafe Scene 1 min.mp3")).title == "Cafe Scene 1 min"


@needs_ffmpeg
def test_renaming_the_song_in_the_player_shows_the_new_name_there(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    library = tmp_path / "lib"
    song = _make_song(library / "Old Title.mp3", "Old Title")
    _library_with(studio, library, song)
    assert studio._load_track_ui(studio._library_row_path(song.name), song.name)

    with patch("app.ui.dialogs.ask_text", return_value=TextAnswer("New Name")):
        studio.rename_library_file()
    _pump(studio, lambda: studio._replacing_song is None)

    assert studio.selected_file_path == studio._library_row_path("New Name.mp3")
    assert studio.lbl_selected.cget("text") == "New Name"


@needs_ffmpeg
def test_renaming_to_the_same_name_fixes_a_song_renamed_before(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    library = tmp_path / "lib"
    song = _make_song(library / "Cafe Scene.mp3", "The Full Movie Title")  # renamed by an older version
    _library_with(studio, library, song)
    assert _rows(studio) == ["The Full Movie Title [00:05]"]

    with patch("app.ui.dialogs.ask_text", return_value=TextAnswer("Cafe Scene")):
        studio.rename_library_file()
    _pump(studio, lambda: _rows(studio) == ["Cafe Scene [00:05]"])

    assert _rows(studio) == ["Cafe Scene [00:05]"]


def test_a_title_that_cannot_be_written_is_reported(studio: UltimateAudioStudio, tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    (library / "old.mp3").write_bytes(b"ID3" + bytes(64))
    studio.library_folder = str(library)
    studio._load_waveform = lambda _path: None  # type: ignore[method-assign]
    studio._load_album_art = lambda _path: None  # type: ignore[method-assign]
    # Known details mean no background reader opens the song: Windows would then refuse the rename
    # ("in use") before the title is ever written, as it did on GitHub's runner.
    cache_mgr.set_metadata(studio._library_row_path("old.mp3"), {"title": "Old", "artist": "", "duration": 100.0})
    studio.refresh_library()
    studio.listbox_lib.selection_set(studio.visible_files.index("old.mp3"))
    status: list[str] = []
    real_set_status = studio.set_status

    def _record(text: str, *args: object, **kwargs: object) -> None:
        status.append(text)
        real_set_status(text, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch("app.ui.dialogs.ask_text", return_value=TextAnswer("new")),
        patch("app.ui.features.library.write_title", side_effect=TitleWriteError("in use")),
        patch.object(studio, "set_status", side_effect=_record),
    ):
        studio.rename_library_file()
        _pump(studio, lambda: studio._replacing_song is None and any("could not be changed" in s for s in status))

    assert any("could not be changed" in s for s in status)
    assert sorted(p.name for p in library.iterdir()) == ["new.mp3"]  # the file itself was still renamed

"""Layout at high display scaling: Save Clip was pushed off a 2880x1800 screen at 200% scaling.

A song title wrapped at a fixed 260 physical pixels (130 at 200%), grew seven lines tall, and pushed
Test Clip and Save Clip below the bottom of the player column, where nobody found them.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Iterator

import pytest

from app.main import UltimateAudioStudio
from app.ui.components import ScrollableFrame, wrap_to_width

LONG_TITLE = 'The Blues Brothers | Aretha Franklin Sings "Think" in 4K HDR'


@pytest.fixture
def window() -> Iterator[tk.Tk]:
    root = tk.Tk()
    # Mapped (a withdrawn window is never laid out at its geometry), but off screen.
    root.geometry("+-3000+-3000")
    root.deiconify()
    try:
        yield root
    finally:
        root.destroy()


def _pump(root: tk.Misc) -> None:
    for _ in range(6):
        root.update()


def _inset(label: tk.Label) -> int:
    return 2 * sum(int(str(label.cget(opt))) for opt in ("padx", "borderwidth", "highlightthickness"))


def _is_inside(widget: tk.Misc, container: tk.Misc) -> bool:
    node: tk.Misc | None = widget
    while node is not None:
        if node is container:
            return True
        node = node.master
    return False


def test_wrapped_label_follows_its_width_as_the_window_grows_and_shrinks(window: tk.Tk) -> None:
    frame = tk.Frame(window)
    frame.pack(fill=tk.BOTH, expand=True)
    label = tk.Label(frame, text=LONG_TITLE * 3, justify="left", anchor="w")
    wrap_to_width(label)
    label.pack(fill=tk.X)

    window.geometry("600x300")
    _pump(window)
    assert int(label.cget("wraplength")) == label.winfo_width() - _inset(label)
    one_line_height = label.winfo_reqheight()

    window.geometry("250x300")
    _pump(window)
    # The label must not hold the window at its old width: it wraps at the new, narrower one.
    assert label.winfo_width() <= 250
    assert int(label.cget("wraplength")) == label.winfo_width() - _inset(label)
    assert label.winfo_reqheight() > one_line_height
    assert frame.winfo_reqwidth() < 100


def test_song_title_wraps_at_the_width_of_the_player_column(studio: UltimateAudioStudio) -> None:
    studio.lbl_selected.config(text=LONG_TITLE)
    studio.lbl_selected_artist.config(text="Universal Pictures")
    _pump(studio.root)

    for label in (studio.lbl_selected, studio.lbl_selected_artist):
        assert label.winfo_width() > 1
        assert int(label.cget("wraplength")) == label.winfo_width() - _inset(label)


def test_footer_sits_under_the_content_and_stays_in_view_when_the_window_is_short(window: tk.Tk) -> None:
    panel = ScrollableFrame(window, bg="#ffffff")
    panel.pack(fill=tk.BOTH, expand=True)
    tk.Frame(panel.body, width=200, height=300).pack()
    footer = panel.add_footer()
    tk.Button(footer, text="Save Clip").pack()
    assert panel.add_footer() is footer

    window.geometry("300x600")
    _pump(window)
    # No gap between the content and the footer while everything fits.
    assert panel.canvas.winfo_height() == 300
    assert footer.winfo_y() == 300
    assert not panel.vbar.winfo_ismapped()

    window.geometry("300x200")
    _pump(window)
    # Too short: the content scrolls, and the footer is still fully on screen.
    assert footer.winfo_y() + footer.winfo_height() <= panel.winfo_height()
    assert footer.winfo_height() == footer.winfo_reqheight()
    assert panel.vbar.winfo_ismapped()


def test_clip_buttons_are_in_the_player_columns_pinned_footer(studio: UltimateAudioStudio) -> None:
    footer = studio.col2.add_footer()
    for widget in (studio.btn_save_clip, studio.chk_loop_clip, studio.btn_restore_original):
        assert _is_inside(widget, footer)
        assert not _is_inside(widget, studio.col2.body)


def test_volume_boost_label_keeps_its_width_so_the_slider_does_not_move(studio: UltimateAudioStudio) -> None:
    label = studio.lbl_gain
    width = label.winfo_reqwidth()
    font = tkfont.nametofont(str(label.cget("font")), root=studio.root)

    for value in (12.0, -12.0, 0.5):
        studio.scale_gain.set(value)
        studio.on_gain_change(value)
        assert label.winfo_reqwidth() == width
        assert font.measure(str(label.cget("text"))) <= width - _inset(label)

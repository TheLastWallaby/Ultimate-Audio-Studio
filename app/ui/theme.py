"""Accessible design system tokens: senior-friendly fonts, high-contrast palette, and colors.

Fonts are Tk *named fonts*: widgets reference them by name, so calling ``apply_text_size``
resizes every label and button in the running app at once. ``init_fonts`` must run after the
Tk root exists and before widgets are built; until then the names fall back to Tk's default font.
"""

from __future__ import annotations

import os
import tkinter as tk
import tkinter.font as tkfont
import weakref
from typing import Literal

FONT_FAMILY = "Segoe UI" if os.name == "nt" else "Helvetica"

FONT_APP_TITLE = "UASAppTitle"
FONT_STEP_BADGE = "UASStepBadge"
FONT_SECTION_HDR = "UASSectionHeader"
FONT_BODY = "UASBody"
FONT_BODY_BOLD = "UASBodyBold"
FONT_TIME_LARGE = "UASTimeLarge"
FONT_HERO_TITLE = "UASHeroTitle"
FONT_HERO_ARTIST = "UASHeroArtist"
FONT_BTN_MAIN = "UASButtonMain"
FONT_BTN_SUB = "UASButtonSub"
FONT_STATUS_BAR = "UASStatusBar"
FONT_TOOLTIP = "UASTooltip"
FONT_SMALL_BOLD = "UASSmallBold"
FONT_HINT = "UASHint"
# Waveform time ruler (was a fixed 7pt font that ignored Text Size).
FONT_WAVE_RULER = "UASWaveRuler"

_Weight = Literal["normal", "bold"]
_Slant = Literal["roman", "italic"]

# Base point sizes at the "Normal" text size (previously 9-11pt; raised for readability at 65+).
_FONT_SPECS: dict[str, tuple[int, _Weight, _Slant]] = {
    FONT_APP_TITLE: (16, "bold", "roman"),
    FONT_STEP_BADGE: (13, "bold", "roman"),
    FONT_SECTION_HDR: (13, "bold", "roman"),
    FONT_BODY: (12, "normal", "roman"),
    FONT_BODY_BOLD: (12, "bold", "roman"),
    FONT_TIME_LARGE: (14, "bold", "roman"),
    FONT_HERO_TITLE: (15, "bold", "roman"),
    FONT_HERO_ARTIST: (12, "normal", "roman"),
    FONT_BTN_MAIN: (13, "bold", "roman"),
    FONT_BTN_SUB: (12, "bold", "roman"),
    FONT_STATUS_BAR: (12, "bold", "roman"),
    FONT_TOOLTIP: (12, "normal", "roman"),
    FONT_SMALL_BOLD: (11, "bold", "roman"),
    FONT_HINT: (11, "normal", "italic"),
    FONT_WAVE_RULER: (9, "normal", "roman"),
}

TEXT_SIZES: dict[str, float] = {"Normal": 1.0, "Large": 1.2, "Extra Large": 1.4}
DEFAULT_TEXT_SIZE = "Normal"


# tkinter deletes a named font when its Font object is garbage-collected, so keep them alive
# (keyed by Tk interpreter, since tests create several roots).
_live_fonts: dict[str, dict[str, tkfont.Font]] = {}


def init_fonts(root: tk.Misc, text_size: str = DEFAULT_TEXT_SIZE) -> None:
    """Create (or update) all named fonts for the given text size."""
    scale = TEXT_SIZES.get(text_size, 1.0)
    _text_scale_by_interp[_interp_key(root)] = scale
    fonts = _live_fonts.setdefault(str(root.winfo_toplevel().tk), {})
    existing = set(tkfont.names(root))
    for name, (size, weight, slant) in _FONT_SPECS.items():
        scaled = round(size * scale)
        font = fonts.get(name)
        if font is None or name not in existing:
            font = tkfont.Font(root=root, name=name, family=FONT_FAMILY, size=scaled, weight=weight, slant=slant)
            fonts[name] = font
        else:
            font.configure(family=FONT_FAMILY, size=scaled, weight=weight, slant=slant)


def apply_text_size(root: tk.Misc, text_size: str) -> None:
    """Resize every widget using the theme fonts, and every registered pixel size, live."""
    init_fonts(root, text_size)
    rescale_widgets()


# --- Pixel sizes that follow Text Size -------------------------------------------------------------
# Fonts scale by themselves (named fonts, in points). Pixel sizes (slider thickness and handle length,
# canvas heights, button padding) do not, so a bigger Text Size used to leave thin sliders and tiny
# targets. Widgets registered here are resized with the text, and also by the Windows display
# scaling (125%/150%), because Tk pixel options are physical pixels.

_text_scale_by_interp: dict[str, float] = {}
_scaled_widgets: list[tuple[weakref.ref[tk.Misc], dict[str, float]]] = []


def _interp_key(widget: tk.Misc) -> str:
    return str(widget.winfo_toplevel().tk)


def _dpi_scale(widget: tk.Misc) -> float:
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except tk.TclError:
        return 1.0


def ui_scale(widget: tk.Misc) -> float:
    """Combined Text Size and display-scaling factor for pixel sizes (1.0 at Normal, 100%)."""
    return _text_scale_by_interp.get(_interp_key(widget), 1.0) * _dpi_scale(widget)


def scaled_px(widget: tk.Misc, base: float) -> int:
    """A pixel size designed at Normal text / 100% scaling, adjusted for the current settings."""
    return max(1, round(base * ui_scale(widget)))


def register_scaled(widget: tk.Misc, **base_px: float) -> None:
    """Apply pixel options (e.g. ``width=22, sliderlength=32``) now and whenever Text Size changes."""
    widget.configure(**{opt: scaled_px(widget, val) for opt, val in base_px.items()})
    if len(_scaled_widgets) > 400:  # dialogs come and go; forget their destroyed buttons
        _scaled_widgets[:] = [(ref, base) for ref, base in _scaled_widgets if ref() is not None]
    _scaled_widgets.append((weakref.ref(widget), dict(base_px)))


def rescale_widgets() -> None:
    """Re-apply every registered pixel size (dropping widgets that no longer exist)."""
    alive = []
    for ref, base_px in _scaled_widgets:
        widget = ref()
        try:
            if widget is None or not widget.winfo_exists():
                continue
            widget.configure(**{opt: scaled_px(widget, val) for opt, val in base_px.items()})
        except tk.TclError:
            continue
        alive.append((ref, base_px))
    _scaled_widgets[:] = alive


def next_text_size(current: str) -> str:
    """Cycle Normal -> Large -> Extra Large -> Normal."""
    names = list(TEXT_SIZES)
    idx = names.index(current) if current in names else 0
    return names[(idx + 1) % len(names)]


# Color Palette
BG_ROOT = "#f1f5f9"  # Soft modern slate background
BG_CARD = "#ffffff"  # Crisp solid white panels
BG_SUB_CARD = "#f8fafc"  # Soft neutral container
BG_INPUT = "#ffffff"  # Pure white input background
BORDER_MAIN = "#cbd5e1"  # Clean slate border (decorative)
BORDER_FOCUS = "#0284c7"  # Focus outline

TEXT_DARK = "#0f172a"  # Almost black, highest legibility
TEXT_MEDIUM = "#334155"  # Crisp charcoal for subtitles
TEXT_MUTED = "#475569"  # Muted slate for secondary labels (WCAG AAA contrast)

# Action Colors: all >= 4.5:1 with white text (WCAG AA for normal-size text).
COLOR_PLAY = "#15803d"  # Green (Play) 5.0:1
COLOR_PLAY_HV = "#166534"
COLOR_PAUSE = "#b45309"  # Amber (Pause) 5.0:1
COLOR_PAUSE_HV = "#92400e"
COLOR_STOP = "#dc2626"  # Red (Stop) 4.8:1
COLOR_STOP_HV = "#b91c1c"
COLOR_ACCENT = "#0369a1"  # Blue (Primary buttons / Selection) 5.9:1
COLOR_ACCENT_HV = "#075985"
COLOR_EXPORT = "#7c3aed"  # Purple (Export) 5.7:1
COLOR_EXPORT_HV = "#6d28d9"
COLOR_DOWNLOAD = "#c2410c"  # Orange (Download / Save) 5.2:1
COLOR_DOWNLOAD_HV = "#9a3412"

# Neutral buttons get a visible dark outline (see create_button): the old
# #f1f5f9-on-white fill was 1.1:1 and did not read as a button.
COLOR_BTN_NEUTRAL = "#e2e8f0"
COLOR_BTN_NEUTRAL_HV = "#cbd5e1"
COLOR_DANGER_BG = "#fee2e2"
COLOR_DANGER_TEXT = "#b91c1c"
COLOR_DANGER_HV = "#fecaca"

"""Accessible design system tokens: senior-friendly fonts, high-contrast palette, and colors."""

import os

FONT_FAMILY = "Segoe UI" if os.name == "nt" else "Helvetica"

FONT_APP_TITLE   = (FONT_FAMILY, 14, "bold")
FONT_STEP_BADGE  = (FONT_FAMILY, 11, "bold")
FONT_SECTION_HDR = (FONT_FAMILY, 11, "bold")
FONT_BODY        = (FONT_FAMILY, 10)
FONT_BODY_BOLD   = (FONT_FAMILY, 10, "bold")
FONT_TIME_LARGE  = (FONT_FAMILY, 12, "bold")
FONT_HERO_TITLE  = (FONT_FAMILY, 12, "bold")
FONT_HERO_ARTIST = (FONT_FAMILY, 10)
FONT_BTN_MAIN    = (FONT_FAMILY, 11, "bold")
FONT_BTN_SUB     = (FONT_FAMILY, 10, "bold")
FONT_STATUS_BAR  = (FONT_FAMILY, 10, "bold")
FONT_TOOLTIP     = (FONT_FAMILY, 10)

# Color Palette
BG_ROOT       = "#f1f5f9"       # Soft modern slate background
BG_CARD       = "#ffffff"       # Crisp solid white panels
BG_SUB_CARD   = "#f8fafc"       # Soft neutral container
BG_INPUT      = "#ffffff"       # Pure white input background
BORDER_MAIN   = "#cbd5e1"       # Clean slate border
BORDER_FOCUS  = "#0284c7"       # Focus outline

TEXT_DARK     = "#0f172a"       # Almost black, highest legibility
TEXT_MEDIUM   = "#334155"       # Crisp charcoal for subtitles
TEXT_MUTED    = "#475569"       # Muted slate for secondary labels (WCAG AAA contrast)

# Action Colors (Bright, distinct, high-contrast)
COLOR_PLAY      = "#16a34a"     # Emerald Green (Play)
COLOR_PLAY_HV   = "#15803d"
COLOR_PAUSE     = "#d97706"     # Deep Amber (Pause)
COLOR_PAUSE_HV  = "#b45309"
COLOR_STOP      = "#dc2626"     # Bright Red (Stop)
COLOR_STOP_HV   = "#b91c1c"
COLOR_ACCENT    = "#0284c7"     # Ocean Blue (Primary buttons / Selection)
COLOR_ACCENT_HV = "#0369a1"
COLOR_EXPORT    = "#7c3aed"     # Royal Purple (Export)
COLOR_EXPORT_HV = "#6d28d9"
COLOR_DOWNLOAD  = "#ea580c"     # Vibrant Rust / Orange
COLOR_DOWNLOAD_HV = "#c2410c"

COLOR_BTN_NEUTRAL    = "#f1f5f9"
COLOR_BTN_NEUTRAL_HV = "#e2e8f0"
COLOR_DANGER_BG      = "#fee2e2"
COLOR_DANGER_TEXT    = "#b91c1c"
COLOR_DANGER_HV      = "#fecaca"

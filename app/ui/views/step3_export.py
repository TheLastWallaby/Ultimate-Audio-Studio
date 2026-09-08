"""Step 3 View: Playlist manager, track reordering, USB flash drive export, and CD burn options."""

import tkinter as tk
from tkinter import ttk
from app.config import DEFAULT_PLAYLIST_NAME
from app.ui.theme import (
    FONT_STEP_BADGE, FONT_SECTION_HDR, FONT_BODY, FONT_BODY_BOLD,
    FONT_BTN_MAIN, FONT_BTN_SUB,
    BG_CARD, BG_SUB_CARD, BORDER_MAIN, TEXT_DARK, TEXT_MUTED,
    COLOR_PLAY, COLOR_PLAY_HV, COLOR_ACCENT,
    COLOR_EXPORT, COLOR_EXPORT_HV, COLOR_BTN_NEUTRAL, COLOR_BTN_NEUTRAL_HV,
    COLOR_DANGER_BG, COLOR_DANGER_TEXT, COLOR_DANGER_HV
)
from app.ui.components import create_button, ToolTip, scrolled_listbox


def build_step3_view(parent, app):
    """Construct Step 3 UI widgets on parent container and attach references to app."""
    # Header Badge
    f_hdr = tk.Frame(parent, bg="#f3e8ff", padx=8, pady=4, highlightbackground="#d8b4fe", highlightthickness=1)
    f_hdr.pack(fill=tk.X, pady=(0, 6))
    tk.Label(f_hdr, text="STEP 3: Playlist & Export", font=FONT_STEP_BADGE, fg="#6b21a8", bg="#f3e8ff").pack()

    # Playlist Selector & Tools
    tk.Label(parent, text="Choose a Playlist:", font=FONT_SECTION_HDR, fg=TEXT_DARK, bg=BG_CARD).pack(anchor="w")

    f_pl_pick = tk.Frame(parent, bg=BG_CARD)
    f_pl_pick.pack(fill=tk.X, pady=1)
    app.playlist_var = tk.StringVar(value=DEFAULT_PLAYLIST_NAME)
    app.cmb_playlists = ttk.Combobox(f_pl_pick, textvariable=app.playlist_var, font=FONT_BODY_BOLD, state="readonly")
    app.cmb_playlists.pack(side=tk.LEFT, fill=tk.X, expand=True)
    app.cmb_playlists.bind("<<ComboboxSelected>>", app.on_playlist_selected)

    f_pl_mgmt = tk.Frame(parent, bg=BG_CARD)
    f_pl_mgmt.pack(fill=tk.X, pady=2)
    btn_new_pl = create_button(f_pl_mgmt, "➕ New", app.create_playlist, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=2)
    btn_new_pl.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
    ToolTip(btn_new_pl, "Create a brand new empty playlist")

    btn_ren_pl = create_button(f_pl_mgmt, "✏ Rename", app.rename_playlist, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=2)
    btn_ren_pl.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
    ToolTip(btn_ren_pl, "Rename the currently active playlist")

    btn_del_pl = create_button(f_pl_mgmt, "🗑 Delete", app.delete_playlist, bg=COLOR_DANGER_BG, fg=COLOR_DANGER_TEXT, hover_bg=COLOR_DANGER_HV, font=FONT_BTN_SUB, pady=2)
    btn_del_pl.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
    ToolTip(btn_del_pl, "Delete this playlist (does not delete audio files)")

    # Add to Playlist Button
    btn_add_pl = create_button(parent, "➕ Add Selected Song to Playlist", app.add_to_playlist, bg="#e0f2fe", fg=COLOR_ACCENT, hover_bg="#bae6fd", font=FONT_BTN_MAIN, pady=4)
    btn_add_pl.pack(fill=tk.X, pady=2)
    ToolTip(btn_add_pl, "Add the currently highlighted song in the library into this playlist")

    # Playlist Listbox
    pl_wrap, app.listbox_pl = scrolled_listbox(parent, font=FONT_BODY, selectmode=tk.SINGLE)
    pl_wrap.pack(fill=tk.BOTH, expand=True, pady=2)
    app.listbox_pl.bind("<Double-Button-1>", app.on_playlist_double_click)

    # Track Reordering Row
    f_reorder = tk.Frame(parent, bg=BG_CARD)
    f_reorder.pack(fill=tk.X, pady=1)
    btn_up = create_button(f_reorder, "▲ Up", app.pl_move_up, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=2)
    btn_up.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)

    btn_down = create_button(f_reorder, "▼ Down", app.pl_move_down, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=2)
    btn_down.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)

    btn_rem = create_button(f_reorder, "✕ Remove", app.pl_remove, bg=COLOR_DANGER_BG, fg=COLOR_DANGER_TEXT, hover_bg=COLOR_DANGER_HV, font=FONT_BTN_SUB, pady=2)
    btn_rem.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)

    # Playlist Playback Controls
    f_pl_play = tk.Frame(parent, bg=BG_CARD)
    f_pl_play.pack(fill=tk.X, pady=2)
    btn_pl_play = create_button(f_pl_play, "▶ Play Playlist", app.play_playlist, bg=COLOR_PLAY, fg="#ffffff", hover_bg=COLOR_PLAY_HV, font=FONT_BTN_MAIN, pady=4)
    btn_pl_play.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)

    btn_pl_prev = create_button(f_pl_play, "⏮ Prev", app.play_prev_in_playlist, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=3, padx=4)
    btn_pl_prev.pack(side=tk.LEFT, padx=1)

    btn_pl_next = create_button(f_pl_play, "Next ⏭", app.play_next_in_playlist, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=3, padx=4)
    btn_pl_next.pack(side=tk.LEFT, padx=1)

    tk.Checkbutton(parent, text="Repeat playlist", variable=app.repeat_playlist, font=FONT_BODY, bg=BG_CARD).pack(fill=tk.X, pady=1)

    # Export Container Card
    f_exp_card = tk.Frame(parent, bg=BG_SUB_CARD, padx=6, pady=6, highlightbackground=BORDER_MAIN, highlightthickness=1)
    f_exp_card.pack(fill=tk.X, pady=2)

    f_exp_top = tk.Frame(f_exp_card, bg=BG_SUB_CARD)
    f_exp_top.pack(fill=tk.X)
    tk.Label(f_exp_top, text="Export To:", font=FONT_SECTION_HDR, fg=TEXT_DARK, bg=BG_SUB_CARD).pack(side=tk.LEFT)
    app.export_var = tk.StringVar(value="USB")
    tk.Radiobutton(f_exp_top, text="💾 USB", variable=app.export_var, value="USB", font=FONT_BODY_BOLD, bg=BG_SUB_CARD).pack(side=tk.LEFT, padx=4)
    tk.Radiobutton(f_exp_top, text="💿 CD", variable=app.export_var, value="CD", font=FONT_BODY_BOLD, bg=BG_SUB_CARD).pack(side=tk.LEFT, padx=4)

    f_usb = tk.Frame(f_exp_card, bg=BG_SUB_CARD)
    f_usb.pack(fill=tk.X, pady=2)
    app.cmb_usb = ttk.Combobox(f_usb, textvariable=app.usb_choice, font=FONT_BODY, state="readonly")
    app.cmb_usb.pack(side=tk.LEFT, fill=tk.X, expand=True)
    btn_usb_ref = create_button(f_usb, "🔄", app.refresh_usb_drives, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=1, padx=4)
    btn_usb_ref.pack(side=tk.LEFT, padx=(3, 0))
    ToolTip(btn_usb_ref, "Check for plugged-in USB flash drives")

    app.btn_usb_eject = create_button(f_usb, "⏏ Eject", app.eject_selected_usb, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, hover_bg=COLOR_BTN_NEUTRAL_HV, font=FONT_BTN_SUB, pady=1, padx=5)
    app.btn_usb_eject.pack(side=tk.LEFT, padx=(3, 0))
    ToolTip(app.btn_usb_eject, "Safely eject the selected USB drive so it can be unplugged")

    tk.Checkbutton(f_exp_card, text="Make all songs equally loud", variable=app.even_volume, font=FONT_BODY, bg=BG_SUB_CARD).pack(fill=tk.X, pady=1)

    app.prog_export = ttk.Progressbar(parent, mode="determinate", style="Export.Horizontal.TProgressbar")

    app.btn_export = create_button(parent, "⚡ Export Playlist Now", app.export_playlist, bg=COLOR_EXPORT, fg="#ffffff", hover_bg=COLOR_EXPORT_HV, font=FONT_BTN_MAIN, pady=6)
    app.btn_export.pack(fill=tk.X, pady=(2, 1))
    ToolTip(app.btn_export, "Copy this entire playlist to your USB flash drive or CD burn folder")

    btn_help = create_button(parent, "❓ Help Guide", app.show_help, bg=COLOR_BTN_NEUTRAL, fg=TEXT_MUTED, font=FONT_BTN_SUB, pady=2)
    btn_help.pack(fill=tk.X, pady=(1, 0))
    ToolTip(btn_help, "Open a simple guide explaining how each feature works")

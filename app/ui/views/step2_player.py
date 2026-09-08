"""Step 2 View: Track hero card, transport controls, waveform viewer, and clip trimming."""

import tkinter as tk
from tkinter import ttk
from app.ui.theme import (
    FONT_STEP_BADGE, FONT_HERO_TITLE, FONT_HERO_ARTIST, FONT_FAMILY,
    FONT_BTN_MAIN, FONT_BTN_SUB, FONT_BODY, FONT_BODY_BOLD, FONT_TIME_LARGE,
    BG_CARD, BG_SUB_CARD, BORDER_MAIN, TEXT_DARK, TEXT_MUTED,
    COLOR_PLAY, COLOR_PLAY_HV, COLOR_PAUSE, COLOR_PAUSE_HV,
    COLOR_STOP, COLOR_STOP_HV, COLOR_ACCENT, COLOR_ACCENT_HV,
    COLOR_DOWNLOAD, COLOR_DOWNLOAD_HV, COLOR_BTN_NEUTRAL, COLOR_BTN_NEUTRAL_HV
)
from app.ui.components import create_button, ToolTip


def build_step2_view(parent, app):
    """Construct Step 2 UI widgets on parent container and attach references to app."""
    # Header Badge
    f_hdr = tk.Frame(parent, bg="#fef3c7", padx=8, pady=4, highlightbackground="#fcd34d", highlightthickness=1)
    f_hdr.pack(fill=tk.X, pady=(0, 6))
    tk.Label(f_hdr, text="STEP 2: Play & Clip Audio", font=FONT_STEP_BADGE, fg="#92400e", bg="#fef3c7").pack()

    # Track Hero Card with Album Art & Live Status
    app.f_track_card = tk.Frame(parent, bg=BG_SUB_CARD, padx=6, pady=6, relief=tk.FLAT, highlightthickness=2, highlightbackground=BORDER_MAIN)
    app.f_track_card.pack(fill=tk.X, pady=(0, 4))

    app.canvas_cover = tk.Canvas(app.f_track_card, width=60, height=60, bg="#e2e8f0", highlightthickness=1, highlightbackground=BORDER_MAIN)
    app.canvas_cover.pack(side=tk.LEFT, padx=(0, 8))
    app._draw_placeholder_cover()

    f_track_info = tk.Frame(app.f_track_card, bg=BG_SUB_CARD)
    f_track_info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    app.lbl_selected = tk.Label(f_track_info, text="No song selected", font=FONT_HERO_TITLE, fg=COLOR_ACCENT, bg=BG_SUB_CARD, wraplength=260, justify="left", anchor="w")
    app.lbl_selected.pack(fill=tk.X)

    app.lbl_selected_artist = tk.Label(f_track_info, text="Click a song on the left to start", font=FONT_HERO_ARTIST, fg=TEXT_MUTED, bg=BG_SUB_CARD, wraplength=260, justify="left", anchor="w")
    app.lbl_selected_artist.pack(fill=tk.X, pady=(1, 2))

    f_state_row = tk.Frame(f_track_info, bg=BG_SUB_CARD)
    f_state_row.pack(fill=tk.X, anchor="w", pady=(1, 0))

    app.lbl_track_state = tk.Label(f_state_row, text="⏹ READY", font=(FONT_FAMILY, 9, "bold"), fg="#ffffff", bg="#64748b", padx=5, pady=1)
    app.lbl_track_state.pack(side=tk.LEFT)

    app.canvas_vu = tk.Canvas(f_state_row, width=80, height=14, bg=BG_SUB_CARD, highlightthickness=0)
    app.canvas_vu.pack(side=tk.LEFT, padx=(8, 0))
    ToolTip(app.canvas_vu, "Audio Activity Indicator (Live playback levels)")

    # Transport Buttons Row
    f_play_row = tk.Frame(parent, bg=BG_CARD)
    f_play_row.pack(fill=tk.X, pady=3)

    btn_play = create_button(f_play_row, "▶ PLAY", app.play_main, bg=COLOR_PLAY, fg="#ffffff", hover_bg=COLOR_PLAY_HV, font=FONT_BTN_MAIN, pady=6)
    btn_play.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
    ToolTip(btn_play, "Play current song or preview clip")

    btn_pause = create_button(f_play_row, "⏸ PAUSE", app.pause_audio, bg=COLOR_PAUSE, fg="#ffffff", hover_bg=COLOR_PAUSE_HV, font=FONT_BTN_MAIN, pady=6)
    btn_pause.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
    ToolTip(btn_pause, "Pause playback")

    btn_stop = create_button(f_play_row, "⏹ STOP", lambda: app.stop_audio(user=True), bg=COLOR_STOP, fg="#ffffff", hover_bg=COLOR_STOP_HV, font=FONT_BTN_MAIN, pady=6)
    btn_stop.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
    ToolTip(btn_stop, "Stop playback and return to start")

    # Skip & Volume Row
    f_skip = tk.Frame(parent, bg=BG_CARD)
    f_skip.pack(fill=tk.X, pady=2)

    btn_back10 = create_button(f_skip, "⏪ -10s", lambda: app.skip_by(-10), bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=2, padx=4)
    btn_back10.pack(side=tk.LEFT, padx=1)
    ToolTip(btn_back10, "Rewind playback by 10 seconds")

    app.btn_mute = create_button(
        f_skip, "🔊", app.toggle_mute,
        bg=BG_CARD, fg=TEXT_DARK, hover_bg=COLOR_BTN_NEUTRAL_HV,
        font=FONT_BODY_BOLD, pady=1, padx=3
    )
    app.btn_mute.pack(side=tk.LEFT, padx=(2, 0))
    ToolTip(app.btn_mute, "Mute / Unmute audio")

    app.scale_volume = tk.Scale(
        f_skip, from_=0, to=100, orient=tk.HORIZONTAL, showvalue=0,
        bg=BG_CARD, fg=TEXT_DARK, troughcolor="#cbd5e1", highlightthickness=0,
        relief=tk.FLAT, command=app.on_volume_change, sliderlength=18, width=14
    )
    vol_init = getattr(app, "_saved_volume", 80)
    app.scale_volume.set(vol_init)
    app.scale_volume.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

    app.lbl_vol_pct = tk.Label(f_skip, text=f"{vol_init}%", font=FONT_BODY_BOLD, fg=TEXT_DARK, bg=BG_CARD, width=4, anchor="w")
    app.lbl_vol_pct.pack(side=tk.LEFT, padx=(1, 2))

    btn_fwd10 = create_button(f_skip, "+10s ⏩", lambda: app.skip_by(10), bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=2, padx=4)
    btn_fwd10.pack(side=tk.LEFT, padx=1)
    ToolTip(btn_fwd10, "Jump forward by 10 seconds")

    # Auto-level Volume Toggle Row
    f_vol_opt = tk.Frame(parent, bg=BG_CARD)
    f_vol_opt.pack(fill=tk.X, pady=(0, 2))
    app.auto_level_playback = tk.BooleanVar(value=getattr(app, "_saved_auto_level_playback", False))
    app.chk_auto_level = tk.Checkbutton(
        f_vol_opt, text="Auto-level playback volume",
        variable=app.auto_level_playback,
        command=app.on_toggle_auto_level,
        font=FONT_BODY, bg=BG_CARD
    )
    app.chk_auto_level.pack(side=tk.RIGHT)
    ToolTip(app.chk_auto_level, "Automatically balance loudness so quiet and loud tracks play at comfortable levels")

    # Waveform Card
    f_wave_card = tk.Frame(parent, bg=BG_SUB_CARD, padx=6, pady=4, relief=tk.FLAT, highlightbackground=BORDER_MAIN, highlightthickness=1)
    f_wave_card.pack(fill=tk.X, pady=3)

    f_wave_top = tk.Frame(f_wave_card, bg=BG_SUB_CARD)
    f_wave_top.pack(fill=tk.X)
    tk.Label(f_wave_top, text="Interactive Waveform:", font=FONT_BODY_BOLD, fg=TEXT_DARK, bg=BG_SUB_CARD).pack(side=tk.LEFT)
    app.btn_zoom = create_button(f_wave_top, "🔍 Zoom Clip", app.toggle_waveform_zoom, bg=COLOR_BTN_NEUTRAL, fg=COLOR_ACCENT, font=FONT_BTN_SUB, pady=1, padx=4)
    app.btn_zoom.pack(side=tk.LEFT, padx=(6, 0))
    ToolTip(app.btn_zoom, "Zoom in on your trimmed clip section for fine-tuning")

    app.lbl_prog_time = tk.Label(f_wave_top, text="00:00 / 00:00", font=FONT_TIME_LARGE, fg=COLOR_ACCENT, bg=BG_SUB_CARD)
    app.lbl_prog_time.pack(side=tk.RIGHT)

    app.canvas_waveform = tk.Canvas(f_wave_card, height=58, bg="#ffffff", highlightthickness=1, highlightbackground=BORDER_MAIN)
    app.canvas_waveform.pack(fill=tk.X, pady=3)

    app.scale_progress = tk.Scale(
        f_wave_card, from_=0, to=100, orient=tk.HORIZONTAL, showvalue=0,
        bg=BG_SUB_CARD, fg=COLOR_PLAY, troughcolor="#e2e8f0", highlightthickness=0,
        command=app.on_progress_drag, resolution=0.1, sliderlength=20, width=14
    )
    app.scale_progress.pack(fill=tk.X)
    app.scale_progress.bind("<ButtonPress-1>", lambda e: app._mark_progress_drag(True))
    app.scale_progress.bind("<ButtonRelease-1>", app._on_progress_release)

    # Trimming Section
    f_trim_boxes = tk.Frame(parent, bg=BG_CARD)
    f_trim_boxes.pack(fill=tk.X, pady=2)

    # Start Mark Box (Blue)
    f_s_box = tk.Frame(f_trim_boxes, bg="#eff6ff", padx=4, pady=3, highlightbackground="#93c5fd", highlightthickness=1)
    f_s_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))

    f_s_top = tk.Frame(f_s_box, bg="#eff6ff")
    f_s_top.pack(fill=tk.X)
    app.lbl_start_time = tk.Label(f_s_top, text="Start: 00:00", font=FONT_BODY_BOLD, fg="#1d4ed8", bg="#eff6ff")
    app.lbl_start_time.pack(side=tk.LEFT, anchor="w")
    app.btn_edit_start = create_button(
        f_s_top, "✏ Edit", app.edit_start_time,
        bg="#dbeafe", fg="#1d4ed8", hover_bg="#bfdbfe",
        font=FONT_BTN_SUB, pady=1, padx=4
    )
    app.btn_edit_start.pack(side=tk.RIGHT)
    ToolTip(app.btn_edit_start, "Type exact Start time (MM:SS)")

    btn_mark_s = create_button(f_s_box, "⬅ Set Start", app.set_start_here, bg="#2563eb", fg="#ffffff", hover_bg="#1d4ed8", font=FONT_BTN_SUB, pady=2)
    btn_mark_s.pack(fill=tk.X, pady=1)
    ToolTip(btn_mark_s, "Set clip start to current playback position")

    f_s_nudge = tk.Frame(f_s_box, bg="#eff6ff")
    f_s_nudge.pack(fill=tk.X)
    btn_ns_m1 = create_button(f_s_nudge, "-1s", lambda: app.nudge_clip_start(-1.0), bg="#dbeafe", fg="#1d4ed8", font=FONT_BTN_SUB, pady=1, padx=2)
    btn_ns_m1.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
    ToolTip(btn_ns_m1, "Nudge Start backward 1.0s")
    btn_ns_mf = create_button(f_s_nudge, "-0.1s", lambda: app.nudge_clip_start(-0.1), bg="#dbeafe", fg="#1d4ed8", font=FONT_BTN_SUB, pady=1, padx=1)
    btn_ns_mf.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
    ToolTip(btn_ns_mf, "Fine-tune Start backward 0.1s")
    btn_ns_pf = create_button(f_s_nudge, "+0.1s", lambda: app.nudge_clip_start(0.1), bg="#dbeafe", fg="#1d4ed8", font=FONT_BTN_SUB, pady=1, padx=1)
    btn_ns_pf.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
    ToolTip(btn_ns_pf, "Fine-tune Start forward 0.1s")
    btn_ns_p1 = create_button(f_s_nudge, "+1s", lambda: app.nudge_clip_start(1.0), bg="#dbeafe", fg="#1d4ed8", font=FONT_BTN_SUB, pady=1, padx=2)
    btn_ns_p1.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ToolTip(btn_ns_p1, "Nudge Start forward 1.0s")

    # End Mark Box (Red)
    f_e_box = tk.Frame(f_trim_boxes, bg="#fef2f2", padx=4, pady=3, highlightbackground="#fca5a5", highlightthickness=1)
    f_e_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))

    f_e_top = tk.Frame(f_e_box, bg="#fef2f2")
    f_e_top.pack(fill=tk.X)
    app.lbl_end_time = tk.Label(f_e_top, text="End: 00:00", font=FONT_BODY_BOLD, fg="#b91c1c", bg="#fef2f2")
    app.lbl_end_time.pack(side=tk.LEFT, anchor="w")
    app.btn_edit_end = create_button(
        f_e_top, "✏ Edit", app.edit_end_time,
        bg="#fee2e2", fg="#b91c1c", hover_bg="#fecaca",
        font=FONT_BTN_SUB, pady=1, padx=4
    )
    app.btn_edit_end.pack(side=tk.RIGHT)
    ToolTip(app.btn_edit_end, "Type exact End time (MM:SS)")

    btn_mark_e = create_button(f_e_box, "Set End ➡", app.set_end_here, bg="#dc2626", fg="#ffffff", hover_bg="#b91c1c", font=FONT_BTN_SUB, pady=2)
    btn_mark_e.pack(fill=tk.X, pady=1)
    ToolTip(btn_mark_e, "Set clip end to current playback position")

    f_e_nudge = tk.Frame(f_e_box, bg="#fef2f2")
    f_e_nudge.pack(fill=tk.X)
    btn_ne_m1 = create_button(f_e_nudge, "-1s", lambda: app.nudge_clip_end(-1.0), bg="#fee2e2", fg="#b91c1c", font=FONT_BTN_SUB, pady=1, padx=2)
    btn_ne_m1.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
    ToolTip(btn_ne_m1, "Nudge End backward 1.0s")
    btn_ne_mf = create_button(f_e_nudge, "-0.1s", lambda: app.nudge_clip_end(-0.1), bg="#fee2e2", fg="#b91c1c", font=FONT_BTN_SUB, pady=1, padx=1)
    btn_ne_mf.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
    ToolTip(btn_ne_mf, "Fine-tune End backward 0.1s")
    btn_ne_pf = create_button(f_e_nudge, "+0.1s", lambda: app.nudge_clip_end(0.1), bg="#fee2e2", fg="#b91c1c", font=FONT_BTN_SUB, pady=1, padx=1)
    btn_ne_pf.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
    ToolTip(btn_ne_pf, "Fine-tune End forward 0.1s")
    btn_ne_p1 = create_button(f_e_nudge, "+1s", lambda: app.nudge_clip_end(1.0), bg="#fee2e2", fg="#b91c1c", font=FONT_BTN_SUB, pady=1, padx=2)
    btn_ne_p1.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ToolTip(btn_ne_p1, "Nudge End forward 1.0s")

    # Clip Length & Options Row
    f_clip_opts = tk.Frame(parent, bg=BG_CARD)
    f_clip_opts.pack(fill=tk.X, pady=1)
    app.lbl_clip_len = tk.Label(f_clip_opts, text="Clip: 00:00", font=FONT_BODY_BOLD, fg=TEXT_DARK, bg=BG_CARD)
    app.lbl_clip_len.pack(side=tk.LEFT)

    f_fade = tk.Frame(f_clip_opts, bg=BG_CARD)
    f_fade.pack(side=tk.RIGHT)
    tk.Checkbutton(f_fade, text="Smooth fade", variable=app.soften_clip, font=FONT_BODY, bg=BG_CARD).pack(side=tk.LEFT)
    app.cmb_fade_dur = ttk.Combobox(
        f_fade, textvariable=app.fade_choice_var,
        values=["0.5s (Quick)", "1.5s (Standard)", "3.0s (Smooth)"],
        width=13, state="readonly", font=FONT_BODY
    )
    app.cmb_fade_dur.pack(side=tk.LEFT, padx=(3, 0))
    ToolTip(app.cmb_fade_dur, "Choose fade duration for clip transitions")

    # Gain Boost Control
    f_gain = tk.Frame(parent, bg=BG_SUB_CARD, padx=4, pady=2, highlightbackground=BORDER_MAIN, highlightthickness=1)
    f_gain.pack(fill=tk.X, pady=1)
    f_gain_top = tk.Frame(f_gain, bg=BG_SUB_CARD)
    f_gain_top.pack(fill=tk.X)
    app.lbl_gain = tk.Label(f_gain_top, text="Volume Boost: 0 dB", font=FONT_BODY_BOLD, fg=TEXT_DARK, bg=BG_SUB_CARD)
    app.lbl_gain.pack(side=tk.LEFT)
    btn_res_gain = create_button(f_gain_top, "Reset", app.reset_gain, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=1, padx=4)
    btn_res_gain.pack(side=tk.RIGHT)

    app.scale_gain = tk.Scale(f_gain, from_=-12, to=12, resolution=0.5, orient=tk.HORIZONTAL, showvalue=0, bg=BG_SUB_CARD, fg=TEXT_DARK, troughcolor="#e2e8f0", highlightthickness=0, command=app.on_gain_change, sliderlength=18, width=12)
    app.scale_gain.set(0.0)
    app.scale_gain.pack(fill=tk.X)

    # Final Clip Action Buttons
    f_actions = tk.Frame(parent, bg=BG_CARD)
    f_actions.pack(fill=tk.X, pady=(3, 0))

    if not hasattr(app, "loop_clip"):
        app.loop_clip = tk.BooleanVar(value=False)

    app.chk_loop_clip = tk.Checkbutton(
        f_actions, text="🔁 Loop", variable=app.loop_clip,
        font=FONT_BODY_BOLD, bg=BG_CARD, fg=TEXT_DARK, selectcolor="#ffffff"
    )
    app.chk_loop_clip.pack(side=tk.LEFT, padx=(0, 4))
    ToolTip(app.chk_loop_clip, "Continuously repeat playback when testing your clip")

    btn_test = create_button(f_actions, "▶ Test Clip", app.test_clip, bg=COLOR_ACCENT, fg="#ffffff", hover_bg=COLOR_ACCENT_HV, font=FONT_BTN_MAIN, pady=5)
    btn_test.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
    ToolTip(btn_test, "Play only between the Start and End marks to preview your clip")

    app.btn_save_clip = create_button(f_actions, "💾 Save Clip", app.save_clip, bg=COLOR_DOWNLOAD, fg="#ffffff", hover_bg=COLOR_DOWNLOAD_HV, font=FONT_BTN_MAIN, pady=5)
    app.btn_save_clip.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))
    ToolTip(app.btn_save_clip, "Save this trimmed audio section as a high-quality 320k MP3 file in your library")

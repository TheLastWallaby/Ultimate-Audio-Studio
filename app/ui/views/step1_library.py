"""Step 1 View: YouTube downloader, music library, search filter, and file tools."""

import tkinter as tk
from tkinter import ttk
from app.ui.theme import (
    FONT_STEP_BADGE, FONT_SECTION_HDR, FONT_BODY, FONT_BODY_BOLD,
    FONT_BTN_MAIN, FONT_BTN_SUB, FONT_FAMILY,
    BG_CARD, BG_SUB_CARD, BG_INPUT, BORDER_MAIN, TEXT_DARK, TEXT_MUTED,
    COLOR_BTN_NEUTRAL, COLOR_BTN_NEUTRAL_HV, COLOR_DOWNLOAD, COLOR_DOWNLOAD_HV,
    COLOR_STOP, COLOR_DANGER_BG, COLOR_DANGER_TEXT, COLOR_DANGER_HV
)
from app.ui.components import create_button, ToolTip, scrolled_listbox


def build_step1_view(parent, app):
    """Construct Step 1 UI widgets on parent container and attach references to app."""
    # Header Badge
    f_hdr = tk.Frame(parent, bg="#e0f2fe", padx=8, pady=4, highlightbackground="#7dd3fc", highlightthickness=1)
    f_hdr.pack(fill=tk.X, pady=(0, 6))
    tk.Label(f_hdr, text="STEP 1: Download & Library", font=FONT_STEP_BADGE, fg="#0369a1", bg="#e0f2fe").pack()

    # YouTube Section Container
    f_yt_box = tk.Frame(parent, bg=BG_SUB_CARD, padx=8, pady=6, relief=tk.FLAT, highlightbackground=BORDER_MAIN, highlightthickness=1)
    f_yt_box.pack(fill=tk.X, pady=(0, 6))

    tk.Label(f_yt_box, text="Enter Song & Artist, or Paste YouTube Link:", font=FONT_SECTION_HDR, fg=TEXT_DARK, bg=BG_SUB_CARD).pack(anchor="w")
    app.entry_url = tk.Entry(f_yt_box, font=FONT_BODY, bg=BG_INPUT, fg=TEXT_DARK, insertbackground=TEXT_DARK, relief=tk.FLAT, highlightthickness=1, highlightbackground=BORDER_MAIN)
    app.entry_url.pack(fill=tk.X, pady=(3, 4), ipady=3)
    app.entry_url.bind("<FocusIn>", app._on_url_focus)
    app.entry_url.bind("<Return>", lambda _e: app.start_download())

    f_dl_btns = tk.Frame(f_yt_box, bg=BG_SUB_CARD)
    f_dl_btns.pack(fill=tk.X, pady=(1, 2))

    btn_paste = create_button(f_dl_btns, "📋 Paste", app.paste_youtube_link, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, hover_bg=COLOR_BTN_NEUTRAL_HV, font=FONT_BTN_SUB, pady=3, padx=6)
    btn_paste.pack(side=tk.LEFT, padx=(0, 4))
    ToolTip(btn_paste, "Paste YouTube link from your clipboard")

    app.btn_download = create_button(f_dl_btns, "⬇ Download MP3", app.start_download, bg=COLOR_DOWNLOAD, fg="#ffffff", hover_bg=COLOR_DOWNLOAD_HV, font=FONT_BTN_MAIN, pady=4, padx=8)
    app.btn_download.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ToolTip(app.btn_download, "Search YouTube by song & artist, or download directly from a link")

    app.btn_cancel_dl = create_button(f_yt_box, "⏹ Stop Download", app.cancel_download, bg=COLOR_BTN_NEUTRAL, fg=COLOR_STOP, hover_bg=COLOR_DANGER_BG, font=FONT_BTN_SUB, pady=2, state=tk.DISABLED)
    app.btn_cancel_dl.pack(fill=tk.X, pady=(3, 0))
    ToolTip(app.btn_cancel_dl, "Cancel ongoing download")

    app.prog_download = ttk.Progressbar(f_yt_box, mode="determinate", style="Download.Horizontal.TProgressbar")
    app.lbl_dl_metrics = tk.Label(f_yt_box, text="", font=FONT_BODY_BOLD, fg=COLOR_DOWNLOAD, bg=BG_SUB_CARD)

    # Library Section
    f_lib_hdr = tk.Frame(parent, bg=BG_CARD)
    f_lib_hdr.pack(fill=tk.X, pady=(4, 2))
    tk.Label(f_lib_hdr, text="Your Music Library:", font=FONT_SECTION_HDR, fg=TEXT_DARK, bg=BG_CARD).pack(side=tk.LEFT)

    btn_open = create_button(f_lib_hdr, "📁 Folder", app.open_library_folder, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=2, padx=5)
    btn_open.pack(side=tk.RIGHT)
    ToolTip(btn_open, "Open music folder in Windows File Explorer")

    btn_ch_folder = create_button(f_lib_hdr, "Change...", app.change_folder, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=2, padx=5)
    btn_ch_folder.pack(side=tk.RIGHT, padx=2)
    ToolTip(btn_ch_folder, "Choose a different folder for your music")

    # Search Bar
    f_search = tk.Frame(parent, bg=BG_CARD)
    f_search.pack(fill=tk.X, pady=3)
    tk.Label(f_search, text="🔍", font=FONT_BODY_BOLD, bg=BG_CARD, fg=TEXT_MUTED).pack(side=tk.LEFT, padx=(0, 3))
    app.entry_search = tk.Entry(f_search, font=FONT_BODY, bg=BG_INPUT, fg=TEXT_DARK, insertbackground=TEXT_DARK, relief=tk.FLAT, highlightthickness=1, highlightbackground=BORDER_MAIN)
    app.entry_search.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=2)
    app.entry_search.bind("<KeyRelease>", lambda e: app.on_search_key_release(e))
    ToolTip(app.entry_search, "Search songs by title, artist, or filename")

    btn_clear = create_button(f_search, "✕", app.clear_search, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=2, padx=6)
    btn_clear.pack(side=tk.RIGHT, padx=(3, 0))
    ToolTip(btn_clear, "Clear search query and show all songs")

    # Song Listbox (Extended selection allows Ctrl/Shift click for batch operations)
    lib_wrap, app.listbox_lib = scrolled_listbox(parent, font=FONT_BODY, selectmode=tk.EXTENDED)
    lib_wrap.pack(fill=tk.BOTH, expand=True, pady=3)
    app.listbox_lib.bind("<<ListboxSelect>>", app.on_library_select)
    app.listbox_lib.bind("<Double-Button-1>", app._on_library_double_click)

    # Song Tools
    f_lib_edit = tk.Frame(parent, bg=BG_CARD)
    f_lib_edit.pack(fill=tk.X, pady=(2, 2))

    btn_ren = create_button(f_lib_edit, "✏ Rename", app.rename_library_file, bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, font=FONT_BTN_SUB, pady=3)
    btn_ren.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 3))
    ToolTip(btn_ren, "Change song title or filename")

    btn_del = create_button(f_lib_edit, "🗑 Delete", app.delete_library_file, bg=COLOR_DANGER_BG, fg=COLOR_DANGER_TEXT, hover_bg=COLOR_DANGER_HV, font=FONT_BTN_SUB, pady=3)
    btn_del.pack(side=tk.LEFT, expand=True, fill=tk.X)
    ToolTip(btn_del, "Move selected song to the Windows Recycle Bin")

    # Import External Audio Button & Drag-and-Drop Hint
    app.btn_load_ext = create_button(
        parent, "📂 Add Music from PC", app.add_external_file,
        bg=COLOR_BTN_NEUTRAL, fg=TEXT_DARK, hover_bg=COLOR_BTN_NEUTRAL_HV, font=FONT_BTN_SUB, pady=4
    )
    app.btn_load_ext.pack(fill=tk.X, pady=(2, 0))
    ToolTip(app.btn_load_ext, "Import songs from any folder on your PC into this library")

    lbl_dnd_hint = tk.Label(parent, text="💡 Tip: You can also drag & drop music files here", font=(FONT_FAMILY, 9, "italic"), fg=TEXT_MUTED, bg=BG_CARD)
    lbl_dnd_hint.pack(pady=(2, 0))

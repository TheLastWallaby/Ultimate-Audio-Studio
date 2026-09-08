"""Interactive canvas waveform renderer with peak mirroring, marker dragging, and zoom support."""

from app.config import format_time
from app.core.waveform import get_waveform_bounds, time_from_waveform_x, time_to_waveform_x
from app.ui.theme import (
    BORDER_MAIN,
    COLOR_ACCENT,
    COLOR_DOWNLOAD,
    COLOR_PLAY,
    FONT_BODY,
    FONT_FAMILY,
    TEXT_MUTED,
)


class WaveformView:
    """Manages waveform drawing, marker handles, needle updates, and zoom toggling on a Tkinter Canvas."""

    MARKER_HIT_TOLERANCE = 22

    def __init__(self, canvas, app):
        self.canvas = canvas
        self.app = app
        self._resize_timer = None
        self._bar_colors = []
        self._last_sig = None

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self.on_hover)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

    def _on_configure(self, _event=None):
        if self._resize_timer:
            try:
                self.app.root.after_cancel(self._resize_timer)
            except Exception:
                pass
        self._resize_timer = self.app.root.after(80, lambda: self.render(full_redraw=True))

    def get_bounds(self):
        return get_waveform_bounds(
            getattr(self.app, "track_duration", 0.0),
            getattr(self.app, "clip_start_sec", 0.0),
            getattr(self.app, "clip_end_sec", 0.0),
            zoomed=getattr(self.app, "waveform_zoomed", False),
        )

    def time_to_x(self, t, w):
        z_start, z_end = self.get_bounds()
        return time_to_waveform_x(t, w, z_start, z_end)

    def time_from_x(self, px, w):
        z_start, z_end = self.get_bounds()
        return min(getattr(self.app, "track_duration", 0.0), time_from_waveform_x(px, w, z_start, z_end))

    def get_marker_x_coords(self):
        w = max(1, self.canvas.winfo_width())
        xs = self.time_to_x(self.app.clip_start_sec, w)
        xe = self.time_to_x(self.app.clip_end_sec, w)
        return xs, xe

    def toggle_zoom(self):
        self.app.waveform_zoomed = not getattr(self.app, "waveform_zoomed", False)
        if hasattr(self.app, "btn_zoom"):
            if self.app.waveform_zoomed:
                self.app.btn_zoom.config(text="🔍 Full Song", fg=COLOR_DOWNLOAD)
                self.app.set_status("Waveform zoomed to active clip region.")
            else:
                self.app.btn_zoom.config(text="🔍 Zoom Clip", fg=COLOR_ACCENT)
                self.app.set_status("Waveform showing full song.")
        self.render(full_redraw=True)

    def on_hover(self, event):
        if not self.app.selected_file_path or self.app.track_duration <= 0 or self.app._progress_dragging:
            return
        xs, xe = self.get_marker_x_coords()
        tol = self.MARKER_HIT_TOLERANCE
        if abs(event.x - xs) <= tol or abs(event.x - xe) <= tol:
            self.canvas.config(cursor="sb_h_double_arrow")
        else:
            self.canvas.config(cursor="")

    def render(self, full_redraw=False):
        c = self.canvas
        w = c.winfo_width()
        if w <= 1:
            w = 400
        h = c.winfo_height()
        if h <= 1:
            h = 58

        mid_y = h / 2.0
        dur = max(0.1, self.app.track_duration)
        s_time = self.app.clip_start_sec
        e_time = self.app.clip_end_sec
        curr_pos = (
            self.app._current_play_seconds()
            if (self.app.is_playing_main or self.app.is_playing_playlist)
            else float(self.app.scale_progress.get() if hasattr(self.app, "scale_progress") else 0.0)
        )

        xs = self.time_to_x(s_time, w)
        xe = self.time_to_x(e_time, w)
        xp = self.time_to_x(curr_pos, w)

        has_peaks = bool(self.app._current_peaks and any(p > 0.001 for p in self.app._current_peaks))
        current_sig = (
            w,
            h,
            id(self.app._current_peaks),
            len(self.app._current_peaks),
            has_peaks,
            bool(getattr(self.app, "waveform_zoomed", False)),
            round(self.app.clip_start_sec, 2),
            round(self.app.clip_end_sec, 2),
        )

        if full_redraw or self._last_sig != current_sig or not c.find_withtag("needle"):
            c.delete("all")
            self._last_sig = current_sig
            self._bar_colors = []

            # Shaded clip active zone
            c.create_rectangle(0, 0, xs, h, fill="#f1f5f9", outline="", tags="bg_left")
            c.create_rectangle(xs, 0, xe, h, fill="#e0f2fe", outline="", tags="bg_mid")
            c.create_rectangle(xe, 0, w, h, fill="#f1f5f9", outline="", tags="bg_right")

            # Centerline
            c.create_line(0, mid_y, w, mid_y, fill=BORDER_MAIN, tags="centerline")

            # Time Ruler with tick marks
            z_start, z_end = self.get_bounds()
            span = max(0.01, z_end - z_start)
            if span <= 20:
                step = 2.0
            elif span <= 60:
                step = 5.0
            elif span <= 120:
                step = 10.0
            elif span <= 300:
                step = 30.0
            elif span <= 600:
                step = 60.0
            else:
                step = 120.0

            import math

            curr_t = math.ceil(z_start / step) * step
            while curr_t <= z_end:
                tx = self.time_to_x(curr_t, w)
                if 22 <= tx <= w - 22:
                    c.create_line(tx, h - 6, tx, h, fill="#cbd5e1", width=1, tags="ruler")
                    c.create_text(
                        tx, h - 8, text=format_time(curr_t), fill="#64748b", font=(FONT_FAMILY, 7), tags="ruler"
                    )
                curr_t += step

            # Mirrored Peak Bars
            if has_peaks:
                n = len(self.app._current_peaks)
                bar_w = max(1.0, (w / n))
                for i in range(n):
                    t_bar = z_start + (i / n) * span
                    peak_idx = int((t_bar / dur) * len(self.app._current_peaks))
                    peak_idx = max(0, min(len(self.app._current_peaks) - 1, peak_idx))
                    p = self.app._current_peaks[peak_idx]
                    bx = i * bar_w
                    bh = max(2.0, p * (mid_y - 6))
                    if bx < xs or bx > xe:
                        color = "#94a3b8"  # Muted slate outside clip
                    elif bx <= xp:
                        color = COLOR_PLAY  # Played green
                    else:
                        color = COLOR_ACCENT  # Active ocean blue inside clip
                    self._bar_colors.append(color)
                    c.create_rectangle(
                        bx,
                        mid_y - bh,
                        bx + max(1, bar_w - 1),
                        mid_y + bh,
                        fill=color,
                        outline="",
                        tags=("bars", f"bar_{i}"),
                    )
            else:
                if not self.app.selected_file_path:
                    msg = "Click a song in your Library to play and trim audio"
                elif getattr(self.app, "_waveform_loading", False) or (
                    getattr(self.app, "_waveform_queue", None) and not self.app._waveform_queue.empty()
                ):
                    msg = "Analyzing audio waveform..."
                else:
                    msg = "Waveform preview unavailable for this track"
                c.create_text(w / 2, mid_y, text=msg, fill=TEXT_MUTED, font=FONT_BODY, tags="loading")

            # Zoom indicator badge on canvas
            if getattr(self.app, "waveform_zoomed", False):
                c.create_text(
                    w - 60, 10, text="🔍 Zoomed View", fill="#0284c7", font=(FONT_FAMILY, 8, "bold"), tags="zoom_tag"
                )

            # Start Marker Line & Top Handle (Blue)
            c.create_line(xs, 0, xs, h, fill="#2563eb", width=2, tags="marker_s_line")
            c.create_polygon(xs, 0, xs - 8, 12, xs + 8, 12, fill="#2563eb", tags="marker_s_poly")

            # End Marker Line & Top Handle (Red)
            c.create_line(xe, 0, xe, h, fill="#dc2626", width=2, tags="marker_e_line")
            c.create_polygon(xe, 0, xe - 8, 12, xe + 8, 12, fill="#dc2626", tags="marker_e_poly")

            # Playhead Needle (Dark Slate with Red Knob)
            c.create_line(xp, 0, xp, h, fill="#0f172a", width=2, tags="needle")
            c.create_oval(
                xp - 5, mid_y - 5, xp + 5, mid_y + 5, fill="#dc2626", outline="#ffffff", width=1.5, tags="knob"
            )
        else:
            # Fast incremental update
            c.coords("bg_left", 0, 0, xs, h)
            c.coords("bg_mid", xs, 0, xe, h)
            c.coords("bg_right", xe, 0, w, h)

            c.coords("marker_s_line", xs, 0, xs, h)
            c.coords("marker_s_poly", xs, 0, xs - 8, 12, xs + 8, 12)

            c.coords("marker_e_line", xe, 0, xe, h)
            c.coords("marker_e_poly", xe, 0, xe - 8, 12, xe + 8, 12)

            c.coords("needle", xp, 0, xp, h)
            c.coords("knob", xp - 5, mid_y - 5, xp + 5, mid_y + 5)

            if getattr(self.app, "waveform_zoomed", False):
                if not c.find_withtag("zoom_tag"):
                    c.create_text(
                        w - 60,
                        10,
                        text="🔍 Zoomed View",
                        fill="#0284c7",
                        font=(FONT_FAMILY, 8, "bold"),
                        tags="zoom_tag",
                    )
                else:
                    c.coords("zoom_tag", w - 60, 10)
            else:
                c.delete("zoom_tag")

            if has_peaks and not self.app._progress_dragging:
                n = len(self.app._current_peaks)
                bar_w = max(1.0, (w / n))
                if len(self._bar_colors) != n:
                    self._bar_colors = [""] * n
                for i in range(n):
                    bx = i * bar_w
                    if bx < xs or bx > xe:
                        color = "#94a3b8"
                    elif bx <= xp:
                        color = COLOR_PLAY
                    else:
                        color = COLOR_ACCENT
                    if self._bar_colors[i] != color:
                        c.itemconfig(f"bar_{i}", fill=color)
                        self._bar_colors[i] = color

            c.tag_raise("needle")
            c.tag_raise("knob")

    def on_click(self, event):
        if not self.app.selected_file_path or self.app.track_duration <= 0:
            return
        w = max(1, self.canvas.winfo_width())
        xs, xe = self.get_marker_x_coords()

        dist_s = abs(event.x - xs)
        dist_e = abs(event.x - xe)
        tol = self.MARKER_HIT_TOLERANCE
        if dist_s <= tol and (dist_s <= dist_e or dist_e > tol):
            self.app._dragging_marker = "start"
            self.app._progress_dragging = True
            return
        elif dist_e <= tol:
            self.app._dragging_marker = "end"
            self.app._progress_dragging = True
            return

        self.app._dragging_marker = "playhead"
        target_sec = self.time_from_x(event.x, w)
        self.app._updating_ui = True
        self.app.scale_progress.set(target_sec)
        self.app.lbl_prog_time.config(text=self.app._prog_label(target_sec))
        self.app._updating_ui = False
        self.render()
        if (self.app.is_playing_main or self.app.is_playing_playlist) and not self.app.is_paused:
            self.app._seek_playback(target_sec)
        else:
            self.app.play_start_offset = target_sec
            if self.app.is_paused:
                self.app._scrubbed_while_paused = True

    def on_drag(self, event):
        if not self.app.selected_file_path or self.app.track_duration <= 0:
            return
        self.app._progress_dragging = True
        w = max(1, self.canvas.winfo_width())
        target_sec = self.time_from_x(event.x, w)

        if self.app._dragging_marker == "start":
            self.app.clip_start_sec = max(0.0, min(self.app.clip_end_sec - 0.05, target_sec))
            is_frac = not float(self.app.clip_start_sec).is_integer()
            self.app.lbl_start_time.config(
                text=f"Start: {format_time(self.app.clip_start_sec, include_fractional=is_frac)}"
            )
            self.app._update_clip_length_label()
            self.render()
        elif self.app._dragging_marker == "end":
            self.app.clip_end_sec = max(self.app.clip_start_sec + 0.05, min(self.app.track_duration, target_sec))
            is_frac = not float(self.app.clip_end_sec).is_integer()
            self.app.lbl_end_time.config(text=f"End: {format_time(self.app.clip_end_sec, include_fractional=is_frac)}")
            self.app._update_clip_length_label()
            self.render()
        else:
            self.app._updating_ui = True
            self.app.scale_progress.set(target_sec)
            self.app.lbl_prog_time.config(text=self.app._prog_label(target_sec))
            self.app._updating_ui = False
            self.render()

    def on_release(self, _event=None):
        if not self.app._progress_dragging:
            return
        self.app._progress_dragging = False
        marker = getattr(self.app, "_dragging_marker", None)
        self.app._dragging_marker = None
        self.canvas.config(cursor="")

        if marker == "start":
            is_frac = not float(self.app.clip_start_sec).is_integer()
            self.app.set_status(
                f"Clip start set to {format_time(self.app.clip_start_sec, include_fractional=is_frac)}."
            )
            self.render()
            return
        elif marker == "end":
            is_frac = not float(self.app.clip_end_sec).is_integer()
            self.app.set_status(f"Clip end set to {format_time(self.app.clip_end_sec, include_fractional=is_frac)}.")
            self.render()
            return

        target_sec = max(0.0, min(max(0.0, self.app.track_duration - 0.05), float(self.app.scale_progress.get())))
        if (self.app.is_playing_main or self.app.is_playing_playlist) and not self.app.is_paused:
            self.app._seek_playback(target_sec)
        else:
            self.app.play_start_offset = target_sec
            if self.app.is_paused:
                self.app._scrubbed_while_paused = True

"""Smoke test verifying Tkinter GUI initialization and component mounting."""

import tkinter as tk
import unittest
from app.config import APP_VERSION
from app.main import UltimateAudioStudio


class TestGUIInitialization(unittest.TestCase):
    def test_gui_instantiation(self):
        root = tk.Tk()
        root.withdraw()
        app = UltimateAudioStudio(root)
        root.update()

        # Title bar version
        self.assertEqual(root.title(), f"Ultimate Audio Studio v{APP_VERSION}")

        # Step 1 widgets
        self.assertIsNotNone(app.entry_url)
        self.assertIsNotNone(app.listbox_lib)
        self.assertIsNotNone(app.entry_search)

        # Step 2 widgets
        self.assertIsNotNone(app.canvas_cover)
        self.assertIsNotNone(app.canvas_waveform)
        self.assertIsNotNone(app.scale_progress)
        self.assertIsNotNone(app.scale_gain)
        self.assertIsNotNone(app.btn_zoom)
        self.assertIsNotNone(app.waveform_view)
        self.assertIsNotNone(app.btn_mute)
        self.assertIsNotNone(app.lbl_vol_pct)
        self.assertIsNotNone(app.btn_edit_start)
        self.assertIsNotNone(app.btn_edit_end)
        self.assertIsNotNone(app.cmb_fade_dur)

        # Mute and volume percentage toggle check
        initial_vol_text = app.lbl_vol_pct.cget("text")
        self.assertTrue(initial_vol_text.endswith("%"))
        app.toggle_mute()
        self.assertTrue(app.is_muted)
        self.assertEqual(app.btn_mute.cget("text"), "🔇")
        self.assertEqual(app.lbl_vol_pct.cget("text"), "0%")
        app.toggle_mute()
        self.assertFalse(app.is_muted)
        self.assertEqual(app.btn_mute.cget("text"), "🔊")
        self.assertEqual(app.lbl_vol_pct.cget("text"), initial_vol_text)

        # Fade duration choice check
        self.assertIn(app.fade_choice_var.get(), ["0.5s (Quick)", "1.5s (Standard)", "3.0s (Smooth)"])
        app.fade_choice_var.set("0.5s (Quick)")
        self.assertEqual(app._get_fade_sec(), 0.5)
        app.fade_choice_var.set("1.5s (Standard)")
        self.assertEqual(app._get_fade_sec(), 1.5)
        app.fade_choice_var.set("3.0s (Smooth)")
        self.assertEqual(app._get_fade_sec(), 3.0)

        # Step 3 widgets
        self.assertIsNotNone(app.cmb_playlists)
        self.assertIsNotNone(app.listbox_pl)
        self.assertIsNotNone(app.cmb_usb)

        # Clean shutdown
        app.on_close()

    def test_search_dialog(self):
        from app.ui.search_dialog import SearchChoiceDialog
        root = tk.Tk()
        root.withdraw()
        sample_results = [
            {"title": "Track 1", "uploader": "Artist 1", "duration_str": "03:45", "url": "https://example.com/1"},
            {"title": "Track 2", "uploader": "Artist 2", "duration_str": "04:12", "url": "https://example.com/2"},
        ]
        chosen = []
        preview_played = []
        dialog = SearchChoiceDialog(
            root,
            query="Track",
            results=sample_results,
            on_select=lambda item: chosen.append(item),
            on_preview_play=lambda: preview_played.append(True)
        )
        root.update()
        self.assertIsNotNone(dialog.win)
        self.assertEqual(len(dialog.tree.get_children()), 2)

        # Verify preview UI widgets exist
        self.assertIsNotNone(dialog.btn_preview_play)
        self.assertIsNotNone(dialog.btn_preview_stop)
        self.assertIsNotNone(dialog.lbl_preview_status)
        self.assertIsNotNone(dialog.prog_preview)
        self.assertIsNotNone(dialog.lbl_preview_time)
        self.assertIsNotNone(dialog.btn_preview_download)
        self.assertIsNotNone(dialog.btn_download)
        self.assertIsNotNone(dialog.btn_more)
        self.assertEqual(dialog.btn_more.cget("text"), "➕ Show More Results")
        self.assertIsNotNone(dialog.btn_cancel)

        root.deiconify()
        dialog.win.update_idletasks()
        dialog.win.update()
        self.assertTrue(dialog.btn_download.winfo_ismapped())
        self.assertTrue(dialog.btn_preview_download.winfo_ismapped())

        # Selection switch updates status label
        dialog.tree.selection_set("1")
        dialog._on_tree_select()
        self.assertIn("Track 2", dialog.lbl_preview_status.cget("text"))

        # Stop preview works cleanly
        dialog._stop_preview()
        self.assertFalse(dialog._is_previewing)

        # Dialog window has no keyboard shortcuts bound
        dialog_binds = dialog.win.bind()
        for shortcut in ("<Escape>", "<Key-Escape>", "<Return>", "<Key-Return>", "<space>", "<Key-space>"):
            self.assertNotIn(shortcut, dialog_binds)

        # Simulate selection/download confirmation
        dialog._do_select()
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["title"], "Track 2")

        root.destroy()

    def test_keyboard_shortcuts_removed_and_return_preserved(self):
        root = tk.Tk()
        root.withdraw()
        app = UltimateAudioStudio(root)
        root.update()

        # Verify root window has no global keyboard shortcuts bound
        root_binds = root.bind()
        for shortcut in (
            "<space>", "<Key-space>",
            "<Left>", "<Key-Left>",
            "<Right>", "<Key-Right>",
            "<bracketleft>", "<Key-bracketleft>",
            "<bracketright>", "<Key-bracketright>",
            "<Control-f>", "<Control-Key-f>",
            "<Control-F>", "<Control-Key-F>"
        ):
            self.assertNotIn(shortcut, root_binds)

        # Verify text entry retains Return key binding for submitting text
        entry_binds = app.entry_url.bind()
        self.assertTrue(
            "<Return>" in entry_binds or "<Key-Return>" in entry_binds,
            f"Expected Return binding in entry_url, got {entry_binds}"
        )

        app.on_close()


if __name__ == "__main__":
    unittest.main()

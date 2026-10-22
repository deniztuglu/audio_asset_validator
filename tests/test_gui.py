"""Real Tk interaction tests. Explicitly skipped when Tk or a display is unavailable."""

import csv
import hashlib
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    import tkinter as tk
except ImportError:
    tk = None

from naming_rules import NamingPreset
from project_profiles import ProjectProfile
from tools.generate_demo import generate
from validator import Rules, scan_files, validate_file


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if tk is None:
            raise unittest.SkipTest("Tkinter is not installed; CLI and model tests still run")
        try:
            root = tk.Tk()
            root.destroy()
        except tk.TclError as error:
            raise unittest.SkipTest(f"GUI display unavailable: {error}")
        from gui import ValidatorApp
        cls.app_class = ValidatorApp

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.assets = self.directory / "audio"
        generate(self.assets)
        self.root = tk.Tk()
        self.callback_errors = []
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.app = self.app_class(self.root)
        self.errors = patch("gui.messagebox.showerror").start()
        self.confirm = patch("gui.messagebox.askyesno", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.root.update()

    def tearDown(self):
        self.app.dirty = False
        if not self.app._closed:
            self.app.close()
        if self.app.worker:
            self.app.worker.join(timeout=3)
            self.assertFalse(self.app.worker.is_alive())
        self.assertEqual(self.callback_errors, [])

    def pump_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.assertTrue(predicate(), "GUI condition did not complete")
        self.root.update()

    def scan(self):
        self.app.folder.set(str(self.assets))
        self.app.scan_button.invoke()
        self.assertTrue(self.app.busy)
        self.pump_until(lambda: not self.app.busy)

    def test_initial_defaults_and_live_preset_controls(self):
        self.assertEqual(self.app.profile_from_controls(), ProjectProfile())
        self.assertFalse(self.app.dirty)
        self.assertIn("PASS", self.app.preview.get())
        self.app.prefixes["SFX"].set(False)
        self.app.prefixes["DX"].set(True)
        self.app.capitalization.set("uppercase")
        self.app.variation_digits.set("3")
        self.app.test_filename.set("DX_HELLO_001.wav")
        self.assertIn("PASS", self.app.preview.get())
        self.assertIsNotNone(re.fullmatch(self.app.profile_from_controls().rules.naming_pattern,
                                         "DX_HELLO_001.wav"))
        self.assertTrue(self.app.dirty)

    def test_audio_controls_create_validated_rules(self):
        self.app.sample_rate.set("96000")
        self.app.depths[16].set(False)
        self.app.channels[2].set(False)
        self.app.minimum_duration.set("0.1")
        rules = self.app.profile_from_controls().rules
        self.assertEqual((rules.sample_rate, rules.bit_depths, rules.channels, rules.min_duration_seconds),
                         (96000, (24,), (1,), 0.1))

    def test_browse_and_scan_folder_with_spaces_and_unicode(self):
        renamed = self.directory / "Audio Exports - Téšt"
        self.assets.rename(renamed)
        self.assets = renamed
        with patch("gui.filedialog.askdirectory", return_value=str(renamed)):
            self.app.choose_folder()
        self.assertEqual(self.app.folder.get(), str(renamed))
        self.app.start_scan()
        self.pump_until(lambda: not self.app.busy)
        self.assertTrue(self.app.complete)
        self.assertEqual(self.app.counts, {"PASS": 2, "WARNING": 1, "ERROR": 7})

    def test_small_window_keeps_scan_buttons_visible_and_rules_scrollable(self):
        self.root.geometry("1000x680")
        self.root.update()
        button = self.app.scan_button
        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                             self.root.winfo_rooty() + self.root.winfo_height())
        canvas = self.app._rule_canvases[0]
        self.assertGreater(canvas.bbox("all")[3], canvas.winfo_height())
        canvas.yview_moveto(1.0)
        self.root.update()
        self.assertGreater(canvas.yview()[0], 0)

    def test_custom_regex_and_nonstandard_allowed_values_round_trip_through_dialogs(self):
        pattern = r"VO_[A-Z]+_(?:take|alt)[0-9]+\.wav"
        profile = ProjectProfile("Custom", Rules(naming_pattern=pattern, bit_depths=(20, 24), channels=(1, 6)), None)
        legacy = self.directory / "legacy.json"
        import json
        legacy.write_text(json.dumps({"naming_pattern": pattern, "bit_depths": [20, 24], "channels": [1, 6]}))
        with patch("gui.filedialog.askopenfilename", return_value=str(legacy)):
            self.app.load_profile()
        self.assertEqual(self.app.naming_mode.get(), "advanced")
        self.assertEqual(self.app.regex.get(), pattern)
        self.app.profile_name.set("Custom")
        saved = self.directory / "saved.json"
        with patch("gui.filedialog.asksaveasfilename", return_value=str(saved)):
            self.app.save_profile()
        self.assertEqual(ProjectProfile.load(saved), profile)
        self.assertFalse(self.app.dirty)
        self.app.regex.set("changed")
        with patch("gui.filedialog.askopenfilename", return_value=str(saved)):
            self.app.load_profile()
        self.assertEqual(self.app.profile_from_controls(), profile)
        self.errors.assert_not_called()

    def test_invalid_controls_never_start_worker(self):
        self.app.folder.set(str(self.assets))
        for update in (lambda: self.app.sample_rate.set("oops"),
                       lambda: self.app.minimum_duration.set("nan"),
                       lambda: [var.set(False) for var in self.app.depths.values()],
                       lambda: [var.set(False) for var in self.app.prefixes.values()]):
            self.app.apply_profile(ProjectProfile())
            update()
            self.app.start_scan()
            self.assertFalse(self.app.busy)
            self.assertIsNone(self.app.worker)
        self.app.naming_mode.set("advanced")
        self.app.regex.set("[")
        self.app.start_scan()
        self.assertIn("needs attention", self.app.preview.get())
        self.assertEqual(self.errors.call_count, 5)

    def test_full_scan_filter_details_csv_and_input_integrity(self):
        before = {p: hashlib.sha256(p.read_bytes()).digest() for p in scan_files(self.assets)}
        self.scan()
        self.assertTrue(self.app.complete)
        self.assertEqual(self.app.counts, {"PASS": 2, "WARNING": 1, "ERROR": 7})
        self.assertEqual(self.app.results, [validate_file(p, self.assets, Rules()) for p in scan_files(self.assets)])
        self.app.result_filter.set("Errors")
        self.app._refresh_results()
        self.assertEqual(len(self.app.tree.get_children()), 7)
        index = next(i for i, asset in enumerate(self.app.results) if asset.file == "SFX_Explosion_03.wav")
        self.app.tree.selection_set(str(index))
        self.root.update()
        details = self.app.details.get("1.0", "end")
        for text in ("48000 Hz", "44100 Hz", "Next step:"):
            self.assertIn(text, details)
        report = self.directory / "report.csv"
        with patch("gui.filedialog.asksaveasfilename", return_value=str(report)):
            self.app.export_csv()
        with report.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 52)  # Export includes passing files despite the current filter.
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).digest() for p in before})
        self.errors.assert_not_called()

    def test_rule_changes_mark_results_stale_and_require_rescan(self):
        self.scan()
        self.assertFalse(self.app.export_button.instate(["disabled"]))
        self.app.sample_rate.set("44100")
        self.assertTrue(self.app.stale)
        self.assertTrue(self.app.export_button.instate(["disabled"]))
        self.assertIn("scan again", self.app.summary.get())
        self.app.start_scan()
        self.pump_until(lambda: not self.app.busy)
        self.assertFalse(self.app.stale)
        self.assertEqual(self.app._scan_profile.rules.sample_rate, 44100)

    def test_ui_remains_responsive_and_cancel_disables_partial_export(self):
        entered, release = threading.Event(), threading.Event()

        def slow_file(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release worker")
            return validate_file(*args)

        with patch("scan_worker.validate_file", side_effect=slow_file):
            self.app.folder.set(str(self.assets))
            self.app.start_scan()
            try:
                self.pump_until(entered.is_set)
                self.assertTrue(self.app.scan_button.instate(["disabled"]))
                self.assertTrue(self.app.regex_entry.instate(["disabled"]))
                heartbeat = []
                self.root.after(0, lambda: heartbeat.append("UI callback ran while file was blocked"))
                self.pump_until(lambda: bool(heartbeat))
                self.app.cancel_button.invoke()
            finally:
                release.set()
            self.pump_until(lambda: not self.app.busy)
        self.assertFalse(self.app.complete)
        self.assertTrue(self.app.export_button.instate(["disabled"]))
        self.assertIn("Cancelled", self.app.summary.get())
        self.assertFalse(self.app.scan_button.instate(["disabled"]))

    def test_scan_failure_restores_editing_and_surfaces_error(self):
        with patch("scan_worker.scan_files", side_effect=PermissionError("denied")):
            self.scan()
        self.assertFalse(self.app.complete)
        self.assertFalse(self.app.scan_button.instate(["disabled"]))
        self.assertTrue(self.app.export_button.instate(["disabled"]))
        self.assertIn("denied", self.errors.call_args.args[1])

    def test_invalid_profile_does_not_replace_current_controls(self):
        before = self.app.profile_from_controls()
        bad = self.directory / "bad.json"
        bad.write_text('{"profile_version": 999}', encoding="utf-8")
        with patch("gui.filedialog.askopenfilename", return_value=str(bad)):
            self.app.load_profile()
        self.assertEqual(self.app.profile_from_controls(), before)
        self.errors.assert_called_once()

    def test_profile_and_csv_outputs_inside_input_are_rejected(self):
        self.scan()
        with patch("gui.filedialog.asksaveasfilename", return_value=str(self.assets / "report.csv")):
            self.app.export_csv()
        with patch("gui.filedialog.asksaveasfilename", return_value=str(self.assets / "profile.json")):
            self.app.save_profile()
        self.assertEqual(self.errors.call_count, 2)
        self.assertFalse((self.assets / "report.csv").exists())
        self.assertFalse((self.assets / "profile.json").exists())


if __name__ == "__main__":
    unittest.main()

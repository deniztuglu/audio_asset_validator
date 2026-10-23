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
from batch_rename import RenameOptions
from project_profiles import ProjectProfile
from tools.generate_demo import generate
from validator import Rules, scan_files, validate_file


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if tk is None:
            raise unittest.SkipTest("Tkinter is not installed; CLI and model tests still run")
        try:
            # Keep one Tcl/Tk interpreter for the entire suite. Repeated Tk()
            # destruction/recreation can leave native Cocoa state on older macOS Tk.
            cls.tk_root = tk.Tk()
        except tk.TclError as error:
            raise unittest.SkipTest(f"GUI display unavailable: {error}")
        cls.tk_root.withdraw()
        cls.addClassCleanup(cls.tk_root.destroy)
        from gui import ValidatorApp
        cls.app_class = ValidatorApp

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.assets = self.directory / "audio"
        generate(self.assets)
        self.root = tk.Toplevel(self.tk_root)
        self.callback_errors = []
        self.tk_root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.app = self.app_class(self.root)
        self.errors = patch("gui.messagebox.showerror").start()
        self.confirm = patch("gui.messagebox.askyesno", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.root.update()

    def tearDown(self):
        self.app.dirty = False
        dialog = self.app.rename_dialog
        if dialog and not dialog._closed:
            if dialog.busy:
                dialog.cancel()
                self.pump_until(lambda: not dialog.busy)
            if dialog.worker:
                dialog.worker.join(timeout=3)
                self.assertFalse(dialog.worker.is_alive())
            dialog.request_close()
        if not self.app._closed:
            self.app.close()
        if self.app.worker:
            self.app.worker.join(timeout=3)
            self.assertFalse(self.app.worker.is_alive())
        self.tk_root.update()
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

    def renamer(self, filenames):
        self.scan()
        indices = [str(i) for i, asset in enumerate(self.app.results) if asset.file in filenames]
        self.app.tree.selection_set(indices)
        self.root.update()
        with patch("rename_dialog.preset_path", return_value=self.directory / "rename-presets.json"):
            self.app.rename_button.invoke()
        return self.app.rename_dialog

    def test_rename_button_opens_popup_for_only_the_selected_assets(self):
        self.assertTrue(self.app.rename_button.instate(["disabled"]))
        names = {"SFX_UI_Click_01.wav", "SFX_Explosion_03.wav"}
        dialog = self.renamer(names)
        self.assertEqual({source.name for source in dialog.sources}, names)
        self.assertEqual(dialog.read_options(), RenameOptions())
        self.assertIsNotNone(dialog.window.grab_current())

    def test_rename_preview_copy_and_revalidation_preserve_originals(self):
        dialog = self.renamer({"footstep-final.wav", "nested/SFX_Footstep_Concrete_01.wav"})
        before = (self.assets / "footstep-final.wav").read_bytes()
        output = self.directory / "renamed"
        dialog.output.set(str(output))
        dialog.variables["find"].set("footstep-final")
        dialog.variables["replacement"].set("SFX_Footstep_Concrete_01")
        dialog.preview_button.invoke()
        self.assertTrue(dialog.plan.can_apply)
        self.assertFalse(output.exists())
        dialog.apply_button.invoke()
        self.assertTrue(dialog.busy)
        self.pump_until(lambda: not dialog.busy)
        self.assertEqual(dialog.outcome.error, "")
        self.assertEqual(len(dialog.outcome.copies), 1)
        self.assertEqual(dialog.outcome.copies[0].validation.status, "PASS")
        self.assertEqual((output / "SFX_Footstep_Concrete_01.wav").read_bytes(), before)
        self.assertEqual((self.assets / "footstep-final.wav").read_bytes(), before)
        self.assertTrue(dialog.apply_button.instate(["disabled"]))
        self.errors.assert_not_called()

    def test_rename_collisions_and_invalid_output_disable_copy(self):
        dialog = self.renamer({"SFX_UI_Click_01.wav", "SFX_Explosion_03.wav"})
        dialog.output.set(str(self.directory / "renamed"))
        dialog.variables["clear_existing"].set(True)
        dialog.variables["replacement"].set("SFX_UI_New_01")
        dialog.preview()
        self.assertTrue(all(row.errors for row in dialog.plan.rows))
        self.assertTrue(dialog.apply_button.instate(["disabled"]))
        dialog.output.set(str(self.assets / "unsafe"))
        dialog.preview()
        self.assertIsNone(dialog.plan)
        self.errors.assert_called_once()

    def test_rename_edit_invalidates_preview_and_presets_restore_every_option(self):
        dialog = self.renamer({"SFX_UI_Click_01.wav"})
        dialog.output.set(str(self.directory / "renamed"))
        dialog.variables["prefix"].set("New_")
        dialog.preview()
        self.assertIsNotNone(dialog.plan)
        dialog.variables["suffix"].set("_Extra")
        self.assertIsNone(dialog.plan)
        self.assertEqual(dialog.tree.get_children(), ())
        options = dialog.read_options()
        dialog.load_preset(3)  # Empty slot selects it without discarding current edits.
        dialog.save_preset()
        dialog.load_preset(0)
        self.assertEqual(dialog.read_options(), RenameOptions())
        dialog.load_preset(3)
        self.assertEqual(dialog.read_options(), options)
        self.errors.assert_not_called()

    def test_rename_regex_trim_range_and_add_controls_make_the_expected_name(self):
        dialog = self.renamer({"SFX_UI_Click_01.wav"})
        dialog.output.set(str(self.directory / "renamed"))
        for key, value in {"find": r"SFX_(.*)_01", "replacement": r"\1", "use_regex": True,
                           "trim_range": True, "range_start": "1", "range_end": "3",
                           "prefix": "DX_", "suffix": "_001"}.items():
            dialog.variables[key].set(value)
        dialog.preview()
        self.assertEqual(dialog.plan.rows[0].destination.name, "DX_Click_001.wav")
        self.assertTrue(dialog.plan.rows[0].warnings)

    def test_renamed_copy_still_reports_audio_metadata_errors(self):
        dialog = self.renamer({"SFX_Explosion_03.wav"})
        dialog.output.set(str(self.directory / "renamed"))
        dialog.variables["find"].set("03")
        dialog.variables["replacement"].set("04")
        dialog.preview()
        dialog.apply()
        self.pump_until(lambda: not dialog.busy)
        self.assertEqual(dialog.outcome.copies[0].validation.status, "ERROR")
        self.assertIn("44100 Hz", dialog.notes.get("1.0", "end"))
        self.assertIn("need validation review", dialog.status.get())

    def test_parent_close_cancels_copy_and_waits_for_the_worker(self):
        dialog = self.renamer({"SFX_UI_Click_01.wav"})
        dialog.output.set(str(self.directory / "renamed"))
        dialog.variables["find"].set("Click")
        dialog.variables["replacement"].set("Tap")
        dialog.preview()
        entered, release = threading.Event(), threading.Event()

        def slow_validation(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release output validation")
            return validate_file(*args)

        with patch("batch_rename.validate_file", side_effect=slow_validation):
            dialog.apply()
            try:
                self.pump_until(entered.is_set)
                heartbeat = []
                self.root.after(0, lambda: heartbeat.append(True))
                self.pump_until(lambda: bool(heartbeat))
                self.app.close()
                self.assertFalse(self.app._closed)
                self.assertTrue(dialog._closing)
            finally:
                release.set()
            self.pump_until(lambda: self.app._closed)
        dialog.worker.join(timeout=3)
        self.assertFalse(dialog.worker.is_alive())
        self.assertTrue(dialog.outcome.cancelled)
        self.assertTrue((self.assets / "SFX_UI_Click_01.wav").exists())

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

    def test_add_remove_custom_prefix_updates_preview_and_selection(self):
        old_variables = list(self.app.prefixes.values())
        trace_count = len(self.app._variable_traces)
        self.app.prefix_text.set(" amb ")
        self.app.prefix_add_button.invoke()
        self.assertTrue(all(not variable.trace_info() for variable in old_variables))
        self.assertEqual(len(self.app._variable_traces), trace_count + 1)
        self.assertTrue(self.app.prefixes["AMB"].get())
        self.app.test_filename.set("AMB_Rain_01.wav")
        self.assertIn("PASS", self.app.preview.get())
        self.assertTrue(self.app.dirty)
        self.app.prefix_remove_button.invoke()
        self.assertEqual(len(self.app._variable_traces), trace_count)
        self.assertNotIn("AMB", self.app.prefixes)
        self.assertIn("NO MATCH", self.app.preview.get())
        self.errors.assert_not_called()

    def test_custom_prefix_catalog_round_trips_through_profile_dialogs(self):
        for prefix in ("AMB", "VO_EN"):
            self.app.prefix_text.set(prefix)
            self.app.add_prefix()
        self.app.prefixes["VO_EN"].set(False)
        self.app.prefix_text.set("DX")
        self.app.remove_prefix()
        before = self.app.profile_from_controls()
        path = self.directory / "custom-prefixes.json"
        with patch("gui.filedialog.asksaveasfilename", return_value=str(path)):
            self.app.save_profile()
        self.app.apply_profile(ProjectProfile())
        with patch("gui.filedialog.askopenfilename", return_value=str(path)):
            self.app.load_profile()
        self.assertEqual(self.app.profile_from_controls(), before)
        self.assertIn("VO_EN", self.app.prefixes)
        self.assertFalse(self.app.prefixes["VO_EN"].get())
        self.assertNotIn("DX", self.app.prefixes)
        self.errors.assert_not_called()

    def test_invalid_duplicate_and_missing_prefix_do_not_modify_choices(self):
        before = self.app.profile_from_controls()
        for value in ("", "../AMB", "SFX|DX", "sfx"):
            self.app.prefix_text.set(value)
            self.app.add_prefix()
        self.app.prefix_text.set("MISSING")
        self.app.remove_prefix()
        self.assertEqual(self.app.profile_from_controls(), before)
        self.assertFalse(self.app.dirty)
        self.assertEqual(self.errors.call_count, 5)

    def test_removing_every_prefix_blocks_scan_until_a_prefix_is_added(self):
        for prefix in tuple(self.app.prefixes):
            self.app.prefix_text.set(prefix)
            self.app.remove_prefix()
        self.assertEqual(self.app.prefixes, {})
        self.app.folder.set(str(self.assets))
        self.app.start_scan()
        self.assertIsNone(self.app.worker)
        self.errors.assert_called_once()
        self.app.prefix_text.set("AMB")
        self.app.add_prefix()
        self.assertEqual(self.app.profile_from_controls().naming.prefixes, ("AMB",))

    def test_custom_prefix_validation_uses_engine_and_marks_existing_results_stale(self):
        self.scan()
        self.app.prefix_text.set("CUSTOM")
        self.app.add_prefix()
        self.assertTrue(self.app.stale)
        self.assertTrue(self.app.export_button.instate(["disabled"]))
        custom = self.assets / "CUSTOM_Click_01.wav"
        custom.write_bytes((self.assets / "SFX_UI_Click_01.wav").read_bytes())
        self.app.start_scan()
        self.pump_until(lambda: not self.app.busy)
        result = next(asset for asset in self.app.results if asset.file == custom.name)
        self.assertEqual(result.status, "PASS")
        self.assertFalse(self.app.stale)

    def test_prefix_editor_is_disabled_during_scan_and_in_advanced_mode(self):
        self.app.naming_mode.set("advanced")
        self.assertTrue(self.app.prefix_add_button.instate(["disabled"]))
        self.app.prefix_text.set("AMB")
        self.app.add_prefix()
        self.assertNotIn("AMB", self.app.prefixes)
        self.app.naming_mode.set("preset")
        self.app.folder.set(str(self.assets))
        self.app.start_scan()
        self.assertTrue(self.app.prefix_entry.instate(["disabled"]))
        self.assertTrue(self.app.prefix_remove_button.instate(["disabled"]))
        self.app.add_prefix()
        self.assertNotIn("AMB", self.app.prefixes)
        self.pump_until(lambda: not self.app.busy)

    def test_window_close_keeps_interpreter_alive_and_removes_callbacks(self):
        interpreter = self.app.root.tk
        poll_id = self.app._poll_id
        traced_variables = [variable for variable, _ in self.app._variable_traces]
        self.app.close()
        self.app.close()  # Closing twice is safe and must not access destroyed widgets.
        self.assertTrue(self.tk_root.winfo_exists())
        self.assertNotIn(poll_id, self.tk_root.tk.call("after", "info"))
        self.assertTrue(all(not variable.trace_info() for variable in traced_variables))
        another_window = tk.Toplevel(self.tk_root)
        try:
            self.assertIs(another_window.tk, interpreter)
        finally:
            another_window.destroy()

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

    def test_metadata_tabs_and_multiple_selection_survive_a_filter_refresh(self):
        self.scan()
        indices = [str(i) for i, asset in enumerate(self.app.results) if asset.status == "PASS"]
        self.app.tree.selection_set(indices)
        self.root.update()
        self.assertEqual(self.app.selection_note.get(), "2 selected")
        self.assertIn("48000 Hz", self.app.metadata.get("1.0", "end"))
        self.assertIn("24-bit", self.app.metadata.get("1.0", "end"))
        self.assertEqual([self.app.context_tabs.tab(tab, "text") for tab in self.app.context_tabs.tabs()],
                         ["Overview", "Audio metadata", "Validation checks"])
        self.app.result_filter.set("Passed")
        self.app._refresh_results()
        self.assertEqual(set(self.app.tree.selection()), set(indices))
        self.assertEqual(self.app.tree.item(indices[0], "values")[2], "PCM WAV")

    def test_native_shift_toggle_and_select_all_bindings(self):
        self.scan()
        items = self.app.tree.get_children()
        tree = self.app.tree

        def click(item, modifier=""):
            x, y, width, height = tree.bbox(item)
            tree.event_generate(f"<{modifier}Button-1>", x=x + 8, y=y + height // 2)
            tree.event_generate("<ButtonRelease-1>", x=x + 8, y=y + height // 2)
            self.root.update()

        click(items[0])
        click(items[2], "Shift-")
        self.assertEqual(set(tree.selection()), set(items[:3]))
        modifier = "Command-" if tree.tk.call("tk", "windowingsystem") == "aqua" else "Control-"
        click(items[1], modifier)
        self.assertEqual(set(tree.selection()), {items[0], items[2]})
        self.app._select_all()
        self.assertEqual(set(tree.selection()), set(items))

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

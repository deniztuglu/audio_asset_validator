from dataclasses import asdict
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from batch_rename import RenameOptions, RenamePresetStore, RenameWorker, build_plan, execute_plan
from tools.generate_demo import generate
from validator import Rules


class RenameOptionTests(unittest.TestCase):
    def test_literal_replace_and_regex_groups(self):
        self.assertEqual(RenameOptions(find="old", replacement="new").rename_stem("old_old"), "new_new")
        self.assertEqual(RenameOptions(find=r"(UI)_(Click)", replacement=r"\2_\1", use_regex=True)
                         .rename_stem("SFX_UI_Click_01"), "SFX_Click_UI_01")

    def test_clear_existing_uses_replace_as_new_base_then_applies_other_edits(self):
        options = RenameOptions(clear_existing=True, replacement="Click", prefix="SFX_UI_", suffix="_01")
        self.assertEqual(options.rename_stem("anything"), "SFX_UI_Click_01")

    def test_trim_zero_and_both_ends_and_overtrim(self):
        self.assertEqual(RenameOptions().rename_stem("abcdef"), "abcdef")
        self.assertEqual(RenameOptions(trim_begin=1, trim_end=2).rename_stem("abcdef"), "bcd")
        self.assertEqual(RenameOptions(trim_begin=30, trim_end=30, prefix="New").rename_stem("abc"), "New")

    def test_inclusive_one_based_ranges_from_either_end(self):
        self.assertEqual(RenameOptions(trim_range=True, range_start=2, range_end=3).rename_stem("abcdef"), "adef")
        self.assertEqual(RenameOptions(trim_range=True, range_start=2, range_end=3,
                                       range_from_end=True).rename_stem("abcdef"), "abcf")
        self.assertEqual(RenameOptions(trim_range=True, range_start=20, range_end=30).rename_stem("abc"), "abc")

    def test_order_is_replace_trim_prefix_insert_suffix(self):
        options = RenameOptions(find="old", replacement="new", trim_begin=1, trim_end=1,
                                prefix="PRE", insert="X", insert_at=3, suffix="END")
        self.assertEqual(options.rename_stem("_old_"), "PREXnewEND")
        self.assertEqual(RenameOptions(insert="X", insert_at=200).rename_stem("abc"), "abcX")

    def test_disabled_sections_do_not_change_names(self):
        options = RenameOptions(replace_enabled=False, find="(", use_regex=True, trim_enabled=False,
                                trim_begin=3, add_enabled=False, prefix="PREFIX")
        self.assertEqual(options.rename_stem("original"), "original")

    def test_bad_options_and_regex_references_are_errors(self):
        for values in ({"find": "[", "use_regex": True}, {"replacement": "foo"},
                       {"trim_begin": -1}, {"range_start": 0}, {"range_start": 3, "range_end": 1},
                       {"insert_at": True}, {"unknown": 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RenameOptions.from_dict(values)
        with self.assertRaises(ValueError):
            RenameOptions(find="(a)", replacement=r"\9", use_regex=True).rename_stem("a")


class RenameFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / "audio"
        generate(self.audio)
        self.output = self.root / "renamed"
        self.source = self.audio / "SFX_UI_Click_01.wav"
        self.options = RenameOptions(find="Click", replacement="Tap")

    def plan(self, sources=None, options=None, output=None):
        return build_plan(self.audio, sources or [self.source], output or self.output,
                          options or self.options, Rules())

    def test_preview_is_read_only_and_keeps_extension_and_subfolders(self):
        source = self.audio / "nested" / "SFX_Footstep_Concrete_01.wav"
        plan = self.plan([source], RenameOptions(find="Concrete", replacement="Wood"))
        self.assertTrue(plan.can_apply)
        self.assertEqual(plan.rows[0].destination, self.output / "nested" / "SFX_Footstep_Wood_01.wav")
        self.assertFalse(self.output.exists())
        uppercase = self.audio / "SFX_UI_Click_02.WAV"
        uppercase.write_bytes(self.source.read_bytes())
        self.assertEqual(self.plan([uppercase]).rows[0].destination.suffix, ".WAV")

    def test_copy_is_byte_identical_revalidated_and_originals_are_untouched(self):
        original = self.source.read_bytes()
        outcome = execute_plan(self.plan())
        self.assertEqual(outcome.error, "")
        self.assertFalse(outcome.cancelled)
        self.assertEqual(len(outcome.copies), 1)
        copied = outcome.copies[0]
        self.assertEqual(copied.destination.read_bytes(), original)
        self.assertEqual(copied.sha256, hashlib.sha256(original).hexdigest())
        self.assertEqual(copied.validation.status, "PASS")
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue(self.source.exists())

    def test_bad_names_and_traversal_block_the_entire_batch(self):
        for name in ("", "../escape", "A/B", "A\\B", "CON", "bad:name", "bad\nname", " trailing ", "x" * 300):
            options = RenameOptions(clear_existing=True, replacement=name)
            with self.subTest(name=name):
                plan = self.plan(options=options)
                self.assertFalse(plan.can_apply)
                self.assertTrue(execute_plan(plan).error)
                self.assertFalse(self.output.exists())

    def test_existing_destination_and_case_insensitive_collision_are_never_overwritten(self):
        self.output.mkdir()
        existing = self.output / "sfx_ui_tap_01.WAV"
        existing.write_bytes(b"keep me")
        plan = self.plan()
        self.assertFalse(plan.can_apply)
        self.assertEqual(execute_plan(plan).copies, ())
        self.assertEqual(existing.read_bytes(), b"keep me")

    def test_duplicate_names_in_one_batch_are_blocked_including_case_variants(self):
        other = self.audio / "SFX_UI_Click_02.wav"
        other.write_bytes(self.source.read_bytes())
        plan = self.plan([self.source, other], RenameOptions(clear_existing=True, replacement="SFX_UI_Tap_01"))
        self.assertFalse(plan.can_apply)
        self.assertTrue(all(row.errors for row in plan.rows))

    def test_input_output_overlap_is_forbidden_in_both_directions(self):
        for path in (self.audio, self.audio / "exports", self.root):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.plan(output=path)

    def test_symlinked_sources_and_output_subfolders_are_blocked(self):
        link = self.audio / "SFX_UI_Link_01.wav"
        try:
            link.symlink_to(self.source)
            self.output.mkdir()
            (self.output / "nested").symlink_to(self.audio / "nested", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertFalse(self.plan([link], RenameOptions(prefix="New_")).can_apply)
        nested = self.audio / "nested" / "SFX_Footstep_Concrete_01.wav"
        self.assertFalse(self.plan([nested], RenameOptions(prefix="New_")).can_apply)

    def test_changed_source_or_new_destination_after_preview_blocks_before_writing(self):
        plan = self.plan()
        self.source.write_bytes(b"changed")
        self.assertIn("changed", execute_plan(plan).error)
        self.assertFalse(self.output.exists())
        plan = self.plan()
        self.output.mkdir()
        plan.rows[0].destination.write_bytes(b"another application's file")
        outcome = execute_plan(plan)
        self.assertTrue(outcome.error)
        self.assertEqual(plan.rows[0].destination.read_bytes(), b"another application's file")

    def test_noops_are_skipped_and_nonconforming_names_get_a_warning(self):
        unchanged = self.plan(options=RenameOptions())
        self.assertFalse(unchanged.can_apply)
        self.assertEqual(unchanged.rows[0].status, "Unchanged (skip)")
        plan = self.plan(options=RenameOptions(prefix="My_"))
        self.assertTrue(plan.can_apply)
        self.assertTrue(plan.rows[0].warnings)
        outcome = execute_plan(plan)
        self.assertEqual(outcome.copies[0].validation.status, "ERROR")

    def test_pre_cancel_does_not_create_output_and_mid_batch_keeps_completed_copies(self):
        cancel = threading.Event()
        cancel.set()
        self.assertTrue(execute_plan(self.plan(), cancel).cancelled)
        self.assertFalse(self.output.exists())
        other = self.audio / "SFX_UI_Click_02.wav"
        other.write_bytes(self.source.read_bytes())
        cancel.clear()
        outcome = execute_plan(self.plan([self.source, other]), cancel, lambda _: cancel.set())
        self.assertTrue(outcome.cancelled)
        self.assertEqual(len(outcome.copies), 1)
        self.assertTrue(outcome.copies[0].destination.exists())
        self.assertFalse((self.output / "SFX_UI_Tap_02.wav").exists())

    def test_failed_verification_removes_only_the_new_copy(self):
        with patch("batch_rename.validate_file", side_effect=OSError("simulated read error")):
            outcome = execute_plan(self.plan())
        self.assertTrue(outcome.error)
        self.assertFalse((self.output / "SFX_UI_Tap_01.wav").exists())
        self.assertTrue(self.source.exists())

    def test_background_worker_emits_copied_and_finished_results(self):
        worker = RenameWorker(self.plan())
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.events.get_nowait().kind, "copied")
        self.assertEqual(worker.events.get_nowait().outcome.copies[0].validation.status, "PASS")


class RenamePresetTests(unittest.TestCase):
    def test_slots_persist_independently_and_round_trip_every_option(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RenamePresetStore(Path(directory) / "settings" / "rename.json")
            options = RenameOptions(find="a", replacement="b", trim_range=True, range_start=2, range_end=3,
                                    range_from_end=True, insert="Extra", insert_at=2)
            store.save(1, options)
            store.save(5, RenameOptions(prefix="SFX_"))
            self.assertEqual(store.load(), {"1": options, "5": RenameOptions(prefix="SFX_")})
            self.assertEqual(asdict(store.load()["1"]), asdict(options))

    def test_unknown_or_broken_settings_are_preserved_instead_of_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rename.json"
            for original in ('{"version": 99, "slots": {}}', '{broken'):
                path.write_text(original, encoding="utf-8")
                with self.assertRaises(ValueError):
                    RenamePresetStore(path).save(1, RenameOptions())
                self.assertEqual(path.read_text(encoding="utf-8"), original)

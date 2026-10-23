from pathlib import Path
import tempfile
import unittest

from result_views import check_details, display_value, issue_summary, metadata_details, metadata_values, selection_overview
from tools.generate_demo import generate
from validator import Rules, validate_file


class ResultViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "audio"
        generate(self.root)

    def asset(self, filename):
        return validate_file(self.root / filename, self.root, Rules())

    def test_all_checked_metadata_is_visible_with_actual_and_expected_values(self):
        asset = self.asset("SFX_Explosion_03.wav")
        self.assertEqual(metadata_values(asset), ("PCM WAV", "44.1 kHz", "16-bit", "Mono (1)", "0.100 s"))
        text = metadata_details([asset])
        for value in ("44.1 kHz", "48 kHz", "16-bit", "Channels", "Duration", "PCM WAV"):
            self.assertIn(value, text)
        # Exact engine and report values remain available in the checks inspector.
        self.assertIn("44100 Hz", check_details([asset]))

    def test_issue_summary_explains_failures_without_repeating_the_naming_regex(self):
        self.assertEqual(issue_summary(self.asset("SFX_Explosion_03.wav")),
                         "Sample rate: 44.1 kHz; expected 48 kHz")
        self.assertEqual(issue_summary(self.asset("SFX_UI_Click_01.wav")), "All checks passed")
        self.assertIn("Filename does not match", issue_summary(self.asset("footstep-final.wav")))
        self.assertIn("Unsupported or unreadable", issue_summary(self.asset("SFX_UI_Broken_01.wav")))
        self.assertIn("expected at least 0.050 s", issue_summary(self.asset("SFX_UI_Tiny_01.wav")))
        self.assertIn("Empty (0 frames)", issue_summary(self.asset("SFX_UI_Empty_01.wav")))

    def test_display_units_preserve_unusual_values_and_very_short_durations(self):
        self.assertEqual(display_value("sample_rate", "44101 Hz"), "44.101 kHz")
        self.assertEqual(display_value("channels", "1, 2, 6"), "Mono (1) / Stereo (2) / 6 channels")
        self.assertEqual(display_value("duration", "0.000001 seconds"), "1e-06 s")
        self.assertEqual(display_value("duration", "nonempty audio"), "nonempty audio")

    def test_unreadable_files_never_show_guessed_metadata(self):
        asset = self.asset("SFX_UI_Broken_01.wav")
        self.assertEqual(metadata_values(asset)[1:], ("Not inspected",) * 4)
        self.assertIn("were not run", check_details([asset]))

    def test_multiple_selection_overview_and_details_cover_every_selected_file(self):
        assets = [self.asset("SFX_UI_Click_01.wav"), self.asset("SFX_UI_Tiny_01.wav")]
        text = selection_overview(assets, "My project")
        self.assertIn("2 selected", text)
        self.assertIn("1 passed | 1 with warnings | 0 with errors", text)
        for asset in assets:
            self.assertIn(asset.file, check_details(assets))

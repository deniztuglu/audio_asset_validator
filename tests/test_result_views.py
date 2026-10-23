from pathlib import Path
import tempfile
import unittest

from result_views import check_details, metadata_details, metadata_values, selection_overview
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
        self.assertEqual(metadata_values(asset), ("PCM WAV", "44100 Hz", "16-bit", "1", "0.100000 seconds"))
        text = metadata_details([asset])
        for value in ("44100 Hz", "48000 Hz", "16-bit", "Channels", "Duration", "PCM WAV"):
            self.assertIn(value, text)

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

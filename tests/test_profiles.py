"""Configuration and naming tests that do not require a display or Tkinter."""

import json
from pathlib import Path
import re
import tempfile
import unittest

from naming_rules import NamingPreset
from project_profiles import ProjectProfile
from validator import DEFAULT_NAMING_PATTERN, Rules


class ProfileTests(unittest.TestCase):
    def test_shared_rule_validation_does_not_mutate_callers(self):
        values = {"channels": [1], "bit_depths": [24]}
        self.assertEqual(Rules.from_dict(values).channels, (1,))
        self.assertEqual(values, {"channels": [1], "bit_depths": [24]})

    def test_default_preset_exactly_preserves_v1_pattern(self):
        self.assertEqual(NamingPreset().pattern(), DEFAULT_NAMING_PATTERN)

    def test_all_prefix_casing_digit_combinations(self):
        for prefix in ("SFX", "DX", "FLY"):
            for case in ("uppercase", "lowercase", "flexible"):
                for digits in range(1, 5):
                    with self.subTest(prefix=prefix, case=case, digits=digits):
                        preset = NamingPreset((prefix,), case, digits)
                        self.assertIsNotNone(re.fullmatch(preset.pattern(), preset.example()))
                        self.assertIsNone(re.fullmatch(preset.pattern(), preset.example() + ".bak"))
                        wrong_digits = preset.example().replace(
                            f"_{1:0{digits}d}.", f"_{1:0{digits + 1}d}."
                        )
                        self.assertIsNone(re.fullmatch(preset.pattern(), wrong_digits))

    def test_casing_applies_to_words_prefix_is_explicit_and_extension_is_flexible(self):
        upper = NamingPreset(("DX",), "uppercase", 2).pattern()
        lower = NamingPreset(("DX",), "lowercase", 2).pattern()
        self.assertIsNotNone(re.fullmatch(upper, "DX_HELLO_01.WAV"))
        self.assertIsNone(re.fullmatch(upper, "DX_Hello_01.wav"))
        self.assertIsNotNone(re.fullmatch(lower, "DX_hello_01.wav"))
        self.assertIsNone(re.fullmatch(lower, "dx_hello_01.wav"))

    def test_multiple_selected_prefixes_are_allowed(self):
        pattern = NamingPreset(("SFX", "DX")).pattern()
        self.assertIsNotNone(re.fullmatch(pattern, "DX_Hello_01.wav"))
        self.assertIsNotNone(re.fullmatch(pattern, "SFX_Click_01.wav"))
        self.assertIsNone(re.fullmatch(pattern, "FLY_Cloth_01.wav"))

    def test_invalid_presets_are_rejected(self):
        base = NamingPreset().to_dict()
        for update in ({"prefixes": []}, {"prefixes": ["BAD"]}, {"prefixes": ["SFX", "SFX"]},
                       {"capitalization": "title"}, {"variation_digits": True},
                       {"variation_digits": 0}, {"variation_digits": 5}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                NamingPreset.from_dict({**base, **update})

    def test_v1_default_and_custom_regex_profiles_load_without_rewriting(self):
        original = Path("config.example.json").read_bytes()
        default = ProjectProfile.load(Path("config.example.json"))
        self.assertEqual(default.rules, Rules())
        self.assertEqual(default.naming, NamingPreset())
        custom = ProjectProfile.from_dict({"naming_pattern": r"VO_.*\.wav"})
        self.assertIsNone(custom.naming)
        self.assertEqual(custom.rules.naming_pattern, r"VO_.*\.wav")
        self.assertEqual(Path("config.example.json").read_bytes(), original)

    def test_versioned_round_trip_retains_preset_or_advanced_mode(self):
        for preset in (NamingPreset(("DX", "FLY"), "lowercase", 4), None):
            pattern = preset.pattern() if preset else r"my_project_[0-9]+\.wav"
            profile = ProjectProfile("Dialogue", Rules(naming_pattern=pattern, channels=(1,)), preset)
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "profile.json"
                profile.save(path)
                self.assertEqual(ProjectProfile.load(path), profile)
                self.assertEqual(Rules.load(path), profile.rules)
                with self.assertRaises(FileExistsError):
                    profile.save(path)
                self.assertEqual(ProjectProfile.load(path), profile)

    def test_bad_versions_unknown_fields_and_inconsistent_presets_fail(self):
        base = ProjectProfile().to_dict()
        for update in ({"profile_version": 2}, {"profile_version": True}, {"name": " "},
                       {"unexpected": 1}, {"rules": {"naming_pattern": "custom"}},
                       {"rules": {"channels": []}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                ProjectProfile.from_dict({**base, **update})

    def test_huge_duration_setting_is_a_configuration_error(self):
        with self.assertRaises(ValueError):
            Rules.from_dict({"min_duration_seconds": 10 ** 1000})


if __name__ == "__main__":
    unittest.main()

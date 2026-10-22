"""Behavioral tests with generated audio; no external fixtures or dependencies."""

import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from validator import Rules, main, scan_files, validate_file


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()

    def wav(self, name="SFX_UI_Click_01.wav", rate=48000, width=2,
            channels=1, frames=4800):
        path = self.assets / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as audio:
            audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0" * frames * width * channels)
        return path

    def check(self, path, name, rules=None):
        asset = validate_file(path, self.assets, rules or Rules())
        return next(check for check in asset.checks if check.check == name)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main([str(arg) for arg in args])
        return status, out.getvalue(), err.getvalue()

    def config(self, value):
        path = self.root / "rules.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_default_valid_file_executes_all_six_checks(self):
        result = validate_file(self.wav(), self.assets, Rules())
        self.assertEqual(result.status, "PASS")
        self.assertEqual([check.check for check in result.checks],
                         ["naming", "format", "sample_rate", "bit_depth", "channels", "duration"])
        self.assertTrue(all(check.result == "PASS" for check in result.checks))

    def test_24_bit_stereo_and_uppercase_extension_pass(self):
        path = self.wav("SFX_Footstep_Concrete_01.WAV", width=3, channels=2)
        self.assertEqual(validate_file(path, self.assets, Rules()).status, "PASS")

    def test_scan_finds_nested_files_and_unsupported_extensions(self):
        first = self.wav()
        nested = self.wav("nested/SFX_UI_Click_02.wav")
        other = self.assets / "example.mp3"
        other.write_bytes(b"not audio")
        self.assertEqual(set(scan_files(self.assets)), {first, nested, other})
        self.assertEqual(scan_files(self.assets), scan_files(self.assets))
        self.assertEqual(self.check(other, "format").result, "ERROR")

    def test_naming_uses_full_basename_not_parent_directories(self):
        good = self.wav("arbitrary folder/SFX_Footstep_Concrete_01.wav")
        bad = self.wav("prefix_SFX_UI_Click_01.wav")
        self.assertEqual(self.check(good, "naming").result, "PASS")
        self.assertEqual(self.check(bad, "naming").result, "ERROR")

    def test_sample_rate_error_includes_expected_and_actual(self):
        check = self.check(self.wav(rate=44100), "sample_rate")
        self.assertEqual((check.result, check.expected, check.actual),
                         ("ERROR", "48000 Hz", "44100 Hz"))

    def test_disallowed_depths_and_channel_counts(self):
        for width in (1, 4):
            with self.subTest(width=width):
                self.assertEqual(self.check(self.wav(width=width), "bit_depth").result, "ERROR")
        self.assertEqual(self.check(self.wav(channels=6), "channels").result, "ERROR")

    def test_empty_audio_is_error_even_with_zero_minimum(self):
        path = self.wav(frames=0)
        check = self.check(path, "duration", Rules(min_duration_seconds=0))
        self.assertEqual(check.result, "ERROR")

    def test_duration_threshold_warning_and_boundary(self):
        for frames, expected in ((1, "WARNING"), (2399, "WARNING"), (2400, "PASS")):
            with self.subTest(frames=frames):
                check = self.check(self.wav(frames=frames), "duration")
                self.assertEqual(check.result, expected)

    def test_corrupt_files_do_not_crash_or_claim_metadata_passed(self):
        path = self.assets / "SFX_UI_Broken_01.wav"
        for data in (b"", b"RIFF", b"not a WAV", b"RIFF\xff\xff\xff\xffWAVE"):
            with self.subTest(data=data):
                path.write_bytes(data)
                asset = validate_file(path, self.assets, Rules())
                self.assertEqual(asset.status, "ERROR")
                self.assertEqual([check.check for check in asset.checks], ["naming", "format"])

    def test_float_encoded_wav_is_unsupported(self):
        path = self.wav(width=4)
        data = bytearray(path.read_bytes())
        struct.pack_into("<H", data, 20, 3)  # IEEE float format tag, instead of integer PCM.
        path.write_bytes(data)
        self.assertEqual(self.check(path, "format").result, "ERROR")

    def test_truncated_payload_is_error(self):
        path = self.wav()
        path.write_bytes(path.read_bytes()[:-10])
        self.assertEqual(self.check(path, "format").result, "ERROR")

    def test_oversized_format_chunk_does_not_stop_other_assets(self):
        path = self.wav("SFX_UI_Broken_01.wav")
        data = bytearray(path.read_bytes())
        struct.pack_into("<I", data, 16, 100000)
        path.write_bytes(data)
        self.wav()
        status, out, err = self.cli(self.assets)
        self.assertEqual(status, 1)
        self.assertIn("[PASS] SFX_UI_Click_01.wav", out)
        self.assertIn("[ERROR] SFX_UI_Broken_01.wav", out)
        self.assertEqual(err, "")

    def test_incomplete_sample_frame_is_error(self):
        path = self.wav(frames=10)
        data = bytearray(path.read_bytes())
        struct.pack_into("<I", data, 40, 19)  # 19 bytes cannot contain whole 16-bit frames.
        path.write_bytes(data[:-1])
        self.assertEqual(self.check(path, "format").result, "ERROR")

    def test_unreadable_file_is_recorded_as_error(self):
        path = self.wav()
        with patch("validator.wave.open", side_effect=PermissionError("permission denied")):
            self.assertEqual(self.check(path, "format").result, "ERROR")

    def test_custom_rules_are_loaded_and_used(self):
        config = self.config({"naming_pattern": r"Dialogue_\d+\.wav", "sample_rate": 44100,
                              "bit_depths": [8], "channels": [2], "min_duration_seconds": 0.01})
        path = self.wav("Dialogue_01.wav", rate=44100, width=1, channels=2)
        self.assertEqual(validate_file(path, self.assets, Rules.load(config)).status, "PASS")

    def test_partial_config_preserves_defaults(self):
        rules = Rules.load(self.config({"channels": [1]}))
        self.assertEqual(rules.channels, (1,))
        self.assertEqual(rules.sample_rate, 48000)

    def test_invalid_configuration_is_rejected(self):
        invalid = [[], {"typo": 1}, {"sample_rate": 0}, {"sample_rate": True},
                   {"sample_rate": 48000.0}, {"channels": []}, {"channels": "mono"},
                   {"channels": [False]}, {"bit_depths": [-1]}, {"bit_depths": [16.0]},
                   {"min_duration_seconds": -1}, {"min_duration_seconds": True},
                   {"min_duration_seconds": float("nan")},
                   {"min_duration_seconds": float("inf")},
                   {"naming_pattern": "["}, {"naming_pattern": ""}, {"naming_pattern": 12}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Rules.load(self.config(value))

    def test_bad_json_is_a_cli_error(self):
        path = self.root / "bad.json"
        path.write_text("{bad json", encoding="utf-8")
        self.wav()
        status, _, err = self.cli(self.assets, "--config", path)
        self.assertEqual(status, 2)
        self.assertNotIn("Traceback", err)

    def test_cli_returns_zero_for_pass_and_one_for_errors(self):
        self.wav()
        self.assertEqual(self.cli(self.assets)[0], 0)
        self.wav("SFX_UI_Bad_01.wav", rate=44100)
        status, out, _ = self.cli(self.assets)
        self.assertEqual(status, 1)
        self.assertIn("2 files: 1 passed, 0 with warnings, 1 with errors.", out)

    def test_warnings_are_nonfatal_unless_strict(self):
        self.wav(frames=100)
        self.assertEqual(self.cli(self.assets)[0], 0)
        self.assertEqual(self.cli(self.assets, "--strict")[0], 1)

    def test_missing_folder_and_empty_folder_are_cli_errors(self):
        for folder in (self.assets, self.root / "missing"):
            with self.subTest(folder=folder):
                status, _, err = self.cli(folder)
                self.assertEqual(status, 2)
                self.assertIn("error:", err)

    def test_csv_contains_per_check_results_and_nested_paths(self):
        self.wav("nested/SFX_UI_Click_01.wav")
        self.wav("SFX_UI_WrongRate_01.wav", rate=44100)
        report = self.root / "report.csv"
        status, _, _ = self.cli(self.assets, "--csv", report)
        self.assertEqual(status, 1)
        with report.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 12)
        self.assertEqual(set(rows[0]), {"file", "check", "result", "expected", "actual", "message"})
        self.assertTrue(any(row["file"] == "nested/SFX_UI_Click_01.wav" for row in rows))
        failures = [row for row in rows if row["result"] == "ERROR"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["actual"], "44100 Hz")

    def test_malformed_file_does_not_stop_scan(self):
        self.wav()
        (self.assets / "SFX_UI_Broken_01.wav").write_bytes(b"broken")
        status, out, _ = self.cli(self.assets)
        self.assertEqual(status, 1)
        self.assertIn("[PASS] SFX_UI_Click_01.wav", out)
        self.assertIn("[ERROR] SFX_UI_Broken_01.wav", out)

    def test_report_inside_assets_is_not_scanned_and_sources_unchanged(self):
        path = self.wav()
        original = hashlib.sha256(path.read_bytes()).digest()
        status, out, _ = self.cli(self.assets, "--csv", self.assets / "report.csv")
        self.assertEqual(status, 0)
        self.assertIn("1 files:", out)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), original)

    def test_reports_cannot_overwrite_existing_files_or_wav(self):
        audio = self.wav()
        report = self.root / "existing.csv"
        report.write_text("keep me", encoding="utf-8")
        for target in (report, audio, self.root / "missing" / "report.csv"):
            with self.subTest(target=target):
                self.assertEqual(self.cli(self.assets, "--csv", target)[0], 2)
        self.assertEqual(report.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(self.check(audio, "format").result, "PASS")

    def test_file_symlinks_are_errors_and_directory_links_are_not_followed(self):
        path = self.wav()
        link = self.assets / "SFX_UI_Link_01.wav"
        try:
            link.symlink_to(path)
            (self.assets / "cycle").symlink_to(self.assets, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(set(scan_files(self.assets)), {path, link})
        self.assertEqual(self.check(link, "format").result, "ERROR")

    def test_directory_read_failure_is_not_silently_ignored(self):
        with patch("validator.os.walk") as walk:
            walk.side_effect = lambda *args, **kwargs: kwargs["onerror"](PermissionError("denied"))
            with self.assertRaises(PermissionError):
                scan_files(self.assets)

    def test_real_entrypoint_preserves_exit_status_and_writes_report(self):
        self.wav(rate=44100)
        script = Path(__file__).resolve().parents[1] / "validator.py"
        report = self.root / "subprocess.csv"
        process = subprocess.run(
            [sys.executable, str(script), str(self.assets), "--csv", str(report)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertIn("44100 Hz", process.stdout)
        self.assertTrue(report.is_file())


if __name__ == "__main__":
    unittest.main()

"""Read-only postflight checks against immutable plans and disposable outputs."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock
import wave

from conversion import (AudioStream, BackendError, ConversionSettings, FFmpegBackend,
                        MediaInfo, build_plan, verify_output)
from validator import Rules


def write_wav(path, rate=48000, channels=1, width=2):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0" * (rate // 10) * channels * width)


class OutputVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.input, self.output_root = self.directory / "Audio", self.directory / "Output"
        self.input.mkdir()
        self.source = self.input / "SFX_UI_Click_01.wav"
        write_wav(self.source)
        self.settings = ConversionSettings("wav", "pcm_s16le", 48000, bit_depth=16)
        self.plan = build_plan(self.input, [self.source], self.output_root, self.settings, probe=self.metadata)
        self.output = self.plan.rows[0].destination
        self.output_root.mkdir()
        self.output.write_bytes(self.source.read_bytes())  # Existing disposable output fixture.
        self.probe = Mock(side_effect=self.metadata)
        self.decode = Mock()

    def metadata(self, path):
        return MediaInfo(path, "wav", 0.1, (AudioStream(0, "pcm_s16le", 48000, "s16", 16, 1, "mono", 0.1),))

    def verify(self, **kwargs):
        return verify_output(self.plan, 0, kwargs.pop("rules", Rules()), probe=self.probe, decode=self.decode, **kwargs)

    @staticmethod
    def check(result, name):
        return next(check for check in result.checks if check.check == name)

    def test_valid_pcm_output_is_fully_read_and_sources_outputs_and_plan_stay_unchanged(self):
        before = {path: path.read_bytes() for path in (self.source, self.output)}
        plan_before = self.plan
        result = self.verify()
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.validation.status, "PASS")
        self.assertEqual(self.check(result, "payload").result, "PASS")
        self.assertEqual(self.check(result, "source_unchanged").result, "PASS")
        self.assertEqual(self.check(result, "output_unchanged").result, "PASS")
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(self.plan, plan_before)
        self.decode.assert_not_called()  # PCM uses the existing complete-frame validator.
        with self.assertRaises(FrozenInstanceError):
            result.output = self.source

    def test_target_container_codec_rate_depth_channels_and_layout_mismatches(self):
        normal = self.metadata(self.output)
        for field, value, check_name in (("codec", "pcm_f32le", "codec"),
                                         ("sample_rate", 44100, "sample_rate"),
                                         ("bit_depth", 24, "bit_depth"),
                                         ("channels", 2, "channels"),
                                         ("channel_layout", "stereo", "channel_layout")):
            with self.subTest(field=field):
                self.probe.side_effect = lambda path: replace(normal, audio_streams=(replace(normal.audio_streams[0], **{field: value}),))
                result = self.verify()
                self.assertEqual(result.status, "ERROR")
                self.assertEqual(self.check(result, check_name).result, "ERROR")
        self.probe.side_effect = lambda path: replace(normal, container="aiff")
        self.assertEqual(self.check(self.verify(), "container").result, "ERROR")

    def test_staged_output_is_verified_before_publication_and_cannot_silently_rename(self):
        staged = self.output_root / ".private-staging" / self.output.name
        staged.parent.mkdir()
        staged.write_bytes(self.source.read_bytes())
        self.output.unlink()  # Fixture: final path has not been published yet.
        result = self.verify(output_path=staged)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.output, staged)
        self.assertFalse(self.output.exists())
        self.assertTrue(staged.exists())
        renamed = staged.with_name("Other.wav")
        staged.rename(renamed)  # Fixture preparation only.
        self.assertEqual(self.verify(output_path=renamed).status, "ERROR")
        self.assertTrue(renamed.exists())

    def test_missing_required_values_and_numeric_zero_remain_distinct(self):
        normal = self.metadata(self.output)
        for rate, expected_actual in ((None, "Unavailable"), (0, "0")):
            self.probe.side_effect = lambda path: replace(normal, audio_streams=(replace(normal.audio_streams[0], sample_rate=rate),))
            check = self.check(self.verify(), "sample_rate")
            self.assertEqual(check.actual, expected_actual)
            self.assertEqual(check.result, "ERROR")

    def test_unavailable_layout_cannot_report_full_verification_pass(self):
        normal = self.metadata(self.output)
        self.probe.side_effect = lambda path: replace(normal, audio_streams=(replace(normal.audio_streams[0], channel_layout=None),))
        result = self.verify()
        self.assertEqual(result.status, "WARNING")
        self.assertEqual(self.check(result, "channel_layout").result, "WARNING")

    def test_duration_resampling_tolerance_detects_truncation_without_exact_float_equality(self):
        normal = self.metadata(self.output)
        for duration, expected in ((0.1 + 1 / 48000, "PASS"), (0.09, "ERROR"), (0.0, "ERROR")):
            self.probe.side_effect = lambda path: replace(normal, audio_streams=(replace(normal.audio_streams[0], duration_seconds=duration),))
            self.assertEqual(self.check(self.verify(), "duration").result, expected)
        self.assertEqual(self.check(self.verify(duration_tolerance_seconds=0.2), "duration").result, "ERROR")  # Zero stays invalid.
        self.probe.side_effect = lambda path: replace(normal, audio_streams=(replace(normal.audio_streams[0], duration_seconds=0.09),))
        self.assertEqual(self.check(self.verify(duration_tolerance_seconds=0.02), "duration").result, "PASS")

    def test_unavailable_duration_and_container_fallback_are_explicit(self):
        normal = self.metadata(self.output)
        self.probe.side_effect = lambda path: replace(normal, audio_streams=(replace(normal.audio_streams[0], duration_seconds=None),))
        result = self.verify()
        self.assertEqual(result.status, "WARNING")
        self.assertEqual(self.check(result, "output_duration_basis").result, "WARNING")
        self.probe.side_effect = lambda path: replace(normal, duration_seconds=None, audio_streams=(replace(normal.audio_streams[0], duration_seconds=None),))
        self.assertEqual(self.check(self.verify(), "duration").actual, "Unavailable")

    def test_truncated_pcm_payload_is_rejected_even_when_probe_headers_look_valid(self):
        original = self.output.read_bytes()
        self.output.write_bytes(original[:-4])
        result = self.verify()
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(self.check(result, "codec").result, "PASS")
        self.assertEqual(self.check(result, "payload").result, "ERROR")
        self.assertEqual(self.output.read_bytes(), original[:-4])  # Failed outputs are not deleted.

    def test_target_match_and_project_rule_failures_are_reported_separately(self):
        result = self.verify(rules=Rules(sample_rate=44100))
        self.assertEqual(self.check(result, "sample_rate").result, "PASS")
        self.assertEqual(result.validation.status, "ERROR")
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.validation_rules.sample_rate, 44100)

    def test_project_duration_warnings_remain_warnings(self):
        result = self.verify(rules=Rules(min_duration_seconds=0.5))
        self.assertEqual(result.validation.status, "WARNING")
        self.assertEqual(result.status, "WARNING")

    def test_missing_empty_directory_and_symlink_outputs_are_rejected_without_probing(self):
        for kind in ("missing", "empty", "directory", "symlink"):
            with self.subTest(kind=kind):
                if self.output.is_dir():
                    self.output.rmdir()
                elif self.output.exists() or self.output.is_symlink():
                    self.output.unlink()
                if kind == "empty":
                    self.output.touch()
                elif kind == "directory":
                    self.output.mkdir()
                elif kind == "symlink":
                    try:
                        self.output.symlink_to(self.source)
                    except (OSError, NotImplementedError):
                        continue
                self.assertEqual(self.verify().status, "ERROR")
        self.probe.assert_not_called()

    def test_hardlink_to_source_is_not_an_independent_output(self):
        self.output.unlink()
        try:
            self.output.hardlink_to(self.source)
        except (OSError, NotImplementedError):
            self.skipTest("hardlinks unavailable")
        self.assertEqual(self.verify().status, "ERROR")
        self.probe.assert_not_called()

    def test_blocked_plan_and_output_outside_tree_are_rejected(self):
        row = self.plan.rows[0]
        self.plan = replace(self.plan, rows=(replace(row, destination=self.source),))
        self.assertEqual(self.verify().status, "ERROR")
        self.plan = replace(self.plan, rows=(replace(row, destination=None),))
        self.assertEqual(self.check(self.verify(), "invalid_plan").result, "ERROR")
        self.probe.assert_not_called()

    def test_source_changed_since_plan_is_rejected(self):
        self.source.write_bytes(b"Externally changed source")
        self.assertEqual(self.check(self.verify(), "source_changed").result, "ERROR")
        self.probe.assert_not_called()

    def test_source_or_output_changed_during_verification_is_rejected(self):
        for path, name in ((self.source, "source_unchanged"), (self.output, "output_unchanged")):
            with self.subTest(path=path):
                original = path.read_bytes()
                def mutate(output):
                    path.write_bytes(b"Externally changed during inspection")
                    return self.metadata(output)
                self.probe.side_effect = mutate
                result = self.verify()
                self.assertEqual(self.check(result, name).result, "ERROR")
                path.write_bytes(original)
                # Recreate the observation after the fixture's external modifications.
                self.plan = build_plan(self.input, [self.source], self.directory / "NewOutput", self.settings, probe=self.metadata)
                self.plan = replace(self.plan, output_root=self.output_root, rows=(replace(self.plan.rows[0], destination=self.output),))

    def test_probe_failure_multiple_streams_and_extra_streams_never_pass(self):
        self.probe.side_effect = BackendError("probe_failed", "Corrupted output")
        self.assertEqual(self.check(self.verify(), "probe_failed").result, "ERROR")
        normal = self.metadata(self.output)
        self.probe.side_effect = lambda path: replace(normal, audio_streams=normal.audio_streams * 2)
        self.assertEqual(self.check(self.verify(), "ambiguous_audio_stream").result, "ERROR")
        self.probe.side_effect = lambda path: replace(normal, non_audio_stream_indices=(1,))
        self.assertEqual(self.check(self.verify(), "unexpected_streams").result, "ERROR")

    def test_probe_metadata_for_a_different_file_is_rejected(self):
        self.probe.side_effect = lambda path: self.metadata(self.source)
        self.assertEqual(self.check(self.verify(), "inspection_failed").result, "ERROR")

    def test_invalid_arguments_are_rejected_before_inspection(self):
        for tolerance in (-1, True, float("inf"), float("nan"), "1"):
            with self.assertRaises(ValueError):
                self.verify(duration_tolerance_seconds=tolerance)
        for index in (-1, 1, True):
            with self.assertRaises(ValueError):
                verify_output(self.plan, index, Rules())
        self.probe.assert_not_called()

    def mp3_plan(self):
        settings = ConversionSettings("mp3", "mp3", 48000, quality=2)
        self.plan = build_plan(self.input, [self.source], self.output_root, settings, probe=self.metadata)
        self.output = self.plan.rows[0].destination
        self.output.write_bytes(b"Disposable MP3 fixture; inspection/decoding are injected")
        self.probe.side_effect = lambda path: replace(self.metadata(path), container="mp3", audio_streams=(
            replace(self.metadata(path).audio_streams[0], codec="mp3", sample_format="fltp", bit_depth=None, duration_seconds=0.124),))

    def test_mp3_requires_full_decode_and_reports_unverifiable_quality_and_project_rules(self):
        self.mp3_plan()
        result = self.verify()
        self.decode.assert_called_once_with(self.output, 0)
        self.assertEqual(result.status, "WARNING")
        self.assertEqual(self.check(result, "payload").result, "PASS")
        self.assertEqual(self.check(result, "duration").result, "PASS")
        self.assertEqual(self.check(result, "vbr_quality").result, "WARNING")
        self.assertEqual(self.check(result, "project_validation").result, "WARNING")
        self.assertIsNone(result.validation)

    def test_mp3_decode_errors_or_missing_executable_are_reported(self):
        self.mp3_plan()
        for code in ("decode_failed", "missing_executable", "timeout"):
            with self.subTest(code=code):
                self.decode.side_effect = BackendError(code, "Cannot decode output")
                self.assertEqual(self.check(self.verify(), code).result, "ERROR")


@unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is unavailable")
class VerificationIntegrationTests(unittest.TestCase):
    def test_real_probe_and_pcm_validation_reject_truncation_without_modifying_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "Audio/SFX_Test_01.wav"
            source.parent.mkdir()
            write_wav(source)
            original = source.read_bytes()
            plan = build_plan(source.parent, [source], root / "Output", ConversionSettings("wav", "pcm_s16le", 48000, bit_depth=16))
            output = plan.rows[0].destination
            output.parent.mkdir()
            output.write_bytes(original)
            result = verify_output(plan, 0, Rules())
            self.assertIn(result.status, ("PASS", "WARNING"))  # Some WAV headers omit layout.
            self.assertEqual(result.validation.status, "PASS")
            output.write_bytes(original[:-4])
            self.assertEqual(verify_output(plan, 0, Rules()).status, "ERROR")
            self.assertEqual(source.read_bytes(), original)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
    def test_real_mp3_fixture_decodes_to_null_with_explicit_verification_limits(self):
        if "libmp3lame" not in {encoder.name for encoder in FFmpegBackend().detect().audio_encoders}:
            self.skipTest("installed FFmpeg has no libmp3lame encoder")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "Audio/SFX_Test_01.wav"
            source.parent.mkdir()
            write_wav(source)
            original = source.read_bytes()
            settings = ConversionSettings("mp3", "mp3", 48000, quality=2)
            plan = build_plan(source.parent, [source], root / "Output", settings)
            output = plan.rows[0].destination
            output.parent.mkdir()
            # Fixture generation only; the application has no conversion executor.
            subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-nostdin", "-n", "-i", str(source),
                            "-c:a", "libmp3lame", "-q:a", "2", str(output)], check=True,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, shell=False)
            result = verify_output(plan, 0, Rules())
            self.assertEqual(result.status, "WARNING")
            self.assertEqual(next(check.result for check in result.checks if check.check == "payload"), "PASS")
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

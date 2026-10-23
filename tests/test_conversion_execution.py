"""PCM execution safety tests plus real resampling and precision checks."""

from dataclasses import replace
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

from conversion import (AudioStream, BackendError, BackendInfo, ConversionSettings, EncoderInfo,
                        FFmpegBackend, MediaInfo, ToolInfo, build_plan, execute_plan)
from conversion.execution import _Cancelled, _run_ffmpeg
from conversion.verification import verify_output
from validator import Check, Rules


def write_signal(path, rate=48000, bits=16, channels=1, silence=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    maximum = 2 ** (bits - 1)
    payload = bytearray()
    for frame in range(rate // 10 + 7):
        for channel in range(channels):
            value = 0 if silence else round(0.2 * maximum * math.sin(2 * math.pi * (440 + channel * 330) * frame / rate))
            payload.extend(value.to_bytes(bits // 8, "little", signed=True))
    with wave.open(str(path), "wb") as audio:
        audio.setparams((channels, bits // 8, rate, 0, "NONE", "not compressed"))
        audio.writeframes(payload)


def pcm(path):
    with wave.open(str(path), "rb") as audio:
        rate, channels, width, frames = audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()
        data = audio.readframes(frames)
    samples = [int.from_bytes(data[index:index + width], "little", signed=True) for index in range(0, len(data), width)]
    return rate, channels, width, frames, samples


class FakeBackend:
    """Real WAV headers via stdlib, no external executables needed for safety tests."""
    def detect(self):
        return BackendInfo(ToolInfo("ffmpeg", Path("/test/ffmpeg"), "test"),
                           ToolInfo("ffprobe", Path("/test/ffprobe"), "test"),
                           tuple(EncoderInfo(f"pcm_s{bits}le", "audio", "PCM", "A....D") for bits in (16, 24)))

    def inspect(self, path):
        try:
            with wave.open(str(path), "rb") as audio:
                bits = audio.getsampwidth() * 8
                channels, rate = audio.getnchannels(), audio.getframerate()
                duration = audio.getnframes() / rate
                stream = AudioStream(0, f"pcm_s{bits}le", rate, "s16" if bits == 16 else "s32",
                                     bits, channels, "mono" if channels == 1 else "stereo", duration)
                return MediaInfo(path, "wav", duration, (stream,))
        except (OSError, wave.Error, EOFError) as error:
            raise BackendError("probe_failed", str(error)) from error


class ExecutionSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.input, self.output = self.root / "Audio", self.root / "Converted"
        self.source = self.input / "SFX_Test_01.wav"
        write_signal(self.source)
        self.backend = FakeBackend()

    def plan(self, sources=None, settings=None):
        settings = settings or ConversionSettings("wav", "pcm_s16le", 48000, bit_depth=16)
        return build_plan(self.input, sources or [self.source], self.output, settings, probe=self.backend.inspect)

    def execute(self, plan=None, **kwargs):
        plan = plan or self.plan()
        rules = kwargs.pop("rules", Rules(sample_rate=plan.settings.sample_rate))
        return execute_plan(plan, rules, backend=self.backend, **kwargs)

    def assert_clean(self):
        self.assertEqual(list(self.root.rglob(".audio-preflight-*")), [])

    def test_unchanged_pcm_is_byte_identical_copy_and_verification_precedes_publication(self):
        plan = self.plan()
        original = self.source.read_bytes()
        observed = []
        def inspect_before_publish(*args, **kwargs):
            self.assertFalse(plan.rows[0].destination.exists())
            self.assertTrue(kwargs["output_path"].exists())
            observed.append(True)
            return verify_output(*args, **kwargs)
        with patch("conversion.execution.verify_output", side_effect=inspect_before_publish):
            result = self.execute(plan)
        self.assertEqual(result.errors, ())
        self.assertEqual(len(observed), 1)
        output = result.outputs[0]
        self.assertEqual(output.command, ())
        self.assertEqual(output.destination.read_bytes(), original)
        self.assertEqual(output.verification.output, output.destination)
        self.assertEqual(output.verification.metadata.path, output.destination)
        self.assertEqual(output.verification.validation.file, self.source.name)
        self.assertEqual(self.source.read_bytes(), original)
        self.assert_clean()

    def test_existing_output_and_stale_source_are_rejected_before_writes(self):
        plan = self.plan()
        self.output.mkdir()
        destination = self.output / self.source.name
        destination.write_bytes(b"Someone else's file")
        result = self.execute(plan)
        self.assertEqual(result.errors[0].code, "stale_plan")
        self.assertEqual(destination.read_bytes(), b"Someone else's file")
        destination.unlink()  # Fixture preparation.
        self.source.write_bytes(self.source.read_bytes() + b"externally changed")
        self.assertEqual(self.execute(plan).errors[0].code, "stale_plan")
        self.assert_clean()

    def test_destination_race_never_overwrites_or_deletes_the_other_file(self):
        plan = self.plan()
        destination = plan.rows[0].destination
        def race(*args, **kwargs):
            destination.write_bytes(b"Created by another application")
            raise FileExistsError("occupied")
        with patch("conversion.execution.os.link", side_effect=race):
            result = self.execute(plan)
        self.assertEqual(result.errors[0].code, "existing_destination")
        self.assertEqual(destination.read_bytes(), b"Created by another application")
        self.assertEqual(result.outputs, ())
        self.assert_clean()

    def test_case_variant_race_removes_only_our_new_link(self):
        plan = self.plan()
        destination = plan.rows[0].destination
        raced = destination.with_name(destination.name.upper())
        real_link = os.link
        def race(*args, **kwargs):
            raced.write_bytes(b"Other application's case variant")
            real_link(*args, **kwargs)
        with patch("conversion.execution.os.link", side_effect=race):
            result = self.execute(plan)
        self.assertTrue(result.errors)
        self.assertEqual(raced.read_bytes(), b"Other application's case variant")
        if destination.exists():  # Case-insensitive macOS path may refer to raced.
            self.assertEqual(destination.read_bytes(), b"Other application's case variant")
        self.assertEqual(result.outputs, ())
        self.assert_clean()

    def test_source_changed_during_conversion_is_not_published_or_restored(self):
        plan = self.plan(settings=ConversionSettings("wav", "pcm_s16le", 44100, bit_depth=16))
        def external_change(command, *args):
            write_signal(Path(command[-1]), rate=44100)
            self.source.write_bytes(b"Externally edited source")
        with patch("conversion.execution._run_ffmpeg", side_effect=external_change):
            result = self.execute(plan)
        self.assertEqual(result.errors[0].code, "source_changed")
        self.assertEqual(self.source.read_bytes(), b"Externally edited source")
        self.assertFalse(plan.rows[0].destination.exists())
        self.assert_clean()

    def test_verification_failure_leaves_no_final_or_temporary_output(self):
        plan = self.plan()
        def fail(*args, **kwargs):
            result = verify_output(*args, **kwargs)
            return replace(result, checks=(*result.checks, Check("output", "bit_depth", "ERROR", "16", "24", "Mismatch")))
        with patch("conversion.execution.verify_output", side_effect=fail):
            result = self.execute(plan)
        self.assertEqual(result.errors[0].code, "verification_failed")
        self.assertIsNotNone(result.failed_verification)
        self.assertFalse(plan.rows[0].destination.exists())
        self.assert_clean()

    def test_failed_or_successful_but_corrupted_ffmpeg_output_is_cleaned(self):
        settings = ConversionSettings("wav", "pcm_s16le", 44100, bit_depth=16)
        plan = self.plan(settings=settings)
        original = self.source.read_bytes()
        def fail(command, cancel, timeout):
            Path(command[-1]).write_bytes(b"partial audio")
            raise BackendError("conversion_failed", "FFmpeg failed")
        with patch("conversion.execution._run_ffmpeg", side_effect=fail):
            result = self.execute(plan)
        self.assertEqual(result.errors[0].code, "conversion_failed")
        self.assertFalse(plan.rows[0].destination.exists())
        self.assert_clean()
        with patch("conversion.execution._run_ffmpeg", side_effect=lambda command, *args: Path(command[-1]).write_bytes(b"corrupt")):
            result = self.execute(plan)
        self.assertEqual(result.errors[0].code, "verification_failed")
        self.assertEqual(self.source.read_bytes(), original)
        self.assert_clean()

    def test_precancel_does_not_create_the_output_directory_or_probe_tools(self):
        event = threading.Event()
        event.set()
        with patch.object(self.backend, "detect") as detect:
            result = self.execute(cancel=event)
        self.assertTrue(result.cancelled)
        self.assertFalse(self.output.exists())
        detect.assert_not_called()

    def test_cancellation_during_conversion_and_after_verification_removes_staging(self):
        plan = self.plan(settings=ConversionSettings("wav", "pcm_s16le", 44100, bit_depth=16))
        event = threading.Event()
        def cancel_while_running(command, *args):
            Path(command[-1]).write_bytes(b"partial")
            event.set()
        with patch("conversion.execution._run_ffmpeg", side_effect=cancel_while_running):
            result = self.execute(plan, cancel=event)
        self.assertTrue(result.cancelled)
        self.assertFalse(plan.rows[0].destination.exists())
        self.assert_clean()
        event.clear()
        plan = self.plan()
        def cancel_after_verify(*args, **kwargs):
            result = verify_output(*args, **kwargs)
            event.set()
            return result
        with patch("conversion.execution.verify_output", side_effect=cancel_after_verify):
            result = self.execute(plan, cancel=event)
        self.assertTrue(result.cancelled)
        self.assertFalse(plan.rows[0].destination.exists())
        self.assert_clean()

    def test_cancellation_keeps_only_previously_completed_outputs(self):
        second = self.input / "nested/SFX_Test_02.wav"
        write_signal(second)
        plan = self.plan([self.source, second])
        event = threading.Event()
        result = self.execute(plan, cancel=event, on_converted=lambda output: event.set())
        self.assertTrue(result.cancelled)
        self.assertEqual(len(result.outputs), 1)
        self.assertTrue(plan.rows[0].destination.exists())
        self.assertFalse(plan.rows[1].destination.exists())
        self.assert_clean()

    def test_missing_tools_and_policy_conflict_create_no_outputs(self):
        capabilities = self.backend.detect()
        with patch.object(self.backend, "detect", return_value=replace(capabilities, ffmpeg=ToolInfo("ffmpeg", None, None))):
            self.assertEqual(self.execute().errors[0].code, "missing_executable")
        self.assertEqual(self.execute(rules=Rules(sample_rate=44100)).errors[0].code, "policy_conflict")
        self.assertFalse(self.output.exists())

    def test_supported_scope_and_explicit_dither_are_enforced(self):
        settings = ConversionSettings("wav", "pcm_s16le", 88200, bit_depth=16)
        self.assertEqual(self.execute(self.plan(settings=settings)).errors[0].code, "unsupported_rate")
        surround = self.input / "SFX_Surround_01.wav"
        write_signal(surround, channels=6)
        self.assertEqual(self.execute(self.plan([surround])).errors[0].code, "unsupported_channels")
        source24 = self.input / "SFX_Wide_01.wav"
        write_signal(source24, bits=24)
        self.assertEqual(self.execute(self.plan([source24])).errors[0].code, "dither_policy_required")
        mp3 = ConversionSettings("mp3", "mp3", 48000, quality=2)
        with patch.object(self.backend, "detect", return_value=replace(self.backend.detect(), encoders=(EncoderInfo("libmp3lame", "audio", "MP3", "A....D"),))):
            self.assertEqual(self.execute(self.plan(settings=mp3)).errors[0].code, "unsupported_target")
        self.assertFalse(self.output.exists())

    def test_timeout_configuration_and_processing_options_are_validated(self):
        for timeout in (0, -1, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.execute(timeout_seconds=timeout)
        for changes in (dict(resampling_quality="unknown"), dict(dither="automatic"), dict(metadata_policy="unknown")):
            with self.assertRaises(ValueError):
                ConversionSettings("wav", "pcm_s16le", 48000, bit_depth=16, **changes)


class ProcessControlTests(unittest.TestCase):
    def test_cancellation_terminates_and_reaps_the_child_without_shell(self):
        event = threading.Event()
        process = Mock()
        process.poll.return_value = None
        def wait(timeout):
            if process.terminate.called:
                return -15
            event.set()
            raise subprocess.TimeoutExpired(["ffmpeg"], timeout)
        process.wait.side_effect = wait
        with patch("conversion.execution.subprocess.Popen", return_value=process) as popen:
            with self.assertRaises(_Cancelled):
                _run_ffmpeg(["ffmpeg", "-n", "temporary.wav"], event, 10)
        process.terminate.assert_called_once()
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertIsInstance(popen.call_args.args[0], list)

    def test_timeout_kills_an_unresponsive_child(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired(["ffmpeg"], 1), -9]
        with patch("conversion.execution.subprocess.Popen", return_value=process), patch("conversion.execution.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(BackendError) as caught:
                _run_ffmpeg(["ffmpeg"], threading.Event(), 1)
        self.assertEqual(caught.exception.code, "timeout")
        process.terminate.assert_called_once()
        process.kill.assert_called_once()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe are required")
class PcmConversionIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.input = self.root / "Audio Exports - Téšt"
        self.backend = FFmpegBackend()

    def convert(self, source, target_rate, target_bits, **options):
        target = ConversionSettings("wav", f"pcm_s{target_bits}le", target_rate, bit_depth=target_bits, **options)
        plan = build_plan(self.input, [source], self.root / f"Output {target_rate} {target_bits} {len(list(self.root.iterdir()))}", target)
        original = source.read_bytes()
        result = execute_plan(plan, Rules(sample_rate=target_rate))
        self.assertEqual(result.errors, ())
        self.assertFalse(result.cancelled)
        self.assertEqual(len(result.outputs), 1)
        output = result.outputs[0]
        actual = self.backend.inspect(output.destination).select_audio_stream()
        self.assertEqual((actual.sample_rate, actual.codec, actual.bit_depth), (target_rate, f"pcm_s{target_bits}le", target_bits))
        self.assertNotEqual(output.verification.status, "ERROR")
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.root.rglob(".audio-preflight-*")), [])
        return output

    def test_44100_to_48000_and_48000_to_44100_resample_audio_not_only_headers(self):
        for source_rate, target_rate, channels in ((44100, 48000, 1), (48000, 44100, 2)):
            with self.subTest(source_rate=source_rate, channels=channels):
                source = self.input / f"nested/SFX_Test_{channels:02}.wav"
                write_signal(source, source_rate, channels=channels)
                converted = self.convert(source, target_rate, 16)
                rate, actual_channels, width, frames, samples = pcm(converted.destination)
                source_frames = pcm(source)[3]
                self.assertEqual(actual_channels, channels)
                self.assertLessEqual(abs(frames / rate - source_frames / source_rate), max(1 / rate, 1 / source_rate) + 0.000001)
                for channel in range(channels):
                    # Preserve tone frequency/amplitude and distinct stereo channels.
                    error = [(samples[frame * channels + channel] / 32768 - 0.2 * math.sin(2 * math.pi * (440 + channel * 330) * frame / rate)) ** 2
                             for frame in range(100, frames - 100)]
                    self.assertLess(math.sqrt(sum(error) / len(error)), 0.002)
                self.assertIn("nested", converted.destination.parts)

    def test_16_to_24_preserves_sample_values_and_24_to_16_uses_explicit_policy(self):
        source = self.input / "SFX_Precision_01.wav"
        write_signal(source, bits=16)
        up = self.convert(source, 48000, 24)
        self.assertEqual([value >> 8 for value in pcm(up.destination)[4]], pcm(source)[4])
        source24 = self.input / "SFX_Precision_02.wav"
        write_signal(source24, bits=24, channels=2)
        down = self.convert(source24, 48000, 16, dither="none")
        self.assertEqual(pcm(down.destination)[1], 2)
        self.assertLessEqual(max(abs(output - original / 256) for output, original in zip(pcm(down.destination)[4], pcm(source24)[4])), 1)

    def test_all_supported_rates_and_resampling_quality_options(self):
        source = self.input / "SFX_Rates_01.wav"
        write_signal(source)
        for rate in (22050, 32000, 44100, 48000, 96000):
            with self.subTest(rate=rate):
                self.convert(source, rate, 16)
        for quality in ("fast", "high"):
            with self.subTest(quality=quality):
                converted = self.convert(source, 44100, 24, resampling_quality=quality)
                self.assertTrue(any(f"resampler=swr" in value for value in converted.command))

    def test_triangular_dither_is_applied_at_the_target_precision(self):
        source = self.input / "SFX_Dither_01.wav"
        write_signal(source, bits=24, silence=True)
        converted = self.convert(source, 48000, 16, dither="triangular")
        values = pcm(converted.destination)[4]
        self.assertTrue(any(value != 0 for value in values))
        self.assertLessEqual(max(map(abs, values)), 2)

    def test_real_ffmpeg_failure_cleans_partial_output(self):
        source = self.input / "SFX_Failure_01.wav"
        write_signal(source, rate=44100)
        settings = ConversionSettings("wav", "pcm_s16le", 48000, bit_depth=16)
        plan = build_plan(self.input, [source], self.root / "Output", settings)
        original = source.read_bytes()
        from conversion.execution import _command
        def broken_command(*args):
            command = _command(*args)
            command[command.index("-af") + 1] = "deliberately_missing_audio_filter"
            return command
        with patch("conversion.execution._command", side_effect=broken_command):
            result = execute_plan(plan, Rules())
        self.assertEqual(result.errors[0].code, "conversion_failed")
        self.assertFalse(plan.rows[0].destination.exists())
        self.assertEqual(list(self.root.rglob(".audio-preflight-*")), [])
        self.assertEqual(source.read_bytes(), original)

    def test_real_ffmpeg_cancellation_reaps_process_and_cleans_staging(self):
        source = self.input / "SFX_Cancel_01.wav"
        write_signal(source, rate=44100)
        plan = build_plan(self.input, [source], self.root / "Output",
                          ConversionSettings("wav", "pcm_s16le", 48000, bit_depth=16))
        original, event, processes = source.read_bytes(), threading.Event(), []
        real_popen = subprocess.Popen
        def start_and_cancel(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            event.set()
            return process
        def run(*args):
            with patch("conversion.execution.subprocess.Popen", side_effect=start_and_cancel):
                _run_ffmpeg(*args)
        with patch("conversion.execution._run_ffmpeg", side_effect=run):
            result = execute_plan(plan, Rules(), cancel=event)
        self.assertTrue(result.cancelled)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(plan.rows[0].destination.exists())
        self.assertEqual(list(self.root.rglob(".audio-preflight-*")), [])
        self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

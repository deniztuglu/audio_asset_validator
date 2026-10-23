"""Backend unit tests run without FFmpeg; integration tests use disposable media."""

import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import Mock
import wave

from conversion import BackendError, FFmpegBackend


def write_wav(path, channels=1, width=2, rate=48000):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(b"\0" * (rate // 10) * channels * width)
    return path


def metadata(**fields):
    stream = {"index": 0, "codec_type": "audio", "codec_name": "pcm_s16le",
              "sample_rate": "48000", "sample_fmt": "s16", "bits_per_sample": 16,
              "channels": 1, "channel_layout": "mono", "duration": "0.100000"}
    stream.update(fields)
    return {"format": {"format_name": "wav", "duration": "0.100000"}, "streams": [stream]}


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class BackendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.source = write_wav(self.directory / "Audio Exports - Téšt with spaces.wav")
        self.finder = Mock(side_effect=lambda tool: str(self.directory / "Tool Folder" / tool))
        self.runner = Mock(return_value=completed(json.dumps(metadata())))
        self.backend = FFmpegBackend(runner=self.runner, finder=self.finder)

    def inspect(self, payload):
        self.runner.return_value = completed(json.dumps(payload))
        return self.backend.inspect(self.source)

    def assert_error(self, code, callback):
        with self.assertRaises(BackendError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_detects_both_versions_and_encoder_flags_without_parsing_the_legend(self):
        self.runner.side_effect = [
            completed("ffmpeg version n7.1-custom Copyright\nconfiguration: ..."),
            completed("ffprobe version 7.1 Copyright\n"),
            completed("Encoders:\n A..... = Audio\n V..... = Video\n ------\n"
                      " A....D pcm_s24le PCM signed 24-bit\n"
                      " A..X.D libtest Experimental audio (codec test)\n"
                      " V....D video_encoder Video\n S..... subtitle_encoder Subtitle\n"),
        ]
        info = self.backend.detect()
        self.assertTrue(info.ffmpeg.available)
        self.assertTrue(info.ffprobe.available)
        self.assertEqual(info.ffmpeg.version, "n7.1-custom")
        self.assertEqual(info.ffprobe.version, "7.1")
        self.assertEqual([encoder.name for encoder in info.audio_encoders], ["pcm_s24le", "libtest"])
        self.assertTrue(info.audio_encoders[1].experimental)
        self.assertEqual(info.audio_encoders[1].codec, "test")
        self.assertEqual(len(info.encoders), 4)
        self.assertIsNone(info.encoder_error)
        for call in self.runner.call_args_list:
            self.assertIsInstance(call.args[0], list)
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["timeout"], 15)

    def test_missing_both_executables_returns_independent_availability(self):
        self.finder.return_value = None
        self.finder.side_effect = None
        info = self.backend.detect()
        for tool in (info.ffmpeg, info.ffprobe):
            self.assertFalse(tool.available)
            self.assertIsNone(tool.path)
            self.assertIsNone(tool.version)
            self.assertIn("missing_executable", tool.error)
        self.assertEqual(info.encoders, ())
        self.runner.assert_not_called()

    def test_missing_ffmpeg_does_not_prevent_ffprobe_inspection(self):
        self.finder.side_effect = lambda tool: None if tool == "ffmpeg" else str(self.directory / "ffprobe")
        self.runner.side_effect = [completed("ffprobe version 7.1\n"), completed(json.dumps(metadata()))]
        info = self.backend.detect()
        self.assertFalse(info.ffmpeg.available)
        self.assertTrue(info.ffprobe.available)
        self.assertEqual(self.backend.inspect(self.source).select_audio_stream().channels, 1)

    def test_missing_ffprobe_does_not_hide_ffmpeg_capabilities(self):
        self.finder.side_effect = lambda tool: None if tool == "ffprobe" else str(self.directory / "ffmpeg")
        self.runner.side_effect = [completed("ffmpeg version 7.1\n"), completed("Encoders:\n A....D pcm_s16le PCM\n")]
        info = self.backend.detect()
        self.assertTrue(info.ffmpeg.available)
        self.assertFalse(info.ffprobe.available)
        self.assertEqual(info.audio_encoders[0].name, "pcm_s16le")
        self.assert_error("missing_executable", lambda: self.backend.inspect(self.source))

    def test_custom_executable_paths_are_passed_as_single_arguments(self):
        custom = self.directory / "Custom Tools" / "ffprobe"
        self.finder.side_effect = None
        self.finder.return_value = str(custom)
        backend = FFmpegBackend(ffprobe=custom, finder=self.finder, runner=self.runner)
        backend.inspect(self.source)
        self.finder.assert_called_once_with(str(custom))
        self.assertEqual(self.runner.call_args.args[0][0], str(custom))

    def test_encoder_failure_does_not_make_detected_tools_unavailable(self):
        self.runner.side_effect = [completed("ffmpeg version 7.1"), completed("ffprobe version 7.1"),
                                   completed(stderr="listing failed", returncode=1)]
        info = self.backend.detect()
        self.assertTrue(info.ffmpeg.available)
        self.assertTrue(info.ffprobe.available)
        self.assertEqual(info.encoders, ())
        self.assertIsNotNone(info.encoder_error)

    def test_unrecognized_version_and_encoder_responses_are_reported(self):
        self.runner.side_effect = [completed("not ffmpeg"), completed("ffprobe version 7.1")]
        self.assertIn("invalid_response", self.backend.detect().ffmpeg.error)
        self.runner.side_effect = [completed("ffmpeg version 7.1"), completed("ffprobe version 7.1"), completed("not an encoder list")]
        self.assertIn("unrecognized", self.backend.detect().encoder_error)

    def test_inspects_metadata_and_passes_paths_with_spaces_without_a_shell(self):
        before = self.source.read_bytes()
        info = self.backend.inspect(self.source)
        self.assertEqual(info.path, self.source)
        self.assertEqual(info.container, "wav")
        self.assertEqual(info.duration_seconds, 0.1)
        audio = info.select_audio_stream()
        self.assertEqual((audio.codec, audio.sample_rate, audio.sample_format, audio.bit_depth,
                          audio.channels, audio.channel_layout, audio.duration_seconds),
                         ("pcm_s16le", 48000, "s16", 16, 1, "mono", 0.1))
        command = self.runner.call_args.args[0]
        self.assertEqual(command[-2:], ["-i", str(self.source)])
        self.assertFalse(self.runner.call_args.kwargs["shell"])
        self.assertNotIn("-select_streams", command)
        self.assertEqual(self.source.read_bytes(), before)

    def test_missing_metadata_is_none_while_numeric_zero_remains_zero(self):
        info = self.inspect({"streams": [{"index": 0, "codec_type": "audio"}]})
        audio = info.select_audio_stream()
        self.assertEqual((info.container, info.duration_seconds, audio.codec, audio.sample_rate,
                          audio.sample_format, audio.bit_depth, audio.channels,
                          audio.channel_layout, audio.duration_seconds), (None,) * 9)
        payload = metadata(sample_rate="0", channels=0, duration="0", bits_per_sample=0)
        payload["format"]["duration"] = "0.0"
        zero = self.inspect(payload)
        self.assertEqual(zero.duration_seconds, 0.0)
        self.assertEqual(zero.select_audio_stream().duration_seconds, 0.0)
        self.assertEqual(zero.select_audio_stream().sample_rate, 0)
        self.assertEqual(zero.select_audio_stream().channels, 0)
        self.assertEqual(zero.select_audio_stream().bits_per_sample, 0)
        self.assertIsNone(zero.select_audio_stream().bit_depth)

    def test_na_and_unknown_strings_are_not_invented_values(self):
        audio = self.inspect(metadata(sample_rate="N/A", sample_fmt="unknown", channels="N/A",
                                      channel_layout="unknown", duration="N/A",
                                      bits_per_sample="N/A")).select_audio_stream()
        self.assertEqual((audio.sample_rate, audio.sample_format, audio.bit_depth, audio.channels,
                          audio.channel_layout, audio.duration_seconds), (None,) * 6)

    def test_bit_depth_uses_effective_bits_instead_of_the_s32_sample_format(self):
        audio = self.inspect(metadata(codec_name="pcm_s24le", sample_fmt="s32", bits_per_sample=32,
                                      bits_per_raw_sample="24")).select_audio_stream()
        self.assertEqual(audio.bit_depth, 24)
        self.assertEqual((audio.bits_per_raw_sample, audio.bits_per_sample), (24, 32))
        audio = self.inspect(metadata(codec_name="aac", sample_fmt="fltp", bits_per_sample=0)).select_audio_stream()
        self.assertIsNone(audio.bit_depth)
        self.assertEqual(audio.sample_format, "fltp")

    def test_container_duration_does_not_fill_an_unknown_stream_duration(self):
        info = self.inspect(metadata(duration="N/A"))
        self.assertEqual(info.duration_seconds, 0.1)
        self.assertIsNone(info.select_audio_stream().duration_seconds)

    def test_multiple_audio_streams_retain_absolute_indices_and_require_selection(self):
        payload = metadata(index=2)
        payload["streams"] = [{"index": 0, "codec_type": "video"}, payload["streams"][0],
                              {"index": 5, "codec_type": "audio", "codec_name": "flac", "channels": 2}]
        info = self.inspect(payload)
        self.assertEqual([stream.index for stream in info.audio_streams], [2, 5])
        self.assertEqual(info.non_audio_stream_indices, (0,))
        self.assert_error("ambiguous_audio_stream", info.select_audio_stream)
        self.assertEqual(info.select_audio_stream(5).channels, 2)
        self.assert_error("invalid_audio_stream", lambda: info.select_audio_stream(0))
        self.assert_error("invalid_audio_stream", lambda: info.select_audio_stream(True))

    def test_unrecognized_codec_is_explicit_without_claiming_decoding_support(self):
        info = self.inspect(metadata(codec_name="unknown", sample_fmt="unknown", bits_per_sample=0))
        self.assertIsNone(info.select_audio_stream().codec)
        self.assertIn("decoding support is unknown", info.warnings[0])
        self.assertIsNone(info.select_audio_stream().bit_depth)

    def test_no_audio_streams_has_a_distinct_error(self):
        for streams in ([], [{"index": 0, "codec_type": "video"}]):
            with self.subTest(streams=streams):
                self.assert_error("no_audio_streams", lambda: self.inspect({"streams": streams}))

    def test_corrupt_or_unsupported_media_has_bounded_diagnostics_and_exit_code(self):
        self.runner.return_value = completed(stderr="Invalid data found " + "x" * 5000, returncode=1)
        error = self.assert_error("probe_failed", lambda: self.backend.inspect(self.source))
        self.assertEqual(error.returncode, 1)
        self.assertEqual(len(error.diagnostic), 4000)

    def test_json_error_object_is_not_treated_as_success(self):
        error = self.assert_error("probe_failed", lambda: self.inspect({"error": {"code": -1, "string": "unsupported codec"}}))
        self.assertIn("unsupported codec", error.diagnostic)

    def test_malformed_json_and_invalid_field_shapes_are_structured_failures(self):
        self.runner.return_value = completed("not JSON")
        self.assert_error("invalid_response", lambda: self.backend.inspect(self.source))
        for payload in ([], {"streams": "bad"}, {"format": []}, {"streams": [None]},
                        metadata(index=None), metadata(index=True), metadata(channels=-1),
                        metadata(sample_rate="bad"), metadata(duration="NaN"), metadata(duration=True),
                        metadata(duration="Infinity"), metadata(codec_name=123)):
            with self.subTest(payload=payload):
                self.assert_error("invalid_response", lambda: self.inspect(payload))
        duplicate = metadata()
        duplicate["streams"].append(dict(duplicate["streams"][0]))
        self.assert_error("invalid_response", lambda: self.inspect(duplicate))

    def test_partial_probe_diagnostics_are_visible(self):
        self.runner.return_value = completed(json.dumps(metadata()), stderr="Some metadata could not be read")
        self.assertIn("Some metadata could not be read", self.backend.inspect(self.source).warnings)

    def test_timeout_and_unusable_executable_are_structured_failures(self):
        self.runner.side_effect = subprocess.TimeoutExpired(["ffprobe"], 15)
        self.assert_error("timeout", lambda: self.backend.inspect(self.source))
        self.assertIn("timeout", self.backend.detect().ffprobe.error)
        self.runner.side_effect = PermissionError("permission denied")
        self.assert_error("executable_failed", lambda: self.backend.inspect(self.source))
        self.assertIn("executable_failed", self.backend.detect().ffmpeg.error)

    def test_nonfiles_and_symlinks_are_not_passed_to_ffprobe(self):
        for path in (self.directory / "missing.wav", self.directory):
            self.assert_error("invalid_input", lambda: self.backend.inspect(path))
        link = self.directory / "link.wav"
        try:
            link.symlink_to(self.source)
        except (OSError, NotImplementedError):
            pass
        else:
            self.assert_error("invalid_input", lambda: self.backend.inspect(link))
        self.runner.assert_not_called()

    def test_invalid_timeout_is_rejected_before_launching_tools(self):
        for timeout in (0, -1, float("nan"), float("inf"), True, "15"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                FFmpegBackend(timeout=timeout)

    def test_decode_checks_use_an_explicit_stream_and_null_sink_without_a_shell(self):
        original = self.source.read_bytes()
        self.runner.return_value = completed()
        self.backend.check_decode(self.source, 3)
        command = self.runner.call_args.args[0]
        self.assertEqual(command[-5:], ["-map", "0:3", "-f", "null", "-"])
        self.assertEqual(command[command.index("-i") + 1], str(self.source))
        self.assertIn("-xerror", command)
        self.assertFalse(self.runner.call_args.kwargs["shell"])
        self.assertEqual(self.source.read_bytes(), original)

    def test_decode_failures_diagnostics_and_timeouts_are_structured(self):
        self.runner.return_value = completed(stderr="Corrupt frame", returncode=1)
        self.assert_error("decode_failed", lambda: self.backend.check_decode(self.source, 0))
        self.runner.return_value = completed(stderr="Corrupt frame", returncode=0)
        self.assert_error("decode_failed", lambda: self.backend.check_decode(self.source, 0))
        self.runner.side_effect = subprocess.TimeoutExpired(["ffmpeg"], 15)
        self.assert_error("timeout", lambda: self.backend.check_decode(self.source, 0))
        self.finder.side_effect = None
        self.finder.return_value = None
        self.assert_error("missing_executable", lambda: self.backend.check_decode(self.source, 0))

    def test_decode_rejects_invalid_stream_indices_and_nonfiles(self):
        for index in (-1, True, "0"):
            with self.assertRaises(ValueError):
                self.backend.check_decode(self.source, index)
        self.assert_error("invalid_input", lambda: self.backend.check_decode(self.directory, 0))
        self.runner.assert_not_called()


@unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is not installed; backend unit tests still run")
class BackendIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.backend = FFmpegBackend()

    def test_real_mono_16_bit_and_stereo_24_bit_wavs_with_spaces(self):
        for channels, width, codec, sample_format in ((1, 2, "pcm_s16le", "s16"), (2, 3, "pcm_s24le", "s32")):
            with self.subTest(channels=channels, width=width):
                source = write_wav(self.directory / f"Téšt audio {channels} channels.wav", channels, width)
                original = hashlib.sha256(source.read_bytes()).digest()
                info = self.backend.inspect(source)
                audio = info.select_audio_stream()
                self.assertEqual(info.container, "wav")
                self.assertAlmostEqual(info.duration_seconds, 0.1, places=5)
                self.assertEqual((audio.codec, audio.sample_rate, audio.sample_format, audio.bit_depth, audio.channels),
                                 (codec, 48000, sample_format, width * 8, channels))
                self.assertAlmostEqual(audio.duration_seconds, 0.1, places=5)
                self.assertIn(audio.channel_layout, (None, "mono" if channels == 1 else "stereo"))
                self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), original)

    def test_real_corrupted_audio_returns_a_probe_failure(self):
        source = self.directory / "Broken audio.wav"
        source.write_bytes(b"not audio data")
        with self.assertRaises(BackendError) as caught:
            self.backend.inspect(source)
        self.assertEqual(caught.exception.code, "probe_failed")
        self.assertTrue(caught.exception.diagnostic)
        self.assertEqual(source.read_bytes(), b"not audio data")

    def test_real_file_without_audio_returns_no_audio_streams(self):
        # A complete one-pixel GIF fixture, recognizable without an audio stream.
        source = self.directory / "Image without audio.gif"
        source.write_bytes(bytes.fromhex(
            "47494638396101000100800000000000ffffff21f9040100000000"
            "2c00000000010001000002024401003b"))
        with self.assertRaises(BackendError) as caught:
            self.backend.inspect(source)
        self.assertEqual(caught.exception.code, "no_audio_streams")

    def test_float_wav_is_inspected_without_claiming_validator_compatibility(self):
        source = self.directory / "Float audio.wav"
        fmt = struct.pack("<HHIIHH", 3, 1, 48000, 48000 * 4, 4, 32)
        data = b"\0" * 4800 * 4
        chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
        source.write_bytes(b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks)
        audio = self.backend.inspect(source).select_audio_stream()
        self.assertEqual(audio.codec, "pcm_f32le")
        self.assertEqual(audio.sample_format, "flt")
        self.assertEqual(audio.bit_depth, 32)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is needed only to mux the multistream test fixture")
    def test_real_multiple_audio_streams_are_not_silently_reduced_to_the_first(self):
        mono = write_wav(self.directory / "Mono source.wav")
        stereo = write_wav(self.directory / "Stereo source.wav", channels=2)
        destination = self.directory / "Two audio tracks.mka"
        # Fixture creation only: stream copy, no audio conversion implementation.
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-nostdin", "-n", "-i", str(mono),
                        "-i", str(stereo), "-map", "0:a:0", "-map", "1:a:0", "-c", "copy", str(destination)],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=15, shell=False)
        info = self.backend.inspect(destination)
        self.assertEqual([stream.channels for stream in info.audio_streams], [1, 2])
        with self.assertRaises(BackendError) as caught:
            info.select_audio_stream()
        self.assertEqual(caught.exception.code, "ambiguous_audio_stream")
        self.assertEqual(info.select_audio_stream(1).channels, 2)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_real_versions_and_pcm_encoder_capabilities(self):
        info = self.backend.detect()
        self.assertTrue(info.ffmpeg.available)
        self.assertTrue(info.ffprobe.available)
        self.assertTrue(info.ffmpeg.version)
        self.assertTrue(info.ffprobe.version)
        self.assertIsNone(info.encoder_error)
        self.assertTrue({"pcm_s16le", "pcm_s24le"}.issubset({encoder.name for encoder in info.audio_encoders}))


if __name__ == "__main__":
    unittest.main()

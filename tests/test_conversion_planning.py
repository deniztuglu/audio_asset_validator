"""Pure planning tests: fake metadata, real disposable paths, no external tools."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import Mock
import wave

from conversion import (AudioStream, BackendError, BackendInfo, ConversionSettings,
                        EncoderInfo, MediaInfo, ToolInfo, build_plan)


def wav_settings(**changes):
    values = dict(container="wav", codec="pcm_s16le", sample_rate=48000, bit_depth=16)
    return ConversionSettings(**(values | changes))


def mp3_settings(**changes):
    values = dict(container="mp3", codec="mp3", sample_rate=48000, quality=2)
    return ConversionSettings(**(values | changes))


class ConversionSettingsTests(unittest.TestCase):
    def test_settings_are_immutable_and_codec_is_distinct_from_encoder(self):
        settings = wav_settings()
        with self.assertRaises(FrozenInstanceError):
            settings.sample_rate = 44100
        self.assertEqual(settings.channel_policy, "preserve")
        self.assertEqual((settings.extension, settings.encoder), (".wav", "pcm_s16le"))
        self.assertEqual((mp3_settings().codec, mp3_settings().encoder), ("mp3", "libmp3lame"))
        self.assertEqual(wav_settings(codec="pcm_s24le", bit_depth=24).bit_depth, 24)

    def test_incompatible_settings_are_rejected_without_fallbacks(self):
        invalid = [dict(container="aiff"), dict(codec="aac"), dict(codec="pcm_s24le"),
                   dict(bit_depth=None), dict(bit_depth=24), dict(bit_depth=True), dict(quality=2),
                   dict(sample_rate=0), dict(sample_rate=-1), dict(sample_rate=48000.0),
                   dict(sample_rate=True), dict(sample_rate=2**31), dict(channel_policy="stereo"),
                   dict(container=[]), dict(codec=[]), dict(channel_policy=[])]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                wav_settings(**changes)

    def test_mp3_requires_explicit_quality_and_supported_rate(self):
        for quality in (0, 9):
            self.assertEqual(mp3_settings(quality=quality).quality, quality)
        for changes in (dict(quality=None), dict(quality=-1), dict(quality=10), dict(quality=True),
                        dict(quality=2.5), dict(bit_depth=16), dict(codec="libmp3lame"),
                        dict(sample_rate=96000)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                mp3_settings(**changes)


class ConversionPlanningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.input = self.root / "Audio Exports - Téšt"
        self.input.mkdir()
        self.output = self.root / "Converted assets"
        self.source = self.file("SFX_Click_01.wav")
        self.probe = Mock(side_effect=self.metadata)

    def file(self, name):
        path = self.input / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Disposable input fixture; probing is injected.")
        return path

    def metadata(self, source):
        return MediaInfo(source, "wav", 0.1, (AudioStream(0, "pcm_s16le", 48000, "s16", 16, 2, "stereo", 0.1),))

    def plan(self, sources=None, settings=None, output=None, **kwargs):
        return build_plan(self.input, [self.source] if sources is None else sources,
                          self.output if output is None else output, settings or wav_settings(),
                          probe=self.probe, **kwargs)

    @staticmethod
    def codes(plan):
        return {issue.code for issue in plan.errors} | {issue.code for row in plan.rows for issue in row.errors}

    def test_dry_run_preserves_paths_metadata_layout_and_input_bytes_without_writes(self):
        nested = self.file("nested/Footsteps/SFX_Concrete_01.WAV")
        before = {path: path.read_bytes() for path in (self.source, nested)}
        plan = self.plan([self.source, nested])
        self.assertTrue(plan.is_valid)
        self.assertFalse(self.output.exists())
        self.assertEqual(plan.rows[1].destination, self.output / "nested/Footsteps/SFX_Concrete_01.WAV")
        row = plan.rows[0]
        self.assertEqual(row.source, self.source)
        self.assertEqual(row.metadata, self.metadata(self.source))
        self.assertIsNotNone(row.source_stamp)
        self.assertEqual((row.target_channels, row.target_channel_layout), (2, "stereo"))
        self.assertEqual(row.settings, wav_settings())
        self.assertEqual([op.kind for op in row.operations], ["copy"])
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertTrue(any("availability has not been checked" in warning for warning in plan.warnings))
        with self.assertRaises(FrozenInstanceError):
            row.destination = self.source

    def test_expected_resampling_encoding_and_depth_changes_are_explicit(self):
        target = wav_settings(codec="pcm_s24le", bit_depth=24, sample_rate=44100)
        plan = self.plan(settings=target)
        self.assertTrue(plan.is_valid)
        operations = {op.kind: (op.before, op.after) for op in plan.rows[0].operations}
        self.assertEqual(operations["resample"], (48000, 44100))
        self.assertEqual(operations["encode"], ("pcm_s16le", "pcm_s24le"))
        self.assertEqual(operations["bit_depth"], (16, 24))
        self.assertFalse({"normalize", "downmix", "rename"} & set(operations))

    def test_lossy_target_keeps_the_stem_and_records_quality_and_extension(self):
        source = self.file("nested/SFX_Footstep.wav")
        plan = self.plan([source], mp3_settings(quality=0))
        self.assertTrue(plan.is_valid)
        row = plan.rows[0]
        self.assertEqual(row.destination, self.output / "nested/SFX_Footstep.mp3")
        self.assertEqual(row.settings.quality, 0)
        self.assertIsNone(row.settings.bit_depth)
        self.assertIn("vbr_quality", {op.kind for op in row.operations})
        self.assertIn("extension", {op.kind for op in row.operations})
        self.assertTrue(any("lossy" in warning for warning in row.warnings))

    def test_same_mp3_codec_is_still_a_reencode_with_explicit_quality(self):
        media = self.metadata(self.source)
        self.probe.side_effect = lambda source: replace(media, container="mp3", audio_streams=(
            replace(media.audio_streams[0], codec="mp3", sample_format="fltp", bit_depth=None),))
        row = self.plan(settings=mp3_settings()).rows[0]
        self.assertIn("encode", {op.kind for op in row.operations})
        self.assertNotIn("copy", {op.kind for op in row.operations})

    def test_mono_and_surround_wav_channels_are_preserved_without_downmix(self):
        media = self.metadata(self.source)
        for channels, layout in ((1, "mono"), (6, "5.1")):
            with self.subTest(channels=channels):
                self.probe.side_effect = lambda source: replace(media, audio_streams=(
                    replace(media.audio_streams[0], channels=channels, channel_layout=layout),))
                plan = self.plan()
                self.assertTrue(plan.is_valid)
                self.assertEqual((plan.rows[0].target_channels, plan.rows[0].target_channel_layout), (channels, layout))
                self.assertNotIn("downmix", {op.kind for op in plan.rows[0].operations})

    def test_mp3_blocks_surround_and_unrepresentable_layouts(self):
        media = self.metadata(self.source)
        for channels, layout in ((6, "5.1"), (2, "FL+FC")):
            self.probe.side_effect = lambda source: replace(media, audio_streams=(
                replace(media.audio_streams[0], channels=channels, channel_layout=layout),))
            plan = self.plan(settings=mp3_settings())
            self.assertFalse(plan.is_valid)
            self.assertTrue({"unsupported_channels", "unsupported_layout"} & self.codes(plan))
            self.assertEqual(plan.rows[0].target_channels, channels)

    def test_overlapping_trees_are_rejected_in_both_directions(self):
        for output in (self.input, self.input / "output", self.root, self.input / "nested/../output"):
            with self.subTest(output=output):
                plan = self.plan(output=output)
                self.assertFalse(plan.is_valid)
                self.assertIn("overlapping_roots", self.codes(plan))
                self.assertIsNone(plan.rows[0].destination)
        self.probe.assert_not_called()

    def test_case_variant_input_root_overlap_is_rejected_portably(self):
        plan = self.plan(output=self.root / self.input.name.upper() / "output")
        self.assertIn("overlapping_roots", self.codes(plan))

    def test_existing_output_and_hardlinks_are_never_replaced(self):
        self.output.mkdir()
        existing = self.output / self.source.name
        existing.write_bytes(b"Existing output")
        plan = self.plan()
        self.assertIn("existing_destination", self.codes(plan))
        self.assertEqual(existing.read_bytes(), b"Existing output")
        existing.unlink()  # Fixture preparation only.
        try:
            existing.hardlink_to(self.source)
        except (OSError, NotImplementedError):
            return
        original = self.source.read_bytes()
        self.assertIn("existing_destination", self.codes(self.plan()))
        self.assertEqual(self.source.read_bytes(), original)

    def test_existing_case_unicode_and_directory_conflicts_block_plans(self):
        self.output.mkdir()
        existing = self.output / self.source.name.upper()
        existing.write_bytes(b"Do not overwrite")
        self.assertIn("existing_collision", self.codes(self.plan()))
        nested = self.file("Subfolder/Test.wav")
        (self.output / "subfolder").mkdir()
        self.assertIn("existing_collision", self.codes(self.plan([nested])))
        unicode_source = self.file("Café.wav")
        (self.output / "Cafe\u0301.wav").write_bytes(b"Keep original normalization")
        self.assertIn("existing_collision", self.codes(self.plan([unicode_source])))
        (self.output / "FileParent").write_bytes(b"A file, not a folder")
        self.assertIn("unsafe_destination", self.codes(self.plan([self.file("FileParent/Child.wav")])))

    def test_duplicate_converted_names_are_reported_on_every_affected_row(self):
        sources = [self.file("nested/Click.wav"), self.file("nested/Click.aiff")]
        plan = self.plan(sources, mp3_settings())
        self.assertFalse(plan.is_valid)
        self.assertTrue(all("duplicate_destination" in {issue.code for issue in row.errors} for row in plan.rows))
        self.assertEqual([row.destination for row in plan.rows], [self.output / "nested/Click.mp3"] * 2)
        self.assertFalse(self.output.exists())

    def test_case_and_unicode_duplicate_outputs_are_detected(self):
        for names in (("Click.wav", "CLICK.aiff"), ("Café.wav", "Cafe\u0301.aiff")):
            with self.subTest(names=names):
                plan = self.plan([self.file(name) for name in names], mp3_settings())
                self.assertIn("duplicate_destination", self.codes(plan))

    def test_duplicate_selection_and_distinct_subfolders(self):
        plan = self.plan([self.source, self.source])
        self.assertIn("duplicate_source", self.codes(plan))
        self.assertIn("duplicate_destination", self.codes(plan))
        valid = self.plan([self.file("One/Same.wav"), self.file("Two/Same.wav")])
        self.assertTrue(valid.is_valid)
        self.assertNotEqual(valid.rows[0].destination, valid.rows[1].destination)

    def test_file_directory_and_case_aliased_folder_collisions(self):
        plan = self.plan([self.file("Take.aiff"), self.file("Take.wav/Child.wav")])
        self.assertIn("file_directory_collision", self.codes(plan))
        self.assertTrue(all(row.errors for row in plan.rows))
        plan = self.plan([self.file("Folder/A.wav"), self.file("folder/B.wav")])
        self.assertIn("directory_alias_collision", self.codes(plan))

    def test_outside_root_and_missing_sources_do_not_stop_valid_peers(self):
        outside = self.root / "Outside.wav"
        outside.write_bytes(b"Outside the selected input tree")
        plan = self.plan([outside, self.input / "../Outside.wav", self.input / "Missing.wav", self.source])
        self.assertEqual(len(plan.rows), 4)
        self.assertTrue(all(row.errors for row in plan.rows[:3]))
        self.assertEqual(plan.rows[-1].errors, ())
        self.assertEqual(self.probe.call_count, 1)

    def test_symlinked_sources_subfolders_output_roots_and_output_parents_are_blocked(self):
        self.output.mkdir()
        try:
            source_link = self.input / "Linked.wav"
            source_link.symlink_to(self.source)
            source_folder = self.input / "LinkedFolder"
            source_folder.symlink_to(self.input, target_is_directory=True)
            output_link = self.root / "OutputLink"
            output_link.symlink_to(self.output, target_is_directory=True)
            (self.output / "nested").symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertIn("unsafe_source", self.codes(self.plan([source_link])))
        self.assertIn("unsafe_source", self.codes(self.plan([source_folder / self.source.name])))
        self.assertIn("unsafe_output_root", self.codes(self.plan(output=output_link)))
        self.assertIn("unsafe_destination", self.codes(self.plan([self.file("nested/Child.wav")])))

    def test_parent_aliases_above_roots_work_without_changing_relative_structure(self):
        alias = self.root / "Alias"
        real = self.root / "Real"
        real.mkdir()
        source = real / "audio" / "nested" / "Test.wav"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"Source")
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        plan = build_plan(alias / "audio", [alias / "audio/nested/Test.wav"], alias / "output", wav_settings(), probe=self.probe)
        self.assertTrue(plan.is_valid)
        self.assertEqual(plan.rows[0].destination, real / "output/nested/Test.wav")

    def test_empty_selection_invalid_roots_and_new_nested_output(self):
        self.assertIn("no_sources", self.codes(self.plan([])))
        plan = build_plan(self.root / "Missing", [self.source], self.output, wav_settings(), probe=self.probe)
        self.assertIn("invalid_input_root", self.codes(plan))
        occupied = self.root / "Not a directory"
        occupied.write_bytes(b"Existing file")
        self.assertIn("unsafe_output_root", self.codes(self.plan(output=occupied / "output")))
        new_output = self.output / "New subfolder"
        plan = self.plan(output=new_output)
        self.assertTrue(plan.is_valid)
        self.assertFalse(new_output.exists())

    def test_unportable_names_are_blocked_without_automatic_renaming(self):
        for name in ("CON.wav", "Bad:Name.wav", " leading.wav"):
            with self.subTest(name=name):
                plan = self.plan([self.file(name)])
                self.assertIn("unsupported_name", self.codes(plan))
                self.assertEqual(plan.rows[0].destination.name, name)

    def test_probe_failure_ambiguity_and_unknown_metadata_are_structured(self):
        self.probe.side_effect = BackendError("probe_failed", "Corrupt or unsupported audio")
        plan = self.plan()
        self.assertIn("probe_failed", self.codes(plan))
        self.assertIsNone(plan.rows[0].metadata)
        media = self.metadata(self.source)
        self.probe.side_effect = lambda source: replace(media, audio_streams=media.audio_streams * 2)
        plan = self.plan()
        self.assertIn("ambiguous_audio_stream", self.codes(plan))
        self.assertEqual(plan.rows[0].metadata, self.probe(self.source))
        self.probe.side_effect = lambda source: replace(media, audio_streams=(replace(media.audio_streams[0], codec=None, sample_rate=0, channels=None),))
        plan = self.plan()
        self.assertTrue({"unknown_codec", "unknown_sample_rate", "unknown_channels"} <= self.codes(plan))

    def test_unknown_layout_and_depth_reduction_are_visible_warnings(self):
        media = self.metadata(self.source)
        self.probe.side_effect = lambda source: replace(media, audio_streams=(
            replace(media.audio_streams[0], codec="pcm_s24le", bit_depth=24, channel_layout=None),),
            non_audio_stream_indices=(1,))
        plan = self.plan()
        self.assertTrue(plan.is_valid)
        row = plan.rows[0]
        self.assertIsNone(row.target_channel_layout)
        for word in ("layout", "dither", "Non-audio"):
            self.assertTrue(any(word in warning for warning in row.warnings))

    def test_changed_source_and_mismatched_probe_path_are_rejected(self):
        media = self.metadata(self.source)
        def mutate(source):
            source.write_bytes(b"Changed externally while probing")  # Fake external writer.
            return media
        self.probe.side_effect = mutate
        self.assertIn("inspection_failed", self.codes(self.plan()))
        self.probe.side_effect = lambda source: replace(media, path=self.root / "Other.wav")
        self.assertIn("inspection_failed", self.codes(self.plan()))

    def test_optional_capabilities_validate_the_encoder_without_spawning_tools(self):
        info = BackendInfo(ToolInfo("ffmpeg", Path("/ffmpeg"), "7"), ToolInfo("ffprobe", None, None),
                           (EncoderInfo("libmp3lame", "audio", "MP3", "A....D", "mp3"),))
        plan = self.plan(settings=mp3_settings(), capabilities=info)
        self.assertTrue(plan.is_valid)
        self.assertEqual(plan.warnings, ())
        self.assertIn("encoder_unavailable", self.codes(self.plan(capabilities=info)))
        self.assertIn("encoder_unavailable", self.codes(self.plan(capabilities=replace(info, encoder_error="Unavailable"))))
        experimental = replace(info, encoders=(replace(info.encoders[0], flags="A..X.D"),))
        self.assertIn("experimental_encoder", self.codes(self.plan(settings=mp3_settings(), capabilities=experimental)))


@unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is unavailable; all planning unit tests still run")
class PlanningIntegrationTests(unittest.TestCase):
    def test_default_probe_plans_a_real_wav_without_creating_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            root, output = directory / "Audio", directory / "Output"
            root.mkdir()
            source = root / "Stereo sample.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\0" * 4800 * 4)
            original = source.read_bytes()
            plan = build_plan(root, [source], output, wav_settings(sample_rate=44100))
            self.assertTrue(plan.is_valid)
            self.assertEqual(plan.rows[0].metadata.select_audio_stream().sample_rate, 48000)
            self.assertEqual(plan.rows[0].target_channels, 2)
            self.assertIn("resample", {operation.kind for operation in plan.rows[0].operations})
            self.assertFalse(output.exists())
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

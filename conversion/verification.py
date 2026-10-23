"""Read-only postflight against a dry-run plan and explicit project Rules.

This does not execute conversions, publish files, or remove failed outputs.
Metadata/payload checks do not establish perceptual quality or preserved tags.
"""

import math
from pathlib import Path
import stat
from typing import Callable

from batch_rename import FileStamp
from validator import Check, Rules, validate_file
from .backend import FFmpegBackend
from .models import BackendError, ConversionPlan, MediaInfo, VerificationResult


def verify_output(plan: ConversionPlan, row_index: int, rules: Rules, *,
                  output_path: Path | None = None,
                  probe: Callable[[Path], MediaInfo] | None = None,
                  decode: Callable[[Path, int], None] | None = None,
                  duration_tolerance_seconds: float | None = None) -> VerificationResult:
    """Inspect an existing planned output; filesystem/probe failures become checks.

    PCM duration tolerance defaults to one source/target sample plus 1 µs for
    ffprobe rounding. MP3 allows two 1152-sample frames (at least 50 ms) for
    encoder delay/padding. An explicit finite nonnegative tolerance can override.
    Missing layout/duration and unprovable VBR quality yield WARNING, never PASS.
    output_path can select an existing staged file under the output tree with the
    planned basename. The executor must exclusively publish verified output
    and recheck these snapshots. The verifier never creates or deletes a file.
    """
    if type(row_index) is not int or not 0 <= row_index < len(plan.rows):
        raise ValueError("row_index must identify a planned output")
    if not isinstance(rules, Rules):
        raise TypeError("rules must be explicit project Rules")
    if duration_tolerance_seconds is not None and (
        type(duration_tolerance_seconds) not in (int, float)
        or not math.isfinite(duration_tolerance_seconds) or duration_tolerance_seconds < 0
    ):
        raise ValueError("duration tolerance must be finite and nonnegative")
    row = plan.rows[row_index]
    output = Path(output_path).expanduser().absolute() if output_path is not None else row.destination
    checks, media, validation = [], None, None
    output_stamp, source_stamp = None, None
    label = str(output.relative_to(plan.output_root)) if output and output.is_relative_to(plan.output_root) else str(output)

    def record(name, result, expected, actual, message):
        checks.append(Check(label, name, result, "Unavailable" if expected is None else str(expected),
                            "Unavailable" if actual is None else str(actual), message))

    def compare(name, expected, actual, *, optional=False):
        if expected is None or actual is None:
            record(name, "WARNING" if optional else "ERROR", expected, actual,
                   "Metadata is unavailable; this property cannot be verified.")
        else:
            record(name, "PASS" if expected == actual else "ERROR", expected, actual,
                   "Actual output must match the planned target.")

    def finish():
        for name, path, expected in (("source_unchanged", row.source, source_stamp),
                                     ("output_unchanged", output, output_stamp)):
            if expected is None:
                continue
            try:
                info = path.lstat()
                actual = FileStamp.read(info)
                unchanged = stat.S_ISREG(info.st_mode) and actual == expected
                record(name, "PASS" if unchanged else "ERROR", expected, actual,
                       "File identity, size, and modification time must remain unchanged during verification.")
            except OSError as error:
                record(name, "ERROR", expected, None, str(error))
        return VerificationResult(output, tuple(checks), rules, media, validation)

    if not plan.is_valid or row.destination is None or output is None:
        record("invalid_plan", "ERROR", "valid dry-run plan", "blocked plan",
               "Resolve planning errors before verifying outputs.")
        return finish()
    try:
        if output.name != row.destination.name:
            raise ValueError("A staged output must use the planned filename so naming rules are verified correctly.")
        if (not output.is_relative_to(plan.output_root) or output.is_relative_to(plan.input_root)
                or plan.output_root.is_relative_to(plan.input_root) or plan.input_root.is_relative_to(plan.output_root)
                or output.resolve() != output):
            raise ValueError("Output must remain in the separate planned output tree, without symlink components.")
        source_info = row.source.lstat()
        source_stamp = FileStamp.read(source_info)
        if not stat.S_ISREG(source_info.st_mode) or row.source_stamp is None or source_stamp != row.source_stamp:
            record("source_changed", "ERROR", row.source_stamp, source_stamp,
                   "Source no longer matches the plan; do not accept the output.")
            return finish()
        info = output.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise ValueError("Output must be a nonempty regular file, not a symlink or directory.")
        output_stamp = FileStamp.read(info)
        if (source_stamp.device, source_stamp.inode) == (output_stamp.device, output_stamp.inode):
            raise ValueError("Output is a hardlink to the original; it is not an independent output.")
        record("output_file", "PASS", "nonempty independent file", f"{info.st_size} bytes",
               "Output exists without replacing or referring to the original.")
    except (OSError, ValueError, RuntimeError) as error:
        record("unsafe_output", "ERROR", "safe existing output", None, str(error))
        return finish()

    inspect = probe if probe is not None else FFmpegBackend().inspect
    stream = None
    try:
        media = inspect(output)
        if not isinstance(media, MediaInfo) or media.path != output:
            raise ValueError("Probe metadata does not describe the planned output.")
        stream = media.select_audio_stream()
        compare("container", row.settings.container, media.container)
        compare("codec", row.settings.codec, stream.codec)
        compare("sample_rate", row.settings.sample_rate, stream.sample_rate)
        compare("channels", row.target_channels, stream.channels)
        compare("channel_layout", row.target_channel_layout, stream.channel_layout, optional=True)
        if row.settings.bit_depth is not None:
            compare("bit_depth", row.settings.bit_depth, stream.bit_depth)
        if media.non_audio_stream_indices:
            record("unexpected_streams", "ERROR", "audio only", media.non_audio_stream_indices,
                   "Additional non-audio output streams were not specified by the target settings.")
        for warning in media.warnings:
            record("probe_warning", "WARNING", "complete inspection", None, warning)
        source = row.metadata.select_audio_stream(row.stream_index) if row.metadata else None
        expected_duration = source.duration_seconds if source else None
        actual_duration = stream.duration_seconds
        if expected_duration is None and row.metadata:
            expected_duration = row.metadata.duration_seconds
            record("source_duration_basis", "WARNING", "audio stream duration", expected_duration,
                   "Source duration uses a container estimate or is unavailable.")
        if actual_duration is None:
            actual_duration = media.duration_seconds
            record("output_duration_basis", "WARNING", "audio stream duration", actual_duration,
                   "Output duration uses a container estimate or is unavailable.")
        if expected_duration is None or actual_duration is None:
            compare("duration", expected_duration, actual_duration, optional=True)
        elif actual_duration <= 0 or expected_duration <= 0:
            record("duration", "ERROR", "nonempty audio", actual_duration, "Empty audio cannot be verified as a successful output.")
        else:
            tolerance = duration_tolerance_seconds
            if tolerance is None:
                tolerance = (max(0.05, 2304 / row.settings.sample_rate) if row.settings.container == "mp3"
                             else max(1 / row.settings.sample_rate, 1 / source.sample_rate if source and source.sample_rate else 0) + 0.000001)
            difference = abs(actual_duration - expected_duration)
            record("duration", "PASS" if difference <= tolerance else "ERROR", expected_duration, actual_duration,
                   f"Duration difference {difference:g} s must be within {tolerance:g} s.")
    except BackendError as error:
        record(error.code, "ERROR", "readable single audio stream", None,
               f"{error} {error.diagnostic}".strip())
    except (OSError, ValueError, RuntimeError) as error:
        record("inspection_failed", "ERROR", "valid output metadata", None, str(error))

    if row.settings.container == "wav":
        try:
            validation = validate_file(output, plan.output_root, rules)
            complete = next((check for check in validation.checks if check.check == "format"), None)
            record("payload", "PASS" if complete and complete.result == "PASS" else "ERROR",
                   "complete integer PCM WAV frames", complete.actual if complete else None,
                   "The existing validator reads the entire PCM payload; project-rule results are attached separately.")
        except (OSError, ValueError, RuntimeError) as error:
            record("validation_failed", "ERROR", "completed project validation", None, str(error))
    else:
        record("project_validation", "WARNING", "project validation", "not supported for MP3",
               "The existing project validator supports PCM WAV only; its policy checks were not run.")
        record("vbr_quality", "WARNING", row.settings.quality, None,
               "VBR quality cannot be recovered reliably from ffprobe metadata; execution evidence will be needed.")
        if stream is not None:
            try:
                check_decode = decode if decode is not None else FFmpegBackend().check_decode
                check_decode(output, stream.index)
                record("payload", "PASS", "complete audio decode", "decoded to null sink",
                       "FFmpeg decoded the output without creating a media file.")
            except BackendError as error:
                record(error.code, "ERROR", "complete audio decode", None,
                       f"{error} {error.diagnostic}".strip())
            except (OSError, ValueError, RuntimeError) as error:
                record("decode_failed", "ERROR", "complete audio decode", None, str(error))
    return finish()

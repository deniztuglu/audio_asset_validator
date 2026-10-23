"""Read-only conversion previews. No commands, output files, or directories created.

Paths and source stamps are observations, not reservations. The executor must
recheck them, obtain destination/plan approval, and exclusively publish output.
"""

from collections import defaultdict
from dataclasses import replace
import os
from pathlib import Path
import stat
import unicodedata
from typing import Callable, Sequence

from batch_rename import FileStamp
from .backend import FFmpegBackend
from .models import (AudioStream, BackendError, BackendInfo, ConversionPlan, ConversionRow,
                     ConversionSettings, MediaInfo, PlanIssue, PlannedOperation)


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _key(path: Path | str) -> str:
    return unicodedata.normalize("NFC", str(path)).casefold()


def _overlap(first: Path, second: Path) -> bool:
    a, b = tuple(map(_key, first.parts)), tuple(map(_key, second.parts))
    return a[:len(b)] == b or b[:len(a)] == a


def _aliases(path: Path) -> bool:
    return path.parent.is_dir() and any(_key(entry.name) == _key(path.name) and entry.name != path.name
                                       for entry in path.parent.iterdir())


def _destination_error(destination: Path, output_root: Path) -> PlanIssue | None:
    current = output_root
    parts = destination.relative_to(output_root).parts
    for index, part in enumerate(parts):
        current /= part
        if current.is_symlink():
            return PlanIssue("unsafe_destination", "An output component is a symlink.")
        if _aliases(current):
            return PlanIssue("existing_collision", "An output component conflicts with an existing case/Unicode variant.")
        if current.exists():
            if index == len(parts) - 1:
                return PlanIssue("existing_destination", "The destination already exists; choose a different output folder.")
            if not current.is_dir():
                return PlanIssue("unsafe_destination", "An output parent is occupied by a file.")
    return None


def _portable_name(name: str) -> bool:
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return (bool(name.strip()) and name == name.strip() and not name.endswith(".")
            and not any(char in '<>:"/\\|?*' or ord(char) < 32 or ord(char) == 127 for char in name)
            and name.split(".")[0].upper() not in reserved and len(os.fsencode(name)) <= 255)


def _encoding_operations(media: MediaInfo, stream: AudioStream, settings: ConversionSettings):
    operations, warnings, errors = [], list(media.warnings), []
    if not stream.codec:
        errors.append(PlanIssue("unknown_codec", "The source audio codec is unavailable."))
    if not stream.sample_rate:
        errors.append(PlanIssue("unknown_sample_rate", "The source sample rate is unavailable or zero."))
    if not stream.channels:
        errors.append(PlanIssue("unknown_channels", "Cannot preserve an unavailable or zero channel count."))
    if stream.channel_layout is None:
        warnings.append("Source channel layout is unavailable; layout preservation cannot yet be verified.")
    if stream.duration_seconds is None and media.duration_seconds is None:
        warnings.append("Source duration is unavailable.")
    if settings.container == "mp3":
        if stream.channels not in (1, 2):
            errors.append(PlanIssue("unsupported_channels", "MP3 supports mono/stereo here; downmixing is not allowed."))
        if stream.channel_layout is not None and stream.channel_layout not in ("mono", "stereo"):
            errors.append(PlanIssue("unsupported_layout", "The source channel layout cannot be preserved in this MP3 target."))
        warnings.append("MP3 encoding is lossy; VBR quality is explicit and source bytes will not be preserved.")
        operations.append(PlannedOperation("encode", stream.codec, settings.codec))
        operations.append(PlannedOperation("vbr_quality", None, settings.quality))
    elif stream.channels and (stream.channels > 65535 or settings.sample_rate * stream.channels * (settings.bit_depth // 8) > 2**32 - 1):
        errors.append(PlanIssue("unsupported_wav_header", "Target settings exceed WAV channel/byte-rate header limits."))
    if media.container != settings.container:
        operations.append(PlannedOperation("container", media.container, settings.container))
    if stream.sample_rate != settings.sample_rate:
        operations.append(PlannedOperation("resample", stream.sample_rate, settings.sample_rate))
        if settings.container == "wav":
            operations.append(PlannedOperation("resampling_quality", None, settings.resampling_quality))
    if settings.container == "wav":
        if stream.codec != settings.codec:
            operations.append(PlannedOperation("encode", stream.codec, settings.codec))
        if stream.bit_depth != settings.bit_depth:
            operations.append(PlannedOperation("bit_depth", stream.bit_depth, settings.bit_depth))
        if stream.bit_depth is None:
            warnings.append("Source bit depth is unavailable; precision changes cannot be fully assessed.")
        elif stream.bit_depth > settings.bit_depth:
            warnings.append("Bit depth will be reduced; " + ("a dither policy must be chosen before execution."
                            if settings.dither is None else f"dither policy: {settings.dither}."))
        if settings.dither is not None and (settings.dither != "none" or (stream.bit_depth and stream.bit_depth > settings.bit_depth)):
            operations.append(PlannedOperation("dither", None, settings.dither))
    if settings.metadata_policy == "strip":
        operations.append(PlannedOperation("metadata", None, "strip"))
    elif operations:
        warnings.append("FFmpeg copies supported metadata tags; arbitrary RIFF/BWF chunks are not guaranteed to survive re-encoding.")
    if media.non_audio_stream_indices:
        warnings.append("Non-audio streams are present; their handling must be explicitly decided before execution.")
    return operations, warnings, errors


def build_plan(input_root: Path, sources: Sequence[Path], output_root: Path,
               settings: ConversionSettings, *, probe: Callable[[Path], MediaInfo] | None = None,
               capabilities: BackendInfo | None = None) -> ConversionPlan:
    """Return global/per-file issues and intended operations; never execute a plan.

    Settings reject incompatible combinations at construction with ValueError.
    File/path/probe failures are structured PlanIssue values and do not stop peers.
    Pass backend.inspect as probe and optional backend.detect() capabilities.
    Missing capabilities produce a warning, not an invented availability claim.
    """
    if not isinstance(settings, ConversionSettings):
        raise TypeError("settings must be ConversionSettings")
    original_root, output = _absolute(input_root), _absolute(output_root)
    root, errors, warnings = original_root, [], []
    try:
        root = original_root.resolve()
        if not root.is_dir():
            errors.append(PlanIssue("invalid_input_root", "The input root must be an existing directory."))
        if output.is_symlink():
            errors.append(PlanIssue("unsafe_output_root", "The output root must not be a symlink."))
        output = output.resolve()
        if _overlap(root, output):
            errors.append(PlanIssue("overlapping_roots", "Input and output trees must be separate, including case/Unicode aliases."))
        if _aliases(output):
            errors.append(PlanIssue("unsafe_output_root", "The output root conflicts with a case/Unicode variant."))
        for current in (output, *output.parents):
            if current.exists():
                if not current.is_dir():
                    errors.append(PlanIssue("unsafe_output_root", "The output root or its parent is occupied by a file."))
                break
    except (OSError, RuntimeError, ValueError) as failure:
        errors.append(PlanIssue("invalid_roots", str(failure)))
    if not sources:
        errors.append(PlanIssue("no_sources", "Select at least one audio file."))
    if capabilities is None:
        warnings.append("Installed encoder availability has not been checked; check capabilities before execution.")
    elif not capabilities.ffmpeg.available or capabilities.encoder_error:
        errors.append(PlanIssue("encoder_unavailable", "FFmpeg encoder capabilities are unavailable."))
    else:
        encoder = next((item for item in capabilities.audio_encoders if item.name == settings.encoder), None)
        if encoder is None:
            errors.append(PlanIssue("encoder_unavailable", f"The installed FFmpeg does not provide {settings.encoder}."))
        elif encoder.experimental:
            errors.append(PlanIssue("experimental_encoder", "Experimental encoders are not supported by this planner."))
    if errors:
        rows = tuple(ConversionRow(_absolute(source), None, None, settings) for source in sources)
        return ConversionPlan(root, output, settings, rows, tuple(errors), tuple(warnings))

    inspect = probe if probe is not None else FFmpegBackend().inspect
    rows, seen = [], set()
    for item in sources:
        source = _absolute(item)
        destination, media, stamp, stream = None, None, None, None
        issues, notes, operations = [], [], []
        try:
            # Permit parent aliases above the selected root (macOS /var, /tmp),
            # but reject symlinked folders inside the selected tree.
            if source.is_relative_to(original_root):
                current = original_root
                for part in source.relative_to(original_root).parts[:-1]:
                    current /= part
                    if current.is_symlink():
                        raise ValueError("Source subfolders must not be symlinks.")
            source = source.parent.resolve() / source.name
            if not source.is_relative_to(root):
                raise ValueError("Source must be inside the input root.")
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Source must be a regular file; symlinks are not inspected.")
            stamp = FileStamp.read(info)
            relative = source.relative_to(root)
            # Preserve the stem and the existing extension's case when its type
            # already matches. A new container changes only the extension.
            extension = source.suffix if source.suffix.lower() == settings.extension else settings.extension
            destination = output / relative.parent / (source.stem + extension)
            if not all(_portable_name(part) for part in destination.relative_to(output).parts):
                issues.append(PlanIssue("unsupported_name", "An output name is not portable; the planner will not rename it."))
            if source in seen:
                issues.append(PlanIssue("duplicate_source", "The same source was selected more than once."))
            seen.add(source)
            conflict = _destination_error(destination, output)
            if conflict:
                issues.append(conflict)
        except (OSError, ValueError, RuntimeError) as failure:
            issues.append(PlanIssue("unsafe_source", str(failure)))

        if stamp is not None:
            try:
                media = inspect(source)
                if not isinstance(media, MediaInfo) or media.path.parent.resolve() / media.path.name != source:
                    raise ValueError("Probe metadata does not describe the selected source.")
                if source.is_symlink() or FileStamp.read(source.stat()) != stamp:
                    raise ValueError("Source changed while probing; create a new plan.")
                stream = media.select_audio_stream()
                operations, notes, metadata_errors = _encoding_operations(media, stream, settings)
                issues.extend(metadata_errors)
                if destination and source.suffix != destination.suffix:
                    operations.append(PlannedOperation("extension", source.suffix, destination.suffix))
                if not any(operation.kind != "extension" for operation in operations):
                    operations.append(PlannedOperation("copy", str(source), str(destination)))
            except BackendError as failure:
                issues.append(PlanIssue(failure.code, str(failure)))
            except (OSError, ValueError, RuntimeError) as failure:
                issues.append(PlanIssue("inspection_failed", str(failure)))
        rows.append(ConversionRow(source, destination, media, settings, stamp,
                                  stream.index if stream else None, stream.channels if stream else None,
                                  stream.channel_layout if stream else None, tuple(operations), tuple(issues), tuple(notes)))

    # Compare the whole relative path, including case/NFC aliases of parent folders.
    targets, directories = defaultdict(list), defaultdict(lambda: defaultdict(list))
    for index, row in enumerate(rows):
        if row.destination is None:
            continue
        relative = row.destination.relative_to(output)
        targets[_key(relative)].append(index)
        for parent in relative.parents:
            if parent != Path("."):
                directories[_key(parent)][str(parent)].append(index)

    def block(indices, code, message):
        for index in set(indices):
            rows[index] = replace(rows[index], errors=(*rows[index].errors, PlanIssue(code, message)))

    for indices in targets.values():
        if len(indices) > 1:
            block(indices, "duplicate_destination", "Multiple sources would use the same output name; no automatic renaming is performed.")
    for key, spellings in directories.items():
        members = [index for group in spellings.values() for index in group]
        if key in targets:
            block([*members, *targets[key]], "file_directory_collision", "A planned output file is also needed as an output directory.")
        if len(spellings) > 1:
            block(members, "directory_alias_collision", "Distinct source folders would collide after case/Unicode normalization.")
    return ConversionPlan(root, output, settings, tuple(rows), warnings=tuple(warnings))

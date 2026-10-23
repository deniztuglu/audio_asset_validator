"""Headless PCM WAV conversion: private staging, postflight, exclusive publication.

Calling execute_plan explicitly approves that immutable plan's destination/settings.
Completed outputs stay on cancellation/failure. Only our private staging is removed.
"""

from dataclasses import replace
import math
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable

from batch_rename import FileStamp
from validator import Rules, validate_file
from .backend import FFmpegBackend
from .models import BackendError, ConversionOutcome, ConvertedOutput, ConversionPlan, PlanIssue
from .planning import build_plan, _aliases, _destination_error, _key
from .verification import verify_output


OUTPUT_RATES = frozenset((22050, 32000, 44100, 48000, 96000))
SOURCE_CODECS = {"pcm_u8": 8, "pcm_s16le": 16, "pcm_s24le": 24, "pcm_s32le": 32}


class _Cancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel.is_set():
        raise _Cancelled


def _stop(process):
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        process.wait(timeout=5)
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_ffmpeg(command: list[str], cancel: threading.Event, timeout: float) -> None:
    """Drain stderr to a disposable file; cancel/timeout always reap the child."""
    _check_cancel(cancel)
    with tempfile.TemporaryFile() as diagnostic:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=diagnostic, shell=False)
        except OSError as error:
            raise BackendError("executable_failed", str(error), tool="ffmpeg") from error
        deadline = time.monotonic() + timeout
        try:
            while True:
                _check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise BackendError("timeout", f"Conversion exceeded {timeout:g} seconds.", tool="ffmpeg")
                try:
                    returncode = process.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
            _check_cancel(cancel)
            if returncode:
                diagnostic.seek(0, os.SEEK_END)
                diagnostic.seek(max(0, diagnostic.tell() - 4000))
                message = diagnostic.read().decode("utf-8", "replace")
                raise BackendError("conversion_failed", f"FFmpeg exited with status {returncode}.",
                                   tool="ffmpeg", diagnostic=message, returncode=returncode)
        finally:
            _stop(process)


def _assert_source(row):
    info = row.source.lstat()
    if not stat.S_ISREG(info.st_mode) or FileStamp.read(info) != row.source_stamp:
        raise BackendError("source_changed", f"Source changed since preview: {row.source}")


def _check_supported(plan, rules, cancel):
    settings = plan.settings
    if settings.container != "wav" or settings.codec not in ("pcm_s16le", "pcm_s24le"):
        raise BackendError("unsupported_target", "Execution currently supports 16/24-bit integer PCM WAV only.")
    if settings.sample_rate not in OUTPUT_RATES:
        raise BackendError("unsupported_rate", "Output sample rate must be 22050, 32000, 44100, 48000, or 96000 Hz.")
    for row in plan.rows:
        _check_cancel(cancel)
        if row.metadata is None:
            raise BackendError("missing_metadata", "Source metadata must be present in the plan.")
        stream = row.metadata.select_audio_stream(row.stream_index)
        if (row.metadata.container != "wav" or row.source.suffix.lower() != ".wav"
                or stream.codec not in SOURCE_CODECS or stream.bit_depth != SOURCE_CODECS[stream.codec]):
            raise BackendError("unsupported_source", "Sources must be uncompressed 8/16/24/32-bit integer PCM WAV.")
        if stream.channels not in (1, 2) or row.target_channels != stream.channels:
            raise BackendError("unsupported_channels", "Only mono/stereo with unchanged channel count is supported.")
        if stream.channel_layout not in (None, "mono" if stream.channels == 1 else "stereo"):
            raise BackendError("unsupported_layout", "Nonstandard channel layouts cannot be preserved by this executor.")
        if row.metadata.non_audio_stream_indices:
            raise BackendError("unsupported_streams", "Non-audio streams require an explicit handling policy.")
        if stream.bit_depth > settings.bit_depth and settings.dither is None:
            raise BackendError("dither_policy_required", "Choose dither='none' or 'triangular' in the plan before reducing bit depth.")
        _assert_source(row)
        source_rules = Rules(naming_pattern=r".+", sample_rate=stream.sample_rate,
                             bit_depths=(stream.bit_depth,), channels=(stream.channels,), min_duration_seconds=0)
        source_result = validate_file(row.source, plan.input_root, source_rules)
        _check_cancel(cancel)
        if source_result.status == "ERROR":
            raise BackendError("invalid_source", f"Source has unsupported, empty, or incomplete PCM data: {row.source}")
        _assert_source(row)
        if (settings.sample_rate != rules.sample_rate or settings.bit_depth not in rules.bit_depths
                or stream.channels not in rules.channels):
            raise BackendError("policy_conflict", "The selected output settings conflict with the explicit project rules.")


def _command(executable, row, staged):
    settings = row.settings
    options = [f"{settings.sample_rate}", "resampler=swr",
               f"osf={'s16' if settings.bit_depth == 16 else 's32'}",
               f"output_sample_bits={settings.bit_depth}", f"dither_method={settings.dither or 'none'}"]
    if settings.resampling_quality == "fast":
        options.extend(("filter_size=16", "phase_shift=8", "cutoff=0.90"))
    elif settings.resampling_quality == "high":
        options.extend(("filter_size=64", "phase_shift=10", "cutoff=0.97"))
    return [str(executable), "-hide_banner", "-v", "error", "-nostdin", "-n", "-xerror",
            "-err_detect", "explode", "-protocol_whitelist", "file", "-i", str(row.source),
            "-map", f"0:{row.stream_index}", "-vn", "-sn", "-dn", "-c:a", settings.encoder,
            "-ar", str(settings.sample_rate), "-ac", str(row.target_channels),
            "-af", "aresample=" + ":".join(options),
            "-map_metadata", "0" if settings.metadata_policy == "copy" else "-1",
            "-f", "wav", str(staged)]


def _copy(source, staged, cancel):
    with source.open("rb") as original, staged.open("xb") as output:
        while block := original.read(65536):
            _check_cancel(cancel)
            output.write(block)


def _safe_parent(plan, destination):
    if (plan.output_root.resolve() != plan.output_root or destination.parent.resolve() != destination.parent
            or _aliases(plan.output_root)):
        raise BackendError("unsafe_destination", "The output tree changed to a symlink; preview again.")
    issue = _destination_error(destination, plan.output_root)
    if issue:
        raise BackendError(issue.code, issue.message)


def _publish(plan, row, staged, expected):
    """Hard-link verified bytes atomically, never replace a destination.

    On macOS/Linux directory descriptors anchor publication across path swaps.
    Filesystems without hard links fail closed; there is no overwrite fallback.
    """
    _safe_parent(plan, row.destination)
    _assert_source(row)
    if staged.is_symlink() or FileStamp.read(staged.stat()) != expected:
        raise BackendError("output_changed", "Staged output changed after verification.")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors = []
    linked = False
    try:
        if os.link in os.supports_dir_fd:
            parent_fd = os.open(row.destination.parent, directory_flags)
            descriptors.append(parent_fd)
            stage_fd = os.open(staged.parent, directory_flags)
            descriptors.append(stage_fd)
            _safe_parent(plan, row.destination)
            if not os.path.samestat(os.fstat(parent_fd), row.destination.parent.stat()):
                raise BackendError("unsafe_destination", "Output parent changed before publication.")
            if (not os.path.samestat(os.fstat(stage_fd), staged.parent.stat())
                    or FileStamp.read(os.stat(staged.name, dir_fd=stage_fd, follow_symlinks=False)) != expected):
                raise BackendError("output_changed", "Staged output changed before publication.")
            os.link(staged.name, row.destination.name, src_dir_fd=stage_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        else:
            os.link(staged, row.destination, follow_symlinks=False)
        linked = True
        _safe_parent_after_publish(plan, row.destination, expected)
        _assert_source(row)
    except Exception as error:
        if linked:
            # Remove only the file we just linked, never a replacement by someone else.
            try:
                if descriptors:
                    info = os.stat(row.destination.name, dir_fd=descriptors[0], follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == (expected.device, expected.inode):
                        os.unlink(row.destination.name, dir_fd=descriptors[0])
                elif (info := row.destination.lstat()).st_ino == expected.inode and info.st_dev == expected.device:
                    row.destination.unlink()
            except FileNotFoundError:
                pass
        if isinstance(error, FileExistsError):
            raise BackendError("existing_destination", "Another file appeared at the destination; it was left unchanged.") from error
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _safe_parent_after_publish(plan, destination, expected):
    if plan.output_root.resolve() != plan.output_root or destination.resolve() != destination:
        raise BackendError("unsafe_destination", "The output tree changed while publishing.")
    actual = FileStamp.read(destination.lstat())
    if actual != expected:
        raise BackendError("output_changed", "Published output does not match verified staged bytes.")
    current = destination.parent
    while current.is_relative_to(plan.output_root):
        if _aliases(current):
            raise BackendError("existing_collision", "A conflicting output folder appeared while publishing.")
        if current == plan.output_root:
            break
        current = current.parent
    if any(entry.name != destination.name and _key(entry.name) == _key(destination.name)
           for entry in destination.parent.iterdir()):
        raise BackendError("existing_collision", "A conflicting output name appeared while publishing.")


def _published_result(result, destination, output_root):
    relative = destination.relative_to(output_root).as_posix()
    validation = result.validation
    if validation:
        validation = replace(validation, file=relative, checks=tuple(replace(check, file=relative) for check in validation.checks))
    return replace(result, output=destination, metadata=replace(result.metadata, path=destination),
                   checks=tuple(replace(check, file=relative) for check in result.checks), validation=validation)


def execute_plan(plan: ConversionPlan, rules: Rules, *, cancel: threading.Event | None = None,
                 backend: FFmpegBackend | None = None, timeout_seconds: float = 300,
                 on_converted: Callable[[ConvertedOutput], None] | None = None) -> ConversionOutcome:
    """Execute an explicitly approved plan. No GUI or implicit execution is involved.

    Only verified required properties and complete payloads can be published.
    Optional WAV layout metadata and project warnings remain visible in results.
    Cancellation is checked during FFmpeg/copy work and before publication; an
    in-progress probe/validation finishes first. Completed outputs are retained.
    """
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    if not isinstance(rules, Rules):
        raise TypeError("rules must be explicit project Rules")
    cancel = cancel if cancel is not None else threading.Event()
    backend = backend if backend is not None else FFmpegBackend()
    outputs, errors, failed_verification = [], [], None
    try:
        _check_cancel(cancel)
        if not plan.is_valid:
            raise BackendError("invalid_plan", "Resolve all planning errors before execution.")
        capabilities = backend.detect()
        if not capabilities.ffmpeg.available or not capabilities.ffprobe.available:
            raise BackendError("missing_executable", "Working FFmpeg and ffprobe are required for execution.")
        def inspect(source):
            _check_cancel(cancel)
            metadata = backend.inspect(source)
            _check_cancel(cancel)
            return metadata
        fresh = build_plan(plan.input_root, [row.source for row in plan.rows], plan.output_root,
                           plan.settings, probe=inspect, capabilities=capabilities)
        if (not fresh.is_valid or fresh.rows != plan.rows or fresh.input_root != plan.input_root
                or fresh.output_root != plan.output_root):
            raise BackendError("stale_plan", "Sources, settings, or destinations changed; create a fresh preview.")
        _check_supported(plan, rules, cancel)
        _check_cancel(cancel)
        for index, row in enumerate(plan.rows):
            _check_cancel(cancel)
            _safe_parent(plan, row.destination)
            row.destination.parent.mkdir(parents=True, exist_ok=True)
            _safe_parent(plan, row.destination)
            _assert_source(row)
            with tempfile.TemporaryDirectory(prefix=".audio-preflight-", dir=row.destination.parent) as temporary:
                staged = Path(temporary) / row.destination.name
                command = []
                if all(operation.kind in ("copy", "extension") for operation in row.operations):
                    _copy(row.source, staged, cancel)
                else:
                    command = _command(capabilities.ffmpeg.path, row, staged)
                    _run_ffmpeg(command, cancel, timeout_seconds)
                _check_cancel(cancel)
                _assert_source(row)
                with staged.open("rb") as data:
                    os.fsync(data.fileno())
                expected = FileStamp.read(staged.stat())
                verification = verify_output(plan, index, rules, output_path=staged, probe=backend.inspect)
                required = {"container", "codec", "sample_rate", "bit_depth", "channels", "duration",
                            "payload", "source_unchanged", "output_unchanged"}
                passed = {check.check for check in verification.checks if check.result == "PASS"}
                incomplete = any(check.result == "WARNING" and check.check != "channel_layout" for check in verification.checks)
                if verification.status == "ERROR" or not required <= passed or incomplete:
                    failed_verification = verification
                    raise BackendError("verification_failed", "Staged output failed postflight; it was not published.")
                _check_cancel(cancel)
                _publish(plan, row, staged, expected)
                completed = ConvertedOutput(row.source, row.destination,
                                            _published_result(verification, row.destination, plan.output_root), tuple(command))
                outputs.append(completed)
            if on_converted:
                on_converted(completed)
        return ConversionOutcome(tuple(outputs), cancelled=cancel.is_set())
    except _Cancelled:
        return ConversionOutcome(tuple(outputs), cancelled=True)
    except Exception as error:
        code = error.code if isinstance(error, BackendError) else "execution_failed"
        diagnostic = error.diagnostic if isinstance(error, BackendError) else ""
        errors.append(PlanIssue(code, f"{error} {diagnostic}".strip()))
        return ConversionOutcome(tuple(outputs), errors=tuple(errors), failed_verification=failed_verification)

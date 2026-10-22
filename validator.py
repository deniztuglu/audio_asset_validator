#!/usr/bin/env python3
"""Read-only PCM WAV preflight checks using only the Python standard library."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, fields
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Sequence
import wave


DEFAULT_NAMING_PATTERN = r"SFX_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_[0-9]{2}\.[wW][aA][vV]"


@dataclass(frozen=True)
class Rules:
    naming_pattern: str = DEFAULT_NAMING_PATTERN
    sample_rate: int = 48000
    bit_depths: tuple[int, ...] = (16, 24)
    channels: tuple[int, ...] = (1, 2)
    min_duration_seconds: float = 0.05

    @classmethod
    def load(cls, path: Path | None) -> Rules:
        if path is None:
            return cls()
        with path.open(encoding="utf-8") as source:
            values = json.load(source)
        if not isinstance(values, dict):
            raise ValueError("configuration must be a JSON object")
        unknown = set(values) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"unknown configuration keys: {', '.join(sorted(unknown))}")
        if "naming_pattern" in values:
            pattern = values["naming_pattern"]
            if not isinstance(pattern, str) or not pattern:
                raise ValueError("naming_pattern must be a nonempty regular expression")
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"invalid naming_pattern: {error}") from error
        if "sample_rate" in values:
            if type(values["sample_rate"]) is not int or values["sample_rate"] <= 0:
                raise ValueError("sample_rate must be a positive integer")
        for key in ("bit_depths", "channels"):
            if key in values:
                items = values[key]
                if not isinstance(items, list) or not items or any(
                    type(item) is not int or item <= 0 for item in items
                ):
                    raise ValueError(f"{key} must be a nonempty array of positive integers")
                values[key] = tuple(items)
        if "min_duration_seconds" in values:
            duration = values["min_duration_seconds"]
            if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                raise ValueError("min_duration_seconds must be a finite, nonnegative number")
        return cls(**values)


@dataclass(frozen=True)
class Check:
    file: str
    check: str
    result: str
    expected: str
    actual: str
    message: str


@dataclass(frozen=True)
class AssetResult:
    file: str
    checks: tuple[Check, ...]

    @property
    def status(self) -> str:
        if any(check.result == "ERROR" for check in self.checks):
            return "ERROR"
        if any(check.result == "WARNING" for check in self.checks):
            return "WARNING"
        return "PASS"


def scan_files(folder: Path, excluded: Sequence[Path] = ()) -> list[Path]:
    """Scan deterministically; report traversal failures rather than miss assets silently."""
    excluded_paths = {path.resolve() for path in excluded}
    found = []

    def on_error(error: OSError) -> None:
        raise error

    for directory, dirs, names in os.walk(folder, followlinks=False, onerror=on_error):
        dirs.sort()
        # Symlinked directories are not traversed; file symlinks are not opened.
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in sorted(names):
            path = Path(directory) / name
            if path.resolve() not in excluded_paths:
                found.append(path)
    return found


def validate_file(path: Path, root: Path, rules: Rules) -> AssetResult:
    name = path.relative_to(root).as_posix()
    checks: list[Check] = []

    def record(check: str, passed: bool, expected: object, actual: object,
               message: str, severity: str = "ERROR") -> None:
        checks.append(Check(name, check, "PASS" if passed else severity,
                            str(expected), str(actual), message))

    record("naming", re.fullmatch(rules.naming_pattern, path.name) is not None,
           rules.naming_pattern, path.name, "Filename must match the project naming pattern.")

    if path.is_symlink() or not path.is_file():
        record("format", False, "regular PCM WAV file", "symlink or non-regular file",
               "Only regular files are inspected; symlinks are not opened.")
        return AssetResult(name, tuple(checks))
    if path.suffix.lower() != ".wav":
        record("format", False, "uncompressed PCM .wav", path.suffix or "no extension",
               "Unsupported file type; export an uncompressed PCM WAV.")
        return AssetResult(name, tuple(checks))

    try:
        with wave.open(str(path), "rb") as audio:
            rate = audio.getframerate()
            channels = audio.getnchannels()
            bit_depth = audio.getsampwidth() * 8
            frames = audio.getnframes()
            frame_size = channels * audio.getsampwidth()
            if rate <= 0 or frame_size <= 0:
                raise wave.Error("invalid sample rate or frame size")
            # Read in bounded blocks to catch truncated payloads, without retaining audio.
            actual_bytes = 0
            while True:
                block = audio.readframes(max(1, 65536 // frame_size))
                if not block:
                    break
                actual_bytes += len(block)
            if actual_bytes != frames * frame_size:
                raise wave.Error(
                    f"incomplete audio data: expected {frames * frame_size} bytes, "
                    f"read {actual_bytes} (frame size {frame_size} bytes)"
                )
    except (wave.Error, OSError, EOFError, ValueError, OverflowError, RuntimeError) as error:
        record("format", False, "readable uncompressed PCM WAV with complete frames",
               str(error) or type(error).__name__,
               "Cannot read complete PCM WAV data; the file may be unsupported or corrupted.")
        return AssetResult(name, tuple(checks))

    record("format", True, "uncompressed PCM WAV", "PCM WAV", "Readable PCM WAV data.")
    record("sample_rate", rate == rules.sample_rate, f"{rules.sample_rate} Hz", f"{rate} Hz",
           "Sample rate must match the project setting.")
    record("bit_depth", bit_depth in rules.bit_depths,
           ", ".join(f"{depth}-bit" for depth in rules.bit_depths), f"{bit_depth}-bit",
           "Bit depth must be allowed by the project.")
    record("channels", channels in rules.channels, ", ".join(map(str, rules.channels)),
           channels, "Channel count must be allowed by the project.")
    duration = frames / rate
    if frames == 0:
        record("duration", False, "nonempty audio", "0 frames / 0 seconds",
               "The WAV contains no audio frames.")
    else:
        record("duration", duration >= rules.min_duration_seconds,
               f">= {rules.min_duration_seconds:g} seconds", f"{duration:.6f} seconds",
               "Very short audio may be intentional; review before import.", "WARNING")
    return AssetResult(name, tuple(checks))


def write_csv(path: Path, results: Sequence[AssetResult]) -> None:
    """One row per executed check, including passing checks."""
    with path.open("x", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=[field.name for field in fields(Check)])
        writer.writeheader()
        for asset in results:
            for check in asset.checks:
                writer.writerow({field.name: getattr(check, field.name) for field in fields(Check)})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check audio assets before import. Source files are never modified."
    )
    parser.add_argument("folder", type=Path, help="asset folder to scan recursively")
    parser.add_argument("--config", type=Path, help="JSON project rules (defaults if omitted)")
    parser.add_argument("--csv", type=Path, metavar="PATH", help="write a CSV report")
    parser.add_argument("--strict", action="store_true", help="also exit 1 for warnings")
    args = parser.parse_args(argv)

    try:
        root = args.folder.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"asset folder does not exist or is not a directory: {root}")
        rules = Rules.load(args.config)
        report = args.csv.expanduser().absolute() if args.csv else None
        # Never let the report overwrite an audio asset, config, or an arbitrary existing file.
        if report:
            if report.suffix.lower() != ".csv":
                raise ValueError("report path must have a .csv extension")
            if report.is_symlink() or report.exists():
                raise ValueError(f"report already exists; choose a new path: {report}")
            if not report.parent.is_dir():
                raise ValueError(f"report directory does not exist: {report.parent}")
        paths = scan_files(root, [report] if report else [])
        if not paths:
            raise ValueError("no files found in the asset folder")
        results = [validate_file(path, root, rules) for path in paths]
        if report:
            write_csv(report, results)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Setup/report error: {error}", file=sys.stderr)
        return 2

    for asset in results:
        print(f"[{asset.status}] {asset.file}")
        for check in asset.checks:
            if check.result != "PASS":
                print(f"  {check.result} {check.check}: {check.message}")
                print(f"    Expected: {check.expected}; actual: {check.actual}")
    counts = {status: sum(asset.status == status for asset in results)
              for status in ("PASS", "WARNING", "ERROR")}
    print(f"\n{len(results)} files: {counts['PASS']} passed, "
          f"{counts['WARNING']} with warnings, {counts['ERROR']} with errors.")
    if report:
        print(f"CSV report: {report}")
    return int(counts["ERROR"] > 0 or (args.strict and counts["WARNING"] > 0))


if __name__ == "__main__":
    raise SystemExit(main())

"""Present the engine's existing check data without rereading or guessing metadata."""

from collections import Counter
from pathlib import Path
import re
from typing import Sequence

from validator import AssetResult


METADATA = (("format", "Format"), ("sample_rate", "Sample rate"),
            ("bit_depth", "Bit depth"), ("channels", "Channels"), ("duration", "Duration"))
FIX_HINTS = {
    "naming": "Rename or re-export in your asset workflow to match the naming rule.",
    "format": "Re-export from the source as an uncompressed integer PCM WAV; check that the export completed.",
    "sample_rate": "Re-export at the expected sample rate, or adjust the project rule if this rate is intentional.",
    "bit_depth": "Re-export at an allowed integer PCM bit depth, or adjust the project rule.",
    "channels": "Choose an allowed channel layout in your source/export workflow, or adjust the project rule.",
    "duration": "Check the export range. Empty exports need correction; short sounds may be intentional.",
}


def display_value(key: str, value: str) -> str:
    """Format existing check strings for the GUI; engine/CSV values stay intact."""
    if key == "sample_rate" and re.fullmatch(r"[0-9]+ Hz", value):
        whole, fraction = divmod(int(value[:-3]), 1000)
        number = f"{whole}.{fraction:03d}".rstrip("0").rstrip(".") if fraction else str(whole)
        return f"{number} kHz"
    if key == "channels":
        parts = value.split(", ")
        if all(part.isdigit() for part in parts):
            return " / ".join({"1": "Mono (1)", "2": "Stereo (2)"}.get(part, f"{part} channels") for part in parts)
    if key == "duration":
        if value == "0 frames / 0 seconds":
            return "Empty (0 frames)"
        match = re.fullmatch(r"(>= )?([0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?) seconds", value)
        if match:
            seconds = float(match[2])
            number = f"{seconds:.3f}" if seconds == 0 or seconds >= 0.001 else f"{seconds:g}"
            return f"{'at least ' if match[1] else ''}{number} s"
    return value


def issue_summary(asset: AssetResult) -> str:
    issues = []
    for check in asset.checks:
        if check.result == "PASS":
            continue
        if check.check == "naming":
            issues.append("Filename does not match naming rule")
        elif check.check == "format":
            issues.append("Unsupported or unreadable file; expected PCM WAV")
        else:
            label = dict(METADATA).get(check.check, check.check)
            issues.append(f"{label}: {display_value(check.check, check.actual)}; expected {display_value(check.check, check.expected)}")
    return " · ".join(issues) or "All checks passed"


def metadata_values(asset: AssetResult) -> tuple[str, ...]:
    checks = {check.check: check for check in asset.checks}
    return tuple(("Unsupported / unreadable" if key == "format" and checks[key].result == "ERROR"
                  else display_value(key, checks[key].actual)) if key in checks else "Not inspected"
                 for key, _ in METADATA)


def selection_overview(assets: Sequence[AssetResult], profile_name: str) -> str:
    if not assets:
        return "Select one or more assets to inspect their results."
    counts = Counter(asset.status for asset in assets)
    lines = [f"{len(assets)} selected", f"Scan profile: {profile_name}",
             f"{counts['PASS']} passed | {counts['WARNING']} with warnings | {counts['ERROR']} with errors", ""]
    for asset in assets:
        lines.append(f"{asset.status}  {asset.file}")
        lines.append("Needs attention: " + issue_summary(asset) if asset.status != "PASS" else "All six validation checks passed.")
        lines.append("")
    return "\n".join(lines)


def metadata_details(assets: Sequence[AssetResult]) -> str:
    lines = []
    for asset in assets:
        lines.extend((asset.file, f"Extension: {Path(asset.file).suffix or '(none)'}"))
        checks = {check.check: check for check in asset.checks}
        for (key, label), value in zip(METADATA, metadata_values(asset)):
            lines.append(f"{label}: {value}")
            if key in checks:
                lines.append(f"  {checks[key].result} | Expected: {display_value(key, checks[key].expected)}")
        lines.append("")
    return "\n".join(lines) or "No assets selected."


def check_details(assets: Sequence[AssetResult]) -> str:
    lines = []
    for asset in assets:
        lines.extend((f"{asset.status}  {asset.file}", ""))
        for check in asset.checks:
            lines.extend((f"{check.result} | {check.check.replace('_', ' ')}",
                          f"Expected: {check.expected}", f"Actual: {check.actual}"))
            if check.result != "PASS":
                lines.extend((check.message, f"Next step: {FIX_HINTS.get(check.check, 'Review this asset.')}"))
            lines.append("")
        if len(asset.checks) < 6:
            lines.append("Metadata checks were not run because the file could not be inspected as PCM WAV.")
        lines.append("")
    return "\n".join(lines) or "No assets selected."

"""Read-only FFmpeg/ffprobe integration using only the Python standard library.

Unknown fields are None; numeric zero is preserved. A reported codec or encoder
is not a claim of successful decoding, conversion, or compliance with Rules.
"""

import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from typing import Callable

from .models import AudioStream, BackendError, BackendInfo, EncoderInfo, MediaInfo, ToolInfo


_ENTRIES = ("format=format_name,duration:"
            "stream=index,codec_type,codec_name,sample_rate,sample_fmt,bits_per_sample,"
            "bits_per_raw_sample,channels,channel_layout,duration")
_DIAGNOSTIC_LIMIT = 4000


def _text(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("text metadata must be a string")
    value = value.strip()
    return None if value.lower() in ("", "n/a", "unknown") else value


def _integer(value) -> int | None:
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "n/a", "unknown")):
        return None
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise ValueError("integer metadata must be a nonnegative integer or unavailable")


def _duration(value) -> float | None:
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "n/a", "unknown")):
        return None
    if type(value) not in (str, int, float):
        raise ValueError("duration must be numeric or unavailable")
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration must be finite and nonnegative")
    return duration


def _media_info(path: Path, payload: object, diagnostic: str) -> MediaInfo:
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    if "error" in payload:
        raise BackendError("probe_failed", f"ffprobe could not inspect {path.name}.", diagnostic=diagnostic or str(payload["error"])[:_DIAGNOSTIC_LIMIT])
    streams = payload.get("streams", [])
    format_info = payload.get("format", {})
    if not isinstance(streams, list) or not isinstance(format_info, dict):
        raise ValueError("invalid streams or format object")
    audio, other, seen, warnings = [], [], set(), []
    for stream in streams:
        if not isinstance(stream, dict):
            raise ValueError("invalid stream object")
        index = _integer(stream.get("index"))
        if index is None or index in seen:
            raise ValueError("missing or duplicate absolute stream index")
        seen.add(index)
        if stream.get("codec_type") != "audio":
            other.append(index)
            continue
        raw_bits = _integer(stream.get("bits_per_raw_sample"))
        coded_bits = _integer(stream.get("bits_per_sample"))
        bit_depth = next((value for value in (raw_bits, coded_bits) if value is not None and value > 0), None)
        codec = _text(stream.get("codec_name"))
        if codec is None:
            warnings.append(f"Audio stream {index} has no recognized codec; decoding support is unknown.")
        audio.append(AudioStream(index, codec, _integer(stream.get("sample_rate")),
                                 _text(stream.get("sample_fmt")), bit_depth,
                                 _integer(stream.get("channels")), _text(stream.get("channel_layout")),
                                 _duration(stream.get("duration")), coded_bits, raw_bits))
    if not audio:
        raise BackendError("no_audio_streams", f"No audio streams were found in {path.name}.", diagnostic=diagnostic)
    if diagnostic:
        warnings.append(diagnostic)
    return MediaInfo(path, _text(format_info.get("format_name")), _duration(format_info.get("duration")),
                     tuple(audio), tuple(other), tuple(warnings))


def _encoders(text: str) -> tuple[EncoderInfo, ...]:
    if "Encoders:" not in text:
        raise ValueError("unrecognized FFmpeg encoder listing")
    encoders = []
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([VAS][A-Z.]{5})\s+(\S+)\s+(.+)", line)
        if not match or match[2] == "=":
            continue  # Ignore the flags legend.
        codec = re.search(r"\(codec ([^()\s]+)\)", match[3])
        encoders.append(EncoderInfo(match[2], {"A": "audio", "V": "video", "S": "subtitle"}[match[1][0]],
                                    match[3], match[1], codec[1] if codec else None))
    return tuple(encoders)


class FFmpegBackend:
    def __init__(self, ffmpeg: str | Path = "ffmpeg", ffprobe: str | Path = "ffprobe", *,
                 timeout: float = 15, runner: Callable | None = None, finder: Callable | None = None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self.ffmpeg, self.ffprobe, self.timeout = str(ffmpeg), str(ffprobe), timeout
        self._runner = runner or subprocess.run
        self._finder = finder or shutil.which

    def _locate(self, tool: str) -> Path:
        found = self._finder(getattr(self, tool))
        if not found:
            raise BackendError("missing_executable", f"{tool} was not found. Install it or supply its executable path.", tool=tool)
        return Path(found).absolute()

    def _run(self, tool: str, path: Path, arguments: list[str]):
        try:
            result = self._runner([str(path), *arguments], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  encoding="utf-8", errors="replace", timeout=self.timeout,
                                  check=False, shell=False)
        except subprocess.TimeoutExpired as error:
            raise BackendError("timeout", f"{tool} exceeded its {self.timeout:g}-second timeout.", tool=tool) from error
        except OSError as error:
            raise BackendError("executable_failed", f"Could not run {tool}: {error}", tool=tool) from error
        if result.returncode:
            raise BackendError("probe_failed" if tool == "ffprobe" and "-version" not in arguments else "command_failed",
                               f"{tool} exited with status {result.returncode}.", tool=tool,
                               diagnostic=(result.stderr or result.stdout)[-_DIAGNOSTIC_LIMIT:], returncode=result.returncode)
        return result

    def _tool_info(self, tool: str) -> ToolInfo:
        path = None
        try:
            path = self._locate(tool)
            result = self._run(tool, path, ["-version"])
            match = re.search(rf"^{tool} version (\S+)", result.stdout or result.stderr, re.MULTILINE)
            if not match:
                raise BackendError("invalid_response", f"Unrecognized {tool} version output.", tool=tool)
            return ToolInfo(tool, path, match[1])
        except BackendError as error:
            return ToolInfo(tool, path, None, f"{error.code}: {error}")

    def detect(self) -> BackendInfo:
        """Report both tools independently; a missing FFmpeg does not block probing."""
        ffmpeg, ffprobe = self._tool_info("ffmpeg"), self._tool_info("ffprobe")
        encoders, error = (), None
        if ffmpeg.available:
            try:
                result = self._run("ffmpeg", ffmpeg.path, ["-hide_banner", "-encoders"])
                encoders = _encoders(result.stdout or result.stderr)
            except (BackendError, ValueError) as failure:
                error = str(failure)
        return BackendInfo(ffmpeg, ffprobe, encoders, error)

    def inspect(self, path: str | Path) -> MediaInfo:
        """Probe a local regular file, reporting all audio tracks without choosing one.

        This reads metadata only: successful probing is not full payload validation.
        Input codec support is not inferred from the installed *encoder* list.
        """
        executable = self._locate("ffprobe")
        source = Path(path).expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            raise BackendError("invalid_input", "Choose an existing regular audio file; symlinks are not inspected.")
        result = self._run("ffprobe", executable,
                           ["-v", "error", "-protocol_whitelist", "file", "-print_format", "json",
                            "-show_error", "-show_entries", _ENTRIES, "-i", str(source)])
        try:
            payload = json.loads(result.stdout)
            return _media_info(source, payload, result.stderr[-_DIAGNOSTIC_LIMIT:].strip())
        except (ValueError, OverflowError) as error:
            raise BackendError("invalid_response", f"Invalid ffprobe metadata: {error}",
                               diagnostic=result.stdout[:_DIAGNOSTIC_LIMIT]) from error

    def check_decode(self, path: str | Path, stream_index: int) -> None:
        """Fully decode one audio stream to FFmpeg's null sink. No media is saved."""
        if type(stream_index) is not int or stream_index < 0:
            raise ValueError("stream_index must be a nonnegative absolute stream index")
        executable = self._locate("ffmpeg")
        source = Path(path).expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            raise BackendError("invalid_input", "Choose an existing regular output file.", tool="ffmpeg")
        try:
            result = self._run("ffmpeg", executable,
                               ["-v", "error", "-nostdin", "-xerror", "-err_detect", "explode",
                                "-protocol_whitelist", "file", "-i", str(source),
                                "-map", f"0:{stream_index}", "-f", "null", "-"])
        except BackendError as error:
            if error.code != "command_failed":
                raise
            raise BackendError("decode_failed", "FFmpeg could not decode the complete output audio.",
                               tool="ffmpeg", diagnostic=error.diagnostic, returncode=error.returncode) from error
        if result.stderr.strip():
            raise BackendError("decode_failed", "FFmpeg reported errors while decoding output audio.",
                               tool="ffmpeg", diagnostic=result.stderr[-_DIAGNOSTIC_LIMIT:])

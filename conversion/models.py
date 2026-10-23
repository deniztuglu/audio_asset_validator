"""Immutable tool/inspection results. No GUI or executable dependencies."""

from dataclasses import dataclass
from pathlib import Path


class BackendError(Exception):
    """An actionable backend failure with a stable code and bounded diagnostics."""

    def __init__(self, code: str, message: str, *, tool: str = "ffprobe",
                 diagnostic: str = "", returncode: int | None = None):
        super().__init__(message)
        self.code = code
        self.tool = tool
        self.diagnostic = diagnostic
        self.returncode = returncode


@dataclass(frozen=True)
class ToolInfo:
    name: str
    path: Path | None
    version: str | None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.path is not None and self.version is not None and self.error is None


@dataclass(frozen=True)
class EncoderInfo:
    name: str
    media_type: str
    description: str
    flags: str
    codec: str | None = None

    @property
    def experimental(self) -> bool:
        return self.flags[3] == "X"


@dataclass(frozen=True)
class BackendInfo:
    ffmpeg: ToolInfo
    ffprobe: ToolInfo
    encoders: tuple[EncoderInfo, ...]
    encoder_error: str | None = None

    @property
    def audio_encoders(self) -> tuple[EncoderInfo, ...]:
        return tuple(encoder for encoder in self.encoders if encoder.media_type == "audio")


@dataclass(frozen=True)
class AudioStream:
    index: int  # Absolute stream index, not the ordinal among audio streams.
    codec: str | None
    sample_rate: int | None
    sample_format: str | None
    bit_depth: int | None
    channels: int | None
    channel_layout: str | None
    duration_seconds: float | None
    # Preserve raw ffprobe values: 0 here is its unknown/not-applicable sentinel.
    # Effective bit_depth stays None unless one of these values is positive.
    bits_per_sample: int | None = None
    bits_per_raw_sample: int | None = None


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    container: str | None
    duration_seconds: float | None  # Container duration, separate from stream duration.
    audio_streams: tuple[AudioStream, ...]
    non_audio_stream_indices: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()

    def select_audio_stream(self, index: int | None = None) -> AudioStream:
        """Never silently choose a track from a file containing multiple tracks."""
        if index is None:
            if len(self.audio_streams) != 1:
                raise BackendError("ambiguous_audio_stream", "Select an audio stream by its absolute stream index.")
            return self.audio_streams[0]
        for stream in self.audio_streams:
            if type(index) is int and stream.index == index:
                return stream
        raise BackendError("invalid_audio_stream", f"No audio stream with index {index}.")

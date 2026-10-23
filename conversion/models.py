"""Immutable tool/inspection results. No GUI or executable dependencies."""

from dataclasses import dataclass
from pathlib import Path

from batch_rename import FileStamp


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


@dataclass(frozen=True)
class ConversionSettings:
    """Explicit target encoding; no remapping, normalization, or renaming options."""

    container: str
    codec: str
    sample_rate: int
    bit_depth: int | None = None
    quality: int | None = None  # MP3/libmp3lame VBR 0–9, not a generic quality scale.
    channel_policy: str = "preserve"

    def __post_init__(self):
        from .formats import validate_settings
        if not all(type(value) is str for value in (self.container, self.codec, self.channel_policy)):
            raise ValueError("container, codec, and channel_policy must be strings")
        validate_settings(self)

    @property
    def extension(self) -> str:
        return "." + self.container

    @property
    def encoder(self) -> str:
        """Keep the requested bitstream codec distinct from the FFmpeg encoder."""
        return "libmp3lame" if self.codec == "mp3" else self.codec


@dataclass(frozen=True)
class PlanIssue:
    code: str
    message: str


@dataclass(frozen=True)
class PlannedOperation:
    kind: str
    before: str | int | None
    after: str | int | None


@dataclass(frozen=True)
class ConversionRow:
    source: Path
    destination: Path | None
    metadata: MediaInfo | None
    settings: ConversionSettings
    source_stamp: FileStamp | None = None
    stream_index: int | None = None
    target_channels: int | None = None
    target_channel_layout: str | None = None
    operations: tuple[PlannedOperation, ...] = ()
    errors: tuple[PlanIssue, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversionPlan:
    input_root: Path
    output_root: Path
    settings: ConversionSettings
    rows: tuple[ConversionRow, ...]
    errors: tuple[PlanIssue, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        """A valid dry run is not execution approval or a reservation of paths."""
        return bool(self.rows) and not self.errors and not any(row.errors for row in self.rows)

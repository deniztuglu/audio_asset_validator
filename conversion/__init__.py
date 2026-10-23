"""Optional audio backend, independent of the validator and desktop interface.

This package currently inspects tools and media only. It cannot convert files.
"""

from .backend import FFmpegBackend
from .models import AudioStream, BackendError, BackendInfo, EncoderInfo, MediaInfo, ToolInfo

__all__ = ["FFmpegBackend", "AudioStream", "BackendError", "BackendInfo", "EncoderInfo", "MediaInfo", "ToolInfo"]

"""Optional audio backend, independent of the validator and desktop interface.

This package inspects media, previews conversions, and verifies outputs. It cannot write audio.
"""

from .backend import FFmpegBackend
from .models import AudioStream, BackendError, BackendInfo, EncoderInfo, MediaInfo, ToolInfo
from .models import ConversionPlan, ConversionRow, ConversionSettings, PlanIssue, PlannedOperation
from .planning import build_plan
from .models import VerificationResult
from .verification import verify_output

__all__ = ["FFmpegBackend", "AudioStream", "BackendError", "BackendInfo", "EncoderInfo", "MediaInfo", "ToolInfo"]
__all__ += ["ConversionSettings", "ConversionPlan", "ConversionRow", "PlanIssue", "PlannedOperation", "build_plan"]
__all__ += ["VerificationResult", "verify_output"]

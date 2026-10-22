"""Background validation with UI-independent progress, errors, and cancellation."""

from dataclasses import dataclass
from pathlib import Path
from queue import Queue
import threading

from validator import AssetResult, Rules, scan_files, validate_file


@dataclass(frozen=True)
class ScanEvent:
    kind: str
    completed: int = 0
    total: int = 0
    asset: AssetResult | None = None
    message: str = ""


class ScanWorker(threading.Thread):
    """Workers never touch Tk widgets. The main thread consumes immutable events.

    Cancellation takes effect between files (including after folder discovery).
    An in-progress WAV read finishes before the worker stops.
    """

    def __init__(self, folder: Path, rules: Rules):
        super().__init__(name="audio-preflight-scan", daemon=True)
        self.folder = folder
        self.rules = rules
        self.events: Queue[ScanEvent] = Queue()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        completed = total = 0
        try:
            root = self.folder.expanduser().resolve()
            if not root.is_dir():
                raise ValueError("choose an existing audio folder")
            paths = scan_files(root)
            total = len(paths)
            if not paths and not self._cancel.is_set():
                raise ValueError("no files found in the audio folder")
            self.events.put(ScanEvent("started", total=total))
            for path in paths:
                if self._cancel.is_set():
                    break
                asset = validate_file(path, root, self.rules)
                completed += 1
                self.events.put(ScanEvent("asset", completed, total, asset))
            kind = "cancelled" if self._cancel.is_set() else "finished"
            self.events.put(ScanEvent(kind, completed, total))
        except Exception as error:
            # Even unexpected worker failures must reach the UI instead of silently hanging.
            self.events.put(ScanEvent("error", completed, total,
                                      message=f"{type(error).__name__}: {error}"))


def output_destination(path: Path, extension: str, input_folder: Path | None) -> Path:
    """Choose a new report/profile outside the input tree; never replace existing data."""
    path = path.expanduser().absolute()
    if path.suffix.lower() != extension:
        raise ValueError(f"choose a filename ending in {extension}")
    if path.is_symlink() or path.exists():
        raise ValueError("that file already exists; choose a new filename")
    if not path.parent.is_dir():
        raise ValueError("the destination folder does not exist")
    if input_folder and path.resolve().is_relative_to(input_folder.expanduser().resolve()):
        raise ValueError("save outside the audio folder so reports and profiles are not scanned as assets")
    return path

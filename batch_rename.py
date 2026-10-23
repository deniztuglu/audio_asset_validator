"""Preview filename edits and write verified renamed copies, never edit originals."""

from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import os
from pathlib import Path
from queue import Queue
import re
import stat
import tempfile
import threading
import unicodedata
from typing import Callable, Sequence

from validator import AssetResult, Rules, validate_file


@dataclass(frozen=True)
class RenameOptions:
    replace_enabled: bool = True
    find: str = ""
    replacement: str = ""
    clear_existing: bool = False
    use_regex: bool = False
    trim_enabled: bool = True
    trim_begin: int = 0
    trim_end: int = 0
    trim_range: bool = False
    range_start: int = 1
    range_end: int = 1
    range_from_end: bool = False
    add_enabled: bool = True
    add_prefix: bool = True
    prefix: str = ""
    add_insert: bool = True
    insert: str = ""
    insert_at: int = 0
    add_suffix: bool = True
    suffix: str = ""

    @classmethod
    def from_dict(cls, values: object) -> "RenameOptions":
        if not isinstance(values, dict) or set(values) - {field.name for field in fields(cls)}:
            raise ValueError("invalid rename preset fields")
        for field in fields(cls):
            if field.name in values and type(values[field.name]) is not type(field.default):
                raise ValueError(f"{field.name} must be {type(field.default).__name__}")
        options = cls(**values)
        if min(options.trim_begin, options.trim_end, options.insert_at) < 0:
            raise ValueError("trim counts and insertion index must be zero or greater")
        if options.range_start < 1 or options.range_end < options.range_start:
            raise ValueError("range positions are 1-based; To must be at least From")
        if options.replace_enabled and not options.clear_existing:
            if not options.find and (options.replacement or options.use_regex):
                raise ValueError("enter Find text, or choose Clear Existing Name")
            if options.use_regex:
                try:
                    re.compile(options.find)
                except re.error as error:
                    raise ValueError(f"invalid Find regular expression: {error}") from error
        return options

    def rename_stem(self, stem: str) -> str:
        RenameOptions.from_dict(asdict(self))
        if self.replace_enabled:
            if self.clear_existing:
                stem = self.replacement
            elif self.find:
                if self.use_regex:
                    try:
                        stem = re.sub(self.find, self.replacement, stem)
                    except (re.error, IndexError) as error:
                        raise ValueError(f"invalid regex replacement: {error}") from error
                else:
                    stem = stem.replace(self.find, self.replacement)
        if self.trim_enabled:
            stem = stem[self.trim_begin:]
            if self.trim_end:
                stem = stem[:max(0, len(stem) - self.trim_end)]
            if self.trim_range:
                if self.range_from_end:
                    start, stop = max(0, len(stem) - self.range_end), max(0, len(stem) - self.range_start + 1)
                else:
                    start, stop = self.range_start - 1, self.range_end
                stem = stem[:start] + stem[stop:]
        if self.add_enabled:
            if self.add_prefix:
                stem = self.prefix + stem
            if self.add_insert:
                index = min(self.insert_at, len(stem))
                stem = stem[:index] + self.insert + stem[index:]
            if self.add_suffix:
                stem += self.suffix
        return stem


@dataclass(frozen=True)
class FileStamp:
    device: int
    inode: int
    size: int
    modified_ns: int

    @classmethod
    def read(cls, info: os.stat_result) -> "FileStamp":
        return cls(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@dataclass(frozen=True)
class RenameRow:
    source: Path
    destination: Path
    stamp: FileStamp | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.source.name != self.destination.name

    @property
    def status(self) -> str:
        return "Blocked" if self.errors else "Unchanged (skip)" if not self.changed else "Warning" if self.warnings else "Ready"


@dataclass(frozen=True)
class RenamePlan:
    input_root: Path
    output_root: Path
    options: RenameOptions
    rules: Rules
    rows: tuple[RenameRow, ...]

    @property
    def can_apply(self) -> bool:
        return bool(self.rows) and not any(row.errors for row in self.rows) and any(row.changed for row in self.rows)


def _key(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _filename_error(stem: str, name: str) -> str:
    if not stem.strip() or name in (".", "..") or name != name.strip() or name.endswith("."):
        return "filename must have a nonempty base and no leading/trailing spaces or trailing dot"
    if any(char in '<>:"/\\|?*' or ord(char) < 32 or ord(char) == 127 for char in name):
        return "filename contains a path separator, control character, or unsupported filename character"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if name.split(".")[0].upper() in reserved or len(os.fsencode(name)) > 255:
        return "filename is reserved or longer than 255 bytes"
    return ""


def _target_error(target: Path, output: Path) -> str:
    if output.is_symlink() or output.resolve() != output:
        return "output folder or its parents changed to a symlink; preview again"
    current = output
    for part in target.relative_to(output).parts[:-1]:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            return "output subfolder is a symlink or is occupied by a file"
    if target.is_symlink() or target.exists():
        return "destination already exists; no files are overwritten"
    if target.parent.is_dir() and any(_key(entry.name) == _key(target.name) for entry in target.parent.iterdir()):
        return "destination conflicts with an existing filename, ignoring case and Unicode normalization"
    return ""


def build_plan(input_root: Path, sources: Sequence[Path], output_root: Path,
               options: RenameOptions, rules: Rules) -> RenamePlan:
    RenameOptions.from_dict(asdict(options))
    root = input_root.expanduser().resolve()
    output = output_root.expanduser().absolute()
    if not root.is_dir():
        raise ValueError("input folder does not exist")
    if output.is_symlink():
        raise ValueError("output folder must not be a symlink")
    output = output.resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("choose a separate output folder outside the input tree (not an ancestor either)")
    if output.exists() and not output.is_dir():
        raise ValueError("output path is occupied by a file")
    if not output.exists() and not output.parent.is_dir():
        raise ValueError("the output folder's parent directory must exist")
    if not sources:
        raise ValueError("select at least one asset")
    rows = []
    targets: dict[str, list[int]] = {}
    seen_sources: set[Path] = set()
    for item in sources:
        source = item.expanduser().absolute()
        # Normalize parent aliases (e.g. macOS /var -> /private/var), but leave
        # the leaf unresolved so source symlinks can still be rejected by lstat.
        source = source.parent.resolve() / source.name
        relative = source.relative_to(root)  # Paths outside the scanned root are rejected.
        stem = options.rename_stem(source.stem)
        name = stem + source.suffix
        target = output / relative.parent / name
        errors, warnings = [], []
        stamp = None
        error = _filename_error(stem, name)
        if error:
            errors.append(error)
        if source in seen_sources:
            errors.append("source was selected more than once")
        seen_sources.add(source)
        try:
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or not source.parent.resolve().is_relative_to(root):
                errors.append("source must be a regular file inside the input tree; symlinks are not copied")
            else:
                stamp = FileStamp.read(info)
        except OSError as error:
            errors.append(f"cannot inspect source: {error}")
        changed = source.name != name
        if changed and not errors:
            error = _target_error(target, output)
            if error:
                errors.append(error)
            if re.fullmatch(rules.naming_pattern, name) is None:
                warnings.append("proposed filename does not match the scan profile's naming rule")
            targets.setdefault(_key(str(target.relative_to(output))), []).append(len(rows))
        rows.append(RenameRow(source, target, stamp, tuple(errors), tuple(warnings)))
    for indices in targets.values():
        if len(indices) > 1:
            for index in indices:
                rows[index] = replace(rows[index], errors=(*rows[index].errors, "multiple assets would use the same output path"))
    return RenamePlan(root, output, options, rules, tuple(rows))


@dataclass(frozen=True)
class CopiedAsset:
    source: Path
    destination: Path
    sha256: str
    validation: AssetResult


@dataclass(frozen=True)
class RenameOutcome:
    copies: tuple[CopiedAsset, ...] = ()
    cancelled: bool = False
    error: str = ""


class _Cancelled(Exception):
    pass


def execute_plan(plan: RenamePlan, cancel: threading.Event | None = None,
                 on_copied: Callable[[CopiedAsset], None] | None = None) -> RenameOutcome:
    """Recheck the preview, exclusively create copies, verify bytes, and revalidate.

    Completed copies are retained on cancellation/failure. A partially written or
    unverified current copy is removed; existing files and originals are untouched.
    """
    cancel = cancel or threading.Event()
    copies = []
    current: Path | None = None
    created = False
    try:
        if not plan.can_apply:
            raise ValueError("preview must contain changes and no blocked rows")
        fresh = build_plan(plan.input_root, [row.source for row in plan.rows], plan.output_root, plan.options, plan.rules)
        if not fresh.can_apply or fresh.rows != plan.rows:
            raise ValueError("source files or destinations changed since preview; preview again")
        if cancel.is_set():
            return RenameOutcome(cancelled=True)
        plan.output_root.mkdir(exist_ok=True)
        for row in plan.rows:
            if cancel.is_set():
                raise _Cancelled
            if not row.changed:
                continue
            current, created = row.destination, False
            if row.source.is_symlink() or FileStamp.read(row.source.stat()) != row.stamp:
                raise ValueError(f"source changed since preview: {row.source.name}")
            error = _target_error(current, plan.output_root)
            if error:
                raise ValueError(error)
            current.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with row.source.open("rb") as source:
                if FileStamp.read(os.fstat(source.fileno())) != row.stamp:
                    raise ValueError("source changed while opening it")
                with current.open("xb") as destination:
                    created = True
                    while True:
                        if cancel.is_set():
                            raise _Cancelled
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        destination.write(block)
                        digest.update(block)
                if FileStamp.read(os.fstat(source.fileno())) != row.stamp:
                    raise ValueError("source changed during copying")
            actual = hashlib.sha256()
            with current.open("rb") as copied:
                while True:
                    if cancel.is_set():
                        raise _Cancelled
                    block = copied.read(1024 * 1024)
                    if not block:
                        break
                    actual.update(block)
            if digest.digest() != actual.digest():
                raise ValueError("copied bytes failed SHA-256 verification")
            validation = validate_file(current, plan.output_root, plan.rules)
            copied_asset = CopiedAsset(row.source, current, digest.hexdigest(), validation)
            copies.append(copied_asset)
            created = False
            if on_copied:
                on_copied(copied_asset)
        return RenameOutcome(tuple(copies), cancelled=cancel.is_set())
    except Exception as error:
        cleanup_error = ""
        if created and current is not None:
            try:
                current.unlink()
            except OSError as cleanup:
                cleanup_error = f"; could not remove incomplete copy {current}: {cleanup}"
        if isinstance(error, _Cancelled):
            return RenameOutcome(tuple(copies), cancelled=True, error=cleanup_error)
        return RenameOutcome(tuple(copies), error=f"{type(error).__name__}: {error}{cleanup_error}")


@dataclass(frozen=True)
class RenameEvent:
    kind: str
    copied: CopiedAsset | None = None
    outcome: RenameOutcome | None = None


class RenameWorker(threading.Thread):
    def __init__(self, plan: RenamePlan):
        super().__init__(name="audio-renamed-copies", daemon=False)
        self.plan = plan
        self.events: Queue[RenameEvent] = Queue()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        outcome = execute_plan(self.plan, self._cancel, lambda copied: self.events.put(RenameEvent("copied", copied)))
        self.events.put(RenameEvent("finished", outcome=outcome))


class RenamePresetStore:
    """Five personal slots; corrupt or unrecognized existing settings are not replaced."""
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, RenameOptions]:
        if self.path.is_symlink():
            raise ValueError("rename preset settings must not be a symlink")
        if not self.path.exists():
            return {}
        values = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(values, dict) or set(values) != {"version", "slots"}
                or type(values["version"]) is not int or values["version"] != 1
                or not isinstance(values["slots"], dict) or set(values["slots"]) - set("12345")):
            raise ValueError("unrecognized rename preset settings; original file was left unchanged")
        return {key: RenameOptions.from_dict(value) for key, value in values["slots"].items()}

    def save(self, slot: int, options: RenameOptions) -> None:
        if type(slot) is not int or slot not in range(1, 6):
            raise ValueError("choose a preset slot from 1 to 5")
        options = RenameOptions.from_dict(asdict(options))
        saved = self.load()
        saved[str(slot)] = options
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".rename-presets-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump({"version": 1, "slots": {key: asdict(value) for key, value in saved.items()}}, output, indent=2)
                output.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

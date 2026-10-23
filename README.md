# Audio Asset Preflight Validator

A small, read-only Python tool for checking audio exports before importing them into
FMOD, Wwise, or a game engine. It scans nested folders, reports actionable errors and
warnings, and exports a CSV with one row per check. No DAW, middleware installation,
third-party Python packages, or credentials are needed.

**Part 2 desktop interface:** run `python3 gui.py` to choose folders, edit rules,
save/load project profiles, inspect audio metadata, select multiple assets, preview
batch filename edits, create renamed copies, and export CSV reports. See
[the desktop guide and Mac verification checklist](README_PART2.md). The existing
CLI below remains available; conversion is not implemented.

**UI review branch:** fetch and select `feature/ui-simplification` in GitHub
Desktop, then run `python3 gui.py`. This branch adds a results-focused workspace,
an **Edit rules...** window, clear issue summaries, compact/full table views,
count filters, and simpler rename presets. Follow the updated desktop guide to
compare the workflow before merging.

## Quick start

Requires **Python 3.10 or later** on macOS or Linux. Check with `python3 --version`.
Python 3.12+ is recommended for its additional PCM WAV compatibility.

From this repository's directory:

```sh
python3 validator.py /path/to/your/audio
mkdir -p reports
python3 validator.py /path/to/your/audio --csv reports/preflight.csv
```

For paths with spaces, use quotes: `python3 validator.py "/path/to/Audio Exports"`.
Put reports and configuration outside the audio folder: every file inside that folder
is considered an asset, including hidden files and non-WAV files.

## Try the included demo

The generator creates ten tiny synthetic examples, including a nested 24-bit stereo
WAV, a wrong-rate file, a naming violation, short/empty audio, unsupported channel
count and bit depth, a malformed WAV, and an unsupported extension. No downloaded
audio or licensed samples are used. These files contain silence, not designed sounds.

```sh
python3 tools/generate_demo.py
mkdir -p reports
python3 validator.py demo_audio --config config.example.json --csv reports/demo.csv
```

Expected summary (exit status **1**, because the demo deliberately contains errors):

```text
10 files: 2 passed, 1 with warnings, 7 with errors.
```

Typical output:

```text
[ERROR] SFX_Explosion_03.wav
  ERROR sample_rate: Sample rate must match the project setting.
    Expected: 48000 Hz; actual: 44100 Hz
[WARNING] SFX_UI_Tiny_01.wav
  WARNING duration: Very short audio may be intentional; review before import.
    Expected: >= 0.05 seconds; actual: 0.010000 seconds
```

The generator refuses to overwrite an existing directory. For another run, specify
a new directory: `python3 tools/generate_demo.py demo_audio_02`.
CSV output also refuses to overwrite existing files; use a new report filename
for each run. Validation never modifies source assets. The optional desktop batch
renamer creates copies in a separate folder and leaves original filenames and bytes intact.

## V1 rules

These defaults are example studio policies, not universal game-audio standards.

| Check | Default expectation | Failure level |
| --- | --- | --- |
| Naming | `SFX_` + one or more underscore-separated alphanumeric segments + two-digit variation + `.wav` | Error |
| Format | Readable, uncompressed integer PCM WAV with complete audio frames | Error |
| Sample rate | 48,000 Hz | Error |
| Bit depth | 16-bit or 24-bit | Error |
| Channels | Mono or stereo | Error |
| Duration | Nonempty, at least 0.05 seconds | Empty: error; short: warning |

`SFX_UI_Click_01.wav` and `SFX_Footstep_Concrete_01.wav` match the default name.
The `SFX_` prefix is case-sensitive; `.wav` and `.WAV` are both accepted.
Naming uses the filename only, not its parent folders. A file can have several issues.

Format validation reads sample data in blocks to detect truncated/incomplete payloads
without loading the entire audio file into memory. WAV extension alone is not enough.
If format inspection fails, metadata checks are **not executed** for that file; the
remaining assets are still checked. An empty but otherwise valid PCM WAV gets a
duration error. Successful metadata checks do not establish audible quality.

Directory symlinks are not followed. File symlinks and non-regular files are reported
as errors rather than opened. Inaccessible directories abort the scan with an
operational error so an incomplete scan is not presented as complete.

## Project configuration

Use `config.example.json` as a starting point:

```sh
python3 validator.py /path/to/audio --config config.example.json
```

All keys are optional; omitted keys retain their defaults. Unknown keys, invalid
regular expressions, and invalid setting types are rejected with a useful error.

```json
{
  "naming_pattern": "SFX_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_[0-9]{2}\\.[wW][aA][vV]",
  "sample_rate": 48000,
  "bit_depths": [16, 24],
  "channels": [1, 2],
  "min_duration_seconds": 0.05
}
```

The pattern is a Python regular expression matched against the **entire filename**.
For example, `"channels": [1]` makes stereo an error. Changing an allowed bit depth
does not enable additional encodings: IEEE float, compressed WAV, MP3, AIFF, and
FLAC remain outside V1. Standard PCM WAV works on Python 3.10+; PCM
`WAVE_FORMAT_EXTENSIBLE` requires Python 3.12+, and other extensible encodings are
unsupported. This tool uses Python's `wave` parser, not a complete RIFF conformance
or audio-content analyzer.

## Reports and automation

CSV columns are `file`, `check`, `result`, `expected`, `actual`, and `message`.
Paths are relative to the scanned folder. Reports include passing checks so reviewers
can distinguish what passed from what could not be inspected. Open the CSV in a
spreadsheet and filter `result` to `ERROR` or `WARNING`.

File summary counts are exclusive: a file with both errors and warnings counts as
having errors; its individual warnings still appear in the terminal and CSV.

| Exit status | Meaning |
| --- | --- |
| `0` | No validation errors (warnings allowed) |
| `1` | At least one validation error; with `--strict`, warnings also cause this status |
| `2` | Invalid arguments/configuration, missing or empty folder, scan failure, or report failure |

```sh
python3 validator.py /path/to/audio --strict --csv reports/strict-preflight.csv
```

An empty folder is an operational error, not a successful zero-file validation.
The report's parent directory must exist. Keep old reports outside the asset folder:
an existing CSV inside it is itself an unsupported asset on subsequent scans.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests generate their own temporary PCM WAVs. They exercise the six rules, configurable
policies, malformed and truncated data, unsupported encodings, symlinks, CSV content,
CLI exit statuses, scan errors, and preservation of source bytes.
The original 28 V1 tests are unchanged. Additional tests cover profiles, naming
presets, background scans, and the desktop interface. GUI tests need Tk and a
graphical display; otherwise that test class is explicitly skipped.

## Verify the project on your Mac

1. In GitHub Desktop, select `audio_asset_validator`, fetch from origin, and select
   `feature/ui-simplification` for this UI review (use `main` after merging).
   Pull if GitHub Desktop offers it. Choose **Repository > Open in Terminal**
   so commands run from your local repository directory.
2. Run `python3 --version`. Use Python 3.10 or later (3.12+ recommended). If needed,
   install a current Python 3 release from <https://www.python.org/downloads/macos/>,
   then reopen Terminal. No `pip install` step is needed.
3. Run `python3 -m unittest discover -s tests -v`. Expect **141 tests** and **OK**
   when Tk and a graphical display are available; see the Part 2 guide for details.
   Six backend integration tests also need ffprobe (two need FFmpeg); missing
   tools skip those tests, while the backend unit tests still run.
4. Run the demo commands above. The expected seven error files and one warning file
   are intentional: they demonstrate that problems are detected. Open the report
   with `open reports/demo.csv` to inspect individual checks in your spreadsheet app.
5. Check the known-good subset with `python3 validator.py demo_audio/nested`.
   Expect `1 files: 1 passed, 0 with warnings, 0 with errors.`
6. Export your own uncompressed integer PCM WAV at 48 kHz, 16-bit or 24-bit,
   mono or stereo, and at least 0.05 seconds long. Name it
   `SFX_Footstep_Concrete_01.wav`, put it in a separate folder, and run
   `python3 validator.py "/full/path/to/that/folder"`. It should pass.
7. Rename that file to `footstep-final.wav` and run the same command. Expect a naming
   error. Rename it back and rerun; it should pass again. Re-export at 44.1 kHz to
   test the sample-rate error, then restore 48 kHz and rerun.

Keep only the intended assets in your test folder. A `.DS_Store` or any other non-WAV
file inside it is also reported as unsupported. Use fresh demo/report paths if you
already ran the demo; the generator and CSV writer deliberately refuse to overwrite.

## Optional FFmpeg/ffprobe inspection backend

`conversion/` currently detects executables and inspects metadata only. It does
not convert files or change the GUI/CLI. Existing validation requires neither
executable. The optional backend uses tools already on `PATH`, or explicit paths:

```python
from conversion import FFmpegBackend

backend = FFmpegBackend()  # Optional: ffmpeg="/path/to/ffmpeg", ffprobe="/path/to/ffprobe"
tools = backend.detect()  # Independent availability/version results and encoder list.
media = backend.inspect("/path/to/Audio Exports/example.wav")
print(media.container, media.audio_streams)
audio = media.select_audio_stream()  # Multiple tracks require an explicit stream index.
```

Unavailable metadata is `None`; numeric zero is preserved. Raw bit-depth fields
retain ffprobe's zero sentinel, while effective `bit_depth` is `None` when unknown.
Sample format `s32` alone does not imply 32-bit source audio. Stream and container
durations remain separate. Successful probing is not full decoding or validation.
`BackendError` exposes a stable `code`, `tool`, `diagnostic`, and optional exit code.
Run the isolated backend tests with
`python3 -m unittest discover -s tests -p test_ffmpeg_backend.py -v`.

## Technical decisions and scope

- `pathlib` / `os.walk` handle paths and recursive scanning with explicit error handling.
- `wave` reads PCM metadata and audio frames; duration is frames divided by sample rate.
- Frozen dataclasses describe policies and structured check results.
- `argparse`, `json`, and `csv` provide the interface, configuration, and reporting.
- `unittest` and `tempfile` keep verification portable and dependency-free.

The implementation deliberately stays small: one validator module, one optional
JSON configuration, tests, and a demo generator. V1 does not measure loudness, detect
silence/clipping, convert audio, inspect FMOD/Wwise projects, detect duplicates, or
provide a GUI. Silence passes if its metadata satisfies the rules. There is no maximum
duration limit. Future iterations can add profiles or richer reporting when needed.

For a portfolio demo, show the fixture folder, run the command, open the CSV, then
correct an intentionally bad export and rerun with a fresh report path. Explain
how early validation reduces avoidable import and asset-management mistakes.

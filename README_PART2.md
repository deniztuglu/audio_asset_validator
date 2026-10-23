# Part 2: desktop rules editor and project profiles

The desktop application uses the existing read-only validation engine. It adds
folder selection, editable project rules, naming presets, saved profiles,
background scans, audio metadata views, multiple selection, per-file explanations,
status filters, CSV export, and a previewable batch renamer that creates verified copies.
It does not rename, rewrite, or convert input files. This is a source-run desktop
application, not yet a packaged, signed, or notarized macOS `.app`.

## Start on your Mac

While reviewing this change, fetch in GitHub Desktop and select
`feature/ui-simplification` under **Current Branch**. This review branch starts
from `feature/part2-desktop-gui` and includes all its features. The UI changes will
not appear on the earlier branch or `main` until merged.

Choose **Repository > Open in Terminal**, then run:

```sh
python3 --version
python3 -m tkinter
```

Use Python 3.10+; Python 3.12+ is recommended. The second command should open a Tk
test window. Close that window, then launch:

```sh
python3 gui.py
```

No `pip install` step is required. Tkinter is part of many Python distributions,
but not every Python installation includes working Tk libraries. If the Tk test
fails, use a current macOS installer from <https://www.python.org/downloads/macos/>
and reopen Terminal. Use the same Python interpreter for the Tk check and the app.
Do not try to fix missing Tk with `pip install tkinter`.

## Simplified workspace

The main window focuses on scanning and inspecting results:

- Choose your **Audio folder** and click **Scan folder** beside it.
- **Active project profile** summarizes the rules. **Edit rules...** opens a
  separate editor; **Load profile...** loads an existing JSON profile.
- The default **Compact** table shows status, relative file path, and the actual
  issue, for example `Sample rate: 44.1 kHz; expected 48 kHz`. Choose **Full
  details** for all metadata columns. Both views retain the same data and selection.
- Click **All files**, **Errors**, **Warnings**, or **Passed** to filter results.
  Each button shows its file count. Counts cover the whole scan, including while
  it is running, rather than just the current filter.
- Select assets, then use **Batch Rename...** beside the selection count.
  **Export report (all files)...** always exports the whole completed scan.
- Progress and **Cancel scan** appear while scanning. A prominent notice appears
  when rules/folder changes require rescanning, or a scan is incomplete.

In the rules editor, **Done** or the window's close button hides the editor.
Changes apply immediately and remain in memory when reopening it. Saving the
profile is a separate action. The active profile explicitly shows **Unsaved
changes** until saved or another profile is loaded.

Advanced controls are expandable. Loading a profile with custom regex or unusual
audio settings exposes those controls automatically. The naming example and test
field stay visible while scrolling through naming settings.

## First scan

Generate the demo once, if you do not already have it:

```sh
python3 tools/generate_demo.py
```

The generator refuses an existing directory. Keep your existing demo, or generate
a new one with `python3 tools/generate_demo.py demo_audio_02`.

1. Click **Browse...** and select the generated demo folder.
2. Leave the default profile and rules in place, then click **Scan folder**.
   Use **Edit rules...** if you want to inspect or customize the rules first.
3. Expect **10 files: 2 passed, 1 with warnings, 7 with errors** for a clean demo.
4. Select `SFX_Explosion_03.wav`. Its Issues column shows actual **44.1 kHz** and
   expected **48 kHz**. The **Validation checks** tab retains exact Hz values and
   a suggested next step.
5. Try the **Errors**, **Warnings**, and **Passed** filters. They filter file-level
   statuses. A file with both an error and warning is listed under Errors.
6. Click **Export report (all files)...** and save a new file outside the audio folder. Export
   always includes the entire completed scan, regardless of the current filter.

The demo contains silent WAVs and deliberately bad exports; the errors demonstrate
the checks working. The engine considers every file an asset, including hidden and
non-audio files. If Finder adds `.DS_Store`, it is also reported as unsupported and
your counts will differ. Directory symlinks are not followed; file symlinks are errors.

## Result columns, context tabs, and multiple selection

Choose **Full details** to include format, sample rate, bit depth, channel count,
and duration alongside file, status, and issues. **Compact** shows the first three
columns; it does not remove metadata. Scroll horizontally when needed.
GUI metadata uses concise units such as `48 kHz`, `Mono (1)`, and `0.100 s`.
The validation-check inspector, CLI, and CSV retain exact engine values.
These are the actual values from the completed validation checks; unsupported or
unreadable files show **Not inspected** for metadata the engine could not check.

Select files to inspect three context tabs:

- **Overview:** selected files, their statuses, scan profile, and checks needing attention.
- **Audio metadata:** all inspected audio properties with expected values and check results.
- **Validation checks:** naming, format, rate, depth, channels, and duration, with
  actual/expected values, explanations, and suggested next steps.

Use **Shift-click** for a contiguous range and **Command-click** on Mac (**Ctrl-click**
on Windows/Linux) to toggle individual files. Command-A/Ctrl-A in the table selects
all currently visible rows. Changing a status filter retains the selected rows still
visible under that filter. Context tabs describe all selected files, not just the first.

## Batch Rename: create renamed copies

Complete a scan, select the assets in the result table, then click **Batch Rename...**
beside the selection count. The button is enabled for a current, completed
scan with a nonempty selection, in either preset or Advanced regex naming mode.
The popup takes a snapshot of those selected files and the scan's validation rules.

This feature changes output filenames only. It **creates renamed copies outside
the input tree**; it does not rename originals in place, change extensions, convert
audio, or alter embedded audio metadata. Relative subfolders are retained. Only
files whose proposed name changes are copied; unchanged rows are skipped.

The controls follow the reference-style Replace / Trim / Add workflow:

| Section | Controls and behavior |
| --- | --- |
| Replace | Find/Replace uses case-sensitive literal text, or enable Regular Expressions for Python `re.sub` patterns and replacement groups such as `\1`. |
| Clear Existing Name | Uses the Replace / new base field as the complete new base; ignores Find and regex, then applies Trim and Add. |
| Trim ends | Remove a nonnegative number of characters from the beginning and/or end. Zero removes nothing. |
| Trim range | Remove the inclusive From–To range, counted from the chosen end, using **1-based** positions. Applies after trimming the ends. |
| Add prefix | Prepend text exactly as entered; include any separator you want. |
| Insert | Insert text at a **0-based** index in the already-prefixed base. An index beyond the end appends. |
| Add suffix | Append text, before the protected original extension. |

Order is **Replace → Trim ends → Trim range → Prefix → Insert → Suffix**.
Operations act on the base name, so `Click.wav` retains `.wav`; do not include an
extension in the new base unless you intentionally want it as part of that base.
Scroll the options area to reach controls on smaller displays.
The preview table shows the selected assets' original relative paths as soon as
the window opens, and keeps them visible while you edit. Select a row to see its
full original path below the table. Proposed names appear after **Preview names**.

1. Choose an output folder outside the scanned input folder. It can be an existing
   folder, or type a new folder whose parent exists. An ancestor of the input is
   also forbidden, so the input and output trees are separate.
2. Configure the enabled operations and click **Preview names**. Preview does not
   create folders or files. Select a preview row to read its proposed destination,
   blockers, or warnings.
3. Resolve every **Blocked** row before applying. Duplicate destinations, existing
   names (including case/Unicode-normalized collisions), invalid filenames,
   symlinks, missing sources, and unsafe paths block the whole plan.
4. A **Warning** means the new name does not match the validation profile. You may
   still create the copies; output revalidation will report that mismatch honestly.
5. Click **Create renamed copies**. The background worker rechecks the preview,
   exclusively creates new files, verifies their bytes with SHA-256, and reruns the
   existing validator on every completed copy using the original scan rules.
6. Inspect **Copied / PASS**, **Copied / WARNING**, or **Copied / ERROR** in the
   popup, and select rows for their output validation details. A renamed 44.1 kHz
   export still fails a 48 kHz rule: renaming does not repair its audio properties.

Editing any option/output path invalidates the preview. Source changes or new
destination conflicts after preview require a fresh preview. No existing file is
overwritten, even if it appears between checking and creating an output file.

**Cancel copy** stops between chunks/files; an in-progress output validation can
finish first. Completed verified copies remain; the current incomplete/unverified
copy is removed. Failures use the same policy and report cleanup failures explicitly.
Closing the popup or app while copying requests cancellation and waits for the
worker to stop before destroying its UI. Renamed copies are independent exports;
the main result table continues to describe the untouched original folder. Browse
to the output folder and scan it if you want it in the main results table.

### Five personal rename presets

The **Rename preset** dropdown selects **Preset 1–5**; **Factory default** resets
the options to enabled sections with blank text and zero edge trims (a no-op).
Selecting an empty slot keeps current edits. Click **Save options to Preset N**
to persist them. Presets store operations, not source selections or an output path.
They are separate from validation project profiles.
Regular expressions are under **Show advanced replace options**. Character-range
trimming is under **Show character range options (advanced)**. Presets using
these operations expose the relevant controls automatically. Collapsing a section
does not turn its operations off; use its checkbox to disable an operation.

On macOS, presets are stored in
`~/Library/Application Support/Audio Asset Validator/rename_presets.json`.
Linux uses `$XDG_CONFIG_HOME/audio-asset-validator/` (or `~/.config/...`); Windows
uses `%APPDATA%/Audio Asset Validator/`. Saving is atomic and preserves other slots;
unrecognized/corrupt existing settings are reported and left unchanged.

For a first Mac check, select `footstep-final.wav`, replace `footstep-final` with
`SFX_Footstep_Concrete_01`, preview, and create a renamed copy in a fresh output
folder. Expect output **PASS**, identical audio bytes, and the original bad-name
file still present under its original name.

## Edit audio rules

Open **Edit rules... > Audio rules** for:

- Sample rate: choose a common rate or type a positive integer.
- Bit depths: select 16-bit and/or 24-bit. Expand **Show advanced audio settings**
  for 8-bit, 32-bit, or other values compatible with existing configurations.
- Channels: select mono/stereo. Advanced settings allow other counts, such as `6, 8`.
- Minimum duration: a finite, nonnegative number of seconds. Short audio is a warning;
  zero frames is always an error, even if the minimum is zero.

These settings change expectations, not the audio. The engine still supports
uncompressed integer PCM WAV only. Adding a value does not enable a new encoding.
Scroll within a rules tab to reach controls on smaller displays.

## Edit naming rules

Open **Edit rules... > Naming rules** for two explicit modes:

**Build a naming rule** starts with `SFX`, `DX`, and `FLY` choices and lets you add or
remove your own prefixes. Check one or more allowed prefixes,
choose flexible/uppercase/lowercase descriptive words, and choose 1–4 variation
digits using **Number suffix digits**, with an example such as `01`.
Prefixes remain exactly uppercase; the WAV extension accepts either case.
For example, DX + lowercase + three digits matches `DX_hello_001.wav`.
The default SFX + flexible + two digits exactly preserves the V1 naming regex.

To add a prefix, enter it in **Prefix to add or remove** and click **Add** (or
press Return). For example, `amb` becomes `AMB` and its checkbox is selected.
Uncheck a prefix to keep it in the editor but disallow it for the next scan.
To delete a choice, enter its name and click **Remove**. Clicking a checkbox also
copies its name into the field. Built-in choices can be removed too.

Prefixes are trimmed and converted to uppercase. They must start with a letter
and can contain letters/numbers, with `_` or `-` between groups: `AMB`, `MUS`,
`VO_EN`, and `SFX-UI` are valid. Do not add the trailing filename separator `_`;
the naming builder supplies it. Duplicates, spaces, path separators, and regex
expressions are rejected. Use Advanced regex for other naming conventions.
Removing every choice is allowed while editing, but a preset needs at least one
checked prefix before it can be saved or used to scan.

Expand **Show custom naming pattern (advanced)** and select **Use a regular
expression instead** to edit the entire filename pattern. Matching uses
`re.fullmatch`, as in V1. This mode supports conventions outside the presets,
including other prefixes and casing. Enter an ordinary Python regex in the UI,
for example `VO_[A-Z]+_[0-9]+\.wav`; JSON escaping is handled when saving.

The filename tester gives immediate match feedback without touching a file.
It tests naming only, not audio metadata. An invalid regex or incomplete preset
must be corrected before scanning or saving.

Loading an arbitrary V1/custom regex selects Advanced mode and preserves that
expression exactly. It is not guessed into dropdown settings. Explicitly switching
back to **Build a naming rule** replaces it with the pattern generated by those controls.

## Save and load profiles

In **Edit rules...**, name your project profile, then use **Save profile as...**. Store it outside the
audio folder and choose a new filename. Existing files are never overwritten.
Use **Load profile...** to reopen it. Unsaved profile edits appear explicitly in
the active profile card and with `*` in the window title. Loading another profile
or quitting the application asks before discarding them. Closing only the rules
editor keeps the edits in memory.

Example profiles are supplied in `profiles/`:

| File | Example policy |
| --- | --- |
| `SFX_Default.json` | Original V1 defaults: SFX, 48 kHz, 16/24-bit, mono/stereo |
| `DX_Dialogue_Example.json` | DX names, 48 kHz, 16/24-bit, mono |
| `FLY_Foley_Example.json` | FLY names, 48 kHz, 16/24-bit, mono |

These are editable examples, not universal dialogue or Foley standards. One profile
applies to the entire scanned folder; category-specific audio rules within one
project remain future work. Selecting multiple prefixes only broadens naming.

The new JSON envelope contains `profile_version: 1`, a `name`, a `rules` object,
and `naming` preset metadata (or `null` for Advanced mode). Customized lists also
save optional `naming.available_prefixes`, so unchecked custom choices and removed
built-in choices are restored when loading. Older profiles without this field still
load with the starter choices plus any prefixes already selected by that profile.
The profile schema version
is independent of the project phase number. Unknown versions and inconsistent
preset/regex combinations are rejected rather than silently changed.

Existing flat V1 JSON configurations, including `config.example.json`, also load.
They are read, not rewritten; saving produces a new versioned file. Profiles contain
rules and editor mode, not an audio-folder path or old scan results. Choose your
folder after launching. Both flat and versioned profiles work with the CLI:

```sh
python3 validator.py demo_audio --config config.example.json
python3 validator.py demo_audio --config profiles/SFX_Default.json
```

## Scan state, progress, and cancellation

Scanning runs on a background thread. The main Tk thread alone updates widgets.
Folder discovery initially shows an activity indicator; file validation then shows
progress. Rules, folder selection, and profile actions are disabled during a scan
so a single scan always uses one snapshot of settings.

**Cancel scan** takes effect after current folder discovery or the current file read.
A large file can take time to finish, but the window remains responsive. Partial
results from cancelled or failed scans remain inspectable and cannot be exported
as a complete report. Fix an error or select a different folder and scan again.

Changing rules, loading a profile, or changing the folder after completion marks
the old results stale and disables CSV export until a new scan completes. This
prevents old results being presented as if they used the new settings.

Profiles and reports must be new files outside the selected audio tree. This also
prevents the next scan from treating your own saved output as an unsupported asset.
Closing the window requests cancellation; scans do not modify source audio.

## Automated verification

```sh
python3 -m unittest discover -s tests -v
```

With working Tk, a display, FFmpeg, and ffprobe, there are **168 tests**: the unchanged
28 V1 tests, 55 configuration/naming/worker/encoding/presentation/rename tests,
30 real Tk interaction tests, 28 backend tests (including six real-tool tests),
and 27 settings/planning tests (including one real-probe test).
Missing backend tools skip only the relevant integration tests; unit tests still run.
The GUI tests create temporary audio, exercise real widgets and the event loop,
and stub native file dialogs/message boxes to keep the run noninteractive. They
keep one Tk interpreter alive for the class and create a separate `Toplevel`
window for each test, rather than repeatedly destroying/recreating Tk itself.
Test windows appearing briefly is expected; no manual interaction is required.

Without a display or Tk installation, the 138 non-GUI tests still run and the GUI
class is explicitly skipped. A skipped GUI run does not establish desktop readiness.
On Linux CI, run the same suite under an Xvfb display when available.

This implementation was exercised with Python 3.12.14 and 3.15.0rc3 on a virtual
Linux display, including a 1000x680 window. Native macOS behavior still needs verification.

### A float-WAV format test fails after updating Python

Python 3.15 adds IEEE float decoding to `wave`, so a file being readable no longer
implies integer PCM. The validator now explicitly checks the decoder's encoding
when the newer `getformat()` API is available. On older Python versions, `wave`
itself rejects unsupported encodings. Float WAV remains outside V1/Part 2 even if
32-bit or 64-bit is selected as an allowed depth; integer PCM and supported
extensible PCM continue to work. Five encoding regression tests cover these cases.

If `test_float_encoded_wav_is_unsupported` fails with `'PASS' != 'ERROR'`, pull the
latest Part 2 changes and rerun. Keep the updated Python installation. The original
test is preserved; the application must enforce the same policy on both runtimes.

## Troubleshooting a macOS crash during tests

The message `Secure coding is not enabled for restorable state` is a macOS/Tk
window-restoration warning. It is separate from a process crash. A segmentation
fault is not a passing test run, even if a window appeared first.

A user report from Python 3.12.0 with Tk 8.6 on macOS 14.4.1 shows the main thread
crashing in Cocoa while `PbarElementDraw` draws a native progress bar. That stack
does not establish a naming/Unicode or audio-validation error. The test harness
now avoids repeated Tk interpreter destruction and explicitly cleans up variable
traces and progress/poll callbacks on close. The crash has not been reproduced
or the correction verified on a Mac; a native rerun remains necessary.

1. Pull the latest Part 2 branch changes.
2. Install a current macOS Python release from
   <https://www.python.org/downloads/macos/> to update the older bundled Tk runtime,
   then reopen Terminal. Confirm which interpreter you are running:

   ```sh
   python3 --version
   python3 -c "import sys; print(sys.executable)"
   ```

   If the version is still `3.12.0`, your shell is still selecting the old Python;
   select the newly installed interpreter before retrying.
3. Run `python3 -m tkinter` and close its test window. Then run
   `python3 -m unittest discover -s tests -v`. Expect **168 tests** and **OK**
   when FFmpeg/ffprobe are also available (otherwise their integration tests skip);
   the GUI windows are part of the tests. Wait for the shell prompt to return.
4. Start the actual application separately with `python3 gui.py`. This command
   remains running until you close the application; it does not print a test summary.

If the crash persists, capture the complete test output and the new crash report.
For a focused GUI rerun with Python fault diagnostics enabled:

```sh
python3 -X faulthandler -m unittest discover -s tests -p 'test_gui.py' -v
```

To check the original engine independently, run
`python3 -m unittest discover -s tests -p 'test_validator.py' -v` (28 tests).
That is useful isolation, but does not establish GUI readiness.

## Mac acceptance checklist before merging

- [ ] Tk test window and `gui.py` launch with the same Python interpreter.
- [ ] Native folder/profile/save dialogs work, including paths with spaces or accents.
- [ ] Default demo produces 2 passed / 1 warning / 7 errors (apart from extra files).
- [ ] Choosing `demo_audio/nested` produces one passing file, matching the CLI.
- [ ] Naming presets accept SFX/DX/FLY, all three word casing options, and 1–4 digits.
- [ ] Add `amb` and confirm `AMB` is checked and `AMB_Rain_01.wav` matches in the
      filename tester. Remove it and confirm it no longer matches. Try `VO_EN` too.
- [ ] Save/reload a profile with checked and unchecked custom prefixes and a removed
      starter prefix; all choices and selections are restored. Existing profiles still load.
- [ ] Filename preview updates immediately; invalid patterns prevent scanning.
- [ ] Audio rule changes affect results after rescanning, e.g. mono-only flags stereo.
- [ ] Load the old `config.example.json`; save a new profile; close/reopen and reload
      it. Values and naming mode remain correct. Repeat with an Advanced custom regex.
- [ ] Selecting a failed file shows actual/expected values and next steps; filters work.
- [ ] Result columns and all three context tabs show rate, depth, channels, duration,
      format, and naming checks. Unsupported files show unavailable metadata honestly.
- [ ] Shift-click selects a range; Command-click toggles individual rows; Command-A
      selects all visible rows. Tabs describe every selected asset.
- [ ] The results toolbar's Batch Rename opens for the selection. Preview Replace, regex, Clear
      Existing Name, both edge trims, range/origin, prefix, insert/index, and suffix.
- [ ] Check collisions, extension protection, separate output, original preservation,
      revalidation statuses, cancellation, and closing during a copy on test assets.
- [ ] Save a rename preset to slot 1–5, close/reopen, and reload it.
- [ ] Edit rules opens a separate window. Done/close and reopening retain edits;
      saving clears Unsaved changes. Custom profile controls expand automatically.
- [ ] Compact/Full details retain selection and metadata; count buttons filter
      correctly; Issues explains failures directly in the table.
- [ ] Naming's test field stays visible while scrolling. Advanced controls expand
      and collapse without changing the configured rules or rename operations.
- [ ] Export CSV outside the input folder; verify all files/checks are included.
- [ ] Existing output paths and output inside the audio folder are rejected.
- [ ] Scan a larger library: window stays responsive; cancellation restores controls
      and leaves export disabled for the partial scan.
- [ ] Editing rules after a scan disables export until rescanning.
- [ ] Resize the window; rules remain scrollable and scan/export buttons remain visible.
- [ ] Automated suite reports 168 passing tests with no skips when Tk and both
      optional backend tools are available on the Mac.

## Code map

| Module | Responsibility |
| --- | --- |
| `validator.py` | Existing validation engine/CLI; adds shared `Rules.from_dict()` and versioned-profile loading |
| `naming_rules.py` | Validated preset settings, generated naming pattern and example |
| `project_profiles.py` | Versioned/legacy profile parsing and exclusive new-file saves |
| `scan_worker.py` | Background scan events, file-boundary cancellation, output path checks |
| `gui.py` | Tk widgets, rule editing, main-thread updates, details and dialogs |
| `result_views.py` | Metadata columns and multi-asset context text from existing check results |
| `batch_rename.py` | Filename transforms, collision preview, verified copies, revalidation, worker and presets |
| `rename_dialog.py` | Batch Rename popup, controls, previews, preset slots, and copy progress |
| `ui_widgets.py` | Reusable expandable advanced-setting sections |

The separate `conversion/` backend supports metadata inspection and read-only plans;
it is not connected to this GUI and cannot write converted audio. App packaging,
conversion, auditioning, loudness checks, and category-specific audio rules remain
outside this implementation.

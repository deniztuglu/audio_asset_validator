# Audio Asset Validator — Codex Handoff (October 22, 2026)

## Project vision
Build a dependable, user-friendly **desktop audio asset preflight and conversion application** for game-audio teams, technical sound designers, sound designers, and audio implementation pipelines. The app should let non-programmers choose audio folders, configure studio/project rules with dropdowns and checkboxes, validate batches, understand problems, and safely produce compliant converted copies. Long-term, aim for a viable distributed product, not just a portfolio demo.

**Product principle:** preserve original recordings. Validation remains read-only. Any future renaming or conversion must show a preview/plan and write only to a separate, user-selected output directory by default, with collision checks, error handling, and revalidation.

## Source of truth and transfer notes
- The **existing GitHub repository** is the source of truth for current V1 code and any newer commits. Inspect it before changing anything.
- There is also an optional `Audio_Asset_Validator_Part2_GUI_Starter.zip` prototype created in ChatGPT. Codex cannot assume that this ZIP or this ChatGPT conversation is accessible through GitHub. To use it, the user must upload the ZIP to Codex or copy its contents into a working branch/repo.
- **Do not blindly overwrite files in the repository with the ZIP.** Compare files/diffs first, preserve any later user work, and implement small, reviewable changes.

## V1 status (from the supplied files)
- Pure Python CLI `validator.py`, with `Rules` dataclass and structured `Check` / `AssetResult` output.
- Recursive deterministic scanning, explicitly handles symlinks and errors.
- Six validation areas: naming full-match regex, readable uncompressed PCM WAV, sample rate, allowed bit depths, allowed channels, duration.
- JSON configuration, CSV export, useful exit statuses, and original-file protection.
- Test/demo system (`tests/test_validator.py`, `tools/generate_demo.py`). 28 V1 tests were reported passing in the previous session; independently rerun locally.
- Existing defaults: prefix `SFX`, sample rate 48000 Hz, 16/24-bit, mono/stereo, minimum duration 0.05 seconds. These are *example project rules*, not universal audio standards.
- V1 recognizes uncompressed integer PCM WAV only (with Python version-dependent support for some WAVE_FORMAT_EXTENSIBLE variants). Other encodings are unsupported, not implicitly converted.

## Optional V2 GUI starter — what's included
The previously prepared ZIP includes:
- `gui.py`: Tkinter GUI (choose folder, edit rules, scan in background thread, inspect results, save/load profiles, export CSV).
- `naming_rules.py`: naming regex builder with allowed `SFX`, `DX`, `FLY` prefixes, capitalization modes, and 1–4 variation digits.
- Updated `validator.py`: factors JSON validation through `Rules.from_dict()`; CLI remains the engine.
- `tests/test_v2.py`: 9 extra tests; previous session reported 37 total passing. Rerun on the user's Mac.
- `profiles/SFX_Default.json`, `profiles/DX_Dialogue_Examples.json` and `README_PART2.md`.
- Demo data should produce 2 pass, 1 warning, 7 error files under default rules.

**Prototype limitations:** naming dropdowns can't reconstruct arbitrary saved regex UI choices; no progress/cancel; not packaged/codesigned/notarized; source files must never be modified; no conversion yet. Tkinter compatibility on the user's Mac must be tested, not assumed.

## Current phase — Part 2: configurable GUI and rules profiles
Aim for a clean, working, testable first GUI. Prefer to reuse/refine the prototype if its implementation is compatible with the repository; otherwise preserve the same behavior in an appropriate architecture.

Acceptance criteria:
1. Run CLI with unchanged behavior and test coverage.
2. Launch GUI on macOS; browse to any audio folder (including nested folders).
3. Users can select SFX/DX/FLY naming prefixes and casing rules (uppercase, lowercase, flexible), choose 1–4 variation digits; advanced editable regex remains possible.
4. Users can set sample rate, allowed bit depths, channels, and minimum duration through UI controls.
5. Save/load versioned project profiles (JSON), supporting existing V1 profiles without breaking them.
6. Show per-file statuses and actionable explanations with actual/expected values; export a CSV report.
7. Run scans without freezing the UI; surface errors clearly.
8. All V1 tests still pass; add focused tests for configuration, naming presets, and GUI-driven interactions where feasible.
9. Don't modify/rename/convert input audio in V2.

Preferred execution: first inspect repo status and supplied V2 prototype, summarize differences, and propose a small implementation plan. Then implement one reviewable phase at a time. Use Git branches/commits or a PR so the user can compare changes before merging.

## Next phase — Part 3: safe converter
Build this **after** the GUI/rules workflow is solid.
- Prefer a mature conversion backend such as FFmpeg; inspect actual installation/dependency needs and licenses before packaging.
- Begin with PCM WAV -> PCM WAV sample-rate and bit-depth conversion; avoid pretending a header change performs actual resampling.
- Present a dry-run table: input path, output path, detected settings, target settings, planned operation, collision/unsupported warnings.
- Copy into a separate destination tree; never overwrite originals; forbid output folder same as / nested dangerously within input unless safe exclusions are implemented.
- Validate conversion settings, check disk space/path conflicts, capture backend errors, process batches with progress + cancellation, allow retries, and provide reproducible logs.
- After conversion, automatically revalidate outputs; show before/after checks. Include tests for sample-rate and PCM-depth outcomes, sample integrity, non-destructive behavior, filenames/collisions, and failures.
- Channel remix/downmix, dither, metadata preservation, normalization, alternative formats, lossy audio and fancy audio transforms **must be explicit choices with appropriate warnings**, not hidden defaults.

## Product improvements backlog (prioritized)
**High value:**
- Multiple categories in *one project* (DX, SFX, FLY, UI, AMB, MUS) with category-specific naming, sample-rate, channel, bit-depth, loudness, and duration profiles.
- Reliable rules editor with live filename examples/tests and clear regex escape hatch.
- Previewable safe conversion and optionally rename suggestions (separate explicit user action).
- Reports that identify *how to fix* each issue; per-asset status filters; rescan changed files.

**Later:**
- Clipping, peak, DC offset, leading/trailing silence, loudness (LUFS), and duplicate detection.
- Batch metadata / format preservation and audio audition before/after conversion.
- FMOD Studio / Wwise project rule matching and integrations once the core product is stable.
- Cross-platform distribution (macOS and Windows); signing, notarization, updates, packaging, release pipeline, and appropriate third-party license notices.

## Architectural decisions to preserve
- Keep the **validation engine separate from the GUI**; maintain a CLI for studio automation and CI pipelines.
- Typed structured rule/result models rather than GUI-only state.
- Make project-specific standards configurable: no universal assumption that all SFX must be stereo or 48 kHz, etc.
- Treat unsupported or malformed media honestly; don't claim pass or lossless conversion without verification.
- Avoid silently destructive changes. Backwards compatibility with config files matters.
- Keep dependencies lean for the validator and tests. Isolate audio conversion dependencies and packaging concerns.
- Prioritize performance on large folders: stream/inspect metadata where possible, incremental scans, background execution, progress, cancellation.

## Commands (verify against the actual repository layout)
```bash
python3 -m tkinter
python3 -m unittest discover -s tests -v
python3 gui.py
python3 validator.py demo_audio --config config.example.json
```

## Initial Codex request
1. Confirm the current repository files, tests, branch, and whether the optional V2 starter has been imported.
2. Diff any V2 starter against the actual repo rather than replacing working files.
3. Explain the smallest safe sequence to reach the Part 2 acceptance criteria.
4. Implement Part 2 in small increments with tests and GUI validation, preserving V1 behavior.
5. Don't implement the converter until Part 2 is verified and the user requests it.
6. Report changed files, tests run/results, any limitations, and what to test manually on macOS.

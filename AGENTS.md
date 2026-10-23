# Audio Asset Validator — Codex instructions

Build a reliable desktop preflight tool for game-audio assets. Preserve the existing CLI; the GUI is a frontend to the same validation rules, not a second validator.

## Where to look
- `validator.py`: rules, scanning, validation, CSV, CLI.
- `gui.py`, `naming_rules.py`, `profiles/`: V2 GUI and rules, **if present**.
- `tests/`: regression tests; `README.md`: run commands and supported formats.
- `CODEX_HANDOFF_Audio_Asset_Validator.md`: product roadmap; consult for a new phase or architecture decision, **not every small edit**.

## Non-negotiable behavior
- Validation is read-only. Never delete, rename, or overwrite source audio or existing reports.
- Future conversion must require a user-approved destination, preview planned outputs, avoid collisions, preserve originals, and revalidate output. Do not implement conversion unless specifically requested.
- Rules are project-specific, not universal audio standards. Keep old JSON profiles and CLI behavior compatible unless a change is explicitly approved.
- Report unsupported/corrupt encodings as unsupported; do not silently claim successful conversion or validation.
- Keep the engine independent of Tkinter and other UI dependencies. Prefer focused changes and minimal dependencies.

## Efficient execution
- Inspect Git status and only the files relevant to the task; search for symbols before reading broad directories. Respect existing uncommitted changes.
- For a clear task, implement it to the stated acceptance criteria without stopping just to restate a plan. For larger/ambiguous work, give a brief plan first.
- Prefer the smallest testable patch; avoid unrelated refactors, new abstractions, or extra documentation.
- Run relevant local tests and fix failures caused by the change. Run the full `python3 -m unittest discover -s tests -v` suite for shared rules/CLI changes and before completing a major milestone. Tests use disposable assets and may run without additional approval.
- For GUI changes, report what needs manual verification on macOS if no GUI display is available in the environment; never claim it was visually checked when it wasn't.
- Do not push, merge, publish, or install new dependencies without an explicit request.
- Finish with a compact report: changed files, test outcome, any limitations, and **one** next suggested action. Do not paste full source files or long logs unless asked.

#!/usr/bin/env python3
"""Desktop rules editor and read-only preflight. Run with: python3 gui.py."""

from __future__ import annotations

from pathlib import Path
from queue import Empty
import re
import sys
try:
    import tkinter as tk
    from tkinter import filedialog, font, messagebox, ttk
except ImportError:
    raise SystemExit("Tkinter is unavailable. Install a Python build with Tk support; see README_PART2.md.")

from naming_rules import CASING, PREFIXES, NamingPreset, normalize_prefix
from project_profiles import ProjectProfile
from result_views import METADATA, check_details, display_value, issue_summary, metadata_details, metadata_values, selection_overview
from scan_worker import ScanWorker, output_destination
from ui_widgets import Disclosure
from validator import AssetResult, Rules, write_csv


FILTERS = {"All files": None, "Errors": "ERROR", "Warnings": "WARNING", "Passed": "PASS"}


class ValidatorApp:
    def __init__(self, root: tk.Tk | tk.Toplevel):
        self.root = root
        self.worker: ScanWorker | None = None
        self.rename_dialog = None
        self.busy = False
        self.complete = False
        self.stale = False
        self.dirty = False
        self._applying = False
        self._closed = False
        self._variable_traces: list[tuple[tk.Variable, str]] = []
        self._scan_root: Path | None = None
        self._scan_profile = ProjectProfile()
        self.results: list[AssetResult] = []
        self.counts = {"PASS": 0, "WARNING": 0, "ERROR": 0}
        self._editable: list[tuple[ttk.Widget, str]] = []
        self._preset_controls: list[ttk.Widget] = []
        self._prefix_buttons: list[ttk.Widget] = []
        self._rule_canvases: list[tk.Canvas] = []
        self.folder = tk.StringVar(master=root)
        self.profile_name = tk.StringVar(master=root)
        self.naming_mode = tk.StringVar(master=root, value="preset")
        self.prefixes: dict[str, tk.BooleanVar] = {}
        self.prefix_text = tk.StringVar(master=root)
        self.capitalization = tk.StringVar(master=root)
        self.variation_digits = tk.StringVar(master=root)
        self.regex = tk.StringVar(master=root)
        self.sample_rate = tk.StringVar(master=root)
        self.depths = {depth: tk.BooleanVar(master=root) for depth in (8, 16, 24, 32)}
        self.other_depths = tk.StringVar(master=root)
        self.channels = {count: tk.BooleanVar(master=root) for count in (1, 2)}
        self.other_channels = tk.StringVar(master=root)
        self.minimum_duration = tk.StringVar(master=root)
        self.test_filename = tk.StringVar(master=root, value="SFX_Footstep_Concrete_01.wav")
        self.preview = tk.StringVar(master=root)
        self.example = tk.StringVar(master=root)
        self.summary = tk.StringVar(master=root, value="Choose an audio folder, review the rules, then scan.")
        self.profile_status = tk.StringVar(master=root, value="Default profile : save a copy for your project.")
        self.active_profile = tk.StringVar(master=root)
        self.rule_summary = tk.StringVar(master=root)
        self.scan_notice = tk.StringVar(master=root)
        self.digit_example = tk.StringVar(master=root)
        self.result_filter = tk.StringVar(master=root, value="All files")
        self.table_view = tk.StringVar(master=root, value="Compact")
        self.selection_note = tk.StringVar(master=root, value="No assets selected")
        self._build_window()
        for variable in (self.profile_name, self.naming_mode, self.capitalization,
                         self.variation_digits, self.regex, self.sample_rate,
                         self.other_depths, self.other_channels, self.minimum_duration,
                         *self.depths.values(), *self.channels.values()):
            self._trace(variable, self._form_changed)
        self._trace(self.folder, self._folder_changed)
        self._trace(self.test_filename, lambda *_: self._update_preview())
        self.apply_profile(ProjectProfile())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if sys.platform == "darwin" and isinstance(root, tk.Tk):
            self.root.createcommand("::tk::mac::Quit", self.close)
        modifier = "Command" if sys.platform == "darwin" else "Control"
        for window in (self.root, self.rules_window):
            window.bind(f"<{modifier}-o>", lambda _: self.load_profile())
            window.bind(f"<{modifier}-s>", lambda _: self.save_profile())
            window.bind(f"<{modifier}-r>", lambda _: self.start_scan())
        self.tree.bind(f"<{modifier}-a>", self._select_all)
        self._poll_id = self.root.after(40, self._poll)

    def _trace(self, variable: tk.Variable, callback) -> None:
        self._variable_traces.append((variable, variable.trace_add("write", callback)))

    def _edit(self, widget: ttk.Widget, state: str = "normal") -> ttk.Widget:
        self._editable.append((widget, state))
        return widget

    def _build_window(self) -> None:
        width = min(1240, self.root.winfo_screenwidth() - 60)
        height = min(880, self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(980, width), min(680, height))
        style = ttk.Style(self.root)
        if sys.platform.startswith("linux") and "clam" in style.theme_names():
            style.theme_use("clam")
        self._title_font = font.nametofont("TkDefaultFont", root=self.root).copy()
        self._title_font.configure(size=20, weight="bold")
        self._heading_font = font.nametofont("TkDefaultFont", root=self.root).copy()
        self._heading_font.configure(size=12, weight="bold")
        style.configure("Title.TLabel", font=self._title_font)
        style.configure("Heading.TLabel", font=self._heading_font)
        style.configure("Treeview", rowheight=26)
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(4, weight=1)
        ttk.Label(container, text="Audio Asset Validator", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(container, text="Choose a folder, check your rules, then scan. Original files stay untouched.").grid(row=1, column=0, sticky="w", pady=(2, 12))
        source = ttk.LabelFrame(container, text="Audio folder", padding=10)
        source.grid(row=2, column=0, sticky="ew")
        self._edit(ttk.Entry(source, textvariable=self.folder)).pack(side="left", fill="x", expand=True)
        self._edit(ttk.Button(source, text="Browse...", command=self.choose_folder)).pack(side="left", padx=(8, 0))
        self.scan_button = ttk.Button(source, text="Scan folder", command=self.start_scan)
        self.scan_button.pack(side="left", padx=(12, 0))
        self.cancel_button = ttk.Button(source, text="Cancel scan", command=self.cancel_scan)
        profile = ttk.LabelFrame(container, text="Active project profile", padding=10)
        profile.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        profile.columnconfigure(0, weight=1)
        ttk.Label(profile, textvariable=self.active_profile, style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        self.edit_rules_button = self._edit(ttk.Button(profile, text="Edit rules...", command=self.open_rules))
        self.edit_rules_button.grid(row=0, column=1, rowspan=2, padx=(12, 0))
        self._edit(ttk.Button(profile, text="Load profile...", command=self.load_profile)).grid(row=0, column=2, rowspan=2, padx=(8, 0))
        rule_label = ttk.Label(profile, textvariable=self.rule_summary, wraplength=850)
        rule_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        profile.bind("<Configure>", lambda event: rule_label.configure(wraplength=max(240, event.width - 290)))
        self._build_rules_editor()
        results = ttk.Frame(container)
        results.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        toolbar = ttk.Frame(results)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="Validation results", style="Heading.TLabel").pack(side="left")
        selector = ttk.Combobox(toolbar, textvariable=self.table_view, values=("Full details", "Compact"),
                                width=12, state="readonly")
        selector.pack(side="right")
        selector.bind("<<ComboboxSelected>>", lambda _: self._set_table_view())
        ttk.Label(toolbar, text="Table view:").pack(side="right", padx=(0, 6))
        filters = ttk.Frame(results)
        filters.pack(fill="x", pady=(0, 8))
        self.filter_buttons = {}
        for label in FILTERS:
            button = ttk.Radiobutton(filters, text=label, value=label, variable=self.result_filter,
                                     command=self._refresh_results, style="Toolbutton")
            button.pack(side="left", padx=(0, 6))
            self.filter_buttons[label] = button
        self.notice_label = ttk.Label(results, textvariable=self.scan_notice, foreground="#9c3f00")
        self.actions_bar = filters
        ttk.Label(filters, textvariable=self.selection_note).pack(side="left", padx=(12, 0))
        self.rename_button = ttk.Button(filters, text="Batch Rename...", command=self.open_batch_rename, state="disabled")
        self.rename_button.pack(side="left", padx=(12, 0))
        self.export_button = ttk.Button(filters, text="Export report (all files)...", command=self.export_csv, state="disabled")
        self.export_button.pack(side="right")
        toggle = "Command" if sys.platform == "darwin" else "Ctrl"
        ttk.Label(results, text=f"Shift-click: range  ·  {toggle}-click: individual files  ·  {toggle}-A: all visible").pack(anchor="w", pady=(0, 6))
        split = ttk.Panedwindow(results, orient="vertical")
        split.pack(fill="both", expand=True)
        list_frame = ttk.Frame(split)
        detail_frame = ttk.Frame(split)
        split.add(list_frame, weight=3)
        split.add(detail_frame, weight=2)
        self.tree = ttk.Treeview(list_frame, columns=("status", "file", "issues", *(key for key, _ in METADATA)),
                                 show="headings", selectmode="extended")
        self.tree.heading("status", text="Status")
        self.tree.heading("file", text="File / relative path")
        self.tree.column("status", width=75, stretch=False)
        self.tree.column("file", width=280, minwidth=180)
        self.tree.heading("issues", text="Issues / why it needs attention")
        self.tree.column("issues", width=360, minwidth=240)
        widths = {"format": 85, "sample_rate": 90, "bit_depth": 75, "channels": 90, "duration": 85}
        for key, label in METADATA:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=widths[key], minwidth=60, stretch=False)
        self.tree.tag_configure("ERROR", foreground="#a12622")
        self.tree.tag_configure("WARNING", foreground="#875500")
        self._set_table_view()
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._show_details())
        self.context_tabs = ttk.Notebook(detail_frame)
        self.context_tabs.pack(fill="both", expand=True)
        self.overview = self._context_tab("Overview")
        self.metadata = self._context_tab("Audio metadata")
        self.details = self._context_tab("Validation checks")
        summary_label = ttk.Label(container, textvariable=self.summary, wraplength=1100)
        summary_label.grid(row=5, column=0, sticky="w", pady=(8, 0))
        container.bind("<Configure>", lambda event: summary_label.configure(wraplength=max(200, event.width)))
        footer = ttk.Frame(container)
        self.scan_footer = footer
        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.pack(fill="x", expand=True)
        self._update_filter_counts()

    def _build_rules_editor(self) -> None:
        self.rules_window = tk.Toplevel(self.root)
        self.rules_window.withdraw()
        self.rules_window.title("Project rules")
        self.rules_window.transient(self.root)
        self.rules_window.geometry(f"600x{min(790, self.root.winfo_screenheight() - 100)}")
        self.rules_window.minsize(540, min(620, self.root.winfo_screenheight() - 100))
        self.rules_window.protocol("WM_DELETE_WINDOW", self.rules_window.withdraw)
        rules = ttk.Frame(self.rules_window, padding=16)
        rules.pack(fill="both", expand=True)
        ttk.Label(rules, text="Project rules", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(rules, text="Changes apply immediately. Scan again to refresh existing results.", wraplength=520).pack(anchor="w", pady=(4, 12))
        ttk.Label(rules, text="Profile name").pack(anchor="w")
        self._edit(ttk.Entry(rules, textvariable=self.profile_name)).pack(fill="x", pady=(4, 8))
        buttons = ttk.Frame(rules)
        buttons.pack(fill="x")
        self._edit(ttk.Button(buttons, text="Load profile...", command=self.load_profile)).pack(side="left")
        self._edit(ttk.Button(buttons, text="Save profile as...", command=self.save_profile)).pack(side="left", padx=8)
        ttk.Label(rules, textvariable=self.profile_status, wraplength=520).pack(anchor="w", pady=(8, 12))
        tabs = ttk.Notebook(rules)
        tabs.pack(fill="both", expand=True)
        self._audio_controls(self._scrollable_tab(tabs, "Audio rules"))
        self._naming_controls(self._scrollable_tab(tabs, "Naming rules", with_preview=True))
        ttk.Button(rules, text="Done", command=self.rules_window.withdraw).pack(anchor="e", pady=(12, 0))

    def open_rules(self) -> None:
        if self.busy:
            return
        self.rules_window.deiconify()
        self.rules_window.lift()
        self.rules_window.focus_set()

    def _set_table_view(self) -> None:
        self.tree.configure(displaycolumns=("status", "file", "issues") if self.table_view.get() == "Compact" else "#all")

    def _update_filter_counts(self) -> None:
        for label, status in FILTERS.items():
            count = self.counts[status] if status else len(self.results)
            self.filter_buttons[label].configure(text=f"{label} ({count})")

    def _context_tab(self, title: str) -> tk.Text:
        frame = ttk.Frame(self.context_tabs)
        self.context_tabs.add(frame, text=title)
        text = tk.Text(frame, wrap="word", height=10, width=50, state="disabled",
                       font="TkDefaultFont", padx=6, pady=6)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return text

    def _scrollable_tab(self, notebook: ttk.Notebook, title: str, with_preview: bool = False) -> ttk.Frame:
        tab = ttk.Frame(notebook)
        notebook.add(tab, text=title)
        if with_preview:
            self.naming_preview = ttk.LabelFrame(tab, text="Try a filename — no file is changed", padding=8)
            self.naming_preview.pack(side="bottom", fill="x", pady=(8, 0))
        canvas = tk.Canvas(tab, width=470, height=350, highlightthickness=0,
                           background=ttk.Style(self.root).lookup("TFrame", "background"))
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content = ttk.Frame(canvas, padding=12)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self._rule_canvases.append(canvas)

        def wheel(event):
            if (canvas.winfo_ismapped() and canvas.winfo_rootx() <= event.x_root < canvas.winfo_rootx() + canvas.winfo_width()
                    and canvas.winfo_rooty() <= event.y_root < canvas.winfo_rooty() + canvas.winfo_height()):
                delta = -event.delta if sys.platform == "darwin" else -int(event.delta / 120)
                canvas.yview_scroll(delta, "units")
                return "break"
            return None

        notebook.winfo_toplevel().bind("<MouseWheel>", wheel, add="+")
        return content

    def _audio_controls(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Sample rate (Hz)").pack(anchor="w")
        self._edit(ttk.Combobox(parent, textvariable=self.sample_rate,
                               values=(22050, 44100, 48000, 88200, 96000, 192000))).pack(fill="x", pady=(4, 12))
        ttk.Label(parent, text="Allowed bit depths").pack(anchor="w")
        depths = ttk.Frame(parent)
        depths.pack(fill="x", pady=(4, 6))
        for depth in (16, 24):
            self._edit(ttk.Checkbutton(depths, text=f"{depth}-bit", variable=self.depths[depth])).pack(side="left", padx=(0, 12))
        ttk.Label(parent, text="Allowed channels").pack(anchor="w")
        channels = ttk.Frame(parent)
        channels.pack(fill="x", pady=(4, 6))
        for count, variable in self.channels.items():
            self._edit(ttk.Checkbutton(channels, text={1: "Mono (1)", 2: "Stereo (2)"}[count], variable=variable)).pack(side="left", padx=(0, 12))
        ttk.Label(parent, text="Minimum duration (seconds)").pack(anchor="w")
        self._edit(ttk.Entry(parent, textvariable=self.minimum_duration)).pack(fill="x", pady=(4, 12))
        ttk.Label(parent, text="Short audio produces a warning. Empty audio is an error.\n"
                  "Supported format: uncompressed integer PCM WAV.", wraplength=480).pack(anchor="w", pady=(0, 12))
        self.audio_advanced = Disclosure(parent, "advanced audio settings")
        self.audio_advanced.pack(fill="x")
        advanced = self.audio_advanced.content
        ttk.Label(advanced, text="Additional allowed bit depths").pack(anchor="w")
        depths = ttk.Frame(advanced)
        depths.pack(fill="x", pady=(4, 8))
        for depth in (8, 32):
            self._edit(ttk.Checkbutton(depths, text=f"{depth}-bit", variable=self.depths[depth])).pack(side="left", padx=(0, 12))
        ttk.Label(advanced, text="Other bit depths (comma-separated)").pack(anchor="w")
        self._edit(ttk.Entry(advanced, textvariable=self.other_depths)).pack(fill="x", pady=(4, 12))
        ttk.Label(advanced, text="Additional channel counts (e.g. 6, 8)").pack(anchor="w")
        self._edit(ttk.Entry(advanced, textvariable=self.other_channels)).pack(fill="x", pady=(4, 12))

    def _naming_controls(self, parent: ttk.Frame) -> None:
        self._edit(ttk.Radiobutton(parent, text="Build a naming rule", value="preset", variable=self.naming_mode)).pack(anchor="w", pady=(0, 10))
        ttk.Label(parent, text="Allowed prefixes (select one or more)").pack(anchor="w")
        self.prefix_frame = ttk.Frame(parent)
        self.prefix_frame.pack(fill="x", pady=(4, 8))
        for column in range(2):
            self.prefix_frame.columnconfigure(column, weight=1)
        self._replace_prefixes(PREFIXES, ("SFX",))
        ttk.Label(parent, text="Prefix to add or remove (e.g. AMB or VO_EN)", wraplength=330).pack(anchor="w")
        prefix_editor = ttk.Frame(parent)
        prefix_editor.pack(fill="x", pady=(4, 6))
        self.prefix_entry = self._edit(ttk.Entry(prefix_editor, textvariable=self.prefix_text, width=12))
        self.prefix_entry.pack(side="left", fill="x", expand=True)
        self.prefix_add_button = self._edit(ttk.Button(prefix_editor, text="Add", command=self.add_prefix))
        self.prefix_add_button.pack(side="left", padx=(6, 4))
        self.prefix_remove_button = self._edit(ttk.Button(prefix_editor, text="Remove", command=self.remove_prefix))
        self.prefix_remove_button.pack(side="left")
        self._preset_controls.extend((self.prefix_entry, self.prefix_add_button, self.prefix_remove_button))
        self.prefix_entry.bind("<Return>", lambda _: self.add_prefix())
        ttk.Label(parent, text="Add selects the new prefix. Remove deletes the entered prefix.\n"
                  "Click a checkbox to copy its name into the field.", wraplength=330).pack(anchor="w", pady=(0, 10))
        for label, variable, values in (("Word casing", self.capitalization, CASING),
                                        ("Number suffix digits", self.variation_digits, (1, 2, 3, 4))):
            ttk.Label(parent, text=label).pack(anchor="w")
            widget = self._edit(ttk.Combobox(parent, textvariable=variable, values=values, state="readonly"), "readonly")
            widget.pack(fill="x", pady=(4, 10))
            self._preset_controls.append(widget)
        ttk.Label(parent, textvariable=self.digit_example).pack(anchor="w", pady=(0, 8))
        ttk.Label(parent, text="Prefixes remain uppercase; word casing is configurable.\n"
                  "WAV extension is case-insensitive.", wraplength=330).pack(anchor="w", pady=(0, 10))
        ttk.Label(self.naming_preview, textvariable=self.example, wraplength=500).pack(anchor="w")
        ttk.Entry(self.naming_preview, textvariable=self.test_filename).pack(fill="x", pady=4)
        ttk.Label(self.naming_preview, textvariable=self.preview, wraplength=500).pack(anchor="w")
        self.naming_advanced = Disclosure(parent, "custom naming pattern (advanced)")
        self.naming_advanced.pack(fill="x", pady=(12, 0))
        advanced = self.naming_advanced.content
        self._edit(ttk.Radiobutton(advanced, text="Use a regular expression instead", value="advanced", variable=self.naming_mode)).pack(anchor="w")
        ttk.Label(advanced, text="Full filename regex").pack(anchor="w", pady=(8, 0))
        self.regex_entry = self._edit(ttk.Entry(advanced, textvariable=self.regex))
        self.regex_entry.pack(fill="x", pady=(4, 10))

    def _replace_prefixes(self, available, selected) -> None:
        """Rebuild checkbox choices and retire their old Tcl callbacks on the UI thread."""
        old_variables = {id(variable) for variable in self.prefixes.values()}
        for variable, trace_id in self._variable_traces:
            if id(variable) in old_variables:
                variable.trace_remove("write", trace_id)
        self._variable_traces = [(variable, trace_id) for variable, trace_id in self._variable_traces
                                 if id(variable) not in old_variables]
        old_buttons = set(self._prefix_buttons)
        self._editable = [(widget, state) for widget, state in self._editable if widget not in old_buttons]
        self._preset_controls = [widget for widget in self._preset_controls if widget not in old_buttons]
        for button in self._prefix_buttons:
            button.destroy()
        self._prefix_buttons = []
        self.prefixes = {}
        for index, prefix in enumerate(available):
            variable = tk.BooleanVar(master=self.root, value=prefix in selected)
            self.prefixes[prefix] = variable
            self._trace(variable, self._form_changed)
            button = self._edit(ttk.Checkbutton(self.prefix_frame, text=prefix, variable=variable,
                                               command=lambda name=prefix: self.prefix_text.set(name)))
            button.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 10), pady=2)
            self._prefix_buttons.append(button)
            self._preset_controls.append(button)

    def add_prefix(self) -> None:
        if self.busy or self.naming_mode.get() != "preset":
            return
        try:
            prefix = normalize_prefix(self.prefix_text.get())
            if prefix in self.prefixes:
                raise ValueError(f"{prefix} already exists. Check its box to allow it, or enter another prefix.")
        except ValueError as error:
            messagebox.showerror("Cannot add prefix", str(error), parent=self.root)
            return
        selected = [name for name, variable in self.prefixes.items() if variable.get()]
        self._replace_prefixes((*self.prefixes, prefix), (*selected, prefix))
        self.prefix_text.set(prefix)
        self._form_changed()
        self.prefix_entry.focus_set()
        self.prefix_entry.selection_range(0, "end")

    def remove_prefix(self) -> None:
        if self.busy or self.naming_mode.get() != "preset":
            return
        try:
            prefix = normalize_prefix(self.prefix_text.get())
            if prefix not in self.prefixes:
                raise ValueError(f"{prefix} is not in this profile's prefix list.")
        except ValueError as error:
            messagebox.showerror("Cannot remove prefix", str(error), parent=self.root)
            return
        available = [name for name in self.prefixes if name != prefix]
        selected = [name for name, variable in self.prefixes.items() if name != prefix and variable.get()]
        self._replace_prefixes(available, selected)
        self.prefix_text.set("")
        self._form_changed()

    def _preset(self) -> NamingPreset:
        try:
            digits = int(self.variation_digits.get())
        except ValueError:
            raise ValueError("choose 1-4 variation digits") from None
        values = {"prefixes": [p for p, var in self.prefixes.items() if var.get()],
                  "capitalization": self.capitalization.get(), "variation_digits": digits}
        if tuple(self.prefixes) != PREFIXES:
            values["available_prefixes"] = list(self.prefixes)
        return NamingPreset.from_dict(values)

    @staticmethod
    def _numbers(text: str) -> list[int]:
        if not text.strip():
            return []
        try:
            return [int(part.strip()) for part in text.split(",")]
        except ValueError:
            raise ValueError("other bit depths/channel counts must be comma-separated integers") from None

    def profile_from_controls(self) -> ProjectProfile:
        preset = self._preset() if self.naming_mode.get() == "preset" else None
        try:
            rate = int(self.sample_rate.get())
            duration = float(self.minimum_duration.get())
        except ValueError:
            raise ValueError("sample rate must be an integer and duration must be a number") from None
        rules = Rules.from_dict({
            "naming_pattern": preset.pattern() if preset else self.regex.get(),
            "sample_rate": rate,
            "bit_depths": sorted(set([d for d, var in self.depths.items() if var.get()]
                                     + self._numbers(self.other_depths.get()))),
            "channels": sorted(set([c for c, var in self.channels.items() if var.get()]
                                    + self._numbers(self.other_channels.get()))),
            "min_duration_seconds": duration,
        })
        profile = ProjectProfile(self.profile_name.get().strip(), rules, preset)
        profile.to_dict()
        return profile

    def apply_profile(self, profile: ProjectProfile) -> None:
        profile.to_dict()  # Validate before touching any current controls.
        self._applying = True
        try:
            self.profile_name.set(profile.name)
            self.naming_mode.set("preset" if profile.naming else "advanced")
            preset = profile.naming or NamingPreset()
            available = preset.available_prefixes
            if available is None:
                available = tuple(dict.fromkeys((*PREFIXES, *preset.prefixes)))
            self._replace_prefixes(available, preset.prefixes)
            self.prefix_text.set("")
            self.capitalization.set(preset.capitalization)
            self.variation_digits.set(str(preset.variation_digits))
            self.regex.set(profile.rules.naming_pattern)
            self.sample_rate.set(str(profile.rules.sample_rate))
            for depth, variable in self.depths.items():
                variable.set(depth in profile.rules.bit_depths)
            self.other_depths.set(", ".join(str(d) for d in profile.rules.bit_depths if d not in self.depths))
            for count, variable in self.channels.items():
                variable.set(count in profile.rules.channels)
            self.other_channels.set(", ".join(str(c) for c in profile.rules.channels if c not in self.channels))
            self.minimum_duration.set(str(profile.rules.min_duration_seconds))
        finally:
            self._applying = False
        self.dirty = False
        self.profile_status.set("Profile ready. Save a copy for your project.")
        if profile.naming is None:
            self.naming_advanced.set_expanded(True)
        if self.other_depths.get() or self.other_channels.get() or self.depths[8].get() or self.depths[32].get():
            self.audio_advanced.set_expanded(True)
        self._invalidate_results()
        self._sync_controls()
        self._update_preview()
        self._update_title()

    def _form_changed(self, *_args) -> None:
        if self._applying:
            return
        self.dirty = True
        self.profile_status.set("Unsaved changes. Save profile as... to keep them.")
        if self.naming_mode.get() == "advanced":
            self.naming_advanced.set_expanded(True)
        self._invalidate_results()
        self._sync_controls()
        self._update_preview()
        self._update_title()

    def _folder_changed(self, *_args) -> None:
        self._invalidate_results()
        self._sync_controls()

    def _invalidate_results(self) -> None:
        if self.results:
            self.stale = True
            self.summary.set("Folder or rules changed : scan again. The displayed results are from the previous scan.")

    def _update_title(self) -> None:
        self.root.title(f"Audio Asset Validator : {self.profile_name.get()}{' *' if self.dirty else ''}")
        self.active_profile.set(f"{self.profile_name.get() or 'Unnamed profile'}{'  ·  Unsaved changes' if self.dirty else ''}")
        try:
            profile = self.profile_from_controls()
            rules = profile.rules
            naming = (f"Prefixes: {', '.join(profile.naming.prefixes)} · {profile.naming.variation_digits}-digit number suffix"
                      if profile.naming else "Custom naming pattern")
            self.rule_summary.set(
                f"PCM WAV · {display_value('sample_rate', f'{rules.sample_rate} Hz')} · "
                f"{' / '.join(str(depth) for depth in rules.bit_depths)}-bit · "
                f"{display_value('channels', ', '.join(map(str, rules.channels)))}\n"
                f"{naming} · Minimum duration: {rules.min_duration_seconds:g} s")
        except (ValueError, OverflowError) as error:
            self.rule_summary.set(f"Rules need attention: {error}")

    def _update_preview(self) -> None:
        digits = self.variation_digits.get()
        self.digit_example.set(f"Example number suffix: {'1'.zfill(int(digits))}" if digits in ("1", "2", "3", "4") else "Choose 1–4 number suffix digits")
        try:
            if self.naming_mode.get() == "preset":
                preset = self._preset()
                pattern = preset.pattern()
                self._applying = True
                try:
                    self.regex.set(pattern)
                finally:
                    self._applying = False
                self.example.set(f"Example: {preset.example()}")
            else:
                pattern = self.regex.get()
                self.example.set("Custom regex is preserved exactly when saving and loading.")
            if not pattern:
                raise ValueError("enter a nonempty naming pattern")
            matches = re.fullmatch(pattern, self.test_filename.get()) is not None
            self.preview.set("PASS : filename matches." if matches else "NO MATCH : filename does not match.")
        except (ValueError, re.error) as error:
            self.example.set("")
            self.preview.set(f"Naming rule needs attention: {error}")

    def _sync_controls(self) -> None:
        for widget, state in self._editable:
            widget.configure(state="disabled" if self.busy else state)
        if not self.busy:
            for widget in self._preset_controls:
                widget.configure(state=("readonly" if isinstance(widget, ttk.Combobox) else "normal")
                                 if self.naming_mode.get() == "preset" else "disabled")
            self.regex_entry.configure(state="normal" if self.naming_mode.get() == "advanced" else "readonly")
        self.scan_button.configure(state="disabled" if self.busy else "normal")
        self.cancel_button.configure(state="normal" if self.busy else "disabled")
        if self.busy:
            self.cancel_button.pack(side="left", padx=(8, 0))
            self.scan_footer.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        else:
            self.cancel_button.pack_forget()
            self.scan_footer.grid_remove()
        notice = ("Rules or folder changed — scan again. Displayed results are from the previous scan." if self.stale
                  else "Incomplete scan — scan again before exporting or renaming." if self.worker and not self.busy and not self.complete else "")
        self.scan_notice.set(notice)
        if notice:
            self.notice_label.pack(fill="x", before=self.actions_bar, pady=(0, 8))
        else:
            self.notice_label.pack_forget()
        self.export_button.configure(state="normal" if self.complete and not self.stale and not self.busy else "disabled")
        self.rename_button.configure(state="normal" if self.complete and not self.stale and not self.busy and self.tree.selection() else "disabled")

    def open_batch_rename(self) -> None:
        if not self.complete or self.stale or self.busy or not self.tree.selection():
            return
        if self.rename_dialog and not self.rename_dialog._closed:
            self.rename_dialog.window.lift()
            return
        from rename_dialog import BatchRenameDialog
        sources = tuple(self._scan_root / asset.file for asset in self.selected_assets())
        self.rename_dialog = BatchRenameDialog(self.root, self._scan_root, sources, self._scan_profile.rules)

    def choose_folder(self) -> None:
        if self.busy:
            return
        selected = filedialog.askdirectory(parent=self.root, title="Choose audio assets folder")
        if selected:
            self.folder.set(selected)

    def load_profile(self) -> None:
        if self.busy:
            return
        parent = self.rules_window if self.rules_window.winfo_viewable() else self.root
        if self.dirty and not messagebox.askyesno("Unsaved profile", "Discard unsaved profile changes and load another profile?", parent=parent):
            return
        selected = filedialog.askopenfilename(parent=parent, title="Load project profile",
                                              initialdir=Path(__file__).parent / "profiles",
                                              filetypes=[("JSON profiles", "*.json")])
        if not selected:
            return
        try:
            profile = ProjectProfile.load(Path(selected))
            self.apply_profile(profile)
            self.profile_status.set(f"Loaded: {Path(selected).name}")
        except (OSError, ValueError) as error:
            messagebox.showerror("Cannot load profile", str(error), parent=parent)

    def save_profile(self) -> None:
        if self.busy:
            return
        parent = self.rules_window if self.rules_window.winfo_viewable() else self.root
        try:
            profile = self.profile_from_controls()
            selected = filedialog.asksaveasfilename(parent=parent, title="Save profile as a new file",
                                                   defaultextension=".json", initialfile="project-profile.json",
                                                   filetypes=[("JSON profiles", "*.json")], confirmoverwrite=False)
            if not selected:
                return
            source = Path(self.folder.get()) if self.folder.get().strip() else None
            destination = output_destination(Path(selected), ".json", source)
            profile.save(destination)
            self.dirty = False
            self.profile_status.set(f"Saved: {destination.name}")
            self._update_title()
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot save profile", str(error), parent=parent)

    def start_scan(self) -> None:
        if self.busy:
            return
        try:
            if not self.folder.get().strip():
                raise ValueError("choose an audio folder first")
            folder = Path(self.folder.get()).expanduser().resolve()
            if not folder.is_dir():
                raise ValueError("choose an existing audio folder")
            profile = self.profile_from_controls()
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot start scan", str(error), parent=self.root)
            return
        self._scan_root, self._scan_profile = folder, profile
        self.results = []
        self.counts = {"PASS": 0, "WARNING": 0, "ERROR": 0}
        self._update_filter_counts()
        self.tree.delete(*self.tree.get_children())
        self._set_details("")
        self.busy, self.complete, self.stale = True, False, False
        self.summary.set(f"Finding files : {profile.name}")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self._sync_controls()
        self.worker = ScanWorker(folder, profile.rules)
        self.worker.start()

    def cancel_scan(self) -> None:
        if self.worker and self.busy:
            self.worker.cancel()
            self.cancel_button.configure(state="disabled")
            self.summary.set("Cancelling : the current file or folder discovery will finish first.")

    def _poll(self) -> None:
        if self._closed:
            return
        if self.worker:
            # Limit widget work per tick so clicks and redraws still get event-loop time.
            for _ in range(50):
                try:
                    event = self.worker.events.get_nowait()
                except Empty:
                    break
                if event.kind == "started":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=max(1, event.total), value=0)
                elif event.kind == "asset" and event.asset is not None:
                    self.results.append(event.asset)
                    self.counts[event.asset.status] += 1
                    self._insert_asset(len(self.results) - 1, event.asset)
                    self.progress.configure(value=event.completed)
                    self.summary.set(f"Scanned {event.completed}/{event.total} : {self._counts_text()}")
                elif event.kind in ("finished", "cancelled", "error"):
                    self.busy = False
                    self.complete = event.kind == "finished"
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=event.completed)
                    if self.complete:
                        self.summary.set(f"Complete : {self._scan_profile.name}: {self._counts_text()}")
                    elif event.kind == "cancelled":
                        self.summary.set(f"Cancelled after {event.completed}/{event.total} files. Partial results cannot be exported.")
                    else:
                        self.summary.set("Scan failed. Partial results cannot be exported.")
                        messagebox.showerror("Scan failed", event.message, parent=self.root)
                    self._sync_controls()
                    if self.tree.get_children() and not self.tree.selection():
                        self.tree.selection_set(self.tree.get_children()[0])
                    break
            self._update_filter_counts()
        self._poll_id = self.root.after(40, self._poll)

    def _counts_text(self) -> str:
        return (f"{len(self.results)} files: {self.counts['PASS']} passed, "
                f"{self.counts['WARNING']} with warnings, {self.counts['ERROR']} with errors.")

    def _insert_asset(self, index: int, asset: AssetResult) -> None:
        wanted = FILTERS[self.result_filter.get()]
        if wanted is None or asset.status == wanted:
            self.tree.insert("", "end", iid=str(index), values=(asset.status, asset.file, issue_summary(asset), *metadata_values(asset)), tags=(asset.status,))

    def _refresh_results(self) -> None:
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        self._set_details("")
        for index, asset in enumerate(self.results):
            self._insert_asset(index, asset)
        visible = self.tree.get_children()
        retained = [item for item in visible if item in selected]
        if retained or visible:
            self.tree.selection_set(retained or [visible[0]])
        self._show_details()

    def _set_details(self, text: str) -> None:
        for widget in (self.details, self.overview, self.metadata):
            self._fill_text(widget, text)

    @staticmethod
    def _fill_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def selected_assets(self) -> list[AssetResult]:
        return [self.results[int(item)] for item in sorted(self.tree.selection(), key=int)]

    def _select_all(self, _event=None) -> str:
        self.tree.selection_set(self.tree.get_children())
        self._show_details()
        return "break"

    def _show_details(self) -> None:
        assets = self.selected_assets()
        self.selection_note.set(f"{len(assets)} selected")
        self._fill_text(self.overview, selection_overview(assets, self._scan_profile.name))
        self._fill_text(self.metadata, metadata_details(assets))
        self._fill_text(self.details, check_details(assets))
        self._sync_controls()

    def export_csv(self) -> None:
        if not self.complete or self.stale or self.busy:
            return
        selected = filedialog.asksaveasfilename(parent=self.root, title="Export full scan as a new CSV",
                                               defaultextension=".csv", initialfile="preflight.csv",
                                               filetypes=[("CSV reports", "*.csv")], confirmoverwrite=False)
        if not selected:
            return
        try:
            path = output_destination(Path(selected), ".csv", self._scan_root)
            write_csv(path, self.results)
            self.summary.set(f"CSV exported: {path} : all files and checks included, regardless of filter.")
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot export CSV", str(error), parent=self.root)

    def close(self) -> None:
        if self._closed:
            return
        if self.rename_dialog and not self.rename_dialog._closed:
            if self.rename_dialog.busy:
                self.rename_dialog.request_close(on_closed=self.close)
                return
            self.rename_dialog.request_close()
        if self.dirty and not messagebox.askyesno("Unsaved profile", "Close without saving your profile changes?", parent=self.root):
            return
        self._closed = True
        if self.worker:
            self.worker.cancel()
        self.progress.stop()
        self.root.after_cancel(self._poll_id)
        for variable, trace_id in self._variable_traces:
            variable.trace_remove("write", trace_id)
        self._variable_traces.clear()
        self.root.destroy()


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as error:
        print(f"Cannot open the desktop window: {error}\n"
              "Use a graphical desktop with Tk support; see README_PART2.md.", file=sys.stderr)
        return 2
    ValidatorApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

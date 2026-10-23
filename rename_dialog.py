"""Batch Rename dialog: filename tools, five presets, preview, and verified copies."""

from dataclasses import fields
import os
from pathlib import Path
from queue import Empty
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from batch_rename import RenameOptions, RenamePlan, RenamePresetStore, RenameWorker, build_plan
from result_views import check_details
from ui_widgets import Disclosure
from validator import Rules


def preset_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Audio Asset Validator"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / "Audio Asset Validator"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "audio-asset-validator"
    return base / "rename_presets.json"


class BatchRenameDialog:
    def __init__(self, parent, input_root: Path, sources: tuple[Path, ...], rules: Rules,
                 store: RenamePresetStore | None = None):
        self.window = tk.Toplevel(parent)
        self.window.title("Batch Rename")
        width, height = min(1280, self.window.winfo_screenwidth() - 70), min(880, self.window.winfo_screenheight() - 90)
        self.window.geometry(f"{width}x{height}")
        self.window.minsize(min(1000, width), min(680, height))
        self.window.transient(parent)
        self.input_root, self.sources, self.rules = input_root, sources, rules
        self.store = store or RenamePresetStore(preset_path())
        self.plan: RenamePlan | None = None
        self._executed_plan: RenamePlan | None = None
        self.worker: RenameWorker | None = None
        self.outcome = None
        self.busy = False
        self._closed = False
        self._applying = False
        self._closing = False
        self._close_callback = None
        self._controls = []
        self._traces = []
        self._copied = {}
        self.variables = {}
        for field in fields(RenameOptions):
            if type(field.default) is bool:
                variable = tk.BooleanVar(master=self.window, value=field.default)
            else:
                variable = tk.StringVar(master=self.window, value=str(field.default))
            self.variables[field.name] = variable
        self.output = tk.StringVar(master=self.window)
        self.preset_name = tk.StringVar(master=self.window, value="Factory default")
        self.slot = 1
        self.status = tk.StringVar(master=self.window, value=f"{len(sources)} selected. Choose an output folder, edit options, then preview.")
        self._build()
        self._show_originals()
        for variable in (*self.variables.values(), self.output):
            self._traces.append((variable, variable.trace_add("write", self._edited)))
        self._sync()
        self.window.protocol("WM_DELETE_WINDOW", self.request_close)
        self.window.update_idletasks()
        self.window.grab_set()
        self._poll_id = self.window.after(40, self._poll)

    def _control(self, widget, enabled=lambda: True, state="normal"):
        self._controls.append((widget, enabled, state))
        return widget

    def _flag(self, name):
        return bool(self.variables[name].get())

    def _build(self):
        root = ttk.Frame(self.window, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        ttk.Label(root, text="Batch Rename - create renamed copies", font="TkHeadingFont").grid(row=0, column=0, sticky="w")
        ttk.Label(root, text="Original files stay untouched. Extensions and subfolders are preserved. No audio conversion.").grid(row=1, column=0, sticky="w", pady=(4, 10))
        split = ttk.Panedwindow(root, orient="horizontal")
        split.grid(row=2, column=0, sticky="nsew")
        options = ttk.Frame(split)
        preview = ttk.Frame(split, padding=(12, 0, 0, 0))
        split.add(options, weight=0)
        split.add(preview, weight=1)
        ttk.Label(options, text="Rename preset").pack(anchor="w", pady=(0, 4))
        combo = self._control(ttk.Combobox(options, textvariable=self.preset_name, state="readonly",
                                          values=("Factory default", *(f"Preset {i}" for i in range(1, 6)))), state="readonly")
        self.preset_selector = combo
        combo.pack(fill="x", pady=(0, 4))
        combo.bind("<<ComboboxSelected>>", lambda _: self.load_preset(0 if self.preset_name.get() == "Factory default" else int(self.preset_name.get()[-1])))
        self.save_preset_button = self._control(ttk.Button(options, text="Save options to Preset 1", command=self.save_preset))
        self.save_preset_button.pack(fill="x", pady=(0, 8))
        scroll_frame = ttk.Frame(options)
        scroll_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(scroll_frame, width=405, height=400, highlightthickness=0,
                           background=ttk.Style(self.window).lookup("TFrame", "background"))
        scroll = ttk.Scrollbar(scroll_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        form = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))

        def wheel(event):
            if (canvas.winfo_rootx() <= event.x_root < canvas.winfo_rootx() + canvas.winfo_width()
                    and canvas.winfo_rooty() <= event.y_root < canvas.winfo_rooty() + canvas.winfo_height()):
                delta = -event.delta if sys.platform == "darwin" else -int(event.delta / 120)
                canvas.yview_scroll(delta, "units")
                return "break"
            return None

        self.window.bind("<MouseWheel>", wheel, add="+")
        replace = self._section(form, "Replace", "replace_enabled")
        self._entry(replace, 1, "Find", "find", lambda: self._flag("replace_enabled") and not self._flag("clear_existing"))
        self._entry(replace, 2, "Replace / new base", "replacement", lambda: self._flag("replace_enabled"))
        self._check(replace, 3, "Clear Existing Name", "clear_existing", lambda: self._flag("replace_enabled"))
        self.replace_advanced = Disclosure(replace, "advanced replace options")
        self.replace_advanced.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._check(self.replace_advanced.content, 0, "Regular Expressions", "use_regex", lambda: self._flag("replace_enabled") and not self._flag("clear_existing"))
        trim = self._section(form, "Trim", "trim_enabled")
        self._entry(trim, 1, "From Beginning", "trim_begin", lambda: self._flag("trim_enabled"))
        self._entry(trim, 2, "From End", "trim_end", lambda: self._flag("trim_enabled"))
        self.trim_advanced = Disclosure(trim, "character range options (advanced)")
        self.trim_advanced.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        range_controls = self.trim_advanced.content
        range_controls.columnconfigure(1, weight=1)
        self._check(range_controls, 0, "Remove range (inclusive, starts at 1)", "trim_range", lambda: self._flag("trim_enabled"))
        active = lambda: self._flag("trim_enabled") and self._flag("trim_range")
        self._entry(range_controls, 1, "From", "range_start", active)
        self._entry(range_controls, 2, "To", "range_end", active)
        for row, (label, value) in enumerate((("Count From Beginning", False), ("Count From End", True)), 3):
            self._control(ttk.Radiobutton(range_controls, text=label, variable=self.variables["range_from_end"], value=value), active).grid(row=row, column=0, columnspan=2, sticky="w", pady=1)
        add = self._section(form, "Add", "add_enabled")
        for row, label, flag, text in ((1, "Prefix", "add_prefix", "prefix"), (2, "Insert", "add_insert", "insert"), (4, "Suffix", "add_suffix", "suffix")):
            self._control(ttk.Checkbutton(add, text=label, variable=self.variables[flag]), lambda: self._flag("add_enabled")).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            self._control(ttk.Entry(add, textvariable=self.variables[text]), lambda key=flag: self._flag("add_enabled") and self._flag(key)).grid(row=row, column=1, sticky="ew", pady=2)
        self._entry(add, 3, "At index (0-based)", "insert_at", lambda: self._flag("add_enabled") and self._flag("add_insert"))
        ttk.Label(form, text="Order: Replace > Trim ends > Trim range > Prefix > Insert > Suffix.\n"
                  "Clear Existing Name uses Replace as the new base. Operations exclude the extension.", wraplength=380).pack(anchor="w", pady=8)
        ttk.Label(preview, text=f"Preview for {len(self.sources)} selected assets", font="TkHeadingFont").pack(anchor="w", pady=(0, 6))
        table = ttk.Frame(preview)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("source", "target", "status"), show="headings", selectmode="browse")
        for key, title, width in (("source", "Original", 220), ("target", "Proposed relative path", 240), ("status", "Status", 130)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=100, stretch=key != "status")
        yscroll = ttk.Scrollbar(table, command=self.tree.yview)
        xscroll = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._show_row())
        note_frame = ttk.Frame(preview)
        note_frame.pack(fill="both", pady=(8, 0))
        self.notes = tk.Text(note_frame, height=9, width=50, wrap="word", font="TkDefaultFont", state="disabled")
        note_scroll = ttk.Scrollbar(note_frame, command=self.notes.yview)
        self.notes.configure(yscrollcommand=note_scroll.set)
        self.notes.pack(side="left", fill="both", expand=True)
        note_scroll.pack(side="right", fill="y")
        output = ttk.LabelFrame(root, text="Output folder - renamed copies", padding=8)
        output.grid(row=3, column=0, sticky="ew", pady=10)
        self._control(ttk.Entry(output, textvariable=self.output)).pack(side="left", fill="x", expand=True)
        self._control(ttk.Button(output, text="Browse...", command=self.browse_output)).pack(side="left", padx=(8, 0))
        status_label = ttk.Label(root, textvariable=self.status, wraplength=1100)
        status_label.grid(row=4, column=0, sticky="w")
        root.bind("<Configure>", lambda event: status_label.configure(wraplength=max(200, event.width)))
        footer = ttk.Frame(root)
        footer.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.preview_button = ttk.Button(footer, text="Preview names", command=self.preview)
        self.preview_button.pack(side="left")
        self.apply_button = ttk.Button(footer, text="Create renamed copies", command=self.apply)
        self.apply_button.pack(side="left", padx=6)
        self.cancel_button = ttk.Button(footer, text="Cancel copy", command=self.cancel)
        self.cancel_button.pack(side="left")
        ttk.Button(footer, text="Close", command=self.request_close).pack(side="left", padx=(6, 0))

    def _section(self, parent, label, variable):
        frame = ttk.Frame(parent, padding=8, relief="groove", borderwidth=1)
        frame.pack(fill="x", pady=(0, 6))
        frame.columnconfigure(1, weight=1)
        self._check(frame, 0, label, variable)
        return frame

    def _check(self, parent, row, label, variable, enabled=lambda: True):
        self._control(ttk.Checkbutton(parent, text=label, variable=self.variables[variable]), enabled).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)

    def _entry(self, parent, row, label, variable, enabled):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
        self._control(ttk.Entry(parent, textvariable=self.variables[variable], width=23), enabled).grid(row=row, column=1, sticky="ew", pady=2)

    def read_options(self):
        values = {}
        for field in fields(RenameOptions):
            value = self.variables[field.name].get()
            if type(field.default) is int:
                try:
                    value = int(value)
                except ValueError:
                    raise ValueError(f"{field.name.replace('_', ' ')} must be an integer") from None
            values[field.name] = value
        return RenameOptions.from_dict(values)

    def _edited(self, *_):
        if self._applying:
            return
        self.plan = None
        self._executed_plan = None
        self._show_originals()
        self.status.set("Settings changed. Preview again before creating copies.")
        self._sync()

    def _show_originals(self):
        # Keep rows in place so editing preserves selection and scroll position.
        for index, source in enumerate(self.sources):
            original = source.relative_to(self.input_root) if source.is_relative_to(self.input_root) else source
            values = (str(original), "", "Preview required")
            if self.tree.exists(str(index)):
                self.tree.item(str(index), values=values)
            else:
                self.tree.insert("", "end", iid=str(index), values=values)
        if not self.tree.selection() and self.sources:
            self.tree.selection_set("0")
        self._show_row()

    def _sync(self):
        # Active advanced operations are always exposed when restoring a preset.
        if self._flag("use_regex"):
            self.replace_advanced.set_expanded(True)
        if self._flag("trim_range"):
            self.trim_advanced.set_expanded(True)
        for widget, enabled, state in self._controls:
            widget.configure(state=state if not self.busy and enabled() else "disabled")
        self.preview_button.configure(state="disabled" if self.busy else "normal")
        self.apply_button.configure(state="normal" if self.plan and self.plan.can_apply and not self.busy else "disabled")
        self.cancel_button.configure(state="normal" if self.busy else "disabled")

    def load_preset(self, slot):
        if self.busy:
            return
        self.slot = slot or 1
        self.save_preset_button.configure(text=f"Save options to Preset {self.slot}")
        try:
            options = RenameOptions() if slot == 0 else self.store.load().get(str(slot))
            if options is None:
                self.preset_name.set(f"Preset {slot}")
                self.status.set(f"Preset {slot} is empty. Your options are kept. Click Save options to Preset {slot} to store them.")
                return
            self._applying = True
            try:
                for field in fields(RenameOptions):
                    self.variables[field.name].set(getattr(options, field.name))
            finally:
                self._applying = False
            self.preset_name.set("Factory default" if slot == 0 else f"Preset {slot}")
            self._edited()
        except (OSError, ValueError) as error:
            messagebox.showerror("Cannot load rename preset", str(error), parent=self.window)

    def save_preset(self):
        if self.busy:
            return
        try:
            self.store.save(self.slot, self.read_options())
            self.preset_name.set(f"Preset {self.slot}")
            self.status.set(f"Saved rename preset {self.slot}.")
        except (OSError, ValueError) as error:
            messagebox.showerror("Cannot save rename preset", str(error), parent=self.window)

    def browse_output(self):
        selected = filedialog.askdirectory(parent=self.window, title="Choose a separate folder for renamed copies")
        if selected:
            self.output.set(selected)

    def preview(self):
        if self.busy:
            return
        self.plan = None
        self._copied = {}
        self._executed_plan = None
        self._show_originals()
        try:
            if not self.output.get().strip():
                raise ValueError("choose a separate output folder first")
            plan = build_plan(self.input_root, self.sources, Path(self.output.get()), self.read_options(), self.rules)
            self.plan = plan
            for index, row in enumerate(plan.rows):
                target = str(row.destination.relative_to(plan.output_root)) if row.destination.is_relative_to(plan.output_root) else str(row.destination)
                self.tree.item(str(index), values=(str(row.source.relative_to(plan.input_root)), target, row.status))
            blocked = sum(bool(row.errors) for row in plan.rows)
            changed = sum(row.changed for row in plan.rows)
            self.status.set(f"Preview: {changed} changes, {blocked} blocked, {len(plan.rows) - changed} unchanged (skipped). Select rows for details.")
            self._show_row()
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot preview rename", str(error), parent=self.window)
        self._sync()

    def _show_row(self):
        selection = self.tree.selection()
        if not selection:
            return
        # Keep the last preview inspectable after execution, even though applying it
        # again is disabled until another preview.
        plan = self.plan or self._executed_plan
        index = int(selection[0])
        if plan is None:
            text = f"Original: {self.sources[index]}\n\nChoose an output folder and click Preview names to see the proposed name."
        else:
            row = plan.rows[index]
            copied = self._copied.get(row.destination)
            text = f"Original: {row.source}\nOutput: {row.destination}\n{row.status}\n"
            text += "\n".join((*row.errors, *row.warnings))
            if copied:
                text += f"\n\nCopied and SHA-256 verified: {copied.sha256}\n\n{check_details([copied.validation])}"
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", text)
        self.notes.configure(state="disabled")

    def apply(self):
        if self.busy or self.plan is None or not self.plan.can_apply:
            return
        self._executed_plan = self.plan
        self.outcome = None
        self.busy = True
        self.progress.configure(maximum=sum(row.changed for row in self.plan.rows), value=0)
        self.status.set("Creating verified copies. Originals are untouched.")
        self._sync()
        self.worker = RenameWorker(self.plan)
        self.worker.start()

    def cancel(self):
        if self.busy and self.worker:
            self.worker.cancel()
            self.cancel_button.configure(state="disabled")
            self.status.set("Cancelling. Completed copies stay; the incomplete current copy is removed.")

    def _poll(self):
        if self._closed:
            return
        if self.worker:
            for _ in range(50):
                try:
                    event = self.worker.events.get_nowait()
                except Empty:
                    break
                if event.kind == "copied":
                    self._copied[event.copied.destination] = event.copied
                    self.progress.configure(value=len(self._copied))
                    for index, row in enumerate(self._executed_plan.rows):
                        if row.destination == event.copied.destination:
                            self.tree.set(str(index), "status", f"Copied / {event.copied.validation.status}")
                else:
                    self.outcome = event.outcome
                    self.busy = False
                    self.plan = None
                    count = len(self.outcome.copies)
                    needs_review = sum(asset.validation.status != "PASS" for asset in self.outcome.copies)
                    state = "Stopped" if self.outcome.error else "Cancelled" if self.outcome.cancelled else "Complete"
                    self.status.set(f"{state}: {count} verified copies; {needs_review} need validation review. Originals untouched.")
                    self._sync()
                    self._show_row()
                    if self.outcome.error:
                        messagebox.showerror("Batch rename stopped", self.outcome.error, parent=self.window)
                    if self._closing:
                        self._destroy()
                        return
                    break
        if not self._closed:
            self._poll_id = self.window.after(40, self._poll)

    def request_close(self, on_closed=None):
        if self._closed:
            if on_closed:
                on_closed()
            return
        if self.busy:
            if not self._closing and not messagebox.askyesno("Cancel copy?", "Cancel and close when copying stops? Completed copies will remain in the output folder.", parent=self.window):
                return
            self._closing = True
            self._close_callback = on_closed
            self.cancel()
        else:
            self._close_callback = on_closed
            self._destroy()

    def _destroy(self):
        if self._closed:
            return
        self._closed = True
        self.window.after_cancel(self._poll_id)
        for variable, callback in self._traces:
            variable.trace_remove("write", callback)
        self.window.grab_release()
        self.window.destroy()
        if self._close_callback:
            self._close_callback()

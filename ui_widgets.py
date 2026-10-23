"""Small, reusable Tk controls for progressively revealing advanced settings."""

import tkinter as tk
from tkinter import ttk


class Disclosure(ttk.Frame):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.title = title
        self.expanded = tk.BooleanVar(master=self, value=False)
        self.toggle = ttk.Button(self, command=lambda: self.set_expanded(not self.expanded.get()))
        self.toggle.pack(fill="x")
        self.content = ttk.Frame(self, padding=(8, 8, 8, 0))
        self._show()

    def set_expanded(self, expanded):
        self.expanded.set(expanded)
        self._show()

    def _show(self):
        self.toggle.configure(text=f"{'Hide' if self.expanded.get() else 'Show'} {self.title}")
        if self.expanded.get():
            self.content.pack(fill="x")
        else:
            self.content.pack_forget()

    def destroy(self):
        super().destroy()
        # Child widgets refer back to their master. Release our extra references
        # and Tcl variable on the UI thread, before a worker can collect cycles.
        self.toggle = self.content = self.expanded = None

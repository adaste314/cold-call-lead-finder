#!/usr/bin/env python3
"""
Cold-Call Lead Finder - desktop app.

A small window: detect or type your location, pick a radius, hit "Find Leads",
and get a ranked table of nearby businesses with no website or a bad website,
with the issues to use as talking points. Export to CSV or an HTML report.

This is the entry point bundled into the standalone executable.
Run from source with:  python app.py
"""

import os
import queue
import re
import sys
import threading
import webbrowser
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import core


def resource_dir():
    """Where to drop exported files - next to the executable, or cwd."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Cold-Call Lead Finder")
        self.geometry("1040x660")
        self.minsize(820, 500)

        self.leads = []
        self.location_label = ""
        self.radius = 2000
        self.q = queue.Queue()

        self._build_ui()
        self.after(100, self._drain_queue)

    # -- UI ------------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 6}
        bar = ttk.Frame(self)
        bar.pack(fill="x", **pad)

        ttk.Label(bar, text="Location:").pack(side="left")
        self.loc_entry = ttk.Entry(bar, width=34)
        self.loc_entry.pack(side="left", padx=(4, 10))
        self.loc_entry.insert(0, "")
        self._placeholder(self.loc_entry, "blank = use my location")

        ttk.Label(bar, text="Radius (m):").pack(side="left")
        self.radius_entry = ttk.Entry(bar, width=7)
        self.radius_entry.insert(0, "2000")
        self.radius_entry.pack(side="left", padx=(4, 10))

        self.find_btn = ttk.Button(bar, text="Find Leads", command=self.on_find)
        self.find_btn.pack(side="left")

        self.csv_btn = ttk.Button(bar, text="Export CSV", command=self.on_csv, state="disabled")
        self.csv_btn.pack(side="right")
        self.html_btn = ttk.Button(bar, text="Open HTML report", command=self.on_html, state="disabled")
        self.html_btn.pack(side="right", padx=(0, 6))

        self.status = ttk.Label(self, text="Enter a location (or leave blank) and click Find Leads.",
                                anchor="w")
        self.status.pack(fill="x", padx=12)
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=12, pady=(2, 6))

        # split: table on top, detail below
        split = ttk.Panedwindow(self, orient="vertical")
        split.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        table_frame = ttk.Frame(split)
        cols = ("rank", "name", "type", "phone", "status", "issues", "score")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        headings = {"rank": "#", "name": "Business", "type": "Type", "phone": "Phone",
                    "status": "Website", "issues": "Issues", "score": "Score"}
        widths = {"rank": 40, "name": 260, "type": 120, "phone": 140,
                  "status": 110, "issues": 60, "score": 60}
        for c in cols:
            self.tree.heading(c, text=headings[c], command=lambda c=c: self._sort_by(c))
            self.tree.column(c, width=widths[c], anchor="w" if c in ("name", "type", "phone") else "center")
        self.tree.tag_configure("nosite", foreground="#c0392b")
        self.tree.tag_configure("badsite", foreground="#b9770e")
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        split.add(table_frame, weight=3)

        detail_frame = ttk.Frame(split)
        self.detail = tk.Text(detail_frame, height=9, wrap="word", font=("Menlo", 12),
                              state="disabled", background="#fbfbfd")
        self.detail.pack(fill="both", expand=True)
        split.add(detail_frame, weight=1)

        self._sort_state = {}

    def _placeholder(self, entry, text):
        entry.insert(0, text)
        entry.config(foreground="grey")

        def focus_in(_):
            if entry.get() == text:
                entry.delete(0, "end")
                entry.config(foreground="black")

        def focus_out(_):
            if not entry.get():
                entry.insert(0, text)
                entry.config(foreground="grey")

        entry.bind("<FocusIn>", focus_in)
        entry.bind("<FocusOut>", focus_out)
        entry._placeholder = text

    # -- actions -------------------------------------------------------------
    def on_find(self):
        try:
            self.radius = max(100, int(self.radius_entry.get()))
        except ValueError:
            messagebox.showerror("Invalid radius", "Radius must be a number of meters.")
            return
        loc = self.loc_entry.get().strip()
        if loc == getattr(self.loc_entry, "_placeholder", None):
            loc = ""
        self.find_btn.config(state="disabled")
        self.csv_btn.config(state="disabled")
        self.html_btn.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self._set_status("Finding your location...")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        threading.Thread(target=self._worker, args=(loc, self.radius), daemon=True).start()

    def _worker(self, loc, radius):
        try:
            if loc:
                lat, lon, label = core.geocode(loc)
            else:
                res = core.locate_by_ip()
                if not res:
                    raise RuntimeError("Could not auto-detect your location. Type a city/address.")
                lat, lon, label = res
            self.q.put(("status", f"Searching businesses near {label}..."))
            businesses = core.normalize(core.fetch_businesses(lat, lon, radius))
            if not businesses:
                raise RuntimeError("No businesses found nearby. Try a larger radius.")
            self.q.put(("status", f"Checking {len(businesses)} businesses' websites..."))
            self.q.put(("progress_max", len(businesses)))

            def prog(done, total):
                self.q.put(("progress", done))

            leads = core.analyze_all(businesses, progress=prog)
            self.q.put(("done", (leads, label)))
        except Exception as e:
            self.q.put(("error", str(e)))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self._set_status(payload)
                elif kind == "progress_max":
                    self.progress.stop()
                    self.progress.config(mode="determinate", maximum=max(1, payload), value=0)
                elif kind == "progress":
                    self.progress.config(value=payload)
                elif kind == "done":
                    self._on_done(*payload)
                elif kind == "error":
                    self._on_error(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _on_done(self, leads, label):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.leads = leads
        self.location_label = label
        no_site = sum(1 for b in leads if not b["website"])
        bad = len(leads) - no_site
        self._set_status(f"{len(leads)} leads near {label}  -  {no_site} with no website, {bad} with a bad website.")
        self._populate()
        self.find_btn.config(state="normal")
        if leads:
            self.csv_btn.config(state="normal")
            self.html_btn.config(state="normal")

    def _on_error(self, msg):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.find_btn.config(state="normal")
        self._set_status("Error: " + msg)
        messagebox.showerror("Error", msg)

    def _populate(self):
        self.tree.delete(*self.tree.get_children())
        for i, b in enumerate(self.leads, 1):
            no_web = not b["website"]
            tag = "nosite" if no_web else "badsite"
            status = "NO WEBSITE" if no_web else "bad site"
            phone = b["phone"] or "— look up —"
            self.tree.insert("", "end", iid=str(i - 1), tags=(tag,), values=(
                i, b["name"], b["category"].title(), phone, status,
                len(b["points"]), b["score"]))

    def on_select(self, _):
        sel = self.tree.selection()
        if not sel:
            return
        b = self.leads[int(sel[0])]
        lines = [b["name"]]
        meta = f'{b["category"].title()}'
        if b["address"]:
            meta += f'  |  {b["address"]}'
        lines.append(meta)
        lines.append(f'Phone: {b["phone"] or "NOT LISTED - look it up before calling"}')
        if b["website"]:
            lines.append(f'Website: {b["website"]}')
        lines.append("")
        lines.append("Talking points:")
        for p in b["points"]:
            lines.append(f"  - {p}")
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(lines))
        self.detail.config(state="disabled")

    def _sort_by(self, col):
        reverse = self._sort_state.get(col, False)
        idx = {"rank": 0, "name": 1, "type": 2, "phone": 3, "status": 4, "issues": 5, "score": 6}[col]
        numeric = col in ("rank", "issues", "score")
        rows = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]

        def key(v):
            return float(v[0]) if numeric and v[0].replace(".", "").isdigit() else v[0].lower()

        rows.sort(key=key, reverse=reverse)
        for pos, (_, k) in enumerate(rows):
            self.tree.move(k, "", pos)
        self._sort_state[col] = not reverse

    def on_csv(self):
        path = filedialog.asksaveasfilename(
            initialdir=resource_dir(), defaultextension=".csv",
            initialfile="cold_call_leads.csv",
            filetypes=[("CSV", "*.csv")])
        if path:
            core.write_csv(self.leads, path)
            self._set_status(f"Saved CSV: {path}")

    def on_html(self):
        path = os.path.join(resource_dir(), "cold_call_leads.html")
        core.write_html(self.leads, path, self.location_label, self.radius)
        webbrowser.open("file://" + os.path.abspath(path))
        self._set_status(f"Opened HTML report: {path}")

    def _set_status(self, text):
        self.status.config(text=text)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()

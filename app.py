#!/usr/bin/env python3
"""
Cold-Call Lead Finder - desktop app.

A small window: detect or type your location, pick a radius, hit "Find Leads",
and get a ranked table of nearby businesses with no website or a bad website,
with the issues to use as talking points. Export to CSV or an HTML report.

This is the entry point bundled into the standalone executable.
Run from source with:  python app.py
"""

import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import core

# Fallback version. Release builds stamp the real tag here (see stamp_version.py),
# and running from source derives it from the git tag - see current_version().
VERSION = "1.0.8"
REPO = "adaste314/cold-call-lead-finder"
UA = "cold-call-lead-finder-app"


def resource_dir():
    """A writable, user-visible directory for exported reports.

    Running from source we use the current directory. In a packaged app we must
    NOT write next to the executable: on macOS an unsigned app is often launched
    from a read-only "translocation" path, and a .app's binary lives inside the
    bundle. Prefer the user's Desktop, then home.
    """
    if getattr(sys, "frozen", False):
        home = os.path.expanduser("~")
        desktop = os.path.join(home, "Desktop")
        return desktop if os.path.isdir(desktop) else home
    return os.getcwd()


def data_dir():
    """A stable, writable folder in the user's home for app data (the Called list)."""
    d = os.path.join(os.path.expanduser("~"), ".cold_call_lead_finder")
    os.makedirs(d, exist_ok=True)
    return d


def called_path():
    return os.path.join(data_dir(), "called.json")


def load_called():
    try:
        with open(called_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_called(called):
    try:
        with open(called_path(), "w", encoding="utf-8") as f:
            json.dump(called, f, indent=2)
    except Exception:
        pass


def lead_key(b):
    """Stable identity for a business, so 'called' persists across searches."""
    name = (b.get("name") or "").strip().lower()
    extra = (b.get("address") or b.get("phone") or "").strip().lower()
    return f"{name}|{extra}"


def _vtuple(v):
    parts = (v or "").lstrip("v").split(".")
    return tuple(int(p) for p in parts if p.isdigit())


def current_version():
    """This build's version. Frozen builds trust the stamped VERSION; from source
    we read the actual git tag so the Update button never reports a stale number."""
    if getattr(sys, "frozen", False):
        return VERSION
    try:
        appdir = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run(["git", "-C", appdir, "describe", "--tags", "--abbrev=0"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().lstrip("v")
    except Exception:
        pass
    return VERSION


# --- GitHub-synced shared called list -------------------------------------
# Stored in the repo at shared/called.json. Download reads it anonymously;
# Upload merges your local list in and writes it back via the Contents API.
SHARED_API = f"https://api.github.com/repos/{REPO}/contents/shared/called.json"
TOKEN_FILE = "token.txt"


def resolve_token():
    """Find a GitHub token for uploading, without one ever being baked in.

    Order: env var, a local token.txt, then `gh auth token` if gh is installed.
    """
    t = os.environ.get("CCLF_GITHUB_TOKEN")
    if t and t.strip():
        return t.strip()
    p = os.path.join(data_dir(), TOKEN_FILE)
    if os.path.isfile(p):
        try:
            s = open(p, encoding="utf-8").read().strip()
            if s:
                return s
        except Exception:
            pass
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def fetch_shared(token=None):
    """Return (data_dict, sha) for the shared list. ({}, None) if it doesn't exist."""
    import base64
    import requests
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(SHARED_API, headers=headers, params={"ref": "main"}, timeout=20)
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    j = r.json()
    try:
        content = base64.b64decode(j.get("content", "")).decode("utf-8") or "{}"
        data = json.loads(content) or {}
    except Exception:
        data = {}
    return data, j.get("sha")


def push_shared(data, sha, token):
    import base64
    import requests
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json",
               "Authorization": f"Bearer {token}"}
    body = {"message": "Update shared called list",
            "content": base64.b64encode(json.dumps(data, indent=2).encode()).decode(),
            "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(SHARED_API, headers=headers, json=body, timeout=25)
    r.raise_for_status()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Cold-Call Lead Finder")
        self.geometry("1040x660")
        self.minsize(820, 500)

        self.leads = []
        self.location_label = ""
        self.radius_mi = 1.5
        self.q = queue.Queue()
        self.called = load_called()
        self._row_biz = {}
        self._called_refresh = None

        self._build_ui()
        self._update_called_btn()
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

        ttk.Label(bar, text="Radius (mi):").pack(side="left")
        self.radius_entry = ttk.Entry(bar, width=6)
        self.radius_entry.insert(0, "1.5")
        self.radius_entry.pack(side="left", padx=(4, 10))

        self.find_btn = ttk.Button(bar, text="Find Leads", command=self.on_find)
        self.find_btn.pack(side="left")

        # Right cluster (packed right-to-left): CSV | HTML | Called | Update
        self.update_btn = ttk.Button(bar, text="Update", command=self.on_update)
        self.update_btn.pack(side="right", padx=(6, 0))
        self.called_btn = ttk.Button(bar, text="Called", command=self.open_called)
        self.called_btn.pack(side="right", padx=(6, 0))
        self.csv_btn = ttk.Button(bar, text="Export CSV", command=self.on_csv, state="disabled")
        self.csv_btn.pack(side="right", padx=(6, 0))
        self.html_btn = ttk.Button(bar, text="Open HTML report", command=self.on_html, state="disabled")
        self.html_btn.pack(side="right", padx=(6, 0))

        self.status = ttk.Label(self, text="Enter a location (or leave blank) and click Find Leads.",
                                anchor="w")
        self.status.pack(fill="x", padx=12)
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=12, pady=(2, 6))

        # split: table on top, detail below
        split = ttk.Panedwindow(self, orient="vertical")
        split.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        table_frame = ttk.Frame(split)
        cols = ("done", "rank", "name", "type", "phone", "status", "issues", "quote", "score")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        headings = {"done": "Called?", "rank": "#", "name": "Business", "type": "Type",
                    "phone": "Phone", "status": "Website", "issues": "Issues",
                    "quote": "Quote", "score": "Score"}
        widths = {"done": 60, "rank": 38, "name": 230, "type": 105, "phone": 130,
                  "status": 100, "issues": 52, "quote": 62, "score": 50}
        for c in cols:
            # the checkbox column isn't sortable; clicking a box marks the lead called
            cmd = (lambda: None) if c == "done" else (lambda c=c: self._sort_by(c))
            self.tree.heading(c, text=headings[c], command=cmd)
            self.tree.column(c, width=widths[c],
                             anchor="w" if c in ("name", "type", "phone") else "center")
        self.tree.tag_configure("nosite", foreground="#c0392b")
        self.tree.tag_configure("badsite", foreground="#b9770e")
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Button-1>", self.on_tree_click)
        split.add(table_frame, weight=3)

        detail_frame = ttk.Frame(split)
        # match the detail pane's background to the business list
        style = ttk.Style(self)
        list_bg = (style.lookup("Treeview", "fieldbackground")
                   or style.lookup("Treeview", "background") or "white")
        self.detail = tk.Text(detail_frame, height=14, wrap="word", font=("Menlo", 12),
                              state="disabled", padx=12, pady=10, borderwidth=0)
        try:
            self.detail.configure(background=list_bg)
        except tk.TclError:
            list_bg = "white"
            self.detail.configure(background="white")
        dsb = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=dsb.set)
        dsb.pack(side="right", fill="y")
        self.detail.pack(side="left", fill="both", expand=True)
        # text styling for the detail pane
        self.detail.tag_configure("title", font=("Menlo", 13, "bold"), spacing3=4)
        self.detail.tag_configure("sub", foreground="#555555", spacing3=6)
        self.detail.tag_configure("h", font=("Menlo", 11, "bold"), spacing1=8, spacing3=2)
        self.detail.tag_configure("s", foreground="#1e8e4e", font=("Menlo", 11, "bold"), spacing1=8, spacing3=2)
        self.detail.tag_configure("w", foreground="#c0392b", font=("Menlo", 11, "bold"), spacing1=8, spacing3=2)
        self.detail.tag_configure("o", foreground="#2d6fd6", font=("Menlo", 11, "bold"), spacing1=8, spacing3=2)
        self.detail.tag_configure("t", foreground="#b9770e", font=("Menlo", 11, "bold"), spacing1=8, spacing3=2)
        self.detail.tag_configure("rec", font=("Menlo", 12, "bold"), foreground="#1e8e4e", spacing1=6)
        self.detail.tag_configure("tier", foreground="#333333")
        split.add(detail_frame, weight=2)

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
            self.radius_mi = max(0.1, float(self.radius_entry.get()))
        except ValueError:
            messagebox.showerror("Invalid radius", "Radius must be a number of miles.")
            return
        meters = int(round(self.radius_mi * 1609.34))
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
        threading.Thread(target=self._worker, args=(loc, meters), daemon=True).start()

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
                elif kind == "update":
                    self._on_update_result(*payload)
                elif kind == "sync":
                    self._on_sync_result(*payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _on_done(self, leads, label):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        for i, b in enumerate(leads, 1):
            b["_rank"] = i
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
        self._row_biz = {}
        shown = 0
        for b in self.leads:
            if lead_key(b) in self.called:
                continue                      # already called - hidden from leads
            iid = str(shown)
            self._row_biz[iid] = b
            shown += 1
            no_web = not b["website"]
            tag = "nosite" if no_web else "badsite"
            status = "NO WEBSITE" if no_web else "bad site"
            phone = b["phone"] or "— look up —"
            self.tree.insert("", "end", iid=iid, tags=(tag,), values=(
                "☐", b.get("_rank", shown), b["name"], b["category"].title(), phone, status,
                len(b["weaknesses"]), f'${b["recommended"][0]}', b["score"]))

    def _insert(self, text, tag=None):
        self.detail.insert("end", text, tag) if tag else self.detail.insert("end", text)

    def on_select(self, _):
        sel = self.tree.selection()
        if not sel:
            return
        b = self._row_biz.get(sel[0])
        if not b:
            return
        tag = "NO WEBSITE" if not b["website"] else "BAD WEBSITE"
        sw = b["swot"]

        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")

        # Header, mirroring the HTML card
        self._insert(f'#{b.get("_rank", "")}  {b["name"]}   [{tag}]   score {b["score"]}\n', "title")
        meta = b["category"].title()
        if b["address"]:
            meta += f'  |  {b["address"]}'
        self._insert(meta + "\n", "sub")
        self._insert(f'Phone:   {b["phone"] or "not listed - look it up before calling"}\n', "sub")
        self._insert(f'Website: {b["website"] or "none"}\n', "sub")

        # SWOT
        for label, key, t in (("STRENGTHS", "strengths", "s"),
                              ("WEAKNESSES", "weaknesses", "w"),
                              ("OPPORTUNITIES", "opportunities", "o"),
                              ("THREATS", "threats", "t")):
            self._insert(label + "\n", t)
            for x in sw[key]:
                self._insert(f"  • {x}\n", "tier")

        # Talking points
        self._insert("TALKING POINTS\n", "h")
        for p in b["points"]:
            self._insert(f"  • {p}\n", "tier")

        # Pricing
        self._insert("SUGGESTED PRICING\n", "h")
        rec_amount, rec_reason = b["recommended"]
        for amt, tname, tdesc in b["price_tiers"]:
            star = "  ← recommended" if amt == rec_amount else ""
            self._insert(f"  ${amt}  {tname}{star}\n", "rec" if amt == rec_amount else "tier")
            self._insert(f"        {tdesc}\n", "tier")
        self._insert(f"→ Pitch ${rec_amount}: {rec_reason}\n", "rec")

        self.detail.config(state="disabled")
        self.detail.yview_moveto(0)

    def _sort_by(self, col):
        reverse = self._sort_state.get(col, False)
        rows = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]

        def key(v):
            raw = v[0].lstrip("$")
            try:
                return (0, float(raw))
            except ValueError:
                return (1, v[0].lower())

        rows.sort(key=key, reverse=reverse)
        for pos, (_, k) in enumerate(rows):
            self.tree.move(k, "", pos)
        self._sort_state[col] = not reverse

    # -- called list ---------------------------------------------------------
    def _active_leads(self):
        """Leads still in play (not marked called)."""
        return [b for b in self.leads if lead_key(b) not in self.called]

    def _update_called_btn(self):
        self.called_btn.config(text=f"Called ({len(self.called)})")

    def on_tree_click(self, event):
        """Click in the 'Called?' box marks that lead called and hides it."""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":   # only the checkbox column
            return
        iid = self.tree.identify_row(event.y)
        b = self._row_biz.get(iid)
        if b:
            self._mark_called(b)
            return "break"

    def _mark_called(self, b):
        self.called[lead_key(b)] = {k: b.get(k) for k in
                                    ("name", "category", "phone", "address", "city")}
        save_called(self.called)
        self._populate()
        self._update_called_btn()
        self._set_status(f'Marked "{b["name"]}" as called - moved to the Called list.')

    def open_called(self):
        win = tk.Toplevel(self)
        win.title("Called businesses")
        win.geometry("660x460")
        win.transient(self)

        info = ttk.Label(win, anchor="w", padding=(10, 8), justify="left",
                         text="Businesses you've called are hidden from the leads list.\n"
                              "Download merges the team's shared list into yours; "
                              "Upload adds your list to the shared one on GitHub.")
        info.pack(fill="x")

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=10)
        cols = ("name", "type", "phone")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for c, t, w in (("name", "Business", 300), ("type", "Type", 130), ("phone", "Phone", 170)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def refresh():
            tv.delete(*tv.get_children())
            for k, v in self.called.items():
                tv.insert("", "end", iid=k, values=(
                    v.get("name", ""), (v.get("category") or "").title(), v.get("phone") or ""))

        def restore():
            for k in tv.selection():
                self.called.pop(k, None)
            save_called(self.called)
            refresh()
            self._populate()
            self._update_called_btn()

        def clear_all():
            if self.called and messagebox.askyesno(
                    "Clear all", "Remove ALL businesses from the Called list?"):
                self.called.clear()
                save_called(self.called)
                refresh()
                self._populate()
                self._update_called_btn()

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=8)
        ttk.Button(btns, text="Restore to leads", command=restore).pack(side="left")
        ttk.Button(btns, text="Clear all", command=clear_all).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Upload", command=self.on_upload).pack(side="right")
        ttk.Button(btns, text="Download", command=self.on_download).pack(side="right", padx=(0, 6))

        # let background sync refresh this window while it's open
        self._called_refresh = refresh
        win.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "_called_refresh", None), win.destroy()))
        refresh()

    def _merge_called(self, data):
        """Merge another person's called list into ours. Returns count added."""
        added = 0
        items = data.values() if isinstance(data, dict) else data
        if not isinstance(items, (list, tuple)) and not isinstance(data, dict):
            return 0
        for v in items:
            if not isinstance(v, dict) or not v.get("name"):
                continue
            k = lead_key(v)
            if k not in self.called:
                self.called[k] = {kk: v.get(kk) for kk in
                                  ("name", "category", "phone", "address", "city")}
                added += 1
        return added

    # -- shared sync (GitHub) ------------------------------------------------
    def on_download(self):
        self._set_status("Downloading the shared called list…")
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        try:
            remote, _ = fetch_shared(resolve_token())
            added = self._merge_called(remote)
            save_called(self.called)
            self.q.put(("sync", ("downloaded", added)))
        except Exception as e:
            self.q.put(("sync", ("error", str(e))))

    def on_upload(self):
        token = resolve_token()
        if not token:
            messagebox.showwarning(
                "GitHub sign-in needed to upload",
                "Downloading works for everyone, but uploading writes to GitHub and "
                "needs a token with repo access.\n\nSet one up once, any of:\n"
                "  • install GitHub CLI and run 'gh auth login', or\n"
                f"  • save the token in {os.path.join(data_dir(), TOKEN_FILE)}, or\n"
                "  • set the CCLF_GITHUB_TOKEN environment variable.")
            return
        self._set_status("Uploading your called list to the shared list…")
        threading.Thread(target=self._upload_worker, args=(token,), daemon=True).start()

    def _upload_worker(self, token):
        try:
            remote, sha = fetch_shared(token)
            before = len(remote)
            for k, v in self.called.items():          # merge local into remote (union)
                if k not in remote:
                    remote[k] = v
            added_remote = len(remote) - before
            pulled = self._merge_called(remote)        # and bring any remote-only into local
            save_called(self.called)
            push_shared(remote, sha, token)
            self.q.put(("sync", ("uploaded", (added_remote, pulled))))
        except Exception as e:
            self.q.put(("sync", ("error", str(e))))

    def _on_sync_result(self, status, info):
        if status == "error":
            self._set_status("Sync failed.")
            messagebox.showerror("Sync failed", info)
            return
        if self._called_refresh:
            self._called_refresh()
        self._populate()
        self._update_called_btn()
        if status == "downloaded":
            self._set_status(f"Download complete - merged {info} new into your Called list.")
        else:  # uploaded
            added_remote, pulled = info
            self._set_status(f"Upload complete - added {added_remote} to the shared list"
                             + (f", pulled {pulled} new to yours." if pulled else "."))

    # -- update --------------------------------------------------------------
    def on_update(self):
        self.update_btn.config(state="disabled")
        self._set_status("Checking for updates…")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            appdir = os.path.dirname(os.path.abspath(__file__))
            git_dir = os.path.join(appdir, ".git")
            if not getattr(sys, "frozen", False) and os.path.isdir(git_dir):
                p = subprocess.run(["git", "-C", appdir, "pull", "--ff-only"],
                                   capture_output=True, text=True, timeout=60)
                out = (p.stdout + "\n" + p.stderr).strip()
                if p.returncode != 0:
                    self.q.put(("update", ("error", out or "git pull failed")))
                elif "Already up to date" in out or "up-to-date" in out:
                    self.q.put(("update", ("current", f"You're already on the latest version (v{current_version()}).")))
                else:
                    self.q.put(("update", ("pulled", out)))
            else:
                import requests
                r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"User-Agent": UA}, timeout=15)
                r.raise_for_status()
                latest = r.json().get("tag_name", "")
                if latest and _vtuple(latest) > _vtuple(current_version()):
                    self.q.put(("update", ("newer", latest)))
                else:
                    self.q.put(("update", ("current", f"You're on the latest version (v{current_version()}).")))
        except Exception as e:
            self.q.put(("update", ("error", str(e))))

    def _on_update_result(self, status, info):
        self.update_btn.config(state="normal")
        if status == "current":
            self._set_status(info)
            messagebox.showinfo("Up to date", info)
        elif status == "pulled":
            self._set_status("Updated from git. Restart to apply.")
            if messagebox.askyesno("Updated",
                                    f"Pulled the latest code:\n\n{info}\n\nRestart the app now?"):
                self._restart()
        elif status == "newer":
            url = f"https://github.com/{REPO}/releases/latest"
            webbrowser.open(url)
            self._set_status(f"New version {info} available - opening the download page.")
            messagebox.showinfo("Update available",
                                f"A newer version ({info}) is available.\n"
                                f"You have v{current_version()}.\n\nOpening the download page.")
        else:  # error
            self._set_status("Update check failed.")
            messagebox.showerror("Update failed", info)

    def _restart(self):
        self.destroy()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def on_csv(self):
        leads = self._active_leads()
        if not leads:
            return
        path = filedialog.asksaveasfilename(
            initialdir=resource_dir(), defaultextension=".csv",
            initialfile="cold_call_leads.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            core.write_csv(leads, path)
            self._set_status(f"Saved CSV: {path}")
        except Exception as e:
            messagebox.showerror("Could not save CSV", str(e))

    def on_html(self):
        leads = self._active_leads()
        if not leads:
            return
        try:
            path = os.path.join(resource_dir(), "cold_call_leads.html")
            core.write_html(leads, path, self.location_label, self.radius_mi)
            opened = webbrowser.open("file://" + os.path.abspath(path))
            if opened:
                self._set_status(f"Opened HTML report: {path}")
            else:
                # No browser could be launched - at least tell the user where it is.
                self._set_status(f"Report saved (open it manually): {path}")
                messagebox.showinfo("Report saved",
                                    f"Couldn't auto-open a browser.\nThe report is here:\n\n{path}")
        except Exception as e:
            messagebox.showerror("Could not open report", str(e))

    def _set_status(self, text):
        self.status.config(text=text)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
WIMU Downloader  v2.2
=====================
Choose the source at start:
  - Smart Station : units connected by USB (original behaviour)
  - USB           : a USB with WIMUS/<tracker>/<month>/*.qul
Then pick a session -> Download.

- Detects WIMU drives by label (WIMU_1 / WIMU 1 / WIMU-1) or by the .qul
  files they contain.
- Groups log files into sessions (new session after a 2 h gap).
- Copies in parallel, verifies every file, skips files already saved.
- Remembers the save folder, warns about missing units, opens the folder
  when done, and writes a log file for troubleshooting.

Standard library only.
"""

import json
import logging
import os
import platform
import queue
import re
import shutil
import string
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ── Settings ──────────────────────────────────────────────────────────────────
APP_NAME        = "WIMU Downloader"
VERSION         = "2.2"
SYSTEM          = platform.system()

LABEL_RE        = re.compile(r"^WIMU[\s_\-]*(\d+)", re.IGNORECASE)
LOG_RE          = re.compile(
    r"^WIMU[\s_\-]*(\d+)-log_(\d{8})-(\d{2}_\d{2}_\d{2})\.qul$", re.IGNORECASE)

SESSION_GAP     = timedelta(hours=2)   # gap that starts a new session
MAX_SPAN        = timedelta(hours=6)   # safety cap on one session's length
SCAN_INTERVAL   = 2.0                  # seconds between background scans
COPY_WORKERS    = 6                    # parallel copies (one per USB device)
DEPTH_LABELLED  = 4                    # folder depth searched on WIMU drives
DEPTH_UNLABELLED = 2                   # depth on other removable drives
ROOT_DIR_NAME   = "WIMUS"              # USB mode: folder at the USB root
DEPTH_TRACKER   = 4                    # USB mode: depth inside each tracker folder
MODE_STATION, MODE_USB = "station", "usb"

HOME            = Path.home()
CONFIG_PATH     = HOME / ".wimu_downloader.json"
LOG_PATH        = HOME / ".wimu_downloader.log"
DEFAULT_DEST    = HOME / "Documents" / "WIMU Sessions"

SKIP_DIRS = {"System Volume Information", "$RECYCLE.BIN", ".Trashes",
             ".Spotlight-V100", ".fseventsd", ".TemporaryItems"}

# ── Logging ───────────────────────────────────────────────────────────────────
try:
    logging.basicConfig(filename=str(LOG_PATH), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
except Exception:
    logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wimu")


# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save config: %s", e)


# ── Data ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LogFile:
    path: Path
    filename: str
    unit: str          # normalised, e.g. "WIMU_14"
    dt: datetime
    size: int
    mtime: float


@dataclass
class Session:
    key: str
    start: datetime
    end: datetime
    records: list

    @property
    def units(self) -> set:
        return {r.unit for r in self.records}

    @property
    def folder_name(self) -> str:
        return f"WIMU_session_{self.start:%Y%m%d_%H%M}-{self.end:%H%M}"


def unit_name(num: str) -> str:
    return f"WIMU_{int(num)}"


def unit_sort_key(unit: str) -> int:
    m = re.search(r"(\d+)", unit)
    return int(m.group(1)) if m else 0


# ── Drive discovery (cross-platform) ──────────────────────────────────────────
if SYSTEM == "Windows":
    import ctypes
    _k32 = ctypes.windll.kernel32
    # Never show "There is no disk in the drive" popups
    _k32.SetErrorMode(0x0001 | 0x8000)

    def _win_drives():
        mask = _k32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if not (mask >> i) & 1 or letter in "AB":
                continue
            root = f"{letter}:\\"
            dtype = _k32.GetDriveTypeW(ctypes.c_wchar_p(root))
            if dtype in (2, 3):            # 2 = removable, 3 = fixed
                yield Path(root), dtype

    def _win_label(root: Path) -> str:
        buf = ctypes.create_unicode_buffer(261)
        ok = _k32.GetVolumeInformationW(ctypes.c_wchar_p(str(root)), buf, 261,
                                        None, None, None, None, 0)
        return buf.value if ok else ""


def candidate_volumes():
    """Return list of (root, label, may_scan_unlabelled)."""
    out = []
    if SYSTEM == "Windows":
        for root, dtype in _win_drives():
            try:
                out.append((root, _win_label(root), dtype == 2))
            except Exception as e:
                log.warning("Label read failed for %s: %s", root, e)
    elif SYSTEM == "Darwin":
        try:
            for e in Path("/Volumes").iterdir():
                if e.is_symlink():          # "Macintosh HD" -> /
                    continue
                out.append((e, e.name, True))
        except OSError:
            pass
    else:
        seen = set()
        for base in (Path("/media"), Path("/run/media"), Path("/mnt")):
            if not base.exists():
                continue
            for p in list(base.glob("*")) + list(base.glob("*/*")):
                try:
                    if p.is_mount() and p not in seen:
                        seen.add(p)
                        out.append((p, p.name, True))
                except OSError:
                    pass
    return out


def find_logs(root: Path, max_depth: int) -> list:
    found = []

    def walk(d, depth):
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith(".") or e.name in SKIP_DIRS:
                        continue
                    try:
                        if e.is_file(follow_symlinks=False):
                            m = LOG_RE.match(e.name)
                            if not m:
                                continue
                            dt = datetime.strptime(f"{m.group(2)} {m.group(3)}",
                                                   "%Y%m%d %H_%M_%S")
                            st = e.stat()
                            found.append(LogFile(Path(e.path), e.name,
                                                 unit_name(m.group(1)), dt,
                                                 st.st_size, st.st_mtime))
                        elif e.is_dir(follow_symlinks=False) and depth < max_depth:
                            walk(e.path, depth + 1)
                    except (OSError, ValueError):
                        continue
        except OSError as ex:
            log.debug("Cannot read %s: %s", d, ex)

    walk(str(root), 1)
    return found


def find_wimus_dir(root: Path):
    """USB mode: the WIMUS folder at the root of a volume (case-insensitive)."""
    try:
        with os.scandir(root) as it:
            for e in it:
                if e.name.upper() == ROOT_DIR_NAME and e.is_dir(follow_symlinks=False):
                    return Path(e.path)
    except OSError:
        pass
    return None


def scan_usb(candidates=None):
    """USB mode. drives = one dict per tracker folder: {root,label,unit,count}.
    Layout: <USB>/WIMUS/<tracker>/<month>/*.qul"""
    drives, records, seen_names = [], [], set()
    vols = candidate_volumes() if candidates is None else candidates
    for root, label, _removable in vols:
        wimus = find_wimus_dir(root)
        if not wimus:
            continue
        try:
            trackers = sorted(p for p in wimus.iterdir()
                              if p.is_dir() and p.name not in SKIP_DIRS
                              and not p.name.startswith("."))
        except OSError as e:
            log.warning("Cannot list %s: %s", wimus, e)
            continue
        for tdir in trackers:
            recs = find_logs(tdir, DEPTH_TRACKER)
            m = LABEL_RE.match(tdir.name)
            if m:
                unit = unit_name(m.group(1))
            elif recs:
                unit = Counter(r.unit for r in recs).most_common(1)[0][0]
            else:
                continue
            drives.append({"root": root, "label": f"{root}  {tdir.name}",
                           "unit": unit, "count": len(recs)})
            for r in recs:
                if r.filename.lower() not in seen_names:
                    seen_names.add(r.filename.lower())
                    records.append(r)
    drives.sort(key=lambda d: unit_sort_key(d["unit"]))
    return drives, records


def scan(mode=MODE_STATION, candidates=None):
    return scan_usb(candidates) if mode == MODE_USB else scan_station(candidates)


def scan_station(candidates=None):
    """Smart Station mode (original). drives = list of dicts {root,label,unit,count}."""
    drives, records, seen_names = [], [], set()
    vols = candidate_volumes() if candidates is None else candidates
    for root, label, removable in vols:
        m = LABEL_RE.match(label or "")
        if not m and not removable:
            continue
        recs = find_logs(root, DEPTH_LABELLED if m else DEPTH_UNLABELLED)
        if not m and not recs:
            continue
        unit = (unit_name(m.group(1)) if m
                else Counter(r.unit for r in recs).most_common(1)[0][0])
        drives.append({"root": root, "label": label or str(root),
                       "unit": unit, "count": len(recs)})
        for r in recs:
            if r.filename.lower() not in seen_names:
                seen_names.add(r.filename.lower())
                records.append(r)
    drives.sort(key=lambda d: unit_sort_key(d["unit"]))
    return drives, records


def group_sessions(records) -> list:
    recs = sorted(records, key=lambda r: r.dt)
    groups, cur = [], []
    for r in recs:
        if cur and (r.dt - cur[-1].dt > SESSION_GAP or r.dt - cur[0].dt > MAX_SPAN):
            groups.append(cur)
            cur = []
        cur.append(r)
    if cur:
        groups.append(cur)
    sessions = [Session(key=g[0].dt.isoformat(), start=g[0].dt,
                        end=g[-1].dt, records=g) for g in groups]
    return sorted(sessions, key=lambda s: s.start, reverse=True)


# ── Copying ───────────────────────────────────────────────────────────────────
def copy_one(rec: LogFile, dest: Path) -> str:
    target = dest / rec.filename
    if target.exists() and target.stat().st_size == rec.size:
        return "skipped"
    tmp = dest / (rec.filename + ".part")
    shutil.copy2(rec.path, tmp)
    if tmp.stat().st_size != rec.size:
        tmp.unlink(missing_ok=True)
        raise IOError("size mismatch after copy (drive disconnected?)")
    os.replace(tmp, target)
    return "copied"


def open_path(p: Path) -> None:
    try:
        if SYSTEM == "Windows":
            os.startfile(str(p))
        elif SYSTEM == "Darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:
        log.warning("Could not open %s: %s", p, e)


# ══════════════════════════════════════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════════════════════════════════════
BG, CARD, BORDER = "#f3f5f8", "#ffffff", "#dfe5ec"
TEXT, MUTED, FAINT = "#18212c", "#5f6b78", "#98a3af"
ACCENT, ACCENT_HI, ACCENT_SOFT = "#0a7cff", "#3a95ff", "#e9f2ff"
GREEN, GREEN_SOFT = "#1d9150", "#e2f5e9"
AMBER, AMBER_SOFT = "#b36b00", "#fff1d9"
RED, RED_SOFT = "#d14343", "#fde6e6"

FONT = {"Windows": "Segoe UI", "Darwin": "Helvetica Neue"}.get(SYSTEM, "DejaVu Sans")


def fs(n):  # Tk on macOS renders slightly smaller
    return n + 2 if SYSTEM == "Darwin" else n


def F(size, weight="normal"):
    return (FONT, fs(size), weight)


def FE(size):
    """Font for emoji icons (Segoe UI lacks them on Windows)."""
    return ("Segoe UI Emoji", fs(size)) if SYSTEM == "Windows" else F(size)


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def set_icon(win) -> None:
    """Window/taskbar icon from icon.ico (next to the script / bundled in the exe)."""
    ico = resource_path("icon.ico")
    if SYSTEM != "Windows" or not os.path.exists(ico):
        return

    def apply():
        try:
            win.iconbitmap(default=ico)   # every window of this app
            win.iconbitmap(ico)
        except Exception as e:
            log.debug("Icon not set: %s", e)

    apply()
    # Windows sometimes ignores the icon until the window is mapped
    win.after(50, apply)
    win.after(400, apply)


def day_label(d) -> str:
    today = datetime.now().date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return d.strftime("%A %d %B %Y")


def span_text(s) -> str:
    if s.end - s.start >= timedelta(minutes=1):
        return f"Units started {s.start:%H:%M} – {s.end:%H:%M}"
    return f"Started {s.start:%H:%M:%S}"


def short_path(p: Path) -> str:
    try:
        return "~/" + str(p.relative_to(HOME)).replace("\\", "/")
    except ValueError:
        return str(p)


class Button(tk.Label):
    """Colour-controllable button (tk.Button ignores colours on macOS)."""

    def __init__(self, master, text, command, kind="primary", big=False):
        super().__init__(master, text=text, cursor="hand2",
                         font=F(13 if big else 10, "bold"),
                         padx=22 if big else 12, pady=12 if big else 6)
        self.kind, self.command, self.enabled, self.hover = kind, command, True, False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", lambda e: self.enabled and self.command())
        self._paint()

    def _set_hover(self, h):
        self.hover = h
        self._paint()

    def _paint(self):
        if not self.enabled:
            bg, fg = BORDER, FAINT
        elif self.kind == "primary":
            bg, fg = (ACCENT_HI if self.hover else ACCENT), "white"
        else:
            bg, fg = (BORDER if self.hover else CARD), TEXT
        self.config(bg=bg, fg=fg, cursor="hand2" if self.enabled else "arrow",
                    highlightthickness=0 if self.kind == "primary" else 1,
                    highlightbackground=BORDER)

    def set(self, text=None, enabled=None):
        if text is not None:
            self.config(text=text)
        if enabled is not None:
            self.enabled = enabled
        self._paint()


class Link(tk.Label):
    def __init__(self, master, text, command, bg=CARD):
        super().__init__(master, text=text, fg=ACCENT, bg=bg, cursor="hand2", font=F(10))
        self.bind("<ButtonRelease-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self.config(font=(FONT, fs(10), "underline")))
        self.bind("<Leave>", lambda e: self.config(font=F(10)))


class ProgressBar(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, height=8, bg=CARD, highlightthickness=0)
        self.frac = 0.0
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, frac):
        self.frac = max(0.0, min(1.0, frac))
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), 8
        if self.frac <= 0:
            return
        self.create_rectangle(0, 0, w, h, fill=BORDER, width=0)
        self.create_rectangle(0, 0, max(4, w * self.frac), h, fill=ACCENT, width=0)


class ScrollArea(tk.Frame):
    def __init__(self, master, bg):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(
            self.win, width=e.width))
        top = self.winfo_toplevel()
        if SYSTEM == "Linux":
            top.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
            top.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
        else:
            top.bind_all("<MouseWheel>", self._wheel)

    def _wheel(self, e):
        if SYSTEM == "Darwin":
            step = -e.delta
        else:
            step = -int(e.delta / 120) or (-1 if e.delta > 0 else 1)
        self.canvas.yview_scroll(step, "units")

    def ensure_visible(self, widget):
        self.update_idletasks()
        total = max(1, self.inner.winfo_height())
        y, h = widget.winfo_y(), widget.winfo_height()
        top, bot = (f * total for f in self.canvas.yview())
        if y < top:
            self.canvas.yview_moveto(y / total)
        elif y + h > bot:
            self.canvas.yview_moveto((y + h - (bot - top)) / total)


class App(tk.Tk):
    def __init__(self, mode=MODE_STATION):
        super().__init__()
        self.mode = mode
        self.back = False
        set_icon(self)
        self.title(f"{APP_NAME}" + ("  ·  USB" if mode == MODE_USB else "  ·  Smart Station"))
        self.geometry("1080x740")
        self.minsize(880, 560)
        self.configure(bg=BG)
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("Vertical.TScrollbar", background=BORDER, troughcolor=BG,
                     bordercolor=BG, arrowcolor=MUTED, lightcolor=BORDER,
                     darkcolor=BORDER, gripcount=0, arrowsize=10)
        st.map("Vertical.TScrollbar", background=[("active", FAINT)])

        self.cfg = load_config()
        self.dest = Path(self.cfg.get("dest", str(DEFAULT_DEST)))
        self.drives, self.sessions = [], []
        self.sel_key = None
        self.cards = {}
        self.tiles = {}
        self.copying = False
        self.last_result = None
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.wake_evt = threading.Event()
        self.force_evt = threading.Event()

        self._build()
        self._render_list()
        self._render_detail()
        threading.Thread(target=self._scanner, daemon=True).start()
        self.after(100, self._poll)
        self.bind("<Up>", lambda e: self._move(-1))
        self.bind("<Down>", lambda e: self._move(1))
        self.bind("<Return>", lambda e: self._download())
        self.protocol("WM_DELETE_WINDOW", self._close)
        log.info("Started v%s on %s (mode=%s)", VERSION, SYSTEM, mode)

    # ── Layout ───────────────────────────────────────────────────────────────
    def _build(self):
        hdr = tk.Frame(self, bg=BG, padx=26, pady=18)
        hdr.pack(fill="x")
        tk.Label(hdr, text="WIMU Downloader", font=F(19, "bold"),
                 fg=TEXT, bg=BG).pack(side="left")
        Button(hdr, "↻  Rescan", self._rescan, kind="secondary").pack(side="right")
        Button(hdr, "←  Source", self._back, kind="secondary").pack(side="right", padx=(0, 8))
        self.pill = tk.Label(hdr, font=F(10, "bold"), padx=12, pady=5)
        self.pill.pack(side="right", padx=12)

        foot = tk.Frame(self, bg=BG, padx=26, pady=10)
        foot.pack(fill="x", side="bottom")
        self.status = tk.Label(foot, text="", font=F(9), fg=FAINT, bg=BG)
        self.status.pack(side="left")
        Link(foot, "log file", lambda: open_path(LOG_PATH), bg=BG).pack(side="right")

        body = tk.Frame(self, bg=BG, padx=26)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=5, uniform="c")
        body.columnconfigure(1, weight=4, uniform="c")
        body.rowconfigure(1, weight=1)

        # Left: session cards
        lh = tk.Frame(body, bg=BG)
        lh.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tk.Label(lh, text="Choose a session", font=F(14, "bold"), fg=TEXT,
                 bg=BG).pack(side="left")
        tk.Label(lh, text="click to select  ·  double-click to download",
                 font=F(9), fg=FAINT, bg=BG).pack(side="right")
        self.scroll = ScrollArea(body, BG)
        self.scroll.grid(row=1, column=0, sticky="nsew", padx=(0, 18))

        # Right: detail panel
        tk.Frame(body, bg=BG).grid(row=0, column=1)
        border = tk.Frame(body, bg=BORDER, padx=1, pady=1)
        border.grid(row=1, column=1, sticky="nsew")
        self.panel = tk.Frame(border, bg=CARD, padx=24, pady=20)
        self.panel.pack(fill="both", expand=True)
        bottom = tk.Frame(self.panel, bg=CARD)
        bottom.pack(fill="x", side="bottom")
        self.result_lbl = tk.Label(bottom, text="", font=F(10, "bold"), bg=CARD,
                                   anchor="w", justify="left", wraplength=360)
        self.result_lbl.pack(fill="x", pady=(0, 8))
        self.bar = ProgressBar(bottom)
        self.bar.pack(fill="x", pady=(0, 10))
        self.dl_btn = Button(bottom, "↓   Download session", self._download, big=True)
        self.dl_btn.pack(fill="x")
        save = tk.Frame(bottom, bg=CARD)
        save.pack(fill="x", pady=(12, 0))
        Link(save, "Open", self._open_dest).pack(side="right")
        tk.Label(save, text="·", fg=FAINT, bg=CARD).pack(side="right", padx=4)
        Link(save, "Change", self._pick_dest).pack(side="right")
        tk.Label(save, text="Saving to", font=F(10), fg=MUTED, bg=CARD).pack(side="left")
        self.dest_lbl = tk.Label(save, text=short_path(self.dest), font=F(10, "bold"),
                                 fg=TEXT, bg=CARD, anchor="w")
        self.dest_lbl.pack(side="left", padx=6, fill="x", expand=True)

        self.detail = tk.Frame(self.panel, bg=CARD)
        self.detail.pack(fill="both", expand=True)

    # ── Session list ─────────────────────────────────────────────────────────
    def _render_list(self):
        inner = self.scroll.inner
        for w in inner.winfo_children():
            w.destroy()
        self.cards = {}
        n = len(self._connected_units())
        usb = self.mode == MODE_USB
        unit_w = "tracker" if usb else "unit"
        self.pill.config(text=(f"●  {n} {unit_w}{'s' if n != 1 else ''} " + ("on USB" if usb else "connected")) if n
                         else ("○  Waiting for USB" if usb else "○  Waiting for Smart Station"),
                         fg=GREEN if n else MUTED, bg=GREEN_SOFT if n else BORDER)

        if not self.sessions:
            box = tk.Frame(inner, bg=BG, pady=80)
            box.pack(fill="x")
            if self.mode == MODE_USB:
                icon, title, sub = (("📂", "No log files found",
                                     "The WIMUS folder doesn't contain any .qul logs.")
                                    if n else
                                    ("🔌", "Connect the USB",
                                     "Plug in the USB with the WIMUS folder.\nSessions will appear here automatically."))
            else:
                icon, title, sub = (("📂", "No log files found",
                                     "The connected units don't contain any WIMU .qul logs.")
                                    if n else
                                    ("🔌", "Connect the Smart Station",
                                     "Plug it in via USB. Your units and sessions\nwill appear here automatically."))
            tk.Label(box, text=icon, font=FE(40), bg=BG).pack()
            tk.Label(box, text=title, font=F(16, "bold"), fg=TEXT, bg=BG).pack(pady=(10, 4))
            tk.Label(box, text=sub, font=F(11), fg=MUTED, bg=BG, justify="center").pack()
            return

        last_day = None
        for i, s in enumerate(self.sessions):
            if s.start.date() != last_day:
                last_day = s.start.date()
                tk.Label(inner, text=day_label(last_day).upper(), font=F(9, "bold"),
                         fg=MUTED, bg=BG, anchor="w").pack(fill="x", pady=(14 if i else 2, 6))
            self._make_card(inner, s, newest=(i == 0))
        self._style_cards()

    def _make_card(self, parent, s, newest):
        outer = tk.Frame(parent, bg=BORDER, padx=2, pady=2, cursor="hand2")
        outer.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(outer, bg=CARD, padx=18, pady=12)
        inner.pack(fill="both", expand=True)

        left = tk.Frame(inner, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        time_lbl = tk.Label(left, text=f"{s.start:%H:%M}", font=F(22, "bold"),
                            fg=TEXT, bg=CARD, anchor="w")
        time_lbl.pack(anchor="w")
        sub = tk.Label(left, text=f"{span_text(s)}   ·   {len(s.records)} files",
                       font=F(10), fg=MUTED, bg=CARD, anchor="w")
        sub.pack(anchor="w")

        right = tk.Frame(inner, bg=CARD)
        right.pack(side="right")
        n = len(self._connected_units())
        got = len(s.units & self._connected_units()) if n else len(s.units)
        complete = not n or got >= n
        tk.Label(right, text=(f"{got} / {n} units" if n else f"{got} units"),
                 font=F(10, "bold"), fg=GREEN if complete else AMBER,
                 bg=GREEN_SOFT if complete else AMBER_SOFT, padx=12, pady=5).pack(anchor="e")
        recolor = [inner, left, right, time_lbl, sub]
        if newest:
            b = tk.Label(right, text="LATEST", font=F(8, "bold"), fg=ACCENT, bg=CARD)
            b.pack(anchor="e", pady=(6, 0))
            recolor.append(b)

        def bind_all(w):
            w.bind("<Button-1>", lambda e, k=s.key: self._select(k))
            w.bind("<Double-Button-1>", lambda e, k=s.key: (self._select(k), self._download()))
            for c in w.winfo_children():
                bind_all(c)
        bind_all(outer)
        self.cards[s.key] = (outer, recolor)

    def _style_cards(self):
        for key, (outer, ws) in self.cards.items():
            sel = key == self.sel_key
            outer.config(bg=ACCENT if sel else BORDER)
            for w in ws:
                w.config(bg=ACCENT_SOFT if sel else CARD)

    def _select(self, key):
        if self.copying or key == self.sel_key:
            return
        self.sel_key = key
        self.last_result = None
        self._style_cards()
        self._render_detail()
        if key in self.cards:
            self.scroll.ensure_visible(self.cards[key][0])

    def _move(self, d):
        keys = [s.key for s in self.sessions]
        if not keys:
            return
        i = keys.index(self.sel_key) if self.sel_key in keys else -1
        self._select(keys[max(0, min(len(keys) - 1, i + d))])

    # ── Detail panel ─────────────────────────────────────────────────────────
    def _connected_units(self) -> set:
        return {d["unit"] for d in self.drives}

    def _selected(self):
        return next((s for s in self.sessions if s.key == self.sel_key), None)

    def _missing(self, s) -> list:
        return sorted(self._connected_units() - s.units, key=unit_sort_key)

    def _render_detail(self):
        for w in self.detail.winfo_children():
            w.destroy()
        self.tiles = {}
        s = self._selected()
        if not s:
            tk.Label(self.detail, text="Select a session on the left", font=F(12),
                     fg=FAINT, bg=CARD).pack(expand=True)
            self._update_controls()
            return

        d = self.detail
        tk.Label(d, text="SELECTED SESSION", font=F(9, "bold"), fg=MUTED,
                 bg=CARD).pack(anchor="w")
        tk.Label(d, text=s.start.strftime("%A %d %B %Y"), font=F(12), fg=MUTED,
                 bg=CARD).pack(anchor="w", pady=(8, 0))
        tk.Label(d, text=f"{s.start:%H:%M}", font=F(30, "bold"), fg=TEXT,
                 bg=CARD).pack(anchor="w")
        tk.Label(d, text=span_text(s), font=F(10), fg=MUTED, bg=CARD).pack(anchor="w")

        missing = self._missing(s)
        chips = tk.Frame(d, bg=CARD)
        chips.pack(anchor="w", pady=(14, 18))
        mb = sum(r.size for r in s.records) / 1e6
        for text, fg, bg in (
                (f"{len(s.units)} units", GREEN if not missing else AMBER,
                 GREEN_SOFT if not missing else AMBER_SOFT),
                (f"{len(s.records)} files", MUTED, BG),
                (f"{mb:.1f} MB" if mb >= 0.1 else f"{mb*1000:.0f} KB", MUTED, BG)):
            tk.Label(chips, text=text, font=F(10, "bold"), fg=fg, bg=bg,
                     padx=10, pady=4).pack(side="left", padx=(0, 6))

        tk.Label(d, text="UNITS", font=F(9, "bold"), fg=MUTED, bg=CARD).pack(anchor="w")
        grid = tk.Frame(d, bg=CARD)
        grid.pack(anchor="w", pady=(6, 8))
        units = sorted(self._connected_units() | s.units, key=unit_sort_key)
        cols = 7
        for i, u in enumerate(units):
            has = u in s.units
            t = tk.Label(grid, text=str(unit_sort_key(u)), width=3, font=F(11, "bold"),
                         fg=GREEN if has else AMBER, bg=GREEN_SOFT if has else AMBER_SOFT,
                         padx=4, pady=6)
            t.grid(row=i // cols, column=i % cols, padx=3, pady=3)
            self.tiles[u] = t

        leg = tk.Frame(d, bg=CARD)
        leg.pack(anchor="w")
        for text, col in (("has file", GREEN), ("no file", AMBER)):
            tk.Label(leg, text="■", fg=col, bg=CARD, font=F(10)).pack(side="left")
            tk.Label(leg, text=text, fg=MUTED, bg=CARD, font=F(9)).pack(side="left", padx=(2, 12))

        if missing:
            tk.Label(d, text=f"⚠  No file from {', '.join(missing)} in this session.",
                     font=F(10), fg=AMBER, bg=CARD, wraplength=360, justify="left"
                     ).pack(anchor="w", pady=(12, 0))
        self._update_controls()

    # ── Destination ──────────────────────────────────────────────────────────
    def _pick_dest(self):
        folder = filedialog.askdirectory(
            title="Choose where to save sessions",
            initialdir=str(self.dest if self.dest.exists() else HOME))
        if folder:
            self.dest = Path(folder)
            self.dest_lbl.config(text=short_path(self.dest))
            self.cfg["dest"] = str(self.dest)
            save_config(self.cfg)

    def _open_dest(self):
        try:
            self.dest.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        open_path(self.dest)

    # ── Scanning ─────────────────────────────────────────────────────────────
    def _scanner(self):
        last_sig = None
        while not self.stop_evt.is_set():
            if not self.copying:
                try:
                    drives, records = scan(self.mode)
                    sig = (tuple((str(d["root"]), d["label"]) for d in drives),
                           tuple(sorted((str(r.path), r.size, r.mtime) for r in records)))
                    if sig != last_sig or self.force_evt.is_set():
                        self.force_evt.clear()
                        last_sig = sig
                        self.q.put(("scan", drives, records))
                except Exception as e:
                    log.exception("Scan failed: %s", e)
            self.wake_evt.wait(SCAN_INTERVAL)
            self.wake_evt.clear()

    def _rescan(self):
        if not self.copying:
            self._set_status("Rescanning…")
            self.force_evt.set()
            self.wake_evt.set()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "scan":
                    self._apply_scan(msg[1], msg[2])
                elif msg[0] == "progress":
                    self._on_progress(*msg[1:])
                elif msg[0] == "done":
                    self._on_done(*msg[1:])
        except queue.Empty:
            pass
        if not self.stop_evt.is_set():
            self.after(100, self._poll)

    def _apply_scan(self, drives, records):
        self.drives = drives
        self.sessions = group_sessions(records)
        keys = [s.key for s in self.sessions]
        if self.sel_key not in keys:
            self.sel_key = keys[0] if keys else None
            self.last_result = None
        self._render_list()
        self._render_detail()
        n = len(self._connected_units())
        self._set_status(f"{len(self.sessions)} session(s) on {n} unit(s)" if n else "")
        if n:
            log.info("Scan: %d drives, %d files, %d sessions", n, len(records), len(keys))

    # ── Download ─────────────────────────────────────────────────────────────
    def _download(self):
        s = self._selected()
        if not s or self.copying:
            return
        missing = self._missing(s)
        if missing and not messagebox.askyesno(
                "Some units have no file",
                f"No file in this session from:\n{', '.join(missing)}\n\n"
                f"Download the other {len(s.records)} file(s) anyway?"):
            return
        dest = self.dest / s.folder_name
        self.copying = True
        self.last_result = None
        self.bar.set(0.001)
        self._update_controls(f"Copying 0 / {len(s.records)}…")
        log.info("Download %s -> %s", s.key, dest)
        threading.Thread(target=self._copy_worker, args=(s, dest), daemon=True).start()

    def _copy_worker(self, s, dest):
        res = {"copied": 0, "skipped": 0, "errors": []}
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            res["errors"].append(f"Cannot create folder: {e}")
            self.q.put(("done", dest, res))
            return
        with ThreadPoolExecutor(max_workers=COPY_WORKERS) as ex:
            futs = {ex.submit(copy_one, r, dest): r for r in s.records}
            for i, f in enumerate(as_completed(futs), 1):
                r = futs[f]
                try:
                    res[f.result()] += 1
                    ok = True
                except Exception as e:
                    res["errors"].append(f"{r.filename}: {e}")
                    log.error("Copy failed %s: %s", r.path, e)
                    ok = False
                self.q.put(("progress", i, len(s.records), r.unit, ok))
        self.q.put(("done", dest, res))

    def _on_progress(self, i, total, unit, ok):
        self.bar.set(i / total)
        t = self.tiles.get(unit)
        if t:
            t.config(bg=GREEN if ok else RED, fg="white")
        self.dl_btn.set(text=f"Copying {i} / {total}…")

    def _on_done(self, dest, res):
        self.copying = False
        c, sk, errs = res["copied"], res["skipped"], res["errors"]
        log.info("Done: copied=%d skipped=%d errors=%d", c, sk, len(errs))
        saved = c + sk
        if errs:
            self.last_result = (f"✗  {len(errs)} file(s) failed. Check the cables and "
                                f"download again (saved files are skipped).", RED)
            messagebox.showerror("Some files failed", "\n".join(errs[:15]) +
                                 ("\n…" if len(errs) > 15 else ""))
        else:
            self.last_result = (f"✓  {saved} file(s) saved to {dest.name}"
                                + (f"  ({sk} were already there)" if sk else ""), GREEN)
        self.bar.set(0)
        self._update_controls()
        if dest.exists():
            open_path(dest)

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _update_controls(self, busy_text=None):
        s = self._selected()
        if self.copying:
            self.dl_btn.set(text=busy_text or self.dl_btn.cget("text"), enabled=False)
        else:
            self.dl_btn.set(text=("↓   Download again" if self.last_result and
                                  self.last_result[1] == GREEN else "↓   Download session"),
                            enabled=bool(s))
        text, col = self.last_result or ("", CARD)
        self.result_lbl.config(text=text, fg=col)

    def _set_status(self, msg):
        self.status.config(text=msg)

    def _back(self):
        if self.copying:
            return
        self.back = True
        self.stop_evt.set()
        self.wake_evt.set()
        self.destroy()

    def _close(self):
        if self.copying and not messagebox.askyesno(
                "Copy in progress", "Files are still copying. Quit anyway?"):
            return
        self.stop_evt.set()
        self.wake_evt.set()
        self.destroy()


class Launcher(tk.Tk):
    """Start screen: choose where the logs come from."""

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("720x400")
        self.minsize(640, 360)
        self.configure(bg=BG)
        self.choice = None
        set_icon(self)
        center = tk.Frame(self, bg=BG)
        center.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(center, text="WIMU Downloader", font=F(20, "bold"), fg=TEXT,
                 bg=BG).pack(pady=(0, 4))
        tk.Label(center, text="Where do you want to download the logs from?",
                 font=F(11), fg=MUTED, bg=BG).pack(pady=(0, 22))
        row = tk.Frame(center, bg=BG)
        row.pack()
        self._card(row, "🔌", "Smart Station",
                   "Units connected through\nthe Smart Station", MODE_STATION)
        self._card(row, "💾", "USB",
                   "USB with a WIMUS folder\n(one folder per tracker)", MODE_USB)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _card(self, parent, icon, title, sub, mode):
        outer = tk.Frame(parent, bg=BORDER, padx=2, pady=2, cursor="hand2")
        outer.pack(side="left", padx=14)
        inner = tk.Frame(outer, bg=CARD, padx=34, pady=24)
        inner.pack()
        tk.Label(inner, text=icon, font=FE(36), bg=CARD).pack()
        tk.Label(inner, text=title, font=F(15, "bold"), fg=TEXT, bg=CARD).pack(pady=(8, 4))
        tk.Label(inner, text=sub, font=F(10), fg=MUTED, bg=CARD, justify="center").pack()

        def pick(_e=None):
            self.choice = mode
            self.destroy()

        def hover(on):
            outer.config(bg=ACCENT if on else BORDER)
            for w in [inner] + inner.winfo_children():
                w.config(bg=ACCENT_SOFT if on else CARD)

        for w in [outer, inner] + inner.winfo_children():
            w.bind("<Button-1>", pick)
            w.bind("<Enter>", lambda e: hover(True))
            w.bind("<Leave>", lambda e: hover(False))


def main():
    if SYSTEM == "Windows":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:  # own taskbar identity, so Windows doesn't group/cache it as python
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Hudl.WIMU.Downloader")
        except Exception:
            pass
    try:
        while True:
            launcher = Launcher()
            launcher.mainloop()
            if not launcher.choice:
                break
            app = App(launcher.choice)
            app.mainloop()
            if not app.back:
                break
    except Exception:
        log.exception("Fatal error")
        raise


if __name__ == "__main__":
    main()

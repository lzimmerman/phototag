#!/usr/bin/env python3
"""
phototag_gui - a small window for running phototag's steps.

Each button runs the same command you could type in Terminal
(`python phototag.py scan ...` etc.) as a separate process, so the window
stays responsive and the command-line tool is unchanged.

    python phototag_gui.py        (or double-click phototag.command)
"""
from __future__ import annotations

import codecs
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "phototag.py"
SETTINGS = Path(os.environ.get("PHOTOTAG_CACHE", Path.home() / ".phototag")) / "gui.json"

PROGRESS = re.compile(r"^\s*(\d+)/(\d+)\b")
TAG_LINE = re.compile(r"^\s*(\d+)\s{2}(\S.*)$")

STEPS = {
    "scan": "Analyse photos in the folder (slow the first time; only new or changed photos later).",
    "tag": "Apply the rules in tags.toml to the analysed photos. Takes seconds; re-run after editing tags.toml.",
    "review": "Open a contact sheet for one tag in your browser, to check it and tune thresholds.",
    "write": "Save the tags to .xmp sidecars next to the photos, then reread the albums in digiKam.",
}


# ------------------------------------------------- logic (no Tk needed) ---


def build_command(step: str, folder: str = "", limit: int | None = None, no_ml=False,
                  tag: str = "", dry_run=False, only_folder=False, retry=False) -> list[str]:
    cmd = [sys.executable, "-u", str(SCRIPT), step]
    if step == "scan":
        cmd.append(folder)
        if limit:
            cmd += ["--limit", str(limit)]
        if no_ml:
            cmd.append("--no-ml")
        if retry:
            cmd.append("--retry-errors")
    elif step == "review":
        cmd.append(tag)
    elif step == "write":
        if only_folder and folder:
            cmd.append(folder)
        if dry_run:
            cmd.append("--dry-run")
    return cmd


def split_output(buffer: str) -> tuple[list[tuple[str, str]], str]:
    """Split raw process output into ("progress"|"line", text) events.

    Progress lines are redrawn with "\\r"; ordinary lines end with "\\n".
    Returns the events and whatever incomplete text is left over.
    """
    events = []
    while True:
        m = re.search(r"[\r\n]", buffer)
        if not m:
            return events, buffer
        piece, buffer = buffer[: m.start()], buffer[m.end():]
        if not piece.strip():
            continue
        events.append(("progress" if PROGRESS.match(piece) else "line", piece.rstrip()))


def parse_tag_list(text: str) -> list[str]:
    """Tags from `phototag.py review` output, plus every parent level."""
    tags = set()
    for line in text.splitlines():
        m = TAG_LINE.match(line)
        if m:
            parts = m.group(2).strip().split("|")
            for i in range(1, len(parts) + 1):
                tags.add("|".join(parts[:i]))
    return sorted(tags, key=lambda t: (t.split("|")[0], t.count("|") > 0, t))


def summarise(step: str, lines: list[str], code: int, stopped: bool) -> str:
    """One-line result for the note under a step."""
    if stopped:
        return "Stopped. Progress so far is saved; run it again to continue."
    if code != 0:
        last = next((l for l in reversed(lines) if l.strip()), "")
        return f"Failed: {last[:160]}  (details in the log)"
    find = lambda pat: next((l.strip() for l in reversed(lines) if re.search(pat, l)), None)
    if step == "scan":
        m = re.search(r"(\d+) photos found, (\d+) to analyse", find(r"to analyse") or "")
        if not m:
            return "Done."
        found, todo = int(m.group(1)), int(m.group(2))
        if todo == 0:
            return f"All {found} photos were already analysed. Next: Tag."
        rest = f" ({found - todo} were already up to date)" if found > todo else ""
        bad = re.search(r"(\d+) photos? couldn't be read", find(r"couldn't be read") or "")
        bad = f" {bad.group(1)} couldn't be read (listed in the log)." if bad else ""
        return f"Analysed {todo} photo{'s' if todo > 1 else ''}{rest}.{bad} Next: Tag."
    if step == "tag":
        n = find(r"photos tagged") or "Done."
        p = find(r"sidecars to write")
        return f"{n}. {p.split('.')[0]}." if p else n
    if step == "write":
        s = find(r"would be written|created|updated|up to date|nothing to do") or "Done."
        return s + ("" if "would" in s or "up to date" in s else "  Now reread the albums in digiKam.")
    if step == "review":
        return "Opened in your browser."
    return "Done."


# Colours. The normal look keeps the system theme (on macOS the buttons are drawn
# by macOS and can't be recoloured) and only darkens the text around them; high
# contrast switches to Tk's "clam" theme, where every colour can be set.
LOOKS = {
    ("normal", "light"): {"note": "#3a3a3a", "error": "#8f0f07"},
    ("normal", "dark"): {"note": "#d0d0d0", "error": "#ff8a80"},
    ("high", "light"): {"bg": "#ffffff", "fg": "#000000", "button": "#003a8c", "button_fg": "#ffffff",
                        "active": "#0050c0", "pressed": "#00255c", "border": "#000000",
                        "disabled": "#d4d4d4", "disabled_fg": "#333333",
                        "note": "#000000", "error": "#a30000"},
    ("high", "dark"): {"bg": "#000000", "fg": "#ffffff", "button": "#8cc4ff", "button_fg": "#000000",
                       "active": "#b3d9ff", "pressed": "#5fa8f5", "border": "#ffffff",
                       "disabled": "#3a3a3a", "disabled_fg": "#e0e0e0",
                       "note": "#ffffff", "error": "#ff8a80"},
}


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two #rrggbb colours (7 or more is the AAA level for text)."""
    def lum(c):
        ch = [int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def tk_missing_help() -> str:
    v = f"{sys.version_info.major}.{sys.version_info.minor}"
    if sys.platform == "darwin":
        how = f"    brew install python-tk@{v}"
    else:
        how = ("    sudo apt install python3-tk        (Debian, Ubuntu, Mint)\n"
               "    sudo dnf install python3-tkinter   (Fedora)\n"
               "    sudo pacman -S tk                  (Arch)")
    return (f"Tkinter isn't available in this Python ({v}). Install it:\n{how}\n"
            "then run this again (no need to recreate the .venv).")


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except Exception:
        return {}


def save_settings(d: dict):
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(d))
    except Exception:
        pass


# -------------------------------------------------------------- window ---


def main():
    try:
        import tkinter as tk
        from tkinter import filedialog, ttk
        from tkinter import font as tkfont
        from tkinter.scrolledtext import ScrolledText
    except ImportError:
        sys.exit(tk_missing_help())

    settings = load_settings()
    root = tk.Tk()
    root.title("phototag")
    root.minsize(640, 560)
    events: queue.Queue = queue.Queue()
    state = {"proc": None, "step": None, "lines": [], "stopped": False}

    style = ttk.Style(root)
    native_theme = style.theme_use()
    fonts = {n: tkfont.nametofont(n) for n in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont")}
    native_sizes = {n: f.actual()["size"] for n, f in fonts.items()}
    log_font = tkfont.Font(family="Menlo" if sys.platform == "darwin" else "monospace", size=12)
    look = {}

    def is_dark() -> bool:
        r, g, b = (v / 257 for v in root.winfo_rgb(style.lookup("TFrame", "background") or "white"))
        return 0.299 * r + 0.587 * g + 0.114 * b < 128

    outer = ttk.Frame(root, padding=14)
    outer.pack(fill="both", expand=True)

    # folder
    ff = ttk.Frame(outer)
    ff.pack(fill="x")
    ttk.Label(ff, text="Photos folder").pack(side="left")
    folder = tk.StringVar(value=settings.get("folder", ""))
    ttk.Entry(ff, textvariable=folder).pack(side="left", fill="x", expand=True, padx=8)

    def choose():
        d = filedialog.askdirectory(initialdir=folder.get() or str(Path.home()), title="Choose a photo folder")
        if d:
            folder.set(d)

    ttk.Button(ff, text="Choose…", command=choose).pack(side="left")
    high_contrast = tk.BooleanVar(value=settings.get("high_contrast", False))
    ttk.Checkbutton(ff, text="High contrast", variable=high_contrast,
                    command=lambda: (apply_look(), remember())).pack(side="left", padx=(14, 0))

    steps = ttk.Frame(outer)
    steps.pack(fill="x", pady=(12, 6))
    steps.columnconfigure(1, weight=1)
    buttons, notes = {}, {}

    def step_row(row: int, key: str, label: str):
        b = ttk.Button(steps, text=label, width=10, command=lambda: start(key))
        b.grid(row=row * 2, column=0, sticky="nw", pady=(8, 0))
        opts = ttk.Frame(steps)
        opts.grid(row=row * 2, column=1, sticky="w", padx=10, pady=(8, 0))
        n = ttk.Label(steps, text=STEPS[key], style="Note.TLabel", wraplength=520, justify="left")
        n.grid(row=row * 2 + 1, column=1, sticky="w", padx=10)
        buttons[key], notes[key] = b, n
        return opts

    # scan options
    o = step_row(0, "scan", "1. Scan")
    use_limit = tk.BooleanVar(value=False)
    limit = tk.StringVar(value="300")
    no_ml = tk.BooleanVar(value=False)
    ttk.Checkbutton(o, text="Only the first", variable=use_limit).pack(side="left")
    ttk.Spinbox(o, from_=10, to=100000, increment=50, textvariable=limit, width=7).pack(side="left", padx=(2, 4))
    ttk.Label(o, text="photos").pack(side="left", padx=(0, 14))
    ttk.Checkbutton(o, text="EXIF and colour only (quick)", variable=no_ml).pack(side="left")
    retry = tk.BooleanVar(value=False)
    ttk.Checkbutton(o, text="Retry unreadable photos", variable=retry).pack(side="left", padx=(14, 0))

    step_row(1, "tag", "2. Tag")

    # review options
    o = step_row(2, "review", "3. Review")
    tag = tk.StringVar(value=settings.get("tag", ""))
    tag_box = ttk.Combobox(o, textvariable=tag, width=36)
    tag_box.pack(side="left")

    # write options
    o = step_row(3, "write", "4. Write")
    dry = tk.BooleanVar(value=True)
    only_folder = tk.BooleanVar(value=False)
    ttk.Checkbutton(o, text="Preview only (dry run)", variable=dry).pack(side="left", padx=(0, 14))
    ttk.Checkbutton(o, text="Only photos in the folder above", variable=only_folder).pack(side="left")

    # progress + log
    pf = ttk.Frame(outer)
    pf.pack(fill="x", pady=(12, 4))
    bar = ttk.Progressbar(pf, mode="determinate", maximum=100)
    bar.pack(side="left", fill="x", expand=True)
    stop_btn = ttk.Button(pf, text="Stop", state="disabled", command=lambda: stop())
    stop_btn.pack(side="left", padx=(8, 0))
    status = ttk.Label(outer, text="Ready.", style="Note.TLabel")
    status.pack(fill="x")
    log = ScrolledText(outer, height=12, font=log_font, wrap="word")
    log.pack(fill="both", expand=True, pady=(6, 0))
    log.configure(state="disabled")
    log_colours = {k: log.cget(k) for k in ("background", "foreground", "insertbackground",
                                            "selectbackground", "selectforeground", "highlightbackground")}

    def apply_look():
        """Switch between the system look and high contrast; also used at start-up."""
        high = high_contrast.get()
        style.theme_use(native_theme)
        if high:
            p = LOOKS[("high", "dark" if is_dark() else "light")]
            style.theme_use("clam")
            style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["bg"],
                            bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
                            troughcolor=p["bg"], selectbackground=p["button"], selectforeground=p["button_fg"],
                            insertcolor=p["fg"], focuscolor=p["fg"], arrowcolor=p["fg"])
            style.configure("TButton", background=p["button"], foreground=p["button_fg"], padding=(10, 5),
                            bordercolor=p["border"], lightcolor=p["button"], darkcolor=p["button"],
                            font=("TkDefaultFont", native_sizes["TkDefaultFont"] + 2, "bold"))
            style.map("TButton",
                      background=[("disabled", p["disabled"]), ("pressed", p["pressed"]), ("active", p["active"])],
                      foreground=[("disabled", p["disabled_fg"])],
                      lightcolor=[("disabled", p["disabled"]), ("pressed", p["pressed"]), ("active", p["active"])],
                      darkcolor=[("disabled", p["disabled"]), ("pressed", p["pressed"]), ("active", p["active"])])
            style.configure("TCheckbutton", indicatorbackground=p["bg"], indicatorforeground=p["fg"])
            style.map("TCheckbutton", background=[("active", p["bg"])],
                      indicatorbackground=[("selected", p["button"]), ("active", p["bg"])],
                      indicatorforeground=[("selected", p["button_fg"])])
            style.map("TCombobox", fieldbackground=[("readonly", p["bg"])], foreground=[("readonly", p["fg"])],
                      selectbackground=[("focus", p["button"])], selectforeground=[("focus", p["button_fg"])])
            style.configure("TEntry", foreground=p["fg"])
            style.configure("TSpinbox", foreground=p["fg"], arrowcolor=p["fg"])
            style.configure("Horizontal.TProgressbar", background=p["button"], bordercolor=p["border"],
                            lightcolor=p["button"], darkcolor=p["button"])
            root.option_add("*TCombobox*Listbox.background", p["bg"])
            root.option_add("*TCombobox*Listbox.foreground", p["fg"])
            root.option_add("*TCombobox*Listbox.selectBackground", p["button"])
            root.option_add("*TCombobox*Listbox.selectForeground", p["button_fg"])
            log.configure(background=p["bg"], foreground=p["fg"], insertbackground=p["fg"],
                          selectbackground=p["button"], selectforeground=p["button_fg"],
                          highlightbackground=p["border"])
            root.configure(background=p["bg"])
            grow = 2
        else:
            p = LOOKS[("normal", "dark" if is_dark() else "light")]
            log.configure(**log_colours)
            grow = 0
        for name, f in fonts.items():
            f.configure(size=native_sizes[name] + grow)
        log_font.configure(size=12 + grow)
        style.configure("Note.TLabel", foreground=p["note"])
        look.update(p)
        for n in notes.values():
            n.configure(wraplength=520 + 60 * grow)

    def remember():
        save_settings({"folder": folder.get().strip(), "tag": tag.get().strip(),
                       "high_contrast": high_contrast.get()})

    def write_log(text: str):
        log.configure(state="normal")
        log.insert("end", text + "\n")
        log.see("end")
        log.configure(state="disabled")

    def set_running(running: bool):
        for b in buttons.values():
            b.configure(state="disabled" if running else "normal")
        stop_btn.configure(state="normal" if running else "disabled")

    def refresh_tags():
        def work():
            try:
                out = subprocess.run([sys.executable, str(SCRIPT), "review"], capture_output=True,
                                     text=True, cwd=HERE, timeout=60).stdout
                events.put(("tags", parse_tag_list(out)))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def start(step: str):
        f = folder.get().strip()
        if step == "scan" or (step == "write" and only_folder.get()):
            if not f or not Path(f).expanduser().is_dir():
                notes[step].configure(text="Choose a photos folder first (or check the drive is connected).", foreground=look["error"])
                return
        if step == "review" and not tag.get().strip():
            notes[step].configure(text="Pick a tag from the list first (run Tag if the list is empty).", foreground=look["error"])
            return
        try:
            n = int(limit.get()) if use_limit.get() else None
        except ValueError:
            n = None
        cmd = build_command(step, str(Path(f).expanduser()) if f else "", n, no_ml.get(),
                            tag.get().strip(), dry.get(), only_folder.get(), retry.get())
        remember()
        write_log(f"\n$ phototag {' '.join(cmd[3:])}")
        notes[step].configure(text="Running…", foreground="")
        status.configure(text="Starting…")
        bar.configure(mode="indeterminate")
        bar.start(12)
        state.update(step=step, lines=[], stopped=False)
        set_running(True)
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        state["proc"] = proc

        def reader():
            buf = ""
            fd = proc.stdout.fileno()
            dec = codecs.getincrementaldecoder("utf-8")("replace")  # characters split across reads stay intact
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                evs, buf = split_output(buf + dec.decode(chunk))
                for e in evs:
                    events.put(e)
            if buf.strip():
                events.put(("line", buf.rstrip()))
            events.put(("done", proc.wait()))

        threading.Thread(target=reader, daemon=True).start()

    def stop():
        p = state["proc"]
        if p and p.poll() is None:
            state["stopped"] = True
            status.configure(text="Stopping (saving progress)…")
            p.send_signal(signal.SIGINT)  # same as Ctrl-C: phototag saves and exits cleanly

    last_progress = {"text": ""}

    def poll():
        try:
            while True:
                kind, val = events.get_nowait()
                if kind == "progress":
                    m = PROGRESS.match(val)
                    done, total = int(m.group(1)), int(m.group(2))
                    if str(bar.cget("mode")) != "determinate":
                        bar.stop()
                        bar.configure(mode="determinate")
                    bar.configure(value=100 * done / max(total, 1))
                    status.configure(text=val.strip())
                    last_progress["text"] = val.strip()
                elif kind == "line":
                    state["lines"].append(val)
                    write_log(val)
                    if not last_progress["text"]:
                        status.configure(text=val.strip()[:120])
                elif kind == "done":
                    step = state["step"]
                    if last_progress["text"]:
                        write_log(last_progress["text"])
                    last_progress["text"] = ""
                    bar.stop()
                    bar.configure(mode="determinate", value=100 if val == 0 else bar.cget("value"))
                    msg = summarise(step, state["lines"], val, state["stopped"])
                    failed = val != 0 and not state["stopped"]
                    notes[step].configure(text=msg, foreground=look["error"] if failed else "")
                    status.configure(text="Ready.")
                    state["proc"] = None
                    set_running(False)
                    if step in ("tag", "scan") and val == 0:
                        refresh_tags()
                elif kind == "tags":
                    tag_box.configure(values=val)
        except queue.Empty:
            pass
        root.after(100, poll)

    def on_close():
        p = state["proc"]
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    apply_look()
    refresh_tags()
    poll()
    root.mainloop()


if __name__ == "__main__":
    main()

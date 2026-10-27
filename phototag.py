#!/usr/bin/env python3
"""
phototag - offline auto-tagging for a photo library, for digiKam.

Analyses each photo once (EXIF, colour/brightness statistics, YOLO person and
object detection, a SigLIP image embedding), caches the results in SQLite,
derives tags from the rules in tags.toml, and writes them as hierarchical
keywords to XMP sidecar files. Original image files are never modified.

    python phototag.py scan /Volumes/Book/Photos     # slow, once (resumable)
    python phototag.py tag                           # fast, re-run after editing tags.toml
    python phototag.py review "Scene|Landscape"      # ranked contact sheet, to tune thresholds
    python phototag.py explain /path/to/IMG_0001.CR2 # everything known about one photo
    python phototag.py write --dry-run               # show what would change
    python phototag.py write                         # write/update the .xmp sidecars
    python phototag.py html                          # web page to browse by tag (no digiKam needed)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None  # big panoramas and scans are fine

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF = True
except ImportError:
    HEIF = False

RAW_EXTS = {
    ".3fr", ".arw", ".cr2", ".cr3", ".crw", ".dcr", ".dng", ".erf", ".iiq", ".kdc",
    ".mef", ".mos", ".nef", ".nrw", ".orf", ".pef", ".raf", ".rw2", ".rwl", ".sr2",
    ".srf", ".srw", ".x3f",
}
RASTER_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".webp"} | (
    {".heic", ".heif"} if HEIF else set()
)
BROWSER_VIEWABLE = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
SKIP_DIRS = {".dtrash", "@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information"}
# library packages (Apple Photos, iPhoto, Aperture, Lightroom): their internal copies
# and previews aren't photos to tag, and sidecars don't belong inside them
SKIP_SUFFIXES = (".photoslibrary", ".photolibrary", ".migratedphotolibrary", ".aplibrary",
                 ".lrdata", ".lrlibrary")

META_TAGS = [
    "-Make", "-Model", "-LensID", "-LensModel", "-Lens",
    "-DateTimeOriginal", "-CreateDate", "-OffsetTimeOriginal", "-OffsetTime", "-GPSDateTime",
    "-FocalLength#", "-FocalLengthIn35mmFormat#", "-FocalLength35efl#",
    "-ExposureTime#", "-FNumber#", "-ISO#", "-Flash#", "-Orientation#",
    "-GPSLatitude#", "-GPSLongitude#",
]

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE = Path(os.environ.get("PHOTOTAG_CACHE", Path.home() / ".phototag"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def keep_awake():
    """Stop the Mac idle-sleeping (and the drive disconnecting) until this process exits."""
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])


def time_left(seconds: float) -> str:
    """'~40 s left', '~25 min left', '~1 h left', '~2 h 5 min left'."""
    if seconds < 90:
        return f"~{seconds:.0f} s left"
    m = round(seconds / 60)
    if m < 60:
        return f"~{m} min left"
    return f"~{m // 60} h {m % 60} min left" if m % 60 else f"~{m // 60} h left"


def progress_line(i: int, total: int, elapsed: float, unit: str) -> str:
    """'  120/2400 (5%)  3.1 photos/s  ~12 min left'; no estimate until there is enough to go on."""
    rate = i / max(elapsed, 1e-6)
    msg = f"  {i}/{total} ({100 * i / max(total, 1):.0f}%)  {rate:.1f} {unit}/s"
    if i < total and (i >= 20 or elapsed >= 10):
        msg += f"  {time_left((total - i) / rate)}"
    return msg


# --------------------------------------------------------------- exiftool ---


class ExifTool:
    """One long-running exiftool process (-stay_open), so per-file calls are cheap."""

    def __init__(self, exe: str = "exiftool"):
        try:
            self.proc = subprocess.Popen(
                [exe, "-stay_open", "True", "-@", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,  # Ctrl-C stops phototag between files, never exiftool mid-write
            )
        except FileNotFoundError:
            sys.exit("exiftool not found. Install it with:  brew install exiftool")
        self.n = 0
        self.errors: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self.errors.append(line.decode("utf-8", "replace").rstrip())

    def run(self, *args) -> bytes:
        self.n += 1
        sentinel = f"{{ready{self.n}}}".encode()
        lines = [str(a) for a in args] + ["-charset", "filename=utf8", f"-execute{self.n}", ""]
        if any("\n" in a for a in lines):
            raise ValueError("newline in exiftool argument")
        self.proc.stdin.write("\n".join(lines).encode("utf-8"))
        self.proc.stdin.flush()
        buf = bytearray()
        fd = self.proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise RuntimeError("exiftool exited unexpectedly")
            buf += chunk
            for end in (b"\n", b"\r\n"):
                if buf.endswith(sentinel + end):
                    return bytes(buf[: -len(sentinel) - len(end)])

    def json(self, *args) -> list[dict]:
        out = self.run("-j", *args).strip()
        return json.loads(out) if out else []

    def close(self):
        try:
            self.proc.stdin.write(b"-stay_open\nFalse\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


# ----------------------------------------------------------------- config ---


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    names = [s["tag"] for s in cfg.get("scene", [])] + list(cfg.get("objects", {}).get("classes", {}).values())
    bad = [n for n in names if "/" in n]
    if bad:
        sys.exit(f"Tag names can't contain '/': {bad}")
    cfg.setdefault("root", "Auto")
    return cfg


# --------------------------------------------------------------------- db ---

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos(
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, kind TEXT,
    meta TEXT, stats TEXT, dets TEXT, emb BLOB, emb_model TEXT,
    thumb TEXT, error TEXT, scanned_at REAL);
CREATE TABLE IF NOT EXISTS tags(path TEXT PRIMARY KEY, tags TEXT, scores TEXT);
CREATE TABLE IF NOT EXISTS written(path TEXT PRIMARY KEY, tags TEXT, sidecar TEXT, written_at REAL);
CREATE TABLE IF NOT EXISTS textcache(model TEXT, prompt TEXT, emb BLOB, PRIMARY KEY(model, prompt));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def open_db(cache: Path) -> sqlite3.Connection:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "thumbs").mkdir(exist_ok=True)
    db = sqlite3.connect(cache / "phototag.sqlite")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# ---------------------------------------------------------------- images ---

_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def to_rgb(im: Image.Image) -> Image.Image:
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):  # 16-bit greyscale scans
        a = np.asarray(im).astype(np.float32)
        a = a / max(a.max(), 1) * 255
        im = Image.fromarray(a.astype(np.uint8))
    if im.mode == "RGBA" or im.mode == "LA" or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def open_oriented(src, fallback_orientation=None, max_side=1024) -> Image.Image:
    im = Image.open(src)
    if im.format == "JPEG":
        im.draft("RGB", (max_side, max_side))  # fast DCT downscale
    own = im.getexif().get(0x0112)
    if own and own != 1:
        im = ImageOps.exif_transpose(im)
    else:
        im.load()
        if fallback_orientation in _TRANSPOSE and not own:
            im = im.transpose(_TRANSPOSE[fallback_orientation])
    im = to_rgb(im)
    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return im


def decode_b64(v) -> bytes | None:
    if isinstance(v, str) and v.startswith("base64:"):
        return base64.b64decode(v[7:])
    return None


PREVIEW_TAGS = ("PreviewImage", "OtherImage")       # small to medium: usually one is 1000px+
FALLBACK_PREVIEWS = ("JpgFromRaw", "ThumbnailImage")  # full-size (slow to extract) or tiny


def read_photo(et: ExifTool, p: Path) -> tuple[dict, bytes | None]:
    """EXIF for one photo and, for a RAW, its embedded JPEG preview, in as few reads as possible.

    Scan works at 1024px, so the smallest preview at least 1000px wide is best: Nikon's
    1620px OtherImage, say, rather than the full-size JpgFromRaw.
    """
    if p.suffix.lower() not in RAW_EXTS:
        return (et.json(*META_TAGS, str(p)) or [{}])[0], None
    d = (et.json(*META_TAGS, "-b", *(f"-{t}" for t in PREVIEW_TAGS), str(p)) or [{}])[0]
    found = [b for t in PREVIEW_TAGS if (b := decode_b64(d.pop(t, None)))]
    big = [b for b in found if _jpeg_side(b) >= 1000]
    if big:
        return d, min(big, key=_jpeg_side)
    more = (et.json("-b", *(f"-{t}" for t in FALLBACK_PREVIEWS), str(p)) or [{}])[0]
    found += [b for t in FALLBACK_PREVIEWS if (b := decode_b64(more.get(t)))]
    return d, max(found, key=_jpeg_side, default=None)


def _jpeg_side(data: bytes) -> int:
    try:
        return max(Image.open(io.BytesIO(data)).size)  # reads the header only
    except Exception:
        return 0


def develop_raw(path: Path) -> Image.Image | None:
    """Last resort for RAWs without a usable preview (needs `pip install rawpy`)."""
    try:
        import rawpy
    except ImportError:
        return None
    with rawpy.imread(str(path)) as r:
        return Image.fromarray(r.postprocess(half_size=True, use_camera_wb=True))


def image_stats(im: Image.Image) -> dict:
    small = im.copy()
    small.thumbnail((512, 512))
    ycc = np.asarray(small.convert("YCbCr")).astype(np.float32)
    y, cb, cr = ycc[..., 0] / 255.0, ycc[..., 1] - 128, ycc[..., 2] - 128
    # ignore near-black and near-white pixels, where chroma is meaningless
    mask = (y > 0.04) & (y < 0.97)
    if mask.sum() < 100:
        mask = np.ones_like(y, dtype=bool)
    mcb, mcr = float(cb[mask].mean()), float(cr[mask].mean())
    spread = np.hypot(cb[mask] - mcb, cr[mask] - mcr)
    lap = (-4 * y[1:-1, 1:-1] + y[:-2, 1:-1] + y[2:, 1:-1] + y[1:-1, :-2] + y[1:-1, 2:])
    return {
        "w": im.width, "h": im.height,
        "tint": round(math.hypot(mcb, mcr), 2),
        "tint_hue": round(math.degrees(math.atan2(mcr, mcb)), 1),
        "spread_p99": round(float(np.percentile(spread, 99)), 2),
        "brightness": round(float(y.mean()), 3),
        "sharpness": round(float(lap.var() * 1e4), 2),
        "dark": round(float((y < 0.06).mean()), 3),  # share of near-black pixels
    }


# -------------------------------------------------------------------- ML ---


def pick_device(pref: str) -> str:
    import torch

    if pref and pref != "auto":
        return pref
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _hf_cached(model_name: str) -> bool:
    """Is open_clip's Hugging Face repo for this model already downloaded?"""
    hub = os.environ.get("HF_HUB_CACHE") or Path(
        os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    snaps = Path(hub) / f"models--timm--{model_name}" / "snapshots"
    return snaps.is_dir() and any(snaps.iterdir())


class Clip:
    FALLBACK = ("ViT-B-16-SigLIP", "webli")

    def __init__(self, cfg: dict):
        m = cfg.get("models", {})
        name, pre = m.get("clip", "ViT-B-16-SigLIP2-256"), m.get("clip_pretrained", "webli")
        # Once the model is downloaded, load it without contacting Hugging Face
        # (no update checks, no "unauthenticated requests" warning). Must be set
        # before open_clip imports huggingface_hub. HF_HUB_OFFLINE=0 forces online.
        if any(_hf_cached(n) for n in (name, self.FALLBACK[0])):
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import open_clip
        import torch

        self.torch = torch
        self.device = pick_device(m.get("device", "auto"))
        try:
            model, _, prep = open_clip.create_model_and_transforms(name, pretrained=pre)
        except Exception as e:
            log(f"Couldn't load {name}/{pre} ({e}); falling back to {'/'.join(self.FALLBACK)}")
            name, pre = self.FALLBACK
            model, _, prep = open_clip.create_model_and_transforms(name, pretrained=pre)
        self.id = f"{name}/{pre}"
        self.model = model.eval().to(self.device)
        self.prep = prep
        self.tok = open_clip.get_tokenizer(name)
        self.scale = model.logit_scale.detach().exp().item()
        bias = getattr(model, "logit_bias", None)
        self.bias = bias.detach().item() if bias is not None else 0.0
        log(f"SigLIP {self.id} on {self.device}")

    def images(self, ims: list[Image.Image]) -> np.ndarray:
        try:
            with self.torch.no_grad():
                x = self.torch.stack([self.prep(im) for im in ims]).to(self.device)
                f = self.model.encode_image(x)
                f = f / f.norm(dim=-1, keepdim=True)
            return f.float().cpu().numpy()
        except Exception as e:
            if self.device == "cpu":
                raise
            log(f"SigLIP failed on {self.device} ({e}); switching to cpu")
            self.device = "cpu"
            self.model = self.model.to("cpu")
            return self.images(ims)

    def texts(self, prompts: list[str]) -> np.ndarray:
        with self.torch.no_grad():
            f = self.model.encode_text(self.tok(prompts).to(self.device))
            f = f / f.norm(dim=-1, keepdim=True)
        return f.float().cpu().numpy()


class Detector:
    def __init__(self, cfg: dict, cache: Path):
        # Ultralytics otherwise pip-installs packages mid-run: its replacement for
        # Image.open installs a HEIC plugin whenever a corrupt file can't be opened.
        # pillow-heif already covers HEIC, and the detector is given opened images,
        # so put Pillow's own Image.open back.
        os.environ.setdefault("YOLO_AUTOINSTALL", "false")
        from ultralytics import YOLO
        from ultralytics.utils import patches
        Image.open = getattr(patches, "_image_open", Image.open)

        m = cfg.get("models", {})
        weights = m.get("yolo", "yolo11s.pt")
        if not os.path.sep in weights:
            (cache / "models").mkdir(exist_ok=True)
            weights = str(cache / "models" / weights)
        self.model = YOLO(weights)
        self.device = pick_device(m.get("device", "auto"))
        self.id = Path(weights).name

    def __call__(self, ims: list[Image.Image]) -> list[list]:
        try:
            res = self.model.predict(ims, imgsz=640, conf=0.25, device=self.device, verbose=False)
        except Exception as e:
            if self.device == "cpu":
                raise
            log(f"YOLO failed on {self.device} ({e}); switching to cpu")
            self.device = "cpu"
            return self(ims)
        out = []
        for r in res:
            dets = []
            for c, cf, box in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist(), r.boxes.xywhn.tolist()):
                dets.append([r.names[int(c)], round(cf, 3), round(box[2] * box[3], 4)])
            out.append(dets)
        return out


# ------------------------------------------------------------------- scan ---


def iter_photos(roots: list[Path]):
    for root in roots:
        if root.is_file():
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            for d in dirnames:
                if d.lower().endswith(SKIP_SUFFIXES):
                    log(f"Skipping library package: {Path(dirpath) / d}")
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                                 and not d.lower().endswith(SKIP_SUFFIXES))
            for fn in sorted(filenames):
                if fn.startswith("."):  # includes macOS "._" files on exFAT drives
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext in RAW_EXTS or ext in RASTER_EXTS:
                    yield Path(dirpath) / fn


def cmd_scan(args, cfg):
    roots = [Path(r).expanduser().resolve() for r in args.roots]
    for r in roots:
        if not r.exists():
            sys.exit(f"Not found: {r}\n(Check the path, or whether the drive is connected. "
                     "Tip: drag the folder from Finder into Terminal to paste its exact path.)")
    db = open_db(args.cache)
    use_ml = not args.no_ml
    known = {r["path"]: r for r in db.execute("SELECT path, size, mtime, emb IS NOT NULL AS has_emb, dets IS NOT NULL AS has_dets, error FROM photos")}

    log("Listing files...")
    todo = []
    seen = set()
    for p in iter_photos(roots):
        sp = str(p)
        seen.add(sp)
        try:
            st = p.stat()
        except OSError as e:
            log(f"  ! {p}: {e}")
            continue
        k = known.get(sp)
        changed = k is None or k["size"] != st.st_size or abs(k["mtime"] - st.st_mtime) > 1
        needs_ml = use_ml and k is not None and not k["error"] and (args.rescan_ml or not (k["has_emb"] and k["has_dets"]))
        if args.rescan or changed or needs_ml or (args.retry_errors and k["error"]):
            todo.append(p)
    if args.prune:
        gone = [p for p in known if any(p.startswith(str(r) + os.sep) for r in roots) and p not in seen]
        for p in gone:
            for t in ("photos", "tags", "written"):
                db.execute(f"DELETE FROM {t} WHERE path=?", (p,))
        db.commit()
        log(f"Pruned {len(gone)} photos that no longer exist")
    if args.limit:
        todo = todo[: args.limit]
    log(f"{len(seen)} photos found, {len(todo)} to analyse")
    if not todo:
        return

    if not shutil.which("exiftool"):
        sys.exit("exiftool not found. Install it with:  brew install exiftool")
    keep_awake()
    if use_ml:
        log("Loading the image models (the first time this downloads them)...")
    clip = Clip(cfg) if use_ml else None
    det = Detector(cfg, args.cache) if use_ml else None
    exiftools, local = [], threading.local()

    def prepare(p: Path):
        """Read, decode and thumbnail one photo, on a worker thread with its own exiftool."""
        if not hasattr(local, "et"):
            local.et = ExifTool()
            exiftools.append(local.et)
        meta = {}
        try:
            meta, preview = read_photo(local.et, p)
            if p.suffix.lower() in RAW_EXTS:
                if preview:
                    im = open_oriented(io.BytesIO(preview), meta.get("Orientation"))
                else:
                    im = develop_raw(p)  # rawpy applies the orientation itself
                    if im is None:
                        raise ValueError("no embedded preview (pip install rawpy to develop it instead)")
                    im = to_rgb(im)
                    im.thumbnail((1024, 1024))
            else:
                im = open_oriented(p)
            stats = image_stats(im)
            thumb = hashlib.sha1(str(p).encode()).hexdigest()[:16] + ".jpg"
            t = im.copy()
            t.thumbnail((360, 360))
            t.save(args.cache / "thumbs" / thumb, quality=82)
            return p, meta, im, stats, thumb, None
        except Exception as e:
            return p, meta, None, None, None, f"{type(e).__name__}: {e}"

    # Four workers read and decode photos (the drive copes well with several reads at
    # once) while the models work through the previous batch on the GPU.
    pool = ThreadPoolExecutor(max_workers=4)
    queued, upcoming = deque(), iter(todo)

    def top_up():  # keep the workers up to two batches ahead, so memory stays flat
        while len(queued) < 3 * args.batch and (p := next(upcoming, None)):
            queued.append(pool.submit(prepare, p))

    t0, done, failed, stopped = time.time(), 0, 0, False
    try:
        top_up()
        while queued:
            results = [queued.popleft().result() for _ in range(min(args.batch, len(queued)))]
            top_up()
            good = [r for r in results if r[2] is not None]
            embs = clip.images([r[2] for r in good]) if (clip and good) else None
            dets = det([r[2] for r in good]) if (det and good) else None
            gi = 0
            for p, meta, im, stats, thumb, err in results:
                try:
                    st = p.stat()
                except OSError as e:  # gone since listing: leave it for the next scan
                    if not all(r.exists() for r in roots):
                        raise SystemExit("\nThe photos folder has gone: is the drive still connected? "
                                         "Progress so far is saved; run the same command to continue.")
                    log(f"\n  ! {p}: {e}")
                    if im is not None:
                        gi += 1
                    continue
                if err:
                    failed += 1
                    log(f"\n  ! {p}: {err}")
                    row = (str(p), st.st_size, st.st_mtime, kind(p), json.dumps(meta), None, None, None, None, None, err, time.time())
                else:
                    e = embs[gi].astype(np.float16).tobytes() if embs is not None else None
                    d = json.dumps(dets[gi]) if dets is not None else None
                    row = (str(p), st.st_size, st.st_mtime, kind(p), json.dumps(meta), json.dumps(stats), d, e,
                           clip.id if clip else None, thumb, None, time.time())
                    gi += 1
                db.execute("INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
            db.commit()
            done += len(results)
            print(f"\r{progress_line(done, len(todo), time.time() - t0, 'photos')}   ", end="", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        stopped = True
        log("\nStopped; progress so far is saved. Run the same command to continue.")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)  # finish the photos being read, drop the rest
        db.commit()  # rows already stored from a batch cut short (e.g. the drive went away)
        for e in exiftools:
            e.close()
    if failed:
        log(f"\n{failed} photo{'s' if failed > 1 else ''} couldn't be read (listed above). "
            "To try them again, run scan with --retry-errors.")
    if not stopped:
        log(f"\nDone. Next: python {Path(__file__).name} tag")


def kind(p: Path) -> str:
    return "raw" if p.suffix.lower() in RAW_EXTS else "raster"


# -------------------------------------------------------------------- tag ---


def parse_dt(s) -> datetime | None:
    if not isinstance(s, str) or len(s) < 19 or s.startswith("0000"):
        return None
    try:
        return datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def parse_offset(s) -> timedelta | None:
    if not isinstance(s, str) or len(s) < 6 or s[0] not in "+-":
        return None
    try:
        h, m = int(s[1:3]), int(s[4:6])
    except ValueError:
        return None
    sign = 1 if s[0] == "+" else -1
    return sign * timedelta(hours=h, minutes=m)


def utc_time(meta: dict) -> datetime | None:
    g = meta.get("GPSDateTime")
    if isinstance(g, str) and g.endswith("Z"):
        d = parse_dt(g)
        if d:
            return d.replace(tzinfo=timezone.utc)
    d = parse_dt(meta.get("DateTimeOriginal") or meta.get("CreateDate"))
    off = parse_offset(meta.get("OffsetTimeOriginal") or meta.get("OffsetTime"))
    if d and off is not None:
        return (d - off).replace(tzinfo=timezone.utc)
    return None


def sun_elevation(lat: float, lon: float, t: datetime) -> float:
    """Solar elevation in degrees (NOAA approximation, good to ~0.5 degree)."""
    n = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (n - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = hour * 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cz))))


def camera_name(meta: dict) -> str | None:
    make = str(meta.get("Make") or "").strip()
    model = str(meta.get("Model") or "").strip()
    if not model:
        return None
    for junk in (" CORPORATION", " Corporation", " IMAGING CORP.", " OPTICAL CO.,LTD", " Co., Ltd.", " CO.,LTD."):
        make = make.replace(junk, "")
    first = make.split(" ")[0] if make else ""
    if first and not model.lower().startswith(first.lower()):
        model = f"{first.title() if first.isupper() and len(first) > 3 else first} {model}"
    return clean(model)


def lens_name(meta: dict) -> str | None:
    for k in ("LensID", "LensModel", "Lens"):
        v = str(meta.get(k) or "").strip()
        if v and not v.lower().startswith("unknown") and not v.replace(".", "").replace(" ", "").isdigit() and v != "----":
            return clean(v)
    return None


def clean(s: str) -> str:
    # "/" and "|" are hierarchy separators; U+2215 looks like a slash but isn't one
    return " ".join(s.replace("/", "∕").replace("|", "-").split())


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


SEASONS_N = {12: "Winter", 1: "Winter", 2: "Winter", 3: "Spring", 4: "Spring", 5: "Spring",
             6: "Summer", 7: "Summer", 8: "Summer", 9: "Autumn", 10: "Autumn", 11: "Autumn"}
FLIP = {"Winter": "Summer", "Summer": "Winter", "Spring": "Autumn", "Autumn": "Spring"}


def exif_tags(meta: dict, cfg: dict, place) -> list[str]:
    e = cfg.get("exif", {})
    out = []
    lat, lon = num(meta.get("GPSLatitude")), num(meta.get("GPSLongitude"))
    has_gps = lat is not None and lon is not None and not (lat == 0 and lon == 0)
    dt = parse_dt(meta.get("DateTimeOriginal") or meta.get("CreateDate"))
    if dt and e.get("season", True):
        s = SEASONS_N[dt.month]
        south = (lat < 0) if has_gps else e.get("hemisphere", "north") == "south"
        out.append(f"Season|{FLIP[s] if south else s}")
    if dt and e.get("time_of_day", True):
        label = None
        for start, name in e.get("time_of_day_starts", []):
            if dt.hour >= start:
                label = name
        if label:
            out.append(f"Time of day|{label}")
    if has_gps and e.get("golden_hour", True):
        t = utc_time(meta)
        if t:
            el = sun_elevation(lat, lon, t)
            if -4 <= el < 6:
                out.append("Light|Golden hour")
            elif -6 <= el < -4:
                out.append("Light|Blue hour")
    if e.get("camera", True) and (c := camera_name(meta)):
        out.append(f"Camera|{c}")
    if e.get("lens", True) and (lz := lens_name(meta)):
        out.append(f"Lens|{lz}")
    f35 = num(meta.get("FocalLengthIn35mmFormat")) or num(meta.get("FocalLength35efl"))
    if f35 and e.get("focal"):
        for name, upper in e["focal"].items():
            if f35 < upper:
                out.append(f"Focal length|{name}")
                break
    exp = num(meta.get("ExposureTime"))
    if exp and exp >= e.get("long_exposure_s", 0.5):
        out.append("Exposure|Long exposure")
    iso = num(meta.get("ISO"))
    if iso and iso >= e.get("high_iso", 3200):
        out.append("Exposure|High ISO")
    fl = num(meta.get("Flash"))
    if fl is not None and e.get("flash", True) and int(fl) & 1:
        out.append("Exposure|Flash")
    if place and e.get("place", True):
        parts = [clean(place[k]) for k in e.get("place_levels", ["country", "region", "city"]) if place.get(k)]
        if parts:
            out.append("Place|" + "|".join(parts))
    return out


def computed_tags(st: dict, cfg: dict) -> list[str]:
    out = []
    c = cfg.get("color", {})
    if c.get("enabled", True) and st["spread_p99"] < c.get("bw_max_chroma", 7.0):
        if st["tint"] < c.get("toned_min_tint", 3.0):
            out.append("Color|Black and white")
        elif st["tint"] <= c.get("toned_max_tint", 30.0):  # stronger = a single-colour subject, not toning
            out.append("Color|Toned")
    fi = cfg.get("film", {})
    if fi.get("enabled", True) and "dark" in st:  # older scans lack "dark": rescan to use this rule
        lo, hi = fi.get("negative_hue", [115, 145])
        if (lo <= st["tint_hue"] <= hi and st["tint"] >= fi.get("negative_min_tint", 20)
                and st["spread_p99"] <= fi.get("negative_max_spread", 32)
                and st["dark"] <= fi.get("negative_max_dark", 0.15)):
            out.append("Film|Negative")
    li = cfg.get("light", {})
    if li.get("enabled", True):
        if st["brightness"] <= li.get("low_key_max", 0.22):
            out.append("Light|Low key")
        elif st["brightness"] >= li.get("high_key_min", 0.75):
            out.append("Light|High key")
    fm = cfg.get("format", {})
    if fm.get("enabled", True):
        w, h = st["w"], st["h"]
        ratio = max(w, h) / max(min(w, h), 1)
        if ratio >= fm.get("panorama_min_ratio", 2.0):
            out.append("Format|Panorama")
        elif ratio <= fm.get("square_max_ratio", 1.05):
            out.append("Format|Square")
        else:
            out.append("Format|Vertical" if h > w else "Format|Horizontal")
    return out


def detection_tags(dets: list, cfg: dict) -> list[str]:
    out = []
    pc = cfg.get("people", {})
    if pc.get("enabled", True):
        people = [a for n, cf, a in dets if n == "person" and cf >= pc.get("min_conf", 0.45) and a >= pc.get("min_area", 0.002)]
        n = len(people)
        if n == 0:
            if pc.get("tag_none"):
                out.append("People|None")
        else:
            out.append("People|One" if n == 1 else "People|Two" if n == 2 else "People|Group")
            big = max(people)
            if n <= 2 and big >= pc.get("portrait_area", 0.15):
                out.append("People|Portrait")
            elif big < pc.get("small_figure_area", 0.03):
                out.append("People|Small figures")
    oc = cfg.get("objects", {})
    if oc.get("enabled", True):
        classes = oc.get("classes", {})
        for n, cf, a in dets:
            if n in classes and cf >= oc.get("min_conf", 0.5) and a >= oc.get("min_area", 0.005):
                out.append(classes[n])
    return out


def scene_vectors(db, cfg, model_ids: set[str], cache: Path):
    """Averaged prompt embeddings per scene tag, cached so `tag` rarely needs the model."""
    scenes = cfg.get("scene", []) if cfg.get("scenes", {}).get("enabled", True) else []
    if not scenes or not model_ids:
        return None
    if len(model_ids) > 1:
        log(f"Photos were embedded with several models {sorted(model_ids)}; "
            "run `scan ROOT --rescan-ml` so they match. Using the most common one.")
    mid = db.execute("SELECT emb_model, COUNT(*) c FROM photos WHERE emb_model IS NOT NULL GROUP BY 1 ORDER BY c DESC").fetchone()[0]
    prompts = sorted({p for s in scenes for p in s["prompts"]})
    have = {r["prompt"]: np.frombuffer(r["emb"], dtype=np.float32)
            for r in db.execute("SELECT prompt, emb FROM textcache WHERE model=?", (mid,))}
    kv = db.execute("SELECT v FROM kv WHERE k=?", (f"logit:{mid}",)).fetchone()
    missing = [p for p in prompts if p not in have]
    if missing or not kv:
        clip = Clip(cfg)
        if clip.id != mid:
            sys.exit(f"Photos were embedded with {mid} but tags.toml now loads {clip.id}. "
                     "Change [models] back, or run `scan ROOT --rescan-ml`.")
        vecs = clip.texts(missing) if missing else []
        for p, v in zip(missing, vecs):
            db.execute("INSERT OR REPLACE INTO textcache VALUES (?,?,?)", (mid, p, v.astype(np.float32).tobytes()))
            have[p] = v
        db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (f"logit:{mid}", json.dumps([clip.scale, clip.bias])))
        db.commit()
        kv = (json.dumps([clip.scale, clip.bias]),)
    scale, bias = json.loads(kv[0])
    default = cfg.get("scenes", {}).get("default_threshold", 0.1)
    names, mats, ths = [], [], []
    for s in scenes:
        v = np.mean([have[p] for p in s["prompts"]], axis=0)
        mats.append(v / np.linalg.norm(v))
        names.append(s["tag"])
        ths.append(s.get("threshold", default))
    return mid, names, np.stack(mats), np.array(ths), scale, bias


def geocode(rows, cfg) -> dict[str, dict]:
    if not cfg.get("exif", {}).get("place", True):
        return {}
    coords, paths = [], []
    for r in rows:
        m = json.loads(r["meta"] or "{}")
        lat, lon = num(m.get("GPSLatitude")), num(m.get("GPSLongitude"))
        if lat is not None and lon is not None and not (lat == 0 and lon == 0):
            coords.append((lat, lon))
            paths.append(r["path"])
    if not coords:
        return {}
    try:
        import reverse_geocoder as rg
    except ImportError:
        log("reverse_geocoder not installed; skipping Place tags")
        return {}
    try:
        import pycountry

        def country(cc):
            c = pycountry.countries.get(alpha_2=cc)
            return getattr(c, "common_name", None) or (c.name if c else cc)
    except ImportError:
        def country(cc):
            return cc
    res = rg.search(coords, mode=1, verbose=False)
    return {p: {"country": country(r["cc"]), "region": r.get("admin1"), "city": r.get("name")} for p, r in zip(paths, res)}


def cmd_tag(args, cfg):
    db = open_db(args.cache)
    rows = db.execute("SELECT path, meta, stats, dets, emb, emb_model FROM photos WHERE error IS NULL").fetchall()
    if not rows:
        sys.exit("Nothing scanned yet. Run: python phototag.py scan /path/to/photos")
    sv = scene_vectors(db, cfg, {r["emb_model"] for r in rows if r["emb_model"]}, args.cache)
    places = geocode(rows, cfg)
    counts: dict[str, int] = {}
    db.execute("DELETE FROM tags")
    for r in rows:
        tags = exif_tags(json.loads(r["meta"] or "{}"), cfg, places.get(r["path"]))
        tags += computed_tags(json.loads(r["stats"]), cfg)
        if r["dets"] is not None:
            tags += detection_tags(json.loads(r["dets"]), cfg)
        scores = {}
        if sv and r["emb"] is not None and r["emb_model"] == sv[0]:
            mid, names, mat, ths, scale, bias = sv
            emb = np.frombuffer(r["emb"], dtype=np.float16).astype(np.float32)
            prob = 1 / (1 + np.exp(-(scale * (mat @ emb) + bias)))
            for n, pr, th in zip(names, prob, ths):
                scores[n] = round(float(pr), 4)
                if pr >= th:
                    tags.append(n)
        tags = sorted(set(tags))
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
        db.execute("INSERT INTO tags VALUES (?,?,?)", (r["path"], json.dumps(tags), json.dumps(scores)))
    db.commit()
    print(f"{len(rows)} photos tagged\n")
    width = max(map(len, counts), default=10)
    group = None
    for t in sorted(counts):
        g = t.split("|")[0]
        if g != group:
            print()
            group = g
        print(f"  {t:<{width}}  {counts[t]:>6}")
    pending = sum(1 for _ in _pending(db, cfg, None))
    print(f"\n{pending} sidecars to write or update. Next: python {Path(__file__).name} write --dry-run")


# ------------------------------------------------------------------ write ---


def sidecar_for(p: Path, cfg) -> Path:
    if cfg.get("sidecar", {}).get("style", "digikam") == "lightroom":
        return p.with_suffix(".xmp")
    return p.with_name(p.name + ".xmp")


def _listify(v) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _pending(db, cfg, under: Path | None, remove=False):
    root = cfg["root"]
    q = """SELECT t.path, t.tags, w.tags AS wtags FROM tags t LEFT JOIN written w ON w.path = t.path"""
    if remove:
        q = "SELECT w.path, '[]' AS tags, w.tags AS wtags FROM written w"
    for r in db.execute(q):
        if under and not r["path"].startswith(str(under) + os.sep):
            continue
        want = sorted(f"{root}|{t}" for t in json.loads(r["tags"]))
        had = json.loads(r["wtags"]) if r["wtags"] else None
        if want != had and not (had is None and not want):
            yield r["path"], want, had or []


def backup_sidecar(sc: Path, cache: Path):
    """Keep a copy of a sidecar phototag hasn't touched before, under cache/backup/<its full path>."""
    dest = cache / "backup" / sc.relative_to(sc.anchor)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sc, dest)


def write_one(et: ExifTool, src: Path, want: list[str], cfg) -> tuple[Path, str]:
    root = cfg["root"]
    sc = sidecar_for(src, cfg)
    exists = sc.exists()
    et.errors.clear()  # so a failure reports this file's errors, not an earlier one's
    d = (et.json("-XMP-lr:HierarchicalSubject", "-XMP-digiKam:TagsList", "-XMP-dc:Subject", str(sc if exists else src)) or [{}])[0]
    hier: list[str] = []
    for h in _listify(d.get("HierarchicalSubject")) + [t.replace("/", "|") for t in _listify(d.get("TagsList"))]:
        if h not in hier:
            hier.append(h)
    if not hier:  # only flat keywords so far: keep them visible as top-level tags
        hier = _listify(d.get("Subject"))
    mine = [h for h in hier if h == root or h.startswith(root + "|")]
    keep = [h for h in hier if h not in mine]
    new = keep + [w for w in want if w not in keep]
    assign = []
    for tag, sep in (("XMP-lr:HierarchicalSubject", "|"), ("XMP-digiKam:TagsList", "/")):
        if new:
            assign += [f"-{tag}={h.replace('|', sep)}" for h in new]
        else:
            assign.append(f"-{tag}=")
    if exists:
        out = et.run("-overwrite_original", *assign, str(sc)).decode("utf-8", "replace")
        ok = "updated" in out or "unchanged" in out
    else:
        # creates the sidecar, seeded with the file's own metadata; the original is not touched
        out = et.run("-o", str(sc), *assign, str(src)).decode("utf-8", "replace")
        ok = "created" in out
    if not ok:
        err = "; ".join(et.errors) or out.strip()
        raise RuntimeError(err)
    return sc, "updated" if exists else "created"


def unwritable_folders(paths) -> dict[str, str]:
    """Folders (that exist) where sidecars can't be created, with the reason."""
    bad = {}
    for d in {os.path.dirname(p) for p in paths}:
        if os.path.isdir(d) and not os.access(d, os.W_OK):
            try:
                ro = bool(os.statvfs(d).f_flag & os.ST_RDONLY)
            except OSError:
                ro = False
            bad[d] = ("they're on a read-only drive (on a Mac, NTFS drives are)" if ro
                      else "you don't have permission to write there")
    return bad


def cmd_write(args, cfg):
    db = open_db(args.cache)
    under = Path(args.root).expanduser().resolve() if args.root else None
    todo = list(_pending(db, cfg, under, remove=args.remove))
    bad = unwritable_folders(path for path, _, _ in todo)
    if bad:
        lines = []
        for why in sorted(set(bad.values())):
            folders = sorted(d for d, w in bad.items() if w == why)
            n = sum(1 for path, _, _ in todo if bad.get(os.path.dirname(path)) == why)
            lines.append(f"{n} photos can't get sidecars because {why}. For example: {folders[0]}")
        todo = [t for t in todo if os.path.dirname(t[0]) not in bad]
        msg = "\n".join(lines)
        if not todo:
            sys.exit(f"{msg}\nNothing was written. To browse your tags without sidecars or digiKam, "
                     f"run: python {Path(__file__).name} html")
        log(f"{msg}\nSkipping those; the rest are written as usual.")
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("Sidecars are up to date.")
        return
    if args.dry_run:
        for path, want, had in todo[:50]:
            add = sorted(set(want) - set(had))
            rem = sorted(set(had) - set(want))
            print(f"{path}\n   + {', '.join(add) or '-'}\n   - {', '.join(rem) or '-'}")
        if len(todo) > 50:
            print(f"... and {len(todo) - 50} more")
        print(f"\n{len(todo)} sidecars would be written. Run without --dry-run to write them.")
        return
    keep_awake()
    et = ExifTool()
    touched = {r[0] for r in db.execute("SELECT path FROM written")}  # their originals are backed up already
    stats = {"created": 0, "updated": 0, "missing": 0, "failed": 0}
    in_a_row = 0  # failures since the last success
    total, t0, last = len(todo), time.time(), 0.0

    def progress(i: int, final=False):
        nonlocal last
        now = time.time()
        if not final and now - last < 0.25:
            return
        last = now
        counts = "  ".join(f"{v} {k}" for k, v in stats.items() if v)
        print(f"\r{progress_line(i, total, now - t0, 'sidecars')}  {counts}   ",
              end="", file=sys.stderr, flush=True)

    log(f"Writing {total} sidecars...")
    i = 0
    try:
        for i, (path, want, had) in enumerate(todo, 1):
            src = Path(path)
            if not src.exists():
                stats["missing"] += 1
                in_a_row += 1
            else:
                try:
                    sc = sidecar_for(src, cfg)
                    if path not in touched and sc.exists():
                        backup_sidecar(sc, args.cache)
                    sc, what = write_one(et, src, want, cfg)
                    stats[what] += 1
                    in_a_row = 0
                    if args.remove:
                        db.execute("DELETE FROM written WHERE path=?", (path,))
                    else:
                        db.execute("INSERT OR REPLACE INTO written VALUES (?,?,?,?)", (path, json.dumps(want), str(sc), time.time()))
                    db.commit()  # record each sidecar as soon as it's written
                except Exception as e:
                    stats["failed"] += 1
                    in_a_row += 1
                    log(f"\n  ! {path}: {e}")
            progress(i)
            if et.proc.poll() is not None or in_a_row >= 20:
                progress(i, final=True)
                why = ("exiftool stopped unexpectedly" if et.proc.poll() is not None else
                       f"the last {in_a_row} photos all failed: is the drive connected and writable?")
                log(f"\nStopped: {why}\nSidecars written so far are saved; fix that and run the same command to continue.")
                break
        else:
            progress(i, final=True)
    except KeyboardInterrupt:
        progress(i, final=True)
        log("\nStopped. Sidecars written so far are saved; run the same command to continue.")
    finally:
        db.commit()
        et.close()
    log("")
    print(", ".join(f"{v} {k}" for k, v in stats.items() if v) or "nothing to do")
    if stats["missing"]:
        print("Some photos weren't found: is the drive connected?")
    print("In digiKam: reread metadata for these albums (see DIGIKAM.md for doing many folders at once).")


# ----------------------------------------------------------------- review ---


def cmd_review(args, cfg):
    db = open_db(args.cache)
    if not args.tag:
        counts = {}
        for (tags,) in db.execute("SELECT tags FROM tags"):
            for t in json.loads(tags):
                counts[t] = counts.get(t, 0) + 1
        for t in sorted(counts):
            print(f"  {counts[t]:>6}  {t}")
        return
    tag = args.tag[len(cfg["root"]) + 1:] if args.tag.startswith(cfg["root"] + "|") else args.tag
    scene = {s["tag"]: s for s in cfg.get("scene", [])}.get(tag)
    rows = db.execute("SELECT t.path, t.tags, t.scores, p.thumb FROM tags t JOIN photos p USING(path)").fetchall()
    th = None
    if scene:
        th = scene.get("threshold", cfg.get("scenes", {}).get("default_threshold", 0.1))
        items = [(json.loads(r["scores"]).get(tag), r) for r in rows]
        items = sorted([(s, r) for s, r in items if s is not None], key=lambda x: -x[0])
        lo = th * args.floor
        items = [(s, r) for s, r in items if s >= lo][: args.max]
        subtitle = (f"Ranked by score. Threshold {th:g} (red line). Showing scores down to {lo:.3g}. "
                    f"Change <code>threshold</code> for this tag in tags.toml, then re-run <code>tag</code>.")
    else:
        # a tag and everything under it: "Animals" also matches "Animals|Bird"
        items = []
        for r in rows:
            hits = [t for t in json.loads(r["tags"]) if t == tag or t.startswith(tag + "|")]
            if hits:
                items.append((None, r, hits))
        items.sort(key=lambda x: (x[2], x[1]["path"]))  # grouped by subtag
        items = items[: args.max]
        subtitle = "Photos with this tag or any tag under it."
    if not items:
        sys.exit(f"No photos for '{tag}'. Run `python phototag.py review` to list tags.")
    cards, line_drawn = [], False
    for s, r, *extra in items:
        if th is not None and not line_drawn and s < th:
            cards.append('<div class="line">threshold</div>')
            line_drawn = True
        name = html.escape(Path(r["path"]).name)
        label = f"{s:.3f} · {name}" if s is not None else name
        subs = [h[len(tag) + 1:] for h in (extra[0] if extra else []) if h != tag]
        if subs:
            label = f"<b>{html.escape(', '.join(subs))}</b> · {label}"
        p = Path(r["path"])
        img = f'<img loading="lazy" src="thumbs/{r["thumb"]}">'
        if p.suffix.lower() in BROWSER_VIEWABLE:  # browsers can show these originals, not RAWs
            img = f'<a href="{html.escape(p.as_uri())}" target="_blank" title="Open the original">{img}</a>'
        cards.append(f'<figure>{img}<figcaption>{label}</figcaption>'
                     f'<div class="path" title="Click to copy the path">{html.escape(str(p))}</div></figure>')
    safe = "".join(c if c.isalnum() else "_" for c in tag)
    out = args.cache / f"review_{safe}.html"
    out.write_text(f"""<!doctype html><meta charset="utf-8"><title>{html.escape(tag)}</title>
<style>
:root{{--bg:#f6f5f2;--fg:#222;--muted:#777}}
@media (prefers-color-scheme: dark){{:root{{--bg:#1b1b1b;--fg:#eee;--muted:#999}}}}
body{{background:var(--bg);color:var(--fg);font:14px -apple-system,system-ui,sans-serif;margin:24px}}
h1{{font-size:20px;margin:0 0 4px}} p{{color:var(--muted);margin:0 0 18px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}}
figure{{margin:0}} img{{width:100%;aspect-ratio:1;object-fit:cover;border-radius:4px;background:#8882}}
figcaption{{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.line{{grid-column:1/-1;border-top:2px solid #d33;color:#d33;font-size:12px;padding-top:2px}}
.path{{font-size:10px;color:var(--muted);word-break:break-all;cursor:copy;line-height:1.3;margin-top:1px}}
.path:hover{{color:var(--fg);text-decoration:underline}}
#toast{{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#333;color:#fff;
padding:6px 12px;border-radius:6px;font-size:12px;opacity:0;transition:opacity .2s;pointer-events:none}}
</style><h1>{html.escape(tag)} <small>({len(items)} shown)</small></h1><p>{subtitle}
Click a path to copy it (in Finder, press ⌘⇧G and paste to go there). Click a JPEG, PNG or HEIC thumbnail to open the original.</p>
<div class="grid">{''.join(cards)}</div><div id="toast">Path copied</div>
<script>
document.addEventListener("click", e => {{
  const el = e.target.closest(".path"); if (!el) return;
  const text = el.textContent, done = () => {{
    const t = document.getElementById("toast"); t.style.opacity = 1; setTimeout(() => t.style.opacity = 0, 1200); }};
  const fallback = () => {{ const a = document.createElement("textarea"); a.value = text; document.body.appendChild(a);
    a.select(); try {{ document.execCommand("copy"); done(); }} catch (_) {{}} a.remove(); }};
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
}});
</script>""", encoding="utf-8")
    print(out)
    if not args.no_open:
        webbrowser.open(out.as_uri())


# ------------------------------------------------------------------- html ---

LIBRARY_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo library</title>
<style>
:root{--bg:#f6f5f2;--panel:#fff;--fg:#1d1d1d;--muted:#5f5f5f;--line:#d9d6cf;--accent:#0b57d0;--accent-fg:#fff;--chip:#e8eefb}
@media (prefers-color-scheme: dark){:root{--bg:#161616;--panel:#202020;--fg:#ececec;--muted:#a8a8a8;--line:#3a3a3a;--accent:#8ab4f8;--accent-fg:#0b1a33;--chip:#26324a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,system-ui,sans-serif;display:grid;grid-template-columns:270px 1fr;height:100vh}
aside{background:var(--panel);border-right:1px solid var(--line);overflow:auto;padding:14px 12px}
main{overflow:auto;padding:14px 18px}
h1{font-size:17px;margin:0 0 10px}
input[type=search]{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
details{margin-top:8px}
summary{cursor:pointer;font-weight:600;padding:3px 0}
.tag{display:flex;justify-content:space-between;gap:8px;width:100%;border:0;background:none;color:var(--fg);font:inherit;text-align:left;padding:2px 6px;border-radius:4px;cursor:pointer}
.tag:hover{background:var(--chip)} .tag.on{background:var(--accent);color:var(--accent-fg)}
.tag .n{color:var(--muted);font-variant-numeric:tabular-nums} .tag.on .n{color:inherit}
.bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px}
.chip{border:0;background:var(--accent);color:var(--accent-fg);border-radius:12px;padding:3px 10px;font:inherit;cursor:pointer}
.count{color:var(--muted)} select,.btn{font:inherit;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
figure{margin:0;cursor:pointer} figure img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:4px;background:#8882;display:block}
figcaption{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
#more{height:40px}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--fg);max-width:min(720px,94vw);padding:16px}
dialog::backdrop{background:#0008}
dialog img{max-width:100%;max-height:60vh;display:block;margin:0 auto 10px;border-radius:4px}
.path{font-size:12px;word-break:break-all;color:var(--muted)}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.tags button{border:1px solid var(--line);background:var(--chip);color:var(--fg);border-radius:12px;padding:2px 9px;font:inherit;font-size:12px;cursor:pointer}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px} a.btn{text-decoration:none}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:6px 12px;border-radius:6px;font-size:12px;opacity:0;transition:opacity .2s;pointer-events:none}
@media (max-width:700px){body{grid-template-columns:1fr;height:auto} aside{border-right:0;border-bottom:1px solid var(--line);max-height:45vh}}
</style></head><body>
<aside><h1>Photo library</h1>
<input type="search" id="q" placeholder="Search file or folder names" aria-label="Search file or folder names">
<div id="facets"></div></aside>
<main><div class="bar"><span id="chips"></span><span class="count" id="count"></span>
<select id="sort" aria-label="Sort"><option value="new">Newest first</option><option value="old">Oldest first</option><option value="path">By folder</option></select>
<button class="btn" id="clear" hidden>Clear filters</button></div>
<div class="grid" id="grid"></div><div id="more"></div></main>
<dialog id="detail"><img id="d-img" alt=""><div id="d-name"></div><div class="path" id="d-path"></div>
<div class="tags" id="d-tags"></div>
<div class="row"><button class="btn" id="d-copy">Copy path</button><a class="btn" id="d-open" target="_blank">Open original</a><button class="btn" id="d-close">Close</button></div></dialog>
<div id="toast"></div>
<script>
const D = __DATA__;
const T = D.tags, P = D.photos, THUMBS = D.thumbs, VIEW = new Set(D.viewable);
const sel = new Set(); let shown = [], rendered = 0;
const $ = id => document.getElementById(id);
const name = p => p.slice(p.lastIndexOf("/") + 1);
const uri = p => "file://" + p.split("/").map(encodeURIComponent).join("/");
function toast(msg){ const t = $("toast"); t.textContent = msg; t.style.opacity = 1; setTimeout(() => t.style.opacity = 0, 1400); }
function copy(text){
  const done = () => toast("Path copied. In Finder, press ⌘⇧G and paste to go there.");
  const fallback = () => { const a = document.createElement("textarea"); a.value = text; document.body.appendChild(a); a.select();
    try { document.execCommand("copy"); done(); } catch (_) {} a.remove(); };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
}
function matches(){
  const q = $("q").value.trim().toLowerCase(), need = [...sel];
  return P.filter(p => need.every(t => p[3].includes(t)) && (!q || p[0].toLowerCase().includes(q)));
}
function update(){
  shown = matches();
  const s = $("sort").value;
  shown.sort(s === "path" ? (a, b) => a[0].localeCompare(b[0]) : s === "old" ? (a, b) => (a[2] || "9").localeCompare(b[2] || "9") : (a, b) => (b[2] || "").localeCompare(a[2] || ""));
  $("count").textContent = shown.length.toLocaleString() + " of " + P.length.toLocaleString() + " photos";
  $("clear").hidden = !sel.size && !$("q").value;
  $("chips").replaceChildren(...[...sel].map(t => { const b = document.createElement("button"); b.className = "chip";
    b.textContent = T[t].replaceAll("|", " › ") + " ×"; b.title = "Remove this filter"; b.onclick = () => toggle(t); return b; }));
  facets(); $("grid").replaceChildren(); rendered = 0; more();
}
function facets(){
  const counts = new Map();
  for (const p of shown) for (const t of p[3]) counts.set(t, (counts.get(t) || 0) + 1);
  const open = new Set([...document.querySelectorAll("#facets details[open]")].map(d => d.dataset.g));
  const groups = new Map();
  T.forEach((t, i) => { if (!counts.has(i) && !sel.has(i)) return; const g = t.split("|")[0];
    (groups.get(g) || groups.set(g, []).get(g)).push(i); });  // the group itself sorts first: "All"
  $("facets").replaceChildren(...[...groups].map(([g, ids]) => {
    const d = document.createElement("details"); d.dataset.g = g; d.open = open.has(g) || ids.some(i => sel.has(i));
    const sm = document.createElement("summary"); sm.textContent = g; d.append(sm);
    for (const i of ids) {
      const b = document.createElement("button"), parts = T[i].split("|");
      b.className = "tag" + (sel.has(i) ? " on" : ""); b.style.paddingLeft = (6 + 12 * Math.max(0, parts.length - 2)) + "px";
      const l = document.createElement("span"); l.textContent = parts.length === 1 ? "All" : parts[parts.length - 1];
      const n = document.createElement("span"); n.className = "n"; n.textContent = (counts.get(i) || 0).toLocaleString();
      b.append(l, n); b.onclick = () => toggle(i); d.append(b);
    }
    return d;
  }));
}
function toggle(i){ sel.has(i) ? sel.delete(i) : sel.add(i); update(); }
function more(){
  const frag = document.createDocumentFragment();
  for (const p of shown.slice(rendered, rendered + 240)) {
    const f = document.createElement("figure"), img = document.createElement("img"), c = document.createElement("figcaption");
    img.loading = "lazy"; img.alt = ""; img.src = THUMBS + "/" + p[1]; c.textContent = (p[2] ? p[2] + " · " : "") + name(p[0]);
    f.title = p[0]; f.append(img, c); f.onclick = () => detail(p); frag.append(f);
  }
  rendered += 240; $("grid").append(frag);
}
function detail(p){
  $("d-img").src = THUMBS + "/" + p[1]; $("d-name").textContent = (p[2] ? p[2] + " · " : "") + name(p[0]);
  $("d-path").textContent = p[0];
  $("d-tags").replaceChildren(...p[3].filter(t => T[t].includes("|")).map(t => { const b = document.createElement("button");
    b.textContent = T[t].replaceAll("|", " › "); b.title = "Show photos with this tag";
    b.onclick = () => { $("detail").close(); if (!sel.has(t)) toggle(t); }; return b; }));
  $("d-copy").onclick = () => copy(p[0]);
  const ext = p[0].slice(p[0].lastIndexOf(".")).toLowerCase(), o = $("d-open");
  o.hidden = !VIEW.has(ext); o.href = uri(p[0]);
  $("detail").showModal();
}
new IntersectionObserver(e => { if (e[0].isIntersecting && rendered < shown.length) more(); }, {root: document.querySelector("main"), rootMargin: "600px"}).observe($("more"));
let qt; $("q").oninput = () => { clearTimeout(qt); qt = setTimeout(update, 200); };
$("sort").onchange = update;
$("clear").onclick = () => { sel.clear(); $("q").value = ""; update(); };
$("d-close").onclick = () => $("detail").close();
update();
</script></body></html>
"""


def cmd_html(args, cfg):
    db = open_db(args.cache)
    rows = db.execute("SELECT t.path, t.tags, p.thumb, p.meta FROM tags t JOIN photos p USING(path) "
                      "WHERE p.thumb IS NOT NULL").fetchall()
    if not rows:
        sys.exit("No tagged photos yet. Run scan, then tag.")
    out = Path(args.out).expanduser().resolve() if args.out else args.cache / "library.html"
    ids: dict[str, int] = {}
    photos = []
    for r in rows:
        tags = set()
        for t in json.loads(r["tags"]):
            parts = t.split("|")
            tags.update("|".join(parts[:i]) for i in range(1, len(parts) + 1))  # "Animals" matches "Animals|Dog"
        m = json.loads(r["meta"] or "{}")
        dt = parse_dt(m.get("DateTimeOriginal") or m.get("CreateDate"))
        if dt:
            tags.update(("Year", f"Year|{dt.year}"))
        photos.append([r["path"], r["thumb"], dt.strftime("%Y-%m-%d") if dt else "",
                       sorted(ids.setdefault(t, len(ids)) for t in tags)])
    # tags in display order: groups alphabetically (Year newest first), then by name
    order = sorted(ids, key=lambda t: (t.split("|")[0], [(-int(x) if x.isdigit() else 0, x) for x in t.split("|")[1:]]))
    remap = {ids[t]: i for i, t in enumerate(order)}
    for p in photos:
        p[3] = sorted(remap[i] for i in p[3])
    thumbs = os.path.relpath(args.cache / "thumbs", out.parent)
    data = {"tags": order, "photos": photos, "thumbs": urllib.parse.quote(thumbs),
            "viewable": sorted(BROWSER_VIEWABLE)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(LIBRARY_PAGE.replace("__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/")),
                   encoding="utf-8")
    print(f"wrote {out} ({len(photos)} photos)")
    if not args.no_open:
        webbrowser.open(out.as_uri())


# ---------------------------------------------------------------- explain ---


def cmd_explain(args, cfg):
    db = open_db(args.cache)
    p = str(Path(args.path).expanduser().resolve())
    r = db.execute("SELECT * FROM photos LEFT JOIN tags USING(path) WHERE path=?", (p,)).fetchone()
    if not r:
        sys.exit("Not scanned yet.")
    print(p)
    if r["error"]:
        print("  error:", r["error"])
    print("\nEXIF:")
    for k, v in json.loads(r["meta"] or "{}").items():
        if k != "SourceFile":
            print(f"  {k}: {v}")
    if r["stats"]:
        print("\nComputed:", json.loads(r["stats"]))
    if r["dets"]:
        print("\nDetections (class, confidence, share of frame):")
        for d in sorted(json.loads(r["dets"]), key=lambda d: -d[1]):
            print("  ", d)
    if r["scores"]:
        print("\nScene scores:")
        for k, v in sorted(json.loads(r["scores"]).items(), key=lambda kv: -kv[1]):
            print(f"  {v:.3f}  {k}")
    if r["tags"]:
        print("\nTags:", ", ".join(json.loads(r["tags"])))
    print("Sidecar:", sidecar_for(Path(p), cfg))


# ------------------------------------------------------------------- main ---


def _interrupt(*_):
    raise KeyboardInterrupt


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=SCRIPT_DIR / "tags.toml")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="database + thumbnails (default ~/.phototag)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="analyse photos (slow; resumable; only new/changed files)")
    s.add_argument("roots", nargs="+")
    s.add_argument("--batch", type=int, default=16)
    s.add_argument("--limit", type=int, help="only analyse this many (for a trial run)")
    s.add_argument("--no-ml", action="store_true", help="EXIF and colour only; models can be run later")
    s.add_argument("--rescan", action="store_true", help="re-analyse everything")
    s.add_argument("--rescan-ml", action="store_true", help="re-run models (after changing [models])")
    s.add_argument("--retry-errors", action="store_true")
    s.add_argument("--prune", action="store_true", help="forget photos under ROOTS that no longer exist")

    sub.add_parser("tag", help="derive tags from the cache using tags.toml (fast)")

    w = sub.add_parser("write", help="write tags to .xmp sidecars")
    w.add_argument("root", nargs="?", help="only photos under this folder")
    w.add_argument("--dry-run", action="store_true")
    w.add_argument("--limit", type=int)
    w.add_argument("--remove", action="store_true", help="remove all automatic tags from sidecars")

    r = sub.add_parser("review", help="contact sheet for one tag (no tag: list tags)")
    r.add_argument("tag", nargs="?")
    r.add_argument("--max", type=int, default=600)
    r.add_argument("--floor", type=float, default=0.5, help="show scores down to threshold x this")
    r.add_argument("--no-open", action="store_true")

    h = sub.add_parser("html", help="a web page for browsing the library by tag (uses the cache only)")
    h.add_argument("out", nargs="?", help="where to save it (default ~/.phototag/library.html)")
    h.add_argument("--no-open", action="store_true")

    e = sub.add_parser("explain", help="show everything known about one photo")
    e.add_argument("path")

    args = ap.parse_args()
    args.cache = args.cache.expanduser()
    # closing the Terminal window or being killed stops cleanly, like Ctrl-C, so progress is saved;
    # SIGINT too, in case it was inherited as ignored (as for `command &` in a script)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _interrupt)
    cfg = load_config(args.config)
    {"scan": cmd_scan, "tag": cmd_tag, "write": cmd_write, "review": cmd_review,
     "html": cmd_html, "explain": cmd_explain}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()

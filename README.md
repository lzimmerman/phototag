# phototag

Offline auto-tagging for a photo library, written as XMP sidecars that digiKam reads.
Your original photos are never modified. Each photo gets a `.xmp` file next to it
(`IMG_0001.CR2` → `IMG_0001.CR2.xmp`).

All tags go under one top-level tag, `Auto`, so they stay separate from your own:

| Group | Tags | From |
|---|---|---|
| Color | Black and white, Toned | pixel colour statistics |
| Light | Low key, High key, Golden hour, Blue hour | brightness, plus sun position from GPS + time |
| Format | Horizontal, Vertical, Square, Panorama | aspect ratio |
| Season, Time of day | Summer, Evening… | capture date (hemisphere from GPS when there is one) |
| Camera, Lens, Focal length | Canon EOS R6, Wide, Telephoto… | EXIF |
| Exposure | Long exposure, High ISO, Flash | EXIF |
| Place | Country › Region › City | GPS, offline lookup |
| People | One, Two, Group, Portrait, Small figures | YOLO detector |
| Animals, Vehicles | Dog, Bird, Car, Boat… | YOLO detector |
| Scene, Subject, Style | Landscape, Mountains, Night, Food, Macro, Silhouette… | SigLIP image model |

Everything is configured in `tags.toml`: add or remove scene tags (each is just a
few descriptive phrases), change thresholds, turn groups off.

## Setup (once)

```sh
brew install exiftool python@3.12
brew install python-tk@3.12               # only needed for the window (phototag_gui.py)
mkdir -p ~/code-ai/scripts/python && cd ~/code-ai/scripts/python
unzip ~/Downloads/phototag.zip            # creates ./phototag
cd phototag
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The folder can live anywhere; adjust the paths above if you move it. Nothing in the script depends on its location:
it reads `tags.toml` from its own folder and keeps its cache in `~/.phototag`.

The first `scan` downloads the models (about 1 GB, from Hugging Face and
Ultralytics). After that everything runs offline.

**Your external drive must be writable.** Many "My Book"-style drives ship
formatted as NTFS, which macOS mounts read-only. Check in Finder (Get Info) or
Disk Utility. exFAT or APFS are fine.

## Use

Activate the environment each time (`cd ~/code-ai/scripts/python/phototag && source .venv/bin/activate`), then:

```sh
# 1. Try a small sample first (use your drive's real path: drag the folder into Terminal to paste it)
python phototag.py scan "/Volumes/Book/Photos" --limit 300
python phototag.py tag
python phototag.py review "Scene|Landscape"     # opens a ranked contact sheet

# 2. Tune tags.toml, re-run `tag` (seconds) and `review` until you like it

# 3. Whole library (resumable: Ctrl-C and re-run continues where it stopped)
python phototag.py scan "/Volumes/Book/Photos"
python phototag.py tag
python phototag.py write --dry-run

# 4. Write one album first and check it in digiKam...
python phototag.py write "/Volumes/Book/Photos/2019"

# 5. ...then the rest (photos already written are skipped)
python phototag.py write
```

### The window (optional)

`phototag_gui.py` puts the same four steps in a small window: a folder picker,
**Scan**, **Tag**, **Review** and **Write** buttons with a note under each
saying what it does and how the last run went, a progress bar, a **Stop**
button, and a log. Each button runs the same command you'd type in Terminal,
so everything above still applies.

One-time setup: Tkinter, the toolkit it uses, needs one extra Homebrew package:

```sh
brew install python-tk@3.12
```

Then double-click **`phototag.command`** in Finder (it opens a Terminal window
alongside; leave that open). Or run `python phototag_gui.py` with the
environment active. If macOS refuses to open `phototag.command`, run
`chmod +x phototag.command` once in this folder.

- **Write starts in Preview mode** (dry run) for safety. Untick
  *Preview only* to actually write sidecars.
- **Only photos in the folder above** limits Write to the chosen folder,
  handy for writing one album and checking it in digiKam first.
- **Stop** works like Ctrl-C: progress so far is saved.
- Under the progress bar, Scan and Write show how far they've got, the
  speed, and the time left (shown once there's enough to estimate from).
- The **Review** list fills in after a Tag run and includes top-level tags.
- **High contrast** (top right) switches to a bolder look: black on white,
  or white on black in dark mode, with solid-coloured buttons and larger
  text. Without it, the window uses the normal macOS look, whose buttons
  only macOS can restyle: turn on System Settings → Accessibility →
  Display → *Increase contrast* to strengthen them.
- The window remembers the last folder, tag and High contrast setting in
  `~/.phototag/gui.json`.

On an M1 Pro, expect very roughly 5–15 photos per second, depending mostly on
how fast the drive reads RAW files. Later scans only look at new or changed files.

### Commands

| Command | What it does |
|---|---|
| `scan ROOT…` | Analyse photos and cache the results in `~/.phototag`. `--limit N` for a trial, `--no-ml` for EXIF/colour only (quick), `--rescan-ml` after changing models, `--prune` to forget deleted files. |
| `tag` | Apply `tags.toml` to the cache and print counts per tag. Fast; re-run after any config change. |
| `review TAG` | Contact sheet of photos ranked by score, with a red line at the threshold. Without a tag: list all tags and counts. |
| `explain FILE` | Every EXIF value, statistic, detection and scene score for one photo. Useful for "why was this tagged Night?" |
| `write [FOLDER]` | Write or update sidecars (only those that changed). `--dry-run` to preview. |
| `write --remove` | Take all `Auto` tags back out of the sidecars. |

### Reviewing and tuning tags

`review` builds a contact sheet in your browser from the thumbnails saved
during `scan`. It only reads: it never changes photos, sidecars or tags.

**`review` with no tag** lists every tag and how many photos have it, as of
your last `tag` run.

**`review` with a scene tag** (any `[[scene]]` entry in `tags.toml`, such as
`Scene|Landscape` or `Style|Silhouette`) is the one for tuning:

- Every photo has a score from 0 to 1 for every scene tag, not just the tags
  it ended up with. The page ranks photos from highest score to lowest.
- A red line marks the threshold. Photos above it get the tag; photos below don't.
- Each thumbnail shows its score and filename. Hover over it for the full path.
- By default the page stops at half the threshold, so you see the near misses
  without hundreds of irrelevant photos. `--floor 0` shows everything, and
  `--max` changes the 600-photo cap.

**`review` with any other tag** (Color, People, Camera, Season…) just shows the
photos that have it, unranked. Those tags are yes/no rules, not scores.

**`review` with a top-level tag** such as `Animals`, `Vehicles` or `Scene`
shows every photo tagged with anything under it, grouped by subtag, with the
subtag(s) in bold in each caption. There is no ranking or threshold line here,
because each subtag has its own threshold; review a single scene tag to tune one.

**How to use it:** scroll to the red line and look on both sides of it.

- **Wrong photos just above the line:** the threshold is too low, so raise it.
- **Good photos just below the line:** lower it.
- **Right and wrong photos mixed all the way down:** a threshold won't fix
  that. Reword the tag's prompts or add new ones.

Set a threshold by adding a line such as `threshold = 0.15` to that tag's
`[[scene]]` entry (otherwise `default_threshold` applies):

```toml
[[scene]]
tag = "Scene|Landscape"
prompts = ["a landscape photograph", "a scenic view of nature", "wide open countryside scenery"]
threshold = 0.15
```

A changed threshold shows up in `review` straight away, because the page reads
it from the config. Changed prompts need a `tag` run first, because that's when
scores are recalculated. Either way, run `tag` before `write` so the sidecars
get the new results.

Practical points:

- With a few dozen photos the ranking is thin; a few hundred gives a much
  clearer picture of where the cutoff should go.
- Each thumbnail shows the photo's full path: click it to copy, then in Finder
  press ⌘⇧G and paste to go straight to the file. Clicking a JPEG, PNG or HEIC
  thumbnail opens the original in a new tab (browsers can't display RAW files).
- Pages are saved in `~/.phototag` as `review_<tag>.html`. Add `--no-open` to
  create one without opening a browser.
- To see why one photo did or didn't get a tag, run `explain` on it: it shows
  all of that photo's scene scores at once.

## digiKam settings (once)

More detail, including bulk rereading across subfolders, is in `DIGIKAM.md`.

1. **Settings › Configure digiKam › Metadata › Sidecars**: turn on **Read from sidecar files**.
2. After each `write`, select the albums and run **Reread Metadata From Files**
   (Item menu) so digiKam picks up the new tags.
3. Browse in the **Tags** view under `Auto`, or combine tags in the filter panel
   (e.g. *Scene › Landscape* AND *Color › Black and white*).

If you remove an automatic tag by hand in digiKam, phototag leaves it removed
until its own rules for that photo change.

## First run and troubleshooting

**Testing status.** The EXIF tags, colour and B&W/sepia detection, RAW preview
handling (Canon and Nikon samples) and sidecar writing were tested: originals
stayed byte-for-byte unchanged, and existing keywords were kept. The two AI models
(YOLO detection and SigLIP scene scoring) could not be downloaded in the
environment where this was written, so they were tested with stand-ins. Your
first `scan --limit 300` is the real test of those two parts.

What to watch for on that first run:

- **Model download.** The first scan pauses while about 1 GB of models
  downloads. If it fails, check your internet connection and re-run; the
  download resumes.
- **"Couldn't load ViT-B-16-SigLIP2-256 … falling back".** Your open_clip version
  doesn't have the newer model. The fallback works fine; to silence the message,
  set `clip = "ViT-B-16-SigLIP"` in `tags.toml`.
- **"YOLO failed on mps … switching to cpu".** A rare Apple GPU problem in
  Ultralytics. Detection still works, just slower.
- **Scene tags look wrong or fire on everything.** That means the thresholds
  need tuning (see above), not that something is broken. Use `explain FILE` to
  see a photo's scores.
- **"Not found …"** The path is wrong, or the drive isn't mounted. Drag the
  folder into Terminal to get its exact path. Don't combine quotes and
  backslashes: use `~/"Pictures/2023 - Best Of"` or `~/Pictures/2023\ -\ Best\ Of`.
- **Hugging Face "unauthenticated requests" warning.** Harmless, and gone once the
  model is downloaded, because phototag then loads it offline. To make it check
  online for a newer model version, run with `HF_HUB_OFFLINE=0 python phototag.py …`.
- **Permission errors when writing.** The drive is read-only (see the NTFS note
  above).

If something else breaks, the full error text is what's needed to fix it.

## Where files go

- **Next to your photos:** only the `.xmp` sidecars, created by `write`. digiKam
  looks for sidecars in the photo's own folder. Photos themselves are never changed.
  `scan`, `tag`, `review` and `explain` write nothing in your photo folders.
- **`~/.phototag`:** database, thumbnails, review pages and the YOLO model.
  Use `--cache PATH` on every command (or set `PHOTOTAG_CACHE`) to keep it
  elsewhere, e.g. `~/code-ai/scripts/python/phototag/cache`.
- **`~/.cache/huggingface`:** the SigLIP scene model.
- **`~/Library/Application Support/Ultralytics`:** a small settings file.
- **`~/.phototag/gui.json`:** the window's last folder, tag and High contrast setting.

## Notes and limits

- **RAW files are judged by their embedded JPEG preview**: what the camera
  produced, not your Lightroom/Capture One edits. A RAW you converted to B&W in an
  editor will only be tagged B&W through the exported JPEG.
- **Dates come from camera clocks.** If a camera was on the wrong time zone,
  Season/Time of day/Golden hour will be off for those photos.
- Existing keywords (embedded in JPEGs or in existing sidecars) are kept; only
  tags under `Auto` are ever changed or removed.
- If you previously used Lightroom-style `IMG_0001.xmp` sidecars, set
  `style = "lightroom"` in `tags.toml` and the matching sidecar naming option in digiKam.
- The cache (database + small thumbnails) lives in `~/.phototag`. Deleting it just
  means the next scan starts over.

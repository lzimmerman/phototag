# phototag

Offline auto-tagging for a photo library, written as XMP sidecars that digiKam reads.
Your original photos are never modified. Each photo gets a `.xmp` file next to it
(`IMG_0001.CR2` → `IMG_0001.CR2.xmp`). Without digiKam, or for photos on a
read-only drive, a web page lets you browse everything by tag instead (see
"Browsing without digiKam").

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
| People | One, Two, Group, Portrait, Small figures | YOLO detector (not on screenshots) |
| Animals | Dog, Cat, Bird › Cardinal, Hawk, Egret…, Wild animals › Coyote, Squirrel…, Insects… | YOLO detector finds the animal; the SigLIP image model names it |
| Vehicles | Car, Boat… | YOLO detector |
| Scene, Subject, Style | Landscape, Coast and beach, Night, Flowers, Macro… | SigLIP image model (not on film negatives) |
| Screenshot | screenshots, saved memes and comics | SigLIP image model |
| Astro | Moon, Night sky, Aurora, Lunar eclipse | SigLIP image model |
| Film | Negative (scanned colour and B&W film negatives) | SigLIP image model, plus colour statistics for orange colour negatives |

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

`phototag_gui.py` puts the same steps in a small window: a folder picker,
**Scan**, **Tag**, **Review**, **Write** and **Web page** buttons with a note under each
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
- **Retry unreadable photos** (next to Scan) tries the photos an earlier scan
  couldn't read again, e.g. after reconnecting a drive or repairing files.
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

On an M1 Pro with photos on a USB drive, a mix of Nikon RAW and JPEG scans at
about 5–6 photos per second, so roughly an hour per 20,000 photos. Large TIFF and
PNG exports are slower, since every byte has to be read. Later scans only look at
new or changed files.

### Commands

| Command | What it does |
|---|---|
| `scan ROOT…` | Analyse photos and cache the results in `~/.phototag`. `--limit N` for a trial, `--no-ml` for EXIF/colour only (quick), `--rescan-ml` after changing models, `--retry-errors` to try unreadable photos again, `--prune` to forget deleted files. |
| `tag` | Apply `tags.toml` to the cache and print counts per tag. Fast; re-run after any config change. |
| `review TAG` | Contact sheet of photos ranked by score, with a red line at the threshold. Without a tag: list all tags and counts. |
| `explain FILE` | Every EXIF value, statistic, detection and scene score for one photo. Useful for "why was this tagged Night?" |
| `write [FOLDER]` | Write or update sidecars (only those that changed). `--dry-run` to preview. |
| `write --remove` | Take all `Auto` tags back out of the sidecars. |
| `html [FILE]` | A web page for browsing the whole library by tag, with thumbnails (see below). Uses only the cache. |

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

Set a threshold with the `threshold` line in that tag's `[[scene]]` entry
(otherwise `default_threshold` applies):

```toml
[[scene]]
tag = "Scene|Landscape"
prompts = ["a landscape photograph", "a scenic view of nature", "wide open countryside scenery"]
threshold = 0.002
```

Scores for broad tags like Landscape are small: on a 29,000-photo library the
best landscapes scored about 0.03 and the useful cutoff was 0.002. Specific
tags (Moon, Film negative) score much higher. The thresholds in `tags.toml`
were set from contact sheets of that library; Silhouette and Aerial found
nothing reliable there, so they're switched off with `threshold = 1.0`.

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

### Naming animals

The detector (YOLO) finds animals but only knows ten kinds, so on its own it
calls a coyote a dog or a cow, and a squirrel a bear. When it finds an animal,
the scene model picks the closest match from the list in `[animals.species]` in
`tags.toml` instead, if it's at least 80% sure (`min_confidence`). Otherwise
the detector's tag stays.

To add an animal you photograph, add a line `"a <description>" = "<tag>"`, then
run `tag` (no re-scan needed) and check it with `review`:

```toml
[animals.species]
"a coyote" = "Animals|Wild animals|Coyote"     # already in the list
"an opossum" = "Animals|Wild animals|Opossum"
"a monarch butterfly" = "Animals|Insects|Butterfly|Monarch"
"a drawing or sign of an animal" = ""         # "" = no tag at all
```

- Describe it the way you'd caption a photo of it: `"an opossum"`, not just
  `"opossum"`.
- Each photo gets only the single best match. Similar entries share the
  confidence between them; if none reaches `min_confidence`, the detector's tag
  stays. Keep the general entries (`"a bird"`) as a fallback.
- `min_confidence = 0.8` is a balance. On a test library, lowering it to 0.7
  caught one more coyote but also tagged dandelions as insects. Raise it if you
  see wrong animals; lower it if too many keep the detector's guess.

**Birds** get a second step. A photo tagged *Bird* is matched against
`[birds.species]`, about 45 common North American and coastal birds, and gets
e.g. *Animals › Bird › Cardinal* when the scene model is at least 80% sure. If
not, it stays plain *Bird*. Add your own the same way:

```toml
[birds.species]
"a great blue heron" = "Animals|Bird|Heron"      # already in the list
"a red-tailed hawk" = "Animals|Bird|Hawk|Red-tailed hawk"
"a snowy owl" = "Animals|Bird|Owl|Snowy owl"
"a raven" = "Animals|Bird|Crow"                   # several can share one tag
```

Entries that share a tag pool their confidence, so a black bird that's half
crow and half raven still counts as a confident *Crow*. On a test library of
about 1,200 bird photos, roughly 60% got a species. Most were right, but the
model can't really tell a crow from a raven or one small shorebird from
another, and it calls a young night heron a shorebird. Distant, backlit or
dark birds mostly stay plain *Bird*. Check with `review "Animals|Bird"`.

### Fixing a single photo by hand

When phototag gets one photo wrong, say a coyote tagged *Auto › Animals › Farm
animals*, fix it in digiKam:

1. **Remove the wrong `Auto` tag** (right-click it in the Tags panel). It stays
   removed: the next time `write` rewrites that photo, it sees the tag has gone,
   remembers that, and leaves it off from then on. `write` reports how many such
   tags it's keeping off, and the web page leaves them out too. To undo, assign
   the tag again in digiKam; phototag then keeps it.
2. **Add the right tag as your own, outside `Auto`**, e.g. create *Animals ›
   Coyote* and assign it. phototag never touches tags outside `Auto`.

Don't add tags under `Auto` yourself: phototag manages that branch and drops
tags there that it didn't write. Removals are seen only if digiKam writes your
edits into the `.xmp` sidecars (see `DIGIKAM.md`, one-time setting 4). When the
same mistake happens on many photos, fix the rule in `tags.toml` instead (a
threshold, prompts or the animal list), then run `tag` and `write`.

## Browsing without digiKam

`python phototag.py html` (or **5. Web page** in the window) makes one web page
of every tagged photo, with its thumbnail, and opens it in your browser:

- **Filter by tag** in the sidebar: tick several to narrow down (Dog + Golden
  hour + 2019). **All** at the top of a group matches anything in it, e.g. any
  animal. Counts update as you filter.
- **Year** comes from the capture date, so you can filter by it like a tag.
- **Search** matches file and folder names. Sort by newest, oldest or folder.
- **Click a photo** for its details: all its tags (click one to filter by it),
  **Copy path** (then ⌘⇧G in Finder to go there), and **Open original** for
  JPEG and PNG files (browsers can't show RAW files).

It's built only from what Scan saved in `~/.phototag`, so it never touches the
photo drive. That makes it the way to use the tags when the drive is read-only
or you don't use digiKam, and browsing works with the drive unplugged (opening
originals needs it connected). It's saved as `~/.phototag/library.html`; run
`html` again after `tag` to update it. Give a file name (`html ~/Desktop/photos.html`)
to save it somewhere else; it still loads thumbnails from `~/.phototag`, so it
only works on this Mac.

## digiKam settings (once)

More detail, including bulk rereading across subfolders, is in `DIGIKAM.md`.

1. **Settings › Configure digiKam › Metadata › Sidecars**: turn on **Read from sidecar files**.
2. After each `write`, select the albums and run **Reread Metadata From Files**
   (Item menu) so digiKam picks up the new tags.
3. Browse in the **Tags** view under `Auto`, or combine tags in the filter panel
   (e.g. *Scene › Landscape* AND *Color › Black and white*).

To correct a single photo by hand, see "Fixing a single photo by hand" above.

## First run and troubleshooting

**Testing status.** Every step has been run on a real library of a few
thousand photos on an Apple Silicon Mac, including both AI models (YOLO
detection and SigLIP scene scoring, on the Apple GPU) and writing sidecars that
digiKam reads. Originals stayed byte-for-byte unchanged, and existing keywords
were kept. Stopping and resuming, unreadable files, read-only folders and
existing sidecars with hand-added tags were also tested on a separate sample.

What to watch for on a first run:

- **Model download.** The first scan pauses while about 1 GB of models
  downloads. If it fails, check your internet connection and re-run; the
  download resumes.
- **"Couldn't load ViT-B-16-SigLIP2-256 … falling back".** Your open_clip version
  doesn't have the newer model. The fallback works fine; to silence the message,
  set `clip = "ViT-B-16-SigLIP"` in `tags.toml`.
- **"YOLO failed on mps … switching to cpu"** (or "SigLIP failed …"). A rare
  Apple GPU problem, such as running out of GPU memory. Scanning carries on,
  just slower.
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

### Long runs

- **Stopping is safe at any point.** Stop, Ctrl-C, closing the window or the
  Terminal window it opened, or the drive disconnecting: Scan keeps every batch
  it finished (16 photos), Write keeps every sidecar it wrote, and running the
  same step again continues from there. Tag either finishes or leaves the
  previous tags as they were.
- **Your Mac stays awake** while Scan or Write runs (the screen can still turn
  off), so the drive isn't disconnected by sleep.
- **Photos that can't be read** are listed as they come up and counted at the
  end; the rest carry on. Try them again with `--retry-errors`.
- **Write checks first for folders it can't write to** (a read-only drive, or
  no permission), says how many photos that affects and why, and writes the
  rest. If nothing can be written, it suggests the web page instead.
- **Write stops by itself after 20 failures in a row** (usually a
  disconnected drive) instead of failing every remaining photo.
- **Photo library packages are skipped:** Apple Photos, iPhoto and Aperture
  libraries, and Lightroom's previews. phototag never looks inside them or writes
  sidecars there. Scan names each one it skips.
- **Disk space:** thumbnails take about 15 KB per photo in `~/.phototag`, about
  1.5 GB per 100,000 photos.

If something else breaks, the full error text is what's needed to fix it.

## Where files go

- **Next to your photos:** only the `.xmp` sidecars, created by `write`. digiKam
  looks for sidecars in the photo's own folder. Photos themselves are never changed.
  `scan`, `tag`, `review` and `explain` write nothing in your photo folders.
- **`~/.phototag`:** database, thumbnails, review pages, the library page
  (`library.html`) and the YOLO model.
  Use `--cache PATH` on every command (or set `PHOTOTAG_CACHE`) to keep it
  elsewhere, e.g. `~/code-ai/scripts/python/phototag/cache`.
- **`~/.cache/huggingface`:** the SigLIP scene model.
- **`~/Library/Application Support/Ultralytics`:** a small settings file.
- **`~/.phototag/backup`:** the first time `write` changes a sidecar that
  already existed (one digiKam wrote, say), a copy of the original goes here,
  under its full path. Sidecars phototag created itself aren't copied.
- **`~/.phototag/gui.json`:** the window's last folder, tag and High contrast setting.

## Notes and limits


- **Film|Negative** combines a scene tag (which finds black-and-white negatives
  and film strips) with a colour rule for orange colour negatives, in `[film]` in
  `tags.toml`. The colour rule needs a reading Scan saves from October 2026 on,
  so for photos scanned earlier, run `scan FOLDER --rescan` once to include them.
  On a test library about 97% of the photos it tagged were negatives; the misses
  were a few warm sunset or silhouette shots and some positive slide scans.

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
- `phototag README.pdf` and `phototag with digiKam.pdf` are printable copies of
  this file and `DIGIKAM.md`. After editing either one, run `./make_readme_pdf.sh`
  to rebuild both (needs `brew install pandoc` and Google Chrome).

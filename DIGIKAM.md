# Using phototag with digiKam

Notes on the digiKam side: settings, getting new tags in, browsing by them, and cleaning up.
Menu names can shift a little between digiKam versions. Every feature named here
has been in digiKam for years.

## One-time settings

Open **Settings › Configure digiKam › Metadata**.

1. **Sidecars tab:** turn on **Read from sidecar files**. Without it, digiKam
   ignores the `.xmp` files phototag writes.
2. **Sidecars tab, sidecar naming:** leave it on digiKam's default
   (`IMG_0001.CR2.xmp`), which matches phototag's default. Change it only if
   you also set `style = "lightroom"` in `tags.toml`.
3. **Behavior tab:** make sure **Tags** is ticked under the settings that write
   information to the files' metadata. Tags you add yourself then get saved
   into the files and sidecars too, not only into digiKam's database.
   See "Before you reread" below for why this matters.
4. **Sidecars tab: turn on writing to sidecar files**, for JPEGs as well as RAW
   files (the option that writes to the XMP sidecar, or to the image and the
   sidecar). Your own tag edits then land in the same `.xmp` files phototag
   uses, which is how phototag sees an Auto tag you've removed and keeps it off
   (see "Correcting tags"). Writing RAW metadata to sidecars only also means
   the RAW files themselves are never written to.

## Getting new tags into digiKam

After `python phototag.py write`, digiKam needs to reread the files. It doesn't
notice sidecar changes on its own.

### A few folders: reread the album tree in one go

1. Select the parent album in the Albums view.
2. Turn on **View › Include Album Sub-Tree**. The thumbnail pane now shows
   photos from all the subfolders together.
3. Select all (⌘A).
4. Run **Item › Reread Metadata From Files**.

### Many folders or the whole library: use Maintenance

1. Open **Tools › Maintenance**.
2. At the top, choose the album(s) to process. A parent album includes its subalbums.
   You can also pick the whole collection.
3. Tick **Sync Metadata and Database** and set the direction to
   **From image metadata to database**.
4. Untick the other maintenance tasks so only the sync runs, then click **OK**.

This runs in the background with a progress bar, so it's the better choice
after a full-library `write`.

### Before you reread

Rereading replaces what digiKam's database holds with what's in the files. Any
tag you added in digiKam that hasn't been written to the files yet would be lost.
Setting 3 above prevents this. If you're unsure, run
**Item › Write Metadata to Files** (or the Maintenance sync in the
*database to image metadata* direction) on those albums first.

Run phototag's `write` and digiKam's reread one after the other, not while you're
editing tags on the same photos in digiKam.

## Browsing by tag

- **Tags view (left sidebar):** expand **Auto** to see every group (Scene,
  Color, People…). Click a tag to see all its photos, from any album.
- **Combine tags:** use the **Tags filter** in the right sidebar. Tick several
  tags and choose **AND** (all must match, e.g. *Scene › Landscape* + *Color ›
  Black and white*) or **OR** (any).
- **Save combinations you reuse:** build it in **Advanced Search** (e.g. Tag is
  *Auto/Scene/Mountains* AND Tag is *Auto/Season/Winter*) and save it. It then
  appears in the Searches sidebar and stays up to date as new photos get tagged.
- **Tags filter plus the album tree:** with Include Album Sub-Tree on, select a
  year folder, then filter by tag to narrow to that year only.
- **Keep your own tags tidy:** all automatic tags sit under **Auto**, so you can
  collapse that branch when you're working with your own tags.

## Correcting tags

- **Remove a wrong Auto tag** the normal way: right-click it in the Tags panel.
  It stays removed: next time phototag rewrites that photo's sidecar, it sees
  the tag has gone and leaves it off from then on, even if its rules would give
  it again. Write reports how many such tags it's keeping off.
- **Changed your mind?** Assign the tag again in digiKam. phototag sees it's
  back and keeps it.
- **Add the right tag as your own, outside Auto.** Example: a coyote tagged
  *Auto › Animals › Farm animals*: remove that, then create your own tag
  *Animals › Coyote* and assign it. phototag never touches tags outside Auto.
- **Don't add or rename tags under Auto by hand:** phototag manages that branch
  and drops tags there that it didn't write.
- **A tag that's wrong across many photos** is better fixed in phototag. Raise
  its threshold, reword its prompts or extend the animal list in `tags.toml`
  (see "Naming animals" in the README), then run `tag` and `write`, and reread
  in digiKam.

This relies on digiKam writing your edits into the `.xmp` sidecars (setting 4
above), and on rereading after each `write` (see "Before you reread"). If
digiKam only keeps an edit in its database, or writes it into the JPEG itself,
phototag can't see it, and a removed tag comes back when phototag rewrites that
photo.

## Removing automatic tags entirely

1. Run `python phototag.py write --remove`.
2. Reread the albums in digiKam (see above). The photos lose their Auto tags.
3. The empty **Auto** branch stays in digiKam's tag tree. Delete it in the Tags
   view (right-click › Delete) or in **Tags › Tags Manager**.

## Troubleshooting

- **No Auto tags after a reread:** check that *Read from sidecar files* is on,
  and that a `.xmp` file sits next to the photo in Finder.
- **Tags show for RAWs but not JPEGs (or the other way round):** check the
  sidecar naming setting matches `style` in `tags.toml`.
- **Some tags appear twice in the tree:** usually a tag was created by hand
  with the same name somewhere else. Merge them by dragging one onto the other
  in Tags Manager.

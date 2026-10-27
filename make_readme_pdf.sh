#!/bin/sh
# Rebuild the printable copies of the docs after editing them:
#   README.md  -> "phototag README.pdf"
#   DIGIKAM.md -> "phototag with digiKam.pdf"
#
#     ./make_readme_pdf.sh
#
# Needs pandoc (brew install pandoc) and Google Chrome, which prints the PDFs.
set -e
cd "$(dirname "$0")"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
command -v pandoc >/dev/null || { echo "pandoc is missing: brew install pandoc" >&2; exit 1; }
[ -x "$CHROME" ] || { echo "Google Chrome is missing (expected in /Applications)" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# build MARKDOWN PDF TITLE: TITLE also appears in the page footer
build() {
    cat > "$tmp/doc.css" <<EOF
@page { size: letter; margin: 0.75in 0.85in 0.6in;
  @bottom-center { content: "$3 · " counter(page) "/" counter(pages); font: 8pt Helvetica, sans-serif; color: #222; } }
EOF
    cat >> "$tmp/doc.css" <<'EOF'
body { font-family: "DejaVu Sans", Verdana, sans-serif; font-size: 10.5pt; line-height: 1.5; color: #222; }
h1 { font-size: 24pt; border-bottom: 2px solid #222; padding-bottom: 6px; margin-top: 0.3in; }
h2 { font-size: 15pt; border-bottom: 1px solid #ccc; padding-bottom: 4px; margin-top: 1.4em; }
h3 { font-size: 12.5pt; margin-top: 1.3em; }
h1, h2, h3 { break-after: avoid; }
code { font-family: "DejaVu Sans Mono", Menlo, monospace; font-size: 8.5pt; background: #f2f2f2; padding: 1px 3px; border-radius: 3px; }
pre { background: #f3f3f3; border: 1px solid #ddd; border-radius: 4px; padding: 10px 13px; line-height: 1.75; white-space: pre-wrap; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: 9.5pt; }
th, td { border: 1px solid #ccc; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #eee; }
tr { break-inside: avoid; }
EOF
    rm -f "$tmp/doc.pdf"
    pandoc -f gfm "$1" -s --syntax-highlighting=none --metadata pagetitle="$3" \
        --css "$tmp/doc.css" --embed-resources -o "$tmp/doc.html"
    "$CHROME" --headless --disable-gpu --no-pdf-header-footer \
        --print-to-pdf="$tmp/doc.pdf" "file://$tmp/doc.html" >/dev/null 2>&1 || true   # can exit non-zero on shutdown after writing
    [ -s "$tmp/doc.pdf" ] || { echo "Chrome didn't produce a PDF for $1" >&2; exit 1; }
    mv "$tmp/doc.pdf" "$2"
    echo "wrote $2"
}

build README.md "phototag README.pdf" "phototag README"
build DIGIKAM.md "phototag with digiKam.pdf" "phototag with digiKam"

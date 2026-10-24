#!/bin/bash
# Double-click in Finder to open the phototag window.
cd "$(dirname "$0")" || exit 1
source .venv/bin/activate || { echo "No .venv here - see README.md (Setup)."; read -r; exit 1; }
python phototag_gui.py

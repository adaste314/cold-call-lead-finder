#!/usr/bin/env bash
# Build a standalone, double-clickable executable of the Cold-Call Lead Finder.
# Output lands in dist/.  Run this on the OS you want a binary for.
set -euo pipefail
cd "$(dirname "$0")"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

python -m PyInstaller \
  --onefile \
  --windowed \
  --name "ColdCallLeadFinder" \
  app.py

echo
echo "Built: dist/ColdCallLeadFinder"
echo "  macOS:   open dist/ColdCallLeadFinder.app  (or run dist/ColdCallLeadFinder)"
echo "  Windows: dist\\ColdCallLeadFinder.exe"
echo "  Linux:   ./dist/ColdCallLeadFinder"

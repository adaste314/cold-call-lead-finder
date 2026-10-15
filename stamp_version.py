#!/usr/bin/env python3
"""Stamp a version string into app.py's VERSION constant at build time.

Used by the release workflow: `python stamp_version.py 1.0.8` rewrites the
VERSION line so the packaged binary reports its real version.
"""
import pathlib
import re
import sys

version = sys.argv[1].lstrip("v")
path = pathlib.Path(__file__).with_name("app.py")
text = path.read_text(encoding="utf-8")
new, n = re.subn(r'^VERSION = ".*"$', f'VERSION = "{version}"', text, count=1, flags=re.M)
if n != 1:
    raise SystemExit("Could not find the VERSION line to stamp in app.py")
path.write_text(new, encoding="utf-8")
print(f"Stamped VERSION = {version}")

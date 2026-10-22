#!/usr/bin/env python3
"""Inject the shared-list upload token into app.py at build time.

Reads the SHARED_TOKEN env var (provided by a GitHub Actions secret), base64-
encodes it, and writes it into app.py's SHARED_TOKEN_B64 constant so release
binaries can upload without any per-user setup. The raw token never appears in
source or in build logs. No-op when SHARED_TOKEN is unset (dev builds).
"""
import base64
import os
import pathlib
import re

token = os.environ.get("SHARED_TOKEN", "").strip()
if not token:
    print("SHARED_TOKEN not set; leaving placeholder empty")
    raise SystemExit(0)

enc = base64.b64encode(token.encode()).decode()
path = pathlib.Path(__file__).with_name("app.py")
text = path.read_text(encoding="utf-8")
new, n = re.subn(r'^SHARED_TOKEN_B64 = ".*"$', f'SHARED_TOKEN_B64 = "{enc}"',
                 text, count=1, flags=re.M)
if n != 1:
    raise SystemExit("Could not find SHARED_TOKEN_B64 line to stamp in app.py")
path.write_text(new, encoding="utf-8")
print("Injected shared upload token")

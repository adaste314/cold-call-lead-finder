# Cold-Call Lead Finder

A small desktop app that finds local businesses near you with **no website** or a
**bad website**, and gives you the specific problems to use as talking points when
you cold call them. Built for web-design / web-dev outreach.

No sign-up, no API keys, nothing to configure. Detect your location (or type a
city), pick a radius, and get a ranked, sortable list with the issues spelled out.

## Download & run

Grab the executable for your OS from the [**Releases**](../../releases) page — no
Python install required.

| OS | File | How to run |
|----|------|-----------|
| **macOS** | `ColdCallLeadFinder-macos.zip` | Unzip, double-click the app. First launch is blocked — open **System Settings → Privacy & Security**, scroll down, click **Open Anyway**, then open it again. |
| **Windows** | `ColdCallLeadFinder-windows.exe` | Double-click. SmartScreen → **More info → Run anyway**. |
| **Linux** | `ColdCallLeadFinder-linux` | `chmod +x ColdCallLeadFinder-linux && ./ColdCallLeadFinder-linux` |

The binaries are unsigned (no paid code-signing cert), which is why the OS shows a
first-run warning — that's expected for indie tools, not an actual malware detection.

> **macOS one-liner alternative:** instead of the Settings click, clear the download
> flag in Terminal: `xattr -dr com.apple.quarantine ~/Downloads/ColdCallLeadFinder.app`

## Using it

1. Leave **Location** blank to use your current location, or type a city/address.
2. Set the **radius** in meters (2000 = ~1.25 miles).
3. Click **Find Leads**. Red rows = no website, amber = website with problems.
4. Click any row to see its phone number and talking points.
5. **Export CSV** for your CRM, or **Open HTML report** for a shareable page with
   tap-to-call links.

Leads are ranked by opportunity: no website at all scores highest, then dead/broken
sites, then sites with fixable problems.

## What counts as a "bad website"

| Check | Why it matters on the call |
|-------|----------------------------|
| No website | Invisible to online searchers |
| Doesn't load / dead | Listed site is broken |
| Redirect loop | Never finishes loading |
| No HTTPS | Chrome flags it "Not Secure" |
| Broken/expired SSL | Security warning blocks visitors |
| Not mobile-friendly | Breaks on phones (most traffic) |
| Missing title / meta description | Hurts Google ranking & snippets |
| Flash | Dead tech, won't run anywhere |
| Stale copyright (2+ yrs old) | Looks abandoned |
| Slow load (>2.5s) | Visitors bounce |

## Where the data comes from (all free, no key)

- **Location** — your IP, or a typed address geocoded via OpenStreetMap Nominatim
- **Businesses** — OpenStreetMap Overpass API
- **Website issues** — the app fetches each site live and inspects it

## Run from source / build it yourself

```bash
pip install -r requirements.txt

python app.py          # desktop app
python leads.py        # command-line version (--address, --radius, --limit, ...)

./build.sh             # produce a standalone executable in dist/
```

GitHub Actions (`.github/workflows/build.yml`) builds macOS/Windows/Linux binaries
on every push, and attaches them to a Release when you push a tag:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

## Notes & etiquette

- Phone numbers come from OpenStreetMap and are sometimes missing — the app flags
  those so you look them up first. Respect Do-Not-Call rules and local regulations.
- "Dead" sites are verified by trying https/http and www/non-www variants before
  being flagged, but OSM data can lag reality — glance at a lead before you claim a
  specific flaw on a call.
- Overpass/Nominatim are free community servers; keep your volume reasonable.

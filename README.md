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
2. Set the **radius** in miles (default 1.5).
3. Click **Find Leads**. Red rows = no website, amber = website with problems.
4. Click any row for a full breakdown: a **SWOT analysis**, detailed **talking
   points**, and a **suggested price** for that specific business.
5. **Export CSV** for your CRM, or **Open HTML report** for a shareable page with
   tap-to-call links. Both carry the SWOT, talking points, and pricing.

Leads are ranked by opportunity: no website at all scores highest, then dead/broken
sites, then sites with fixable problems.

## Tracking who you've called

Tick the **Called?** box on a row once you've phoned a business. It's hidden from the
leads list and saved permanently, so it won't reappear in future searches either.

- **Called (N)** button — opens the list of businesses you've marked called. Select
  any and **Restore to leads** to bring them back, or **Clear all** to wipe the list.
- **Export list… / Import list…** (inside the Called window) — the called list is a
  JSON file (`~/.cold_call_lead_finder/called.json`). Export yours to share it;
  import a teammate's file to merge their called businesses into yours (so two people
  working the same area don't call the same places twice).

## Updating

The **Update** button pulls the latest version:

- Running from the source checkout → it runs `git pull` and offers to restart.
- Running the downloaded app → it checks GitHub Releases and, if a newer version
  exists, opens the download page.

## Pricing model

Each lead gets a recommended tier based on its business type:

| Price | Tier | What it covers |
|-------|------|----------------|
| **$400** | Simple custom site | One custom page with animations, click-to-call, redirect/social links |
| **$600** | Multi-page + SEO | Several pages plus SEO so they rank for local searches |
| **$800** | Advanced build | The above plus online ordering, booking, or payments |

Restaurants/cafes/bars are pitched at **$800** (online ordering / digital menu);
appointment businesses (salons, dentists, vets, etc.) at **$800** (online booking);
everything else defaults to **$600** (multi-page + SEO).

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

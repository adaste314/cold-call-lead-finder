#!/usr/bin/env python3
"""
Core logic for the cold-call lead finder: location, business discovery, and
website analysis. Shared by the CLI (leads.py) and the desktop app (app.py).

Data sources (all free, no API key):
  - Location:   IP geolocation (ipapi.co / ip-api.com)
  - Geocoding:  OpenStreetMap Nominatim
  - Businesses: OpenStreetMap Overpass API
"""

import csv
import html
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

UA = "cold-call-lead-finder/1.0 (local outreach tool)"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# ----------------------------------------------------------------------------
# Location
# ----------------------------------------------------------------------------

def locate_by_ip():
    """Best-effort IP geolocation. Returns (lat, lon, label) or None."""
    providers = [
        ("https://ipapi.co/json/", lambda d: (d["latitude"], d["longitude"],
                                               f'{d.get("city","?")}, {d.get("region","")}')),
        ("http://ip-api.com/json/", lambda d: (d["lat"], d["lon"],
                                               f'{d.get("city","?")}, {d.get("regionName","")}')),
    ]
    for url, parse in providers:
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=8)
            r.raise_for_status()
            lat, lon, label = parse(r.json())
            if lat and lon:
                return float(lat), float(lon), label.strip(", ")
        except Exception:
            continue
    return None


def geocode(address):
    """Geocode a free-text address via Nominatim. Returns (lat, lon, label)."""
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": UA},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"Could not find a location for: {address!r}")
    top = data[0]
    return float(top["lat"]), float(top["lon"]), top.get("display_name", address)


# ----------------------------------------------------------------------------
# Business discovery (Overpass)
# ----------------------------------------------------------------------------

def fetch_businesses(lat, lon, radius):
    """Query Overpass for named businesses within `radius` meters."""
    amenity = ("restaurant|cafe|bar|pub|fast_food|dentist|doctors|clinic|"
               "veterinary|pharmacy|car_repair|fuel|bank|beauty|hairdresser|"
               "car_wash|driving_school|childcare")
    tourism = "hotel|motel|guest_house|hostel|bed_and_breakfast"
    q = f"""
    [out:json][timeout:60];
    (
      nwr["shop"]["name"](around:{radius},{lat},{lon});
      nwr["craft"]["name"](around:{radius},{lat},{lon});
      nwr["office"]["name"](around:{radius},{lat},{lon});
      nwr["amenity"~"^({amenity})$"]["name"](around:{radius},{lat},{lon});
      nwr["leisure"]["name"](around:{radius},{lat},{lon});
      nwr["tourism"~"^({tourism})$"]["name"](around:{radius},{lat},{lon});
    );
    out center tags;
    """
    last_err = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            r = requests.post(endpoint, data={"data": q},
                              headers={"User-Agent": UA}, timeout=90)
            r.raise_for_status()
            return r.json().get("elements", [])
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Overpass API failed on all endpoints: {last_err}")


def normalize(elements):
    """Turn raw Overpass elements into clean business dicts, deduped by name."""
    out = {}
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        website = (tags.get("website") or tags.get("contact:website")
                   or tags.get("url") or tags.get("contact:url"))
        phone = (tags.get("phone") or tags.get("contact:phone")
                 or tags.get("mobile"))
        category = (tags.get("shop") or tags.get("craft") or tags.get("office")
                    or tags.get("amenity") or tags.get("leisure")
                    or tags.get("tourism") or "business")
        addr_parts = [tags.get("addr:housenumber"), tags.get("addr:street"),
                      tags.get("addr:city")]
        address = " ".join(p for p in addr_parts if p)
        key = name.strip().lower()
        if key in out:
            if phone and not out[key]["phone"]:
                out[key]["phone"] = phone
            continue
        out[key] = {
            "name": name.strip(),
            "category": category.replace("_", " "),
            "website": website.strip() if website else None,
            "phone": phone.strip() if phone else None,
            "address": address,
        }
    return list(out.values())


# ----------------------------------------------------------------------------
# Website analysis
# ----------------------------------------------------------------------------

def check_ssl(hostname):
    """Return True if the host presents a valid TLS cert on 443."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname):
                return True
    except Exception:
        return False


def _candidate_urls(url):
    """Given a listed URL, produce sensible variants to try before giving up."""
    base = url if "://" in url else "http://" + url
    p = urlparse(base)
    host = p.hostname or ""
    schemes = ["https", "http"] if p.scheme == "http" else [p.scheme, "http"]
    hosts = [host]
    if host.startswith("www."):
        hosts.append(host[4:])
    elif host and host.count(".") == 1:
        hosts.append("www." + host)
    seen, out = set(), []
    for h in hosts:
        for s in schemes:
            u = f"{s}://{h}{p.path or '/'}"
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out


def _fetch(url):
    """Try a URL and its variants. Returns (resp, elapsed, error_kind)."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; lead-finder)"}
    first_error = None
    for cand in _candidate_urls(url):
        start = time.time()
        try:
            resp = requests.get(cand, headers=headers, timeout=12, allow_redirects=True)
            return resp, time.time() - start, None
        except requests.exceptions.TooManyRedirects:
            first_error = first_error or "redirect_loop"
        except requests.exceptions.SSLError:
            first_error = first_error or "ssl"
        except Exception:
            first_error = first_error or "dead"
    return None, 0, (first_error or "dead")


def analyze_website(url):
    """Fetch a site and return (problems, status)."""
    problems = []
    host = urlparse(url if "://" in url else "http://" + url).hostname or ""

    resp, elapsed, err = _fetch(url)
    if resp is None:
        if err == "redirect_loop":
            problems.append("Site is stuck in a redirect loop and never finishes loading")
            return problems, "error"
        if err == "ssl":
            problems.append("SSL certificate is broken or expired (browser shows a security warning, blocks visitors)")
            return problems, "unreachable"
        problems.append("Website does not load / server is unreachable (the listed site is effectively dead)")
        return problems, "dead"

    final_url = resp.url
    if resp.status_code >= 400:
        problems.append(f"Website returns an error (HTTP {resp.status_code}) - page is broken")
        return problems, "error"

    # HTTPS / security
    if not final_url.startswith("https://"):
        problems.append("No HTTPS - site is insecure and Chrome flags it 'Not Secure'")
    elif host and not check_ssl(host):
        problems.append("TLS/SSL certificate problem on the secure site")

    body = resp.text or ""
    lower = body.lower()

    # Mobile friendliness (viewport is the single best proxy)
    if "name=\"viewport\"" not in lower and "name='viewport'" not in lower:
        problems.append("Not mobile-friendly (no responsive viewport) - breaks on phones")

    # Title & meta description (SEO basics)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = title_match.group(1).strip() if title_match else ""
    if not title:
        problems.append("Missing page title - hurts Google ranking and browser tabs")
    if not re.search(r'<meta[^>]+name=["\']description["\']', body, re.I):
        problems.append("No meta description - Google shows a messy snippet in search results")

    # Legacy tech
    if ".swf" in lower or "shockwave-flash" in lower:
        problems.append("Uses Flash - completely dead tech, won't run in any modern browser")

    # Stale content (old copyright year)
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)[^0-9]{0,12}(20\d{2})", lower)]
    current_year = datetime.now().year
    if years and max(years) <= current_year - 2:
        problems.append(f"Copyright says {max(years)} - site looks abandoned/out of date")

    # Performance
    if elapsed > 4:
        problems.append(f"Very slow to load ({elapsed:.1f}s) - visitors bounce before it opens")
    elif elapsed > 2.5:
        problems.append(f"Slow load time ({elapsed:.1f}s)")

    # Thin / placeholder content
    text_only = re.sub(r"<[^>]+>", " ", body)
    if len(text_only.split()) < 60:
        problems.append("Almost no content / looks like a placeholder or parked domain")

    status = "ok" if not problems else "issues"
    return problems, status


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------

WEIGHTS = {
    "no_website": 55, "dead": 50, "unreachable": 48, "error": 45,
    "per_problem": 8, "has_phone": 6,
}


def score(biz):
    s = 0
    if not biz["website"]:
        s += WEIGHTS["no_website"]
    else:
        st = biz.get("status")
        if st in ("dead", "unreachable", "error"):
            s += WEIGHTS[st]
        s += WEIGHTS["per_problem"] * len(biz.get("problems", []))
    if biz["phone"]:
        s += WEIGHTS["has_phone"]
    return s


def build_talking_points(biz):
    if not biz["website"]:
        return [
            "No website at all - you're invisible to the ~60% of customers who search online first",
            "Competitors with a site are capturing your walk-in and call-in traffic",
            "A simple one-page site + Google Business listing would start bringing in calls",
        ]
    return biz.get("problems", [])


def analyze_all(businesses, workers=12, progress=None):
    """Analyze every business's website. `progress(done, total)` is optional."""
    have_site = [b for b in businesses if b["website"]]
    no_site = [b for b in businesses if not b["website"]]
    for b in no_site:
        b["problems"], b["status"] = [], "no_website"

    total = len(have_site)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(analyze_website, b["website"]): b for b in have_site}
        for fut in as_completed(futs):
            b = futs[fut]
            try:
                b["problems"], b["status"] = fut.result()
            except Exception:
                b["problems"], b["status"] = ["Website could not be analyzed"], "error"
            done += 1
            if progress:
                progress(done, total)

    all_biz = no_site + have_site
    for b in all_biz:
        b["score"] = score(b)
        b["points"] = build_talking_points(b)
    leads = [b for b in all_biz if not b["website"] or b["problems"]]
    leads.sort(key=lambda b: b["score"], reverse=True)
    return leads


def find_leads(lat, lon, radius, progress=None):
    """Full pipeline: discover businesses near (lat, lon) and rank leads."""
    elements = fetch_businesses(lat, lon, radius)
    businesses = normalize(elements)
    return analyze_all(businesses, progress=progress), len(businesses)


# ----------------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------------

def write_csv(leads, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "name", "category", "phone", "website",
                    "address", "status", "score", "talking_points"])
        for i, b in enumerate(leads, 1):
            w.writerow([i, b["name"], b["category"], b["phone"] or "",
                        b["website"] or "", b["address"], b["status"],
                        b["score"], " | ".join(b["points"])])


def write_html(leads, path, location_label, radius):
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    no_site = sum(1 for b in leads if not b["website"])
    bad_site = sum(1 for b in leads if b["website"])
    cards = []
    for i, b in enumerate(leads, 1):
        no_web = not b["website"]
        tag = "NO WEBSITE" if no_web else "BAD WEBSITE"
        tag_cls = "nosite" if no_web else "badsite"
        phone = html.escape(b["phone"]) if b["phone"] else "<em>not listed - look it up</em>"
        site = (f'<a href="{html.escape(b["website"])}" target="_blank" rel="noopener">'
                f'{html.escape(b["website"])}</a>') if b["website"] else "&mdash;"
        points = "".join(f"<li>{html.escape(p)}</li>" for p in b["points"])
        addr = f'<div class="addr">{html.escape(b["address"])}</div>' if b["address"] else ""
        tel = (f'<a class="call" href="tel:{re.sub(chr(92)+"D","",b["phone"])}">Call</a>'
               if b["phone"] else "")
        cards.append(f"""
        <div class="card">
          <div class="head">
            <span class="rank">#{i}</span>
            <span class="name">{html.escape(b['name'])}</span>
            <span class="tag {tag_cls}">{tag}</span>
            <span class="score">score {b['score']}</span>
          </div>
          <div class="meta">
            <span class="cat">{html.escape(b['category'].title())}</span>
            <span class="phone">&#128222; {phone}</span> {tel}
          </div>
          {addr}
          <div class="site">&#127760; {site}</div>
          <div class="pts-label">Talking points</div>
          <ul class="pts">{points}</ul>
        </div>""")

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cold-Call Leads</title>
<style>
  :root {{ --bg:#0f1115; --card:#191c23; --line:#2a2f3a; --text:#e7e9ee;
    --muted:#9aa3b2; --accent:#5b9dff; --nosite:#ff5c5c; --badsite:#ffb020; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  header {{ padding:28px 24px 16px; border-bottom:1px solid var(--line);
    position:sticky; top:0; background:var(--bg); z-index:2; }}
  h1 {{ margin:0 0 6px; font-size:22px; }}
  .sub {{ color:var(--muted); font-size:13px; }}
  .stats {{ margin-top:12px; display:flex; gap:10px; flex-wrap:wrap; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:8px;
    padding:8px 14px; font-size:13px; }}
  .stat b {{ font-size:18px; display:block; }}
  .filter {{ margin-left:auto; }}
  input[type=search] {{ background:var(--card); border:1px solid var(--line);
    color:var(--text); padding:8px 12px; border-radius:8px; width:220px; }}
  main {{ padding:20px 24px; display:grid; gap:14px;
    grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }}
  .head {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  .rank {{ color:var(--muted); font-weight:700; }}
  .name {{ font-weight:700; font-size:16px; }}
  .tag {{ font-size:10px; font-weight:800; letter-spacing:.5px; padding:3px 7px; border-radius:999px; }}
  .tag.nosite {{ background:rgba(255,92,92,.15); color:var(--nosite); }}
  .tag.badsite {{ background:rgba(255,176,32,.15); color:var(--badsite); }}
  .score {{ margin-left:auto; color:var(--muted); font-size:12px; }}
  .meta {{ margin:8px 0 2px; display:flex; gap:12px; align-items:center;
    color:var(--muted); font-size:13px; flex-wrap:wrap; }}
  .call {{ background:var(--accent); color:#06122b; text-decoration:none;
    font-weight:700; padding:2px 10px; border-radius:6px; font-size:12px; }}
  .addr {{ color:var(--muted); font-size:12px; }}
  .site {{ margin:6px 0; font-size:13px; word-break:break-all; }}
  .site a, a {{ color:var(--accent); }}
  .pts-label {{ margin-top:10px; font-size:11px; text-transform:uppercase;
    letter-spacing:.6px; color:var(--muted); }}
  ul.pts {{ margin:6px 0 0; padding-left:18px; }}
  ul.pts li {{ margin:4px 0; }}
</style></head><body>
<header>
  <h1>Cold-Call Leads &mdash; {html.escape(location_label)}</h1>
  <div class="sub">Within {radius} m &middot; generated {now} &middot;
    sorted by opportunity. Red = no website, amber = website with problems.</div>
  <div class="stats">
    <div class="stat"><b>{len(leads)}</b> total leads</div>
    <div class="stat"><b>{no_site}</b> no website</div>
    <div class="stat"><b>{bad_site}</b> bad website</div>
    <div class="filter"><input type="search" id="q" placeholder="Filter by name..."></div>
  </div>
</header>
<main id="list">{''.join(cards)}</main>
<script>
  const q=document.getElementById('q'), cards=[...document.querySelectorAll('.card')];
  q.addEventListener('input',()=>{{const t=q.value.toLowerCase();
    cards.forEach(c=>c.style.display=c.textContent.toLowerCase().includes(t)?'':'none');}});
</script>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)

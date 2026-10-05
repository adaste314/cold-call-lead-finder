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
        city = tags.get("addr:city") or ""
        addr_parts = [tags.get("addr:housenumber"), tags.get("addr:street"), city]
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
            "city": city,
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
    """Fetch a site and return (issues, status).

    `issues` is a list of (code, param) tuples; _issue_text() turns each into a
    short label, a weakness line, and a detailed talking point.
    """
    host = urlparse(url if "://" in url else "http://" + url).hostname or ""

    resp, elapsed, err = _fetch(url)
    if resp is None:
        if err == "redirect_loop":
            return [("redirect_loop", None)], "error"
        if err == "ssl":
            return [("ssl_broken", None)], "unreachable"
        return [("dead", None)], "dead"

    if resp.status_code >= 400:
        return [("http_error", resp.status_code)], "error"

    issues = []
    final_url = resp.url
    if not final_url.startswith("https://"):
        issues.append(("no_https", None))
    elif host and not check_ssl(host):
        issues.append(("ssl_problem", None))

    body = resp.text or ""
    lower = body.lower()

    if "name=\"viewport\"" not in lower and "name='viewport'" not in lower:
        issues.append(("not_mobile", None))

    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if not (title_match and title_match.group(1).strip()):
        issues.append(("no_title", None))
    if not re.search(r'<meta[^>]+name=["\']description["\']', body, re.I):
        issues.append(("no_meta", None))

    if ".swf" in lower or "shockwave-flash" in lower:
        issues.append(("flash", None))

    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)[^0-9]{0,12}(20\d{2})", lower)]
    if years and max(years) <= datetime.now().year - 2:
        issues.append(("stale_copyright", max(years)))

    if elapsed > 2.5:
        issues.append(("slow", round(elapsed, 1)))

    text_only = re.sub(r"<[^>]+>", " ", body)
    if len(text_only.split()) < 60:
        issues.append(("thin", None))

    return issues, ("ok" if not issues else "issues")


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------

WEIGHTS = {
    "no_website": 55, "dead": 50, "unreachable": 48, "error": 45,
    "per_issue": 8, "has_phone": 6,
}


def score(biz):
    s = 0
    if not biz["website"]:
        s += WEIGHTS["no_website"]
    else:
        st = biz.get("status")
        if st in ("dead", "unreachable", "error"):
            s += WEIGHTS[st]
        s += WEIGHTS["per_issue"] * len(biz.get("issues", []))
    if biz["phone"]:
        s += WEIGHTS["has_phone"]
    return s


# ----------------------------------------------------------------------------
# Issue descriptions, SWOT, talking points, pricing
# ----------------------------------------------------------------------------

# Categories that justify the top tier (online ordering / booking).
ORDERING_CATS = {"restaurant", "cafe", "bar", "pub", "fast_food", "bakery",
                 "ice_cream", "confectionery", "deli", "food", "food_court",
                 "caterer", "coffee", "pastry"}
BOOKING_CATS = {"hairdresser", "beauty", "dentist", "doctors", "clinic",
                "veterinary", "car_repair", "driving_school", "childcare",
                "hotel", "motel", "guest_house", "hostel", "spa", "massage",
                "nail", "barber", "tattoo", "physiotherapist", "optician"}

# Price model (amount, short name, what it covers).
PRICE_TIERS = [
    (400, "Simple custom site",
     "One custom page with animations, click-to-call, and redirect/social links"),
    (600, "Multi-page + SEO",
     "Several pages plus search-engine optimization so you rank for local searches"),
    (800, "Advanced build",
     "Everything above, plus complex features like online ordering, booking, or payments"),
]


def _catkey(biz):
    return biz["category"].lower().replace(" ", "_")


def _issue_text(code, param, city):
    """Return (short_label, weakness_line, detailed_talking_point) for an issue."""
    where = f" in {city}" if city else ""
    table = {
        "dead": ("Site down", "Listed website doesn't load - effectively dead",
                 "The website on your listing doesn't load at all right now. To anyone who clicks it, that reads as 'closed' or 'out of business' - worse than having no site. I'd get a working site back up and point your listings at it."),
        "ssl_broken": ("SSL broken", "Security certificate broken/expired - browser blocks the site",
                       "Your site's security certificate is broken or expired, so browsers throw a full-page security warning before anyone can even see your page. You're losing every visitor who clicks through. It's a quick fix."),
        "redirect_loop": ("Redirect loop", "Stuck in a redirect loop - never finishes loading",
                          "Your site is stuck redirecting to itself and never finishes loading, so visitors just get a spinning page and leave. I'd rebuild it on a clean, fast setup."),
        "http_error": (f"HTTP {param}", f"Returns an error (HTTP {param}) - page is broken",
                       f"Your site returns an HTTP {param} error instead of a real page, so customers hit a broken screen. I'd replace it with a working site."),
        "no_https": ("No HTTPS", "No HTTPS - browsers label it 'Not Secure'",
                     "Your site loads over plain HTTP, so Chrome and Safari show a 'Not Secure' warning in the address bar. Visitors - especially on phones - see that and bounce. Adding SSL fixes it and is table stakes today."),
        "ssl_problem": ("SSL warning", "TLS/SSL certificate problem on the secure site",
                        "There's a certificate problem on your secure site that can trigger browser warnings. I'd sort out the SSL so it loads cleanly everywhere."),
        "not_mobile": ("Not mobile-friendly", "Not mobile-friendly - breaks on phones",
                       "Your site isn't responsive, so on a phone it shows up zoomed-out and hard to tap. Most local searches happen on phones, so this is where you're losing the most people. A mobile-first rebuild fixes it."),
        "no_title": ("No page title", "Missing page title - hurts Google & browser tabs",
                     "Your pages have no title tag, which is one of the first things Google reads. It hurts your ranking and makes your search result look generic. Easy, high-impact fix."),
        "no_meta": ("No meta description", "No meta description - messy Google snippet",
                    "There's no meta description, so Google invents a messy snippet under your result instead of a clean pitch. Proper descriptions make your listing far more clickable."),
        "flash": ("Uses Flash", "Uses Flash - dead tech, won't run anywhere",
                  "Your site still uses Flash, which no modern browser runs at all - meaning parts of it are completely broken for every visitor. That needs a full rebuild on current tech."),
        "stale_copyright": (f"Stale ({param})", f"Copyright says {param} - looks abandoned",
                            f"Your footer still says {param}, which signals the site - and maybe the business - is neglected. Refreshing the design and content makes you look open and active."),
        "slow": (f"Slow ({param}s)", f"Slow to load ({param}s) - visitors bounce",
                 f"Your site takes about {param} seconds to load. People start leaving after ~3 seconds, so you lose visitors before they see anything. A rebuild on fast hosting fixes this."),
        "thin": ("Thin content", "Almost no content - looks parked/placeholder",
                 "There's barely any real content on the site - it looks like a placeholder or parked domain. A proper site with your services, photos, and info would actually sell for you."),
    }
    return table.get(code, (code, code, code))


def build_weaknesses(biz, city):
    if not biz["website"]:
        w = ["No website at all - invisible to people searching online or on maps"]
    else:
        w = [_issue_text(c, p, city)[1] for c, p in biz["issues"]]
    if not biz["phone"]:
        w.append("No phone number listed online - searchers can't contact you from results")
    return w


def build_talking_points(biz, city):
    cat = biz["category"]
    ck = _catkey(biz)
    where = f" in {city}" if city else ""
    if not biz["website"]:
        pts = [
            f"Right now you have no website, so when someone searches \"{biz['name']}\" or \"{cat} near me{where}\", you're either missing or a competitor shows up instead. That's the biggest and easiest win on the table.",
            "Around 60% of people look a local business up online before they call or walk in. Without a site, that whole group defaults to whoever does have one.",
            "A clean one-page site - your hours, location, photos, and a click-to-call button - can go live quickly and start turning those searches into calls and foot traffic.",
        ]
        if ck in ORDERING_CATS:
            pts.append(f"Since you're a {cat}, a simple online menu - and optionally online ordering - captures people who pick where to eat based on what they can see and order online.")
        if ck in BOOKING_CATS:
            pts.append(f"As a {cat}, online booking lets customers grab appointments any time without tying up your phone.")
        pts.append("I'd also claim and optimize your Google Business profile and link it to the site, so you show up on Google Maps.")
        return pts
    pts = [_issue_text(c, p, city)[2] for c, p in biz["issues"]]
    pts.append("The upside: you already own the domain, so this is an upgrade, not starting from zero - I can modernize it and keep your current web address.")
    return pts


def build_swot(biz, city):
    cat = biz["category"]
    ck = _catkey(biz)
    where = f" in {city}" if city else ""

    strengths = []
    if biz["address"]:
        strengths.append(f"Established physical location ({biz['address']}) - a real local base to drive online traffic to")
    else:
        strengths.append(f"Operating local {cat} with existing walk-in customers to build on")
    if biz["phone"]:
        strengths.append("Phone number is public, so customers (and we) can reach you easily")
    if biz["website"]:
        strengths.append("Already owns a domain - we can modernize it and keep the existing web address")
    else:
        strengths.append("A name people search for - a site would immediately capture that demand")

    weaknesses = biz["weaknesses"]

    opportunities = ["Capture the ~60% of customers who look you up online before visiting"]
    if ck in ORDERING_CATS:
        opportunities.append("Add online ordering / a digital menu to win takeout and delivery business")
    if ck in BOOKING_CATS:
        opportunities.append("Add online booking so customers schedule appointments 24/7")
    opportunities.append(f"Rank on Google and Maps for \"{cat} near me{where}\"")
    opportunities.append("Mobile-first design to capture the majority of local searches done on phones")

    threats = ["Competitors with modern, mobile sites rank above you and win the click"]
    if biz["status"] in ("dead", "unreachable", "error"):
        threats.append("Your listed site is broken right now, which makes you look closed or unreliable")
    threats.append("Google and map apps push businesses with a weak web presence down the results")
    threats.append("Every month without a proper site is revenue lost to online-first rivals")

    return {
        "strengths": strengths[:3],
        "weaknesses": weaknesses[:6] or ["No major site issues found beyond the basics"],
        "opportunities": opportunities[:4],
        "threats": threats[:3],
    }


def recommend_tier(biz):
    """Return (amount, reason) - the price tier that best fits this business."""
    ck = _catkey(biz)
    if ck in ORDERING_CATS:
        return 800, "online ordering / a digital menu will pay for itself in takeout and delivery orders"
    if ck in BOOKING_CATS:
        return 800, "online booking lets customers schedule 24/7 without tying up your phone"
    return 600, f"multiple pages plus SEO will get you ranking for local \"{biz['category']} near me\" searches"


def _enrich(biz):
    """Attach weaknesses, talking points, SWOT, pricing, and score to a lead."""
    city = biz.get("city") or ""
    biz["weaknesses"] = build_weaknesses(biz, city)
    biz["points"] = build_talking_points(biz, city)
    biz["swot"] = build_swot(biz, city)
    biz["price_tiers"] = PRICE_TIERS
    biz["recommended"] = recommend_tier(biz)
    biz["score"] = score(biz)
    return biz


def analyze_all(businesses, workers=12, progress=None):
    """Analyze every business's website. `progress(done, total)` is optional."""
    have_site = [b for b in businesses if b["website"]]
    no_site = [b for b in businesses if not b["website"]]
    for b in no_site:
        b["issues"], b["status"] = [], "no_website"

    total = len(have_site)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(analyze_website, b["website"]): b for b in have_site}
        for fut in as_completed(futs):
            b = futs[fut]
            try:
                b["issues"], b["status"] = fut.result()
            except Exception:
                b["issues"], b["status"] = [("dead", None)], "error"
            done += 1
            if progress:
                progress(done, total)

    all_biz = no_site + have_site
    for b in all_biz:
        _enrich(b)
    leads = [b for b in all_biz if not b["website"] or b["issues"]]
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
        w.writerow(["rank", "name", "category", "phone", "website", "address",
                    "status", "score", "recommended_price", "price_reason",
                    "strengths", "weaknesses", "opportunities", "threats",
                    "talking_points"])
        for i, b in enumerate(leads, 1):
            s = b["swot"]
            amount, reason = b["recommended"]
            w.writerow([i, b["name"], b["category"], b["phone"] or "",
                        b["website"] or "", b["address"], b["status"], b["score"],
                        f"${amount}", reason,
                        " | ".join(s["strengths"]), " | ".join(s["weaknesses"]),
                        " | ".join(s["opportunities"]), " | ".join(s["threats"]),
                        " | ".join(b["points"])])


def write_html(leads, path, location_label, radius_mi):
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

        sw = b["swot"]

        def _quad(title, items, cls):
            lis = "".join(f"<li>{html.escape(x)}</li>" for x in items)
            return (f'<div class="quad {cls}"><h4>{title}</h4>'
                    f'<ul>{lis}</ul></div>')

        swot = (f'<div class="swot">'
                f'{_quad("Strengths", sw["strengths"], "s")}'
                f'{_quad("Weaknesses", sw["weaknesses"], "w")}'
                f'{_quad("Opportunities", sw["opportunities"], "o")}'
                f'{_quad("Threats", sw["threats"], "t")}'
                f'</div>')

        rec_amount, rec_reason = b["recommended"]
        tier_rows = "".join(
            f'<div class="tier{" rec" if amt == rec_amount else ""}">'
            f'<span class="price">${amt}</span>'
            f'<span class="tname">{html.escape(tname)}'
            f'{" &nbsp;<em>recommended</em>" if amt == rec_amount else ""}</span>'
            f'<span class="tdesc">{html.escape(tdesc)}</span>'
            f'</div>'
            for amt, tname, tdesc in b["price_tiers"])
        rec_line = (f'<div class="rec">&rarr; Pitch <b>${rec_amount}</b>: '
                    f'{html.escape(rec_reason)}</div>')

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
          <div class="section-label">SWOT analysis</div>
          {swot}
          <div class="section-label">Talking points</div>
          <ul class="pts">{points}</ul>
          <div class="section-label">Suggested pricing</div>
          <div class="tiers">{tier_rows}</div>
          {rec_line}
        </div>""")

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cold-Call Leads</title>
<style>
  :root {{ --bg:#0f1115; --card:#191c23; --line:#2a2f3a; --text:#e7e9ee;
    --muted:#9aa3b2; --accent:#5b9dff; --nosite:#ff5c5c; --badsite:#ffb020;
    --good:#3fc97a; }}
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
    grid-template-columns:repeat(auto-fill,minmax(400px,1fr)); align-items:start; }}
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
  .section-label {{ margin-top:14px; font-size:11px; text-transform:uppercase;
    letter-spacing:.6px; color:var(--muted); }}
  ul.pts {{ margin:6px 0 0; padding-left:18px; font-size:13px; }}
  ul.pts li {{ margin:5px 0; }}
  .swot {{ margin-top:8px; display:grid; gap:8px;
    grid-template-columns:repeat(2, minmax(0,1fr)); }}
  .quad {{ border:1px solid var(--line); border-radius:8px; padding:8px 10px;
    border-left-width:3px; }}
  .quad h4 {{ margin:0 0 4px; font-size:11px; text-transform:uppercase;
    letter-spacing:.5px; }}
  .quad ul {{ margin:0; padding-left:15px; font-size:12px; }}
  .quad li {{ margin:3px 0; color:var(--text); }}
  .quad.s {{ border-left-color:var(--good); }}  .quad.s h4 {{ color:var(--good); }}
  .quad.w {{ border-left-color:var(--nosite); }}  .quad.w h4 {{ color:var(--nosite); }}
  .quad.o {{ border-left-color:var(--accent); }}  .quad.o h4 {{ color:var(--accent); }}
  .quad.t {{ border-left-color:var(--badsite); }}  .quad.t h4 {{ color:var(--badsite); }}
  .tiers {{ margin-top:6px; display:flex; flex-direction:column; gap:6px; }}
  .tier {{ display:grid; grid-template-columns:64px 1fr; gap:2px 10px;
    border:1px solid var(--line); border-radius:8px; padding:7px 10px; }}
  .tier .price {{ grid-row:span 2; align-self:center; font-weight:800;
    font-size:17px; color:var(--text); }}
  .tier .tname {{ font-weight:700; font-size:13px; }}
  .tier .tname em {{ font-style:normal; font-size:10px; font-weight:800;
    color:var(--good); text-transform:uppercase; letter-spacing:.5px; }}
  .tier .tdesc {{ font-size:12px; color:var(--muted); }}
  .tier.rec {{ border-color:var(--good); background:rgba(63,201,122,.08); }}
  .rec {{ margin-top:8px; font-size:13px; color:var(--text); }}
</style></head><body>
<header>
  <h1>Cold-Call Leads &mdash; {html.escape(location_label)}</h1>
  <div class="sub">Within {radius_mi} mi &middot; generated {now} &middot;
    sorted by opportunity. Each lead has a SWOT analysis, talking points, and a
    suggested price. Red = no website, amber = website with problems.</div>
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

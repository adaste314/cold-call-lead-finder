#!/usr/bin/env python3
"""
Cold-call lead finder - command-line version.

Finds local businesses near you that have NO website or a BAD website, and hands
you the specific problems to use as talking points. For the desktop app, run app.py.

Usage:
    python leads.py                      # use your IP location, 2km radius
    python leads.py --address "Austin, TX"
    python leads.py --radius 3000 --limit 40
    python leads.py --lat 30.26 --lon -97.74
    python leads.py --no-open            # don't auto-open the HTML report
"""

import argparse
import os
import sys
import webbrowser

import core


def print_console(leads, limit, location_label):
    print()
    print("=" * 78)
    print(f"  COLD-CALL LEADS near {location_label}")
    print(f"  {len([l for l in leads if not l['website']])} with NO website, "
          f"{len([l for l in leads if l['website']])} with a BAD website")
    print("=" * 78)
    for i, b in enumerate(leads[:limit], 1):
        tag = "NO WEBSITE" if not b["website"] else "BAD WEBSITE"
        print(f"\n{i}. {b['name']}  [{tag}]  (score {b['score']})")
        print(f"   {b['category'].title()}" + (f"  |  {b['address']}" if b["address"] else ""))
        print(f"   Phone: {b['phone'] or 'NOT LISTED - look it up before calling'}")
        if b["website"]:
            print(f"   Site:  {b['website']}")
        print("   Talking points:")
        for p in b["points"]:
            print(f"     - {p}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Find local cold-call leads with bad/no websites.")
    ap.add_argument("--address", help="Address/city to search around (else uses your IP location)")
    ap.add_argument("--lat", type=float, help="Latitude (overrides address/IP)")
    ap.add_argument("--lon", type=float, help="Longitude (overrides address/IP)")
    ap.add_argument("--radius", type=int, default=2000, help="Search radius in meters (default 2000)")
    ap.add_argument("--limit", type=int, default=30, help="Max leads to print (default 30)")
    ap.add_argument("--out", default=None, help="Output basename (default cold_call_leads)")
    ap.add_argument("--no-open", action="store_true", help="Do not auto-open the HTML report")
    args = ap.parse_args()

    if args.lat is not None and args.lon is not None:
        lat, lon, label = args.lat, args.lon, f"{args.lat:.4f}, {args.lon:.4f}"
    elif args.address:
        lat, lon, label = core.geocode(args.address)
    else:
        loc = core.locate_by_ip()
        if not loc:
            raise SystemExit("Could not auto-detect location. Pass --address or --lat/--lon.")
        lat, lon, label = loc
    print(f"Location: {label}  ({lat:.4f}, {lon:.4f})  radius {args.radius}m", file=sys.stderr)

    def prog(done, total):
        print(f"\r  checking websites {done}/{total}", end="", file=sys.stderr)

    leads, n = core.find_leads(lat, lon, args.radius, progress=prog)
    print(f"\n  {n} businesses found.", file=sys.stderr)

    base = args.out or "cold_call_leads"
    outdir = os.path.dirname(os.path.abspath(sys.argv[0]))
    csv_path = os.path.join(outdir, base + ".csv")
    html_path = os.path.join(outdir, base + ".html")
    print_console(leads, args.limit, label)
    core.write_csv(leads, csv_path)
    core.write_html(leads, html_path, label, args.radius)
    print(f"Saved {len(leads)} leads -> {csv_path}", file=sys.stderr)
    print(f"Report -> {html_path}", file=sys.stderr)
    if not args.no_open:
        webbrowser.open("file://" + html_path)


if __name__ == "__main__":
    main()

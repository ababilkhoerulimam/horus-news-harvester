"""
Analyzes all distinct media sites and news channels present in the Dewa United dataset.
Extracts volume, domain mapping, and RSS/harvester feasibility.
"""

import csv
import json
import os
from collections import Counter
from pathlib import Path

SOURCE_CSV = Path(r"C:\Github\horus-v2\dewa-united-2026-09-21.csv")
ALT_CSV = Path(r"C:\Github\dewa\dewa-united-2026-09-21.csv")
OUT_JSON = Path(r"C:\Github\ind-sports-scrapper\config\portals_catalog.json")


def analyze_sites():
    csv_path = SOURCE_CSV if SOURCE_CSV.exists() and SOURCE_CSV.stat().st_size > 0 else ALT_CSV
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        print(f"[!] Error: Target CSV not found or empty: {csv_path}")
        return

    print(f"[*] Reading dataset: {csv_path} ({csv_path.stat().st_size / (1024*1024):.2f} MB)...")

    site_counter = Counter()
    total_articles = 0
    sample_headlines = {}

    with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_articles += 1
            site = (row.get("site") or "unknown").strip().lower()
            site_counter[site] += 1
            if site not in sample_headlines:
                sample_headlines[site] = row.get("headline", "")

    distinct_sites = len(site_counter)
    print(f"[+] Total Articles: {total_articles}")
    print(f"[+] Distinct Media Channels / Sites: {distinct_sites}\n")

    catalog = []
    print(f"{'No.':<4} {'Site Identifier':<30} {'Articles':<10} {'Percentage':<10} {'Sample Headline'}")

    for idx, (site, count) in enumerate(site_counter.most_common(), 1):
        pct = (count / total_articles) * 100
        headline = (sample_headlines.get(site) or "")[:45]
        print(f"{idx:<4} {site:<30} {count:<10} {pct:>6.2f}%    {headline}")
        catalog.append({
            "rank": idx,
            "site": site,
            "count": count,
            "percentage": round(pct, 2),
            "sample_headline": sample_headlines.get(site, "")
        })

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "total_articles": total_articles,
            "total_sites": distinct_sites,
            "sites": catalog
        }, f, indent=2, ensure_ascii=False)

    print(f"\n[+] Saved detailed catalog to {OUT_JSON}")


if __name__ == "__main__":
    analyze_sites()

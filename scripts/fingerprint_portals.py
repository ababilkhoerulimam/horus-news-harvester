"""
Automated CMS fingerprinting and archive probe for Indonesian sports portals.
Probes 45 sports portals for WP-REST, sitemaps, archive endpoints, and WAF.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from processors.http_client import ResilientHttpClient
from channels.portal_news import is_cloudflare_challenge

# Probe client with zero jitter and short timeout for fast auditing
probe_client = ResilientHttpClient(enable_jitter=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fingerprint")


def probe_portal(portal: Dict[str, Any]) -> Dict[str, Any]:
    domain = portal["domain"]
    name = portal.get("name", domain)
    sports_url = portal.get("sports_url") or f"https://{domain}"
    sports_parsed = urlparse(sports_url)
    base_host = f"{sports_parsed.scheme}://{sports_parsed.netloc}"

    result: Dict[str, Any] = {
        "domain": domain,
        "name": name,
        "tier": portal.get("tier", 3),
        "base_host": base_host,
        "sports_url": sports_url,
        "archetype": "unknown",
        "has_wp_json": False,
        "wp_json_endpoint": None,
        "active_sitemaps": [],
        "archive_pattern": None,
        "waf_challenge": False,
        "status_code": 0,
        "notes": [],
    }

    # 1. Base sports URL probe
    try:
        status, body, _ = probe_client.fetch(sports_url, timeout=6.0, max_retries=0)
        result["status_code"] = status
        html = body.decode("utf-8", errors="replace") if body else ""
        if is_cloudflare_challenge(status, html):
            result["waf_challenge"] = True
            result["notes"].append("Cloudflare / WAF challenge detected")
    except Exception as e:
        result["notes"].append(f"Base fetch error: {e}")

    # 2. WordPress REST API probe
    for test_base in [base_host, f"https://{domain}"]:
        wp_endpoint = f"{test_base}/wp-json/wp/v2/posts?per_page=1"
        try:
            st, b, h = probe_client.fetch(wp_endpoint, timeout=5.0, max_retries=0)
            if st == 200 and b and b.strip().startswith(b"["):
                result["has_wp_json"] = True
                result["wp_json_endpoint"] = f"{test_base}/wp-json/wp/v2/posts"
                result["archetype"] = "wordpress_rest"
                break
        except Exception:
            pass

    # 3. Sitemap discovery
    candidate_sitemaps = [
        f"{base_host}/sitemap.xml",
        f"{base_host}/sitemap_news.xml",
        f"{base_host}/sitemap-news.xml",
        f"{base_host}/sitemap_index.xml",
        f"{base_host}/post-sitemap.xml",
        f"https://{domain}/sitemap.xml",
        f"https://{domain}/sitemap_news.xml",
        f"https://{domain}/sitemap-news.xml",
    ]
    seen_sm = set()
    for sm_url in candidate_sitemaps:
        if sm_url in seen_sm:
            continue
        seen_sm.add(sm_url)
        try:
            st, b, _ = probe_client.fetch(sm_url, timeout=5.0, max_retries=0)
            if st == 200 and b and (b"<urlset" in b or b"<sitemapindex" in b or b"<?xml" in b):
                result["active_sitemaps"].append(sm_url)
        except Exception:
            pass

    # 4. Check monthly sitemap archive pattern (Promedia / WP standard: YYYY-MM or YYYY/MM)
    monthly_tests = [
        f"{base_host}/sitemap-2026-05.xml",
        f"{base_host}/sitemap_2026_05.xml",
        f"https://{domain}/sitemap-2026-05.xml",
        f"https://{domain}/post-sitemap-2026-05.xml",
    ]
    for m_url in monthly_tests:
        try:
            st, b, _ = probe_client.fetch(m_url, timeout=5.0, max_retries=0)
            if st == 200 and b and (b"<urlset" in b or b"<?xml" in b):
                result["active_sitemaps"].append(m_url)
                if result["archetype"] == "unknown":
                    result["archetype"] = "monthly_sitemap"
                break
        except Exception:
            pass

    # 5. Archive date pattern probe
    # Test common Indonesian news archive patterns on 2026-04-15
    date_test_patterns = [
        ("{base}/search/2026-04-15", "search_iso"),
        ("{base}/indeks?date=04%2F15%2F2026", "detik_m_d_y"),
        ("{base}/indeks/0?t=2026-04-15", "sindo_iso"),
        ("{base}/arsip/2026-04-15", "arsip_iso"),
        ("{base}/indeks/2026/04/15", "slash_ymd"),
        ("{base}/arsip/2026/04/15", "arsip_slash_ymd"),
    ]
    for pattern_tpl, pat_name in date_test_patterns:
        test_url = pattern_tpl.format(base=base_host)
        try:
            st, b, _ = probe_client.fetch(test_url, timeout=5.0, max_retries=0)
            if st == 200 and b and len(b) > 5000:
                html = b.decode("utf-8", errors="replace")
                # Ensure it has articles and not just an empty layout / 404 disguised as 200
                if re.search(r'href=[\"\'](https?://[^\"\']+(?:/read/|/berita/|/d-|artikel)[^\"\']*)[\"\']', html):
                    result["archive_pattern"] = pattern_tpl
                    if result["archetype"] == "unknown":
                        result["archetype"] = "archive_query"
                    break
        except Exception:
            pass

    # Final archetype classification fallback
    if result["archetype"] == "unknown":
        if result["waf_challenge"]:
            result["archetype"] = "heavy_waf"
        elif result["active_sitemaps"]:
            result["archetype"] = "sitemap_only"
        elif result["status_code"] in (403, 500, 503):
            result["archetype"] = "blocked_or_restricted"
        else:
            result["archetype"] = "generic_html"

    logger.info(f"[{result['archetype']}] {domain} -> sitemaps={len(result['active_sitemaps'])}, wp={result['has_wp_json']}, arch={bool(result['archive_pattern'])}")
    return result


def main():
    cat_path = REPO_ROOT / "config" / "portals_catalog.json"
    if not cat_path.exists():
        print(f"Error: {cat_path} not found.")
        sys.exit(1)

    with open(cat_path, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    portals = [p for p in catalog.get("portals", []) if p.get("has_sports_desk")]
    print(f"[*] Starting fingerprint audit on {len(portals)} sports portals...")

    results = []
    # 8 concurrent workers
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(probe_portal, p): p for p in portals}
        for fut in as_completed(futures):
            try:
                res = fut.result()
                results.append(res)
            except Exception as e:
                p = futures[fut]
                logger.error(f"Error probing {p.get('domain')}: {e}")

    out_file = REPO_ROOT / "config" / "portal_fingerprints.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n[+] Audit finished! Saved {len(results)} fingerprints to {out_file}")

    # Summary
    archetypes = {}
    for r in results:
        at = r["archetype"]
        archetypes[at] = archetypes.get(at, 0) + 1

    print("\n=== Archetype Distribution ===")
    for at, cnt in sorted(archetypes.items(), key=lambda x: -x[1]):
        print(f"  * {at:<22}: {cnt} portals")


if __name__ == "__main__":
    main()

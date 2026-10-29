"""
Ad and Tracker Domain Filter for Indonesian News Portals.
Inspired by Scrapling ad_domains and network filtering architecture.
Identifies ad networks, tracking telemetry, and sponsored redirect links (Taboola, MGID, Outbrain, Criteo, PopIn, Dable, etc.).
"""

from urllib.parse import urlparse
import re

# Core ad, tracking, and sponsored-content networks dominant in Indonesian media portals
AD_TRACKER_DOMAINS: frozenset = frozenset({
    # Global tracking & ad exchanges
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "criteo.com",
    "criteo.net",
    "rubiconproject.com",
    "adnxs.com",
    "pubmatic.com",
    "openx.net",
    "adroll.com",
    "adsystem.com",
    "scorecardresearch.com",
    "facebook.net",
    "adsrvr.org",
    "zemanta.com",
    "teads.tv",
    "bidswitch.net",
    "casalemedia.com",
    "smartadserver.com",
    "advertising.com",
    "adtech.de",
    "contextweb.com",
    "infolinks.com",
    "media.net",

    # Content recommendation / native ads omnipresent on Indonesian portals (Detik, Tribun, Kompas, Sindonews)
    "taboola.com",
    "outbrain.com",
    "mgid.com",
    "popin.cc",
    "dable.io",
    "innity.com",
    "innity.net",
    "revcontent.com",
    "adman.id",
    "uadexchange.com",
    "hyprmx.com",
    "any-manager.com",
    "anymind360.com",
    "socdm.com",
    "microad.net",
    "adstir.com",
    "geniee.jp",
    "yieldone.com",
    "adscale.de",
    "sharethrough.com",
    "trafficjunky.com",
    "exoclick.com",
    "propellerads.com",
    "adcash.com",
})


def is_ad_domain(url_or_domain: str) -> bool:
    """
    Returns True if the given URL or host belongs to a known ad, telemetry,
    or sponsored recommendation network.
    """
    if not url_or_domain:
        return False

    raw = url_or_domain.strip().lower()
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.netloc.split(":")[0]
    else:
        host = raw.split("/")[0].split(":")[0]

    if not host:
        return False

    # Check exact match or subdomain match
    parts = host.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in AD_TRACKER_DOMAINS:
            return True

    return False


def strip_ad_links(html: str) -> str:
    """
    Strips anchor tags pointing to ad or tracker domains from HTML snippets.
    Prevents crawler traps and affiliate link pollution in corpus.
    """
    if not html:
        return ""

    def _replace_link(match):
        href = match.group(1) or match.group(2) or ""
        if is_ad_domain(href):
            return ""  # excise ad link completely
        return match.group(0)

    pattern = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>.*?</a>', re.IGNORECASE | re.DOTALL)
    return pattern.sub(_replace_link, html)

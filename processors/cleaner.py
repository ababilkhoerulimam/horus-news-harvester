import email.utils
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


AD_PATTERNS = [
    r"baca\s+juga\s*:.*?(?=\n|$)",
    r"simak\s+video.*?(?=\n|$)",
    r"pilihan\s+editor\s*:.*?(?=\n|$)",
    r"halaman\s+selanjutnya.*?(?=\n|$)",
    r"halaman\s+\d+\s+dari\s+\d+.*?(?=\n|$)",
    r"artikel\s+ini\s+telah\s+tayang\s+di.*?(?=\n|$)",
    r"\[gambas:.*?\]",
    r"tonton\s+video.*?(?=\n|$)"
]

ID_MONTHS = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "agustus": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "agu": 8, "agt": 8,
    "sep": 9, "okt": 10, "nov": 11, "des": 12
}


# Zero-width unicode & control character patterns inspired by Scrapling AI-sanitizer
ZERO_WIDTH_CHARS = re.compile(r"[​-‍﻿]")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def clean_news_text(raw_text: str) -> str:
    """
    Cleans raw news article text by stripping advertisements,
    editorial disclaimers, zero-width chars, and normalizing whitespace.
    """
    if not raw_text:
        return ""

    text = raw_text

    # 1. Strip zero-width and control characters (invisible noise / prompt injection defense)
    text = ZERO_WIDTH_CHARS.sub("", text)
    text = CONTROL_CHARS.sub("", text)

    # 2. Strip ad and editorial disclaimers
    for pat in AD_PATTERNS:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)

    # 3. Normalize multiple whitespace and newlines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


# --- article normalization (prototype, Change 4 step 2) ---------------------
#
# Ported from horus-graph `scripts/korpus_sumber.py` (`_TAK_TAMPAK`, `_DATELINE`,
# `_BACA_JUGA`, `_JUDUL_BOILER`) with one addition: a bare city dateline such as
# "Jakarta - ", which the original pattern does not catch and which is the
# dominant form in this corpus.
#
# Additive: `clean_news_text` is unchanged, so nothing calls these functions
# until a caller opts in. Numbers behind the rules: `_audit/horus_reuse.md`.

# Bidi and word-joiner marks. `ZERO_WIDTH_CHARS` above already covers U+200B to
# U+200D and U+FEFF; these are the ones it misses.
_BIDI = re.compile("[\u200e\u200f\u202a-\u202e\u2060]")

_DOMAIN = r"[\w\-]+(?:\.[\w\-]+)*[.,](?i:com|co\.id|id|net|tv)\b"

# Every alternative is anchored at the start of the body. `[*_]{0,2}` tolerates
# markdown emphasis left by the extractor ("**BANDUNG**- "). The order matters
# only for attribution in the audit script, not for the combined pattern.
DATELINE_PATTERNS = (
    ("domain_city", re.compile(
        rf"^\s*[*_]{{0,2}}{_DOMAIN}\s*,\s*[A-Z][A-Za-z .]{{2,30}},?\s*[-\u2013\u2014]\s*")),
    ("city_domain", re.compile(
        rf"^\s*[*_]{{0,2}}[A-Z][A-Za-z .]{{2,30}},\s*{_DOMAIN}\s*[-\u2013\u2014]\s*")),
    ("domain_only", re.compile(
        rf"^\s*[*_]{{0,2}}{_DOMAIN}(?:\s+[A-Z]\w*){{0,3}}[*_]{{0,2}}\s*[-\u2013\u2014]\s*")),
    ("city_agency", re.compile(
        r"^\s*[*_]{0,2}[A-Z][A-Za-z .]{2,30}\s*\([^()\n]{2,30}\)\s*[-\u2013\u2014]\s*")),
    ("city_caps", re.compile(
        r"^\s*[*_]{0,2}[A-Z][A-Z0-9]{2,}(?:\s+[A-Z][A-Z0-9]{2,}){0,2}[*_]{0,2}"
        r"(?:\s*[\u2013\u2014]\s*|\s+-[ \t]*)")),
    ("bare_city", re.compile(
        r"^\s*[*_]{0,2}[A-Z][a-z]{2,15}(?:\s+[A-Z][a-z]{2,15})?[*_]{0,2}"
        r"\s*[-\u2013\u2014]\s+")),
)

_DATELINE = re.compile("(?:" + "|".join(p.pattern for _, p in DATELINE_PATTERNS) + ")")

# A "Baca juga" line links to a different article. `AD_PATTERNS` above only
# matches it when a colon follows, which leaves the no-colon form in the text.
_BACA_JUGA_LINE = re.compile(r"^[ \t]*(?:baca|simak)\s+juga\b.*$", re.IGNORECASE | re.MULTILINE)

# The outlet suffix in a headline is not part of the headline.
_TITLE_OUTLET = re.compile(
    r"\s*[|\u2013\u2014-]\s*[\w.\-]+\.(?:co\.id|com|co|id|net|tv|news|org)\s*$",
    re.IGNORECASE,
)

# Stacked datelines happen ("KOMPAS.com - Jakarta - ..."); three passes is the
# bound horus measured, and a fourth never fired there.
DATELINE_REPEAT_MAX = 3


def strip_dateline(text: str) -> str:
    """Removes leading desk-city datelines, up to three stacked ones.

    "Jakarta - BRI Super League ..." becomes "BRI Super League ...".
    """
    body = text
    for _ in range(DATELINE_REPEAT_MAX):
        stripped = _DATELINE.sub("", body, count=1)
        if stripped == body:
            break
        body = stripped
    return body


def normalize_article(body: str) -> str:
    """Body text without bidi marks, datelines, or cross-link lines.

    Paragraph breaks are kept: only runs of spaces and tabs are collapsed, so a
    caller can still tell paragraphs apart after normalization.
    """
    if not body:
        return ""
    text = _BIDI.sub("", body)
    text = _BACA_JUGA_LINE.sub("", text)
    text = strip_dateline(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def normalize_title(title: str) -> str:
    """Title without bidi marks or the outlet suffix ("... | detik.com")."""
    if not title:
        return ""
    text = _TITLE_OUTLET.sub("", _BIDI.sub("", title).strip())
    return " ".join(text.split())




def normalize_timestamp(raw_date: Optional[str], base_time: Optional[datetime] = None) -> str:
    """
    Normalizes multi-channel date/time representations to standardized ISO-8601 UTC (YYYY-MM-DDTHH:MM:SSZ).
    Handles Indonesian relative strings (e.g. '2 hari lalu', '18 jam yang lalu', 'kemarin'),
    Twitter RFC-2822, news timestamps (WIB/WITA/WIT), and ISO variations.
    Falls back to cleaned input string if unparseable.
    """
    if not raw_date:
        return ""

    s = str(raw_date).strip()
    if not s:
        return ""

    now = base_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    s_lower = s.lower()

    # 1. Indonesian & English Relative Strings
    # 'kemarin' / 'yesterday'
    if s_lower in ("kemarin", "yesterday"):
        dt = now - timedelta(days=1)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 'X hari/hours/mins lalu'
    rel_match = re.search(r"(\d+)\s*(hari|day|days|jam|hour|hours|menit|minute|minutes|detik|second|seconds|minggu|week|weeks|bulan|month|months|tahun|year|years)\s*(?:yang\s+)?(?:lalu|ago)", s_lower)
    if rel_match:
        qty = int(rel_match.group(1))
        unit = rel_match.group(2)
        if unit in ("hari", "day", "days"):
            dt = now - timedelta(days=qty)
        elif unit in ("jam", "hour", "hours"):
            dt = now - timedelta(hours=qty)
        elif unit in ("menit", "minute", "minutes"):
            dt = now - timedelta(minutes=qty)
        elif unit in ("detik", "second", "seconds"):
            dt = now - timedelta(seconds=qty)
        elif unit in ("minggu", "week", "weeks"):
            dt = now - timedelta(weeks=qty)
        elif unit in ("bulan", "month", "months"):
            dt = now - timedelta(days=qty * 30)
        elif unit in ("tahun", "year", "years"):
            dt = now - timedelta(days=qty * 365)
        else:
            dt = now
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 2. RFC-2822 (e.g. Twitter created_at: "Wed Oct 18 10:00:00 +0000 2026")
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass

    # 3. Standard ISO formats (e.g. "2026-10-18T10:00:00Z" or "2026-10-18 10:00:00")
    iso_clean = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass

    # 4. Indonesian Portal Formats: "18 Oktober 2026 14:30 WIB" or "18/10/2026 14:30"
    tz_offset = 7  # default WIB (UTC+7)
    if "wita" in s_lower:
        tz_offset = 8
    elif "wit" in s_lower:
        tz_offset = 9

    # Pattern: DD <MonthName> YYYY [HH:MM[:SS]]
    m_name_match = re.search(r"(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})(?:\s*,?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?", s_lower)
    if m_name_match:
        d = int(m_name_match.group(1))
        m_name = m_name_match.group(2)
        y = int(m_name_match.group(3))
        hr = int(m_name_match.group(4) or 0)
        mn = int(m_name_match.group(5) or 0)
        sc = int(m_name_match.group(6) or 0)
        month = ID_MONTHS.get(m_name)
        if month:
            try:
                local_dt = datetime(y, month, d, hr, mn, sc, tzinfo=timezone(timedelta(hours=tz_offset)))
                return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass

    # Pattern: DD/MM/YYYY or DD-MM-YYYY [HH:MM[:SS]]
    dmy_match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    if dmy_match:
        d = int(dmy_match.group(1))
        m = int(dmy_match.group(2))
        y = int(dmy_match.group(3))
        hr = int(dmy_match.group(4) or 0)
        mn = int(dmy_match.group(5) or 0)
        sc = int(dmy_match.group(6) or 0)
        try:
            local_dt = datetime(y, m, d, hr, mn, sc, tzinfo=timezone(timedelta(hours=tz_offset)))
            return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass

    return s


def deduplicate_jsonl_file(file_path: Path, id_key: str = "url", prefer: str = "first") -> int:
    """
    Cleans an existing JSONL file by removing duplicate records in-place.
    Returns the count of duplicate rows removed.

    prefer="first" keeps the record that appears first, which is the historical
    behaviour. prefer="longest" keeps the one with the longest content, which is
    what a re-harvest needs: the later record is only worth having if it is better.
    """
    if prefer not in ("first", "longest"):
        raise ValueError(f"prefer must be 'first' or 'longest', got {prefer!r}")

    fpath = Path(file_path)
    if not fpath.exists() or not fpath.is_file():
        return 0

    seen_ids = set()
    unique_lines = []
    duplicates_removed = 0
    # Slot and content length per id, so a longer later record replaces in place
    # without reordering the file and without re-parsing what is already stored.
    line_index = {}
    best_length = {}

    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except Exception:
                continue

            raw_id = (
                item.get(id_key)
                or item.get("url")
                or item.get("tweet_id")
                or item.get("video_id")
                or item.get("id")
            )
            if not raw_id:
                unique_lines.append(stripped)
                continue

            id_str = str(raw_id)
            if id_str in seen_ids:
                duplicates_removed += 1
                if prefer == "longest":
                    length = len(item.get("content") or "")
                    if length > best_length[id_str]:
                        unique_lines[line_index[id_str]] = stripped
                        best_length[id_str] = length
                continue

            seen_ids.add(id_str)
            line_index[id_str] = len(unique_lines)
            best_length[id_str] = len(item.get("content") or "")
            unique_lines.append(stripped)

    if duplicates_removed > 0:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=fpath.parent) as tmp:
            tmp_path = Path(tmp.name)
            for uline in unique_lines:
                tmp.write(uline + "\n")
        tmp_path.replace(fpath)

    return duplicates_removed





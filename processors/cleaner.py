import re
from typing import Dict, Any, List, Optional


AD_PATTERNS = [
    r"baca\s+juga\s*:.*?(?=\n|$)",
    r"simak\s+video.*?(?=\n|$)",
    r"pilihan\s+editor\s*:.*?(?=\n|$)",
    r"halaman\s+selanjutnya.*?(?=\n|$)",
    r"\[gambas:.*?\]",
    r"tonton\s+video.*?(?=\n|$)"
]


def clean_news_text(raw_text: str) -> str:
    """
    Cleans raw news article text by stripping advertisements,
    editorial disclaimers, and normalizing whitespace.
    """
    if not raw_text:
        return ""

    text = raw_text
    for pat in AD_PATTERNS:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)

    # Normalize multiple whitespace and newlines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def deduplicate_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicates articles based on normalized title or exact URL.
    """
    seen_urls = set()
    seen_titles = set()
    deduped = []

    for art in articles:
        url = (art.get("url") or "").strip().lower()
        title = (art.get("title") or "").strip().lower()

        # Simplify title for fuzzy matching
        norm_title = re.sub(r"[^\w\s]", "", title)

        if url and url in seen_urls:
            continue
        if norm_title and norm_title in seen_titles:
            continue

        if url:
            seen_urls.add(url)
        if norm_title:
            seen_titles.add(norm_title)

        deduped.append(art)

    return deduped

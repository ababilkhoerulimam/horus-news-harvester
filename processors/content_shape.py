"""Classifies what a fetched body is: prose, chrome, an envelope, a caption.

Length is not part of the decision. A 900-character article is an article, and
a 2.000-character CloudFront error page is an error page; the previous rule
labelled both "low" purely by size.
"""

import html as html_lib
import re
from typing import List, Tuple

READER_ENVELOPE = "reader_envelope"
CHROME_ONLY = "chrome_only"
ERROR_PAGE = "error_page"
AD_INTERLEAVED = "ad_interleaved"
PHOTO_CAPTION = "photo_caption"
ARTICLE = "article"
MISSING_TITLE = "missing_title"

# Literals, not regexes: each one is a marker its producing tool prints.
_ENVELOPE_MARKERS = ("URL Source:", "Markdown Content:")
_CHROME_MARKERS = ("Detail Berita", "Link Copied!")
_ERROR_MARKERS = ("request could not be satisfied", "403 error", "request blocked")

_AD_LINE = "Advertisement"
_AD_LINE_MIN = 3

# "(BP/Antara)" is a two-part credit. "(Foto: UEFA)" is chrome, not a credit.
_CREDIT_TAIL = re.compile(r"\([A-Za-z]{2,6}\s*/\s*[^()]{2,40}\)$")
_PHOTO_MAX_CHARS = 400
_PHOTO_MAX_PARAGRAPHS = 1
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_TAIL_CHARS = 120
_TAIL_STRIP = " \t\r\n\xa0\u200b\u200e\u200f"


def _tail(content: str) -> str:
    """Last 120 characters, entity-decoded so "&nbsp;" cannot hide a ")"."""
    return html_lib.unescape(content[-_TAIL_CHARS:]).rstrip(_TAIL_STRIP)


def _count_ad_lines(content: str) -> int:
    return sum(1 for line in content.splitlines() if line.strip() == _AD_LINE)


def _paragraphs(content: str) -> List[str]:
    return [part.strip() for part in _PARAGRAPH_SPLIT.split(content.strip()) if part.strip()]


def classify(content: str, url: str = "") -> Tuple[str, str]:
    """Returns (label, reason). Pure: no I/O, no network, no state.

    The evaluation order is part of the contract. A CloudFront error page can
    carry both chrome markers, and a Jina envelope can carry both chrome markers
    as well, so the narrower causes are tested before the wider symptoms.

    `url` is accepted but unread: photo captions may need a domain allowlist
    once the corpus control is measured, and adding the parameter later would
    change the signature twice.
    """
    if not content or not content.strip():
        return CHROME_ONLY, "empty"
    lowered = content.lower()
    for marker in _ERROR_MARKERS:
        if marker in lowered:
            return ERROR_PAGE, "error_marker"
    if all(marker in content for marker in _ENVELOPE_MARKERS):
        return READER_ENVELOPE, "envelope_marker"
    if all(marker in content for marker in _CHROME_MARKERS):
        return CHROME_ONLY, "chrome_marker"
    if _count_ad_lines(content) >= _AD_LINE_MIN:
        return AD_INTERLEAVED, "ad_lines"
    if (
        len(content) < _PHOTO_MAX_CHARS
        and len(_paragraphs(content)) <= _PHOTO_MAX_PARAGRAPHS
        and _CREDIT_TAIL.search(_tail(content))
    ):
        return PHOTO_CAPTION, "photo_credit"
    return ARTICLE, "prose"


def title_warning(title: str) -> str:
    """Returns MISSING_TITLE for a blank title, else "".

    Kept out of classify() on purpose: a missing title is a warning for the
    report, never a reason to change the body's label.
    """
    return MISSING_TITLE if not (title or "").strip() else ""

"""Maps a content shape to the quality label a record may carry."""

from typing import Tuple

from processors.content_shape import (
    AD_INTERLEAVED,
    ARTICLE,
    CHROME_ONLY,
    ERROR_PAGE,
    PHOTO_CAPTION,
    READER_ENVELOPE,
    classify,
)

REJECT, LOW, OK = "reject", "low", "ok"

# "reject" is never written to a record. It reaches the frontier as an error
# message, and the corpus relabelling in normalize_corpus.py, but the record
# itself only ever carries "low" or "ok".
QUALITY_FOR_LABEL = {
    READER_ENVELOPE: REJECT,
    CHROME_ONLY: REJECT,
    ERROR_PAGE: REJECT,
    AD_INTERLEAVED: LOW,
    PHOTO_CAPTION: LOW,
    ARTICLE: OK,
}


def shape_decision(content: str, url: str = "") -> Tuple[str, str, str]:
    """Returns (label, quality, reason) for one fetched body."""
    label, reason = classify(content, url)
    return label, QUALITY_FOR_LABEL[label], reason

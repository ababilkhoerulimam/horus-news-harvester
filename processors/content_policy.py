"""Maps a content shape to the quality label a record may carry."""

import logging
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

logger = logging.getLogger("content_policy")

REJECT, LOW, OK = "reject", "low", "ok"

# The one length gate left in the pipeline: below this, a body is not an
# article page at all, whatever its shape. It replaces the 120 and 150 that
# used to differ between the topic harvester and the backfill.
MIN_BODY_CHARS = 120

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

_WARNED_SOURCES = set()


def warn_deprecated_quality_threshold(value: int, source: str) -> None:
    """Warns once per source that a length threshold no longer sets quality.

    The value is still accepted so an existing config or CLI flag keeps
    validating, but nothing compares against it any more. Warning per source
    rather than per record keeps a 200.000-record run from writing 200.000
    identical lines.
    """
    if source in _WARNED_SOURCES:
        return
    _WARNED_SOURCES.add(source)
    logger.warning(
        "min_quality_chars=%s from %s is ignored: quality comes from the body "
        "shape, and MIN_BODY_CHARS=%s only decides whether a body is an article "
        "page at all",
        value,
        source,
        MIN_BODY_CHARS,
    )


def shape_decision(content: str, url: str = "") -> Tuple[str, str, str]:
    """Returns (label, quality, reason) for one fetched body."""
    label, reason = classify(content, url)
    return label, QUALITY_FOR_LABEL[label], reason

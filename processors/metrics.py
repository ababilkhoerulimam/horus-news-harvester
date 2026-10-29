"""Shared numeric parsing for social counters."""

import re
from typing import Any

_SUFFIX_MULTIPLIERS = {
    "rb": 1_000,
    "ribu": 1_000,
    "k": 1_000,
    "jt": 1_000_000,
    "juta": 1_000_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
}

_NUMBER = re.compile(r"(\d[\d.,]*)")
_DOT_GROUPED = re.compile(r"\d{1,3}(?:\.\d{3})+")
_COMMA_GROUPED = re.compile(r"\d{1,3}(?:,\d{3})+")


def parse_count(value: Any) -> int:
    """Parses a social counter into an int.

    Handles plain digits, Indonesian thousands separators such as 1.234,
    decimal commas with a suffix such as 1,2 rb, English suffixes such as
    1.2K, and blank strings, all of which appear in the harvested corpora.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if value is None:
        return 0

    text = str(value).replace("\u00a0", " ").strip().lower()
    if not text:
        return 0

    match = _NUMBER.search(text)
    if not match:
        return 0

    raw = match.group(1).rstrip(".,")
    rest = text[match.end():].strip()
    suffix = rest.split()[0] if rest else ""
    multiplier = _SUFFIX_MULTIPLIERS.get(suffix, 1)

    if multiplier != 1:
        number = float(raw.replace(",", "."))
    elif _DOT_GROUPED.fullmatch(raw):
        number = float(raw.replace(".", ""))
    elif _COMMA_GROUPED.fullmatch(raw):
        number = float(raw.replace(",", ""))
    else:
        number = float(raw.replace(",", "."))

    return int(round(number * multiplier))

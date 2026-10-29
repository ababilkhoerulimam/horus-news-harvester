"""
Per-domain extraction rules.

The generic extractor has to guess at every theme's markup, and on the big
Indonesian portals that guess loses most of the article: a 400 KB page yields a few
hundred characters because the regex container match stops at the first closing
div. A rule replaces the guess with one verified answer per domain.

Two strategies are supported, in this order:

1. next_data - read the article body out of the Next.js __NEXT_DATA__ payload. This
   is the publisher's own JSON, so it is both smaller and cleaner than the DOM.
2. content   - XPath expressions evaluated against the parsed page. XPath is used
   instead of CSS because lxml needs the separate cssselect package for CSS.

Rules live in config/extraction_rules.json and are loaded lazily on first use, not
at import, so importing this module stays free of filesystem work.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from processors.cleaner import clean_news_text

RULES_PATH = Path("config/extraction_rules.json")

_TAG_PATTERN = re.compile(r"<[^>]+>")
_NEXT_DATA_XPATH = "//script[@id='__NEXT_DATA__']"


@dataclass(frozen=True)
class ExtractionRule:
    """One domain's verified recipe for pulling the article body out of a page."""

    content: Tuple[str, ...] = ()
    strip: Tuple[str, ...] = ()
    next_data: Optional[str] = None
    min_chars: int = 300


_rules: Optional[Dict[str, ExtractionRule]] = None


def reset_cache() -> None:
    """Drops the cached rules. Used by tests and after editing the JSON file."""
    global _rules
    _rules = None


def _parse_rule(raw: Any) -> Optional[ExtractionRule]:
    if not isinstance(raw, dict):
        return None
    content = raw.get("content") or []
    strip = raw.get("strip") or []
    next_data = raw.get("next_data")
    if not content and not next_data:
        return None
    return ExtractionRule(
        content=tuple(content),
        strip=tuple(strip),
        next_data=next_data if isinstance(next_data, str) else None,
        min_chars=int(raw.get("min_chars") or 300),
    )


def _load_rules() -> Dict[str, ExtractionRule]:
    global _rules
    if _rules is not None:
        return _rules
    loaded: Dict[str, ExtractionRule] = {}
    try:
        with open(RULES_PATH, "r", encoding="utf-8") as handle:
            raw_rules = json.load(handle)
    except (OSError, ValueError):
        raw_rules = {}
    if isinstance(raw_rules, dict):
        for domain, raw in raw_rules.items():
            rule = _parse_rule(raw)
            if rule is not None and isinstance(domain, str):
                loaded[domain.strip().lower()] = rule
    _rules = loaded
    return loaded


def rules_for(domain: Optional[str]) -> Optional[ExtractionRule]:
    """Returns the rule for a domain, matching parent domains for subdomains."""
    if not domain:
        return None
    rules = _load_rules()
    if not rules:
        return None
    labels = domain.strip().lower().split(".")
    for index in range(len(labels) - 1):
        rule = rules.get(".".join(labels[index:]))
        if rule is not None:
            return rule
    return None


def _strip_tags(raw: str) -> str:
    return html_lib.unescape(_TAG_PATTERN.sub(" ", raw or ""))


def _dig(payload: Any, dotted_path: str) -> Optional[str]:
    node = payload
    for part in dotted_path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            index = int(part)
            node = node[index] if 0 <= index < len(node) else None
        else:
            return None
        if node is None:
            return None
    return node if isinstance(node, str) else None


def _from_next_data(tree: Any, dotted_path: str, min_chars: int) -> Optional[str]:
    scripts = tree.xpath(_NEXT_DATA_XPATH)
    if not scripts:
        return None
    try:
        payload = json.loads(scripts[0].text_content())
    except ValueError:
        return None
    raw = _dig(payload, dotted_path)
    if not raw:
        return None
    text = clean_news_text(_strip_tags(raw))
    return text if len(text) >= min_chars else None


_JUNK_TAGS = ("script", "style", "noscript", "iframe", "form", "svg")


def _drop_nodes(tree: Any, expressions: Sequence[str]) -> None:
    """Removes matching nodes in place; text_content() would otherwise keep their text."""
    for expression in expressions:
        for node in tree.xpath(expression):
            try:
                node.drop_tree()
            except AttributeError:
                parent = node.getparent()
                if parent is not None:
                    parent.remove(node)


def _from_xpath(tree: Any, rule: ExtractionRule) -> Optional[str]:
    # Script bodies are the usual reason a rule returns thousands of characters of
    # JavaScript after the real article text.
    _drop_nodes(tree, [f"//{tag}" for tag in _JUNK_TAGS])
    _drop_nodes(tree, rule.strip)
    for expression in rule.content:
        nodes = tree.xpath(expression)
        if not nodes:
            continue
        text = clean_news_text(" ".join(node.text_content() for node in nodes))
        if len(text) >= rule.min_chars:
            return text
    return None


def apply_rule(html: str, rule: Optional[ExtractionRule]) -> Optional[str]:
    """Applies a rule and returns the article text, or None when it does not fit.

    Returning None is the normal fallback signal: the caller then runs the generic
    extractor exactly as before.
    """
    if not html or rule is None:
        return None
    try:
        from lxml import html as lxml_html

        tree = lxml_html.fromstring(html)
    except Exception:
        return None

    if rule.next_data:
        text = _from_next_data(tree, rule.next_data, rule.min_chars)
        if text:
            return text
    if rule.content:
        return _from_xpath(tree, rule)
    return None

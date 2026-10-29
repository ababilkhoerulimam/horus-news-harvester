"""
Per-domain extraction rules: lookup, both strategies, and the fallback signal.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from channels.portal_news import extract_clean_article_text
from processors import extraction_rules
from processors.extraction_rules import ExtractionRule, apply_rule, reset_cache, rules_for

LONG = "Kalimat berita olahraga Indonesia yang cukup panjang untuk lolos ambang. " * 8


def _next_data_html(content: str) -> str:
    payload = {"props": {"pageProps": {"article": {"content": content}}}}
    return (
        "<html><body><div id='app'></div>"
        f"<script id='__NEXT_DATA__' type='application/json'>{json.dumps(payload)}</script>"
        "</body></html>"
    )


def _xpath_html() -> str:
    return (
        "<html><body>"
        "<div class='read__content'><p>" + LONG + "</p></div>"
        "<div class='sidebar'>iklan sampingan</div>"
        "<script>var tracking = 'junk';</script>"
        "</body></html>"
    )


class RulesFileTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "rules.json"
        self.path.write_text(
            json.dumps(
                {
                    "detik.com": {"content": ["//article"], "min_chars": 50},
                    "jawapos.com": {"next_data": "props.pageProps.article.content", "min_chars": 50},
                    "broken.com": {"min_chars": 50},
                }
            ),
            encoding="utf-8",
        )
        self.patch = patch.object(extraction_rules, "RULES_PATH", self.path)
        self.patch.start()
        reset_cache()

    def tearDown(self):
        self.patch.stop()
        reset_cache()
        self.temp_dir.cleanup()

    def test_exact_domain(self):
        rule = rules_for("detik.com")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.content, ("//article",))

    def test_subdomain_matches_parent(self):
        self.assertIsNotNone(rules_for("sport.detik.com"))

    def test_unknown_domain_returns_none(self):
        self.assertIsNone(rules_for("unknown.example"))

    def test_empty_domain_returns_none(self):
        self.assertIsNone(rules_for(""))

    def test_rule_without_strategy_is_ignored(self):
        self.assertIsNone(rules_for("broken.com"))


class ApplyRuleTests(unittest.TestCase):
    def test_xpath_strategy(self):
        rule = ExtractionRule(content=("//div[contains(@class,'read__content')]",), min_chars=100)
        text = apply_rule(_xpath_html(), rule)
        self.assertIsNotNone(text)
        self.assertIn("Kalimat berita", text)
        self.assertNotIn("iklan sampingan", text)

    def test_script_text_is_not_returned(self):
        rule = ExtractionRule(content=("//body",), min_chars=10)
        text = apply_rule(_xpath_html(), rule)
        self.assertNotIn("tracking", text)

    def test_next_data_strategy(self):
        rule = ExtractionRule(next_data="props.pageProps.article.content", min_chars=100)
        text = apply_rule(_next_data_html("<p>" + LONG + "</p>"), rule)
        self.assertIsNotNone(text)
        self.assertIn("Kalimat berita", text)

    def test_next_data_missing_path_returns_none(self):
        rule = ExtractionRule(next_data="props.pageProps.nope.content", min_chars=10)
        self.assertIsNone(apply_rule(_next_data_html("<p>" + LONG + "</p>"), rule))

    def test_below_min_chars_returns_none(self):
        rule = ExtractionRule(content=("//div[contains(@class,'read__content')]",), min_chars=10000)
        self.assertIsNone(apply_rule(_xpath_html(), rule))

    def test_none_rule_returns_none(self):
        self.assertIsNone(apply_rule(_xpath_html(), None))

    def test_unparseable_html_returns_none(self):
        rule = ExtractionRule(content=("//article",), min_chars=1)
        self.assertIsNone(apply_rule("", rule))


class PortalIntegrationTests(unittest.TestCase):
    def test_rule_short_circuits_generic_extraction(self):
        with patch("channels.portal_news.rules_for", return_value=ExtractionRule(content=("//article",), min_chars=50)), patch(
            "channels.portal_news.apply_rule", return_value="ISI DARI RULE"
        ):
            text = extract_clean_article_text("<html><body><article>x</article></body></html>", "detik.com")
        self.assertEqual(text, "ISI DARI RULE")

    def test_no_domain_skips_rules(self):
        with patch("channels.portal_news.apply_rule") as mock_apply:
            extract_clean_article_text("<html><body><article>" + LONG + "</article></body></html>")
        mock_apply.assert_not_called()

    def test_failed_rule_falls_back_to_generic(self):
        with patch("channels.portal_news.apply_rule", return_value=None):
            text = extract_clean_article_text(
                "<html><body><article><p>" + LONG + "</p></article></body></html>", "detik.com"
            )
        self.assertIn("Kalimat berita", text)


class ShippedRulesTests(unittest.TestCase):
    """Guards the real config file: a typo there silently disables a rule."""

    def setUp(self):
        reset_cache()

    def tearDown(self):
        reset_cache()

    def test_expected_domains_are_configured(self):
        for domain in ("detik.com", "kompas.com", "jawapos.com"):
            self.assertIsNotNone(rules_for(domain), f"missing rule for {domain}")

    def test_subdomains_resolve(self):
        self.assertIsNotNone(rules_for("sport.detik.com"))
        self.assertIsNotNone(rules_for("bola.kompas.com"))


if __name__ == "__main__":
    unittest.main()

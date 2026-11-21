"""Tests for the single body gate and the retired length thresholds."""

import inspect
import unittest

from processors import content_policy
from processors.content_policy import (
    MIN_BODY_CHARS,
    OK,
    shape_decision,
    warn_deprecated_quality_threshold,
)
from processors.extraction_rules import ExtractionRule, apply_rule
from processors.fetch_pipeline import _success, fetch_article
from processors.topic_config import OutputConfig
from scripts import sitemap_backfill, topic_harvester


class BodyGateTests(unittest.TestCase):
    def test_body_gate_is_one_number(self):
        self.assertEqual(MIN_BODY_CHARS, 120)
        for name in (
            "_async_backfill_worker_loop",
            "_backfill_worker_process_entrypoint",
            "run_sitemap_backfill",
        ):
            with self.subTest(function=name):
                signature = inspect.signature(getattr(sitemap_backfill, name))
                self.assertEqual(
                    signature.parameters["min_content_length"].default, MIN_BODY_CHARS
                )

    def test_backfill_default_equals_min_body_chars(self):
        signature = inspect.signature(sitemap_backfill.run_sitemap_backfill)
        self.assertEqual(signature.parameters["min_content_length"].default, 120)

    def test_topic_harvester_uses_the_same_gate(self):
        self.assertEqual(topic_harvester.MIN_BODY_CHARS, MIN_BODY_CHARS)
        self.assertFalse(hasattr(topic_harvester, "TOPIC_MIN_CONTENT_LENGTH"))
        signature = inspect.signature(topic_harvester._async_crawl_worker_loop)
        self.assertNotIn("min_quality_chars", signature.parameters)

    def test_rule_min_chars_is_not_a_record_gate(self):
        # 300 measures whether the XPath fitted the page; a 150-character body
        # that fits is still an article. Conflating the two is what made a
        # missing XPath look like a short article.
        html = "<html><body><article>" + ("Kalimat pendek. " * 10) + "</article></body></html>"
        rule = ExtractionRule(content=("//article",), min_chars=300)
        self.assertIsNone(apply_rule(html, rule))

        body = "Kalimat pendek. " * 10
        self.assertGreaterEqual(len(body), MIN_BODY_CHARS)
        self.assertEqual(shape_decision(body)[1], OK)


class DeprecatedThresholdTests(unittest.TestCase):
    def setUp(self):
        content_policy._WARNED_SOURCES.clear()

    def tearDown(self):
        content_policy._WARNED_SOURCES.clear()

    def test_min_quality_chars_is_ignored(self):
        with self.assertRaises(TypeError):
            _success("x" * 200, "direct_http", min_quality_chars=5000)
        self.assertIn("min_quality_chars", inspect.signature(fetch_article).parameters)

    def test_deprecation_warning_is_logged_once(self):
        with self.assertLogs("content_policy", level="WARNING") as logs:
            warn_deprecated_quality_threshold(4000, "unit-test")
            warn_deprecated_quality_threshold(4000, "unit-test")

        self.assertEqual(len(logs.output), 1)
        self.assertIn("min_quality_chars=4000", logs.output[0])

    def test_topic_yaml_with_min_quality_chars_still_validates(self):
        config = OutputConfig(min_quality_chars=400)

        self.assertEqual(config.min_quality_chars, 400)
        self.assertIn("Deprecated", OutputConfig.model_fields["min_quality_chars"].description)


if __name__ == "__main__":
    unittest.main()

import unittest
import tempfile
import shutil
from pathlib import Path
import yaml
from pydantic import ValidationError

from processors.topic_config import (
    TopicConfig,
    TopicMeta,
    PortalsConfig,
    OutputConfig,
    load_topic_config,
)
from processors.portal_catalog import (
    PortalCatalogRegistry,
    resolve_topic_portals,
    CATALOG_PRESETS,
)
from scripts.topic_harvester import (
    generate_topic_template,
    validate_topic,
    list_available_topics,
)


class TestTopicConfig(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_topic_config_minimal_valid(self):
        raw = {
            "topic": {
                "id": "sports_test",
                "name": "Indonesian Sports Intelligence",
            },
            "keywords": ["timnas indonesia", "shinne tae-yong", "pssi"],
        }
        cfg = TopicConfig.model_validate(raw)
        self.assertEqual(cfg.topic.id, "sports_test")
        self.assertEqual(len(cfg.get_keywords()), 3)
        self.assertIn("timnas indonesia", cfg.get_keywords())

    def test_topic_config_nested_taxonomy_flattening(self):
        raw = {
            "topic": {"id": "karhutla", "name": "Forest Fire Intelligence"},
            "keywords": {
                "events": ["kebakaran hutan", "titik api", "hotspot"],
                "operations": ["water bombing", "manggala agni"],
                "regions": ["riau", "jambi"],
            },
        }
        cfg = TopicConfig.model_validate(raw)
        keywords = cfg.get_keywords()
        self.assertEqual(len(keywords), 7)
        self.assertIn("water bombing", keywords)
        self.assertIn("manggala agni", keywords)
        self.assertIn("riau", keywords)

    def test_topic_config_missing_required_fields(self):
        raw = {"keywords": ["foo", "bar"]}
        with self.assertRaises(ValidationError):
            TopicConfig.model_validate(raw)

    def test_load_topic_config_from_file(self):
        file_path = self.tmp_dir / "test_topic.yaml"
        data = {
            "topic": {
                "id": "file_test",
                "name": "File Test Topic",
            },
            "keywords": ["term1", "term2"],
            "portals": {
                "presets": ["national_wire"],
                "include": ["custom.co.id"],
            },
        }
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

        cfg = load_topic_config(str(file_path))
        self.assertEqual(cfg.topic.id, "file_test")
        self.assertIn("national_wire", cfg.portals.presets)


class TestPortalCatalogResolver(unittest.TestCase):
    def setUp(self):
        self.catalog = PortalCatalogRegistry()

    def test_catalog_presets_defined(self):
        self.assertIn("national_wire", CATALOG_PRESETS)
        self.assertIn("regional_epicenters", CATALOG_PRESETS)
        self.assertIn("investigative_environment", CATALOG_PRESETS)
        self.assertIn("all_66_catalog", CATALOG_PRESETS)
        self.assertGreater(len(CATALOG_PRESETS["all_66_catalog"]), 30)

    def test_resolve_topic_portals_preset_and_exclude(self):
        raw = {
            "topic": {"id": "demo", "name": "Demo"},
            "keywords": ["demo"],
            "portals": {
                "presets": ["national_wire"],
                "exclude": ["antaranews.com"],
            },
        }
        cfg = TopicConfig.model_validate(raw)
        resolved = resolve_topic_portals(cfg, catalog=self.catalog)

        domains = [p.domain for p in resolved]
        self.assertNotIn("antaranews.com", domains)
        self.assertTrue(any("kompas.com" in d or "detik.com" in d for d in domains))

    def test_resolve_topic_portals_fingerprint_auto_routing(self):
        raw = {
            "topic": {"id": "env", "name": "Environment"},
            "keywords": ["forest"],
            "portals": {
                "include": ["mongabay.co.id", "kaltengpos.co"],
            },
        }
        cfg = TopicConfig.model_validate(raw)
        resolved = resolve_topic_portals(cfg, catalog=self.catalog)

        by_domain = {p.domain: p for p in resolved}
        self.assertIn("mongabay.co.id", by_domain)
        # mongabay.co.id uses sitemap
        self.assertTrue(by_domain["mongabay.co.id"].sitemap_url is not None)


class TestTopicScaffolding(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_generate_topic_template(self):
        out_file = self.tmp_dir / "custom_test.yaml"
        generated_path = generate_topic_template("custom_test", str(out_file))
        self.assertTrue(generated_path.exists())

        cfg = load_topic_config(str(generated_path))
        self.assertEqual(cfg.topic.id, "custom_test")
        self.assertGreater(len(cfg.get_keywords()), 0)

    def test_validate_built_in_topics(self):
        for topic_id in ["ikn", "karhutla"]:
            diag = validate_topic(topic_id)
            self.assertTrue(diag["valid"], f"Topic {topic_id} failed validation: {diag.get('error')}")
            self.assertGreater(diag["total_keywords"], 0)
            self.assertGreater(diag["resolved_portals_count"], 0)


if __name__ == "__main__":
    unittest.main()

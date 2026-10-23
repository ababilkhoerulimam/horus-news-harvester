"""
Catalog Registry and Auto-Routing Resolver for Indonesian Media Portals.
Maps declarative topic presets and domains to concrete sitemap and WP-REST endpoints
using empirical fingerprints from 66 Indonesian news portals.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from processors.topic_config import TopicConfig, PortalEntry


@dataclass
class ResolvedPortal:
    """A concrete portal crawler target with verified endpoints and extraction adapter."""
    domain: str
    name: str
    adapter: str  # "sitemap" or "wordpress_rest"
    sitemap_url: Optional[str] = None
    wp_api_base: Optional[str] = None
    region: Optional[str] = None
    tier: int = 1

    @property
    def portal_id(self) -> str:
        return self.domain.replace(".", "_").replace("-", "_")


# Standard path constants
CATALOG_PATH = Path("config/portals_catalog.json")
FINGERPRINTS_PATH = Path("config/portal_fingerprints.json")


class PortalCatalogRegistry:
    """In-memory index of all 66 fingerprinted Indonesian portals."""

    def __init__(self, catalog_file: Optional[Path] = None, fingerprints_file: Optional[Path] = None):
        self.catalog_file = catalog_file or CATALOG_PATH
        self.fingerprints_file = fingerprints_file or FINGERPRINTS_PATH
        self._fingerprints: Dict[str, Dict[str, Any]] = {}
        self._catalog: Dict[str, Dict[str, Any]] = {}
        self._load_datasets()

    def _load_datasets(self):
        """Loads and indexes portal catalog and technical fingerprints."""
        if self.fingerprints_file.exists():
            try:
                with open(self.fingerprints_file, "r", encoding="utf-8") as f:
                    fp_list = json.load(f)
                    for item in fp_list:
                        dom = item.get("domain", "").lower()
                        if dom:
                            self._fingerprints[dom] = item
            except Exception:
                pass

        if self.catalog_file.exists():
            try:
                with open(self.catalog_file, "r", encoding="utf-8") as f:
                    cat_data = json.load(f)
                    for item in cat_data.get("portals", []):
                        dom = item.get("domain", "").lower()
                        if dom:
                            self._catalog[dom] = item
            except Exception:
                pass

    def get_known_domains(self) -> Set[str]:
        return set(self._fingerprints.keys()) | set(self._catalog.keys())

    def resolve_domain(self, domain: str, overrides: Optional[Dict[str, Any]] = None) -> ResolvedPortal:
        """
        Auto-resolves a domain into a concrete ResolvedPortal instance.
        Infers adapter (sitemap vs WP-REST), endpoints, and regional metadata.
        """
        dom = domain.strip().lower()
        if "://" in dom:
            dom = dom.split("://", 1)[1]
        dom = dom.split("/")[0].split(":")[0]

        ovr = overrides or {}
        fp = self._fingerprints.get(dom, {})
        cat = self._catalog.get(dom, {})

        name = ovr.get("name") or fp.get("name") or cat.get("name") or dom
        region = ovr.get("region")
        tier = ovr.get("tier") or fp.get("tier") or cat.get("tier") or 2

        # 1. Determine adapter
        explicit_adapter = ovr.get("adapter")
        if explicit_adapter and explicit_adapter != "auto":
            adapter = explicit_adapter
        elif fp.get("has_wp_json") and fp.get("wp_json_endpoint"):
            adapter = "wordpress_rest"
        else:
            adapter = "sitemap"

        # 2. Determine endpoints
        sitemap_url = ovr.get("sitemap_url")
        if not sitemap_url:
            active_sitemaps = fp.get("active_sitemaps", [])
            if active_sitemaps:
                sitemap_url = active_sitemaps[0]
            else:
                sitemap_url = f"https://{dom}/sitemap.xml"

        wp_api_base = ovr.get("wp_api_base")
        if not wp_api_base:
            wp_api_base = fp.get("wp_json_endpoint") or f"https://{dom}/wp-json/wp/v2"

        return ResolvedPortal(
            domain=dom,
            name=name,
            adapter=adapter,
            sitemap_url=sitemap_url,
            wp_api_base=wp_api_base,
            region=region,
            tier=tier,
        )


# Global catalog instance
default_catalog = PortalCatalogRegistry()


# Predefined Topic Presets
CATALOG_PRESETS: Dict[str, List[str]] = {
    # 15 Tier-1 National Mainstream Portals
    "national_wire": [
        "antaranews.com",
        "detik.com",
        "kompas.com",
        "cnnindonesia.com",
        "tempo.co",
        "republika.co.id",
        "jawapos.com",
        "tribunnews.com",
        "tvonenews.com",
        "liputan6.com",
        "sindonews.com",
        "suara.com",
        "merdeka.com",
        "viva.co.id",
        "jpnn.com",
    ],

    # Regional Episenters & Local News Networks
    "regional_epicenters": [
        "kaltengpos.co",
        "riaupos.co",
        "pontianakpost.jawapos.com",
        "jambiekspres.co.id",
        "sumeks.co",
        "beritajatim.com",
        "harianjogja.com",
        "fajar.co.id",
        "banjarmasinpost.co.id",
        "bantennews.co.id",
        "balipost.com",
        "niaga.asia",
        "borneonews.co.id",
        "goriau.com",
    ],

    # Specialized Environmental & Policy Investigative Media
    "investigative_environment": [
        "mongabay.co.id",
        "betahita.id",
        "forestdigest.com",
        "projectmultatuli.org",
    ],

    # Complete 66 Indonesian News Portals Catalog
    "all_66_catalog": [
        "harianjogja.com", "detik.com", "antaranews.com", "beritajatim.com", "tvonenews.com",
        "kompas.com", "cnnindonesia.com", "tribunnews.com", "jawapos.com", "sindonews.com",
        "suara.com", "bolasport.com", "viva.co.id", "jpnn.com", "republika.co.id",
        "liputan6.com", "okezone.com", "skor.id", "merdeka.com", "inews.id",
        "tempo.co", "kumparan.com", "idntimes.com", "bola.net", "bola.com",
        "panditfootball.com", "football5star.com", "liga1skor.com", "ligaindonesiabaru.com", "indosport.com",
        "katadata.co.id", "bisnis.com", "kontan.co.id", "cnbcindonesia.com", "idxchannel.com",
        "tirto.id", "merahputih.com", "thejakartapost.com", "mediaindonesia.com", "koran-jakarta.com",
        "rmol.id", "fajar.co.id", "bantennews.co.id", "balipost.com", "nusabali.com",
        "pikiran-rakyat.com", "poskota.co.id", "suaramerdeka.com", "solopos.com", "rri.co.id",
        "tvrinews.com", "metrotvnews.com", "kompas.tv", "beritasatu.com", "radarsurabaya.jawapos.com",
        "radarjogja.jawapos.com", "radarbanyuwangi.jawapos.com", "radarmalang.jawapos.com", "radarsolo.jawapos.com",
        "kaltengpos.co", "riaupos.co", "jambiekspres.co.id", "sumeks.co", "borneonews.co.id",
        "goriau.com", "niaga.asia"
    ],
}


def resolve_topic_portals(
    cfg: TopicConfig,
    catalog: Optional[PortalCatalogRegistry] = None
) -> List[ResolvedPortal]:
    """
    Expands declarative presets and explicit portal inclusions for a TopicConfig.
    Applies exclusions and resolves each portal's optimal extraction adapter.
    """
    cat = catalog or default_catalog
    resolved_map: Dict[str, ResolvedPortal] = {}
    exclude_set: Set[str] = set()

    # 1. Modern preset-driven configuration
    if cfg.portals:
        for ex in cfg.portals.exclude:
            exclude_set.add(ex.strip().lower())

        # Expand Presets
        for p_name in cfg.portals.presets:
            preset_domains = CATALOG_PRESETS.get(p_name.strip().lower(), [])
            for dom in preset_domains:
                if dom not in exclude_set and dom not in resolved_map:
                    resolved_map[dom] = cat.resolve_domain(dom)

        # Process Explicit Inclusions
        for inc in cfg.portals.include:
            if isinstance(inc, str):
                dom = inc.strip().lower()
                if dom not in exclude_set:
                    resolved_map[dom] = cat.resolve_domain(dom)
            elif isinstance(inc, PortalEntry):
                dom = inc.domain.strip().lower()
                if dom not in exclude_set:
                    resolved_map[dom] = cat.resolve_domain(dom, overrides=inc.model_dump())

    # 2. Legacy target_portals support (nested dictionary of portal lists)
    if cfg.target_portals:
        for cat_name, portal_list in cfg.target_portals.items():
            if isinstance(portal_list, list):
                for p in portal_list:
                    if isinstance(p, dict):
                        dom = p.get("domain", "").strip().lower()
                        if dom and dom not in exclude_set:
                            resolved_map[dom] = cat.resolve_domain(dom, overrides=p)

    return list(resolved_map.values())

"""
Pydantic v2 Topic Configuration Schema and Validator for NNIH.
Provides declarative validation, keyword extraction, and portal targeting.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union
import yaml
from pydantic import BaseModel, Field, field_validator, ValidationError


class TopicMeta(BaseModel):
    """Metadata identifying the intelligence harvesting topic."""
    id: str = Field(..., description="Unique slug for the topic (e.g. karhutla, ikn, pemilu)")
    name: str = Field(..., description="Human-readable title of the topic")
    description: Optional[str] = Field(default="", description="Scope and analytical objective")
    default_output: Optional[str] = Field(default=None, description="Target JSONL file path")
    default_dapt_output: Optional[str] = Field(default=None, description="Target DAPT text corpus path")

    @field_validator("id")
    @classmethod
    def validate_id_slug(cls, v: str) -> str:
        clean = v.strip().lower()
        if not clean:
            raise ValueError("Topic id cannot be empty")
        if not clean.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Topic id '{v}' must be alphanumeric with dashes or underscores")
        return clean


class PortalEntry(BaseModel):
    """Explicit portal override entry within a topic configuration."""
    domain: str = Field(..., description="Apex or subdomain (e.g. detik.com, kaltengpos.co)")
    name: Optional[str] = Field(default=None, description="Display name of the portal")
    adapter: str = Field(default="auto", description="Extraction strategy: auto, sitemap, wordpress_rest")
    sitemap_url: Optional[str] = Field(default=None, description="Explicit XML sitemap URL")
    wp_api_base: Optional[str] = Field(default=None, description="Explicit WP-JSON REST base URL")
    region: Optional[str] = Field(default=None, description="Regional epicenter coverage")

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, v: str) -> str:
        clean = v.strip().lower()
        if "://" in clean:
            clean = clean.split("://", 1)[1]
        return clean.split("/")[0].split(":")[0]



class PortalsConfig(BaseModel):
    """Declarative portal targeting using presets, inclusions, and exclusions."""
    presets: List[str] = Field(default_factory=list, description="Catalog preset names (e.g. national_wire, all_66_catalog)")
    include: List[Union[str, PortalEntry]] = Field(default_factory=list, description="Explicit additional portals")
    exclude: List[str] = Field(default_factory=list, description="Domains to blacklist from crawl")


class OutputConfig(BaseModel):
    """Analytical export options for harvested topic records."""
    format: str = Field(default="jsonl", description="Export format: jsonl, parquet, sqlite, duckdb")
    destination_path: Optional[str] = Field(default=None, description="Custom export destination")
    min_word_count: int = Field(default=50, description="Minimum word threshold for inclusion in DAPT corpus")
    min_quality_chars: int = Field(
        default=1000,
        ge=0,
        description="Bodies below this character count are kept but tagged quality='low'",
    )


class YouTubeSocialConfig(BaseModel):
    """YouTube discovery and enrichment budget for one topic.

    A cap of None means unlimited. Zero is rejected because the fetch path already
    reads 0 as "fetch nothing", and two meanings for one value is a trap.
    """
    queries: List[str] = Field(default_factory=list, description="Search queries, e.g. \"timnas indonesia\"")
    channels: List[str] = Field(default_factory=list, description="Channel handles or URLs to scan")
    max_videos_per_run: int = Field(default=25, description="Cap on videos enriched per run")
    max_comments_per_video: Optional[int] = Field(default=None, description="Cap on root comments per video, None for unlimited")
    max_replies_per_comment: Optional[int] = Field(default=None, description="Cap on replies per root comment, None for unlimited")
    max_runtime_seconds: Optional[int] = Field(default=None, description="Wall-clock budget per run, None for unlimited")
    max_seconds_per_video: Optional[int] = Field(default=None, description="Soft stop per video, None for unlimited")
    video_workers: int = Field(default=4, ge=1, description="Videos harvested concurrently")
    reply_workers: int = Field(default=4, ge=1, description="Threads used to fetch reply threads")
    request_min_interval_ms: int = Field(default=250, ge=0, description="Minimum gap between InnerTube requests")
    progress_every: int = Field(default=500, ge=1, description="Log progress every N comments")
    yt_dlp_max_comments: int = Field(default=20000, ge=1, description="Ceiling for the yt-dlp fallback, which buffers in memory")

    @field_validator("max_comments_per_video", "max_replies_per_comment", "max_runtime_seconds", "max_seconds_per_video", mode="before")
    @classmethod
    def _reject_zero_cap(cls, value: Any) -> Any:
        if value == 0:
            raise ValueError("use null for unlimited, not 0")
        if isinstance(value, int) and value < 0:
            raise ValueError("cap must not be negative")
        return value


class TwitterSocialConfig(BaseModel):
    """Twitter discovery budget for one topic."""
    queries: List[str] = Field(default_factory=list)
    handles: List[str] = Field(default_factory=list)
    max_tweets_per_query: int = Field(default=50, description="Cap on tweets per query or handle")


class SocialConfig(BaseModel):
    """Social channel targets. Platforms with no targets are skipped, not failed."""
    enabled: bool = Field(default=True, description="Run social harvest for this topic")
    youtube: YouTubeSocialConfig = Field(default_factory=YouTubeSocialConfig)
    twitter: TwitterSocialConfig = Field(default_factory=TwitterSocialConfig)


class TopicConfig(BaseModel):
    """Root configuration model for a national news intelligence topic."""
    topic: TopicMeta
    keywords: Union[List[str], Dict[str, Any]] = Field(
        ...,
        description="Flat keyword list or categorized dictionary (events, operations, focal_regions, etc.)"
    )
    portals: Optional[PortalsConfig] = Field(default=None, description="Modern preset-driven portal configuration")
    target_portals: Optional[Dict[str, Any]] = Field(default=None, description="Legacy portal categorization map")
    output: Optional[OutputConfig] = Field(default_factory=OutputConfig)
    social: Optional[SocialConfig] = Field(default=None, description="Social channel targets (YouTube, Twitter)")

    def get_keywords(self) -> List[str]:
        """Flattens, lowercases, strips, and deduplicates all topic keywords."""
        extracted: Set[str] = set()

        def _walk(item: Any):
            if isinstance(item, str):
                cleaned = item.strip().lower()
                if cleaned:
                    extracted.add(cleaned)
            elif isinstance(item, list):
                for sub in item:
                    _walk(sub)
            elif isinstance(item, dict):
                for val in item.values():
                    _walk(val)

        _walk(self.keywords)
        return sorted(list(extracted))

    def get_output_path(self) -> Path:
        """Determines the default JSONL output destination."""
        if self.topic.default_output:
            return Path(self.topic.default_output)
        return Path(f"data/raw/news/{self.topic.id}.jsonl")

    def get_dapt_output_path(self) -> Path:
        """Determines the default DAPT text output destination."""
        if self.topic.default_dapt_output:
            return Path(self.topic.default_dapt_output)
        return Path(f"data/processed/nusabert_{self.topic.id}_dapt.txt")


def load_topic_config(path_or_id: Union[str, Path]) -> TopicConfig:
    """
    Loads and validates a topic YAML configuration file.
    Accepts either a direct file path or a topic slug (searches config/topics/<slug>.yaml).
    Raises FileNotFoundError or ValueError with detailed diagnostic messages.
    """
    target = Path(path_or_id)
    if not target.suffix:
        # Passed as topic slug
        target = Path("config/topics") / f"{path_or_id}.yaml"

    if not target.exists():
        raise FileNotFoundError(f"Topic configuration file not found: {target}")

    try:
        with open(target, "r", encoding="utf-8") as f:
            raw_dict = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML syntax error in '{target}': {e}")

    if not isinstance(raw_dict, dict):
        raise ValueError(f"Topic configuration '{target}' must be a YAML mapping/dictionary")

    try:
        return TopicConfig.model_validate(raw_dict)
    except ValidationError as err:
        errors_summary = []
        for e in err.errors():
            loc = " -> ".join(str(x) for x in e["loc"])
            msg = e["msg"]
            errors_summary.append(f"  * [{loc}]: {msg}")
        raise ValueError(
            f"Validation failed for topic config '{target.name}':\n" + "\n".join(errors_summary)
        )

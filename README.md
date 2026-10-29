<div align="center">
  <img src="logo.png" alt="Horus Logo" width="128" height="128">

  <h1>Horus News Harvester</h1>
  <p><strong>Topic-Agnostic News Intelligence, Verification, and Multi-Modal Corpus Engine for Indonesian Media</strong></p>

  <p align="center">
    <img src="https://img.shields.io/badge/Language-Python_3.9+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/Package-v0.3.0-009688?style=flat-square" alt="Version">
    <img src="https://img.shields.io/badge/Architecture-Declarative_YAML-F05033?style=flat-square&logo=yaml&logoColor=white" alt="YAML">
    <img src="https://img.shields.io/badge/Anti--Bot-TLS_JA4_Chrome124-blue?style=flat-square" alt="TLS">
    <img src="https://img.shields.io/badge/Frontier-SQLite_WAL-4479A1?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite">
    <img src="https://img.shields.io/badge/License-MIT-success?style=flat-square" alt="MIT">
  </p>

  <p align="center">
    A production-grade intelligence harvesting and data engineering platform built for NLP researchers, social network analysts, and computational journalists. Horus automates multi-portal news discovery across 66 fingerprinted Indonesian outlets, extracts clean long-form bodies via domain-specific rule matching and WordPress REST APIs, mines social discourse on YouTube and Twitter/X, enforces crash-proof SQLite WAL frontier queues, and exports downstream corpora to Parquet, DuckDB, SQLite, and NusaBERT DAPT formats.
  </p>
</div>

## Tech Stack

![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
![Git](https://img.shields.io/badge/git-%23F05033.svg?style=for-the-badge&logo=git&logoColor=white)
![Markdown](https://img.shields.io/badge/markdown-%23000000.svg?style=for-the-badge&logo=markdown&logoColor=white)
![Pandas](https://img.shields.io/badge/pandas-%23150458.svg?style=for-the-badge&logo=pandas&logoColor=white)
![NumPy](https://img.shields.io/badge/numpy-%23013243.svg?style=for-the-badge&logo=numpy&logoColor=white)

## Core Capabilities

### 1. Declarative Topic Taxonomy
Define custom multi-axis monitoring configurations in YAML under `config/topics/<id>.yaml`. Topics specify keyword hierarchies, geographic boundaries, output destinations, and automated portal routing presets validated strictly through Pydantic v2.

### 2. Fingerprinted 66-Portal Auto-Routing
The crawler references `config/portal_fingerprints.json` to automatically discover XML sitemaps (including `.xml.gz` decompression) and direct WordPress REST API endpoints (`/wp-json/wp/v2/posts`) across national wire and regional publications:
* `national_wire`: Primary national news hubs including Antara, Detik, Kompas, CNN Indonesia, Tempo, Republika, Jawa Pos, and Tribunnews.
* `regional_epicenters`: Regional newspapers and provincial leaders including Kalteng Pos, Riau Pos, Pontianak Post, Jambi Ekspres, Sumeks, BeritaJatim, Bali Post, and Niaga Asia.
* `investigative_environment`: Specialized environmental investigative desks including Mongabay Indonesia, Betahita, Forest Digest, and Project Multatuli.
* `all_66_catalog`: The comprehensive catalog of 66 fingerprinted Indonesian media outlets.

### 3. Rule-Based Extraction and Unpacking
Generic HTML scrapers frequently lose article bodies when encountering complex JavaScript layouts. Horus resolves this via two strategies configured in `config/extraction_rules.json`:
* Next.js Data Unpacking: Extracts raw JSON payloads directly from `__NEXT_DATA__` script blocks (such as Jawa Pos article state) with zero browser overhead.
* Domain-Specific XPath Rules: Targeted DOM containers for high-traffic portals like Detik (`//article`) and Kompas (`//div[contains(@class,'read__content')]`), preventing fragment truncation and capturing full multi-thousand-character narratives.
* WordPress REST Fast Path: Reads structured posts directly from REST endpoints, reducing payload size by over 80 percent compared to full HTML DOM rendering.

### 4. Multi-Channel Social Discourse Harvesting
Beyond static news websites, Horus ingests related public discourse from social platforms:
* YouTube InnerTube Transport: Discovers topic-relevant videos and extracts deep nested comment threads and replies using authenticated InnerTube calls, SAPISIDHASH signing, and configurable concurrency pools.
* Twitter / X GraphQL Gateway: Performs keyword search and handle timeline extraction through Twitter Web GraphQL APIs with automated session token extraction from local Firefox browser profiles.

### 5. Resilient Concurrency and Quality Control
* Adaptive Domain Semaphores: Throttles requests to fragile self-hosted regional servers (such as local WordPress VPS sites) while allowing high-throughput enterprise CDNs to operate at full concurrency.
* Quality Tagging: Measures extracted character volume against empirical thresholds (`MIN_QUALITY_CHARS = 1000`), tagging low-volume fragments as `quality="low"` for targeted reharvesting via `scripts/reharvest_low_quality.py`.
* Crash-Resilient SQLite WAL Frontier: Tracks pending, in-progress, completed, and failed URLs in dedicated SQLite databases with 60-second busy timeouts and batch commit buffering.

### 6. Downstream Analytical Export
Convert raw line-delimited JSONL feeds into optimized analytical storage formats:
* SQLite Analytical Store: Relational schema indexed for rapid multi-channel joins.
* Apache Parquet: Compressed columnar files partitioned by sport or topic.
* DuckDB: High-performance analytical database for ad-hoc SQL aggregation.
* NusaBERT DAPT Corpus: Sanitized sentence-segmented text corpus for Domain-Adaptive Pretraining with masked language modeling.

## Mathematical Formulation

The extraction quality metric evaluates candidate articles based on character yield:

$$
\text{Yield Ratio} = \frac{\text{Extracted Narrative Characters}}{\text{Raw HTML Payload Bytes}}
$$

For domain concurrency control, the adaptive worker capacity dynamically scales per origin:

$$
C_{\text{domain}} = \min \left( C_{\text{max}}, \; \frac{W_{\text{active}}}{K_{\text{slow}}} \right)
$$

where $C_{\text{max}}$ is the process socket concurrency limit, $W_{\text{active}}$ represents the total active worker threads, and $K_{\text{slow}}$ denotes the concurrency dampening constant applied to low-resource VPS endpoints.

## Installation

### Prerequisites
* Python 3.9 or higher
* Git

```bash
git clone https://github.com/ababilkhoerulimam/horus-news-harvester.git
cd horus-news-harvester
pip install -e ".[all]"
```

## CLI Usage Reference

### Topic Management

List all configured intelligence topics:

```bash
python cli.py topic list
```

Initialize a new topic configuration:

```bash
python cli.py topic init food_estate
```

Validate topic syntax and verify portal endpoint connectivity:

```bash
python cli.py topic validate food_estate
```

### News Harvesting

Run historical backfill and active topic harvesting:

```bash
# Harvest topic using multi-process engine
python cli.py topic harvest --topic karhutla --cores 2 --concurrency 25

# Harvest with date range constraints
python cli.py topic harvest --topic ikn --days 90 --resume

# Inspect crawl queue and portal breakdown statistics
python cli.py topic harvest --topic sports --stats
```

### Social Media Harvesting

Mine public discussions across YouTube and Twitter:

```bash
# Harvest both YouTube videos and Twitter timelines for a topic
python cli.py topic social --topic sports --platform both --limit 50

# Mine YouTube comments with reply nesting
python cli.py topic social --topic karhutla --platform youtube --limit 20
```

### Quality Reporting and Reharvesting

Audit extracted content length distributions and reharvest short fragments:

```bash
# Generate offline yield report across harvested portals
python scripts/report_yield.py --channel news --top 15

# Reharvest low-quality records for a specific domain
python scripts/reharvest_low_quality.py --path data/raw/news/football.jsonl --domain detik.com --merge
```

### Analytical Export and NLP Corpus Generation

Export raw JSONL feeds into analytical stores or pretraining corpora:

```bash
# Export to DuckDB database
python cli.py topic export --topic sports --format duckdb

# Export to partitioned Apache Parquet
python cli.py topic export --topic karhutla --format parquet

# Build pre-tokenized corpus for NusaBERT Domain-Adaptive Pretraining
python cli.py topic dapt --topic sports --min-words 50
```

## Repository Structure

```
horus-news-harvester/
├── backends/
│   ├── curl_impersonate.py      Chrome 124 TLS JA4 impersonation with HTTP/2 keep-alive
│   ├── firefox_cookies.py       Cross-platform Firefox browser profile cookie extractor
│   ├── headless_browser.py      Isolated zero-temp Chromium automation gateway
│   ├── twitter_graphql.py       Authenticated Twitter Web GraphQL client
│   └── wordpress_rest.py        Native WordPress REST API post content reader
├── channels/
│   ├── innertube.py             InnerTube client transport and request signing
│   ├── portal_news.py           Web news parser and Inertia.js fast-path unpacker
│   ├── social_twitter.py        Twitter timeline and search channel adapter
│   └── social_youtube.py        YouTube video and nested reply harvester
├── config/
│   ├── extraction_rules.json    Verified domain XPath and Next.js extraction rules
│   ├── portal_fingerprints.json Sitemaps and REST endpoint fingerprints for 66 outlets
│   ├── portals_catalog.json     Comprehensive 66-portal metadata registry
│   └── topics/                  Declarative topic YAML definitions (karhutla, ikn, sports)
├── data/
│   ├── raw/                     Standardized line-delimited JSONL feeds
│   └── state/                   SQLite WAL crawl frontiers and tracking cursors
├── processors/
│   ├── cleaner.py               Zero-width stripper, timestamp normalizer, and deduplicator
│   ├── crawl_frontier.py        Resumable SQLite WAL queue with in-memory commit buffer
│   ├── dapt.py                  NLP pretraining corpus formatter and text sanitizer
│   ├── exporter.py              Analytical streaming exporter for SQLite, Parquet, and DuckDB
│   ├── extraction_rules.py      Rule-based extraction engine with XPath and Next.js support
│   ├── fetch_pipeline.py        Hierarchical extraction ladder and proxy retry pipeline
│   ├── http_client.py           Resilient HTTP client with proxy pooling and backoff
│   ├── metrics.py               Multilingual counter parser for view, like, and reply metrics
│   ├── portal_catalog.py        Domain preset resolver and auto-routing engine
│   ├── record_writer.py         Canonical JSONL record sink with quality thresholding
│   ├── sqlite_util.py           Shared SQLite WAL connection utilities
│   ├── topic_config.py          Pydantic v2 declarative topic validation schema
│   ├── tweet_normalizer.py      Twitter payload mapping to canonical harvest records
│   ├── worker_loop.py           Multi-core async queue dispatch loop with domain semaphores
│   └── youtube_normalizer.py    YouTube payload mapping to video and comment records
├── scripts/
│   ├── discover_catalog_portals.py Catalog endpoint discovery and health auditor
│   ├── fingerprint_portals.py   Fingerprint generator for Indonesian media outlets
│   ├── reharvest_low_quality.py Targeted reharvesting engine for truncated articles
│   ├── report_yield.py          Harvest yield and content length analytical reporter
│   ├── sitemap_backfill.py      Multi-process historical sitemap and date archive harvester
│   ├── social_harvester.py      Social harvesting orchestrator for YouTube and Twitter
│   └── topic_harvester.py       Topic-agnostic 2-core and 4-core multi-process harvester
├── tests/                       Comprehensive test suite (272 unit and integration tests)
├── cli.py                       Unified command line interface
├── CLAUDE.md                    Developer guidance and architectural invariants
├── logo.png                     Horus News Harvester visual emblem
└── pyproject.toml               PEP 621 package specification and entrypoint definitions
```

## Running Tests

Execute the complete test suite:

```bash
python -m unittest discover tests
```

All 272 unit and integration tests must pass before committing changes.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

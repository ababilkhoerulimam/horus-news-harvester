<div align="center">
  <h1>National News Intelligence Harvester (NNIH)</h1>
  <p><strong>Topic-Agnostic, Plug-and-Play Intelligence Crawling & Corpus Pipeline for Indonesian Media</strong></p>

  <p align="center">
    <img src="https://img.shields.io/badge/Language-Python_3.9+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/Package-NNIH_v0.2.0-009688?style=flat-square" alt="Version">
    <img src="https://img.shields.io/badge/Architecture-Declarative_YAML-F05033?style=flat-square&logo=yaml&logoColor=white" alt="YAML">
    <img src="https://img.shields.io/badge/Anti--Bot-TLS_JA4_Chrome124-blue?style=flat-square" alt="TLS">
    <img src="https://img.shields.io/badge/Frontier-SQLite_WAL-4479A1?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite">
    <img src="https://img.shields.io/badge/License-MIT-success?style=flat-square" alt="MIT">
  </p>

  <p align="center">
    A production-grade, self-service news intelligence harvesting engine engineered for researchers, data scientists, and analysts. Define any national topic (e.g. Karhutla, IKN, Pemilu, Food Estate, or Sports) via declarative YAML—NNIH handles automated sitemap resolution across 66 fingerprinted Indonesian portals, TLS JA4 anti-bot bypass, Inertia.js fast unpacking, crash-proof WAL frontier management, and multi-format export (Parquet, DuckDB, SQLite, NusaBERT DAPT).
  </p>
</div>

---

## Key Capabilities

* **Declarative Topic Taxonomy (`config/topics/<id>.yaml`)**: Zero-code crawling. Define custom multi-axis keyword taxonomies and target portals in YAML. Validated strictly by Pydantic v2.
* **Empirical 66-Portal Auto-Routing**: Automatically discovers XML sitemaps (with transparent `.xml.gz` decompression) and WordPress REST API endpoints (`/wp-json/wp/v2/posts`) from `config/portal_fingerprints.json`.
* **Out-of-the-Box Presets**:
  * `national_wire`: Primary national news hubs (Antara, Detik, Kompas, CNN Indonesia, Tempo, Republika, Jawa Pos, Tribunnews, etc.).
  * `regional_epicenters`: Jawa Pos Regional Network & provincial leaders (Kalteng Pos, Riau Pos, Pontianak Post, Jambi Ekspres, Sumeks, BeritaJatim, etc.).
  * `investigative_environment`: Specialized investigative desks (Mongabay Indonesia, Betahita, Forest Digest, Project Multatuli).
  * `all_66_catalog`: Complete catalog of 66 fingerprinted Indonesian media outlets.
* **Inertia.js Fast-Path Unpacking**: Extracts raw article payloads directly from client-side rendered `data-page="..."` JSON attributes dominant across regional networks (Radar, Kalteng Pos, Riau Pos)—zero headless browser overhead.
* **Transport-Level TLS JA4 Fingerprint Spoofing**: Transparent Chrome 124 TLS signature emulation via `curl_cffi` to bypass Cloudflare/WAF anti-scraping challenges.
* **Crash-Resilient SQLite WAL Frontier**: State-managed crawl queue (`data/state/crawl_{topic}_frontier.db`) enabling pause, resume, and multithreaded delta crawling without memory bloat.
* **Multi-Format Analytical Export**: Stream harvested articles directly to JSONL, Apache Parquet, DuckDB, SQLite, or pre-tokenized NusaBERT DAPT corpus files.

---

## Quickstart (3-Minute Self-Service Topic)

### 1. Installation

```bash
git clone https://github.com/your-username/national-news-intelligence-harvester.git
cd national-news-intelligence-harvester
pip install -e .
```

### 2. Scaffold a New Topic

Generate a fresh topic configuration template:

```bash
nnih topic init pemilu2024
```

This creates `config/topics/pemilu2024.yaml`:

```yaml
topic:
  id: "pemilu2024"
  name: "Pemilu & Pilpres 2024 Intelligence"
  description: "Monitoring dinamika pilpres, sengketa pemilu, dan rekapitulasi KPU"

keywords:
  candidates:
    - "prabowo subianto"
    - "gibran rakabuming"
    - "anies baswedan"
    - "ganjar pranowo"
  institutions:
    - "kpu ri"
    - "bawaslu"
    - "mahkamah konstitusi"
    - "sengketa hasil pilpres"

portals:
  presets:
    - "national_wire"
    - "regional_epicenters"
  include:
    - "katadata.co.id"
  exclude: []

output:
  format: "jsonl"
  min_word_count: 50
```

### 3. Validate Topic Configuration

Verify YAML syntax, keyword flattening, and portal endpoint resolution:

```bash
nnih topic validate pemilu2024
```

Output:
```text
[OK] Topic 'pemilu2024' (Pemilu & Pilpres 2024 Intelligence) is VALID
- Total Keywords: 8
- Resolved Portals: 42
- Output File: data/raw/news/pemilu2024.jsonl
```

### 4. Execute Harvest

Start multithreaded discovery and harvesting:

```bash
# Discover & harvest articles from the last 30 days using 16 workers
nnih topic harvest --topic pemilu2024 --days 30 --workers 16 --limit 5000

# Resume interrupted crawl
nnih topic harvest --topic pemilu2024 --resume
```

### 5. Analytical Export

Export harvested raw intelligence into modern columnar formats or NLP pretraining corpora:

```bash
# Export to Apache Parquet
nnih topic export --topic pemilu2024 --format parquet

# Export to DuckDB database table
nnih topic export --topic pemilu2024 --format duckdb

# Generate clean corpus for NusaBERT Domain-Adaptive Pretraining (DAPT)
nnih topic dapt --topic pemilu2024 --min-words 60
```

---

## Production Starters Included

The repository includes battle-tested intelligence configurations ready for deployment:

1. **`config/topics/ikn.yaml` (Ibu Kota Nusantara)**:
   - Tracks KIPP infrastructure, governance (OIKN), consortium investments, and agrarian/environmental impacts across Kalimantan.
2. **`config/topics/karhutla.yaml` (Forest & Peatland Fires)**:
   - 5-axis intelligence tracking physical fire indicators, water bombing operations, KLHK enforcement, corporate concessions, and ground-zero regencies in Sumatra/Kalimantan.

List all available topics anytime:

```bash
nnih topic list
```

---

## Directory Layout

```
national-news-intelligence-harvester/
├── backends/               Transport gateways (curl_impersonate, direct_http, jina_reader)
├── channels/               Extraction channels (portal_news, social_youtube, social_twitter)
├── config/
│   ├── topics/             Declarative topic YAML definitions (ikn.yaml, karhutla.yaml)
│   ├── portal_fingerprints.json Empirical sitemap and WP-REST endpoint fingerprints
│   └── portals_catalog.json Complete 66-portal metadata registry
├── data/
│   ├── raw/                Standardized line-delimited JSONL feeds
│   └── state/              SQLite WAL crawl frontiers and cursors
├── processors/
│   ├── topic_config.py     Pydantic v2 validation schema for topic files
│   ├── portal_catalog.py   Smart preset resolver and auto-routing engine
│   ├── crawl_frontier.py   Persistent SQLite queue with URL hashing & retry logic
│   └── cleaner.py          NLP sanitization, zero-width stripper & ad filter
├── scripts/
│   ├── topic_harvester.py  Topic harvesting and export engine
│   ├── sitemap_backfill.py Historical XML sitemap and WP-REST crawler
│   └── local_harvester.py  Legacy sports-specific multi-channel runner
├── tests/                  Unit test suites (test_topic_engine.py, test_channels.py)
├── cli.py                  Unified CLI command gateway (nnih)
└── pyproject.toml          PEP 621 open-source package metadata
```

---

## Legacy Sports Multi-Channel Harvester

The original sports intelligence subsystem remains fully operational:

```bash
# Search articles across sports taxonomy
python cli.py search --query "Timnas Indonesia" --sport football --limit 5

# Run sports batch harvester across portals, Twitter/X, and YouTube
python scripts/local_harvester.py
```

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

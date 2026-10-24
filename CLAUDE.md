# National News Intelligence Harvester (NNIH) - Developer & Agent Guide

## Core Architecture
NNIH is a topic-agnostic, declarative news intelligence crawler for 66 fingerprinted Indonesian national and regional media portals.

### Golden Rules for Agents:
1. **Never use or resurrect legacy scripts**:
   - `scripts/local_harvester.py` (DELETED)
   - `scripts/scheduler.py` (DELETED)
   - `scripts/prepare_dapt_corpus.py` (DELETED)
   - `scripts/analyze_dewa_sites.py` (DELETED)
2. **Always use declarative topic engine (`nnih` or `python cli.py topic ...`)**:
   - Configuration files live in `config/topics/<topic_id>.yaml` (e.g. `karhutla.yaml`, `ikn.yaml`, `sports.yaml`).
   - Every topic is strictly validated via Pydantic v2 (`processors/topic_config.py`).
3. **Execution Engine**:
   - The engine is the **2-Core Multi-Process Async Engine** (`scripts/topic_harvester.py`).
   - Allocates exactly 2 physical CPU cores via `multiprocessing.Process` + `curl_cffi.requests.AsyncSession` (25 concurrent sockets per core = 50 total).
   - Keeps the remaining 10 cores idle to prevent system lag and keep workstation cool.
   - State and queue are managed crash-proof in SQLite WAL (`data/state/crawl_{topic}_frontier.db`) with in-memory batch commit buffers (50 items).

## CLI Reference
```bash
# 1. Topic management
python cli.py topic list
python cli.py topic init <topic_id>
python cli.py topic validate <topic_id>

# 2. Harvesting (default: 2 cores, 25 async sockets/core)
python cli.py topic harvest --topic <topic_id> [--days N] [--limit N] [--resume] [--stats]

# 3. Multi-format analytical export
python cli.py topic export --topic <topic_id> --format parquet
python cli.py topic export --topic <topic_id> --format duckdb

# 4. NusaBERT DAPT MLM corpus generation
python cli.py topic dapt --topic <topic_id> --min-words 50
```

## Directory Responsibilities
- `config/topics/`: Declarative YAML topic definitions.
- `config/portal_fingerprints.json`: Sitemaps and WP-REST API endpoints for 66 Indonesian outlets.
- `processors/`:
  - `topic_config.py`: Pydantic schema validation.
  - `portal_catalog.py`: Preset auto-routing (`national_wire`, `regional_epicenters`, `investigative_environment`).
  - `crawl_frontier.py`: Resumable SQLite WAL queue with in-memory batch commit buffer.
  - `cleaner.py`: Ad filtering and text sanitization.
  - `dapt.py`: NLP corpus formatting.
- `backends/curl_impersonate.py`: Chrome 124 TLS JA4 impersonation with HTTP/2 keep-alive and `AsyncSession`.
- `channels/portal_news.py`: Multi-page normalization and Inertia.js fast unpacking.
- `scripts/topic_harvester.py`: Central 2-core harvesting orchestrator.

# Horus News Harvester - Developer & Agent Guide

## Core Architecture
Horus News Harvester (formerly NNIH) is a topic-agnostic, declarative news intelligence crawler for 66 fingerprinted Indonesian national and regional media portals.

### Golden Rules for Agents:
1. **Never use or resurrect legacy scripts**:
   - `scripts/local_harvester.py` (DELETED)
   - `scripts/scheduler.py` (DELETED)
   - `scripts/prepare_dapt_corpus.py` (DELETED)
   - `scripts/analyze_dewa_sites.py` (DELETED)
2. **Always use declarative topic engine (`horus` / `nnih` or `python cli.py topic ...`)**:
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

# 4. Social harvesting (YouTube videos + comment threads, Twitter/X tweets)
python cli.py topic social --topic <topic_id> [--platform youtube|twitter|both] [--limit N] [--resume]

# 5. NusaBERT DAPT MLM corpus generation
python cli.py topic dapt --topic <topic_id> --min-words 50
```

`topic social` reads the `social:` block in the topic YAML. YouTube harvests every discovered
video plus its full reply threads; Twitter harvests keyword queries and handle timelines through
the GraphQL backend. Both write JSONL to `data/raw/{youtube,twitter}/<topic_id>.jsonl` and
deduplicate through `data/state/social_<topic>_<platform>_frontier.db`. The command prints a
per-target coverage report (discovered, new, failed) so silent zero-yield runs are visible.

## Directory Responsibilities
- `config/topics/`: Declarative YAML topic definitions.
- `config/portal_fingerprints.json`: Sitemaps and WP-REST API endpoints for 66 Indonesian outlets.
- `processors/`:
  - `topic_config.py`: Pydantic schema validation.
  - `portal_catalog.py`: Preset auto-routing (`national_wire`, `regional_epicenters`, `investigative_environment`).
  - `crawl_frontier.py`: Resumable SQLite WAL queue with in-memory batch commit buffer.
  - `cleaner.py`: Ad filtering and text sanitization.
  - `dapt.py`: NLP corpus formatting.
  - `metrics.py`: `parse_count` for Indonesian/English abbreviated counts ("1,2 rb", "1.2K").
  - `youtube_normalizer.py` / `tweet_normalizer.py`: single mapping per platform to the harvest record.
- `backends/curl_impersonate.py`: Chrome 124 TLS JA4 impersonation with HTTP/2 keep-alive and `AsyncSession`.
- `channels/portal_news.py`: Multi-page normalization and Inertia.js fast unpacking.
- `scripts/topic_harvester.py`: Central 2-core harvesting orchestrator.

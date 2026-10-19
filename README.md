<div align="center">
  <h1>ind-sports-scrapper</h1>
  <p><strong>Multi-Channel Sports Harvester and Corpus Pipeline for Indonesian Sports</strong></p>

  <p align="center">
    <img src="https://img.shields.io/badge/Language-Python_3.11-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/Execution-Local_Workstation-blue?style=flat-square" alt="Local">
    <img src="https://img.shields.io/badge/Channels-Portals_YouTube_Twitter-009688?style=flat-square" alt="Channels">
    <img src="https://img.shields.io/badge/Persistence-SQLite_State-4479A1?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite">
    <img src="https://img.shields.io/badge/Status-Active-success?style=flat-square" alt="Status">
  </p>

  <p align="center">
    Decoupled local ingestion engine harvesting Indonesian sports news portals and social discussions with ordered multi-backend fallback, supplying line-delimited JSONL feeds to downstream GPU NLP and graph modeling pipelines.
  </p>
</div>

## Tech Stack

![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
![Git](https://img.shields.io/badge/git-%23F05033.svg?style=for-the-badge&logo=git&logoColor=white)
![YAML](https://img.shields.io/badge/yaml-%23ffffff.svg?style=for-the-badge&logo=yaml&logoColor=black)

## Architecture and Lineage

The data pipeline decouples I/O-intensive web harvesting from compute-intensive transformer inference:

1. Local Ingestion: Workstation execution (`scripts/local_harvester.py`) queries national wire feeds, regional portals, Twitter/X timelines, and video discussion streams without consuming cloud GPU quotas.
2. Ordered Multi-Backend Fallback: Channels cycle across backends:
   - News portals: `rss_parser` -> `jina_reader` -> `direct_http`.
   - YouTube discussions: `innertube_api` -> `yt_dlp` -> `jina_reader`.
   - Twitter/X timelines: `syndication_api` -> `jina_reader` -> `twitter_cli`.
3. Persistent State and Cursors: Tracks harvested URLs, video IDs, and tweet IDs in SQLite (`data/state/harvest_state.db`). Guarantees incremental, delta-only runs without duplicate scraping or memory bloat.
4. Interval Daemon Scheduler: Automated background daemon (`scripts/scheduler.py`) periodically runs delta-harvest cycles (News 30m, Twitter 60m, YouTube 180m) with graceful signal handling.
5. Anti-Bot Guard: Inspects the initial 4 KB of HTTP payloads for Cloudflare and CAPTCHA challenge markers (`is_antibot_page`).
6. Unified Ingestion Schema: Emits standardized line-delimited JSONL files partitioned by channel and sport at `data/raw/<channel>/<sport>.jsonl`.
7. Downstream Analytics: Output feeds mount directly into Kaggle GPU environments for NusaBERT NER (19-class NERGrit), Hawkes point process diffusion modeling, and signed social network analysis (Horus v2).

## Media Portal Catalog (66 Portals)

The repository catalogs all 66 Indonesian media portals identified from the empirical 29,069-article corpus (`config/portals_catalog.json`):

* Tier 1 Primary Wire and National Sports: Antara News (primary wire originator), Detikcom (dominant volume hub), Kompas.com, CNN Indonesia, Tempo, Republika, Jawa Pos, Tribunnews, tvOne News, Liputan6, SINDOnews, Suara.com.
* Tier 2 Regional Sports and Aggregators: Harian Jogja (PSS Sleman local anchor), BeritaJatim (East Java hub), Bali Post, NusaBali, Pikiran Rakyat, Poskota, Suara Merdeka, BantenNews, Fajar Makassar.
* Tier 3 Specialized and Economic Desks: Bisnis Indonesia, Kontan, Katadata, CNBC Indonesia, IDX Channel, Tirto.id, MerahPutih.com, The Jakarta Post.

## Supported Sports Targets, Communities, and Influencers

* Football: BRI Liga 1, Timnas Indonesia, PSSI. Influencers & Communities: Coach Justin, Bung Binder, Bung Towel, Bung Ropan, Valentino Simanjuntak, idextratime, FaktaSepakbola, Box2Box Bola, MafiaWasit.
* Basketball: Indonesian Basketball League (IBL), Timnas Basket Indonesia, Perbasi. Influencers & Communities: Augie Fantinus, Denny Sumargo, Mainbasket.
* Badminton: PBSI, BWF World Tour, Indonesia Open. Influencers & Communities: Yuni Kartika, Oma Gill, BadmintonTalk, Djarum Badminton.
* Volleyball: Proliga, Timnas Voli Indonesia, PBVSI. Influencers & Communities: Rivan Nurmulki, Wilda Nurfadhilah, VoliIndo.

## Directory Structure

```
ind-sports-scrapper/
├── backends/           Gateway backends (rss_parser, jina_reader)
├── channels/           Channel definitions (base, portal_news, social_youtube, social_twitter)
├── config/             Catalog (portals_catalog.json) and targets (sports_targets.yaml)
├── data/
│   ├── raw/            Partitioned raw JSONL outputs (news, youtube, twitter)
│   └── state/          Persistent SQLite state database (harvest_state.db)
├── processors/         Text cleaning, deduplication, and SQLite state store
├── scripts/            Local harvester, daemon scheduler, and catalog analyzer
├── tests/              Unit tests for channels, state store, and fallback routing
├── cli.py              Command line interface for search and extraction
└── doctor.py           Zero-side-effect environment diagnostic tool
```

## CLI Usage

Run environment health check:

```bash
python cli.py doctor
```

Search articles across channels:

```bash
python cli.py search --query "Dewa United" --sport football --limit 5
```

Execute local batch harvesting across sports targets:

```bash
python scripts/local_harvester.py
```

Run background daemon scheduler:

```bash
python scripts/scheduler.py
```

Run single one-off cycle across all channels:

```bash
python scripts/scheduler.py --once
```

## License

This project is licensed under the MIT License.

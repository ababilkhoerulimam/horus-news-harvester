"""
Local Sports Harvester Runner
Executes bulk sports harvesting locally across portals, YouTube, and Twitter/X targets.
Integrates StateStore for persistent delta-only harvesting.
"""

import os
import sys
import yaml
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Any, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import get_channel
from processors.cleaner import clean_news_text, deduplicate_articles, deduplicate_jsonl_file
from processors.state_store import StateStore


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = str(REPO_ROOT / "config" / "sports_targets.yaml")
DEFAULT_RAW_DIR = str(REPO_ROOT / "data" / "raw")
DEFAULT_STATE_DB = str(REPO_ROOT / "data" / "state" / "harvest_state.db")


def _load_existing_ids(file_path: Path, id_key: str = "url") -> Set[str]:
    """Reads existing item IDs from a JSONL file to prevent duplicate disk appends."""
    if not file_path.exists() or not file_path.is_file():
        return set()
    existing: Set[str] = set()
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                val = (
                    obj.get(id_key)
                    or obj.get("url")
                    or obj.get("tweet_id")
                    or obj.get("video_id")
                    or obj.get("id")
                )
                if val:
                    existing.add(str(val))
            except Exception:
                continue
    return existing


def run_local_harvest(
    config_path: str = DEFAULT_CONFIG,
    base_output_dir: str = DEFAULT_RAW_DIR,
    items_per_keyword: int = 5,
    harvest_news: bool = True,
    harvest_youtube: bool = True,
    harvest_twitter: bool = True,
    state_db_path: str = DEFAULT_STATE_DB,
    incremental: bool = True,
    max_workers: int = 5
) -> Dict[str, Any]:
    resolved_config = Path(config_path)
    if not resolved_config.is_absolute():
        resolved_config = REPO_ROOT / config_path

    if not resolved_config.exists():
        print(f"[!] Error: Config '{resolved_config}' not found.")
        return {"status": "error", "message": "Config not found"}

    with open(resolved_config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    portal_ch = get_channel("portal_news") if harvest_news else None
    youtube_ch = get_channel("social_youtube") if harvest_youtube else None
    twitter_ch = get_channel("social_twitter") if harvest_twitter else None

    state = StateStore(state_db_path) if incremental else None

    base_folder = Path(base_output_dir)
    news_folder = base_folder / "news"
    youtube_folder = base_folder / "youtube"
    twitter_folder = base_folder / "twitter"

    if portal_ch:
        news_folder.mkdir(parents=True, exist_ok=True)
    if youtube_ch:
        youtube_folder.mkdir(parents=True, exist_ok=True)
    if twitter_ch:
        twitter_folder.mkdir(parents=True, exist_ok=True)

    summary_stats = {"news": 0, "youtube": 0, "twitter": 0}
    sports = cfg.get("sports", {})

    for sport_id, sport_data in sports.items():
        sport_name = sport_data.get("name", sport_id)
        print(f"\n[*] Harvesting {sport_name} ({sport_id})...", flush=True)

        keywords = []
        for league in sport_data.get("leagues", []):
            keywords.extend(league.get("keywords", []))
        keywords.extend(sport_data.get("top_clubs", []))
        keywords.extend(sport_data.get("focal_athletes", []))

        # Include sport-specific influencers & fan communities
        influencers = cfg.get("social_channels", {}).get("influencers", {}).get(sport_id, [])
        for inf in influencers:
            if inf.get("name"):
                keywords.append(inf.get("name"))

        communities = cfg.get("social_channels", {}).get("communities", {}).get(sport_id, [])
        for comm in communities:
            if comm.get("name"):
                keywords.append(comm.get("name"))

        keywords = [k for k in dict.fromkeys(keywords) if k]

        # 1. Harvest Portal News
        if portal_ch:
            raw_candidates = []
            # Query top keyword targets to discover relevant articles
            for kw in keywords[:8]:
                items = portal_ch.search(query=kw, sport=sport_id, limit=items_per_keyword)
                raw_candidates.extend(items)

            deduped_candidates = deduplicate_articles(raw_candidates)
            unseen_news = state.filter_unseen("portal_news", deduped_candidates, id_key="url") if state else deduped_candidates

            # Bound content extraction per sport per cycle to avoid HTTP timeouts
            max_news_extract = min(len(unseen_news), items_per_keyword * 2)
            candidates_to_extract = unseen_news[:max_news_extract]

            def _extract_single_news(it):
                url = it.get("url")
                if url:
                    try:
                        extracted = portal_ch.extract(url)
                        if extracted.get("status") == "success":
                            it["content"] = extracted.get("content", "")
                            it["extracted_via"] = extracted.get("backend")
                        else:
                            it["content"] = it.get("summary", "")
                            it["extracted_via"] = "rss_summary"
                    except Exception:
                        it["content"] = it.get("summary", "")
                        it["extracted_via"] = "rss_summary"
                return it

            collected_news = []
            if candidates_to_extract:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    collected_news = list(executor.map(_extract_single_news, candidates_to_extract))

            news_file = news_folder / f"{sport_id}.jsonl"
            existing_news_ids = _load_existing_ids(news_file, id_key="url")
            new_articles = [art for art in collected_news if str(art.get("url", "")) not in existing_news_ids]

            if new_articles:
                with open(news_file, "a", encoding="utf-8") as f:
                    for art in new_articles:
                        f.write(json.dumps(art, ensure_ascii=False) + "\n")
                if state:
                    state.mark_seen("portal_news", sport_id, new_articles, id_key="url")
                summary_stats["news"] += len(new_articles)
                print(f"[+] [News] Saved {len(new_articles)} new articles to {news_file}", flush=True)
            else:
                print(f"[=] [News] No new articles (all seen or empty).", flush=True)

        # 2. Harvest YouTube Discussions
        if youtube_ch:
            yt_candidates = []
            for kw in keywords[:4]:
                vids = youtube_ch.search(query=f"{kw} highlights", sport=sport_id, limit=2)
                yt_candidates.extend(vids)

            unseen_yt = state.filter_unseen("social_youtube", yt_candidates, id_key="url") if state else yt_candidates

            def _extract_single_yt(v):
                v_url = v.get("url")
                if v_url:
                    try:
                        v_details = youtube_ch.extract(v_url)
                        if v_details.get("status") == "success":
                            v["comments"] = v_details.get("comments", [])
                            v["comment_count"] = v_details.get("comment_count", 0)
                            v["description"] = v_details.get("content", "")
                    except Exception:
                        pass
                return v

            collected_yt = []
            if unseen_yt:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    collected_yt = list(executor.map(_extract_single_yt, unseen_yt))

            yt_file = youtube_folder / f"{sport_id}.jsonl"
            existing_yt_ids = _load_existing_ids(yt_file, id_key="url")
            new_yt = [y for y in collected_yt if str(y.get("url", "")) not in existing_yt_ids]

            if new_yt:
                with open(yt_file, "a", encoding="utf-8") as f:
                    for yitem in new_yt:
                        f.write(json.dumps(yitem, ensure_ascii=False) + "\n")
                if state:
                    state.mark_seen("social_youtube", sport_id, new_yt, id_key="url")
                summary_stats["youtube"] += len(new_yt)
                print(f"[+] [YouTube] Saved {len(new_yt)} new video discussions to {yt_file}", flush=True)
            else:
                print(f"[=] [YouTube] No new video discussions.", flush=True)

        # 3. Harvest Twitter/X Disucssions
        if twitter_ch:
            twitter_targets = []
            all_tx = cfg.get("social_channels", {}).get("twitter_x", {})
            for t_id, t_info in all_tx.items():
                if t_info.get("sport") == sport_id:
                    twitter_targets.append(t_info.get("username"))

            for inf in influencers:
                if "twitter_x" in inf.get("platform", []):
                    twitter_targets.append(inf.get("handle"))

            for comm in communities:
                if "twitter_x" in comm.get("platform", []):
                    twitter_targets.append(comm.get("handle"))

            twitter_targets = [u for u in dict.fromkeys(twitter_targets) if u]

            def _fetch_user_tweets(user):
                try:
                    return twitter_ch.get_user_tweets(user, sport=sport_id, limit=items_per_keyword)
                except Exception:
                    return []

            collected_tweets = []
            targets_to_fetch = twitter_targets[:5]
            if targets_to_fetch:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    results = executor.map(_fetch_user_tweets, targets_to_fetch)
                    for user_tweets in results:
                        unseen_tweets = state.filter_unseen("social_twitter", user_tweets, id_key="tweet_id") if state else user_tweets
                        collected_tweets.extend(unseen_tweets)

            tx_file = twitter_folder / f"{sport_id}.jsonl"
            existing_tx_ids = _load_existing_ids(tx_file, id_key="tweet_id")
            new_tweets = [tw for tw in collected_tweets if str(tw.get("tweet_id", "")) not in existing_tx_ids]

            if new_tweets:
                with open(tx_file, "a", encoding="utf-8") as f:
                    for tw in new_tweets:
                        f.write(json.dumps(tw, ensure_ascii=False) + "\n")
                if state:
                    state.mark_seen("social_twitter", sport_id, new_tweets, id_key="tweet_id")
                summary_stats["twitter"] += len(new_tweets)
                print(f"[+] [Twitter] Saved {len(new_tweets)} new tweets to {tx_file}", flush=True)
            else:
                print(f"[=] [Twitter] No new tweets.", flush=True)

    if state:
        st = state.get_stats()
        print(f"\n[+] Total Persistent State Items: {st['total_items']} ({st['by_channel']})", flush=True)

    return {"status": "ok", "stats": summary_stats}


if __name__ == "__main__":
    run_local_harvest()


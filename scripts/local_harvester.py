"""
Local Sports Harvester Runner
Executes bulk sports harvesting locally across portals, YouTube, and Twitter/X targets.
Integrates StateStore for persistent delta-only harvesting.
"""

import os
import sys
import yaml
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import get_channel
from processors.cleaner import clean_news_text, deduplicate_articles
from processors.state_store import StateStore


def run_local_harvest(
    config_path: str = "config/sports_targets.yaml",
    base_output_dir: str = "data/raw",
    items_per_keyword: int = 5,
    harvest_news: bool = True,
    harvest_youtube: bool = True,
    harvest_twitter: bool = True,
    state_db_path: str = "data/state/harvest_state.db",
    incremental: bool = True
) -> Dict[str, Any]:
    if not os.path.exists(config_path):
        print(f"[!] Error: Config '{config_path}' not found.")
        return {"status": "error", "message": "Config not found"}

    with open(config_path, "r", encoding="utf-8") as f:
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
        print(f"\n[*] Harvesting {sport_name} ({sport_id})...")

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
            for kw in keywords:
                items = portal_ch.search(query=kw, sport=sport_id, limit=items_per_keyword)
                raw_candidates.extend(items)

            deduped_candidates = deduplicate_articles(raw_candidates)
            unseen_news = state.filter_unseen("portal_news", deduped_candidates, id_key="url") if state else deduped_candidates

            collected_news = []
            for it in unseen_news:
                url = it.get("url")
                if url:
                    extracted = portal_ch.extract(url)
                    if extracted.get("status") == "success":
                        it["content"] = extracted.get("content", "")
                        it["extracted_via"] = extracted.get("backend")
                collected_news.append(it)

            if collected_news:
                news_file = news_folder / f"{sport_id}.jsonl"
                with open(news_file, "a", encoding="utf-8") as f:
                    for art in collected_news:
                        f.write(json.dumps(art, ensure_ascii=False) + "\n")
                if state:
                    state.mark_seen("portal_news", sport_id, collected_news, id_key="url")
                summary_stats["news"] += len(collected_news)
                print(f"[+] [News] Saved {len(collected_news)} new articles to {news_file}")
            else:
                print(f"[=] [News] No new articles (all seen or empty).")

        # 2. Harvest YouTube Discussions
        if youtube_ch:
            yt_candidates = []
            for kw in keywords[:4]:
                vids = youtube_ch.search(query=f"{kw} highlights", sport=sport_id, limit=2)
                yt_candidates.extend(vids)

            unseen_yt = state.filter_unseen("social_youtube", yt_candidates, id_key="url") if state else yt_candidates

            collected_yt = []
            for v in unseen_yt:
                v_url = v.get("url")
                if v_url:
                    v_details = youtube_ch.extract(v_url)
                    if v_details.get("status") == "success":
                        v["comments"] = v_details.get("comments", [])
                        v["comment_count"] = v_details.get("comment_count", 0)
                        v["description"] = v_details.get("content", "")
                collected_yt.append(v)

            if collected_yt:
                yt_file = youtube_folder / f"{sport_id}.jsonl"
                with open(yt_file, "a", encoding="utf-8") as f:
                    for yitem in collected_yt:
                        f.write(json.dumps(yitem, ensure_ascii=False) + "\n")
                if state:
                    state.mark_seen("social_youtube", sport_id, collected_yt, id_key="url")
                summary_stats["youtube"] += len(collected_yt)
                print(f"[+] [YouTube] Saved {len(collected_yt)} new video discussions to {yt_file}")
            else:
                print(f"[=] [YouTube] No new video discussions.")

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

            collected_tweets = []
            for user in twitter_targets[:5]:
                user_tweets = twitter_ch.get_user_tweets(user, sport=sport_id, limit=items_per_keyword)
                unseen_tweets = state.filter_unseen("social_twitter", user_tweets, id_key="tweet_id") if state else user_tweets
                collected_tweets.extend(unseen_tweets)

            if collected_tweets:
                tx_file = twitter_folder / f"{sport_id}.jsonl"
                with open(tx_file, "a", encoding="utf-8") as f:
                    for tw in collected_tweets:
                        f.write(json.dumps(tw, ensure_ascii=False) + "\n")
                if state:
                    state.mark_seen("social_twitter", sport_id, collected_tweets, id_key="tweet_id")
                summary_stats["twitter"] += len(collected_tweets)
                print(f"[+] [Twitter] Saved {len(collected_tweets)} new tweets to {tx_file}")
            else:
                print(f"[=] [Twitter] No new tweets.")

    if state:
        st = state.get_stats()
        print(f"\n[+] Total Persistent State Items: {st['total_items']} ({st['by_channel']})")

    return {"status": "ok", "stats": summary_stats}


if __name__ == "__main__":
    run_local_harvest()


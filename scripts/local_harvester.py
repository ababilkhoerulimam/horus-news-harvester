"""
Local Sports Harvester Runner
Executes bulk sports harvesting locally across portals & sports targets.
"""

import os
import sys
import yaml
import json
from pathlib import Path
from typing import Dict, Any, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import get_channel
from processors.cleaner import clean_news_text, deduplicate_articles


def run_local_harvest(
    config_path: str = "config/sports_targets.yaml",
    base_output_dir: str = "data/raw",
    items_per_keyword: int = 5,
    harvest_youtube: bool = True
):
    if not os.path.exists(config_path):
        print(f"[!] Error: Config '{config_path}' not found.")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    portal_ch = get_channel("portal_news")
    youtube_ch = get_channel("social_youtube") if harvest_youtube else None

    if not portal_ch:
        print("[!] Error: Portal news channel not found.")
        return

    base_folder = Path(base_output_dir)
    news_folder = base_folder / "news"
    youtube_folder = base_folder / "youtube"
    news_folder.mkdir(parents=True, exist_ok=True)
    if youtube_ch:
        youtube_folder.mkdir(parents=True, exist_ok=True)

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
        collected_news = []
        for kw in keywords:
            items = portal_ch.search(query=kw, sport=sport_id, limit=items_per_keyword)
            for it in items:
                url = it.get("url")
                if url:
                    extracted = portal_ch.extract(url)
                    if extracted.get("status") == "success":
                        it["content"] = extracted.get("content", "")
                        it["extracted_via"] = extracted.get("backend")
                collected_news.append(it)

        deduped_news = deduplicate_articles(collected_news)
        news_file = news_folder / f"{sport_id}.jsonl"
        with open(news_file, "w", encoding="utf-8") as f:
            for art in deduped_news:
                f.write(json.dumps(art, ensure_ascii=False) + "\n")
        print(f"[+] [News] Saved {len(deduped_news)} articles to {news_file}")

        # 2. Harvest YouTube Discussions (if enabled)
        if youtube_ch:
            collected_yt = []
            for kw in keywords[:4]:  # Top focus keywords for video highlights & discussions
                vids = youtube_ch.search(query=f"{kw} highlights", sport=sport_id, limit=2)
                for v in vids:
                    v_url = v.get("url")
                    if v_url:
                        v_details = youtube_ch.extract(v_url)
                        if v_details.get("status") == "success":
                            v["comments"] = v_details.get("comments", [])
                            v["comment_count"] = v_details.get("comment_count", 0)
                            v["description"] = v_details.get("content", "")
                    collected_yt.append(v)

            yt_file = youtube_folder / f"{sport_id}.jsonl"
            with open(yt_file, "w", encoding="utf-8") as f:
                for yitem in collected_yt:
                    f.write(json.dumps(yitem, ensure_ascii=False) + "\n")
            print(f"[+] [YouTube] Saved {len(collected_yt)} video discussions to {yt_file}")


if __name__ == "__main__":
    run_local_harvest()

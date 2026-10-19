"""
Sports Harvester Daemon & Interval Scheduler.
Runs local workstation harvesting on scheduled intervals with graceful signal handling.
Zero third-party daemon dependencies (pure Python stdlib).
"""

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.local_harvester import run_local_harvest
from processors.state_store import StateStore

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = str(REPO_ROOT / "config" / "sports_targets.yaml")
DEFAULT_STATE_DB = str(REPO_ROOT / "data" / "state" / "harvest_state.db")

RUNNING = True


def _signal_handler(sig, frame):
    global RUNNING
    print(f"\n[!] Signal {sig} received. Terminating harvest loop gracefully...")
    RUNNING = False


class HarvestScheduler:
    """Orchestrates periodic delta-harvesting across news, Twitter, and YouTube channels."""

    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG,
        state_db_path: str = DEFAULT_STATE_DB,
        news_interval_sec: int = 1800,
        twitter_interval_sec: int = 3600,
        youtube_interval_sec: int = 10800,
        items_per_keyword: int = 5
    ):
        self.config_path = config_path
        self.state_db_path = state_db_path
        self.news_interval_sec = news_interval_sec
        self.twitter_interval_sec = twitter_interval_sec
        self.youtube_interval_sec = youtube_interval_sec
        self.items_per_keyword = items_per_keyword

        self.last_runs = {
            "news": 0.0,
            "twitter": 0.0,
            "youtube": 0.0,
        }
        self.state = StateStore(state_db_path)

    def run_cycle(self) -> Dict[str, Any]:
        now = time.time()
        due_news = (now - self.last_runs["news"]) >= self.news_interval_sec
        due_twitter = (now - self.last_runs["twitter"]) >= self.twitter_interval_sec
        due_youtube = (now - self.last_runs["youtube"]) >= self.youtube_interval_sec

        if not (due_news or due_twitter or due_youtube):
            return {"status": "idle", "due": False}

        utc_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n[+] [{utc_time}] Triggering harvest round (News: {due_news}, Twitter: {due_twitter}, YouTube: {due_youtube})")

        res = run_local_harvest(
            config_path=self.config_path,
            harvest_news=due_news,
            harvest_youtube=due_youtube,
            harvest_twitter=due_twitter,
            state_db_path=self.state_db_path,
            items_per_keyword=self.items_per_keyword,
            incremental=True
        )

        if due_news:
            self.last_runs["news"] = now
        if due_twitter:
            self.last_runs["twitter"] = now
        if due_youtube:
            self.last_runs["youtube"] = now

        return {"status": "success", "due": True, "result": res}

    def start(self, poll_interval_sec: int = 30) -> None:
        print("[*] Indonesian Sports Harvester Scheduler started.")
        print(f"[*] Intervals: News: {self.news_interval_sec}s, Twitter: {self.twitter_interval_sec}s, YouTube: {self.youtube_interval_sec}s")
        print("[*] Press Ctrl+C to stop.")

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        while RUNNING:
            try:
                self.run_cycle()
            except Exception as e:
                print(f"[!] Cycle error: {e}")

            # Sleep in 1-second chunks for instant response to signals
            for _ in range(poll_interval_sec):
                if not RUNNING:
                    break
                time.sleep(1)

        st = self.state.get_stats()
        print(f"\n[+] Harvester stopped. Final database stats: {st['total_items']} items in {st['db_path']}.")


def main():
    parser = argparse.ArgumentParser(description="Indonesian Sports Harvester Interval Scheduler")
    parser.add_argument("--once", action="store_true", help="Run a single harvest cycle for all channels and exit")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to sports targets yaml")
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB, help="Path to SQLite state db")
    parser.add_argument("--news-interval", type=int, default=1800, help="News check interval in seconds")
    parser.add_argument("--twitter-interval", type=int, default=3600, help="Twitter check interval in seconds")
    parser.add_argument("--youtube-interval", type=int, default=10800, help="YouTube check interval in seconds")
    args = parser.parse_args()

    scheduler = HarvestScheduler(
        config_path=args.config,
        state_db_path=args.state_db,
        news_interval_sec=args.news_interval,
        twitter_interval_sec=args.twitter_interval,
        youtube_interval_sec=args.youtube_interval
    )

    if args.once:
        print("[*] Executing one-off cycle across all channels...")
        res = run_local_harvest(
            config_path=args.config,
            harvest_news=True,
            harvest_youtube=True,
            harvest_twitter=True,
            state_db_path=args.state_db,
            incremental=True
        )
        print(f"[+] Done: {res}")
        return

    scheduler.start()


if __name__ == "__main__":
    main()

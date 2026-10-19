"""
Twitter/X Sports Channel & Timeline Harvester.
Implements multi-backend fallback: Syndication API -> Jina Reader -> twitter-cli.
Extracts public timeline tweets, reactions, and replies without API keys.
"""

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .base import SportsChannel
from backends.jina_reader import JinaReaderBackend
from processors.cleaner import clean_news_text

SYNDICATION_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
WEB_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class TwitterChannel(SportsChannel):
    """Channel handling Indonesian sports Twitter/X discussions, pundits, and communities."""

    name = "social_twitter"
    description = "Twitter/X Diskusi Komunitas dan Influencer Olahraga Indonesia"
    backends = ["syndication_api", "jina_reader", "twitter_cli"]
    tier = 0

    def __init__(self):
        self.jina = JinaReaderBackend()

    def can_handle(self, url_or_source: str) -> bool:
        url_lower = url_or_source.lower()
        return any(x in url_lower for x in ("x.com", "twitter.com", "@"))

    def check(self, config: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        cli_present = shutil.which("twitter-cli") is not None or shutil.which("bird") is not None
        self.active_backend = "syndication_api"
        status_msg = f"Active: syndication_api, twitter_cli: {'available' if cli_present else 'not found'}"
        return "ok", status_msg

    def _extract_username(self, identifier: str) -> str:
        ident = identifier.strip()
        if "twitter.com/" in ident or "x.com/" in ident:
            parsed = urllib.parse.urlparse(ident)
            path_parts = [p for p in parsed.path.split("/") if p]
            return path_parts[0] if path_parts else ident
        return ident.lstrip("@")

    def _fetch_syndication(self, username: str, limit: int = 20) -> List[Dict[str, Any]]:
        # ponytail: Twitter syndication endpoint parses __NEXT_DATA__; upgrade to twikit or guest token if CDN alters SSR shape.
        url = SYNDICATION_URL.format(username=username)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": WEB_USER_AGENT, "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return []

        match = re.search(r'<script\s+id="__NEXT_DATA__"\s+type="application/json">([^<]+)</script>', html)
        if not match:
            return []

        try:
            data = json.loads(match.group(1))
        except Exception:
            return []

        timeline = data.get("props", {}).get("pageProps", {}).get("timeline", {})
        entries = timeline.get("entries", [])
        tweets = []

        for entry in entries:
            content = entry.get("content", {})
            tweet_data = content.get("tweet")
            if not tweet_data and "itemContent" in content:
                tweet_data = content.get("itemContent", {}).get("tweet_results", {}).get("result")

            if not tweet_data:
                continue

            legacy = tweet_data.get("legacy", tweet_data)
            tweet_id = tweet_data.get("id_str") or legacy.get("id_str") or tweet_data.get("rest_id", "")
            raw_text = legacy.get("full_text") or legacy.get("text") or ""
            cleaned_text = clean_news_text(raw_text)

            user_info = tweet_data.get("user", {})
            author_name = user_info.get("name", username)
            screen_name = user_info.get("screen_name", username)

            tweets.append({
                "tweet_id": str(tweet_id),
                "title": f"Tweet by @{screen_name}",
                "author": author_name,
                "username": screen_name,
                "text": cleaned_text,
                "url": f"https://x.com/{screen_name}/status/{tweet_id}" if tweet_id else f"https://x.com/{screen_name}",
                "published_date": legacy.get("created_at", ""),
                "metrics": {
                    "likes": legacy.get("favorite_count", 0),
                    "retweets": legacy.get("retweet_count", 0),
                    "replies": legacy.get("reply_count", 0),
                },
                "backend": "syndication_api"
            })
            if len(tweets) >= limit:
                break

        return tweets

    def _fetch_jina(self, username: str, limit: int = 10) -> List[Dict[str, Any]]:
        target_url = f"https://x.com/{username}"
        extracted = self.jina.extract(target_url)
        if extracted.get("status") != "success":
            return []

        content = extracted.get("content", "")
        if not content:
            return []

        blocks = [b.strip() for b in re.split(r"\n\s*---\s*\n|\n\s*\*\*\*\s*\n|\n(?=@[\w_]+)", content) if b.strip()]
        tweets = []
        for idx, block in enumerate(blocks[:limit]):
            clean_body = clean_news_text(block)
            tweets.append({
                "tweet_id": f"jina_{username}_{idx}",
                "title": f"Post by @{username}",
                "author": username,
                "username": username,
                "text": clean_body,
                "url": f"https://x.com/{username}",
                "published_date": "",
                "metrics": {"likes": 0, "retweets": 0, "replies": 0},
                "backend": "jina_reader"
            })
        return tweets

    def _fetch_cli(self, username: str, limit: int = 10) -> List[Dict[str, Any]]:
        cli_bin = shutil.which("twitter-cli") or shutil.which("bird")
        if not cli_bin:
            return []

        cmd = [cli_bin, "timeline", username, "--limit", str(limit), "--json"]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            if proc.returncode != 0:
                return []
            data = json.loads(proc.stdout)
            tweets = []
            for item in data if isinstance(data, list) else data.get("tweets", []):
                tweets.append({
                    "tweet_id": str(item.get("id", "")),
                    "title": f"Tweet by @{username}",
                    "author": item.get("author", username),
                    "username": username,
                    "text": clean_news_text(item.get("text", "")),
                    "url": item.get("url", f"https://x.com/{username}"),
                    "published_date": item.get("created_at", ""),
                    "metrics": {
                        "likes": item.get("likes", 0),
                        "retweets": item.get("retweets", 0),
                        "replies": item.get("replies", 0),
                    },
                    "backend": "twitter_cli"
                })
            return tweets
        except Exception:
            return []

    def get_user_tweets(
        self,
        username: str,
        sport: str = "football",
        limit: int = 20,
        config: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        clean_user = self._extract_username(username)
        candidates = self.ordered_backends(config)

        for backend in candidates:
            tweets = []
            if backend == "syndication_api":
                tweets = self._fetch_syndication(clean_user, limit=limit)
            elif backend == "jina_reader":
                tweets = self._fetch_jina(clean_user, limit=limit)
            elif backend == "twitter_cli":
                tweets = self._fetch_cli(clean_user, limit=limit)

            if tweets:
                for t in tweets:
                    t["sport"] = sport
                return tweets

        return []

    def search(
        self,
        query: str,
        sport: str = "football",
        limit: int = 10,
        config: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        if query.startswith("@") or "x.com" in query or "twitter.com" in query:
            return self.get_user_tweets(query, sport=sport, limit=limit, config=config)

        search_url = f"https://x.com/search?q={urllib.parse.quote(query)}"
        res = self.jina.extract(search_url)
        if res.get("status") != "success":
            return []

        text = res.get("content", "")
        chunks = [c.strip() for c in text.split("\n\n") if len(c.strip()) > 30]
        items = []
        for idx, chunk in enumerate(chunks[:limit]):
            items.append({
                "tweet_id": f"search_{idx}",
                "title": f"Twitter search: {query}",
                "author": "Twitter Search",
                "username": "search",
                "text": clean_news_text(chunk),
                "url": search_url,
                "published_date": "",
                "sport": sport,
                "backend": "jina_reader"
            })
        return items

    def extract(self, url: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        match = re.search(r"(?:twitter\.com|x\.com)\/([^\/]+)(?:\/status\/(\d+))?", url)
        if match:
            username = match.group(1)
            tweet_id = match.group(2)
            tweets = self.get_user_tweets(username, limit=10, config=config)
            if tweet_id:
                for tw in tweets:
                    if tw.get("tweet_id") == tweet_id:
                        return {"status": "success", "tweet": tw, "backend": tw.get("backend")}
            if tweets:
                return {"status": "success", "tweet": tweets[0], "backend": tweets[0].get("backend")}

        return {"status": "error", "message": "Failed to extract tweet from URL", "backend": None}

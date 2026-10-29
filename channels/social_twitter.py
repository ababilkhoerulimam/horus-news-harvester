"""
Twitter/X Sports Channel & Timeline Harvester.
Implements multi-backend fallback: Syndication API -> Jina Reader -> twitter-cli.
Extracts public timeline tweets, reactions, and replies without API keys.
"""

import json
import logging
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .base import SportsChannel
from processors.tweet_normalizer import normalize_tweet
from backends.firefox_cookies import load_twitter_session, extract_firefox_twitter_tokens, save_twitter_session
from backends.twitter_graphql import TwitterGraphQLBackend

logger = logging.getLogger("social_twitter")

SYNDICATION_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
TWEET_RESULT_URL = "https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=id&token=a"
WEB_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class TwitterChannel(SportsChannel):
    """Channel handling Indonesian sports Twitter/X discussions, pundits, and communities."""

    name = "social_twitter"
    description = "Twitter/X Diskusi Komunitas dan Influencer Olahraga Indonesia"
    backends = ["graphql_api", "syndication_api", "twitter_cli"]
    tier = 0

    def __init__(self):
        self.graphql_client = None
        self._init_session()

    def _init_session(self):
        # 1. Try saved session file
        session = load_twitter_session()
        if session:
            auth_token = session.get("auth_token")
            ct0 = session.get("ct0")
            if auth_token and ct0:
                self.graphql_client = TwitterGraphQLBackend(auth_token, ct0)
                if not self.graphql_client.validate_session():
                    self.ensure_active_session(force_refresh=True)
                return

        # 2. Fallback to extracting from Firefox directly
        self.ensure_active_session(force_refresh=True)

    def ensure_active_session(self, force_refresh: bool = False) -> bool:
        """
        Validates current GraphQL session, auto-refreshing from Firefox if expired or forced.
        Returns True if authenticated session is live, False otherwise.
        """
        # ponytail: Auto-recovers Twitter cookies from Firefox profile without user intervention; falls back to syndication on failure.
        if self.graphql_client and not force_refresh:
            if self.graphql_client.validate_session():
                return True

        extracted = extract_firefox_twitter_tokens()
        if extracted:
            auth_token = extracted.get("auth_token")
            ct0 = extracted.get("ct0")
            if auth_token and ct0:
                client = TwitterGraphQLBackend(auth_token, ct0)
                if client.validate_session():
                    save_twitter_session(extracted)
                    self.graphql_client = client
                    return True

        self.graphql_client = None
        return False

    def can_handle(self, url_or_source: str) -> bool:
        url_lower = url_or_source.lower()
        return any(x in url_lower for x in ("x.com", "twitter.com", "@"))

    def check(self, config: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        cli_present = shutil.which("twitter-cli") is not None or shutil.which("bird") is not None
        if self.ensure_active_session():
            self.active_backend = "graphql_api"
            return "ok", "Active: graphql_api (Firefox session authenticated & validated)"
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
        except Exception as exc:
            logger.warning("Syndication fetch failed for %s: %s", username, exc)
            return []

        match = re.search(r'<script\s+id="__NEXT_DATA__"\s+type="application/json">([^<]+)</script>', html)
        if not match:
            return []

        try:
            data = json.loads(match.group(1))
        except Exception as exc:
            logger.warning("Syndication payload for %s is not JSON: %s", username, exc)
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

            record = normalize_tweet(tweet_data, "syndication_api", fallback_username=username)
            if record is None:
                continue
            tweets.append(record)
            if len(tweets) >= limit:
                break

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
                payload = {
                    "id_str": str(item.get("id", "")),
                    "text": item.get("text", ""),
                    "created_at": item.get("created_at", ""),
                    "favorite_count": item.get("likes", 0),
                    "retweet_count": item.get("retweets", 0),
                    "reply_count": item.get("replies", 0),
                    "user": {
                        "screen_name": item.get("username", username),
                        "name": item.get("author", username),
                    },
                }
                record = normalize_tweet(payload, "twitter_cli")
                if record is not None:
                    tweets.append(record)
            return tweets
        except Exception as exc:
            logger.warning("CLI tweet parse failed: %s", exc)
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
            if backend == "graphql_api":
                if self.ensure_active_session():
                    tweets = self.graphql_client.get_user_tweets(clean_user, limit=limit)
                    if not tweets:
                        tweets = self.graphql_client.search_tweets(f"from:{clean_user}", limit=limit)
                    # If empty due to mid-run token expiration, attempt single force recovery
                    if not tweets and self.ensure_active_session(force_refresh=True):
                        tweets = self.graphql_client.get_user_tweets(clean_user, limit=limit)
                        if not tweets:
                            tweets = self.graphql_client.search_tweets(f"from:{clean_user}", limit=limit)
            elif backend == "syndication_api":
                tweets = self._fetch_syndication(clean_user, limit=limit)
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

        candidates = self.ordered_backends(config)
        for backend in candidates:
            if backend == "graphql_api":
                if self.ensure_active_session():
                    items = self.graphql_client.search_tweets(query, limit=limit)
                    if not items and self.ensure_active_session(force_refresh=True):
                        items = self.graphql_client.search_tweets(query, limit=limit)
                    if items:
                        for it in items:
                            it["sport"] = sport
                        return items

        logger.warning("[twitter] search backend unavailable for %r", query)
        return []

    def _fetch_tweet_by_id(self, tweet_id: str, sport: str = "football") -> Optional[Dict[str, Any]]:
        """Fetches a single tweet by id through the public syndication endpoint."""
        url = TWEET_RESULT_URL.format(tweet_id=tweet_id)
        req = urllib.request.Request(url, headers={"User-Agent": WEB_USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.warning("Tweet %s lookup failed: %s", tweet_id, exc)
            return None

        record = normalize_tweet(payload, "syndication_api")
        if record is None:
            return None
        record["sport"] = sport
        return record

    def extract(self, url: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        match = re.search(r"(?:twitter\.com|x\.com)\/([^\/]+)(?:\/status\/(\d+))?", url)
        if match:
            username = match.group(1)
            tweet_id = match.group(2)
            sport = (config or {}).get("sport", "football")
            if tweet_id:
                direct = self._fetch_tweet_by_id(tweet_id, sport=sport)
                if direct is not None:
                    return {"status": "success", "tweet": direct, "backend": direct.get("backend")}

            tweets = self.get_user_tweets(username, limit=10, config=config)
            if tweet_id:
                for tw in tweets:
                    if tw.get("tweet_id") == tweet_id:
                        return {"status": "success", "tweet": tw, "backend": tw.get("backend")}
            if tweets and not tweet_id:
                return {"status": "success", "tweet": tweets[0], "backend": tweets[0].get("backend")}

        return {"status": "error", "message": "Failed to extract tweet from URL", "backend": None}

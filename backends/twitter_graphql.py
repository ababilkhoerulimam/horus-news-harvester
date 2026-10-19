"""
Twitter Web GraphQL Client.
Executes keyword search, user timelines, and reply threads using browser session tokens.
Bypasses public timeline limits and syndication restrictions.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from processors.cleaner import clean_news_text

# Public Twitter Web client bearer token embedded in main.js
TWITTER_WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

GRAPHQL_SEARCH_ENDPOINT = "https://x.com/i/api/graphql/gkjsKepM6gl_HmFWLbehAg/SearchTimeline"
USER_BY_SCREEN_NAME_ENDPOINT = "https://x.com/i/api/graphql/sLVLhk0bGj3MVFEKTdax1w/UserByScreenName"
USER_TWEETS_ENDPOINT = "https://x.com/i/api/graphql/V3vQt7M18_v6qMeix0iSnQ/UserTweets"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
    "Content-Type": "application/json",
    "x-twitter-active-user": "yes",
    "x-twitter-client-language": "id",
}


class TwitterGraphQLBackend:
    """Authenticated Twitter web scraper using Firefox session tokens."""

    def __init__(self, auth_token: str, ct0: str):
        self.auth_token = auth_token
        self.ct0 = ct0

    def _get_headers(self) -> Dict[str, str]:
        headers = dict(DEFAULT_HEADERS)
        headers["Authorization"] = f"Bearer {TWITTER_WEB_BEARER}"
        headers["x-csrf-token"] = self.ct0
        headers["Cookie"] = f"auth_token={self.auth_token}; ct0={self.ct0};"
        return headers

    def _execute_get(self, url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # ponytail: URL encode GraphQL variables; upgrade to twikit client if query IDs get rotated by X.
        query_str = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v) for k, v in params.items()})
        full_url = f"{url}?{query_str}"
        req = urllib.request.Request(full_url, headers=self._get_headers())

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read().decode("utf-8", errors="replace")
                return json.loads(data)
        except Exception:
            return None

    def search_tweets(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Searches Twitter for keyword queries using SearchTimeline endpoint."""
        variables = {
            "rawQuery": query,
            "count": limit,
            "querySource": "typed_query",
            "product": "Top"
        }
        features = {
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "responsive_web_home_pinned_timelines_enabled": True,
            "responsive_web_enhance_cards_enabled": False
        }

        data = self._execute_get(GRAPHQL_SEARCH_ENDPOINT, {"variables": variables, "features": features})
        if not data:
            return []

        tweets = []
        try:
            instructions = data.get("data", {}).get("search_by_raw_query", {}).get("search_timeline", {}).get("timeline", {}).get("instructions", [])
            for inst in instructions:
                entries = inst.get("entries", [])
                for entry in entries:
                    content = entry.get("content", {})
                    item_content = content.get("itemContent", {})
                    tweet_result = item_content.get("tweet_results", {}).get("result", {})

                    if not tweet_result:
                        continue

                    legacy = tweet_result.get("legacy", tweet_result)
                    tweet_id = tweet_result.get("rest_id") or legacy.get("id_str", "")
                    raw_text = legacy.get("full_text") or legacy.get("text", "")
                    user_core = tweet_result.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {})

                    screen_name = user_core.get("screen_name", "unknown")
                    author_name = user_core.get("name", screen_name)

                    tweets.append({
                        "tweet_id": str(tweet_id),
                        "title": f"Tweet by @{screen_name}",
                        "author": author_name,
                        "username": screen_name,
                        "text": clean_news_text(raw_text),
                        "url": f"https://x.com/{screen_name}/status/{tweet_id}",
                        "published_date": legacy.get("created_at", ""),
                        "metrics": {
                            "likes": legacy.get("favorite_count", 0),
                            "retweets": legacy.get("retweet_count", 0),
                            "replies": legacy.get("reply_count", 0),
                        },
                        "backend": "graphql_api"
                    })
                    if len(tweets) >= limit:
                        break
        except Exception:
            pass

        return tweets

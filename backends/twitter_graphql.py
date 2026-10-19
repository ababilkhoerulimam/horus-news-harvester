"""
Twitter Web GraphQL Client.
Executes keyword search, user timelines, and reply threads using browser session tokens.
Bypasses public timeline limits and syndication restrictions.
"""

import json
import re
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

GRAPHQL_BASE = "https://x.com/i/api/graphql"

DEFAULT_QUERY_IDS = {
    "UserByScreenName": "AMIBMjtxEEATh4z8V9GtRg",
    "UserTweets": "7_KBXBpG0zBPRN6UCIV0-w",
    "SearchTimeline": "irezafrnzav4Ulf1vDkEBg",
}

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
        self.query_ids = dict(DEFAULT_QUERY_IDS)
        self._user_id_cache: Dict[str, str] = {}
        self._discovered = False

    def _get_headers(self) -> Dict[str, str]:
        headers = dict(DEFAULT_HEADERS)
        headers["Authorization"] = f"Bearer {TWITTER_WEB_BEARER}"
        headers["x-csrf-token"] = self.ct0
        headers["Cookie"] = f"auth_token={self.auth_token}; ct0={self.ct0};"
        return headers

    def validate_session(self, timeout: float = 8.0) -> bool:
        """
        Pre-flight check against Twitter account settings endpoint.
        Returns True if session cookie is active, False on 401/403 or network failure.
        """
        # ponytail: Validates auth_token + ct0 against settings endpoint; returns False on expiry to trigger Firefox resync.
        url = "https://x.com/i/api/1.1/account/settings.json"
        req = urllib.request.Request(url, headers=self._get_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    return bool(data.get("screen_name"))
                return False
        except urllib.error.HTTPError:
            return False
        except Exception:
            return False

    def discover_query_ids(self) -> None:
        """Dynamically scrapes live Twitter client JS bundles to extract active GraphQL query IDs."""
        if self._discovered:
            return
        self._discovered = True

        html_headers = {
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
            "Cookie": f"auth_token={self.auth_token}; ct0={self.ct0};"
        }
        try:
            req = urllib.request.Request("https://x.com/timnasindonesia", headers=html_headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            js_urls = re.findall(r'https://abs\.twimg\.com/responsive-web/client-web/[^"\'\s]+\.js', html)
            for js_url in js_urls[:10]:
                try:
                    js_req = urllib.request.Request(js_url, headers={"User-Agent": html_headers["User-Agent"]})
                    with urllib.request.urlopen(js_req, timeout=10) as js_resp:
                        js_text = js_resp.read().decode("utf-8", errors="replace")

                    for qid, op in re.findall(r'queryId:"([a-zA-Z0-9_\-]+)",operationName:"([a-zA-Z0-9_\-]+)"', js_text):
                        if op in self.query_ids:
                            self.query_ids[op] = qid
                    for op, qid in re.findall(r'operationName:"([a-zA-Z0-9_\-]+)",queryId:"([a-zA-Z0-9_\-]+)"', js_text):
                        if op in self.query_ids:
                            self.query_ids[op] = qid
                except Exception:
                    continue
        except Exception:
            pass

    def _execute_get(self, operation: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # ponytail: URL encode GraphQL variables; upgrade to twikit client if query IDs get rotated by X.
        query_id = self.query_ids.get(operation, "")
        url = f"{GRAPHQL_BASE}/{query_id}/{operation}"
        query_str = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v) for k, v in params.items()})
        full_url = f"{url}?{query_str}"
        req = urllib.request.Request(full_url, headers=self._get_headers())

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read().decode("utf-8", errors="replace")
                return json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code == 404 and not self._discovered:
                self.discover_query_ids()
                new_id = self.query_ids.get(operation, "")
                if new_id and new_id != query_id:
                    return self._execute_get(operation, params)
            return None
        except Exception:
            return None

    def get_user_id(self, screen_name: str) -> Optional[str]:
        """Resolves Twitter screen name to rest_id via UserByScreenName."""
        clean_user = screen_name.lstrip("@").strip()
        if clean_user in self._user_id_cache:
            return self._user_id_cache[clean_user]

        variables = {"screen_name": clean_user, "withSafetyModeUserFields": True}
        features = {"hidden_profile_likes_enabled": True}

        data = self._execute_get("UserByScreenName", {"variables": variables, "features": features})
        if not data:
            return None

        rest_id = data.get("data", {}).get("user", {}).get("result", {}).get("rest_id")
        if rest_id:
            self._user_id_cache[clean_user] = str(rest_id)
            return str(rest_id)
        return None

    def get_user_tweets(self, screen_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetches tweets from user timeline using authenticated UserTweets GraphQL."""
        clean_user = screen_name.lstrip("@").strip()
        rest_id = self.get_user_id(clean_user)
        if not rest_id:
            return []

        variables = {
            "userId": rest_id,
            "count": limit,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": False,
            "withVoice": True,
            "withV2Timeline": True
        }
        features = {
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_claim_filtering_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "tweet_awards_web_tipping_enabled": False,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "rweb_video_timestamps_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True,
            "responsive_web_enhance_cards_enabled": False
        }

        data = self._execute_get("UserTweets", {"variables": variables, "features": features})
        if not data:
            return []

        tweets = []
        try:
            u_res = data.get("data", {}).get("user", {}).get("result", {})
            tl = u_res.get("timeline") or u_res.get("timeline_v2") or {}
            instructions = tl.get("timeline", {}).get("instructions", []) if "timeline" in tl else tl.get("instructions", [])

            for inst in instructions:
                entries = inst.get("entries", [])
                for entry in entries:
                    content = entry.get("content", {})
                    item_content = content.get("itemContent", {})
                    tweet_result = item_content.get("tweet_results", {}).get("result", {})
                    if not tweet_result:
                        continue

                    # Handle tweet wrapper variants
                    tweet_data = tweet_result.get("tweet", tweet_result)
                    legacy = tweet_data.get("legacy", tweet_data)
                    tweet_id = tweet_data.get("rest_id") or legacy.get("id_str", "")
                    raw_text = legacy.get("full_text") or legacy.get("text", "")
                    user_core = tweet_data.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {})

                    screen_name_found = user_core.get("screen_name", clean_user)
                    author_name = user_core.get("name", screen_name_found)

                    tweets.append({
                        "tweet_id": str(tweet_id),
                        "title": f"Tweet by @{screen_name_found}",
                        "author": author_name,
                        "username": screen_name_found,
                        "text": clean_news_text(raw_text),
                        "url": f"https://x.com/{screen_name_found}/status/{tweet_id}",
                        "published_date": legacy.get("created_at", ""),
                        "metrics": {
                            "likes": legacy.get("favorite_count", 0),
                            "retweets": legacy.get("retweet_count", 0),
                            "replies": legacy.get("reply_count", 0),
                        },
                        "backend": "graphql_api"
                    })
                    if len(tweets) >= limit:
                        return tweets
        except Exception:
            pass

        return tweets

    def search_tweets(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Searches Twitter for keyword queries using SearchTimeline or UserTweets delegation."""
        # If query matches from:username or @username, route to get_user_tweets
        if query.startswith("from:"):
            target_user = query[5:].strip()
            return self.get_user_tweets(target_user, limit=limit)
        if query.startswith("@"):
            return self.get_user_tweets(query[1:].strip(), limit=limit)

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

        data = self._execute_get("SearchTimeline", {"variables": variables, "features": features})
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

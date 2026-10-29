"""
Twitter Web GraphQL Client.
Executes keyword search, user timelines, and reply threads using browser session tokens.
Bypasses public timeline limits and syndication restrictions.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from processors.tweet_normalizer import normalize_tweet

# Public Twitter Web client bearer token embedded in main.js
TWITTER_WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

logger = logging.getLogger("twitter_graphql")

GRAPHQL_BASE = "https://x.com/i/api/graphql"
MAX_TIMELINE_PAGES = 5
MAX_RATE_LIMIT_WAIT = 900.0

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
        """Resolves a known handle to confirm the cookies still authenticate.

        The legacy settings.json endpoint now returns 404 for every session, so
        the probe uses a GraphQL query instead of a retired REST route.
        """
        return bool(self.get_user_id("X"))

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
                except Exception as exc:
                    logger.debug("Skipping bundle %s: %s", js_url, exc)
                    continue
        except Exception as exc:
            logger.warning("Query id discovery failed: %s", exc)

    @staticmethod
    def _rate_limit_wait(error: urllib.error.HTTPError) -> float:
        """Returns seconds to wait from the reset header, clamped to the cap."""
        reset = error.headers.get("x-rate-limit-reset") if error.headers else None
        try:
            wait = float(reset) - time.time()
        except (TypeError, ValueError):
            return 60.0
        return max(1.0, min(wait, MAX_RATE_LIMIT_WAIT))

    def _execute(
        self,
        operation: str,
        params: Dict[str, Any],
        method: str = "GET",
        retried: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Calls one GraphQL operation, retrying once on 404 (rotated id) and 429."""
        # ponytail: URL encode GraphQL variables; upgrade to twikit client if query IDs get rotated by X.
        query_id = self.query_ids.get(operation, "")
        url = f"{GRAPHQL_BASE}/{query_id}/{operation}"
        headers = self._get_headers()

        if method == "POST":
            body = json.dumps({**params, "queryId": query_id}).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        else:
            query_str = urllib.parse.urlencode(
                {k: json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v) for k, v in params.items()}
            )
            req = urllib.request.Request(f"{url}?{query_str}", headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            if e.code == 404 and not self._discovered:
                self.discover_query_ids()
                new_id = self.query_ids.get(operation, "")
                if new_id and new_id != query_id:
                    return self._execute(operation, params, method=method)
                return None
            if e.code == 429 and not retried:
                wait = self._rate_limit_wait(e)
                if wait <= MAX_RATE_LIMIT_WAIT:
                    logger.warning("Rate limited on %s; sleeping %.0fs", operation, wait)
                    time.sleep(wait)
                    return self._execute(operation, params, method=method, retried=True)
            logger.warning("GraphQL %s failed with HTTP %s", operation, e.code)
            return None
        except Exception as exc:
            logger.warning("GraphQL %s failed: %s", operation, exc)
            return None

    def get_user_id(self, screen_name: str) -> Optional[str]:
        """Resolves Twitter screen name to rest_id via UserByScreenName."""
        clean_user = screen_name.lstrip("@").strip()
        if clean_user in self._user_id_cache:
            return self._user_id_cache[clean_user]

        variables = {"screen_name": clean_user, "withSafetyModeUserFields": True}
        features = {"hidden_profile_likes_enabled": True}

        data = self._execute("UserByScreenName", {"variables": variables, "features": features})
        if not data:
            return None

        rest_id = data.get("data", {}).get("user", {}).get("result", {}).get("rest_id")
        if rest_id:
            self._user_id_cache[clean_user] = str(rest_id)
            return str(rest_id)
        return None

    def _collect(
        self,
        instructions: List[Dict[str, Any]],
        limit: int,
        seen: set,
        fallback_username: str,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Maps one timeline page into records and returns the next cursor."""
        records: List[Dict[str, Any]] = []
        cursor = None

        for inst in instructions:
            for entry in inst.get("entries", []):
                content = entry.get("content", {})
                if content.get("cursorType") == "Bottom" and content.get("value"):
                    cursor = content["value"]
                    continue
                tweet_result = content.get("itemContent", {}).get("tweet_results", {}).get("result", {})
                if not tweet_result:
                    continue
                record = normalize_tweet(tweet_result, "graphql_api", fallback_username=fallback_username)
                if record is None or record["tweet_id"] in seen:
                    continue
                seen.add(record["tweet_id"])
                records.append(record)
                if len(records) >= limit:
                    return records, None

        return records, cursor

    def get_user_tweets(self, screen_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetches a user timeline, following the bottom cursor until limit is met."""
        clean_user = screen_name.lstrip("@").strip()
        rest_id = self.get_user_id(clean_user)
        if not rest_id:
            return []

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
            "responsive_web_enhance_cards_enabled": False,
        }

        tweets: List[Dict[str, Any]] = []
        seen: set = set()
        cursor = None
        pages = 0

        while len(tweets) < limit and pages < MAX_TIMELINE_PAGES:
            pages += 1
            variables = {
                "userId": rest_id,
                "count": limit - len(tweets),
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": False,
                "withVoice": True,
                "withV2Timeline": True,
            }
            if cursor:
                variables["cursor"] = cursor

            data = self._execute("UserTweets", {"variables": variables, "features": features})
            if not data:
                break

            user_result = data.get("data", {}).get("user", {}).get("result", {})
            timeline = user_result.get("timeline") or user_result.get("timeline_v2") or {}
            instructions = timeline.get("timeline", {}).get("instructions", []) if "timeline" in timeline else timeline.get("instructions", [])

            page_records, cursor = self._collect(instructions, limit - len(tweets), seen, clean_user)
            tweets.extend(page_records)
            if not cursor:
                break

        return tweets

    def search_tweets(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Searches Twitter for keyword queries, following the bottom cursor."""
        if query.startswith("from:"):
            target_user = query[5:].strip()
            return self.get_user_tweets(target_user, limit=limit)
        if query.startswith("@"):
            return self.get_user_tweets(query[1:].strip(), limit=limit)

        features = {
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "responsive_web_home_pinned_timelines_enabled": True,
            "responsive_web_enhance_cards_enabled": False,
        }

        tweets: List[Dict[str, Any]] = []
        seen: set = set()
        cursor = None
        pages = 0

        while len(tweets) < limit and pages < MAX_TIMELINE_PAGES:
            pages += 1
            variables = {
                "rawQuery": query,
                "count": limit - len(tweets),
                "querySource": "typed_query",
                "product": "Top",
            }
            if cursor:
                variables["cursor"] = cursor

            data = self._execute("SearchTimeline", {"variables": variables, "features": features}, method="POST")
            if not data:
                break

            instructions = (
                data.get("data", {})
                .get("search_by_raw_query", {})
                .get("search_timeline", {})
                .get("timeline", {})
                .get("instructions", [])
            )

            page_records, cursor = self._collect(instructions, limit - len(tweets), seen, "unknown")
            tweets.extend(page_records)
            if not cursor:
                break

        return tweets
"""Single mapping from Twitter payloads to harvest records."""

from typing import Any, Dict, Optional

from processors.cleaner import clean_news_text, normalize_timestamp


def normalize_tweet(
    tweet_result: Dict[str, Any],
    backend: str,
    fallback_username: str = "unknown",
) -> Optional[Dict[str, Any]]:
    """Maps a GraphQL or syndication tweet payload to a harvest record.

    Returns None when the payload carries no tweet id, because a record without
    one cannot be deduplicated downstream.
    """
    tweet_data = tweet_result.get("tweet", tweet_result)
    legacy = tweet_data.get("legacy", tweet_data)
    tweet_id = tweet_data.get("rest_id") or legacy.get("id_str", "")
    if not tweet_id:
        return None

    raw_text = legacy.get("full_text") or legacy.get("text", "")
    user_result = tweet_data.get("core", {}).get("user_results", {}).get("result", {})
    user_legacy = user_result.get("legacy", {})
    user_core = user_result.get("core", {})
    user_info = tweet_data.get("user", {})
    screen_name = (
        user_core.get("screen_name")
        or user_legacy.get("screen_name")
        or user_info.get("screen_name")
        or fallback_username
    )
    author_name = (
        user_core.get("name")
        or user_legacy.get("name")
        or user_info.get("name")
        or screen_name
    )

    return {
        "tweet_id": str(tweet_id),
        "title": f"Tweet by @{screen_name}",
        "author": author_name,
        "username": screen_name,
        "text": clean_news_text(raw_text),
        "url": f"https://x.com/{screen_name}/status/{tweet_id}",
        "published_date": normalize_timestamp(legacy.get("created_at", "")),
        "metrics": {
            "likes": legacy.get("favorite_count", 0),
            "retweets": legacy.get("retweet_count", 0),
            "replies": legacy.get("reply_count", 0),
        },
        "backend": backend,
    }

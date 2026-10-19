import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("base_spider")


class BaseSpider(ABC):
    """
    Abstract Base Spider for Indonesian Sports News Portals.
    Enforces standardized article data structure and politeness rate limiting.
    """

    def __init__(self, portal_name: str, rate_limit_sec: float = 1.0):
        self.portal_name = portal_name
        self.rate_limit_sec = rate_limit_sec
        self.last_request_time = 0.0

    def wait_rate_limit(self):
        """Enforces request throttling to avoid IP bans."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.rate_limit_sec:
            time.sleep(self.rate_limit_sec - elapsed)
        self.last_request_time = time.time()

    @abstractmethod
    def search_articles(self, query: str, max_pages: int = 5) -> List[Dict[str, Any]]:
        """
        Searches portal for articles matching query keyword.
        Must return list of dicts with:
        - title: str
        - url: str
        - published_date: str (ISO format or YYYY-MM-DD)
        - author: Optional[str]
        - summary: Optional[str]
        """
        pass

    @abstractmethod
    def extract_article_content(self, url: str) -> Dict[str, Any]:
        """
        Fetches and extracts full text content of a specific article.
        Returns dict with:
        - content: str
        - tags: List[str]
        """
        pass

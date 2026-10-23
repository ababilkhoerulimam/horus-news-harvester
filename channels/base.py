"""
Base Sports Channel definition and backend routing semantics.
Adapted from Agent-Reach channel pattern for Indonesian sports domain.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple


class SportsChannel(ABC):
    """
    Abstract base class for all sports information channels.
    Enforces ordered backend fallback, health checking, and standardized schema.
    """

    name: str = ""
    description: str = ""
    sports_supported: List[str] = ["football", "basketball", "badminton", "volleyball"]
    backends: List[str] = []
    tier: int = 0  # 0: zero-config (public/RSS), 1: requires free key, 2: requires session/cookie

    active_backend: Optional[str] = None

    @abstractmethod
    def can_handle(self, url_or_source: str) -> bool:
        """Determines if this channel can ingest or query the given URL or identifier."""
        pass

    def ordered_backends(self, config: Optional[Dict[str, Any]] = None) -> List[str]:
        """
        Returns backends in priority probe order.
        Honors user override in config if specified.
        """
        if config and "order" in config:
            return [b for b in config["order"] if b in self.backends]
        candidates = list(self.backends)
        if config:
            override = config.get(f"{self.name}_backend")
            if override and override in candidates:
                candidates.remove(override)
                candidates.insert(0, override)
        return candidates

    def check(self, config: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        """
        Probes channel availability and sets active_backend.
        Returns status ('ok', 'warn', 'error') and descriptive message.
        """
        self.active_backend = self.backends[0] if self.backends else "native"
        return "ok", f"Active backend: {self.active_backend}"

    @abstractmethod
    def search(
        self,
        query: str,
        sport: str = "football",
        limit: int = 10,
        config: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Searches channel for articles or social discussions.
        Returns standardized list of dicts:
        - title: str
        - url: str
        - published_date: str
        - author: Optional[str]
        - summary: Optional[str]
        - sport: str
        - source: str
        - backend: str
        """
        pass

    @abstractmethod
    def extract(self, url: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Extracts full text and metadata from content URL.
        Returns:
        - url: str
        - title: Optional[str]
        - content: str
        - author: Optional[str]
        - published_date: Optional[str]
        - tags: List[str]
        - backend: str
        - status: str ('success' | 'failed')
        """
        pass

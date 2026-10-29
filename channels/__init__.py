"""
Channel registry for ind-sports-scrapper.

Channels are built on first use rather than at import time: constructing
TwitterChannel validates a live session, and an import must not depend on the
network or on a browser profile being present.
"""

from typing import Callable, Dict, List, Optional
from .base import SportsChannel
from .portal_news import PortalNewsChannel
from .social_youtube import YouTubeChannel
from .social_twitter import TwitterChannel

_FACTORIES: Dict[str, Callable[[], SportsChannel]] = {
    "portal_news": PortalNewsChannel,
    "social_youtube": YouTubeChannel,
    "social_twitter": TwitterChannel,
}

_INSTANCES: Dict[str, SportsChannel] = {}


def get_channel(name: str) -> Optional[SportsChannel]:
    """Returns a shared channel instance, building it on first request."""
    factory = _FACTORIES.get(name)
    if factory is None:
        return None
    if name not in _INSTANCES:
        _INSTANCES[name] = factory()
    return _INSTANCES[name]


def get_all_channels() -> List[SportsChannel]:
    """Returns every registered channel, building each one."""
    return [get_channel(name) for name in _FACTORIES]


__all__ = [
    "SportsChannel",
    "PortalNewsChannel",
    "YouTubeChannel",
    "TwitterChannel",
    "get_all_channels",
    "get_channel",
]
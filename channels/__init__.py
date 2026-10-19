"""
Channel registry for ind-sports-scrapper.
"""

from typing import Dict, List, Optional
from .base import SportsChannel
from .portal_news import PortalNewsChannel
from .social_youtube import YouTubeChannel
from .social_twitter import TwitterChannel

_CHANNELS: Dict[str, SportsChannel] = {
    "portal_news": PortalNewsChannel(),
    "social_youtube": YouTubeChannel(),
    "social_twitter": TwitterChannel(),
}


def get_all_channels() -> List[SportsChannel]:
    """Returns list of all registered sports channels."""
    return list(_CHANNELS.values())


def get_channel(name: str) -> Optional[SportsChannel]:
    """Retrieves a specific channel by identifier."""
    return _CHANNELS.get(name)


__all__ = [
    "SportsChannel",
    "PortalNewsChannel",
    "YouTubeChannel",
    "TwitterChannel",
    "get_all_channels",
    "get_channel",
]

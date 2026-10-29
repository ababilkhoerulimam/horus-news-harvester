"""Shared CLI helpers for proxy configuration."""

from typing import Optional, Tuple

from processors.http_client import DEFAULT_PROXIES_FILE, default_http_client


def configure_proxy(
    proxy: Optional[str] = None,
    proxies_file: Optional[str] = None,
) -> Tuple[int, str]:
    """Applies an explicit proxy, an explicit file, or the default proxy file.

    Returns the number of applied proxies and the source that produced them, so
    callers can log one consistent line regardless of which path was taken.
    """
    if proxy:
        default_http_client.set_single_proxy(proxy)
        return 1, proxy
    if proxies_file:
        return default_http_client.load_proxies_from_file(proxies_file), proxies_file
    if DEFAULT_PROXIES_FILE.exists():
        return (
            default_http_client.load_proxies_from_file(str(DEFAULT_PROXIES_FILE)),
            str(DEFAULT_PROXIES_FILE),
        )
    return 0, ""

"""
Curl Impersonate Backend for High-Speed Anti-Bot TLS/JA4 Impersonation.
Mimics genuine browser TLS handshakes (Chrome, Safari, Edge) via curl_cffi.
Bypasses Cloudflare / Akamai / WAFs at the transport layer without headless browser overhead.
Inspired by Scrapling fetcher architecture.
"""

import threading
from typing import Any, Dict, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests import AsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    AsyncSession = None
    HAS_CURL_CFFI = False


class CurlImpersonateBackend:
    """
    Browser-grade TLS impersonation backend using curl_cffi.
    Supports both synchronous thread-local session pooling and AsyncSession.
    Falls back gracefully when curl_cffi is not installed.
    """

    name = "curl_impersonate"

    def __init__(self, impersonate: str = "chrome124", timeout: int = 10):
        self.impersonate = impersonate
        self.timeout = timeout
        self._local = threading.local()

    @staticmethod
    def is_available() -> bool:
        return HAS_CURL_CFFI

    def check(self) -> Tuple[str, str]:
        if HAS_CURL_CFFI:
            return "ok", f"curl_cffi operational (impersonating {self.impersonate})"
        return "off", "curl_cffi not installed (optional: pip install curl_cffi for browser TLS spoofing)"

    def _get_sync_session(self) -> Any:
        if not hasattr(self._local, "session") or self._local.session is None:
            if HAS_CURL_CFFI and curl_requests is not None:
                self._local.session = curl_requests.Session(
                    impersonate=self.impersonate,
                    timeout=self.timeout,
                    headers={
                        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Sec-Ch-Ua-Platform": '"Windows"',
                    },
                )
        return getattr(self._local, "session", None)

    def fetch(self, url: str, proxy: Optional[str] = None) -> Tuple[int, str]:
        """
        Fetches URL using browser TLS handshake and HTTP/2 connection pooling.
        Returns (status_code, html_text).
        """
        if not HAS_CURL_CFFI:
            return 0, ""

        proxies = {"http": proxy, "https": proxy} if proxy else None
        session = self._get_sync_session()
        try:
            if session is not None:
                resp = session.get(url, proxies=proxies, timeout=self.timeout)
            else:
                resp = curl_requests.get(
                    url,
                    impersonate=self.impersonate,
                    timeout=self.timeout,
                    proxies=proxies,
                    headers={
                        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Sec-Ch-Ua-Platform": '"Windows"',
                    },
                )
            return resp.status_code, resp.text
        except Exception:
            if hasattr(self._local, "session"):
                self._local.session = None
            return 0, ""

    async def fetch_async(
        self,
        session: Any,
        url: str,
        proxy: Optional[str] = None,
        cookies: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
    ) -> Tuple[int, str]:
        """
        Fetches URL asynchronously using an active HTTP/2 AsyncSession.
        Returns (status_code, html_text).

        cookies and user_agent replay a stored browser session. They belong together:
        a session's cookies over a different User-Agent is not a client any browser
        would produce, and WAFs score it worse than sending no cookies at all.
        """
        if not HAS_CURL_CFFI or session is None:
            return 0, ""

        proxies = {"http": proxy, "https": proxy} if proxy else None
        headers = {
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua-Platform": '"Windows"',
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        try:
            resp = await session.get(
                url,
                timeout=self.timeout,
                proxies=proxies,
                headers=headers,
                cookies=cookies,
            )
            return resp.status_code, resp.text
        except Exception:
            return 0, ""

    def create_async_session(self) -> Any:
        """Creates an initialized AsyncSession instance if curl_cffi is available."""
        if HAS_CURL_CFFI and AsyncSession is not None:
            return AsyncSession(
                impersonate=self.impersonate,
                timeout=self.timeout,
                headers={
                    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                },
            )
        return None

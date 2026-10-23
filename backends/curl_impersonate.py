"""
Curl Impersonate Backend for High-Speed Anti-Bot TLS/JA4 Impersonation.
Mimics genuine browser TLS handshakes (Chrome, Safari, Edge) via curl_cffi.
Bypasses Cloudflare / Akamai / WAFs at the transport layer without headless browser overhead.
Inspired by Scrapling fetcher architecture.
"""

from typing import Dict, Any, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    HAS_CURL_CFFI = False


class CurlImpersonateBackend:
    """
    Browser-grade TLS impersonation backend using curl_cffi.
    Falls back gracefully when curl_cffi is not installed.
    """

    name = "curl_impersonate"

    def __init__(self, impersonate: str = "chrome124", timeout: int = 10):
        self.impersonate = impersonate
        self.timeout = timeout

    @staticmethod
    def is_available() -> bool:
        return HAS_CURL_CFFI

    def check(self) -> Tuple[str, str]:
        if HAS_CURL_CFFI:
            return "ok", f"curl_cffi operational (impersonating {self.impersonate})"
        return "off", "curl_cffi not installed (optional: pip install curl_cffi for browser TLS spoofing)"

    def fetch(self, url: str, proxy: Optional[str] = None) -> Tuple[int, str]:
        """
        Fetches URL using browser TLS handshake and HTTP/2 fingerprint.
        Returns (status_code, html_text).
        """
        if not HAS_CURL_CFFI:
            return 0, ""

        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
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
            return 0, ""

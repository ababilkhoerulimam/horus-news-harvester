"""
Resilient HTTP Client with Pluggable Proxy Pool, User-Agent Rotation, and Request Jitter.

The default transport is the Python standard library (urllib.request) with zero external
dependencies. An optional curl_cffi transport reuses one browser-fingerprinted connection
pool per thread, which removes the TLS handshake that urllib pays on every request.
"""

import random
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True
except ImportError:  # optional dependency, see pyproject extra "advanced-stealth"
    curl_requests = None
    HAS_CURL_CFFI = False


# Transport-level failures that mean "this proxy is unusable", as opposed to a target
# error such as 429 or 403 where rotating the proxy is still useful.
_PROXY_ERROR_INDICATORS = (
    "net::err_proxy",
    "net::err_tunnel",
    "connection refused",
    "connection reset",
    "connection timed out",
    "failed to connect",
    "could not resolve proxy",
)


def is_proxy_error(error: BaseException) -> bool:
    """True when the failure points at the proxy rather than the target."""
    message = str(error).lower()
    return any(indicator in message for indicator in _PROXY_ERROR_INDICATORS)


def backoff_seconds(attempts: int, retry_after: Optional[str] = None) -> float:
    """Sleep length for a 429/503 retry.

    Honours Retry-After when it is a plain integer count of seconds, otherwise uses
    capped exponential growth plus jitter.
    """
    if retry_after and retry_after.strip().isdigit():
        return min(45.0, float(retry_after.strip()))
    return min(30.0, 5.0 * (2 ** (attempts - 1))) + random.uniform(0.5, 1.5)


DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

DEFAULT_PROXIES_FILE = Path(__file__).resolve().parent.parent / "config" / "proxies.txt"


def normalize_proxy_url(raw: str) -> str:
    """Normalizes raw proxy string into standard URL format (e.g. IP:PORT:USER:PASS -> http://USER:PASS@IP:PORT)."""
    clean = raw.strip()
    if not clean:
        return ""
    parts = clean.split(":")
    if len(parts) == 4 and not clean.startswith(("http://", "https://", "socks5://")):
        ip, port, user, pwd = parts
        return f"http://{user}:{pwd}@{ip}:{port}"
    if not clean.startswith(("http://", "https://", "socks5://")):
        return f"http://{clean}"
    return clean


class ResilientHttpClient:
    """
    HTTP client providing automatic User-Agent rotation, optional rotating proxy pool,
    random jitter to break bot-detection patterns, and 429 auto-backoff.
    """

    def __init__(
        self,
        proxies_file: Optional[Path] = None,
        min_jitter: float = 0.8,
        max_jitter: float = 2.0,
        enable_jitter: bool = True,
    ):
        self.proxies_file = Path(proxies_file) if proxies_file else None
        self.min_jitter = min_jitter
        self.max_jitter = max_jitter
        self.enable_jitter = enable_jitter
        self._pinned_user_agent: Optional[str] = None
        self._proxies: List[str] = []
        self._failed_proxies: set = set()
        self._single_proxy: Optional[str] = None
        self._curl_local = threading.local()
        if self.proxies_file:
            self.reload_proxies()

    def set_single_proxy(self, proxy_url: Optional[str]) -> None:
        """Forces all requests to route through this specific proxy endpoint."""
        if proxy_url and proxy_url.strip():
            self._single_proxy = normalize_proxy_url(proxy_url)
        else:
            self._single_proxy = None

    def add_proxies(self, proxy_list: List[str]) -> int:
        """Adds a list of proxies to the rotating pool."""
        added = 0
        for p in proxy_list:
            clean = normalize_proxy_url(p)
            if clean and clean not in self._proxies:
                self._proxies.append(clean)
                added += 1
        return added

    def load_proxies_from_file(self, file_path: str) -> int:
        """Loads additional proxies from a user-specified text file."""
        p_path = Path(file_path)
        if not p_path.exists():
            return 0
        loaded = 0
        try:
            with open(p_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        clean = normalize_proxy_url(line)
                        if clean and clean not in self._proxies:
                            self._proxies.append(clean)
                            loaded += 1
        except Exception:
            pass
        return loaded

    def reload_proxies(self) -> int:
        """Loads proxies from config/proxies.txt if present."""
        self._proxies = []
        if self.proxies_file.exists():
            try:
                with open(self.proxies_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            clean = normalize_proxy_url(line)
                            if clean and clean not in self._proxies:
                                self._proxies.append(clean)
            except Exception:
                pass
        return len(self._proxies)

    def get_active_proxy(self) -> Optional[str]:
        """Returns pinned single proxy or a random active proxy from the pool."""
        if self._single_proxy:
            return self._single_proxy
        available = [p for p in self._proxies if p not in self._failed_proxies]
        if not available:
            return None
        return random.choice(available)

    def mark_proxy_failed(self, proxy: str) -> None:
        """Temporarily marks an unresponsive proxy as failed."""
        self._failed_proxies.add(proxy)

    def set_user_agent(self, user_agent: Optional[str]) -> Optional[str]:
        """Pins one User-Agent for every request, or clears the pin when None.

        Rotation is the right default for anonymous scraping, but it is the wrong
        choice for a cookie session: curl_cffi already pins the TLS fingerprint to
        Chrome 124, and sending five different browser strings over one Chrome
        fingerprint is a louder bot signal than sending the same one every time.
        """
        self._pinned_user_agent = (user_agent or "").strip() or None
        return self._pinned_user_agent

    def pinned_user_agent(self) -> Optional[str]:
        """Returns the pinned User-Agent, or None when rotation is active."""
        return self._pinned_user_agent

    def get_random_user_agent(self) -> str:
        """Selects a modern realistic desktop browser User-Agent."""
        if self._pinned_user_agent:
            return self._pinned_user_agent
        return random.choice(DEFAULT_USER_AGENTS)

    def apply_jitter(self, min_sec: Optional[float] = None, max_sec: Optional[float] = None) -> None:
        """Applies random delay to break robotic cadence."""
        if not self.enable_jitter:
            return
        low = min_sec if min_sec is not None else self.min_jitter
        high = max_sec if max_sec is not None else self.max_jitter
        if high > 0 and high >= low:
            time.sleep(random.uniform(low, high))

    def fetch(
        self,
        url: str,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 6.0,
        use_jitter: Optional[bool] = None,
        max_retries: int = 1,
        transport: str = "urllib",
    ) -> Tuple[int, bytes, Dict[str, str]]:
        """
        Executes HTTP request with proxy rotation, UA randomization, jitter,
        and adaptive exponential backoff on HTTP 429/503.
        Returns (status_code, body_bytes, headers_dict).

        transport selects the wire implementation:
          "urllib" - standard library, new TLS handshake per request (default)
          "curl"   - curl_cffi, pooled and browser-fingerprinted per thread
          "auto"   - curl when curl_cffi is installed, otherwise urllib
        """
        should_jitter = self.enable_jitter if use_jitter is None else use_jitter
        if should_jitter:
            self.apply_jitter()

        use_curl = self._resolve_transport(transport)
        base_headers = dict(headers or {})
        attempts = 0
        last_error = None

        while attempts <= max_retries:
            attempts += 1
            req_headers = dict(base_headers)
            # Rotate User-Agent on initial attempt if not given, and on each retry
            if "User-Agent" not in base_headers or attempts > 1:
                req_headers["User-Agent"] = self.get_random_user_agent()

            current_proxy = self.get_active_proxy()

            if use_curl:
                try:
                    status, body, resp_headers = self._send_curl(
                        url, data, req_headers, timeout, current_proxy
                    )
                except Exception as e:
                    if current_proxy and is_proxy_error(e):
                        self.mark_proxy_failed(current_proxy)
                    last_error = e
                    if attempts <= max_retries:
                        time.sleep(random.uniform(1.0, 2.5))
                        continue
                    break

                if status in (429, 503) and attempts <= max_retries:
                    retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
                    time.sleep(backoff_seconds(attempts, retry_after))
                    if current_proxy:
                        self.mark_proxy_failed(current_proxy)
                    last_error = status
                    continue
                if status >= 400:
                    return status, b"", resp_headers
                return status, body, resp_headers

            try:
                return self._send_urllib(url, data, req_headers, timeout, current_proxy)

            except urllib.error.HTTPError as he:
                # Adaptive backoff on 429 Too Many Requests / 503 Service Unavailable.
                if he.code in (429, 503) and attempts <= max_retries:
                    retry_after_str = he.headers.get("Retry-After") if hasattr(he, "headers") else None
                    time.sleep(backoff_seconds(attempts, retry_after_str))
                    if current_proxy:
                        self.mark_proxy_failed(current_proxy)
                    last_error = he
                    continue
                return he.code, b"", {}

            except Exception as e:
                if current_proxy and is_proxy_error(e):
                    self.mark_proxy_failed(current_proxy)
                last_error = e
                if attempts <= max_retries:
                    time.sleep(random.uniform(1.0, 2.5))
                    continue
                break

        return 500, b"", {}

    def _resolve_transport(self, transport: str) -> bool:
        """Decides whether this request goes over curl_cffi."""
        if transport == "curl":
            return HAS_CURL_CFFI
        if transport == "auto":
            return HAS_CURL_CFFI
        return False

    def _send_urllib(
        self,
        url: str,
        data: Optional[bytes],
        headers: Dict[str, str],
        timeout: float,
        proxy: Optional[str],
    ) -> Tuple[int, bytes, Dict[str, str]]:
        """One attempt over urllib. Raises HTTPError so the caller keeps its retry rules."""
        req = urllib.request.Request(url, data=data, headers=headers)
        if proxy:
            proxy_handler = urllib.request.ProxyHandler({
                "http": proxy,
                "https": proxy,
            })
            opener = urllib.request.build_opener(proxy_handler)
            resp_ctx = opener.open(req, timeout=timeout)
        else:
            resp_ctx = urllib.request.urlopen(req, timeout=timeout)

        with resp_ctx as resp:
            raw_status = getattr(resp, "status", None)
            status = raw_status if isinstance(raw_status, int) else 200
            body = resp.read()
            resp_headers = dict(resp.headers.items()) if hasattr(resp, "headers") else {}
            return status, body, resp_headers

    def _curl_session(self) -> Any:
        """Returns this thread's pooled curl_cffi session, creating it on first use."""
        session = getattr(self._curl_local, "session", None)
        if session is None:
            session = curl_requests.Session(
                impersonate="chrome124",
                headers={
                    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                },
            )
            self._curl_local.session = session
        return session

    def _send_curl(
        self,
        url: str,
        data: Optional[bytes],
        headers: Dict[str, str],
        timeout: float,
        proxy: Optional[str],
    ) -> Tuple[int, bytes, Dict[str, str]]:
        """One attempt over a pooled curl_cffi session. HTTP errors come back as a status."""
        session = self._curl_session()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        method = "POST" if data is not None else "GET"
        resp = session.request(
            method,
            url,
            data=data,
            headers=headers,
            timeout=timeout,
            proxies=proxies,
        )
        body = resp.content or b""
        if isinstance(body, str):
            body = body.encode("utf-8")
        resp_headers = dict(resp.headers.items()) if resp.headers else {}
        return int(resp.status_code), body, resp_headers


# Singleton instance for simple import
default_http_client = ResilientHttpClient()

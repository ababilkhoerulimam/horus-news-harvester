"""
Resilient HTTP Client with Pluggable Proxy Pool, User-Agent Rotation, and Request Jitter.
Uses Python standard library (urllib.request) with zero external dependencies.
"""

import os
import random
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any


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
        self._proxies: List[str] = []
        self._failed_proxies: set = set()
        self._single_proxy: Optional[str] = None
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

    def get_random_user_agent(self) -> str:
        """Selects a modern realistic desktop browser User-Agent."""
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
    ) -> Tuple[int, bytes, Dict[str, str]]:
        """
        Executes HTTP request with proxy rotation, UA randomization, jitter,
        and adaptive exponential backoff on HTTP 429/503.
        Returns (status_code, body_bytes, headers_dict).
        """
        should_jitter = self.enable_jitter if use_jitter is None else use_jitter
        if should_jitter:
            self.apply_jitter()

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

            # Configure request execution (proxy vs direct)
            try:
                req = urllib.request.Request(url, data=data, headers=req_headers)
                if current_proxy:
                    proxy_handler = urllib.request.ProxyHandler({
                        "http": current_proxy,
                        "https": current_proxy,
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

            except urllib.error.HTTPError as he:
                # ponytail: Adaptive backoff on 429 Too Many Requests / 503 Service Unavailable.
                if he.code in (429, 503) and attempts <= max_retries:
                    retry_after_str = he.headers.get("Retry-After") if hasattr(he, "headers") else None
                    if retry_after_str and retry_after_str.strip().isdigit():
                        sleep_sec = min(45.0, float(retry_after_str.strip()))
                    else:
                        sleep_sec = min(30.0, 5.0 * (2 ** (attempts - 1))) + random.uniform(0.5, 1.5)

                    time.sleep(sleep_sec)
                    if current_proxy:
                        self.mark_proxy_failed(current_proxy)
                    last_error = he
                    continue
                return he.code, b"", {}

            except Exception as e:
                if current_proxy:
                    self.mark_proxy_failed(current_proxy)
                last_error = e
                if attempts <= max_retries:
                    time.sleep(random.uniform(1.0, 2.5))
                    continue
                break

        return 500, b"", {}


# Singleton instance for simple import
default_http_client = ResilientHttpClient()

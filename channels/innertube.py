"""InnerTube transport shared by the YouTube channel call sites."""

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

from processors.http_client import default_http_client

logger = logging.getLogger("innertube")

# Observed live on 2026-10-29. The harvester refreshes this once per run so the
# value does not rot, and this constant covers offline or blocked runs.
_CLIENT_VERSION_FALLBACK = "2.20261028.01.00"
_CLIENT_VERSION_PATTERN = re.compile(r'"INNERTUBE_CLIENT_VERSION":"([0-9.]+)"')
_VISITOR_DATA_PATTERN = re.compile(r'"VISITOR_DATA":"([^"]+)"')
_CLIENT_NAME_WEB = "1"

# The origin a logged-in web client claims, and the salt for SAPISIDHASH.
_ORIGIN = "https://www.youtube.com"

_client_version = _CLIENT_VERSION_FALLBACK

# Visitor id that pairs with the cookie session. Empty until a page fetch finds it.
_visitor_data = ""

# Wire implementation for every InnerTube call. "auto" uses curl_cffi's pooled,
# browser-fingerprinted session when it is installed and falls back to urllib when
# it is not, so the harvester runs on a bare install without changing behaviour.
_TRANSPORTS = ("urllib", "curl", "auto")
_transport = "auto"

# Minimum gap between request starts, shared by every thread that calls post_innertube.
# Zero keeps the historical behaviour: no gate, caller-controlled jitter only.
_pace_lock = threading.Lock()
_min_interval = 0.0
_last_call = 0.0

# Optional cookie header for age-restricted, members-only, or consent-walled videos.
# Empty by default, which leaves the request byte-identical to the historical one.
_cookies: Dict[str, str] = {}


def configure_cookies(mapping: Optional[Dict[str, str]]) -> int:
    """Sets the Cookie header contents and returns how many cookies are active."""
    global _cookies
    _cookies = dict(mapping or {})
    return len(_cookies)


def cookie_header() -> str:
    """Returns the Cookie header value, or an empty string when no session is loaded."""
    return "; ".join(f"{name}={value}" for name, value in _cookies.items())


def configure_visitor_data(value: Optional[str]) -> str:
    """Sets the visitor id that accompanies the cookie session."""
    global _visitor_data
    _visitor_data = (value or "").strip()
    return _visitor_data


def visitor_data() -> str:
    """Returns the visitor id currently used, or an empty string."""
    return _visitor_data


def _sapisid() -> str:
    """Returns the account token used to sign requests, or an empty string."""
    return _cookies.get("SAPISID") or _cookies.get("__Secure-3PAPISID") or ""


def sapisid_hash(timestamp: Optional[int] = None) -> Optional[str]:
    """Builds the SAPISIDHASH Authorization value for the loaded session.

    YouTube derives the signature from the timestamp, the account token, and the
    origin, so the header is only valid for the second it was minted in. Returns
    None when the session has no token to sign with.
    """
    token = _sapisid()
    if not token:
        return None
    stamp = int(timestamp if timestamp is not None else time.time())
    digest = hashlib.sha1(f"{stamp} {token} {_ORIGIN}".encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {stamp}_{digest}"


def identity_headers() -> Dict[str, str]:
    """Headers that make a request look like the logged-in browser that minted the cookies.

    Empty when no session is loaded, which keeps anonymous traffic byte-identical
    to what it was before sessions existed.
    """
    if not _cookies:
        return {}
    headers = {"X-Origin": _ORIGIN, "X-Goog-AuthUser": "0"}
    if _visitor_data:
        headers["X-Goog-Visitor-Id"] = _visitor_data
    signature = sapisid_hash()
    if signature:
        headers["Authorization"] = signature
    return headers


def configure_pacing(seconds: float) -> float:
    """Sets the minimum gap between InnerTube requests and returns it.

    Applies to every caller at once, which is what makes a reply worker pool safe:
    the workers may run concurrently but their requests still leave at a fixed rate.
    """
    global _min_interval, _last_call
    if seconds < 0:
        raise ValueError(f"pacing must not be negative, got {seconds!r}")
    with _pace_lock:
        _min_interval = float(seconds)
        _last_call = 0.0
    return _min_interval


def pacing_interval() -> float:
    """Returns the active minimum gap in seconds."""
    return _min_interval


def _wait_turn() -> bool:
    """Blocks until this request may start. Returns True when pacing is active."""
    global _last_call
    if _min_interval <= 0:
        return False
    with _pace_lock:
        now = time.monotonic()
        wait = _min_interval - (now - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
    return True


def configure_transport(name: str) -> str:
    """Selects the InnerTube wire implementation and returns the active one."""
    global _transport
    if name not in _TRANSPORTS:
        raise ValueError(f"transport must be one of {_TRANSPORTS}, got {name!r}")
    _transport = name
    return _transport


def client_version() -> str:
    """Returns the client version currently used for InnerTube requests."""
    return _client_version


def refresh_client_version(timeout: int = 10) -> str:
    """Reads the live InnerTube client version once and caches it.

    Called from the harvester entrypoint rather than from post_innertube so a
    single request path stays single-request.
    """
    global _client_version
    try:
        status, body, _ = default_http_client.fetch(
            "https://www.youtube.com/", timeout=timeout, transport=_transport
        )
        if status == 200 and body:
            text = body.decode("utf-8", "replace")
            match = _CLIENT_VERSION_PATTERN.search(text)
            if match:
                _client_version = match.group(1)
            visitor = _VISITOR_DATA_PATTERN.search(text)
            if visitor:
                configure_visitor_data(visitor.group(1))
    except Exception as exc:
        logger.warning("Client version refresh failed, keeping %s: %s", _client_version, exc)
    return _client_version


def post_innertube(
    endpoint: str,
    payload: Dict[str, Any],
    timeout: int = 12,
    use_jitter: bool = False,
) -> Optional[Dict[str, Any]]:
    """Posts a client payload to an InnerTube endpoint and returns parsed JSON.

    Injects the client version and the client headers YouTube expects, which the
    bare context object alone does not satisfy. Returns None for non-200
    responses, empty bodies, or malformed JSON.
    """
    if _wait_turn():
        # Pacing already spaces the requests out; stacking jitter on top would only
        # add seconds without changing the shape of the traffic.
        use_jitter = False

    body = dict(payload)
    context = dict(body.get("context", {}))
    client = dict(context.get("client", {}))
    client["clientVersion"] = _client_version
    if _visitor_data and _cookies:
        # Real web clients send the visitor id inside the context as well as in
        # the header; the two disagreeing is itself a signal.
        client["visitorData"] = _visitor_data
    context["client"] = client
    body["context"] = context

    data = json.dumps(body).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "X-YouTube-Client-Name": _CLIENT_NAME_WEB,
        "X-YouTube-Client-Version": _client_version,
    }
    cookies = cookie_header()
    if cookies:
        request_headers["Cookie"] = cookies
        request_headers.update(identity_headers())

    status, response_body, _ = default_http_client.fetch(
        endpoint,
        data=data,
        headers=request_headers,
        timeout=timeout,
        use_jitter=use_jitter,
        transport=_transport,
    )
    if status != 200 or not response_body:
        return None
    try:
        return json.loads(response_body.decode("utf-8"))
    except ValueError:
        return None

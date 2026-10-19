"""
Jina Reader Backend — Zero-config Markdown content extraction via r.jina.ai.
Mirrors Agent-Reach web channel anti-bot detection and safety limits.
"""

import urllib.request
import urllib.error
from typing import Dict, Any, Tuple

DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB
ANTIBOT_SCAN_BYTES = 4096


def is_antibot_page(sample_bytes: bytes) -> bool:
    """Detects Cloudflare and anti-bot challenge signatures."""
    sample = sample_bytes[:ANTIBOT_SCAN_BYTES].decode("utf-8", errors="ignore").casefold()
    jina_warning = "warning:" in sample and "requiring captcha" in sample
    challenge_signatures = (
        "just a moment...",
        "## performing security verification",
        "attention required! | cloudflare",
        "access denied",
        "checking your browser before accessing",
    )
    if jina_warning or any(sig in sample for sig in challenge_signatures):
        return True
    return False


class JinaReaderBackend:
    """Extracts article content in clean Markdown via r.jina.ai gateway."""

    name = "jina_reader"

    def __init__(self, timeout: int = 8):
        self.timeout = timeout

    def check(self) -> Tuple[str, str]:
        """Probes connectivity to r.jina.ai."""
        try:
            req = urllib.request.Request(
                "https://r.jina.ai/https://example.com",
                headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/plain"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    return "ok", "Jina Reader gateway operational (r.jina.ai)"
            return "warn", f"Jina Reader returned status {resp.status}"
        except Exception as e:
            return "warn", f"Jina Reader unreachable ({e}); falling back to direct HTTP/RSS"

    def read_markdown(self, target_url: str) -> Dict[str, Any]:
        """Fetches target article and returns cleaned Markdown content."""
        clean_url = target_url.strip()
        if not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url

        jina_url = f"https://r.jina.ai/{clean_url}"
        req = urllib.request.Request(
            jina_url,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/plain",
                "X-Timeout": str(self.timeout),
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)

            if len(raw_bytes) > MAX_RESPONSE_BYTES:
                return {
                    "url": target_url,
                    "content": "",
                    "status": "failed",
                    "error": f"Response exceeded byte limit ({MAX_RESPONSE_BYTES} bytes)",
                }

            if is_antibot_page(raw_bytes):
                return {
                    "url": target_url,
                    "content": "",
                    "status": "failed",
                    "error": "Encountered Cloudflare/Anti-bot challenge page",
                }

            text = raw_bytes.decode("utf-8", errors="replace")
            return {
                "url": target_url,
                "content": text,
                "status": "success",
                "backend": self.name,
            }
        except urllib.error.HTTPError as e:
            return {
                "url": target_url,
                "content": "",
                "status": "failed",
                "error": f"HTTP Error {e.code}: {e.reason}",
            }
        except Exception as e:
            return {
                "url": target_url,
                "content": "",
                "status": "failed",
                "error": str(e),
            }

    extract = read_markdown

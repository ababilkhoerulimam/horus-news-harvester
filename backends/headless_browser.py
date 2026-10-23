"""
Headless Browser Backend for Full JS / Single Page Application (SPA) News Portals.
Uses native Chromium (Google Chrome or Microsoft Edge) via CLI --dump-dom.
Zero external library dependencies required (stdlib subprocess only).
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from processors.cleaner import clean_news_text


SPA_INDICATORS = (
    r'id=["\'](?:root|__next|app|__nuxt)["\']',
    r'window\.__INITIAL_STATE__',
    r'<noscript>.*?(?:enable javascript|perlu mengaktifkan javascript).*?</noscript>',
)


def is_spa_skeleton(html_content: str, text_content: str) -> bool:
    """
    Returns True if the page appears to be a client-side rendered SPA shell
    with missing article body text (< 400 characters).
    """
    if len(text_content.strip()) >= 400:
        return False

    return any(re.search(pat, html_content, re.IGNORECASE | re.DOTALL) for pat in SPA_INDICATORS)


class HeadlessBrowserBackend:
    """
    Headless Chromium backend for rendering JavaScript-heavy Indonesian news portals.
    Discovers local Chrome or Microsoft Edge binaries and invokes CLI --headless=new --dump-dom.
    """

    name = "headless_browser"

    def __init__(self, browser_path: Optional[str] = None, timeout: int = 15):
        self.timeout = timeout
        self.browser_path = browser_path or self.discover_browser()

    @staticmethod
    def discover_browser() -> Optional[str]:
        """Locates installed Google Chrome or Microsoft Edge binary."""
        env_path = os.environ.get("CHROME_PATH") or os.environ.get("EDGE_PATH")
        if env_path and os.path.exists(env_path):
            return env_path

        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            shutil.which("chrome"),
            shutil.which("msedge"),
            shutil.which("chromium"),
            shutil.which("google-chrome"),
        ]

        for p in candidates:
            if p and os.path.exists(p):
                return p

        return None

    def check(self) -> Tuple[str, str]:
        """Verifies whether a headless Chromium engine is available locally."""
        if self.browser_path and os.path.exists(self.browser_path):
            browser_name = Path(self.browser_path).name
            return "ok", f"Native Chromium ready ({browser_name})"
        return "warn", "No native Chrome/Edge binary detected; JS-only portals depend on Jina Reader"

    def extract(self, url: str, render_budget_ms: int = 2500) -> Dict[str, Any]:
        """
        Loads the URL in headless Chromium, waits for DOM hydration, and extracts rendered text.
        """
        clean_url = url.strip()
        if not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url

        if not self.browser_path or not os.path.exists(self.browser_path):
            return {
                "url": clean_url,
                "content": "",
                "status": "failed",
                "backend": self.name,
                "error": "No Chromium executable available",
            }

        with tempfile.TemporaryDirectory(prefix="crawler_chrome_") as tmp_user_data:
            # ponytail: Anti-bot stealth args inspired by Scrapling.
            # Omit --disable-extensions (flagged by Cloudflare WAF as automation signal).
            # Add AutomationControlled suppression to unset navigator.webdriver.
            cmd = [
                self.browser_path,
                "--headless=new",
                f"--user-data-dir={tmp_user_data}",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--mute-audio",
                "--no-first-run",
                "--password-store=basic",
                "--disable-search-engine-choice-screen",
                "--disable-blink-features=AutomationControlled",
                "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
                f"--virtual-time-budget={render_budget_ms}",
                "--dump-dom",
                clean_url,
            ]

            try:
                # ponytail: 15s execution timeout with auto-kill to prevent runaway browser zombie processes.
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                )

                if proc.returncode != 0:
                    return {
                        "url": clean_url,
                        "content": "",
                        "status": "failed",
                        "backend": self.name,
                        "error": f"Browser process returned exit code {proc.returncode}",
                    }

                html = proc.stdout
                html_no_scripts = re.sub(
                    r"<(?:script|style|noscript|header|footer|nav|aside)[^>]*>.*?</(?:script|style|noscript|header|footer|nav|aside)>",
                    " ",
                    html,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                raw_text = re.sub(r"<[^>]+>", " ", html_no_scripts)
                clean_text = clean_news_text(raw_text)

                return {
                    "url": clean_url,
                    "content": clean_text,
                    "status": "success",
                    "backend": self.name,
                    "rendered_html_length": len(html),
                }

            except subprocess.TimeoutExpired:
                return {
                    "url": clean_url,
                    "content": "",
                    "status": "failed",
                    "backend": self.name,
                    "error": f"Rendering timed out after {self.timeout}s",
                }
            except Exception as e:
                return {
                    "url": clean_url,
                    "content": "",
                    "status": "failed",
                    "backend": self.name,
                    "error": str(e),
                }

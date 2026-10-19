"""
Firefox Cookie Extractor for Twitter/X Session.
Locates Firefox profile, clones SQLite cookies database to bypass file locks,
and extracts auth_token and ct0 CSRF tokens.
"""

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple


DEFAULT_CONFIG_PATH = Path("config/twitter_session.json")


def find_firefox_profiles() -> list[Path]:
    """Finds all candidate Firefox profile directories containing cookies.sqlite."""
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        base_roaming = Path(appdata) / "Mozilla" / "Firefox" / "Profiles"
        if base_roaming.exists():
            for p in base_roaming.iterdir():
                if p.is_dir() and (p / "cookies.sqlite").exists():
                    candidates.append(p)

    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        base_local = Path(localappdata) / "Mozilla" / "Firefox" / "Profiles"
        if base_local.exists():
            for p in base_local.iterdir():
                if p.is_dir() and (p / "cookies.sqlite").exists():
                    candidates.append(p)

    return candidates


def extract_firefox_twitter_tokens() -> Optional[Dict[str, str]]:
    """
    Extracts auth_token and ct0 from Firefox's cookies.sqlite database.
    Copies the SQLite file to a temporary location first to avoid lock errors while Firefox is running.
    """
    profiles = find_firefox_profiles()
    if not profiles:
        return None

    # Prioritize default or default-release profile
    profiles.sort(key=lambda p: 0 if "default-release" in p.name else (1 if "default" in p.name else 2))

    for profile in profiles:
        cookie_db = profile / "cookies.sqlite"
        if not cookie_db.exists():
            continue

        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Copy to temp to bypass SQLite locked error if browser is currently open
            shutil.copy2(cookie_db, tmp_path)
            conn = sqlite3.connect(str(tmp_path), timeout=10.0)
            cur = conn.cursor()

            query = """
                SELECT name, value FROM moz_cookies
                WHERE (host LIKE '%x.com' OR host LIKE '%twitter.com')
                  AND name IN ('auth_token', 'ct0');
            """
            cur.execute(query)
            tokens = {}
            for row in cur.fetchall():
                tokens[row[0]] = row[1]
            conn.close()

            if "auth_token" in tokens and "ct0" in tokens:
                return {
                    "auth_token": tokens["auth_token"],
                    "ct0": tokens["ct0"],
                    "profile": profile.name,
                    "extracted_at": datetime.now(timezone.utc).isoformat()
                }
        except Exception:
            pass
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    return None


def save_twitter_session(tokens: Dict[str, str], out_path: Path = DEFAULT_CONFIG_PATH) -> bool:
    """Saves extracted tokens to config/twitter_session.json (gitignored)."""
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)
        return True
    except Exception:
        return False


def load_twitter_session(config_path: Path = DEFAULT_CONFIG_PATH) -> Optional[Dict[str, str]]:
    """Loads active session if present."""
    if not config_path.exists():
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("auth_token") and data.get("ct0"):
                return data
    except Exception:
        return None
    return None

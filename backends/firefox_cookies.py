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
from typing import Dict, Optional, Sequence, Tuple


DEFAULT_CONFIG_PATH = Path("config/twitter_session.json")


def find_firefox_profiles() -> list[Path]:
    """Finds all candidate Firefox profile directories containing cookies.sqlite across Windows, Linux, and macOS."""
    candidates = []

    # 0. Custom profile override via env var
    custom_dir = os.environ.get("FIREFOX_PROFILE_DIR")
    if custom_dir:
        cp = Path(custom_dir)
        if cp.exists() and (cp / "cookies.sqlite").exists():
            candidates.append(cp)

    # 1. Windows paths
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

    # 2. Linux paths (~/.mozilla/firefox)
    linux_base = Path.home() / ".mozilla" / "firefox"
    if linux_base.exists():
        for p in linux_base.iterdir():
            if p.is_dir() and (p / "cookies.sqlite").exists():
                candidates.append(p)

    # 3. macOS paths (~/Library/Application Support/Firefox/Profiles)
    mac_base = Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles"
    if mac_base.exists():
        for p in mac_base.iterdir():
            if p.is_dir() and (p / "cookies.sqlite").exists():
                candidates.append(p)

    # Deduplicate while preserving order
    unique_candidates = []
    seen = set()
    for c in candidates:
        resolved = str(c.resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique_candidates.append(c)

    return unique_candidates


def _profile_priority(profile: Path) -> int:
    """Orders profiles so the daily-driver one is read first."""
    if "default-release" in profile.name:
        return 0
    if "default" in profile.name:
        return 1
    return 2


def _read_cookie_rows(
    profile: Path,
    domains: Sequence[str],
    names: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, str]]:
    """Reads matching cookies from one profile, keyed by host then cookie name.

    The database is copied to a temporary file first because Firefox keeps it locked
    while running. Any failure yields an empty mapping rather than raising, so the
    caller can move on to the next profile.
    """
    cookie_db = profile / "cookies.sqlite"
    if not cookie_db.exists() or not domains:
        return {}

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    found: Dict[str, Dict[str, str]] = {}
    try:
        shutil.copy2(cookie_db, tmp_path)
        conn = sqlite3.connect(str(tmp_path), timeout=10.0)
        try:
            conditions = " OR ".join("host LIKE ?" for _ in domains)
            params: list = [f"%{domain}" for domain in domains]
            if names:
                placeholders = ",".join("?" for _ in names)
                conditions = f"({conditions}) AND name IN ({placeholders})"
                params.extend(names)
            query = f"SELECT host, name, value FROM moz_cookies WHERE {conditions};"
            for host, name, value in conn.execute(query, params).fetchall():
                found.setdefault(host, {})[name] = value
        finally:
            conn.close()
    except Exception:
        return {}
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
    return found


def _extract_with_profile(
    domains: Sequence[str],
    names: Optional[Sequence[str]] = None,
    profile: Optional[Path] = None,
) -> Tuple[Dict[str, Dict[str, str]], Optional[str]]:
    """Returns the cookies and the name of the profile they came from."""
    if profile is not None:
        candidate = Path(profile)
        found = _read_cookie_rows(candidate, domains, names)
        return (found, candidate.name) if found else ({}, None)

    profiles = sorted(find_firefox_profiles(), key=_profile_priority)
    for candidate in profiles:
        found = _read_cookie_rows(candidate, domains, names)
        if found:
            return found, candidate.name
    return {}, None


def extract_cookies(
    domains: Sequence[str],
    names: Optional[Sequence[str]] = None,
    profile: Optional[Path] = None,
) -> Dict[str, Dict[str, str]]:
    """Returns {host: {cookie_name: value}} for every cookie matching the domains.

    Domains match as a suffix, so "youtube.com" also picks up ".youtube.com" and
    "www.youtube.com". names, when given, narrows the result to those cookie names.
    """
    cookies, _ = _extract_with_profile(domains, names, profile)
    return cookies


def extract_firefox_twitter_tokens() -> Optional[Dict[str, str]]:
    """
    Extracts auth_token and ct0 from environment variables or Firefox's cookies.sqlite database.
    Checks TWITTER_AUTH_TOKEN & TWITTER_CT0 first for zero-friction headless/CI deployments.
    Copies the SQLite file to a temporary location first to avoid lock errors while Firefox is running.
    """
    # 1. Environment variables priority (Headless / Docker / GitHub Actions)
    env_auth = os.environ.get("TWITTER_AUTH_TOKEN")
    env_ct0 = os.environ.get("TWITTER_CT0")
    if env_auth and env_ct0:
        return {
            "auth_token": env_auth.strip(),
            "ct0": env_ct0.strip(),
            "profile": "environment_variables"
        }

    # 2. Browser profile discovery, same query shape as before but through the generic reader
    found, profile_name = _extract_with_profile(
        ("x.com", "twitter.com"), ("auth_token", "ct0")
    )
    tokens: Dict[str, str] = {}
    for host_cookies in found.values():
        tokens.update(host_cookies)

    if "auth_token" in tokens and "ct0" in tokens:
        return {
            "auth_token": tokens["auth_token"],
            "ct0": tokens["ct0"],
            "profile": profile_name,
            "extracted_at": datetime.now(timezone.utc).isoformat()
        }
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

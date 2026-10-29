"""
Storage for per-domain cookie sessions captured from the local browser profile.

Sessions live in config/sessions/<domain>.json, which is gitignored, so a harvested
cookie never ends up in version control. The values are written verbatim; callers
must not log them.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

DEFAULT_SESSION_DIR = Path("config/sessions")


def session_path(domain: str, directory: Optional[Path] = None) -> Path:
    """Returns the JSON path used for a domain."""
    safe = domain.strip().lower().replace("/", "_").replace(":", "_")
    base = Path(directory) if directory else DEFAULT_SESSION_DIR
    return base / f"{safe}.json"


def save(
    domain: str,
    cookies: Dict[str, str],
    directory: Optional[Path] = None,
    user_agent: Optional[str] = None,
) -> bool:
    """Writes a cookie mapping for one domain. Returns False on any filesystem error."""
    if not domain or not cookies:
        return False
    target = session_path(domain, directory)
    payload = {
        "domain": domain,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "cookies": dict(cookies),
    }
    if user_agent:
        payload["user_agent"] = user_agent
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        return True
    except Exception:
        return False


def load(domain: str, directory: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """Reads the stored cookie mapping for one domain, or None when absent."""
    target = session_path(domain, directory)
    if not target.exists():
        return None
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        cookies = payload.get("cookies")
        if isinstance(cookies, dict) and cookies:
            return {str(key): str(value) for key, value in cookies.items()}
    except Exception:
        return None
    return None


def load_user_agent(domain: str, directory: Optional[Path] = None) -> Optional[str]:
    """Reads the User-Agent captured with a session, or None when it was not recorded.

    A session's cookies and its User-Agent belong together: replaying one without
    the other produces a client no real browser ever sends.
    """
    target = session_path(domain, directory)
    if not target.exists():
        return None
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        user_agent = payload.get("user_agent")
        if isinstance(user_agent, str) and user_agent.strip():
            return user_agent.strip()
    except Exception:
        return None
    return None


def clear(domain: str, directory: Optional[Path] = None) -> bool:
    """Deletes a stored session. Returns True when a file was removed."""
    target = session_path(domain, directory)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False

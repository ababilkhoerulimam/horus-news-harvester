"""
Doctor Diagnostic Module for Indonesian Sports Scrapper.
Mirrors Agent-Reach doctor pattern for zero-side-effect environment health checking.
"""

import sys
import importlib
from typing import Dict, Any, List

from channels import get_all_channels

CORE_PACKAGES = [
    ("requests", "requests"),
    ("beautifulsoup4", "bs4"),
    ("pyyaml", "yaml"),
    ("lxml", "lxml"),
]

OPTIONAL_PACKAGES = [
    ("pandas", "pandas"),
    ("pydantic", "pydantic"),
]


def check_packages() -> Dict[str, Any]:
    """Inspects installed Python libraries."""
    res = {}
    for pkg_name, import_name in CORE_PACKAGES:
        try:
            importlib.import_module(import_name)
            res[pkg_name] = {"status": "ok", "message": "Installed"}
        except ImportError:
            res[pkg_name] = {"status": "warn", "message": f"Missing (pip install {pkg_name})"}

    for pkg_name, import_name in OPTIONAL_PACKAGES:
        try:
            importlib.import_module(import_name)
            res[pkg_name] = {"status": "ok", "message": "Installed"}
        except ImportError:
            res[pkg_name] = {"status": "off", "message": f"Optional missing ({pkg_name})"}

    return res


def run_doctor() -> Dict[str, Any]:
    """Runs complete diagnostic check on all channels and dependencies."""
    channel_results = {}
    for ch in get_all_channels():
        try:
            status, message = ch.check()
            channel_results[ch.name] = {
                "name": ch.description,
                "status": status,
                "message": message,
                "active_backend": ch.active_backend,
                "backends": ch.backends,
                "tier": ch.tier,
            }
        except Exception as e:
            channel_results[ch.name] = {
                "name": ch.description,
                "status": "error",
                "message": f"Check failed: {e}",
                "active_backend": None,
                "backends": ch.backends,
                "tier": ch.tier,
            }

    pkg_results = check_packages()

    return {
        "status": "ok",
        "channels": channel_results,
        "packages": pkg_results,
    }


def format_doctor_report(doc: Dict[str, Any]) -> str:
    """Formats diagnostic report as clean text."""
    lines = [
        "Indonesian Sports Scrapper - Health Report",
        "============================================",
        "Channel Availability:",
    ]
    for ch_name, info in doc.get("channels", {}).items():
        status_sym = "[OK]" if info["status"] == "ok" else "[WARN]"
        lines.append(f"  {status_sym} {info['name']} (Active: {info['active_backend']})")
        lines.append(f"       Details: {info['message']}")

    lines.append("\nDependencies:")
    for pkg, info in doc.get("packages", {}).items():
        status_sym = "[OK]" if info["status"] == "ok" else "[!]"
        lines.append(f"  {status_sym} {pkg}: {info['message']}")

    return "\n".join(lines)


if __name__ == "__main__":
    rep = run_doctor()
    print(format_doctor_report(rep))

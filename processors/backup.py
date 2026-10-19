"""
Cold Storage Backup and Cloud Archive Sync.
Bundles raw JSONL harvests, state databases, and analytical stores into compressed archives.
Supports zero-dependency local gzip tarfiles and S3/R2 presigned URL cloud uploads.
"""

import os
import tarfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_SOURCE_DIRS = ["data/raw", "data/analytics", "data/state"]
DEFAULT_BACKUP_DIR = "data/backups"


def create_backup_archive(
    source_dirs: Optional[List[str]] = None,
    output_dir: str = DEFAULT_BACKUP_DIR,
    archive_name: Optional[str] = None
) -> Path:
    """
    Creates a gzip-compressed tar archive of harvest datasets and databases.
    Preserves relative paths within data directory.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if not archive_name:
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        archive_name = f"sports_backup_{timestamp_str}.tar.gz"

    tar_path = out_path / archive_name
    sources = source_dirs or DEFAULT_SOURCE_DIRS

    with tarfile.open(tar_path, "w:gz") as tar:
        for src in sources:
            p = Path(src)
            if p.exists():
                arcname = p.name
                tar.add(p, arcname=arcname)

    return tar_path


def upload_to_presigned_url(archive_path: Path, presigned_url: str, timeout: int = 60) -> bool:
    """
    Uploads backup archive to AWS S3, Cloudflare R2, or MinIO via HTTP PUT presigned URL.
    Zero-dependency stdlib urllib implementation.
    """
    if not archive_path.exists():
        return False

    file_size = archive_path.stat().st_size
    with open(archive_path, "rb") as f:
        data = f.read()

    req = urllib.request.Request(
        presigned_url,
        data=data,
        method="PUT",
        headers={
            "Content-Type": "application/gzip",
            "Content-Length": str(file_size)
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def run_backup(
    output_dir: str = DEFAULT_BACKUP_DIR,
    upload_url: Optional[str] = None,
    source_dirs: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Executes full backup workflow: local gzip archiving + optional cloud sync.
    """
    # Check env var for cloud upload URL if not explicitly provided
    target_url = upload_url or os.environ.get("BACKUP_S3_PRESIGNED_URL")

    try:
        archive_path = create_backup_archive(source_dirs=source_dirs, output_dir=output_dir)
        size_bytes = archive_path.stat().st_size
        size_mb = round(size_bytes / (1024 * 1024), 2)

        uploaded = False
        if target_url:
            uploaded = upload_to_presigned_url(archive_path, target_url)

        return {
            "status": "success",
            "archive_path": str(archive_path),
            "size_bytes": size_bytes,
            "size_mb": size_mb,
            "cloud_uploaded": uploaded,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

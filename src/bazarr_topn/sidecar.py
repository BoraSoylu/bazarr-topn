"""Sidecar metadata files for tracking topn download state."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bazarr_topn.config import Config

logger = logging.getLogger(__name__)


SCHEMA_VERSION = 2


@dataclass
class SidecarData:
    target: int
    saved: int
    available: int
    clean: bool
    completed_at: str | None = None
    search_ok: bool = True
    schema_version: int = SCHEMA_VERSION


def sidecar_path(video_path: str | Path, lang: str) -> Path:
    """Build the sidecar JSON path for a video+language pair."""
    video = Path(video_path)
    return video.parent / f"{video.stem}.{lang}.topn.json"


def write_sidecar(video_path: str | Path, lang: str, data: SidecarData) -> Path:
    """Write sidecar JSON. Stamps current schema_version and completed_at."""
    if data.completed_at is None:
        data.completed_at = datetime.now(timezone.utc).isoformat()
    data.schema_version = SCHEMA_VERSION
    path = sidecar_path(video_path, lang)
    path.write_text(json.dumps(asdict(data), indent=2) + "\n")
    logger.debug("Wrote sidecar %s", path.name)
    return path


def read_sidecar(video_path: str | Path, lang: str) -> SidecarData | None:
    """Read sidecar JSON. Returns None if missing, corrupt, or missing v1 fields.

    Tolerates missing v2-only fields (schema_version, search_ok): legacy v1
    sidecars load with schema_version=1 and search_ok=False, which is_topn_done
    will reject so they get rewritten on the next scan.
    """
    path = sidecar_path(video_path, lang)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        for key in ("target", "saved", "available", "clean", "completed_at"):
            if key not in raw:
                logger.debug("Sidecar %s missing field '%s', treating as absent", path.name, key)
                return None
        return SidecarData(
            target=raw["target"],
            saved=raw["saved"],
            available=raw["available"],
            clean=raw["clean"],
            completed_at=raw["completed_at"],
            # Pessimistic defaults for legacy v1 files: pre-v2 code wrote
            # clean=True even on rate-limited 0-result runs, so an absent
            # search_ok must not be trusted. is_topn_done relies on these
            # defaults to reject v1 sidecars and force a rewrite.
            search_ok=raw.get("search_ok", False),
            schema_version=raw.get("schema_version", 1),
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.debug("Corrupt sidecar %s, treating as absent", path.name)
        return None


def delete_sidecar(video_path: str | Path, lang: str) -> None:
    """Delete sidecar file if it exists."""
    path = sidecar_path(video_path, lang)
    if path.exists():
        path.unlink()
        logger.debug("Deleted sidecar %s", path.name)


def is_topn_done(video_path: str | Path, lang: str, config: Config) -> bool:
    """Check if a video+language pair has a valid, complete sidecar.

    Returns True only if:
    1. Sidecar feature is enabled
    2. Sidecar exists and is parseable
    3. schema_version >= 2 (v1 legacy always rejected)
    4. search_ok is True (search actually completed)
    5. clean is True (all attempted downloads succeeded)
    6. saved >= config.top_n (we already saved the full target)

    Once a video has the full top_n saved cleanly, it is considered done
    forever — no re-search, no re-download. Partial sidecars (saved < top_n)
    are re-searched every scan; download is suppressed at that layer when
    the search returns no new candidates beyond what is already saved.
    """
    if not config.topn_sidecar_enabled:
        return False

    data = read_sidecar(video_path, lang)
    if data is None:
        return False

    try:
        if int(data.schema_version) < SCHEMA_VERSION:
            return False
    except (TypeError, ValueError):
        return False

    if not data.search_ok:
        return False

    if not data.clean:
        return False

    if data.saved < config.top_n:
        return False

    return True

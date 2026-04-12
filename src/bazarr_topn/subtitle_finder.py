"""Subtitle discovery, scoring, and download using subliminal."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from babelfish import Language
from subliminal import (
    Video,
    region,
)
from subliminal.core import ProviderPool
from subliminal.score import compute_score
from subliminal.subtitle import Subtitle

from bazarr_topn.config import Config
from bazarr_topn.naming import subtitle_path

logger = logging.getLogger(__name__)


def configure_cache() -> None:
    """Set up subliminal's dogpile cache (file-based)."""
    region.configure(
        "dogpile.cache.dbm",
        arguments={"filename": str(Path(tempfile.gettempdir()) / "bazarr_topn_subliminal_cache.dbm")},
        replace_existing_backend=True,
    )


@dataclass
class ScoredSubtitle:
    subtitle: Subtitle
    score: int
    provider: str


def scan_video(video_path: str | Path) -> Video:
    """Scan a video file and return a subliminal Video object."""
    from subliminal import scan_video as _scan

    return _scan(str(video_path))


def find_subtitles(
    video: Video,
    language: Language,
    config: Config,
) -> list[ScoredSubtitle]:
    """Find and score all available subtitles for a video+language.

    Returns subtitles sorted by score descending.
    """
    provider_names = config.provider_names or None
    provider_configs = config.provider_configs or {}

    with ProviderPool(
        providers=provider_names,
        provider_configs=provider_configs,
    ) as pool:
        raw_subs = pool.list_subtitles(video, {language})

    scored: list[ScoredSubtitle] = []
    for sub in raw_subs:
        try:
            score = compute_score(sub, video)
        except Exception:
            score = 0
        scored.append(
            ScoredSubtitle(
                subtitle=sub,
                score=score,
                provider=sub.provider_name,
            )
        )

    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


def download_top_n(
    video: Video,
    video_path: str | Path,
    language: Language,
    config: Config,
    downloads_remaining: int | None = None,
) -> list[Path]:
    """Download the top N subtitles for a video, returning paths of saved files.

    Args:
        video: Subliminal Video object.
        video_path: Path to the video file on disk.
        language: Target language.
        config: Application config.
        downloads_remaining: If set, cap downloads at this number.

    Returns:
        List of paths to downloaded subtitle files.
    """
    lang_str = str(language)

    candidates = find_subtitles(video, language, config)

    # Filter by minimum score
    if config.min_score > 0:
        candidates = [s for s in candidates if s.score >= config.min_score]

    if not candidates:
        logger.info("No subtitles found for %s [%s]", video_path, lang_str)
        return []

    # Cap at top_n (or downloads_remaining if lower)
    limit = config.top_n
    if downloads_remaining is not None:
        limit = min(limit, downloads_remaining)
    candidates = candidates[:limit]

    logger.info(
        "Found %d candidates for %s [%s], downloading top %d",
        len(candidates),
        video_path,
        lang_str,
        len(candidates),
    )

    saved: list[Path] = []
    provider_names = config.provider_names or None
    provider_configs = config.provider_configs or {}

    with ProviderPool(
        providers=provider_names,
        provider_configs=provider_configs,
    ) as pool:
        for i, scored in enumerate(candidates):
            rank = i + 2  # rank 1 = Bazarr's subtitle
            try:
                pool.download_subtitle(scored.subtitle)
                if scored.subtitle.content is None:
                    logger.warning("Empty subtitle content for rank %d, skipping", rank)
                    continue

                out_path = subtitle_path(
                    video_path, lang_str, rank, config.naming_pattern
                )
                out_path.write_bytes(scored.subtitle.content)
                logger.info(
                    "  rank %d: score=%d provider=%s → %s",
                    rank,
                    scored.score,
                    scored.provider,
                    out_path.name,
                )
                saved.append(out_path)
            except Exception:
                logger.exception("Failed to download rank %d subtitle", rank)

    return saved

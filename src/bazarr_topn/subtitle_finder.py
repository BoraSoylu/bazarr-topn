"""Subtitle discovery, scoring, and download using subliminal."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from babelfish import Language
from subliminal import (
    Video,
    refine,
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
    """Scan a video file and return a subliminal Video object.

    Runs refiners (hash, metadata, omdb, tmdb, tvdb) to populate
    IMDB ID, TMDB ID, year, and other metadata needed for precise
    subtitle searches.
    """
    from subliminal import scan_video as _scan

    video = _scan(str(video_path))
    refine(video)
    logger.debug(
        "Scanned %s: title=%r year=%s imdb_id=%s",
        video_path,
        getattr(video, "title", None),
        getattr(video, "year", None),
        video.imdb_id,
    )
    return video


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

    logger.info("  Searching subtitles [%s]...", lang_str)
    candidates = find_subtitles(video, language, config)

    # Filter by minimum score
    unfiltered_count = len(candidates)
    if config.min_score > 0:
        candidates = [s for s in candidates if s.score >= config.min_score]

    if not candidates:
        logger.info("  No subtitles found for [%s] (%d below min_score=%d)",
                     lang_str, unfiltered_count, config.min_score)
        return []

    # Cap at top_n (or downloads_remaining if lower)
    limit = config.top_n
    if downloads_remaining is not None:
        limit = min(limit, downloads_remaining)
    candidates = candidates[:limit]

    logger.info("  Found %d candidates [%s], downloading %d...",
                unfiltered_count, lang_str, len(candidates))

    saved: list[Path] = []
    skipped = 0
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
                    logger.debug("Empty subtitle content for rank %d, skipping", rank)
                    skipped += 1
                    continue

                out_path = subtitle_path(
                    video_path, lang_str, rank, config.naming_pattern
                )
                out_path.write_bytes(scored.subtitle.content)
                logger.debug(
                    "  rank %d: score=%d provider=%s -> %s",
                    rank, scored.score, scored.provider, out_path.name,
                )
                saved.append(out_path)
            except Exception:
                logger.debug("Failed to download rank %d subtitle", rank, exc_info=True)
                skipped += 1

    logger.info("  Downloaded %d subtitles [%s]%s",
                len(saved), lang_str,
                f" ({skipped} skipped)" if skipped else "")

    return saved

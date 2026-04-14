"""Subtitle discovery, scoring, and download using subliminal."""

from __future__ import annotations

import logging
import tempfile
import time
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


def create_pool(config: Config) -> ProviderPool:
    """Create a ProviderPool from config. Caller must use as context manager or call terminate()."""
    return ProviderPool(
        providers=config.provider_names or None,
        provider_configs=config.provider_configs or {},
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
    # Skip omdb refiner — subliminal's built-in OMDB API key is expired,
    # causing 401 tracebacks on every video. tmdb provides the same metadata.
    refine(video, refiners=("hash", "metadata", "tmdb", "tvdb"))
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
    pool: ProviderPool,
    config: Config | None = None,
) -> list[ScoredSubtitle]:
    """Find and score all available subtitles for a video+language.

    If the pool discards providers mid-call (typically OpenSubtitles
    "Too Many Requests"), we sleep with exponential backoff, un-discard
    the provider, and retry up to `config.rate_limit_retries` times.

    Returns subtitles sorted by score descending.
    """
    retries = config.rate_limit_retries if config is not None else 0
    backoff = config.rate_limit_initial_backoff if config is not None else 0.0

    raw_subs: list = []
    for attempt in range(retries + 1):
        before = set(pool.discarded_providers)
        raw_subs = pool.list_subtitles(video, {language})
        newly_discarded = set(pool.discarded_providers) - before
        if not newly_discarded or attempt >= retries:
            break
        sleep_s = backoff * (2 ** attempt)
        logger.warning(
            "Providers %s discarded during search (likely rate-limited). "
            "Sleeping %.0fs before retry %d/%d",
            sorted(newly_discarded), sleep_s, attempt + 1, retries,
        )
        if sleep_s > 0:
            time.sleep(sleep_s)
        for p in newly_discarded:
            pool.discarded_providers.discard(p)

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


def _download_with_retry(
    pool: ProviderPool,
    subtitle: Subtitle,
    retries: int,
    initial_backoff: float,
) -> bool:
    """Download a single subtitle, retrying if the pool discards its provider.

    Subliminal's ProviderPool catches any exception from a provider's
    download_subtitle and adds the provider to `discarded_providers`,
    which poisons every subsequent call for the pool's lifetime. For
    OpenSubtitles 429s this collapses a whole scan after the first rate
    limit hit. We detect the discard, clear it, sleep, and retry.
    """
    for attempt in range(retries + 1):
        before = set(pool.discarded_providers)
        pool.download_subtitle(subtitle)
        if subtitle.content is not None:
            return True
        newly_discarded = set(pool.discarded_providers) - before
        if not newly_discarded or attempt >= retries:
            return False
        sleep_s = initial_backoff * (2 ** attempt)
        logger.warning(
            "Provider %s discarded on download (likely rate-limited). "
            "Sleeping %.0fs before retry %d/%d",
            subtitle.provider_name, sleep_s, attempt + 1, retries,
        )
        if sleep_s > 0:
            time.sleep(sleep_s)
        for p in newly_discarded:
            pool.discarded_providers.discard(p)
    return False


def download_top_n(
    video: Video,
    video_path: str | Path,
    language: Language,
    config: Config,
    pool: ProviderPool,
    downloads_remaining: int | None = None,
) -> list[Path]:
    """Download the top N subtitles for a video, returning paths of saved files.

    Args:
        video: Subliminal Video object.
        video_path: Path to the video file on disk.
        language: Target language.
        config: Application config.
        pool: Shared ProviderPool (single login, reused across videos).
        downloads_remaining: If set, cap downloads at this number.

    Returns:
        List of paths to downloaded subtitle files.
    """
    lang_str = str(language)

    if config.search_delay > 0:
        time.sleep(config.search_delay)
    logger.info("  Searching subtitles [%s]...", lang_str)
    candidates = find_subtitles(video, language, pool, config=config)

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

    for i, scored in enumerate(candidates):
        rank = i + 2  # rank 1 = Bazarr's subtitle
        if i > 0 and config.download_delay > 0:
            time.sleep(config.download_delay)
        try:
            ok = _download_with_retry(
                pool,
                scored.subtitle,
                retries=config.rate_limit_retries,
                initial_backoff=config.rate_limit_initial_backoff,
            )
            if not ok or scored.subtitle.content is None:
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

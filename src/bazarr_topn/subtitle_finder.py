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


class SearchUnavailable(Exception):
    """Raised when find_subtitles exhausts retries with a discarded provider.

    Distinct from 'search returned empty' — this means the search never
    completed successfully (rate limit, network error at all attempts).
    """


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


@dataclass
class DownloadResult:
    saved_paths: list[Path]
    clean: bool
    available_count: int
    search_ok: bool = True


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
        if not newly_discarded:
            break
        if attempt >= retries:
            # Exhausted retries with provider still being discarded — the
            # search never completed. Signal so the caller writes search_ok=False.
            raise SearchUnavailable(
                f"Providers {sorted(newly_discarded)} stayed discarded after "
                f"{retries + 1} attempts for language {language}"
            )
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
) -> DownloadResult:
    """Download the top N subtitles for a video, iterating deeper on failures.

    Iterates through candidates beyond top_n when downloads fail (empty content,
    invalid data). Stops when saved_count reaches the target or all candidates
    (up to max_candidates_tried) are exhausted.

    Returns:
        DownloadResult with saved paths, clean flag, and available count.
    """
    lang_str = str(language)

    if config.search_delay > 0:
        time.sleep(config.search_delay)
    logger.info("  Searching subtitles [%s]...", lang_str)
    try:
        candidates = find_subtitles(video, language, pool, config=config)
    except SearchUnavailable as e:
        logger.warning(
            "  Search unavailable for [%s] (%s); will retry next scan",
            lang_str, e,
        )
        return DownloadResult(
            saved_paths=[], clean=False, available_count=0, search_ok=False,
        )

    # Filter by minimum score
    unfiltered_count = len(candidates)
    if config.min_score > 0:
        candidates = [s for s in candidates if s.score >= config.min_score]

    if not candidates:
        if unfiltered_count == 0:
            logger.info("  No candidates returned for [%s] from any provider", lang_str)
        else:
            logger.info("  No subtitles passed min_score=%d for [%s] (%d candidates filtered out)",
                         config.min_score, lang_str, unfiltered_count)
        return DownloadResult(
            saved_paths=[], clean=True, available_count=unfiltered_count, search_ok=True,
        )

    # Cap candidates at max_candidates_tried
    max_try = config.max_candidates_tried
    if max_try > 0 and len(candidates) > max_try:
        candidates = candidates[:max_try]

    # Target: how many we want to save
    target = config.top_n
    if downloads_remaining is not None:
        target = min(target, downloads_remaining)

    logger.info("  Found %d candidates [%s], downloading up to %d...",
                unfiltered_count, lang_str, target)

    saved: list[Path] = []
    rank = 2  # rank 1 = Bazarr's subtitle
    clean = True
    attempted = 0

    for scored in candidates:
        if len(saved) >= target:
            break

        if attempted > 0 and config.download_delay > 0:
            time.sleep(config.download_delay)
        attempted += 1

        try:
            ok = _download_with_retry(
                pool,
                scored.subtitle,
                retries=config.rate_limit_retries,
                initial_backoff=config.rate_limit_initial_backoff,
            )
            if not ok or scored.subtitle.content is None:
                logger.debug("Empty subtitle content for candidate (rank slot %d), trying deeper", rank)
                if scored.subtitle.provider_name in pool.discarded_providers:
                    clean = False
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
            rank += 1
        except Exception:
            logger.debug("Failed to download candidate subtitle", exc_info=True)
            clean = False
            continue

    logger.info("  Downloaded %d subtitles [%s]%s",
                len(saved), lang_str,
                f" ({attempted - len(saved)} skipped)" if len(saved) < attempted else "")

    return DownloadResult(
        saved_paths=saved,
        clean=clean,
        available_count=unfiltered_count,
        search_ok=True,
    )

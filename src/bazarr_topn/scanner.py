"""Scan and process video files for subtitle downloads."""

from __future__ import annotations

import logging
from pathlib import Path

from babelfish import Language
from subliminal.core import ProviderPool

from bazarr_topn.config import Config
from bazarr_topn.naming import clean_existing_topn, existing_topn_subs
from bazarr_topn.subtitle_finder import configure_cache, create_pool, download_top_n, scan_video
from bazarr_topn.sync import sync_batch

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts",
}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS and path.is_file()


def find_videos(paths: list[str | Path]) -> list[Path]:
    """Recursively find video files in the given paths."""
    videos: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_file() and is_video(p):
            videos.append(p)
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if is_video(child):
                    videos.append(child)
    return videos


def process_video(
    video_path: Path,
    config: Config,
    pool: ProviderPool,
    downloads_remaining: int | None = None,
    force: bool = False,
) -> int:
    """Process a single video: find, download, and optionally sync subtitles.

    Returns the number of subtitles downloaded, or -1 if skipped.
    """
    # Skip if all languages already have topn subs (unless --force)
    if not force:
        langs_with_subs = [
            lang for lang in config.languages
            if existing_topn_subs(video_path, lang, config.naming_pattern)
        ]
        if len(langs_with_subs) == len(config.languages):
            return -1  # signal: skipped

    logger.info("%s", video_path.name)

    try:
        video = scan_video(video_path)
    except Exception:
        logger.error("  Failed to scan video: %s", video_path)
        logger.debug("Scan error details:", exc_info=True)
        return 0

    total_downloaded = 0
    all_saved: list[Path] = []

    for lang_code in config.languages:
        language = Language.fromalpha2(lang_code)

        # Skip this language if it already has topn subs (unless --force)
        if not force and existing_topn_subs(video_path, lang_code, config.naming_pattern):
            logger.debug("  Skipping [%s] — already has topn subs", lang_code)
            continue

        # Clean previous topn subs for this video+lang (only reached with --force)
        removed = clean_existing_topn(video_path, lang_code, config.naming_pattern)
        if removed:
            logger.debug("Cleaned %d old topn subs for %s [%s]", removed, video_path.name, lang_code)

        per_lang_remaining = None
        if downloads_remaining is not None:
            per_lang_remaining = downloads_remaining - total_downloaded
            if per_lang_remaining <= 0:
                logger.info("  Download limit reached, stopping")
                break

        result = download_top_n(video, video_path, language, config, pool, per_lang_remaining)
        total_downloaded += len(result.saved_paths)
        all_saved.extend(result.saved_paths)

    # Sync all subtitles in one batch — extracts speech from video once
    if all_saved and config.ffsubsync.enabled:
        synced = sync_batch(video_path, all_saved, config.ffsubsync)
        logger.info("  Synced %d/%d subtitles", synced, len(all_saved))

    return total_downloaded


def scan(paths: list[str | Path], config: Config, force: bool = False) -> dict[str, int]:
    """Scan paths and process all videos found.

    Returns a summary dict with counts.
    """
    configure_cache()

    videos = find_videos(paths)
    logger.info("Found %d video files", len(videos))

    total_downloaded = 0
    total_processed = 0
    total_skipped_existing = 0
    total_skipped_limit = 0

    downloads_remaining = config.max_downloads_per_cycle if config.max_downloads_per_cycle > 0 else None

    # Single ProviderPool for the entire scan — one login, reused across all videos.
    # OpenSubtitles JWT is valid for 24h, so this avoids hitting the 5 login/IP limit.
    with create_pool(config) as pool:
        for video_path in videos:
            count = process_video(video_path, config, pool, downloads_remaining, force=force)
            if count == -1:
                # Skipped — already has topn subs
                total_skipped_existing += 1
                continue
            total_downloaded += count
            total_processed += 1

            if downloads_remaining is not None:
                downloads_remaining -= count
                if downloads_remaining <= 0:
                    total_skipped_limit = len(videos) - total_processed - total_skipped_existing
                    logger.warning(
                        "Download limit reached after %d files (%d skipped)",
                        total_processed,
                        total_skipped_limit,
                    )
                    break

    if total_skipped_existing:
        logger.info("Skipped %d videos with existing topn subs", total_skipped_existing)

    return {
        "videos_found": len(videos),
        "videos_processed": total_processed,
        "videos_skipped": total_skipped_limit,
        "videos_skipped_existing": total_skipped_existing,
        "subtitles_downloaded": total_downloaded,
    }

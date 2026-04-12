"""Scan and process video files for subtitle downloads."""

from __future__ import annotations

import logging
from pathlib import Path

from babelfish import Language

from bazarr_topn.config import Config
from bazarr_topn.naming import clean_existing_topn, existing_topn_subs
from bazarr_topn.subtitle_finder import configure_cache, download_top_n, scan_video
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
    downloads_remaining: int | None = None,
    force: bool = False,
) -> int:
    """Process a single video: find, download, and optionally sync subtitles.

    Returns the number of subtitles downloaded.
    """
    # Skip if all languages already have topn subs (unless --force)
    if not force:
        all_have_subs = all(
            existing_topn_subs(video_path, lang, config.naming_pattern)
            for lang in config.languages
        )
        if all_have_subs:
            logger.debug("Skipping %s — topn subs already exist", video_path.name)
            return 0

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

        saved = download_top_n(video, video_path, language, config, per_lang_remaining)
        total_downloaded += len(saved)
        all_saved.extend(saved)

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
    logger.info("Found %d video files to process", len(videos))

    total_downloaded = 0
    total_processed = 0
    total_skipped = 0

    downloads_remaining = config.max_downloads_per_cycle if config.max_downloads_per_cycle > 0 else None

    for video_path in videos:
        count = process_video(video_path, config, downloads_remaining, force=force)
        total_downloaded += count
        total_processed += 1

        if downloads_remaining is not None:
            downloads_remaining -= count
            if downloads_remaining <= 0:
                total_skipped = len(videos) - total_processed
                logger.warning(
                    "Download limit reached after %d files (%d skipped)",
                    total_processed,
                    total_skipped,
                )
                break

    return {
        "videos_found": len(videos),
        "videos_processed": total_processed,
        "videos_skipped": total_skipped,
        "subtitles_downloaded": total_downloaded,
    }

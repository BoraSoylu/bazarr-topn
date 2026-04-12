"""ffsubsync wrapper for subtitle timing correction."""

from __future__ import annotations

import logging
from pathlib import Path

from bazarr_topn.config import FfsubsyncConfig

logger = logging.getLogger(__name__)


def is_available() -> bool:
    """Check if ffsubsync is importable."""
    try:
        import ffsubsync  # noqa: F401

        return True
    except ImportError:
        return False


def sync_subtitle(
    video_path: str | Path,
    subtitle_path: str | Path,
    config: FfsubsyncConfig,
) -> bool:
    """Run ffsubsync to correct subtitle timing against the video.

    Syncs in-place (overwrites the subtitle file).

    Returns:
        True if sync succeeded, False otherwise.
    """
    if not config.enabled:
        return False

    if not is_available():
        logger.warning("ffsubsync not installed, skipping sync")
        return False

    from ffsubsync.ffsubsync import make_parser, run

    video_path = Path(video_path)
    subtitle_path = Path(subtitle_path)
    tmp_out = subtitle_path.with_suffix(".synced.srt")

    args_list = [
        str(video_path),
        "-i", str(subtitle_path),
        "-o", str(tmp_out),
    ]
    if config.gss:
        args_list.append("--gss")
    if config.vad:
        args_list.extend(["--vad", config.vad])
    if config.max_offset_seconds is not None:
        args_list.extend(["--max-offset-seconds", str(config.max_offset_seconds)])
    if config.no_fix_framerate:
        args_list.append("--no-fix-framerate")
    if config.reference_stream:
        args_list.extend(["--reference-stream", config.reference_stream])
    if config.extra_args:
        args_list.extend(config.extra_args)

    logger.debug("ffsubsync args: %s", args_list)

    try:
        parser = make_parser()
        args = parser.parse_args(args_list)
        result = run(args)

        if result.get("retval") == 0 and tmp_out.exists():
            tmp_out.replace(subtitle_path)
            offset = result.get("offset_seconds")
            framerate = result.get("framerate_scale_factor")
            logger.info(
                "Synced: %s (offset=%.2fs, framerate_scale=%.4f)",
                subtitle_path.name,
                offset if offset is not None else 0.0,
                framerate if framerate is not None else 1.0,
            )
            return True
        else:
            logger.warning(
                "ffsubsync failed (retval=%s) for %s",
                result.get("retval"),
                subtitle_path.name,
            )
            if tmp_out.exists():
                tmp_out.unlink()
            return False
    except Exception:
        logger.exception("ffsubsync error for %s", subtitle_path.name)
        if tmp_out.exists():
            tmp_out.unlink()
        return False


def sync_batch(
    video_path: str | Path,
    subtitle_paths: list[Path],
    config: FfsubsyncConfig,
) -> int:
    """Sync a batch of subtitles against a video. Returns count of successful syncs."""
    if not config.enabled:
        return 0
    count = 0
    for sp in subtitle_paths:
        if sync_subtitle(video_path, sp, config):
            count += 1
    return count

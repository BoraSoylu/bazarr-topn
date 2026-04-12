"""ffsubsync wrapper for subtitle timing correction."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from bazarr_topn.config import FfsubsyncConfig

logger = logging.getLogger(__name__)


def is_available() -> bool:
    """Check if ffsubsync is installed."""
    return shutil.which("ffsubsync") is not None


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

    video_path = Path(video_path)
    subtitle_path = Path(subtitle_path)
    tmp_out = subtitle_path.with_suffix(".synced.srt")

    cmd = [
        "ffsubsync",
        str(video_path),
        "-i", str(subtitle_path),
        "-o", str(tmp_out),
    ]
    if config.gss:
        cmd.append("--gss")
    if config.vad:
        cmd.extend(["--vad", config.vad])
    if config.max_offset_seconds is not None:
        cmd.extend(["--max-offset-seconds", str(config.max_offset_seconds)])
    if config.no_fix_framerate:
        cmd.append("--no-fix-framerate")
    if config.reference_stream:
        cmd.extend(["--reference-stream", config.reference_stream])
    if config.extra_args:
        cmd.extend(config.extra_args)

    logger.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0 and tmp_out.exists():
            tmp_out.replace(subtitle_path)
            logger.info("Synced: %s", subtitle_path.name)
            return True
        else:
            logger.warning(
                "ffsubsync failed (rc=%d) for %s: %s",
                result.returncode,
                subtitle_path.name,
                result.stderr[:500] if result.stderr else "",
            )
            if tmp_out.exists():
                tmp_out.unlink()
            return False
    except subprocess.TimeoutExpired:
        logger.error("ffsubsync timed out for %s", subtitle_path.name)
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

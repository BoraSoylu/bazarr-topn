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


def _build_args(
    reference: str,
    subtitle_path: Path,
    tmp_out: Path,
    config: FfsubsyncConfig,
    *,
    serialize_speech: bool = False,
) -> list[str]:
    """Build the ffsubsync argument list."""
    args_list = [
        reference,
        "-i", str(subtitle_path),
        "-o", str(tmp_out),
    ]
    if serialize_speech:
        args_list.append("--serialize-speech")
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
    return args_list


def _run_sync(
    reference: str,
    subtitle_path: Path,
    config: FfsubsyncConfig,
    *,
    serialize_speech: bool = False,
) -> bool:
    """Run a single ffsubsync invocation. Returns True on success."""
    from ffsubsync.ffsubsync import make_parser, run

    tmp_out = subtitle_path.with_suffix(".synced.srt")
    args_list = _build_args(
        reference, subtitle_path, tmp_out, config, serialize_speech=serialize_speech
    )
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
    return _run_sync(str(video_path), Path(subtitle_path), config)


def sync_batch(
    video_path: str | Path,
    subtitle_paths: list[Path],
    config: FfsubsyncConfig,
) -> int:
    """Sync a batch of subtitles against a video.

    Extracts the speech reference (VAD) from the video once on the first
    subtitle, saves it as a .npz file, then reuses it for all remaining
    subtitles. This avoids re-extracting audio for every subtitle.

    Returns count of successful syncs.
    """
    if not config.enabled:
        return 0
    if not subtitle_paths:
        return 0
    if not is_available():
        logger.warning("ffsubsync not installed, skipping sync")
        return 0

    video_path = Path(video_path)
    speech_npz = video_path.with_suffix(".npz")
    count = 0
    used_cache = False

    try:
        for i, sp in enumerate(subtitle_paths):
            if i == 0:
                # First subtitle: extract speech from video and serialize to .npz
                logger.info("Extracting speech reference from %s (this takes a while)...", video_path.name)
                ok = _run_sync(str(video_path), sp, config, serialize_speech=True)
                if ok:
                    count += 1
                if speech_npz.exists():
                    used_cache = True
                    logger.info("Speech reference cached at %s", speech_npz.name)
                else:
                    logger.warning("Speech cache not created, subsequent syncs will re-extract")
            else:
                # Subsequent subtitles: use cached .npz as reference (fast)
                reference = str(speech_npz) if used_cache else str(video_path)
                if _run_sync(reference, sp, config):
                    count += 1
    finally:
        # Clean up the .npz cache file
        if speech_npz.exists():
            speech_npz.unlink()
            logger.debug("Cleaned up speech cache %s", speech_npz.name)

    return count

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
) -> dict:
    """Run a single ffsubsync invocation.

    Returns a dict with 'ok' (bool), 'offset' (float|None), 'scale' (float|None).
    """
    from ffsubsync.ffsubsync import make_parser, run

    tmp_out = subtitle_path.with_suffix(".synced.srt")
    args_list = _build_args(
        reference, subtitle_path, tmp_out, config, serialize_speech=serialize_speech
    )
    logger.debug("ffsubsync args: %s", args_list)

    # Suppress harmless "Input audio chunk is too short" tracebacks from silero VAD.
    # ffsubsync.speech_transformers logs these at ERROR with full traceback via
    # logger.exception(), but they're expected at audio boundaries and not actionable.
    _speech_logger = logging.getLogger("ffsubsync.speech_transformers")
    _prev_level = _speech_logger.level
    _speech_logger.setLevel(logging.CRITICAL)

    try:
        parser = make_parser()
        args = parser.parse_args(args_list)
        result = run(args)

        if result.get("retval") == 0 and tmp_out.exists():
            tmp_out.replace(subtitle_path)
            return {
                "ok": True,
                "offset": result.get("offset_seconds"),
                "scale": result.get("framerate_scale_factor"),
            }
        else:
            logger.debug("ffsubsync failed (retval=%s) for %s",
                         result.get("retval"), subtitle_path.name)
            if tmp_out.exists():
                tmp_out.unlink()
            return {"ok": False, "offset": None, "scale": None}
    except Exception:
        logger.debug("ffsubsync error for %s", subtitle_path.name, exc_info=True)
        if tmp_out.exists():
            tmp_out.unlink()
        return {"ok": False, "offset": None, "scale": None}
    finally:
        _speech_logger.setLevel(_prev_level)


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
    result = _run_sync(str(video_path), Path(subtitle_path), config)
    return result["ok"]


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
    total = len(subtitle_paths)
    count = 0
    used_cache = False

    # Build a description of sync settings for the header
    vad_name = config.vad or "webrtc"
    extras = []
    if config.gss:
        extras.append("GSS")
    label = f"{vad_name} VAD" + (f" + {', '.join(extras)}" if extras else "")
    logger.info("  Syncing %d subtitles (%s)...", total, label)

    try:
        for i, sp in enumerate(subtitle_paths):
            idx = i + 1
            if i == 0:
                # First subtitle: extract speech from video and serialize to .npz
                result = _run_sync(str(video_path), sp, config, serialize_speech=True)
                if speech_npz.exists():
                    used_cache = True
                    logger.debug("Speech reference cached at %s", speech_npz.name)
                else:
                    logger.debug("Speech cache not created, subsequent syncs will re-extract")
            else:
                # Subsequent subtitles: use cached .npz as reference (fast)
                reference = str(speech_npz) if used_cache else str(video_path)
                result = _run_sync(reference, sp, config)

            if result["ok"]:
                count += 1
                offset = result["offset"] or 0.0
                scale = result["scale"] or 1.0
                logger.info("    [%d/%d] %s  offset=%+.1fs scale=%.3f",
                            idx, total, sp.name, offset, scale)
            else:
                logger.info("    [%d/%d] %s  FAILED", idx, total, sp.name)
    finally:
        # Clean up the .npz cache file
        if speech_npz.exists():
            speech_npz.unlink()
            logger.debug("Cleaned up speech cache %s", speech_npz.name)

    return count

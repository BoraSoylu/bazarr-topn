"""Subtitle file naming with rank suffixes."""

from __future__ import annotations

from pathlib import Path


def subtitle_path(
    video_path: str | Path,
    lang: str,
    rank: int,
    pattern: str = "{video_stem}.{lang}.topn-{rank}.srt",
) -> Path:
    """Build the output path for a ranked subtitle file.

    Args:
        video_path: Path to the video file.
        lang: ISO 639-1 language code (e.g. "en").
        rank: Subtitle rank (2 = first from us, since Bazarr owns rank 1).
        pattern: Naming pattern with {video_stem}, {lang}, {rank} placeholders.

    Returns:
        Full path to the subtitle file, in the same directory as the video.
    """
    video = Path(video_path)
    filename = pattern.format(video_stem=video.stem, lang=lang, rank=f"{rank:02d}")
    return video.parent / filename


def existing_topn_subs(video_path: str | Path, lang: str, pattern: str) -> list[Path]:
    """Find existing topn subtitle files for a video+language pair."""
    video = Path(video_path)
    # Build a glob from the pattern by replacing {rank} with *
    glob_pattern = pattern.format(video_stem=video.stem, lang=lang, rank="*")
    return sorted(video.parent.glob(glob_pattern))


def clean_existing_topn(video_path: str | Path, lang: str, pattern: str) -> int:
    """Remove existing topn subtitle files and sidecar. Returns count of sub files removed."""
    from bazarr_topn.sidecar import delete_sidecar

    existing = existing_topn_subs(video_path, lang, pattern)
    for p in existing:
        p.unlink()
    delete_sidecar(video_path, lang)
    return len(existing)

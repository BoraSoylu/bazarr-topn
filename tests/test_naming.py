"""Tests for subtitle file naming."""

from __future__ import annotations

from pathlib import Path

from bazarr_topn.naming import clean_existing_topn, existing_topn_subs, subtitle_path


class TestSubtitlePath:
    def test_default_pattern(self, tmp_path: Path) -> None:
        video = tmp_path / "Movie (2024).mkv"
        result = subtitle_path(video, "en", 2)
        assert result == tmp_path / "Movie (2024).en.topn-2.srt"

    def test_custom_pattern(self, tmp_path: Path) -> None:
        video = tmp_path / "Movie.mkv"
        result = subtitle_path(video, "tr", 5, "{video_stem}.{lang}.alt{rank}.srt")
        assert result == tmp_path / "Movie.tr.alt5.srt"

    def test_rank_numbering(self, tmp_path: Path) -> None:
        video = tmp_path / "Video.mkv"
        paths = [subtitle_path(video, "en", rank) for rank in range(2, 12)]
        assert len(paths) == 10
        assert paths[0].name == "Video.en.topn-2.srt"
        assert paths[-1].name == "Video.en.topn-11.srt"


class TestExistingTopnSubs:
    def test_finds_existing(self, tmp_path: Path) -> None:
        video = tmp_path / "Movie.mkv"
        video.touch()
        for rank in [2, 3, 4]:
            (tmp_path / f"Movie.en.topn-{rank}.srt").write_text("sub content")

        found = existing_topn_subs(
            video, "en", "{video_stem}.{lang}.topn-{rank}.srt"
        )
        assert len(found) == 3

    def test_ignores_other_langs(self, tmp_path: Path) -> None:
        video = tmp_path / "Movie.mkv"
        video.touch()
        (tmp_path / "Movie.en.topn-2.srt").write_text("english")
        (tmp_path / "Movie.tr.topn-2.srt").write_text("turkish")

        found = existing_topn_subs(
            video, "en", "{video_stem}.{lang}.topn-{rank}.srt"
        )
        assert len(found) == 1
        assert "en" in found[0].name


class TestCleanExisting:
    def test_removes_files(self, tmp_path: Path) -> None:
        video = tmp_path / "Movie.mkv"
        video.touch()
        for rank in [2, 3]:
            (tmp_path / f"Movie.en.topn-{rank}.srt").write_text("sub")

        count = clean_existing_topn(
            video, "en", "{video_stem}.{lang}.topn-{rank}.srt"
        )
        assert count == 2
        assert not list(tmp_path.glob("*.srt"))

    def test_clean_empty(self, tmp_path: Path) -> None:
        video = tmp_path / "Movie.mkv"
        video.touch()
        count = clean_existing_topn(
            video, "en", "{video_stem}.{lang}.topn-{rank}.srt"
        )
        assert count == 0

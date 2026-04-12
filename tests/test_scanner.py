"""Tests for video scanning and file discovery."""

from __future__ import annotations

from pathlib import Path

from bazarr_topn.scanner import VIDEO_EXTENSIONS, find_videos, is_video


class TestIsVideo:
    def test_video_extensions(self, tmp_path: Path) -> None:
        for ext in [".mkv", ".mp4", ".avi", ".m4v"]:
            f = tmp_path / f"test{ext}"
            f.touch()
            assert is_video(f), f"{ext} should be recognized as video"

    def test_non_video_extensions(self, tmp_path: Path) -> None:
        for ext in [".srt", ".txt", ".jpg", ".nfo"]:
            f = tmp_path / f"test{ext}"
            f.touch()
            assert not is_video(f), f"{ext} should not be recognized as video"

    def test_directory_not_video(self, tmp_path: Path) -> None:
        d = tmp_path / "subdir.mkv"
        d.mkdir()
        assert not is_video(d)


class TestFindVideos:
    def test_find_in_directory(self, tmp_path: Path) -> None:
        (tmp_path / "movie.mkv").touch()
        (tmp_path / "movie.srt").touch()
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "episode.mp4").touch()

        videos = find_videos([tmp_path])
        assert len(videos) == 2
        names = {v.name for v in videos}
        assert "movie.mkv" in names
        assert "episode.mp4" in names

    def test_find_single_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.mkv"
        f.touch()
        videos = find_videos([f])
        assert len(videos) == 1
        assert videos[0] == f

    def test_find_empty_dir(self, tmp_path: Path) -> None:
        videos = find_videos([tmp_path])
        assert len(videos) == 0

    def test_find_multiple_paths(self, tmp_path: Path) -> None:
        d1 = tmp_path / "movies"
        d2 = tmp_path / "tv"
        d1.mkdir()
        d2.mkdir()
        (d1 / "a.mkv").touch()
        (d2 / "b.mp4").touch()

        videos = find_videos([d1, d2])
        assert len(videos) == 2

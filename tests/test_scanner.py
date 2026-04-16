"""Tests for video scanning and file discovery."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from bazarr_topn.config import Config
from bazarr_topn.scanner import VIDEO_EXTENSIONS, find_videos, is_video, process_video
from bazarr_topn.sidecar import SidecarData, sidecar_path, write_sidecar
from bazarr_topn.subtitle_finder import DownloadResult


def _make_config(**overrides) -> Config:
    defaults = dict(
        languages=["en"], top_n=3, min_score=0,
        naming_pattern="{video_stem}.{lang}.topn-{rank}.srt",
        search_delay=0, download_delay=0,
        rate_limit_initial_backoff=0, rate_limit_retries=0,
        topn_recheck_days=30, topn_sidecar_enabled=True,
        max_candidates_tried=50,
    )
    defaults.update(overrides)
    return Config(**defaults)


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


class TestProcessVideoSidecar:
    """process_video uses sidecar for skip logic and writes sidecar on completion."""

    def test_skips_when_sidecar_is_done(self, tmp_video: Path) -> None:
        config = _make_config()
        data = SidecarData(target=3, saved=3, available=5, clean=True)
        write_sidecar(tmp_video, "en", data)
        pool = MagicMock()
        result = process_video(tmp_video, config, pool)
        assert result == -1

    def test_does_not_skip_when_sidecar_missing(self, tmp_video: Path) -> None:
        config = _make_config()
        pool = MagicMock()
        fake_result = DownloadResult(saved_paths=[], clean=True, available_count=0)
        with patch("bazarr_topn.scanner.scan_video") as mock_scan, \
             patch("bazarr_topn.scanner.download_top_n", return_value=fake_result):
            mock_scan.return_value = MagicMock()
            result = process_video(tmp_video, config, pool)
        assert result != -1

    def test_writes_sidecar_on_clean_completion(self, tmp_video: Path) -> None:
        config = _make_config()
        pool = MagicMock()
        saved_path = tmp_video.parent / "Test Movie (2024).en.topn-02.srt"
        saved_path.write_text("sub data")
        fake_result = DownloadResult(saved_paths=[saved_path], clean=True, available_count=5)
        with patch("bazarr_topn.scanner.scan_video") as mock_scan, \
             patch("bazarr_topn.scanner.download_top_n", return_value=fake_result):
            mock_scan.return_value = MagicMock()
            process_video(tmp_video, config, pool)
        sc = sidecar_path(tmp_video, "en")
        assert sc.exists()
        data = json.loads(sc.read_text())
        assert data["saved"] == 1
        assert data["available"] == 5
        assert data["clean"] is True
        assert data["target"] == 3

    def test_writes_sidecar_on_partial_unclean(self, tmp_video: Path) -> None:
        config = _make_config()
        pool = MagicMock()
        fake_result = DownloadResult(saved_paths=[], clean=False, available_count=10)
        with patch("bazarr_topn.scanner.scan_video") as mock_scan, \
             patch("bazarr_topn.scanner.download_top_n", return_value=fake_result):
            mock_scan.return_value = MagicMock()
            process_video(tmp_video, config, pool)
        sc = sidecar_path(tmp_video, "en")
        assert sc.exists()
        data = json.loads(sc.read_text())
        assert data["clean"] is False
        assert data["saved"] == 0

    def test_force_ignores_sidecar(self, tmp_video: Path) -> None:
        config = _make_config()
        data = SidecarData(target=3, saved=3, available=5, clean=True)
        write_sidecar(tmp_video, "en", data)
        pool = MagicMock()
        fake_result = DownloadResult(saved_paths=[], clean=True, available_count=5)
        with patch("bazarr_topn.scanner.scan_video") as mock_scan, \
             patch("bazarr_topn.scanner.download_top_n", return_value=fake_result):
            mock_scan.return_value = MagicMock()
            result = process_video(tmp_video, config, pool, force=True)
        assert result != -1

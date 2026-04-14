"""Tests for watcher.cold_start_scan — catch-up pass over existing files."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bazarr_topn.config import Config
from bazarr_topn.watcher import cold_start_scan


def _make_config(watch_path: Path) -> Config:
    return Config(
        languages=["en"],
        top_n=3,
        min_score=0,
        search_delay=0,
        download_delay=0,
        watch_paths=[str(watch_path)],
    )


class TestColdStartScan:
    def test_empty_watch_paths_returns_zero(self) -> None:
        config = Config(watch_paths=[])
        result = cold_start_scan(config, pool=MagicMock())
        assert result == {
            "videos_processed": 0,
            "videos_skipped": 0,
            "subtitles_downloaded": 0,
        }

    def test_nonexistent_watch_path_returns_zero(self, tmp_path: Path) -> None:
        config = Config(watch_paths=[str(tmp_path / "does-not-exist")])
        result = cold_start_scan(config, pool=MagicMock())
        assert result["videos_processed"] == 0

    def test_processes_new_videos(self, tmp_path: Path) -> None:
        (tmp_path / "a.mkv").touch()
        (tmp_path / "b.mkv").touch()
        (tmp_path / "readme.txt").touch()  # ignored
        config = _make_config(tmp_path)
        pool = MagicMock()

        with patch("bazarr_topn.watcher.process_video") as fake_process:
            # Return 3 downloaded per video, not skipped
            fake_process.return_value = 3
            result = cold_start_scan(config, pool)

        assert fake_process.call_count == 2
        assert result["videos_processed"] == 2
        assert result["videos_skipped"] == 0
        assert result["subtitles_downloaded"] == 6

    def test_counts_skipped_videos(self, tmp_path: Path) -> None:
        (tmp_path / "a.mkv").touch()
        (tmp_path / "b.mkv").touch()
        (tmp_path / "c.mkv").touch()
        config = _make_config(tmp_path)
        pool = MagicMock()

        with patch("bazarr_topn.watcher.process_video") as fake_process:
            # Simulate: first two already have topn subs, third is new
            fake_process.side_effect = [-1, -1, 5]
            result = cold_start_scan(config, pool)

        assert fake_process.call_count == 3
        assert result["videos_processed"] == 1
        assert result["videos_skipped"] == 2
        assert result["subtitles_downloaded"] == 5

    def test_continues_past_exceptions(self, tmp_path: Path) -> None:
        (tmp_path / "a.mkv").touch()
        (tmp_path / "b.mkv").touch()
        config = _make_config(tmp_path)
        pool = MagicMock()

        with patch("bazarr_topn.watcher.process_video") as fake_process:
            fake_process.side_effect = [RuntimeError("boom"), 4]
            result = cold_start_scan(config, pool)

        # One video failed, one succeeded; we keep going
        assert fake_process.call_count == 2
        assert result["videos_processed"] == 1
        assert result["subtitles_downloaded"] == 4

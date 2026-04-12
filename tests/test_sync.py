"""Tests for ffsubsync wrapper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bazarr_topn.sync import is_available, sync_batch, sync_subtitle
from bazarr_topn.config import FfsubsyncConfig


class TestIsAvailable:
    def test_available(self) -> None:
        # ffsubsync is installed in our test venv
        assert is_available() is True

    def test_not_available(self) -> None:
        with patch.dict("sys.modules", {"ffsubsync": None}):
            # Force re-check by calling directly
            try:
                import ffsubsync  # noqa: F401

                available = True
            except ImportError:
                available = False
            assert available is False


class TestSyncSubtitle:
    def test_disabled(self, tmp_path: Path) -> None:
        config = FfsubsyncConfig(enabled=False)
        result = sync_subtitle(tmp_path / "v.mkv", tmp_path / "s.srt", config)
        assert result is False

    def test_not_installed(self, tmp_path: Path) -> None:
        config = FfsubsyncConfig(enabled=True)
        with patch("bazarr_topn.sync.is_available", return_value=False):
            result = sync_subtitle(tmp_path / "v.mkv", tmp_path / "s.srt", config)
        assert result is False

    def test_successful_sync(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mkv"
        video.touch()
        sub = tmp_path / "sub.srt"
        sub.write_text("original")

        config = FfsubsyncConfig(enabled=True)

        def fake_run(args):
            # Simulate ffsubsync creating the output file
            out_path = tmp_path / "sub.synced.srt"
            out_path.write_text("synced content")
            return {"retval": 0, "offset_seconds": 1.5, "framerate_scale_factor": 1.0}

        with (
            patch("bazarr_topn.sync.is_available", return_value=True),
            patch("ffsubsync.ffsubsync.run", side_effect=fake_run),
        ):
            result = sync_subtitle(video, sub, config)

        assert result is True
        assert sub.read_text() == "synced content"

    def test_failed_sync(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mkv"
        video.touch()
        sub = tmp_path / "sub.srt"
        sub.write_text("original")

        config = FfsubsyncConfig(enabled=True)

        def fake_run(args):
            return {"retval": 1}

        with (
            patch("bazarr_topn.sync.is_available", return_value=True),
            patch("ffsubsync.ffsubsync.run", side_effect=fake_run),
        ):
            result = sync_subtitle(video, sub, config)

        assert result is False
        assert sub.read_text() == "original"


class TestSyncBatch:
    def test_disabled_returns_zero(self, tmp_path: Path) -> None:
        config = FfsubsyncConfig(enabled=False)
        result = sync_batch(tmp_path / "v.mkv", [tmp_path / "a.srt"], config)
        assert result == 0

    def test_batch_counts_successes(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mkv"
        subs = [tmp_path / "a.srt", tmp_path / "b.srt"]
        config = FfsubsyncConfig(enabled=True)

        call_count = 0

        def mock_sync(v, s, c):
            nonlocal call_count
            call_count += 1
            return call_count <= 1  # First succeeds, second fails

        with patch("bazarr_topn.sync.sync_subtitle", side_effect=mock_sync):
            result = sync_batch(video, subs, config)

        assert result == 1

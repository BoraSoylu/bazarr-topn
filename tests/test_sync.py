"""Tests for ffsubsync wrapper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bazarr_topn.sync import is_available, sync_batch, sync_subtitle, _run_sync
from bazarr_topn.config import FfsubsyncConfig


class TestIsAvailable:
    def test_available(self) -> None:
        # ffsubsync is installed in our test venv
        assert is_available() is True

    def test_not_available(self) -> None:
        with patch.dict("sys.modules", {"ffsubsync": None}):
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

    def test_empty_list(self, tmp_path: Path) -> None:
        config = FfsubsyncConfig(enabled=True)
        result = sync_batch(tmp_path / "v.mkv", [], config)
        assert result == 0

    def test_batch_caches_speech(self, tmp_path: Path) -> None:
        """First subtitle triggers --serialize-speech, rest use the .npz cache."""
        video = tmp_path / "video.mkv"
        video.touch()
        subs = [tmp_path / f"sub{i}.srt" for i in range(3)]
        for s in subs:
            s.write_text("content")

        config = FfsubsyncConfig(enabled=True)
        npz_path = video.with_suffix(".npz")

        calls_seen: list[tuple[str, bool]] = []

        def mock_run_sync(reference, subtitle_path, cfg, *, serialize_speech=False):
            calls_seen.append((reference, serialize_speech))
            # Simulate the .npz being created on first call
            if serialize_speech:
                npz_path.write_bytes(b"fake npz")
            return {"ok": True, "offset": 1.5, "scale": 1.0}

        with (
            patch("bazarr_topn.sync.is_available", return_value=True),
            patch("bazarr_topn.sync._run_sync", side_effect=mock_run_sync),
        ):
            result = sync_batch(video, subs, config)

        assert result == 3
        # First call: video as reference with serialize_speech=True
        assert calls_seen[0] == (str(video), True)
        # Subsequent calls: .npz as reference
        assert calls_seen[1] == (str(npz_path), False)
        assert calls_seen[2] == (str(npz_path), False)
        # .npz cleaned up
        assert not npz_path.exists()

    def test_batch_falls_back_without_cache(self, tmp_path: Path) -> None:
        """If .npz is not created, falls back to video for all syncs."""
        video = tmp_path / "video.mkv"
        video.touch()
        subs = [tmp_path / "a.srt", tmp_path / "b.srt"]
        for s in subs:
            s.write_text("content")

        config = FfsubsyncConfig(enabled=True)

        calls_seen: list[str] = []

        def mock_run_sync(reference, subtitle_path, cfg, *, serialize_speech=False):
            calls_seen.append(reference)
            # Don't create .npz — simulates serialize failure
            return {"ok": True, "offset": 0.5, "scale": 1.0}

        with (
            patch("bazarr_topn.sync.is_available", return_value=True),
            patch("bazarr_topn.sync._run_sync", side_effect=mock_run_sync),
        ):
            result = sync_batch(video, subs, config)

        assert result == 2
        # Both calls use video since .npz was never created
        assert calls_seen[0] == str(video)
        assert calls_seen[1] == str(video)

"""Tests for subtitle_finder — rate-limit retry behavior."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from babelfish import Language

from bazarr_topn.config import Config
from bazarr_topn.subtitle_finder import DownloadResult, download_top_n, find_subtitles


class FakeSubtitle:
    """Minimal stand-in for subliminal.subtitle.Subtitle."""

    def __init__(self, provider_name: str, content: bytes | None = b"sub data") -> None:
        self.provider_name = provider_name
        self.content = content
        self.id = f"{provider_name}-id"


class FakePool:
    """Fake ProviderPool that mimics subliminal's discard-on-error behavior.

    Scripted to fail for the first N calls (by adding the provider to
    discarded_providers and returning None/empty), then succeed.
    """

    def __init__(
        self,
        provider: str = "opensubtitlescom",
        fail_list_times: int = 0,
        fail_download_times: int = 0,
        subtitles_to_return: list[FakeSubtitle] | None = None,
    ) -> None:
        self.provider = provider
        self.discarded_providers: set[str] = set()
        self.fail_list_times = fail_list_times
        self.fail_download_times = fail_download_times
        self.list_calls = 0
        self.download_calls = 0
        self._subtitles = [FakeSubtitle(provider)] if subtitles_to_return is None else subtitles_to_return

    def list_subtitles(self, video: Any, languages: set[Language]) -> list[FakeSubtitle]:
        self.list_calls += 1
        if self.list_calls <= self.fail_list_times:
            # Simulate subliminal's behavior: add to discarded set, return []
            self.discarded_providers.add(self.provider)
            return []
        return list(self._subtitles)

    def download_subtitle(self, subtitle: FakeSubtitle) -> bool:
        self.download_calls += 1
        if self.download_calls <= self.fail_download_times:
            # Simulate subliminal: add to discarded set, leave content None
            self.discarded_providers.add(subtitle.provider_name)
            subtitle.content = None
            return False
        # Success path — ensure content is set
        if subtitle.content is None:
            subtitle.content = b"sub data"
        return True


@pytest.fixture
def no_delay_config() -> Config:
    """Config with zero delays/backoff so tests run instantly."""
    return Config(
        languages=["en"],
        top_n=3,
        min_score=0,
        search_delay=0,
        download_delay=0,
        rate_limit_initial_backoff=0,
        rate_limit_retries=2,
    )


class TestFindSubtitlesRetry:
    def test_no_failure_single_call(self, no_delay_config: Config) -> None:
        pool = FakePool(fail_list_times=0)
        subs = find_subtitles(
            MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
        )
        assert pool.list_calls == 1
        assert len(subs) == 1

    def test_retries_after_discard(self, no_delay_config: Config) -> None:
        pool = FakePool(fail_list_times=1)
        subs = find_subtitles(
            MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
        )
        assert pool.list_calls == 2
        assert len(subs) == 1
        # Provider should have been cleared from discarded set after retry
        assert "opensubtitlescom" not in pool.discarded_providers

    def test_gives_up_after_max_retries(self, no_delay_config: Config) -> None:
        # fail_list_times=5 > retries=2, so all attempts fail
        pool = FakePool(fail_list_times=5)
        subs = find_subtitles(
            MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
        )
        # initial + 2 retries = 3 total attempts
        assert pool.list_calls == 3
        assert subs == []


class TestDownloadTopNRetry:
    def test_download_retries_on_discard(
        self, tmp_path, no_delay_config: Config
    ) -> None:
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        # First download call fails and discards, second succeeds
        pool = FakePool(
            fail_download_times=1,
            subtitles_to_return=[FakeSubtitle("opensubtitlescom")],
        )
        result = download_top_n(
            MagicMock(),
            video_path,
            Language.fromalpha2("en"),
            no_delay_config,
            pool,
        )
        assert pool.download_calls == 2
        assert len(result.saved_paths) == 1
        assert result.saved_paths[0].exists()

    def test_download_gives_up_after_retries(
        self, tmp_path, no_delay_config: Config
    ) -> None:
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        pool = FakePool(
            fail_download_times=99,
            subtitles_to_return=[FakeSubtitle("opensubtitlescom")],
        )
        result = download_top_n(
            MagicMock(),
            video_path,
            Language.fromalpha2("en"),
            no_delay_config,
            pool,
        )
        # All retries exhausted; no files saved
        assert result.saved_paths == []
        # initial + retries = 3 attempts for the single subtitle
        assert pool.download_calls == 3


class TestLogMessage:
    """Issue 3: log message should distinguish 0-returned from N-filtered."""

    def test_zero_candidates_returned(
        self, tmp_path: Path, no_delay_config: Config, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When provider returns 0 candidates, say so clearly."""
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        pool = FakePool(subtitles_to_return=[])
        with caplog.at_level(logging.INFO):
            result = download_top_n(
                MagicMock(), video_path, Language.fromalpha2("en"),
                no_delay_config, pool,
            )
        assert result.saved_paths == []
        assert any("No candidates returned" in r.message for r in caplog.records)
        assert not any("filtered out" in r.message for r in caplog.records)
        assert not any("below min_score" in r.message for r in caplog.records)

    def test_all_filtered_by_min_score(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When candidates exist but all below min_score, say N filtered out."""
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        config = Config(
            languages=["en"], top_n=3, min_score=50,
            search_delay=0, download_delay=0,
            rate_limit_initial_backoff=0, rate_limit_retries=0,
            max_candidates_tried=50,
        )
        pool = FakePool(subtitles_to_return=[
            FakeSubtitle("opensubtitlescom"),
            FakeSubtitle("opensubtitlescom"),
        ])
        with caplog.at_level(logging.INFO):
            result = download_top_n(
                MagicMock(), video_path, Language.fromalpha2("en"),
                config, pool,
            )
        assert result.saved_paths == []
        assert any("filtered out" in r.message for r in caplog.records)
        assert any("2" in r.message and "filtered out" in r.message for r in caplog.records)


class TestDeepCandidateIteration:
    """Issue 2: iterate deeper into candidate list when subs come back invalid."""

    def test_skips_none_content_tries_deeper(
        self, tmp_path: Path, no_delay_config: Config
    ) -> None:
        """If first candidates have None content, keep trying deeper ranks."""
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        subs = [
            FakeSubtitle("opensubtitlescom", content=None),
            FakeSubtitle("opensubtitlescom", content=None),
            FakeSubtitle("opensubtitlescom", content=b"good1"),
            FakeSubtitle("opensubtitlescom", content=b"good2"),
            FakeSubtitle("opensubtitlescom", content=b"good3"),
        ]
        pool = FakePool(subtitles_to_return=subs)
        # Override download to use scripted content (don't let FakePool reset content)
        def scripted_download(subtitle):
            pool.download_calls += 1
            return subtitle.content is not None
        pool.download_subtitle = scripted_download
        pool.download_calls = 0

        result = download_top_n(
            MagicMock(), video_path, Language.fromalpha2("en"),
            no_delay_config, pool,
        )
        assert isinstance(result, DownloadResult)
        assert len(result.saved_paths) == 3  # top_n=3, got 3 valid
        assert result.clean is True
        assert result.available_count == 5

    def test_returns_download_result(
        self, tmp_path: Path, no_delay_config: Config
    ) -> None:
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        pool = FakePool(subtitles_to_return=[
            FakeSubtitle("opensubtitlescom", content=b"data"),
        ])
        result = download_top_n(
            MagicMock(), video_path, Language.fromalpha2("en"),
            no_delay_config, pool,
        )
        assert isinstance(result, DownloadResult)
        assert len(result.saved_paths) == 1
        assert result.clean is True
        assert result.available_count == 1

    def test_clean_false_when_retry_exhausted(
        self, tmp_path: Path, no_delay_config: Config
    ) -> None:
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        pool = FakePool(
            fail_download_times=99,
            subtitles_to_return=[FakeSubtitle("opensubtitlescom")],
        )
        result = download_top_n(
            MagicMock(), video_path, Language.fromalpha2("en"),
            no_delay_config, pool,
        )
        assert isinstance(result, DownloadResult)
        assert result.saved_paths == []
        assert result.clean is False

    def test_max_candidates_tried_cap(self, tmp_path: Path) -> None:
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        subs = [FakeSubtitle("opensubtitlescom", content=None) for _ in range(10)]
        pool = FakePool(subtitles_to_return=subs)
        def scripted_download(subtitle):
            pool.download_calls += 1
            return subtitle.content is not None
        pool.download_subtitle = scripted_download
        pool.download_calls = 0

        config = Config(
            languages=["en"], top_n=3, min_score=0,
            search_delay=0, download_delay=0,
            rate_limit_initial_backoff=0, rate_limit_retries=0,
            max_candidates_tried=5,
        )
        result = download_top_n(
            MagicMock(), video_path, Language.fromalpha2("en"),
            config, pool,
        )
        assert isinstance(result, DownloadResult)
        assert result.saved_paths == []
        assert pool.download_calls == 5

    def test_zero_candidates_returns_clean_result(
        self, tmp_path: Path, no_delay_config: Config
    ) -> None:
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        pool = FakePool(subtitles_to_return=[])
        result = download_top_n(
            MagicMock(), video_path, Language.fromalpha2("en"),
            no_delay_config, pool,
        )
        assert isinstance(result, DownloadResult)
        assert result.saved_paths == []
        assert result.clean is True
        assert result.available_count == 0

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
            # Match real subliminal: the generic `except Exception` branch in
            # ProviderPool.list_subtitles_provider logs via handle_exception
            # ("Unexpected error. Provider %s") at ERROR and returns []. It
            # does NOT add the provider to discarded_providers; only
            # DiscardingError takes that path.
            logging.getLogger("subliminal.core").error(
                "Unexpected error. Provider %s", self.provider,
            )
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

    def test_retries_after_logged_error(self, no_delay_config: Config) -> None:
        """First attempt emits an error log; retry attempt succeeds and returns subs."""
        pool = FakePool(fail_list_times=1)
        subs = find_subtitles(
            MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
        )
        assert pool.list_calls == 2
        assert len(subs) == 1
        # discarded_providers was never populated on the fail path
        # (real subliminal doesn't discard on generic exceptions).
        assert pool.discarded_providers == set()

    def test_gives_up_after_max_retries(self, no_delay_config: Config) -> None:
        from bazarr_topn.subtitle_finder import SearchUnavailable

        # fail_list_times=5 > retries=2, so all attempts fail
        pool = FakePool(fail_list_times=5)
        with pytest.raises(SearchUnavailable):
            find_subtitles(
                MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
            )
        # initial + 2 retries = 3 total attempts
        assert pool.list_calls == 3


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


class TestSearchUnavailable:
    def test_raises_when_retries_exhausted_with_discard(
        self, no_delay_config: Config
    ) -> None:
        from bazarr_topn.subtitle_finder import SearchUnavailable

        pool = FakePool(fail_list_times=99)  # always fails + discards
        with pytest.raises(SearchUnavailable):
            find_subtitles(
                MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
            )
        # initial + 2 retries = 3 attempts
        assert pool.list_calls == 3

    def test_no_raise_when_search_returns_empty_cleanly(
        self, no_delay_config: Config
    ) -> None:
        from bazarr_topn.subtitle_finder import SearchUnavailable

        pool = FakePool(subtitles_to_return=[])  # returns [] without discard
        subs = find_subtitles(
            MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
        )
        assert subs == []
        assert pool.list_calls == 1
        assert not pool.discarded_providers


class TestDownloadTopNSearchOk:
    def test_search_ok_true_on_normal_path(
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
        assert result.search_ok is True

    def test_search_ok_true_on_empty_candidates(
        self, tmp_path: Path, no_delay_config: Config
    ) -> None:
        """Genuine empty result — search completed fine, just nothing matched."""
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        pool = FakePool(subtitles_to_return=[])
        result = download_top_n(
            MagicMock(), video_path, Language.fromalpha2("en"),
            no_delay_config, pool,
        )
        assert result.saved_paths == []
        assert result.clean is True
        assert result.search_ok is True
        assert result.available_count == 0

    def test_search_ok_false_on_rate_limit(
        self, tmp_path: Path, no_delay_config: Config
    ) -> None:
        """SearchUnavailable from find_subtitles → search_ok=False, clean=False."""
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        pool = FakePool(fail_list_times=99)  # always discards
        result = download_top_n(
            MagicMock(), video_path, Language.fromalpha2("en"),
            no_delay_config, pool,
        )
        assert result.saved_paths == []
        assert result.clean is False
        assert result.search_ok is False
        assert result.available_count == 0


class TestCapturedSubliminalErrors:
    def test_captures_handle_exception_unexpected_error(self) -> None:
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        with _captured_subliminal_errors() as cap:
            logging.getLogger("subliminal.utils").error(
                "Unexpected error. Provider %s", "opensubtitlescom",
            )
        assert cap.had_errors is True
        assert "opensubtitlescom" in cap.first_error_message

    def test_captures_error_on_subliminal_child_logger(self) -> None:
        """Propagation: a handler on 'subliminal' sees records from 'subliminal.x.y'."""
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        with _captured_subliminal_errors() as cap:
            logging.getLogger("subliminal.providers.opensubtitlescom").error(
                "Unexpected error. Provider %s", "opensubtitlescom",
            )
        assert cap.had_errors is True
        assert "opensubtitlescom" in cap.first_error_message

    def test_captures_all_handle_exception_prefixes(self) -> None:
        """Each of subliminal.utils.handle_exception's 5 branches is captured."""
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        messages = [
            "Unexpected error. Provider opensubtitlescom",
            "Request timed out. Provider opensubtitlescom",
            "Service unavailable. Provider opensubtitlescom",
            "HTTP error 500. Provider opensubtitlescom",
            "SSL error 'handshake'. Provider opensubtitlescom",
        ]
        for msg in messages:
            with _captured_subliminal_errors() as cap:
                logging.getLogger("subliminal.utils").error(msg)
            assert cap.had_errors is True, f"Missed: {msg}"

    def test_ignores_unrelated_subliminal_errors(self) -> None:
        """Provider modules log ERROR for legit non-failure reasons —
        those must NOT be captured or we get false-positive retries."""
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        non_failure_messages = [
            "No show id found for 'whatever'",
            "Episode 42 not found",
            "No data returned from provider",
            "Failed to match some internal regex",
        ]
        for msg in non_failure_messages:
            with _captured_subliminal_errors() as cap:
                logging.getLogger("subliminal.providers.tvsubtitles").error(msg)
            assert cap.had_errors is False, f"False positive on: {msg}"

    def test_ignores_warning_and_info(self) -> None:
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        with _captured_subliminal_errors() as cap:
            logging.getLogger("subliminal").warning("Unexpected error. Provider X")
            logging.getLogger("subliminal").info("Unexpected error. Provider X")
        assert cap.had_errors is False

    def test_ignores_errors_on_unrelated_loggers(self) -> None:
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        with _captured_subliminal_errors() as cap:
            logging.getLogger("not_subliminal").error("Unexpected error. Provider X")
        assert cap.had_errors is False

    def test_handler_removed_on_clean_exit(self) -> None:
        from bazarr_topn.subtitle_finder import (
            _captured_subliminal_errors, _SubliminalErrorCapture,
        )

        logger = logging.getLogger("subliminal")
        before = list(logger.handlers)
        with _captured_subliminal_errors():
            pass
        after = list(logger.handlers)
        assert after == before
        assert not any(isinstance(h, _SubliminalErrorCapture) for h in after)

    def test_handler_removed_on_exception(self) -> None:
        from bazarr_topn.subtitle_finder import (
            _captured_subliminal_errors, _SubliminalErrorCapture,
        )

        logger = logging.getLogger("subliminal")
        before = list(logger.handlers)
        with pytest.raises(RuntimeError):
            with _captured_subliminal_errors():
                raise RuntimeError("oops")
        after = list(logger.handlers)
        assert after == before
        assert not any(isinstance(h, _SubliminalErrorCapture) for h in after)

    def test_first_error_message_when_empty(self) -> None:
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        with _captured_subliminal_errors() as cap:
            pass
        assert cap.had_errors is False
        assert cap.first_error_message == ""

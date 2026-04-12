"""Tests for Bazarr API client."""

from __future__ import annotations

import pytest
import responses

from bazarr_topn.bazarr_client import BazarrClient, MediaItem
from bazarr_topn.config import BazarrConfig

BASE = "http://localhost:6767"


@pytest.fixture
def client() -> BazarrClient:
    return BazarrClient(BazarrConfig(url=BASE, api_key="test-key"))


class TestBazarrClient:
    @responses.activate
    def test_get_movies(self, client: BazarrClient) -> None:
        responses.add(
            responses.GET,
            f"{BASE}/api/movies",
            json={
                "data": [
                    {
                        "title": "Inception",
                        "path": "/media/movies/Inception (2010)/Inception (2010).mkv",
                        "radarrId": 1,
                        "monitored": True,
                        "subtitles": [
                            {"code2": "en", "path": "/media/movies/Inception/Inception.en.srt"},
                        ],
                    },
                    {
                        "title": "Unmonitored Movie",
                        "path": "/media/movies/Unmonitored/Unmonitored.mkv",
                        "radarrId": 2,
                        "monitored": False,
                        "subtitles": [],
                    },
                ]
            },
        )

        movies = client.get_movies()
        assert len(movies) == 2
        assert movies[0].title == "Inception"
        assert movies[0].media_type == "movie"
        assert movies[0].radarr_id == 1
        assert movies[0].existing_subtitles == ["en"]
        assert movies[1].monitored is False

    @responses.activate
    def test_get_episodes(self, client: BazarrClient) -> None:
        responses.add(
            responses.GET,
            f"{BASE}/api/episodes",
            json={
                "data": [
                    {
                        "title": "Pilot",
                        "path": "/media/tv/Show/Season 01/Show - S01E01.mkv",
                        "sonarrSeriesId": 10,
                        "sonarrEpisodeId": 100,
                        "monitored": True,
                        "subtitles": [],
                    },
                ]
            },
        )

        episodes = client.get_episodes(10)
        assert len(episodes) == 1
        assert episodes[0].media_type == "episode"
        assert episodes[0].sonarr_series_id == 10

    @responses.activate
    def test_health_check_ok(self, client: BazarrClient) -> None:
        responses.add(
            responses.GET,
            f"{BASE}/api/system/status",
            json={"version": "1.5.6"},
        )
        assert client.health_check() is True

    @responses.activate
    def test_health_check_fail(self, client: BazarrClient) -> None:
        responses.add(
            responses.GET,
            f"{BASE}/api/system/status",
            status=500,
        )
        assert client.health_check() is False

    def test_api_key_header(self, client: BazarrClient) -> None:
        assert client.session.headers["X-API-KEY"] == "test-key"

    @responses.activate
    def test_get_series(self, client: BazarrClient) -> None:
        responses.add(
            responses.GET,
            f"{BASE}/api/series",
            json={
                "data": [
                    {"sonarrSeriesId": 1, "title": "Breaking Bad"},
                    {"sonarrSeriesId": 2, "title": "The Wire"},
                ]
            },
        )

        series = client.get_series()
        assert len(series) == 2
        assert series[0]["title"] == "Breaking Bad"

"""Tests for the Sonarr/Radarr webhook receiver."""

from __future__ import annotations

from bazarr_topn.webhook import (
    SonarrPayload,
    RadarrPayload,
)


# --- Fixture payloads (camelCase, exact field names from Sonarr/Radarr develop) ---

SONARR_DOWNLOAD = {
    "eventType": "Download",
    "isUpgrade": False,
    "instanceName": "Sonarr",
    "applicationUrl": "",
    "series": {
        "id": 42,
        "title": "Test Show",
        "path": "/media/tv/Test Show",
        "tvdbId": 1234,
        "year": 2024,
    },
    "episodes": [
        {"id": 1, "episodeNumber": 1, "seasonNumber": 1, "title": "Pilot"},
    ],
    "episodeFile": {
        "id": 100,
        "relativePath": "Season 01/Test Show - S01E01.mkv",
        "path": "/media/tv/Test Show/Season 01/Test Show - S01E01.mkv",
        "quality": "WEBDL-1080p",
    },
}

SONARR_UPGRADE = {
    "eventType": "Download",
    "isUpgrade": True,
    "instanceName": "Sonarr",
    "applicationUrl": "",
    "series": {
        "id": 42,
        "title": "Test Show",
        "path": "/media/tv/Test Show",
        "tvdbId": 1234,
        "year": 2024,
    },
    "episodes": [
        {"id": 1, "episodeNumber": 1, "seasonNumber": 1, "title": "Pilot"},
    ],
    "episodeFile": {
        "id": 200,
        "relativePath": "Season 01/Test Show - S01E01.WEBDL-2160p.mkv",
        "path": "/media/tv/Test Show/Season 01/Test Show - S01E01.WEBDL-2160p.mkv",
        "quality": "WEBDL-2160p",
    },
    "deletedFiles": [
        {
            "id": 100,
            "relativePath": "Season 01/Test Show - S01E01.mkv",
            "path": "/media/tv/Test Show/Season 01/Test Show - S01E01.mkv",
            "quality": "WEBDL-1080p",
        }
    ],
}

RADARR_DOWNLOAD = {
    "eventType": "Download",
    "isUpgrade": False,
    "instanceName": "Radarr",
    "applicationUrl": "",
    "movie": {
        "id": 7,
        "title": "Test Movie",
        "year": 2024,
        "folderPath": "/media/movies/Test Movie (2024)",
        "tmdbId": 99,
    },
    "movieFile": {
        "id": 500,
        "relativePath": "Test Movie (2024).mkv",
        "path": "/media/movies/Test Movie (2024)/Test Movie (2024).mkv",
        "quality": "WEBDL-1080p",
    },
}

RADARR_UPGRADE = {
    "eventType": "Download",
    "isUpgrade": True,
    "instanceName": "Radarr",
    "applicationUrl": "",
    "movie": {
        "id": 7,
        "title": "Test Movie",
        "year": 2024,
        "folderPath": "/media/movies/Test Movie (2024)",
        "tmdbId": 99,
    },
    "movieFile": {
        "id": 600,
        "relativePath": "Test Movie (2024) [2160p].mkv",
        "path": "/media/movies/Test Movie (2024)/Test Movie (2024) [2160p].mkv",
        "quality": "WEBDL-2160p",
    },
    "deletedFiles": [
        {
            "id": 500,
            "relativePath": "Test Movie (2024).mkv",
            "path": "/media/movies/Test Movie (2024)/Test Movie (2024).mkv",
            "quality": "WEBDL-1080p",
        }
    ],
}

SONARR_TEST = {
    "eventType": "Test",
    "instanceName": "Sonarr",
    "applicationUrl": "",
    "series": {"id": 0, "title": "Test Title", "path": "/", "tvdbId": 0, "year": 0},
    "episodes": [{"id": 0, "episodeNumber": 1, "seasonNumber": 1, "title": "Test"}],
}

RADARR_TEST = {
    "eventType": "Test",
    "instanceName": "Radarr",
    "applicationUrl": "",
    "movie": {"id": 0, "title": "Test Title", "year": 0, "folderPath": "/", "tmdbId": 0},
    "remoteMovie": {"tmdbId": 0, "imdbId": "", "title": "Test Title", "year": 0},
    "release": {"quality": "Bluray-1080p", "qualityVersion": 1, "releaseGroup": "", "releaseTitle": "", "indexer": "", "size": 0},
}


class TestSonarrPayloadParsing:
    def test_download(self) -> None:
        p = SonarrPayload.model_validate(SONARR_DOWNLOAD)
        assert p.event_type == "Download"
        assert p.is_upgrade is False
        assert p.series.path == "/media/tv/Test Show"
        assert p.episode_file is not None
        assert p.episode_file.relative_path == "Season 01/Test Show - S01E01.mkv"
        assert p.episode_file.path == "/media/tv/Test Show/Season 01/Test Show - S01E01.mkv"
        assert p.deleted_files == []

    def test_upgrade_carries_deleted_files(self) -> None:
        p = SonarrPayload.model_validate(SONARR_UPGRADE)
        assert p.event_type == "Download"
        assert p.is_upgrade is True
        assert len(p.deleted_files) == 1
        assert p.deleted_files[0].path == "/media/tv/Test Show/Season 01/Test Show - S01E01.mkv"

    def test_test_event_has_no_episode_file(self) -> None:
        p = SonarrPayload.model_validate(SONARR_TEST)
        assert p.event_type == "Test"
        assert p.episode_file is None


class TestRadarrPayloadParsing:
    def test_download(self) -> None:
        p = RadarrPayload.model_validate(RADARR_DOWNLOAD)
        assert p.event_type == "Download"
        assert p.is_upgrade is False
        assert p.movie.folder_path == "/media/movies/Test Movie (2024)"
        assert p.movie_file is not None
        assert p.movie_file.path == "/media/movies/Test Movie (2024)/Test Movie (2024).mkv"
        assert p.deleted_files == []

    def test_upgrade_carries_deleted_files(self) -> None:
        p = RadarrPayload.model_validate(RADARR_UPGRADE)
        assert p.is_upgrade is True
        assert len(p.deleted_files) == 1
        assert p.deleted_files[0].path == "/media/movies/Test Movie (2024)/Test Movie (2024).mkv"

    def test_test_event_has_no_movie_file(self) -> None:
        p = RadarrPayload.model_validate(RADARR_TEST)
        assert p.event_type == "Test"
        assert p.movie_file is None

"""Tests for the Sonarr/Radarr webhook receiver."""

from __future__ import annotations

from pathlib import Path

from bazarr_topn.config import Config
from bazarr_topn.webhook import (
    SonarrPayload,
    RadarrPayload,
    WebhookJob,
    cleanup_orphan_sidecars,
    resolve_sonarr_video_path,
    resolve_radarr_video_path,
    resolve_sonarr_deleted_paths,
    resolve_radarr_deleted_paths,
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


class TestResolveSonarrVideoPath:
    def test_uses_absolute_path_when_present(self) -> None:
        p = SonarrPayload.model_validate(SONARR_DOWNLOAD)
        config = Config()
        assert resolve_sonarr_video_path(p, config) == (
            "/media/tv/Test Show/Season 01/Test Show - S01E01.mkv"
        )

    def test_joins_relative_with_series_path_when_absolute_missing(self) -> None:
        payload = {
            **SONARR_DOWNLOAD,
            "episodeFile": {"relativePath": "Season 01/X.mkv"},  # no `path`
        }
        p = SonarrPayload.model_validate(payload)
        assert resolve_sonarr_video_path(p, Config()) == "/media/tv/Test Show/Season 01/X.mkv"

    def test_applies_path_mapping(self) -> None:
        p = SonarrPayload.model_validate(SONARR_DOWNLOAD)
        config = Config(path_mappings=[{"container": "/media", "host": "/mnt/media"}])
        assert resolve_sonarr_video_path(p, config) == (
            "/mnt/media/tv/Test Show/Season 01/Test Show - S01E01.mkv"
        )

    def test_returns_none_when_no_episode_file(self) -> None:
        p = SonarrPayload.model_validate(SONARR_TEST)
        assert resolve_sonarr_video_path(p, Config()) is None


class TestResolveRadarrVideoPath:
    def test_uses_absolute_path_when_present(self) -> None:
        p = RadarrPayload.model_validate(RADARR_DOWNLOAD)
        assert resolve_radarr_video_path(p, Config()) == (
            "/media/movies/Test Movie (2024)/Test Movie (2024).mkv"
        )

    def test_joins_relative_with_folder_path(self) -> None:
        payload = {
            **RADARR_DOWNLOAD,
            "movieFile": {"relativePath": "movie.mkv"},
        }
        p = RadarrPayload.model_validate(payload)
        assert resolve_radarr_video_path(p, Config()) == (
            "/media/movies/Test Movie (2024)/movie.mkv"
        )

    def test_applies_path_mapping(self) -> None:
        p = RadarrPayload.model_validate(RADARR_DOWNLOAD)
        config = Config(path_mappings=[{"container": "/media", "host": "/mnt/media"}])
        assert resolve_radarr_video_path(p, config) == (
            "/mnt/media/movies/Test Movie (2024)/Test Movie (2024).mkv"
        )

    def test_returns_none_when_no_movie_file(self) -> None:
        p = RadarrPayload.model_validate(RADARR_TEST)
        assert resolve_radarr_video_path(p, Config()) is None


class TestResolveDeletedPaths:
    def test_sonarr_deleted_paths_remapped(self) -> None:
        p = SonarrPayload.model_validate(SONARR_UPGRADE)
        config = Config(path_mappings=[{"container": "/media", "host": "/mnt/media"}])
        assert resolve_sonarr_deleted_paths(p, config) == [
            "/mnt/media/tv/Test Show/Season 01/Test Show - S01E01.mkv",
        ]

    def test_radarr_deleted_paths_remapped(self) -> None:
        p = RadarrPayload.model_validate(RADARR_UPGRADE)
        config = Config(path_mappings=[{"container": "/media", "host": "/mnt/media"}])
        assert resolve_radarr_deleted_paths(p, config) == [
            "/mnt/media/movies/Test Movie (2024)/Test Movie (2024).mkv",
        ]

    def test_no_deleted_files_returns_empty(self) -> None:
        p = SonarrPayload.model_validate(SONARR_DOWNLOAD)
        assert resolve_sonarr_deleted_paths(p, Config()) == []


class TestCleanupOrphanSidecars:
    def test_deletes_topn_srts_and_sidecar_for_one_language(self, tmp_path: Path) -> None:
        old_video = tmp_path / "Old.mkv"
        # We never need the old video to exist on disk — just its siblings.
        (tmp_path / "Old.en.topn-02.srt").write_text("a")
        (tmp_path / "Old.en.topn-03.srt").write_text("b")
        (tmp_path / "Old.en.topn.json").write_text("{}")
        # Unrelated files we must NOT touch
        (tmp_path / "Old.en.srt").write_text("bazarr's original — keep")
        (tmp_path / "Other.mkv").write_text("unrelated")
        (tmp_path / "Other.en.topn-02.srt").write_text("unrelated topn")

        config = Config(
            languages=["en"],
            naming_pattern="{video_stem}.{lang}.topn-{rank}.srt",
        )
        removed = cleanup_orphan_sidecars(str(old_video), config)

        assert removed == 3
        assert not (tmp_path / "Old.en.topn-02.srt").exists()
        assert not (tmp_path / "Old.en.topn-03.srt").exists()
        assert not (tmp_path / "Old.en.topn.json").exists()
        # Untouched
        assert (tmp_path / "Old.en.srt").exists()
        assert (tmp_path / "Other.en.topn-02.srt").exists()

    def test_handles_multiple_languages(self, tmp_path: Path) -> None:
        old_video = tmp_path / "Old.mkv"
        (tmp_path / "Old.en.topn-02.srt").write_text("a")
        (tmp_path / "Old.en.topn.json").write_text("{}")
        (tmp_path / "Old.tr.topn-02.srt").write_text("a")
        (tmp_path / "Old.tr.topn.json").write_text("{}")

        config = Config(
            languages=["en", "tr"],
            naming_pattern="{video_stem}.{lang}.topn-{rank}.srt",
        )
        removed = cleanup_orphan_sidecars(str(old_video), config)

        assert removed == 4
        assert list(tmp_path.iterdir()) == []  # everything cleaned

    def test_missing_files_are_ok(self, tmp_path: Path) -> None:
        old_video = tmp_path / "DoesNotExist.mkv"
        config = Config(
            languages=["en"],
            naming_pattern="{video_stem}.{lang}.topn-{rank}.srt",
        )
        # Should not raise; nothing to delete.
        assert cleanup_orphan_sidecars(str(old_video), config) == 0


class TestWebhookJob:
    def test_construct_download(self) -> None:
        job = WebhookJob(video_path="/x/a.mkv", deleted_paths=[])
        assert job.video_path == "/x/a.mkv"
        assert job.deleted_paths == []
        assert job.is_upgrade is False  # derived from deleted_paths

    def test_construct_upgrade(self) -> None:
        job = WebhookJob(video_path="/x/new.mkv", deleted_paths=["/x/old.mkv"])
        assert job.is_upgrade is True

"""Tests for the Sonarr/Radarr webhook receiver."""

from __future__ import annotations

import queue as _queue
import threading
import time as _time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bazarr_topn.config import Config
from bazarr_topn.webhook import (
    SonarrPayload,
    RadarrPayload,
    WebhookJob,
    build_app,
    cleanup_orphan_sidecars,
    resolve_sonarr_video_path,
    resolve_radarr_video_path,
    resolve_sonarr_deleted_paths,
    resolve_radarr_deleted_paths,
    run_worker,
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


def _config_with_token(token: str = "secret") -> Config:
    config = Config()
    config.webhook.token = token
    return config


class TestAuth:
    def test_missing_token_returns_401(self) -> None:
        config = _config_with_token()
        app, _q = build_app(config)
        client = TestClient(app)
        r = client.post("/sonarr", json=SONARR_DOWNLOAD)
        assert r.status_code == 401

    def test_wrong_token_returns_401(self) -> None:
        config = _config_with_token()
        app, _q = build_app(config)
        client = TestClient(app)
        r = client.post(
            "/sonarr",
            json=SONARR_DOWNLOAD,
            headers={"X-Webhook-Token": "wrong"},
        )
        assert r.status_code == 401

    def test_correct_token_returns_200(self) -> None:
        config = _config_with_token()
        app, _q = build_app(config)
        client = TestClient(app)
        r = client.post(
            "/sonarr",
            json=SONARR_DOWNLOAD,
            headers={"X-Webhook-Token": "secret"},
        )
        assert r.status_code == 200

    def test_healthz_does_not_require_auth(self) -> None:
        config = _config_with_token()
        app, _q = build_app(config)
        client = TestClient(app)
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestRouting:
    def test_sonarr_download_enqueues_job(self) -> None:
        config = _config_with_token()
        app, q = build_app(config)
        client = TestClient(app)
        r = client.post(
            "/sonarr",
            json=SONARR_DOWNLOAD,
            headers={"X-Webhook-Token": "secret"},
        )
        assert r.status_code == 200
        assert q.qsize() == 1
        job = q.get_nowait()
        assert job.video_path == "/media/tv/Test Show/Season 01/Test Show - S01E01.mkv"
        assert job.deleted_paths == []

    def test_sonarr_upgrade_enqueues_job_with_deleted(self) -> None:
        config = _config_with_token()
        app, q = build_app(config)
        client = TestClient(app)
        r = client.post(
            "/sonarr",
            json=SONARR_UPGRADE,
            headers={"X-Webhook-Token": "secret"},
        )
        assert r.status_code == 200
        job = q.get_nowait()
        assert job.is_upgrade is True
        assert len(job.deleted_paths) == 1

    def test_radarr_download_enqueues_job(self) -> None:
        config = _config_with_token()
        app, q = build_app(config)
        client = TestClient(app)
        r = client.post(
            "/radarr",
            json=RADARR_DOWNLOAD,
            headers={"X-Webhook-Token": "secret"},
        )
        assert r.status_code == 200
        job = q.get_nowait()
        assert job.video_path == "/media/movies/Test Movie (2024)/Test Movie (2024).mkv"

    def test_test_event_returns_200_without_enqueueing(self) -> None:
        config = _config_with_token()
        app, q = build_app(config)
        client = TestClient(app)
        r = client.post(
            "/sonarr",
            json=SONARR_TEST,
            headers={"X-Webhook-Token": "secret"},
        )
        assert r.status_code == 200
        assert q.qsize() == 0

        r2 = client.post(
            "/radarr",
            json=RADARR_TEST,
            headers={"X-Webhook-Token": "secret"},
        )
        assert r2.status_code == 200
        assert q.qsize() == 0

    def test_path_mapping_applied(self) -> None:
        config = _config_with_token()
        config.path_mappings = [{"container": "/media", "host": "/mnt/media"}]
        app, q = build_app(config)
        client = TestClient(app)
        r = client.post(
            "/sonarr",
            json=SONARR_DOWNLOAD,
            headers={"X-Webhook-Token": "secret"},
        )
        assert r.status_code == 200
        job = q.get_nowait()
        assert job.video_path.startswith("/mnt/media/tv/")

    def test_malformed_payload_returns_422(self) -> None:
        config = _config_with_token()
        app, _q = build_app(config)
        client = TestClient(app)
        r = client.post(
            "/sonarr",
            json={"not": "a real payload"},
            headers={"X-Webhook-Token": "secret"},
        )
        assert r.status_code == 422

    def test_unknown_event_type_returns_200_without_enqueueing(self) -> None:
        """Sonarr/Radarr have many event types we don't care about (Grab,
        Health, etc.). The receiver must accept them with 200 to avoid
        triggering retries on the *arr side."""
        config = _config_with_token()
        app, q = build_app(config)
        client = TestClient(app)
        payload = {**SONARR_DOWNLOAD, "eventType": "Grab"}
        r = client.post(
            "/sonarr",
            json=payload,
            headers={"X-Webhook-Token": "secret"},
        )
        assert r.status_code == 200
        assert q.qsize() == 0


class TestWorker:
    def test_processes_jobs_in_order(self, tmp_path: Path) -> None:
        a = tmp_path / "a.mkv"; a.write_bytes(b"")
        b = tmp_path / "b.mkv"; b.write_bytes(b"")
        config = Config(languages=["en"])
        config.webhook.lockfile = str(tmp_path / "test.lock")
        q: _queue.Queue = _queue.Queue()
        q.put(WebhookJob(video_path=str(a), deleted_paths=[]))
        q.put(WebhookJob(video_path=str(b), deleted_paths=[]))
        q.put(None)
        pool = MagicMock()
        with patch("bazarr_topn.webhook.process_video") as fake_process:
            fake_process.return_value = 5
            run_worker(q, config, pool)
        calls = [str(c.args[0]) for c in fake_process.call_args_list]
        assert calls == [str(a), str(b)]

    def test_calls_cleanup_on_upgrade(self, tmp_path: Path) -> None:
        new = tmp_path / "new.mkv"; new.write_bytes(b"")
        config = Config(languages=["en"])
        config.webhook.lockfile = str(tmp_path / "test.lock")
        q: _queue.Queue = _queue.Queue()
        q.put(WebhookJob(video_path=str(new), deleted_paths=["/x/old.mkv"]))
        q.put(None)
        pool = MagicMock()
        with patch("bazarr_topn.webhook.process_video") as fake_process, \
             patch("bazarr_topn.webhook.cleanup_orphan_sidecars") as fake_cleanup:
            fake_process.return_value = 5
            fake_cleanup.return_value = 2
            run_worker(q, config, pool)
        fake_cleanup.assert_called_once_with("/x/old.mkv", config)
        fake_process.assert_called_once()

    def test_swallows_process_video_exceptions(self, tmp_path: Path) -> None:
        a = tmp_path / "a.mkv"; a.write_bytes(b"")
        b = tmp_path / "b.mkv"; b.write_bytes(b"")
        config = Config(languages=["en"])
        config.webhook.lockfile = str(tmp_path / "test.lock")
        q: _queue.Queue = _queue.Queue()
        q.put(WebhookJob(video_path=str(a)))
        q.put(WebhookJob(video_path=str(b)))
        q.put(None)
        pool = MagicMock()
        with patch("bazarr_topn.webhook.process_video") as fake_process:
            fake_process.side_effect = [RuntimeError("boom"), 3]
            run_worker(q, config, pool)
        assert fake_process.call_count == 2

    # The thread raises SystemExit (via fake_exit) which pytest sees as an
    # "unhandled thread exception" and re-emits as a warning. That warning is
    # exactly what we want — it proves the thread terminated instead of looping
    # silently. Mark it as expected so the suite stays clean.
    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_unrecoverable_error_calls_os_exit(self, tmp_path: Path) -> None:
        """Worker outer-loop fault handler: an OSError opening the lockfile
        (e.g. PermissionError on os.makedirs) must trigger os._exit(1) so
        systemd can restart the service, rather than silently dying while
        uvicorn continues to accept and 200-ack requests into a dead queue.
        """
        config = Config(languages=["en"])
        # Point lockfile at a path whose *parent* cannot be created (simulate
        # PermissionError) by making _scan_lock raise directly via mock.
        config.webhook.lockfile = str(tmp_path / "test.lock")

        q: _queue.Queue = _queue.Queue()
        q.put(WebhookJob(video_path=str(tmp_path / "movie.mkv"), deleted_paths=[]))
        # No sentinel — the worker should os._exit before it would ever drain it.
        pool = MagicMock()

        exit_calls: list[int] = []

        def fake_exit(code: int) -> None:  # noqa: ANN001
            exit_calls.append(code)
            # Raise so the thread actually terminates in the test.
            raise SystemExit(code)

        with patch("bazarr_topn.webhook._scan_lock", side_effect=OSError("permission denied")), \
             patch("bazarr_topn.webhook.os._exit", side_effect=fake_exit):
            t = threading.Thread(target=run_worker, args=(q, config, pool), daemon=True)
            t.start()
            t.join(timeout=3)

        assert not t.is_alive(), "Worker thread should have terminated"
        assert exit_calls == [1], "os._exit(1) must be called exactly once"

    def test_holds_lockfile_while_processing(self, tmp_path: Path) -> None:
        import fcntl

        lockpath = tmp_path / "test.lock"
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"\x00" * 16)  # exists() must return True
        config = Config(languages=["en"])
        config.webhook.lockfile = str(lockpath)
        q: _queue.Queue = _queue.Queue()
        q.put(WebhookJob(video_path=str(video_path)))
        q.put(None)
        pool = MagicMock()

        # Pre-acquire the lock from the test thread
        lockpath.touch()
        holder = open(lockpath, "w")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

        with patch("bazarr_topn.webhook.process_video") as fake_process:
            fake_process.return_value = 0
            t = threading.Thread(
                target=run_worker, args=(q, config, pool), daemon=True
            )
            t.start()
            _time.sleep(0.3)
            assert fake_process.call_count == 0

            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
            t.join(timeout=3)
            assert not t.is_alive()
            assert fake_process.call_count == 1


class TestEndToEndIntegration:
    def test_24_events_drain_through_worker(self, tmp_path: Path) -> None:
        """Season-pack burst: 24 episodes posted in rapid succession.

        Each POST returns 200 fast. The worker drains all 24 in order.
        """
        config = _config_with_token()
        config.languages = ["en"]
        config.webhook.lockfile = str(tmp_path / "scan.lock")

        # Create 24 fake episode files under the path Sonarr would report
        series_dir = tmp_path / "Test Show" / "Season 01"
        series_dir.mkdir(parents=True)
        episode_files: list[Path] = []
        for i in range(1, 25):
            f = series_dir / f"Test Show - S01E{i:02d}.mkv"
            f.write_bytes(b"\x00" * 16)
            episode_files.append(f)

        # Path mapping: webhook reports /media/tv/...; we host them in tmp_path
        config.path_mappings = [
            {"container": "/media/tv/Test Show", "host": str(tmp_path / "Test Show")}
        ]

        app, job_queue = build_app(config)
        pool = MagicMock()
        worker = threading.Thread(
            target=run_worker, args=(job_queue, config, pool), daemon=True,
        )
        worker.start()

        client = TestClient(app)
        with patch("bazarr_topn.webhook.process_video") as fake_process:
            fake_process.return_value = 5
            for i in range(1, 25):
                payload = {
                    "eventType": "Download",
                    "isUpgrade": False,
                    "series": {"path": "/media/tv/Test Show", "title": "Test Show"},
                    "episodes": [
                        {"id": i, "episodeNumber": i, "seasonNumber": 1, "title": f"E{i}"},
                    ],
                    "episodeFile": {
                        "relativePath": f"Season 01/Test Show - S01E{i:02d}.mkv",
                        "path": f"/media/tv/Test Show/Season 01/Test Show - S01E{i:02d}.mkv",
                    },
                }
                r = client.post(
                    "/sonarr",
                    json=payload,
                    headers={"X-Webhook-Token": "secret"},
                )
                assert r.status_code == 200

            # Wait for worker to drain.
            job_queue.join()
            # Stop the worker.
            job_queue.put(None)
            worker.join(timeout=3)

        assert fake_process.call_count == 24
        # Order preserved: E01..E24
        called_paths = [str(c.args[0]) for c in fake_process.call_args_list]
        for i, p in enumerate(called_paths, start=1):
            assert p.endswith(f"S01E{i:02d}.mkv")

    def test_upgrade_event_cleans_orphans_then_processes(self, tmp_path: Path) -> None:
        """Sonarr upgrade event: orphan sidecars deleted, then process_video called once.

        Setup: a "Show - S01E01.mkv" file exists with a topn sidecar set
        from the previous (lower-quality) version. Sonarr fires an upgrade
        webhook with the new file path and the old file in deletedFiles.
        Assert: old sidecar files are gone, the unrelated keep-me file
        survives, process_video is invoked once with the new path.
        """
        config = _config_with_token()
        config.languages = ["en"]
        config.webhook.lockfile = str(tmp_path / "scan.lock")
        config.naming_pattern = "{video_stem}.{lang}.topn-{rank}.srt"

        season = tmp_path / "Season 01"
        season.mkdir()
        old_stem = "Test Show - S01E01.WEBDL-1080p"
        new_stem = "Test Show - S01E01.WEBDL-2160p"
        new_file = season / f"{new_stem}.mkv"
        new_file.write_bytes(b"\x00" * 16)

        # Pre-existing orphans (will be deleted)
        (season / f"{old_stem}.en.topn-02.srt").write_text("a")
        (season / f"{old_stem}.en.topn-03.srt").write_text("b")
        (season / f"{old_stem}.en.topn.json").write_text("{}")
        # Keep-me marker
        keep = season / f"{new_stem}.en.srt"
        keep.write_text("bazarr's original")

        config.path_mappings = [
            {"container": "/media/tv/Show", "host": str(tmp_path)},
        ]

        app, job_queue = build_app(config)
        pool = MagicMock()
        worker = threading.Thread(
            target=run_worker, args=(job_queue, config, pool), daemon=True,
        )
        worker.start()

        upgrade_payload = {
            "eventType": "Download",
            "isUpgrade": True,
            "series": {"path": "/media/tv/Show", "title": "Test Show"},
            "episodes": [
                {"id": 1, "episodeNumber": 1, "seasonNumber": 1, "title": "Pilot"},
            ],
            "episodeFile": {
                "relativePath": f"Season 01/{new_stem}.mkv",
                "path": f"/media/tv/Show/Season 01/{new_stem}.mkv",
            },
            "deletedFiles": [
                {
                    "relativePath": f"Season 01/{old_stem}.mkv",
                    "path": f"/media/tv/Show/Season 01/{old_stem}.mkv",
                }
            ],
        }

        client = TestClient(app)
        with patch("bazarr_topn.webhook.process_video") as fake_process:
            fake_process.return_value = 5
            r = client.post(
                "/sonarr",
                json=upgrade_payload,
                headers={"X-Webhook-Token": "secret"},
            )
            assert r.status_code == 200
            job_queue.join()
            job_queue.put(None)
            worker.join(timeout=3)

        # Old sidecars gone
        assert not (season / f"{old_stem}.en.topn-02.srt").exists()
        assert not (season / f"{old_stem}.en.topn-03.srt").exists()
        assert not (season / f"{old_stem}.en.topn.json").exists()
        # Keep-me untouched
        assert keep.exists()
        # process_video called exactly once with the new path
        assert fake_process.call_count == 1
        called_path = fake_process.call_args.args[0]
        assert str(called_path) == str(new_file)

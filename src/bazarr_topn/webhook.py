"""FastAPI receiver for Sonarr/Radarr webhooks. Public entry: serve(config)."""

from __future__ import annotations

import hmac
import logging
import posixpath  # webhooks always carry forward-slash paths from arr
import queue as _queue
from dataclasses import dataclass, field
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from bazarr_topn.config import Config
from bazarr_topn.naming import existing_topn_subs
from bazarr_topn.sidecar import sidecar_path

logger = logging.getLogger(__name__)


# --- Pydantic v2 base with camelCase aliases and "ignore extras" ---
#
# Sonarr/Radarr emit camelCase JSON. We keep snake_case Python attribute
# names and use Field(alias=...) for translation. populate_by_name lets
# constructors still accept snake_case, which simplifies tests if needed.
# extra="ignore" is critical: *arr payloads carry many fields we don't
# read (releaseGroup, mediaInfo, customFormats, etc.) and we must not
# break when they evolve.

class _ArrModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
    )


# --- Shared file sub-models ---

class SonarrEpisodeFile(_ArrModel):
    """Sonarr's webhook EpisodeFile object. Only fields we read."""

    relative_path: str = Field(alias="relativePath")
    path: Optional[str] = None  # absolute path when present


class SonarrSeries(_ArrModel):
    """Sonarr's webhook Series object. Only fields we read."""

    path: str  # absolute series root (e.g. /media/tv/Show Name)
    title: str = ""


class RadarrMovieFile(_ArrModel):
    """Radarr's webhook MovieFile object. Only fields we read."""

    relative_path: str = Field(alias="relativePath")
    path: Optional[str] = None


class RadarrMovie(_ArrModel):
    """Radarr's webhook Movie object. Only fields we read."""

    folder_path: str = Field(alias="folderPath")
    title: str = ""


# --- Top-level payloads ---

class SonarrPayload(_ArrModel):
    """Top-level Sonarr webhook payload.

    Field names track Sonarr's WebhookImportPayload (develop branch). Sonarr
    fires `eventType: "Download"` for both new imports and upgrades; the
    receiver distinguishes them via `is_upgrade`. On upgrade events,
    `deleted_files` contains the replaced episode files.
    """

    event_type: str = Field(alias="eventType")
    is_upgrade: bool = Field(default=False, alias="isUpgrade")
    series: SonarrSeries = Field(default_factory=lambda: SonarrSeries(path="/"))
    episode_file: Optional[SonarrEpisodeFile] = Field(default=None, alias="episodeFile")
    deleted_files: list[SonarrEpisodeFile] = Field(default_factory=list, alias="deletedFiles")


class RadarrPayload(_ArrModel):
    """Top-level Radarr webhook payload."""

    event_type: str = Field(alias="eventType")
    is_upgrade: bool = Field(default=False, alias="isUpgrade")
    movie: RadarrMovie = Field(default_factory=lambda: RadarrMovie(folder_path="/"))
    movie_file: Optional[RadarrMovieFile] = Field(default=None, alias="movieFile")
    deleted_files: list[RadarrMovieFile] = Field(default_factory=list, alias="deletedFiles")


# --- Path resolution helpers ---


def _join_arr_path(parent: str, child_relative: str) -> str:
    """Join a Sonarr/Radarr parent path + relative path.

    Always uses POSIX semantics — these payloads come from .NET apps that
    normalize to forward-slash, regardless of the host OS. The result is a
    string we then pass to Config.map_path; Path() conversion happens later
    inside scanner.process_video.
    """
    return posixpath.join(parent.rstrip("/"), child_relative.lstrip("/"))


def resolve_sonarr_video_path(payload: SonarrPayload, config: Config) -> Optional[str]:
    """Return the absolute host-side path of the imported episode file, or None.

    Prefers the payload's absolute `path` field; falls back to joining
    `series.path` + `episodeFile.relativePath`. Always runs through
    Config.map_path to translate container paths to host paths.
    """
    if payload.episode_file is None:
        return None
    raw = payload.episode_file.path
    if not raw:
        raw = _join_arr_path(payload.series.path, payload.episode_file.relative_path)
    return config.map_path(raw)


def resolve_radarr_video_path(payload: RadarrPayload, config: Config) -> Optional[str]:
    """Return the absolute host-side path of the imported movie file, or None."""
    if payload.movie_file is None:
        return None
    raw = payload.movie_file.path
    if not raw:
        raw = _join_arr_path(payload.movie.folder_path, payload.movie_file.relative_path)
    return config.map_path(raw)


def resolve_sonarr_deleted_paths(payload: SonarrPayload, config: Config) -> list[str]:
    """Return mapped host-side paths of all deletedFiles entries."""
    out: list[str] = []
    for f in payload.deleted_files:
        raw = f.path or _join_arr_path(payload.series.path, f.relative_path)
        out.append(config.map_path(raw))
    return out


def resolve_radarr_deleted_paths(payload: RadarrPayload, config: Config) -> list[str]:
    """Return mapped host-side paths of all deletedFiles entries."""
    out: list[str] = []
    for f in payload.deleted_files:
        raw = f.path or _join_arr_path(payload.movie.folder_path, f.relative_path)
        out.append(config.map_path(raw))
    return out


def cleanup_orphan_sidecars(old_video_path: str, config: Config) -> int:
    """Delete topn-N srt files and topn.json sidecars keyed to a replaced video.

    On a Sonarr/Radarr Upgrade, the old episode/movie file gets replaced by a
    new one with a different stem (e.g. quality bump). The topn sidecars
    we wrote for the old stem are now orphaned. This helper removes them
    across every configured language.

    Returns:
        Number of files actually deleted.
    """
    removed = 0
    for lang in config.languages:
        for srt in existing_topn_subs(old_video_path, lang, config.naming_pattern):
            try:
                srt.unlink()
                removed += 1
                logger.debug("Removed orphan topn srt: %s", srt)
            except OSError as e:
                logger.debug("Could not remove %s: %s", srt, e)
        json_path = sidecar_path(old_video_path, lang)
        if json_path.exists():
            try:
                json_path.unlink()
                removed += 1
                logger.debug("Removed orphan topn sidecar: %s", json_path)
            except OSError as e:
                logger.debug("Could not remove %s: %s", json_path, e)
    return removed


@dataclass
class WebhookJob:
    """A single unit of work for the worker thread.

    `deleted_paths` is non-empty only on upgrade events. The worker iterates
    those before processing `video_path`, calling cleanup_orphan_sidecars
    once per old stem.
    """

    video_path: str
    deleted_paths: list[str] = field(default_factory=list)

    @property
    def is_upgrade(self) -> bool:
        return bool(self.deleted_paths)


def _make_auth_dependency(expected_token: str):
    """Build a FastAPI dependency that constant-time-compares X-Webhook-Token."""

    def verify_token(x_webhook_token: str = Header(default="")) -> None:
        if not expected_token or not hmac.compare_digest(x_webhook_token, expected_token):
            # No INFO log — avoids spam if a misconfigured *arr keeps retrying.
            raise HTTPException(status_code=401, detail="invalid webhook token")

    return verify_token


def build_app(config: Config) -> tuple[FastAPI, _queue.Queue]:
    """Build the FastAPI app and the job queue it feeds.

    The queue is returned so callers (`serve` and tests) can attach a worker
    to it. Tests assert on queue contents directly without spinning a worker.
    """
    job_queue: _queue.Queue = _queue.Queue()
    auth = _make_auth_dependency(config.webhook.token)

    app = FastAPI(title="bazarr-topn webhook receiver")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sonarr", dependencies=[Depends(auth)])
    def sonarr(payload: SonarrPayload) -> dict[str, str]:
        if payload.event_type == "Test":
            return {"status": "ok"}
        if payload.event_type != "Download":
            # Quietly accept unknown event types; *arr won't retry on 200.
            logger.debug("Sonarr: ignoring eventType=%s", payload.event_type)
            return {"status": "ok"}
        video = resolve_sonarr_video_path(payload, config)
        if video is None:
            logger.warning("Sonarr download payload missing episodeFile; skipping")
            return {"status": "ok"}
        deleted = resolve_sonarr_deleted_paths(payload, config)
        job = WebhookJob(video_path=video, deleted_paths=deleted)
        job_queue.put(job)
        logger.info(
            "Sonarr: queued %s (upgrade=%s, %d deleted)",
            video, job.is_upgrade, len(deleted),
        )
        return {"status": "queued"}

    @app.post("/radarr", dependencies=[Depends(auth)])
    def radarr(payload: RadarrPayload) -> dict[str, str]:
        if payload.event_type == "Test":
            return {"status": "ok"}
        if payload.event_type != "Download":
            logger.debug("Radarr: ignoring eventType=%s", payload.event_type)
            return {"status": "ok"}
        video = resolve_radarr_video_path(payload, config)
        if video is None:
            logger.warning("Radarr download payload missing movieFile; skipping")
            return {"status": "ok"}
        deleted = resolve_radarr_deleted_paths(payload, config)
        job = WebhookJob(video_path=video, deleted_paths=deleted)
        job_queue.put(job)
        logger.info(
            "Radarr: queued %s (upgrade=%s, %d deleted)",
            video, job.is_upgrade, len(deleted),
        )
        return {"status": "queued"}

    return app, job_queue

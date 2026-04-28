"""FastAPI receiver for Sonarr/Radarr webhooks. Public entry: serve(config)."""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

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

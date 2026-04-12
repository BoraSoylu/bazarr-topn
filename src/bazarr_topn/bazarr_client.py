"""Read-only Bazarr API client for media inventory."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from bazarr_topn.config import BazarrConfig

logger = logging.getLogger(__name__)

API_PREFIX = "/api"


@dataclass
class MediaItem:
    title: str
    path: str
    media_type: str  # "movie" or "episode"
    sonarr_series_id: int | None = None
    sonarr_episode_id: int | None = None
    radarr_id: int | None = None
    monitored: bool = True
    existing_subtitles: list[str] | None = None


class BazarrClient:
    """Thin wrapper around Bazarr's REST API (read-only)."""

    def __init__(self, config: BazarrConfig) -> None:
        self.base_url = config.url.rstrip("/")
        self.api_key = config.api_key
        self.session = requests.Session()
        self.session.headers["X-API-KEY"] = self.api_key

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{API_PREFIX}{endpoint}"
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_movies(self) -> list[MediaItem]:
        data = self._get("/movies")
        items = []
        for m in data.get("data", []):
            items.append(
                MediaItem(
                    title=m.get("title", ""),
                    path=m.get("path", ""),
                    media_type="movie",
                    radarr_id=m.get("radarrId"),
                    monitored=m.get("monitored", True),
                    existing_subtitles=[
                        s.get("code2") for s in m.get("subtitles", []) if s.get("path")
                    ],
                )
            )
        return items

    def get_series(self) -> list[dict[str, Any]]:
        data = self._get("/series")
        return data.get("data", [])

    def get_episodes(self, series_id: int) -> list[MediaItem]:
        data = self._get("/episodes", params={"seriesid": series_id})
        items = []
        for ep in data.get("data", []):
            items.append(
                MediaItem(
                    title=ep.get("title", ""),
                    path=ep.get("path", ""),
                    media_type="episode",
                    sonarr_series_id=ep.get("sonarrSeriesId"),
                    sonarr_episode_id=ep.get("sonarrEpisodeId"),
                    monitored=ep.get("monitored", True),
                    existing_subtitles=[
                        s.get("code2") for s in ep.get("subtitles", []) if s.get("path")
                    ],
                )
            )
        return items

    def get_all_episodes(self) -> list[MediaItem]:
        series_list = self.get_series()
        episodes = []
        for s in series_list:
            sid = s.get("sonarrSeriesId")
            if sid is not None:
                episodes.extend(self.get_episodes(sid))
        return episodes

    def health_check(self) -> bool:
        try:
            self._get("/system/status")
            return True
        except Exception:
            return False

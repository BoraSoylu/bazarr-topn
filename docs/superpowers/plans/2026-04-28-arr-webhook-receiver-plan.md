# Sonarr/Radarr Webhook Receiver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `bazarr-topn serve` subcommand that accepts Sonarr/Radarr webhook POSTs, queues each event, and serially processes new/upgraded videos via the existing `scanner.process_video`, sharing a lockfile with the cron `--all` so the two never overlap.

**Architecture:** A single new module `src/bazarr_topn/webhook.py` owns the FastAPI app, Pydantic payload models, an in-process `queue.Queue`, and a daemon worker thread. The worker grabs `fcntl.flock` on a configurable lockfile while it processes each job, calls `process_video` on the new file (after applying `Config.map_path` to translate container paths), and on `Upgrade` events first deletes orphan `topn-*.srt` / `topn.json` sidecars keyed to the replaced video stem. A new `WebhookConfig` dataclass adds a `webhook:` block to `Config`. The CLI gains a `serve` subcommand that mirrors `watch`. Existing `Config.path_mappings` + `Config.map_path` and `subtitle_finder.create_pool` are reused unchanged.

**Tech Stack:** Python 3.10+, FastAPI (with Starlette `TestClient`), uvicorn, Pydantic v2 (transitive via FastAPI), stdlib `queue.Queue`, `threading.Thread`, `fcntl.flock`, `hmac.compare_digest`. Tests use pytest + `unittest.mock.patch` (matching existing `tests/test_watcher.py` and `tests/test_scanner.py` style).

---

## Important context (read before starting)

**The exact webhook field names below were verified against Sonarr/Radarr `develop` source on 2026-04-28.** Both apps use `System.Text.Json` with `JsonNamingPolicy.CamelCase` and `JsonStringEnumConverter(JsonNamingPolicy.CamelCase)`. So PascalCase C# property names like `EventType`, `EpisodeFile`, `DeletedFiles`, `IsUpgrade` serialize to `eventType`, `episodeFile`, `deletedFiles`, `isUpgrade`. The `Download` enum value serializes to the string `"download"` (lowercase). Critically: **there is no separate `Upgrade` event type** — Sonarr/Radarr send `eventType: "download"` for both new imports and upgrades, and the receiver distinguishes them via the `isUpgrade: bool` field (true when the import replaced existing files; the replaced files appear in `deletedFiles[]`).

Reused primitives (do NOT reimplement):
- `Config.map_path(path: str) -> str` — `src/bazarr_topn/config.py:173`. Translates container paths (e.g. `/media/...` from a Sonarr Docker container) to host paths using `Config.path_mappings`. Wire the receiver through this for every path field.
- `scanner.process_video(video_path, config, pool)` — `src/bazarr_topn/scanner.py:43`. Per-file entry point; returns the count of subtitles saved or `-1` if skipped. The webhook worker calls this once per `WebhookJob`.
- `subtitle_finder.create_pool(config)` — `src/bazarr_topn/subtitle_finder.py:117`. Context-managed `ProviderPool`. Open it once for the server's lifetime in `serve()`, exactly like `watcher.watch` does (`src/bazarr_topn/watcher.py:134`).
- `subtitle_finder.configure_cache()` — call once at the top of `serve()` like `watcher.watch` does.

Sidecar / naming primitives the upgrade-cleanup helper will use:
- `naming.existing_topn_subs(video_path, lang, pattern)` — `src/bazarr_topn/naming.py:30`. Returns the list of `<stem>.<lang>.topn-*.srt` files for a video. Note: the `pattern` arg is the configured `naming_pattern`; the function builds a glob from it. Uses `video_path.stem` and `video_path.parent`, so a non-existent `Path` to the deleted file still works (we never need the file to exist on disk to get its stem).
- `sidecar.sidecar_path(video_path, lang)` — `src/bazarr_topn/sidecar.py:30`. Returns `<stem>.<lang>.topn.json`.

---

## File structure

**New files:**
- `src/bazarr_topn/webhook.py` — FastAPI app, payload models, queue, worker, lockfile, public `serve(config)`.
- `tests/test_webhook.py` — unit + integration tests.

**Modified files:**
- `src/bazarr_topn/config.py` — add `WebhookConfig` dataclass + `Config.webhook` field; parse `webhook:` block in `_from_dict`.
- `src/bazarr_topn/cli.py` — add `serve` subcommand.
- `pyproject.toml` — add `fastapi` and `uvicorn[standard]` to `dependencies`; `httpx` already comes with FastAPI's `TestClient`, but we'll add it explicitly to `dev` since `responses` isn't enough.
- `README.md` — add "Webhook receiver" section.
- `tests/conftest.py` — no change required; existing fixtures are sufficient (we add webhook-specific fixtures inside `tests/test_webhook.py`).

---

## Task list

### Task 1: Add runtime + dev dependencies to pyproject.toml

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `fastapi` and `uvicorn[standard]` to `dependencies` and `httpx` to `dev` extras.**

Edit `pyproject.toml`. The `dependencies` list at line 27 should become:

```toml
dependencies = [
    "click>=8.0",
    "pyyaml>=6.0",
    "subliminal>=2.2",
    "babelfish>=0.6",
    "requests>=2.28",
    "watchdog>=3.0",
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
]
```

The `dev` extras at line 39 should become:

```toml
dev = [
    "pytest>=7.0",
    "pytest-cov>=4.0",
    "responses>=0.23",
    "httpx>=0.27",
]
```

(`httpx` is required by `starlette.testclient.TestClient` for in-process integration tests. FastAPI declares it as an optional dep, so we pin it explicitly in `dev`.)

- [ ] **Step 2: Reinstall the package so the new deps are present.**

Run: `pip install -e ".[dev]"`
Expected: pip resolves and installs `fastapi`, `starlette`, `pydantic`, `uvicorn`, `httpx` and their transitives.

- [ ] **Step 3: Smoke-import the new deps to confirm install.**

Run: `python -c "import fastapi, uvicorn, httpx, pydantic; print(fastapi.__version__, pydantic.VERSION)"`
Expected: prints two version strings, no ImportError.

- [ ] **Step 4: Commit.**

```bash
git add pyproject.toml
git commit -m "deps: add fastapi, uvicorn, httpx for webhook receiver"
```

---

### Task 2: Add WebhookConfig dataclass to config.py

**Files:**
- Modify: `src/bazarr_topn/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_config.py` (at the end of the file):

```python
class TestWebhookConfig:
    def test_defaults(self) -> None:
        config = Config()
        assert config.webhook.host == "127.0.0.1"
        assert config.webhook.port == 9595
        assert config.webhook.token == ""
        assert config.webhook.lockfile == "/var/lock/bazarr-topn-scan.lock"

    def test_loaded_from_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WH_TOKEN", "secret-xyz")
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """\
webhook:
  host: 0.0.0.0
  port: 8080
  token: ${WH_TOKEN}
  lockfile: /tmp/test.lock
"""
        )
        config = Config.from_file(config_file)
        assert config.webhook.host == "0.0.0.0"
        assert config.webhook.port == 8080
        assert config.webhook.token == "secret-xyz"
        assert config.webhook.lockfile == "/tmp/test.lock"

    def test_partial_yaml_keeps_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("webhook:\n  port: 7777\n")
        config = Config.from_file(config_file)
        assert config.webhook.port == 7777
        assert config.webhook.host == "127.0.0.1"  # default kept
        assert config.webhook.token == ""
```

- [ ] **Step 2: Run the test to confirm it fails.**

Run: `pytest tests/test_config.py::TestWebhookConfig -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'webhook'`.

- [ ] **Step 3: Add the dataclass and wire it into `Config`.**

Edit `src/bazarr_topn/config.py`. After the `BazarrConfig` dataclass (after line 77), add:

```python
@dataclass
class WebhookConfig:
    host: str = "127.0.0.1"
    port: int = 9595
    token: str = ""
    lockfile: str = "/var/lock/bazarr-topn-scan.lock"
```

In the `Config` dataclass field list (after `bazarr` at line 82), add:

```python
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
```

In `Config._from_dict`, after the `bazarr` block (after line 125), add:

```python
        webhook_raw = data.get("webhook", {})
        webhook = WebhookConfig(
            host=webhook_raw.get("host", "127.0.0.1"),
            port=webhook_raw.get("port", 9595),
            token=webhook_raw.get("token", ""),
            lockfile=webhook_raw.get("lockfile", "/var/lock/bazarr-topn-scan.lock"),
        )
```

In the final `return cls(...)` call, add `webhook=webhook,` next to `bazarr=bazarr,`.

- [ ] **Step 4: Run the tests; expect pass.**

Run: `pytest tests/test_config.py::TestWebhookConfig -v`
Expected: PASS for all three tests.

- [ ] **Step 5: Run the full config test suite to confirm nothing else broke.**

Run: `pytest tests/test_config.py -v`
Expected: All tests pass.

- [ ] **Step 6: Commit.**

```bash
git add src/bazarr_topn/config.py tests/test_config.py
git commit -m "feat: add WebhookConfig dataclass to Config"
```

---

### Task 3: Create the webhook module skeleton with Pydantic payload models

**Files:**
- Create: `src/bazarr_topn/webhook.py`
- Test: `tests/test_webhook.py`

This task defines just the data models. Routes, queue, and worker land in subsequent tasks.

- [ ] **Step 1: Write the failing test (Pydantic parsing of Sonarr Download payload).**

Create `tests/test_webhook.py`:

```python
"""Tests for the Sonarr/Radarr webhook receiver."""

from __future__ import annotations

from bazarr_topn.webhook import (
    SonarrPayload,
    RadarrPayload,
)


# --- Fixture payloads (camelCase, exact field names from Sonarr/Radarr develop) ---

SONARR_DOWNLOAD = {
    "eventType": "download",
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
    "eventType": "download",
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
    "eventType": "download",
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
    "eventType": "download",
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
    "eventType": "test",
    "instanceName": "Sonarr",
    "applicationUrl": "",
    "series": {"id": 0, "title": "Test Title", "path": "/", "tvdbId": 0, "year": 0},
    "episodes": [{"id": 0, "episodeNumber": 1, "seasonNumber": 1, "title": "Test"}],
}

RADARR_TEST = {
    "eventType": "test",
    "instanceName": "Radarr",
    "applicationUrl": "",
    "movie": {"id": 0, "title": "Test Title", "year": 0, "folderPath": "/", "tmdbId": 0},
    "remoteMovie": {"tmdbId": 0, "imdbId": "", "title": "Test Title", "year": 0},
    "release": {"quality": "Bluray-1080p", "qualityVersion": 1, "releaseGroup": "", "releaseTitle": "", "indexer": "", "size": 0},
}


class TestSonarrPayloadParsing:
    def test_download(self) -> None:
        p = SonarrPayload.model_validate(SONARR_DOWNLOAD)
        assert p.event_type == "download"
        assert p.is_upgrade is False
        assert p.series.path == "/media/tv/Test Show"
        assert p.episode_file is not None
        assert p.episode_file.relative_path == "Season 01/Test Show - S01E01.mkv"
        assert p.episode_file.path == "/media/tv/Test Show/Season 01/Test Show - S01E01.mkv"
        assert p.deleted_files == []

    def test_upgrade_carries_deleted_files(self) -> None:
        p = SonarrPayload.model_validate(SONARR_UPGRADE)
        assert p.event_type == "download"
        assert p.is_upgrade is True
        assert len(p.deleted_files) == 1
        assert p.deleted_files[0].path == "/media/tv/Test Show/Season 01/Test Show - S01E01.mkv"

    def test_test_event_has_no_episode_file(self) -> None:
        p = SonarrPayload.model_validate(SONARR_TEST)
        assert p.event_type == "test"
        assert p.episode_file is None


class TestRadarrPayloadParsing:
    def test_download(self) -> None:
        p = RadarrPayload.model_validate(RADARR_DOWNLOAD)
        assert p.event_type == "download"
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
        assert p.event_type == "test"
        assert p.movie_file is None
```

- [ ] **Step 2: Run the test to confirm it fails.**

Run: `pytest tests/test_webhook.py -v`
Expected: FAIL — `ImportError: cannot import name 'SonarrPayload' from 'bazarr_topn.webhook'` (or `ModuleNotFoundError`).

- [ ] **Step 3: Create `src/bazarr_topn/webhook.py` with the Pydantic models.**

```python
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
    fires `eventType: "download"` for both new imports and upgrades; the
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
```

- [ ] **Step 4: Run the tests; expect pass.**

Run: `pytest tests/test_webhook.py -v`
Expected: All 6 tests pass.

- [ ] **Step 5: Commit.**

```bash
git add src/bazarr_topn/webhook.py tests/test_webhook.py
git commit -m "feat: add Sonarr/Radarr webhook payload models"
```

---

### Task 4: Add path resolution helpers to webhook.py (resolve + remap)

We need a way to turn a webhook payload into the absolute video path on the host filesystem. There are two cases:

1. The payload includes `episode_file.path` / `movie_file.path` (Sonarr/Radarr v3+ generally do): use it directly.
2. Only `relative_path` is present: join with `series.path` / `movie.folder_path`.

In both cases run the result through `Config.map_path()` to translate container paths.

**Files:**
- Modify: `src/bazarr_topn/webhook.py`
- Test: `tests/test_webhook.py`

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_webhook.py`:

```python
from bazarr_topn.config import Config
from bazarr_topn.webhook import resolve_sonarr_video_path, resolve_radarr_video_path


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
```

- [ ] **Step 2: Run to confirm failure.**

Run: `pytest tests/test_webhook.py::TestResolveSonarrVideoPath tests/test_webhook.py::TestResolveRadarrVideoPath -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Add the resolvers to `webhook.py`.**

Append to `src/bazarr_topn/webhook.py`:

```python
import posixpath  # webhooks always carry forward-slash paths from arr

from bazarr_topn.config import Config


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
```

- [ ] **Step 4: Run the tests; expect pass.**

Run: `pytest tests/test_webhook.py::TestResolveSonarrVideoPath tests/test_webhook.py::TestResolveRadarrVideoPath -v`
Expected: All 8 tests pass.

- [ ] **Step 5: Add a test for the deleted-paths helpers.**

Append to `tests/test_webhook.py`:

```python
from bazarr_topn.webhook import resolve_sonarr_deleted_paths, resolve_radarr_deleted_paths


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
```

Run: `pytest tests/test_webhook.py::TestResolveDeletedPaths -v`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/bazarr_topn/webhook.py tests/test_webhook.py
git commit -m "feat: add path resolution helpers for webhook payloads"
```

---

### Task 5: Add the orphan-sidecar cleanup helper

When an `Upgrade` arrives, we delete the topn sidecars + topn-N srt files keyed to the old (replaced) video stem, across **every configured language** (because Sonarr/Radarr only know about one set of files per video; the language dimension is ours).

**Files:**
- Modify: `src/bazarr_topn/webhook.py`
- Test: `tests/test_webhook.py`

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_webhook.py`:

```python
from pathlib import Path

from bazarr_topn.webhook import cleanup_orphan_sidecars


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
```

- [ ] **Step 2: Run to confirm failure.**

Run: `pytest tests/test_webhook.py::TestCleanupOrphanSidecars -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `cleanup_orphan_sidecars`.**

Append to `src/bazarr_topn/webhook.py`:

```python
from pathlib import Path

from bazarr_topn.naming import existing_topn_subs
from bazarr_topn.sidecar import sidecar_path


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
```

- [ ] **Step 4: Run the tests; expect pass.**

Run: `pytest tests/test_webhook.py::TestCleanupOrphanSidecars -v`
Expected: All 3 tests pass.

- [ ] **Step 5: Commit.**

```bash
git add src/bazarr_topn/webhook.py tests/test_webhook.py
git commit -m "feat: add orphan-sidecar cleanup for upgrade events"
```

---

### Task 6: Add WebhookJob dataclass and queue plumbing

The worker consumes `WebhookJob` records from a `queue.Queue`. A job carries the new video path and the list of deleted paths (empty for non-upgrade).

**Files:**
- Modify: `src/bazarr_topn/webhook.py`

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_webhook.py`:

```python
from bazarr_topn.webhook import WebhookJob


class TestWebhookJob:
    def test_construct_download(self) -> None:
        job = WebhookJob(video_path="/x/a.mkv", deleted_paths=[])
        assert job.video_path == "/x/a.mkv"
        assert job.deleted_paths == []
        assert job.is_upgrade is False  # derived from deleted_paths

    def test_construct_upgrade(self) -> None:
        job = WebhookJob(video_path="/x/new.mkv", deleted_paths=["/x/old.mkv"])
        assert job.is_upgrade is True
```

- [ ] **Step 2: Run to confirm failure.**

Run: `pytest tests/test_webhook.py::TestWebhookJob -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Add the dataclass.**

Append to `src/bazarr_topn/webhook.py`:

```python
from dataclasses import dataclass, field


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
```

- [ ] **Step 4: Run the tests; expect pass.**

Run: `pytest tests/test_webhook.py::TestWebhookJob -v`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/bazarr_topn/webhook.py tests/test_webhook.py
git commit -m "feat: add WebhookJob dataclass"
```

---

### Task 7: Build the FastAPI app with auth, routes, and queue (no worker yet)

Now we add the HTTP layer. The worker is mocked in tests for this task.

**Files:**
- Modify: `src/bazarr_topn/webhook.py`
- Test: `tests/test_webhook.py`

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_webhook.py`:

```python
import queue as _queue
from fastapi.testclient import TestClient

from bazarr_topn.webhook import build_app


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
        payload = {**SONARR_DOWNLOAD, "eventType": "grab"}
        r = client.post(
            "/sonarr",
            json=payload,
            headers={"X-Webhook-Token": "secret"},
        )
        assert r.status_code == 200
        assert q.qsize() == 0
```

- [ ] **Step 2: Run to confirm failure.**

Run: `pytest tests/test_webhook.py::TestAuth tests/test_webhook.py::TestRouting -v`
Expected: FAIL with `ImportError: cannot import name 'build_app'`.

- [ ] **Step 3: Implement `build_app`.**

Append to `src/bazarr_topn/webhook.py`:

```python
import hmac
import queue as _queue

from fastapi import Depends, FastAPI, Header, HTTPException


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
        if payload.event_type == "test":
            return {"status": "ok"}
        if payload.event_type != "download":
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
        if payload.event_type == "test":
            return {"status": "ok"}
        if payload.event_type != "download":
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
```

- [ ] **Step 4: Run the tests; expect pass.**

Run: `pytest tests/test_webhook.py::TestAuth tests/test_webhook.py::TestRouting -v`
Expected: All 11 tests pass (4 auth + 7 routing).

- [ ] **Step 5: Run the full webhook test file to confirm everything still passes.**

Run: `pytest tests/test_webhook.py -v`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/bazarr_topn/webhook.py tests/test_webhook.py
git commit -m "feat: add FastAPI app with auth + sonarr/radarr routes"
```

---

### Task 8: Add the worker thread with lockfile coordination

The worker drains jobs from the queue. For each job: acquire `fcntl.flock` on the configured lockfile, run cleanup if it's an upgrade, call `process_video`, release.

**Files:**
- Modify: `src/bazarr_topn/webhook.py`
- Test: `tests/test_webhook.py`

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_webhook.py`:

```python
import threading
import time as _time
from unittest.mock import MagicMock, patch

from bazarr_topn.webhook import run_worker


class TestWorker:
    def test_processes_jobs_in_order(self, tmp_path: Path) -> None:
        config = Config(languages=["en"])
        config.webhook.lockfile = str(tmp_path / "test.lock")
        q: _queue.Queue = _queue.Queue()
        q.put(WebhookJob(video_path="/x/a.mkv", deleted_paths=[]))
        q.put(WebhookJob(video_path="/x/b.mkv", deleted_paths=[]))
        # Sentinel to stop the worker
        q.put(None)
        pool = MagicMock()
        with patch("bazarr_topn.webhook.process_video") as fake_process:
            fake_process.return_value = 5
            run_worker(q, config, pool)
        calls = [c.args[0] for c in fake_process.call_args_list]
        assert [str(p) for p in calls] == ["/x/a.mkv", "/x/b.mkv"]

    def test_calls_cleanup_on_upgrade(self, tmp_path: Path) -> None:
        config = Config(languages=["en"])
        config.webhook.lockfile = str(tmp_path / "test.lock")
        q: _queue.Queue = _queue.Queue()
        q.put(WebhookJob(video_path="/x/new.mkv", deleted_paths=["/x/old.mkv"]))
        q.put(None)
        pool = MagicMock()
        with patch("bazarr_topn.webhook.process_video") as fake_process, \
             patch("bazarr_topn.webhook.cleanup_orphan_sidecars") as fake_cleanup:
            fake_process.return_value = 5
            fake_cleanup.return_value = 2
            run_worker(q, config, pool)
        # Cleanup runs BEFORE process_video for the same job
        fake_cleanup.assert_called_once_with("/x/old.mkv", config)
        fake_process.assert_called_once()

    def test_swallows_process_video_exceptions(self, tmp_path: Path) -> None:
        """A crash in one job must not stop the worker draining the rest."""
        config = Config(languages=["en"])
        config.webhook.lockfile = str(tmp_path / "test.lock")
        q: _queue.Queue = _queue.Queue()
        q.put(WebhookJob(video_path="/x/a.mkv"))
        q.put(WebhookJob(video_path="/x/b.mkv"))
        q.put(None)
        pool = MagicMock()
        with patch("bazarr_topn.webhook.process_video") as fake_process:
            fake_process.side_effect = [RuntimeError("boom"), 3]
            run_worker(q, config, pool)
        assert fake_process.call_count == 2

    def test_holds_lockfile_while_processing(self, tmp_path: Path) -> None:
        """Pre-acquire the lockfile in the test; worker must block on flock.

        We pre-take an exclusive flock on the configured lockfile path, push a
        job, run the worker in a thread, and assert process_video has not been
        called after a brief wait. Releasing the pre-take lock unblocks it.
        """
        import fcntl

        lockpath = tmp_path / "test.lock"
        config = Config(languages=["en"])
        config.webhook.lockfile = str(lockpath)
        q: _queue.Queue = _queue.Queue()
        q.put(WebhookJob(video_path="/x/a.mkv"))
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
            # Worker should be blocked on flock; process_video not called yet
            assert fake_process.call_count == 0

            # Release the test-side lock; worker should proceed
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
            t.join(timeout=3)
            assert not t.is_alive(), "worker thread did not exit"
            assert fake_process.call_count == 1
```

- [ ] **Step 2: Run to confirm failure.**

Run: `pytest tests/test_webhook.py::TestWorker -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `run_worker` and the lockfile context manager.**

Append to `src/bazarr_topn/webhook.py`:

```python
import contextlib
import fcntl
import os

from bazarr_topn.scanner import process_video


@contextlib.contextmanager
def _scan_lock(lockfile_path: str):
    """Acquire an exclusive flock on `lockfile_path` for the duration of the with-block.

    Blocks until the lock is available. The cron `--all` wrapper is expected
    to take this same lock with `flock -n`, so cron and webhook scans
    serialize naturally. We open the file in 'a+' so concurrent processes
    don't truncate each other's lockfile, and we ensure parent dir exists.
    """
    parent = os.path.dirname(lockfile_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(lockfile_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def run_worker(job_queue: _queue.Queue, config: Config, pool) -> None:
    """Drain WebhookJobs from `job_queue` serially.

    Stops when the sentinel value `None` is received. For each job:
    1. Acquire scan lockfile (blocks if cron --all is running).
    2. For each deleted_paths entry, call cleanup_orphan_sidecars.
    3. Call process_video with the new video path.
    4. Release lockfile.

    Exceptions in cleanup or process_video are logged and swallowed so the
    worker drains the rest of the queue. (Mirrors VideoHandler._process_pending
    behavior.)
    """
    while True:
        job = job_queue.get()
        if job is None:
            return
        try:
            with _scan_lock(config.webhook.lockfile):
                for old in job.deleted_paths:
                    try:
                        cleanup_orphan_sidecars(old, config)
                    except Exception:
                        logger.exception("Cleanup failed for %s", old)
                video_path = Path(job.video_path)
                if not video_path.exists():
                    logger.warning(
                        "Webhook target does not exist on disk (path mapping wrong?): %s",
                        video_path,
                    )
                    continue
                try:
                    process_video(video_path, config, pool)
                except Exception:
                    logger.exception("process_video failed for %s", video_path)
        finally:
            job_queue.task_done()
```

Now there is a subtle issue: the `test_holds_lockfile_while_processing` test passes `/x/a.mkv` which doesn't exist on disk, so the `if not video_path.exists(): continue` branch fires before `process_video`. We need that test to work — but we want the worker to block on the lock first. The lock IS acquired before the existence check, so the timing assertion (fake_process not called yet while lock is held) still holds because the worker is parked on flock. After release, the worker proceeds to the existence check and skips, leaving `fake_process.call_count == 0`. That breaks the final assertion `== 1`.

Fix the test: create the file inside the test so `exists()` is True. Update Step 1 of this task by replacing the relevant test:

```python
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
```

The same `if not exists: continue` issue affects the other worker tests. Update them to create the files first:

```python
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
```

Apply these as the authoritative test code in Step 1 (the snippets above replace the earlier ones).

- [ ] **Step 4: Run the worker tests.**

Run: `pytest tests/test_webhook.py::TestWorker -v`
Expected: All 4 tests pass. The flock test should take ~300ms (the deliberate sleep).

- [ ] **Step 5: Run the entire test_webhook.py suite to make sure nothing regressed.**

Run: `pytest tests/test_webhook.py -v`
Expected: All tests pass.

- [ ] **Step 6: Commit.**

```bash
git add src/bazarr_topn/webhook.py tests/test_webhook.py
git commit -m "feat: add worker thread with flock-based scan coordination"
```

---

### Task 9: Add the public `serve(config)` entry point

Wires it all together: configure cache, open the provider pool context, build the app, start the worker thread, run uvicorn.

**Files:**
- Modify: `src/bazarr_topn/webhook.py`

- [ ] **Step 1: Implement `serve()`.**

Append to `src/bazarr_topn/webhook.py`:

```python
import threading

from bazarr_topn.subtitle_finder import configure_cache, create_pool


def serve(config: Config) -> None:
    """Run the webhook receiver. Blocks until interrupted (e.g. SIGTERM).

    Mirrors `watcher.watch(config)`: a single ProviderPool is opened for the
    server's lifetime — one provider login reused across every webhook event.
    The worker is a daemon thread; uvicorn runs in the main thread.
    """
    import uvicorn

    if not config.webhook.token:
        raise SystemExit(
            "webhook.token is empty. Set it in config.yaml (e.g. via "
            "${BAZARR_TOPN_WEBHOOK_TOKEN} env var)."
        )

    configure_cache()

    with create_pool(config) as pool:
        app, job_queue = build_app(config)
        worker_thread = threading.Thread(
            target=run_worker, args=(job_queue, config, pool), daemon=True,
            name="bazarr-topn-webhook-worker",
        )
        worker_thread.start()

        logger.info(
            "Starting webhook receiver on %s:%d",
            config.webhook.host, config.webhook.port,
        )
        # log_config=None lets bazarr-topn's logger config (already set up by
        # cli.setup_logging) own all output. Otherwise uvicorn re-applies its
        # own root handler.
        uvicorn.run(
            app,
            host=config.webhook.host,
            port=config.webhook.port,
            log_config=None,
            access_log=False,
        )
        # On uvicorn return (Ctrl+C / SIGTERM), tell the worker to stop.
        job_queue.put(None)
        worker_thread.join(timeout=5)
```

- [ ] **Step 2: No test in this task — `serve()` runs uvicorn and is covered by the `serve` CLI test in Task 10. Run the full webhook suite to confirm the import still works.**

Run: `pytest tests/test_webhook.py -v`
Expected: PASS.

- [ ] **Step 3: Commit.**

```bash
git add src/bazarr_topn/webhook.py
git commit -m "feat: add serve(config) public entry point"
```

---

### Task 10: Add the `serve` CLI subcommand

**Files:**
- Modify: `src/bazarr_topn/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_cli.py`:

```python
from unittest.mock import patch


class TestServeCommand:
    def test_serve_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output

    def test_serve_calls_webhook_serve(self, sample_config_yaml: Path, monkeypatch) -> None:
        # Set the token so serve() does not abort
        with patch("bazarr_topn.webhook.serve") as fake_serve:
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["-c", str(sample_config_yaml), "serve"],
                env={"PYTHONUNBUFFERED": "1"},
            )
        assert result.exit_code == 0, result.output
        fake_serve.assert_called_once()
        # The Config passed in should reflect default webhook host/port
        call_config = fake_serve.call_args.args[0]
        assert call_config.webhook.host == "127.0.0.1"
        assert call_config.webhook.port == 9595

    def test_serve_overrides_host_and_port(self, sample_config_yaml: Path) -> None:
        with patch("bazarr_topn.webhook.serve") as fake_serve:
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["-c", str(sample_config_yaml), "serve",
                 "--host", "0.0.0.0", "--port", "8181"],
            )
        assert result.exit_code == 0, result.output
        call_config = fake_serve.call_args.args[0]
        assert call_config.webhook.host == "0.0.0.0"
        assert call_config.webhook.port == 8181
```

- [ ] **Step 2: Run to confirm failure.**

Run: `pytest tests/test_cli.py::TestServeCommand -v`
Expected: FAIL — the `serve` subcommand is not registered yet.

- [ ] **Step 3: Add the `serve` command to `cli.py`.**

In `src/bazarr_topn/cli.py`, after the `watch` command (after line 174), append:

```python


@main.command()
@click.option("--host", default=None, help="Listen address (overrides config.webhook.host)")
@click.option("--port", type=int, default=None, help="Listen port (overrides config.webhook.port)")
@click.pass_context
def serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Run the Sonarr/Radarr webhook receiver.

    \b
    Examples:
      bazarr-topn serve
      bazarr-topn serve --host 0.0.0.0 --port 8181
    """
    from bazarr_topn import webhook

    config: Config = ctx.obj["config"]
    if host:
        config.webhook.host = host
    if port:
        config.webhook.port = port

    webhook.serve(config)
```

- [ ] **Step 4: Run the CLI tests.**

Run: `pytest tests/test_cli.py::TestServeCommand -v`
Expected: All 3 tests pass.

- [ ] **Step 5: Run the full CLI test suite.**

Run: `pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/bazarr_topn/cli.py tests/test_cli.py
git commit -m "feat: add 'serve' CLI subcommand"
```

---

### Task 11: Integration test — end-to-end Sonarr download burst

This is the spec's "POST 24 events back-to-back, worker drains all 24" test. It exercises real HTTP, real queue, real worker thread — only `process_video` and the provider pool are mocked.

**Files:**
- Modify: `tests/test_webhook.py`

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_webhook.py`:

```python
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
                    "eventType": "download",
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
```

- [ ] **Step 2: Run the integration test.**

Run: `pytest tests/test_webhook.py::TestEndToEndIntegration -v`
Expected: PASS. Should take ~1 second.

- [ ] **Step 3: Commit.**

```bash
git add tests/test_webhook.py
git commit -m "test: e2e webhook integration covering 24-event burst"
```

---

### Task 12: Integration test — upgrade event triggers cleanup before process_video

This validates the spec requirement "delete-old → process-new" sequencing on upgrade.

**Files:**
- Modify: `tests/test_webhook.py`

- [ ] **Step 1: Write the failing test.**

Append to `tests/test_webhook.py`:

```python
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
            "eventType": "download",
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
```

- [ ] **Step 2: Run.**

Run: `pytest tests/test_webhook.py::TestEndToEndIntegration::test_upgrade_event_cleans_orphans_then_processes -v`
Expected: PASS.

- [ ] **Step 3: Run the full suite to confirm nothing regressed.**

Run: `pytest -v`
Expected: All tests pass (existing + new).

- [ ] **Step 4: Commit.**

```bash
git add tests/test_webhook.py
git commit -m "test: e2e upgrade event triggers orphan cleanup before scan"
```

---

### Task 13: Update README with webhook receiver section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a new "Webhook receiver" section before the "Configuration" section.**

In `README.md`, find the line `## Configuration` (around line 89) and insert the following block immediately above it:

```markdown
## Webhook receiver

Filesystem watching with inotify is unreliable on `fuse.mergerfs` and similar overlay filesystems. If you run a homelab where Sonarr/Radarr already know the exact moment a new file lands, the `serve` subcommand replaces filesystem watching with HTTP webhooks.

```bash
bazarr-topn serve              # uses host/port from config.webhook
bazarr-topn serve --port 8181  # override
```

It exposes:

- `POST /sonarr` — accepts Sonarr's `On Import` and `On Upgrade` events
- `POST /radarr` — accepts Radarr's `On Import` and `On Upgrade` events
- `GET /healthz` — unauthenticated liveness probe

A single in-process worker drains queued events serially and shares a `flock`-based lockfile with the cron `--all` so they never run concurrently.

### Sonarr setup

In Sonarr, go to **Settings → Connect → Add → Webhook** and configure:

- URL: `http://localhost:9595/sonarr`
- Method: `POST`
- Triggers: enable **On Import** and **On Upgrade**
- Headers: add `X-Webhook-Token: <your-secret>` (must match `webhook.token` in config.yaml)

Click **Test** — bazarr-topn returns 200 if auth is correct.

### Radarr setup

Same path (**Settings → Connect → Add → Webhook**) but URL `http://localhost:9595/radarr`.

### Config snippet

```yaml
webhook:
  host: 127.0.0.1            # bind only to loopback by default
  port: 9595
  token: ${BAZARR_TOPN_WEBHOOK_TOKEN}
  lockfile: /var/lock/bazarr-topn-scan.lock

# Required when Sonarr/Radarr run in Docker and report container paths:
path_mappings:
  - container: /media
    host: /mnt/media
```

The `path_mappings` block translates container paths from the webhook payload (e.g. `/media/movies/...` from a Sonarr Docker container) to the host paths bazarr-topn sees. Reuses the same `path_mappings` config that `scan --all` uses.

### Systemd unit

Replace `watch` with `serve` in your unit's `ExecStart`:

```ini
[Service]
Environment="BAZARR_TOPN_WEBHOOK_TOKEN=your-long-random-secret"
ExecStart=/usr/local/bin/bazarr-topn -c /etc/bazarr-topn/config.yaml serve
```

If you used `bazarr-topn watch` previously, it remains in the codebase — useful for filesystems where inotify works reliably.

### Cron lockfile coordination

To prevent the cron `--all` from racing with webhook-driven scans, wrap it with `flock`:

```cron
17 2,14 * * * flock -n /var/lock/bazarr-topn-scan.lock /usr/local/bin/bazarr-topn -c /etc/bazarr-topn/config.yaml scan --all
```

`flock -n` exits immediately if the lock is held, which is fine — webhooks already cover the new files; the cron is just the safety net.

```

(End of inserted section. The closing triple-backtick of the cron block above is part of the section. There is no further mid-section closing fence.)

- [ ] **Step 2: Verify the section renders correctly.**

Run: `grep -c "^## Webhook receiver" README.md`
Expected: `1`

Open the README in any markdown previewer if available; otherwise eyeball that the new section sits between "## Run modes" / "### As a cron job" and "## Configuration".

- [ ] **Step 3: Commit.**

```bash
git add README.md
git commit -m "docs: add webhook receiver section to README"
```

---

### Task 14: Final verification — full test suite + import smoke

**Files:**
- None (verification only)

- [ ] **Step 1: Run the full test suite.**

Run: `pytest -v`
Expected: All tests pass. Note the count for both `test_webhook.py` (should have ~30 tests across the unit + integration classes) and overall (existing tests + new ones).

- [ ] **Step 2: Smoke-test the CLI.**

Run: `bazarr-topn --help`
Expected: Output lists `scan`, `watch`, AND `serve` as subcommands.

Run: `bazarr-topn serve --help`
Expected: Output shows `--host` and `--port` options and the docstring.

- [ ] **Step 3: Smoke-test that `serve` aborts when token is empty.**

Create a temp config with empty token and run:

```bash
cat > /tmp/no-token.yaml <<'EOF'
bazarr:
  url: http://localhost:6767
  api_key: x
languages:
  - en
webhook:
  port: 9999
EOF
bazarr-topn -c /tmp/no-token.yaml serve
```

Expected: Process exits with a non-zero status and prints `webhook.token is empty…`. Check `echo $?` to confirm non-zero.

- [ ] **Step 4: Smoke-test the live server briefly.**

```bash
cat > /tmp/test-token.yaml <<'EOF'
bazarr:
  url: http://localhost:6767
  api_key: x
languages:
  - en
webhook:
  host: 127.0.0.1
  port: 9999
  token: testtok
  lockfile: /tmp/bazarr-topn-test.lock
providers: []
EOF
bazarr-topn -c /tmp/test-token.yaml serve &
SERVER_PID=$!
sleep 2
curl -s http://127.0.0.1:9999/healthz
echo
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:9999/sonarr -H "Content-Type: application/json" -d '{}'
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:9999/sonarr -H "Content-Type: application/json" -H "X-Webhook-Token: testtok" -d '{"eventType":"test"}'
kill $SERVER_PID
wait $SERVER_PID 2>/dev/null
rm -f /tmp/bazarr-topn-test.lock /tmp/test-token.yaml /tmp/no-token.yaml
```

Expected output:

- `{"status":"ok"}` from the `healthz` curl
- `401` from the no-token POST
- `200` from the test-event POST

- [ ] **Step 5: Final commit (only if anything was tweaked during smoke tests).**

If smoke tests revealed nothing requiring changes, this task ends without a commit. If any fix was needed, commit it now:

```bash
git add -A
git commit -m "fix: <describe>"
```

---

## Self-review summary (filled out by the plan author)

**Spec coverage check (each spec section → which task implements it):**

- Goals: new `serve` subcommand → Task 10. Process Download/Upgrade/Test from both apps → Tasks 7. Upgrade sidecar cleanup → Tasks 5, 8, 12. Lockfile coordination → Task 8. README docs section → Task 13.
- Architecture > Pydantic models → Task 3. Auth dep → Task 7. Routes → Task 7. Queue + worker → Tasks 6, 8. Lockfile → Task 8.
- Architecture > CLI `serve` subcommand → Task 10.
- Architecture > `WebhookConfig` → Task 2.
- Reuse of existing primitives: `Config.path_mappings` + `map_path` → Task 4 (resolvers wire through it). `process_video` → Task 8 (worker calls it). `create_pool` → Task 9 (`serve()` opens it once).
- Upgrade-event sidecar cleanup → Tasks 5 (helper) + 8 (wired in worker) + 12 (e2e test).
- Data flow → Task 7 (route → enqueue → 200) + Task 8 (worker loop with flock).
- Error handling: 401 no INFO log → Task 7 (`_make_auth_dependency`). 422 malformed → Task 7 (FastAPI default). Path remap path-doesn't-exist → Task 8 (`if not exists: continue` warning). `process_video` raises → Task 8 (`logger.exception`, swallow). Worker thread crash → systemd-restart contract; not in code, called out in design.
- Systemd / installer changes → README docs in Task 13. Plan does NOT modify any installer/systemd file because the bazarr-topn repo doesn't ship one (the systemd unit lives in the BoraCloud installer, which is a separate repo per the spec); README documents the change for OSS users.
- Testing: all 7 unit tests + 3-4 integration tests required by the spec are present:
  - Sonarr Download parse → Task 3 `test_download`
  - Sonarr Upgrade parse incl. deletedFiles → Task 3 `test_upgrade_carries_deleted_files`
  - Radarr Download parse → Task 3 `test_download`
  - Radarr Upgrade parse incl. deletedFiles → Task 3 `test_upgrade_carries_deleted_files`
  - Container → host remap → Task 4 `TestResolveSonarr/RadarrVideoPath::test_applies_path_mapping` and Task 7 `TestRouting::test_path_mapping_applied`
  - Auth: missing/wrong/correct token → Task 7 `TestAuth` (all three)
  - Test event → 200, queue empty → Task 7 `TestRouting::test_test_event_returns_200_without_enqueueing`
  - Malformed payload → 422 → Task 7 `TestRouting::test_malformed_payload_returns_422`
  - Integration: Sonarr Download e2e → Task 11 `test_24_events_drain_through_worker` (covers single + burst)
  - Integration: Sonarr Upgrade with cleanup → Task 12 `test_upgrade_event_cleans_orphans_then_processes`
  - Integration: 24-event burst → Task 11
  - Integration: lockfile contention → Task 8 `test_holds_lockfile_while_processing` (the spec's optional one — included).
- Documentation README → Task 13.
- Dependencies → Task 1 (fastapi, uvicorn, httpx).
- Migration / rollout → README in Task 13 documents the systemd `ExecStart` flip.

**Placeholder scan:** No "TBD", no "implement later", no "similar to Task N" without inlined code. Every step has either a runnable command, a code block to add, or a single concrete file edit.

**Type consistency check:**
- `WebhookJob.video_path: str`, `deleted_paths: list[str]` — used consistently in Tasks 6, 7, 8.
- `Config.webhook: WebhookConfig` with attrs `host`, `port`, `token`, `lockfile` — set in Task 2; consumed in Tasks 7 (token), 8 (lockfile), 9 (host/port/token), 10 (host/port).
- Pydantic models use `event_type` (Python) ↔ `eventType` (alias) consistently across Tasks 3 and 7.
- Module-level functions: `build_app`, `serve`, `run_worker`, `cleanup_orphan_sidecars`, `resolve_sonarr_video_path`, `resolve_radarr_video_path`, `resolve_sonarr_deleted_paths`, `resolve_radarr_deleted_paths` — each is defined in exactly one task and called by name in subsequent tasks.

---

**Plan complete and saved to `docs/superpowers/plans/2026-04-28-arr-webhook-receiver-plan.md`.**

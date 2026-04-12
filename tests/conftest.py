"""Shared fixtures for bazarr-topn tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from bazarr_topn.config import Config


@pytest.fixture
def tmp_video(tmp_path: Path) -> Path:
    """Create a fake video file for testing."""
    video = tmp_path / "Test Movie (2024)" / "Test Movie (2024).mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00" * 1024)
    return video


@pytest.fixture
def default_config() -> Config:
    """Return a config with sensible test defaults."""
    return Config(
        languages=["en"],
        top_n=3,
        min_score=0,
        naming_pattern="{video_stem}.{lang}.topn-{rank}.srt",
    )


@pytest.fixture
def sample_config_yaml(tmp_path: Path) -> Path:
    """Write a sample config.yaml and return its path."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """\
bazarr:
  url: "http://localhost:6767"
  api_key: "test-api-key"

languages:
  - en
  - tr

top_n: 5
min_score: 20
max_downloads_per_cycle: 100

naming_pattern: "{video_stem}.{lang}.topn-{rank}.srt"

providers:
  - name: opensubtitlescom
    username: "testuser"
    password: "testpass"

ffsubsync:
  enabled: false

watch_paths:
  - /tmp/test-media

watch_cooldown: 10
log_level: DEBUG
"""
    )
    return config_file


@pytest.fixture
def config_with_env_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a config that uses env vars and set those vars."""
    monkeypatch.setenv("TEST_BAZARR_KEY", "secret-key-123")
    monkeypatch.setenv("TEST_OS_USER", "myuser")
    monkeypatch.setenv("TEST_OS_PASS", "mypass")

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """\
bazarr:
  url: "http://localhost:6767"
  api_key: "${TEST_BAZARR_KEY}"

languages:
  - en

top_n: 10

providers:
  - name: opensubtitlescom
    username: "${TEST_OS_USER}"
    password: "${TEST_OS_PASS}"
"""
    )
    return config_file

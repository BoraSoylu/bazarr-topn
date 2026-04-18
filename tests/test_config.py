"""Tests for configuration loading and env var expansion."""

from __future__ import annotations

from pathlib import Path

import pytest

from bazarr_topn.config import Config, _expand_env


class TestEnvExpansion:
    def test_expand_single_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FOO", "bar")
        assert _expand_env("${FOO}") == "bar"

    def test_expand_multiple_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOST", "localhost")
        monkeypatch.setenv("PORT", "6767")
        assert _expand_env("http://${HOST}:${PORT}") == "http://localhost:6767"

    def test_expand_missing_var_raises(self) -> None:
        with pytest.raises(ValueError, match="NONEXISTENT_VAR_12345"):
            _expand_env("${NONEXISTENT_VAR_12345}")

    def test_no_vars_unchanged(self) -> None:
        assert _expand_env("plain string") == "plain string"


class TestConfigFromFile:
    def test_load_basic(self, sample_config_yaml: Path) -> None:
        config = Config.from_file(sample_config_yaml)
        assert config.bazarr.url == "http://localhost:6767"
        assert config.bazarr.api_key == "test-api-key"
        assert config.languages == ["en", "tr"]
        assert config.top_n == 5
        assert config.min_score == 20
        assert config.max_downloads_per_cycle == 100
        assert len(config.providers) == 1
        assert config.providers[0].name == "opensubtitlescom"
        assert config.ffsubsync.enabled is False
        assert config.watch_cooldown == 10
        assert config.log_level == "DEBUG"

    def test_load_with_env_vars(self, config_with_env_vars: Path) -> None:
        config = Config.from_file(config_with_env_vars)
        assert config.bazarr.api_key == "secret-key-123"
        assert config.providers[0].username == "myuser"
        assert config.providers[0].password == "mypass"

    def test_provider_configs(self, sample_config_yaml: Path) -> None:
        config = Config.from_file(sample_config_yaml)
        assert config.provider_names == ["opensubtitlescom"]
        assert config.provider_configs == {
            "opensubtitlescom": {"username": "testuser", "password": "testpass", "max_result_pages": 3}
        }


class TestConfigDefaults:
    def test_defaults(self) -> None:
        config = Config()
        assert config.top_n == 10
        assert config.min_score == 30
        assert config.max_downloads_per_cycle == 0
        assert config.languages == ["en"]
        assert config.providers == []
        assert config.ffsubsync.enabled is False
        assert config.log_file is None

    def test_from_empty_dict(self) -> None:
        config = Config.from_dict({})
        assert config.top_n == 10
        assert config.bazarr.url == "http://localhost:6767"


class TestNewConfigDefaults:
    def test_topn_recheck_days_default(self) -> None:
        config = Config()
        assert config.topn_recheck_days == 3

    def test_topn_sidecar_enabled_default(self) -> None:
        config = Config()
        assert config.topn_sidecar_enabled is True

    def test_max_candidates_tried_default(self) -> None:
        config = Config()
        assert config.max_candidates_tried == 50

    def test_default_is_three(self) -> None:
        from bazarr_topn.config import Config
        cfg = Config()
        assert cfg.topn_recheck_days == 3


class TestPathMappings:
    def test_no_mappings(self) -> None:
        config = Config()
        assert config.map_path("/media/movies/Inception/Inception.mkv") == "/media/movies/Inception/Inception.mkv"

    def test_mapping_applied(self) -> None:
        config = Config(path_mappings=[{"container": "/media", "host": "/mnt/media"}])
        assert config.map_path("/media/movies/Inception/Inception.mkv") == "/mnt/media/movies/Inception/Inception.mkv"

    def test_no_match(self) -> None:
        config = Config(path_mappings=[{"container": "/data", "host": "/mnt/data"}])
        assert config.map_path("/media/movies/Inception/Inception.mkv") == "/media/movies/Inception/Inception.mkv"

    def test_first_match_wins(self) -> None:
        config = Config(path_mappings=[
            {"container": "/media/movies", "host": "/mnt/fast/movies"},
            {"container": "/media", "host": "/mnt/media"},
        ])
        assert config.map_path("/media/movies/Inception/Inception.mkv") == "/mnt/fast/movies/Inception/Inception.mkv"

"""Tests for sidecar metadata — full truth table."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bazarr_topn.config import Config
from bazarr_topn.sidecar import (
    SidecarData,
    is_topn_done,
    read_sidecar,
    sidecar_path,
    write_sidecar,
    delete_sidecar,
)


@pytest.fixture
def video(tmp_path: Path) -> Path:
    v = tmp_path / "Movie (2024).mkv"
    v.write_bytes(b"\x00" * 64)
    return v


@pytest.fixture
def cfg() -> Config:
    return Config(
        languages=["en"],
        top_n=10,
        min_score=0,
        topn_recheck_days=30,
        topn_sidecar_enabled=True,
    )


class TestSidecarPath:
    def test_builds_correct_path(self, video: Path) -> None:
        p = sidecar_path(video, "en")
        assert p == video.parent / "Movie (2024).en.topn.json"

    def test_different_langs(self, video: Path) -> None:
        assert sidecar_path(video, "en") != sidecar_path(video, "tr")
        assert sidecar_path(video, "tr").name == "Movie (2024).tr.topn.json"


class TestWriteAndRead:
    def test_roundtrip(self, video: Path) -> None:
        data = SidecarData(target=10, saved=5, available=8, clean=False)
        write_sidecar(video, "en", data)
        loaded = read_sidecar(video, "en")
        assert loaded is not None
        assert loaded.target == 10
        assert loaded.saved == 5
        assert loaded.available == 8
        assert loaded.clean is False
        assert loaded.completed_at is not None

    def test_write_sets_completed_at(self, video: Path) -> None:
        data = SidecarData(target=10, saved=10, available=15, clean=True)
        write_sidecar(video, "en", data)
        loaded = read_sidecar(video, "en")
        assert loaded is not None
        dt = datetime.fromisoformat(loaded.completed_at)
        assert (datetime.now(timezone.utc) - dt).total_seconds() < 5

    def test_read_missing_returns_none(self, video: Path) -> None:
        assert read_sidecar(video, "en") is None

    def test_read_corrupt_json_returns_none(self, video: Path) -> None:
        sidecar_path(video, "en").write_text("not valid json {{{")
        assert read_sidecar(video, "en") is None

    def test_read_missing_fields_returns_none(self, video: Path) -> None:
        sidecar_path(video, "en").write_text(json.dumps({"target": 10}))
        assert read_sidecar(video, "en") is None


class TestDeleteSidecar:
    def test_delete_existing(self, video: Path) -> None:
        data = SidecarData(target=10, saved=10, available=10, clean=True)
        write_sidecar(video, "en", data)
        assert sidecar_path(video, "en").exists()
        delete_sidecar(video, "en")
        assert not sidecar_path(video, "en").exists()

    def test_delete_nonexistent_no_error(self, video: Path) -> None:
        delete_sidecar(video, "en")  # should not raise


class TestIsTopnDone:
    """Full truth table for the skip-check helper."""

    def _write(self, video: Path, lang: str, **overrides) -> None:
        defaults = dict(
            target=10, saved=10, available=15, clean=True,
            completed_at=datetime.now(timezone.utc).isoformat(),
            search_ok=True, schema_version=2,
        )
        defaults.update(overrides)
        p = sidecar_path(video, lang)
        p.write_text(json.dumps(defaults))

    def test_missing_sidecar_not_done(self, video: Path, cfg: Config) -> None:
        assert is_topn_done(video, "en", cfg) is False

    def test_corrupt_sidecar_not_done(self, video: Path, cfg: Config) -> None:
        sidecar_path(video, "en").write_text("garbage")
        assert is_topn_done(video, "en", cfg) is False

    def test_clean_complete_is_done(self, video: Path, cfg: Config) -> None:
        self._write(video, "en", target=10, saved=10, available=15, clean=True)
        assert is_topn_done(video, "en", cfg) is True

    def test_clean_niche_is_done(self, video: Path, cfg: Config) -> None:
        """available < target, but saved == available and clean — done."""
        self._write(video, "en", target=10, saved=3, available=3, clean=True)
        assert is_topn_done(video, "en", cfg) is True

    def test_available_zero_saved_zero_clean_search_ok_is_done(
        self, video: Path, cfg: Config
    ) -> None:
        """v2 sidecar: genuinely no Turkish subs exist — is_topn_done returns True."""
        self._write(
            video, "en",
            target=10, saved=0, available=0, clean=True, search_ok=True,
        )
        assert is_topn_done(video, "en", cfg) is True

    def test_v1_legacy_sidecar_not_done(self, video: Path, cfg: Config) -> None:
        """Legacy v1 sidecar (no schema_version, no search_ok) is never done."""
        raw = dict(
            target=10, saved=0, available=0, clean=True,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        sidecar_path(video, "en").write_text(json.dumps(raw))
        assert is_topn_done(video, "en", cfg) is False

    def test_v2_search_ok_false_not_done(self, video: Path, cfg: Config) -> None:
        """Rate-limited sidecar is retried next scan."""
        self._write(
            video, "en",
            target=10, saved=0, available=0, clean=False, search_ok=False,
        )
        assert is_topn_done(video, "en", cfg) is False

    def test_v2_schema_version_1_explicitly_not_done(
        self, video: Path, cfg: Config
    ) -> None:
        """Explicit schema_version=1 (written by old code) is rejected."""
        raw = dict(
            target=10, saved=10, available=15, clean=True,
            completed_at=datetime.now(timezone.utc).isoformat(),
            schema_version=1, search_ok=True,
        )
        sidecar_path(video, "en").write_text(json.dumps(raw))
        assert is_topn_done(video, "en", cfg) is False

    def test_clean_false_not_done(self, video: Path, cfg: Config) -> None:
        """Run had failures — needs retry."""
        self._write(video, "en", target=10, saved=3, available=15, clean=False)
        assert is_topn_done(video, "en", cfg) is False

    def test_target_upgraded_not_done(self, video: Path, cfg: Config) -> None:
        """User raised top_n since last run."""
        self._write(video, "en", target=5, saved=5, available=15, clean=True)
        assert is_topn_done(video, "en", cfg) is False

    def test_partial_with_candidates_remaining_not_done(self, video: Path, cfg: Config) -> None:
        """saved < min(target, available) — partial failure."""
        self._write(video, "en", target=10, saved=3, available=15, clean=True)
        assert is_topn_done(video, "en", cfg) is False

    def test_stale_sidecar_not_done(self, video: Path, cfg: Config) -> None:
        """Sidecar older than topn_recheck_days."""
        old = datetime.now(timezone.utc) - timedelta(days=31)
        self._write(video, "en", target=10, saved=10, available=15, clean=True,
                    completed_at=old.isoformat())
        assert is_topn_done(video, "en", cfg) is False

    def test_fresh_sidecar_within_recheck_window(self, video: Path, cfg: Config) -> None:
        recent = datetime.now(timezone.utc) - timedelta(days=15)
        self._write(video, "en", target=10, saved=10, available=15, clean=True,
                    completed_at=recent.isoformat())
        assert is_topn_done(video, "en", cfg) is True

    def test_sidecar_disabled_always_not_done(self, video: Path, cfg: Config) -> None:
        cfg.topn_sidecar_enabled = False
        self._write(video, "en", target=10, saved=10, available=15, clean=True)
        assert is_topn_done(video, "en", cfg) is False


class TestSchemaV2:
    def test_write_defaults_include_schema_version_and_search_ok(
        self, video: Path
    ) -> None:
        data = SidecarData(target=10, saved=5, available=8, clean=True)
        write_sidecar(video, "en", data)
        raw = json.loads(sidecar_path(video, "en").read_text())
        assert raw["schema_version"] == 2
        assert raw["search_ok"] is True

    def test_write_preserves_explicit_search_ok_false(self, video: Path) -> None:
        data = SidecarData(
            target=10, saved=0, available=0, clean=False, search_ok=False,
        )
        write_sidecar(video, "en", data)
        raw = json.loads(sidecar_path(video, "en").read_text())
        assert raw["search_ok"] is False
        assert raw["schema_version"] == 2

    def test_roundtrip_search_ok(self, video: Path) -> None:
        data = SidecarData(
            target=10, saved=0, available=0, clean=False, search_ok=False,
        )
        write_sidecar(video, "en", data)
        loaded = read_sidecar(video, "en")
        assert loaded is not None
        assert loaded.search_ok is False
        assert loaded.schema_version == 2


class TestLegacyV1Read:
    def test_v1_sidecar_reads_with_defaults(self, video: Path) -> None:
        """Legacy file without schema_version / search_ok loads with defaults."""
        raw = {
            "target": 10,
            "saved": 0,
            "available": 0,
            "clean": True,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        sidecar_path(video, "en").write_text(json.dumps(raw))
        loaded = read_sidecar(video, "en")
        assert loaded is not None
        # Missing schema_version defaults to 1
        assert loaded.schema_version == 1
        # Missing search_ok defaults to False (legacy wrote clean=True on any result)
        assert loaded.search_ok is False

    def test_v2_sidecar_reads_cleanly(self, video: Path) -> None:
        data = SidecarData(
            target=10, saved=10, available=15, clean=True, search_ok=True,
        )
        write_sidecar(video, "en", data)
        loaded = read_sidecar(video, "en")
        assert loaded is not None
        assert loaded.schema_version == 2
        assert loaded.search_ok is True

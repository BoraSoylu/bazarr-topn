"""Tests for CLI commands."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bazarr_topn.cli import main


class TestCLI:
    def test_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "bazarr-topn" in result.output
        assert "scan" in result.output
        assert "watch" in result.output

    def test_scan_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["scan", "--help"])
        assert result.exit_code == 0
        assert "--all" in result.output

    def test_watch_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["watch", "--help"])
        assert result.exit_code == 0

    def test_scan_no_args(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["scan"])
        assert result.exit_code != 0
        assert "provide paths or use --all" in result.output

    def test_scan_with_config(self, sample_config_yaml: Path, tmp_path: Path) -> None:
        # Create a directory with no video files
        scan_dir = tmp_path / "empty_media"
        scan_dir.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", str(sample_config_yaml), "scan", str(scan_dir)]
        )
        assert result.exit_code == 0
        assert "0/0 videos processed" in result.output

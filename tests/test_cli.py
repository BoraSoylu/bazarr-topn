"""Tests for CLI commands."""

from __future__ import annotations

import logging
from pathlib import Path

from click.testing import CliRunner

from bazarr_topn.cli import main, _QuietConsoleFilter


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


class TestQuietConsoleFilter:
    def setup_method(self) -> None:
        self.filt = _QuietConsoleFilter()

    def _record(self, name: str, level: int) -> logging.LogRecord:
        return logging.LogRecord(
            name=name, level=level, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )

    def test_passes_bazarr_topn_info(self) -> None:
        assert self.filt.filter(self._record("bazarr_topn.scanner", logging.INFO))

    def test_passes_bazarr_topn_debug(self) -> None:
        assert self.filt.filter(self._record("bazarr_topn.sync", logging.DEBUG))

    def test_blocks_subliminal_info(self) -> None:
        assert not self.filt.filter(self._record("subliminal.core", logging.INFO))

    def test_blocks_subliminal_debug(self) -> None:
        assert not self.filt.filter(self._record("subliminal.providers", logging.DEBUG))

    def test_passes_subliminal_warning(self) -> None:
        assert self.filt.filter(self._record("subliminal.core", logging.WARNING))

    def test_blocks_torch_info(self) -> None:
        assert not self.filt.filter(self._record("torch.jit", logging.INFO))

    def test_blocks_srt_info(self) -> None:
        assert not self.filt.filter(self._record("srt", logging.INFO))

    def test_blocks_ffsubsync_info(self) -> None:
        assert not self.filt.filter(self._record("ffsubsync.speech_transformers", logging.INFO))

    def test_passes_ffsubsync_warning(self) -> None:
        assert self.filt.filter(self._record("ffsubsync", logging.WARNING))

"""CLI entry point — click-based with scan and watch commands."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from bazarr_topn import __version__
from bazarr_topn.config import Config


class _QuietConsoleFilter(logging.Filter):
    """Block noisy third-party loggers from the console unless WARNING+."""

    NOISY = frozenset({
        "subliminal", "stevedore", "rebulk", "enzyme", "guessit",
        "dogpile", "urllib3", "requests", "srt", "ffsubsync", "torch",
        "chardet", "babelfish", "knowit", "pymediainfo",
    })

    def filter(self, record: logging.LogRecord) -> bool:
        top = record.name.split(".")[0]
        if top in self.NOISY:
            return record.levelno >= logging.WARNING
        return True


def setup_logging(level: str, log_file: str | None = None) -> None:
    """Configure dual logging: clean console + optional full-debug file log."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console handler: clean output at user's chosen level
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter("%(message)s"))
    console.addFilter(_QuietConsoleFilter())
    root.addHandler(console)

    # File handler: full DEBUG output including all third-party internals
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(fh)


def load_config(config_path: str | None) -> Config:
    if config_path:
        return Config.from_file(config_path)

    # Search default locations
    candidates = [
        Path("config.yaml"),
        Path("config.yml"),
        Path.home() / ".config" / "bazarr-topn" / "config.yaml",
        Path("/etc/bazarr-topn/config.yaml"),
    ]
    for p in candidates:
        if p.exists():
            click.echo(f"Using config: {p}")
            return Config.from_file(p)

    click.echo("No config file found, using defaults.", err=True)
    return Config()


@click.group()
@click.version_option(version=__version__)
@click.option(
    "-c", "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to config.yaml",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default=None,
    help="Override log level from config",
)
@click.pass_context
def main(ctx: click.Context, config_path: str | None, log_level: str | None) -> None:
    """bazarr-topn — Download the top N subtitles for every video in your library."""
    ctx.ensure_object(dict)
    config = load_config(config_path)
    if log_level:
        config.log_level = log_level.upper()
    setup_logging(config.log_level, config.log_file)
    ctx.obj["config"] = config


@main.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
@click.option("--all", "scan_all", is_flag=True, help="Full library rescan using Bazarr inventory")
@click.pass_context
def scan(ctx: click.Context, paths: tuple[str, ...], scan_all: bool) -> None:
    """Scan files or directories for subtitles.

    \b
    Examples:
      bazarr-topn scan /media/movies/Inception
      bazarr-topn scan /media/movies /media/tv
      bazarr-topn scan --all
    """
    from bazarr_topn.bazarr_client import BazarrClient
    from bazarr_topn.scanner import scan as do_scan

    config: Config = ctx.obj["config"]

    if scan_all:
        client = BazarrClient(config.bazarr)
        click.echo("Fetching inventory from Bazarr...")
        items = client.get_movies() + client.get_all_episodes()
        scan_paths = [config.map_path(item.path) for item in items if item.monitored]
        click.echo(f"Found {len(scan_paths)} monitored items in Bazarr")
    elif paths:
        scan_paths = list(paths)
    else:
        click.echo("Error: provide paths or use --all", err=True)
        sys.exit(1)

    result = do_scan(scan_paths, config)
    click.echo(
        f"\nDone: {result['videos_processed']}/{result['videos_found']} videos processed, "
        f"{result['subtitles_downloaded']} subtitles downloaded"
    )
    if result["videos_skipped"]:
        click.echo(f"  ({result['videos_skipped']} videos skipped — download limit reached)")


@main.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
@click.pass_context
def watch(ctx: click.Context, paths: tuple[str, ...]) -> None:
    """Watch directories for new video files and auto-process them.

    \b
    Examples:
      bazarr-topn watch /media/movies /media/tv
      bazarr-topn watch  # uses watch_paths from config
    """
    from bazarr_topn.watcher import watch as do_watch

    config: Config = ctx.obj["config"]

    if paths:
        config.watch_paths = list(paths)

    if not config.watch_paths:
        click.echo("Error: no watch paths provided (pass as args or set in config)", err=True)
        sys.exit(1)

    do_watch(config)

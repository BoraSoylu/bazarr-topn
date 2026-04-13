"""Filesystem watcher using watchdog — monitors directories for new video files."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from bazarr_topn.config import Config
from bazarr_topn.scanner import VIDEO_EXTENSIONS, process_video
from bazarr_topn.subtitle_finder import configure_cache, create_pool

logger = logging.getLogger(__name__)


class VideoHandler(FileSystemEventHandler):
    """Handles new video file events with a cooldown to avoid processing incomplete files."""

    def __init__(self, config: Config, pool) -> None:
        self.config = config
        self.pool = pool
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _is_video(self, path: str) -> bool:
        return Path(path).suffix.lower() in VIDEO_EXTENSIONS

    def _schedule(self, path: str) -> None:
        with self._lock:
            self._pending[path] = time.time() + self.config.watch_cooldown
        self._ensure_timer()

    def _ensure_timer(self) -> None:
        if self._timer is None or not self._timer.is_alive():
            self._timer = threading.Timer(self.config.watch_cooldown + 1, self._process_pending)
            self._timer.daemon = True
            self._timer.start()

    def _process_pending(self) -> None:
        now = time.time()
        ready: list[str] = []
        with self._lock:
            for path, deadline in list(self._pending.items()):
                if now >= deadline:
                    ready.append(path)
                    del self._pending[path]
            has_more = len(self._pending) > 0

        for path in ready:
            video_path = Path(path)
            if video_path.exists():
                logger.info("Watch: processing %s", video_path.name)
                try:
                    process_video(video_path, self.config, self.pool)
                except Exception:
                    logger.exception("Watch: failed to process %s", video_path.name)

        if has_more:
            self._ensure_timer()

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory and self._is_video(event.src_path):
            logger.info("Watch: new file detected: %s", event.src_path)
            self._schedule(event.src_path)

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory and self._is_video(event.dest_path):
            logger.info("Watch: file moved in: %s", event.dest_path)
            self._schedule(event.dest_path)


def watch(config: Config) -> None:
    """Start watching configured paths for new video files. Blocks until interrupted."""
    configure_cache()

    paths = config.watch_paths
    if not paths:
        logger.error("No watch_paths configured")
        return

    # Single ProviderPool for the entire watch session — one login, reused
    # across every video event. Use as a context manager to match scanner.py;
    # subliminal's ProviderPool auto-initializes on __enter__ and terminates
    # on __exit__. Calling .initialize() directly raises AttributeError on
    # current subliminal versions.
    with create_pool(config) as pool:
        handler = VideoHandler(config, pool)
        observer = Observer()

        for watch_path in paths:
            p = Path(watch_path)
            if not p.is_dir():
                logger.warning("Watch path does not exist or is not a directory: %s", watch_path)
                continue
            observer.schedule(handler, str(p), recursive=True)
            logger.info("Watching: %s", watch_path)

        observer.start()
        logger.info("Watch mode started. Press Ctrl+C to stop.")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Stopping watch mode...")
            observer.stop()

        observer.join()

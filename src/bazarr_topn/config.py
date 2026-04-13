"""YAML configuration with ${ENV_VAR} expansion."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env(value: str) -> str:
    """Replace ${VAR} placeholders with environment variable values."""

    def _replace(match: re.Match) -> str:
        var = match.group(1)
        result = os.environ.get(var)
        if result is None:
            raise ValueError(f"Environment variable ${{{var}}} is not set")
        return result

    return ENV_PATTERN.sub(_replace, value)


def _expand_recursive(obj: Any) -> Any:
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _expand_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_recursive(item) for item in obj]
    return obj


@dataclass
class ProviderConfig:
    name: str
    username: str | None = None
    password: str | None = None
    max_result_pages: int = 3

    def to_subliminal_config(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        if self.username:
            cfg["username"] = self.username
        if self.password:
            cfg["password"] = self.password
        if self.max_result_pages > 0:
            cfg["max_result_pages"] = self.max_result_pages
        return cfg


@dataclass
class FfsubsyncConfig:
    enabled: bool = False
    gss: bool = True
    vad: str = "silero"
    max_offset_seconds: int = 600
    no_fix_framerate: bool = False
    reference_stream: str | None = None  # e.g. "a:0", "s:1"
    extra_args: list[str] = field(default_factory=list)


@dataclass
class BazarrConfig:
    url: str = "http://localhost:6767"
    api_key: str = ""


@dataclass
class Config:
    bazarr: BazarrConfig = field(default_factory=BazarrConfig)
    languages: list[str] = field(default_factory=lambda: ["en"])
    top_n: int = 10
    min_score: int = 30
    max_downloads_per_cycle: int = 0
    naming_pattern: str = "{video_stem}.{lang}.topn-{rank}.srt"
    download_delay: float = 1.5
    providers: list[ProviderConfig] = field(default_factory=list)
    ffsubsync: FfsubsyncConfig = field(default_factory=FfsubsyncConfig)
    watch_paths: list[str] = field(default_factory=list)
    watch_cooldown: int = 30
    path_mappings: list[dict[str, str]] = field(default_factory=list)
    log_level: str = "INFO"
    log_file: str | None = None

    @classmethod
    def from_file(cls, path: str | Path) -> Config:
        path = Path(path)
        with open(path) as f:
            raw = yaml.safe_load(f)
        if raw is None:
            raw = {}
        data = _expand_recursive(raw)
        return cls._from_dict(data)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        data = _expand_recursive(raw)
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> Config:
        bazarr_raw = data.get("bazarr", {})
        bazarr = BazarrConfig(
            url=bazarr_raw.get("url", "http://localhost:6767"),
            api_key=bazarr_raw.get("api_key", ""),
        )

        providers = []
        for p in data.get("providers", []):
            providers.append(
                ProviderConfig(
                    name=p["name"],
                    username=p.get("username"),
                    password=p.get("password"),
                    max_result_pages=p.get("max_result_pages", 3),
                )
            )

        ffs_raw = data.get("ffsubsync", {})
        ffsubsync = FfsubsyncConfig(
            enabled=ffs_raw.get("enabled", False),
            gss=ffs_raw.get("gss", True),
            vad=ffs_raw.get("vad", "silero"),
            max_offset_seconds=ffs_raw.get("max_offset_seconds", 600),
            no_fix_framerate=ffs_raw.get("no_fix_framerate", False),
            reference_stream=ffs_raw.get("reference_stream"),
            extra_args=ffs_raw.get("extra_args", []),
        )

        return cls(
            bazarr=bazarr,
            languages=data.get("languages", ["en"]),
            top_n=data.get("top_n", 10),
            min_score=data.get("min_score", 30),
            max_downloads_per_cycle=data.get("max_downloads_per_cycle", 0),
            naming_pattern=data.get("naming_pattern", "{video_stem}.{lang}.topn-{rank}.srt"),
            download_delay=data.get("download_delay", 1.5),
            providers=providers,
            ffsubsync=ffsubsync,
            path_mappings=data.get("path_mappings", []),
            watch_paths=data.get("watch_paths", []),
            watch_cooldown=data.get("watch_cooldown", 30),
            log_level=data.get("log_level", "INFO"),
            log_file=data.get("log_file"),
        )

    def map_path(self, path: str) -> str:
        """Apply path_mappings to translate container paths to host paths."""
        for m in self.path_mappings:
            container = m.get("container", "")
            host = m.get("host", "")
            if container and path.startswith(container):
                return host + path[len(container):]
        return path

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self.providers]

    @property
    def provider_configs(self) -> dict[str, dict[str, str]]:
        return {p.name: p.to_subliminal_config() for p in self.providers if p.to_subliminal_config()}

# bazarr-topn

**Download the top N subtitles for every video in your library — ranked, synced, and ready to switch.**

Bazarr gives you one subtitle per video. That subtitle might have bad timing, missing lines, or just a translation style you don't like. **bazarr-topn** downloads additional alternatives so you can pick the one that actually works.

With the default `top_n=10`, you get **11 subtitle options** per video per language: 1 from Bazarr + 10 from bazarr-topn. Each one is automatically time-synced with [ffsubsync](https://github.com/smacke/ffsubsync).

## How it works

1. **Inventory** — Reads your media library from Bazarr's API (read-only) or scans paths directly
2. **Discovery** — Uses [subliminal](https://github.com/Diaoul/subliminal) to search OpenSubtitles, Addic7ed, Podnapisi, and other providers
3. **Scoring** — Ranks candidates by hash match, release group, resolution, and other metadata
4. **Download** — Saves the top N as `Movie.en.topn-2.srt` through `Movie.en.topn-11.srt` (rank 1 = Bazarr's subtitle)
5. **Sync** — Runs ffsubsync against the video to fix timing on each downloaded subtitle

## Installation

```bash
git clone https://github.com/BoraSoylu/bazarr-topn
cd bazarr-topn
pip install -e ".[all]"
```

## Quick start

```bash
# 1. Copy and edit the config
cp config.example.yaml config.yaml
# Edit config.yaml with your Bazarr URL, API key, and provider credentials

# 2. Scan specific files or directories
bazarr-topn scan /media/movies/Inception\ \(2010\)/
bazarr-topn scan /media/movies /media/tv

# 3. Full library rescan using Bazarr inventory
bazarr-topn scan --all

# 4. Watch mode — auto-process new files as they land
bazarr-topn watch /media/movies /media/tv
```

## Run modes

| Mode | Command | Use case |
|------|---------|----------|
| **One-shot** | `bazarr-topn scan <path>...` | Target specific files or directories |
| **Full scan** | `bazarr-topn scan --all` | Rescan entire library via Bazarr inventory |
| **Watch** | `bazarr-topn watch <path>...` | Daemon that auto-processes new files |

### As a systemd service

```ini
# /etc/systemd/system/bazarr-topn.service
[Unit]
Description=bazarr-topn subtitle watcher
After=network.target docker.service

[Service]
Type=simple
User=your-media-user
ExecStart=/usr/local/bin/bazarr-topn -c /etc/bazarr-topn/config.yaml watch
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### As a cron job

```cron
# Full library rescan every Sunday at 3 AM
0 3 * * 0 /usr/local/bin/bazarr-topn -c /etc/bazarr-topn/config.yaml scan --all
```

## Configuration

All settings live in `config.yaml`. Environment variables are supported with `${VAR_NAME}` syntax.

See [`config.example.yaml`](config.example.yaml) for the full annotated config.

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `top_n` | 10 | Number of additional subtitles to download per video per language |
| `min_score` | 30 | Minimum subliminal score (0–100) to accept a subtitle |
| `max_downloads_per_cycle` | 0 | Download cap per run (0 = unlimited, for VIP accounts) |
| `ffsubsync.enabled` | true | Auto-sync subtitle timing against the video |
| `languages` | `[en]` | Target languages (ISO 639-1 codes) |

## File naming

Downloaded subtitles follow a configurable pattern (default: `{video_stem}.{lang}.topn-{rank}.srt`):

```
Movie (2024).mkv              # Your video
Movie (2024).en.srt           # Rank 1: Bazarr's subtitle (untouched)
Movie (2024).en.topn-2.srt    # Rank 2: Best from bazarr-topn
Movie (2024).en.topn-3.srt    # Rank 3: Second best
...
Movie (2024).en.topn-11.srt   # Rank 11: Tenth best
```

Jellyfin, Plex, and Emby all detect these as selectable subtitle tracks.

## Requirements

- Python 3.10+
- Bazarr instance with API access (for `--all` mode)
- Subtitle provider account (OpenSubtitles VIP recommended for no rate limits)
- ffsubsync (optional but recommended — `pip install bazarr-topn[sync]`)

## Development

```bash
git clone https://github.com/BoraSoylu/bazarr-topn
cd bazarr-topn
pip install -e ".[all]"
pytest
```

## License

MIT

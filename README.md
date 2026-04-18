# bazarr-topn

**Download the top N subtitles for every video in your library — ranked, synced, and ready to switch.**

Bazarr gives you one subtitle per video. That subtitle might have bad timing, missing lines, or just a translation style you don't like. **bazarr-topn** downloads additional alternatives so you can pick the one that actually works.

With the default `top_n=10`, you get **11 subtitle options** per video per language: 1 from Bazarr + 10 from bazarr-topn. The "shotgun" approach: cast a wide net, and a well-timed candidate almost always lands naturally — no resyncing required.

## How it works

1. **Inventory** — Reads your media library from Bazarr's API (read-only) or scans paths directly
2. **Discovery** — Uses [subliminal](https://github.com/Diaoul/subliminal) to search OpenSubtitles, Addic7ed, Podnapisi, and other providers
3. **Scoring** — Ranks candidates by hash match, release group, resolution, and other metadata
4. **Download** — Saves the top N as `Movie.en.topn-2.srt` through `Movie.en.topn-11.srt` (rank 1 = Bazarr's subtitle). If some of the top candidates are broken or empty, it iterates deeper (up to `max_candidates_tried`, default 50) to fill the quota.
5. **Track** — Writes a `.topn.json` sidecar per video+language recording `saved`, `available`, `clean`, and `search_ok`. Re-runs skip videos already done; rate-limited or partial runs are retried next scan.
6. **(Optional) Sync** — If `ffsubsync.enabled: true`, runs ffsubsync to adjust timing. **Off by default** — see the config for why.

## Installation

```bash
git clone https://github.com/BoraSoylu/bazarr-topn
cd bazarr-topn

# Full install with best-quality sync (silero VAD + torch, ~2GB):
pip install -e ".[all]"

# Lighter install if you don't want torch:
pip install -e ".[sync]"
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
| **Force** | `bazarr-topn scan --all --force` | Re-download even for videos already marked done |
| **Rescan stale** | `bazarr-topn scan --all --rescan-stale` | Reprocess sidecars older than `topn_recheck_days` |
| **Watch** | `bazarr-topn watch <path>...` | Daemon that auto-processes new files |

Sidecar tracking makes `scan --all` cheap to re-run on a cron — it processes only the backlog (missing, stale, or previously rate-limited) and skips everything already complete.

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
# Twice-daily full library rescan (what I run on my own server)
17 2,14 * * * /usr/local/bin/bazarr-topn -c /etc/bazarr-topn/config.yaml scan --all
```

Sidecars keep this cheap: each run only works on the backlog (missing sidecars, stale sidecars past `topn_recheck_days`, or sidecars flagged `search_ok=false` from a previous rate-limited run).

## Configuration

All settings live in `config.yaml`. Environment variables are supported with `${VAR_NAME}` syntax.

See [`config.example.yaml`](config.example.yaml) for the full annotated config.

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `top_n` | 10 | Number of additional subtitles to download per video per language |
| `min_score` | 30 | Minimum subliminal score (0–100) to accept a subtitle |
| `max_downloads_per_cycle` | 0 | Download cap per run (0 = unlimited, for VIP accounts) |
| `max_candidates_tried` | 50 | Deep-iteration cap when top candidates are broken/empty |
| `languages` | `[en]` | Target languages (ISO 639-1 codes) |
| `search_delay` | 3.0 | Seconds between provider searches (rate-limit floor) |
| `download_delay` | 5.0 | Seconds between subtitle downloads |
| `rate_limit_retries` | 2 | Retries on provider errors during search/download (e.g. OpenSubtitles 429) |
| `rate_limit_initial_backoff` | 60.0 | First-retry backoff seconds (doubles each attempt) |
| `topn_recheck_days` | 3 | Stale-sidecar age before a fresh rescan |
| `topn_sidecar_enabled` | true | Write/read `.topn.json` sidecars for skip logic |
| `ffsubsync.enabled` | **false** | Off by default — in my experience it often makes timing worse, not better. See `config.example.yaml` for the full rationale. |
| `ffsubsync.gss` | true | Golden-section search for optimal framerate ratio (slower, more accurate) |
| `ffsubsync.vad` | `silero` | Voice activity detection — `silero` (best, needs torch) or `webrtc` (lighter) |
| `ffsubsync.max_offset_seconds` | 600 | Max subtitle shift in seconds (ffsubsync default is only 60) |

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
- Subtitle provider account (OpenSubtitles VIP recommended to keep rate-limit stalls rare)
- ffsubsync (optional, off by default — `pip install bazarr-topn[sync]` if you want to try enabling it)

## Development

```bash
git clone https://github.com/BoraSoylu/bazarr-topn
cd bazarr-topn
pip install -e ".[all]"
pytest
```

## License

MIT

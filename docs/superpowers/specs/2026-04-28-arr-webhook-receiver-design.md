# Design: Sonarr/Radarr webhook receiver (`serve` subcommand)

**Status:** proposed · **Date:** 2026-04-28

## Problem

The `bazarr-topn watch` subcommand uses inotify (via `watchdog`) to detect new
videos. On fuse.mergerfs filesystems — common in homelab setups — `IN_CREATE`
events for new video files in newly-created subdirectories are not reliably
delivered. In the deployment that motivated this work the watcher logged 158
`IN_CREATE` events over multiple weeks and **zero** of them were for `.mkv` /
`.mp4` files; new movies were only picked up by the twice-daily `--all` cron.

Replacing inotify with a Sonarr/Radarr webhook eliminates the dependency on the
filesystem layer entirely: the *arr instances already know the authoritative
moment a new file lands.

## Goals

- New `bazarr-topn serve` subcommand that runs an HTTP listener for
  Sonarr and Radarr "Connection" webhooks.
- Process `Download`, `Upgrade`, and `Test` events from both Sonarr and Radarr.
- On `Upgrade`, also delete sidecars keyed to the old (replaced) video stem so
  orphans do not accumulate.
- Single in-process worker drains a queue of pending scans serially, sharing a
  lockfile with the existing cron `--all` so the two never overlap.
- Replace `bazarr-topn.service` `ExecStart` from `watch` to `serve` in the
  installer; `watch` remains in the codebase for users on filesystems where
  inotify works.

## Non-goals

- `Rename` events (rare; deferred).
- HMAC payload signing (out of scope; *arr does not natively sign payloads;
  shared-secret header is sufficient for the localhost / private-network
  threat model).
- Generic webhook framework or shared library with other tools
  (subtitle-translator). bazarr-topn stays self-contained.
- Persistent queue / restart-survivability. The cron `--all` at the configured
  schedule is the safety net for events lost during downtime.
- Cleanup of historical orphaned sidecars from past upgrades (a separate
  `cleanup-orphans` subcommand could be added later if real-world demand
  appears; not part of this work).

## Architecture

### New module: `src/bazarr_topn/webhook.py`

Owns the FastAPI app, payload models, queue, and worker thread. Public entry
point `serve(config: Config) -> None` mirrors `watcher.watch(config)`.

Components:

1. **Pydantic payload models** — separate models for Sonarr and Radarr,
   covering only the fields we read (`eventType`, file path(s), and the upgrade
   `deletedFiles`). Unknown fields ignored. Examples of fields used:
   - Sonarr `Download`: `eventType`, `series.path`, `episodeFile.relativePath`
     (joined to `series.path`).
   - Sonarr `Upgrade`: same as Download, plus `deletedFiles[].path`.
   - Radarr `Download`: `eventType`, `movie.folderPath`,
     `movieFile.relativePath`.
   - Radarr `Upgrade`: same as Download, plus `deletedFiles[].path`.
   - `Test`: schema differs trivially; handler returns 200 without queuing
     work.
2. **Auth dependency** — FastAPI dependency that compares
   `X-Webhook-Token` against `Config.webhook.token`. Missing or mismatched
   token → 401. Constant-time comparison (`hmac.compare_digest`).
3. **Routes** — `POST /sonarr` and `POST /radarr`. Each parses the
   provider-specific payload, applies `config.map_path()` to every path, and
   enqueues work. `GET /healthz` returns 200 unauthenticated for liveness.
4. **Queue + worker** — `queue.Queue` of `WebhookJob` records (one job per
   `Download`/`Upgrade` event, possibly carrying multiple `new_files` and
   `deleted_files`). A single daemon thread drains the queue. The worker holds
   the lockfile (see below) for the duration of each job.
5. **Lockfile coordination** — `fcntl.flock` on
   `/var/lock/bazarr-topn-scan.lock` (path configurable, default in `Config`).
   Held while the worker processes a job. The cron wrapper script also takes
   this lock with `flock -n` (a separate change to the installer) so cron
   `--all` and webhook-driven scans never run concurrently. If `--all` is
   currently running, queued jobs simply block until it finishes — fine,
   nothing is lost.

### CLI: new `serve` subcommand

`cli.py` gains `@cli.command()` `serve` that calls `webhook.serve(config)`.
Options:

- `--host` (default from config, fallback `127.0.0.1`)
- `--port` (default from config, fallback `9595`)

Mirrors the structure of the existing `watch` subcommand.

### Config additions: `WebhookConfig`

New dataclass alongside `BazarrConfig`:

```yaml
webhook:
  host: 127.0.0.1
  port: 9595
  token: ${BAZARR_TOPN_WEBHOOK_TOKEN}    # required; env-expansion already supported
  lockfile: /var/lock/bazarr-topn-scan.lock
```

Reuses the existing `path_mappings` config — **no new path config**. Example:

```yaml
path_mappings:
  - container: /media
    host: /mnt/media
```

Loaded by `Config._from_dict` with the same env-expansion the existing config
fields already get.

### Reuse of existing primitives

- `Config.path_mappings` + `Config.map_path(path)` — already exists at
  `src/bazarr_topn/config.py:173`. The receiver passes every file path through
  `map_path` before doing anything else. No new path-mapping code.
- `scanner.process_video(video_path, config, pool)` — per-file entry point
  the worker calls. Already returns count or `-1` for skipped.
- `subtitle_finder.create_pool(config)` — context manager the server holds
  open for its lifetime. One login, reused across all events. Pattern lifted
  from `watcher.watch`.

### Upgrade-event sidecar cleanup

When an `Upgrade` event arrives with `deletedFiles`, before queuing the new
file's scan job, the worker:

1. For each deleted file path: derive the stem (filename minus extension).
2. Delete sibling files matching `<stem>.tr.topn-*.srt` and
   `<stem>.tr.topn.json` (and the same for any other configured language).
3. Log each deletion. Failures (e.g. file already gone) are swallowed with a
   debug log — best-effort, not transactional.

Cleanup runs in the same queued job, before `process_video` for the new file,
so the sequence is: delete-old → process-new.

## Data flow

```
Sonarr/Radarr  ──HTTP POST──▶  FastAPI route  ──▶  enqueue WebhookJob
                                  │
                                  ▼
                              200 OK (fast)

(separate thread)
worker loop ──▶ flock(scan.lock) ──▶ for each job:
                                        - cleanup orphan sidecars (Upgrade)
                                        - process_video(new_file, config, pool)
```

## Error handling

- Auth failure → 401 immediately, no logging at INFO level (avoid log
  spam from misconfigured *arr).
- Payload parse failure (malformed JSON, schema mismatch) → 422 with the
  validation error.
- Path remap produces a path that doesn't exist on disk → log a warning,
  skip that file, return 200 (the *arr-side webhook will get a success and
  not retry; the cron `--all` will catch it eventually).
- `process_video` raises → logged with `exception` level, worker continues
  with next job. (Mirrors `VideoHandler._process_pending` behavior.)
- Worker thread crash → process exit. systemd will restart. Acceptable;
  cron is the safety net.

## Systemd / installer changes

Installer script (or whatever populates `/etc/systemd/system/bazarr-topn.service`)
flips:

```
ExecStart=…/bazarr-topn -c …/config.yaml watch
```

to:

```
ExecStart=…/bazarr-topn -c …/config.yaml serve
```

Cron wrapper for `--all` is updated to acquire the same lockfile with
`flock -n /var/lock/bazarr-topn-scan.lock` so it cannot run concurrently with
a webhook-driven scan.

No port is opened to the network — the listener binds `127.0.0.1` by default.
Operators wanting *arr-on-different-host can override via config.

## Testing

Existing test suite uses `pytest`, `responses` (outbound HTTP mocking), and has
direct precedent in `tests/test_watcher.py` for testing event-handler-style
components. New tests in `tests/test_webhook.py`:

**Unit-level (test the parsing & remap layer in isolation):**
- Parse Sonarr `Download` payload → correct video path extracted.
- Parse Sonarr `Upgrade` payload → correct new path AND deleted paths.
- Parse Radarr `Download` payload → correct video path.
- Parse Radarr `Upgrade` payload → correct new path AND deleted paths.
- Container path → host path remap applied via `Config.map_path` (no new
  remap code, but verify wiring).
- Auth: missing token → 401. Wrong token → 401. Correct token → 200.
- `Test` event → 200 OK, queue empty.
- Malformed payload → 422.

**Integration-level (FastAPI `TestClient`, in-process):**
- POST a Sonarr `Download` payload with valid auth → 200, worker processes
  one job calling `process_video` once with the remapped path. `process_video`
  is mocked at the module boundary.
- POST a Sonarr `Upgrade` with `deletedFiles` → orphan-sidecar deletion is
  invoked for each deleted stem, then `process_video` runs once. Use a tmpdir
  with fixture sidecar files; assert the right files are removed and others
  are untouched.
- POST 24 events back-to-back (season-pack burst) → all return 200 quickly,
  worker drains all 24, mocked `process_video` invoked 24 times in order.
- Lockfile contention: pre-acquire the lockfile in the test; POST an event;
  assert the worker blocks; release; assert the worker proceeds. (Optional;
  this is OS-level behavior of `flock` and may be skipped if it adds
  flakiness.)

`process_video` is mocked in webhook tests — it has its own
`tests/test_scanner.py` coverage. The webhook layer's contract is
"parse → remap → enqueue → cleanup → call process_video"; tests verify each
step at that boundary.

## Documentation

A new "Webhook receiver" section in `README.md` covering:

1. What it does (one paragraph).
2. Sonarr setup: Settings → Connect → + → Webhook → URL
   `http://localhost:9595/sonarr`, method `POST`, header
   `X-Webhook-Token: <secret>`, triggered on `On Import`, `On Upgrade`. Test
   button validates connectivity.
3. Radarr setup: same but URL `/radarr`.
4. Config snippet showing the `webhook:` block and a `path_mappings:` example
   for Docker users.
5. Example systemd unit override pointing to `serve` instead of `watch`.

No `docs/webhook.md`. Architecture and migration docs deferred until real-user
demand justifies them.

## Dependencies

Adds two runtime deps to `pyproject.toml`:
- `fastapi` (latest stable)
- `uvicorn[standard]` (production-grade ASGI server with reload disabled in
  prod)

Pydantic comes transitively via FastAPI. The added install footprint is
non-trivial (FastAPI + Starlette + Pydantic + uvicorn + their transitive deps)
but acceptable for an OSS tool the user runs locally. If install footprint
becomes a complaint, an extras group `[webhook]` could move them out of the
default install (deferred).

## Migration / rollout

Breaking-change considerations: none for OSS users running `watch`; the
existing `watch` subcommand is unchanged. `serve` is purely additive on the
code side. The deployment that motivated this work flips the systemd unit's
`ExecStart` and adds the `webhook:` config block.

## Open questions

None blocking. Listed for record:
- Should `serve` also do a `cold_start_scan` on startup like `watch` does? **No** — the cron `--all` already catches anything that arrived while the
  service was down. Adding it to `serve` duplicates work.
- Should the worker's lockfile path default to `/tmp` or `/var/lock`?
  `/var/lock` is more correct (FHS) but requires write permission for the
  service user. Default to `/var/lock/bazarr-topn-scan.lock`; the systemd
  unit creates the dir with proper ownership via `RuntimeDirectory=` or the
  installer pre-creates it.

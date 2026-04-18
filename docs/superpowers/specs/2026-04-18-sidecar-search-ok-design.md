# Sidecar `search_ok` — permanent fix for rate-limit-as-done bug

**Date:** 2026-04-18
**Status:** approved (brainstorming)

## Background

On the BoraCloud deployment (`languages: [tr]`, provider: `opensubtitlescom`), 394 of 554 Turkish sidecars (71%) currently sit at `saved=0, available=0, clean=true` and are therefore skipped by every scheduled scan until the 30-day recheck. The bulk were written on 2026-04-16 during the initial `scan --all` while opensubtitlescom was returning `429 Too Many Requests`. `scheduled-scan.log` contains 615 such errors.

The root cause is that `download_top_n` treats "empty candidate list" as a clean outcome regardless of *why* it was empty:

- [`src/bazarr_topn/subtitle_finder.py:198-204`](src/bazarr_topn/subtitle_finder.py:198) returns `DownloadResult(clean=True, available_count=unfiltered_count)` when `candidates` is `[]`, even if the list is empty because `find_subtitles` ran out of retries while every attempt had the provider discarded.
- [`src/bazarr_topn/subtitle_finder.py:99-114`](src/bazarr_topn/subtitle_finder.py:99) only triggers a retry when `newly_discarded = discarded - before` is non-empty. Because `ProviderPool` is reused across the whole scan, a provider discarded on video #1 stays discarded on video #2, where `newly_discarded` is empty and the retry loop breaks immediately.
- [`src/bazarr_topn/sidecar.py:93`](src/bazarr_topn/sidecar.py:93) treats `saved < min(target, available)` as the completeness check. With `available=0`, `min(10, 0) = 0`, and `0 < 0` is false, so the sidecar is considered complete and `is_topn_done` returns true.

## Goals

1. **Permanent fix:** stuck videos return to the eligible pool without `--force`, a manual migration script, or any operator action.
2. **Idempotent:** running `scan --all` repeatedly converges. Once a video has a schema-v2 sidecar, subsequent runs that don't lower `config.top_n` or age past `topn_recheck_days` are no-ops.
3. **Distinguishable states:** a sidecar must carry enough information to tell "search completed, genuinely 0 results" apart from "search was rate-limited, retry later."
4. **Cross-video resilience:** one rate-limited video must not poison the rest of the scan.

## Non-goals

- Retroactively repairing already-downloaded subtitle files. This spec only changes sidecar semantics and the search/download flow.
- Changing rate-limit backoff defaults, provider list, or `min_score`. Those are orthogonal.
- Mover-side changes. Confirmed out of scope for this shortfall; the mover is tier-agnostic via mergerfs.

## Design

### 1. Schema bump: `schema_version: 2` and `search_ok: true`

```jsonc
{
  "schema_version": 2,
  "target": 10,
  "saved": 10,
  "available": 42,
  "clean": true,
  "search_ok": true,
  "completed_at": "2026-04-19T03:14:15+00:00"
}
```

- `schema_version` starts at `2` on every new write. Any sidecar without this key is treated as v1 (legacy).
- `search_ok` is the new authoritative "did the search actually run to completion?" flag. `true` when `find_subtitles` returned without raising `SearchUnavailable` (see below). `false` when it raised.
- `clean` keeps its existing meaning: "all attempted downloads succeeded." It is *not* a proxy for search success anymore.

### 2. New exception: `SearchUnavailable`

In `subtitle_finder.py`:

```python
class SearchUnavailable(Exception):
    """Raised when find_subtitles exhausts retries with a discarded provider."""
```

`find_subtitles` raises this when the retry loop terminates with a discarded provider still in the pool and `raw_subs == []`. It does **not** raise when the search actually returned an empty list without discards (genuinely niche content).

### 3. `download_top_n` behavior

```python
try:
    candidates = find_subtitles(video, language, pool, config=config)
except SearchUnavailable:
    logger.warning("  Search unavailable for [%s] (rate-limited); will retry next scan", lang_str)
    return DownloadResult(saved_paths=[], clean=False, available_count=0, search_ok=False)
```

The existing "genuinely empty" branch stays intact and returns `search_ok=True`. `DownloadResult` gains a `search_ok: bool` field.

### 4. Cross-video discard reset

In `scanner.process_video`, before each language's `download_top_n` call:

```python
pool.discarded_providers.clear()
```

This resets the pool's discard state per video. Within-video retry is still handled by the existing `newly_discarded` logic. Rationale: the existing mechanism depends on "newly" discarded, which is broken across videos when the same provider stays discarded; clearing per-video restores the invariant the retry code was written against.

### 5. `is_topn_done` acceptance rule

A sidecar is "done" only when **all** of:

- `schema_version >= 2`
- `search_ok is True`
- `clean is True`
- `saved >= min(target, available)`
- `target >= config.top_n`
- `completed_at` within `config.topn_recheck_days`

Legacy v1 sidecars (missing `schema_version`) always return `False` from `is_topn_done`. They will be overwritten by v2 on the next scan.

### 6. No migration script

The 394 stuck sidecars are legacy v1. The next scheduled scan (02:17 / 14:17 UTC+3 cron) will naturally rewrite them because `is_topn_done` rejects v1. If the re-search succeeds, they get a correct v2 sidecar; if rate-limited, they get `search_ok=false` and will retry the run after that. The system self-heals with no operator step.

## Data flow summary

```
find_subtitles
  ├── provider discarded + retries exhausted → raise SearchUnavailable
  └── returns (possibly empty) list

download_top_n
  ├── SearchUnavailable     → DownloadResult(clean=False, search_ok=False, available=0)
  ├── candidates == []      → DownloadResult(clean=True,  search_ok=True,  available=unfiltered_count)
  └── candidates downloaded → DownloadResult(clean=…,     search_ok=True,  available=unfiltered_count)

scanner.process_video
  ├── pool.discarded_providers.clear()          (per video)
  ├── for lang:
  │     result = download_top_n(...)
  │     write_sidecar(..., SidecarData(
  │         schema_version=2,
  │         target=config.top_n,
  │         saved=len(result.saved_paths),
  │         available=result.available_count,
  │         clean=result.clean,
  │         search_ok=result.search_ok,
  │     ))
```

## Testing

- **Unit: `SearchUnavailable`** — `find_subtitles` called against a fake pool that discards the provider on every attempt raises `SearchUnavailable` after `rate_limit_retries` attempts.
- **Unit: genuinely empty** — fake pool returns `[]` without discarding. `find_subtitles` does not raise.
- **Unit: `download_top_n` rate-limit path** — when `find_subtitles` raises, returns `clean=False, search_ok=False`.
- **Unit: `download_top_n` empty path** — when `find_subtitles` returns `[]` cleanly, returns `clean=True, search_ok=True, available=0`.
- **Unit: sidecar v2 round-trip** — write then read returns every new field.
- **Unit: `is_topn_done`** truth table:
  - v1 sidecar (no `schema_version`) → False
  - v2 `search_ok=false` → False
  - v2 `clean=false` → False
  - v2 all good, fresh `completed_at` → True
  - v2 all good, stale `completed_at` → False
- **Unit: scanner clears discards** — `process_video` called twice with a pool whose `discarded_providers` is pre-populated. Second call sees a clear pool on entry.
- **Integration: legacy resurrection** — create a v1 sidecar with `saved=0, available=0, clean=true` on disk; run `scan`; assert sidecar is overwritten with v2 fields matching the fresh search outcome.

## Rollout / ops

1. Land the change, run the test suite clean.
2. Deploy to `/opt/boracloud/bazarr-topn/` (systemd restart of `bazarr-topn.service` plus the existing cron continues driving scheduled scans).
3. The next `14:17 UTC+3` scan will start rewriting legacy sidecars. First pass across ~394 stuck videos will likely take multiple scans to finish, bounded by `search_delay=3.0s` plus backoffs.
4. Success metric: count of v2 sidecars with `search_ok=true, saved>=min(target, available)` trends upward scan over scan; v1 sidecars trend to 0.

## Open risks

- **Re-invite the rate limit.** 394 retries in short succession could re-trip opensubtitlescom 429s. The `search_delay: 3.0` + `download_delay: 5.0` floors plus per-video pool reset make this acceptable, but it's worth watching `scheduled-scan.log` for the first few scans. If needed, `rate_limit_initial_backoff` can be raised via config without code changes.
- **Niche Turkish content really does have 0 subs.** After the first clean retry for such videos, the v2 sidecar with `saved=0, available=0, clean=true, search_ok=true` will correctly park them for 30 days. That's the intended steady state — not a bug.

# Log-Capture Detection for Provider Failures

**Date:** 2026-04-18 (second iteration)
**Status:** approved (brainstorming)
**Supersedes (partially):** [2026-04-18-sidecar-search-ok-design.md](2026-04-18-sidecar-search-ok-design.md) — keeps the v2 schema, `SearchUnavailable`, `search_ok`, and scanner changes; replaces the `discarded_providers`-based detection with log-capture.

## Background

The prior fix assumed subliminal's `ProviderPool.list_subtitles_provider` adds failed providers to `pool.discarded_providers` on rate-limit errors. Reading the actual source on the deployed venv (`subliminal.core.ProviderPool.list_subtitles_provider`):

```python
try:
    subtitles = self[provider].list_subtitles(video, provider_languages)
except DiscardingError as e:
    handle_exception(e, f'Provider {provider}')
    return None              # <-- only this path triggers discard in the caller
except Exception as e:
    handle_exception(e, f'Provider {provider}')
    return []                # <-- 429s, network errors, unknown bugs land here
```

`OpenSubtitlesComError: Too Many Requests` is a generic `Exception`, so it returns `[]` with **no** change to `discarded_providers`. Our `newly_discarded` check never fires; `find_subtitles` returns `[]` cleanly; `download_top_n` writes `DownloadResult(clean=True, search_ok=True, available_count=0)`; the scanner persists a v2 sidecar that `is_topn_done` treats as "genuinely empty."

Confirmed on the production backfill run before kill: 12 v2 sidecars were written with `saved=0, available=0, clean=True, search_ok=True` while the log showed repeated `Too Many Requests` tracebacks for the same videos. The `SearchUnavailable` warning from the previous fix never appeared in the log.

## Goals

1. **Correct detection.** A provider failure during `pool.list_subtitles()` — for any exception type subliminal swallows — is detected and drives the existing `SearchUnavailable` path.
2. **Robust across subliminal versions.** Do not depend on subliminal's internal bookkeeping (`discarded_providers`, exception class hierarchy).
3. **Rescue the poisoned sidecars.** The 12 v2 sidecars with `saved=0, available=0` currently on disk get re-processed after the fix lands.
4. **More aggressive recheck.** Niche content and stable content alike get re-searched every 3 days (down from 30), so a miss of any kind self-heals quickly.

## Non-goals

- Replacing subliminal. Rate-limit handling continues to flow through subliminal's provider calls.
- Per-provider error classification. We only distinguish "search completed without errors" vs "search emitted at least one provider error."
- Per-video concurrency. The scan loop remains single-threaded; log handler scoping does not need to be thread-safe.

## Design

### 1. `_captured_subliminal_errors` context manager

New helper in `src/bazarr_topn/subtitle_finder.py`:

```python
@contextlib.contextmanager
def _captured_subliminal_errors() -> Iterator[_SubliminalErrorCapture]:
    """Capture ERROR-level log records emitted by subliminal during the block.

    Subliminal's handle_exception logs 'Unexpected error. Provider %s' at ERROR
    whenever it swallows a provider exception. That is our only cross-version
    signal that a provider call failed — discarded_providers is only updated
    for DiscardingError subclasses.
    """
    capture = _SubliminalErrorCapture()
    logger = logging.getLogger("subliminal")
    logger.addHandler(capture)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)
```

`_SubliminalErrorCapture` is a `logging.Handler` subclass with `level=logging.ERROR` that stores emitted records on `self.records: list[logging.LogRecord]` and exposes `had_errors: bool` and `first_error_message: str` properties.

The handler is installed on the `"subliminal"` logger (covers `subliminal.core`, `subliminal.providers.*`, etc. via Python's hierarchical logger propagation). Because the handler is added to a named logger's `handlers` attribute, it receives records regardless of what root handlers exist — safe alongside the user's configured file/console logging.

### 2. `find_subtitles` retry loop

Replace the `discarded_providers`-based retry with capture-based retry:

```python
def find_subtitles(video, language, pool, *, config=None):
    retries = config.rate_limit_retries if config is not None else 0
    backoff = config.rate_limit_initial_backoff if config is not None else 0.0

    raw_subs: list = []
    for attempt in range(retries + 1):
        with _captured_subliminal_errors() as captured:
            raw_subs = pool.list_subtitles(video, {language})
        if not captured.had_errors:
            break
        if attempt >= retries:
            raise SearchUnavailable(
                f"subliminal reported errors during search for {language} "
                f"after {retries + 1} attempts: {captured.first_error_message}"
            )
        sleep_s = backoff * (2 ** attempt)
        logger.warning(
            "Provider errors during search (likely rate-limited). "
            "Sleeping %.0fs before retry %d/%d. First error: %s",
            sleep_s, attempt + 1, retries, captured.first_error_message,
        )
        if sleep_s > 0:
            time.sleep(sleep_s)

    # score + return (unchanged)
    ...
```

The `pool.discarded_providers.discard(...)` cleanup from the old retry loop is dropped: the detection no longer depends on that set, and subliminal already knows how to manage it.

### 3. Everything else stays the same

- `SearchUnavailable` exception class — unchanged.
- `download_top_n` try/except — unchanged (still catches `SearchUnavailable`, returns `DownloadResult(clean=False, search_ok=False, available_count=0)`).
- `DownloadResult.search_ok: bool = True` field — unchanged.
- `SidecarData.search_ok`, `schema_version`, `SCHEMA_VERSION = 2` — unchanged.
- `write_sidecar` stamps schema_version on every write — unchanged.
- `read_sidecar` legacy tolerance (v1 defaults to `search_ok=False`) — unchanged.
- `is_topn_done` acceptance rule — unchanged.
- `scanner.process_video` clears `pool.discarded_providers` at entry — **kept** (defensive; low cost even if not the primary signal anymore).
- `scanner.process_video` persists `result.search_ok` — unchanged.

### 4. Config change: `topn_recheck_days: 30 → 3`

`Config.topn_recheck_days: int = 3` in `src/bazarr_topn/config.py`. Update `config.example.yaml` and the BoraCloud installer (`BoraCloud/scripts/43-bazarr-topn.sh`) so the deployed config picks up the new default. The `topn_empty_recheck_days` tier discussed during brainstorming is dropped — one knob, applies uniformly.

### 5. Test contract

`tests/test_subtitle_finder.py::FakePool` today simulates the wrong behavior (adds to `discarded_providers` on failure). It's updated to match real subliminal:

```python
class FakePool:
    def __init__(self, *, fail_list_times=0, fail_download_times=0, subtitles_to_return=None, provider="opensubtitlescom"):
        ...

    def list_subtitles(self, video, languages):
        self.list_calls += 1
        if self.list_calls <= self.fail_list_times:
            # Real subliminal swallows the exception, emits an ERROR log, returns [].
            # discarded_providers is NOT touched (that path is DiscardingError only).
            logging.getLogger("subliminal.core").error(
                "Unexpected error. Provider %s", self.provider
            )
            return []
        return list(self._subtitles)
```

Tests updated:
- `test_gives_up_after_max_retries` — still expects `SearchUnavailable`, but its trigger is now the emitted log rather than `discarded_providers`.
- `test_retries_after_discard` — renamed to `test_retries_after_error` for clarity; same behavioral contract (N fails then a success returns subtitles).
- New: `test_find_subtitles_returns_empty_cleanly` — FakePool returns `[]` without emitting an error; `find_subtitles` returns `[]` without raising.
- New: `test_captured_subliminal_errors_handler_installation` — context manager adds/removes its handler around the block, including on exception.
- New: `test_captured_subliminal_errors_captures_child_loggers` — an error emitted on `subliminal.providers.opensubtitlescom` is captured by a handler installed on `subliminal` (verifies propagation).
- `TestDownloadTopNSearchOk` — existing tests keep their contracts with the updated FakePool.
- `TestProcessVideoSearchOkSidecar::test_writes_search_ok_false_on_rate_limit_result` — already drives from a fake `DownloadResult`, still works.

Obsolete assertions on `pool.discarded_providers` inside retry tests are removed.

## Rescue

Operator command, run once after `git pull` on the server and before the next `scan --all`:

```bash
find /mnt/media -name "*.topn.json" -print0 \
  | xargs -0 grep -l '"schema_version": 2' \
  | xargs grep -l '"saved": 0' \
  | xargs grep -l '"available": 0' \
  | tee /tmp/rescue-suspect.txt \
  | xargs -r rm

wc -l /tmp/rescue-suspect.txt   # expect ~12 lines
```

Pre-fix, no v2 sidecars existed on disk. Every `schema_version=2, saved=0, available=0` sidecar is from the buggy run. Deletion is safe; the next `scan --all` rewrites them with correct `search_ok` state. Genuinely-niche videos land back at `search_ok=true, saved=0, available=0` and stick; rate-limit victims land at `search_ok=false` and retry next cron.

## Rollout

1. Land all code changes on branch `fix/log-capture-detection`, merge to main, push.
2. On server: `cd /home/bora/bazarr-topn && git pull`.
3. Run the rescue command above.
4. Verify with `is_topn_done` smoke script that a typical fresh v1 sidecar is rejected (as before) and that the detection triggers on a synthesized ERROR record.
5. Either `sudo systemctl restart bazarr-topn.service` to refresh the watcher (optional — cron fires a fresh process anyway), or wait for the next cron at 02:17 UTC+3.
6. Tail `/opt/boracloud/bazarr-topn/logs/scheduled-scan.log`. Expect `SearchUnavailable` paths to now produce the new warning `Provider errors during search (likely rate-limited). Sleeping …` and, on final exhaustion, `Search unavailable for [tr] (…); will retry next scan`.
7. Count v2 sidecars with `search_ok=true` vs `search_ok=false` after the scan stabilizes. Rate-limit victims should be in the `false` bucket and re-retried each subsequent cron.

## Open risks

- **Unrelated ERROR logs from subliminal** could trigger a false positive retry (e.g., a provider logs ERROR for reasons unrelated to the current `list_subtitles` call). In practice, subliminal's own code paths only log ERROR in `handle_exception` during provider work, and our capture scope is the `pool.list_subtitles(...)` call — tight enough. Mitigation if ever observed: narrow the filter to records whose message matches `"Unexpected error. Provider "` prefix.
- **Third-party loggers named under `subliminal.*`** — hypothetical, none known. Propagation is controlled by our handler being on the `"subliminal"` logger itself.
- **Shorter 3-day recheck** means each video re-downloads its top 10 every 3 days. On a 554-video × 10 sub library that's ~5,540 downloads per 3 days (~1,850/day), well under any VIP quota, but worth monitoring if the library grows or more languages are added.

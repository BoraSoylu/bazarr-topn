# Log-Capture Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `discarded_providers`-based detection in `find_subtitles` with a `logging.Handler`-based capture that matches real subliminal behavior, so rate-limit errors actually drive the `SearchUnavailable` / `search_ok=False` path instead of being silently persisted as "clean empty" sidecars.

**Architecture:** A new `_captured_subliminal_errors()` context manager in `subtitle_finder.py` installs a level-`ERROR` handler on the `subliminal` logger around each `pool.list_subtitles()` attempt. The retry loop treats `captured.had_errors` as the failure signal. `SearchUnavailable`, `DownloadResult.search_ok`, sidecar v2 schema, scanner pool-clear, `is_topn_done` acceptance rule — all untouched. Separately, the `topn_recheck_days` default drops from 30 → 3.

**Tech Stack:** Python 3.12, pytest, subliminal, `logging`, `contextlib`, dataclasses.

**Spec:** [docs/superpowers/specs/2026-04-18-log-capture-detection-design.md](../specs/2026-04-18-log-capture-detection-design.md)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/bazarr_topn/subtitle_finder.py` | Modify | Add `_SubliminalErrorCapture` handler + `_captured_subliminal_errors` context manager; rewrite `find_subtitles` retry loop to use them |
| `src/bazarr_topn/config.py` | Modify | `topn_recheck_days: int = 3` (was 30) |
| `config.example.yaml` | Modify | Update the `topn_recheck_days` doc comment + default to 3 |
| `tests/test_subtitle_finder.py` | Modify | Rewrite `FakePool.list_subtitles` to match real subliminal (emit log, return `[]`, no `discarded_providers` mutation); add capture-mechanism unit tests; update retry tests |

Files deliberately NOT touched: `src/bazarr_topn/sidecar.py`, `src/bazarr_topn/scanner.py`, `tests/test_sidecar.py`, `tests/test_scanner.py`, `tests/conftest.py` (its `default_config` fixture keeps `topn_recheck_days=30` explicit so the sidecar staleness tests stay deterministic regardless of Config defaults).

---

### Task 1: Add the error-capture context manager and its unit tests

**Files:**
- Modify: `src/bazarr_topn/subtitle_finder.py` — add new imports, `_SubliminalErrorCapture` class, `_captured_subliminal_errors` context manager. Place after `SearchUnavailable` class, before `configure_cache()`.
- Modify: `tests/test_subtitle_finder.py` — add new `TestCapturedSubliminalErrors` test class at end of file.

- [ ] **Step 1: Write failing tests for the capture helper**

Add to the end of `tests/test_subtitle_finder.py`:

```python
class TestCapturedSubliminalErrors:
    def test_captures_error_on_subliminal_logger(self) -> None:
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        with _captured_subliminal_errors() as cap:
            logging.getLogger("subliminal").error("boom")
        assert cap.had_errors is True
        assert "boom" in cap.first_error_message

    def test_captures_error_on_subliminal_child_logger(self) -> None:
        """Propagation: a handler on 'subliminal' sees records from 'subliminal.x.y'."""
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        with _captured_subliminal_errors() as cap:
            logging.getLogger("subliminal.providers.opensubtitlescom").error(
                "Unexpected error. Provider %s", "opensubtitlescom",
            )
        assert cap.had_errors is True
        assert "opensubtitlescom" in cap.first_error_message

    def test_ignores_warning_and_info(self) -> None:
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        with _captured_subliminal_errors() as cap:
            logging.getLogger("subliminal").warning("warn")
            logging.getLogger("subliminal").info("info")
        assert cap.had_errors is False

    def test_ignores_errors_on_unrelated_loggers(self) -> None:
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        with _captured_subliminal_errors() as cap:
            logging.getLogger("not_subliminal").error("unrelated")
        assert cap.had_errors is False

    def test_handler_removed_on_clean_exit(self) -> None:
        from bazarr_topn.subtitle_finder import (
            _captured_subliminal_errors, _SubliminalErrorCapture,
        )

        logger = logging.getLogger("subliminal")
        before = list(logger.handlers)
        with _captured_subliminal_errors():
            pass
        after = list(logger.handlers)
        assert after == before
        assert not any(isinstance(h, _SubliminalErrorCapture) for h in after)

    def test_handler_removed_on_exception(self) -> None:
        from bazarr_topn.subtitle_finder import (
            _captured_subliminal_errors, _SubliminalErrorCapture,
        )

        logger = logging.getLogger("subliminal")
        before = list(logger.handlers)
        with pytest.raises(RuntimeError):
            with _captured_subliminal_errors():
                raise RuntimeError("oops")
        after = list(logger.handlers)
        assert after == before
        assert not any(isinstance(h, _SubliminalErrorCapture) for h in after)

    def test_first_error_message_when_empty(self) -> None:
        from bazarr_topn.subtitle_finder import _captured_subliminal_errors

        with _captured_subliminal_errors() as cap:
            pass
        assert cap.had_errors is False
        assert cap.first_error_message == ""
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /Users/bora/Developer/repos/personal/bazarr-topn
.venv/bin/python -m pytest tests/test_subtitle_finder.py::TestCapturedSubliminalErrors -v
```

Expected: FAIL with `ImportError: cannot import name '_captured_subliminal_errors'`.

- [ ] **Step 3: Add imports**

In `src/bazarr_topn/subtitle_finder.py`, add near the top-of-file imports (after the existing `import logging` / `import time` / etc.):

```python
import contextlib
from collections.abc import Iterator
```

- [ ] **Step 4: Add `_SubliminalErrorCapture` and `_captured_subliminal_errors`**

In `src/bazarr_topn/subtitle_finder.py`, add **after** the existing `class SearchUnavailable(Exception):` block and **before** `def configure_cache():`:

```python
class _SubliminalErrorCapture(logging.Handler):
    """Logging handler that collects ERROR-level records from subliminal.

    Used by `_captured_subliminal_errors` to detect provider failures that
    subliminal swallows with a generic `except Exception` (rate-limit errors,
    network errors, etc.). Those show up only as log records; subliminal does
    not add the provider to `discarded_providers` in that branch.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def had_errors(self) -> bool:
        return bool(self.records)

    @property
    def first_error_message(self) -> str:
        if not self.records:
            return ""
        return self.records[0].getMessage()


@contextlib.contextmanager
def _captured_subliminal_errors() -> Iterator[_SubliminalErrorCapture]:
    """Capture ERROR-level log records from the ``subliminal`` logger tree.

    Subliminal's ``handle_exception`` logs ``Unexpected error. Provider %s``
    at ERROR whenever it swallows a provider exception in
    ``ProviderPool.list_subtitles_provider``. That is the only cross-version
    signal that a search attempt failed, because the generic ``except
    Exception`` branch does not update ``discarded_providers``.

    The handler attaches to the ``subliminal`` logger, so records from child
    loggers (``subliminal.core``, ``subliminal.providers.*``) are captured
    via standard propagation.
    """
    capture = _SubliminalErrorCapture()
    logger = logging.getLogger("subliminal")
    logger.addHandler(capture)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)
```

- [ ] **Step 5: Run tests — verify all pass**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py::TestCapturedSubliminalErrors -v
```

Expected: 7 passed.

- [ ] **Step 6: Run the full file to confirm no regression in existing tests**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py -v
```

Expected: all pre-existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add src/bazarr_topn/subtitle_finder.py tests/test_subtitle_finder.py
git commit -m "$(cat <<'EOF'
feat: add _captured_subliminal_errors context manager

Installs a logging.Handler on the 'subliminal' logger for the scope of
the block. Will be used in find_subtitles to detect provider failures
that subliminal swallows silently (generic except Exception branch in
ProviderPool.list_subtitles_provider).
EOF
)"
```

---

### Task 2: Rewrite `find_subtitles` retry and fix `FakePool` to match real subliminal

**Files:**
- Modify: `src/bazarr_topn/subtitle_finder.py` — replace the retry loop inside `find_subtitles`.
- Modify: `tests/test_subtitle_finder.py` — rewrite `FakePool.list_subtitles` behavior and the three retry tests.

**Context:** `FakePool` today simulates a discard behavior that real subliminal does not implement for generic exceptions. This is the core reason the prior fix passed tests but failed in production. The updated FakePool emits an ERROR record on the `subliminal.core` logger (matching `handle_exception`) and returns `[]` without touching `discarded_providers`.

- [ ] **Step 1: Update `FakePool.list_subtitles`**

In `tests/test_subtitle_finder.py`, replace the `list_subtitles` method of `FakePool` with:

```python
    def list_subtitles(self, video: Any, languages: set[Language]) -> list[FakeSubtitle]:
        self.list_calls += 1
        if self.list_calls <= self.fail_list_times:
            # Match real subliminal: the generic `except Exception` branch in
            # ProviderPool.list_subtitles_provider logs via handle_exception
            # ("Unexpected error. Provider %s") at ERROR and returns []. It
            # does NOT add the provider to discarded_providers; only
            # DiscardingError takes that path.
            logging.getLogger("subliminal.core").error(
                "Unexpected error. Provider %s", self.provider,
            )
            return []
        return list(self._subtitles)
```

Also add `import logging` at the top of `tests/test_subtitle_finder.py` if it is not already there (it is — leave the existing import alone, just verify).

- [ ] **Step 2: Update `test_retries_after_discard`**

Rename (replace in place) `TestFindSubtitlesRetry::test_retries_after_discard` to reflect the real mechanism:

```python
    def test_retries_after_logged_error(self, no_delay_config: Config) -> None:
        """First attempt emits an error log; retry attempt succeeds and returns subs."""
        pool = FakePool(fail_list_times=1)
        subs = find_subtitles(
            MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
        )
        assert pool.list_calls == 2
        assert len(subs) == 1
        # discarded_providers was never populated on the fail path
        # (real subliminal doesn't discard on generic exceptions).
        assert pool.discarded_providers == set()
```

- [ ] **Step 3: Leave `test_gives_up_after_max_retries` and `TestSearchUnavailable` alone for now**

Their current assertions (that `SearchUnavailable` is raised after exhausting retries) express a behavioral contract that must still hold after the rewrite. They will pass once the retry loop is rewritten in Step 5. Verify by reading them — do not modify.

- [ ] **Step 4: Run tests to see the current failure shape**

```bash
cd /Users/bora/Developer/repos/personal/bazarr-topn
.venv/bin/python -m pytest tests/test_subtitle_finder.py::TestFindSubtitlesRetry tests/test_subtitle_finder.py::TestSearchUnavailable -v
```

Expected: At least `test_retries_after_logged_error`, `test_gives_up_after_max_retries`, and `test_raises_when_retries_exhausted_with_discard` FAIL. The old retry logic keys off `newly_discarded`, which is empty under the new FakePool, so the loop exits after the first attempt and no `SearchUnavailable` is raised.

- [ ] **Step 5: Rewrite the retry loop in `find_subtitles`**

In `src/bazarr_topn/subtitle_finder.py`, replace the entire body of `find_subtitles` from the line `raw_subs: list = []` through the end of the retry loop (up to but not including the `scored: list[ScoredSubtitle] = []` line) with:

```python
    raw_subs: list = []
    for attempt in range(retries + 1):
        with _captured_subliminal_errors() as captured:
            raw_subs = pool.list_subtitles(video, {language})
        if not captured.had_errors:
            break
        if attempt >= retries:
            # Subliminal swallowed at least one provider exception on every
            # attempt. Signal up so the caller writes search_ok=False.
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
```

The prior `before = set(pool.discarded_providers)`, `newly_discarded = ... - before`, and `for p in newly_discarded: pool.discarded_providers.discard(p)` are all removed — they depended on a mechanism that doesn't fire in practice.

- [ ] **Step 6: Run the retry/SearchUnavailable tests — verify they pass**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py::TestFindSubtitlesRetry tests/test_subtitle_finder.py::TestSearchUnavailable -v
```

Expected: all PASS. Specifically:
- `test_no_failure_single_call` — pool doesn't emit errors, retry loop exits on first iteration, returns 1 sub.
- `test_retries_after_logged_error` — first call emits ERROR, retry succeeds.
- `test_gives_up_after_max_retries` — all 3 attempts emit ERROR, `SearchUnavailable` raised.
- `test_raises_when_retries_exhausted_with_discard` — same contract, now driven by log capture.
- `test_no_raise_when_search_returns_empty_cleanly` — empty result without error, no raise.

- [ ] **Step 7: Run the full subtitle_finder test file**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py -v
```

Expected: all tests PASS. In particular, the three `TestDownloadTopNSearchOk` tests still pass because `download_top_n` catches `SearchUnavailable` regardless of how it was triggered.

- [ ] **Step 8: Run the broader test suite to confirm no regressions elsewhere**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py tests/test_sidecar.py tests/test_scanner.py -v
```

Expected: all PASS (sidecar and scanner tests are untouched; they should remain green).

- [ ] **Step 9: Commit**

```bash
git add src/bazarr_topn/subtitle_finder.py tests/test_subtitle_finder.py
git commit -m "$(cat <<'EOF'
fix: detect provider failures via subliminal ERROR logs, not discards

Real subliminal only updates discarded_providers for DiscardingError
subclasses; generic exceptions (including OpenSubtitlesComError:
'Too Many Requests') are caught with `except Exception`, logged via
handle_exception, and return []. The prior retry keyed off
discarded_providers and thus never fired for rate limits.

Replace the detection with a logging.Handler installed on the
'subliminal' logger around each list_subtitles attempt. Any
ERROR-level record during the call means the attempt failed; retries
then decrement toward the existing SearchUnavailable raise.

Also update FakePool in the tests to match real subliminal: emit an
ERROR log and return [] on failure, without touching
discarded_providers. Rename test_retries_after_discard to
test_retries_after_logged_error to reflect the real mechanism.
EOF
)"
```

---

### Task 3: Drop `topn_recheck_days` default from 30 to 3

**Files:**
- Modify: `src/bazarr_topn/config.py` — change the default value of `topn_recheck_days` in the `Config` dataclass.
- Modify: `config.example.yaml` — update the documented default.

**Rationale:** Server is 24/7 and downloads are the only real cost; 30-day staleness is too slow to recover from rate-limit windows or to pick up newly-available subtitles for niche content.

- [ ] **Step 1: Write a failing test for the new default**

Append to `tests/test_config.py` — add at the end of the file, inside a new class (or append to an existing `TestNewConfigDefaults` class if one exists — check before writing):

```python
class TestTopnRecheckDaysDefault:
    def test_default_is_three(self) -> None:
        from bazarr_topn.config import Config
        cfg = Config()
        assert cfg.topn_recheck_days == 3
```

- [ ] **Step 2: Run test — verify it fails**

```bash
cd /Users/bora/Developer/repos/personal/bazarr-topn
.venv/bin/python -m pytest tests/test_config.py::TestTopnRecheckDaysDefault -v
```

Expected: FAIL with `assert 30 == 3`.

- [ ] **Step 3: Change the default in `Config`**

In `src/bazarr_topn/config.py`, locate the `topn_recheck_days: int = 30` field (around line 97) and change it to:

```python
    topn_recheck_days: int = 3
```

Leave every other field and line alone.

- [ ] **Step 4: Update the documented default in `config.example.yaml`**

Find the `topn_recheck_days:` block in `config.example.yaml` (currently around lines 60-63) and replace the existing block (from the comment that begins with `# Re-check interval in days.` through the `topn_recheck_days: 30` line) with:

```yaml
# Re-check interval in days. Sidecars older than this age are treated as
# stale and the video is re-processed — even if it was previously marked
# done. Keeps niche content re-searched regularly in case new subs appear,
# and lets any rate-limit victim recover within a few days. Default is
# low because the server side is cheap; raise this if download quota
# becomes a concern.
topn_recheck_days: 3
```

- [ ] **Step 5: Run test — verify it passes**

```bash
.venv/bin/python -m pytest tests/test_config.py::TestTopnRecheckDaysDefault -v
```

Expected: PASS.

- [ ] **Step 6: Verify the broader config test module still passes**

```bash
.venv/bin/python -m pytest tests/test_config.py -v
```

Expected: all PASS. The `default_config` fixture in `tests/conftest.py` explicitly passes `topn_recheck_days=30`, so it stays deterministic regardless of the Config default.

- [ ] **Step 7: Commit**

```bash
git add src/bazarr_topn/config.py config.example.yaml tests/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): topn_recheck_days default 30 → 3

Server is 24/7; downloads are the only real cost of rechecks, and 3-day
cadence still only re-downloads each video's top 10 ~twice a week on
a typical library (well under any VIP quota). Gives niche content a
prompt reprocess if subs appear later, and lets rate-limit victims
recover within a few days even when the new detection somehow misses.
EOF
)"
```

---

### Task 4: Full-suite verification and production smoke check

**Files:** No code changes. Verification gate only.

- [ ] **Step 1: Run the entire test suite**

```bash
cd /Users/bora/Developer/repos/personal/bazarr-topn
.venv/bin/python -m pytest -v
```

Expected: every test in the new chain passes. The 3 `tests/test_sync.py` failures that were pre-existing on main (missing `ffsubsync` module) stay failing for the same environmental reason — not a regression.

- [ ] **Step 2: Smoke-check `SearchUnavailable` actually fires against a FakePool that matches the bug pattern**

```bash
.venv/bin/python - <<'PY'
import logging
from unittest.mock import MagicMock
from babelfish import Language
from bazarr_topn.config import Config
from bazarr_topn.subtitle_finder import find_subtitles, SearchUnavailable


class AlwaysRateLimitedPool:
    """Mimics real subliminal: emits an ERROR log and returns [] on every call.
    Does NOT add anything to discarded_providers. This is the exact shape that
    broke the previous fix."""
    discarded_providers: set[str] = set()

    def list_subtitles(self, video, languages):
        logging.getLogger("subliminal.core").error(
            "Unexpected error. Provider %s", "opensubtitlescom"
        )
        return []


cfg = Config(
    languages=["tr"], top_n=10,
    search_delay=0, download_delay=0,
    rate_limit_initial_backoff=0, rate_limit_retries=2,
)
pool = AlwaysRateLimitedPool()
try:
    find_subtitles(MagicMock(), Language.fromalpha2("tr"), pool, config=cfg)
except SearchUnavailable as e:
    print(f"OK — SearchUnavailable raised as expected: {e}")
else:
    raise SystemExit("BUG: SearchUnavailable NOT raised against rate-limit pattern")
PY
```

Expected output:
```
OK — SearchUnavailable raised as expected: subliminal reported errors during search for tr after 3 attempts: Unexpected error. Provider opensubtitlescom
```

If the output is `BUG: SearchUnavailable NOT raised against rate-limit pattern`, **stop and report** — the fix is not working and must not be merged.

- [ ] **Step 3: Smoke-check that `is_topn_done` still rejects v1 legacy sidecars (regression guard for the prior fix)**

```bash
.venv/bin/python - <<'PY'
import json, tempfile
from pathlib import Path
from bazarr_topn.config import Config
from bazarr_topn.sidecar import is_topn_done, sidecar_path

with tempfile.TemporaryDirectory() as d:
    video = Path(d) / "Frasier - S02E03.mkv"
    video.write_bytes(b"x")
    legacy = {
        "target": 10, "saved": 0, "available": 0, "clean": True,
        "completed_at": "2026-04-16T05:14:43.501667+00:00",
    }
    sidecar_path(video, "tr").write_text(json.dumps(legacy))
    cfg = Config(languages=["tr"], top_n=10,
                 topn_recheck_days=30, topn_sidecar_enabled=True)
    done = is_topn_done(video, "tr", cfg)
    print(f"legacy v1 is_topn_done: {done}  (expect False)")
    assert done is False
    print("OK — v1 legacy rejection still works")
PY
```

Expected:
```
legacy v1 is_topn_done: False  (expect False)
OK — v1 legacy rejection still works
```

- [ ] **Step 4: Search for dormant references to the old mechanism**

```bash
cd /Users/bora/Developer/repos/personal/bazarr-topn

# newly_discarded should no longer be a control-flow variable in find_subtitles.
# It is acceptable for the tests to MENTION it (e.g., in comments explaining
# the old bug); not acceptable for the production retry loop to depend on it.
grep -n "newly_discarded" src/bazarr_topn/subtitle_finder.py && echo "--- STOP: remove any remaining newly_discarded references ---" || echo "no production references ✓"

# Confirm the capture helper exists in the module.
grep -n "_captured_subliminal_errors\|_SubliminalErrorCapture" src/bazarr_topn/subtitle_finder.py
```

Expected:
- First grep: `no production references ✓`.
- Second grep: 2+ hits (class definition + context manager + at least one call-site inside `find_subtitles`).

- [ ] **Step 5: Review the git log for the branch**

```bash
cd /Users/bora/Developer/repos/personal/bazarr-topn
git log --oneline main..HEAD
```

Expected: 3 commits (Task 1 helper, Task 2 retry rewrite + FakePool, Task 3 recheck default).

- [ ] **Step 6: Do NOT commit**

This task is pure verification.

---

## Rollout (operator steps, outside the plan's commit scope)

These are documented here so the plan is complete but are **not** tasks for an implementer subagent — the controller executes them after the branch merges to main:

1. Merge `fix/log-capture-detection` to main locally (fast-forward), push to `origin/main`.
2. On server: `cd /home/bora/bazarr-topn && git pull --ff-only`.
3. **Rescue:** delete the 12 v2 sidecars left over from the prior buggy run **before** starting any new scan:
   ```bash
   find /mnt/media -name "*.topn.json" -print0 \
     | xargs -0 grep -l '"schema_version": 2' \
     | xargs grep -l '"saved": 0' \
     | xargs grep -l '"available": 0' \
     | tee /tmp/rescue-suspect.txt \
     | xargs -r rm
   wc -l /tmp/rescue-suspect.txt   # expect ~12 lines
   ```
4. (Optional) Restart the watch service to pick up the new code in its long-running process: `sudo systemctl restart bazarr-topn.service`. The cron will pick up new code regardless on its next run.
5. (Optional) Manually kick the backfill instead of waiting for the 02:17 cron:
   ```bash
   set -a; source /opt/boracloud/bazarr-topn/.env; set +a
   nohup /opt/boracloud/bazarr-topn/venv/bin/bazarr-topn \
     -c /opt/boracloud/bazarr-topn/config.yaml scan --all \
     >> /opt/boracloud/bazarr-topn/logs/scheduled-scan.log 2>&1 &
   disown
   ```
6. Tail the log and expect to see the new warning `Provider errors during search (likely rate-limited). Sleeping …` on rate-limit hits, and eventually `Search unavailable for [tr] (...); will retry next scan` on exhausted attempts.

---

## Self-Review Checklist (ran at plan-write time)

**Spec coverage:**
- Goal 1 (correct detection) → Task 1 + Task 2.
- Goal 2 (robust across subliminal versions) → Task 1 (log capture avoids subliminal internals) + Task 4 Step 4 (grep for removed dependencies).
- Goal 3 (rescue) → Rollout step 3 (documented, operator-run).
- Goal 4 (3-day recheck) → Task 3.
- Test contract changes → Task 1 (new tests), Task 2 (FakePool rewrite + retry test updates).

**Placeholder scan:** No TBDs / TODOs / "similar to"s; every code step has concrete Python.

**Type consistency:** `_SubliminalErrorCapture` / `_captured_subliminal_errors` / `captured.had_errors` / `captured.first_error_message` spelled identically across Task 1 (definition + tests), Task 2 (find_subtitles usage), and Task 4 (smoke checks).

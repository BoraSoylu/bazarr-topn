# Sidecar `search_ok` — Permanent Rate-Limit Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop rate-limited searches from permanently marking videos as "done"; let the 394 stuck Turkish sidecars on production self-heal on the next scheduled scan.

**Architecture:** Introduce `SearchUnavailable` exception that `find_subtitles` raises when retries exhaust with a discarded provider; `download_top_n` catches it and returns `search_ok=False`. Sidecars bump to `schema_version=2` with a persistent `search_ok` flag. `is_topn_done` rejects v1 and any v2 with `search_ok=False`. `scanner.process_video` clears `pool.discarded_providers` per video so one rate-limited video doesn't poison the rest of the scan.

**Tech Stack:** Python 3.12, pytest, subliminal, babelfish, dataclasses, JSON.

**Spec:** [docs/superpowers/specs/2026-04-18-sidecar-search-ok-design.md](../specs/2026-04-18-sidecar-search-ok-design.md)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/bazarr_topn/subtitle_finder.py` | Modify | Add `SearchUnavailable`; raise from `find_subtitles`; add `search_ok` to `DownloadResult`; catch in `download_top_n` |
| `src/bazarr_topn/sidecar.py` | Modify | Add `schema_version` + `search_ok` to `SidecarData`; write v2; read-tolerant for v1; `is_topn_done` rejects v1 and `search_ok=False` |
| `src/bazarr_topn/scanner.py` | Modify | Clear `pool.discarded_providers` per video; pass `search_ok` through to sidecar |
| `tests/test_subtitle_finder.py` | Modify | Tests for `SearchUnavailable`; update `test_gives_up_after_max_retries`; `search_ok` defaulting |
| `tests/test_sidecar.py` | Modify | v2 round-trip; v1 legacy read; `is_topn_done` v2/v1 truth table |
| `tests/test_scanner.py` | Modify | Pool clears per video; sidecar written with `search_ok`; legacy-v1 resurrection integration test |

---

### Task 1: Introduce `SearchUnavailable` exception

**Files:**
- Modify: `src/bazarr_topn/subtitle_finder.py` (add exception class near top, after imports)
- Modify: `tests/test_subtitle_finder.py` (add new test class)

- [ ] **Step 1: Write failing test**

Add to `tests/test_subtitle_finder.py` at the end of the file:

```python
class TestSearchUnavailable:
    def test_raises_when_retries_exhausted_with_discard(
        self, no_delay_config: Config
    ) -> None:
        from bazarr_topn.subtitle_finder import SearchUnavailable

        pool = FakePool(fail_list_times=99)  # always fails + discards
        with pytest.raises(SearchUnavailable):
            find_subtitles(
                MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
            )
        # initial + 2 retries = 3 attempts
        assert pool.list_calls == 3

    def test_no_raise_when_search_returns_empty_cleanly(
        self, no_delay_config: Config
    ) -> None:
        from bazarr_topn.subtitle_finder import SearchUnavailable

        pool = FakePool(subtitles_to_return=[])  # returns [] without discard
        subs = find_subtitles(
            MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
        )
        assert subs == []
        assert pool.list_calls == 1
        assert not pool.discarded_providers
```

- [ ] **Step 2: Run test — verify it fails**

```bash
cd /Users/bora/Developer/repos/personal/bazarr-topn
.venv/bin/python -m pytest tests/test_subtitle_finder.py::TestSearchUnavailable -v
```

Expected: `ImportError: cannot import name 'SearchUnavailable'`.

- [ ] **Step 3: Add exception class**

In `src/bazarr_topn/subtitle_finder.py`, add after the `logger = logging.getLogger(__name__)` line:

```python
class SearchUnavailable(Exception):
    """Raised when find_subtitles exhausts retries with a discarded provider.

    Distinct from 'search returned empty' — this means the search never
    completed successfully (rate limit, network error at all attempts).
    """
```

- [ ] **Step 4: Verify test still fails for the right reason**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py::TestSearchUnavailable::test_raises_when_retries_exhausted_with_discard -v
```

Expected: FAIL with `DID NOT RAISE` because `find_subtitles` doesn't raise yet (wired up in Task 2). The second test (`test_no_raise_when_search_returns_empty_cleanly`) should now PASS because it only imports and checks the empty path.

- [ ] **Step 5: Commit**

```bash
cd /Users/bora/Developer/repos/personal/bazarr-topn
git add src/bazarr_topn/subtitle_finder.py tests/test_subtitle_finder.py
git commit -m "$(cat <<'EOF'
feat: add SearchUnavailable exception for rate-limited searches

Distinguishes "search failed due to discard" from "search returned empty".
Raised in Task 2 from find_subtitles.
EOF
)"
```

---

### Task 2: Raise `SearchUnavailable` from `find_subtitles`

**Files:**
- Modify: `src/bazarr_topn/subtitle_finder.py:99-114` (retry loop in `find_subtitles`)
- Modify: `tests/test_subtitle_finder.py:102-110` (`test_gives_up_after_max_retries`)

- [ ] **Step 1: Update existing test that asserted `subs == []` on exhausted retries**

In `tests/test_subtitle_finder.py`, replace the body of `test_gives_up_after_max_retries` (currently around lines 102-110):

```python
    def test_gives_up_after_max_retries(self, no_delay_config: Config) -> None:
        from bazarr_topn.subtitle_finder import SearchUnavailable

        # fail_list_times=5 > retries=2, so all attempts fail
        pool = FakePool(fail_list_times=5)
        with pytest.raises(SearchUnavailable):
            find_subtitles(
                MagicMock(), Language.fromalpha2("en"), pool, config=no_delay_config
            )
        # initial + 2 retries = 3 total attempts
        assert pool.list_calls == 3
```

- [ ] **Step 2: Run test — verify it fails**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py::TestFindSubtitlesRetry::test_gives_up_after_max_retries tests/test_subtitle_finder.py::TestSearchUnavailable::test_raises_when_retries_exhausted_with_discard -v
```

Expected: both FAIL with `DID NOT RAISE`.

- [ ] **Step 3: Implement the raise**

In `src/bazarr_topn/subtitle_finder.py`, replace the body of `find_subtitles` from `raw_subs: list = []` through the end of the retry loop with:

```python
    raw_subs: list = []
    for attempt in range(retries + 1):
        before = set(pool.discarded_providers)
        raw_subs = pool.list_subtitles(video, {language})
        newly_discarded = set(pool.discarded_providers) - before
        if not newly_discarded:
            break
        if attempt >= retries:
            # Exhausted retries with provider still being discarded — the
            # search never completed. Signal so the caller writes search_ok=False.
            raise SearchUnavailable(
                f"Providers {sorted(newly_discarded)} stayed discarded after "
                f"{retries + 1} attempts for language {language}"
            )
        sleep_s = backoff * (2 ** attempt)
        logger.warning(
            "Providers %s discarded during search (likely rate-limited). "
            "Sleeping %.0fs before retry %d/%d",
            sorted(newly_discarded), sleep_s, attempt + 1, retries,
        )
        if sleep_s > 0:
            time.sleep(sleep_s)
        for p in newly_discarded:
            pool.discarded_providers.discard(p)
```

(The only substantive change is splitting the `if not newly_discarded or attempt >= retries: break` into two branches so the exhausted-retries branch can `raise`.)

- [ ] **Step 4: Run tests — verify both pass**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py::TestFindSubtitlesRetry tests/test_subtitle_finder.py::TestSearchUnavailable -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bazarr_topn/subtitle_finder.py tests/test_subtitle_finder.py
git commit -m "$(cat <<'EOF'
feat: find_subtitles raises SearchUnavailable on exhausted retries

Instead of returning [] silently when all retries fail with a discarded
provider, raise SearchUnavailable so the caller can distinguish rate-limit
from "genuinely 0 results."
EOF
)"
```

---

### Task 3: Add `search_ok` to `DownloadResult` and catch in `download_top_n`

**Files:**
- Modify: `src/bazarr_topn/subtitle_finder.py:52-55` (`DownloadResult` dataclass)
- Modify: `src/bazarr_topn/subtitle_finder.py:186-204` (`download_top_n` search call + empty path)
- Modify: `tests/test_subtitle_finder.py` (add new test class)

- [ ] **Step 1: Write failing test**

Add to `tests/test_subtitle_finder.py` at the end of the file:

```python
class TestDownloadTopNSearchOk:
    def test_search_ok_true_on_normal_path(
        self, tmp_path: Path, no_delay_config: Config
    ) -> None:
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        pool = FakePool(subtitles_to_return=[
            FakeSubtitle("opensubtitlescom", content=b"data"),
        ])
        result = download_top_n(
            MagicMock(), video_path, Language.fromalpha2("en"),
            no_delay_config, pool,
        )
        assert result.search_ok is True

    def test_search_ok_true_on_empty_candidates(
        self, tmp_path: Path, no_delay_config: Config
    ) -> None:
        """Genuine empty result — search completed fine, just nothing matched."""
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        pool = FakePool(subtitles_to_return=[])
        result = download_top_n(
            MagicMock(), video_path, Language.fromalpha2("en"),
            no_delay_config, pool,
        )
        assert result.saved_paths == []
        assert result.clean is True
        assert result.search_ok is True
        assert result.available_count == 0

    def test_search_ok_false_on_rate_limit(
        self, tmp_path: Path, no_delay_config: Config
    ) -> None:
        """SearchUnavailable from find_subtitles → search_ok=False, clean=False."""
        video_path = tmp_path / "movie.mkv"
        video_path.write_bytes(b"x")
        pool = FakePool(fail_list_times=99)  # always discards
        result = download_top_n(
            MagicMock(), video_path, Language.fromalpha2("en"),
            no_delay_config, pool,
        )
        assert result.saved_paths == []
        assert result.clean is False
        assert result.search_ok is False
        assert result.available_count == 0
```

- [ ] **Step 2: Run test — verify it fails**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py::TestDownloadTopNSearchOk -v
```

Expected: FAIL — first two with `AttributeError: 'DownloadResult' object has no attribute 'search_ok'`; third with `SearchUnavailable` propagating out of `download_top_n`.

- [ ] **Step 3: Add `search_ok` field to `DownloadResult`**

In `src/bazarr_topn/subtitle_finder.py`, update the `DownloadResult` dataclass to:

```python
@dataclass
class DownloadResult:
    saved_paths: list[Path]
    clean: bool
    available_count: int
    search_ok: bool = True
```

The `= True` default keeps existing test constructions like `DownloadResult(saved_paths=[], clean=True, available_count=0)` working.

- [ ] **Step 4: Catch `SearchUnavailable` in `download_top_n`**

In `src/bazarr_topn/subtitle_finder.py`, replace the block starting at `if config.search_delay > 0:` through `candidates = find_subtitles(...)` with:

```python
    if config.search_delay > 0:
        time.sleep(config.search_delay)
    logger.info("  Searching subtitles [%s]...", lang_str)
    try:
        candidates = find_subtitles(video, language, pool, config=config)
    except SearchUnavailable as e:
        logger.warning(
            "  Search unavailable for [%s] (%s); will retry next scan",
            lang_str, e,
        )
        return DownloadResult(
            saved_paths=[], clean=False, available_count=0, search_ok=False,
        )
```

Then update the genuinely-empty return (around line 204) to explicitly pass `search_ok=True`:

```python
    if not candidates:
        if unfiltered_count == 0:
            logger.info("  No candidates returned for [%s] from any provider", lang_str)
        else:
            logger.info("  No subtitles passed min_score=%d for [%s] (%d candidates filtered out)",
                         config.min_score, lang_str, unfiltered_count)
        return DownloadResult(
            saved_paths=[], clean=True, available_count=unfiltered_count, search_ok=True,
        )
```

And the final return of the function (after the download loop) gets `search_ok=True` as well:

```python
    return DownloadResult(
        saved_paths=saved,
        clean=clean,
        available_count=unfiltered_count,
        search_ok=True,
    )
```

- [ ] **Step 5: Run the new tests — verify they pass**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py::TestDownloadTopNSearchOk -v
```

Expected: all PASS.

- [ ] **Step 6: Run full subtitle_finder test suite to check nothing regressed**

```bash
.venv/bin/python -m pytest tests/test_subtitle_finder.py -v
```

Expected: all PASS (existing tests keep working because `search_ok` defaults to `True`).

- [ ] **Step 7: Commit**

```bash
git add src/bazarr_topn/subtitle_finder.py tests/test_subtitle_finder.py
git commit -m "$(cat <<'EOF'
feat: DownloadResult.search_ok signals rate-limit vs genuine empty

download_top_n now catches SearchUnavailable and returns search_ok=False,
clean=False. The genuinely-empty path stays clean=True, search_ok=True.
Default True preserves existing callers.
EOF
)"
```

---

### Task 4: Bump sidecar schema to v2 with `search_ok`

**Files:**
- Modify: `src/bazarr_topn/sidecar.py:17-22` (`SidecarData` dataclass)
- Modify: `src/bazarr_topn/sidecar.py:31-38` (`write_sidecar`)
- Modify: `tests/test_sidecar.py` (add v2 tests)

- [ ] **Step 1: Write failing test**

Add to `tests/test_sidecar.py` at the end of the file:

```python
class TestSchemaV2:
    def test_write_defaults_include_schema_version_and_search_ok(
        self, video: Path
    ) -> None:
        data = SidecarData(target=10, saved=5, available=8, clean=True)
        write_sidecar(video, "en", data)
        raw = json.loads(sidecar_path(video, "en").read_text())
        assert raw["schema_version"] == 2
        assert raw["search_ok"] is True

    def test_write_preserves_explicit_search_ok_false(self, video: Path) -> None:
        data = SidecarData(
            target=10, saved=0, available=0, clean=False, search_ok=False,
        )
        write_sidecar(video, "en", data)
        raw = json.loads(sidecar_path(video, "en").read_text())
        assert raw["search_ok"] is False
        assert raw["schema_version"] == 2

    def test_roundtrip_search_ok(self, video: Path) -> None:
        data = SidecarData(
            target=10, saved=0, available=0, clean=False, search_ok=False,
        )
        write_sidecar(video, "en", data)
        loaded = read_sidecar(video, "en")
        assert loaded is not None
        assert loaded.search_ok is False
        assert loaded.schema_version == 2
```

- [ ] **Step 2: Run test — verify it fails**

```bash
.venv/bin/python -m pytest tests/test_sidecar.py::TestSchemaV2 -v
```

Expected: FAIL with `TypeError: SidecarData.__init__() got an unexpected keyword argument 'search_ok'`.

- [ ] **Step 3: Add fields to `SidecarData`**

In `src/bazarr_topn/sidecar.py`, replace the `SidecarData` dataclass with:

```python
SCHEMA_VERSION = 2


@dataclass
class SidecarData:
    target: int
    saved: int
    available: int
    clean: bool
    completed_at: str | None = None
    search_ok: bool = True
    schema_version: int = SCHEMA_VERSION
```

- [ ] **Step 4: Update `write_sidecar` to stamp version on every write**

In `src/bazarr_topn/sidecar.py`, replace `write_sidecar` with:

```python
def write_sidecar(video_path: str | Path, lang: str, data: SidecarData) -> Path:
    """Write sidecar JSON. Stamps current schema_version and completed_at."""
    if data.completed_at is None:
        data.completed_at = datetime.now(timezone.utc).isoformat()
    data.schema_version = SCHEMA_VERSION
    path = sidecar_path(video_path, lang)
    path.write_text(json.dumps(asdict(data), indent=2) + "\n")
    logger.debug("Wrote sidecar %s", path.name)
    return path
```

The explicit `data.schema_version = SCHEMA_VERSION` line ensures callers who pass legacy `SidecarData` instances still produce v2 on disk.

- [ ] **Step 5: Run new tests — verify the first two pass**

```bash
.venv/bin/python -m pytest tests/test_sidecar.py::TestSchemaV2::test_write_defaults_include_schema_version_and_search_ok tests/test_sidecar.py::TestSchemaV2::test_write_preserves_explicit_search_ok_false -v
```

Expected: PASS. The third `test_roundtrip_search_ok` still FAILS (read_sidecar doesn't parse the new fields yet — handled in Task 5).

- [ ] **Step 6: Commit**

```bash
git add src/bazarr_topn/sidecar.py tests/test_sidecar.py
git commit -m "$(cat <<'EOF'
feat: sidecar schema v2 adds search_ok flag

SidecarData gains search_ok (default True) and schema_version=2. Writes
always stamp the current version so legacy sidecars self-migrate as
they get rewritten.
EOF
)"
```

---

### Task 5: Update `read_sidecar` to accept v1 legacy files

**Files:**
- Modify: `src/bazarr_topn/sidecar.py:41-61` (`read_sidecar`)
- Modify: `tests/test_sidecar.py` (add legacy read tests)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_sidecar.py` inside the existing `TestSchemaV2` class (or in a new `TestLegacyV1Read` class):

```python
class TestLegacyV1Read:
    def test_v1_sidecar_reads_with_defaults(self, video: Path) -> None:
        """Legacy file without schema_version / search_ok loads with defaults."""
        raw = {
            "target": 10,
            "saved": 0,
            "available": 0,
            "clean": True,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        sidecar_path(video, "en").write_text(json.dumps(raw))
        loaded = read_sidecar(video, "en")
        assert loaded is not None
        # Missing schema_version defaults to 1
        assert loaded.schema_version == 1
        # Missing search_ok defaults to False (legacy wrote clean=True on any result)
        assert loaded.search_ok is False

    def test_v2_sidecar_reads_cleanly(self, video: Path) -> None:
        data = SidecarData(
            target=10, saved=10, available=15, clean=True, search_ok=True,
        )
        write_sidecar(video, "en", data)
        loaded = read_sidecar(video, "en")
        assert loaded is not None
        assert loaded.schema_version == 2
        assert loaded.search_ok is True
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
.venv/bin/python -m pytest tests/test_sidecar.py::TestLegacyV1Read tests/test_sidecar.py::TestSchemaV2::test_roundtrip_search_ok -v
```

Expected: FAIL — current `read_sidecar` returns `None` for any missing field including the new ones.

- [ ] **Step 3: Update `read_sidecar`**

In `src/bazarr_topn/sidecar.py`, replace `read_sidecar` with:

```python
def read_sidecar(video_path: str | Path, lang: str) -> SidecarData | None:
    """Read sidecar JSON. Returns None if missing, corrupt, or missing v1 fields.

    Tolerates missing v2-only fields (schema_version, search_ok): legacy v1
    sidecars load with schema_version=1 and search_ok=False, which is_topn_done
    will reject so they get rewritten on the next scan.
    """
    path = sidecar_path(video_path, lang)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        for key in ("target", "saved", "available", "clean", "completed_at"):
            if key not in raw:
                logger.debug("Sidecar %s missing field '%s', treating as absent", path.name, key)
                return None
        return SidecarData(
            target=raw["target"],
            saved=raw["saved"],
            available=raw["available"],
            clean=raw["clean"],
            completed_at=raw["completed_at"],
            search_ok=raw.get("search_ok", False),
            schema_version=raw.get("schema_version", 1),
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.debug("Corrupt sidecar %s, treating as absent", path.name)
        return None
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
.venv/bin/python -m pytest tests/test_sidecar.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bazarr_topn/sidecar.py tests/test_sidecar.py
git commit -m "$(cat <<'EOF'
feat: read_sidecar tolerates v1 files with defaults

Legacy sidecars without schema_version/search_ok load as v1/search_ok=False.
is_topn_done will reject them in the next task.
EOF
)"
```

---

### Task 6: `is_topn_done` rejects v1 and `search_ok=False`

**Files:**
- Modify: `src/bazarr_topn/sidecar.py:72-109` (`is_topn_done`)
- Modify: `tests/test_sidecar.py:97-102` (`_write` helper) + `test_available_zero_saved_zero_clean_is_done`

- [ ] **Step 1: Update the `_write` helper and fix the pre-existing test**

In `tests/test_sidecar.py`, update `TestIsTopnDone._write` to write v2 sidecars by default:

```python
    def _write(self, video: Path, lang: str, **overrides) -> None:
        defaults = dict(
            target=10, saved=10, available=15, clean=True,
            completed_at=datetime.now(timezone.utc).isoformat(),
            search_ok=True, schema_version=2,
        )
        defaults.update(overrides)
        p = sidecar_path(video, lang)
        p.write_text(json.dumps(defaults))
```

Replace the test body for `test_available_zero_saved_zero_clean_is_done`:

```python
    def test_available_zero_saved_zero_clean_search_ok_is_done(
        self, video: Path, cfg: Config
    ) -> None:
        """v2 sidecar: genuinely no Turkish subs exist — is_topn_done returns True."""
        self._write(
            video, "en",
            target=10, saved=0, available=0, clean=True, search_ok=True,
        )
        assert is_topn_done(video, "en", cfg) is True
```

- [ ] **Step 2: Add new tests for v1 rejection and search_ok=False**

Add to `TestIsTopnDone`:

```python
    def test_v1_legacy_sidecar_not_done(self, video: Path, cfg: Config) -> None:
        """Legacy v1 sidecar (no schema_version, no search_ok) is never done."""
        raw = dict(
            target=10, saved=0, available=0, clean=True,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        sidecar_path(video, "en").write_text(json.dumps(raw))
        assert is_topn_done(video, "en", cfg) is False

    def test_v2_search_ok_false_not_done(self, video: Path, cfg: Config) -> None:
        """Rate-limited sidecar is retried next scan."""
        self._write(
            video, "en",
            target=10, saved=0, available=0, clean=False, search_ok=False,
        )
        assert is_topn_done(video, "en", cfg) is False

    def test_v2_schema_version_1_explicitly_not_done(
        self, video: Path, cfg: Config
    ) -> None:
        """Explicit schema_version=1 (written by old code) is rejected."""
        raw = dict(
            target=10, saved=10, available=15, clean=True,
            completed_at=datetime.now(timezone.utc).isoformat(),
            schema_version=1, search_ok=True,
        )
        sidecar_path(video, "en").write_text(json.dumps(raw))
        assert is_topn_done(video, "en", cfg) is False
```

- [ ] **Step 3: Run tests — verify they fail**

```bash
.venv/bin/python -m pytest tests/test_sidecar.py::TestIsTopnDone -v
```

Expected: the three new tests FAIL (current `is_topn_done` doesn't check version or search_ok).

- [ ] **Step 4: Update `is_topn_done`**

In `src/bazarr_topn/sidecar.py`, replace `is_topn_done` with:

```python
def is_topn_done(video_path: str | Path, lang: str, config: Config) -> bool:
    """Check if a video+language pair has a valid, complete sidecar.

    Returns True only if:
    1. Sidecar feature is enabled
    2. Sidecar exists and is parseable
    3. schema_version >= 2 (v1 legacy always rejected)
    4. search_ok is True (search actually completed)
    5. clean is True (all attempted downloads succeeded)
    6. saved >= min(target, available)
    7. target >= config.top_n (user hasn't raised the target)
    8. completed_at within config.topn_recheck_days
    """
    if not config.topn_sidecar_enabled:
        return False

    data = read_sidecar(video_path, lang)
    if data is None:
        return False

    if data.schema_version < SCHEMA_VERSION:
        return False

    if not data.search_ok:
        return False

    if not data.clean:
        return False

    if data.saved < min(data.target, data.available):
        return False

    if data.target < config.top_n:
        return False

    try:
        completed = datetime.fromisoformat(data.completed_at)
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - completed
        if age > timedelta(days=config.topn_recheck_days):
            return False
    except (ValueError, TypeError):
        return False

    return True
```

- [ ] **Step 5: Run all sidecar tests — verify pass**

```bash
.venv/bin/python -m pytest tests/test_sidecar.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bazarr_topn/sidecar.py tests/test_sidecar.py
git commit -m "$(cat <<'EOF'
feat: is_topn_done rejects v1 legacy and search_ok=False

Legacy sidecars (schema_version<2) and v2 sidecars with search_ok=False
both return False, forcing re-scan on the next run. Existing 394 stuck
Turkish sidecars on production will be rewritten on the next cron scan.
EOF
)"
```

---

### Task 7: Scanner clears discards per video and persists `search_ok`

**Files:**
- Modify: `src/bazarr_topn/scanner.py:43-108` (`process_video`)
- Modify: `tests/test_scanner.py` (add pool-clear + search_ok sidecar tests)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_scanner.py` at the end of the file:

```python
class TestProcessVideoPoolClear:
    def test_clears_pool_discards_before_processing(self, tmp_video: Path) -> None:
        """process_video starts with an empty discard set each call."""
        config = _make_config()
        pool = MagicMock()
        pool.discarded_providers = {"opensubtitlescom"}  # left over from previous video
        fake_result = DownloadResult(
            saved_paths=[], clean=True, available_count=0, search_ok=True,
        )
        with patch("bazarr_topn.scanner.scan_video") as mock_scan, \
             patch("bazarr_topn.scanner.download_top_n", return_value=fake_result):
            mock_scan.return_value = MagicMock()
            process_video(tmp_video, config, pool)
        # The pool's discard set should have been cleared at process_video entry
        # (download_top_n returns success so it doesn't re-populate)
        assert pool.discarded_providers == set()


class TestProcessVideoSearchOkSidecar:
    def test_writes_search_ok_true_on_clean_result(self, tmp_video: Path) -> None:
        config = _make_config()
        pool = MagicMock()
        pool.discarded_providers = set()
        fake_result = DownloadResult(
            saved_paths=[], clean=True, available_count=0, search_ok=True,
        )
        with patch("bazarr_topn.scanner.scan_video") as mock_scan, \
             patch("bazarr_topn.scanner.download_top_n", return_value=fake_result):
            mock_scan.return_value = MagicMock()
            process_video(tmp_video, config, pool)
        sc = sidecar_path(tmp_video, "en")
        data = json.loads(sc.read_text())
        assert data["search_ok"] is True
        assert data["schema_version"] == 2

    def test_writes_search_ok_false_on_rate_limit_result(self, tmp_video: Path) -> None:
        config = _make_config()
        pool = MagicMock()
        pool.discarded_providers = set()
        fake_result = DownloadResult(
            saved_paths=[], clean=False, available_count=0, search_ok=False,
        )
        with patch("bazarr_topn.scanner.scan_video") as mock_scan, \
             patch("bazarr_topn.scanner.download_top_n", return_value=fake_result):
            mock_scan.return_value = MagicMock()
            process_video(tmp_video, config, pool)
        sc = sidecar_path(tmp_video, "en")
        data = json.loads(sc.read_text())
        assert data["search_ok"] is False
        assert data["clean"] is False
        assert data["schema_version"] == 2

    def test_legacy_v1_sidecar_gets_rewritten_as_v2(self, tmp_video: Path) -> None:
        """The smoking-gun integration: the 394 stuck Turkish sidecars."""
        # Hand-craft a legacy v1 sidecar on disk
        legacy = dict(
            target=10, saved=0, available=0, clean=True,
            completed_at="2026-04-16T04:21:16.720980+00:00",
        )
        sc = sidecar_path(tmp_video, "en")
        sc.write_text(json.dumps(legacy))

        config = _make_config()
        pool = MagicMock()
        pool.discarded_providers = set()
        # Simulate a successful re-scan that finds nothing (niche content)
        fake_result = DownloadResult(
            saved_paths=[], clean=True, available_count=0, search_ok=True,
        )
        with patch("bazarr_topn.scanner.scan_video") as mock_scan, \
             patch("bazarr_topn.scanner.download_top_n", return_value=fake_result):
            mock_scan.return_value = MagicMock()
            result = process_video(tmp_video, config, pool)

        assert result != -1  # not skipped — v1 triggered reprocessing
        data = json.loads(sc.read_text())
        assert data["schema_version"] == 2
        assert data["search_ok"] is True
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
.venv/bin/python -m pytest tests/test_scanner.py::TestProcessVideoPoolClear tests/test_scanner.py::TestProcessVideoSearchOkSidecar -v
```

Expected: FAIL — scanner doesn't clear discards yet, and the `SidecarData` constructed by the scanner doesn't pass `search_ok`.

- [ ] **Step 3: Update `process_video`**

In `src/bazarr_topn/scanner.py`, inside `process_video` (around line 43), make two changes.

First, at the very top of the function body (before the `if not force:` check), add the discard clear:

```python
    # Reset the provider pool's discard state so a rate-limited prior video
    # does not poison this one. Within-video retry is still handled by
    # find_subtitles' newly_discarded tracking.
    pool.discarded_providers.clear()
```

Second, replace the `SidecarData` construction (around line 102) with:

```python
        # Write sidecar regardless of outcome
        if config.topn_sidecar_enabled:
            sidecar_data = SidecarData(
                target=config.top_n,
                saved=len(result.saved_paths),
                available=result.available_count,
                clean=result.clean,
                search_ok=result.search_ok,
            )
            write_sidecar(video_path, lang_code, sidecar_data)
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
.venv/bin/python -m pytest tests/test_scanner.py -v
```

Expected: all PASS, including pre-existing scanner tests.

- [ ] **Step 5: Commit**

```bash
git add src/bazarr_topn/scanner.py tests/test_scanner.py
git commit -m "$(cat <<'EOF'
feat: scanner clears pool discards per video, persists search_ok

process_video now clears pool.discarded_providers at entry so one rate-
limited video does not silently poison the rest of the scan. Sidecar
writes carry result.search_ok through to disk, completing the v2
self-heal path for legacy stuck sidecars.
EOF
)"
```

---

### Task 8: Full-suite verification and smoke-check against the stuck production pattern

**Files:**
- No code changes; this is a verification gate.

- [ ] **Step 1: Run the entire test suite**

```bash
cd /Users/bora/Developer/repos/personal/bazarr-topn
.venv/bin/python -m pytest -v
```

Expected: every test passes. If anything fails, fix it before proceeding — do not mark this task complete.

- [ ] **Step 2: Hand-run the legacy-resurrection simulation**

In a Python REPL inside the repo:

```bash
.venv/bin/python - <<'PY'
import json, tempfile
from pathlib import Path
from bazarr_topn.config import Config
from bazarr_topn.sidecar import is_topn_done, sidecar_path

with tempfile.TemporaryDirectory() as d:
    video = Path(d) / "Frasier - S02E03.mkv"
    video.write_bytes(b"x")

    # The exact shape of the 394 stuck production sidecars
    legacy = {
        "target": 10, "saved": 0, "available": 0, "clean": True,
        "completed_at": "2026-04-16T05:14:43.501667+00:00",
    }
    sidecar_path(video, "tr").write_text(json.dumps(legacy))

    cfg = Config(languages=["tr"], top_n=10, topn_recheck_days=30, topn_sidecar_enabled=True)
    done = is_topn_done(video, "tr", cfg)
    print(f"legacy v1 is_topn_done: {done}  (expect False)")
    assert done is False, "BUG: legacy sidecar still treated as done"
    print("OK — the 394 stuck sidecars will self-heal on next scan")
PY
```

Expected output:
```
legacy v1 is_topn_done: False  (expect False)
OK — the 394 stuck sidecars will self-heal on next scan
```

- [ ] **Step 3: Confirm no dormant references to the old behavior**

```bash
cd /Users/bora/Developer/repos/personal/bazarr-topn
grep -rn "clean=True, available_count=0" src/ tests/ || echo "no stale callers"
grep -rn "schema_version" src/ tests/
```

Expected: the first grep returns nothing (or only on deliberate test construction like our new ones); the second grep shows references in `sidecar.py` and `test_sidecar.py` only.

- [ ] **Step 4: No commit here — verification task only**

The prior task commits are the deployable history.

---

## Self-Review Checklist (ran at plan-write time)

- **Spec coverage:** Sections 1-6 of the spec all map to tasks (1→T1-2, 2→T1, 3→T3, 4→T7, 5→T4-6, 6→T8 simulation). Testing section lines up with the test additions in T1, T3, T4, T5, T6, T7.
- **Type consistency:** `search_ok: bool` appears in `DownloadResult`, `SidecarData`, and the scanner's persistence. `schema_version` consistently typed as `int`. `SCHEMA_VERSION = 2` is the single source of truth.
- **No placeholders:** every step ships runnable code or an exact command.
- **Frequent commits:** seven commits across seven tasks, each independently verifiable.

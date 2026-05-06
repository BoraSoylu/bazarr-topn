"""Repair bazarr-topn sidecars by reconciling with actual .srt files on disk.

A prior version of bazarr-topn (before commit 72d8dd4) had a 3-day recheck
cycle that re-processed already-done videos. When OS.com quota crashed
mid-recheck, sidecars got rewritten with saved=0 / clean=False even though
the .srt files were still intact on disk. This script walks the disk, counts
actual .{lang}.topn-NN.srt files per video, and where the sidecar
undercounts disk reality, rewrites it to match
(saved=actual, clean=True, search_ok=True).

Usage (run with the project venv so paths resolve):
    python scripts/repair_sidecars.py --media-root /mnt/media --lang tr
    python scripts/repair_sidecars.py --media-root /mnt/media --lang tr --apply

Defaults to dry-run unless --apply is passed.
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def repair(media_root: Path, lang: str, apply: bool) -> None:
    srt_pat = re.compile(rf"\.{re.escape(lang)}\.topn-\d+\.srt$")
    sc_suffix = f".{lang}.topn.json"

    srt_counts: Counter[str] = Counter()
    for srt in media_root.rglob(f"*.{lang}.topn-*.srt"):
        stem = srt_pat.sub("", str(srt))
        srt_counts[stem] += 1

    sidecars = list(media_root.rglob(f"*{sc_suffix}"))
    fixed = consistent = overstate = no_srts = errors = 0

    for sc_path in sidecars:
        stem = str(sc_path)[: -len(sc_suffix)]
        actual = srt_counts.get(stem, 0)

        try:
            data = json.loads(sc_path.read_text())
        except Exception as e:
            errors += 1
            print(f"  ERR reading {sc_path}: {e}", file=sys.stderr)
            continue

        sv = data.get("saved", 0)
        cl = data.get("clean", False)
        if sv >= actual:
            if sv > actual:
                overstate += 1
                print(f"  WARN overstate {sv}>{actual}: {Path(stem).name[:80]}", file=sys.stderr)
            else:
                consistent += 1
            continue
        if actual == 0:
            no_srts += 1
            continue

        new = {
            "target": data.get("target", 10),
            "saved": actual,
            "available": max(data.get("available", 0), actual),
            "clean": True,
            "completed_at": data.get("completed_at") or datetime.now(timezone.utc).isoformat(),
            "search_ok": True,
            "schema_version": 2,
        }
        action = "WRITE" if apply else "DRYRUN"
        print(f"  {action} saved {sv}->{actual} clean {cl}->True : {Path(stem).name[:80]}")
        if apply:
            sc_path.write_text(json.dumps(new, indent=2) + "\n")
        fixed += 1

    print()
    print(f"Total sidecars examined: {len(sidecars)}")
    print(f"  Repaired:           {fixed}")
    print(f"  Already consistent: {consistent}")
    print(f"  Overstating (kept): {overstate}")
    print(f"  No srts on disk:    {no_srts}")
    print(f"  Read errors:        {errors}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--media-root", default="/mnt/media")
    p.add_argument("--lang", default="tr")
    p.add_argument("--apply", action="store_true", help="Actually write (otherwise dry run)")
    args = p.parse_args()
    print(f"lang={args.lang}  root={args.media_root}  apply={args.apply}")
    print()
    repair(Path(args.media_root), args.lang, args.apply)


if __name__ == "__main__":
    main()

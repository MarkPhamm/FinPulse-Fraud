#!/usr/bin/env python3
"""
Generate small, plain-text previews of data/*.gz so the schema and a few
sample rows are browseable without running gunzip every time.

Output goes to data/preview/, one file per input:
  *.csv.gz   -> first 100 rows + header        (transactions.csv, etc.)
  *.json.gz  -> first 50 records, pretty array (customer-profiles.json, etc.)

Run from repo root:  python3 src/preview_data.py
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

PREVIEW_CSV_ROWS = 100
PREVIEW_JSON_RECORDS = 50

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
OUT_DIR = DATA_DIR / "preview"


def preview_csv(src: Path, dst: Path, n_rows: int) -> int:
    with gzip.open(src, "rt", encoding="utf-8") as fh, dst.open("w", encoding="utf-8") as out:
        header = next(fh)
        out.write(header)
        rows_written = 0
        for line in fh:
            if rows_written >= n_rows:
                break
            out.write(line)
            rows_written += 1
        return rows_written


def preview_json(src: Path, dst: Path, n_records: int) -> int:
    with gzip.open(src, "rt", encoding="utf-8") as fh:
        records = json.load(fh)
    if not isinstance(records, list):
        raise ValueError(f"{src.name}: expected top-level JSON array, got {type(records).__name__}")
    sample = records[:n_records]
    with dst.open("w", encoding="utf-8") as out:
        json.dump(sample, out, indent=2)
        out.write("\n")
    return len(sample)


def main() -> int:
    if not DATA_DIR.is_dir():
        print(f"data dir not found: {DATA_DIR}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(exist_ok=True)

    sources = sorted(DATA_DIR.glob("*.gz"))
    if not sources:
        print(f"no *.gz files under {DATA_DIR}", file=sys.stderr)
        return 1

    print(f"Writing previews to {OUT_DIR.relative_to(REPO_ROOT)}/")
    for src in sources:
        dst = OUT_DIR / src.with_suffix("").name  # foo.csv.gz -> foo.csv
        if src.name.endswith(".csv.gz"):
            n = preview_csv(src, dst, PREVIEW_CSV_ROWS)
            print(f"  {src.name:32s} -> {dst.name} ({n} rows)")
        elif src.name.endswith(".json.gz"):
            n = preview_json(src, dst, PREVIEW_JSON_RECORDS)
            print(f"  {src.name:32s} -> {dst.name} ({n} records)")
        else:
            print(f"  {src.name:32s} -> skipped (unknown format)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

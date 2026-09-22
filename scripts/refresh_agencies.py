#!/usr/bin/env python3
"""Refresh the licensed-agency list from a public BMET source.

Run:
    python scripts/refresh_agencies.py --source <url-or-path> [--source-date YYYY-MM-DD] [--dry-run]

This script is intentionally loud about failure. The plan lists "does an official
download or API exist, and what are its terms of use" as a `[verify]` item, so when
the input format is not one we recognise the script exits with code 2 and prints what
it saw, instead of writing a half-parsed file that would make the licence checker
give wrong "found / not found" answers.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "backend" / "data" / "agencies.csv"

RL_RE = re.compile(r"\bR\.?L\.?\s*[-/]?\s*(\d{2,6})\b", re.IGNORECASE)
ROW_RE = re.compile(
    r"(?P<rl>R\.?L\.?\s*[-/]?\s*\d{2,6})\s*(?P<rest>[^\n]{3,120})", re.IGNORECASE
)
HEADER = ["rl_number", "name", "status", "address", "valid_until", "source_date", "data_status"]


def fetch(source: str) -> bytes:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "fraud-shield-refresh/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.read()
    return Path(source).read_bytes()


def rows_from_csv(data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fields = {field.strip().lower() for field in (reader.fieldnames or [])}
    if not {"rl_number", "name"} <= fields:
        return []
    rows = []
    for row in reader:
        clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        rows.append(
            {
                "rl_number": clean.get("rl_number", ""),
                "name": clean.get("name", ""),
                "status": clean.get("status", "unknown") or "unknown",
                "address": clean.get("address", ""),
                "valid_until": clean.get("valid_until", ""),
            }
        )
    return [row for row in rows if row["rl_number"] and row["name"]]


def rows_from_pdf(data: bytes) -> list[dict]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return []
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        print(f"pdf parse failed: {type(exc).__name__}", file=sys.stderr)
        return []

    rows: list[dict] = []
    seen: set[str] = set()
    for page in reader.pages:
        text = page.extract_text() or ""
        for match in ROW_RE.finditer(text):
            rl = normalise_rl(match.group("rl"))
            if not rl or rl in seen:
                continue
            name = re.split(r"\s{2,}|\n", match.group("rest").strip())[0].strip(" ,-")
            if len(name) < 3:
                continue
            seen.add(rl)
            rows.append(
                {"rl_number": f"RL-{rl}", "name": name, "status": "Active", "address": "", "valid_until": ""}
            )
    return rows


def rows_from_html(data: bytes) -> list[dict]:
    text = data.decode("utf-8", errors="replace")
    rows: list[dict] = []
    seen: set[str] = set()
    for match in ROW_RE.finditer(re.sub(r"<[^>]+>", " ", text)):
        rl = normalise_rl(match.group("rl"))
        if not rl or rl in seen:
            continue
        seen.add(rl)
        rows.append(
            {
                "rl_number": f"RL-{rl}",
                "name": match.group("rest").strip(" ,-")[:120],
                "status": "Active",
                "address": "",
                "valid_until": "",
            }
        )
    return rows


def normalise_rl(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return digits.lstrip("0")


def pick_parser(data: bytes, source: str, declared: str | None):
    if declared == "csv" or source.lower().endswith(".csv"):
        return "csv", rows_from_csv(data)
    if data.startswith(b"%PDF") or source.lower().endswith(".pdf"):
        return "pdf", rows_from_pdf(data)
    if data.lstrip()[:1] in (b"<",) or source.lower().endswith((".html", ".htm")):
        return "html", rows_from_html(data)
    return "unknown", []


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the licensed agency list")
    parser.add_argument("--source", required=True, help="URL or local path of the BMET list")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--source-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-status", default="OFFICIAL", choices=["OFFICIAL", "SYNTHETIC"])
    parser.add_argument("--format", default=None, choices=["csv", "pdf", "html"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = fetch(args.source)
    kind, rows = pick_parser(data, args.source, args.format)
    print(f"source={args.source} bytes={len(data)} detected={kind} rows={len(rows)}")

    if not rows:
        print(
            "ERROR: no rows parsed.\n"
            "Do NOT write this to agencies.csv - an empty or partial list would make the "
            "licence checker answer 'not found' for real agencies.\n"
            "Check the source format by hand, then extend the matching parser.",
            file=sys.stderr,
        )
        return 2

    out = Path(args.out)
    existing_rows = 0
    if out.exists():
        with out.open(newline="", encoding="utf-8") as handle:
            existing_rows = sum(1 for _ in csv.DictReader(handle))
    if existing_rows and len(rows) < existing_rows * 0.5:
        print(
            f"ERROR: parsed {len(rows)} rows but the current file has {existing_rows}. "
            "A drop this large usually means a parsing failure. Refusing to overwrite.",
            file=sys.stderr,
        )
        return 2

    rl_numbers = [row["rl_number"] for row in rows]
    duplicates = {rl for rl in rl_numbers if rl_numbers.count(rl) > 1}
    if duplicates:
        print(f"WARNING: duplicate RL numbers in source: {sorted(duplicates)[:10]}")

    if args.dry_run:
        print("dry run: nothing written. First 3 parsed rows:")
        for row in rows[:3]:
            print(" ", row)
        return 0

    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{key: row.get(key, "") for key in HEADER},
                    "source_date": args.source_date,
                    "data_status": args.data_status,
                }
            )
    print(f"wrote {len(rows)} rows to {out} (source_date={args.source_date}, status={args.data_status})")
    print("Next: set AGENCY_DATA_IS_OFFICIAL=true only after a human has spot-checked the file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

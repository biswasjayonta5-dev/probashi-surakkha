"""Licence checker: exact RL lookup first, then fuzzy name matching.

Design rule (from the plan): this module is a *database lookup*, not AI. It never
guesses and never returns a verdict of "safe" - only "found in the list dated X"
or "not found in the list dated X".

The agency list is a local CSV so that the checker keeps working when the LLM or
the network is down. While the CSV is synthetic demo data the caller must show a
warning banner; ``is_official_data`` is carried on every response.
"""

from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

from ..config import settings
from ..schemas import AgencyRecord, LicenceMatch, LicenceResult

# Fuzzy thresholds. Below AMBIGUOUS_GAP the two best hits are treated as equally
# plausible, and the tool asks the user to disambiguate instead of picking one.
NAME_MATCH_THRESHOLD = 82.0
NAME_REVIEW_THRESHOLD = 68.0
AMBIGUOUS_GAP = 4.0
MAX_MATCHES = 5

# Only genuinely meaningless tokens are dropped. Words like "overseas",
# "international" and "employment" look generic but are the only thing separating
# many similarly named agencies, so removing them made truncated queries
# ("Turag Overs") unmatchable.
_LEGAL_NOISE = re.compile(
    r"\b(m\.?s\.?|messrs|limited|ltd|pvt|private|co|company|corp|corporation|and|&)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\u0980-\u09FF\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


@dataclass
class AgencyTable:
    records: list[AgencyRecord] = field(default_factory=list)
    data_date: str | None = None
    is_official: bool = False
    source: str = "BMET licensed recruitment agency list"
    by_rl: dict[str, AgencyRecord] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)
    search_names: list[str] = field(default_factory=list)


_MESSRS = re.compile(r"\bm\s*[/.]?\s*s\.?\b", re.IGNORECASE)


def normalise_name(raw: str) -> str:
    """Lowercase, drop punctuation and legal boilerplate, squeeze whitespace."""
    if not raw:
        return ""
    # "M/S." survives punctuation stripping as "m s", so collapse it first.
    text = _MESSRS.sub(" ", raw.lower())
    text = _PUNCT.sub(" ", text)
    text = _LEGAL_NOISE.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def normalise_rl(raw: str) -> str:
    """RL numbers arrive as 'RL-1234', 'rl 1234', 'RL/1234'. Keep digits only."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    return digits.lstrip("0") or "0"


def fuzzy_score(query: str, candidate: str, score_cutoff: float | None = None) -> float:
    """Blend of edit-distance ratio and token coverage.

    ``token_set_ratio`` alone was actively harmful here: "Padma-Kushiyara Employm"
    scored 100 against "Kushiyara Overseas" because the query's tokens are a
    superset. Coverage punishes names that explain less of what the worker typed,
    and accepts truncated tokens ("overs" -> "overseas") at a discount.
    """
    ratio = fuzz.WRatio(query, candidate)
    query_tokens = [t for t in query.split() if t]
    candidate_tokens = [t for t in candidate.split() if t]
    if not query_tokens or not candidate_tokens:
        return ratio

    covered = 0.0
    for token in query_tokens:
        best = max((fuzz.ratio(token, other) / 100.0 for other in candidate_tokens), default=0.0)
        if best >= 0.8:
            covered += 1.0
        elif best >= 0.6:
            covered += 0.6
    coverage = covered / len(query_tokens)
    score = 0.55 * ratio + 45.0 * coverage
    if score_cutoff is not None and score < score_cutoff:
        return 0.0
    return score


def looks_like_rl(query: str) -> bool:
    return bool(re.search(r"\brl\b", query, re.IGNORECASE)) or bool(
        re.fullmatch(r"\s*\d{3,6}\s*", query)
    )


def _read_rows(path: Path) -> tuple[list[dict[str, str]], str | None, bool]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = [row for row in reader]
        meta = reader.fieldnames or []
    data_date = None
    is_official = False
    for row in rows:
        if row.get("source_date"):
            data_date = max(data_date or "", row["source_date"])
    # The demo marker is a column value, not a filename, so it survives a rename.
    is_official = bool(rows) and all(
        (row.get("data_status", "").strip().upper() != "SYNTHETIC") for row in rows
    )
    return rows, data_date, is_official


@lru_cache(maxsize=1)
def _cached_table(path_str: str, mtime: float) -> AgencyTable:
    path = Path(path_str)
    if not path.exists():
        sys.stderr.write(f"agency list not found: {path}\n")
        return AgencyTable()

    rows, data_date, is_official = _read_rows(path)
    table = AgencyTable(data_date=data_date, is_official=is_official)
    for row in rows:
        record = AgencyRecord(
            rl_number=(row.get("rl_number") or "").strip(),
            name=(row.get("name") or "").strip(),
            status=(row.get("status") or "unknown").strip(),
            address=(row.get("address") or "").strip() or None,
            valid_until=(row.get("valid_until") or "").strip() or None,
            source_date=(row.get("source_date") or "").strip() or None,
            demo_data=(row.get("data_status", "").strip().upper() == "SYNTHETIC"),
        )
        key = normalise_rl(record.rl_number)
        if key in table.by_rl:
            # Duplicate RL numbers are a data-quality error, not something to hide.
            sys.stderr.write(f"duplicate RL number in agency list: {record.rl_number}\n")
        table.by_rl[key] = record
        table.records.append(record)
        table.names.append(record.name)
        table.search_names.append(normalise_name(record.name))
    return table


def load_table() -> AgencyTable:
    path = settings.agencies_csv
    mtime = path.stat().st_mtime if path.exists() else 0.0
    return _cached_table(str(path), mtime)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def lookup(query: str) -> LicenceResult:
    table = load_table()
    query = (query or "").strip()
    is_official = settings.agency_data_is_official and table.is_official

    if not query:
        return LicenceResult(
            query=query,
            matched=False,
            status="invalid_query",
            message_bn="এজেন্সির নাম বা আরএল (RL) নম্বর লিখুন।",
            data_date=table.data_date,
            is_official_data=is_official,
        )

    notes = _notes(table, query)

    if not table.records:
        return LicenceResult(
            query=query,
            matched=False,
            status="not_found",
            message_bn=(
                "এজেন্সির তালিকা এখনো লোড করা যায়নি, তাই যাচাই করা সম্ভব হয়নি। "
                "এটি 'পাওয়া যায়নি' নয় — বিএমইটি অফিসে যাচাই করুন।"
            ),
            data_date=None,
            is_official_data=is_official,
            notes_bn=notes,
        )

    if looks_like_rl(query):
        found = table.by_rl.get(normalise_rl(query))
        if found:
            return LicenceResult(
                query=query,
                matched=True,
                status="found",
                message_bn=_found_message(table, found, "আরএল নম্বর"),
                matches=[LicenceMatch(record=found, score=100.0, match_type="exact_rl")],
                data_date=table.data_date,
                is_official_data=is_official,
                notes_bn=notes,
            )
        # An RL-looking query that misses is a hard "not found": do not fuzzy-match
        # it against names, or a typo would silently return a different agency.
        return LicenceResult(
            query=query,
            matched=False,
            status="not_found",
            message_bn=(
                f"আরএল নম্বর “{query}” এই তালিকায় পাওয়া যায়নি "
                f"(তালিকার তারিখ: {table.data_date or 'অজানা'})। "
                "অর্থ এই নয় যে এজেন্সিটি অবৈধ — নম্বরটি ভুল হতে পারে। "
                "টাকা দেওয়ার আগে বিএমইটি অফিসে যাচাই করুন।"
            ),
            data_date=table.data_date,
            is_official_data=is_official,
            notes_bn=notes,
        )

    normalised = normalise_name(query)
    if not normalised:
        return LicenceResult(
            query=query,
            matched=False,
            status="invalid_query",
            message_bn="নামটি বুঝতে পারা যায়নি। আবার লিখুন বা আরএল নম্বর দিন।",
            data_date=table.data_date,
            is_official_data=is_official,
        )

    if normalised in table.search_names:
        index = table.search_names.index(normalised)
        return LicenceResult(
            query=query,
            matched=True,
            status="found",
            message_bn=_found_message(table, table.records[index], "নাম"),
            matches=[
                LicenceMatch(
                    record=table.records[index], score=100.0, match_type="exact_name"
                )
            ],
            data_date=table.data_date,
            is_official_data=is_official,
            notes_bn=notes,
        )

    results = process.extract(
        normalised, table.search_names, scorer=fuzzy_score, limit=MAX_MATCHES
    )
    good = [(name, score, idx) for name, score, idx in results if score >= NAME_REVIEW_THRESHOLD]
    if not good:
        return LicenceResult(
            query=query,
            matched=False,
            status="not_found",
            message_bn=(
                f"“{query}” নামে কোনো এজেন্সি এই তালিকায় পাওয়া যায়নি "
                f"(তালিকার তারিখ: {table.data_date or 'অজানা'})। "
                "নামের বানান ভিন্ন হতে পারে, তাই বিএমইটি অফিসে যাচাই করুন। "
                "টাকা দেওয়ার আগে অবশ্যই যাচাই করুন।"
            ),
            data_date=table.data_date,
            is_official_data=is_official,
            notes_bn=notes,
        )

    best_name, best_score, best_idx = good[0]
    matches = [
        LicenceMatch(
            record=table.records[idx],
            score=round(float(score), 1),
            match_type="fuzzy_name",
        )
        for _, score, idx in good
        if score >= NAME_MATCH_THRESHOLD
    ]
    runner_up = good[1][1] if len(good) > 1 else 0.0

    if best_score < NAME_MATCH_THRESHOLD or (
        runner_up and (best_score - runner_up) < AMBIGUOUS_GAP and len(matches) > 1
    ):
        return LicenceResult(
            query=query,
            matched=False,
            status="ambiguous",
            message_bn=(
                f"“{query}” নামের সাথে মিলে যেতে পারে এমন একাধিক এজেন্সি পাওয়া গেছে। "
                "নিচের তালিকা থেকে সঠিকটি বেছে নিন, অথবা আরএল (RL) নম্বর দিয়ে খুঁজুন। "
                "নিশ্চিত না হলে টাকা দেবেন না।"
            ),
            matches=matches or [
                LicenceMatch(
                    record=table.records[idx],
                    score=round(float(score), 1),
                    match_type="fuzzy_name",
                )
                for _, score, idx in good[:3]
            ],
            data_date=table.data_date,
            is_official_data=is_official,
            notes_bn=notes,
        )

    return LicenceResult(
        query=query,
        matched=True,
        status="found",
        message_bn=_found_message(table, table.records[best_idx], "নাম (কাছাকাছি মিল)"),
        matches=matches,
        data_date=table.data_date,
        is_official_data=is_official,
        notes_bn=notes,
    )


def _found_message(table: AgencyTable, record: AgencyRecord, how: str) -> str:
    parts = [
        f"{how} অনুযায়ী “{record.name}” (আরএল: {record.rl_number}) তালিকায় পাওয়া গেছে।",
        f"তালিকার অবস্থা: {record.status}।",
    ]
    if record.valid_until:
        parts.append(f"লাইসেন্সের মেয়াদ: {record.valid_until}।")
    parts.append(f"তথ্যের তারিখ: {record.source_date or table.data_date or 'অজানা'}।")
    parts.append(
        "মনে রাখুন: তালিকায় থাকা মানে এই নয় যে সব কিছু ঠিক আছে বা এজেন্সিটি 'নিরাপদ'। "
        "ফি, ভিসার ধরন ও চুক্তি আলাদাভাবে যাচাই করুন এবং টাকা দেওয়ার আগে বিএমইটি অফিসে যান।"
    )
    if record.demo_data:
        parts.append("এই রেকর্ডটি ডেমো (নমুনা) তথ্য — প্রকৃত যাচাইয়ের জন্য ব্যবহার করবেন না।")
    return " ".join(parts)


def _notes(table: AgencyTable, query: str) -> list[str]:
    notes = [
        "এই তালিকা শুধু অনুমোদিত এজেন্সির তালিকা; এতে না থাকা মানেই প্রতারণা নয়।",
        "সাব-এজেন্ট (দালাল) তালিকায় থাকে না — তাদের মাধ্যমে টাকা দেওয়া ঝুঁকিপূর্ণ।",
    ]
    if not (settings.agency_data_is_official and table.is_official):
        notes.insert(
            0,
            "তালিকাটি এখন ডেমো/স্যান্ডবক্স ডেটা। লঞ্চের আগে বিএমইটির অফিসিয়াল তালিকা যুক্ত করতে হবে।",
        )
    return notes


def data_summary() -> tuple[str | None, bool, int]:
    table = load_table()
    return table.data_date, (settings.agency_data_is_official and table.is_official), len(table.records)

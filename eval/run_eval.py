#!/usr/bin/env python3
"""Run the whole test set and print accuracy per module against plan §4 targets.

    python eval/run_eval.py [--json eval/report.json]

The report is printed even when targets are missed - the point is to know where the
product stands, not to look good. Contract field accuracy is measured on whatever
extraction path is active: with ANTHROPIC_API_KEY set that is the model, without it
the deterministic regex reader. Both numbers are reported with the mode named.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "backend"))

from app.config import settings  # noqa: E402
from app.contracts.pipeline import analyse_contract  # noqa: E402
from app.licence.search import lookup as licence_lookup  # noqa: E402
from app.rag.answer import answer_question  # noqa: E402
from app.rag.index import retrieve  # noqa: E402

# Data-quality flags say "this input is not trustworthy", not "this contract has a
# problem", so they are counted separately from false alarms.
DATA_QUALITY_FLAGS = {"fee_cap_unverified"}

TARGETS = {
    "licence_exact_rl": ("Exact RL numbers correct", 1.00),
    "licence_typo_name": ("Typo'd / partial names correct (top-1)", 0.90),
    "licence_typo_name_candidates": ("Typo'd names: right agency offered as a candidate", 1.00),
    "licence_invalid": ("Invalid numbers rejected (not mis-matched)", 1.00),
    "field_accuracy": ("Contract key fields correct", 0.90),
    "flag_recall": ("Planted red flags detected", 0.85),
    "flag_false_alarm": ("False alarms on clean contracts (lower is better)", 0.15),
    "qa_top3": ("Q&A: right section in top 3", 0.80),
    "qa_uncited": ("Q&A answers with no citation (must be 0)", 0.00),
    "latency_p95": ("Contract analysis P95 (seconds, LLM path target 20s)", 20.0),
}


def pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


# --------------------------------------------------------------------------- #
# 1. Licence checker
# --------------------------------------------------------------------------- #
def run_licence(queries: list[dict]) -> dict:
    groups = {"exact_rl": [0, 0], "invalid": [0, 0], "typo_name": [0, 0]}
    candidates = {"exact_rl": [0, 0], "invalid": [0, 0], "typo_name": [0, 0]}
    failures: list[dict] = []

    for item in queries:
        result = licence_lookup(item["q"])
        group = item["group"]
        groups.setdefault(group, [0, 0])
        groups[group][1] += 1

        top = result.matches[0] if result.matches else None
        top_rl = top.record.rl_number if top else None

        if group == "invalid":
            # The only correct answer for a made-up number is "not found". Handing
            # back some other agency would be the dangerous failure mode.
            ok = result.status != "found"
        else:
            ok = result.status == "found" and top_rl == item["expect_rl"]

        # Reported separately: for a misspelled name, having the right agency in the
        # candidate list still helps a worker, even when the tool asks them to choose.
        in_candidates = item["expect_rl"] in {
            match.record.rl_number for match in result.matches
        }

        if ok:
            groups[group][0] += 1
            candidates[group][0] += 1
        elif group == "typo_name" and in_candidates:
            candidates[group][0] += 1
            failures.append({"query": item["q"], "group": group, "status": result.status,
                             "got": top_rl, "expected": item["expect_rl"],
                             "note": "expected agency present in candidates, not top-1"})
        else:
            failures.append({"query": item["q"], "group": group, "status": result.status,
                             "got": top_rl, "expected": item["expect_rl"]})

    metrics = {f"licence_{group}": (hit / total) if total else 0.0
               for group, (hit, total) in groups.items()}
    metrics["licence_typo_name_candidates"] = (
        candidates["typo_name"][0] / groups["typo_name"][1] if groups["typo_name"][1] else 0.0
    )
    return {"metrics": metrics, "failures": failures, "counts": groups,
            "candidates": candidates}


# --------------------------------------------------------------------------- #
# 2. Contract analyser + red-flag detector
# --------------------------------------------------------------------------- #
FIELD_KEYS = ["monthly_wage", "duration", "accommodation", "return_ticket", "compensation", "visa"]


def _field_correct(key: str, expected, actual) -> bool:
    if key == "monthly_wage":
        if expected is None:
            return actual is None
        return actual is not None and abs(float(actual) - float(expected)) <= 1
    if key == "duration":
        if expected is None:
            return actual is None
        return actual is not None and abs(float(actual) - float(expected)) <= 1
    if key == "visa":
        if expected is None:
            return True
        return bool(actual) and expected.lower() in str(actual).lower()
    return expected == actual


def run_contracts(expected: dict) -> dict:
    field_hits = {key: [0, 0] for key in FIELD_KEYS}
    planted_total = 0
    planted_hit = 0
    clean_contracts = 0
    clean_with_false_alarm = 0
    latencies: list[float] = []
    per_contract: list[dict] = []
    skipped_needs_llm = 0

    for item in expected["contracts"]:
        path = ROOT / "contracts" / item["file"]
        data = path.read_bytes()
        started = time.monotonic()
        analysis = analyse_contract(
            filename=item["file"],
            data=data,
            declared_type="text/plain",
            answers=item.get("answers") or {},
        )
        latencies.append(time.monotonic() - started)

        extraction = analysis.extraction
        actual = {
            "monthly_wage": extraction.monthly_wage.amount,
            "duration": extraction.contract_duration_months,
            "accommodation": extraction.accommodation_provided,
            "return_ticket": extraction.return_ticket_provided,
            "compensation": extraction.compensation_for_death_or_injury_stated,
            "visa": extraction.visa_type,
        }
        # A fixture can be marked needs_llm when only the model can read it (an
        # Arabic-only document for the regex reader). Those fields are reported as
        # skipped rather than failed, so the offline score stays interpretable.
        skip_fields = bool(item.get("needs_llm")) and not settings.llm_enabled
        field_results = {}
        for key in FIELD_KEYS:
            if key not in item["expect"]:
                continue
            if skip_fields:
                skipped_needs_llm += 1
                field_results[key] = {"expected": item["expect"][key], "got": actual[key],
                                      "ok": None, "skipped": "needs_llm"}
                continue
            ok = _field_correct(key, item["expect"][key], actual[key])
            field_hits[key][1] += 1
            field_hits[key][0] += 1 if ok else 0
            field_results[key] = {"expected": item["expect"][key], "got": actual[key], "ok": ok}

        detected = {flag.id for flag in analysis.flags}
        quality = detected & DATA_QUALITY_FLAGS
        findings = detected - DATA_QUALITY_FLAGS

        planted = set(item["flags"])
        planted_total += len(planted)
        planted_hit += len(planted & detected)

        if item["kind"] == "clean" and not skip_fields:
            # Fixtures the active path cannot even read are excluded: a "missing terms"
            # flag on an unreadable document is the honest answer, not an alarm.
            clean_contracts += 1
            if findings:
                clean_with_false_alarm += 1

        per_contract.append({
            "file": item["file"],
            "extraction_path": extraction.source_mode,
            "fields": field_results,
            "planted": sorted(planted),
            "detected": sorted(detected),
            "missed": sorted(planted - detected),
            "extra_findings": sorted(findings - planted),
            "data_quality_flags": sorted(quality),
        })

    total_fields = sum(v[1] for v in field_hits.values())
    correct_fields = sum(v[0] for v in field_hits.values())
    latencies.sort()
    p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))] if latencies else 0.0

    metrics = {
        "field_accuracy": (correct_fields / total_fields) if total_fields else 0.0,
        "flag_recall": (planted_hit / planted_total) if planted_total else 0.0,
        "flag_false_alarm": (clean_with_false_alarm / clean_contracts) if clean_contracts else 0.0,
        "latency_p95": p95,
    }
    return {
        "metrics": metrics,
        "fields": {key: (v[0], v[1]) for key, v in field_hits.items()},
        "fields_skipped_needs_llm": skipped_needs_llm,
        "per_contract": per_contract,
    }


# --------------------------------------------------------------------------- #
# 3. Rights / complaint Q&A
# --------------------------------------------------------------------------- #
def run_qa(k: int = 3) -> dict:
    """Retrieval hit rate + the grounding invariant.

    ``qa_uncited`` is measured over *answered* questions only: a refusal carries no
    claim, so it cannot be an uncited claim. Refusals are reported separately, and a
    refusal rate that is too high is a coverage problem, not a grounding one.
    """
    pairs = json.loads((ROOT / "qa_pairs.json").read_text(encoding="utf-8"))
    hits = 0
    answered = 0
    refused = 0
    uncited = 0

    for pair in pairs:
        results = retrieve(pair["q"], k=k)
        sections = {chunk.section for chunk, _, _ in results if chunk.section}
        if sections & set(pair["expected_sections"]):
            hits += 1

        answer = answer_question(pair["q"], k=k)
        if answer.refused:
            refused += 1
            continue
        answered += 1
        if not answer.citations:
            uncited += 1

    return {
        "metrics": {
            "qa_top3": hits / len(pairs),
            "qa_uncited": (uncited / answered) if answered else 0.0,
        },
        "total": len(pairs),
        "hits": hits,
        "answered": answered,
        "refused": refused,
        "uncited": uncited,
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default=None, help="write the full report to this path")
    parser.add_argument("--no-qa", action="store_true", help="skip the Q&A pass")
    args = parser.parse_args()

    expected = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))

    print("=" * 78)
    print("Migrant Worker Fraud Shield - evaluation")
    print(f"extraction mode: {settings.mode}"
          + (f" ({settings.anthropic_model})" if settings.llm_enabled else " (regex reader, no API key)"))
    print("=" * 78)

    licence = run_licence(expected["licence_queries"])
    contracts = run_contracts(expected)
    qa = {"metrics": {}, "total": 0, "hits": 0, "uncited": 0} if args.no_qa else run_qa()

    metrics: dict[str, float] = {}
    metrics.update(licence["metrics"])
    metrics.update(contracts["metrics"])
    metrics.update(qa["metrics"])

    print(f"\n{'metric':<52}{'target':>10}{'actual':>12}  {'':<6}")
    print("-" * 78)
    missed_targets = []
    for key, (label, target) in TARGETS.items():
        if key not in metrics:
            continue
        actual = metrics[key]
        if key in ("flag_false_alarm", "qa_uncited", "latency_p95"):
            ok = actual <= target
        else:
            ok = actual >= target
        shown = f"{actual:.2f}s" if key == "latency_p95" else pct(actual)
        target_shown = f"{target:.2f}s" if key == "latency_p95" else pct(target)
        print(f"{label:<52}{target_shown:>10}{shown:>12}  {'PASS' if ok else 'MISS':<6}")
        if not ok:
            missed_targets.append(key)

    print("\ncontract field breakdown:")
    for key, (hit, total) in contracts["fields"].items():
        if total:
            print(f"  {key:<45}{hit:>4}/{total:<4}{pct(hit / total)}")
    if contracts["fields_skipped_needs_llm"]:
        print(f"  ({contracts['fields_skipped_needs_llm']} fields skipped: fixture needs the "
              f"vision/LLM path, which is off in this run)")

    if licence["failures"]:
        print("\nlicence failures (first 8):")
        for failure in licence["failures"][:8]:
            print(f"  {failure['group']:<10} {failure['query']:<32} "
                  f"status={failure['status']:<10} got={failure['got']} expected={failure['expected']}")

    missed = [c for c in contracts["per_contract"] if c["missed"]]
    if missed:
        print("\ncontracts with missed planted flags (first 8):")
        for item in missed[:8]:
            print(f"  {item['file']:<28} missed={item['missed']}")

    extra = [
        c for c in contracts["per_contract"]
        if c["extra_findings"] and c["file"].startswith(("c01", "c02", "c03", "c04", "c05", "c06", "c07", "c08"))
    ]
    if extra:
        print("\nfalse alarms on clean contracts:")
        for item in extra:
            print(f"  {item['file']:<28} extra={item['extra_findings']}")

    print(f"\nQ&A: {qa['hits']}/{qa['total']} questions had the expected section in the top 3; "
          f"{qa['answered']} answered, {qa['refused']} refused (no sources), "
          f"{qa['uncited']} answered without a citation.")
    if qa["refused"]:
        print("  note: refusals are the designed behaviour when retrieval is weak; "
              "a high refusal rate means the corpus or keywords need work, not that the "
              "grounding rule should be relaxed.")
    print(f"\nresult: {'all targets met' if not missed_targets else 'missed targets: ' + ', '.join(missed_targets)}")

    if args.json:
        report = {
            "mode": settings.mode,
            "metrics": metrics,
            "missed_targets": missed_targets,
            "licence": licence,
            "contracts": contracts,
            "qa": qa,
        }
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

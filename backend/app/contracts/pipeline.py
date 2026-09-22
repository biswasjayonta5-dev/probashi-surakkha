"""End-to-end contract analysis, independent of HTTP so it can be tested directly.

Order of operations matters and is deliberate:
    validate bytes -> read text -> neutralise injections -> extract facts
    -> deterministic rules -> (optional) model-only extra flags -> Bangla summary
The rule engine runs *after* extraction and its output is never overridden by the
model. The Bangla summary is generated from validated JSON + flags, never from the
raw document.
"""

from __future__ import annotations

import time

from ..errors import EmptyDocument, LLMUnavailable, log_event
from ..llm import extract_json, is_enabled, llm
from ..licence.search import lookup as licence_lookup
from ..rules.engine import contract_flags, llm_vague_flags
from ..safety.guard import (
    DISCLAIMER_BN,
    RedactionResult,
    neutralise_untrusted,
    redact_pii,
)
from ..schemas import ContractAnalysis, RedactionReport
from . import docio
from .extract import extract_document
from .summarize import summarise

UNUSUAL_SYSTEM_PROMPT = """You read a labour contract or job offer.

List at most 3 short phrases (verbatim, quoted from the document) that are vague,
one-sided or unusual in a way that could hide a risk for the worker - for example
"as per company policy", "salary may be revised", "termination at employer's
discretion".

Rules:
1. Quote exactly from the document. Do not paraphrase.
2. Output only JSON: {"phrases": ["...", "..."]}
3. If nothing is unusual, output {"phrases": []}.
4. Do not judge whether the contract is safe, legal or genuine."""


def _unusual_phrases(text: str, max_phrases: int = 3) -> list[str]:
    if not text.strip():
        return []
    try:
        response = llm.complete(
            system=UNUSUAL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text[:12000]}],
            model=None,  # Sonnet-class: wording judgement is not a routing task
            max_tokens=400,
            json_prefill=True,
        )
        data = extract_json(response)
    except LLMUnavailable:
        return []
    except Exception as exc:
        log_event("unusual_pass_failed", detail=f"{type(exc).__name__}:{exc}"[:200])
        return []

    phrases = data.get("phrases", []) if isinstance(data, dict) else []
    cleaned: list[str] = []
    lowered = text.lower()
    for phrase in phrases[: max_phrases * 2]:
        if not isinstance(phrase, str):
            continue
        phrase = phrase.strip().strip("“”\"'")
        # Only accept phrases that really appear in the document: a model that
        # invents a clause must not be able to put words in the user's contract.
        if phrase and phrase.lower() in lowered and phrase not in cleaned:
            cleaned.append(phrase)
    return cleaned[:max_phrases]


def analyse_contract(
    *,
    filename: str,
    data: bytes,
    declared_type: str | None,
    answers: dict | None = None,
) -> ContractAnalysis:
    started = time.monotonic()
    answers = {k: v for k, v in (answers or {}).items() if v not in (None, "")}

    media_type = docio.validate(data, filename, declared_type)
    text, info = docio.extract_text(data, media_type)

    if not text.strip() and not media_type.startswith("image/") and media_type != "application/pdf":
        raise EmptyDocument(media_type)

    safe_text, injection_hits = neutralise_untrusted(text)
    if injection_hits:
        log_event("injection_detected", patterns=injection_hits, file=filename)

    extraction, meta = extract_document(
        text=safe_text, raw=data, media_type=media_type, allow_llm=True
    )

    if not safe_text.strip() and meta.get("path") != "llm":
        # Nothing readable and no vision path: ask for a clearer photo rather than
        # returning an empty analysis the user might read as "no problems found".
        raise EmptyDocument("unreadable_document")

    agency_not_found = False
    agency_missing = not (answers.get("agency_name") or answers.get("agency_rl"))
    if not agency_missing:
        result = licence_lookup(
            str(answers.get("agency_rl") or answers.get("agency_name") or "")
        )
        agency_not_found = result.status == "not_found"

    flags = contract_flags(
        extraction,
        raw_text=safe_text,
        agency_not_found=agency_not_found,
        agency_missing=agency_missing,
        user_answers=answers,
    )
    if is_enabled():
        phrases = _unusual_phrases(safe_text)
        if phrases:
            flags.extend(llm_vague_flags(safe_text, phrases))

    summary_bn, summary_source = summarise(extraction, flags)

    # Redaction is computed for the log line only; the file itself is already gone.
    redaction: RedactionResult = redact_pii(text)
    log_event(
        "contract_analysed",
        file=filename,
        media_type=media_type,
        read_mode=info.get("read_mode"),
        extraction_path=meta.get("path"),
        problem=meta.get("problem"),
        flags=[flag.id for flag in flags],
        redacted=redaction.counts,
        chars=len(text),
    )

    return ContractAnalysis(
        extraction=extraction,
        flags=flags,
        summary_bn=summary_bn,
        summary_source=summary_source,
        redaction=RedactionReport(applied=redaction.applied, counts=redaction.counts),
        injection_removed=sorted(set(injection_hits)),
        disclaimer_bn=DISCLAIMER_BN,
        mode="llm" if is_enabled() else "offline",
        processing_seconds=round(time.monotonic() - started, 2),
    )

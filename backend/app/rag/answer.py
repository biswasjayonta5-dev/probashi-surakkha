"""Answering rights/complaint questions with mandatory citations.

Invariants enforced here (not merely prompted for):
* an answer with no citation is discarded and replaced by the refusal text;
* every number in the answer must appear in the retrieved sources;
* a weak retrieval result produces "I don't know", never a guess.
"""

from __future__ import annotations

import re

from ..config import settings
from ..errors import LLMUnavailable, log_event
from ..llm import is_enabled, llm
from ..safety.guard import DISCLAIMER_BN, REFUSAL_BN, safe_wording, unsafe_verdict_hits
from ..schemas import AskAnswer, Citation
from .index import Chunk, is_grounded, retrieve

CITATION_PATTERN = re.compile(r"\[\s*(?:ধারা|section|s\.)\s*[০-৯\d]+\s*\]|\(card:[A-Z0-9\-]+\)", re.IGNORECASE)
_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

SYSTEM_PROMPT = """You answer questions for Bangladeshi migrant workers, in Bangla.

Rules:
1. Answer ONLY from the sources given in the user message. Never add outside facts.
2. Every factual or legal claim must end with its citation, e.g. [ধারা ২২] or (card:CARD-04).
3. If the sources do not contain the answer, reply exactly:
   "এই প্রশ্নের উত্তর আমার হাতে থাকা অফিসিয়াল তথ্যে পাওয়া যায়নি।"
   Do not guess and do not pad.
4. Never state or imply that an agency, recruiter or contract is safe, verified or legal.
5. Plain Bangla for a reader with limited literacy: short sentences, no legal jargon.
   If you must use a legal term, explain it in brackets in simple words.
6. No legal advice and no promises about what will happen in a case.
7. Maximum 6 sentences. Prefer concrete next steps and the office to visit.
"""


def _prompt_sources(hits: list[tuple[Chunk, float, float]]) -> str:
    blocks = []
    for chunk, score, _ in hits:
        label = f"[ধারা {chunk.section}]" if chunk.section else f"({chunk.source_id})"
        blocks.append(f"{label} {chunk.title}\n{chunk.text}")
    return "\n\n---\n\n".join(blocks)


def _citations(hits: list[tuple[Chunk, float, float]], limit: int = 4) -> list[Citation]:
    citations: list[Citation] = []
    for chunk, score, _ in hits[:limit]:
        if not chunk.section and chunk.kind != "rule_card":
            continue
        citations.append(
            Citation(
                source_id=chunk.source_id,
                section=f"ধারা {chunk.section}" if chunk.section else None,
                title=chunk.title,
                snippet=chunk.snippet(),
                score=round(float(score), 2),
            )
        )
    return citations


def _offline_answer(hits: list[tuple[Chunk, float, float]]) -> str:
    """Extractive answer: quote the law, label it honestly, never paraphrase."""
    top, _, _ = hits[0]
    lines: list[str] = []
    if top.kind == "rule_card":
        bangla_lines = []
        for line in top.text.splitlines():
            if line.lstrip().startswith("#") or not any("\u0980" <= ch <= "\u09FF" for ch in line):
                continue  # headings are cited in the source line instead
            cleaned = re.sub(r"[*_`]+", "", line).strip(" -#").strip()
            if len(cleaned) > 12:
                bangla_lines.append(cleaned)
        lines.append(f"আপনার প্রশ্নের সাথে সম্পর্কিত নিয়ম:")
        lines.extend(bangla_lines[:4])
        lines.append(f"সূত্র: {top.source_id} — {top.title}।")
    else:
        lines.append(
            f"আপনার প্রশ্নের সাথে মিলে যাওয়া আইনের ধারা: ধারা {top.section} — {top.title}। "
            "নিচে মূল ইংরেজি অংশ দেওয়া হলো (বাংলা ব্যাখ্যা ছাড়া)।"
        )
        lines.append(top.snippet())
        lines.append(
            "বাংলায় সহজ ব্যাখ্যা পেতে এআই সেবা চালু করতে হবে; এখন শুধু মূল আইনের অংশ দেখানো হচ্ছে।"
        )
    if len(hits) > 1:
        others = ", ".join(
            f"ধারা {c.section}" if c.section else c.source_id for c, _, _ in hits[1:3]
        )
        lines.append(f"সম্পর্কিত আরও: {others}।")
    return " ".join(lines)


def _numbers(text: str) -> set[str]:
    normalized = (text or "").translate(_BN_DIGITS)
    return {n.replace(",", "") for n in re.findall(r"\d+(?:\.\d+)?", normalized)}


def validate_answer(answer: str, hits: list[tuple[Chunk, float, float]]) -> list[str]:
    """Return a list of problems; empty means the answer is acceptable."""
    problems: list[str] = []
    if not CITATION_PATTERN.search(answer or ""):
        problems.append("no_citation")
    allowed = _numbers(" ".join(chunk.text for chunk, _, _ in hits))
    invented = sorted(n for n in _numbers(answer) if n not in allowed and len(n) >= 3)
    if invented:
        problems.append("numbers_not_in_sources:" + ",".join(invented))
    if unsafe_verdict_hits(answer):
        problems.append("unsafe_verdict_wording")
    return problems


def answer_question(question: str, k: int = 4) -> AskAnswer:
    question = (question or "").strip()
    if not question:
        return AskAnswer(
            question=question,
            answer_bn="প্রশ্নটি লিখুন বা বলুন।",
            citations=[],
            grounded=False,
            refused=True,
            mode="llm" if is_enabled() else "offline",
            disclaimer_bn=DISCLAIMER_BN,
        )

    hits = retrieve(question, k=k)
    grounded = is_grounded(hits)
    mode = "llm" if is_enabled() else "offline"

    if not grounded:
        log_event("ask_refused", reason="weak_retrieval", best=round(hits[0][1], 2) if hits else 0)
        return AskAnswer(
            question=question,
            answer_bn=REFUSAL_BN,
            citations=[],
            grounded=False,
            refused=True,
            mode=mode,
            disclaimer_bn=DISCLAIMER_BN,
        )

    if not is_enabled():
        return AskAnswer(
            question=question,
            answer_bn=safe_wording(_offline_answer(hits), REFUSAL_BN),
            citations=_citations(hits),
            grounded=True,
            refused=False,
            mode="offline",
            disclaimer_bn=DISCLAIMER_BN,
        )

    user_message = (
        "Sources:\n\n"
        f"{_prompt_sources(hits)}\n\n"
        "---\n\n"
        f"Question (Bangla or English): {question}"
    )
    try:
        raw = llm.complete(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=700,
        )
    except LLMUnavailable:
        log_event("ask_llm_unavailable")
        return AskAnswer(
            question=question,
            answer_bn=safe_wording(_offline_answer(hits), REFUSAL_BN),
            citations=_citations(hits),
            grounded=True,
            refused=False,
            mode="offline",
            disclaimer_bn=DISCLAIMER_BN,
        )

    candidate = raw.strip()
    problems = validate_answer(candidate, hits)
    if problems:
        log_event("ask_answer_rejected", problems=problems)
        return AskAnswer(
            question=question,
            answer_bn=REFUSAL_BN,
            citations=_citations(hits),
            grounded=False,
            refused=True,
            mode=mode,
            disclaimer_bn=DISCLAIMER_BN,
        )

    return AskAnswer(
        question=question,
        answer_bn=candidate,
        citations=_citations(hits),
        grounded=True,
        refused=False,
        mode=mode,
        disclaimer_bn=DISCLAIMER_BN,
    )

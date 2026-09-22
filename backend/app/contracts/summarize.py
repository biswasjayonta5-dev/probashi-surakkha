"""Bangla summary generation.

The plan is explicit: the Bangla explanation is built from the *validated JSON plus
the rule flags*, never from the raw document. So the template path is the primary
implementation, and the model is only allowed to rewrite the template into smoother
Bangla. A rewrite that loses or invents a number is thrown away.
"""

from __future__ import annotations

from ..errors import LLMUnavailable, log_event
from ..llm import is_enabled, llm
from ..schemas import ContractExtraction, Flag
from ..safety.guard import safe_wording

SEVERITY_LABEL_BN = {"high": "জরুরি", "medium": "লক্ষ্য করার মতো", "low": "ছোট"}

_TEMPLATE_FALLBACK_BN = (
    "চুক্তির সারসংক্ষেপ তৈরি করা যায়নি। উপরের ঘরগুলো নিজে পড়ে দেখুন এবং "
    "টাকা দেওয়ার আগে বিএমইটি অফিসে যাচাই করুন।"
)

SUMMARY_SYSTEM_PROMPT = """You rewrite a contractor's structured summary into simple Bangla.

Rules:
1. Keep every number exactly as given (same digits, same currency). Never add numbers.
2. Do not add facts, advice or opinions that are not in the input.
3. Do not say any agency, contract or offer is safe, verified or legal.
4. Short sentences. Explain legal words in brackets in everyday words.
5. Maximum 7 sentences. Return plain text, no markdown, no headings.
"""


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "চুক্তিতে লেখা নেই"
    return "লেখা আছে" if value else "লেখা নেই"


def _money(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "উল্লেখ নেই"
    label = f"{amount:,.0f}"
    if currency:
        label += f" {currency}"
    return label


def template_summary(extraction: ContractExtraction, flags: list[Flag]) -> str:
    lines: list[str] = []

    lines.append("চুক্তির সহজ ব্যাখ্যা:")
    if extraction.employer:
        lines.append(f"• নিয়োগকর্তা: {extraction.employer}।")
    if extraction.job_title:
        lines.append(f"• কাজ: {extraction.job_title}।")
    if extraction.destination_country:
        lines.append(f"• দেশ: {extraction.destination_country}।")
    lines.append(
        f"• মাসিক বেতন: {_money(extraction.monthly_wage.amount, extraction.monthly_wage.currency)}।"
    )
    if extraction.contract_duration_months:
        lines.append(f"• চুক্তির সময়কাল: {extraction.contract_duration_months:,.0f} মাস।")
    else:
        lines.append("• চুক্তির সময়কাল: উল্লেখ নেই।")
    lines.append(f"• থাকার ব্যবস্থা: {_yes_no(extraction.accommodation_provided)}।")
    lines.append(f"• ফেরার টিকিটের কথা: {_yes_no(extraction.return_ticket_provided)}।")
    lines.append(
        "• মৃত্যু বা আঘাতে ক্ষতিপূরণের কথা: "
        f"{_yes_no(extraction.compensation_for_death_or_injury_stated)}।"
    )
    if extraction.visa_type:
        lines.append(f"• ভিসার ধরন: {extraction.visa_type}।")

    if extraction.fees_mentioned:
        fees = "; ".join(
            f"{fee.amount:,.0f} {fee.currency}" for fee in extraction.fees_mentioned[:4]
        )
        lines.append(f"• চুক্তিতে উল্লেখ থাকা টাকার অঙ্ক: {fees}।")

    if extraction.unclear_or_missing:
        lines.append(
            "• যেসব তথ্য পাওয়া যায়নি: " + ", ".join(extraction.unclear_or_missing) + "।"
        )

    lines.append("")
    if flags:
        lines.append("যা নিয়ে সতর্ক থাকা দরকার:")
        for flag in flags:
            marker = SEVERITY_LABEL_BN.get(flag.severity, "")
            lines.append(f"• [{marker}] {flag.label_bn}: {flag.reason_bn}")
    else:
        lines.append(
            "স্বাভাবিক নিয়মে কোনো সতর্কতা পাওয়া যায়নি — তবে এর অর্থ এই নয় যে সব ঠিক আছে। "
            "রসিদ, ভিসার ধরন ও ফি আলাদাভাবে যাচাই করুন।"
        )

    lines.append("")
    lines.append(
        "পরের ধাপ: (১) এজেন্সির আরএল নম্বর ও নাম বিএমইটি তালিকায় মিলিয়ে দেখুন, "
        "(২) টাকা দেওয়ার আগে লিখিত চুক্তি ও রসিদ নিন, "
        "(৩) ব্যাংকের মাধ্যমে টাকা দিন — নগদ নয়, "
        "(৪) সন্দেহ হলে বিএমইটি অফিস বা জেলা কর্মসংস্থান ও জনশক্তি অফিসে (DEMO) অভিযোগ করুন।"
    )
    if extraction.confidence == "low":
        lines.append(
            "লক্ষ্য করুন: ফাইলটি ভালোভাবে পড়া যায়নি, তাই এই সারসংক্ষেপ অসম্পূর্ণ হতে পারে। "
            "পরিষ্কার ছবি দিলে আরও নির্ভুল হবে।"
        )
    return "\n".join(lines)


def _numbers(text: str) -> set[str]:
    import re

    return {token.replace(",", "") for token in re.findall(r"\d[\d,]*", text or "")}


def llm_summary(extraction: ContractExtraction, flags: list[Flag], template: str) -> str:
    """Polish the template into smoother Bangla, or keep the template."""
    flag_lines = "\n".join(f"- [{f.severity}] {f.label_bn}: {f.reason_bn}" for f in flags) or "- none"
    payload = (
        "Structured facts (JSON):\n"
        f"{extraction.model_dump_json()}\n\n"
        f"Rule flags:\n{flag_lines}\n\n"
        "Draft summary to rewrite:\n"
        f"{template}"
    )
    candidate = llm.complete(
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": payload}],
        max_tokens=900,
    ).strip()

    if not candidate:
        return template
    missing = _numbers(template) - _numbers(candidate)
    invented = _numbers(candidate) - _numbers(template)
    if missing or invented:
        log_event(
            "summary_rewrite_rejected",
            missing=sorted(missing)[:5],
            invented=sorted(invented)[:5],
        )
        return template
    safe = safe_wording(candidate, template)
    if safe == template and candidate != template:
        log_event("summary_rewrite_rejected", reason="unsafe_wording")
    return safe


def summarise(extraction: ContractExtraction, flags: list[Flag]) -> tuple[str, str]:
    """Return ``(summary_bn, source)`` where source is 'llm' or 'template'."""
    template = template_summary(extraction, flags)
    if not is_enabled():
        return template, "template"
    try:
        return llm_summary(extraction, flags, template), "llm"
    except LLMUnavailable as exc:
        log_event("summary_llm_unavailable", detail=str(exc))
    except Exception as exc:  # never let a rewrite failure break analysis
        log_event("summary_llm_error", detail=f"{type(exc).__name__}:{exc}"[:200])
    return template, "template"

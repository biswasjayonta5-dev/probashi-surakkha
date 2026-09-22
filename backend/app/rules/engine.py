"""Deterministic red-flag engine.

Everything the plan listed as "must be exactly right" is computed here in plain
Python from validated facts - never by the model. The optional LLM pass only adds
*extra* flags for wording it cannot classify; it can never remove a rule flag.

Every flag carries its legal basis so the UI can show a source line.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from ..config import settings
from ..schemas import ContractExtraction, FeeCap, FeeCheckResult, Flag

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Currencies we can convert. Rates are a hard-coded, clearly-dated snapshot so the
# prototype never depends on a live FX call; refresh with the fee cap review.
FX_TO_BDT = {
    "BDT": 1.0,
    "Tk": 1.0,
    "TK": 1.0,
    "SAR": 32.0,  # [verify] refresh with the cap review
    "MYR": 27.0,  # [verify]
    "USD": 122.0,  # [verify]
    "AED": 33.0,  # [verify]
    "KWD": 396.0,  # [verify]
    "QAR": 33.5,  # [verify]
    "OMR": 317.0,  # [verify]
}
FX_SNAPSHOT_DATE = "2026-09"

_DEST_ALIASES = {
    "saudi": "SA", "saudi arabia": "SA", "ksa": "SA", "সৌদি": "SA", "সৌদি আরব": "SA",
    "riyadh": "SA", "jeddah": "SA", "dammam": "SA",
    "malaysia": "MY", "মালয়েশিয়া": "MY", "kuala lumpur": "MY", "kl": "MY",
}

# Words that mean "the contract deliberately does not commit to a number".
VAGUE_PATTERNS = [
    re.compile(r"as\s+per\s+(company|the\s+company|management|employer)\s+policy", re.I),
    re.compile(r"\b(various|reasonable|appropriate|usual|standard)\s+(duties|tasks|hours|overtime|allowance|benefits)\b", re.I),
    re.compile(r"\bmay\s+be\s+(changed|revised|adjusted)\b[^.\n]{0,40}", re.I),
    re.compile(r"কোম্পানির\s+নীতি\s+অনুযায়ী"),
    re.compile(r"যা\s+প্রযোজ্য\s+হবে"),
]
VAGUE_HINTS = [
    re.compile(r"\b(net|gross)\s+salary\s+as\s+agreed\b", re.I),
]

_MONTH_PATTERNS = [
    re.compile(r"(\d{1,3})\s*(?:months?|মাস)", re.I),
    re.compile(r"(\d{1,2})\s*(?:years?|বছর)", re.I),
]


# --------------------------------------------------------------------------- #
# Fee caps
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _load_fee_caps(mtime: float) -> dict:
    path = settings.fee_caps_json
    if not path.exists():
        return {"caps": {}, "source": "missing"}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def fee_caps() -> dict:
    path = settings.fee_caps_json
    mtime = path.stat().st_mtime if path.exists() else 0.0
    return _load_fee_caps(mtime)


DESTINATION_CODES = {"SA", "MY", "AE", "QA", "KW", "OM"}


def resolve_destination(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\w\u0980-\u09FF\s]", " ", raw.strip())
    # Exact ISO code first (e.g. the extractor storing "SA"): substring matching on
    # two-letter codes would be dangerous ("om" appears inside many words).
    if cleaned.strip().upper() in DESTINATION_CODES:
        return cleaned.strip().upper()
    key = re.sub(r"\s+", " ", cleaned.lower()).strip()
    if key in _DEST_ALIASES:
        return _DEST_ALIASES[key]
    for alias, code in _DEST_ALIASES.items():
        if alias in key:
            return code
    return None


def cap_for(destination: str | None) -> FeeCap | None:
    data = fee_caps()
    code = resolve_destination(destination) or (destination or "").upper()
    entry = data.get("caps", {}).get(code)
    if not entry:
        return None
    return FeeCap(
        country_code=code,
        country_en=entry["country_en"],
        country_bn=entry["country_bn"],
        cap_bdt=entry.get("cap_bdt"),
        effective_date=entry.get("effective_date"),
        verified=bool(entry.get("verified", False)),
        source=entry.get("source", ""),
        note=entry.get("note", ""),
    )


def to_bdt(amount: float | None, currency: str | None) -> float | None:
    if amount is None:
        return None
    code = (currency or "BDT").strip()
    rate = FX_TO_BDT.get(code) or FX_TO_BDT.get(code.upper())
    if rate is None:
        return None
    return amount * rate


def check_fee(destination: str, quoted_fee_bdt: float) -> FeeCheckResult:
    from ..safety.guard import DISCLAIMER_BN

    cap = cap_for(destination)
    flags: list[Flag] = []

    if cap is None or cap.cap_bdt is None:
        return FeeCheckResult(
            destination=destination,
            cap=cap,
            quoted_fee_bdt=quoted_fee_bdt,
            exceeds_cap=None,
            message_bn=(
                "এই দেশের জন্য সরকার-নির্ধারিত ফি আমার হাতে নেই, তাই তুলনা করা সম্ভব হয়নি। "
                "অনুমান করে বলব না। বিএমইটি অফিস বা অফিসিয়াল সার্কুলার থেকে সর্বশেষ ফি জেনে নিন।"
            ),
            disclaimer_bn=DISCLAIMER_BN,
        )

    over = quoted_fee_bdt - cap.cap_bdt
    if over > 0:
        flags.append(
            Flag(
                id="fee_above_cap",
                label_bn="সরকার-নির্ধারিত ফি-সীমার চেয়ে বেশি",
                label_en="Fee above the government ceiling",
                severity="high",
                reason_bn=(
                    f"{cap.country_bn}-এর জন্য সরকার-নির্ধারিত সর্বোচ্চ খরচ "
                    f"{cap.cap_bdt:,.0f} টাকা, কিন্তু চাওয়া হচ্ছে {quoted_fee_bdt:,.0f} টাকা — "
                    f"অতিরিক্ত {over:,.0f} টাকা।"
                ),
                evidence=f"{quoted_fee_bdt:,.0f} BDT vs cap {cap.cap_bdt:,.0f} BDT",
                source_ref="OEMA 2013 ধারা ২১ (সরকার ফি-সীমা নির্ধারণ করে); BMET সার্কুলার",
            )
        )
        if cap.cap_bdt > 0 and quoted_fee_bdt > cap.cap_bdt * 1.5:
            flags.append(
                Flag(
                    id="fee_far_above_cap",
                    label_bn="সীমার চেয়ে অনেক বেশি ফি (৫০%+)",
                    label_en="Fee more than 50% above the ceiling",
                    severity="high",
                    reason_bn=(
                        "চাওয়া ফি সরকার-নির্ধারিত সীমার চেয়ে ৫০% এর বেশি। "
                        "এমন ঘটনা রিপোর্ট অনুযায়ী প্রতারণার সাথে জড়িত থাকে।"
                    ),
                    evidence=f"{quoted_fee_bdt:,.0f} BDT",
                    source_ref="OEMA 2013 ধারা ৩১ (অবৈধ ফি নেওয়া শাস্তিযোগ্য)",
                )
            )
        message = (
            f"⚠️ চাওয়া ফি সরকার-নির্ধারিত সীমার চেয়ে বেশি। {cap.country_bn}: সর্বোচ্চ "
            f"{cap.cap_bdt:,.0f} টাকা (কার্যকর তারিখ: {cap.effective_date or 'অজানা'}), "
            f"চাওয়া হয়েছে {quoted_fee_bdt:,.0f} টাকা।"
        )
    else:
        message = (
            f"চাওয়া ফি সরকার-নির্ধারিত সীমার মধ্যে আছে বলে দেখা যাচ্ছে। {cap.country_bn}: "
            f"সর্বোচ্চ {cap.cap_bdt:,.0f} টাকা (কার্যকর তারিখ: {cap.effective_date or 'অজানা'}), "
            f"চাওয়া {quoted_fee_bdt:,.0f} টাকা। "
            "এটি অনুমোদন নয় — আসল রসিদ, চুক্তি ও ভিসার ধরন আলাদাভাবে যাচাই করুন।"
        )

    if not cap.verified:
        flags.append(
            Flag(
                id="fee_cap_unverified",
                label_bn="ফি-সীমার তথ্য যাচাই করা বাকি",
                label_en="Fee ceiling not yet verified against the latest circular",
                severity="medium",
                reason_bn=(
                    "এই ফি-সীমাটি সংবাদ/মাধ্যম থেকে নেওয়া, সর্বশেষ সরকারি সার্কুলার থেকে "
                    "যাচাই করা হয়নি। চূড়ান্ত সিদ্ধান্তের আগে বিএমইটি থেকে যাচাই করুন।"
                ),
                source_ref="[verify] latest BMET/Ministry circular",
            )
        )

    return FeeCheckResult(
        destination=destination,
        cap=cap,
        quoted_fee_bdt=quoted_fee_bdt,
        exceeds_cap=over > 0,
        over_by_bdt=over if over > 0 else 0.0,
        message_bn=message,
        flags=flags,
        disclaimer_bn=DISCLAIMER_BN,
    )


# --------------------------------------------------------------------------- #
# Contract rules
# --------------------------------------------------------------------------- #
def contract_flags(
    extraction: ContractExtraction,
    *,
    raw_text: str = "",
    agency_not_found: bool = False,
    agency_missing: bool = False,
    user_answers: dict | None = None,
) -> list[Flag]:
    """All deterministic flags for one analysed contract.

    ``user_answers`` carries facts only the worker knows (cash payment, verbal
    promises) and never silently overrides the document.
    """
    answers = {k: v for k, v in (user_answers or {}).items() if v not in (None, "")}
    flags: list[Flag] = []
    missing = set(extraction.unclear_or_missing)

    if agency_missing:
        flags.append(
            Flag(
                id="no_recruiter_named",
                label_bn="চুক্তিতে কোনো এজেন্সি বা নিয়োগকর্তার নাম নেই",
                label_en="No agency / employer identified",
                severity="high",
                reason_bn=(
                    "আপনি কোনো এজেন্সির নাম বা আরএল নম্বর লেখেননি, তাই তালিকায় যাচাই করা যায়নি। "
                    "প্রথমে নাম ও আরএল নম্বর দিয়ে যাচাই করুন।"
                ),
                source_ref="OEMA 2013 ধারা ৯ (লাইসেন্স ছাড়া নিয়োগ নিষিদ্ধ)",
            )
        )
    if agency_not_found:
        flags.append(
            Flag(
                id="agency_not_found",
                label_bn="এজেন্সি সরকারি তালিকায় পাওয়া যায়নি",
                label_en="Agency not found in the licence list",
                severity="high",
                reason_bn=(
                    "এই নাম/আরএল নম্বর আমার হাতে থাকা তালিকায় পাওয়া যায়নি। "
                    "ভুল বানানও হতে পারে, তবে টাকা দেওয়ার আগে বিএমইটি অফিসে যাচাই করুন।"
                ),
                source_ref="OEMA 2013 ধারা ৯",
            )
        )

    fee_flag_ids = {"fee_above_cap", "fee_far_above_cap"}
    mentioned = [f for f in extraction.fees_mentioned]
    quoted = answers.get("quoted_fee_bdt")
    if quoted:
        try:
            mentioned.append(_fee_from_answer(float(quoted), answers.get("quoted_fee_currency", "BDT")))
        except (TypeError, ValueError):
            pass
    for fee in mentioned:
        bdt = to_bdt(fee.amount, fee.currency)
        code = resolve_destination(extraction.destination_country) or resolve_destination(
            answers.get("destination")
        )
        if bdt is None or not code:
            continue
        result = check_fee(code, bdt)
        for flag in result.flags:
            if flag.id in fee_flag_ids and flag.id not in {f.id for f in flags}:
                flags.append(flag)

    if answers.get("cash_payment") or mentions_cash(raw_text):
        flags.append(
            Flag(
                id="cash_payment_no_receipt",
                label_bn="নগদ টাকা, রসিদ নেই",
                label_en="Cash payment without a receipt",
                severity="high",
                reason_bn=(
                    "নগদ টাকা দেওয়া বা ব্যক্তির নামে টাকা দেওয়ার কথা পাওয়া গেছে। "
                    "প্রতারণার অভিযোগে পরে টাকা ফেরানো কঠিন হয়। "
                    "সরকার-অনুমোদিত ব্যাংক/অনলাইন মাধ্যমে টাকা দিন এবং রসিদ নিন।"
                ),
                source_ref="OEMA 2013 ধারা ৩১(খ)(গ) (অবৈধ ফি ও প্রতারণা শাস্তিযোগ্য)",
            )
        )

    visa = (extraction.visa_type or answers.get("visa_type") or "").lower()
    if visa and any(word in visa for word in ("tourist", "visit", "tourism", "ভ্রমণ", "পর্যটন", "ziyarat")):
        flags.append(
            Flag(
                id="tourist_visa_for_work",
                label_bn="কাজের জন্য ট্যুরিস্ট/ভিজিট ভিসা",
                label_en="Tourist or visit visa offered for work",
                severity="high",
                reason_bn=(
                    f"চুক্তি/উত্তরে ভিসার ধরন “{extraction.visa_type or answers.get('visa_type')}” "
                    "বলা হয়েছে। ভ্রমণ ভিসায় বিদেশে কাজ করতে যাওয়া আইনত নিষিদ্ধ এবং আপনাকে "
                    "কাজের অনুমতি ছাড়া ফেলে দেওয়া হতে পারে।"
                ),
                source_ref="OEMA 2013 ধারা ৫(খ) (ছাত্র, প্রশিক্ষণার্থী, পর্যটক কাজে যেতে পারবে না)",
            )
        )

    if answers.get("written_contract") is False:
        flags.append(
            Flag(
                id="no_written_contract",
                label_bn="টাকা দেওয়ার আগে লিখিত চুক্তি নেই",
                label_en="No written contract before payment",
                severity="high",
                reason_bn=(
                    "টাকা দেওয়ার আগে লিখিত চুক্তি না থাকলে পরে বেতন বা শর্ত প্রমাণ করার উপায় থাকে না। "
                    "লিখিত চুক্তি ছাড়া টাকা দেবেন না।"
                ),
                source_ref="OEMA 2013 ধারা ২২ (লিখিত চুক্তিতে শর্ত থাকতে হবে)",
            )
        )

    missing_terms: list[str] = []
    term_map = {
        "monthly_wage": ("মাসিক বেতন", extraction.monthly_wage.amount),
        "contract_duration_months": ("চাকরির সময়কাল", extraction.contract_duration_months),
        "accommodation_provided": ("থাকার ব্যবস্থা", extraction.accommodation_provided),
        "return_ticket_provided": ("ফেরার টিকিটের খরচ", extraction.return_ticket_provided),
        "compensation_for_death_or_injury_stated": (
            "মৃত্যু/আঘাতে ক্ষতিপূরণ",
            extraction.compensation_for_death_or_injury_stated,
        ),
    }
    for key, (label_bn, value) in term_map.items():
        if value is None or key in missing:
            missing_terms.append(label_bn)

    if missing_terms:
        flags.append(
            Flag(
                id="missing_key_terms",
                label_bn="চুক্তিতে জরুরি কিছু শর্ত লেখা নেই",
                label_en="Missing key contract terms",
                severity="medium",
                reason_bn=(
                    "চুক্তিতে পাওয়া যায়নি: " + ", ".join(missing_terms) + "। "
                    "আইন অনুযায়ী চুক্তিতে বেতন, থাকার ব্যবস্থা, চাকরির সময়কাল, "
                    "মৃত্যু/আঘাতে ক্ষতিপূরণ এবং যাওয়া-আসার খরচ লেখা থাকা উচিত।"
                ),
                source_ref="OEMA 2013 ধারা ২২(১)",
            )
        )

    verbal = answers.get("verbal_wage_bdt")
    if verbal and extraction.monthly_wage.amount:
        doc_bdt = to_bdt(extraction.monthly_wage.amount, extraction.monthly_wage.currency)
        try:
            verbal_bdt = float(verbal)
        except (TypeError, ValueError):
            verbal_bdt = None
        if doc_bdt and verbal_bdt and abs(doc_bdt - verbal_bdt) > max(500.0, 0.1 * verbal_bdt):
            flags.append(
                Flag(
                    id="wage_mismatch_verbal_vs_contract",
                    label_bn="মুখে বলা বেতন আর চুক্তির বেতন মিলছে না",
                    label_en="Wage differs from what the agent said verbally",
                    severity="high",
                    reason_bn=(
                        f"এজেন্ট মুখে যে বেতন বলেছে ({verbal_bdt:,.0f} টাকা) আর চুক্তিতে যা লেখা "
                        f"({doc_bdt:,.0f} টাকা) মিলছে না। শুধু চুক্তিতে লেখা বেতনই আইনি ভিত্তি।"
                    ),
                    evidence=f"verbal {verbal_bdt:,.0f} BDT vs contract {doc_bdt:,.0f} BDT",
                    source_ref="OEMA 2013 ধারা ২২(২) (এজেন্সি ও নিয়োগকর্তা যৌথভাবে দায়ী)",
                )
            )

    vague = find_vague_clauses(raw_text)
    if vague:
        flags.append(
            Flag(
                id="vague_clause",
                label_bn="অস্পষ্ট শর্ত (“কোম্পানির নীতি অনুযায়ী”)",
                label_en="Vague wording in the contract",
                severity="medium",
                reason_bn=(
                    "চুক্তিতে অস্পষ্ট শর্ত পাওয়া গেছে: "
                    + " | ".join(f"“{phrase}”" for phrase in vague[:3])
                    + "। সংখ্যা ও শর্ত স্পষ্টভাবে লেখা না থাকলে পরে দাবি করা কঠিন হয়।"
                ),
                evidence="; ".join(vague[:3]),
                source_ref="OEMA 2013 ধারা ২২(১)",
            )
        )

    flags.sort(key=lambda flag: SEVERITY_ORDER.get(flag.severity, 3))
    return flags


def _fee_from_answer(amount: float, currency: str):
    from ..schemas import FeeMention

    return FeeMention(amount=amount, currency=currency or "BDT", payee=None)


_CASH_PHRASES = (
    "in cash",
    "cash payment",
    "pay cash",
    "cash only",
    "paid cash",
    "hand to hand",
    "no receipt",
    "without receipt",
    "নগদ",
    "হাতে হাতে",
    "রসিদ ছাড়া",
    "ব্যক্তিগতভাবে টাকা",
)


def mentions_cash(text: str) -> bool:
    """Cheap keyword check on the document text (runs without the LLM)."""
    raw = (text or "").lower()
    if not raw:
        return False
    return any(phrase in raw for phrase in _CASH_PHRASES)


def find_vague_clauses(text: str) -> list[str]:
    hits: list[str] = []
    for pattern in VAGUE_PATTERNS + VAGUE_HINTS:
        for match in pattern.finditer(text or ""):
            phrase = match.group(0).strip()
            if phrase and phrase not in hits:
                hits.append(phrase)
    return hits


def llm_vague_flags(text: str, extra_phrases: list[str]) -> list[Flag]:
    """Turn phrases found by the LLM pass into flags. Rule flags always win."""
    flags: list[Flag] = []
    for phrase in extra_phrases:
        phrase = (phrase or "").strip()
        if not phrase or len(phrase) > 200:
            continue
        flags.append(
            Flag(
                id="llm_vague_clause",
                label_bn="অস্পষ্ট/অস্বাভাবিক শর্ত",
                label_en="Unusual or vague wording (model-assisted)",
                severity="medium",
                reason_bn=f"চুক্তিতে অস্বাভাবিক শর্ত মনে হয়েছে: “{phrase}”। টাকা দেওয়ার আগে লিখিতভাবে স্পষ্ট করুন।",
                evidence=phrase,
                source_ref="OEMA 2013 ধারা ২২(১) — মডেল-সহায়ক ইঙ্গিত, নিয়ম-ইঞ্জিন নয়",
            )
        )
    return flags

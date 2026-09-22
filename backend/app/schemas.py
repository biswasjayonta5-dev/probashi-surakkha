"""Typed contracts for every module boundary.

The LLM is only ever trusted to fill ``ContractExtraction``. Everything a user
sees as a verdict (flags, licence status, wording) is assembled by Python from
these validated objects - never taken verbatim from the model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["high", "medium", "low"]
Confidence = Literal["high", "medium", "low"]


# --------------------------------------------------------------------------- #
# Contract analysis
# --------------------------------------------------------------------------- #
class Money(BaseModel):
    amount: float | None = None
    currency: str | None = None


class FeeMention(BaseModel):
    amount: float
    currency: str
    payee: str | None = None


class ContractExtraction(BaseModel):
    """Structured facts read out of a contract. ``None`` means "not stated"."""

    employer: str | None = None
    job_title: str | None = None
    destination_country: str | None = None
    monthly_wage: Money = Field(default_factory=Money)
    contract_duration_months: float | None = None
    accommodation_provided: bool | None = None
    return_ticket_provided: bool | None = None
    compensation_for_death_or_injury_stated: bool | None = None
    # Added to the plan's starter schema: the rule engine needs the visa type to
    # run the "tourist/visit visa for work" rule (OEMA 2013 s.5).
    visa_type: str | None = None
    fees_mentioned: list[FeeMention] = Field(default_factory=list)
    unclear_or_missing: list[str] = Field(default_factory=list)
    confidence: Confidence = "low"
    # Provenance of the extraction, not of the document.
    source_mode: Literal["llm", "heuristic"] = "heuristic"
    notes: list[str] = Field(default_factory=list)


class Flag(BaseModel):
    id: str
    label_bn: str
    label_en: str
    severity: Severity
    reason_bn: str
    evidence: str | None = None
    source_ref: str | None = None


class RedactionReport(BaseModel):
    applied: bool = False
    counts: dict[str, int] = Field(default_factory=dict)


class ContractAnalysis(BaseModel):
    extraction: ContractExtraction
    flags: list[Flag]
    summary_bn: str
    summary_source: Literal["llm", "template"] = "template"
    redaction: RedactionReport = Field(default_factory=RedactionReport)
    injection_removed: list[str] = Field(default_factory=list)
    disclaimer_bn: str = ""
    mode: Literal["llm", "offline"] = "offline"
    processing_seconds: float = 0.0


# --------------------------------------------------------------------------- #
# Licence lookup
# --------------------------------------------------------------------------- #
class AgencyRecord(BaseModel):
    rl_number: str
    name: str
    status: str
    address: str | None = None
    valid_until: str | None = None
    source_date: str | None = None
    demo_data: bool = False


class LicenceMatch(BaseModel):
    record: AgencyRecord
    score: float
    match_type: Literal["exact_rl", "exact_name", "fuzzy_name"]


class LicenceResult(BaseModel):
    query: str
    matched: bool
    status: Literal["found", "not_found", "ambiguous", "invalid_query"]
    message_bn: str
    matches: list[LicenceMatch] = Field(default_factory=list)
    data_date: str | None = None
    source: str = "BMET licensed recruitment agency list"
    is_official_data: bool = False
    notes_bn: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Fee check
# --------------------------------------------------------------------------- #
class FeeCap(BaseModel):
    country_code: str
    country_en: str
    country_bn: str
    cap_bdt: float | None
    effective_date: str | None = None
    verified: bool = False
    source: str = ""
    note: str = ""


class FeeCheckRequest(BaseModel):
    destination: str
    quoted_fee_bdt: float
    itemised: bool = False


class FeeCheckResult(BaseModel):
    destination: str
    cap: FeeCap | None
    quoted_fee_bdt: float
    exceeds_cap: bool | None
    over_by_bdt: float | None = None
    message_bn: str
    flags: list[Flag] = Field(default_factory=list)
    disclaimer_bn: str = ""


# --------------------------------------------------------------------------- #
# Rights / complaint Q&A
# --------------------------------------------------------------------------- #
class Citation(BaseModel):
    source_id: str
    section: str | None = None
    title: str
    snippet: str
    score: float


class AskRequest(BaseModel):
    question: str


class AskAnswer(BaseModel):
    question: str
    answer_bn: str
    citations: list[Citation] = Field(default_factory=list)
    grounded: bool = False
    refused: bool = False
    mode: Literal["llm", "offline"] = "offline"
    disclaimer_bn: str = ""


# --------------------------------------------------------------------------- #
# Meta / errors
# --------------------------------------------------------------------------- #
class MetaResponse(BaseModel):
    mode: Literal["llm", "offline"]
    agency_data_date: str | None = None
    agency_data_is_official: bool = False
    fee_cap_dates: dict[str, str | None] = Field(default_factory=dict)
    helpline_bn: str
    helpline_verified: bool
    partner_name_bn: str = ""
    partner_logo_url: str = ""
    act_source: str = ""
    disclaimer_bn: str = ""
    voice_hint_bn: str = ""


class ErrorResponse(BaseModel):
    error: str
    message_bn: str

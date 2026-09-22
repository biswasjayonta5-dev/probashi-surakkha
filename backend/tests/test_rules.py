"""Red-flag engine: the part that must be exactly right, so it is pure Python."""

from __future__ import annotations

from app.rules.engine import (
    cap_for,
    check_fee,
    contract_flags,
    find_vague_clauses,
    mentions_cash,
    resolve_destination,
    to_bdt,
)
from app.schemas import ContractExtraction, FeeMention, Money

CLEAN_TEXT = """Employer: Al Faisal Trading Est.
Monthly salary: SAR 1200
Contract duration: 24 months
Accommodation: provided
Return ticket: provided
Compensation for death or injury: as per labour law
Visa: work visa
"""


def clean_extraction(**overrides) -> ContractExtraction:
    values = {
        "employer": "Al Faisal Trading Est.",
        "job_title": "Cleaner",
        "destination_country": "SA",
        "monthly_wage": Money(amount=1200, currency="SAR"),
        "contract_duration_months": 24,
        "accommodation_provided": True,
        "return_ticket_provided": True,
        "compensation_for_death_or_injury_stated": True,
        "visa_type": "work visa",
        "confidence": "high",
    }
    values.update(overrides)
    return ContractExtraction(**values)


class TestDestinationResolution:
    def test_codes_and_names_and_bangla(self):
        assert resolve_destination("SA") == "SA"
        assert resolve_destination("Saudi Arabia") == "SA"
        assert resolve_destination("সৌদি আরব") == "SA"
        assert resolve_destination("Malaysia") == "MY"
        assert resolve_destination("মালয়েশিয়া") == "MY"

    def test_two_letter_codes_are_not_substring_matched(self):
        """'om' (Oman) inside a word must not be read as a destination."""
        assert resolve_destination("from") is None
        assert resolve_destination("Random text") is None


class TestFeeCheck:
    def test_over_cap_is_flagged_high(self):
        result = check_fee("SA", 400_000)
        assert result.exceeds_cap is True
        assert result.over_by_bdt == 235_000
        ids = {flag.id for flag in result.flags}
        assert "fee_above_cap" in ids
        assert "fee_far_above_cap" in ids
        assert "⚠️" in result.message_bn

    def test_within_cap_is_not_an_approval(self):
        result = check_fee("SA", 150_000)
        assert result.exceeds_cap is False
        assert "অনুমোদন নয়" in result.message_bn

    def test_unknown_destination_says_it_does_not_know(self):
        result = check_fee("Narnia", 100_000)
        assert result.exceeds_cap is None
        assert result.cap is None
        assert "অনুমান করে বলব না" in result.message_bn

    def test_unverified_cap_produces_a_data_quality_flag(self):
        result = check_fee("MY", 200_000)
        assert "fee_cap_unverified" in {flag.id for flag in result.flags}

    def test_currency_conversion(self):
        assert to_bdt(1200, "SAR") == 1200 * 32.0
        assert to_bdt(1000, "XYZ") is None
        assert to_bdt(None, "SAR") is None

    def test_cap_lookup_by_alias(self):
        assert cap_for("সৌদি আরব").country_code == "SA"


class TestContractFlags:
    def test_clean_contract_with_licensed_agency_has_no_findings(self):
        flags = contract_flags(clean_extraction(), raw_text=CLEAN_TEXT)
        assert flags == []

    def test_agency_not_found_flag(self):
        flags = contract_flags(clean_extraction(), raw_text=CLEAN_TEXT, agency_not_found=True)
        assert "agency_not_found" in {flag.id for flag in flags}

    def test_agency_missing_flag(self):
        flags = contract_flags(clean_extraction(), raw_text=CLEAN_TEXT, agency_missing=True)
        assert "no_recruiter_named" in {flag.id for flag in flags}

    def test_missing_wage_and_other_terms(self):
        extraction = clean_extraction(
            monthly_wage=Money(amount=None),
            contract_duration_months=None,
            accommodation_provided=None,
            return_ticket_provided=None,
            compensation_for_death_or_injury_stated=None,
        )
        flags = {flag.id: flag for flag in contract_flags(extraction, raw_text="")}
        assert "missing_key_terms" in flags
        reason = flags["missing_key_terms"].reason_bn
        for label in ("মাসিক বেতন", "চাকরির সময়কাল", "থাকার ব্যবস্থা"):
            assert label in reason

    def test_tourist_visa_flag_from_document(self):
        extraction = clean_extraction(visa_type="tourist visa")
        flags = {flag.id for flag in contract_flags(extraction, raw_text="Visa: tourist visa")}
        assert "tourist_visa_for_work" in flags

    def test_tourist_visa_flag_from_user_answer(self):
        flags = {flag.id for flag in contract_flags(clean_extraction(visa_type=None), user_answers={"visa_type": "tourist"})}
        assert "tourist_visa_for_work" in flags

    def test_no_written_contract_flag(self):
        flags = contract_flags(clean_extraction(), user_answers={"written_contract": False})
        assert "no_written_contract" in {flag.id for flag in flags}

    def test_cash_payment_from_answer_and_from_document(self):
        from_answer = contract_flags(clean_extraction(), user_answers={"cash_payment": True})
        assert "cash_payment_no_receipt" in {flag.id for flag in from_answer}
        from_document = contract_flags(
            clean_extraction(), raw_text="Agency fee: SAR 9000 payable in cash to the agent."
        )
        assert "cash_payment_no_receipt" in {flag.id for flag in from_document}

    def test_wage_mismatch_verbal_vs_contract(self):
        flags = {
            flag.id
            for flag in contract_flags(
                clean_extraction(), user_answers={"verbal_wage_bdt": 60_000}
            )
        }
        assert "wage_mismatch_verbal_vs_contract" in flags

    def test_fee_above_cap_detected_from_contract_figures(self):
        extraction = clean_extraction(
            fees_mentioned=[FeeMention(amount=9500, currency="SAR", payee="agent")]
        )
        flags = {flag.id for flag in contract_flags(extraction, raw_text="")}
        assert "fee_above_cap" in flags

    def test_vague_wording(self):
        text = "Salary may be revised as per company policy."
        assert find_vague_clauses(text)
        flags = {flag.id for flag in contract_flags(clean_extraction(), raw_text=text)}
        assert "vague_clause" in flags

    def test_high_severity_flags_come_first(self):
        extraction = clean_extraction(monthly_wage=Money(amount=None), visa_type="tourist visa")
        flags = contract_flags(extraction, raw_text="")
        assert flags[0].severity == "high"

    def test_every_flag_has_a_legal_basis(self):
        extraction = clean_extraction(
            monthly_wage=Money(amount=None), visa_type="tourist visa", fees_mentioned=[]
        )
        for flag in contract_flags(extraction, raw_text="as per company policy", agency_not_found=True):
            assert flag.source_ref, f"{flag.id} has no source"
            assert flag.reason_bn
            assert flag.label_bn

    def test_language_helpers(self):
        assert mentions_cash("pay in cash only") is True
        assert mentions_cash("বিনামূল্যে থাকার ব্যবস্থা") is False

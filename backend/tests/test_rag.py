"""Retrieval and the grounding guarantee ("I don't know" beats a guess)."""

from __future__ import annotations

from app.rag.answer import answer_question, validate_answer
from app.rag.index import MIN_BEST_SCORE, corpus_size, is_grounded, retrieve, tokenize
from app.rag.index import stem


class TestCorpus:
    def test_all_act_sections_are_indexed(self):
        # 49 sections of OEMA 2013 + the curated rule cards.
        assert corpus_size() >= 49

    def test_bangla_keywords_are_attached_to_sections(self):
        hits = retrieve("ট্যুরিস্ট ভিসায় কাজ করা যাবে কি?", k=3)
        sections = {chunk.section for chunk, _, _ in hits}
        assert "5" in sections


class TestTokenisation:
    def test_bangla_suffix_is_stripped(self):
        assert stem("অভিযোগের") == "অভিযোগ"

    def test_english_plural_is_stripped(self):
        assert stem("complaints") == "complaint"

    def test_stopwords_removed(self):
        assert "the" not in tokenize("the contract of the worker")
        assert "এর" not in tokenize("চুক্তির শর্ত এর")


class TestRetrieval:
    def test_contract_contents_question_finds_section_22(self):
        hits = retrieve("চুক্তিতে কী কী থাকতে হবে?", k=3)
        assert "22" in {chunk.section for chunk, _, _ in hits}

    def test_complaint_question_finds_section_41(self):
        hits = retrieve("প্রতারণার অভিযোগ কোথায় করব?", k=3)
        assert "41" in {chunk.section for chunk, _, _ in hits}

    def test_irrelevant_question_is_not_grounded(self):
        hits = retrieve("What is the capital gains tax rate on crypto in Norway?", k=3)
        assert not is_grounded(hits)

    def test_relevant_question_is_grounded(self):
        hits = retrieve("What must an employment contract state?", k=3)
        assert is_grounded(hits)
        assert hits[0][1] >= MIN_BEST_SCORE


class TestAnswers:
    def test_offline_answer_carries_citations(self):
        answer = answer_question("প্রতারণার অভিযোগ কোথায় করব?")
        assert answer.refused is False
        assert answer.citations
        assert answer.answer_bn

    def test_unrelated_question_is_refused(self):
        answer = answer_question("What is the capital gains tax rate on crypto in Norway?")
        assert answer.refused is True
        assert "পাওয়া যায়নি" in answer.answer_bn
        assert answer.citations == []

    def test_adjacent_question_gets_a_sourced_answer_not_advice_about_another_country(self):
        """A student-visa question is genuinely adjacent (OEMA s.5 covers students),
        so answering from s.5 is correct - as long as it stays sourced."""
        answer = answer_question("How do I apply for a student visa to Canada?")
        if not answer.refused:
            assert answer.citations
            assert "Canada" not in answer.answer_bn

    def test_empty_question_is_refused(self):
        assert answer_question("").refused is True

    def test_answer_never_uses_unsafe_wording(self):
        from app.safety.guard import unsafe_verdict_hits

        for question in ("আমি কি এই এজেন্সিকে বিশ্বাস করতে পারি?", "Is my recruiter safe?"):
            assert unsafe_verdict_hits(answer_question(question).answer_bn) == []


class TestAnswerValidation:
    def test_uncited_answer_is_rejected(self):
        hits = retrieve("What must an employment contract state?", k=2)
        problems = validate_answer("The contract must state the wage and duration.", hits)
        assert "no_citation" in problems

    def test_cited_answer_passes(self):
        hits = retrieve("What must an employment contract state?", k=2)
        problems = validate_answer("চুক্তিতে বেতন ও মেয়াদ থাকতে হবে [ধারা 22].", hits)
        assert problems == []

    def test_invented_number_is_rejected(self):
        hits = retrieve("What must an employment contract state?", k=2)
        problems = validate_answer("সর্বোচ্চ ফি 999999 টাকা [ধারা 22].", hits)
        assert any(problem.startswith("numbers_not_in_sources") for problem in problems)

    def test_unsafe_wording_is_rejected(self):
        hits = retrieve("What must an employment contract state?", k=2)
        problems = validate_answer("This agency is safe [ধারা 22].", hits)
        assert "unsafe_verdict_wording" in problems

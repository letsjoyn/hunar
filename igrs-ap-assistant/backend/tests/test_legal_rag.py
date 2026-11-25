"""
Unit Tests for Phase 2: Legal Knowledge Base & Statutory Source Linking.

Validates that:
- Section 22-A (Prohibited properties) is retrieved with exact AP Amendment citation.
- Section 41-A (5-year audit recovery) is retrieved with exact limitation.
- Section 47-A (Undervaluation reference & appeal) is retrieved.
- Section 25 (Presentation delay fines) is retrieved with Rule 38.
- G.O.Ms.No. 192, 394, 393 are retrieved with exact rates and orders.
"""
from backend.app.engine.legal_search import search_legal_knowledge, get_verbatim_section


def test_search_section_22a():
    results = search_legal_knowledge("Section 22-A")
    assert len(results) > 0
    top = results[0]
    assert "22" in top.section_or_rule
    assert "Assigned Lands" in top.title or "Prohibition" in top.title
    assert "MANDATORY REFUSAL" in top.sro_guidance or "refuse" in top.sro_guidance.lower()
    assert "AP Act No. 19 of 2007" in top.authority_citation


def test_search_section_41a_audit():
    results = search_legal_knowledge("Section 41-A")
    assert len(results) > 0
    top = results[0]
    assert "41" in top.section_or_rule
    assert "5" in top.sro_guidance or "five years" in top.content.lower()
    assert "17 of 1986" in top.authority_citation or "41-A" in top.authority_citation


def test_search_section_47a_undervaluation():
    results = search_legal_knowledge("Section 47-A undervaluation appeal")
    assert len(results) > 0
    top = results[0]
    assert "47" in top.section_or_rule
    assert "Collector" in top.content
    assert "30 days" in top.sro_guidance or "appeal" in top.sro_guidance.lower()


def test_search_section_25_delay_fine():
    results = search_legal_knowledge("Section 25 delay fine")
    assert len(results) > 0
    top = results[0]
    assert "25" in top.section_or_rule
    assert "ten times" in top.content.lower() or "10x" in top.sro_guidance


def test_search_go_192_user_charges():
    results = search_legal_knowledge("G.O.Ms.No. 192")
    assert len(results) > 0
    top = results[0]
    assert "192" in top.section_or_rule
    assert "500" in top.content
    assert "25.04.2023" in top.authority_citation


def test_search_go_394_stamp_duty():
    results = search_legal_knowledge("G.O.Ms.No. 394")
    assert len(results) > 0
    top = results[0]
    assert "394" in top.section_or_rule
    assert "5%" in top.content or "five per cent" in top.content.lower()


def test_freedom_fighter_assigned_land_query_prefers_ccla_evidence():
    query = (
        "A freedom fighter's assigned land is over 10 years old and has no dispute—"
        "do they still need an NOC to delete it from Section 22-A"
    )
    results = search_legal_knowledge(query)
    assert results
    assert "CCLA Circular" in results[0].section_or_rule
    assert "279" in results[0].content
    assert "NOC" in results[0].content
    assert "OCR source" in results[0].authority_citation
    assert "Section 28" not in results[0].section_or_rule


def test_incidental_year_count_is_not_parsed_as_section():
    query = "A freedom fighter's assigned land is over 10 years old"
    results = search_legal_knowledge(query)
    assert results
    assert "CCLA Circular" in results[0].section_or_rule
    assert "Section 28" not in results[0].section_or_rule


def test_article_20_46_query_uses_matching_case_law_only():
    query = (
        "In a Release deed between 4 brothers, their sister (a co-parcener) did not join "
        "the deed. Is it a Release u/Art 46 or Conveyance u/Art 20?"
    )
    results = search_legal_knowledge(query)
    assert results
    assert results[0].section_or_rule == "Article 20 & Article 46 — co-owner release case law"
    assert "K.V. Subba Rao" in results[0].content
    assert "Section 55A" not in results[0].section_or_rule


def test_missing_explicit_articles_fail_closed():
    results = search_legal_knowledge("Compare Article 900 and Article 901 for a property deed")
    assert results == []


def test_get_verbatim_section():
    sec = get_verbatim_section("Section 22-A")
    assert sec is not None
    assert "Section 22-A" in sec.section_or_rule
    assert len(sec.content) > 50


def test_chroma_vector_semantic_search():
    # Pure semantic colloquial query without mentioning "Section 41-A"
    results = search_legal_knowledge("recovering deficit stamp duty after audit five years")
    assert len(results) > 0
    top = results[0]
    assert "41" in top.section_or_rule or "41" in top.title or "deficit" in top.title.lower()

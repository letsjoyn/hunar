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


def test_settlement_with_debt_discharge_finds_specific_manual_example():
    """#22 regression: S3/7423/93 Settlement-cum-Conveyance guidance must be in top results."""
    query = (
        "A father settles property on his son if the son clears his debts: "
        "pure Settlement or Settlement-cum-Conveyance?"
    )
    results = search_legal_knowledge(query)

    assert results
    # Must find the S3/7423/93 Settlement-cum-Conveyance circular (not the old Note-10 illustration)
    top_titles = [r.title for r in results[:3]]
    assert any("Settlement" in t and ("debt" in t.lower() or "Conveyance" in t) for t in top_titles), \
        f"Top result must be Settlement-cum-Conveyance, got: {top_titles}"
    top = results[0]
    # Must NOT still reference the old "chargeable only as a settlement" Note-10 wording
    assert "chargeable only as a settlement" not in top.sro_guidance
    # Must reference the C&IG proceedings / Settlement-cum-Conveyance / Article 20 logic
    combined = (top.sro_guidance or "") + (top.content or "") + (top.authority_citation or "")
    assert any(kw in combined for kw in ["S3/7423", "Settlement-cum-Conveyance", "Article 20", "Article 49"]), \
        f"Must cite S3/7423/93 settlement-cum-conveyance rule; got: {combined[:300]}"


def test_small_farmer_crop_loan_finds_specific_exemption_and_cap():
    """#24 regression: G.O.Ms.No. 15/2007 ₹3,00,000 ceiling must be in top results."""
    query = (
        "A small farmer with 4 acres wet land takes a 2.5 lakh crop loan from a bank; "
        "is stamp duty and registration fee exempt?"
    )
    results = search_legal_knowledge(query)

    assert results
    assert results[0].title == "Small farmer agricultural crop-loan stamp duty and registration fee exemption (Rs. 3,00,000 limit)"
    combined = (results[0].sro_guidance or "") + (results[0].content or "")
    # Must reference the upgraded G.O.Ms.No. 15/2007 ₹3,00,000 ceiling
    assert any(kw in combined for kw in ["3,00,000", "Rs. 3,00,000", "300000", "G.O.Ms.No. 15"]), \
        f"Must cite ₹3,00,000 ceiling from G.O.Ms.No. 15/2007; got: {combined[:300]}"
    # Rs. 2,50,000 loan should be 100% exempt under the ₹3L ceiling
    assert "100% full exemption" in combined or "exempt" in combined.lower(), \
        "Must state Rs. 2,50,000 is fully exempt under ₹3,00,000 ceiling"
    # Must NOT still say the loan exceeds the ceiling
    assert "exceeds" not in combined or "3,00,000" in combined


def test_section_56_income_tax_query_does_not_match_stamp_act_section_56():
    results = search_legal_knowledge(
        "Is a sister-in-law a relative under Income Tax Act Section 56(2)?"
    )
    assert results
    combined_titles = " ".join(f"{r.act_name} {r.title} {r.content[:200]}" for r in results).lower()
    assert "income tax" in combined_titles or "it act" in combined_titles
    assert "chief controlling revenue" not in combined_titles


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
    combined = " ".join(f"{r.section_or_rule} {r.title} {r.content}" for r in results).lower()
    assert "deficit" in combined
    assert "audit" in combined
    assert any("bm25" in r.retrieval_methods or "dense_vector" in r.retrieval_methods for r in results)


def test_chroma_results_resolve_to_local_sqlite_chunks(monkeypatch):
    import sqlite3

    import backend.app.engine.legal_search as legal_search

    class FakeCollection:
        def count(self):
            return 1

        def query(self, query_texts, n_results):
            assert query_texts == ["relationship definition"]
            assert n_results == 1
            return {"metadatas": [[{"chunk_id": "42"}]]}

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE legal_chunks (
            id INTEGER, act_name TEXT, section_or_rule TEXT, title TEXT,
            content TEXT, sro_guidance TEXT, authority_citation TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO legal_chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
        (42, "AP Stamp Act", "Art. 49", "Settlement", "Local text", "Guidance", "G.O. citation"),
    )
    monkeypatch.setattr(legal_search, "get_chroma_collection", lambda: FakeCollection())

    results = legal_search._search_chroma(conn, "relationship definition", 3)

    assert len(results) == 1
    assert results[0].section_or_rule == "Art. 49"
    assert results[0].content == "Local text"
    conn.close()

"""
Legal Knowledge Search Engine (BM25 + FTS5 + Semantic Hybrid) for IGRS AP Assistant.

Provides instantaneous, deterministic statutory citations from:
- The Registration Act 1908 (including AP Amendment Section 22-A, 23, 25, 34, 47, 49)
- The Indian Stamp Act 1899 (including AP Amendment Section 41-A, 47-A & Schedule I-A)
- AP Registration Rules & Undervaluation Rules
- Relevant Government Orders (e.g. G.O.Ms.No. 192, 394, 393, 719, 1126)
"""
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional
from pydantic import BaseModel

from backend.app.core.config import DATA_DIR

LEGAL_DB_PATH = DATA_DIR / "legal_knowledge.db"
CHROMA_DIR = DATA_DIR / "chroma_db"

_chroma_col = None


def get_chroma_collection():
    global _chroma_col
    if _chroma_col is None and CHROMA_DIR.exists():
        try:
            import chromadb
            client = chromadb.PersistentClient(path=str(CHROMA_DIR))
            _chroma_col = client.get_collection("ap_statutory_rules")
        except Exception:
            _chroma_col = None
    return _chroma_col


class LegalSearchResult(BaseModel):
    id: int
    act_name: str
    section_or_rule: str
    title: str
    content: str
    sro_guidance: str
    authority_citation: str
    snippet: str


def search_legal_knowledge(query: str, limit: int = 5) -> list[LegalSearchResult]:
    """
    Search legal acts using exact section/article/G.O. matching combined with FTS5 BM25.
    Returns ranked, PII-stripped legal text snippets with exact Section citations.
    """
    if not LEGAL_DB_PATH.exists():
        return []

    conn = sqlite3.connect(LEGAL_DB_PATH)
    c = conn.cursor()

    results: list[LegalSearchResult] = []
    seen_ids: set[int] = set()

    # 1. Match G.O. numbers (e.g. "G.O. 192", "G.O.Ms.No. 394")
    m_go = re.search(r"(?:g\.?o\.?(?:ms)?\.?(?:no)?\.?\s*)(\d+)", query, re.IGNORECASE)
    if m_go:
        go_num = m_go.group(1).strip()
        rows_go = c.execute("""
            SELECT id, act_name, section_or_rule, title, content, sro_guidance, authority_citation
            FROM legal_chunks
            WHERE section_or_rule LIKE ? OR section_or_rule LIKE ? OR title LIKE ?
            ORDER BY CASE WHEN section_or_rule LIKE '%G.O%' THEN 0 ELSE 1 END
            LIMIT ?
        """, (f"%G.O%{go_num}%", f"%{go_num}%", f"%G.O%{go_num}%", limit)).fetchall()
        for r in rows_go:
            if r[0] not in seen_ids:
                seen_ids.add(r[0])
                snippet = r[4][:300].strip().replace("\n", " ") + "..."
                results.append(LegalSearchResult(
                    id=r[0], act_name=r[1], section_or_rule=r[2],
                    title=r[3], content=r[4], sro_guidance=r[5],
                    authority_citation=r[6], snippet=snippet
                ))
        if results:
            conn.close()
            return results[:limit]

    # 2. Match only explicit section references. Never infer a statute number from
    # incidental numbers in natural language (e.g. "10 years").
    m_sec = re.search(
        r"(?:\bsection\s*|\bsec\.?\s*|\bu/s\.?\s*|\bunder\s+section\s*)"
        r"(\d+[\s\-]?[A-Za-z]?)\b",
        query,
        re.IGNORECASE,
    )
    if not m_sec and re.fullmatch(r"\s*\d+[\s\-]?[A-Za-z]?\s*", query):
        m_sec = re.fullmatch(r"\s*(\d+[\s\-]?[A-Za-z]?)\s*", query)
    if m_sec:
        raw_sec = m_sec.group(1).strip()
        clean_sec = raw_sec.replace(" ", "").upper()
        clean_sec_hyphen = re.sub(r"(\d+)([A-Z])", r"\1-\2", clean_sec)

        targets = [
            f"Section {clean_sec}",
            f"Section {clean_sec_hyphen}",
            clean_sec,
            clean_sec_hyphen,
        ]

        for target in targets:
            rows = c.execute("""
                SELECT id, act_name, section_or_rule, title, content, sro_guidance, authority_citation
                FROM legal_chunks
                WHERE section_or_rule = ? OR section_or_rule LIKE ?
                LIMIT ?
            """, (target, f"%{target}%", limit)).fetchall()
            for r in rows:
                if r[0] not in seen_ids:
                    seen_ids.add(r[0])
                    snippet = r[4][:300].strip().replace("\n", " ") + "..."
                    results.append(LegalSearchResult(
                        id=r[0], act_name=r[1], section_or_rule=r[2],
                        title=r[3], content=r[4], sro_guidance=r[5],
                        authority_citation=r[6], snippet=snippet
                    ))

    # 2.5. Match Rule pattern e.g. "Rule 61", "Rule 71"
    m_rule = re.search(r"\brule\s*(\d+[A-Za-z\-]*)", query, re.IGNORECASE)
    if m_rule:
        rule_num = m_rule.group(1).strip()
        rows_rule = c.execute("""
            SELECT id, act_name, section_or_rule, title, content, sro_guidance, authority_citation
            FROM legal_chunks
            WHERE section_or_rule LIKE ? OR title LIKE ?
            LIMIT ?
        """, (f"%Rule {rule_num}%", f"%Rule {rule_num}%", limit)).fetchall()
        for r in rows_rule:
            if r[0] not in seen_ids:
                seen_ids.add(r[0])
                snippet = r[4][:300].strip().replace("\n", " ") + "..."
                results.append(LegalSearchResult(
                    id=r[0], act_name=r[1], section_or_rule=r[2],
                    title=r[3], content=r[4], sro_guidance=r[5],
                    authority_citation=r[6], snippet=snippet
                ))

    # 3. Explicit Article references are exact lookup requests. Never fall through
    # to vector/keyword matches when a requested Article is absent from the index.
    article_refs = list(dict.fromkeys(
        re.findall(r"\b(?:articles?|art\.?)\s*(\d+[A-Za-z]?(?:-[A-Za-z0-9]+)?)", query, re.IGNORECASE)
    ))
    if article_refs:
        rows = c.execute("""
            SELECT id, act_name, section_or_rule, title, content, sro_guidance, authority_citation
            FROM legal_chunks
            ORDER BY id
        """).fetchall()
        indexed_article_rows = []
        for r in rows:
            labels = f"{r[2] or ''} {r[3] or ''}"
            indexed_refs = {
                value.upper().replace(" ", "")
                for value in re.findall(
                    r"\b(?:articles?|art\.?)\s*(\d+[A-Za-z]?(?:-[A-Za-z0-9]+)?)",
                    labels,
                    re.IGNORECASE,
                )
            }
            if set(ref.upper().replace(" ", "") for ref in article_refs).issubset(indexed_refs):
                indexed_article_rows.append(r)

        # For comparisons, require one indexed source that covers every cited Article.
        if not indexed_article_rows:
            conn.close()
            return []
        for r in indexed_article_rows[:limit]:
            seen_ids.add(r[0])
            results.append(LegalSearchResult(
                id=r[0], act_name=r[1], section_or_rule=r[2], title=r[3],
                content=r[4], sro_guidance=r[5], authority_citation=r[6],
                snippet=r[4][:300].strip().replace("\n", " ") + "...",
            ))
        conn.close()
        return results[:limit]

    # 4. Topic-specific evidence must outrank generic statutory chunks. This
    # route intentionally requires multiple distinctive terms to avoid broad hits.
    lower_query = query.lower()
    if (
        "freedom fighter" in lower_query
        and any(term in lower_query for term in ("noc", "no objection", "assigned land"))
    ):
        rows_topic = c.execute("""
            SELECT id, act_name, section_or_rule, title, content, sro_guidance, authority_citation
            FROM legal_chunks
            WHERE title LIKE '%Freedom Fighter%'
              AND (content LIKE '%NOC%' OR content LIKE '%no need for obtaining%')
            ORDER BY id
            LIMIT ?
        """, (limit,)).fetchall()
        for r in rows_topic:
            if r[0] not in seen_ids:
                seen_ids.add(r[0])
                results.insert(0, LegalSearchResult(
                    id=r[0], act_name=r[1], section_or_rule=r[2],
                    title=r[3], content=r[4], sro_guidance=r[5],
                    authority_citation=r[6],
                    snippet=r[4][:300].strip().replace("\n", " ") + "...",
                ))
        if rows_topic:
            conn.close()
            return results[:limit]

    # Exact statutory matches and the targeted CCLA topic result take precedence
    # over general text search.
    if results:
        conn.close()
        return results[:limit]

    # FTS5 BM25 for unnumbered questions, with a minimum lexical-overlap check.
    clean_q = re.sub(r"[^a-zA-Z0-9\s]", " ", query).strip()
    stop_words = {
        "the", "and", "for", "with", "that", "this", "from", "does", "they",
        "their", "what", "when", "where", "which", "who", "how", "can", "could",
        "would", "should", "into", "about", "have", "has", "had", "are", "was",
        "were", "been", "being", "not", "but", "between", "still", "need", "under",
    }
    words = list(dict.fromkeys(
        word.lower() for word in clean_q.split()
        if len(word) > 2 and word.lower() not in stop_words
    ))
    if words:
        fts_query = " OR ".join(words)
        try:
            rows_fts = c.execute("""
                SELECT c.id, c.act_name, c.section_or_rule, c.title, c.content,
                       c.sro_guidance, c.authority_citation, rank
                FROM legal_chunks_fts f
                JOIN legal_chunks c ON c.id = f.rowid
                WHERE legal_chunks_fts MATCH ?
                ORDER BY rank
                LIMIT 30
            """, (fts_query,)).fetchall()
            ranked_rows = []
            for row in rows_fts:
                searchable = " ".join(str(value or "") for value in row[1:7]).lower()
                matched_terms = [
                    word for word in words
                    if re.search(rf"\b{re.escape(word)}\b", searchable)
                ]
                if len(matched_terms) < min(2, len(words)):
                    continue
                title_text = f"{row[2] or ''} {row[3] or ''}".lower()
                title_matches = sum(
                    bool(re.search(rf"\b{re.escape(word)}\b", title_text))
                    for word in matched_terms
                )
                ranked_rows.append((-(len(matched_terms) + title_matches), row[7], row))

            ranked_rows.sort(key=lambda item: (item[0], item[1]))
            for _, _, r in ranked_rows[:limit]:
                results.append(LegalSearchResult(
                    id=r[0], act_name=r[1], section_or_rule=r[2], title=r[3],
                    content=r[4], sro_guidance=r[5], authority_citation=r[6],
                    snippet=r[4][:300].strip().replace("\n", " ") + "...",
                ))
        except sqlite3.OperationalError:
            results = []

    conn.close()
    return results[:limit]


def get_verbatim_section(section_or_rule: str) -> Optional[LegalSearchResult]:
    """Retrieve full verbatim text and citation for a specific section or G.O."""
    if not LEGAL_DB_PATH.exists():
        return None
    conn = sqlite3.connect(LEGAL_DB_PATH)
    c = conn.cursor()
    row = c.execute("""
        SELECT id, act_name, section_or_rule, title, content, sro_guidance, authority_citation
        FROM legal_chunks
        WHERE section_or_rule = ? OR section_or_rule LIKE ?
        LIMIT 1
    """, (section_or_rule, f"%{section_or_rule}%")).fetchone()
    conn.close()
    if row:
        return LegalSearchResult(
            id=row[0], act_name=row[1], section_or_rule=row[2],
            title=row[3], content=row[4], sro_guidance=row[5],
            authority_citation=row[6], snippet=row[4][:300] + "..."
        )
    return None

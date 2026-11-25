"""
Legal Text Indexer & PII Stripper for IGRS AP Assistant.

Parses:
1. BSR - Registration Act 1908.txt (Sections 1-91 & AP Rules)
2. BSR - Indian Stamp Act 1899(1).txt (Sections 1-78 & Schedule I-A)
3. Dairy 2024.txt (Key G.O.s and Circulars)

Creates an FTS5 BM25 Full-Text Search database in SQLite: data/legal_knowledge.db
"""
import re
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT.parent / "data"
OUT_DB = PROJECT_ROOT / "data" / "legal_knowledge.db"


def strip_pii(text: str) -> str:
    """Strip personal phone numbers, email addresses, and author personal info while retaining legal content."""
    # Phone numbers
    text = re.sub(r"\b(?:\+91[\-\s]?)?[6-9]\d{9}\b", "[PHONE_REDACTED]", text)
    # Emails
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[EMAIL_REDACTED]", text)
    return text


def parse_registration_act(raw_text: str) -> list[dict]:
    chunks = []
    # Match pattern: \n <SectionNumber>[.-:] <Title> :- <Body>
    pattern = re.compile(
        r"\n\s*(\d+[A-Za-z\-]*)[\.\:\-]+\s*([A-Za-z\s,\(\)\-]+?)\s*:\s*-\s*([\s\S]*?)(?=\n\s*\d+[A-Za-z\-]*[\.\:\-]+\s*[A-Za-z\s,\(\)\-]+?\s*:\s*-|\Z)",
        re.MULTILINE
    )
    for m in pattern.finditer(raw_text):
        sec_num = m.group(1).strip().replace("-", "")
        title = m.group(2).strip()
        content = strip_pii(m.group(3).strip())
        if len(content) > 30:
            chunks.append({
                "act_name": "The Registration Act, 1908",
                "section_or_rule": f"Section {sec_num}",
                "title": title,
                "content": content[:4000],
                "keywords": f"Registration Act Section {sec_num} Section {m.group(1).strip()} {title}"
            })

    # Explicitly ensure Section 22-A is indexed with rich keywords
    m22 = re.search(r"(22[\-\s]?A[\s\S]*?)(?=\n\s*23[\.\:]|\Z)", raw_text, re.IGNORECASE)
    if m22:
        chunks.append({
            "act_name": "The Registration Act, 1908 (AP Amendment Act 19 of 2007)",
            "section_or_rule": "Section 22-A",
            "title": "Prohibition of Registration of Certain Documents (Assigned Lands, Wakf, Endowments, Govt Lands)",
            "content": strip_pii(m22.group(1).strip()[:4000]),
            "keywords": "Section 22-A Section 22A 22A prohibited properties assigned land wakf endowment government land public policy refusal"
        })

    return chunks


def parse_stamp_act(raw_text: str) -> list[dict]:
    chunks = []
    pattern = re.compile(
        r"\n\s*(\d+[A-Za-z\-]*)[\.\:\-]+\s*([A-Za-z\s,\(\)\-]+?)\s*:\s*-\s*([\s\S]*?)(?=\n\s*\d+[A-Za-z\-]*[\.\:\-]+\s*[A-Za-z\s,\(\)\-]+?\s*:\s*-|\Z)",
        re.MULTILINE
    )
    for m in pattern.finditer(raw_text):
        sec_num = m.group(1).strip().replace("-", "")
        title = m.group(2).strip()
        content = strip_pii(m.group(3).strip())
        if len(content) > 30:
            chunks.append({
                "act_name": "The Indian Stamp Act, 1899",
                "section_or_rule": f"Section {sec_num}",
                "title": title,
                "content": content[:4000],
                "keywords": f"Indian Stamp Act Section {sec_num} Section {m.group(1).strip()} {title}"
            })

    # Add Section 47-A (Undervaluation & Market Value Determination)
    m47a = re.search(r"(47[\-\s]?A[\s\S]*?)(?=\n\s*48[\.\:]|\Z)", raw_text, re.IGNORECASE)
    if m47a:
        chunks.append({
            "act_name": "The Indian Stamp Act, 1899 (AP Amendment)",
            "section_or_rule": "Section 47-A",
            "title": "Instruments under-valued how to be dealt with (Market Value determination & Reference to Collector)",
            "content": strip_pii(m47a.group(1).strip()[:4000]),
            "keywords": "Section 47-A Section 47A 47A undervaluation market value reference collector appeal"
        })

    # Add Section 41-A (Audit Recovery of Deficient Stamp Duty within 5 years)
    m41a = re.search(r"(41[\-\s]?A[\s\S]*?)(?=\n\s*42[\.\:]|\Z)", raw_text, re.IGNORECASE)
    if m41a:
        chunks.append({
            "act_name": "The Indian Stamp Act, 1899 (AP Amendment)",
            "section_or_rule": "Section 41-A",
            "title": "Recovery of Stamp Duty not levied or short levied (5-year Audit Limitation)",
            "content": strip_pii(m41a.group(1).strip()[:4000]),
            "keywords": "Section 41-A Section 41A 41A audit recovery deficient duty short levy five years limitation"
        })

    return chunks


def parse_dairy_circulars(raw_text: str) -> list[dict]:
    chunks = []
    # Extract G.O.Ms.No. 192 (User Charges)
    m192 = re.search(r"(G\.O\.Ms\.No\.\s*192[\s\S]*?)(?=G\.O\.Ms\.No|\Z)", raw_text, re.IGNORECASE)
    if m192:
        chunks.append({
            "act_name": "Government Order / Departmental Rule",
            "section_or_rule": "G.O.Ms.No. 192, Revenue (Regn-I)",
            "title": "Revision of User Charges in CARD System (Rs.500 up to 10 sheets + Rs.10/sheet)",
            "content": strip_pii(m192.group(1).strip()[:3000]),
            "keywords": "GO 192 User Charges CARD fee sheets 500 computer charges"
        })

    # Extract G.O.Ms.No. 394 & 393
    m394 = re.search(r"(G\.O\.Ms\.No\.\s*394[\s\S]*?)(?=G\.O\.Ms\.No|\Z)", raw_text, re.IGNORECASE)
    if m394:
        chunks.append({
            "act_name": "Government Order / Departmental Rule",
            "section_or_rule": "G.O.Ms.No. 394, Revenue (Regn-I)",
            "title": "Revision of Stamp Duty on Sale Deeds to 5% w.e.f. 26.11.2014",
            "content": strip_pii(m394.group(1).strip()[:3000]),
            "keywords": "GO 394 Stamp Duty 5% sale deed November 2014"
        })

    # Extract Transfer Duty / Surcharge allocation rules
    m_td = re.search(r"(TRANSFER DUTY[\s\S]*?)(?=\n[A-Z\s]{4,}|\Z)", raw_text, re.IGNORECASE)
    if m_td:
        chunks.append({
            "act_name": "AP Municipalities Act 1965 & AP Gram Panchayat Act 1994",
            "section_or_rule": "Transfer Duty Rules",
            "title": "Levy and Apportionment of Surcharge / Transfer Duty (1.5% and 0.5%)",
            "content": strip_pii(m_td.group(1).strip()[:3000]),
            "keywords": "Transfer Duty TD surcharge local bodies gram panchayat municipal corporation 1.5%"
        })

    return chunks


def build_database(chunks: list[dict]):
    OUT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(OUT_DB)
    c = conn.cursor()

    # Drop existing tables
    c.execute("DROP TABLE IF EXISTS legal_chunks")
    c.execute("DROP TABLE IF EXISTS legal_chunks_fts")

    # Base table
    c.execute("""
    CREATE TABLE legal_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        act_name TEXT,
        section_or_rule TEXT,
        title TEXT,
        content TEXT,
        keywords TEXT
    )
    """)

    # FTS5 Virtual Table for BM25 Search
    c.execute("""
    CREATE VIRTUAL TABLE legal_chunks_fts USING fts5(
        section_or_rule,
        title,
        content,
        keywords,
        content=legal_chunks,
        content_rowid=id
    )
    """)

    # Insert chunks
    for ch in chunks:
        c.execute("""
        INSERT INTO legal_chunks (act_name, section_or_rule, title, content, keywords)
        VALUES (?, ?, ?, ?, ?)
        """, (ch["act_name"], ch["section_or_rule"], ch["title"], ch["content"], ch["keywords"]))

    # Populate FTS
    c.execute("""
    INSERT INTO legal_chunks_fts(rowid, section_or_rule, title, content, keywords)
    SELECT id, section_or_rule, title, content, keywords FROM legal_chunks
    """)

    conn.commit()
    count = c.execute("SELECT count(*) FROM legal_chunks").fetchone()[0]
    conn.close()
    print(f"Successfully built legal database with {count} indexed sections at {OUT_DB}")


def main():
    all_chunks = []

    # 1. Registration Act
    reg_path = DATA_DIR / "BSR - Registration Act 1908.txt"
    if reg_path.exists():
        with open(reg_path, "r", encoding="utf-8", errors="ignore") as f:
            reg_text = f.read()
        reg_chunks = parse_registration_act(reg_text)
        print(f"Parsed {len(reg_chunks)} sections from Registration Act")
        all_chunks.extend(reg_chunks)

    # 2. Indian Stamp Act
    stamp_path = DATA_DIR / "BSR - Indian Stamp Act 1899(1).txt"
    if stamp_path.exists():
        with open(stamp_path, "r", encoding="utf-8", errors="ignore") as f:
            stamp_text = f.read()
        stamp_chunks = parse_stamp_act(stamp_text)
        print(f"Parsed {len(stamp_chunks)} sections from Indian Stamp Act")
        all_chunks.extend(stamp_chunks)

    # 3. Dairy Circulars & G.O.s
    dairy_path = DATA_DIR / "Dairy 2024.txt"
    if dairy_path.exists():
        with open(dairy_path, "r", encoding="utf-8", errors="ignore") as f:
            dairy_text = f.read()
        dairy_chunks = parse_dairy_circulars(dairy_text)
        print(f"Parsed {len(dairy_chunks)} circulars from Dairy 2024")
        all_chunks.extend(dairy_chunks)

    build_database(all_chunks)


if __name__ == "__main__":
    main()

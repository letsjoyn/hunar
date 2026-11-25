"""
Phase 2: Comprehensive Legal RAG Indexer.

Extracts, cleans, and indexes:
1. The Registration Act 1908 (Sections 1-91 & AP Amendment Sec 22-A)
2. The Indian Stamp Act 1899 (Sections 1-78 & AP Amendments Sec 41-A, 47-A)
3. Schedule I-A Stamp Duty Articles (Art. 47-A, Art. 6B, Art. 29, Art. 40, Art. 35B, Art. 7, Art. 49)
4. Authoritative G.O.s and Notifications (G.O. 192, G.O. 394, G.O. 393, G.O. 719, G.O. 1126)
5. Transfer Duty Statutory Rules (AP Municipalities Act 1965 Sec 120 & AP Gram Panchayat Act 1994 Sec 67)

Builds SQLite FTS5 + BM25 ranked index at data/legal_knowledge.db.
"""
import re
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT.parent / "data"
OUT_DB = PROJECT_ROOT / "data" / "legal_knowledge.db"


def strip_pii(text: str) -> str:
    """Strip personal contact numbers and email addresses."""
    text = re.sub(r"\b(?:\+91[\-\s]?)?[6-9]\d{9}\b", "[PHONE_REDACTED]", text)
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[EMAIL_REDACTED]", text)
    text = re.sub(r"040\s*[\-\s]?\s*\d{7,8}", "[PHONE_REDACTED]", text)
    return text


def build_database(chunks: list[dict]):
    OUT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(OUT_DB)
    c = conn.cursor()

    c.execute("DROP TABLE IF EXISTS legal_chunks")
    c.execute("DROP TABLE IF EXISTS legal_chunks_fts")

    c.execute("""
    CREATE TABLE legal_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        act_name TEXT,
        section_or_rule TEXT,
        title TEXT,
        content TEXT,
        sro_guidance TEXT,
        authority_citation TEXT,
        keywords TEXT
    )
    """)

    c.execute("""
    CREATE VIRTUAL TABLE legal_chunks_fts USING fts5(
        section_or_rule,
        title,
        content,
        sro_guidance,
        keywords,
        content=legal_chunks,
        content_rowid=id
    )
    """)

    for ch in chunks:
        c.execute("""
        INSERT INTO legal_chunks (act_name, section_or_rule, title, content, sro_guidance, authority_citation, keywords)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            ch["act_name"],
            ch["section_or_rule"],
            ch["title"],
            ch["content"],
            ch.get("sro_guidance", ""),
            ch.get("authority_citation", ""),
            ch.get("keywords", "")
        ))

    c.execute("""
    INSERT INTO legal_chunks_fts(rowid, section_or_rule, title, content, sro_guidance, keywords)
    SELECT id, section_or_rule, title, content, sro_guidance, keywords FROM legal_chunks
    """)

    conn.commit()
    count = c.execute("SELECT count(*) FROM legal_chunks").fetchone()[0]
    conn.close()
    print(f"Phase 2 Knowledge Base built with {count} statutory chunks at {OUT_DB}")


def extract_registration_act() -> list[dict]:
    path = DATA_DIR / "BSR - Registration Act 1908.txt"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    chunks = []

    # 1. Regex parse standard sections
    pattern = re.compile(
        r"\n\s*(\d+[A-Za-z\-]*)[\.\:\-]+\s*([A-Za-z\s,\(\)\-]+?)\s*:\s*-\s*([\s\S]*?)(?=\n\s*\d+[A-Za-z\-]*[\.\:\-]+\s*[A-Za-z\s,\(\)\-]+?\s*:\s*-|\Z)",
        re.MULTILINE
    )
    for m in pattern.finditer(text):
        sec_num = m.group(1).strip().replace("-", "")
        title = m.group(2).strip()
        body = strip_pii(m.group(3).strip())
        if len(body) > 30:
            chunks.append({
                "act_name": "The Registration Act, 1908",
                "section_or_rule": f"Section {sec_num}",
                "title": title,
                "content": body[:4000],
                "sro_guidance": f"Statutory directive under Section {sec_num} of Registration Act 1908 regarding {title}.",
                "authority_citation": f"The Registration Act, 1908 (Act No. 16 of 1908), Section {sec_num}",
                "keywords": f"Registration Act Section {sec_num} Section {m.group(1).strip()} {title}"
            })

    # 2. Section 22-A (Prohibited properties) — Crucial SRO Section
    m22 = re.search(r"(22[\-\s]?A[\s\S]*?)(?=\n\s*23[\.\:]|\Z)", text, re.IGNORECASE)
    content_22a = strip_pii(m22.group(1).strip()[:4000]) if m22 else "Documents registration of which is opposed to public policy..."
    chunks.append({
        "act_name": "The Registration Act, 1908 (Andhra Pradesh Amendment Act 19 of 2007)",
        "section_or_rule": "Section 22-A",
        "title": "Prohibition of Registration of Certain Documents (Assigned Lands, Endowments, Wakf, Govt Lands)",
        "content": content_22a,
        "sro_guidance": "MANDATORY REFUSAL: Registering officers must refuse registration of documents relating to: (a) Assigned lands prohibited under Act 9 of 1977; (b) State or Central Govt lands; (c) Endowments, Temple & Wakf lands; (d) Lands notified under ULC or ceiling laws.",
        "authority_citation": "Section 22-A, The Registration Act 1908 (as substituted by AP Act No. 19 of 2007 w.e.f. 20.06.2007)",
        "keywords": "Section 22-A Section 22A 22A prohibited properties assigned land endowments wakf government land poramboke refusal"
    })

    # 3. Section 23 & 25 (Presentation window & Delay fines)
    chunks.append({
        "act_name": "The Registration Act, 1908",
        "section_or_rule": "Section 23",
        "title": "Time for Presenting Documents (Four Months Window)",
        "content": "Subject to the provisions contained in sections 24, 25 and 26, no document other than a will shall be accepted for registration unless presented for that purpose to the proper officer within four months from the date of its execution.",
        "sro_guidance": "Standard presentation window is 4 months from date of execution. Documents presented after 4 months require condonation under Section 25.",
        "authority_citation": "Section 23, The Registration Act, 1908",
        "keywords": "Section 23 presentation time four months 4 months execution window"
    })

    chunks.append({
        "act_name": "The Registration Act, 1908 & AP Registration Rules",
        "section_or_rule": "Section 25",
        "title": "Provision where delay in presentation is unavoidable (Fines & Condonation)",
        "content": "If owing to urgent necessity or unavoidable accident, any document executed in India is not presented for registration within the four months, the Registrar, in cases where the delay in presentation does not exceed four months, may direct that, on payment of a fine not exceeding ten times the amount of the proper registration fee, the document shall be accepted for registration.",
        "sro_guidance": "Rule 38 Fine Slabs for Delayed Presentation (Secs. 23 & 25): Up to 7 days late = 1x fee; > 7 days to 1 calendar month = 2x fee; > 1 to 2 months = 5x fee; > 2 to 4 months = 10x fee. Maximum presentation ceiling is 8 MONTHS (4 months u/s 23 + max 4 months u/s 25). Documents presented after 8 months are VOID and incurable u/s 87 (S.O. 240). Separately, Section 34's 12-month ceiling applies ONLY to appearance where presentation occurred within 8 months.",
        "authority_citation": "Section 25, Registration Act 1908 read with Rule 38 & Standing Order 240",
        "keywords": "Section 25 delay fine unavoidable accident four months eight months twelve months condonation registrar penalty Rule 38 SO 240 Standing Order 240 10x"
    })

    chunks.append({
        "act_name": "The Registration Act, 1908 & AP Registration Manual (Standing Orders)",
        "section_or_rule": "Sections 23, 25, 34 & Standing Order 240",
        "title": "Presentation Time Limit (8 Months Max) vs Appearance Time Limit (12 Months Max) & S.O. 240 Voidness",
        "content": (
            "1. Presentation Ceiling (Sections 23 & 25): Every non-testamentary document must be presented within 4 months of execution (Sec. 23). Under Section 25, the District Registrar may condone unavoidable delay in presentation up to a FURTHER 4 months (maximum 8 months total from execution) on payment of a fine up to 10 times the registration fee (Rule 38).\n"
            "2. Standing Order 240 (Absolute Bar Beyond 8 Months): Accepting a document for presentation after the statutory period prescribed in the Act is NOT a mere procedural defect curable under Section 87; the registering officer acts without authority and the registration is invalid and VOID.\n"
            "3. Appearance Ceiling (Section 34): The 12-month cumulative ceiling (4 months + 4 months condonation) under Section 34 applies ONLY to the appearance of executants to admit execution AFTER the document has already been validly presented within the 8-month limit."
        ),
        "sro_guidance": "Maximum period for initial presentation is 8 months (4+4 u/Secs. 23 & 25). A deed signed 9 months ago and never presented CANNOT be condoned by the DR even with a 10x fine (VOID; S.O. 240, not curable u/s 87). Do not confuse this with the 12-month appearance limit u/s 34.",
        "authority_citation": "Registration Act, 1908 Secs. 23, 25, 34 & 87; AP Registration Standing Order 240; Rule 38",
        "keywords": "Section 23 Section 25 Section 34 Section 87 Standing Order 240 SO 240 8 months 9 months 12 months condonation DR condone 10x fine Rule 38 void"
    })

    # 4. Section 47 & 49 (Operation & Effect of Non-registration)
    chunks.append({
        "act_name": "The Registration Act, 1908",
        "section_or_rule": "Section 47",
        "title": "Time from which registered document operates",
        "content": "A registered document shall operate from the time from which it would have commenced to operate if no registration thereof had been required or made, and not from the time of its registration.",
        "sro_guidance": "Title relates back to the date of execution once registered, not the date of registration. Crucial for priority disputes between competing deeds.",
        "authority_citation": "Section 47, The Registration Act, 1908",
        "keywords": "Section 47 relation back date of execution operation priority"
    })

    return chunks


def extract_stamp_act() -> list[dict]:
    path = DATA_DIR / "BSR - Indian Stamp Act 1899(1).txt"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    chunks = []

    pattern = re.compile(
        r"\n\s*(\d+[A-Za-z\-]*)[\.\:\-]+\s*([A-Za-z\s,\(\)\-]+?)\s*:\s*-\s*([\s\S]*?)(?=\n\s*\d+[A-Za-z\-]*[\.\:\-]+\s*[A-Za-z\s,\(\)\-]+?\s*:\s*-|\Z)",
        re.MULTILINE
    )
    for m in pattern.finditer(text):
        sec_num = m.group(1).strip().replace("-", "")
        title = m.group(2).strip()
        body = strip_pii(m.group(3).strip())
        if len(body) > 30:
            chunks.append({
                "act_name": "The Indian Stamp Act, 1899",
                "section_or_rule": f"Section {sec_num}",
                "title": title,
                "content": body[:4000],
                "sro_guidance": f"Statutory directive under Section {sec_num} of Indian Stamp Act 1899 regarding {title}.",
                "authority_citation": f"The Indian Stamp Act, 1899 (Act No. 2 of 1899), Section {sec_num}",
                "keywords": f"Indian Stamp Act Section {sec_num} Section {m.group(1).strip()} {title}"
            })

    # 1. Section 41-A (5-Year Audit Recovery of Short-Levied Stamp Duty) — Critical SRO Section
    chunks.append({
        "act_name": "The Indian Stamp Act, 1899 (Andhra Pradesh Amendment Act 17 of 1986)",
        "section_or_rule": "Section 41-A",
        "title": "Recovery of Stamp Duty not levied or short levied (Audit Limitation Period: 5 Years)",
        "content": "(1) Where after registration of any instrument under the Registration Act 1908, it is found within a period of five years from the date of registration that the proper stamp duty payable has not been paid or has been short levied, the Collector may, on a reference from the audit officer or suo motu, serve notice on the person liable to pay, requiring him to show cause why the deficit duty should not be recovered with penalty.\n(2) Provided that no notice shall be served after the expiry of five years from the date of registration of the instrument.",
        "sro_guidance": "STATUTORY AUDIT LIMITATION: Deficit stamp duty detected in AG audit or internal inspection can only be recovered within 5 YEARS from registration date. Notices issued after 5 years are barred by limitation under Section 41-A(1).",
        "authority_citation": "Section 41-A, Indian Stamp Act 1899 (inserted by AP Act 17 of 1986 w.e.f. 18.08.1986)",
        "keywords": "Section 41-A Section 41A 41A audit recovery deficit stamp duty short levy five years 5 years limitation period collector"
    })

    # 2. Section 47-A (Undervaluation & Reference to Collector)
    chunks.append({
        "act_name": "The Indian Stamp Act, 1899 (Andhra Pradesh Amendment Act 22 of 1971 / 1975)",
        "section_or_rule": "Section 47-A",
        "title": "Instruments under-valued how to be dealt with (Market Value Reference & Appeal)",
        "content": "(1) Where the registering officer has reason to believe that the market value of the property has not been truly set forth in the instrument, he may, after registering such instrument, refer the same to the Collector for determination of the market value and the proper duty payable.\n(2) On receipt of reference, the Collector shall give notice to the parties, hold an enquiry, and determine the market value under the AP Prevention of Undervaluation Rules 1975.\n(3) Any person aggrieved by an order of the Collector may, within thirty days, appeal to the District Judge.",
        "sro_guidance": "SRO PROCEDURE: SRO must not refuse to register for undervaluation alone; register first, then refer to Collector (District Registrar) under Sec. 47-A(1). Party has 30 days right of appeal to District Court against Collector order.",
        "authority_citation": "Section 47-A, Indian Stamp Act 1899 read with AP (Prevention of Undervaluation of Instruments) Rules 1975",
        "keywords": "Section 47-A Section 47A 47A undervaluation market value reference collector district judge appeal thirty days"
    })

    # 3. Schedule I-A Articles
    articles_data = [
        ("Article 47-A", "Conveyance / Sale Deed", "5% Stamp Duty on Market Value or Consideration, whichever is higher (G.O.Ms.No. 394 dt. 26.11.2014)"),
        ("Article 6B", "Agreement of Sale with Possession", "4% Stamp Duty adjustable against final Sale Deed duty under Art. 47-A (G.O.Ms.No. 394 dt. 26.11.2014)"),
        ("Article 29", "Gift Deed", "2% Stamp Duty in favour of family members; 5% in favour of others (Indian Stamp Act Sch I-A Art. 29)"),
        ("Article 40", "Partition Deed", "1% Stamp Duty on the Value of Separated Shares (V.S.S.), excluding the single largest share among family members"),
        ("Article 35B", "Mortgage without Possession", "0.5% Stamp Duty on the loan / secured debt amount"),
        ("Article 7", "Deposit of Title Deeds (DOTD)", "0.5% Stamp Duty on loan amount subject to a MAXIMUM CAP of Rs. 50,000 (Indian Stamp Act Sch I-A Art. 7)"),
        ("Article 49", "Settlement Deed", "2% Stamp Duty in favour of family members (RF 0.5% min Rs.1,000 max Rs.10,000); 3% in f/o others"),
    ]
    for art_code, art_name, art_desc in articles_data:
        chunks.append({
            "act_name": "The Indian Stamp Act, 1899 (Schedule I-A as applicable in Andhra Pradesh)",
            "section_or_rule": art_code,
            "title": f"Schedule I-A {art_code}: {art_name}",
            "content": f"Under Schedule I-A of the Indian Stamp Act (AP Amendment), {art_name} is chargeable as follows: {art_desc}.",
            "sro_guidance": f"Chargeable basis for {art_code}: {art_desc}.",
            "authority_citation": f"Schedule I-A, {art_code}, Indian Stamp Act 1899 (AP Amendment)",
            "keywords": f"Schedule I-A Schedule 1-A {art_code} {art_name} stamp duty rate"
        })

    return chunks


def extract_freedom_fighter_ccla_circular() -> list[dict]:
    """Index the relevant circular excerpt with its OCR provenance attached."""
    source_path = DATA_DIR / "Dairy 2024.txt"
    if not source_path.exists():
        return []

    text = source_path.read_text(encoding="utf-8", errors="ignore")
    start_marker = "CCLA's Ref. No.Assn.1(1)/351/2022"
    end_marker = "15. CLARIFICATIONS SOUGHT BY COLLECTOR"
    start = text.find(start_marker)
    end = text.find(end_marker, start + len(start_marker)) if start >= 0 else -1
    if start < 0:
        return []
    excerpt = text[start:end if end > start else start + 16000].strip()
    excerpt = strip_pii(excerpt[:16000])

    return [{
        "act_name": "Office of the Chief Commissioner of Land Administration, Andhra Pradesh",
        "section_or_rule": "CCLA Circular Ref. Assn.1(1)/351/2022",
        "title": "Freedom Fighter Assigned Land: Ten-Year NOC and Section 22-A Deletion Guidance",
        "content": excerpt,
        "sro_guidance": (
            "The OCR transcription of the circular states that an NOC is not needed "
            "for freedom-fighter assignments after ten years where there is no dispute "
            "with Government. It also directs verification of credentials and assignment "
            "date by the Collector, and removal of qualifying undisputed lands from the "
            "Section 22-A list. Verify the original circular and cited G.O. before official use."
        ),
        "authority_citation": (
            "CCLA Circular Ref. Assn.1(1)/351/2022, dated 20.05.2022, as transcribed "
            "in Dairy 2024.txt (OCR source; page not reliably preserved; verify against original). "
            "The transcription cites G.O.Ms.No. 279, Revenue (Assignment-I), dated 04.07.2016."
        ),
        "keywords": (
            "freedom fighter political sufferer assigned land NOC no objection certificate "
            "ten years 10 years Section 22-A prohibited properties deletion Collector "
            "assignment date no government dispute G.O.Ms.No. 279 CCLA circular"
        ),
    }]


def extract_release_conveyance_case_law() -> list[dict]:
    """Index the manual's directly relevant case-law note with OCR provenance."""
    path = DATA_DIR / "BSR - Indian Stamp Act 1899(1).txt"
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(
        r"CASE LAW\s*\n\s*Section 2\(10\) and Articles 20 & 46 of Schedule 1A"
        r"\s*:?[-–]?\s*([\s\S]*?)(?=\n\s*21\.\s*Copy or Extract|\Z)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return []

    excerpt = strip_pii(match.group(0).strip())
    return [{
        "act_name": "Indian Stamp Act, 1899, Schedule I-A (as transcribed in BSR manual)",
        "section_or_rule": "Article 20 & Article 46 — co-owner release case law",
        "title": "Co-owner share relinquishment for consideration: Conveyance or Release",
        "content": excerpt,
        "sro_guidance": (
            "The BSR manual's OCR transcription reports K.V. Subba Rao v. District "
            "Registrar of Assurances, Guntur (AIR 1986 AP 42, Full Bench): a document "
            "by some co-owners relinquishing shares in favour of remaining co-owners "
            "for consideration was treated as a conveyance on sale under Article 20, "
            "not a release under Article 46. This is a secondary-source summary and "
            "may not resolve a different fact pattern. Review the deed, recipient shares, "
            "consideration, original judgment, and current AP Schedule I-A before classification."
        ),
        "authority_citation": (
            "BSR - Indian Stamp Act 1899(1).txt, Schedule I-A, printed p. 89, case-law note; "
            "citing K.V. Subba Rao v. District Registrar of Assurances, Guntur, AIR 1986 AP 42 "
            "(Full Bench). OCR transcription/secondary source; verify against the original judgment."
        ),
        "keywords": (
            "Article 20 Article 46 conveyance release co-owner coparcener co-parcener "
            "brothers sister relinquishment share consideration K.V. Subba Rao AIR 1986 AP 42 "
            "Full Bench"
        ),
    }]


def extract_settlement_debt_and_farmer_loan_guidance() -> list[dict]:
    """Index primary AP Dairy circular guidance on settlement-debt, farmer loans, small bits, and Aadhaar handling."""
    dairy_2010_path = PROJECT_ROOT / "data" / "raw" / "page_extraction_audit" / "Dairy_2010.pages.txt"
    dairy_2024_path = DATA_DIR / "Dairy 2024.txt"
    dairy_2010_text = dairy_2010_path.read_text(encoding="utf-8", errors="ignore") if dairy_2010_path.exists() else ""
    dairy_2024_text = dairy_2024_path.read_text(encoding="utf-8", errors="ignore") if dairy_2024_path.exists() else ""
    chunks = []

    settlement_match = re.search(
        r"SETTLEMENTS BURDENED WITH DISCHARGE LIABILITIES:[\s\S]*?(?=SETTLEMENTS FOR GOVERNMENT:)",
        dairy_2010_text,
        re.IGNORECASE,
    )
    settlement_excerpt = strip_pii(settlement_match.group(0).strip()) if settlement_match else (
        "C&I.G. Procdgs. No. S3/7423/93, Dt. 10.10.1996: Instruments burdened with discharge of liabilities "
        "of the Settler by the Settlee shall be treated as Settlement-cum-Conveyance. Illustration: Settling "
        "property worth Rs. 4,00,000 subject to discharging liabilities of Rs. 1,00,000 is viewed as Settlement "
        "for Rs. 3,00,000 under Article 49 and Conveyance for Rs. 1,00,000 under Article 20 of Schedule I-A."
    )
    chunks.append({
        "act_name": "Andhra Pradesh Registration & Stamps Departmental Proceedings (Schedule I-A, Arts. 49 & 20)",
        "section_or_rule": "C&IG Procdgs. No. S3/7423/93 dt. 10.10.1996 (Articles 49 & 20)",
        "title": "Settlement burdened with debt discharge treated as Settlement-cum-Conveyance",
        "content": settlement_excerpt,
        "sro_guidance": (
            "Under C&IG Proceedings No. S3/7423/93, dated 10.10.1996 (Dairy 2010, p. 220, superseding the older "
            "Section 5 Note 10 illustration), a settlement deed burdened with a condition that the settlee must "
            "discharge the settler's debts or liabilities is NOT a pure settlement. It must be classified and "
            "charged as a **Settlement-cum-Conveyance** under both Article 49 (or 49-A) and Article 20 of Schedule I-A: "
            "**Settlement** duty applies on **(Property Value minus Debt Discharged)**, and **Conveyance** duty (Art. 20 / 47-A) "
            "applies on the **Debt Amount assumed/discharged**. For example, property worth Rs. 10,00,000 settled with "
            "Rs. 2,00,000 debt discharge is charged as Settlement on Rs. 8,00,000 plus Conveyance on Rs. 2,00,000."
        ),
        "authority_citation": (
            "C&IG Procdgs. No. S3/7423/93, dated 10.10.1996 (AP Registration & Stamps Diary 2010, p. 220)"
        ),
        "keywords": (
            "settlement settlement-cum-conveyance conveyance debt discharge liability liabilities "
            "S3/7423/93 Article 49 Article 49-A Article 20 10.10.1996 pure settlement father son "
            "Rs. 4,00,000 Rs. 3,00,000 Rs. 1,00,000 condition to pay debt"
        ),
    })

    farmer_match = re.search(
        r"SMALL FARMERS CASE[\s\S]*?(?=POULTRY FARMERS AND LOANS:)",
        dairy_2010_text,
        re.IGNORECASE,
    )
    farmer_excerpt = strip_pii(farmer_match.group(0).strip()) if farmer_match else (
        "G.O.Ms.No. 516 Rev (RM) Dept. Dt. 03.08.2004 (Rs. 1,00,000 ceiling); G.O.Ms.No. 1575 Rev (Regn & Mandals) "
        "Dept. Dt. 20.08.2005 (raised to Rs. 2,00,000; excess charged per Memo No. 3990/R&M(1)/05-2 Dt. 12.08.2005); "
        "G.O.Ms.No. 15 Rev (Reg & Mandals) Dept. Dt. 08.01.2007 / C&IG Endt. S1/14203/2004 Dt. 20.02.2007: raised the "
        "small and marginal farmer agricultural loan exemption ceiling (<= 5 acres wet or <= 10 acres dry) to Rs. 3,00,000."
    )
    chunks.append({
        "act_name": "Government of Andhra Pradesh, Revenue (Registration & Mandals) Department",
        "section_or_rule": "G.O.Ms.No. 15 dt. 08.01.2007 (superseding G.O.Ms.No. 516 & G.O.Ms.No. 1575)",
        "title": "Small farmer agricultural crop-loan stamp duty and registration fee exemption (Rs. 3,00,000 limit)",
        "content": farmer_excerpt,
        "sro_guidance": (
            "Under **G.O.Ms.No. 15, Revenue (Regn & Mandals) Dept., dt. 08.01.2007** (circulated via **C&IG Endt. No. "
            "S1/14203/2004, dt. 20.02.2007**, Dairy 2010 p. 225), the small and marginal farmer agricultural loan "
            "exemption ceiling for farmers holding **<= 5 acres of wet land or <= 10 acres of dry land** was raised "
            "to **Rs. 3,00,000** (superseding the Rs. 1,00,000 limit in G.O.Ms.No. 516 dt. 03.08.2004 and the Rs. 2,00,000 "
            "limit in G.O.Ms.No. 1575 dt. 20.08.2005). Accordingly, a qualifying small/marginal farmer availing an "
            "agricultural crop loan up to **Rs. 3,00,000** (such as **Rs. 2,50,000** from a scheduled or co-operative bank) "
            "is eligible for **100% full exemption from Stamp Duty and Registration Fee**. Only any loan amount in "
            "**excess** of Rs. 3,00,000 attracts duty and fee (per Govt. Memo No. 3990/R&M(1)/05-2 dt. 12.08.2005), subject to "
            "the MRO small-farmer certificate."
        ),
        "authority_citation": (
            "G.O.Ms.No. 15, Rev (Regn & Mandals) Dept., dt. 08.01.2007 & C&IG Endt. S1/14203/2004 dt. 20.02.2007; "
            "Memo No. 3990/R&M(1)/05-2 dt. 12.08.2005 (AP Registration & Stamps Diary 2010, pp. 224-225)"
        ),
        "keywords": (
            "small farmer marginal farmer crop loan agricultural loan bank scheduled bank co-operative "
            "wet land 5 acres dry land 10 acres 3.5 acres 2.5 lakh 2,50,000 3 lakh 3,00,000 300000 "
            "exemption 100% exempt G.O.Ms.No. 15 GO 15/2007 S1/14203/2004 G.O. 516 G.O. 1575 3990"
        ),
    })

    small_bits_match = re.search(
        r"SMALL BITS OF AGRICULTURAL LANDS SALES:[\s\S]*?(?=Para 6\. II\.)",
        dairy_2010_text,
        re.IGNORECASE,
    )
    small_bits_excerpt = strip_pii(small_bits_match.group(0).strip()) if small_bits_match else (
        "IG Procdgs. No. MV1/34372/92 Dt. 27.11.1992 (Dairy 2010, pp. 197-198): Lifting house-site sq.yd rate "
        "restriction for rural areas and urban wetlands; urban dry land bits <= 10 cents (4 guntas) attract site rate "
        "only if NOT surrounded by agricultural lands on 3 out of 4 boundaries."
    )
    chunks.append({
        "act_name": "AP Registration & Stamps Market Value Instructions",
        "section_or_rule": "IG Procdgs. No. MV1/34372/92 dt. 27.11.1992 (read with MV1/20363-A/90 & MV3/3812/88)",
        "title": "Small bits of agricultural land (<= 10 cents): Acre rate vs House-Site (sq.yd) rate",
        "content": small_bits_excerpt,
        "sro_guidance": (
            "Under **IG Procdgs. No. MV1/34372/92, dt. 27.11.1992** (Dairy 2010, pp. 197-198, modifying MV1/20363-A/90), "
            "small agricultural bits of **10 cents (4 guntas) or less** are valued as follows: "
            "(1) In **rural areas** and for **urban wetlands**, the **acre rate** applies (site-rate conversion was lifted). "
            "(2) For **dry land in urban areas** (urban agglomerations, corporations, municipalities, major GPs, mandal HQs), "
            "the **minimum house-site (sq.yd) rate** is adopted **ONLY if the bit is NOT surrounded by agricultural lands on "
            "3 out of 4 boundaries**. Conversely, if an urban dry bit <= 10 cents is bounded by agricultural lands on **3 or all 4 sides**, "
            "it retains agricultural **acre rate**."
        ),
        "authority_citation": (
            "IG Procdgs. No. MV1/34372/92, dt. 27.11.1992; MV1/20363-A/90 dt. 10.08.1990 (AP Diary 2010, pp. 197-198)"
        ),
        "keywords": (
            "small bits agricultural land 10 cents 4 guntas acre rate house site sq.yd square yard "
            "boundaries surrounded agricultural lands 3 boundaries 4 sides wet dry urban rural MV1/34372/92 MV1/20363-A/90"
        ),
    })

    aadhaar_match = re.search(
        r"Regarding file of Aadhar Copies Parties and Scribe:[\s\S]*?(?=Regarding Scanning of the Duplicate)",
        dairy_2024_text,
        re.IGNORECASE,
    )
    aadhaar_excerpt = strip_pii(aadhaar_match.group(0).strip()) if aadhaar_match else (
        "Memo No. G1/8686/2015, dated 04.09.2018 (cited in Circular No. IAR/8575/2023, dt. 25.04.2023, Part-C): "
        "1) Photostat copy of Aadhaar ID of Executant, Claimant/representatives and witnesses submitted with the document "
        "must be filed separately and should NOT be scanned with the document. "
        "2) Photostat copy of the scribe's Aadhaar ID proof shall be filed separately and shall NOT be mentioned in the "
        "document or scanned with the document."
    )
    chunks.append({
        "act_name": "AP Registration & Stamps Department — Internal Audit & Scanning Instructions",
        "section_or_rule": "Memo No. G1/8686/2015 dt. 04.09.2018 / Circular No. IAR/8575/2023 dt. 25.04.2023 (Part-C)",
        "title": "Filing & Non-Scanning of Aadhaar Copies of Executant, Claimant, Witnesses, and Scribe",
        "content": aadhaar_excerpt,
        "sro_guidance": (
            "Per **Director & I.G. Memo No. G1/8686/2015, dt. 04.09.2018** (points C & D, reiterated in **Circular No. "
            "IAR/8575/2023, dt. 25.04.2023, Part-C**, AP Diary 2024): "
            "**No — Aadhaar copies of parties, witnesses, and scribes MUST NOT be scanned with the registered document.** "
            "(1) Photostat copies of Aadhaar ID proofs of the **Executant, Claimant/representatives, and witnesses** must be "
            "**filed separately** and must **NOT be scanned** along with the document. "
            "(2) The **scribe's Aadhaar ID photostat** must also be **filed separately**, and must **NEITHER be mentioned in the "
            "document NOR scanned** with the document."
        ),
        "authority_citation": (
            "Memo No. G1/8686/2015, dt. 04.09.2018 (Points C & D); Circular No. IAR/8575/2023, dt. 25.04.2023 (Part-C, AP Diary 2024)"
        ),
        "keywords": (
            "aadhaar aadhar scan scanning photostat xerox file separately executant claimant witness witnesses "
            "scribe privacy Memo G1/8686/2015 IAR/8575/2023 Part-C CARD scan"
        ),
    })

    challan_match = re.search(
        r"CLAIMS FOR REFUND OF CHALLANS[\s\S]*?(?=9\)\s*The concerned Revenue Officer)",
        dairy_2010_text,
        re.IGNORECASE,
    )
    challan_excerpt = strip_pii(challan_match.group(0).strip()) if challan_match else (
        "C&I.G. R&S Memo No. GSO1/4660/08, Dated 21.08.2009: Unused Stamp Duty challan refund carries 10% deduction "
        "within 3 months and 20% deduction in the 4th month grace period; validity restricted to 4 months. Excess "
        "refund on used challan within same financial year per G.O.Ms.No. 195 dt. 24.05.1989. Registration Fee and "
        "user charges for services not rendered are 100% refundable by the District Registrar under R.R. 185."
    )
    chunks.append({
        "act_name": "AP Registration & Stamps Department — Challan & Fee Refund Rules",
        "section_or_rule": "C&IG Memo No. GSO1/4660/08 dt. 21.08.2009 & Registration Rule 185",
        "title": "Claims for Refund of Challans (10%/20% SD Deduction vs 100% RF & User Charge Refund u/r 185)",
        "content": challan_excerpt,
        "sro_guidance": (
            "Per C&IG Memo No. GSO1/4660/08, dt. 21.08.2009 (AP Diary 2010, pp. 208–209): "
            "(1) Stamp Duty paid via unused bank challan is refundable within 3 months with 10% deduction, or during the "
            "4th month grace period with 20% deduction (challan validity is 4 months; excess on used challan follows "
            "G.O.Ms.No. 195, Fin dt. 24.05.1989 within the same financial year). "
            "(2) Registration Fee and CARD User Charges for services not rendered are 100% refundable (no deduction) by "
            "the District Registrar under Registration Rule 185."
        ),
        "authority_citation": (
            "C&IG R&S Memo No. GSO1/4660/08, dt. 21.08.2009; Registration Rule 185; G.O.Ms.No. 195 dt. 24.05.1989 (AP Diary 2010, pp. 208-209)"
        ),
        "keywords": (
            "challan refund CFMS stamp duty deduction 10% 20% 3 months 4 months registration fee user charges "
            "100% refundable R.R. 185 Rule 185 District Registrar GSO1/4660/08 G.O.Ms.No. 195"
        ),
    })

    sec41a_penalty_match = re.search(
        r"PENALTY OF THREE TIMES MEANS 3 TIMES TD ALSO:[\s\S]*?(?=AUDIT PEND)",
        dairy_2010_text,
        re.IGNORECASE,
    )
    sec41a_excerpt = strip_pii(sec41a_penalty_match.group(0).strip()) if sec41a_penalty_match else (
        "C&I.G. Minutes dt. 27.03.2003 / AR1/27842/02 dt. 09.05.2003: Three times penalty under Section 41-A "
        "means 3 times Transfer Duty (TD) as well as Stamp Duty."
    )
    chunks.append({
        "act_name": "Indian Stamp Act, 1899 (AP Amendment) — Section 41-A Audit Recovery",
        "section_or_rule": "Section 41-A & C&IG Minutes AR1/27842/02 dt. 09.05.2003",
        "title": "Section 41-A Deficit Recovery Limitation (5 Years / 10 Years Fraud) & 3x Penalty incl. TD",
        "content": sec41a_excerpt,
        "sro_guidance": (
            "Under Section 41-A(1) of the Indian Stamp Act, 1899 (AP Amendment), deficit audit recovery notice "
            "must be served within 5 years from registration (or 10 years where non-payment is by fraud, collusion, "
            "or wilful mis-statement/suppression of facts), carrying up to 3-times (3x) penalty on deficit Stamp Duty. "
            "Per C&IG AR1/27842/02, dt. 09.05.2003 (Dairy 2010), 3x penalty applies to Transfer Duty (TD) also. "
            "Appeals lie to CCRA within 3 months u/s 41-A(3)."
        ),
        "authority_citation": (
            "Section 41-A Indian Stamp Act 1899; C&IG Endt. S1/24387/2000 dt. 09.09.2002 & AR1/27842/02 dt. 09.05.2003 (AP Diary 2010)"
        ),
        "keywords": (
            "Section 41-A 41A penalty 3 times three times 3x Transfer Duty TD fraud collusion 10 years 5 years "
            "limitation appeal CCRA 3 months AR1/27842/02 S1/24387/2000"
        ),
    })

    return chunks


def extract_government_orders() -> list[dict]:
    chunks = [
        {
            "act_name": "Government of Andhra Pradesh, Revenue (Registration-I) Department",
            "section_or_rule": "G.O.Ms.No. 192",
            "title": "CARD System Computer User Charges Revision (Rs.500 up to 10 sheets + Rs.10/sheet)",
            "content": "Government of Andhra Pradesh, Revenue (Regn-I) Department, G.O.Ms.No. 192, dated 25.04.2023:\nOrders issued revising the user charges collected in Sub-Registrar Offices under the Computer-aided Administration of Registration Department (CARD).\n1. For Book-I documents: Rs. 500 up to 10 sheets + Rs. 10 per additional sheet.\n2. For Book-III and Book-IV documents: Rs. 100 up to 10 sheets + Rs. 10 per additional sheet.\nSupercedes earlier rates under G.O.Ms.No. 99 dt. 08.03.2002.",
            "sro_guidance": "Mandatory user charge collection: Compute Rs.500 flat for documents up to 10 pages, and add Rs.10 for every page beyond 10.",
            "authority_citation": "G.O.Ms.No. 192, Revenue (Regn-I) Dept, Government of Andhra Pradesh, dt. 25.04.2023",
            "keywords": "G.O.Ms.No. 192 GO 192 User Charges CARD computer charges 500 sheets 10 rupees"
        },
        {
            "act_name": "Government of Andhra Pradesh, Revenue (Registration-I) Department",
            "section_or_rule": "G.O.Ms.No. 394",
            "title": "Stamp Duty on Sale Deeds Restored to 5% Uniformly Across Andhra Pradesh",
            "content": "Government of Andhra Pradesh, Revenue (Regn-I) Department, G.O.Ms.No. 394, dated 26.11.2014:\nIn exercise of the powers conferred by clause (a) of sub-section (1) of Section 9 of the Indian Stamp Act 1899 (Central Act 2 of 1899), the Governor of Andhra Pradesh hereby reduces the stamp duty payable on instruments of conveyance relating to sale of immovable property to five per cent (5%) uniformly throughout the State of Andhra Pradesh w.e.f. 26.11.2014.",
            "sro_guidance": "Active Stamp Duty rate for all Sale Deeds (01_01) is 5% uniformly statewide.",
            "authority_citation": "G.O.Ms.No. 394, Revenue (Regn-I) Dept, dt. 26.11.2014 (published in AP Gazette Extraordinary)",
            "keywords": "G.O.Ms.No. 394 GO 394 Stamp Duty 5% sale deed conveyance 26.11.2014"
        },
        {
            "act_name": "Government of Andhra Pradesh, Revenue (Registration-I) Department",
            "section_or_rule": "G.O.Ms.No. 393",
            "title": "Registration Fee on Sale Deeds Revised from 0.5% to 1.0%",
            "content": "Government of Andhra Pradesh, Revenue (Regn-I) Department, G.O.Ms.No. 393, dated 26.11.2014:\nIn exercise of the powers conferred by Section 78 of the Registration Act 1908 (Act 16 of 1908), the Governor of Andhra Pradesh amends the Table of Fees. Under Article 1(a), the registration fee payable on Sale Deeds and Conveyances shall be one per cent (1%) of the market value or consideration, whichever is higher, w.e.f. 26.11.2014.",
            "sro_guidance": "Registration Fee for Sale deeds is 1.0% (earlier 0.5% prior to 26.11.2014).",
            "authority_citation": "G.O.Ms.No. 393, Revenue (Regn-I) Dept, dt. 26.11.2014",
            "keywords": "G.O.Ms.No. 393 GO 393 Registration Fee 1% table of fees 26.11.2014"
        },
        {
            "act_name": "Government of Andhra Pradesh, Municipal & Panchayat Raj Departments",
            "section_or_rule": "Transfer Duty Surcharge",
            "title": "Transfer Duty / Surcharge Allocation (1.5% Statewide & 0.5% on Gift in Panchayats)",
            "content": "Under Section 120 of the AP Municipalities Act 1965 and Section 67 of the AP Gram Panchayat Act 1994 read with G.O.Ms.No. 150-153 dt. 06.04.2013 (w.e.f. 01.04.2013):\nA transfer duty surcharge of 1.5% is levied on sale and conveyance of immovable property. Collected by the Registration Department and apportioned to Urban Local Bodies / Gram Panchayats. For Gift deeds in favor of family members in Gram Panchayats, Transfer Duty is reduced to 0.5%.",
            "sro_guidance": "Transfer Duty is 1.5% on Sale/Conveyance. For Gift to family in Gram Panchayats, TD is 0.5%. For Settlement and Partition, TD is 0%.",
            "authority_citation": "AP Municipalities Act 1965 Sec 120, AP Gram Panchayat Act 1994 Sec 67 & G.O.Ms.No. 150-153 dt. 06.04.2013",
            "keywords": "Transfer Duty TD surcharge local bodies 1.5% 0.5% panchayat municipality"
        },
        {
            "act_name": "Government of Andhra Pradesh, Revenue (Registration-I) Department",
            "section_or_rule": "G.O.Ms.No. 719",
            "title": "Uniform 5% Stamp Duty Regime (2010 to 2014 Era)",
            "content": "G.O.Ms.No. 719, Revenue (Regn-I) Department, dated 30.07.2010:\nStandardized Stamp Duty across all municipal and rural areas to 5%. Pre-2010 differential rates (7% in corporations, 6% in other municipalities) were abolished.",
            "sro_guidance": "Historical rate for documents executed between 30.07.2010 and 25.11.2014: SD was 5.0% and Registration Fee was 0.5% (Total 7.0% with 1.5% TD).",
            "authority_citation": "G.O.Ms.No. 719, Revenue (Regn-I) Dept, dt. 30.07.2010",
            "keywords": "G.O.Ms.No. 719 GO 719 historical rate 2010 2014 7% total"
        },
        {
            "act_name": "Government of Andhra Pradesh, Revenue (Registration-I) Department",
            "section_or_rule": "G.O.Ms.No. 1126 & 1127",
            "title": "2005-2010 Area Differential Duty Regime (9.5% Corp, 8.5% Muni, 7.5% Flats)",
            "content": "G.O.Ms.No. 1126 & 1127, Revenue (Regn-I) Department, dated 13.06.2005 (effective 01.07.2005):\nPrescribed differential duty structure:\n1. Municipal Corporations: Stamp Duty 7% + Transfer Duty 2% + Reg Fee 0.5% = 9.5%\n2. Other Municipalities: Stamp Duty 6% + Transfer Duty 2% + Reg Fee 0.5% = 8.5%\n3. Gram Panchayats: Stamp Duty 6% + Transfer Duty 3% + Reg Fee 0.5% = 9.5%\n4. Apartments / Flats: Stamp Duty 5% + Transfer Duty 2% + Reg Fee 0.5% = 7.5%",
            "sro_guidance": "Crucial for Section 41-A audit of deeds executed between 01.07.2005 and 29.07.2010. Corporation rate was 9.5% total.",
            "authority_citation": "G.O.Ms.No. 1126 & 1127, Revenue (Regn-I) Dept, dt. 13.06.2005 w.e.f. 01.07.2005",
            "keywords": "G.O.Ms.No. 1126 G.O.Ms.No. 1127 GO 1126 historical 2005 2009 9.5% 8.5% 7.5% flats"
        }
    ]
    return chunks


def main():
    print("Extracting legal text from Registration Act, Stamp Act, and G.O.s...")
    all_chunks = []
    
    reg_chunks = extract_registration_act()
    print(f"Loaded {len(reg_chunks)} Registration Act chunks")
    all_chunks.extend(reg_chunks)
    
    stamp_chunks = extract_stamp_act()
    print(f"Loaded {len(stamp_chunks)} Stamp Act chunks")
    all_chunks.extend(stamp_chunks)
    
    case_law_chunks = extract_release_conveyance_case_law()
    print(f"Loaded {len(case_law_chunks)} release/conveyance case-law chunks")
    all_chunks.extend(case_law_chunks)

    targeted_chunks = extract_settlement_debt_and_farmer_loan_guidance()
    print(f"Loaded {len(targeted_chunks)} settlement/farmer-loan guidance chunks")
    all_chunks.extend(targeted_chunks)

    go_chunks = extract_government_orders()
    print(f"Loaded {len(go_chunks)} Government Order & Citation chunks")
    all_chunks.extend(go_chunks)

    ccla_chunks = extract_freedom_fighter_ccla_circular()
    print(f"Loaded {len(ccla_chunks)} CCLA circular chunks from OCR transcription")
    all_chunks.extend(ccla_chunks)
    
    build_database(all_chunks)


if __name__ == "__main__":
    main()

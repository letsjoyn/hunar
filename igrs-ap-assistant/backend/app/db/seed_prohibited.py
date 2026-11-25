"""
Seed data for Section 22-A Prohibited Properties Register.
Covers all 5 statutory clauses of Section 22-A of Registration Act 1908 (AP Amendment):
- 22-A(1)(a): Government Assigned Lands / Poramboke / Water bodies
- 22-A(1)(b): Endowments & Hindu Religious Devasthanam Properties
- 22-A(1)(c): AP State Waqf Board Properties
- 22-A(1)(d): Urban Land Ceiling (ULC) & Agricultural Surplus Lands
- 22-A(1)(e): Court Attachment / Lokayukta / Anti-Corruption Bureau (ACB) notified
"""
from typing import List
from sqlalchemy.orm import Session
from backend.app.db.models import ProhibitedProperty

PROHIBITED_SEED_DATA = [
    # Krishna / Vijayawada
    {
        "district": "KRISHNA",
        "mandal": "VIJAYAWADA URBAN",
        "village": "GUNADALA",
        "survey_number": "102/1",
        "subdivision": "1",
        "prohibition_category": "22-A(1)(a)",
        "prohibition_reason": "ASSIGNED_LAND",
        "notified_authority": "Tahsildar / Collector Krishna",
        "gazette_notification": "AP Gazette No. 28/2007 (POTD Act 1977)",
        "remarks": "Assigned land for landless poor. Transfer/sale strictly prohibited under Section 3 of Act 9/1977.",
    },
    {
        "district": "KRISHNA",
        "mandal": "VIJAYAWADA URBAN",
        "village": "BHAVANIPURAM",
        "survey_number": "45/2",
        "subdivision": "2",
        "prohibition_category": "22-A(1)(a)",
        "prohibition_reason": "PORAMBOKE_WATERBODY",
        "notified_authority": "Water Resources Dept / District Collector",
        "gazette_notification": "Krishna District Gazette dt. 14.05.2012",
        "remarks": "Krishna River embankment buffer zone poramboke. Mandatory refusal.",
    },
    {
        "district": "KRISHNA",
        "mandal": "MACHILIPATNAM",
        "village": "CHILAKALAPUDI",
        "survey_number": "144",
        "subdivision": "ALL",
        "prohibition_category": "22-A(1)(c)",
        "prohibition_reason": "WAQF_PROPERTY",
        "notified_authority": "AP State Waqf Board",
        "gazette_notification": "AP Gazette Ext. No. 44 dt. 12.08.2004",
        "remarks": "Registered Waqf institution property under Waqf Act 1995. Non-alienable.",
    },
    {
        "district": "KRISHNA",
        "mandal": "VIJAYAWADA URBAN",
        "village": "PATAMATA",
        "survey_number": "189",
        "subdivision": "A",
        "prohibition_category": "22-A(1)(e)",
        "prohibition_reason": "COURT_ATTACHMENT",
        "notified_authority": "Hon'ble High Court of Andhra Pradesh",
        "gazette_notification": "Interim Order in WP No. 1029/2021",
        "remarks": "Subject to High Court status quo injunction. Prohibited from registration until final disposal.",
    },
    # Visakhapatnam
    {
        "district": "VISAKHAPATNAM",
        "mandal": "VISAKHAPATNAM RURAL",
        "village": "SIMHACHALAM",
        "survey_number": "210",
        "subdivision": "ALL",
        "prohibition_category": "22-A(1)(b)",
        "prohibition_reason": "ENDOWMENTS",
        "notified_authority": "Commissioner, Endowments Dept AP",
        "gazette_notification": "AP Gazette No. 511 dt. 03.11.2006",
        "remarks": "Sri Varaha Lakshmi Narasimha Swamy Devasthanam property. Absolute statutory bar.",
    },
    {
        "district": "VISAKHAPATNAM",
        "mandal": "VISAKHAPATNAM RURAL",
        "village": "MADHURAWADA",
        "survey_number": "56/3",
        "subdivision": "3",
        "prohibition_category": "22-A(1)(d)",
        "prohibition_reason": "ULC_SURPLUS",
        "notified_authority": "Special Officer & Competent Authority, ULC",
        "gazette_notification": "G.O.Ms.No. 747 Revenue dt. 18.06.2008",
        "remarks": "Urban Land Ceiling surplus land vested with State Government.",
    },
    # Guntur
    {
        "district": "GUNTUR",
        "mandal": "MANGALAGIRI",
        "village": "MANGALAGIRI",
        "survey_number": "88",
        "subdivision": "B",
        "prohibition_category": "22-A(1)(b)",
        "prohibition_reason": "ENDOWMENTS",
        "notified_authority": "Endowments Dept AP",
        "gazette_notification": "Guntur District Gazette dt. 19.04.2007",
        "remarks": "Sri Panakala Lakshmi Narasimha Swamy Devasthanam inam land.",
    },
    {
        "district": "GUNTUR",
        "mandal": "THULLUR",
        "village": "THULLUR",
        "survey_number": "115/4",
        "subdivision": "4",
        "prohibition_category": "22-A(1)(a)",
        "prohibition_reason": "CRDA_GOVERNMENT",
        "notified_authority": "APCRDA / State Government",
        "gazette_notification": "CRDA Land Pooling Gazette No. 12/2015",
        "remarks": "Capital Region Development Authority pooled government lands.",
    },
    # Tirupati / Chittoor
    {
        "district": "TIRUPATI",
        "mandal": "TIRUPATI URBAN",
        "village": "TIRUPATI",
        "survey_number": "305",
        "subdivision": "ALL",
        "prohibition_category": "22-A(1)(b)",
        "prohibition_reason": "ENDOWMENTS_TTD",
        "notified_authority": "Tirumala Tirupati Devasthanams (TTD)",
        "gazette_notification": "AP Gazette No. 119 dt. 22.09.2003",
        "remarks": "TTD trust property. Permanent prohibition under Sec. 22-A(1)(b).",
    },
]


def seed_prohibited_properties(session: Session) -> int:
    """Insert Section 22-A prohibited records if table is empty."""
    count = session.query(ProhibitedProperty).count()
    if count > 0:
        return count

    for row in PROHIBITED_SEED_DATA:
        prop = ProhibitedProperty(**row)
        session.add(prop)
    session.commit()
    return len(PROHIBITED_SEED_DATA)

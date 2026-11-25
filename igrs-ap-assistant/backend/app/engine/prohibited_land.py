"""
Section 22-A Prohibited Land Verification Engine.

Enforces Section 22-A of The Registration Act, 1908 (Andhra Pradesh Amendment Act 19/2007).
Prohibits registration of:
- Category 1 [Sec 22-A(1)(a)]: Assigned lands (POTD Act 1977) & Government Poramboke
- Category 2 [Sec 22-A(1)(b)]: Endowments (Devasthanam / Temple lands)
- Category 3 [Sec 22-A(1)(c)]: Waqf Board properties
- Category 4 [Sec 22-A(1)(d)]: Urban Land Ceiling (ULC) surplus
- Category 5 [Sec 22-A(1)(e)]: Court attachments, CBI/ACB notifications

Under Section 22-A(2), any document registered in contravention of these provisions
is void ab initio and the Registering Officer is personally liable for departmental action.
"""
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from sqlalchemy.orm import Session
from backend.app.core.config import IS_PRODUCTION
from backend.app.db.models import ProhibitedProperty


class ProhibitedCheckResult(BaseModel):
    is_prohibited: bool
    status: str  # MANDATORY_REFUSAL or CLEAR
    survey_number: str
    village: Optional[str] = None
    district: Optional[str] = None
    category: Optional[str] = None
    category_title: Optional[str] = None
    reason: Optional[str] = None
    notified_authority: Optional[str] = None
    gazette_notification: Optional[str] = None
    statutory_directive: str
    legal_citation: str
    remarks: Optional[str] = None


CATEGORY_TITLES = {
    "22-A(1)(a)": "State Government / Assigned Lands (POTD Act 1977)",
    "22-A(1)(b)": "Endowments & Religious Institutions (Devasthanam)",
    "22-A(1)(c)": "Andhra Pradesh State Waqf Board Properties",
    "22-A(1)(d)": "Urban Land Ceiling (ULC) Surplus Lands",
    "22-A(1)(e)": "Judicial Attachment / Law Enforcement Prohibitions",
}


def normalize_survey_number(s: str) -> str:
    """Normalize '102 - 1', '102/1', 'Sy.No. 102/1' to '102/1'."""
    clean = re.sub(r"(?i)sy\.?\s*no\.?|survey\s*no\.?", "", s).strip()
    clean = re.sub(r"\s*-\s*", "/", clean)
    clean = re.sub(r"\s+", "", clean)
    return clean


def check_prohibited_land(
    session: Session,
    survey_number: str,
    village: Optional[str] = None,
    district: Optional[str] = None,
) -> ProhibitedCheckResult:
    """Check the local prototype index; a miss is never a clearance."""
    norm_sy = normalize_survey_number(survey_number)

    if IS_PRODUCTION:
        return ProhibitedCheckResult(
            is_prohibited=False,
            status="NOT_VERIFIED",
            survey_number=norm_sy,
            village=village,
            district=district,
            statutory_directive=(
                "This prototype has no production-approved Section 22-A registry. "
                "Do not infer that the property is clear or registerable."
            ),
            legal_citation="Section 22-A lookup unavailable: no approved registry configured.",
            remarks="No production clearance is provided by this prototype.",
        )

    query = session.query(ProhibitedProperty)
    
    # Check exact survey number first
    base_match = query.filter(ProhibitedProperty.survey_number == norm_sy)
    if village:
        base_match = base_match.filter(ProhibitedProperty.village.ilike(f"%{village.strip()}%"))
    if district:
        base_match = base_match.filter(ProhibitedProperty.district.ilike(f"%{district.strip()}%"))
        
    match = base_match.first()
    
    # If not found by exact, try prefix (e.g. 102/1 matches 102 if whole sy is prohibited)
    if not match and "/" in norm_sy:
        parent_sy = norm_sy.split("/")[0]
        parent_match = query.filter(ProhibitedProperty.survey_number == parent_sy)
        if village:
            parent_match = parent_match.filter(ProhibitedProperty.village.ilike(f"%{village.strip()}%"))
        match = parent_match.first()

    if match:
        cat_title = CATEGORY_TITLES.get(match.prohibition_category, "Prohibited Land")
        directive = (
            f"MANDATORY REFUSAL under Section 22-A of the Registration Act, 1908. "
            f"This parcel is classified under {match.prohibition_category} ({match.prohibition_reason}) "
            f"notified by {match.notified_authority or 'Competent Authority'}. "
            f"Under Section 22-A(2), any registration of this property is void ab initio."
        )
        citation = f"The Registration Act, 1908 (AP Amendment Act 19 of 2007), Section {match.prohibition_category}; {match.gazette_notification or 'Official Gazette'}"
        
        return ProhibitedCheckResult(
            is_prohibited=True,
            status="MANDATORY_REFUSAL",
            survey_number=match.survey_number,
            village=match.village,
            district=match.district,
            category=match.prohibition_category,
            category_title=cat_title,
            reason=match.prohibition_reason,
            notified_authority=match.notified_authority,
            gazette_notification=match.gazette_notification,
            statutory_directive=directive,
            legal_citation=citation,
            remarks=match.remarks,
        )
    else:
        return ProhibitedCheckResult(
            is_prohibited=False,
            status="NOT_FOUND",
            survey_number=norm_sy,
            village=village,
            district=district,
            statutory_directive=(
                "No matching record was found in this prototype's limited index. Absence from this index is not proof that the property is clear or registerable."
            ),
            legal_citation="The Registration Act, 1908 (Section 22-A lookup; no clearance provided)",
            remarks="No matching record in this index; official verification is still required.",
        )

"""
Real Google Gemini API Integration with Grounded Deterministic Verification.

Architecture:
1. Runs AP CARD deterministic engine and ChromaDB vector search to establish
   uncompromised ground-truth calculations, Government Orders, and legal citations.
2. Hands the verified statutory ground-truth to the Gemini API (gemini-3.5-flash-lite / gemini-3.8-flash)
   to synthesize authoritative, articulate, and professional natural language responses.
3. Cryptographically signs the official CARD check-slip (HMAC-SHA256) and binds it to the response
   so the live reactive SRO Check-Slip workspace always reflects exact verified numbers.
4. Fault-Tolerant Resilience: If the Gemini API experiences network limits or demand spikes (e.g. 503),
   it falls back seamlessly to the deterministic local engine with zero downtime.
"""
import os
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from backend.app.core.config import ALLOW_EXTERNAL_LLM, GEMINI_API_KEY, GEMINI_MODEL
from backend.app.engine.chatbot_orchestrator import (
    ChatResponse,
    process_chat_message,
)

try:
    from google import genai
    from google.genai import types
    GENAI_INSTALLED = True
except ImportError:
    GENAI_INSTALLED = False


def is_gemini_available() -> bool:
    """Check if Gemini SDK is installed and an API key is provided."""
    key = (os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY or "").strip()
    return ALLOW_EXTERNAL_LLM and GENAI_INSTALLED and bool(key)


def get_gemini_client() -> Optional[Any]:
    key = (os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY or "").strip()
    if not ALLOW_EXTERNAL_LLM or not key or not GENAI_INSTALLED:
        return None
    try:
        return genai.Client(api_key=key)
    except Exception:
        return None


SYSTEM_PROMPT = """
You are the Andhra Pradesh Registration & Stamps Department Statutory Intelligence Engine.

AESTHETIC & FORMATTING DIRECTIVES (ZERODHA & UBER FINTECH STANDARD):
1. ABSOLUTE PROHIBITION ON EMOJIS: Never use emojis under any circumstances (no emoji icons whatsoever). Maintain strict institutional fintech minimalism.
2. NO ROBOTIC BOILERPLATE: Never output robotic conversational greetings or intros ("Welcome to...", "Here is the statutory assessment...", "As an official assistant..."). Start directly with the factual answer or structured assessment. Never output robotic sign-offs ("Verified against AP IGRS...").
3. DO NOT ASSUME OR FABRICATE AMOUNTS: If the citizen did NOT state a property value, DO NOT assume ₹ 15,00,000 or any dummy number. Provide ONLY the statutory percentage rates, fee rules, caps, and legal eligibility. Explicitly invite the citizen to provide their property value if they want exact rupee calculations.
4. SECTION 56(2) OF INCOME TAX ACT & AP STAMP ACT ARTICLE 29 DEFINITION OF "RELATIVE":
   Under Section 56(2)(x) Explanation clause (e) of the Income Tax Act, 1961 (adopted verbatim by Andhra Pradesh Stamp Act Schedule I-A Article 29 for concessional 2% Stamp Duty):
   "Relative" is evaluated from the perspective of the recipient (donee) and explicitly includes:
   - Spouse of the individual;
   - Brother or sister of the individual;
   - Brother or sister of the spouse of the individual (e.g. husband's sister / sister-in-law);
   - Brother or sister of either of the parents of the individual;
   - Any lineal ascendant or descendant of the individual;
   - Any lineal ascendant or descendant of the spouse of the individual;
   - Spouse of any person referred to above (e.g. brother's wife / sister-in-law).
   THEREFORE: A gift from a sister to her brother's wife (or vice versa) IS 100% EXEMPT under Section 56(2) of the Income Tax Act AND qualifies for the 2.0% concessional Stamp Duty under AP Schedule I-A Article 29 (`03_06`). NEVER claim a sister-in-law or brother's wife is not an exempt relative!
5. ACCURATE INSTRUMENT LABELS:
   - `03_06`: "Gift in favour of Relatives as defined u/s 56(2) of IT Act" (2.0% Stamp Duty). NEVER label 03_06 as "Non-Relative".
   - `03_07`: "Gift in favour of Others / Non-Relatives" (5.0% Stamp Duty).
6. FORMATTING: Use clean Markdown tables with right-aligned numbers, bold keys, uppercase category markers, and crisp horizontal rules.
7. STRICT GROUND TRUTH: Preserve all deterministic rates, caps, and legal citations verbatim.
"""


def run_gemini_chat(
    session: Session,
    message: str,
    model_name: Optional[str] = None,
    client_ip: str = "127.0.0.1",
) -> ChatResponse:
    """
    Executes conversational turn grounded in the deterministic CARD 2.0 engine.
    """
    # Step 1: Compute verified deterministic ground truth and sign check-slip
    deterministic_res: ChatResponse = process_chat_message(session, message)

    client = get_gemini_client()
    if not client:
        return deterministic_res

    target_model = model_name or os.getenv("GEMINI_MODEL") or GEMINI_MODEL or "gemini-3.5-flash-lite"

    # For simple greetings, statutory schedules, and area conversions, return deterministic response directly for zero-drift precision
    if deterministic_res.intent in ("GREETING", "DEED_SCHEDULE", "AREA_CONVERT"):
        return deterministic_res

    # Step 2: Formulate synthesis prompt with verified ground truth
    prompt = (
        f"Citizen Query: {message}\n\n"
        f"Official AP CARD Deterministic Ground Truth:\n"
        f"----------------------------------------\n"
        f"Intent: {deterministic_res.intent}\n"
        f"{deterministic_res.reply}\n"
        f"----------------------------------------\n\n"
        f"Synthesize a clear, authoritative response for the citizen strictly preserving every number, rate, and legal citation."
    )

    try:
        response = client.models.generate_content(
            model=target_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.1,  # Low temperature for strict adherence
            ),
        )

        synthesized_text = response.text.strip() if response.text else deterministic_res.reply

        return ChatResponse(
            reply=synthesized_text,
            intent=deterministic_res.intent,
            card_slip=deterministic_res.card_slip,
            audit_comparison=deterministic_res.audit_comparison,
            legal_docs=deterministic_res.legal_docs,
            fine_data=deterministic_res.fine_data,
            area_data=deterministic_res.area_data,
            prohibited_data=deterministic_res.prohibited_data,
        )

    except Exception:
        # Seamless fallback on demand spikes or connection limits
        return deterministic_res

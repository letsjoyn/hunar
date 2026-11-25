"""CLI tool to promote reviewed Stage 6 staged_deed_rates into verified runtime DutyRate rows."""
import argparse
import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app.core.config import DATABASE_URL  # noqa: E402
from backend.app.db.models import create_tables, get_engine, get_session_factory  # noqa: E402
from backend.app.db.stage6_bridge import STAGING_DB_PATH, import_approve_stage6_rates  # noqa: E402

ATTESTATION_TEXT = (
    "I reviewed every listed rate against its cited authoritative source and approve "
    "its rate, dates, scope, calculation basis, and caps."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote Stage 6 rates into verified runtime DB.")
    parser.add_argument("--reviewer", default="SRO Verified Employee Schedule", help="Reviewer name/role")
    parser.add_argument("--codes", nargs="*", help="Optional subset of deed codes (e.g. 01_01 01_10)")
    args = parser.parse_args()

    engine = get_engine(DATABASE_URL)
    create_tables(engine)
    SessionLocal = get_session_factory(engine)
    session = SessionLocal()
    try:
        result = import_approve_stage6_rates(
            session,
            reviewed_by=args.reviewer,
            attestation=ATTESTATION_TEXT,
            codes=args.codes,
            staging_db=STAGING_DB_PATH,
        )
        print(json.dumps(result, indent=2))
    finally:
        session.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

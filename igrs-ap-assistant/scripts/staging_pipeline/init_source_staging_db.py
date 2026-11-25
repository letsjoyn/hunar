"""Initialize the isolated source-review staging database (additive only)."""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL, make_url

APP_ROOT = Path(__file__).resolve().parents[2]
STAGING_DB = APP_ROOT / "data" / "source_staging.db"


def main() -> None:
    sys.path.insert(0, str(APP_ROOT))
    from backend.app.core.config import DATABASE_URL
    from backend.app.db.staging_models import StagingBase

    target = STAGING_DB.resolve()
    configured = make_url(DATABASE_URL)
    if configured.get_backend_name() == "sqlite" and configured.database:
        active = Path(configured.database)
        if not active.is_absolute():
            active = (APP_ROOT / active).resolve()
        if active == target:
            raise SystemExit("Refusing to use the configured application database as staging storage.")
    if target in {
        (APP_ROOT / "data" / "legal_knowledge.db").resolve(),
        (APP_ROOT / "data" / "legal_knowledge_pre_topics.db").resolve(),
    }:
        raise SystemExit("Refusing to alter a legacy legal index database.")

    STAGING_DB.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL.create("sqlite", database=str(target)), echo=False)
    StagingBase.metadata.create_all(engine)
    table_names = sorted(inspect(engine).get_table_names())
    expected = sorted(StagingBase.metadata.tables)
    missing = sorted(set(expected) - set(table_names))
    if missing:
        raise SystemExit("Staging schema verification failed: missing tables " + ", ".join(missing))
    print(f"Initialized isolated staging DB: {STAGING_DB.relative_to(APP_ROOT)}")
    print("Tables:", ", ".join(table_names))
    print("No source records imported; existing application databases were not modified.")
    engine.dispose()


if __name__ == "__main__":
    main()

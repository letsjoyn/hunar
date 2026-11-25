"""Build the isolated Stage 7 FTS5 + local-vector legal index."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from backend.app.engine.hybrid_rag import (  # noqa: E402
    INDEX_DB_PATH,
    MANIFEST_PATH,
    SOURCE_DB_PATH,
    build_stage7_index,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=SOURCE_DB_PATH)
    parser.add_argument("--output-db", type=Path, default=INDEX_DB_PATH)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--embedding-model",
        help="Optional pre-cached local sentence-transformers model; no download is attempted",
    )
    args = parser.parse_args()
    manifest = build_stage7_index(
        args.source_db, args.output_db, args.manifest, embedding_model=args.embedding_model
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

# AP IGRS Assistant — Local Prototype

This repository is an **independent local prototype**, not an Andhra Pradesh
Government system and not an approved tool for registration, fee collection,
property clearance, legal advice, or audit decisions.

It contains a FastAPI app, a small browser UI, deterministic calculators, local
search indexes, and sample rate data. The legal/rate data and several business
rules have **not** been independently verified against the complete official
source documents. The application intentionally marks local calculations as
demo-only. Do not use its results operationally.

## Run locally on Windows

Use the installed Python 3.11 interpreter explicitly (the default `py` on
some Windows machines may select Python 3.14):

```powershell
cd D:\gov\igrs-ap-assistant
py -3.11 -m venv .venv311
.\.venv311\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:APP_ENV = "development"
$env:DATABASE_URL = "sqlite:///./igrs_ap_local.db"
$env:ALLOW_EXTERNAL_LLM = "true"
# Set GEMINI_API_KEY in your shell before enabling Gemini.
python -m pip install -r requirements-ai.txt
python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. API documentation is at
<http://127.0.0.1:8000/docs>; health check is <http://127.0.0.1:8000/health>.

On the first local start, the app creates SQLite tables and loads the sample
rate CSVs. Sample rates are marked `PENDING_REVIEW`; development mode permits
them only so the prototype can be exercised, and calculator responses carry a
demo-only warning. Production mode does not seed this sample data and rejects
pending rate rows.

To run the test suite:

```powershell
python -m pytest backend/tests -q
```

## Stage 7 local hybrid legal search

Stage 7 builds a separate, local index from provision-level records in
`data/source_staging.db`. It writes `data/stage7_hybrid_rag.db` and a manifest;
it does not overwrite `data/legal_knowledge.db`, the calculator DB, or the
existing Chroma store. Rebuild from this project directory with:

```powershell
python scripts/build_stage7_hybrid_rag.py
```

By default, retrieval combines SQLite FTS5 BM25 with offline
512-dimensional hashed word/character vectors using reciprocal-rank fusion.
This is a reproducible local vector baseline, **not a pretrained semantic
model**: it does not provide translation or deep semantic equivalence. An
optional pre-cached local sentence-transformers model can be used consistently
for both indexing and queries:

```powershell
python scripts/build_stage7_hybrid_rag.py --embedding-model "D:\models\your-cached-model"
```

Install `requirements-ai.txt` and ensure the model files are already available
locally before using that option. The builder sets local-only loading; it does
not download a model or send source text to an embedding service. Rebuild the
index with the same model to switch embedding backends. If the configured local
model is later unavailable, legal search falls back to BM25 keyword retrieval
rather than failing or contacting a remote service.

Results preserve document/provision review, authority, extraction, privacy,
source-page, and effective-date metadata. Pending and OCR-derived records
remain explicitly unverified and are never promoted to calculator rates.
Non-legal personal-content records, records matching email/phone/ID patterns,
and recognized personal diary/contact/identity-form pages are excluded from
the index.

The older `build_full_legal_rag.py` / `build_chroma_vector_db.py` path is a
legacy prototype indexer; the Chroma builder deletes and recreates its legacy
collection. It is not used to rebuild the Stage 7 index. Do not send raw
personal/internal source text to external services.

## Optional local Docker run

Docker Compose binds the web app and database to loopback only and uses
local-development defaults. It is not a production deployment recipe.

```powershell
cd D:\gov\igrs-ap-assistant
docker compose up --build
```

Then open <http://127.0.0.1:8000>. Chat uses Gemini when the optional SDK,
`GEMINI_API_KEY`, and `ALLOW_EXTERNAL_LLM=true` are all configured; otherwise it
falls back to local-only parsing. Gemini first classifies the request, then the
app searches its local legal index/rate database and runs deterministic
calculations before Gemini writes the response. The user prompt and retrieved
source excerpts are sent to Google's Gemini API, so do not enable this for
sensitive department or citizen data without approval.

For Docker Compose, provide `GEMINI_API_KEY` and set `ALLOW_EXTERNAL_LLM=true`
in the environment (or Compose `.env`) before starting. The optional Gemini,
Chroma, and indexing dependencies are in `requirements-ai.txt`.

## What currently works

- Local FastAPI service and browser UI.
- Deed duty calculation and historical comparison against the loaded sample
  rows.
- Building-value, delay-fine, and area-conversion endpoints.
- Local document search against the SQLite/Chroma indexes when those indexes
  are available.
- Draft and reviewer approval routes for runtime rate rows. The bulk approval
  routes always require `ADMIN_API_TOKEN`, even in local development.
- Automated API/calculator tests.

### Batch review of runtime rates

The reviewer workflow is deliberately separate from the Stage 6 reconciliation
CSV. It does not import or approve `data/stage6_rate_reconciliation.csv` or
`data/stage6_actionable_review_queue.csv`.

1. Configure `ADMIN_API_TOKEN` and use an authorized reviewer account/process.
2. Submit pending runtime `rate_ids` to
   `POST /api/admin/rates/bulk-approval-preview` with `x-admin-token`. Review
   every returned field, blocker, source, and fingerprint against the cited
   authoritative document.
3. Submit only complete, non-overlapping rows to
   `POST /api/admin/rates/bulk-approve`, providing a source document and page
   reference for each row, its preview fingerprint, the exact reviewer
   attestation, and `x-admin-token`.

The endpoint rejects incomplete rows, stale fingerprints, and overlapping
validity windows atomically; it never silently changes prior approved dates.
It records the supplied reviewer attestation but cannot independently verify
legal correctness or reviewer identity. The current Stage 6 candidate rows are
still `NOT_FOR_CALCULATION` and are not made eligible by this API. Production
use still requires department identity/access controls and legal sign-off.

A passing test means the code matched the test fixture. It does **not** mean
that a rate, citation, legal interpretation, or production workflow is legally
correct.

## Data status and boundaries

- `data/verified_seeds/` is a legacy directory name. The CSV rows are now
  `PENDING_REVIEW`; none should be interpreted as officially approved by virtue
  of being present in the app.
- The rate generator is a draft importer, not a source-verification system.
- `data/raw/` currently contains the 2014 G.O. PDFs and reviewed/sanitized
  extracts. The Stage 7 corpus is source-cited but mostly pending review; the
  2024/2010 diaries and older manuals are secondary compilations, not a complete
  verified official corpus.
- `data/stage6_rate_reconciliation.json` is a documentary crosswalk, not an
  approved temporal rate database. Unresolved deed mappings and effective dates
  are not used for calculations.
- The pre-2005 rate rows were removed from the active seed file because this
  project has not verified them from a primary source. Requests for those dates
  should return `NOT_FOUND`.
- The sample Section 22-A records in `backend/app/db/seed_prohibited.py` are
  test fixtures only. The running app no longer inserts them into its database.
- Do not put employee personal data, citizen property records, credentials, or
  unredacted diary pages into the indexes or repository.

## Before any real departmental pilot

This prototype is **not production ready**. At minimum, a department-led
project must complete all of the following:

1. Have an authorized domain expert verify every rate, effective date,
   exemption, formula, and citation against the original G.O./Gazette and page.
2. Replace demo records with a reviewed, versioned source-of-truth dataset;
   maintain review identity, source page, and approval history.
3. Replace heuristic chat parsing with explicit clarification when instrument,
   date, jurisdiction, amount, or other required inputs are missing.
4. Audit every deterministic rule and legal-search record. Search results are
   references, not legal determinations.
5. Implement department SSO/RBAC, secure secrets management, deployment
   controls, backups, monitoring, security testing, and data-residency review.
6. Obtain written department approval for data access and any CARD integration.

Production startup requires `APP_ENV=production`, a `CARD_HMAC_SECRET` and
`ADMIN_API_TOKEN` of at least 32 characters. Even with those settings, this
application must not be considered production-ready until the data, security,
and legal-validation work above is complete.

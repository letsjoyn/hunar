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
$env:ALLOW_EXTERNAL_LLM = "false"
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

## Optional local Docker run

Docker Compose binds the web app and database to loopback only and uses
local-development defaults. It is not a production deployment recipe.

```powershell
cd D:\gov\igrs-ap-assistant
docker compose up --build
```

Then open <http://127.0.0.1:8000>. To use a Gemini API key, set
`GEMINI_API_KEY` in the shell before starting Compose. Chat falls back to the
local deterministic prototype if the key is absent. For smaller installs, the
base requirements omit optional AI/indexing packages; install them separately
with `python -m pip install -r requirements-ai.txt`. External Gemini calls are
opt-in only: set `ALLOW_EXTERNAL_LLM=true` as well as `GEMINI_API_KEY`; do not
enable this for sensitive department or citizen data without approval.

## What currently works

- Local FastAPI service and browser UI.
- Deed duty calculation and historical comparison against the loaded sample
  rows.
- Building-value, delay-fine, and area-conversion endpoints.
- Local document search against the SQLite/Chroma indexes when those indexes
  are available.
- A draft/approval route for rate changes. Local development omits token
  enforcement; set `ADMIN_API_TOKEN` to exercise token-protected admin routes.
- Automated API/calculator tests.

A passing test means the code matched the test fixture. It does **not** mean
that a rate, citation, legal interpretation, or production workflow is legally
correct.

## Data status and boundaries

- `data/verified_seeds/` is a legacy directory name. The CSV rows are now
  `PENDING_REVIEW`; none should be interpreted as officially approved by virtue
  of being present in the app.
- The rate generator is a draft importer, not a source-verification system.
- `data/raw/` currently contains only two 2014 G.O. PDFs. The source PDFs and
  OCR text under `D:\gov\data` are not a complete official corpus.
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

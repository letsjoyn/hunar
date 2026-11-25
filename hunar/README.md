# AP Registration & Stamps — Research Prototype

An early-stage prototype for answering Andhra Pradesh Registration & Stamps questions with dated, source-backed calculations. This repository is **not an official government service** and is not approved for registration, duty collection, property clearance, or legal decisions.

## Current source set

`data/sources/` contains two one-page 2014 Government Order PDFs supplied for this project. They are candidate sources for a sale-deed calculator. They have not been independently approved by a departmental domain expert, and they are not enough to implement a complete sale-deed assessment: transfer duty, effective-date boundaries, jurisdictional exceptions, user charges, and later amendments still need their primary sources and review.

## Build plan

1. Verify each source against the original Gazette/G.O. and record its effective date, applicable instrument/jurisdiction, page, and reviewer.
2. Implement one sale-deed calculation for one verified date range, with deterministic arithmetic and a refusal when a needed rate is missing.
3. Test against officer-reviewed examples and date boundaries.
4. Add further historical periods and instrument types only after each is verified.
5. Add legal document search after the source inventory and citations are reliable. An LLM must not select rates or invent legal conclusions.

## Data handling

Raw OCR diaries, personal or internal records, generated SQLite databases, and vector indexes are intentionally not committed. The source directory supplied for initial exploration contains OCR text with contact/ID-like strings and has not been safely redacted. Keep those originals local and do not publish them. Add further files only after privacy, provenance, and publication review.

A passing software test does not establish that a rate or legal interpretation is correct. Every production value needs verification against an authoritative primary source and review by an authorized domain expert.

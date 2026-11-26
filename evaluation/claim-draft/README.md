# T-004A Fact·Claim Synthetic Dataset Draft (`evaluation/claim-draft`)

- **Dataset Version**: `0.1.0-draft`
- **Schema Version**: `0.1.0-draft`
- **Oracle Status**: `DRAFT` (`frozen_for_golden: false`, pending human review in **T-004B**)
- **Base Commit SHA**: `efe251071fbee1be9a2ffee680a73f3ca3cd8084`
- **Branch**: `qa/claim-fixtures`

## Purpose & Boundary

This directory contains **100% synthetic** fixtures and programmatic validation tools for the Career Memory Grounded Claim Pipeline:

`Source Revision → Content Unit → Fact Support → Canonical Fact → Claim Atom Support → Claim Atom → Claim` + `Refetch Verification (REF-01..08 & Option C Canary)`

- Zero external LLMs, zero real user documents, zero real URLs/OAuth tokens/API keys, and zero production DB or product code modifications.
- Artificial raw texts exist **only** inside `sources/` as test fixtures. Downstream stage records (`stages/stage1..stage4`) simulate Option C (방안 C) non-persistence (`persists_raw_text: false`) and store only hashes, offsets, locators, and derived structured statements.
- **Structural validation ≠ semantic approval**: passing `validate_dataset.py` certifies schema compliance, locator/hash round-trip integrity, anti-self-evidence guards, canary non-persistence, and `SOURCE_VERIFIED` safety guards. Human semantic sign-off happens in **T-004B** before any golden freeze.

## Directory Layout

| Path | Role | Description |
|---|---|---|
| `schemas/claim_pipeline.schema.json` | Schema | JSON Schema (Draft 2020-12) covering sources, stages 1–4, refetch snapshots, and DRAFT oracles |
| `sources/source_revisions.json` | Synthetic Source Input | 18 synthetic Source Revisions (EN/KO/mixed), including deleted-source and REF-07 canary fixtures |
| `sources/refetch_snapshots.json` | Synthetic Refetch Stub Input | 8 synthetic refetch stub responses (`REF-01..08`), including unsafe URL vectors and canary token |
| `stages/stage1_content_units.json` | Stage 1 Stub Output | 25 Content Units with programmatically computed locators and hashes |
| `stages/stage2_fact_extraction.json` | Stage 2 Stub Output | 25 Canonical Facts (0/1/N coverage) & 28 Fact Supports (`DIRECT`/`CONTEXT`/`CONTRADICTS`) |
| `stages/stage3_claim_assembly.json` | Stage 3 Stub Output | 23 Claims, 30 Claim Atoms, 33 Claim Atom Supports, and simulated user edit/delete events |
| `stages/stage4_refetch_checks.json` | Stage 4 Stub Output | `REF-01..08` verification stub outputs, clean persisted snapshots, negative canary-leak fixture, Post-MVP tags |
| `expected/oracle_draft.json` | Expected Oracle (`DRAFT`) | Expected atom & claim grounding statuses (strictly separated from stage outputs) |
| `expected/refetch_oracle_draft.json` | Expected Refetch Oracle (`DRAFT`) | Expected `REF-01..08` verdicts, `unverifiable_now` vs `historical_fact_falsified`, Post-MVP tags |
| `tools/locator_hash.py` | Utility | Programmatic normalization, SHA-256 hashing, char/UTF-8/UTF-16/line offset locator, round-trip verifier, URL safety guard |
| `tools/build_fixtures.py` | Builder | Deterministically computes all IDs, hashes, spans, and offsets into JSON files |
| `tools/validate_dataset.py` | Validator | Runs schema, round-trip, containment, referential integrity, anti-self-evidence, status guard, URL safety, and canary leak checks |
| `tools/test_validator.py` | Test Suite | 15 tests including deliberate fault injection (hash, char/UTF-8 byte offset, reference, self-evidence, illegal `SOURCE_VERIFIED`, canary leak, unsafe URL, premature golden freeze) |
| `COVERAGE.md` | Documentation | Scenario/atom/refetch coverage matrices and T-004B independent review queue |

## Fixture-Only Normalization & Offset Assumptions (`TEST_FIXTURE_ONLY`)

> **Important**: Notion pages (`T-004A`, `Grounded Claim Pipeline`, `방안 C`, `Dataset 가이드`, `QA 전략`) require authenticated access and could not be fetched in this environment. The agent did **not** assume or freeze production parser contracts. All conventions below are explicitly scoped as `TEST_FIXTURE_ONLY` and marked `REVIEW_REQUIRED`:

1. **Text Normalization (`fixture-nfc-lf-v0-draft`, `REVIEW_REQUIRED`)**:
   - Line endings normalized to LF (`\r\n`, `\r` → `\n`), Unicode normalized to NFC (`unicodedata.normalize("NFC", text)`).
   - Internal spaces/indentation preserved verbatim.
2. **Offsets (`fixture-locator-v0-draft`, `REVIEW_REQUIRED`)**:
   - Half-open `[start, end)` intervals computed programmatically in three coordinate spaces simultaneously so cross-language parity can be audited:
     - `char_start` / `char_end`: 0-based Unicode codepoint indices (Python `str` slicing)
     - `utf8_byte_start` / `utf8_byte_end`: 0-based UTF-8 byte offsets (multi-byte verified on Korean fixtures `SCN-01`, `SCN-03`, `SCN-06`, `SCN-09`)
     - `utf16_start` / `utf16_end`: 0-based UTF-16 code unit offsets (JS/TS `String.slice` parity)
   - `line_start` / `line_end`: 1-based inclusive line numbers.
3. **Hashes & Deterministic IDs (`REVIEW_REQUIRED`)**:
   - `sha256:<64-hex>` over UTF-8 bytes of normalized text/span.
   - `source_revision_id`: `rev-<source_key>-v<seq>-<content_sha256[:8]>`.
   - `locator_id`: `loc-<owner_id>-<char_start>_<char_end>-<span_sha256[:8]>`.
4. **Open Product Policies (`REVIEW_REQUIRED`)**:
   - Exact `REF-01..08` ID-to-label ordering vs Notion `T-004A` specification.
   - Minor user paraphrase edit policy (`SCN-12` `claim-12-2`): conservatively cleared from `SOURCE_VERIFIED` until re-verified.
   - Option C transient snippet display vs zero-raw-text persistence across DB, logs, caches, and traces.
   - Multi-atom Claim precedence when both `CONTRADICTED` and `UNSUPPORTED` atoms coexist (`SCN-14`).

## Commands

Run from the worktree root:

```bash
# 1. Programmatically rebuild all fixtures (idempotent)
python3 evaluation/claim-draft/tools/build_fixtures.py

# 2. Run full structural, schema, locator/hash round-trip, canary, and policy validator
python3 evaluation/claim-draft/tools/validate_dataset.py

# 3. Run unit + deliberate fault-injection test suite
python3 -m unittest discover -s evaluation/claim-draft/tools -p "test_*.py" -v
```

## Explicitly Not Executed (Out of Scope for T-004A Draft)

- Real external provider HTTP refetch / live OAuth / live URL fetching
- External LLM inference quality evaluation, prompt tuning, or token/cost benchmarking
- Product approval UI/API workflow or Export product E2E
- T-004B human semantic sign-off and golden dataset freeze

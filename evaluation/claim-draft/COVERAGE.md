# T-004A Coverage Matrix & T-004B Human Review Handoff

- **Dataset Version**: `0.1.0-draft`
- **Oracle Status**: `DRAFT` (`frozen_for_golden: false`)
- **Totals**: **14 Scenarios** (target ≥12) · **30 Claim Atoms** (target ≥24) · **23 Claims** · **25 Canonical Facts** · **28 Fact Supports** · **8 Refetch Cases (`REF-01..08`)** · **1 Synthetic Non-Persistence Canary**

## 1. Fact·Claim Scenario Coverage (`SCN-01`..`SCN-14`)

| Scenario | Language | Facts | Atoms | Claims | Support Roles | Key Coverage Tags | Expected Claim Status (`DRAFT`) |
|---|---|---:|---:|---:|---|---|---|
| `SCN-01` | mixed (KO/EN) | 0 | 1 | 1 | none (0 supports) | 0 facts from boilerplate, hard negative hallucination | `claim-01-1`: `UNSUPPORTED` |
| `SCN-02` | EN | 1 | 1 | 1 | `DIRECT` | 1 fact baseline, verified owner | `claim-02-1`: `SOURCE_VERIFIED` *(DRAFT)* |
| `SCN-03` | KO | 3 | 3 | 1 | `DIRECT` | N=3 facts, multi-atom claim, Korean UTF-8 byte offsets | `claim-03-1`: `SOURCE_VERIFIED` *(DRAFT)* |
| `SCN-04` | EN | 2 | 2 | 2 | `DIRECT`, `CONTEXT` | `DIRECT+CONTEXT` vs `CONTEXT`-only hard negative | `claim-04-1`: `SOURCE_VERIFIED` *(DRAFT)*; `claim-04-2`: `UNSUPPORTED` |
| `SCN-05` | EN | 2 | 2 | 2 | `DIRECT`, `CONTRADICTS` | Cross-revision contradiction (draft vs addendum) | `claim-05-1`: `CONFLICTING`; `claim-05-2`: `SOURCE_VERIFIED` *(DRAFT)* |
| `SCN-06` | KO | 2 | 2 | 2 | `DIRECT`, `CONTRADICTS` | Completed vs Planned RFC, Korean UTF-8 offsets | `claim-06-1`: `SOURCE_VERIFIED` *(DRAFT)*; `claim-06-2`: `CONFLICTING` |
| `SCN-07` | EN | 2 | 2 | 2 | `DIRECT`, `CONTRADICTS` | Negation context (not deployed / reverted / no 30% savings) | `claim-07-1`: `SOURCE_VERIFIED` *(DRAFT)*; `claim-07-2`: `CONFLICTING` |
| `SCN-08` | EN | 2 | 2 | 2 | `DIRECT`, `CONTEXT` | Team (6 engineers) vs Individual contribution attribution | `claim-08-1`: `SOURCE_VERIFIED` *(DRAFT)*; `claim-08-2`: `UNSUPPORTED` |
| `SCN-09` | KO | 2 | 2 | 2 | `DIRECT`, `CONTEXT`, `CONTRADICTS` | Staging/test sandbox vs unstarted production deployment | `claim-09-1`: `SOURCE_VERIFIED` *(DRAFT)*; `claim-09-2`: `CONFLICTING` |
| `SCN-10` | EN | 2 | 3 | 1 | `DIRECT`, `CONTEXT`, 0-support | Unverified causation (confounder, no A/B test) + fabricated metric + partial atom support (1/3) | `claim-10-1`: `PARTIALLY_SUPPORTED` |
| `SCN-11` | EN | 2 | 2 | 2 | `DIRECT` | Identity ambiguity (unmapped `'Alex'`) vs verified owner | `claim-11-1`: `IDENTITY_AMBIGUOUS`; `claim-11-2`: `SOURCE_VERIFIED` *(DRAFT)* |
| `SCN-12` | EN | 1 | 2 | 2 | `DIRECT` | User edit invalidation (substantive inflation + minor paraphrase) | `claim-12-1`: `INVALIDATED_BY_USER_EDIT`; `claim-12-2`: `INVALIDATED_BY_USER_EDIT` |
| `SCN-13` | EN | 2 | 3 | 2 | `DIRECT` (removed/active) | User source deletion cascade (full invalidation + partial support 1/2) | `claim-13-1`: `INVALIDATED_BY_DELETION`; `claim-13-2`: `PARTIALLY_SUPPORTED` |
| `SCN-14` | EN | 2 | 3 | 1 | `DIRECT`, `CONTEXT`, `CONTRADICTS` | Hard negatives: 10x numeric distortion (400ms→40ms) + cross-project conflation + partial anchor | `claim-14-1`: `CONFLICTING` |

### Requirement Checklist Verification

- **0 / 1 / N Facts**: Covered (`SCN-01`=0, `SCN-02`/`SCN-12`=1, `SCN-03`=3).
- **`DIRECT` / `CONTEXT` / `CONTRADICTS`**: All three roles exercised at both `FactSupport` and `ClaimAtomSupport` levels.
- **Safety Invariants Enforced Programmatically**:
  - 0 user-edited Claims set to `SOURCE_VERIFIED` (`SCN-12`).
  - 0 unsupported (`SCN-01`, `SCN-04`, `SCN-08`) or partially supported (`SCN-10`, `SCN-13`, `SCN-14`) Claims set to `SOURCE_VERIFIED`.
  - 0 self-evidencing Claims (no Claim/ClaimAtom appears as a Source Revision, Content Unit, or Fact Support).

## 2. Refetch & Option C Canary Coverage (`REF-01`..`REF-08`)

> **Note (`REVIEW_REQUIRED`)**: Exact `REF-01..08` ID-to-label alignment with Notion `T-004A` must be verified during T-004B review.

| Case | Scenario | Fetch Attempted | `unverifiable_now` | `historical_fact_falsified` | `claim_auto_mutated` | Post-MVP Tags | Expected Verdict (`DRAFT`) |
|---|---|---:|---:|---:|---:|---|---|
| `REF-01` | Same version, identical content hash & locator | `true` | `false` | `false` | `false` | `POST_MVP_AUTO_DETECT_SOURCE_CHANGE`, `POST_MVP_AUTO_REFRESH_CLAIM` | `REFETCH_MATCHED` |
| `REF-02` | Changed content hash & shifted locator (drift) | `true` | `true` | `false` | `false` | `POST_MVP_AUTO_DETECT_SOURCE_CHANGE`, `POST_MVP_AUTO_REFRESH_CLAIM` | `CONTENT_CHANGED_LOCATOR_DRIFT` |
| `REF-03` | Unavailable source (HTTP 404 Not Found) | `true` | `true` | `false` | `false` | — | `CURRENTLY_UNVERIFIABLE` |
| `REF-04` | Partial/truncated response (HTTP 206) | `true` | `true` | `false` | `false` | — | `CURRENTLY_UNVERIFIABLE_PARTIAL` |
| `REF-05` | Policy failure (HTTP 403 revoked OAuth scope) | `true` | `true` | `false` | `false` | — | `REFETCH_POLICY_FAILED` |
| `REF-06` | Unsafe URL rejected pre-fetch (metadata IP, loopback, `file://`, `ftp://`, credentials, private CIDR) | `false` | `true` | `false` | `false` | — | `BLOCKED_UNSAFE_URL` |
| `REF-07` | Option C raw-source non-persistence canary (`CM_SYNTHETIC_CANARY_RAW_TEXT_7F9A2C_DO_NOT_PERSIST`) + negative leak fixture | `true` | `false` | `false` | `false` | — | `CANARY_NON_PERSISTENCE_VERIFIED` |
| `REF-08` | Successful refetch on new upstream revision — no auto-detect / no auto-claim-refresh | `true` | `false` | `false` | `false` | `POST_MVP_AUTO_DETECT_SOURCE_CHANGE`, `POST_MVP_AUTO_REFRESH_CLAIM` | `NEW_REVISION_DETECTED_NO_AUTO_MUTATION` |

## 3. Independent Human Review Queue & T-004B Handoff

Before promoting `expected/oracle_draft.json` or `expected/refetch_oracle_draft.json` to golden (`frozen_for_golden: true`), an **independent human reviewer** in **T-004B** must resolve the following queue:

### A. Contract & Normalization Alignment (`REVIEW_REQUIRED` — Notion unavailable during T-004A draft)
1. **Normalization & Offset Contract**: Confirm whether production parser uses Unicode codepoints, UTF-8 bytes, or UTF-16 code units, and confirm whitespace/Markdown stripping rules vs `fixture-nfc-lf-v0-draft`.
2. **Hash & ID Format**: Confirm production `source_revision_id`, `locator_id`, and `sha256` input framing against `Grounded Claim Pipeline` and `방안 C`.
3. **`REF-01..08` ID Mapping**: Cross-check our `REF-01..08` numbering against the exact Notion `T-004A` refetch table.
4. **Option C Non-Persistence Boundary**: Confirm whether any short excerpt/snippet is transiently allowed or strictly forbidden across persisted DB tables, logs, caches, and traces (`REF-07`).

### B. Semantic Grounding Rubric Sign-Off (`DRAFT` → Golden Gate)
5. **Minor Paraphrase User Edits (`SCN-12` `claim-12-2`)**: Confirm whether minor non-numeric user paraphrases should stay strictly `INVALIDATED_BY_USER_EDIT` until re-verified (current conservative draft) or enter a separate `NEEDS_LIGHT_RECHECK` state.
6. **Planned-as-Completed & Environment Conflation Enum (`SCN-06` `claim-06-2`, `SCN-09` `claim-09-2`)**: Confirm `CONFLICTING` vs `UNSUPPORTED` label when the source explicitly states future plans or unstarted production rollout.
7. **Multi-Atom Conflict Precedence (`SCN-14` `claim-14-1`)**: Confirm `CONFLICTING` takes precedence over `PARTIALLY_SUPPORTED` when a Claim mixes supported, contradicted, and unsupported atoms.
8. **`CONTEXT`-Only Claims (`SCN-04` `claim-04-2`, `SCN-08` `claim-08-2`)**: Confirm `CONTEXT`-only support maps to `UNSUPPORTED` (never `PARTIALLY_SUPPORTED` or `SOURCE_VERIFIED`).
9. **Identity Ambiguity Policy (`SCN-11` `claim-11-1`)**: Confirm exact user-resolution flow and status label (`IDENTITY_AMBIGUOUS` vs `NEEDS_USER_CONFIRMATION`).
10. **Deleted Source Cascade Naming (`SCN-13` `claim-13-1`, `claim-13-2`)**: Confirm `INVALIDATED_BY_DELETION` vs `UNSUPPORTED` for full vs partial deletion cascades.
11. **Refetch Drift Re-Anchoring (`REF-02`)**: Confirm whether offset-shifted text matching is Post-MVP or part of MVP re-anchoring.

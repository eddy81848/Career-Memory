# Isolated Negative Fixtures (Intentionally Broken Variants)

These files are **deliberately separated** from `../cases/` so normal dataset consumers never treat them as valid draft fixtures. They exist solely to prove that `../lint/lint_fixtures.py` catches structural, referential, scope, governance, and official Event-domain invariant violations.

| Variant ID | File | Intentionally Injected Defect | Expected Lint Rule Code(s) |
| --- | --- | --- | --- |
| `NEG-001` | `NEG-001-broken-id-ref.json` | References nonexistent `pr_syn_missing_999` and `cmt_syn_missing_999` | `ID_REFERENCE_INTEGRITY` |
| `NEG-002` | `NEG-002-missing-required-field.json` | Omits required `rationale`, `expected_proposal.approval_status`, and command `expected_version` | `SCHEMA_REQUIRED_FIELD` |
| `NEG-003` | `NEG-003-unknown-as-no-pr.json` | Misclassifies an `UNKNOWN` commit from a `PARTIAL` run as a `DIRECT_COMMIT` candidate | `INVARIANT_UNKNOWN_NOT_NO_PR`, `INVARIANT_PARTIAL_RUN_NO_PROMOTION` |
| `NEG-004` | `NEG-004-composite-double-count.json` | Marks `SUPERSEDED` `USER_COMPOSITE` member as top-level searchable/countable and returns both `USER_COMPOSITE` and member in search | `INVARIANT_NO_COMPOSITE_DOUBLE_COUNT` |
| `NEG-005` | `NEG-005-context-only-auto-merge.json` | Auto-merges 2 PRs via `AUTO_CONTEXT_LINK` without a user manual merge command | `INVARIANT_NO_CONTEXT_AUTO_MERGE` |
| `NEG-006` | `NEG-006-premature-approval-and-od007.json` | Self-approves (`APPROVED`), mistags `B-003` as `MVP`, and includes an OD-007 physical table field | `APPROVAL_STATUS_MUST_BE_DRAFT`, `SCOPE_TAG_POLICY`, `OD007_NO_PHYSICAL_TABLE` |

Additional regression assertions (`PARTIAL_SUCCESS` rejection, `PARTIAL` run event promotion rejection, arbitrary `ACTIVE`/`EXCLUDED`/`DISSOLVED` rejection, `CANNOT_LINK` catch-all wire code rejection, overlapping split `evidence_scope` rejection, non-`SUPERSEDED` merge member rejection, dissolve missing `idempotency_key` or `FACT`/`CLAIM`/`EXPORT` `STALE` rejection, cross-repo merge & nested composite rejection, unmerged PR / incomplete lookup rejection, and multi-workspace aggregation separation) are covered in `../lint/test_lint_fixtures.py`.

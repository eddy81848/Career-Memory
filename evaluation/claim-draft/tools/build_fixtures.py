"""Programmatically build all T-004A/B synthetic dataset candidate fixtures (v0.4.0-owner-review-candidate).

Run from repo/worktree root:
    python3 -B evaluation/claim-draft/tools/build_fixtures.py

Design guarantees:
- Produces a NEW version (`0.4.0-owner-review-candidate`) in `PENDING_OWNER_REVIEW` status
  (ADR-018: AI does NOT record human approval; prior `0.3.0-synthetic-golden` baseline stays
  intact in Git at fa6332d2dda00c59dd922465c1005349017577fe and its approved flags/hashes are
  NEVER copied into this candidate).
- Separates Stage 2A `FactCandidateExtractor` vs Stage 2B `FactSupportValidator` vs CanonicalFact
  promotion, and Stage 3A `ClaimGenerator` vs Stage 3B `ClaimAtomizer` vs Stage 3C `ClaimSupportValidator`
  with globally distinct per-role execution `run_id` metadata (no response reused as both output & evidence).
- Separates official domain values (`PROBLEM|ACTION|TECHNOLOGY|RESULT|ROLE|METRIC` Fact types;
  `SUPPORTED|USER_CONFIRMED|UNSUPPORTED|STALE` Atom statuses; computed `SOURCE_VERIFIED`) from
  `TEST_FIXTURE_ONLY_NOT_DOMAIN_API` labels (`DRAFT_*`, `ENGINEERING_DELIVERY`, etc.).
- Preserves all 18 prior sources, 14 prior scenarios (including SCN-05/06/07/09 & SCN-13 OD-003),
  and existing DEV/TEST family partition boundaries; adds SCN-15 (`CLAIM-05` role pass/fail),
  SCN-16 (`CLAIM-06` metric pass/fail), SCN-12 `claim-12-3`/`claim-12-4` (`CLAIM-08` post-edit pass
  and `USER_CONFIRMED`-only fail), `REF-04` multi-surface canary (`refcase-04b`), and synthetic cases
  for `REF-06` (crash/replay), `REF-07` (approval/export/evidence-view refetch), and `REF-08`
  (limits/dedup/inter-job metadata-only handoff), while explicitly keeping `product_runtime_verified=false`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from locator_hash import (
    CONVENTION_SCOPE,
    NORMALIZATION_POLICY_ID,
    check_url_safety,
    compute_locator_id,
    compute_version_id,
    locate_span,
    normalize_text,
    sha256_prefixed,
    short_hash,
)

SCHEMA_VERSION = "0.4.0-owner-review-candidate"
DATASET_VERSION = "0.4.0-owner-review-candidate"
SUPERSEDES_BASELINE_COMMIT_SHA = "fa6332d2dda00c59dd922465c1005349017577fe"
SUPERSEDES_BASELINE_VERSION = "0.3.0-synthetic-golden"
PRIOR_BASELINE_BUNDLE_SHA256 = "sha256:881b6bb182b65c51c88a09ecf605322167ff439a4fffc37c1d77f07f1ad6b51f"
CANDIDATE_SCOPE = "OWNER_REVIEW_CANDIDATE_SYNTHETIC_ONLY_NOT_APPROVED"
ORACLE_STATUS_PENDING = "PENDING_OWNER_REVIEW"

CANARY_TOKEN_REF04 = "CM_SYNTHETIC_CANARY_RAW_TEXT_7F9A2C_DO_NOT_PERSIST"

# Anti-leakage family-to-partition map: preserves 100% of existing v0.3.0 DEV/TEST assignments
# and places new independent families SCN-15 (DEV) and SCN-16 (TEST) cleanly without cross-source leakage.
FAMILY_PARTITION_MAP = {
    "family-src01": "SYNTHETIC_REGRESSION_DEV",
    "family-src02": "SYNTHETIC_REGRESSION_TEST",
    "family-src03": "SYNTHETIC_REGRESSION_DEV",
    "family-src04": "SYNTHETIC_REGRESSION_DEV",
    "family-src05": "SYNTHETIC_REGRESSION_TEST",
    "family-src06": "SYNTHETIC_REGRESSION_DEV",
    "family-src07": "SYNTHETIC_REGRESSION_TEST",
    "family-src08": "SYNTHETIC_REGRESSION_DEV",
    "family-src09": "SYNTHETIC_REGRESSION_TEST",
    "family-src10": "SYNTHETIC_REGRESSION_DEV",
    "family-src11": "SYNTHETIC_REGRESSION_TEST",
    "family-src12": "SYNTHETIC_REGRESSION_DEV",
    "family-src13": "SYNTHETIC_REGRESSION_TEST",
    "family-src14": "SYNTHETIC_REGRESSION_TEST",
    "family-src15": "SYNTHETIC_REGRESSION_DEV",
    "family-src16": "SYNTHETIC_REGRESSION_TEST",
    "family-src_ref07_canary": "SYNTHETIC_REGRESSION_DEV",
}

REVIEW_REQUIRED_SPECS = [
    "확인 필요 (REVIEW_REQUIRED): Production text normalization policy (CRLF/NFC/whitespace stripping) — fixture uses TEST_FIXTURE_ONLY fixture-nfc-lf-v0-draft",
    "확인 필요 (REVIEW_REQUIRED): Production offset unit standard (Unicode codepoint vs UTF-8 byte vs UTF-16 code unit) — fixture uses TEST_FIXTURE_ONLY fixture-locator-v0-draft",
    "확인 필요 (REVIEW_REQUIRED): Finalized product non-SUPPORTED CanonicalFact status enum names and non-SOURCE_VERIFIED Claim status enum names (Notion direct read unavailable; only SUPPORTED, USER_CONFIRMED, UNSUPPORTED, STALE, DRAFT, SOURCE_VERIFIED, and Fact types PROBLEM|ACTION|TECHNOLOGY|RESULT|ROLE|METRIC are confirmed)",
    "확인 필요 (REVIEW_REQUIRED): Exact FactCandidate DB schema field names for completion_state and uncertainty_level",
    "확인 필요 (REVIEW_REQUIRED): Exact REF-08 production numeric thresholds for max_requests, max_bytes, max_tokens, max_retries, and max_concurrency (fixture thresholds are TEST_FIXTURE_ONLY)",
    "확인 필요 (REVIEW_REQUIRED): Exact REF-07 Export file-header / API field layout for verification metadata (locator_id, span_sha256, source_revision_id, source_content_sha256, checked_at) is NOT frozen here (`TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN`); raw source quotes are strictly excluded from Export either way",
    "확인 필요 (REVIEW_REQUIRED): Exact DB Model §3.8 product table/column names for role attribution & contribution evidence (fixture uses `FIXTURE_SEMANTICS_FOR_DB_MODEL_3_8_NOT_PRODUCT_TABLE` without freezing product DB columns)",
    "확인 필요 (REVIEW_REQUIRED): Option C zero-raw-text runtime verification across live DB, app logs, job queues, caches, and Export (REF-04/REF-06/REF-08 product runtime tests NOT executed)",
]


def _make_source(
    *,
    source_key: str,
    revision_seq: int,
    scenario_ids: list[str],
    source_kind: str,
    uri_slug: str,
    language: str,
    author_label: str,
    author_identity_confidence: str,
    created_at: str,
    raw_text: str,
    deleted_by_user: bool = False,
    contains_canary: bool = False,
    canary_token_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    normalized = normalize_text(raw_text)
    version_id = compute_version_id(source_key, revision_seq, normalized)
    content_hash = sha256_prefixed(normalized)
    record = {
        "source_revision_id": version_id,
        "scenario_ids": scenario_ids,
        "source_key": source_key,
        "revision_seq": revision_seq,
        "source_kind": source_kind,
        "synthetic_uri": f"https://synthetic.example.test/sources/{uri_slug}",
        "language": language,
        "author_label": author_label,
        "author_identity_confidence": author_identity_confidence,
        "created_at": created_at,
        "normalization_policy": NORMALIZATION_POLICY_ID,
        "content_sha256": content_hash,
        "char_length": len(normalized),
        "utf8_byte_length": len(normalized.encode("utf-8")),
        "is_synthetic_fixture": True,
        "deleted_by_user": deleted_by_user,
        "contains_canary": contains_canary,
        "canary_token_id": canary_token_id,
        "raw_text_fixture_only": normalized,
    }
    return record, normalized


def _make_locator(owner_id: str, normalized_text: str, substring: str, occurrence: int | None = None) -> dict[str, Any]:
    span = locate_span(normalized_text, substring, occurrence=occurrence)
    locator_id = compute_locator_id(owner_id, span["char_start"], span["char_end"], span["span_sha256"])
    return {"locator_id": locator_id, **span}


def build_all_artifacts() -> dict[str, dict[str, Any]]:
    """Build all 9 JSON artifacts deterministically in memory (8 dataset files + CANDIDATE_MANIFEST.json)."""
    sources: list[dict[str, Any]] = []
    norm_by_key: dict[str, str] = {}
    rev_id_by_key: dict[str, str] = {}
    hash_by_key: dict[str, str] = {}

    def register_source(key: str, **kwargs: Any) -> str:
        rec, norm = _make_source(source_key=key, **kwargs)
        sources.append(rec)
        norm_by_key[key] = norm
        rev_id_by_key[key] = rec["source_revision_id"]
        hash_by_key[key] = rec["content_sha256"]
        return rec["source_revision_id"]

    # --- Synthetic Source Revisions (18 preserved verbatim + 2 new: src15 for CLAIM-05, src16 for CLAIM-06) ---
    register_source(
        "src01",
        revision_seq=1,
        scenario_ids=["SCN-01"],
        source_kind="SYNTHETIC_MEETING_NOTE",
        uri_slug="scn01-standup-chatter",
        language="mixed",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-04-02T09:00:00Z",
        raw_text=(
            "2026-04-02 주간 스탠드업 메모\n"
            "내일 오전 10시에 커피 마시면서 아젠다를 정리합시다. 아직 확정된 작업 항목은 없습니다.\n"
            "Let's sync tomorrow after coffee; agenda TBD."
        ),
    )
    register_source(
        "src02",
        revision_seq=1,
        scenario_ids=["SCN-02", "REF-01", "REF-02", "REF-03", "REF-05", "REF-06", "REF-07", "REF-08"],
        source_kind="SYNTHETIC_PR_SUMMARY",
        uri_slug="scn02-pr418-billing-webhooks",
        language="en",
        author_label="Alex Kim (@alexkim-synth)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-04-10T14:20:00Z",
        raw_text=(
            "PR #418 — Idempotent Retry Queue for Billing Webhooks\n"
            "Author: Alex Kim (@alexkim-synth)\n\n"
            "I implemented an idempotent retry queue with exponential backoff for billing webhooks "
            "in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts."
        ),
    )
    register_source(
        "src03",
        revision_seq=1,
        scenario_ids=["SCN-03"],
        source_kind="SYNTHETIC_POSTMORTEM",
        uri_slug="scn03-aurora-search-postmortem",
        language="ko",
        author_label="김알렉스 (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-05-05T11:30:00Z",
        raw_text=(
            "프로젝트 오로라 2026년 5월 장애 회고 및 개선 기록\n"
            "작성자: 김알렉스 (Verified Owner)\n\n"
            "1. 검색 인덱스 샤드 재분배 스크립트를 작성하여 운영 클러스터의 p95 검색 지연 시간을 620ms에서 180ms로 단축했다.\n"
            "2. 배포 실패 시 2분 이내로 이전 스냅샷으로 복구하는 자동 롤백 스크립트를 추가했다.\n"
            "3. 슬로우 쿼리 임계값 300ms 초과 시 경보를 발송하는 알림 규칙을 새로 구성했다."
        ),
    )
    register_source(
        "src04",
        revision_seq=1,
        scenario_ids=["SCN-04"],
        source_kind="SYNTHETIC_DESIGN_DOC",
        uri_slug="scn04-q3-reliability-workerpool",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-05-18T10:00:00Z",
        raw_text=(
            "Q3 Reliability Initiative Overview\n"
            "Context: The Q3 reliability initiative aimed to eliminate missed nightly batch runs across billing pipelines.\n\n"
            "Delivery Log (Alex Kim): I migrated the nightly billing job queue from legacy cron scripts to WorkerPool v2 in production."
        ),
    )
    register_source(
        "src05a",
        revision_seq=1,
        scenario_ids=["SCN-05"],
        source_kind="SYNTHETIC_POSTMORTEM",
        uri_slug="scn05-incident77-draft",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-06-01T16:00:00Z",
        raw_text=(
            "Incident #77 Draft Summary (2026-06-01)\n"
            "During the database primary failover, zero checkout requests were dropped."
        ),
    )
    register_source(
        "src05b",
        revision_seq=2,
        scenario_ids=["SCN-05"],
        source_kind="SYNTHETIC_POSTMORTEM",
        uri_slug="scn05-incident77-addendum",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-06-03T09:45:00Z",
        raw_text=(
            "Incident #77 Postmortem Addendum (2026-06-03)\n"
            "Correction: Log audit confirmed that 412 checkout requests returned HTTP 503 during the 90-second failover window; "
            "the earlier zero-drop note was inaccurate."
        ),
    )
    register_source(
        "src06",
        revision_seq=1,
        scenario_ids=["SCN-06"],
        source_kind="SYNTHETIC_RFC_PLAN",
        uri_slug="scn06-q4-storage-rfc",
        language="ko",
        author_label="김알렉스 (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-06-12T08:30:00Z",
        raw_text=(
            "2026 Q4 스토리지 확장 RFC\n"
            "작성자: 김알렉스\n\n"
            "완료 항목: 읽기 전용 복제본 헬스체크 엔드포인트를 구현하여 운영 모니터링에 연동했다.\n"
            "계획 항목: Q4에 user_events 테이블 샤딩을 도입할 예정이며, 다음 달 프로토타입 벤치마크를 진행할 계획이다."
        ),
    )
    register_source(
        "src07",
        revision_seq=1,
        scenario_ids=["SCN-07"],
        source_kind="SYNTHETIC_RETRO_NOTE",
        uri_slug="scn07-sprint19-autoscaler-retro",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-06-20T17:15:00Z",
        raw_text=(
            "Sprint 19 Retro — Autoscaler Experiment\n"
            "Author: Alex Kim\n\n"
            "We did not deploy the canary autoscaler to production; I reverted the Terraform config before rollout "
            "because cold-start spikes persisted, so we did not achieve the target 30% compute cost savings."
        ),
    )
    register_source(
        "src08",
        revision_seq=1,
        scenario_ids=["SCN-08"],
        source_kind="SYNTHETIC_LAUNCH_UPDATE",
        uri_slug="scn08-q2-platform-digest",
        language="en",
        author_label="Platform Reliability Team",
        author_identity_confidence="TEAM_SHARED",
        created_at="2026-07-01T12:00:00Z",
        raw_text=(
            "Q2 Platform Engineering Digest\n"
            "The Platform Reliability team (6 engineers) rebuilt the monorepo CI pipeline, reducing median build duration from 22 minutes to 8 minutes.\n"
            "Within this effort, Alex Kim implemented the distributed build artifact cache layer."
        ),
    )
    register_source(
        "src09",
        revision_seq=1,
        scenario_ids=["SCN-09"],
        source_kind="SYNTHETIC_BENCHMARK_NOTE",
        uri_slug="scn09-staging-index-benchmark",
        language="ko",
        author_label="김알렉스 (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-07-08T13:10:00Z",
        raw_text=(
            "쿼리 최적화 스테이징 벤치마크 메모\n"
            "작성자: 김알렉스\n\n"
            "로컬 스테이징 샌드박스(합성 데이터 1만 건)에서 복합 인덱스 적용 후 조회 p95가 840ms에서 95ms로 감소했다. "
            "운영 환경 배포는 아직 시작하지 않았다."
        ),
    )
    register_source(
        "src10",
        revision_seq=1,
        scenario_ids=["SCN-10"],
        source_kind="SYNTHETIC_LAUNCH_UPDATE",
        uri_slug="scn10-may-product-update",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-06-02T10:30:00Z",
        raw_text=(
            "Monthly Product Update — May 2026\n"
            "Author: Alex Kim\n\n"
            "I shipped the redesigned onboarding checklist UI on May 12. "
            "Total monthly revenue rose 18% in May compared to April, though the sales team also ran our largest annual discount campaign during the same two weeks; "
            "no A/B test or causal attribution study was conducted."
        ),
    )
    register_source(
        "src11a",
        revision_seq=1,
        scenario_ids=["SCN-11"],
        source_kind="SYNTHETIC_ISSUE_COMMENT",
        uri_slug="scn11-issue904-ambiguous-alex",
        language="en",
        author_label="triage-bot (mentions unmapped 'Alex')",
        author_identity_confidence="AMBIGUOUS",
        created_at="2026-07-14T08:00:00Z",
        raw_text=(
            "Issue #904 Triage Note\n"
            "Commenter: triage-bot\n\n"
            "Note: Alex fixed the memory leak in the ingestion worker overnight. "
            "(Workspace directory lists both Alex Kim [Backend] and Alex Kang [Data]; handle not linked.)"
        ),
    )
    register_source(
        "src11b",
        revision_seq=1,
        scenario_ids=["SCN-11"],
        source_kind="SYNTHETIC_PR_SUMMARY",
        uri_slug="scn11-pr905-verified-alexkim",
        language="en",
        author_label="Alex Kim <alex.kim@synthetic.example.test>",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-07-15T11:20:00Z",
        raw_text=(
            "PR #905 — Unit Tests for Ingestion Worker Shutdown\n"
            "Author: Alex Kim <alex.kim@synthetic.example.test> (Verified Account)\n\n"
            "I added graceful shutdown unit tests for the ingestion worker."
        ),
    )
    register_source(
        "src12",
        revision_seq=1,
        scenario_ids=["SCN-12"],
        source_kind="SYNTHETIC_PR_SUMMARY",
        uri_slug="scn12-pr512-connection-pool",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-07-20T15:00:00Z",
        raw_text=(
            "PR #512 — Connection Pool Tuning\n"
            "Author: Alex Kim\n\n"
            "I tuned PostgreSQL connection pool sizing in the checkout service, reducing p99 checkout API latency by 25% under peak load."
        ),
    )
    register_source(
        "src13a",
        revision_seq=1,
        scenario_ids=["SCN-13"],
        source_kind="SYNTHETIC_DESIGN_DOC",
        uri_slug="scn13-deleted-memcached-memo",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-07-22T09:00:00Z",
        raw_text=(
            "Draft Memo — Legacy Cache Removal (Synthetic)\n"
            "Author: Alex Kim\n\n"
            "I deprecated the legacy Memcached cluster and migrated session keys to Redis Cluster."
        ),
        deleted_by_user=True,
    )
    register_source(
        "src13b",
        revision_seq=1,
        scenario_ids=["SCN-13"],
        source_kind="SYNTHETIC_PR_SUMMARY",
        uri_slug="scn13-pr601-redis-dashboard",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-07-23T10:40:00Z",
        raw_text=(
            "PR #601 — Redis Health Dashboard\n"
            "Author: Alex Kim\n\n"
            "I created a Redis Cluster saturation alert dashboard for on-call engineers."
        ),
    )
    register_source(
        "src14",
        revision_seq=1,
        scenario_ids=["SCN-14"],
        source_kind="SYNTHETIC_RETRO_NOTE",
        uri_slug="scn14-h1-engineering-notes",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-07-28T16:30:00Z",
        raw_text=(
            "H1 Engineering Notes\n"
            "Author: Alex Kim\n\n"
            "Project Alpha: I optimized image thumbnail decoding, reducing processing latency from 900ms to 400ms.\n"
            "Project Borealis: I migrated 12 internal documentation pages to the new Markdown linter."
        ),
    )
    # NEW SCN-15: CLAIM-05 Role attribution pass/fail (ROLE fact type with verified contribution vs unrecorded role)
    register_source(
        "src15",
        revision_seq=1,
        scenario_ids=["SCN-15"],
        source_kind="SYNTHETIC_DESIGN_DOC",
        uri_slug="scn15-q1-payments-role-review",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-04-15T10:00:00Z",
        raw_text=(
            "Q1 Payments Platform Architecture Review\n"
            "Author: Alex Kim (Verified Owner)\n\n"
            "During the Q1 2026 payments ledger migration, Alex Kim served as Tech Lead for the ledger migration squad, "
            "authoring the dual-write cutover RFC and signing off on schema rollout reviews.\n"
            "The broader Core Platform group also ran a quarterly security compliance audit led by the Security team, "
            "with no recorded role for Alex Kim in the compliance audit."
        ),
    )
    # NEW SCN-16: CLAIM-06 Metric grounding pass/fail + PROBLEM / TECHNOLOGY / METRIC official Fact types
    register_source(
        "src16",
        revision_seq=1,
        scenario_ids=["SCN-16"],
        source_kind="SYNTHETIC_BENCHMARK_NOTE",
        uri_slug="scn16-checkout-api-april-latency-report",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-05-02T14:00:00Z",
        raw_text=(
            "Production Checkout API Latency Report (Datadog APM, 2026-04-01 to 2026-04-30)\n"
            "Author: Alex Kim (Verified Owner)\n\n"
            "Problem: Checkout API p99 response latency spiked to 920ms during April 2026 peak traffic due to unindexed order-lookup queries.\n"
            "Technology: I applied a PostgreSQL 16 covering composite index (orders_checkout_idx) to optimize checkout order-lookup queries.\n"
            "Metric: In production checkout-api (Datadog APM, 2026-04-01 to 2026-04-30, 30-day window), p99 checkout API latency decreased from 920ms to 210ms (77.2% reduction)."
        ),
    )
    # Dedicated synthetic canary source (preserved verbatim; mapped to official REF-04 원문 비영속)
    register_source(
        "src_ref07_canary",
        revision_seq=1,
        scenario_ids=["REF-04"],
        source_kind="SYNTHETIC_PR_SUMMARY",
        uri_slug="ref07-canary-non-persistence",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-08-01T10:00:00Z",
        raw_text=(
            "PR #700 — Audit Log Index Maintenance (Synthetic Canary Document)\n"
            f"Transient Raw Body Sentinel: {CANARY_TOKEN_REF04}\n\n"
            "I added automated weekly partition pruning for synthetic audit log tables."
        ),
        contains_canary=True,
        canary_token_id="canary-ref04-raw-text",
    )

    # --- Stage 1: Content Units (25 preserved + 5 new for SCN-15/SCN-16 = 30) ---
    content_units: list[dict[str, Any]] = []
    units_by_scenario: dict[str, list[dict[str, Any]]] = {}

    def add_unit(
        cu_id: str,
        scenario_id: str,
        src_key: str,
        unit_index: int,
        unit_type: str,
        exact_text: str,
    ) -> None:
        norm = norm_by_key[src_key]
        loc = _make_locator(cu_id, norm, exact_text)
        cu_rec = {
            "content_unit_id": cu_id,
            "scenario_id": scenario_id,
            "source_revision_id": rev_id_by_key[src_key],
            "unit_index": unit_index,
            "unit_type": unit_type,
            "locator": loc,
            "unit_sha256": loc["span_sha256"],
            "persists_raw_text": False,
        }
        content_units.append(cu_rec)
        units_by_scenario.setdefault(scenario_id, []).append(cu_rec)

    add_unit(
        "cu-01-0",
        "SCN-01",
        "src01",
        0,
        "PARAGRAPH",
        "내일 오전 10시에 커피 마시면서 아젠다를 정리합시다. 아직 확정된 작업 항목은 없습니다.\nLet's sync tomorrow after coffee; agenda TBD.",
    )
    add_unit(
        "cu-02-0",
        "SCN-02",
        "src02",
        0,
        "PARAGRAPH",
        "I implemented an idempotent retry queue with exponential backoff for billing webhooks in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts.",
    )
    add_unit(
        "cu-03-0",
        "SCN-03",
        "src03",
        0,
        "BULLET_ITEM",
        "1. 검색 인덱스 샤드 재분배 스크립트를 작성하여 운영 클러스터의 p95 검색 지연 시간을 620ms에서 180ms로 단축했다.",
    )
    add_unit(
        "cu-03-1",
        "SCN-03",
        "src03",
        1,
        "BULLET_ITEM",
        "2. 배포 실패 시 2분 이내로 이전 스냅샷으로 복구하는 자동 롤백 스크립트를 추가했다.",
    )
    add_unit(
        "cu-03-2",
        "SCN-03",
        "src03",
        2,
        "BULLET_ITEM",
        "3. 슬로우 쿼리 임계값 300ms 초과 시 경보를 발송하는 알림 규칙을 새로 구성했다.",
    )
    add_unit(
        "cu-04-0",
        "SCN-04",
        "src04",
        0,
        "SECTION_NOTE",
        "Context: The Q3 reliability initiative aimed to eliminate missed nightly batch runs across billing pipelines.",
    )
    add_unit(
        "cu-04-1",
        "SCN-04",
        "src04",
        1,
        "PARAGRAPH",
        "Delivery Log (Alex Kim): I migrated the nightly billing job queue from legacy cron scripts to WorkerPool v2 in production.",
    )
    add_unit(
        "cu-05-0",
        "SCN-05",
        "src05a",
        0,
        "PARAGRAPH",
        "During the database primary failover, zero checkout requests were dropped.",
    )
    add_unit(
        "cu-05-1",
        "SCN-05",
        "src05b",
        0,
        "PARAGRAPH",
        "Correction: Log audit confirmed that 412 checkout requests returned HTTP 503 during the 90-second failover window; the earlier zero-drop note was inaccurate.",
    )
    add_unit(
        "cu-06-0",
        "SCN-06",
        "src06",
        0,
        "SECTION_NOTE",
        "완료 항목: 읽기 전용 복제본 헬스체크 엔드포인트를 구현하여 운영 모니터링에 연동했다.",
    )
    add_unit(
        "cu-06-1",
        "SCN-06",
        "src06",
        1,
        "SECTION_NOTE",
        "계획 항목: Q4에 user_events 테이블 샤딩을 도입할 예정이며, 다음 달 프로토타입 벤치마크를 진행할 계획이다.",
    )
    add_unit(
        "cu-07-0",
        "SCN-07",
        "src07",
        0,
        "PARAGRAPH",
        "We did not deploy the canary autoscaler to production; I reverted the Terraform config before rollout because cold-start spikes persisted, so we did not achieve the target 30% compute cost savings.",
    )
    add_unit(
        "cu-08-0",
        "SCN-08",
        "src08",
        0,
        "PARAGRAPH",
        "The Platform Reliability team (6 engineers) rebuilt the monorepo CI pipeline, reducing median build duration from 22 minutes to 8 minutes.",
    )
    add_unit(
        "cu-08-1",
        "SCN-08",
        "src08",
        1,
        "PARAGRAPH",
        "Within this effort, Alex Kim implemented the distributed build artifact cache layer.",
    )
    add_unit(
        "cu-09-0",
        "SCN-09",
        "src09",
        0,
        "PARAGRAPH",
        "로컬 스테이징 샌드박스(합성 데이터 1만 건)에서 복합 인덱스 적용 후 조회 p95가 840ms에서 95ms로 감소했다. 운영 환경 배포는 아직 시작하지 않았다.",
    )
    add_unit(
        "cu-10-0",
        "SCN-10",
        "src10",
        0,
        "PARAGRAPH",
        "I shipped the redesigned onboarding checklist UI on May 12.",
    )
    add_unit(
        "cu-10-1",
        "SCN-10",
        "src10",
        1,
        "PARAGRAPH",
        "Total monthly revenue rose 18% in May compared to April, though the sales team also ran our largest annual discount campaign during the same two weeks; no A/B test or causal attribution study was conducted.",
    )
    add_unit(
        "cu-11-0",
        "SCN-11",
        "src11a",
        0,
        "COMMENT_BLOCK",
        "Note: Alex fixed the memory leak in the ingestion worker overnight. (Workspace directory lists both Alex Kim [Backend] and Alex Kang [Data]; handle not linked.)",
    )
    add_unit(
        "cu-11-1",
        "SCN-11",
        "src11b",
        0,
        "PARAGRAPH",
        "I added graceful shutdown unit tests for the ingestion worker.",
    )
    add_unit(
        "cu-12-0",
        "SCN-12",
        "src12",
        0,
        "PARAGRAPH",
        "I tuned PostgreSQL connection pool sizing in the checkout service, reducing p99 checkout API latency by 25% under peak load.",
    )
    add_unit(
        "cu-13-0",
        "SCN-13",
        "src13a",
        0,
        "PARAGRAPH",
        "I deprecated the legacy Memcached cluster and migrated session keys to Redis Cluster.",
    )
    add_unit(
        "cu-13-1",
        "SCN-13",
        "src13b",
        0,
        "PARAGRAPH",
        "I created a Redis Cluster saturation alert dashboard for on-call engineers.",
    )
    add_unit(
        "cu-14-0",
        "SCN-14",
        "src14",
        0,
        "SECTION_NOTE",
        "Project Alpha: I optimized image thumbnail decoding, reducing processing latency from 900ms to 400ms.",
    )
    add_unit(
        "cu-14-1",
        "SCN-14",
        "src14",
        1,
        "SECTION_NOTE",
        "Project Borealis: I migrated 12 internal documentation pages to the new Markdown linter.",
    )
    add_unit(
        "cu-15-0",
        "SCN-15",
        "src15",
        0,
        "PARAGRAPH",
        "During the Q1 2026 payments ledger migration, Alex Kim served as Tech Lead for the ledger migration squad, authoring the dual-write cutover RFC and signing off on schema rollout reviews.",
    )
    add_unit(
        "cu-15-1",
        "SCN-15",
        "src15",
        1,
        "PARAGRAPH",
        "The broader Core Platform group also ran a quarterly security compliance audit led by the Security team, with no recorded role for Alex Kim in the compliance audit.",
    )
    add_unit(
        "cu-16-0",
        "SCN-16",
        "src16",
        0,
        "SECTION_NOTE",
        "Problem: Checkout API p99 response latency spiked to 920ms during April 2026 peak traffic due to unindexed order-lookup queries.",
    )
    add_unit(
        "cu-16-1",
        "SCN-16",
        "src16",
        1,
        "SECTION_NOTE",
        "Technology: I applied a PostgreSQL 16 covering composite index (orders_checkout_idx) to optimize checkout order-lookup queries.",
    )
    add_unit(
        "cu-16-2",
        "SCN-16",
        "src16",
        2,
        "SECTION_NOTE",
        "Metric: In production checkout-api (Datadog APM, 2026-04-01 to 2026-04-30, 30-day window), p99 checkout API latency decreased from 920ms to 210ms (77.2% reduction).",
    )
    add_unit(
        "cu-ref07-0",
        "REF-04",
        "src_ref07_canary",
        0,
        "PARAGRAPH",
        "I added automated weekly partition pruning for synthetic audit log tables.",
    )

    # --- Stage 2: Separated FactCandidateExtractor (2A), FactSupportValidator (2B), & CanonicalFact Promotion ---
    extractor_runs: list[dict[str, Any]] = []
    validator_runs: list[dict[str, Any]] = []
    fact_candidates: list[dict[str, Any]] = []
    fact_support_validations: list[dict[str, Any]] = []
    canonical_facts: list[dict[str, Any]] = []
    fact_supports: list[dict[str, Any]] = []
    expected_facts: list[dict[str, Any]] = []

    extractor_run_by_scn: dict[str, str] = {}
    validator_run_by_scn: dict[str, str] = {}
    for scn_idx in range(1, 17):
        sid = f"SCN-{scn_idx:02d}"
        ext_run_id = f"run-fact-extract-scn{scn_idx:02d}-v1"
        extractor_run_by_scn[sid] = ext_run_id
        scn_units = units_by_scenario.get(sid, [])
        extractor_runs.append(
            {
                "run_id": ext_run_id,
                "scenario_id": sid,
                "pipeline_role": "FactCandidateExtractor",
                "stage_code": "STAGE_2A_FACT_CANDIDATE_EXTRACTION",
                "model_or_stub_version": "stub-fact-extractor-v0.4",
                "prompt_version": "stub-prompt-fact-extract-v0.4",
                "execution_mode": "SYNTHETIC_STUB_OFFLINE",
                "input_version_refs": [
                    {
                        "entity_type": "CONTENT_UNIT",
                        "entity_id": cu["content_unit_id"],
                        "version_or_hash": cu["unit_sha256"],
                    }
                    for cu in scn_units
                ],
                "persists_raw_text": False,
            }
        )

    cand_refs_by_scn: dict[str, list[dict[str, str]]] = {}

    def add_candidate(
        cand_id: str,
        scenario_id: str,
        src_key: str,
        cu_id: str,
        span_substring: str,
        proposed_fact_text: str,
        fact_type: str,
        completion_state: str,
        uncertainty_level: str,
    ) -> dict[str, Any]:
        loc = _make_locator(cand_id, norm_by_key[src_key], span_substring)
        rec = {
            "fact_candidate_id": cand_id,
            "scenario_id": scenario_id,
            "extractor_run_id": extractor_run_by_scn[scenario_id],
            "extractor_version": "stub-fact-extractor-v0.4",
            "source_revision_id": rev_id_by_key[src_key],
            "content_unit_id": cu_id,
            "locator": loc,
            "proposed_fact_text": proposed_fact_text,
            "fact_type": fact_type,
            "completion_state": completion_state,
            "uncertainty_level": uncertainty_level,
            "candidate_status": "EXTRACTED_UNVALIDATED",
            "persists_raw_text": False,
        }
        fact_candidates.append(rec)
        cand_refs_by_scn.setdefault(scenario_id, []).append(
            {
                "entity_type": "FACT_CANDIDATE",
                "entity_id": cand_id,
                "version_or_hash": loc["span_sha256"],
            }
        )
        return rec

    def _get_or_init_val_run_id(scenario_id: str) -> str:
        if scenario_id not in validator_run_by_scn:
            scn_num = scenario_id.split("-")[1]
            validator_run_by_scn[scenario_id] = f"run-fact-validate-scn{scn_num}-v1"
        return validator_run_by_scn[scenario_id]

    def add_validation(
        val_id: str,
        scenario_id: str,
        cand_id: str,
        src_key: str,
        cu_id: str,
        span_substring: str,
        support_role: str,
        promotion_decision: str,
        promoted_fact_id: str | None,
        rejection_reason: str | None = None,
    ) -> None:
        vrun_id = _get_or_init_val_run_id(scenario_id)
        loc = _make_locator(val_id, norm_by_key[src_key], span_substring)
        fact_support_validations.append(
            {
                "validation_id": val_id,
                "scenario_id": scenario_id,
                "fact_candidate_id": cand_id,
                "validator_run_id": vrun_id,
                "source_revision_id": rev_id_by_key[src_key],
                "content_unit_id": cu_id,
                "verified_locator": loc,
                "revision_and_locator_verified": True,
                "support_role": support_role,
                "promotion_decision": promotion_decision,
                "promoted_canonical_fact_id": promoted_fact_id,
                "rejection_reason": rejection_reason,
                "persists_raw_text": False,
            }
        )

    def add_fact(
        fact_id: str,
        cand_id: str,
        scenario_id: str,
        fact_text: str,
        fact_type: str,
        legacy_test_category: str,
        modality: str,
        environment_scope: str,
        attribution_scope: str,
        *,
        identity_status: str = "RESOLVED",
        deleted_by_user: bool = False,
        fact_support_status: str = "SUPPORTED",
        is_active_supported_fact: bool = True,
        metric_details: dict[str, Any] | None = None,
        role_attribution_grounding: dict[str, Any] | None = None,
    ) -> None:
        vrun_id = _get_or_init_val_run_id(scenario_id)
        fixture_fact_sha256 = sha256_prefixed(f"{fact_id}|{fact_type}|{fact_text}")
        entry: dict[str, Any] = {
            "canonical_fact_id": fact_id,
            "scenario_id": scenario_id,
            "source_candidate_id": cand_id,
            "validator_run_id": vrun_id,
            "fact_text": fact_text,
            "fact_type": fact_type,
            "fact_support_status": fact_support_status,
            "is_active_supported_fact": is_active_supported_fact,
            "modality": modality,
            "environment_scope": environment_scope,
            "attribution_scope": attribution_scope,
            "identity_status": identity_status,
            "deleted_by_user": deleted_by_user,
            "fixture_classification": {
                "label_scope": "TEST_FIXTURE_ONLY_NOT_DOMAIN_API",
                "legacy_test_category": legacy_test_category,
                "fixture_fact_sha256": fixture_fact_sha256,
            },
        }
        if metric_details is not None:
            entry["metric_details"] = metric_details
        if role_attribution_grounding is not None:
            entry["role_attribution_grounding"] = role_attribution_grounding
        canonical_facts.append(entry)

    def add_fact_support(
        fs_id: str,
        scenario_id: str,
        fact_id: str,
        src_key: str,
        cu_id: str,
        support_role: str,
        span_substring: str,
    ) -> None:
        vrun_id = _get_or_init_val_run_id(scenario_id)
        loc = _make_locator(fs_id, norm_by_key[src_key], span_substring)
        fact_supports.append(
            {
                "fact_support_id": fs_id,
                "scenario_id": scenario_id,
                "canonical_fact_id": fact_id,
                "source_revision_id": rev_id_by_key[src_key],
                "content_unit_id": cu_id,
                "validator_run_id": vrun_id,
                "support_role": support_role,
                "locator": loc,
                "persists_raw_text": False,
            }
        )

    def add_expected_fact(
        cand_id: str,
        scenario_id: str,
        fact_type: str,
        roles: list[str],
        promotion_decision: str,
        promoted_fact_id: str | None,
        active_supported: bool,
        rationale: str,
    ) -> None:
        expected_facts.append(
            {
                "fact_candidate_id": cand_id,
                "scenario_id": scenario_id,
                "expected_fact_type": fact_type,
                "expected_validator_support_roles": roles,
                "expected_promotion_decision": promotion_decision,
                "expected_canonical_fact_id": promoted_fact_id,
                "expected_active_supported_fact": active_supported,
                "oracle_status": ORACLE_STATUS_PENDING,
                "review_required": True,
                "rationale": rationale,
            }
        )

    # SCN-01: 0 candidates, 0 canonical facts
    # SCN-02: 1 candidate -> 1 promoted ACTION fact
    span_02_1 = "I implemented an idempotent retry queue with exponential backoff for billing webhooks in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts."
    add_candidate("fcand-02-1", "SCN-02", "src02", "cu-02-0", span_02_1, "Implemented an idempotent retry queue with exponential backoff for billing webhooks in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts.", "ACTION", "COMPLETED", "QUALIFIED_OR_CONDITIONAL")
    add_validation("fval-02-1", "SCN-02", "fcand-02-1", "src02", "cu-02-0", span_02_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-02-1")
    add_fact("fact-02-1", "fcand-02-1", "SCN-02", "Implemented an idempotent retry queue with exponential backoff for billing webhooks in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts.", "ACTION", "ENGINEERING_DELIVERY", "COMPLETED", "UNSPECIFIED", "INDIVIDUAL")
    add_fact_support("fs-02-1", "SCN-02", "fact-02-1", "src02", "cu-02-0", "DIRECT", span_02_1)
    add_expected_fact("fcand-02-1", "SCN-02", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-02-1", True, "Single unambiguous PR sentence validated DIRECT -> promoted to active SUPPORTED ACTION fact.")

    # SCN-03: 3 candidates -> 3 promoted facts (METRIC, ACTION, ACTION)
    span_03_1 = "검색 인덱스 샤드 재분배 스크립트를 작성하여 운영 클러스터의 p95 검색 지연 시간을 620ms에서 180ms로 단축했다."
    add_candidate("fcand-03-1", "SCN-03", "src03", "cu-03-0", span_03_1, "검색 인덱스 샤드 재분배 스크립트를 작성하여 운영 클러스터 p95 검색 지연을 620ms에서 180ms로 단축했다.", "METRIC", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-03-1", "SCN-03", "fcand-03-1", "src03", "cu-03-0", span_03_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-03-1")
    add_fact(
        "fact-03-1",
        "fcand-03-1",
        "SCN-03",
        "검색 인덱스 샤드 재분배 스크립트를 작성하여 운영 클러스터 p95 검색 지연을 620ms에서 180ms로 단축했다.",
        "METRIC",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
        metric_details={
            "value": "620 -> 180",
            "unit": "ms (p95 search latency)",
            "period": "2026-05 postmortem window (exact dates unspecified in source)",
            "source_basis": "Project Aurora May 2026 postmortem record",
            "environment_scope": "PRODUCTION",
            "measurement_scope": "운영 클러스터 p95 검색 지연 시간",
        },
    )
    add_fact_support("fs-03-1", "SCN-03", "fact-03-1", "src03", "cu-03-0", "DIRECT", span_03_1)
    add_expected_fact("fcand-03-1", "SCN-03", "METRIC", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-03-1", True, "Korean bullet 1 DIRECT-verified -> promoted to SUPPORTED METRIC fact in production scope.")

    span_03_2 = "배포 실패 시 2분 이내로 이전 스냅샷으로 복구하는 자동 롤백 스크립트를 추가했다."
    add_candidate("fcand-03-2", "SCN-03", "src03", "cu-03-1", span_03_2, "배포 실패 시 2분 이내 이전 스냅샷 복구용 자동 롤백 스크립트를 추가했다.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-03-2", "SCN-03", "fcand-03-2", "src03", "cu-03-1", span_03_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-03-2")
    add_fact("fact-03-2", "fcand-03-2", "SCN-03", "배포 실패 시 2분 이내 이전 스냅샷 복구용 자동 롤백 스크립트를 추가했다.", "ACTION", "ENGINEERING_DELIVERY", "COMPLETED", "UNSPECIFIED", "INDIVIDUAL")
    add_fact_support("fs-03-2", "SCN-03", "fact-03-2", "src03", "cu-03-1", "DIRECT", span_03_2)
    add_expected_fact("fcand-03-2", "SCN-03", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-03-2", True, "Korean bullet 2 DIRECT-verified -> promoted to SUPPORTED ACTION fact.")

    span_03_3 = "슬로우 쿼리 임계값 300ms 초과 시 경보를 발송하는 알림 규칙을 새로 구성했다."
    add_candidate("fcand-03-3", "SCN-03", "src03", "cu-03-2", span_03_3, "슬로우 쿼리 300ms 초과 시 경보를 발송하는 알림 규칙을 새로 구성했다.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-03-3", "SCN-03", "fcand-03-3", "src03", "cu-03-2", span_03_3, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-03-3")
    add_fact("fact-03-3", "fcand-03-3", "SCN-03", "슬로우 쿼리 300ms 초과 시 경보를 발송하는 알림 규칙을 새로 구성했다.", "ACTION", "ENGINEERING_DELIVERY", "COMPLETED", "UNSPECIFIED", "INDIVIDUAL")
    add_fact_support("fs-03-3", "SCN-03", "fact-03-3", "src03", "cu-03-2", "DIRECT", span_03_3)
    add_expected_fact("fcand-03-3", "SCN-03", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-03-3", True, "Korean bullet 3 DIRECT-verified -> promoted to SUPPORTED ACTION fact.")

    # SCN-04: DIRECT + CONTEXT vs CONTEXT-only
    span_04_1 = "I migrated the nightly billing job queue from legacy cron scripts to WorkerPool v2 in production."
    span_04_ctx = "The Q3 reliability initiative aimed to eliminate missed nightly batch runs across billing pipelines."
    add_candidate("fcand-04-1", "SCN-04", "src04", "cu-04-1", span_04_1, "Migrated the nightly billing job queue from legacy cron scripts to WorkerPool v2 in production.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-04-1", "SCN-04", "fcand-04-1", "src04", "cu-04-1", span_04_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-04-1")
    add_validation("fval-04-2", "SCN-04", "fcand-04-1", "src04", "cu-04-0", span_04_ctx, "CONTEXT", "PROMOTE_TO_CANONICAL_FACT", "fact-04-1")
    add_fact("fact-04-1", "fcand-04-1", "SCN-04", "Migrated the nightly billing job queue from legacy cron scripts to WorkerPool v2 in production.", "ACTION", "ENGINEERING_DELIVERY", "COMPLETED", "PRODUCTION", "INDIVIDUAL")
    add_fact_support("fs-04-1", "SCN-04", "fact-04-1", "src04", "cu-04-1", "DIRECT", span_04_1)
    add_fact_support("fs-04-2", "SCN-04", "fact-04-1", "src04", "cu-04-0", "CONTEXT", span_04_ctx)
    add_expected_fact("fcand-04-1", "SCN-04", "ACTION", ["DIRECT", "CONTEXT"], "PROMOTE_TO_CANONICAL_FACT", "fact-04-1", True, "Delivery log DIRECT-supports migration with Q3 initiative goal as CONTEXT.")

    add_candidate("fcand-04-2", "SCN-04", "src04", "cu-04-0", span_04_ctx, "The Q3 reliability initiative had a stated goal to eliminate missed nightly batch runs across billing pipelines.", "PROBLEM", "PLANNED", "OBSERVATION_TIME_PLAN")
    add_validation("fval-04-3", "SCN-04", "fcand-04-2", "src04", "cu-04-0", span_04_ctx, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-04-2")
    add_fact("fact-04-2", "fcand-04-2", "SCN-04", "The Q3 reliability initiative had a stated goal to eliminate missed nightly batch runs across billing pipelines.", "PROBLEM", "PROJECT_CONTEXT", "PLANNED", "UNSPECIFIED", "TEAM")
    add_fact_support("fs-04-3", "SCN-04", "fact-04-2", "src04", "cu-04-0", "DIRECT", span_04_ctx)
    add_expected_fact("fcand-04-2", "SCN-04", "PROBLEM", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-04-2", True, "Context section DIRECT-supports that eliminating missed nightly batch runs was a stated goal (not a completed 100% outcome).")

    # SCN-05: Representative CORRECTION case (correction sentence is NOT deleted — it refutes fact-05-1 and directly grounds fact-05-2)
    span_05_1 = "During the database primary failover, zero checkout requests were dropped."
    span_05_contra = "412 checkout requests returned HTTP 503 during the 90-second failover window; the earlier zero-drop note was inaccurate."
    span_05_2 = "Log audit confirmed that 412 checkout requests returned HTTP 503 during the 90-second failover window"
    add_candidate("fcand-05-1", "SCN-05", "src05a", "cu-05-0", span_05_1, "Initial incident #77 draft (2026-06-01) asserted zero checkout requests were dropped during failover (later refuted by 2026-06-03 log audit addendum; executor unspecified).", "RESULT", "CORRECTED", "REFUTED_BY_CORRECTION")
    add_validation("fval-05-1", "SCN-05", "fcand-05-1", "src05a", "cu-05-0", span_05_1, "DIRECT", "PROMOTE_AS_CONFLICTING_RECORD", "fact-05-1")
    add_validation("fval-05-2", "SCN-05", "fcand-05-1", "src05b", "cu-05-1", span_05_contra, "CONTRADICTS", "PROMOTE_AS_CONFLICTING_RECORD", "fact-05-1")
    add_fact(
        "fact-05-1",
        "fcand-05-1",
        "SCN-05",
        "Initial incident #77 draft (2026-06-01) asserted zero checkout requests were dropped during failover (later refuted by 2026-06-03 log audit addendum; executor unspecified).",
        "RESULT",
        "INCIDENT_RECORD",
        "COMPLETED",
        "UNSPECIFIED",
        "UNSPECIFIED",
        fact_support_status="CONFLICTING_REVIEW_REQUIRED",
        is_active_supported_fact=False,
    )
    add_fact_support("fs-05-1", "SCN-05", "fact-05-1", "src05a", "cu-05-0", "DIRECT", span_05_1)
    add_fact_support("fs-05-2", "SCN-05", "fact-05-1", "src05b", "cu-05-1", "CONTRADICTS", span_05_contra)
    add_expected_fact("fcand-05-1", "SCN-05", "RESULT", ["DIRECT", "CONTRADICTS"], "PROMOTE_AS_CONFLICTING_RECORD", "fact-05-1", False, "Draft zero-drop assertion is preserved as a conflicting record (NOT active SUPPORTED fact) because addendum src05b explicitly CONTRADICTS it.")

    add_candidate("fcand-05-2", "SCN-05", "src05b", "cu-05-1", span_05_2, "Postmortem addendum (2026-06-03) records that log audit confirmed 412 checkout requests returned HTTP 503 during the 90-second failover window (individual auditor unspecified).", "PROBLEM", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-05-3", "SCN-05", "fcand-05-2", "src05b", "cu-05-1", span_05_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-05-2")
    add_fact("fact-05-2", "fcand-05-2", "SCN-05", "Postmortem addendum (2026-06-03) records that log audit confirmed 412 checkout requests returned HTTP 503 during the 90-second failover window (individual auditor unspecified).", "PROBLEM", "CORRECTION_NOTICE", "COMPLETED", "UNSPECIFIED", "UNSPECIFIED")
    add_fact_support("fs-05-3", "SCN-05", "fact-05-2", "src05b", "cu-05-1", "DIRECT", span_05_2)
    add_expected_fact("fcand-05-2", "SCN-05", "PROBLEM", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-05-2", True, "Correction addendum is NOT discarded: it DIRECT-supports the corrected 412 HTTP 503 incident record while leaving individual auditor unspecified.")

    # SCN-06: Representative PLAN vs COMPLETED case (+ explicit REJECTED plan-as-completed candidate!)
    span_06_1 = "읽기 전용 복제본 헬스체크 엔드포인트를 구현하여 운영 모니터링에 연동했다."
    span_06_2 = "Q4에 user_events 테이블 샤딩을 도입할 예정이며, 다음 달 프로토타입 벤치마크를 진행할 계획이다."
    add_candidate("fcand-06-1", "SCN-06", "src06", "cu-06-0", span_06_1, "읽기 전용 복제본 헬스체크 엔드포인트를 구현하여 운영 모니터링에 연동했다.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-06-1", "SCN-06", "fcand-06-1", "src06", "cu-06-0", span_06_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-06-1")
    add_fact("fact-06-1", "fcand-06-1", "SCN-06", "읽기 전용 복제본 헬스체크 엔드포인트를 구현하여 운영 모니터링에 연동했다.", "ACTION", "ENGINEERING_DELIVERY", "COMPLETED", "PRODUCTION", "INDIVIDUAL")
    add_fact_support("fs-06-1", "SCN-06", "fact-06-1", "src06", "cu-06-0", "DIRECT", span_06_1)
    add_expected_fact("fcand-06-1", "SCN-06", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-06-1", True, "Completed section DIRECT-supports endpoint implementation and production monitoring integration.")

    add_candidate("fcand-06-2", "SCN-06", "src06", "cu-06-1", span_06_2, "RFC 작성 시점 기준으로 Q4에 user_events 테이블 샤딩 도입 및 다음 달 프로토타입 벤치마크 진행을 계획하고 있다(관찰 시점 기준 계획 항목; 이후 완료 여부 미확인).", "ACTION", "PLANNED", "OBSERVATION_TIME_PLAN")
    add_validation("fval-06-2", "SCN-06", "fcand-06-2", "src06", "cu-06-1", span_06_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-06-2")
    add_fact("fact-06-2", "fcand-06-2", "SCN-06", "RFC 작성 시점 기준으로 Q4에 user_events 테이블 샤딩 도입 및 다음 달 프로토타입 벤치마크 진행을 계획하고 있다(관찰 시점 기준 계획 항목; 이후 완료 여부 미확인).", "ACTION", "PLAN_OR_PROPOSAL", "PLANNED", "UNSPECIFIED", "INDIVIDUAL")
    add_fact_support("fs-06-2", "SCN-06", "fact-06-2", "src06", "cu-06-1", "DIRECT", span_06_2)
    add_expected_fact("fcand-06-2", "SCN-06", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-06-2", True, "Plan sentence is NOT deleted: it DIRECT-supports a PLANNED fact at observation time, which contradicts completion overclaims.")

    # Negative extractor candidate: mistakenly elevates planned Q4 sharding to COMPLETED -> REJECTED by FactSupportValidator!
    add_candidate("fcand-06-3-rejected", "SCN-06", "src06", "cu-06-1", span_06_2, "운영 환경의 user_events 테이블 샤딩과 프로토타입 벤치마크를 완료했다.", "ACTION", "COMPLETED", "UNVERIFIED_EXTRACTOR_OVERCLAIM")
    add_validation(
        "fval-06-3-rejected",
        "SCN-06",
        "fcand-06-3-rejected",
        "src06",
        "cu-06-1",
        span_06_2,
        "CONTRADICTS",
        "REJECT_DO_NOT_PROMOTE",
        None,
        rejection_reason="RFC span explicitly states future Q4 plan ('도입할 예정', '진행할 계획이다'); FactSupportValidator forbids promoting planned work to a completed CanonicalFact.",
    )
    add_expected_fact("fcand-06-3-rejected", "SCN-06", "ACTION", ["CONTRADICTS"], "REJECT_DO_NOT_PROMOTE", None, False, "Extractor over-claim promoting planned RFC work to COMPLETED must be rejected by FactSupportValidator and never promoted.")

    # SCN-07: Representative NEGATION case (+ explicit REJECTED negation-reversal candidate!)
    span_07_1 = "I reverted the Terraform config before rollout because cold-start spikes persisted"
    span_07_2 = "We did not deploy the canary autoscaler to production"
    span_07_3 = "we did not achieve the target 30% compute cost savings."
    add_candidate("fcand-07-1", "SCN-07", "src07", "cu-07-0", span_07_1, "Reverted the canary autoscaler Terraform config before rollout because cold-start spikes persisted.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-07-1", "SCN-07", "fcand-07-1", "src07", "cu-07-0", span_07_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-07-1")
    add_fact("fact-07-1", "fcand-07-1", "SCN-07", "Reverted the canary autoscaler Terraform config before rollout because cold-start spikes persisted.", "ACTION", "ENGINEERING_DELIVERY", "COMPLETED", "STAGING_TEST", "INDIVIDUAL")
    add_fact_support("fs-07-1", "SCN-07", "fact-07-1", "src07", "cu-07-0", "DIRECT", span_07_1)
    add_expected_fact("fcand-07-1", "SCN-07", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-07-1", True, "Rollback action is DIRECT-supported and promoted.")

    add_candidate("fcand-07-2", "SCN-07", "src07", "cu-07-0", span_07_2, "Did not deploy the canary autoscaler to production.", "ACTION", "NEGATED", "EXPLICIT_CERTAIN")
    add_validation("fval-07-2", "SCN-07", "fcand-07-2", "src07", "cu-07-0", span_07_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-07-2")
    add_fact("fact-07-2", "fcand-07-2", "SCN-07", "Did not deploy the canary autoscaler to production.", "ACTION", "NEGATIVE_OUTCOME", "NEGATED", "PRODUCTION", "TEAM")
    add_fact_support("fs-07-2", "SCN-07", "fact-07-2", "src07", "cu-07-0", "DIRECT", span_07_2)
    add_expected_fact("fcand-07-2", "SCN-07", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-07-2", True, "Negation clause is NOT deleted: it DIRECT-supports negated non-deployment fact-07-2 to contradict false deployment claims.")

    add_candidate("fcand-07-3", "SCN-07", "src07", "cu-07-0", span_07_3, "Did not achieve the target 30% compute cost savings.", "RESULT", "NEGATED", "EXPLICIT_CERTAIN")
    add_validation("fval-07-3", "SCN-07", "fcand-07-3", "src07", "cu-07-0", span_07_3, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-07-3")
    add_fact("fact-07-3", "fcand-07-3", "SCN-07", "Did not achieve the target 30% compute cost savings.", "RESULT", "NEGATIVE_OUTCOME", "NEGATED", "UNSPECIFIED", "TEAM")
    add_fact_support("fs-07-3", "SCN-07", "fact-07-3", "src07", "cu-07-0", "DIRECT", span_07_3)
    add_expected_fact("fcand-07-3", "SCN-07", "RESULT", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-07-3", True, "Negation clause is NOT deleted: it DIRECT-supports negated non-savings fact-07-3 to contradict false savings claims.")

    # Negative extractor candidate: mistakenly strips negation -> REJECTED by FactSupportValidator!
    add_candidate("fcand-07-4-rejected", "SCN-07", "src07", "cu-07-0", span_07_2, "Deployed the canary autoscaler to production.", "ACTION", "COMPLETED", "UNVERIFIED_EXTRACTOR_OVERCLAIM")
    add_validation(
        "fval-07-4-rejected",
        "SCN-07",
        "fcand-07-4-rejected",
        "src07",
        "cu-07-0",
        span_07_2,
        "CONTRADICTS",
        "REJECT_DO_NOT_PROMOTE",
        None,
        rejection_reason="Source explicitly negates production deployment ('We did not deploy'); FactSupportValidator rejects positive promotion.",
    )
    add_expected_fact("fcand-07-4-rejected", "SCN-07", "ACTION", ["CONTRADICTS"], "REJECT_DO_NOT_PROMOTE", None, False, "Extractor dropping explicit negation must be rejected by FactSupportValidator and never promoted.")

    # SCN-08: Team vs Individual (+ explicit REJECTED team-as-individual candidate!)
    span_08_1 = "The Platform Reliability team (6 engineers) rebuilt the monorepo CI pipeline, reducing median build duration from 22 minutes to 8 minutes."
    span_08_2 = "Alex Kim implemented the distributed build artifact cache layer."
    span_08_ctx = "The Platform Reliability team (6 engineers) rebuilt the monorepo CI pipeline"
    add_candidate("fcand-08-1", "SCN-08", "src08", "cu-08-0", span_08_1, "Platform Reliability team (6 engineers) rebuilt the monorepo CI pipeline, reducing median build duration from 22 minutes to 8 minutes.", "RESULT", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-08-1", "SCN-08", "fcand-08-1", "src08", "cu-08-0", span_08_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-08-1")
    add_fact("fact-08-1", "fcand-08-1", "SCN-08", "Platform Reliability team (6 engineers) rebuilt the monorepo CI pipeline, reducing median build duration from 22 minutes to 8 minutes.", "RESULT", "ENGINEERING_DELIVERY", "COMPLETED", "UNSPECIFIED", "TEAM")
    add_fact_support("fs-08-1", "SCN-08", "fact-08-1", "src08", "cu-08-0", "DIRECT", span_08_1)
    add_expected_fact("fcand-08-1", "SCN-08", "RESULT", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-08-1", True, "Preserves 6-engineer team attribution strictly at TEAM scope.")

    add_candidate("fcand-08-2", "SCN-08", "src08", "cu-08-1", span_08_2, "Alex Kim implemented the distributed build artifact cache layer within the CI pipeline rebuild effort.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-08-2", "SCN-08", "fcand-08-2", "src08", "cu-08-1", span_08_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-08-2")
    add_validation("fval-08-3", "SCN-08", "fcand-08-2", "src08", "cu-08-0", span_08_ctx, "CONTEXT", "PROMOTE_TO_CANONICAL_FACT", "fact-08-2")
    add_fact("fact-08-2", "fcand-08-2", "SCN-08", "Alex Kim implemented the distributed build artifact cache layer within the CI pipeline rebuild effort.", "ACTION", "ENGINEERING_DELIVERY", "COMPLETED", "UNSPECIFIED", "INDIVIDUAL")
    add_fact_support("fs-08-2", "SCN-08", "fact-08-2", "src08", "cu-08-1", "DIRECT", span_08_2)
    add_fact_support("fs-08-3", "SCN-08", "fact-08-2", "src08", "cu-08-0", "CONTEXT", span_08_ctx)
    add_expected_fact("fcand-08-2", "SCN-08", "ACTION", ["DIRECT", "CONTEXT"], "PROMOTE_TO_CANONICAL_FACT", "fact-08-2", True, "Individual cache layer contribution is DIRECT-supported with team CI rebuild as CONTEXT.")

    # Negative extractor candidate: mistakenly promotes 6-engineer team work as individual attribution -> REJECTED!
    add_candidate("fcand-08-3-rejected", "SCN-08", "src08", "cu-08-0", span_08_1, "Alex Kim single-handedly rebuilt the monorepo CI pipeline and reduced median build duration from 22m to 8m.", "ACTION", "COMPLETED", "UNVERIFIED_EXTRACTOR_OVERCLAIM")
    add_validation(
        "fval-08-3-rejected",
        "SCN-08",
        "fcand-08-3-rejected",
        "src08",
        "cu-08-0",
        span_08_1,
        "CONTRADICTS",
        "REJECT_DO_NOT_PROMOTE",
        None,
        rejection_reason="Source attributes rebuild to Platform Reliability team (6 engineers); FactSupportValidator forbids promoting team work to individual attribution.",
    )
    add_expected_fact("fcand-08-3-rejected", "SCN-08", "ACTION", ["CONTRADICTS"], "REJECT_DO_NOT_PROMOTE", None, False, "Team-to-individual elevation during extraction must be rejected by FactSupportValidator and never promoted.")

    # SCN-09: Representative STAGING vs PRODUCTION case
    span_09_1 = "로컬 스테이징 샌드박스(합성 데이터 1만 건)에서 복합 인덱스 적용 후 조회 p95가 840ms에서 95ms로 감소했다."
    span_09_2 = "운영 환경 배포는 아직 시작하지 않았다."
    add_candidate("fcand-09-1", "SCN-09", "src09", "cu-09-0", span_09_1, "로컬 스테이징 샌드박스(합성 데이터 1만 건)에서 복합 인덱스 적용 후 조회 p95가 840ms에서 95ms로 감소했다.", "METRIC", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-09-1", "SCN-09", "fcand-09-1", "src09", "cu-09-0", span_09_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-09-1")
    add_fact(
        "fact-09-1",
        "fcand-09-1",
        "SCN-09",
        "로컬 스테이징 샌드박스(합성 데이터 1만 건)에서 복합 인덱스 적용 후 조회 p95가 840ms에서 95ms로 감소했다.",
        "METRIC",
        "BENCHMARK_RESULT",
        "COMPLETED",
        "STAGING_TEST",
        "INDIVIDUAL",
        metric_details={
            "value": "840 -> 95",
            "unit": "ms (query p95)",
            "period": "UNSPECIFIED_IN_SOURCE (benchmark snapshot 2026-07-08)",
            "source_basis": "로컬 스테이징 샌드박스 벤치마크 메모",
            "environment_scope": "STAGING_TEST",
            "measurement_scope": "합성 데이터 1만 건 기준 복합 인덱스 조회 p95",
        },
    )
    add_fact_support("fs-09-1", "SCN-09", "fact-09-1", "src09", "cu-09-0", "DIRECT", span_09_1)
    add_expected_fact("fcand-09-1", "SCN-09", "METRIC", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-09-1", True, "Staging benchmark metric is DIRECT-supported strictly in STAGING_TEST scope.")

    add_candidate("fcand-09-2", "SCN-09", "src09", "cu-09-0", span_09_2, "복합 인덱스 변경사항의 운영 환경 배포는 아직 시작하지 않았다.", "ACTION", "NEGATED", "EXPLICIT_CERTAIN")
    add_validation("fval-09-2", "SCN-09", "fcand-09-2", "src09", "cu-09-0", span_09_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-09-2")
    add_fact("fact-09-2", "fcand-09-2", "SCN-09", "복합 인덱스 변경사항의 운영 환경 배포는 아직 시작하지 않았다.", "ACTION", "NEGATIVE_OUTCOME", "NEGATED", "PRODUCTION", "INDIVIDUAL")
    add_fact_support("fs-09-2", "SCN-09", "fact-09-2", "src09", "cu-09-0", "DIRECT", span_09_2)
    add_expected_fact("fcand-09-2", "SCN-09", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-09-2", True, "Negated production rollout sentence is preserved as a SUPPORTED negated fact contradicting production deployment claims.")

    # SCN-10: Unverified causation & fabricated metric
    span_10_1 = "I shipped the redesigned onboarding checklist UI on May 12."
    span_10_2 = "Total monthly revenue rose 18% in May compared to April, though the sales team also ran our largest annual discount campaign during the same two weeks; no A/B test or causal attribution study was conducted."
    add_candidate("fcand-10-1", "SCN-10", "src10", "cu-10-0", span_10_1, "Shipped the redesigned onboarding checklist UI on May 12.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-10-1", "SCN-10", "fcand-10-1", "src10", "cu-10-0", span_10_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-10-1")
    add_fact("fact-10-1", "fcand-10-1", "SCN-10", "Shipped the redesigned onboarding checklist UI on May 12.", "ACTION", "LAUNCH_EVENT", "COMPLETED", "PRODUCTION", "INDIVIDUAL")
    add_fact_support("fs-10-1", "SCN-10", "fact-10-1", "src10", "cu-10-0", "DIRECT", span_10_1)
    add_expected_fact("fcand-10-1", "SCN-10", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-10-1", True, "May 12 UI launch is DIRECT-supported.")

    add_candidate("fcand-10-2", "SCN-10", "src10", "cu-10-1", span_10_2, "Monthly revenue rose 18% in May during a simultaneous annual discount campaign with no A/B test or causal attribution study.", "METRIC", "OBSERVED_CORRELATION", "CONFOUNDED_NO_CAUSATION")
    add_validation("fval-10-2", "SCN-10", "fcand-10-2", "src10", "cu-10-1", span_10_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-10-2")
    add_fact(
        "fact-10-2",
        "fcand-10-2",
        "SCN-10",
        "Monthly revenue rose 18% in May during a simultaneous annual discount campaign with no A/B test or causal attribution study.",
        "METRIC",
        "PROJECT_CONTEXT",
        "OBSERVED_CORRELATION",
        "PRODUCTION",
        "TEAM",
        metric_details={
            "value": "+18",
            "unit": "percent (total monthly revenue)",
            "period": "May 2026 vs April 2026",
            "source_basis": "Monthly Product Update — May 2026 (confounded by annual discount campaign; no A/B test)",
            "environment_scope": "PRODUCTION",
            "measurement_scope": "Company-wide monthly revenue correlation without causal attribution",
        },
    )
    add_fact_support("fs-10-2", "SCN-10", "fact-10-2", "src10", "cu-10-1", "DIRECT", span_10_2)
    add_expected_fact("fcand-10-2", "SCN-10", "METRIC", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-10-2", True, "Confounded 18% revenue correlation is preserved with OBSERVED_CORRELATION modality (provides CONTEXT only for causal claims).")

    # SCN-11: Identity ambiguity vs resolved owner
    span_11_1 = "Alex fixed the memory leak in the ingestion worker overnight."
    span_11_2 = "I added graceful shutdown unit tests for the ingestion worker."
    add_candidate("fcand-11-1", "SCN-11", "src11a", "cu-11-0", span_11_1, "Unmapped contributor 'Alex' fixed the memory leak in the ingestion worker (ambiguous between Alex Kim and Alex Kang).", "ACTION", "COMPLETED", "AMBIGUOUS_ATTRIBUTION")
    add_validation("fval-11-1", "SCN-11", "fcand-11-1", "src11a", "cu-11-0", span_11_1, "DIRECT", "PROMOTE_AS_IDENTITY_UNRESOLVED_RECORD", "fact-11-1")
    add_fact(
        "fact-11-1",
        "fcand-11-1",
        "SCN-11",
        "Unmapped contributor 'Alex' fixed the memory leak in the ingestion worker (ambiguous between Alex Kim and Alex Kang).",
        "ACTION",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "AMBIGUOUS",
        identity_status="AMBIGUOUS",
        fact_support_status="IDENTITY_UNRESOLVED_REVIEW_REQUIRED",
        is_active_supported_fact=False,
    )
    add_fact_support("fs-11-1", "SCN-11", "fact-11-1", "src11a", "cu-11-0", "DIRECT", span_11_1)
    add_expected_fact("fcand-11-1", "SCN-11", "ACTION", ["DIRECT"], "PROMOTE_AS_IDENTITY_UNRESOLVED_RECORD", "fact-11-1", False, "Ambiguous homonym 'Alex' is NOT an active owner-resolved SUPPORTED fact.")

    add_candidate("fcand-11-2", "SCN-11", "src11b", "cu-11-1", span_11_2, "Alex Kim (verified account) added graceful shutdown unit tests for the ingestion worker.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-11-2", "SCN-11", "fcand-11-2", "src11b", "cu-11-1", span_11_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-11-2")
    add_fact("fact-11-2", "fcand-11-2", "SCN-11", "Alex Kim (verified account) added graceful shutdown unit tests for the ingestion worker.", "ACTION", "ENGINEERING_DELIVERY", "COMPLETED", "STAGING_TEST", "INDIVIDUAL", identity_status="RESOLVED")
    add_fact_support("fs-11-2", "SCN-11", "fact-11-2", "src11b", "cu-11-1", "DIRECT", span_11_2)
    add_expected_fact("fcand-11-2", "SCN-11", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-11-2", True, "Verified owner PR DIRECT-supports unit test addition.")

    # SCN-12: User edit & user confirmation (CLAIM-08)
    span_12_1 = "I tuned PostgreSQL connection pool sizing in the checkout service, reducing p99 checkout API latency by 25% under peak load."
    add_candidate("fcand-12-1", "SCN-12", "src12", "cu-12-0", span_12_1, "Tuned PostgreSQL connection pool sizing in the checkout service, reducing p99 checkout API latency by 25% under peak load.", "METRIC", "COMPLETED", "QUALIFIED_OR_CONDITIONAL")
    add_validation("fval-12-1", "SCN-12", "fcand-12-1", "src12", "cu-12-0", span_12_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-12-1")
    add_fact(
        "fact-12-1",
        "fcand-12-1",
        "SCN-12",
        "Tuned PostgreSQL connection pool sizing in the checkout service, reducing p99 checkout API latency by 25% under peak load.",
        "METRIC",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "UNSPECIFIED",
        "INDIVIDUAL",
        metric_details={
            "value": "-25",
            "unit": "percent (p99 checkout API latency)",
            "period": "peak load window (exact dates unspecified in PR #512)",
            "source_basis": "PR #512 — Connection Pool Tuning",
            "environment_scope": "UNSPECIFIED",
            "measurement_scope": "checkout service p99 API latency under peak load",
        },
    )
    add_fact_support("fs-12-1", "SCN-12", "fact-12-1", "src12", "cu-12-0", "DIRECT", span_12_1)
    add_expected_fact("fcand-12-1", "SCN-12", "METRIC", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-12-1", True, "Checkout service peak-load 25% p99 latency reduction is DIRECT-supported.")

    # SCN-13: User deletion invalidation (OD-003)
    span_13_1 = "I deprecated the legacy Memcached cluster and migrated session keys to Redis Cluster."
    span_13_2 = "I created a Redis Cluster saturation alert dashboard for on-call engineers."
    add_candidate("fcand-13-1", "SCN-13", "src13a", "cu-13-0", span_13_1, "Deprecated the legacy Memcached cluster and migrated session keys to Redis Cluster.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-13-1", "SCN-13", "fcand-13-1", "src13a", "cu-13-0", span_13_1, "DIRECT", "PROMOTE_AS_DELETED_RECORD", "fact-13-1")
    add_fact(
        "fact-13-1",
        "fcand-13-1",
        "SCN-13",
        "Deprecated the legacy Memcached cluster and migrated session keys to Redis Cluster.",
        "ACTION",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
        deleted_by_user=True,
        fact_support_status="DELETED_REVIEW_REQUIRED",
        is_active_supported_fact=False,
    )
    add_fact_support("fs-13-1", "SCN-13", "fact-13-1", "src13a", "cu-13-0", "DIRECT", span_13_1)
    add_expected_fact("fcand-13-1", "SCN-13", "ACTION", ["DIRECT"], "PROMOTE_AS_DELETED_RECORD", "fact-13-1", False, "User-deleted src13a marks fact-13-1 inactive/deleted under OD-003.")

    add_candidate("fcand-13-2", "SCN-13", "src13b", "cu-13-1", span_13_2, "Created a Redis Cluster saturation alert dashboard for on-call engineers.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-13-2", "SCN-13", "fcand-13-2", "src13b", "cu-13-1", span_13_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-13-2")
    add_fact("fact-13-2", "fcand-13-2", "SCN-13", "Created a Redis Cluster saturation alert dashboard for on-call engineers.", "ACTION", "ENGINEERING_DELIVERY", "COMPLETED", "PRODUCTION", "INDIVIDUAL", deleted_by_user=False)
    add_fact_support("fs-13-2", "SCN-13", "fact-13-2", "src13b", "cu-13-1", "DIRECT", span_13_2)
    add_expected_fact("fcand-13-2", "SCN-13", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-13-2", True, "Independent non-deleted source src13b keeps fact-13-2 preserved and active.")

    # SCN-14: Hard negatives (10x distortion + cross-project conflation)
    span_14_1 = "Project Alpha: I optimized image thumbnail decoding, reducing processing latency from 900ms to 400ms."
    span_14_2 = "Project Borealis: I migrated 12 internal documentation pages to the new Markdown linter."
    add_candidate("fcand-14-1", "SCN-14", "src14", "cu-14-0", span_14_1, "In Project Alpha, optimized image thumbnail decoding, reducing processing latency from 900ms to 400ms.", "METRIC", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-14-1", "SCN-14", "fcand-14-1", "src14", "cu-14-0", span_14_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-14-1")
    add_fact(
        "fact-14-1",
        "fcand-14-1",
        "SCN-14",
        "In Project Alpha, optimized image thumbnail decoding, reducing processing latency from 900ms to 400ms.",
        "METRIC",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
        metric_details={
            "value": "900 -> 400",
            "unit": "ms (thumbnail decoding latency)",
            "period": "H1 2026 engineering notes",
            "source_basis": "H1 Engineering Notes (Project Alpha)",
            "environment_scope": "PRODUCTION",
            "measurement_scope": "Project Alpha image thumbnail decoding",
        },
    )
    add_fact_support("fs-14-1", "SCN-14", "fact-14-1", "src14", "cu-14-0", "DIRECT", span_14_1)
    add_expected_fact("fcand-14-1", "SCN-14", "METRIC", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-14-1", True, "Project Alpha 900ms->400ms latency metric is DIRECT-supported.")

    add_candidate("fcand-14-2", "SCN-14", "src14", "cu-14-1", span_14_2, "In Project Borealis, migrated 12 internal documentation pages to the new Markdown linter.", "ACTION", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-14-2", "SCN-14", "fcand-14-2", "src14", "cu-14-1", span_14_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-14-2")
    add_fact("fact-14-2", "fcand-14-2", "SCN-14", "In Project Borealis, migrated 12 internal documentation pages to the new Markdown linter.", "ACTION", "ENGINEERING_DELIVERY", "COMPLETED", "PRODUCTION", "INDIVIDUAL")
    add_fact_support("fs-14-2", "SCN-14", "fact-14-2", "src14", "cu-14-1", "DIRECT", span_14_2)
    add_expected_fact("fcand-14-2", "SCN-14", "ACTION", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-14-2", True, "Project Borealis docs migration is DIRECT-supported.")

    # SCN-15 (NEW — CLAIM-05): Role attribution with direct personal contribution vs team-only audit with no Alex Kim role
    span_15_1 = "During the Q1 2026 payments ledger migration, Alex Kim served as Tech Lead for the ledger migration squad, authoring the dual-write cutover RFC and signing off on schema rollout reviews."
    span_15_1_contrib = "authoring the dual-write cutover RFC and signing off on schema rollout reviews"
    span_15_2 = "The broader Core Platform group also ran a quarterly security compliance audit led by the Security team, with no recorded role for Alex Kim in the compliance audit."
    add_candidate("fcand-15-1", "SCN-15", "src15", "cu-15-0", span_15_1, "Alex Kim served as Tech Lead for the Q1 2026 payments ledger migration squad, authoring the dual-write cutover RFC and signing off on schema rollout reviews.", "ROLE", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-15-1", "SCN-15", "fcand-15-1", "src15", "cu-15-0", span_15_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-15-1")
    add_validation("fval-15-1b", "SCN-15", "fcand-15-1", "src15", "cu-15-0", span_15_1_contrib, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-15-1")
    add_fact(
        "fact-15-1",
        "fcand-15-1",
        "SCN-15",
        "Alex Kim served as Tech Lead for the Q1 2026 payments ledger migration squad, authoring the dual-write cutover RFC and signing off on schema rollout reviews.",
        "ROLE",
        "ROLE_EVIDENCE",
        "COMPLETED",
        "UNSPECIFIED",
        "INDIVIDUAL",
        role_attribution_grounding={
            "representation_scope": "FIXTURE_SEMANTICS_FOR_DB_MODEL_3_8_NOT_PRODUCT_TABLE",
            "subject_person_ref": "person-alex-kim-verified",
            "confirmed_event_id": "evt-career-15",
            "role_label": "Tech Lead",
            "author_match_or_role_string_alone_insufficient": True,
            "active_contribution_fact_support_ids": ["fs-15-1b"],
            "contribution_contributor_person_ref": "person-alex-kim-verified",
            "contribution_summary": "Authoring the dual-write cutover RFC and signing off on schema rollout reviews",
        },
    )
    add_fact_support("fs-15-1", "SCN-15", "fact-15-1", "src15", "cu-15-0", "DIRECT", span_15_1)
    add_fact_support("fs-15-1b", "SCN-15", "fact-15-1", "src15", "cu-15-0", "DIRECT", span_15_1_contrib)
    add_expected_fact("fcand-15-1", "SCN-15", "ROLE", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-15-1", True, "CLAIM-05 / DB Model §3.8 fixture semantics: Binds person-alex-kim-verified + evt-career-15 + Tech Lead role to active concrete contribution span fs-15-1b (RFC authoring & schema rollout review sign-off); author match or role string alone is insufficient.")

    add_candidate("fcand-15-2", "SCN-15", "src15", "cu-15-1", span_15_2, "The Core Platform quarterly security compliance audit was led by the Security team with no recorded role for Alex Kim.", "ROLE", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-15-2", "SCN-15", "fcand-15-2", "src15", "cu-15-1", span_15_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-15-2")
    add_fact("fact-15-2", "fcand-15-2", "SCN-15", "The Core Platform quarterly security compliance audit was led by the Security team with no recorded role for Alex Kim.", "ROLE", "ROLE_EVIDENCE", "COMPLETED", "UNSPECIFIED", "TEAM")
    add_fact_support("fs-15-2", "SCN-15", "fact-15-2", "src15", "cu-15-1", "DIRECT", span_15_2)
    add_expected_fact("fcand-15-2", "SCN-15", "ROLE", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-15-2", True, "CLAIM-05: Direct record that Security team led the compliance audit with no recorded role for Alex Kim; contradicts personal Security Lead claims.")

    # SCN-16 (NEW — CLAIM-06): Structured PROBLEM + TECHNOLOGY + METRIC (value/unit/period/source/env/scope)
    span_16_1 = "Checkout API p99 response latency spiked to 920ms during April 2026 peak traffic due to unindexed order-lookup queries."
    span_16_2 = "I applied a PostgreSQL 16 covering composite index (orders_checkout_idx) to optimize checkout order-lookup queries."
    span_16_3 = "In production checkout-api (Datadog APM, 2026-04-01 to 2026-04-30, 30-day window), p99 checkout API latency decreased from 920ms to 210ms (77.2% reduction)."
    add_candidate("fcand-16-1", "SCN-16", "src16", "cu-16-0", span_16_1, "Checkout API p99 response latency spiked to 920ms during April 2026 peak traffic due to unindexed order-lookup queries.", "PROBLEM", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-16-1", "SCN-16", "fcand-16-1", "src16", "cu-16-0", span_16_1, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-16-1")
    add_fact("fact-16-1", "fcand-16-1", "SCN-16", "Checkout API p99 response latency spiked to 920ms during April 2026 peak traffic due to unindexed order-lookup queries.", "PROBLEM", "SYSTEM_PROBLEM", "COMPLETED", "PRODUCTION", "INDIVIDUAL")
    add_fact_support("fs-16-1", "SCN-16", "fact-16-1", "src16", "cu-16-0", "DIRECT", span_16_1)
    add_expected_fact("fcand-16-1", "SCN-16", "PROBLEM", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-16-1", True, "Production latency bottleneck problem directly evidenced in report.")

    add_candidate("fcand-16-2", "SCN-16", "src16", "cu-16-1", span_16_2, "Applied PostgreSQL 16 covering composite index (orders_checkout_idx) to optimize checkout order-lookup queries.", "TECHNOLOGY", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-16-2", "SCN-16", "fcand-16-2", "src16", "cu-16-1", span_16_2, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-16-2")
    add_fact("fact-16-2", "fcand-16-2", "SCN-16", "Applied PostgreSQL 16 covering composite index (orders_checkout_idx) to optimize checkout order-lookup queries.", "TECHNOLOGY", "TECHNOLOGY_ADOPTION", "COMPLETED", "PRODUCTION", "INDIVIDUAL")
    add_fact_support("fs-16-2", "SCN-16", "fact-16-2", "src16", "cu-16-1", "DIRECT", span_16_2)
    add_expected_fact("fcand-16-2", "SCN-16", "TECHNOLOGY", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-16-2", True, "PostgreSQL 16 covering composite index technology adoption directly evidenced.")

    add_candidate("fcand-16-3", "SCN-16", "src16", "cu-16-2", span_16_3, "In production checkout-api (Datadog APM, 2026-04-01 to 2026-04-30, 30-day window), p99 checkout API latency decreased from 920ms to 210ms (77.2% reduction).", "METRIC", "COMPLETED", "EXPLICIT_CERTAIN")
    add_validation("fval-16-3", "SCN-16", "fcand-16-3", "src16", "cu-16-2", span_16_3, "DIRECT", "PROMOTE_TO_CANONICAL_FACT", "fact-16-3")
    add_fact(
        "fact-16-3",
        "fcand-16-3",
        "SCN-16",
        "In production checkout-api (Datadog APM, 2026-04-01 to 2026-04-30, 30-day window), p99 checkout API latency decreased from 920ms to 210ms (77.2% reduction).",
        "METRIC",
        "BENCHMARK_RESULT",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
        metric_details={
            "value": "920 -> 210 (-77.2%)",
            "unit": "ms / percent (p99 latency)",
            "period": "2026-04-01 to 2026-04-30 (30-day window)",
            "source_basis": "Datadog APM",
            "environment_scope": "PRODUCTION",
            "measurement_scope": "checkout-api p99 response latency",
        },
    )
    add_fact_support("fs-16-3", "SCN-16", "fact-16-3", "src16", "cu-16-2", "DIRECT", span_16_3)
    add_expected_fact("fcand-16-3", "SCN-16", "METRIC", ["DIRECT"], "PROMOTE_TO_CANONICAL_FACT", "fact-16-3", True, "CLAIM-06: Complete metric record with value, unit, 30-day period, Datadog APM source, production env, and checkout-api p99 scope.")

    # Populate Stage 2B FactSupportValidator runs (distinct run_ids from Stage 2A FactCandidateExtractor!)
    for scn_idx in range(2, 17):
        sid = f"SCN-{scn_idx:02d}"
        vrun_id = validator_run_by_scn[sid]
        scn_units = units_by_scenario.get(sid, [])
        refs = [
            *cand_refs_by_scn.get(sid, []),
            *[
                {
                    "entity_type": "CONTENT_UNIT",
                    "entity_id": cu["content_unit_id"],
                    "version_or_hash": cu["unit_sha256"],
                }
                for cu in scn_units
            ],
        ]
        validator_runs.append(
            {
                "run_id": vrun_id,
                "scenario_id": sid,
                "pipeline_role": "FactSupportValidator",
                "stage_code": "STAGE_2B_FACT_SUPPORT_VALIDATION",
                "model_or_stub_version": "stub-fact-validator-v0.4",
                "prompt_version": "stub-prompt-fact-val-v0.4",
                "execution_mode": "SYNTHETIC_STUB_OFFLINE",
                "input_version_refs": refs,
                "persists_raw_text": False,
            }
        )

    scenario_candidate_counts: dict[str, int] = {f"SCN-{i:02d}": 0 for i in range(1, 17)}
    for fc in fact_candidates:
        scenario_candidate_counts[fc["scenario_id"]] += 1

    scenario_fact_counts: dict[str, int] = {f"SCN-{i:02d}": 0 for i in range(1, 17)}
    for fact in canonical_facts:
        scenario_fact_counts[fact["scenario_id"]] += 1

    facts_by_id_local = {f["canonical_fact_id"]: f for f in canonical_facts}

    # --- Stage 3: Confirmed Events, Distinct Role Execution Runs (3A/3B/3C), Claims, Atoms, Supports ---
    confirmed_events: list[dict[str, Any]] = [
        {"event_id": "evt-career-01", "scenario_id": "SCN-01", "event_status": "CONFIRMED", "title": "Weekly Standup Sync (Synthetic)", "source_revision_ids": [rev_id_by_key["src01"]]},
        {"event_id": "evt-career-02", "scenario_id": "SCN-02", "event_status": "CONFIRMED", "title": "Project Aurora Billing Webhooks Retry Queue", "source_revision_ids": [rev_id_by_key["src02"]]},
        {"event_id": "evt-career-03", "scenario_id": "SCN-03", "event_status": "CONFIRMED", "title": "Project Aurora May 2026 Search Reliability Improvements", "source_revision_ids": [rev_id_by_key["src03"]]},
        {"event_id": "evt-career-04", "scenario_id": "SCN-04", "event_status": "CONFIRMED", "title": "Nightly Billing Job Queue WorkerPool v2 Migration", "source_revision_ids": [rev_id_by_key["src04"]]},
        {"event_id": "evt-career-05", "scenario_id": "SCN-05", "event_status": "CONFIRMED", "title": "Incident #77 Database Primary Failover Postmortem", "source_revision_ids": [rev_id_by_key["src05a"], rev_id_by_key["src05b"]]},
        {"event_id": "evt-career-06", "scenario_id": "SCN-06", "event_status": "CONFIRMED", "title": "2026 Q4 Storage Scaling & Read-Replica Healthcheck", "source_revision_ids": [rev_id_by_key["src06"]]},
        {"event_id": "evt-career-07", "scenario_id": "SCN-07", "event_status": "CONFIRMED", "title": "Sprint 19 Canary Autoscaler Experiment", "source_revision_ids": [rev_id_by_key["src07"]]},
        {"event_id": "evt-career-08", "scenario_id": "SCN-08", "event_status": "CONFIRMED", "title": "Q2 Monorepo CI Pipeline Rebuild", "source_revision_ids": [rev_id_by_key["src08"]]},
        {"event_id": "evt-career-09", "scenario_id": "SCN-09", "event_status": "CONFIRMED", "title": "Composite Index Staging Query Benchmark", "source_revision_ids": [rev_id_by_key["src09"]]},
        {"event_id": "evt-career-10", "scenario_id": "SCN-10", "event_status": "CONFIRMED", "title": "May 2026 Onboarding Checklist UI Launch", "source_revision_ids": [rev_id_by_key["src10"]]},
        {"event_id": "evt-career-11", "scenario_id": "SCN-11", "event_status": "CONFIRMED", "title": "Ingestion Worker Maintenance & Shutdown Tests", "source_revision_ids": [rev_id_by_key["src11a"], rev_id_by_key["src11b"]]},
        {"event_id": "evt-career-12", "scenario_id": "SCN-12", "event_status": "CONFIRMED", "title": "Checkout Service PostgreSQL Connection Pool Tuning", "source_revision_ids": [rev_id_by_key["src12"]]},
        {"event_id": "evt-career-13", "scenario_id": "SCN-13", "event_status": "CONFIRMED", "title": "Cache Migration & Redis Health Alerting", "source_revision_ids": [rev_id_by_key["src13a"], rev_id_by_key["src13b"]]},
        {"event_id": "evt-career-14", "scenario_id": "SCN-14", "event_status": "CONFIRMED", "title": "H1 2026 Project Alpha & Project Borealis Tasks", "source_revision_ids": [rev_id_by_key["src14"]]},
        {"event_id": "evt-career-15", "scenario_id": "SCN-15", "event_status": "CONFIRMED", "title": "Q1 2026 Payments Ledger Migration & Core Platform Review", "source_revision_ids": [rev_id_by_key["src15"]]},
        {"event_id": "evt-career-16", "scenario_id": "SCN-16", "event_status": "CONFIRMED", "title": "April 2026 Production Checkout API Latency Optimization", "source_revision_ids": [rev_id_by_key["src16"]]},
    ]
    for evt in confirmed_events:
        evt["fixture_event_sha256"] = sha256_prefixed(
            f"{evt['event_id']}|{evt['title']}|" + ",".join(sorted(evt["source_revision_ids"]))
        )
    event_by_scn = {e["scenario_id"]: e["event_id"] for e in confirmed_events}
    events_by_id_local = {e["event_id"]: e for e in confirmed_events}

    role_execution_runs: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    claim_atoms: list[dict[str, Any]] = []
    claim_atom_supports: list[dict[str, Any]] = []

    claim_val_run_by_claim: dict[str, str | None] = {}
    claim_stale_v1_val_run_by_claim: dict[str, str] = {}
    claim_atomizer_run_by_claim: dict[str, str] = {}
    claim_version_by_claim: dict[str, int] = {}
    validator_candidate_facts_by_run: dict[str, list[str]] = {}

    def add_claim(
        claim_id: str,
        scenario_id: str,
        claim_text: str,
        atom_ids: list[str],
        *,
        input_fact_ids: list[str],
        generation_fixture_origin: str,
        claim_version: int = 1,
        previous_claim_version: int | None = None,
        user_edited: bool = False,
        reverification_state: str = "NOT_APPLICABLE_UNEDITED",
        user_edit_history: list[dict[str, Any]] | None = None,
        deletion_lifecycle: dict[str, Any] | None = None,
        pending_v2_reverification: bool = False,
        reverify_job_ctx: dict[str, bool] | None = None,
        validator_candidate_fact_ids: list[str] | None = None,
    ) -> None:
        evt_id = event_by_scn[scenario_id]
        gen_run_id = f"run-claim-gen-{claim_id}-v1"
        atom_run_id = f"run-atomize-{claim_id}-v{claim_version}"
        val_run_id: str | None = None if pending_v2_reverification else f"run-claim-validate-{claim_id}-v{claim_version}"

        claim_atomizer_run_by_claim[claim_id] = atom_run_id
        claim_val_run_by_claim[claim_id] = val_run_id
        claim_version_by_claim[claim_id] = claim_version

        # Stage 3A: ClaimGenerator run (input = ConfirmedEvent + input_fact_ids with real fixture hashes; initial status ALWAYS DRAFT)
        gen_refs: list[dict[str, str]] = [
            {
                "entity_type": "CONFIRMED_EVENT",
                "entity_id": evt_id,
                "version_or_hash": events_by_id_local[evt_id]["fixture_event_sha256"],
            }
        ]
        for fid in input_fact_ids:
            gen_refs.append(
                {
                    "entity_type": "CANONICAL_FACT",
                    "entity_id": fid,
                    "version_or_hash": facts_by_id_local[fid]["fixture_classification"]["fixture_fact_sha256"],
                }
            )
        role_execution_runs.append(
            {
                "run_id": gen_run_id,
                "scenario_id": scenario_id,
                "pipeline_role": "ClaimGenerator",
                "stage_code": "STAGE_3A_CLAIM_GENERATION",
                "model_or_stub_version": "stub-claim-generator-v0.4",
                "prompt_version": "stub-prompt-claim-gen-v0.4",
                "execution_mode": "SYNTHETIC_STUB_OFFLINE",
                "input_version_refs": gen_refs,
                "target_claim_id": claim_id,
                "target_claim_version": 1,
                "output_initial_status": "DRAFT",
                "uses_claim_draft_or_ai_explanation_as_evidence": None,
                "reverification_job_context": None,
                "persists_raw_text": False,
            }
        )

        # Stage 3B: ClaimAtomizer run (input = Claim version + content hash; distinct run_id)
        role_execution_runs.append(
            {
                "run_id": atom_run_id,
                "scenario_id": scenario_id,
                "pipeline_role": "ClaimAtomizer",
                "stage_code": "STAGE_3B_CLAIM_ATOMIZATION",
                "model_or_stub_version": "stub-claim-atomizer-v0.4",
                "prompt_version": "stub-prompt-atomizer-v0.4",
                "execution_mode": "SYNTHETIC_STUB_OFFLINE",
                "input_version_refs": [
                    {
                        "entity_type": "CLAIM_VERSION",
                        "entity_id": claim_id,
                        "version_or_hash": f"v{claim_version}:{short_hash(sha256_prefixed(claim_text), 12)}",
                    }
                ],
                "target_claim_id": claim_id,
                "target_claim_version": claim_version,
                "output_initial_status": None,
                "uses_claim_draft_or_ai_explanation_as_evidence": None,
                "reverification_job_context": None,
                "persists_raw_text": False,
            }
        )

        # Stage 3C: ClaimSupportValidator run (input_version_refs & verified_evidence_bindings finalized after atoms/supports registration)
        cand_facts = validator_candidate_fact_ids if validator_candidate_fact_ids is not None else input_fact_ids
        if pending_v2_reverification:
            stale_v1_run_id = f"run-claim-validate-{claim_id}-v1"
            claim_stale_v1_val_run_by_claim[claim_id] = stale_v1_run_id
            validator_candidate_facts_by_run[stale_v1_run_id] = list(cand_facts)
            role_execution_runs.append(
                {
                    "run_id": stale_v1_run_id,
                    "scenario_id": scenario_id,
                    "pipeline_role": "ClaimSupportValidator",
                    "stage_code": "STAGE_3C_CLAIM_SUPPORT_VALIDATION",
                    "model_or_stub_version": "stub-claim-validator-v0.4",
                    "prompt_version": "stub-prompt-claim-val-v0.4",
                    "execution_mode": "SYNTHETIC_STUB_OFFLINE",
                    "input_version_refs": [],
                    "target_claim_id": claim_id,
                    "target_claim_version": 1,
                    "output_initial_status": None,
                    "uses_claim_draft_or_ai_explanation_as_evidence": False,
                    "reverification_job_context": {
                        "refetch_checked_in_same_job": False,
                        "hash_and_locator_verified": False,
                        "permission_verified": False,
                        "semantics_verified": False,
                        "verified_evidence_bindings": [],
                    },
                    "persists_raw_text": False,
                }
            )
        else:
            assert val_run_id is not None
            validator_candidate_facts_by_run[val_run_id] = list(cand_facts)
            ctx_base = reverify_job_ctx or {
                "refetch_checked_in_same_job": True,
                "hash_and_locator_verified": True,
                "permission_verified": True,
                "semantics_verified": True,
            }
            ctx: dict[str, Any] = {
                **ctx_base,
                "verified_evidence_bindings": [],
            }
            role_execution_runs.append(
                {
                    "run_id": val_run_id,
                    "scenario_id": scenario_id,
                    "pipeline_role": "ClaimSupportValidator",
                    "stage_code": "STAGE_3C_CLAIM_SUPPORT_VALIDATION",
                    "model_or_stub_version": "stub-claim-validator-v0.4",
                    "prompt_version": "stub-prompt-claim-val-v0.4",
                    "execution_mode": "SYNTHETIC_STUB_OFFLINE",
                    "input_version_refs": [],
                    "target_claim_id": claim_id,
                    "target_claim_version": claim_version,
                    "output_initial_status": None,
                    "uses_claim_draft_or_ai_explanation_as_evidence": False,
                    "reverification_job_context": ctx,
                    "persists_raw_text": False,
                }
            )

        entry: dict[str, Any] = {
            "claim_id": claim_id,
            "scenario_id": scenario_id,
            "confirmed_event_id": evt_id,
            "input_fact_ids": input_fact_ids,
            "claim_version": claim_version,
            "previous_claim_version": previous_claim_version,
            "generator_initial_status": "DRAFT",
            "user_can_directly_set_source_verified": False,
            "generator_run_id": gen_run_id,
            "atomizer_run_id": atom_run_id,
            "validator_run_id": val_run_id,
            "generation_fixture_origin": generation_fixture_origin,
            "claim_text": claim_text,
            "claim_atom_ids": atom_ids,
            "user_edited": user_edited,
            "reverification_state_on_new_version": reverification_state,
            "user_edit_history": user_edit_history or [],
            "derived_from_claim_ids": [],
        }
        if deletion_lifecycle is not None:
            entry["deletion_lifecycle"] = deletion_lifecycle
        claims.append(entry)

    def add_atom(
        atom_id: str,
        scenario_id: str,
        claim_id: str,
        atom_text: str,
        test_atom_kind: str,
        *,
        is_required_atom: bool = True,
        user_edited: bool = False,
        user_confirmed_by_owner: bool = False,
        edit_severity: str | None = "NONE",
        is_hard_negative: bool = False,
        hard_negative_type: str | None = None,
    ) -> None:
        claim_atoms.append(
            {
                "claim_atom_id": atom_id,
                "scenario_id": scenario_id,
                "claim_id": claim_id,
                "claim_version": claim_version_by_claim[claim_id],
                "atomizer_run_id": claim_atomizer_run_by_claim[claim_id],
                "validator_run_id": claim_val_run_by_claim[claim_id],
                "atom_text": atom_text,
                "is_required_atom": is_required_atom,
                "user_edited": user_edited,
                "user_confirmed_by_owner": user_confirmed_by_owner,
                "edit_severity": edit_severity,
                "is_hard_negative": is_hard_negative,
                "hard_negative_type": hard_negative_type,
                "fixture_classification": {
                    "label_scope": "TEST_FIXTURE_ONLY_NOT_DOMAIN_API",
                    "test_atom_kind": test_atom_kind,
                },
            }
        )

    def add_cas(
        cas_id: str,
        scenario_id: str,
        atom_id: str,
        fact_id: str,
        support_role: str,
        *,
        claim_id: str,
        evaluated_on_claim_version: int = 1,
        removed_by_user: bool = False,
    ) -> None:
        vrun = claim_val_run_by_claim[claim_id]
        if vrun is None:
            vrun = claim_stale_v1_val_run_by_claim[claim_id]
        claim_atom_supports.append(
            {
                "claim_atom_support_id": cas_id,
                "scenario_id": scenario_id,
                "claim_atom_id": atom_id,
                "canonical_fact_id": fact_id,
                "validator_run_id": vrun,
                "evidence_source_kind": "CANONICAL_FACT",
                "support_role": support_role,
                "evaluated_on_claim_version": evaluated_on_claim_version,
                "removed_by_user": removed_by_user,
            }
        )

    # SCN-01
    add_claim(
        "claim-01-1",
        "SCN-01",
        "결제 API 응답 속도를 40% 개선했다.",
        ["atom-01-1"],
        input_fact_ids=[],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-01-1", "SCN-01", "claim-01-1", "결제 API 응답 속도를 40% 개선함", "OUTCOME_METRIC", is_hard_negative=True, hard_negative_type="ZERO_FACT_HALLUCINATION")

    # SCN-02
    add_claim(
        "claim-02-1",
        "SCN-02",
        "Implemented an idempotent retry queue with exponential backoff for Project Aurora billing webhooks, preventing duplicate invoice notifications during transient gateway timeouts.",
        ["atom-02-1", "atom-02-2"],
        input_fact_ids=["fact-02-1"],
        generation_fixture_origin="GENERATED_FROM_SUPPORTED_FACTS",
    )
    add_atom("atom-02-1", "SCN-02", "claim-02-1", "Implemented idempotent retry queue with exponential backoff for billing webhooks in Project Aurora", "ACTION")
    add_atom("atom-02-2", "SCN-02", "claim-02-1", "Prevented duplicate invoice notifications during transient gateway timeouts", "OUTCOME_METRIC")
    add_cas("cas-02-1", "SCN-02", "atom-02-1", "fact-02-1", "DIRECT", claim_id="claim-02-1")
    add_cas("cas-02-2", "SCN-02", "atom-02-2", "fact-02-1", "DIRECT", claim_id="claim-02-1")

    # SCN-03
    add_claim(
        "claim-03-1",
        "SCN-03",
        "프로젝트 오로라에서 검색 인덱스 샤드 재분배 스크립트를 작성해 운영 클러스터의 p95 검색 지연 시간을 620ms에서 180ms로 단축했고, 배포 실패 시 2분 이내 이전 스냅샷으로 복구하는 자동 롤백 스크립트와 슬로우 쿼리 300ms 초과 시 경보를 발송하는 알림 규칙을 구성했다.",
        ["atom-03-1", "atom-03-2", "atom-03-3", "atom-03-4"],
        input_fact_ids=["fact-03-1", "fact-03-2", "fact-03-3"],
        generation_fixture_origin="GENERATED_FROM_SUPPORTED_FACTS",
    )
    add_atom("atom-03-1", "SCN-03", "claim-03-1", "검색 인덱스 샤드 재분배 스크립트 작성", "ACTION")
    add_atom("atom-03-2", "SCN-03", "claim-03-1", "운영 클러스터의 p95 검색 지연 시간을 620ms에서 180ms로 단축", "OUTCOME_METRIC")
    add_atom("atom-03-3", "SCN-03", "claim-03-1", "배포 실패 시 2분 이내로 이전 스냅샷으로 복구하는 자동 롤백 스크립트 추가", "ACTION")
    add_atom("atom-03-4", "SCN-03", "claim-03-1", "슬로우 쿼리 임계값 300ms 초과 시 경보를 발송하는 알림 규칙 구성", "ACTION")
    add_cas("cas-03-1", "SCN-03", "atom-03-1", "fact-03-1", "DIRECT", claim_id="claim-03-1")
    add_cas("cas-03-2", "SCN-03", "atom-03-2", "fact-03-1", "DIRECT", claim_id="claim-03-1")
    add_cas("cas-03-3", "SCN-03", "atom-03-3", "fact-03-2", "DIRECT", claim_id="claim-03-1")
    add_cas("cas-03-4", "SCN-03", "atom-03-4", "fact-03-3", "DIRECT", claim_id="claim-03-1")

    # SCN-04
    add_claim(
        "claim-04-1",
        "SCN-04",
        "Migrated the nightly billing job queue from legacy cron scripts to WorkerPool v2 in production.",
        ["atom-04-1"],
        input_fact_ids=["fact-04-1", "fact-04-2"],
        generation_fixture_origin="GENERATED_FROM_SUPPORTED_FACTS",
    )
    add_atom("atom-04-1", "SCN-04", "claim-04-1", "Migrated nightly billing job queue from legacy cron scripts to WorkerPool v2 in production", "ACTION")
    add_cas("cas-04-1", "SCN-04", "atom-04-1", "fact-04-1", "DIRECT", claim_id="claim-04-1")
    add_cas("cas-04-2", "SCN-04", "atom-04-1", "fact-04-2", "CONTEXT", claim_id="claim-04-1")

    add_claim(
        "claim-04-2",
        "SCN-04",
        "Eliminated 100% of missed nightly batch runs across all billing pipelines.",
        ["atom-04-2"],
        input_fact_ids=["fact-04-2"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-04-2", "SCN-04", "claim-04-2", "Eliminated 100% of missed nightly batch runs across billing pipelines", "OUTCOME_METRIC", is_hard_negative=True, hard_negative_type="CONTEXT_ONLY_INSUFFICIENT")
    add_cas("cas-04-3", "SCN-04", "atom-04-2", "fact-04-2", "CONTEXT", claim_id="claim-04-2")

    # SCN-05
    add_claim(
        "claim-05-1",
        "SCN-05",
        "Executed database primary failover with zero dropped checkout requests.",
        ["atom-05-1", "atom-05-2"],
        input_fact_ids=["fact-05-1"],
        validator_candidate_fact_ids=["fact-05-1", "fact-05-2"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-05-1", "SCN-05", "claim-05-1", "User personally executed the database primary failover", "ATTRIBUTION", is_hard_negative=True, hard_negative_type="UNSUPPORTED_EXECUTION_ATTRIBUTION")
    add_atom("atom-05-2", "SCN-05", "claim-05-1", "Zero checkout requests were dropped during database primary failover", "OUTCOME_METRIC", is_hard_negative=True, hard_negative_type="CONTRADICTED_BY_CORRECTION")
    add_cas("cas-05-1", "SCN-05", "atom-05-2", "fact-05-1", "DIRECT", claim_id="claim-05-1")
    add_cas("cas-05-2", "SCN-05", "atom-05-2", "fact-05-2", "CONTRADICTS", claim_id="claim-05-1")

    add_claim(
        "claim-05-2",
        "SCN-05",
        "Audited failover logs and documented 412 HTTP 503 checkout responses during the 90-second failover window.",
        ["atom-05-3", "atom-05-4"],
        input_fact_ids=["fact-05-2"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-05-3", "SCN-05", "claim-05-2", "Log audit confirmed 412 checkout requests returned HTTP 503 during the 90-second failover window", "OUTCOME_METRIC")
    add_atom("atom-05-4", "SCN-05", "claim-05-2", "User personally audited failover logs and documented the 412 HTTP 503 findings", "ATTRIBUTION", is_hard_negative=True, hard_negative_type="UNSUPPORTED_INDIVIDUAL_AUDIT_ATTRIBUTION")
    add_cas("cas-05-3", "SCN-05", "atom-05-3", "fact-05-2", "DIRECT", claim_id="claim-05-2")
    add_cas("cas-05-4", "SCN-05", "atom-05-4", "fact-05-2", "CONTEXT", claim_id="claim-05-2")

    # SCN-06
    add_claim(
        "claim-06-1",
        "SCN-06",
        "읽기 전용 복제본 헬스체크 엔드포인트를 구현하여 운영 모니터링에 연동했다.",
        ["atom-06-1", "atom-06-2"],
        input_fact_ids=["fact-06-1"],
        generation_fixture_origin="GENERATED_FROM_SUPPORTED_FACTS",
    )
    add_atom("atom-06-1", "SCN-06", "claim-06-1", "읽기 전용 복제본 헬스체크 엔드포인트 구현 완료", "ACTION")
    add_atom("atom-06-2", "SCN-06", "claim-06-1", "읽기 전용 복제본 헬스체크 엔드포인트를 운영 모니터링에 연동 완료", "ACTION")
    add_cas("cas-06-1", "SCN-06", "atom-06-1", "fact-06-1", "DIRECT", claim_id="claim-06-1")
    add_cas("cas-06-2", "SCN-06", "atom-06-2", "fact-06-1", "DIRECT", claim_id="claim-06-1")

    add_claim(
        "claim-06-2",
        "SCN-06",
        "운영 환경의 user_events 테이블 샤딩을 완료하고 벤치마크를 마쳤다.",
        ["atom-06-3", "atom-06-4"],
        input_fact_ids=["fact-06-2"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-06-3", "SCN-06", "claim-06-2", "운영 환경의 user_events 테이블 샤딩 완료", "ACTION", is_hard_negative=True, hard_negative_type="PLANNED_AS_COMPLETED")
    add_atom("atom-06-4", "SCN-06", "claim-06-2", "user_events 테이블 샤딩 프로토타입 벤치마크 완료", "ACTION", is_hard_negative=True, hard_negative_type="PLANNED_AS_COMPLETED")
    add_cas("cas-06-3", "SCN-06", "atom-06-3", "fact-06-2", "CONTRADICTS", claim_id="claim-06-2")
    add_cas("cas-06-4", "SCN-06", "atom-06-4", "fact-06-2", "CONTRADICTS", claim_id="claim-06-2")

    # SCN-07
    add_claim(
        "claim-07-1",
        "SCN-07",
        "Reverted the canary autoscaler Terraform config before production rollout after observing persistent cold-start spikes.",
        ["atom-07-1"],
        input_fact_ids=["fact-07-1"],
        generation_fixture_origin="GENERATED_FROM_SUPPORTED_FACTS",
    )
    add_atom("atom-07-1", "SCN-07", "claim-07-1", "Reverted canary autoscaler Terraform config before production rollout due to cold-start spikes", "ACTION")
    add_cas("cas-07-1", "SCN-07", "atom-07-1", "fact-07-1", "DIRECT", claim_id="claim-07-1")

    add_claim(
        "claim-07-2",
        "SCN-07",
        "Deployed the canary autoscaler to production and achieved 30% compute cost savings.",
        ["atom-07-2", "atom-07-3"],
        input_fact_ids=["fact-07-2", "fact-07-3"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-07-2", "SCN-07", "claim-07-2", "Deployed the canary autoscaler to production", "ACTION", is_hard_negative=True, hard_negative_type="NEGATION_REVERSAL")
    add_atom("atom-07-3", "SCN-07", "claim-07-2", "Achieved 30% compute cost savings from the canary autoscaler", "OUTCOME_METRIC", is_hard_negative=True, hard_negative_type="NEGATION_REVERSAL")
    add_cas("cas-07-2", "SCN-07", "atom-07-2", "fact-07-2", "CONTRADICTS", claim_id="claim-07-2")
    add_cas("cas-07-3", "SCN-07", "atom-07-3", "fact-07-3", "CONTRADICTS", claim_id="claim-07-2")

    # SCN-08
    add_claim(
        "claim-08-1",
        "SCN-08",
        "Implemented the distributed build artifact cache layer during the Platform Reliability team's CI pipeline rebuild.",
        ["atom-08-1"],
        input_fact_ids=["fact-08-2", "fact-08-1"],
        generation_fixture_origin="GENERATED_FROM_SUPPORTED_FACTS",
    )
    add_atom("atom-08-1", "SCN-08", "claim-08-1", "Implemented the distributed build artifact cache layer within the CI pipeline rebuild", "ATTRIBUTION")
    add_cas("cas-08-1", "SCN-08", "atom-08-1", "fact-08-2", "DIRECT", claim_id="claim-08-1")
    add_cas("cas-08-2", "SCN-08", "atom-08-1", "fact-08-1", "CONTEXT", claim_id="claim-08-1")

    add_claim(
        "claim-08-2",
        "SCN-08",
        "Single-handedly rebuilt the entire monorepo CI pipeline from scratch, reducing median build time from 22m to 8m.",
        ["atom-08-2", "atom-08-3", "atom-08-4"],
        input_fact_ids=["fact-08-1"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-08-2", "SCN-08", "claim-08-2", "Single-handedly rebuilt the entire monorepo CI pipeline", "ATTRIBUTION", is_hard_negative=True, hard_negative_type="TEAM_OVERCLAIM_AS_INDIVIDUAL")
    add_atom("atom-08-3", "SCN-08", "claim-08-2", "Rebuilt the monorepo CI pipeline from scratch", "ACTION", is_hard_negative=True, hard_negative_type="UNSUPPORTED_FROM_SCRATCH_CLAIM")
    add_atom("atom-08-4", "SCN-08", "claim-08-2", "Reduced median monorepo build duration from 22m to 8m (team-level outcome attributed to solo rebuild)", "OUTCOME_METRIC", is_hard_negative=True, hard_negative_type="TEAM_METRIC_CONTEXT_ONLY")
    add_cas("cas-08-3", "SCN-08", "atom-08-2", "fact-08-1", "CONTRADICTS", claim_id="claim-08-2")
    add_cas("cas-08-4", "SCN-08", "atom-08-4", "fact-08-1", "CONTEXT", claim_id="claim-08-2")

    # SCN-09
    add_claim(
        "claim-09-1",
        "SCN-09",
        "로컬 스테이징 샌드박스(합성 데이터 1만 건)에서 복합 인덱스로 조회 p95를 840ms에서 95ms로 단축했다.",
        ["atom-09-1"],
        input_fact_ids=["fact-09-1"],
        generation_fixture_origin="GENERATED_FROM_SUPPORTED_FACTS",
    )
    add_atom("atom-09-1", "SCN-09", "claim-09-1", "로컬 스테이징 샌드박스(1만 건)에서 복합 인덱스 적용으로 조회 p95를 840ms에서 95ms로 단축", "SCOPE_ENVIRONMENT")
    add_cas("cas-09-1", "SCN-09", "atom-09-1", "fact-09-1", "DIRECT", claim_id="claim-09-1")

    add_claim(
        "claim-09-2",
        "SCN-09",
        "운영 데이터베이스에 복합 인덱스를 배포해 조회 p95를 840ms에서 95ms로 개선했다.",
        ["atom-09-2", "atom-09-3"],
        input_fact_ids=["fact-09-1", "fact-09-2"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-09-2", "SCN-09", "claim-09-2", "운영 데이터베이스에 복합 인덱스를 배포함", "SCOPE_ENVIRONMENT", is_hard_negative=True, hard_negative_type="UNSTARTED_PRODUCTION_ROLLOUT")
    add_atom("atom-09-3", "SCN-09", "claim-09-2", "운영 데이터베이스에서 조회 p95를 840ms에서 95ms로 개선함", "OUTCOME_METRIC", is_hard_negative=True, hard_negative_type="STAGING_METRIC_CONFLATED_WITH_PRODUCTION")
    add_cas("cas-09-2", "SCN-09", "atom-09-2", "fact-09-2", "CONTRADICTS", claim_id="claim-09-2")
    add_cas("cas-09-3", "SCN-09", "atom-09-3", "fact-09-1", "CONTEXT", claim_id="claim-09-2")

    # SCN-10
    add_claim(
        "claim-10-1",
        "SCN-10",
        "Shipped the redesigned onboarding checklist UI on May 12, directly driving 18% monthly revenue growth and a 45% conversion lift.",
        ["atom-10-1", "atom-10-2", "atom-10-3"],
        input_fact_ids=["fact-10-1", "fact-10-2"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-10-1", "SCN-10", "claim-10-1", "Shipped the redesigned onboarding checklist UI on May 12", "TIMING")
    add_atom("atom-10-2", "SCN-10", "claim-10-1", "Directly drove 18% monthly revenue growth via the onboarding checklist redesign", "OUTCOME_METRIC", is_hard_negative=True, hard_negative_type="UNVERIFIED_CAUSATION")
    add_atom("atom-10-3", "SCN-10", "claim-10-1", "Achieved a 45% onboarding funnel conversion lift", "OUTCOME_METRIC", is_hard_negative=True, hard_negative_type="FABRICATED_METRIC")
    add_cas("cas-10-1", "SCN-10", "atom-10-1", "fact-10-1", "DIRECT", claim_id="claim-10-1")
    add_cas("cas-10-2", "SCN-10", "atom-10-2", "fact-10-2", "CONTEXT", claim_id="claim-10-1")

    # SCN-11
    add_claim(
        "claim-11-1",
        "SCN-11",
        "Fixed the memory leak in the ingestion worker overnight.",
        ["atom-11-1"],
        input_fact_ids=["fact-11-1"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-11-1", "SCN-11", "claim-11-1", "Fixed the memory leak in the ingestion worker (attributed to ambiguous 'Alex')", "ATTRIBUTION", is_hard_negative=True, hard_negative_type="IDENTITY_AMBIGUITY")
    add_cas("cas-11-1", "SCN-11", "atom-11-1", "fact-11-1", "DIRECT", claim_id="claim-11-1")

    add_claim(
        "claim-11-2",
        "SCN-11",
        "Added graceful shutdown unit tests for the ingestion worker.",
        ["atom-11-2"],
        input_fact_ids=["fact-11-2"],
        generation_fixture_origin="GENERATED_FROM_SUPPORTED_FACTS",
    )
    add_atom("atom-11-2", "SCN-11", "claim-11-2", "Added graceful shutdown unit tests for the ingestion worker", "ACTION")
    add_cas("cas-11-2", "SCN-11", "atom-11-2", "fact-11-2", "DIRECT", claim_id="claim-11-2")

    # SCN-12 (Full CLAIM-08 4-case matrix in family-src12 / DEV!)
    # 1) claim-12-1 (KEPT): v2 substantive edit -> re-verified on v2 -> FAILED (UNSUPPORTED)
    add_claim(
        "claim-12-1",
        "SCN-12",
        "Tuned PostgreSQL connection pool sizing globally, cutting p99 API latency by 60%.",
        ["atom-12-1", "atom-12-3"],
        input_fact_ids=["fact-12-1"],
        generation_fixture_origin="USER_EDITED_NEW_VERSION",
        claim_version=2,
        previous_claim_version=1,
        user_edited=True,
        reverification_state="REVERIFIED_FAILED_ON_NEW_VERSION",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
        user_edit_history=[
            {
                "event_id": "evt-edit-12-1",
                "from_version": 1,
                "to_version": 2,
                "action": "EDIT_CLAIM_TEXT",
                "severity": "SUBSTANTIVE",
                "note": "User created v2 expanding 'checkout service / 25% under peak load' to 'globally / 60%'; v2 re-verification against fact-12-1 fails due to unsupported scope & metric (does not infer 60% is false in every scope)",
            }
        ],
    )
    add_atom("atom-12-1", "SCN-12", "claim-12-1", "Tuned PostgreSQL connection pool sizing globally across all services", "SCOPE_ENVIRONMENT", user_edited=True, edit_severity="SUBSTANTIVE", is_hard_negative=True, hard_negative_type="USER_EDIT_SCOPE_OVERGENERALIZATION")
    add_atom("atom-12-3", "SCN-12", "claim-12-1", "Cut global p99 API latency by 60%", "OUTCOME_METRIC", user_edited=True, edit_severity="SUBSTANTIVE", is_hard_negative=True, hard_negative_type="USER_EDIT_METRIC_SCOPE_MISMATCH")
    add_cas("cas-12-1", "SCN-12", "atom-12-1", "fact-12-1", "CONTEXT", claim_id="claim-12-1", evaluated_on_claim_version=2)
    add_cas("cas-12-3", "SCN-12", "atom-12-3", "fact-12-1", "CONTEXT", claim_id="claim-12-1", evaluated_on_claim_version=2)

    # 2) claim-12-2 (KEPT): v2 minor paraphrase -> NOT YET re-verified on v2 (STALE v1 support!)
    add_claim(
        "claim-12-2",
        "SCN-12",
        "Optimized Postgres pool configuration in the checkout service, lowering peak p99 API latency by 25%.",
        ["atom-12-2"],
        input_fact_ids=["fact-12-1"],
        generation_fixture_origin="USER_EDITED_NEW_VERSION",
        claim_version=2,
        previous_claim_version=1,
        user_edited=True,
        reverification_state="REVERIFY_REQUIRED_ON_NEW_VERSION",
        pending_v2_reverification=True,
        user_edit_history=[
            {
                "event_id": "evt-edit-12-2",
                "from_version": 1,
                "to_version": 2,
                "action": "EDIT_CLAIM_TEXT",
                "severity": "MINOR_PARAPHRASE",
                "note": "User paraphrased claim into v2; prior v1 support is STALE and cannot auto-carryover SOURCE_VERIFIED until v2 is re-verified against fact-12-1",
            }
        ],
    )
    add_atom("atom-12-2", "SCN-12", "claim-12-2", "Optimized Postgres pool configuration in checkout service, lowering peak p99 API latency by 25%", "ACTION", user_edited=True, edit_severity="MINOR_PARAPHRASE")
    add_cas("cas-12-2", "SCN-12", "atom-12-2", "fact-12-1", "DIRECT", claim_id="claim-12-2", evaluated_on_claim_version=1)

    # 3) claim-12-3 (NEW — CLAIM-08 PASS): v2 faithful paraphrase RE-VERIFIED on v2 by distinct ClaimSupportValidator -> SUPPORTED & SOURCE_VERIFIED computed!
    add_claim(
        "claim-12-3",
        "SCN-12",
        "Adjusted PostgreSQL connection pool sizing in the checkout service, reducing peak-load checkout API p99 latency by 25%.",
        ["atom-12-4"],
        input_fact_ids=["fact-12-1"],
        generation_fixture_origin="USER_EDITED_NEW_VERSION",
        claim_version=2,
        previous_claim_version=1,
        user_edited=True,
        reverification_state="REVERIFIED_PASSED_ON_NEW_VERSION",
        user_edit_history=[
            {
                "event_id": "evt-edit-12-3",
                "from_version": 1,
                "to_version": 2,
                "action": "EDIT_CLAIM_TEXT",
                "severity": "MINOR_PARAPHRASE",
                "note": "CLAIM-08 PASS: User paraphrased claim into v2 preserving exact scope & 25% metric; fresh v2 ClaimSupportValidator run + same-job refetch verified atom-12-4 DIRECT against fact-12-1.",
            }
        ],
    )
    add_atom("atom-12-4", "SCN-12", "claim-12-3", "Adjusted PostgreSQL connection pool sizing in checkout service, reducing peak-load checkout API p99 latency by 25%", "OUTCOME_METRIC", user_edited=True, edit_severity="MINOR_PARAPHRASE")
    add_cas("cas-12-4", "SCN-12", "atom-12-4", "fact-12-1", "DIRECT", claim_id="claim-12-3", evaluated_on_claim_version=2)

    # 4) claim-12-4 (NEW — CLAIM-08 FAIL: USER_CONFIRMED-only atom MUST NOT become SOURCE_VERIFIED!)
    add_claim(
        "claim-12-4",
        "SCN-12",
        "Tuned PostgreSQL connection pool sizing in the checkout service, reducing peak-load checkout API p99 latency by 25% and saving $4,000/month in RDS spend.",
        ["atom-12-5", "atom-12-6"],
        input_fact_ids=["fact-12-1"],
        generation_fixture_origin="USER_CONFIRMED_UNSUPPORTED_TEST_INPUT",
        claim_version=2,
        previous_claim_version=1,
        user_edited=True,
        reverification_state="REVERIFIED_FAILED_ON_NEW_VERSION",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
        user_edit_history=[
            {
                "event_id": "evt-edit-12-4a",
                "from_version": 1,
                "to_version": 2,
                "action": "EDIT_CLAIM_TEXT",
                "severity": "SUBSTANTIVE",
                "note": "CLAIM-08 FAIL: User added '$4,000/month in RDS spend' not present in PR #512.",
            },
            {
                "event_id": "evt-edit-12-4b",
                "from_version": 2,
                "to_version": 2,
                "action": "USER_CONFIRM_UNSUPPORTED_ATOM",
                "severity": "USER_CONFIRMATION_ONLY",
                "note": "Owner marked atom-12-6 as USER_CONFIRMED without source Fact support; USER_CONFIRMED MUST NEVER promote the Claim to SOURCE_VERIFIED.",
            },
        ],
    )
    add_atom("atom-12-5", "SCN-12", "claim-12-4", "Tuned PostgreSQL connection pool sizing in checkout service, reducing peak-load checkout API p99 latency by 25%", "OUTCOME_METRIC", user_edited=False, edit_severity="NONE")
    add_atom("atom-12-6", "SCN-12", "claim-12-4", "Saved $4,000/month in RDS spend from connection pool tuning", "OUTCOME_METRIC", user_edited=True, user_confirmed_by_owner=True, edit_severity="SUBSTANTIVE", is_hard_negative=True, hard_negative_type="USER_CONFIRMED_WITHOUT_SOURCE_SUPPORT")
    add_cas("cas-12-5", "SCN-12", "atom-12-5", "fact-12-1", "DIRECT", claim_id="claim-12-4", evaluated_on_claim_version=2)
    # atom-12-6 intentionally has 0 ClaimAtomSupports (USER_CONFIRMED only!)

    # SCN-13 (KEPT: OD-003 deletion lifecycle)
    add_claim(
        "claim-13-1",
        "SCN-13",
        "Deprecated the legacy Memcached cluster and migrated session keys to Redis Cluster.",
        ["atom-13-1"],
        input_fact_ids=["fact-13-1"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        claim_version=1,
        previous_claim_version=None,
        user_edited=False,
        reverification_state="NOT_APPLICABLE_UNEDITED",
        reverify_job_ctx={"refetch_checked_in_same_job": False, "hash_and_locator_verified": False, "permission_verified": False, "semantics_verified": False},
        user_edit_history=[
            {"event_id": "evt-del-13-1a", "from_version": 1, "to_version": 1, "action": "DELETE_SOURCE_PREVIEW_CONFIRMED", "severity": "CASCADE_DELETE", "note": "OD-003: Deletion preview shown for src13a listing dependent claim-13-1; user explicitly confirmed deletion."},
            {"event_id": "evt-del-13-1b", "from_version": 1, "to_version": 1, "action": "BLOCK_DEPENDENT_CLAIM_ACCESS", "severity": "CASCADE_DELETE", "note": "OD-003: Dependent claim-13-1 immediately blocked from read/export/approval access upon confirmation."},
            {"event_id": "evt-del-13-1c", "from_version": 1, "to_version": 1, "action": "PURGE_DEPENDENT_CLAIM_AND_SERVER_COPIES", "severity": "CASCADE_DELETE", "note": "OD-003: Cleanup completed; claim-13-1 and its server approved copy & v1 record purged (0 retained)."},
        ],
        deletion_lifecycle={
            "policy_ref": "OD-003",
            "trigger_source_revision_id": rev_id_by_key["src13a"],
            "deletion_preview_shown": True,
            "explicit_user_confirmation": True,
            "immediate_access_blocked_after_confirmation": True,
            "cleanup_completed": True,
            "pre_deletion_server_artifacts": {"claim_versions": [1], "has_server_edited_copy": False, "has_server_approved_copy": True},
            "post_cleanup_retained_claim_versions": [],
            "post_cleanup_retained_edited_copies": 0,
            "post_cleanup_retained_approved_copies": 0,
            "independent_source_revisions_preserved": [rev_id_by_key["src13b"]],
            "independent_facts_preserved": ["fact-13-2"],
            "distinguished_from_refetch_failure_ref03": True,
        },
    )
    add_atom("atom-13-1", "SCN-13", "claim-13-1", "Deprecated legacy Memcached cluster and migrated session keys to Redis Cluster", "ACTION")
    add_cas("cas-13-1", "SCN-13", "atom-13-1", "fact-13-1", "DIRECT", claim_id="claim-13-1", removed_by_user=True)

    add_claim(
        "claim-13-2",
        "SCN-13",
        "Migrated session keys from Memcached to Redis Cluster and built a Redis saturation alert dashboard.",
        ["atom-13-2", "atom-13-3"],
        input_fact_ids=["fact-13-1", "fact-13-2"],
        generation_fixture_origin="USER_EDITED_NEW_VERSION",
        claim_version=2,
        previous_claim_version=1,
        user_edited=True,
        reverification_state="REVERIFY_REQUIRED_ON_NEW_VERSION",
        reverify_job_ctx={"refetch_checked_in_same_job": False, "hash_and_locator_verified": False, "permission_verified": False, "semantics_verified": False},
        user_edit_history=[
            {"event_id": "evt-del-13-2a", "from_version": 1, "to_version": 2, "action": "EDIT_CLAIM_TEXT", "severity": "MINOR_PARAPHRASE", "note": "Pre-deletion server state: claim-13-2 had v1 approved copy and v2 server edited copy mixing src13a + src13b."},
            {"event_id": "evt-del-13-2b", "from_version": 2, "to_version": 2, "action": "DELETE_SOURCE_PREVIEW_CONFIRMED", "severity": "CASCADE_DELETE", "note": "OD-003: Deletion preview shown for src13a listing mixed-source dependent claim-13-2; user explicitly confirmed deletion."},
            {"event_id": "evt-del-13-2c", "from_version": 2, "to_version": 2, "action": "BLOCK_DEPENDENT_CLAIM_ACCESS", "severity": "CASCADE_DELETE", "note": "OD-003: Dependent claim-13-2 immediately blocked from access despite also citing surviving src13b."},
            {"event_id": "evt-del-13-2d", "from_version": 2, "to_version": 2, "action": "PURGE_DEPENDENT_CLAIM_AND_SERVER_COPIES", "severity": "CASCADE_DELETE", "note": "OD-003: Cleanup completed; claim-13-2 and all server edited copies, approved copies, and versions [1, 2] purged (NOT left as PARTIALLY_GROUNDED). Independent source src13b & fact-13-2 preserved."},
        ],
        deletion_lifecycle={
            "policy_ref": "OD-003",
            "trigger_source_revision_id": rev_id_by_key["src13a"],
            "deletion_preview_shown": True,
            "explicit_user_confirmation": True,
            "immediate_access_blocked_after_confirmation": True,
            "cleanup_completed": True,
            "pre_deletion_server_artifacts": {"claim_versions": [1, 2], "has_server_edited_copy": True, "has_server_approved_copy": True},
            "post_cleanup_retained_claim_versions": [],
            "post_cleanup_retained_edited_copies": 0,
            "post_cleanup_retained_approved_copies": 0,
            "independent_source_revisions_preserved": [rev_id_by_key["src13b"]],
            "independent_facts_preserved": ["fact-13-2"],
            "distinguished_from_refetch_failure_ref03": True,
        },
    )
    add_atom("atom-13-2", "SCN-13", "claim-13-2", "Migrated session keys from Memcached to Redis Cluster", "ACTION")
    add_atom("atom-13-3", "SCN-13", "claim-13-2", "Built a Redis Cluster saturation alert dashboard for on-call engineers", "ACTION")
    add_cas("cas-13-2", "SCN-13", "atom-13-2", "fact-13-1", "DIRECT", claim_id="claim-13-2", evaluated_on_claim_version=2, removed_by_user=True)
    add_cas("cas-13-3", "SCN-13", "atom-13-3", "fact-13-2", "DIRECT", claim_id="claim-13-2", evaluated_on_claim_version=2)

    # SCN-14
    add_claim(
        "claim-14-1",
        "SCN-14",
        "Migrated 12 Project Borealis docs to the Markdown linter, cut Project Alpha thumbnail decoding latency from 900ms to 40ms, and sped up Project Borealis image decoding to 400ms.",
        ["atom-14-1", "atom-14-2", "atom-14-3"],
        input_fact_ids=["fact-14-1", "fact-14-2"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-14-1", "SCN-14", "claim-14-1", "Migrated 12 internal documentation pages in Project Borealis to the new Markdown linter", "ACTION")
    add_atom("atom-14-2", "SCN-14", "claim-14-1", "Reduced Project Alpha image thumbnail decoding latency from 900ms to 40ms", "OUTCOME_METRIC", is_hard_negative=True, hard_negative_type="NUMERIC_MAGNITUDE_DISTORTION")
    add_atom("atom-14-3", "SCN-14", "claim-14-1", "Reduced Project Borealis image thumbnail decoding latency to 400ms", "SCOPE_ENVIRONMENT", is_hard_negative=True, hard_negative_type="CROSS_ENTITY_CONFLATION")
    add_cas("cas-14-1", "SCN-14", "atom-14-1", "fact-14-2", "DIRECT", claim_id="claim-14-1")
    add_cas("cas-14-2", "SCN-14", "atom-14-2", "fact-14-1", "CONTRADICTS", claim_id="claim-14-1")
    add_cas("cas-14-3", "SCN-14", "atom-14-3", "fact-14-1", "CONTEXT", claim_id="claim-14-1")
    add_cas("cas-14-4", "SCN-14", "atom-14-3", "fact-14-2", "CONTEXT", claim_id="claim-14-1")

    # SCN-15 (NEW — CLAIM-05 PASS/FAIL pair)
    add_claim(
        "claim-15-1",
        "SCN-15",
        "Served as Tech Lead for the Q1 2026 payments ledger migration squad, authoring the dual-write cutover RFC and signing off on schema rollout reviews.",
        ["atom-15-1", "atom-15-2"],
        input_fact_ids=["fact-15-1"],
        generation_fixture_origin="GENERATED_FROM_SUPPORTED_FACTS",
    )
    add_atom("atom-15-1", "SCN-15", "claim-15-1", "Served as Tech Lead for the Q1 2026 payments ledger migration squad", "ROLE")
    add_atom("atom-15-2", "SCN-15", "claim-15-1", "Authored the dual-write cutover RFC and signed off on schema rollout reviews", "ACTION")
    add_cas("cas-15-1", "SCN-15", "atom-15-1", "fact-15-1", "DIRECT", claim_id="claim-15-1")
    add_cas("cas-15-2", "SCN-15", "atom-15-2", "fact-15-1", "DIRECT", claim_id="claim-15-1")

    add_claim(
        "claim-15-2",
        "SCN-15",
        "Led the Core Platform quarterly security compliance audit as Security Lead.",
        ["atom-15-3"],
        input_fact_ids=["fact-15-2"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-15-3", "SCN-15", "claim-15-2", "Led the Core Platform quarterly security compliance audit as Security Lead", "ROLE", is_hard_negative=True, hard_negative_type="UNSUPPORTED_ROLE_WITHOUT_CONTRIBUTION_EVIDENCE")
    add_cas("cas-15-3", "SCN-15", "atom-15-3", "fact-15-2", "CONTRADICTS", claim_id="claim-15-2")

    # SCN-16 (NEW — CLAIM-06 PASS/FAIL pair)
    add_claim(
        "claim-16-1",
        "SCN-16",
        "Applied a PostgreSQL 16 covering composite index (orders_checkout_idx) in production checkout-api, reducing p99 latency from 920ms to 210ms (77.2%) from 2026-04-01 to 2026-04-30 per Datadog APM.",
        ["atom-16-1", "atom-16-2"],
        input_fact_ids=["fact-16-1", "fact-16-2", "fact-16-3"],
        generation_fixture_origin="GENERATED_FROM_SUPPORTED_FACTS",
    )
    add_atom("atom-16-1", "SCN-16", "claim-16-1", "Applied PostgreSQL 16 covering composite index (orders_checkout_idx) in production checkout-api", "TECHNOLOGY")
    add_atom("atom-16-2", "SCN-16", "claim-16-1", "Reduced production checkout-api p99 latency from 920ms to 210ms (77.2%) over 2026-04-01 to 2026-04-30 per Datadog APM", "OUTCOME_METRIC")
    add_cas("cas-16-1", "SCN-16", "atom-16-1", "fact-16-2", "DIRECT", claim_id="claim-16-1")
    add_cas("cas-16-2", "SCN-16", "atom-16-2", "fact-16-3", "DIRECT", claim_id="claim-16-1")
    add_cas("cas-16-3", "SCN-16", "atom-16-1", "fact-16-1", "CONTEXT", claim_id="claim-16-1")

    add_claim(
        "claim-16-2",
        "SCN-16",
        "Reduced company-wide API latency by 77.2% across all services for the full year 2026.",
        ["atom-16-3"],
        input_fact_ids=["fact-16-3"],
        generation_fixture_origin="ADVERSARIAL_NEGATIVE_TEST_INPUT",
        reverify_job_ctx={"refetch_checked_in_same_job": True, "hash_and_locator_verified": True, "permission_verified": True, "semantics_verified": False},
    )
    add_atom("atom-16-3", "SCN-16", "claim-16-2", "Reduced company-wide API latency by 77.2% across all services for full year 2026", "OUTCOME_METRIC", is_hard_negative=True, hard_negative_type="METRIC_PERIOD_AND_SCOPE_OVERGENERALIZATION")
    add_cas("cas-16-4", "SCN-16", "atom-16-3", "fact-16-3", "CONTEXT", claim_id="claim-16-2")

    # Post-registration finalizer: bind every ClaimSupportValidator run to real Atom/Fact/Revision/Unit hashes
    # and populate verified_evidence_bindings linking Atom -> Fact -> active DIRECT FactSupport -> Revision/Unit/Locator -> same-job refetch
    sources_by_rev_local = {s["source_revision_id"]: s for s in sources}
    units_by_id_local = {u["content_unit_id"]: u for u in content_units}
    fs_by_fact_local: dict[str, list[dict[str, Any]]] = {}
    for fs in fact_supports:
        fs_by_fact_local.setdefault(fs["canonical_fact_id"], []).append(fs)
    claims_by_id_local = {c["claim_id"]: c for c in claims}
    atoms_by_id_local = {a["claim_atom_id"]: a for a in claim_atoms}
    cas_by_vrun_local: dict[str, list[dict[str, Any]]] = {}
    for cas in claim_atom_supports:
        cas_by_vrun_local.setdefault(cas["validator_run_id"], []).append(cas)

    for run in role_execution_runs:
        if run["pipeline_role"] != "ClaimSupportValidator":
            continue
        cid = run["target_claim_id"]
        cver = run["target_claim_version"]
        cl = claims_by_id_local[cid]
        run_cas = cas_by_vrun_local.get(run["run_id"], [])

        cand_facts_ordered: list[str] = []
        for fid in validator_candidate_facts_by_run.get(run["run_id"], []):
            if fid not in cand_facts_ordered:
                cand_facts_ordered.append(fid)
        for cas in run_cas:
            if cas["canonical_fact_id"] not in cand_facts_ordered:
                cand_facts_ordered.append(cas["canonical_fact_id"])

        val_refs: list[dict[str, str]] = []
        for aid in cl["claim_atom_ids"]:
            atom = atoms_by_id_local[aid]
            if cver == atom["claim_version"]:
                atom_ver_hash = f"v{cver}:{short_hash(sha256_prefixed(atom['atom_text']), 12)}"
            else:
                atom_ver_hash = f"v{cver}:{short_hash(sha256_prefixed(f'prior-v{cver}|{aid}'), 12)}"
            val_refs.append(
                {
                    "entity_type": "CLAIM_ATOM",
                    "entity_id": aid,
                    "version_or_hash": atom_ver_hash,
                }
            )
        seen_revs: list[str] = []
        seen_units: list[str] = []
        for fid in cand_facts_ordered:
            fact = facts_by_id_local[fid]
            val_refs.append(
                {
                    "entity_type": "CANONICAL_FACT",
                    "entity_id": fid,
                    "version_or_hash": fact["fixture_classification"]["fixture_fact_sha256"],
                }
            )
            for fs in fs_by_fact_local.get(fid, []):
                if fs["source_revision_id"] not in seen_revs:
                    seen_revs.append(fs["source_revision_id"])
                if fs["content_unit_id"] not in seen_units:
                    seen_units.append(fs["content_unit_id"])
        for rev_id in seen_revs:
            val_refs.append(
                {
                    "entity_type": "SOURCE_REVISION",
                    "entity_id": rev_id,
                    "version_or_hash": sources_by_rev_local[rev_id]["content_sha256"],
                }
            )
        for cu_id in seen_units:
            val_refs.append(
                {
                    "entity_type": "CONTENT_UNIT",
                    "entity_id": cu_id,
                    "version_or_hash": units_by_id_local[cu_id]["unit_sha256"],
                }
            )
        run["input_version_refs"] = val_refs

        ctx = run["reverification_job_context"]
        all_ctx_ok = bool(
            ctx["refetch_checked_in_same_job"]
            and ctx["hash_and_locator_verified"]
            and ctx["permission_verified"]
            and ctx["semantics_verified"]
        )
        bindings: list[dict[str, Any]] = []
        for cas in run_cas:
            if cas["removed_by_user"] or cas["support_role"] != "DIRECT" or cas["evaluated_on_claim_version"] != cver:
                continue
            fid = cas["canonical_fact_id"]
            fact = facts_by_id_local[fid]
            for fs in fs_by_fact_local.get(fid, []):
                if fs["support_role"] != "DIRECT":
                    continue
                src = sources_by_rev_local[fs["source_revision_id"]]
                cu = units_by_id_local[fs["content_unit_id"]]
                binding_ok = bool(
                    all_ctx_ok
                    and fact["is_active_supported_fact"]
                    and not fact["deleted_by_user"]
                    and not src["deleted_by_user"]
                    and cver == cl["claim_version"]
                )
                bindings.append(
                    {
                        "claim_atom_id": cas["claim_atom_id"],
                        "claim_version": cver,
                        "canonical_fact_id": fid,
                        "fact_fixture_sha256": fact["fixture_classification"]["fixture_fact_sha256"],
                        "fact_support_id": fs["fact_support_id"],
                        "support_role": "DIRECT",
                        "source_revision_id": fs["source_revision_id"],
                        "source_content_sha256": src["content_sha256"],
                        "content_unit_id": fs["content_unit_id"],
                        "unit_sha256": cu["unit_sha256"],
                        "locator_id": fs["locator"]["locator_id"],
                        "span_sha256": fs["locator"]["span_sha256"],
                        "same_job_refetch_result": "VERIFIED_EXACT_MATCH" if binding_ok else "NOT_VERIFIED_OR_BLOCKED",
                    }
                )
        ctx["verified_evidence_bindings"] = bindings

    # --- Refetch Stub Snapshots & Stage 4 Checks (12 preserved + 15 added = 27 covering REF-01..REF-08) ---
    src02_norm = norm_by_key["src02"]
    src02_span = span_02_1
    src02_baseline_loc = _make_locator("ref-base-src02", src02_norm, src02_span)
    src02_url = "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks"

    wrong_slice = src02_norm[src02_baseline_loc["char_start"] - 15 : src02_baseline_loc["char_end"] - 15]
    src02_wrong_loc_base = _make_locator("ref-wrong-loc-src02", src02_norm, wrong_slice)
    src02_corrupted_locator = {
        **src02_wrong_loc_base,
        "span_sha256": src02_baseline_loc["span_sha256"],
        "locator_id": compute_locator_id(
            "ref-wrong-loc-src02",
            src02_wrong_loc_base["char_start"],
            src02_wrong_loc_base["char_end"],
            src02_baseline_loc["span_sha256"],
        ),
    }

    src02_drifted_body = normalize_text(
        "[EDIT NOTICE 2026-08-05] Added billing compliance header above PR description.\n\n"
        + src02_norm
    )
    src02_partial_body = src02_norm[:95]
    src02_v2_body = normalize_text(
        src02_norm
        + "\n\nFollow-up (2026-08-10): Added dead-letter queue metrics dashboard for webhook retries."
    )

    ref04_canary_norm = norm_by_key["src_ref07_canary"]
    ref04_canary_span = "I added automated weekly partition pruning for synthetic audit log tables."
    ref04_canary_baseline_loc = _make_locator("ref-base-ref07", ref04_canary_norm, ref04_canary_span)
    ref04_canary_url = "https://synthetic.example.test/sources/ref07-canary-non-persistence"

    unsafe_primary_url = "http://169.254.169.254/latest/meta-data/iam/"
    unsafe_extra_urls = [
        "http://127.0.0.1:8080/internal/sources",
        "file:///etc/passwd",
        "ftp://synthetic.example.test/raw-export",
        "https://user:pass@localhost/secret-doc",
        "http://10.0.0.12/admin",
    ]
    for u in [unsafe_primary_url, *unsafe_extra_urls]:
        ok, _ = check_url_safety(u)
        if ok:
            raise RuntimeError(f"Expected unsafe URL to be blocked: {u}")

    refetch_snapshots: list[dict[str, Any]] = [
        # REF-01..REF-05 (12 preserved + 1 new refcase-04b)
        {
            "fixture_case_id": "refcase-01a",
            "official_ref_id": "REF-01",
            "legacy_draft_id": "REF-01",
            "scenario_title": "Same revision, identical content hash and exact locator span reproduction",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-02a",
            "official_ref_id": "REF-02",
            "legacy_draft_id": "REF-02",
            "scenario_title": "Changed content hash and shifted locator offsets blocked (body drift)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_drifted_body),
            "returned_body_fixture_only": src02_drifted_body,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-02b",
            "official_ref_id": "REF-02",
            "legacy_draft_id": "REF-08",
            "scenario_title": "Appended new upstream revision changes content hash (blocked without auto-detect/auto-claim-refresh; Post-MVP boundary)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_v2_body),
            "returned_body_fixture_only": src02_v2_body,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-02c",
            "official_ref_id": "REF-02",
            "legacy_draft_id": None,
            "scenario_title": "Identical content hash but wrong/corrupted locator offsets blocked (distinguished from body change refcase-02a/02b)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-03a",
            "official_ref_id": "REF-03",
            "legacy_draft_id": "REF-03",
            "scenario_title": "Source unavailable (HTTP 404 Not Found — distinguished from transient failures, confirmed deletion, and historical falsity)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 404,
            "stub_error_code": "HTTP_404_NOT_FOUND",
            "is_partial_response": False,
            "returned_content_sha256": None,
            "returned_body_fixture_only": None,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-03b",
            "official_ref_id": "REF-03",
            "legacy_draft_id": "REF-04",
            "scenario_title": "Partial/truncated refetch response (HTTP 206 — distinguished from confirmed deletion and historical falsity)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 206,
            "stub_error_code": "PARTIAL_TRUNCATED_RESPONSE",
            "is_partial_response": True,
            "returned_content_sha256": sha256_prefixed(src02_partial_body),
            "returned_body_fixture_only": src02_partial_body,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-03c",
            "official_ref_id": "REF-03",
            "legacy_draft_id": None,
            "scenario_title": "Transient rate-limit failure (HTTP 429 Too Many Requests — retry cap reached, approval blocked, distinguished from 404/deletion/falsity)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 429,
            "stub_error_code": "HTTP_429_RATE_LIMITED",
            "is_partial_response": False,
            "returned_content_sha256": None,
            "returned_body_fixture_only": None,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-03d",
            "official_ref_id": "REF-03",
            "legacy_draft_id": None,
            "scenario_title": "Transient upstream server error (HTTP 503 5xx — retry cap reached, approval blocked, distinguished from 404/deletion/falsity)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 503,
            "stub_error_code": "HTTP_503_UPSTREAM_SERVER_ERROR",
            "is_partial_response": False,
            "returned_content_sha256": None,
            "returned_body_fixture_only": None,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-03e",
            "official_ref_id": "REF-03",
            "legacy_draft_id": None,
            "scenario_title": "Transient network/read timeout (no HTTP status — retry cap reached, approval blocked, distinguished from 404/deletion/falsity)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": None,
            "stub_error_code": "NETWORK_READ_TIMEOUT",
            "is_partial_response": False,
            "returned_content_sha256": None,
            "returned_body_fixture_only": None,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-04a",
            "official_ref_id": "REF-04",
            "legacy_draft_id": "REF-07",
            "scenario_title": "Option C raw-source non-persistence static canary audit (DB/app log snapshot + negative leak fixture)",
            "target_url": ref04_canary_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(ref04_canary_norm),
            "returned_body_fixture_only": ref04_canary_norm,
            "canary_tokens": [CANARY_TOKEN_REF04],
        },
        {
            "fixture_case_id": "refcase-04b",
            "official_ref_id": "REF-04",
            "legacy_draft_id": None,
            "scenario_title": "Option C multi-surface static canary audit across DB, app log, Job payload, cache entry, and Export artifact + negative leaky Job/Export fixture",
            "target_url": ref04_canary_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(ref04_canary_norm),
            "returned_body_fixture_only": ref04_canary_norm,
            "canary_tokens": [CANARY_TOKEN_REF04],
        },
        {
            "fixture_case_id": "refcase-05a",
            "official_ref_id": "REF-05",
            "legacy_draft_id": "REF-05",
            "scenario_title": "Access permission failure (HTTP 403 revoked OAuth scope / workspace policy denied)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 403,
            "stub_error_code": "POLICY_OAUTH_SCOPE_REVOKED",
            "is_partial_response": False,
            "returned_content_sha256": None,
            "returned_body_fixture_only": None,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-05b",
            "official_ref_id": "REF-05",
            "legacy_draft_id": "REF-06",
            "scenario_title": "Unsafe URL rejected prior to network fetch (SSRF metadata IP / loopback / private CIDR / file & ftp schemes / credentials)",
            "target_url": unsafe_primary_url,
            "additional_unsafe_urls": unsafe_extra_urls,
            "url_safety_precheck": "BLOCKED_UNSAFE_URL",
            "fetch_attempted": False,
            "http_status": None,
            "stub_error_code": "UNSAFE_URL_BLOCKED_PREFETCH",
            "is_partial_response": False,
            "returned_content_sha256": None,
            "returned_body_fixture_only": None,
            "canary_tokens": [],
        },
        # NEW REF-06: Metadata-only crash/replay (3 fixtures)
        {
            "fixture_case_id": "refcase-06a",
            "official_ref_id": "REF-06",
            "legacy_draft_id": None,
            "scenario_title": "REF-06 Crash BEFORE atomic metadata+checkpoint commit -> memory wiped -> replay refetches unchanged source & commits atomically with 0 duplicates",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-06b",
            "official_ref_id": "REF-06",
            "legacy_draft_id": None,
            "scenario_title": "REF-06 Crash AFTER atomic metadata+checkpoint commit -> memory wiped -> idempotent replay creates 0 duplicate records and reproduces from same-hash refetch",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-06c",
            "official_ref_id": "REF-06",
            "legacy_draft_id": None,
            "scenario_title": "REF-06 Crash + memory lost -> replay refetches source with CHANGED hash -> explicit block with NO fallback to persisted raw text",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": "REPLAY_SOURCE_HASH_CHANGED",
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_drifted_body),
            "returned_body_fixture_only": src02_drifted_body,
            "canary_tokens": [],
        },
        # NEW REF-07: Approval / Export / Evidence-View refetch (5 fixtures)
        {
            "fixture_case_id": "refcase-07a",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "scenario_title": "REF-07 Claim Approval performs fresh same-job refetch -> hash/locator/permission verified -> approval allowed with checked_at audit",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-07b",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "scenario_title": "REF-07 New Export/Download performs fresh same-job refetch -> checked_at recorded in verification audit AND raw source quotes excluded from Export payload",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-07c",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "scenario_title": "REF-07 Evidence View performs fresh same-job refetch -> ephemeral in-memory span display with checked_at recorded and zero raw-text persistence",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-07d",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "scenario_title": "REF-07 Prior verification record ONLY (no fresh refetch in current Approval/Export operation) -> explicitly blocked",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": False,
            "http_status": None,
            "stub_error_code": "PRIOR_VERIFICATION_RECORD_ONLY_STALE",
            "is_partial_response": False,
            "returned_content_sha256": None,
            "returned_body_fixture_only": None,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-07e",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "scenario_title": "REF-07 TOCTOU race: refetch succeeded, but permission/validity revoked before commit/save -> operation & Export blocked",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": "TOCTOU_PERMISSION_OR_VALIDITY_CHANGED_BEFORE_SAVE",
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-07f",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "scenario_title": "REF-07 Export contrast (unapproved Claim): same-job refetch passes (HTTP 200, hash/locator match), yet requested Claim version is unapproved -> Export blocked and no Export artifact emitted",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": "EXPORT_CLAIM_NOT_APPROVED",
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-07g",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "scenario_title": "REF-07 Export contrast (version mismatch): same-job refetch passes (HTTP 200, hash/locator match), yet requested Claim version (v2) differs from approved version (v1) -> Export blocked and no Export artifact emitted",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": "EXPORT_CLAIM_VERSION_MISMATCH",
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        # NEW REF-08: Limits & inter-stage / inter-job memory boundary (4 fixtures)
        {
            "fixture_case_id": "refcase-08a",
            "official_ref_id": "REF-08",
            "legacy_draft_id": None,
            "scenario_title": "REF-08 Within request/byte/token/retry/concurrency limits + same-job duplicate ContentUnit deduplication + in-memory same-job transport",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-08b",
            "official_ref_id": "REF-08",
            "legacy_draft_id": None,
            "scenario_title": "REF-08 Separate Job receives ID/hash metadata ONLY (zero raw text) and next Job executes fresh refetch cleanly",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-08c",
            "official_ref_id": "REF-08",
            "legacy_draft_id": None,
            "scenario_title": "REF-08 Byte & token limits exceeded -> explicit block/failure (NEVER silently truncate required evidence and succeed)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": "LIMIT_EXCEEDED_BYTE_AND_TOKEN",
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(src02_norm),
            "returned_body_fixture_only": src02_norm,
            "canary_tokens": [],
        },
        {
            "fixture_case_id": "refcase-08d",
            "official_ref_id": "REF-08",
            "legacy_draft_id": None,
            "scenario_title": "REF-08 Request, retry, and concurrency limits exceeded -> explicit block/failure (NEVER silently drop required evidence and succeed)",
            "target_url": src02_url,
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 429,
            "stub_error_code": "LIMIT_EXCEEDED_REQUEST_RETRY_CONCURRENCY",
            "is_partial_response": False,
            "returned_content_sha256": None,
            "returned_body_fixture_only": None,
            "canary_tokens": [],
        },
    ]

    refetch_checks: list[dict[str, Any]] = [
        # REF-01..05 (12 preserved + refcase-04b)
        {
            "fixture_case_id": "refcase-01a",
            "official_ref_id": "REF-01",
            "legacy_draft_id": "REF-01",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REFETCH_MATCHED",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "content_sha256": sha256_prefixed(src02_norm),
                "locator_id": src02_baseline_loc["locator_id"],
                "span_sha256": src02_baseline_loc["span_sha256"],
                "stub_status": "STUB_REFETCH_MATCHED",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
        },
        {
            "fixture_case_id": "refcase-02a",
            "official_ref_id": "REF-02",
            "legacy_draft_id": "REF-02",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_HASH_LOCATOR_MISMATCH_BLOCKED",
            "locator_recheck_matched": False,
            "hash_recheck_matched": False,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "observed_content_sha256": sha256_prefixed(src02_drifted_body),
                "locator_id": src02_baseline_loc["locator_id"],
                "stub_status": "STUB_HASH_LOCATOR_MISMATCH_BLOCKED",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
        },
        {
            "fixture_case_id": "refcase-02b",
            "official_ref_id": "REF-02",
            "legacy_draft_id": "REF-08",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_HASH_MISMATCH_NEW_REVISION_NO_AUTO_MUTATION",
            "locator_recheck_matched": True,
            "hash_recheck_matched": False,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "observed_content_sha256": sha256_prefixed(src02_v2_body),
                "locator_id": src02_baseline_loc["locator_id"],
                "claim_auto_mutated": False,
                "stub_status": "STUB_HASH_MISMATCH_NEW_REVISION_NO_AUTO_MUTATION",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
        },
        {
            "fixture_case_id": "refcase-02c",
            "official_ref_id": "REF-02",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "tested_locator": src02_corrupted_locator,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_SAME_HASH_LOCATOR_ONLY_MISMATCH_BLOCKED",
            "locator_recheck_matched": False,
            "hash_recheck_matched": True,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "observed_content_sha256": sha256_prefixed(src02_norm),
                "tested_locator_id": src02_corrupted_locator["locator_id"],
                "stub_status": "STUB_SAME_HASH_LOCATOR_ONLY_MISMATCH_BLOCKED",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
        },
        {
            "fixture_case_id": "refcase-03a",
            "official_ref_id": "REF-03",
            "legacy_draft_id": "REF-03",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_SOURCE_UNAVAILABLE_404",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "stub_status": "STUB_SOURCE_UNAVAILABLE_404",
                "http_status": 404,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-03b",
            "official_ref_id": "REF-03",
            "legacy_draft_id": "REF-04",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_PARTIAL_TRUNCATED_206",
            "locator_recheck_matched": None,
            "hash_recheck_matched": False,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "stub_status": "STUB_PARTIAL_TRUNCATED_206",
                "http_status": 206,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-03c",
            "official_ref_id": "REF-03",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_TRANSIENT_RATE_LIMIT_429_RETRY_EXHAUSTED",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "retry_policy_stub": {
                "transient_failure_class": "HTTP_429_RATE_LIMIT",
                "max_retry_attempts": 3,
                "attempts_exhausted_in_stub": 3,
                "approval_blocked_while_unavailable": True,
                "distinguished_from_404": True,
            },
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "stub_status": "STUB_TRANSIENT_RATE_LIMIT_429_RETRY_EXHAUSTED",
                "http_status": 429,
                "attempts_exhausted": 3,
                "approval_blocked": True,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-03d",
            "official_ref_id": "REF-03",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_TRANSIENT_SERVER_ERROR_5XX_RETRY_EXHAUSTED",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "retry_policy_stub": {
                "transient_failure_class": "HTTP_5XX_SERVER_ERROR",
                "max_retry_attempts": 3,
                "attempts_exhausted_in_stub": 3,
                "approval_blocked_while_unavailable": True,
                "distinguished_from_404": True,
            },
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "stub_status": "STUB_TRANSIENT_SERVER_ERROR_5XX_RETRY_EXHAUSTED",
                "http_status": 503,
                "attempts_exhausted": 3,
                "approval_blocked": True,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-03e",
            "official_ref_id": "REF-03",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_TRANSIENT_TIMEOUT_RETRY_EXHAUSTED",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "retry_policy_stub": {
                "transient_failure_class": "NETWORK_TIMEOUT",
                "max_retry_attempts": 3,
                "attempts_exhausted_in_stub": 3,
                "approval_blocked_while_unavailable": True,
                "distinguished_from_404": True,
            },
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "stub_status": "STUB_TRANSIENT_TIMEOUT_RETRY_EXHAUSTED",
                "error_code": "NETWORK_READ_TIMEOUT",
                "attempts_exhausted": 3,
                "approval_blocked": True,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-04a",
            "official_ref_id": "REF-04",
            "legacy_draft_id": "REF-07",
            "baseline_source_revision_id": rev_id_by_key["src_ref07_canary"],
            "baseline_locator": ref04_canary_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_CANARY_STATIC_SNAPSHOT_CLEAN",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "canary_audit_surfaces": ["DB_SNAPSHOT", "APP_LOG"],
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src_ref07_canary"],
                "content_sha256": sha256_prefixed(ref04_canary_norm),
                "locator_id": ref04_canary_baseline_loc["locator_id"],
                "span_sha256": ref04_canary_baseline_loc["span_sha256"],
                "char_start": ref04_canary_baseline_loc["char_start"],
                "char_end": ref04_canary_baseline_loc["char_end"],
                "derived_fact_summary": "Added automated weekly partition pruning for synthetic audit log tables.",
                "raw_text_persisted": False,
            },
            "negative_leaky_payload_fixture": {
                "purpose": "Deliberate negative fixture to verify canary non-persistence detector catches raw source leakage",
                "leaked_debug_log_entry": f"ERROR raw_body_dump={CANARY_TOKEN_REF04}",
            },
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-04b",
            "official_ref_id": "REF-04",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src_ref07_canary"],
            "baseline_locator": ref04_canary_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_CANARY_JOB_CACHE_EXPORT_SURFACES_CLEAN",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "canary_audit_surfaces": ["DB_SNAPSHOT", "APP_LOG", "JOB_PAYLOAD", "CACHE_ENTRY", "EXPORT_ARTIFACT"],
            "persisted_payload_snapshot": {
                "db_metadata_row": {
                    "source_revision_id": rev_id_by_key["src_ref07_canary"],
                    "content_sha256": sha256_prefixed(ref04_canary_norm),
                    "locator_id": ref04_canary_baseline_loc["locator_id"],
                },
                "app_log_entry": {"event": "refetch_verified", "source_revision_id": rev_id_by_key["src_ref07_canary"], "status": "MATCH"},
                "job_queue_payload": {"job_id": "job-ref04b-01", "source_revision_id": rev_id_by_key["src_ref07_canary"], "content_sha256": sha256_prefixed(ref04_canary_norm)},
                "cache_entry": {"cache_key": "verif:ref04b", "locator_id": ref04_canary_baseline_loc["locator_id"], "verified": True},
                "export_artifact_row": {"claim_summary": "Automated weekly partition pruning for audit log tables.", "raw_quote_included": False},
            },
            "negative_leaky_payload_fixture": {
                "purpose": "Deliberate negative fixture verifying detector catches canary leaked into Job queue payload or Export artifact",
                "leaked_job_payload": f"job_arg_raw={CANARY_TOKEN_REF04}",
                "leaked_export_quote": f"Quote: {CANARY_TOKEN_REF04}",
            },
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-05a",
            "official_ref_id": "REF-05",
            "legacy_draft_id": "REF-05",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_POLICY_PERMISSION_DENIED_403",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "stub_status": "STUB_POLICY_PERMISSION_DENIED_403",
                "error_code": "POLICY_OAUTH_SCOPE_REVOKED",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-05b",
            "official_ref_id": "REF-05",
            "legacy_draft_id": "REF-06",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_UNSAFE_URL_BLOCKED_PREFETCH",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "stub_status": "STUB_UNSAFE_URL_BLOCKED_PREFETCH",
                "fetch_attempted": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        # NEW REF-06: Crash/Replay (3 fixtures)
        {
            "fixture_case_id": "refcase-06a",
            "official_ref_id": "REF-06",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF06_PRE_COMMIT_CRASH_REPLAY_REPRODUCED",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "crash_replay_stub": {
                "crash_timing": "BEFORE_COMMIT",
                "atomic_metadata_checkpoint_commit": True,
                "checkpoint_persisted_before_crash": False,
                "in_memory_raw_text_wiped": True,
                "raw_text_fallback_path_exists": False,
                "idempotency_key": "idem-ref06a-src02-v1",
                "replay_refetch_executed": True,
                "duplicate_records_created": 0,
                "checkpoint_payload": {
                    "job_id": "job-ref06a-replay",
                    "source_revision_id": rev_id_by_key["src02"],
                    "content_sha256": sha256_prefixed(src02_norm),
                    "locator_id": src02_baseline_loc["locator_id"],
                    "span_sha256": src02_baseline_loc["span_sha256"],
                    "checkpoint_stage": "COMMITTED_AFTER_REPLAY",
                },
            },
            "persisted_payload_snapshot": {
                "idempotency_key": "idem-ref06a-src02-v1",
                "source_revision_id": rev_id_by_key["src02"],
                "content_sha256": sha256_prefixed(src02_norm),
                "locator_id": src02_baseline_loc["locator_id"],
                "duplicate_records_created": 0,
                "raw_text_persisted": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-06b",
            "official_ref_id": "REF-06",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF06_POST_COMMIT_CRASH_IDEMPOTENT_REPLAY",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "crash_replay_stub": {
                "crash_timing": "AFTER_COMMIT",
                "atomic_metadata_checkpoint_commit": True,
                "checkpoint_persisted_before_crash": True,
                "in_memory_raw_text_wiped": True,
                "raw_text_fallback_path_exists": False,
                "idempotency_key": "idem-ref06b-src02-v1",
                "replay_refetch_executed": True,
                "duplicate_records_created": 0,
                "checkpoint_payload": {
                    "job_id": "job-ref06b-replay",
                    "source_revision_id": rev_id_by_key["src02"],
                    "content_sha256": sha256_prefixed(src02_norm),
                    "locator_id": src02_baseline_loc["locator_id"],
                    "span_sha256": src02_baseline_loc["span_sha256"],
                    "checkpoint_stage": "COMMITTED_BEFORE_CRASH_REPLAYED_IDEMPOTENTLY",
                },
            },
            "persisted_payload_snapshot": {
                "idempotency_key": "idem-ref06b-src02-v1",
                "source_revision_id": rev_id_by_key["src02"],
                "content_sha256": sha256_prefixed(src02_norm),
                "locator_id": src02_baseline_loc["locator_id"],
                "duplicate_records_created": 0,
                "raw_text_persisted": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-06c",
            "official_ref_id": "REF-06",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF06_MEMORY_LOST_HASH_CHANGED_BLOCKED",
            "locator_recheck_matched": False,
            "hash_recheck_matched": False,
            "crash_replay_stub": {
                "crash_timing": "AFTER_COMMIT",
                "atomic_metadata_checkpoint_commit": True,
                "checkpoint_persisted_before_crash": True,
                "in_memory_raw_text_wiped": True,
                "raw_text_fallback_path_exists": False,
                "idempotency_key": "idem-ref06c-src02-v1",
                "replay_refetch_executed": True,
                "duplicate_records_created": 0,
                "checkpoint_payload": {
                    "job_id": "job-ref06c-replay",
                    "source_revision_id": rev_id_by_key["src02"],
                    "expected_content_sha256": sha256_prefixed(src02_norm),
                    "observed_content_sha256": sha256_prefixed(src02_drifted_body),
                    "locator_id": src02_baseline_loc["locator_id"],
                    "checkpoint_stage": "BLOCKED_ON_HASH_MISMATCH_NO_RAW_FALLBACK",
                },
            },
            "persisted_payload_snapshot": {
                "idempotency_key": "idem-ref06c-src02-v1",
                "source_revision_id": rev_id_by_key["src02"],
                "observed_content_sha256": sha256_prefixed(src02_drifted_body),
                "approval_blocked": True,
                "raw_text_fallback_used": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        # NEW REF-07: Approval / Export / Evidence View refetch (5 fixtures)
        {
            "fixture_case_id": "refcase-07a",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF07_APPROVAL_SAME_JOB_REFETCH_PASSED",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "operation_refetch_stub": {
                "operation_type": "APPROVAL",
                "same_job_refetch_executed": True,
                "prior_verification_record_only": False,
                "permission_or_validity_changed_before_save": False,
                "checked_at_recorded_in_audit": True,
                "verification_audit_record": {
                    "job_id": "job-ref07a-approve",
                    "claim_id": "claim-02-1",
                    "source_revision_id": rev_id_by_key["src02"],
                    "locator_id": src02_baseline_loc["locator_id"],
                    "checked_at": "2026-08-12T10:00:00Z",
                    "result": "VERIFIED_IN_SAME_JOB",
                },
                "export_payload_snapshot": None,
                "export_excludes_raw_source_quotes": None,
                "ephemeral_memory_only_display": None,
            },
            "persisted_payload_snapshot": {
                "operation_type": "APPROVAL",
                "claim_id": "claim-02-1",
                "checked_at": "2026-08-12T10:00:00Z",
                "source_revision_id": rev_id_by_key["src02"],
                "raw_text_persisted": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-07b",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF07_EXPORT_SAME_JOB_REFETCH_PASSED_NO_RAW_QUOTE",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "operation_refetch_stub": {
                "operation_type": "EXPORT_DOWNLOAD",
                "same_job_refetch_executed": True,
                "prior_verification_record_only": False,
                "permission_or_validity_changed_before_save": False,
                "checked_at_recorded_in_audit": True,
                "verification_audit_record": {
                    "job_id": "job-ref07b-export",
                    "claim_id": "claim-02-1",
                    "approved_claim_version": 1,
                    "source_revision_id": rev_id_by_key["src02"],
                    "source_content_sha256": sha256_prefixed(src02_norm),
                    "locator_id": src02_baseline_loc["locator_id"],
                    "span_sha256": src02_baseline_loc["span_sha256"],
                    "checked_at": "2026-08-12T10:15:00Z",
                    "result": "VERIFIED_IN_SAME_JOB",
                },
                "export_precondition_input": {
                    "requested_claim_id": "claim-02-1",
                    "requested_claim_version": 1,
                    "claim_version_approved": True,
                    "approved_claim_version": 1,
                    "requested_version_matches_approved_version": True,
                    "export_artifact_emitted": True,
                    "metadata_layout_convention_scope": "TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN",
                },
                "export_payload_snapshot": {
                    "export_id": "exp-ref07b-01",
                    "approved_claim_id": "claim-02-1",
                    "approved_claim_version": 1,
                    "claim_text": "Implemented an idempotent retry queue with exponential backoff for Project Aurora billing webhooks, preventing duplicate invoice notifications during transient gateway timeouts.",
                    "verification_metadata_fixture_only": {
                        "layout_scope": "TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN",
                        "source_revision_id": rev_id_by_key["src02"],
                        "source_content_sha256": sha256_prefixed(src02_norm),
                        "locator_id": src02_baseline_loc["locator_id"],
                        "span_sha256": src02_baseline_loc["span_sha256"],
                        "checked_at": "2026-08-12T10:15:00Z",
                    },
                    "raw_source_quote_included": False,
                },
                "export_excludes_raw_source_quotes": True,
                "ephemeral_memory_only_display": None,
            },
            "persisted_payload_snapshot": {
                "operation_type": "EXPORT_DOWNLOAD",
                "export_id": "exp-ref07b-01",
                "claim_id": "claim-02-1",
                "approved_claim_version": 1,
                "checked_at": "2026-08-12T10:15:00Z",
                "export_artifact_emitted": True,
                "raw_source_quote_included": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-07c",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF07_EVIDENCE_VIEW_SAME_JOB_REFETCH_PASSED",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "operation_refetch_stub": {
                "operation_type": "EVIDENCE_VIEW",
                "same_job_refetch_executed": True,
                "prior_verification_record_only": False,
                "permission_or_validity_changed_before_save": False,
                "checked_at_recorded_in_audit": True,
                "verification_audit_record": {
                    "job_id": "job-ref07c-view",
                    "claim_id": "claim-02-1",
                    "source_revision_id": rev_id_by_key["src02"],
                    "locator_id": src02_baseline_loc["locator_id"],
                    "checked_at": "2026-08-12T10:30:00Z",
                    "result": "EPHEMERAL_SPAN_RENDERED_FROM_SAME_JOB_REFETCH",
                },
                "export_payload_snapshot": None,
                "export_excludes_raw_source_quotes": None,
                "ephemeral_memory_only_display": True,
            },
            "persisted_payload_snapshot": {
                "operation_type": "EVIDENCE_VIEW",
                "claim_id": "claim-02-1",
                "checked_at": "2026-08-12T10:30:00Z",
                "ephemeral_memory_only_display": True,
                "raw_text_persisted": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-07d",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF07_PRIOR_CHECK_ONLY_BLOCKED",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "operation_refetch_stub": {
                "operation_type": "APPROVAL",
                "same_job_refetch_executed": False,
                "prior_verification_record_only": True,
                "permission_or_validity_changed_before_save": False,
                "checked_at_recorded_in_audit": False,
                "verification_audit_record": {
                    "job_id": "job-ref07d-approve-blocked",
                    "claim_id": "claim-02-1",
                    "prior_checked_at": "2026-08-10T09:00:00Z",
                    "result": "BLOCKED_PRIOR_RECORD_ONLY_NO_SAME_JOB_REFETCH",
                },
                "export_payload_snapshot": None,
                "export_excludes_raw_source_quotes": None,
                "ephemeral_memory_only_display": None,
            },
            "persisted_payload_snapshot": {
                "operation_type": "APPROVAL",
                "claim_id": "claim-02-1",
                "approval_blocked": True,
                "reason": "PRIOR_VERIFICATION_RECORD_ONLY_STALE",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-07e",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF07_TOCTOU_PERMISSION_OR_VALIDITY_CHANGED_BLOCKED",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "operation_refetch_stub": {
                "operation_type": "EXPORT_DOWNLOAD",
                "same_job_refetch_executed": True,
                "prior_verification_record_only": False,
                "permission_or_validity_changed_before_save": True,
                "checked_at_recorded_in_audit": True,
                "verification_audit_record": {
                    "job_id": "job-ref07e-export-toctou-blocked",
                    "claim_id": "claim-02-1",
                    "approved_claim_version": 1,
                    "checked_at": "2026-08-12T10:45:00Z",
                    "pre_save_recheck_status": "REVOKED_OR_INVALIDATED_BEFORE_COMMIT",
                    "result": "BLOCKED_TOCTOU_STATE_CHANGED_BEFORE_SAVE",
                },
                "export_precondition_input": {
                    "requested_claim_id": "claim-02-1",
                    "requested_claim_version": 1,
                    "claim_version_approved": True,
                    "approved_claim_version": 1,
                    "requested_version_matches_approved_version": True,
                    "export_artifact_emitted": False,
                    "metadata_layout_convention_scope": "TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN",
                },
                "export_payload_snapshot": None,
                "export_excludes_raw_source_quotes": True,
                "ephemeral_memory_only_display": None,
            },
            "persisted_payload_snapshot": {
                "operation_type": "EXPORT_DOWNLOAD",
                "claim_id": "claim-02-1",
                "approval_blocked": True,
                "export_artifact_emitted": False,
                "reason": "TOCTOU_PERMISSION_OR_VALIDITY_CHANGED_BEFORE_SAVE",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-07f",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF07_EXPORT_UNAPPROVED_CLAIM_BLOCKED",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "operation_refetch_stub": {
                "operation_type": "EXPORT_DOWNLOAD",
                "same_job_refetch_executed": True,
                "prior_verification_record_only": False,
                "permission_or_validity_changed_before_save": False,
                "checked_at_recorded_in_audit": True,
                "verification_audit_record": {
                    "job_id": "job-ref07f-export-unapproved-blocked",
                    "claim_id": "claim-02-1",
                    "requested_claim_version": 1,
                    "approved_claim_version": None,
                    "source_revision_id": rev_id_by_key["src02"],
                    "source_content_sha256": sha256_prefixed(src02_norm),
                    "locator_id": src02_baseline_loc["locator_id"],
                    "span_sha256": src02_baseline_loc["span_sha256"],
                    "checked_at": "2026-08-12T10:50:00Z",
                    "result": "REFETCH_MATCHED_BUT_BLOCKED_UNAPPROVED_CLAIM_VERSION",
                },
                "export_precondition_input": {
                    "requested_claim_id": "claim-02-1",
                    "requested_claim_version": 1,
                    "claim_version_approved": False,
                    "approved_claim_version": None,
                    "requested_version_matches_approved_version": False,
                    "export_artifact_emitted": False,
                    "metadata_layout_convention_scope": "TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN",
                },
                "export_payload_snapshot": None,
                "export_excludes_raw_source_quotes": True,
                "ephemeral_memory_only_display": None,
            },
            "persisted_payload_snapshot": {
                "operation_type": "EXPORT_DOWNLOAD",
                "claim_id": "claim-02-1",
                "requested_claim_version": 1,
                "approval_blocked": True,
                "export_artifact_emitted": False,
                "reason": "REQUESTED_CLAIM_VERSION_NOT_APPROVED",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-07g",
            "official_ref_id": "REF-07",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF07_EXPORT_VERSION_MISMATCH_BLOCKED",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "operation_refetch_stub": {
                "operation_type": "EXPORT_DOWNLOAD",
                "same_job_refetch_executed": True,
                "prior_verification_record_only": False,
                "permission_or_validity_changed_before_save": False,
                "checked_at_recorded_in_audit": True,
                "verification_audit_record": {
                    "job_id": "job-ref07g-export-version-mismatch-blocked",
                    "claim_id": "claim-02-1",
                    "requested_claim_version": 2,
                    "approved_claim_version": 1,
                    "source_revision_id": rev_id_by_key["src02"],
                    "source_content_sha256": sha256_prefixed(src02_norm),
                    "locator_id": src02_baseline_loc["locator_id"],
                    "span_sha256": src02_baseline_loc["span_sha256"],
                    "checked_at": "2026-08-12T10:55:00Z",
                    "result": "REFETCH_MATCHED_BUT_BLOCKED_APPROVED_VERSION_MISMATCH",
                },
                "export_precondition_input": {
                    "requested_claim_id": "claim-02-1",
                    "requested_claim_version": 2,
                    "claim_version_approved": False,
                    "approved_claim_version": 1,
                    "requested_version_matches_approved_version": False,
                    "export_artifact_emitted": False,
                    "metadata_layout_convention_scope": "TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN",
                },
                "export_payload_snapshot": None,
                "export_excludes_raw_source_quotes": True,
                "ephemeral_memory_only_display": None,
            },
            "persisted_payload_snapshot": {
                "operation_type": "EXPORT_DOWNLOAD",
                "claim_id": "claim-02-1",
                "requested_claim_version": 2,
                "approved_claim_version": 1,
                "approval_blocked": True,
                "export_artifact_emitted": False,
                "reason": "REQUESTED_CLAIM_VERSION_DIFFERS_FROM_APPROVED_VERSION",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        # NEW REF-08: Limits, same-job dedup, inter-job metadata-only handoff (4 fixtures)
        {
            "fixture_case_id": "refcase-08a",
            "official_ref_id": "REF-08",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF08_IN_MEMORY_DEDUP_WITHIN_LIMITS_PASSED",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "limits_and_boundary_stub": {
                "limits_convention_scope": "TEST_FIXTURE_ONLY_NUMERIC_THRESHOLDS_REVIEW_REQUIRED",
                "transport_mode": "IN_MEMORY_SAME_JOB_ONLY",
                "limits_configured": {"max_requests": 10, "max_bytes": 1048576, "max_tokens": 16000, "max_retries": 3, "max_concurrency": 4},
                "observed_usage": {"requests": 1, "bytes": len(src02_norm.encode("utf-8")), "tokens": 120, "retries": 0, "concurrency": 1},
                "limit_breached": None,
                "input_unit_refs_count": 2,
                "deduplicated_unit_count": 1,
                "duplicate_units_removed_in_job": 1,
                "inter_job_handoff_payload": None,
                "next_job_refetch_executed": False,
                "silent_truncation_occurred": False,
                "required_evidence_dropped_silently": False,
            },
            "persisted_payload_snapshot": {
                "job_id": "job-ref08a-inmem",
                "transport_mode": "IN_MEMORY_SAME_JOB_ONLY",
                "duplicate_units_removed_in_job": 1,
                "raw_text_persisted": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-08b",
            "official_ref_id": "REF-08",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF08_SEPARATE_JOB_METADATA_HANDOFF_REFETCH_PASSED",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "limits_and_boundary_stub": {
                "limits_convention_scope": "TEST_FIXTURE_ONLY_NUMERIC_THRESHOLDS_REVIEW_REQUIRED",
                "transport_mode": "SEPARATE_JOB_METADATA_ONLY_HANDOFF",
                "limits_configured": {"max_requests": 10, "max_bytes": 1048576, "max_tokens": 16000, "max_retries": 3, "max_concurrency": 4},
                "observed_usage": {"requests": 2, "bytes": len(src02_norm.encode("utf-8")), "tokens": 120, "retries": 0, "concurrency": 1},
                "limit_breached": None,
                "input_unit_refs_count": 1,
                "deduplicated_unit_count": 1,
                "duplicate_units_removed_in_job": 0,
                "inter_job_handoff_payload": {
                    "from_job_id": "job-ref08b-stage2",
                    "to_job_id": "job-ref08b-stage3-verify",
                    "source_revision_id": rev_id_by_key["src02"],
                    "content_sha256": sha256_prefixed(src02_norm),
                    "locator_id": src02_baseline_loc["locator_id"],
                    "span_sha256": src02_baseline_loc["span_sha256"],
                    "metadata_only_handoff": True,
                },
                "next_job_refetch_executed": True,
                "silent_truncation_occurred": False,
                "required_evidence_dropped_silently": False,
            },
            "persisted_payload_snapshot": {
                "job_id": "job-ref08b-stage3-verify",
                "transport_mode": "SEPARATE_JOB_METADATA_ONLY_HANDOFF",
                "next_job_refetch_executed": True,
                "raw_text_persisted": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-08c",
            "official_ref_id": "REF-08",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF08_BYTE_TOKEN_LIMIT_EXCEEDED_BLOCKED_NO_TRUNCATION",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "limits_and_boundary_stub": {
                "limits_convention_scope": "TEST_FIXTURE_ONLY_NUMERIC_THRESHOLDS_REVIEW_REQUIRED",
                "transport_mode": "IN_MEMORY_SAME_JOB_ONLY",
                "limits_configured": {"max_requests": 10, "max_bytes": 128, "max_tokens": 32, "max_retries": 3, "max_concurrency": 4},
                "observed_usage": {"requests": 1, "bytes": len(src02_norm.encode("utf-8")), "tokens": 120, "retries": 0, "concurrency": 1},
                "limit_breached": "BYTE_AND_TOKEN_LIMIT_EXCEEDED",
                "input_unit_refs_count": 1,
                "deduplicated_unit_count": 1,
                "duplicate_units_removed_in_job": 0,
                "inter_job_handoff_payload": None,
                "next_job_refetch_executed": False,
                "silent_truncation_occurred": False,
                "required_evidence_dropped_silently": False,
            },
            "persisted_payload_snapshot": {
                "job_id": "job-ref08c-limit-blocked",
                "limit_breached": "BYTE_AND_TOKEN_LIMIT_EXCEEDED",
                "approval_blocked": True,
                "silent_truncation_occurred": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "fixture_case_id": "refcase-08d",
            "official_ref_id": "REF-08",
            "legacy_draft_id": None,
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "STUB_REF08_REQUEST_RETRY_CONCURRENCY_LIMIT_BLOCKED_NO_TRUNCATION",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "limits_and_boundary_stub": {
                "limits_convention_scope": "TEST_FIXTURE_ONLY_NUMERIC_THRESHOLDS_REVIEW_REQUIRED",
                "transport_mode": "IN_MEMORY_SAME_JOB_ONLY",
                "limits_configured": {"max_requests": 5, "max_bytes": 1048576, "max_tokens": 16000, "max_retries": 3, "max_concurrency": 2},
                "observed_usage": {"requests": 6, "bytes": 0, "tokens": 0, "retries": 4, "concurrency": 3},
                "limit_breached": "REQUEST_RETRY_CONCURRENCY_LIMIT_EXCEEDED",
                "input_unit_refs_count": 1,
                "deduplicated_unit_count": 1,
                "duplicate_units_removed_in_job": 0,
                "inter_job_handoff_payload": None,
                "next_job_refetch_executed": False,
                "silent_truncation_occurred": False,
                "required_evidence_dropped_silently": False,
            },
            "persisted_payload_snapshot": {
                "job_id": "job-ref08d-limit-blocked",
                "limit_breached": "REQUEST_RETRY_CONCURRENCY_LIMIT_EXCEEDED",
                "approval_blocked": True,
                "silent_truncation_occurred": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
    ]

    # --- Expected Oracle (v0.4.0-owner-review-candidate, PENDING_OWNER_REVIEW, ADR-018 compliant) ---
    expected_scenarios = [
        {
            "scenario_id": "SCN-01",
            "evaluation_family_id": "family-src01",
            "title": "Zero facts from standup boilerplate + ungrounded candidate claim",
            "notion_test_ids": ["T-004A"],
            "coverage_tags": ["zero_facts", "hard_negative", "ungrounded_claim", "mixed_ko_en"],
            "expected_candidate_count": 0,
            "expected_fact_count": 0,
            "expected_atom_count": 1,
            "review_required_items": ["Confirm 0-candidate & 0-fact threshold for pure scheduling/boilerplate notes"],
            "rationale": "FactCandidateExtractor yields 0 candidates from standup chatter; required atom-01-1 has 0 supports (UNSUPPORTED) and cannot compute SOURCE_VERIFIED.",
        },
        {
            "scenario_id": "SCN-02",
            "evaluation_family_id": "family-src02",
            "title": "Single fact clean individual delivery baseline (implementation + conditional effect atoms)",
            "notion_test_ids": ["T-004A"],
            "coverage_tags": ["single_fact", "direct_support", "individual_attribution", "effect_with_condition"],
            "expected_candidate_count": 1,
            "expected_fact_count": 1,
            "expected_atom_count": 2,
            "review_required_items": ["Confirm Stage 2A->2B->3A->3B->3C separation and 2/2 SUPPORTED atoms (implementation + conditional effect) for SOURCE_VERIFIED"],
            "rationale": "Single unambiguous PR directly supports both implementation (atom-02-1) and conditional duplicate-notification prevention (atom-02-2).",
        },
        {
            "scenario_id": "SCN-03",
            "evaluation_family_id": "family-src03",
            "title": "Multiple (N=3) facts from Korean postmortem with split action/metric atoms and multi-byte UTF-8 offsets",
            "notion_test_ids": ["T-004A", "CLAIM-06"],
            "coverage_tags": ["n_facts", "multi_atom_claim", "korean_utf8_offsets", "direct_support"],
            "expected_candidate_count": 3,
            "expected_fact_count": 3,
            "expected_atom_count": 4,
            "review_required_items": ["Confirm fact-03-1 METRIC classification and 4/4 SUPPORTED Korean atoms"],
            "rationale": "Three Korean bullets yield 3 candidates -> 3 SUPPORTED facts (METRIC, ACTION, ACTION) and 4 SUPPORTED atoms.",
        },
        {
            "scenario_id": "SCN-04",
            "evaluation_family_id": "family-src04",
            "title": "DIRECT+CONTEXT grounding vs CONTEXT-only insufficient grounding",
            "notion_test_ids": ["T-004A"],
            "coverage_tags": ["direct_support", "context_support", "hard_negative"],
            "expected_candidate_count": 2,
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "review_required_items": ["Confirm CONTEXT-only atom-04-2 resolves to UNSUPPORTED and blocks SOURCE_VERIFIED"],
            "rationale": "claim-04-1 is DIRECT+CONTEXT grounded (SUPPORTED); claim-04-2 has CONTEXT only (UNSUPPORTED).",
        },
        {
            "scenario_id": "SCN-05",
            "evaluation_family_id": "family-src05",
            "title": "CONTRADICTS across incident draft vs addendum + separation of log-confirmed fact from unverified personal execution/audit claims",
            "notion_test_ids": ["T-004A", "CLAIM-05"],
            "coverage_tags": ["contradicts", "multi_revision", "correction_preserved", "attribution_split", "partial_support", "hard_negative"],
            "expected_candidate_count": 2,
            "expected_fact_count": 2,
            "expected_atom_count": 4,
            "review_required_items": [
                "Confirm Stage 2B preserves correction addendum (fact-05-2 SUPPORTED) while marking contradicted draft (fact-05-1) as non-active conflicting record",
                "Confirm claim-05-2 atom-05-3=SUPPORTED vs personal audit atom-05-4=UNSUPPORTED (blocks SOURCE_VERIFIED)"
            ],
            "rationale": "Correction sentence is NOT discarded: it directly supports fact-05-2 and contradicts fact-05-1; personal execution/audit attributions lack direct evidence.",
        },
        {
            "scenario_id": "SCN-06",
            "evaluation_family_id": "family-src06",
            "title": "Completed delivery vs planned Q4 RFC work (including Stage 2B rejection of plan-as-completed extraction)",
            "notion_test_ids": ["T-004A"],
            "coverage_tags": ["planned_vs_completed", "contradicts", "unpromoted_candidate_rejected", "korean_utf8_offsets", "hard_negative"],
            "expected_candidate_count": 3,
            "expected_fact_count": 2,
            "expected_atom_count": 4,
            "review_required_items": [
                "Confirm Stage 2B rejects fcand-06-3-rejected (plan promoted as completed) while keeping fact-06-2 as PLANNED to contradict claim-06-2"
            ],
            "rationale": "Plan sentence is preserved as a PLANNED fact (fact-06-2) rather than deleted; extractor mistake elevating plan to completed (fcand-06-3-rejected) is rejected by FactSupportValidator.",
        },
        {
            "scenario_id": "SCN-07",
            "evaluation_family_id": "family-src07",
            "title": "Negation context (rollback KEPT; negated deployment & 30% savings preserved; Stage 2B rejects negation-stripping extraction)",
            "notion_test_ids": ["T-004A"],
            "coverage_tags": ["negation_context", "contradicts", "unpromoted_candidate_rejected", "hard_negative"],
            "expected_candidate_count": 4,
            "expected_fact_count": 3,
            "expected_atom_count": 3,
            "review_required_items": [
                "Confirm Stage 2B preserves negated facts (fact-07-2, fact-07-3) and rejects positive extractor error fcand-07-4-rejected"
            ],
            "rationale": "Explicit negations support negated CanonicalFacts that refute claim-07-2; extractor error stripping negation is rejected at Stage 2B.",
        },
        {
            "scenario_id": "SCN-08",
            "evaluation_family_id": "family-src08",
            "title": "Team achievement vs individual contribution (Stage 2B rejects team-to-individual promotion; claim-08-2 splits solo conflict)",
            "notion_test_ids": ["T-004A", "CLAIM-05"],
            "coverage_tags": ["team_vs_individual", "contradicts", "unpromoted_candidate_rejected", "context_support", "hard_negative"],
            "expected_candidate_count": 3,
            "expected_fact_count": 2,
            "expected_atom_count": 4,
            "review_required_items": [
                "Confirm Stage 2B rejects fcand-08-3-rejected (6-engineer team work elevated to solo attribution) and preserves fact-08-1 at TEAM scope"
            ],
            "rationale": "Team work is preserved at TEAM scope and never promoted to individual attribution; claim-08-1 passes while solo-overclaim claim-08-2 fails.",
        },
        {
            "scenario_id": "SCN-09",
            "evaluation_family_id": "family-src09",
            "title": "Staging/test benchmark vs unstarted production deployment & staging-only metric split",
            "notion_test_ids": ["T-004A", "CLAIM-06"],
            "coverage_tags": ["test_vs_production", "contradicts", "context_support", "korean_utf8_offsets", "hard_negative"],
            "expected_candidate_count": 2,
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "review_required_items": [
                "Confirm fact-09-1 STAGING_TEST metric_details and fact-09-2 negated production rollout blocking claim-09-2"
            ],
            "rationale": "claim-09-1 accurately scopes the staging benchmark; claim-09-2 fails due to unstarted production rollout (CONTRADICTS) and staging-only metric (CONTEXT).",
        },
        {
            "scenario_id": "SCN-10",
            "evaluation_family_id": "family-src10",
            "title": "Unverified causation and fabricated metric in a 3-atom partial claim",
            "notion_test_ids": ["T-004A", "CLAIM-06"],
            "coverage_tags": ["unverified_metric_causation", "partial_support", "hard_negative"],
            "expected_candidate_count": 2,
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "review_required_items": ["Confirm confounded 18% revenue correlation (CONTEXT) + fabricated 45% lift (0 supports) stay UNSUPPORTED"],
            "rationale": "Only 1 of 3 required atoms is SUPPORTED; causal overclaim and fabricated metric remain UNSUPPORTED.",
        },
        {
            "scenario_id": "SCN-11",
            "evaluation_family_id": "family-src11",
            "title": "Identity ambiguity (unmapped homonym 'Alex') vs verified account author",
            "notion_test_ids": ["T-004A", "CLAIM-05"],
            "coverage_tags": ["identity_ambiguity", "hard_negative"],
            "expected_candidate_count": 2,
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "review_required_items": ["Confirm ambiguous identity fact-11-1 (is_active_supported_fact=false) blocks claim-11-1 SOURCE_VERIFIED"],
            "rationale": "Unmapped 'Alex' blocks SOURCE_VERIFIED on claim-11-1; verified owner PR passes claim-11-2.",
        },
        {
            "scenario_id": "SCN-12",
            "evaluation_family_id": "family-src12",
            "title": "CLAIM-08 User edit & user confirmation matrix: v2 failed edit, v2 STALE un-reverified edit, v2 re-verified PASS, and USER_CONFIRMED-only FAIL",
            "notion_test_ids": ["T-004A", "CLAIM-08"],
            "coverage_tags": ["user_edit_new_version_reverification", "user_confirmed_not_source_verified", "stale_atom_status", "scope_mismatch", "hard_negative"],
            "expected_candidate_count": 1,
            "expected_fact_count": 1,
            "expected_atom_count": 6,
            "review_required_items": [
                "Confirm CLAIM-08 4-case matrix in family-src12: claim-12-1 (v2 failed -> UNSUPPORTED), claim-12-2 (v2 un-reverified -> STALE), claim-12-3 (v2 re-verified -> SUPPORTED & SOURCE_VERIFIED), claim-12-4 (atom-12-6 USER_CONFIRMED -> SOURCE_VERIFIED forbidden)"
            ],
            "rationale": "Covers all official Atom statuses related to user edits/confirmations (SUPPORTED, STALE, UNSUPPORTED, USER_CONFIRMED) while keeping all src12 derivatives inside family-src12 (DEV).",
        },
        {
            "scenario_id": "SCN-13",
            "evaluation_family_id": "family-src13",
            "title": "OD-003 user source deletion: preview + explicit confirmation -> immediate access block -> full purge of dependent Claims & server copies (even mixed-source), preserving independent Source",
            "notion_test_ids": ["T-004A", "OD-003"],
            "coverage_tags": ["od003_user_delete", "immediate_access_block", "full_server_purge", "independent_source_preserved"],
            "expected_candidate_count": 2,
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "review_required_items": [
                "Confirm OD-003 full purge of claim-13-1 & mixed-source claim-13-2 (0 retained versions/edits/approvals, never PARTIALLY_GROUNDED) and preservation of independent src13b/fact-13-2"
            ],
            "rationale": "Preserves OD-003 user-deletion cascade semantics cleanly separated from REF-03 refetch unavailability.",
        },
        {
            "scenario_id": "SCN-14",
            "evaluation_family_id": "family-src14",
            "title": "Hard negatives bundle: 10x numeric distortion and cross-project conflation",
            "notion_test_ids": ["T-004A", "CLAIM-06"],
            "coverage_tags": ["hard_negative", "partial_support", "contradicts", "numeric_distortion", "entity_conflation"],
            "expected_candidate_count": 2,
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "review_required_items": ["Confirm 400ms->40ms distortion & Alpha/Borealis conflation keep claim-14-1 NOT SOURCE_VERIFIED"],
            "rationale": "Only atom-14-1 is SUPPORTED; atom-14-2 (10x distortion) and atom-14-3 (project conflation) remain UNSUPPORTED.",
        },
        {
            "scenario_id": "SCN-15",
            "evaluation_family_id": "family-src15",
            "title": "CLAIM-05 Role attribution pass/fail: verified Tech Lead contribution (PASS) vs unrecorded Security Lead role (FAIL)",
            "notion_test_ids": ["CLAIM-05"],
            "coverage_tags": ["claim_05_role_attribution", "official_fact_type_role", "direct_support", "contradicts", "hard_negative"],
            "expected_candidate_count": 2,
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "review_required_items": [
                "Confirm CLAIM-05 PASS (claim-15-1: explicit Tech Lead role + RFC/review contribution evidence) vs FAIL (claim-15-2: Security Lead role contradicted by Security-team-led audit with no Alex Kim role)"
            ],
            "rationale": "Tests official ROLE Fact type and verifies roles are supported only when direct personal contribution evidence exists.",
        },
        {
            "scenario_id": "SCN-16",
            "evaluation_family_id": "family-src16",
            "title": "CLAIM-06 Metric grounding pass/fail + official PROBLEM/TECHNOLOGY/METRIC Fact types (value/unit/period/source/env/scope)",
            "notion_test_ids": ["CLAIM-06"],
            "coverage_tags": ["claim_06_metric_grounding", "official_fact_type_problem", "official_fact_type_technology", "official_fact_type_metric", "hard_negative"],
            "expected_candidate_count": 3,
            "expected_fact_count": 3,
            "expected_atom_count": 3,
            "review_required_items": [
                "Confirm CLAIM-06 PASS (claim-16-1: exact value/unit/30-day period/Datadog APM/production checkout-api p99) vs FAIL (claim-16-2: overbroad company-wide & full-year 2026 scope)"
            ],
            "rationale": "Tests structured metric grounding (value, unit, period, source basis, environment, measurement scope) and covers PROBLEM, TECHNOLOGY, and METRIC official Fact types.",
        },
    ]
    for scn in expected_scenarios:
        scn["evaluation_partition"] = FAMILY_PARTITION_MAP[scn["evaluation_family_id"]]
        scn["oracle_status"] = ORACLE_STATUS_PENDING

    # Tuple format: (atom_id, scenario_id, claim_id, expected_domain_atom_status, draft_atom_verdict, od003_purged, rationale)
    expected_atoms_specs = [
        ("atom-01-1", "SCN-01", "claim-01-1", "UNSUPPORTED", "DRAFT_ATOM_UNSUPPORTED", False, "0 canonical facts exist in boilerplate source src01."),
        ("atom-02-1", "SCN-02", "claim-02-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Implementation of idempotent retry queue with exponential backoff for billing webhooks in Project Aurora is directly supported by active SUPPORTED fact-02-1."),
        ("atom-02-2", "SCN-02", "claim-02-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Conditional effect (preventing duplicate invoice notifications during transient gateway timeouts) is directly supported by active SUPPORTED fact-02-1."),
        ("atom-03-1", "SCN-03", "claim-03-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Shard redistribution script authoring action is directly supported by Korean bullet 1 (fact-03-1)."),
        ("atom-03-2", "SCN-03", "claim-03-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Production cluster p95 latency reduction (620ms -> 180ms) is independently directly supported by Korean bullet 1 (fact-03-1)."),
        ("atom-03-3", "SCN-03", "claim-03-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Automatic rollback within 2 minutes on deployment failure is directly supported by Korean bullet 2 (fact-03-2)."),
        ("atom-03-4", "SCN-03", "claim-03-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Slow-query alert rule above 300ms threshold is directly supported by Korean bullet 3 (fact-03-3)."),
        ("atom-04-1", "SCN-04", "claim-04-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT_WITH_CONTEXT", False, "WorkerPool v2 production migration is directly supported by fact-04-1 with initiative overview fact-04-2 as CONTEXT."),
        ("atom-04-2", "SCN-04", "claim-04-2", "UNSUPPORTED", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", False, "Only initiative goal CONTEXT exists (fact-04-2); CONTEXT-only cannot verify."),
        ("atom-05-1", "SCN-05", "claim-05-1", "UNSUPPORTED", "DRAFT_ATOM_UNSUPPORTED", False, "Neither incident draft (src05a) nor addendum (src05b) states that the user personally executed the database primary failover (0 supports)."),
        ("atom-05-2", "SCN-05", "claim-05-1", "UNSUPPORTED", "DRAFT_ATOM_CONTRADICTED", False, "Initial draft zero-drop assertion (fact-05-1, non-active conflicting record) is explicitly refuted by postmortem addendum fact-05-2."),
        ("atom-05-3", "SCN-05", "claim-05-2", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Postmortem addendum directly confirms that log audit recorded 412 HTTP 503 checkout responses during the 90-second failover window (fact-05-2)."),
        ("atom-05-4", "SCN-05", "claim-05-2", "UNSUPPORTED", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", False, "Passive 'Log audit confirmed' in fact-05-2 does NOT directly establish that the user personally performed the log audit and documentation (CONTEXT-only -> UNSUPPORTED)."),
        ("atom-06-1", "SCN-06", "claim-06-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Read-replica health-check endpoint implementation is directly supported by completed section fact-06-1."),
        ("atom-06-2", "SCN-06", "claim-06-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Production monitoring integration of the health-check endpoint is independently directly supported by completed section fact-06-1."),
        ("atom-06-3", "SCN-06", "claim-06-2", "UNSUPPORTED", "DRAFT_ATOM_CONTRADICTED", False, "Source RFC lists user_events sharding as a future Q4 plan at observation time (fact-06-2), contradicting completed claim."),
        ("atom-06-4", "SCN-06", "claim-06-2", "UNSUPPORTED", "DRAFT_ATOM_CONTRADICTED", False, "Source RFC lists prototype benchmark as a next-month plan at observation time (fact-06-2), contradicting completed claim."),
        ("atom-07-1", "SCN-07", "claim-07-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Directly supported rollback action fact-07-1."),
        ("atom-07-2", "SCN-07", "claim-07-2", "UNSUPPORTED", "DRAFT_ATOM_CONTRADICTED", False, "Production deployment claim is directly contradicted by explicit negation fact-07-2."),
        ("atom-07-3", "SCN-07", "claim-07-2", "UNSUPPORTED", "DRAFT_ATOM_CONTRADICTED", False, "30% compute cost savings claim is independently contradicted by explicit negation fact-07-3."),
        ("atom-08-1", "SCN-08", "claim-08-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT_WITH_CONTEXT", False, "Individual cache layer directly supported (fact-08-2); team rebuild provides CONTEXT (fact-08-1)."),
        ("atom-08-2", "SCN-08", "claim-08-2", "UNSUPPORTED", "DRAFT_ATOM_CONTRADICTED", False, "Claiming to have single-handedly rebuilt the entire CI pipeline directly contradicts 6-engineer team attribution in fact-08-1."),
        ("atom-08-3", "SCN-08", "claim-08-2", "UNSUPPORTED", "DRAFT_ATOM_UNSUPPORTED", False, "No source evidence supports the 'from scratch' qualifier (0 supports)."),
        ("atom-08-4", "SCN-08", "claim-08-2", "UNSUPPORTED", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", False, "22m -> 8m median build duration reduction is preserved strictly as a team-level outcome (CONTEXT-only for an individual solo-rebuild claim)."),
        ("atom-09-1", "SCN-09", "claim-09-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Accurately scoped to local staging sandbox benchmark (fact-09-1)."),
        ("atom-09-2", "SCN-09", "claim-09-2", "UNSUPPORTED", "DRAFT_ATOM_CONTRADICTED", False, "Production database deployment claim directly contradicts negated fact-09-2."),
        ("atom-09-3", "SCN-09", "claim-09-2", "UNSUPPORTED", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", False, "840ms -> 95ms p95 improvement was observed only in local staging sandbox (fact-09-1); CONTEXT-only cannot verify a production claim."),
        ("atom-10-1", "SCN-10", "claim-10-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Launch date May 12 directly supported by fact-10-1."),
        ("atom-10-2", "SCN-10", "claim-10-1", "UNSUPPORTED", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", False, "Revenue correlation with discount confounder and no A/B test is CONTEXT-only, not causal proof."),
        ("atom-10-3", "SCN-10", "claim-10-1", "UNSUPPORTED", "DRAFT_ATOM_UNSUPPORTED", False, "Required atom has 0 supporting facts (fabricated 45% conversion lift)."),
        ("atom-11-1", "SCN-11", "claim-11-1", "UNSUPPORTED", "DRAFT_ATOM_IDENTITY_AMBIGUOUS", False, "Source names unmapped 'Alex' across two workspace members (fact-11-1 is_active_supported_fact=false)."),
        ("atom-11-2", "SCN-11", "claim-11-2", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Verified owner account PR directly supports unit test addition (fact-11-2)."),
        ("atom-12-1", "SCN-12", "claim-12-1", "UNSUPPORTED", "DRAFT_ATOM_REVERIFY_FAILED_AFTER_EDIT", False, "User edit to v2 expanded scope to global tuning across all services; checkout-only fact-12-1 fails v2 re-verification (CONTEXT-only -> UNSUPPORTED)."),
        ("atom-12-3", "SCN-12", "claim-12-1", "UNSUPPORTED", "DRAFT_ATOM_REVERIFY_FAILED_AFTER_EDIT", False, "User edit to v2 claimed 60% global p99 reduction vs 25% checkout peak-load p99 in fact-12-1; fails v2 re-verification without inferring universal falsity."),
        ("atom-12-2", "SCN-12", "claim-12-2", "STALE", "DRAFT_ATOM_REVERIFY_REQUIRED_NEW_VERSION", False, "CLAIM-08: User paraphrase created v2; existing support cas-12-2 was only evaluated on v1 -> official Atom status is STALE until re-verified on v2."),
        ("atom-12-4", "SCN-12", "claim-12-3", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "CLAIM-08 PASS: User paraphrase v2 re-verified on v2 by distinct ClaimSupportValidator against active SUPPORTED fact-12-1 -> SUPPORTED."),
        ("atom-12-5", "SCN-12", "claim-12-4", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Checkout service pool tuning & 25% peak p99 latency reduction is directly supported on v2 by fact-12-1."),
        ("atom-12-6", "SCN-12", "claim-12-4", "USER_CONFIRMED", "DRAFT_ATOM_USER_CONFIRMED_NOT_SOURCE_VERIFIED", False, "CLAIM-08 FAIL: Owner explicitly confirmed '$4,000/month RDS spend savings' without source Fact support -> USER_CONFIRMED (NEVER qualifies for SOURCE_VERIFIED)."),
        ("atom-13-1", "SCN-13", "claim-13-1", "UNSUPPORTED", "DRAFT_ATOM_PURGED_WITH_DEPENDENT_CLAIM_OD003", True, "OD-003: src13a deletion preview + explicit confirmation immediately blocked claim-13-1; atom-13-1 purged with claim-13-1 on cleanup."),
        ("atom-13-2", "SCN-13", "claim-13-2", "UNSUPPORTED", "DRAFT_ATOM_PURGED_WITH_DEPENDENT_CLAIM_OD003", True, "OD-003: src13a deleted; atom-13-2 purged together with dependent claim-13-2 and all server copies."),
        ("atom-13-3", "SCN-13", "claim-13-2", "UNSUPPORTED", "DRAFT_ATOM_PURGED_WITH_DEPENDENT_CLAIM_OD003", True, "OD-003: Purged together with mixed-source dependent claim-13-2 (not left as surviving partial claim); independent source src13b & fact-13-2 preserved."),
        ("atom-14-1", "SCN-14", "claim-14-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "Directly supported by Project Borealis fact-14-2."),
        ("atom-14-2", "SCN-14", "claim-14-1", "UNSUPPORTED", "DRAFT_ATOM_CONTRADICTED", False, "10x numeric distortion: source fact-14-1 states 400ms, claim states 40ms."),
        ("atom-14-3", "SCN-14", "claim-14-1", "UNSUPPORTED", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", False, "Cross-project conflation attaching Project Alpha metric to Project Borealis (CONTEXT-only)."),
        ("atom-15-1", "SCN-15", "claim-15-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "CLAIM-05 PASS: Tech Lead role for Q1 2026 payments ledger migration squad directly evidenced in fact-15-1."),
        ("atom-15-2", "SCN-15", "claim-15-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "CLAIM-05 PASS: Concrete individual contribution (dual-write cutover RFC & schema rollout sign-off) directly evidenced in fact-15-1."),
        ("atom-15-3", "SCN-15", "claim-15-2", "UNSUPPORTED", "DRAFT_ATOM_CONTRADICTED", False, "CLAIM-05 FAIL: Security Lead role claim directly contradicted by fact-15-2 (audit led by Security team; no recorded role for Alex Kim)."),
        ("atom-16-1", "SCN-16", "claim-16-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT_WITH_CONTEXT", False, "CLAIM-06 PASS: PostgreSQL 16 covering composite index directly supported by fact-16-2 with problem context fact-16-1."),
        ("atom-16-2", "SCN-16", "claim-16-1", "SUPPORTED", "DRAFT_ATOM_SUPPORTED_DIRECT", False, "CLAIM-06 PASS: Complete metric (920ms->210ms / 77.2%, production checkout-api p99, 2026-04-01..2026-04-30, Datadog APM) directly supported by fact-16-3."),
        ("atom-16-3", "SCN-16", "claim-16-2", "UNSUPPORTED", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", False, "CLAIM-06 FAIL: Company-wide all-services & full-year 2026 claim exceeds 30-day April 2026 production checkout-api Datadog APM scope (CONTEXT-only -> UNSUPPORTED)."),
    ]

    od003_expected_by_claim = {
        "claim-13-1": {
            "immediate_access_blocked_after_confirmation": True,
            "claim_and_server_copies_purged": True,
            "retained_claim_versions": [],
            "retained_edited_copies": 0,
            "retained_approved_copies": 0,
            "independent_source_revisions_preserved": [rev_id_by_key["src13b"]],
            "independent_facts_preserved": ["fact-13-2"],
            "distinguished_from_ref03_refetch_failure": True,
            "partially_grounded_forbidden_post_deletion": True,
        },
        "claim-13-2": {
            "immediate_access_blocked_after_confirmation": True,
            "claim_and_server_copies_purged": True,
            "retained_claim_versions": [],
            "retained_edited_copies": 0,
            "retained_approved_copies": 0,
            "independent_source_revisions_preserved": [rev_id_by_key["src13b"]],
            "independent_facts_preserved": ["fact-13-2"],
            "distinguished_from_ref03_refetch_failure": True,
            "partially_grounded_forbidden_post_deletion": True,
        },
    }

    # Tuple format: (claim_id, scenario_id, notion_test_ids, source_verified_computed, draft_claim_verdict, proposal, rationale)
    expected_claims_specs = [
        ("claim-01-1", "SCN-01", ["T-004A"], False, "DRAFT_CLAIM_UNGROUNDED", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "0 supporting facts for required atom-01-1; SOURCE_VERIFIED forbidden."),
        ("claim-02-1", "SCN-02", ["T-004A"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "2/2 required atoms SUPPORTED (retry queue implementation + conditional duplicate-notification prevention), active SUPPORTED fact, same-job refetch pass -> computed SOURCE_VERIFIED."),
        ("claim-03-1", "SCN-03", ["T-004A", "CLAIM-06"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "4/4 required Korean atoms SUPPORTED (shard script action, prod p95 620->180ms metric, 2-min rollback, >300ms alert) -> computed SOURCE_VERIFIED."),
        ("claim-04-1", "SCN-04", ["T-004A"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "DIRECT + CONTEXT support on required atom-04-1 -> computed SOURCE_VERIFIED."),
        ("claim-04-2", "SCN-04", ["T-004A"], False, "DRAFT_CLAIM_UNGROUNDED", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "CONTEXT-only support on required atom-04-2 (UNSUPPORTED); SOURCE_VERIFIED forbidden."),
        ("claim-05-1", "SCN-05", ["T-004A", "CLAIM-05"], False, "DRAFT_CLAIM_CONFLICTING", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "Personal failover execution (atom-05-1) is UNSUPPORTED and zero-drop claim (atom-05-2) is contradicted by postmortem addendum; SOURCE_VERIFIED forbidden."),
        ("claim-05-2", "SCN-05", ["T-004A", "CLAIM-05"], False, "DRAFT_CLAIM_PARTIALLY_GROUNDED", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "1/2 required atoms SUPPORTED (atom-05-3), but personal log-audit & documentation attribution (atom-05-4) is UNSUPPORTED (CONTEXT-only); SOURCE_VERIFIED forbidden."),
        ("claim-06-1", "SCN-06", ["T-004A"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "2/2 required atoms SUPPORTED (health-check endpoint implementation + production monitoring integration) -> computed SOURCE_VERIFIED."),
        ("claim-06-2", "SCN-06", ["T-004A"], False, "DRAFT_CLAIM_CONFLICTING", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "Both sharding completion and benchmark completion contradict RFC plan at observation time; SOURCE_VERIFIED forbidden."),
        ("claim-07-1", "SCN-07", ["T-004A"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "Accurately describes reverted rollout (atom-07-1 SUPPORTED) -> computed SOURCE_VERIFIED."),
        ("claim-07-2", "SCN-07", ["T-004A"], False, "DRAFT_CLAIM_CONFLICTING", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "Both production deployment and 30% compute cost savings are independently negated in source; SOURCE_VERIFIED forbidden."),
        ("claim-08-1", "SCN-08", ["T-004A", "CLAIM-05"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "Accurately scoped to individual artifact cache contribution -> computed SOURCE_VERIFIED."),
        ("claim-08-2", "SCN-08", ["T-004A", "CLAIM-05"], False, "DRAFT_CLAIM_CONFLICTING", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "Solo rebuild attribution contradicts 6-engineer team work, 'from scratch' is unsupported, and 22->8m is team-only CONTEXT; SOURCE_VERIFIED forbidden."),
        ("claim-09-1", "SCN-09", ["T-004A", "CLAIM-06"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "Accurately scoped to local staging sandbox benchmark -> computed SOURCE_VERIFIED."),
        ("claim-09-2", "SCN-09", ["T-004A", "CLAIM-06"], False, "DRAFT_CLAIM_CONFLICTING", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "Production rollout contradicts unstarted rollout and production p95 metric has only staging CONTEXT; SOURCE_VERIFIED forbidden."),
        ("claim-10-1", "SCN-10", ["T-004A", "CLAIM-06"], False, "DRAFT_CLAIM_PARTIALLY_GROUNDED", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "Only 1/3 required atoms SUPPORTED (atom-10-2 CONTEXT-only, atom-10-3 0 supports); SOURCE_VERIFIED forbidden."),
        ("claim-11-1", "SCN-11", ["T-004A", "CLAIM-05"], False, "DRAFT_CLAIM_IDENTITY_AMBIGUOUS", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "Ambiguous 'Alex' author (fact-11-1 inactive) blocks SOURCE_VERIFIED."),
        ("claim-11-2", "SCN-11", ["T-004A", "CLAIM-05"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "Unambiguous verified account PR -> computed SOURCE_VERIFIED."),
        ("claim-12-1", "SCN-12", ["T-004A", "CLAIM-08"], False, "DRAFT_CLAIM_REVERIFY_FAILED_AFTER_EDIT", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "CLAIM-08 FAIL: User edit v2 over-generalized to global scope & 60% metric; v2 re-verification fails (both atoms UNSUPPORTED); SOURCE_VERIFIED forbidden."),
        ("claim-12-2", "SCN-12", ["T-004A", "CLAIM-08"], False, "DRAFT_CLAIM_REVERIFY_REQUIRED_NEW_VERSION", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "CLAIM-08 STALE: User paraphrase v2 has only stale v1 support (atom-12-2=STALE); SOURCE_VERIFIED forbidden until re-verified on v2."),
        ("claim-12-3", "SCN-12", ["CLAIM-08"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "CLAIM-08 PASS: User paraphrase v2 re-verified on v2 by distinct ClaimSupportValidator + same-job refetch against active SUPPORTED fact-12-1 -> computed SOURCE_VERIFIED."),
        ("claim-12-4", "SCN-12", ["CLAIM-08"], False, "DRAFT_CLAIM_USER_CONFIRMED_ONLY_NOT_SOURCE_VERIFIED", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "CLAIM-08 FAIL: atom-12-6 ($4,000/month RDS savings) is USER_CONFIRMED only (no active SUPPORTED Fact / DIRECT support) -> SOURCE_VERIFIED strictly forbidden."),
        ("claim-13-1", "SCN-13", ["T-004A", "OD-003"], False, "DRAFT_CLAIM_BLOCKED_THEN_PURGED_OD003", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "OD-003: Deletion preview + explicit confirmation of src13a immediately blocks access; cleanup purges claim-13-1 & server copies (distinct from REF-03). Independent src13b preserved."),
        ("claim-13-2", "SCN-13", ["T-004A", "OD-003"], False, "DRAFT_CLAIM_BLOCKED_THEN_PURGED_OD003", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "OD-003: Mixed-source dependent claim-13-2 immediately blocked and purged with all server copies & versions [1,2] (PARTIALLY_GROUNDED forbidden). Independent src13b & fact-13-2 preserved."),
        ("claim-14-1", "SCN-14", ["T-004A", "CLAIM-06"], False, "DRAFT_CLAIM_CONFLICTING", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "Required atoms include CONTRADICTED 10x distortion and CONTEXT-only conflation; SOURCE_VERIFIED forbidden."),
        ("claim-15-1", "SCN-15", ["CLAIM-05"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "CLAIM-05 PASS: Tech Lead role (atom-15-1) and concrete RFC/review contributions (atom-15-2) both SUPPORTED by fact-15-1 -> computed SOURCE_VERIFIED."),
        ("claim-15-2", "SCN-15", ["CLAIM-05"], False, "DRAFT_CLAIM_CONFLICTING", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "CLAIM-05 FAIL: Security Lead role claim lacks personal contribution evidence and is contradicted by fact-15-2; SOURCE_VERIFIED forbidden."),
        ("claim-16-1", "SCN-16", ["CLAIM-06"], True, "DRAFT_CLAIM_GROUNDED_CANDIDATE", "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", "CLAIM-06 PASS: PostgreSQL 16 index (atom-16-1) + full metric with value/unit/30-day period/Datadog APM/prod checkout-api p99 scope (atom-16-2) both SUPPORTED -> computed SOURCE_VERIFIED."),
        ("claim-16-2", "SCN-16", ["CLAIM-06"], False, "DRAFT_CLAIM_UNGROUNDED", "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", "CLAIM-06 FAIL: Company-wide & full-year 2026 metric over-generalizes 30-day April 2026 checkout-api p99 Datadog APM scope (CONTEXT-only -> UNSUPPORTED); SOURCE_VERIFIED forbidden."),
    ]

    # Official REF-01..REF-08 Matrix: ALL 8 have synthetic cases prepared, ALL 8 have product_runtime_verified=False!
    official_ref_matrix = [
        {
            "official_ref_id": "REF-01",
            "official_requirement_label": "동일 버전과 정확한 근거 위치 재현",
            "synthetic_fixture_coverage_status": "SYNTHETIC_STUB_PREPARED",
            "product_runtime_verified": False,
            "linked_fixture_case_ids": ["refcase-01a"],
            "coverage_gap_details": [
                "Synthetic stub & programmatic locator round-trip prepared only; live external connector HTTP refetch is NOT executed (product_runtime_verified=false)."
            ],
            "oracle_status": ORACLE_STATUS_PENDING,
            "review_required_items": ["확인 필요: Production normalization/offset contract for exact span reproduction"],
        },
        {
            "official_ref_id": "REF-02",
            "official_requirement_label": "hash/locator 불일치 차단",
            "synthetic_fixture_coverage_status": "SYNTHETIC_STUB_PREPARED",
            "product_runtime_verified": False,
            "linked_fixture_case_ids": ["refcase-02a", "refcase-02b", "refcase-02c"],
            "coverage_gap_details": [
                "Synthetic stubs separate body drift (refcase-02a), appended new revision (refcase-02b), and same-hash locator-only mismatch (refcase-02c); live external refetch is NOT executed (product_runtime_verified=false)."
            ],
            "oracle_status": ORACLE_STATUS_PENDING,
            "review_required_items": ["확인 필요: Production error telemetry separating body drift vs locator-only mismatch"],
        },
        {
            "official_ref_id": "REF-03",
            "official_requirement_label": "재조회 불가와 삭제·거짓 구분",
            "synthetic_fixture_coverage_status": "SYNTHETIC_STUB_PREPARED",
            "product_runtime_verified": False,
            "linked_fixture_case_ids": ["refcase-03a", "refcase-03b", "refcase-03c", "refcase-03d", "refcase-03e"],
            "coverage_gap_details": [
                "Synthetic stubs separate HTTP 404 (03a), partial 206 (03b), and transient 429/503/timeout (03c..03e) from SCN-13 OD-003 deletion and historical falsity; live retry/backoff & product approval-gate E2E are NOT executed (product_runtime_verified=false)."
            ],
            "oracle_status": ORACLE_STATUS_PENDING,
            "review_required_items": ["확인 필요: Production retry cap, backoff policy, and HTTP 410 vs 404 product status mapping"],
        },
        {
            "official_ref_id": "REF-04",
            "official_requirement_label": "원문 비영속",
            "synthetic_fixture_coverage_status": "PARTIAL_STATIC_FIXTURE_PREPARED",
            "product_runtime_verified": False,
            "linked_fixture_case_ids": ["refcase-04a", "refcase-04b"],
            "coverage_gap_details": [
                "PARTIAL STATIC ONLY (product_runtime_verified=false): Static canary fixtures audit JSON snapshots across DB/log/Job/cache/Export surfaces plus negative leak fixtures, but live DB tables, real app logs, real job queues, real caches, and real Export files are NOT executed in M4 product tests. RUNTIME Spike results are reference-only and do NOT substitute for M4 product tests."
            ],
            "oracle_status": ORACLE_STATUS_PENDING,
            "review_required_items": ["확인 필요: Live DB/log/Job/cache/Export raw-text non-persistence audit in M4 product QA"],
        },
        {
            "official_ref_id": "REF-05",
            "official_requirement_label": "접근 권한·허용된 URL 경계",
            "synthetic_fixture_coverage_status": "PARTIAL_STATIC_FIXTURE_PREPARED",
            "product_runtime_verified": False,
            "linked_fixture_case_ids": ["refcase-05a", "refcase-05b"],
            "coverage_gap_details": [
                "PARTIAL STATIC ONLY (product_runtime_verified=false): Covers synthetic HTTP 403 revoked-scope stub and static URL pre-check; live OAuth/tenant boundaries, HTTP 30x redirect SSRF chains, and DNS-rebinding defenses are NOT executed in M4 product tests."
            ],
            "oracle_status": ORACLE_STATUS_PENDING,
            "review_required_items": ["확인 필요: Production URL allowlist, redirect SSRF policy, and tenant/OAuth error codes"],
        },
        {
            "official_ref_id": "REF-06",
            "official_requirement_label": "중단·재실행 시 원문 저장에 의존하지 않음",
            "synthetic_fixture_coverage_status": "SYNTHETIC_CASE_PREPARED_RUNTIME_UNVERIFIED",
            "product_runtime_verified": False,
            "linked_fixture_case_ids": ["refcase-06a", "refcase-06b", "refcase-06c"],
            "coverage_gap_details": [
                "SYNTHETIC CASE PREPARED ONLY (product_runtime_verified=false): Synthetic crash-before-commit (06a), crash-after-atomic-commit idempotent replay (06b), and memory-lost changed-hash explicit block (06c) fixtures are prepared for owner review; real worker crash/DB-transaction/replay execution is NOT run here. RUNTIME Spike is reference-only and does NOT replace M4 product verification."
            ],
            "oracle_status": ORACLE_STATUS_PENDING,
            "review_required_items": ["확인 필요: Production worker checkpoint table schema, idempotency key contract, and M4 worker crash-replay test"],
        },
        {
            "official_ref_id": "REF-07",
            "official_requirement_label": "승인·Export·근거 열람 시 재조회",
            "synthetic_fixture_coverage_status": "SYNTHETIC_CASE_PREPARED_RUNTIME_UNVERIFIED",
            "product_runtime_verified": False,
            "linked_fixture_case_ids": ["refcase-07a", "refcase-07b", "refcase-07c", "refcase-07d", "refcase-07e", "refcase-07f", "refcase-07g"],
            "coverage_gap_details": [
                "SYNTHETIC CASE PREPARED ONLY (product_runtime_verified=false): Synthetic Approval (07a), Export/Download with approved Claim ID+version precondition & verification metadata without raw quotes (07b), Evidence View (07c), prior-check-only block (07d), TOCTOU pre-save change block (07e), and Export contrast blocks where refetch passes (HTTP 200) yet unapproved Claim (07f) or approved-version mismatch v2!=v1 (07g) prevents Export emission; real product Approval/Export/Evidence-Viewer E2E is NOT executed."
            ],
            "oracle_status": ORACLE_STATUS_PENDING,
            "review_required_items": [
                "확인 필요: Export file-header / API field layout for verification metadata (`locator_id`, `span_sha256`, `source_revision_id`, `source_content_sha256`, `checked_at`) is NOT frozen here (`TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN`); raw source quotes strictly excluded either way",
                "확인 필요: Product Approval / Export / Evidence-Viewer on-demand refetch E2E in M4"
            ],
        },
        {
            "official_ref_id": "REF-08",
            "official_requirement_label": "비용·크기 한도와 단계 간 메모리 전용 처리",
            "synthetic_fixture_coverage_status": "SYNTHETIC_CASE_PREPARED_RUNTIME_UNVERIFIED",
            "product_runtime_verified": False,
            "linked_fixture_case_ids": ["refcase-08a", "refcase-08b", "refcase-08c", "refcase-08d"],
            "coverage_gap_details": [
                "SYNTHETIC CASE PREPARED ONLY (product_runtime_verified=false): Synthetic in-memory same-job dedup within limits (08a), separate-job ID/hash-only handoff + next-job refetch (08b), byte/token limit block without silent truncation (08c), and request/retry/concurrency limit block (08d) fixtures are prepared for owner review; real runtime limit/memory-boundary tests are NOT executed."
            ],
            "oracle_status": ORACLE_STATUS_PENDING,
            "review_required_items": [
                "확인 필요: Final production numeric limits for max_requests, max_bytes, max_tokens, max_retries, and max_concurrency (fixture numbers are TEST_FIXTURE_ONLY)"
            ],
        },
    ]

    def _ref_oracle(
        fcid: str,
        fam: str,
        ref_id: str,
        legacy_id: str | None,
        draft_verdict: str,
        *,
        fetch_attempted: bool,
        unverifiable_now: bool,
        approval_blocked: bool,
        deleted_confirmed: bool = False,
        falsified: bool = False,
        auto_mutated: bool = False,
        canary_clean: bool = False,
        canary_neg: bool | None = None,
        post_mvp: list[str] | None = None,
        review_items: list[str],
        rationale: str,
        ref06: dict[str, Any] | None = None,
        ref07: dict[str, Any] | None = None,
        ref08: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "fixture_case_id": fcid,
            "evaluation_family_id": fam,
            "evaluation_partition": FAMILY_PARTITION_MAP[fam],
            "official_ref_id": ref_id,
            "legacy_draft_id": legacy_id,
            "fixture_test_classification": {
                "label_scope": "TEST_FIXTURE_ONLY_NOT_DOMAIN_API",
                "draft_refetch_verdict": draft_verdict,
            },
            "expected_fetch_attempted": fetch_attempted,
            "expected_unverifiable_now": unverifiable_now,
            "expected_approval_blocked": approval_blocked,
            "expected_source_deleted_confirmed": deleted_confirmed,
            "expected_historical_fact_falsified": falsified,
            "expected_claim_auto_mutated": auto_mutated,
            "expected_canary_leaked_in_clean_snapshot": canary_clean,
            "expected_canary_detected_in_negative_fixture": canary_neg,
            "post_mvp_tags": post_mvp or [],
            "oracle_status": ORACLE_STATUS_PENDING,
            "review_required_items": review_items,
            "rationale": rationale,
        }
        if ref06 is not None:
            rec["expected_ref06_assertions"] = ref06
        if ref07 is not None:
            rec["expected_ref07_assertions"] = ref07
        if ref08 is not None:
            rec["expected_ref08_assertions"] = ref08
        return rec

    expected_refetch_fixtures = [
        _ref_oracle(
            "refcase-01a", "family-src02", "REF-01", "REF-01", "DRAFT_REFETCH_EXACT_SPAN_REPRODUCED",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=False,
            post_mvp=["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            review_items=["확인 필요: Exact product status name for successful refetch reproduction"],
            rationale="Same source revision reproduces identical content_sha256 and baseline_locator span; Post-MVP background auto-detection/refresh disabled.",
        ),
        _ref_oracle(
            "refcase-02a", "family-src02", "REF-02", "REF-02", "DRAFT_REFETCH_HASH_LOCATOR_MISMATCH_BLOCKED",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            post_mvp=["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            review_items=["확인 필요: Mismatch block UX/status vs Post-MVP re-anchoring"],
            rationale="Inserted header changes content hash and shifts offsets (body change); verification & approval blocked without asserting historical falsity.",
        ),
        _ref_oracle(
            "refcase-02b", "family-src02", "REF-02", "REF-08", "DRAFT_REFETCH_HASH_MISMATCH_NEW_REVISION_BLOCKED_NO_AUTO_MUTATION",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            post_mvp=["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            review_items=["확인 필요: Policy when document hash changes via appended text while prefix span still matches"],
            rationale="Appended follow-up changes content_sha256; hash mismatch blocks silent reuse and approval without Post-MVP auto-mutation.",
        ),
        _ref_oracle(
            "refcase-02c", "family-src02", "REF-02", None, "DRAFT_REFETCH_SAME_HASH_LOCATOR_ONLY_MISMATCH_BLOCKED",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            post_mvp=["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            review_items=["확인 필요: Distinct telemetry for same-hash locator-only corruption vs body drift"],
            rationale="Identical content_sha256 but corrupted tested_locator offsets fail round-trip and block approval.",
        ),
        _ref_oracle(
            "refcase-03a", "family-src02", "REF-03", "REF-03", "DRAFT_REFETCH_UNAVAILABLE_DISTINGUISHED_FROM_DELETED_AND_FALSE",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Product status for HTTP 404 distinct from transient failures, SCN-13 deletion, and historical falsity"],
            rationale="Single HTTP 404 makes source unverifiable now and blocks approval without confirming deletion or falsifying historical facts.",
        ),
        _ref_oracle(
            "refcase-03b", "family-src02", "REF-03", "REF-04", "DRAFT_REFETCH_PARTIAL_DISTINGUISHED_FROM_DELETED_AND_FALSE",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Truncated HTTP 206 retry/fallback handling"],
            rationale="Truncated HTTP 206 omits baseline locator range -> unverifiable now and blocks approval.",
        ),
        _ref_oracle(
            "refcase-03c", "family-src02", "REF-03", None, "DRAFT_REFETCH_TRANSIENT_429_BLOCKED_APPROVAL_AFTER_RETRY_CAP",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Production max retry cap and Retry-After backoff for HTTP 429"],
            rationale="Transient HTTP 429 exhausts bounded retry cap and blocks approval, distinct from 404, deletion, and falsity.",
        ),
        _ref_oracle(
            "refcase-03d", "family-src02", "REF-03", None, "DRAFT_REFETCH_TRANSIENT_5XX_BLOCKED_APPROVAL_AFTER_RETRY_CAP",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Production max retry cap for HTTP 5xx transient outages"],
            rationale="Transient HTTP 503 exhausts bounded retry cap and blocks approval, distinct from 404, deletion, and falsity.",
        ),
        _ref_oracle(
            "refcase-03e", "family-src02", "REF-03", None, "DRAFT_REFETCH_TRANSIENT_TIMEOUT_BLOCKED_APPROVAL_AFTER_RETRY_CAP",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Production timeout threshold & retry cap"],
            rationale="Transient network timeout exhausts bounded retry cap and blocks approval, distinct from 404, deletion, and falsity.",
        ),
        _ref_oracle(
            "refcase-04a", "family-src_ref07_canary", "REF-04", "REF-07", "DRAFT_REFETCH_STATIC_CANARY_NON_PERSISTENCE_PASS",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=False, canary_neg=True,
            review_items=["확인 필요: Extend canary audit to live DB/logs in M4 product QA (PARTIAL_STATIC_FIXTURE_PREPARED)"],
            rationale="Static canary check confirms 0 leaks in DB/app-log snapshot while catching negative_leaky_payload_fixture.",
        ),
        _ref_oracle(
            "refcase-04b", "family-src_ref07_canary", "REF-04", None, "DRAFT_REFETCH_STATIC_CANARY_JOB_CACHE_EXPORT_CLEAN",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=False, canary_neg=True,
            review_items=["확인 필요: Extend multi-surface canary audit (DB/log/Job/cache/Export) to live M4 runtime (PARTIAL_STATIC_FIXTURE_PREPARED)"],
            rationale="Static canary check verifies 0 leaks across DB/log/Job/cache/Export snapshots while catching negative leaky Job/Export fixture.",
        ),
        _ref_oracle(
            "refcase-05a", "family-src02", "REF-05", "REF-05", "DRAFT_REFETCH_POLICY_ACCESS_DENIED",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Live OAuth scope revocation & tenant boundary codes (PARTIAL_STATIC_FIXTURE_PREPARED)"],
            rationale="HTTP 403 revoked scope blocks access and approval without deleting or falsifying historical facts.",
        ),
        _ref_oracle(
            "refcase-05b", "family-src02", "REF-05", "REF-06", "DRAFT_REFETCH_UNSAFE_URL_BLOCKED_PREFETCH",
            fetch_attempted=False, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Add live 30x redirect & DNS-rebinding SSRF tests in M4 backend QA"],
            rationale="Unsafe URLs (metadata IP, loopback, private CIDR, credentials, non-HTTPS) blocked prior to network fetch.",
        ),
        # NEW REF-06 oracles
        _ref_oracle(
            "refcase-06a", "family-src02", "REF-06", None, "DRAFT_REFETCH_REF06_PRE_COMMIT_CRASH_REPLAY_REPRODUCED",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=False,
            review_items=["확인 필요: Production worker transaction boundary for atomic metadata+checkpoint commit (M4 runtime unverified)"],
            rationale="Crash before commit discards uncommitted metadata & in-memory raw text; replay refetches unchanged source with matching hash, commits metadata+checkpoint atomically, creates 0 duplicates, and never falls back to stored raw text.",
            ref06={"atomic_metadata_checkpoint_required": True, "expected_duplicate_records": 0, "raw_text_fallback_forbidden": True, "expected_replay_reproduced": True},
        ),
        _ref_oracle(
            "refcase-06b", "family-src02", "REF-06", None, "DRAFT_REFETCH_REF06_POST_COMMIT_CRASH_IDEMPOTENT_REPLAY",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=False,
            review_items=["확인 필요: Production idempotency key enforcement across worker retries (M4 runtime unverified)"],
            rationale="Crash after atomic metadata+checkpoint commit loses in-memory raw text; replaying the exact same job reuses idempotency key with 0 duplicate records and reproduces from same-hash refetch without stored raw fallback.",
            ref06={"atomic_metadata_checkpoint_required": True, "expected_duplicate_records": 0, "raw_text_fallback_forbidden": True, "expected_replay_reproduced": True},
        ),
        _ref_oracle(
            "refcase-06c", "family-src02", "REF-06", None, "DRAFT_REFETCH_REF06_MEMORY_LOST_HASH_CHANGED_BLOCKED",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Operator UX & retry guidance when replay detects upstream hash change after worker crash"],
            rationale="After crash and memory loss, replay refetches source whose content_sha256 changed -> explicitly blocked with NO fallback to persisted raw text (neither confirmed deletion nor historical falsity).",
            ref06={"atomic_metadata_checkpoint_required": True, "expected_duplicate_records": 0, "raw_text_fallback_forbidden": True, "expected_replay_reproduced": False},
        ),
        # NEW REF-07 oracles
        _ref_oracle(
            "refcase-07a", "family-src02", "REF-07", None, "DRAFT_REFETCH_REF07_APPROVAL_SAME_JOB_VERIFIED",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=False,
            review_items=["확인 필요: Product Approval API transaction & audit log schema (M4 product E2E unverified)"],
            rationale="Claim approval executes a fresh same-job refetch verifying hash, locator, and permissions, recording `checked_at` in audit metadata without persisting raw text.",
            ref07={"operation_type": "APPROVAL", "expected_operation_allowed": True, "requires_same_job_refetch": True, "expected_checked_at_recorded_in_audit": True, "expected_export_excludes_raw_quotes": None, "requires_approved_exact_claim_version": None, "expected_export_artifact_emitted": None},
        ),
        _ref_oracle(
            "refcase-07b", "family-src02", "REF-07", None, "DRAFT_REFETCH_REF07_EXPORT_SAME_JOB_VERIFIED_NO_RAW_QUOTE",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=False,
            review_items=["확인 필요: Exact Export file-header / API field layout for verification metadata is NOT frozen (`TEST_FIXTURE_ONLY_API_OR_FILE_HEADER_LAYOUT_NOT_FROZEN`); raw source quotes strictly excluded from Export"],
            rationale="New Export/Download takes approved Claim ID + exact version (`claim-02-1` v1) as precondition, executes fresh same-job refetch, records verification metadata (`source_revision_id`, `source_content_sha256`, `locator_id`, `span_sha256`, `checked_at`), emits Export artifact, and excludes raw source quotes.",
            ref07={"operation_type": "EXPORT_DOWNLOAD", "expected_operation_allowed": True, "requires_same_job_refetch": True, "expected_checked_at_recorded_in_audit": True, "expected_export_excludes_raw_quotes": True, "requires_approved_exact_claim_version": True, "expected_export_artifact_emitted": True},
        ),
        _ref_oracle(
            "refcase-07c", "family-src02", "REF-07", None, "DRAFT_REFETCH_REF07_EVIDENCE_VIEW_SAME_JOB_VERIFIED",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=False,
            review_items=["확인 필요: Evidence-viewer ephemeral response caching headers (`Cache-Control: no-store`) in M4 product QA"],
            rationale="Evidence View executes fresh same-job refetch, renders the exact span ephemerally in memory, and records `checked_at` without persisting raw text.",
            ref07={"operation_type": "EVIDENCE_VIEW", "expected_operation_allowed": True, "requires_same_job_refetch": True, "expected_checked_at_recorded_in_audit": True, "expected_export_excludes_raw_quotes": None, "requires_approved_exact_claim_version": None, "expected_export_artifact_emitted": None},
        ),
        _ref_oracle(
            "refcase-07d", "family-src02", "REF-07", None, "DRAFT_REFETCH_REF07_PRIOR_RECORD_ONLY_BLOCKED",
            fetch_attempted=False, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Error code returned when Approval/Export/View lacks same-job refetch"],
            rationale="Prior verification record alone (without a fresh refetch in the current operation) MUST block Approval/Export/Evidence View.",
            ref07={"operation_type": "APPROVAL", "expected_operation_allowed": False, "requires_same_job_refetch": True, "expected_checked_at_recorded_in_audit": False, "expected_export_excludes_raw_quotes": None, "requires_approved_exact_claim_version": None, "expected_export_artifact_emitted": None},
        ),
        _ref_oracle(
            "refcase-07e", "family-src02", "REF-07", None, "DRAFT_REFETCH_REF07_TOCTOU_STATE_CHANGED_BLOCKED",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Production transactional guard / lock detecting permission or validity revocation between refetch and commit/save"],
            rationale="Even after refetch succeeds for approved claim-02-1 v1, if permission or source validity changes before commit/save (TOCTOU), the operation and Export MUST be blocked with no Export artifact emitted.",
            ref07={"operation_type": "EXPORT_DOWNLOAD", "expected_operation_allowed": False, "requires_same_job_refetch": True, "expected_checked_at_recorded_in_audit": True, "expected_export_excludes_raw_quotes": True, "requires_approved_exact_claim_version": True, "expected_export_artifact_emitted": False},
        ),
        _ref_oracle(
            "refcase-07f", "family-src02", "REF-07", None, "DRAFT_REFETCH_REF07_EXPORT_UNAPPROVED_CLAIM_BLOCKED",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=True,
            review_items=["확인 필요: Product error code when Export is requested for an unapproved Claim version despite successful source refetch"],
            rationale="Contrast case: same-job refetch succeeds (HTTP 200, hash/locator match), yet requested `claim-02-1` v1 is unapproved (`claim_version_approved=false`) -> Export MUST be blocked with no Export artifact emitted.",
            ref07={"operation_type": "EXPORT_DOWNLOAD", "expected_operation_allowed": False, "requires_same_job_refetch": True, "expected_checked_at_recorded_in_audit": True, "expected_export_excludes_raw_quotes": True, "requires_approved_exact_claim_version": True, "expected_export_artifact_emitted": False},
        ),
        _ref_oracle(
            "refcase-07g", "family-src02", "REF-07", None, "DRAFT_REFETCH_REF07_EXPORT_VERSION_MISMATCH_BLOCKED",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=True,
            review_items=["확인 필요: Product error code when Export requests Claim v2 while only Claim v1 is approved despite successful source refetch"],
            rationale="Contrast case: same-job refetch succeeds (HTTP 200, hash/locator match), yet requested `claim-02-1` v2 differs from approved version v1 (`requested_version_matches_approved_version=false`) -> Export MUST be blocked with no Export artifact emitted.",
            ref07={"operation_type": "EXPORT_DOWNLOAD", "expected_operation_allowed": False, "requires_same_job_refetch": True, "expected_checked_at_recorded_in_audit": True, "expected_export_excludes_raw_quotes": True, "requires_approved_exact_claim_version": True, "expected_export_artifact_emitted": False},
        ),
        # NEW REF-08 oracles
        _ref_oracle(
            "refcase-08a", "family-src02", "REF-08", None, "DRAFT_REFETCH_REF08_IN_MEMORY_DEDUP_WITHIN_LIMITS_PASS",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=False,
            review_items=["확인 필요: Production numeric limits for requests/bytes/tokens/retries/concurrency (fixture uses TEST_FIXTURE_ONLY thresholds)"],
            rationale="Within request/byte/token/retry/concurrency limits, duplicate ContentUnits inside the same job are deduplicated (2 -> 1) and processed in memory only with zero silent truncation.",
            ref08={"expected_operation_allowed": True, "expected_silent_truncation_forbidden": True, "expected_inter_job_metadata_only": True, "expected_duplicate_units_removed": 1},
        ),
        _ref_oracle(
            "refcase-08b", "family-src02", "REF-08", None, "DRAFT_REFETCH_REF08_SEPARATE_JOB_METADATA_HANDOFF_REFETCH_PASS",
            fetch_attempted=True, unverifiable_now=False, approval_blocked=False,
            review_items=["확인 필요: Inter-job queue message schema containing ID/hash/locator metadata only (M4 runtime unverified)"],
            rationale="Cross-job boundary passes ONLY source_revision_id, content_sha256, and locator metadata (zero raw text payload), and the next Job executes fresh refetch cleanly.",
            ref08={"expected_operation_allowed": True, "expected_silent_truncation_forbidden": True, "expected_inter_job_metadata_only": True, "expected_duplicate_units_removed": 0},
        ),
        _ref_oracle(
            "refcase-08c", "family-src02", "REF-08", None, "DRAFT_REFETCH_REF08_BYTE_TOKEN_LIMIT_BLOCKED_NO_TRUNCATION",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Exact byte/token ceiling error codes and user feedback when limit is exceeded"],
            rationale="Byte and token ceiling breach explicitly blocks/fails the job; MUST NEVER silently truncate required evidence and report success.",
            ref08={"expected_operation_allowed": False, "expected_silent_truncation_forbidden": True, "expected_inter_job_metadata_only": True, "expected_duplicate_units_removed": 0},
        ),
        _ref_oracle(
            "refcase-08d", "family-src02", "REF-08", None, "DRAFT_REFETCH_REF08_REQUEST_RETRY_CONCURRENCY_BLOCKED_NO_TRUNCATION",
            fetch_attempted=True, unverifiable_now=True, approval_blocked=True,
            review_items=["확인 필요: Exact request/retry/concurrency ceiling enforcement in worker runtime"],
            rationale="Request, retry, and concurrency limit breach explicitly blocks/fails the job; MUST NEVER silently drop required evidence and report success.",
            ref08={"expected_operation_allowed": False, "expected_silent_truncation_forbidden": True, "expected_inter_job_metadata_only": True, "expected_duplicate_units_removed": 0},
        ),
    ]

    dataset_artifacts: dict[str, dict[str, Any]] = {
        "sources/source_revisions.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "artifact_role": "SYNTHETIC_SOURCE_INPUT",
            "normalization_policy": NORMALIZATION_POLICY_ID,
            "convention_scope": CONVENTION_SCOPE,
            "review_required_specs": REVIEW_REQUIRED_SPECS,
            "source_revisions": sources,
        },
        "sources/refetch_snapshots.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "artifact_role": "SYNTHETIC_REFETCH_STUB_INPUT",
            "refetch_snapshots": refetch_snapshots,
        },
        "stages/stage1_content_units.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "stage_name": "STAGE_1_CONTENT_UNIT_SEGMENTATION",
            "stage_responsibility": "Deterministically segment normalized Source Revisions into Content Units with programmatically computed locators and hashes, without persisting raw text.",
            "artifact_role": "STAGE_STUB_OUTPUT",
            "content_units": content_units,
        },
        "stages/stage2_fact_extraction.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "stage_name": "STAGE_2_FACT_EXTRACTION_AND_VALIDATION",
            "stage_responsibility": "Strictly separate Stage 2A FactCandidateExtractor (0..N unvalidated candidates per Content Unit with span/version/completion/uncertainty) from Stage 2B FactSupportValidator (distinct run_id verifying exact Revision/Unit/Locator and DIRECT|CONTEXT|CONTRADICTS) and CanonicalFact promotion (official Fact types PROBLEM|ACTION|TECHNOLOGY|RESULT|ROLE|METRIC; plans/negations/corrections preserved accurately and never promoted falsely).",
            "artifact_role": "STAGE_STUB_OUTPUT",
            "extractor_runs": extractor_runs,
            "validator_runs": validator_runs,
            "scenario_candidate_counts": scenario_candidate_counts,
            "scenario_fact_counts": scenario_fact_counts,
            "fact_candidates": fact_candidates,
            "fact_support_validations": fact_support_validations,
            "canonical_facts": canonical_facts,
            "fact_supports": fact_supports,
        },
        "stages/stage3_claim_assembly.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "stage_name": "STAGE_3_CLAIM_GENERATION_ATOMIZATION_VALIDATION",
            "stage_responsibility": "Strictly separate Stage 3A ClaimGenerator (ConfirmedEvent + SUPPORTED Facts -> DRAFT + input_fact_ids), Stage 3B ClaimAtomizer (Claim version -> independent Atoms), and Stage 3C ClaimSupportValidator (distinct run_id validating Atoms against CanonicalFacts only, never Claim drafts or AI explanations), enforcing new-version re-verification after edits, CLAIM-05/06/08 rules, and OD-003 deletion lifecycle.",
            "artifact_role": "STAGE_STUB_OUTPUT",
            "confirmed_events": confirmed_events,
            "role_execution_runs": role_execution_runs,
            "claims": claims,
            "claim_atoms": claim_atoms,
            "claim_atom_supports": claim_atom_supports,
        },
        "stages/stage4_refetch_checks.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "stage_name": "STAGE_4_REFETCH_VERIFICATION",
            "stage_responsibility": "Evaluate synthetic refetch & Option C boundary stubs mapped across official REF-01..REF-08 (exact span reproduction, hash/locator mismatch, unavailable vs deletion/falsity, static multi-surface canary non-persistence, permission/URL boundary, crash/replay without raw fallback, Approval/Export/Evidence-View same-job refetch, and limits/dedup/inter-job metadata-only handoff), explicitly keeping product_runtime_verified=false.",
            "artifact_role": "STAGE_STUB_OUTPUT",
            "refetch_checks": refetch_checks,
        },
        "expected/oracle_draft.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "artifact_role": "EXPECTED_ORACLE_OWNER_REVIEW_CANDIDATE",
            "oracle_status": ORACLE_STATUS_PENDING,
            "candidate_scope": CANDIDATE_SCOPE,
            "domain_vs_fixture_label_policy": "Official domain fields use spec enums only (Fact types: PROBLEM|ACTION|TECHNOLOGY|RESULT|ROLE|METRIC; Atom statuses: SUPPORTED|USER_CONFIRMED|UNSUPPORTED|STALE; computed SOURCE_VERIFIED). All DRAFT_* and legacy test taxonomy labels live inside fixture_test_classification (TEST_FIXTURE_ONLY_NOT_DOMAIN_API) and MUST NOT be read as API/DB domain enums.",
            "frozen_for_golden": False,
            "human_approved": False,
            "requires_human_review_task": "T-004B",
            "provenance_and_freeze_preconditions": {
                "supersedes_baseline_commit_sha": SUPERSEDES_BASELINE_COMMIT_SHA,
                "supersedes_baseline_version": SUPERSEDES_BASELINE_VERSION,
                "spec_sync_basis": "Orchestrator-transmitted B-002 / Grounded Claim Pipeline / DB Model / Option C / T-004A / T-004B / REF-06..08 / CLAIM-05,06,08 / ADR-018 criteria (direct Notion read unavailable; unconfirmed API/DB details explicitly marked REVIEW_REQUIRED).",
                "human_approval_status": "PENDING_OWNER_REVIEW_NOT_APPROVED",
                "human_approved": False,
                "adr_018_ai_cannot_self_approve": True,
                "family_split_anti_leakage_rule": "All cases, edited versions, and refetch fixtures sharing an evaluation_family_id or source revision remain locked inside the same partition (SYNTHETIC_REGRESSION_DEV or SYNTHETIC_REGRESSION_TEST), preserving all v0.3.0 DEV/TEST boundaries.",
                "live_llm_evaluation_in_scope": False,
                "production_connector_integration_in_scope": False,
                "synthetic_semantics_only_not_connector_evidence": True,
            },
            "scenarios": expected_scenarios,
            "expected_facts": expected_facts,
            "expected_atoms": [
                {
                    "claim_atom_id": aid,
                    "scenario_id": sid,
                    "claim_id": cid,
                    "expected_domain_atom_status": dom_status,
                    **({"od003_purged_after_cleanup": True} if od003_purged else {}),
                    "fixture_test_classification": {
                        "label_scope": "TEST_FIXTURE_ONLY_NOT_DOMAIN_API",
                        "draft_atom_verdict": draft_verdict,
                    },
                    "oracle_status": ORACLE_STATUS_PENDING,
                    "review_required": True,
                    "rationale": rat,
                }
                for aid, sid, cid, dom_status, draft_verdict, od003_purged, rat in expected_atoms_specs
            ],
            "expected_claims": [
                {
                    "claim_id": cid,
                    "scenario_id": sid,
                    "notion_test_ids": ntids,
                    "expected_source_verified_computed": sv_comp,
                    "same_job_refetch_required_for_source_verified": True,
                    "expected_domain_claim_status_note": (
                        "COMPUTED_SOURCE_VERIFIED_WHEN_SAME_JOB_REFETCH_PASSES"
                        if sv_comp
                        else "NOT_SOURCE_VERIFIED_NON_VERIFIED_PRODUCT_ENUM_REVIEW_REQUIRED"
                    ),
                    "fixture_test_classification": {
                        "label_scope": "TEST_FIXTURE_ONLY_NOT_DOMAIN_API",
                        "draft_claim_verdict": draft_verdict,
                        "product_status_proposal": proposal,
                    },
                    **(
                        {"od003_expected_post_cleanup": od003_expected_by_claim[cid]}
                        if cid in od003_expected_by_claim
                        else {}
                    ),
                    "oracle_status": ORACLE_STATUS_PENDING,
                    "review_required": True,
                    "rationale": rat,
                }
                for cid, sid, ntids, sv_comp, draft_verdict, proposal, rat in expected_claims_specs
            ],
        },
        "expected/refetch_oracle_draft.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "artifact_role": "EXPECTED_REFETCH_ORACLE_OWNER_REVIEW_CANDIDATE",
            "oracle_status": ORACLE_STATUS_PENDING,
            "candidate_scope": CANDIDATE_SCOPE,
            "domain_vs_fixture_label_policy": "DRAFT_REFETCH_* verdicts live inside fixture_test_classification (TEST_FIXTURE_ONLY_NOT_DOMAIN_API). Synthetic case preparation across REF-01..REF-08 is strictly distinguished from unexecuted product runtime/E2E verification (product_runtime_verified=false for all 8).",
            "frozen_for_golden": False,
            "human_approved": False,
            "requires_human_review_task": "T-004B",
            "official_ref_matrix": official_ref_matrix,
            "expected_refetch_fixtures": expected_refetch_fixtures,
        },
    }

    # Build family-preserving partition index for CANDIDATE_MANIFEST.json
    partitions_summary: dict[str, dict[str, list[str]]] = {
        "SYNTHETIC_REGRESSION_DEV": {"family_ids": [], "scenario_ids": [], "claim_ids": [], "refetch_fixture_case_ids": []},
        "SYNTHETIC_REGRESSION_TEST": {"family_ids": [], "scenario_ids": [], "claim_ids": [], "refetch_fixture_case_ids": []},
    }
    scenario_partition_by_id = {scn["scenario_id"]: scn["evaluation_partition"] for scn in expected_scenarios}
    for fam_id, part_name in sorted(FAMILY_PARTITION_MAP.items()):
        partitions_summary[part_name]["family_ids"].append(fam_id)
    for scn in expected_scenarios:
        partitions_summary[scn["evaluation_partition"]]["scenario_ids"].append(scn["scenario_id"])
    for cl in claims:
        partitions_summary[scenario_partition_by_id[cl["scenario_id"]]]["claim_ids"].append(cl["claim_id"])
    for rf in expected_refetch_fixtures:
        partitions_summary[rf["evaluation_partition"]]["refetch_fixture_case_ids"].append(rf["fixture_case_id"])

    base_dir = Path(__file__).resolve().parent.parent
    schema_path = base_dir / "schemas" / "claim_pipeline.schema.json"
    artifact_sha256: dict[str, str] = {}
    if schema_path.exists():
        artifact_sha256["schemas/claim_pipeline.schema.json"] = (
            "sha256:" + hashlib.sha256(schema_path.read_bytes()).hexdigest()
        )
    for rel_path in sorted(dataset_artifacts.keys()):
        serialized = (json.dumps(dataset_artifacts[rel_path], indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        artifact_sha256[rel_path] = "sha256:" + hashlib.sha256(serialized).hexdigest()

    manifest_lines = "\n".join(f"{path}  {digest}" for path, digest in sorted(artifact_sha256.items())) + "\n"
    bundle_sha256 = sha256_prefixed(manifest_lines)
    if bundle_sha256 == PRIOR_BASELINE_BUNDLE_SHA256:
        raise RuntimeError("Candidate bundle_sha256 unexpectedly matched prior baseline hash!")

    candidate_manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "artifact_role": "OWNER_REVIEW_CANDIDATE_MANIFEST",
        "candidate_status": ORACLE_STATUS_PENDING,
        "candidate_scope": CANDIDATE_SCOPE,
        "human_approved": False,
        "human_approval_recorded_by": None,
        "adr_018_ai_cannot_self_approve": True,
        "frozen_for_golden": False,
        "supersedes_baseline_commit_sha": SUPERSEDES_BASELINE_COMMIT_SHA,
        "supersedes_baseline_version": SUPERSEDES_BASELINE_VERSION,
        "scope_statement": {
            "included_in_candidate": [
                "Stage 2A FactCandidateExtractor vs Stage 2B FactSupportValidator vs CanonicalFact promotion separation (34 candidates including 3 rejected extractor overclaims -> 31 promoted facts & 35 FactSupports covering all 6 official Fact types)",
                "Stage 3A ClaimGenerator vs Stage 3B ClaimAtomizer vs Stage 3C ClaimSupportValidator separation with globally distinct per-role run_ids, real fixture version/hash input_version_refs, same-Event Fact scoping, and end-to-end verified_evidence_bindings (16 confirmed events, 29 claims, 50 atoms covering SUPPORTED|USER_CONFIRMED|UNSUPPORTED|STALE)",
                "Dedicated CLAIM-05 (SCN-15 role pass/fail anchored to person+event+role+active contribution span fs-15-1b per DB Model §3.8 fixture semantics), CLAIM-06 (SCN-16 metric pass/fail), and CLAIM-08 (SCN-12 4-case edit/reverify/USER_CONFIRMED matrix) pairs",
                "Synthetic cases prepared across official REF-01..REF-08 (27 fixtures: REF-01..03 stubs, REF-04 multi-surface static canary, REF-05 static 403/URL precheck, REF-06 crash/replay, REF-07 Approval/Export with approved Claim ID+version preconditions & unapproved/version-mismatch contrast blocks/Evidence-View refetch, REF-08 limits/dedup/inter-job metadata handoff)",
                "Family-preserving DEV/TEST evaluation partitioning preserving all v0.3.0 boundaries + programmatic locator/hash round-trip integrity",
            ],
            "excluded_not_executed": [
                "Human owner T-004B semantic approval / golden freeze (ADR-018: pending human review, human_approved=false)",
                "Real external LLM inference quality, prompt tuning, or token/cost benchmarking",
                "Product Claim pipeline implementation, live external connectors, live HTTP refetch/retry, live OAuth/SSRF redirect/DNS-rebinding, live DB/log/queue/cache/Export audit, live worker crash/replay, or M4 product E2E",
                "RUNTIME Spike results are reference-only and do NOT substitute for M4 product tests",
                "Unconfirmed production normalization/offset rules, exact REF-08 numeric limits, REF-07 Export checked_at header policy, and non-SUPPORTED/non-SOURCE_VERIFIED product enums (marked REVIEW_REQUIRED)",
            ],
            "live_llm_evaluation_in_scope": False,
            "production_connector_integration_in_scope": False,
            "product_e2e_verified": False,
            "synthetic_semantics_only_not_connector_evidence": True,
        },
        "ref_verification_status": {
            "synthetic_stubs_prepared": ["REF-01", "REF-02", "REF-03"],
            "partial_static_fixtures_prepared": ["REF-04", "REF-05"],
            "synthetic_cases_prepared_runtime_unverified": ["REF-06", "REF-07", "REF-08"],
            "product_runtime_unverified_all": ["REF-01", "REF-02", "REF-03", "REF-04", "REF-05", "REF-06", "REF-07", "REF-08"],
        },
        "evaluation_partitions": partitions_summary,
        "artifact_sha256": dict(sorted(artifact_sha256.items())),
        "bundle_sha256": bundle_sha256,
    }

    return {
        **dataset_artifacts,
        "CANDIDATE_MANIFEST.json": candidate_manifest,
    }


def write_all_artifacts(base_dir: Path) -> list[Path]:
    artifacts = build_all_artifacts()
    written: list[Path] = []
    for rel_path, payload in artifacts.items():
        target = base_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(target)
    return written


def main() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    written = write_all_artifacts(base_dir)
    print(f"Generated {len(written)} T-004A/B owner-review candidate files (v{DATASET_VERSION}) under {base_dir}:")
    for p in written:
        print(f"  - {p.relative_to(base_dir)}")


if __name__ == "__main__":
    main()

"""Programmatically build all T-004A synthetic dataset fixtures (v0.3.0-draft).

Run from repo/worktree root:
    python3 evaluation/claim-draft/tools/build_fixtures.py

Design guarantees:
- Preserves all 18 synthetic Source Revisions and 12 kept Claims (including SCN-13 OD-003).
- Applies all 11 independent-review Claim revisions (atom granularity, effect-with-condition on claim-02-1,
  fact-vs-personal-attribution split on claim-05-2, scope/negation/team/plan splits, and non-overreaching tags).
- Aligns refetch cases with official REF-01..REF-08, adding REF-02 same-hash/locator-only mismatch (refcase-02c)
  and REF-03 transient failures 429/503/timeout with retry cap & approval block (refcase-03c..03e),
  while honestly recording unverified requirements (REF-06, REF-07, REF-08) and runtime gaps (REF-04, REF-05)
  as COVERAGE_GAP / PARTIAL_COVERAGE_GAP.
- Strictly separates provisional draft verdicts (DRAFT_*) from product state enums and preserves DRAFT status.
- Retains TEST_FIXTURE_ONLY & REVIEW_REQUIRED markers on unconfirmed normalization/offset rules.
"""

from __future__ import annotations

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
)

SCHEMA_VERSION = "0.3.0-draft"
DATASET_VERSION = "0.3.0-draft"

CANARY_TOKEN_REF04 = "CM_SYNTHETIC_CANARY_RAW_TEXT_7F9A2C_DO_NOT_PERSIST"

REVIEW_REQUIRED_SPECS = [
    "REVIEW_REQUIRED: Production text normalization policy (CRLF/NFC/whitespace stripping) — fixture uses TEST_FIXTURE_ONLY fixture-nfc-lf-v0-draft",
    "REVIEW_REQUIRED: Production offset unit standard (Unicode codepoint vs UTF-8 byte vs UTF-16 code unit) — fixture uses TEST_FIXTURE_ONLY fixture-locator-v0-draft",
    "REVIEW_REQUIRED: Production canonical entity ID formats and hashing algorithm/salt policy",
    "REVIEW_REQUIRED: Final product Claim/Atom/Refetch status enum mapping (provisional DRAFT_* verdicts are NOT product states)",
    "REVIEW_REQUIRED: User-edit re-verification transition policy for minor paraphrases after v2 re-verification passes (SCN-12 claim-12-2)",
    "REVIEW_REQUIRED: Option C zero-raw-text runtime persistence boundary across DB, queues, logs, caches, and traces (REF-04/REF-06/REF-08)",
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
    """Build all 8 JSON artifacts deterministically in memory."""
    sources: list[dict[str, Any]] = []
    norm_by_key: dict[str, str] = {}
    rev_id_by_key: dict[str, str] = {}

    def register_source(key: str, **kwargs: Any) -> str:
        rec, norm = _make_source(source_key=key, **kwargs)
        sources.append(rec)
        norm_by_key[key] = norm
        rev_id_by_key[key] = rec["source_revision_id"]
        return rec["source_revision_id"]

    # --- Synthetic Source Revisions (all 18 synthetic texts preserved verbatim from b816d01) ---
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
        scenario_ids=["SCN-02", "REF-01", "REF-02", "REF-03", "REF-05"],
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

    # --- Stage 1: Content Units (all 25 preserved) ---
    content_units: list[dict[str, Any]] = []

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
        content_units.append(
            {
                "content_unit_id": cu_id,
                "scenario_id": scenario_id,
                "source_revision_id": rev_id_by_key[src_key],
                "unit_index": unit_index,
                "unit_type": unit_type,
                "locator": loc,
                "unit_sha256": loc["span_sha256"],
                "persists_raw_text": False,
            }
        )

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
        "cu-ref07-0",
        "REF-04",
        "src_ref07_canary",
        0,
        "PARAGRAPH",
        "I added automated weekly partition pruning for synthetic audit log tables.",
    )

    # --- Stage 2: Canonical Facts & Fact Supports (all 25 facts & 28 supports preserved) ---
    canonical_facts: list[dict[str, Any]] = []
    fact_supports: list[dict[str, Any]] = []

    def add_fact(
        fact_id: str,
        scenario_id: str,
        fact_text: str,
        fact_category: str,
        modality: str,
        environment_scope: str,
        attribution_scope: str,
        identity_status: str = "RESOLVED",
        deleted_by_user: bool = False,
    ) -> None:
        canonical_facts.append(
            {
                "canonical_fact_id": fact_id,
                "scenario_id": scenario_id,
                "fact_text": fact_text,
                "fact_category": fact_category,
                "modality": modality,
                "environment_scope": environment_scope,
                "attribution_scope": attribution_scope,
                "identity_status": identity_status,
                "deleted_by_user": deleted_by_user,
            }
        )

    def add_fact_support(
        fs_id: str,
        scenario_id: str,
        fact_id: str,
        src_key: str,
        cu_id: str,
        support_role: str,
        span_substring: str,
    ) -> None:
        loc = _make_locator(fs_id, norm_by_key[src_key], span_substring)
        fact_supports.append(
            {
                "fact_support_id": fs_id,
                "scenario_id": scenario_id,
                "canonical_fact_id": fact_id,
                "source_revision_id": rev_id_by_key[src_key],
                "content_unit_id": cu_id,
                "support_role": support_role,
                "locator": loc,
                "persists_raw_text": False,
            }
        )

    # SCN-01: 0 canonical facts
    # SCN-02: 1 canonical fact (PR summary does not explicitly state production -> UNSPECIFIED)
    add_fact(
        "fact-02-1",
        "SCN-02",
        "Implemented an idempotent retry queue with exponential backoff for billing webhooks in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "UNSPECIFIED",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-02-1",
        "SCN-02",
        "fact-02-1",
        "src02",
        "cu-02-0",
        "DIRECT",
        "I implemented an idempotent retry queue with exponential backoff for billing webhooks in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts.",
    )

    # SCN-03: 3 canonical facts (bullet 1 explicitly says 운영 클러스터; bullets 2-3 do not explicitly specify environment)
    add_fact(
        "fact-03-1",
        "SCN-03",
        "검색 인덱스 샤드 재분배 스크립트를 작성하여 운영 클러스터 p95 검색 지연을 620ms에서 180ms로 단축했다.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-03-1",
        "SCN-03",
        "fact-03-1",
        "src03",
        "cu-03-0",
        "DIRECT",
        "검색 인덱스 샤드 재분배 스크립트를 작성하여 운영 클러스터의 p95 검색 지연 시간을 620ms에서 180ms로 단축했다.",
    )
    add_fact(
        "fact-03-2",
        "SCN-03",
        "배포 실패 시 2분 이내 이전 스냅샷 복구용 자동 롤백 스크립트를 추가했다.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "UNSPECIFIED",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-03-2",
        "SCN-03",
        "fact-03-2",
        "src03",
        "cu-03-1",
        "DIRECT",
        "배포 실패 시 2분 이내로 이전 스냅샷으로 복구하는 자동 롤백 스크립트를 추가했다.",
    )
    add_fact(
        "fact-03-3",
        "SCN-03",
        "슬로우 쿼리 300ms 초과 시 경보를 발송하는 알림 규칙을 새로 구성했다.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "UNSPECIFIED",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-03-3",
        "SCN-03",
        "fact-03-3",
        "src03",
        "cu-03-2",
        "DIRECT",
        "슬로우 쿼리 임계값 300ms 초과 시 경보를 발송하는 알림 규칙을 새로 구성했다.",
    )

    # SCN-04: DIRECT + CONTEXT vs CONTEXT-only
    add_fact(
        "fact-04-1",
        "SCN-04",
        "Migrated the nightly billing job queue from legacy cron scripts to WorkerPool v2 in production.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-04-1",
        "SCN-04",
        "fact-04-1",
        "src04",
        "cu-04-1",
        "DIRECT",
        "I migrated the nightly billing job queue from legacy cron scripts to WorkerPool v2 in production.",
    )
    add_fact_support(
        "fs-04-2",
        "SCN-04",
        "fact-04-1",
        "src04",
        "cu-04-0",
        "CONTEXT",
        "The Q3 reliability initiative aimed to eliminate missed nightly batch runs across billing pipelines.",
    )
    add_fact(
        "fact-04-2",
        "SCN-04",
        "The Q3 reliability initiative had a stated goal to eliminate missed nightly batch runs across billing pipelines.",
        "PROJECT_CONTEXT",
        "PLANNED",
        "UNSPECIFIED",
        "TEAM",
    )
    add_fact_support(
        "fs-04-3",
        "SCN-04",
        "fact-04-2",
        "src04",
        "cu-04-0",
        "DIRECT",
        "The Q3 reliability initiative aimed to eliminate missed nightly batch runs across billing pipelines.",
    )

    # SCN-05: CONTRADICTS across revisions (distinct draft vs correction scopes; passive log audit -> UNSPECIFIED attribution)
    add_fact(
        "fact-05-1",
        "SCN-05",
        "Initial incident #77 draft (2026-06-01) asserted zero checkout requests were dropped during failover (later refuted by 2026-06-03 log audit addendum; executor unspecified).",
        "INCIDENT_RECORD",
        "COMPLETED",
        "UNSPECIFIED",
        "UNSPECIFIED",
    )
    add_fact_support(
        "fs-05-1",
        "SCN-05",
        "fact-05-1",
        "src05a",
        "cu-05-0",
        "DIRECT",
        "During the database primary failover, zero checkout requests were dropped.",
    )
    add_fact_support(
        "fs-05-2",
        "SCN-05",
        "fact-05-1",
        "src05b",
        "cu-05-1",
        "CONTRADICTS",
        "412 checkout requests returned HTTP 503 during the 90-second failover window; the earlier zero-drop note was inaccurate.",
    )
    add_fact(
        "fact-05-2",
        "SCN-05",
        "Postmortem addendum (2026-06-03) records that log audit confirmed 412 checkout requests returned HTTP 503 during the 90-second failover window (individual auditor unspecified).",
        "CORRECTION_NOTICE",
        "COMPLETED",
        "UNSPECIFIED",
        "UNSPECIFIED",
    )
    add_fact_support(
        "fs-05-3",
        "SCN-05",
        "fact-05-2",
        "src05b",
        "cu-05-1",
        "DIRECT",
        "Log audit confirmed that 412 checkout requests returned HTTP 503 during the 90-second failover window",
    )

    # SCN-06: Planned vs Completed
    add_fact(
        "fact-06-1",
        "SCN-06",
        "읽기 전용 복제본 헬스체크 엔드포인트를 구현하여 운영 모니터링에 연동했다.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-06-1",
        "SCN-06",
        "fact-06-1",
        "src06",
        "cu-06-0",
        "DIRECT",
        "읽기 전용 복제본 헬스체크 엔드포인트를 구현하여 운영 모니터링에 연동했다.",
    )
    add_fact(
        "fact-06-2",
        "SCN-06",
        "RFC 작성 시점 기준으로 Q4에 user_events 테이블 샤딩 도입 및 다음 달 프로토타입 벤치마크 진행을 계획하고 있다(관찰 시점 기준 계획 항목; 이후 완료 여부 미확인).",
        "PLAN_OR_PROPOSAL",
        "PLANNED",
        "UNSPECIFIED",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-06-2",
        "SCN-06",
        "fact-06-2",
        "src06",
        "cu-06-1",
        "DIRECT",
        "Q4에 user_events 테이블 샤딩을 도입할 예정이며, 다음 달 프로토타입 벤치마크를 진행할 계획이다.",
    )

    # SCN-07: Negation context (production deployment & 30% cost savings independently negated)
    add_fact(
        "fact-07-1",
        "SCN-07",
        "Reverted the canary autoscaler Terraform config before rollout because cold-start spikes persisted.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "STAGING_TEST",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-07-1",
        "SCN-07",
        "fact-07-1",
        "src07",
        "cu-07-0",
        "DIRECT",
        "I reverted the Terraform config before rollout because cold-start spikes persisted",
    )
    add_fact(
        "fact-07-2",
        "SCN-07",
        "Did not deploy the canary autoscaler to production.",
        "NEGATIVE_OUTCOME",
        "NEGATED",
        "PRODUCTION",
        "TEAM",
    )
    add_fact_support(
        "fs-07-2",
        "SCN-07",
        "fact-07-2",
        "src07",
        "cu-07-0",
        "DIRECT",
        "We did not deploy the canary autoscaler to production",
    )
    add_fact(
        "fact-07-3",
        "SCN-07",
        "Did not achieve the target 30% compute cost savings.",
        "NEGATIVE_OUTCOME",
        "NEGATED",
        "UNSPECIFIED",
        "TEAM",
    )
    add_fact_support(
        "fs-07-3",
        "SCN-07",
        "fact-07-3",
        "src07",
        "cu-07-0",
        "DIRECT",
        "we did not achieve the target 30% compute cost savings.",
    )

    # SCN-08: Team vs Individual (digest does not explicitly mention production -> UNSPECIFIED)
    add_fact(
        "fact-08-1",
        "SCN-08",
        "Platform Reliability team (6 engineers) rebuilt the monorepo CI pipeline, reducing median build duration from 22 minutes to 8 minutes.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "UNSPECIFIED",
        "TEAM",
    )
    add_fact_support(
        "fs-08-1",
        "SCN-08",
        "fact-08-1",
        "src08",
        "cu-08-0",
        "DIRECT",
        "The Platform Reliability team (6 engineers) rebuilt the monorepo CI pipeline, reducing median build duration from 22 minutes to 8 minutes.",
    )
    add_fact(
        "fact-08-2",
        "SCN-08",
        "Alex Kim implemented the distributed build artifact cache layer within the CI pipeline rebuild effort.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "UNSPECIFIED",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-08-2",
        "SCN-08",
        "fact-08-2",
        "src08",
        "cu-08-1",
        "DIRECT",
        "Alex Kim implemented the distributed build artifact cache layer.",
    )
    add_fact_support(
        "fs-08-3",
        "SCN-08",
        "fact-08-2",
        "src08",
        "cu-08-0",
        "CONTEXT",
        "The Platform Reliability team (6 engineers) rebuilt the monorepo CI pipeline",
    )

    # SCN-09: Staging/Test vs Production
    add_fact(
        "fact-09-1",
        "SCN-09",
        "로컬 스테이징 샌드박스(합성 데이터 1만 건)에서 복합 인덱스 적용 후 조회 p95가 840ms에서 95ms로 감소했다.",
        "BENCHMARK_RESULT",
        "COMPLETED",
        "STAGING_TEST",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-09-1",
        "SCN-09",
        "fact-09-1",
        "src09",
        "cu-09-0",
        "DIRECT",
        "로컬 스테이징 샌드박스(합성 데이터 1만 건)에서 복합 인덱스 적용 후 조회 p95가 840ms에서 95ms로 감소했다.",
    )
    add_fact(
        "fact-09-2",
        "SCN-09",
        "복합 인덱스 변경사항의 운영 환경 배포는 아직 시작하지 않았다.",
        "NEGATIVE_OUTCOME",
        "NEGATED",
        "PRODUCTION",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-09-2",
        "SCN-09",
        "fact-09-2",
        "src09",
        "cu-09-0",
        "DIRECT",
        "운영 환경 배포는 아직 시작하지 않았다.",
    )

    # SCN-10: Unverified causation & fabricated metric
    add_fact(
        "fact-10-1",
        "SCN-10",
        "Shipped the redesigned onboarding checklist UI on May 12.",
        "LAUNCH_EVENT",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-10-1",
        "SCN-10",
        "fact-10-1",
        "src10",
        "cu-10-0",
        "DIRECT",
        "I shipped the redesigned onboarding checklist UI on May 12.",
    )
    add_fact(
        "fact-10-2",
        "SCN-10",
        "Monthly revenue rose 18% in May during a simultaneous annual discount campaign with no A/B test or causal attribution study.",
        "PROJECT_CONTEXT",
        "OBSERVED_CORRELATION",
        "PRODUCTION",
        "TEAM",
    )
    add_fact_support(
        "fs-10-2",
        "SCN-10",
        "fact-10-2",
        "src10",
        "cu-10-1",
        "DIRECT",
        "Total monthly revenue rose 18% in May compared to April, though the sales team also ran our largest annual discount campaign during the same two weeks; no A/B test or causal attribution study was conducted.",
    )

    # SCN-11: Identity ambiguity vs resolved owner
    add_fact(
        "fact-11-1",
        "SCN-11",
        "Unmapped contributor 'Alex' fixed the memory leak in the ingestion worker (ambiguous between Alex Kim and Alex Kang).",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "AMBIGUOUS",
        identity_status="AMBIGUOUS",
    )
    add_fact_support(
        "fs-11-1",
        "SCN-11",
        "fact-11-1",
        "src11a",
        "cu-11-0",
        "DIRECT",
        "Alex fixed the memory leak in the ingestion worker overnight.",
    )
    add_fact(
        "fact-11-2",
        "SCN-11",
        "Alex Kim (verified account) added graceful shutdown unit tests for the ingestion worker.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "STAGING_TEST",
        "INDIVIDUAL",
        identity_status="RESOLVED",
    )
    add_fact_support(
        "fs-11-2",
        "SCN-11",
        "fact-11-2",
        "src11b",
        "cu-11-1",
        "DIRECT",
        "I added graceful shutdown unit tests for the ingestion worker.",
    )

    # SCN-12: User edit -> re-verification on new version (PR #512 does not explicitly state production -> UNSPECIFIED)
    add_fact(
        "fact-12-1",
        "SCN-12",
        "Tuned PostgreSQL connection pool sizing in the checkout service, reducing p99 checkout API latency by 25% under peak load.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "UNSPECIFIED",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-12-1",
        "SCN-12",
        "fact-12-1",
        "src12",
        "cu-12-0",
        "DIRECT",
        "I tuned PostgreSQL connection pool sizing in the checkout service, reducing p99 checkout API latency by 25% under peak load.",
    )

    # SCN-13: User deletion invalidation
    add_fact(
        "fact-13-1",
        "SCN-13",
        "Deprecated the legacy Memcached cluster and migrated session keys to Redis Cluster.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
        deleted_by_user=True,
    )
    add_fact_support(
        "fs-13-1",
        "SCN-13",
        "fact-13-1",
        "src13a",
        "cu-13-0",
        "DIRECT",
        "I deprecated the legacy Memcached cluster and migrated session keys to Redis Cluster.",
    )
    add_fact(
        "fact-13-2",
        "SCN-13",
        "Created a Redis Cluster saturation alert dashboard for on-call engineers.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
        deleted_by_user=False,
    )
    add_fact_support(
        "fs-13-2",
        "SCN-13",
        "fact-13-2",
        "src13b",
        "cu-13-1",
        "DIRECT",
        "I created a Redis Cluster saturation alert dashboard for on-call engineers.",
    )

    # SCN-14: Hard negatives (10x distortion + cross-project conflation)
    add_fact(
        "fact-14-1",
        "SCN-14",
        "In Project Alpha, optimized image thumbnail decoding, reducing processing latency from 900ms to 400ms.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-14-1",
        "SCN-14",
        "fact-14-1",
        "src14",
        "cu-14-0",
        "DIRECT",
        "Project Alpha: I optimized image thumbnail decoding, reducing processing latency from 900ms to 400ms.",
    )
    add_fact(
        "fact-14-2",
        "SCN-14",
        "In Project Borealis, migrated 12 internal documentation pages to the new Markdown linter.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-14-2",
        "SCN-14",
        "fact-14-2",
        "src14",
        "cu-14-1",
        "DIRECT",
        "Project Borealis: I migrated 12 internal documentation pages to the new Markdown linter.",
    )

    scenario_fact_counts: dict[str, int] = {f"SCN-{i:02d}": 0 for i in range(1, 15)}
    for fact in canonical_facts:
        scenario_fact_counts[fact["scenario_id"]] += 1

    # --- Stage 3: Claims, Claim Atoms, Claim Atom Supports (all 23 claims, 30 atoms, 33 supports preserved) ---
    claims: list[dict[str, Any]] = []
    claim_atoms: list[dict[str, Any]] = []
    claim_atom_supports: list[dict[str, Any]] = []

    def add_claim(
        claim_id: str,
        scenario_id: str,
        claim_text: str,
        atom_ids: list[str],
        *,
        claim_version: int = 1,
        previous_claim_version: int | None = None,
        user_edited: bool = False,
        reverification_state: str = "NOT_APPLICABLE_UNEDITED",
        user_edit_history: list[dict[str, Any]] | None = None,
        deletion_lifecycle: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "claim_id": claim_id,
            "scenario_id": scenario_id,
            "claim_version": claim_version,
            "previous_claim_version": previous_claim_version,
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
        atom_kind: str,
        *,
        is_required_atom: bool = True,
        user_edited: bool = False,
        edit_severity: str | None = "NONE",
        is_hard_negative: bool = False,
        hard_negative_type: str | None = None,
    ) -> None:
        claim_atoms.append(
            {
                "claim_atom_id": atom_id,
                "scenario_id": scenario_id,
                "claim_id": claim_id,
                "atom_text": atom_text,
                "atom_kind": atom_kind,
                "is_required_atom": is_required_atom,
                "user_edited": user_edited,
                "edit_severity": edit_severity,
                "is_hard_negative": is_hard_negative,
                "hard_negative_type": hard_negative_type,
            }
        )

    def add_cas(
        cas_id: str,
        scenario_id: str,
        atom_id: str,
        fact_id: str,
        support_role: str,
        *,
        evaluated_on_claim_version: int = 1,
        removed_by_user: bool = False,
    ) -> None:
        claim_atom_supports.append(
            {
                "claim_atom_support_id": cas_id,
                "scenario_id": scenario_id,
                "claim_atom_id": atom_id,
                "canonical_fact_id": fact_id,
                "support_role": support_role,
                "evaluated_on_claim_version": evaluated_on_claim_version,
                "removed_by_user": removed_by_user,
            }
        )

    # SCN-01
    add_claim("claim-01-1", "SCN-01", "결제 API 응답 속도를 40% 개선했다.", ["atom-01-1"])
    add_atom(
        "atom-01-1",
        "SCN-01",
        "claim-01-1",
        "결제 API 응답 속도를 40% 개선함",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="ZERO_FACT_HALLUCINATION",
    )

    # SCN-02 (UPDATED: claim-02-1 verifies both implementation and conditional effect as separate DIRECT atoms)
    add_claim(
        "claim-02-1",
        "SCN-02",
        "Implemented an idempotent retry queue with exponential backoff for Project Aurora billing webhooks, preventing duplicate invoice notifications during transient gateway timeouts.",
        ["atom-02-1", "atom-02-2"],
    )
    add_atom(
        "atom-02-1",
        "SCN-02",
        "claim-02-1",
        "Implemented idempotent retry queue with exponential backoff for billing webhooks in Project Aurora",
        "ACTION",
    )
    add_atom(
        "atom-02-2",
        "SCN-02",
        "claim-02-1",
        "Prevented duplicate invoice notifications during transient gateway timeouts",
        "OUTCOME_METRIC",
    )
    add_cas("cas-02-1", "SCN-02", "atom-02-1", "fact-02-1", "DIRECT")
    add_cas("cas-02-2", "SCN-02", "atom-02-2", "fact-02-1", "DIRECT")

    # SCN-03 (UPDATED: claim-03-1 splits shard script action vs p95 620->180ms result, keeps 2-min rollback condition & >300ms alert)
    add_claim(
        "claim-03-1",
        "SCN-03",
        "프로젝트 오로라에서 검색 인덱스 샤드 재분배 스크립트를 작성해 운영 클러스터의 p95 검색 지연 시간을 620ms에서 180ms로 단축했고, 배포 실패 시 2분 이내 이전 스냅샷으로 복구하는 자동 롤백 스크립트와 슬로우 쿼리 300ms 초과 시 경보를 발송하는 알림 규칙을 구성했다.",
        ["atom-03-1", "atom-03-2", "atom-03-3", "atom-03-4"],
    )
    add_atom(
        "atom-03-1",
        "SCN-03",
        "claim-03-1",
        "검색 인덱스 샤드 재분배 스크립트 작성",
        "ACTION",
    )
    add_atom(
        "atom-03-2",
        "SCN-03",
        "claim-03-1",
        "운영 클러스터의 p95 검색 지연 시간을 620ms에서 180ms로 단축",
        "OUTCOME_METRIC",
    )
    add_atom(
        "atom-03-3",
        "SCN-03",
        "claim-03-1",
        "배포 실패 시 2분 이내로 이전 스냅샷으로 복구하는 자동 롤백 스크립트 추가",
        "ACTION",
    )
    add_atom(
        "atom-03-4",
        "SCN-03",
        "claim-03-1",
        "슬로우 쿼리 임계값 300ms 초과 시 경보를 발송하는 알림 규칙 구성",
        "ACTION",
    )
    add_cas("cas-03-1", "SCN-03", "atom-03-1", "fact-03-1", "DIRECT")
    add_cas("cas-03-2", "SCN-03", "atom-03-2", "fact-03-1", "DIRECT")
    add_cas("cas-03-3", "SCN-03", "atom-03-3", "fact-03-2", "DIRECT")
    add_cas("cas-03-4", "SCN-03", "atom-03-4", "fact-03-3", "DIRECT")

    # SCN-04 (UPDATED: claim-04-1 removes unverified 'as part of Q3 reliability work' qualifier; claim-04-2 KEPT)
    add_claim(
        "claim-04-1",
        "SCN-04",
        "Migrated the nightly billing job queue from legacy cron scripts to WorkerPool v2 in production.",
        ["atom-04-1"],
    )
    add_atom(
        "atom-04-1",
        "SCN-04",
        "claim-04-1",
        "Migrated nightly billing job queue from legacy cron scripts to WorkerPool v2 in production",
        "ACTION",
    )
    add_cas("cas-04-1", "SCN-04", "atom-04-1", "fact-04-1", "DIRECT")
    add_cas("cas-04-2", "SCN-04", "atom-04-1", "fact-04-2", "CONTEXT")

    add_claim(
        "claim-04-2",
        "SCN-04",
        "Eliminated 100% of missed nightly batch runs across all billing pipelines.",
        ["atom-04-2"],
    )
    add_atom(
        "atom-04-2",
        "SCN-04",
        "claim-04-2",
        "Eliminated 100% of missed nightly batch runs across billing pipelines",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="CONTEXT_ONLY_INSUFFICIENT",
    )
    add_cas("cas-04-3", "SCN-04", "atom-04-2", "fact-04-2", "CONTEXT")

    # SCN-05 (UPDATED: claim-05-1 splits executor attribution vs zero-drop contradiction; claim-05-2 splits log-confirmed 412/503 fact vs personal audit/documentation claim)
    add_claim(
        "claim-05-1",
        "SCN-05",
        "Executed database primary failover with zero dropped checkout requests.",
        ["atom-05-1", "atom-05-2"],
    )
    add_atom(
        "atom-05-1",
        "SCN-05",
        "claim-05-1",
        "User personally executed the database primary failover",
        "ATTRIBUTION",
        is_hard_negative=True,
        hard_negative_type="UNSUPPORTED_EXECUTION_ATTRIBUTION",
    )
    add_atom(
        "atom-05-2",
        "SCN-05",
        "claim-05-1",
        "Zero checkout requests were dropped during database primary failover",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="CONTRADICTED_BY_CORRECTION",
    )
    # atom-05-1 intentionally has 0 supports (neither draft src05a nor addendum src05b attributes failover execution to the user)
    add_cas("cas-05-1", "SCN-05", "atom-05-2", "fact-05-1", "DIRECT")
    add_cas("cas-05-2", "SCN-05", "atom-05-2", "fact-05-2", "CONTRADICTS")

    add_claim(
        "claim-05-2",
        "SCN-05",
        "Audited failover logs and documented 412 HTTP 503 checkout responses during the 90-second failover window.",
        ["atom-05-3", "atom-05-4"],
    )
    add_atom(
        "atom-05-3",
        "SCN-05",
        "claim-05-2",
        "Log audit confirmed 412 checkout requests returned HTTP 503 during the 90-second failover window",
        "OUTCOME_METRIC",
    )
    add_atom(
        "atom-05-4",
        "SCN-05",
        "claim-05-2",
        "User personally audited failover logs and documented the 412 HTTP 503 findings",
        "ATTRIBUTION",
        is_hard_negative=True,
        hard_negative_type="UNSUPPORTED_INDIVIDUAL_AUDIT_ATTRIBUTION",
    )
    add_cas("cas-05-3", "SCN-05", "atom-05-3", "fact-05-2", "DIRECT")
    add_cas("cas-05-4", "SCN-05", "atom-05-4", "fact-05-2", "CONTEXT")

    # SCN-06 (UPDATED: claim-06-1 splits endpoint implementation vs monitoring integration; claim-06-2 splits sharding completion vs missing benchmark completion)
    add_claim(
        "claim-06-1",
        "SCN-06",
        "읽기 전용 복제본 헬스체크 엔드포인트를 구현하여 운영 모니터링에 연동했다.",
        ["atom-06-1", "atom-06-2"],
    )
    add_atom(
        "atom-06-1",
        "SCN-06",
        "claim-06-1",
        "읽기 전용 복제본 헬스체크 엔드포인트 구현 완료",
        "ACTION",
    )
    add_atom(
        "atom-06-2",
        "SCN-06",
        "claim-06-1",
        "읽기 전용 복제본 헬스체크 엔드포인트를 운영 모니터링에 연동 완료",
        "ACTION",
    )
    add_cas("cas-06-1", "SCN-06", "atom-06-1", "fact-06-1", "DIRECT")
    add_cas("cas-06-2", "SCN-06", "atom-06-2", "fact-06-1", "DIRECT")

    add_claim(
        "claim-06-2",
        "SCN-06",
        "운영 환경의 user_events 테이블 샤딩을 완료하고 벤치마크를 마쳤다.",
        ["atom-06-3", "atom-06-4"],
    )
    add_atom(
        "atom-06-3",
        "SCN-06",
        "claim-06-2",
        "운영 환경의 user_events 테이블 샤딩 완료",
        "ACTION",
        is_hard_negative=True,
        hard_negative_type="PLANNED_AS_COMPLETED",
    )
    add_atom(
        "atom-06-4",
        "SCN-06",
        "claim-06-2",
        "user_events 테이블 샤딩 프로토타입 벤치마크 완료",
        "ACTION",
        is_hard_negative=True,
        hard_negative_type="PLANNED_AS_COMPLETED",
    )
    add_cas("cas-06-3", "SCN-06", "atom-06-3", "fact-06-2", "CONTRADICTS")
    add_cas("cas-06-4", "SCN-06", "atom-06-4", "fact-06-2", "CONTRADICTS")

    # SCN-07 (claim-07-1 KEPT; UPDATED: claim-07-2 splits negated production deployment vs negated 30% cost savings)
    add_claim(
        "claim-07-1",
        "SCN-07",
        "Reverted the canary autoscaler Terraform config before production rollout after observing persistent cold-start spikes.",
        ["atom-07-1"],
    )
    add_atom(
        "atom-07-1",
        "SCN-07",
        "claim-07-1",
        "Reverted canary autoscaler Terraform config before production rollout due to cold-start spikes",
        "ACTION",
    )
    add_cas("cas-07-1", "SCN-07", "atom-07-1", "fact-07-1", "DIRECT")

    add_claim(
        "claim-07-2",
        "SCN-07",
        "Deployed the canary autoscaler to production and achieved 30% compute cost savings.",
        ["atom-07-2", "atom-07-3"],
    )
    add_atom(
        "atom-07-2",
        "SCN-07",
        "claim-07-2",
        "Deployed the canary autoscaler to production",
        "ACTION",
        is_hard_negative=True,
        hard_negative_type="NEGATION_REVERSAL",
    )
    add_atom(
        "atom-07-3",
        "SCN-07",
        "claim-07-2",
        "Achieved 30% compute cost savings from the canary autoscaler",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="NEGATION_REVERSAL",
    )
    add_cas("cas-07-2", "SCN-07", "atom-07-2", "fact-07-2", "CONTRADICTS")
    add_cas("cas-07-3", "SCN-07", "atom-07-3", "fact-07-3", "CONTRADICTS")

    # SCN-08 (claim-08-1 KEPT; UPDATED: claim-08-2 splits solo-attribution CONTRADICTS vs 'from scratch' UNSUPPORTED vs 22->8m team metric CONTEXT-only)
    add_claim(
        "claim-08-1",
        "SCN-08",
        "Implemented the distributed build artifact cache layer during the Platform Reliability team's CI pipeline rebuild.",
        ["atom-08-1"],
    )
    add_atom(
        "atom-08-1",
        "SCN-08",
        "claim-08-1",
        "Implemented the distributed build artifact cache layer within the CI pipeline rebuild",
        "ATTRIBUTION",
    )
    add_cas("cas-08-1", "SCN-08", "atom-08-1", "fact-08-2", "DIRECT")
    add_cas("cas-08-2", "SCN-08", "atom-08-1", "fact-08-1", "CONTEXT")

    add_claim(
        "claim-08-2",
        "SCN-08",
        "Single-handedly rebuilt the entire monorepo CI pipeline from scratch, reducing median build time from 22m to 8m.",
        ["atom-08-2", "atom-08-3", "atom-08-4"],
    )
    add_atom(
        "atom-08-2",
        "SCN-08",
        "claim-08-2",
        "Single-handedly rebuilt the entire monorepo CI pipeline",
        "ATTRIBUTION",
        is_hard_negative=True,
        hard_negative_type="TEAM_OVERCLAIM_AS_INDIVIDUAL",
    )
    add_atom(
        "atom-08-3",
        "SCN-08",
        "claim-08-2",
        "Rebuilt the monorepo CI pipeline from scratch",
        "ACTION",
        is_hard_negative=True,
        hard_negative_type="UNSUPPORTED_FROM_SCRATCH_CLAIM",
    )
    add_atom(
        "atom-08-4",
        "SCN-08",
        "claim-08-2",
        "Reduced median monorepo build duration from 22m to 8m (team-level outcome attributed to solo rebuild)",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="TEAM_METRIC_CONTEXT_ONLY",
    )
    # 6-engineer team work contradicts single-handed attribution; 'from scratch' has 0 supports; 22->8m is team CONTEXT only
    add_cas("cas-08-3", "SCN-08", "atom-08-2", "fact-08-1", "CONTRADICTS")
    add_cas("cas-08-4", "SCN-08", "atom-08-4", "fact-08-1", "CONTEXT")

    # SCN-09 (claim-09-1 KEPT; UPDATED: claim-09-2 splits unstarted production deployment CONTRADICTS vs production p95 metric CONTEXT-only from staging)
    add_claim(
        "claim-09-1",
        "SCN-09",
        "로컬 스테이징 샌드박스(합성 데이터 1만 건)에서 복합 인덱스로 조회 p95를 840ms에서 95ms로 단축했다.",
        ["atom-09-1"],
    )
    add_atom(
        "atom-09-1",
        "SCN-09",
        "claim-09-1",
        "로컬 스테이징 샌드박스(1만 건)에서 복합 인덱스 적용으로 조회 p95를 840ms에서 95ms로 단축",
        "SCOPE_ENVIRONMENT",
    )
    add_cas("cas-09-1", "SCN-09", "atom-09-1", "fact-09-1", "DIRECT")

    add_claim(
        "claim-09-2",
        "SCN-09",
        "운영 데이터베이스에 복합 인덱스를 배포해 조회 p95를 840ms에서 95ms로 개선했다.",
        ["atom-09-2", "atom-09-3"],
    )
    add_atom(
        "atom-09-2",
        "SCN-09",
        "claim-09-2",
        "운영 데이터베이스에 복합 인덱스를 배포함",
        "SCOPE_ENVIRONMENT",
        is_hard_negative=True,
        hard_negative_type="UNSTARTED_PRODUCTION_ROLLOUT",
    )
    add_atom(
        "atom-09-3",
        "SCN-09",
        "claim-09-2",
        "운영 데이터베이스에서 조회 p95를 840ms에서 95ms로 개선함",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="STAGING_METRIC_CONFLATED_WITH_PRODUCTION",
    )
    add_cas("cas-09-2", "SCN-09", "atom-09-2", "fact-09-2", "CONTRADICTS")
    add_cas("cas-09-3", "SCN-09", "atom-09-3", "fact-09-1", "CONTEXT")

    # SCN-10 (KEPT: 3 required atoms: 1 DIRECT, 1 CONTEXT-only, 1 zero-support)
    add_claim(
        "claim-10-1",
        "SCN-10",
        "Shipped the redesigned onboarding checklist UI on May 12, directly driving 18% monthly revenue growth and a 45% conversion lift.",
        ["atom-10-1", "atom-10-2", "atom-10-3"],
    )
    add_atom(
        "atom-10-1",
        "SCN-10",
        "claim-10-1",
        "Shipped the redesigned onboarding checklist UI on May 12",
        "TIMING",
    )
    add_atom(
        "atom-10-2",
        "SCN-10",
        "claim-10-1",
        "Directly drove 18% monthly revenue growth via the onboarding checklist redesign",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="UNVERIFIED_CAUSATION",
    )
    add_atom(
        "atom-10-3",
        "SCN-10",
        "claim-10-1",
        "Achieved a 45% onboarding funnel conversion lift",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="FABRICATED_METRIC",
    )
    add_cas("cas-10-1", "SCN-10", "atom-10-1", "fact-10-1", "DIRECT")
    add_cas("cas-10-2", "SCN-10", "atom-10-2", "fact-10-2", "CONTEXT")
    # atom-10-3 intentionally has 0 supports

    # SCN-11 (KEPT)
    add_claim(
        "claim-11-1",
        "SCN-11",
        "Fixed the memory leak in the ingestion worker overnight.",
        ["atom-11-1"],
    )
    add_atom(
        "atom-11-1",
        "SCN-11",
        "claim-11-1",
        "Fixed the memory leak in the ingestion worker (attributed to ambiguous 'Alex')",
        "ATTRIBUTION",
        is_hard_negative=True,
        hard_negative_type="IDENTITY_AMBIGUITY",
    )
    add_cas("cas-11-1", "SCN-11", "atom-11-1", "fact-11-1", "DIRECT")

    add_claim(
        "claim-11-2",
        "SCN-11",
        "Added graceful shutdown unit tests for the ingestion worker.",
        ["atom-11-2"],
    )
    add_atom(
        "atom-11-2",
        "SCN-11",
        "claim-11-2",
        "Added graceful shutdown unit tests for the ingestion worker",
        "ACTION",
    )
    add_cas("cas-11-2", "SCN-11", "atom-11-2", "fact-11-2", "DIRECT")

    # SCN-12 (UPDATED: claim-12-1 splits global tuning scope vs global 60% metric without inferring 60% is false in every scope; claim-12-2 KEPT)
    add_claim(
        "claim-12-1",
        "SCN-12",
        "Tuned PostgreSQL connection pool sizing globally, cutting p99 API latency by 60%.",
        ["atom-12-1", "atom-12-3"],
        claim_version=2,
        previous_claim_version=1,
        user_edited=True,
        reverification_state="REVERIFIED_FAILED_ON_NEW_VERSION",
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
    add_atom(
        "atom-12-1",
        "SCN-12",
        "claim-12-1",
        "Tuned PostgreSQL connection pool sizing globally across all services",
        "SCOPE_ENVIRONMENT",
        user_edited=True,
        edit_severity="SUBSTANTIVE",
        is_hard_negative=True,
        hard_negative_type="USER_EDIT_SCOPE_OVERGENERALIZATION",
    )
    add_atom(
        "atom-12-3",
        "SCN-12",
        "claim-12-1",
        "Cut global p99 API latency by 60%",
        "OUTCOME_METRIC",
        user_edited=True,
        edit_severity="SUBSTANTIVE",
        is_hard_negative=True,
        hard_negative_type="USER_EDIT_METRIC_SCOPE_MISMATCH",
    )
    # Re-evaluated on v2: checkout-only peak-load 25% provides CONTEXT only (different measurement scope; does not prove 60% false everywhere, but fails v2 re-verification)
    add_cas(
        "cas-12-1",
        "SCN-12",
        "atom-12-1",
        "fact-12-1",
        "CONTEXT",
        evaluated_on_claim_version=2,
    )
    add_cas(
        "cas-12-3",
        "SCN-12",
        "atom-12-3",
        "fact-12-1",
        "CONTEXT",
        evaluated_on_claim_version=2,
    )

    add_claim(
        "claim-12-2",
        "SCN-12",
        "Optimized Postgres pool configuration in the checkout service, lowering peak p99 API latency by 25%.",
        ["atom-12-2"],
        claim_version=2,
        previous_claim_version=1,
        user_edited=True,
        reverification_state="REVERIFY_REQUIRED_ON_NEW_VERSION",
        user_edit_history=[
            {
                "event_id": "evt-edit-12-2",
                "from_version": 1,
                "to_version": 2,
                "action": "EDIT_CLAIM_TEXT",
                "severity": "MINOR_PARAPHRASE",
                "note": "User paraphrased claim into v2; prior v1 support cannot auto-carryover SOURCE_VERIFIED until v2 is re-verified against fact-12-1",
            }
        ],
    )
    add_atom(
        "atom-12-2",
        "SCN-12",
        "claim-12-2",
        "Optimized Postgres pool configuration in checkout service, lowering peak p99 API latency by 25%",
        "ACTION",
        user_edited=True,
        edit_severity="MINOR_PARAPHRASE",
    )
    # Prior support was only evaluated on v1 (stale relative to claim_version=2 -> must re-verify on v2!)
    add_cas(
        "cas-12-2",
        "SCN-12",
        "atom-12-2",
        "fact-12-1",
        "DIRECT",
        evaluated_on_claim_version=1,
    )

    # SCN-13 (OD-003: Deletion preview + explicit confirmation -> immediate access block -> full purge of dependent Claim & server edited/approved/versioned copies even when mixed with other sources; independent source src13b preserved)
    add_claim(
        "claim-13-1",
        "SCN-13",
        "Deprecated the legacy Memcached cluster and migrated session keys to Redis Cluster.",
        ["atom-13-1"],
        claim_version=1,
        previous_claim_version=None,
        user_edited=False,
        reverification_state="NOT_APPLICABLE_UNEDITED",
        user_edit_history=[
            {
                "event_id": "evt-del-13-1a",
                "from_version": 1,
                "to_version": 1,
                "action": "DELETE_SOURCE_PREVIEW_CONFIRMED",
                "severity": "CASCADE_DELETE",
                "note": "OD-003: Deletion preview shown for src13a listing dependent claim-13-1; user explicitly confirmed deletion.",
            },
            {
                "event_id": "evt-del-13-1b",
                "from_version": 1,
                "to_version": 1,
                "action": "BLOCK_DEPENDENT_CLAIM_ACCESS",
                "severity": "CASCADE_DELETE",
                "note": "OD-003: Dependent claim-13-1 immediately blocked from read/export/approval access upon confirmation.",
            },
            {
                "event_id": "evt-del-13-1c",
                "from_version": 1,
                "to_version": 1,
                "action": "PURGE_DEPENDENT_CLAIM_AND_SERVER_COPIES",
                "severity": "CASCADE_DELETE",
                "note": "OD-003: Cleanup completed; claim-13-1 and its server approved copy & v1 record purged (0 retained).",
            },
        ],
        deletion_lifecycle={
            "policy_ref": "OD-003",
            "trigger_source_revision_id": rev_id_by_key["src13a"],
            "deletion_preview_shown": True,
            "explicit_user_confirmation": True,
            "immediate_access_blocked_after_confirmation": True,
            "cleanup_completed": True,
            "pre_deletion_server_artifacts": {
                "claim_versions": [1],
                "has_server_edited_copy": False,
                "has_server_approved_copy": True,
            },
            "post_cleanup_retained_claim_versions": [],
            "post_cleanup_retained_edited_copies": 0,
            "post_cleanup_retained_approved_copies": 0,
            "independent_source_revisions_preserved": [rev_id_by_key["src13b"]],
            "independent_facts_preserved": ["fact-13-2"],
            "distinguished_from_refetch_failure_ref03": True,
        },
    )
    add_atom(
        "atom-13-1",
        "SCN-13",
        "claim-13-1",
        "Deprecated legacy Memcached cluster and migrated session keys to Redis Cluster",
        "ACTION",
    )
    add_cas("cas-13-1", "SCN-13", "atom-13-1", "fact-13-1", "DIRECT", removed_by_user=True)

    add_claim(
        "claim-13-2",
        "SCN-13",
        "Migrated session keys from Memcached to Redis Cluster and built a Redis saturation alert dashboard.",
        ["atom-13-2", "atom-13-3"],
        claim_version=2,
        previous_claim_version=1,
        user_edited=True,
        reverification_state="REVERIFY_REQUIRED_ON_NEW_VERSION",
        user_edit_history=[
            {
                "event_id": "evt-del-13-2a",
                "from_version": 1,
                "to_version": 2,
                "action": "EDIT_CLAIM_TEXT",
                "severity": "MINOR_PARAPHRASE",
                "note": "Pre-deletion server state: claim-13-2 had v1 approved copy and v2 server edited copy mixing src13a + src13b.",
            },
            {
                "event_id": "evt-del-13-2b",
                "from_version": 2,
                "to_version": 2,
                "action": "DELETE_SOURCE_PREVIEW_CONFIRMED",
                "severity": "CASCADE_DELETE",
                "note": "OD-003: Deletion preview shown for src13a listing mixed-source dependent claim-13-2; user explicitly confirmed deletion.",
            },
            {
                "event_id": "evt-del-13-2c",
                "from_version": 2,
                "to_version": 2,
                "action": "BLOCK_DEPENDENT_CLAIM_ACCESS",
                "severity": "CASCADE_DELETE",
                "note": "OD-003: Dependent claim-13-2 immediately blocked from access despite also citing surviving src13b.",
            },
            {
                "event_id": "evt-del-13-2d",
                "from_version": 2,
                "to_version": 2,
                "action": "PURGE_DEPENDENT_CLAIM_AND_SERVER_COPIES",
                "severity": "CASCADE_DELETE",
                "note": "OD-003: Cleanup completed; claim-13-2 and all server edited copies, approved copies, and versions [1, 2] purged (NOT left as PARTIALLY_GROUNDED). Independent source src13b & fact-13-2 preserved.",
            },
        ],
        deletion_lifecycle={
            "policy_ref": "OD-003",
            "trigger_source_revision_id": rev_id_by_key["src13a"],
            "deletion_preview_shown": True,
            "explicit_user_confirmation": True,
            "immediate_access_blocked_after_confirmation": True,
            "cleanup_completed": True,
            "pre_deletion_server_artifacts": {
                "claim_versions": [1, 2],
                "has_server_edited_copy": True,
                "has_server_approved_copy": True,
            },
            "post_cleanup_retained_claim_versions": [],
            "post_cleanup_retained_edited_copies": 0,
            "post_cleanup_retained_approved_copies": 0,
            "independent_source_revisions_preserved": [rev_id_by_key["src13b"]],
            "independent_facts_preserved": ["fact-13-2"],
            "distinguished_from_refetch_failure_ref03": True,
        },
    )
    add_atom(
        "atom-13-2",
        "SCN-13",
        "claim-13-2",
        "Migrated session keys from Memcached to Redis Cluster",
        "ACTION",
    )
    add_atom(
        "atom-13-3",
        "SCN-13",
        "claim-13-2",
        "Built a Redis Cluster saturation alert dashboard for on-call engineers",
        "ACTION",
    )
    add_cas("cas-13-2", "SCN-13", "atom-13-2", "fact-13-1", "DIRECT", removed_by_user=True)
    add_cas("cas-13-3", "SCN-13", "atom-13-3", "fact-13-2", "DIRECT")

    # SCN-14
    add_claim(
        "claim-14-1",
        "SCN-14",
        "Migrated 12 Project Borealis docs to the Markdown linter, cut Project Alpha thumbnail decoding latency from 900ms to 40ms, and sped up Project Borealis image decoding to 400ms.",
        ["atom-14-1", "atom-14-2", "atom-14-3"],
    )
    add_atom(
        "atom-14-1",
        "SCN-14",
        "claim-14-1",
        "Migrated 12 internal documentation pages in Project Borealis to the new Markdown linter",
        "ACTION",
    )
    add_atom(
        "atom-14-2",
        "SCN-14",
        "claim-14-1",
        "Reduced Project Alpha image thumbnail decoding latency from 900ms to 40ms",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="NUMERIC_MAGNITUDE_DISTORTION",
    )
    add_atom(
        "atom-14-3",
        "SCN-14",
        "claim-14-1",
        "Reduced Project Borealis image thumbnail decoding latency to 400ms",
        "SCOPE_ENVIRONMENT",
        is_hard_negative=True,
        hard_negative_type="CROSS_ENTITY_CONFLATION",
    )
    add_cas("cas-14-1", "SCN-14", "atom-14-1", "fact-14-2", "DIRECT")
    add_cas("cas-14-2", "SCN-14", "atom-14-2", "fact-14-1", "CONTRADICTS")
    add_cas("cas-14-3", "SCN-14", "atom-14-3", "fact-14-1", "CONTEXT")
    add_cas("cas-14-4", "SCN-14", "atom-14-3", "fact-14-2", "CONTEXT")

    # --- Refetch Stub Snapshots & Stage 4 Checks (8 preserved + 4 added: refcase-02c same-hash locator-only mismatch; refcase-03c..03e transient 429/503/timeout) ---
    src02_norm = norm_by_key["src02"]
    src02_span = "I implemented an idempotent retry queue with exponential backoff for billing webhooks in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts."
    src02_baseline_loc = _make_locator("ref-base-src02", src02_norm, src02_span)

    # Corrupted locator on identical src02_norm (same content hash, wrong in-bounds offsets -> span_sha256 mismatch)
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
        {
            "fixture_case_id": "refcase-01a",
            "official_ref_id": "REF-01",
            "legacy_draft_id": "REF-01",
            "scenario_title": "Same revision, identical content hash and exact locator span reproduction",
            "target_url": "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks",
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
            "target_url": "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks",
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
            "target_url": "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks",
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
            "target_url": "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks",
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
            "target_url": "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks",
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
            "target_url": "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks",
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
            "target_url": "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks",
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
            "target_url": "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks",
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
            "target_url": "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks",
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
            "scenario_title": "Option C raw-source non-persistence static canary audit + negative leak fixture",
            "target_url": "https://synthetic.example.test/sources/ref07-canary-non-persistence",
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
            "target_url": "https://synthetic.example.test/sources/scn02-pr418-billing-webhooks",
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
    ]

    refetch_checks: list[dict[str, Any]] = [
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
    ]

    # --- Expected Oracle (DRAFT, provisional DRAFT_* labels strictly separated from product states) ---
    expected_scenarios = [
        {
            "scenario_id": "SCN-01",
            "evaluation_family_id": "family-src01",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "Zero facts from standup boilerplate + ungrounded candidate claim",
            "coverage_tags": ["zero_facts", "hard_negative", "ungrounded_claim", "mixed_ko_en"],
            "expected_fact_count": 0,
            "expected_atom_count": 1,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm 0-fact threshold for pure scheduling/boilerplate notes"],
            "rationale": "Meeting logistics chatter yields 0 canonical facts; required atom-01-1 has 0 supports and cannot be SOURCE_VERIFIED.",
        },
        {
            "scenario_id": "SCN-02",
            "evaluation_family_id": "family-src02",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "Single fact clean individual delivery baseline (implementation + conditional effect atoms)",
            "coverage_tags": ["single_fact", "direct_support", "individual_attribution", "effect_with_condition"],
            "expected_fact_count": 1,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": ["T-004B human confirmation of baseline SOURCE_VERIFIED candidate criteria (2/2 required atoms: implementation + conditional duplicate-notification prevention)"],
            "rationale": "Single unambiguous PR authored by verified owner directly supports both the retry queue implementation (atom-02-1) and the conditional effect preventing duplicate invoice notifications during transient gateway timeouts (atom-02-2).",
        },
        {
            "scenario_id": "SCN-03",
            "evaluation_family_id": "family-src03",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "Multiple (N=3) facts from Korean postmortem with split action/metric atoms and multi-byte UTF-8 offsets",
            "coverage_tags": ["n_facts", "multi_atom_claim", "korean_utf8_offsets", "direct_support"],
            "expected_fact_count": 3,
            "expected_atom_count": 4,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm multi-atom aggregation rule when all 4 required atoms (script action, p95 620->180ms result, 2-min rollback, >300ms alert) have DIRECT support"],
            "rationale": "Three Korean bullet items yield 3 canonical facts and 4 independently verifiable DIRECT-supported required atoms in claim-03-1 (action separated from p95 measurement).",
        },
        {
            "scenario_id": "SCN-04",
            "evaluation_family_id": "family-src04",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "DIRECT+CONTEXT grounding (qualifier-aligned) vs CONTEXT-only insufficient grounding",
            "coverage_tags": ["direct_support", "context_support", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm product state mapping for CONTEXT-only claim-04-2 (must never be SOURCE_VERIFIED) and verify tightened claim-04-1 wording"],
            "rationale": "claim-04-1 removes the unverified 'as part of Q3 reliability work' modifier so the Claim aligns strictly with DIRECT fact-04-1 (with fact-04-2 as CONTEXT); claim-04-2 remains CONTEXT-only and cannot be SOURCE_VERIFIED.",
        },
        {
            "scenario_id": "SCN-05",
            "evaluation_family_id": "family-src05",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "CONTRADICTS across incident draft vs addendum + separation of log-confirmed fact from unverified personal execution/audit claims",
            "coverage_tags": ["contradicts", "multi_revision", "attribution_split", "partial_support", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 4,
            "oracle_status": "DRAFT",
            "review_required_items": [
                "Confirm conflict precedence policy when a newer addendum contradicts an older draft (distinct draft vs correction scopes)",
                "Confirm claim-05-2 is blocked from SOURCE_VERIFIED because passive 'Log audit confirmed' verifies 412/503 (atom-05-3) but does NOT verify personal audit/documentation (atom-05-4)"
            ],
            "rationale": "claim-05-1 separates ungrounded personal failover execution (atom-05-1) from contradicted zero-drop claim (atom-05-2); claim-05-2 separates log-confirmed 412 HTTP 503s (atom-05-3, DIRECT) from personal log-audit attribution (atom-05-4, CONTEXT-only -> PARTIALLY_GROUNDED, SOURCE_VERIFIED forbidden).",
        },
        {
            "scenario_id": "SCN-06",
            "evaluation_family_id": "family-src06",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "Completed delivery (split implementation & monitoring atoms) vs planned Q4 RFC work (split sharding & benchmark atoms, observation-time scoped)",
            "coverage_tags": ["planned_vs_completed", "contradicts", "korean_utf8_offsets", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 4,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm product state mapping for planned-as-completed claim-06-2 (observation-time unconfirmed, NOT permanent incompleteness assertion)"],
            "rationale": "claim-06-1 splits endpoint implementation and production monitoring integration into 2 DIRECT atoms; claim-06-2 splits sharding completion and benchmark completion into 2 CONTRADICTED atoms scoped to observation time.",
        },
        {
            "scenario_id": "SCN-07",
            "evaluation_family_id": "family-src07",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "Negation context (reverted rollout KEPT; split negated production deployment & negated 30% cost savings)",
            "coverage_tags": ["negation_context", "contradicts", "hard_negative"],
            "expected_fact_count": 3,
            "expected_atom_count": 3,
            "oracle_status": "DRAFT",
            "review_required_items": ["Verify negation reversal rubric for independently negated atoms atom-07-2 (deployment) and atom-07-3 (30% savings)"],
            "rationale": "Explicit negations in src07 independently refute production deployment (fact-07-2 -> atom-07-2) and 30% compute cost savings (fact-07-3 -> atom-07-3) while supporting rollback claim-07-1.",
        },
        {
            "scenario_id": "SCN-08",
            "evaluation_family_id": "family-src08",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "Team achievement vs individual contribution (KEPT claim-08-1; split solo contradiction, unsupported 'from scratch', and team-only 22->8m metric)",
            "coverage_tags": ["team_vs_individual", "contradicts", "context_support", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 4,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm claim-08-2 CONFLICTING verdict combining solo-attribution contradiction (6-engineer team), unsupported 'from scratch', and team-only 22->8m CONTEXT"],
            "rationale": "claim-08-1 remains DIRECT-supported for individual cache layer work; claim-08-2 separates solo rebuild attribution (CONTRADICTS 6-engineer team fact-08-1), 'from scratch' (UNSUPPORTED), and 22->8m team outcome (CONTEXT-only).",
        },
        {
            "scenario_id": "SCN-09",
            "evaluation_family_id": "family-src09",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "Staging/test benchmark (KEPT claim-09-1) vs unstarted production deployment & staging-only metric split (claim-09-2)",
            "coverage_tags": ["test_vs_production", "contradicts", "context_support", "korean_utf8_offsets", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm environment-scope split on claim-09-2: unstarted production deployment (CONTRADICTED) vs production p95 metric (CONTEXT-only from staging)"],
            "rationale": "claim-09-1 accurately describes the local staging sandbox benchmark; claim-09-2 separates unstarted production deployment (CONTRADICTS fact-09-2) from production p95 improvement (CONTEXT-only from staging fact-09-1).",
        },
        {
            "scenario_id": "SCN-10",
            "evaluation_family_id": "family-src10",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "Unverified causation and fabricated metric in a 3-atom partial claim",
            "coverage_tags": ["unverified_metric_causation", "partial_support", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm causal attribution bar when confounders exist and no A/B test was run"],
            "rationale": "Only 1 of 3 required atoms is DIRECT-supported; atom-10-2 is CONTEXT-only and atom-10-3 has 0 supports -> cannot be SOURCE_VERIFIED.",
        },
        {
            "scenario_id": "SCN-11",
            "evaluation_family_id": "family-src11",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "Identity ambiguity (unmapped homonym 'Alex') vs verified account author",
            "coverage_tags": ["identity_ambiguity", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm identity resolution threshold before allowing SOURCE_VERIFIED"],
            "rationale": "Unmapped 'Alex' shared by two team members blocks automatic SOURCE_VERIFIED on claim-11-1.",
        },
        {
            "scenario_id": "SCN-12",
            "evaluation_family_id": "family-src12",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "User edit creates new version requiring re-verification (claim-12-1 split global scope & 60% metric without global-falsity inference; claim-12-2 KEPT)",
            "coverage_tags": ["user_edit_new_version_reverification", "scope_mismatch", "hard_negative"],
            "expected_fact_count": 1,
            "expected_atom_count": 3,
            "oracle_status": "DRAFT",
            "review_required_items": [
                "REVIEW_REQUIRED: Confirm product state transition when minor paraphrase v2 (claim-12-2) completes re-verification against fact-12-1",
                "Confirm claim-12-1 v2 re-verification fails on both global tuning scope (atom-12-1) and global 60% metric (atom-12-3) without inferring 60% is false in every measurement scope"
            ],
            "rationale": "User edits increment claim_version to v2 and forbid v1 auto-promotion; claim-12-1 splits global tuning and global 60% latency claims which fail v2 re-verification against checkout-only 25% evidence without over-inferring universal falsity.",
        },
        {
            "scenario_id": "SCN-13",
            "evaluation_family_id": "family-src13",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "OD-003 user source deletion: preview + explicit confirmation -> immediate access block -> full purge of dependent Claims & server copies (even mixed-source), preserving independent Source",
            "coverage_tags": ["od003_user_delete", "immediate_access_block", "full_server_purge", "independent_source_preserved"],
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "oracle_status": "DRAFT",
            "review_required_items": [
                "Confirm OD-003 deletion preview + explicit confirmation UX and audit log requirements",
                "Confirm mixed-source dependent claim-13-2 and all server edited/approved/versioned copies are purged after cleanup (never retained as PARTIALLY_GROUNDED), while independent src13b & fact-13-2 remain preserved",
                "Confirm clear separation from simple refetch failure (REF-03)"
            ],
            "rationale": "Under OD-003, after deletion preview and explicit user confirmation for src13a, dependent claims claim-13-1 and mixed-source claim-13-2 are immediately blocked from access; after cleanup completes, neither claim nor any server-side edited copy, approved copy, or version remains (PARTIALLY_GROUNDED is forbidden post-deletion), while independent source src13b (rev-src13b-v1-227d1587) and fact-13-2 are preserved.",
        },
        {
            "scenario_id": "SCN-14",
            "evaluation_family_id": "family-src14",
            "evaluation_partition": "DRAFT_UNASSIGNED_KEEP_FAMILY_TOGETHER",
            "title": "Hard negatives bundle: 10x numeric distortion and cross-project conflation",
            "coverage_tags": ["hard_negative", "partial_support", "contradicts", "numeric_distortion", "entity_conflation"],
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm multi-atom precedence when CONTRADICTED and CONTEXT-only atoms coexist"],
            "rationale": "Despite 1 DIRECT anchor atom, 400ms->40ms distortion and Alpha/Borealis conflation block SOURCE_VERIFIED.",
        },
    ]

    expected_atoms = [
        ("atom-01-1", "SCN-01", "claim-01-1", "DRAFT_ATOM_UNSUPPORTED", True, "0 canonical facts exist in boilerplate source src01."),
        ("atom-02-1", "SCN-02", "claim-02-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Implementation of idempotent retry queue with exponential backoff for billing webhooks in Project Aurora is directly supported by fact-02-1."),
        ("atom-02-2", "SCN-02", "claim-02-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Conditional effect (preventing duplicate invoice notifications during transient gateway timeouts) is directly supported by fact-02-1."),
        ("atom-03-1", "SCN-03", "claim-03-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Shard redistribution script authoring action is directly supported by Korean bullet 1 (fact-03-1 / fs-03-1)."),
        ("atom-03-2", "SCN-03", "claim-03-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Production cluster p95 latency reduction (620ms -> 180ms) is independently directly supported by Korean bullet 1 (fact-03-1 / fs-03-1)."),
        ("atom-03-3", "SCN-03", "claim-03-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Automatic rollback within 2 minutes on deployment failure is directly supported by Korean bullet 2 (fact-03-2 / fs-03-2)."),
        ("atom-03-4", "SCN-03", "claim-03-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Slow-query alert rule above 300ms threshold is directly supported by Korean bullet 3 (fact-03-3 / fs-03-3)."),
        ("atom-04-1", "SCN-04", "claim-04-1", "DRAFT_ATOM_SUPPORTED_DIRECT_WITH_CONTEXT", True, "WorkerPool v2 production migration is directly supported by fact-04-1 with initiative overview fact-04-2 as CONTEXT; unverified qualifier removed from claim-04-1."),
        ("atom-04-2", "SCN-04", "claim-04-2", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", True, "Only initiative goal CONTEXT exists (fact-04-2); CONTEXT-only cannot verify."),
        ("atom-05-1", "SCN-05", "claim-05-1", "DRAFT_ATOM_UNSUPPORTED", True, "Neither incident draft (src05a) nor addendum (src05b) states that the user personally executed the database primary failover (0 supports)."),
        ("atom-05-2", "SCN-05", "claim-05-1", "DRAFT_ATOM_CONTRADICTED", True, "Initial draft zero-drop assertion (fact-05-1) is explicitly refuted by postmortem addendum fact-05-2 (412 HTTP 503s during 90s failover window)."),
        ("atom-05-3", "SCN-05", "claim-05-2", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Postmortem addendum directly confirms that log audit recorded 412 HTTP 503 checkout responses during the 90-second failover window (fact-05-2)."),
        ("atom-05-4", "SCN-05", "claim-05-2", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", True, "Passive 'Log audit confirmed' in fact-05-2 does NOT directly establish that the user personally performed the log audit and documentation (CONTEXT-only -> SOURCE_VERIFIED forbidden)."),
        ("atom-06-1", "SCN-06", "claim-06-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Read-replica health-check endpoint implementation is directly supported by completed section fact-06-1."),
        ("atom-06-2", "SCN-06", "claim-06-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Production monitoring integration of the health-check endpoint is independently directly supported by completed section fact-06-1."),
        ("atom-06-3", "SCN-06", "claim-06-2", "DRAFT_ATOM_CONTRADICTED", True, "Source RFC lists user_events sharding as a future Q4 plan at observation time (fact-06-2), contradicting completed claim without asserting permanent future incompleteness."),
        ("atom-06-4", "SCN-06", "claim-06-2", "DRAFT_ATOM_CONTRADICTED", True, "Source RFC lists prototype benchmark as a next-month plan at observation time (fact-06-2), contradicting completed claim without asserting permanent future incompleteness."),
        ("atom-07-1", "SCN-07", "claim-07-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Directly supported rollback action fact-07-1."),
        ("atom-07-2", "SCN-07", "claim-07-2", "DRAFT_ATOM_CONTRADICTED", True, "Production deployment claim is directly contradicted by explicit negation fact-07-2 ('We did not deploy the canary autoscaler to production')."),
        ("atom-07-3", "SCN-07", "claim-07-2", "DRAFT_ATOM_CONTRADICTED", True, "30% compute cost savings claim is independently contradicted by explicit negation fact-07-3 ('we did not achieve the target 30% compute cost savings')."),
        ("atom-08-1", "SCN-08", "claim-08-1", "DRAFT_ATOM_SUPPORTED_DIRECT_WITH_CONTEXT", True, "Individual cache layer directly supported (fact-08-2); team rebuild provides CONTEXT (fact-08-1)."),
        ("atom-08-2", "SCN-08", "claim-08-2", "DRAFT_ATOM_CONTRADICTED", True, "Claiming to have single-handedly rebuilt the entire CI pipeline directly contradicts 6-engineer Platform Reliability team attribution in fact-08-1."),
        ("atom-08-3", "SCN-08", "claim-08-2", "DRAFT_ATOM_UNSUPPORTED", True, "No source evidence supports the 'from scratch' qualifier (0 supports)."),
        ("atom-08-4", "SCN-08", "claim-08-2", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", True, "22m -> 8m median build duration reduction is preserved strictly as a team-level outcome (CONTEXT-only for an individual solo-rebuild claim)."),
        ("atom-09-1", "SCN-09", "claim-09-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Accurately scoped to local staging sandbox benchmark (fact-09-1)."),
        ("atom-09-2", "SCN-09", "claim-09-2", "DRAFT_ATOM_CONTRADICTED", True, "Production database deployment claim directly contradicts fact-09-2 ('운영 환경 배포는 아직 시작하지 않았다')."),
        ("atom-09-3", "SCN-09", "claim-09-2", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", True, "840ms -> 95ms p95 improvement was observed only in local staging sandbox (fact-09-1); CONTEXT-only cannot verify a production performance claim."),
        ("atom-10-1", "SCN-10", "claim-10-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Launch date May 12 directly supported by fact-10-1."),
        ("atom-10-2", "SCN-10", "claim-10-1", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", True, "Revenue correlation with discount confounder and no A/B test is CONTEXT-only, not causal proof."),
        ("atom-10-3", "SCN-10", "claim-10-1", "DRAFT_ATOM_UNSUPPORTED", True, "Required atom has 0 supporting facts (fabricated 45% conversion lift)."),
        ("atom-11-1", "SCN-11", "claim-11-1", "DRAFT_ATOM_IDENTITY_AMBIGUOUS", True, "Source names unmapped 'Alex' across two workspace members (fact-11-1)."),
        ("atom-11-2", "SCN-11", "claim-11-2", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Verified owner account PR directly supports unit test addition (fact-11-2)."),
        ("atom-12-1", "SCN-12", "claim-12-1", "DRAFT_ATOM_REVERIFY_FAILED_AFTER_EDIT", True, "User edit to v2 expanded scope to global tuning across all services; checkout-only fact-12-1 fails v2 re-verification (CONTEXT-only)."),
        ("atom-12-3", "SCN-12", "claim-12-1", "DRAFT_ATOM_REVERIFY_FAILED_AFTER_EDIT", True, "User edit to v2 claimed 60% global p99 reduction vs 25% checkout peak-load p99 in fact-12-1; different measurement scopes -> v2 re-verification fails without inferring 60% is false in every scope."),
        ("atom-12-2", "SCN-12", "claim-12-2", "DRAFT_ATOM_REVERIFY_REQUIRED_NEW_VERSION", True, "User paraphrase created v2; existing support cas-12-2 was only evaluated on v1 and must be re-verified on v2 before any SOURCE_VERIFIED eligibility."),
        ("atom-13-1", "SCN-13", "claim-13-1", "DRAFT_ATOM_PURGED_WITH_DEPENDENT_CLAIM_OD003", True, "OD-003: src13a deletion preview + explicit confirmation immediately blocked claim-13-1; atom-13-1 purged with claim-13-1 on cleanup."),
        ("atom-13-2", "SCN-13", "claim-13-2", "DRAFT_ATOM_PURGED_WITH_DEPENDENT_CLAIM_OD003", True, "OD-003: src13a deleted; atom-13-2 purged together with dependent claim-13-2 and all server copies."),
        ("atom-13-3", "SCN-13", "claim-13-2", "DRAFT_ATOM_PURGED_WITH_DEPENDENT_CLAIM_OD003", True, "OD-003: Purged together with mixed-source dependent claim-13-2 (not left as surviving partial claim); underlying independent source src13b & fact-13-2 remain preserved."),
        ("atom-14-1", "SCN-14", "claim-14-1", "DRAFT_ATOM_SUPPORTED_DIRECT", True, "Directly supported by Project Borealis fact-14-2."),
        ("atom-14-2", "SCN-14", "claim-14-1", "DRAFT_ATOM_CONTRADICTED", True, "10x numeric distortion: source fact-14-1 states 400ms, claim states 40ms."),
        ("atom-14-3", "SCN-14", "claim-14-1", "DRAFT_ATOM_CONTEXT_ONLY_INSUFFICIENT", True, "Cross-project conflation attaching Project Alpha metric to Project Borealis (CONTEXT-only)."),
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

    expected_claims = [
        ("claim-01-1", "SCN-01", "DRAFT_CLAIM_UNGROUNDED", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "0 supporting facts for required atom-01-1; SOURCE_VERIFIED forbidden."),
        ("claim-02-1", "SCN-02", "DRAFT_CLAIM_GROUNDED_CANDIDATE", True, "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", True, "Draft candidate: 2/2 required atoms DIRECT-supported (retry queue implementation + duplicate invoice notification prevention during transient gateway timeouts), unedited v1, resolved owner."),
        ("claim-03-1", "SCN-03", "DRAFT_CLAIM_GROUNDED_CANDIDATE", True, "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", True, "Draft candidate: 4/4 required atoms DIRECT-supported (shard script action, p95 620->180ms result, 2-min rollback on deploy failure, >300ms slow-query alert)."),
        ("claim-04-1", "SCN-04", "DRAFT_CLAIM_GROUNDED_CANDIDATE", True, "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", True, "Draft candidate: unverified Q3-initiative modifier removed from claim; DIRECT + CONTEXT support on unedited required atom-04-1."),
        ("claim-04-2", "SCN-04", "DRAFT_CLAIM_UNGROUNDED", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "CONTEXT-only support on required atom-04-2; SOURCE_VERIFIED forbidden."),
        ("claim-05-1", "SCN-05", "DRAFT_CLAIM_CONFLICTING", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "Personal failover execution (atom-05-1) is unsupported and zero-drop claim (atom-05-2) is contradicted by postmortem addendum; SOURCE_VERIFIED forbidden."),
        ("claim-05-2", "SCN-05", "DRAFT_CLAIM_PARTIALLY_GROUNDED", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "1/2 required atoms DIRECT-supported: 412 HTTP 503s/90s confirmed by log audit (atom-05-3), but personal log-audit & documentation attribution (atom-05-4) is CONTEXT-only; SOURCE_VERIFIED forbidden."),
        ("claim-06-1", "SCN-06", "DRAFT_CLAIM_GROUNDED_CANDIDATE", True, "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", True, "Draft candidate: 2/2 required atoms DIRECT-supported (health-check endpoint implementation + production monitoring integration)."),
        ("claim-06-2", "SCN-06", "DRAFT_CLAIM_CONFLICTING", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "Both sharding completion (atom-06-3) and benchmark completion (atom-06-4) contradict RFC plan at observation time (not asserting permanent future incompleteness); SOURCE_VERIFIED forbidden."),
        ("claim-07-1", "SCN-07", "DRAFT_CLAIM_GROUNDED_CANDIDATE", True, "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", True, "Draft candidate: accurately describes reverted rollout."),
        ("claim-07-2", "SCN-07", "DRAFT_CLAIM_CONFLICTING", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "Both production deployment (atom-07-2) and 30% compute cost savings (atom-07-3) are independently negated in source; SOURCE_VERIFIED forbidden."),
        ("claim-08-1", "SCN-08", "DRAFT_CLAIM_GROUNDED_CANDIDATE", True, "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", True, "Draft candidate: scoped to individual artifact cache contribution."),
        ("claim-08-2", "SCN-08", "DRAFT_CLAIM_CONFLICTING", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "Solo rebuild attribution contradicts 6-engineer team work (atom-08-2 CONTRADICTED), 'from scratch' is unsupported (atom-08-3), and 22->8m is team-only CONTEXT (atom-08-4); SOURCE_VERIFIED forbidden."),
        ("claim-09-1", "SCN-09", "DRAFT_CLAIM_GROUNDED_CANDIDATE", True, "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", True, "Draft candidate: accurately scoped to staging sandbox."),
        ("claim-09-2", "SCN-09", "DRAFT_CLAIM_CONFLICTING", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "Production rollout claim contradicts unstarted rollout (atom-09-2 CONTRADICTED) and production p95 metric has only staging CONTEXT (atom-09-3); SOURCE_VERIFIED forbidden."),
        ("claim-10-1", "SCN-10", "DRAFT_CLAIM_PARTIALLY_GROUNDED", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "Only 1/3 required atoms DIRECT-supported (atom-10-2 CONTEXT-only, atom-10-3 0 supports); SOURCE_VERIFIED forbidden."),
        ("claim-11-1", "SCN-11", "DRAFT_CLAIM_IDENTITY_AMBIGUOUS", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "Ambiguous 'Alex' author blocks SOURCE_VERIFIED."),
        ("claim-11-2", "SCN-11", "DRAFT_CLAIM_GROUNDED_CANDIDATE", True, "CANDIDATE_FOR_SOURCE_VERIFIED_REVIEW", True, "Draft candidate: unambiguous verified account PR."),
        ("claim-12-1", "SCN-12", "DRAFT_CLAIM_REVERIFY_FAILED_AFTER_EDIT", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "User edit created v2; both global tuning scope (atom-12-1) and global 60% latency reduction (atom-12-3) fail v2 re-verification against checkout-only 25% evidence (without inferring 60% is false in every scope); v2 auto-promotion & SOURCE_VERIFIED forbidden."),
        ("claim-12-2", "SCN-12", "DRAFT_CLAIM_REVERIFY_REQUIRED_NEW_VERSION", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "User paraphrase created v2; must re-verify against fact-12-1 on v2 before any SOURCE_VERIFIED eligibility."),
        ("claim-13-1", "SCN-13", "DRAFT_CLAIM_BLOCKED_THEN_PURGED_OD003", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "OD-003: Deletion preview + explicit confirmation of src13a immediately blocks access; cleanup purges claim-13-1 and all server approved/version copies (distinct from REF-03). Independent src13b preserved."),
        ("claim-13-2", "SCN-13", "DRAFT_CLAIM_BLOCKED_THEN_PURGED_OD003", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "OD-003: Despite mixed dependency with surviving src13b, explicit confirmation of src13a deletion immediately blocks access and cleanup purges claim-13-2 plus all server edited copies, approved copies, and versions [1,2] (PARTIALLY_GROUNDED forbidden). Independent src13b & fact-13-2 preserved."),
        ("claim-14-1", "SCN-14", "DRAFT_CLAIM_CONFLICTING", False, "DO_NOT_MARK_SOURCE_VERIFIED_REVIEW_REQUIRED", True, "Required atoms include CONTRADICTED 10x distortion and CONTEXT-only conflation; SOURCE_VERIFIED forbidden."),
    ]

    # Official REF-01..REF-08 Coverage Matrix (honestly separating covered stubs vs partial/full coverage gaps)
    official_ref_matrix = [
        {
            "official_ref_id": "REF-01",
            "official_requirement_label": "동일 버전과 정확한 근거 위치 재현",
            "coverage_status": "COVERED_SYNTHETIC_STUB",
            "verified_in_draft": True,
            "linked_fixture_case_ids": ["refcase-01a"],
            "coverage_gap_details": [
                "Static synthetic fixture & round-trip locator check only (URL-only match or partial response must never pass REF-01); real external provider HTTP refetch is NOT executed."
            ],
            "oracle_status": "DRAFT",
            "review_required_items": [
                "REVIEW_REQUIRED: Confirm production normalization/offset contract for exact span reproduction"
            ],
        },
        {
            "official_ref_id": "REF-02",
            "official_requirement_label": "hash/locator 불일치 차단",
            "coverage_status": "COVERED_SYNTHETIC_STUB",
            "verified_in_draft": True,
            "linked_fixture_case_ids": ["refcase-02a", "refcase-02b", "refcase-02c"],
            "coverage_gap_details": [
                "Static synthetic stubs distinguish body drift (refcase-02a), appended new revision (refcase-02b), and identical-hash corrupted-locator mismatch (refcase-02c); Post-MVP auto-detect/auto-refresh remains disabled; real provider refetch NOT executed."
            ],
            "oracle_status": "DRAFT",
            "review_required_items": [
                "REVIEW_REQUIRED: Confirm whether shifted-offset fuzzy re-anchoring is MVP or Post-MVP",
                "REVIEW_REQUIRED: Confirm distinct error telemetry between upstream content change (refcase-02a/02b) and internal/corrupted locator mismatch (refcase-02c)"
            ],
        },
        {
            "official_ref_id": "REF-03",
            "official_requirement_label": "재조회 불가와 삭제·거짓 구분",
            "coverage_status": "COVERED_SYNTHETIC_STUB",
            "verified_in_draft": True,
            "linked_fixture_case_ids": ["refcase-03a", "refcase-03b", "refcase-03c", "refcase-03d", "refcase-03e"],
            "coverage_gap_details": [
                "Static synthetic stubs distinguish HTTP 404 (refcase-03a) and HTTP 206 partial (refcase-03b) from transient failures HTTP 429 (refcase-03c), HTTP 503 5xx (refcase-03d), and network timeout (refcase-03e) with retry-cap and approval-block expectations, all distinct from confirmed deletion (SCN-13) and historical falsity; live retry/backoff execution, HTTP 410 confirmed upstream delete, and runtime approval-gate E2E are NOT executed."
            ],
            "oracle_status": "DRAFT",
            "review_required_items": [
                "REVIEW_REQUIRED: Confirm product status distinction between 404, partial 206, transient 429/5xx/timeout retry-cap block, confirmed upstream delete (410), and user deletion (SCN-13)"
            ],
        },
        {
            "official_ref_id": "REF-04",
            "official_requirement_label": "원문 비영속",
            "coverage_status": "PARTIAL_COVERAGE_GAP",
            "verified_in_draft": True,
            "linked_fixture_case_ids": ["refcase-04a"],
            "coverage_gap_details": [
                "COVERAGE GAP (runtime): Synthetic canary (refcase-04a) audits static persisted JSON snapshots & negative leak fixture only; live DB tables, application logs, queue payloads, cache entries, error trackers, Export artifacts, and disk temp files are NOT yet tested."
            ],
            "oracle_status": "DRAFT",
            "review_required_items": [
                "REVIEW_REQUIRED: Confirm Option C zero-raw-text persistence boundary across DB, logs, caches, queues, and Export"
            ],
        },
        {
            "official_ref_id": "REF-05",
            "official_requirement_label": "접근 권한·허용된 URL 경계",
            "coverage_status": "PARTIAL_COVERAGE_GAP",
            "verified_in_draft": True,
            "linked_fixture_case_ids": ["refcase-05a", "refcase-05b"],
            "coverage_gap_details": [
                "COVERAGE GAP (runtime): Covers synthetic HTTP 403 revoked-scope stub and static URL pre-check (metadata IP, loopback, private IP, file/ftp, credentials); live OAuth/tenant isolation, HTTP 30x redirect SSRF chains, and DNS-rebinding enforcement are NOT yet tested."
            ],
            "oracle_status": "DRAFT",
            "review_required_items": [
                "REVIEW_REQUIRED: Confirm full production URL allowlist, redirect policy, and tenant/OAuth permission error codes"
            ],
        },
        {
            "official_ref_id": "REF-06",
            "official_requirement_label": "중단·재실행 시 원문 저장에 의존하지 않음",
            "coverage_status": "COVERAGE_GAP",
            "verified_in_draft": False,
            "linked_fixture_case_ids": [],
            "coverage_gap_details": [
                "COVERAGE GAP (unverified): No synthetic fixture or worker crash/checkpoint/resume harness exists in T-004A static draft (local T-007E runtime evidence is NOT product E2E verification). Requires worker restart/replay integration test proving checkpoint state stores only IDs/hashes/locators and re-fetches source on retry without reading persisted raw text."
            ],
            "oracle_status": "DRAFT",
            "review_required_items": [
                "REVIEW_REQUIRED: Define worker checkpoint schema and add restart/replay test in worker/integration QA harness"
            ],
        },
        {
            "official_ref_id": "REF-07",
            "official_requirement_label": "승인·Export·근거 열람 시 재조회",
            "coverage_status": "COVERAGE_GAP",
            "verified_in_draft": False,
            "linked_fixture_case_ids": [],
            "coverage_gap_details": [
                "COVERAGE GAP (unverified): No synthetic fixture or product E2E exists for approval, Export, or evidence-viewer refetch triggers (local T-007E runtime evidence is NOT product E2E verification). Explicitly out of scope for offline T-004A static dataset; requires product API/UI integration QA."
            ],
            "oracle_status": "DRAFT",
            "review_required_items": [
                "REVIEW_REQUIRED: Define trigger contract for on-demand refetch during Claim approval, Export generation, and evidence inspection"
            ],
        },
        {
            "official_ref_id": "REF-08",
            "official_requirement_label": "비용·크기 한도와 단계 간 메모리 전용 처리",
            "coverage_status": "COVERAGE_GAP",
            "verified_in_draft": False,
            "linked_fixture_case_ids": [],
            "coverage_gap_details": [
                "COVERAGE GAP (unverified): No synthetic fixture or runtime test exists for payload byte/char size caps, LLM token/cost ceilings, or in-memory-only inter-stage transport (local T-007E runtime evidence is NOT product E2E verification)."
            ],
            "oracle_status": "DRAFT",
            "review_required_items": [
                "REVIEW_REQUIRED: Confirm exact max source byte size, LLM budget/token cap, and inter-stage memory-only execution contract"
            ],
        },
    ]

    expected_refetch_fixtures = [
        {
            "fixture_case_id": "refcase-01a",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-01",
            "legacy_draft_id": "REF-01",
            "draft_refetch_verdict": "DRAFT_REFETCH_EXACT_SPAN_REPRODUCED",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": False,
            "expected_approval_blocked": False,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm exact product status name for successful refetch reproduction (URL-only or partial match must not pass)"],
            "rationale": "Same source revision reproduces identical content_sha256 and baseline_locator span; Post-MVP background auto-detection and auto-refresh remain disabled.",
        },
        {
            "fixture_case_id": "refcase-02a",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-02",
            "legacy_draft_id": "REF-02",
            "draft_refetch_verdict": "DRAFT_REFETCH_HASH_LOCATOR_MISMATCH_BLOCKED",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_approval_blocked": True,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm mismatch block UX/status vs Post-MVP re-anchoring"],
            "rationale": "Inserted header changes content hash and shifts offsets (body change); verification & approval are blocked without claiming historical falsity or auto-updating the claim.",
        },
        {
            "fixture_case_id": "refcase-02b",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-02",
            "legacy_draft_id": "REF-08",
            "draft_refetch_verdict": "DRAFT_REFETCH_HASH_MISMATCH_NEW_REVISION_BLOCKED_NO_AUTO_MUTATION",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_approval_blocked": True,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm policy when document hash changes via appended text while prefix span still matches"],
            "rationale": "Appended follow-up changes document content_sha256; hash mismatch blocks silent reuse and approval while Post-MVP auto-detect/auto-claim-refresh remains disabled.",
        },
        {
            "fixture_case_id": "refcase-02c",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-02",
            "legacy_draft_id": None,
            "draft_refetch_verdict": "DRAFT_REFETCH_SAME_HASH_LOCATOR_ONLY_MISMATCH_BLOCKED",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_approval_blocked": True,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm diagnostic/block handling when content_sha256 matches identically but stored locator offsets/span_sha256 fail round-trip"],
            "rationale": "Returned source body has the exact same content_sha256 as baseline, but tested_locator offsets point to the wrong span; blocks verification & approval cleanly distinct from upstream body drift (refcase-02a/02b).",
        },
        {
            "fixture_case_id": "refcase-03a",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-03",
            "legacy_draft_id": "REF-03",
            "draft_refetch_verdict": "DRAFT_REFETCH_UNAVAILABLE_DISTINGUISHED_FROM_DELETED_AND_FALSE",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_approval_blocked": True,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm product status for HTTP 404 (distinct from transient 429/5xx/timeout, confirmed deletion SCN-13, and historical falsity)"],
            "rationale": "Single HTTP 404 makes the source currently unverifiable and blocks approval; it MUST be distinguished from transient outages (refcase-03c..03e), confirmed user deletion (SCN-13), and historical falsity.",
        },
        {
            "fixture_case_id": "refcase-03b",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-03",
            "legacy_draft_id": "REF-04",
            "draft_refetch_verdict": "DRAFT_REFETCH_PARTIAL_DISTINGUISHED_FROM_DELETED_AND_FALSE",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_approval_blocked": True,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm truncated HTTP 206 retry/fallback handling"],
            "rationale": "Truncated HTTP 206 omits the baseline locator range -> currently unverifiable and blocks approval, neither confirmed deleted nor historically false.",
        },
        {
            "fixture_case_id": "refcase-03c",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-03",
            "legacy_draft_id": None,
            "draft_refetch_verdict": "DRAFT_REFETCH_TRANSIENT_429_BLOCKED_APPROVAL_AFTER_RETRY_CAP",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_approval_blocked": True,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm production max retry cap, Retry-After backoff, and approval-gate block UX for HTTP 429 (runtime retry harness NOT executed)"],
            "rationale": "Transient HTTP 429 rate-limit failure exhausts bounded retry cap (stub max=3) and blocks approval while unavailable; explicitly distinguished from HTTP 404, confirmed deletion (SCN-13), and historical falsity.",
        },
        {
            "fixture_case_id": "refcase-03d",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-03",
            "legacy_draft_id": None,
            "draft_refetch_verdict": "DRAFT_REFETCH_TRANSIENT_5XX_BLOCKED_APPROVAL_AFTER_RETRY_CAP",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_approval_blocked": True,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm production max retry cap and approval-gate block UX for HTTP 5xx transient outages (runtime retry harness NOT executed)"],
            "rationale": "Transient HTTP 503 upstream server outage exhausts bounded retry cap (stub max=3) and blocks approval while unavailable; explicitly distinguished from HTTP 404, confirmed deletion (SCN-13), and historical falsity.",
        },
        {
            "fixture_case_id": "refcase-03e",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-03",
            "legacy_draft_id": None,
            "draft_refetch_verdict": "DRAFT_REFETCH_TRANSIENT_TIMEOUT_BLOCKED_APPROVAL_AFTER_RETRY_CAP",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_approval_blocked": True,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm production timeout threshold, retry cap, and approval-gate block UX (runtime timeout harness NOT executed)"],
            "rationale": "Transient network/read timeout exhausts bounded retry cap (stub max=3) and blocks approval while unavailable; explicitly distinguished from HTTP 404, confirmed deletion (SCN-13), and historical falsity.",
        },
        {
            "fixture_case_id": "refcase-04a",
            "evaluation_family_id": "family-src_ref07_canary",
            "official_ref_id": "REF-04",
            "legacy_draft_id": "REF-07",
            "draft_refetch_verdict": "DRAFT_REFETCH_STATIC_CANARY_NON_PERSISTENCE_PASS",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": False,
            "expected_approval_blocked": False,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": True,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Extend canary audit to live DB/logs/caches/queues/Export in backend QA (PARTIAL_COVERAGE_GAP)"],
            "rationale": "Static Option C canary check confirms 0 canary tokens in clean persisted snapshot & stage/oracle JSON while proving detector sensitivity on negative_leaky_payload_fixture (not a substitute for runtime leak auditing).",
        },
        {
            "fixture_case_id": "refcase-05a",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-05",
            "legacy_draft_id": "REF-05",
            "draft_refetch_verdict": "DRAFT_REFETCH_POLICY_ACCESS_DENIED",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_approval_blocked": True,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm OAuth scope revocation & tenant permission boundary codes (PARTIAL_COVERAGE_GAP)"],
            "rationale": "HTTP 403 / revoked OAuth scope blocks access and approval without deleting or falsifying historical facts.",
        },
        {
            "fixture_case_id": "refcase-05b",
            "evaluation_family_id": "family-src02",
            "official_ref_id": "REF-05",
            "legacy_draft_id": "REF-06",
            "draft_refetch_verdict": "DRAFT_REFETCH_UNSAFE_URL_BLOCKED_PREFETCH",
            "expected_fetch_attempted": False,
            "expected_unverifiable_now": True,
            "expected_approval_blocked": True,
            "expected_source_deleted_confirmed": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Add live redirect/DNS-rebinding SSRF tests in backend QA (PARTIAL_COVERAGE_GAP)"],
            "rationale": "Metadata IP, loopback, private CIDR, embedded credentials, and non-HTTPS schemes are rejected prior to any outbound fetch.",
        },
    ]

    return {
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
            "stage_name": "STAGE_2_FACT_EXTRACTION",
            "stage_responsibility": "Extract 0/1/N Canonical Facts from Content Units and bind typed Fact Supports (DIRECT/CONTEXT/CONTRADICTS) with exact span locators.",
            "artifact_role": "STAGE_STUB_OUTPUT",
            "scenario_fact_counts": scenario_fact_counts,
            "canonical_facts": canonical_facts,
            "fact_supports": fact_supports,
        },
        "stages/stage3_claim_assembly.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "stage_name": "STAGE_3_CLAIM_ASSEMBLY",
            "stage_responsibility": "Assemble versioned Claims and required Claim Atoms linked to Canonical Facts via Claim Atom Supports, modeling user-edit re-verification on new versions and OD-003 deletion cascades without self-evidencing claims.",
            "artifact_role": "STAGE_STUB_OUTPUT",
            "claims": claims,
            "claim_atoms": claim_atoms,
            "claim_atom_supports": claim_atom_supports,
        },
        "stages/stage4_refetch_checks.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "stage_name": "STAGE_4_REFETCH_VERIFICATION",
            "stage_responsibility": "Evaluate synthetic refetch stub responses mapped to official REF-01..REF-05 (distinguishing body drift vs same-hash locator-only mismatch and HTTP 404 vs transient 429/5xx/timeout failures, with REF-06..REF-08 explicitly tracked as COVERAGE_GAP in oracle matrix), checking locator/hash consistency, URL safety, static canary non-persistence, and Post-MVP non-mutation boundary.",
            "artifact_role": "STAGE_STUB_OUTPUT",
            "refetch_checks": refetch_checks,
        },
        "expected/oracle_draft.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "artifact_role": "EXPECTED_ORACLE_DRAFT",
            "oracle_status": "DRAFT",
            "verdict_namespace": "DRAFT_PROVISIONAL_NOT_PRODUCT_STATUS",
            "product_status_mapping_policy": "REVIEW_REQUIRED — draft_*_verdict values are provisional T-004A evaluation labels and MUST NOT be conflated with finalized product status enums.",
            "frozen_for_golden": False,
            "requires_human_review_task": "T-004B",
            "provenance_and_freeze_preconditions": {
                "base_commit_sha": "f90b716036a9392b06b465f8d5545502d8eb18cd",
                "review_source_ref": "career-memory-round-01/claim-qa-independent-review.md (11 Claim revisions applied; 12 kept Claims & SCN-13 OD-003 preserved)",
                "human_approval_status": "PENDING_T004B_HUMAN_SIGNOFF",
                "family_split_anti_leakage_rule": "Keep all cases sharing evaluation_family_id (e.g., family-src02 across SCN-02 & refcase-01a..05b; family-src12 original/edited versions) in the same evaluation split before any golden freeze.",
                "synthetic_semantics_only_not_connector_evidence": True,
            },
            "scenarios": expected_scenarios,
            "expected_atoms": [
                {
                    "claim_atom_id": aid,
                    "scenario_id": sid,
                    "claim_id": cid,
                    "draft_atom_verdict": verdict,
                    "oracle_status": "DRAFT",
                    "review_required": req,
                    "rationale": rat,
                }
                for aid, sid, cid, verdict, req, rat in expected_atoms
            ],
            "expected_claims": [
                {
                    "claim_id": cid,
                    "scenario_id": sid,
                    "draft_claim_verdict": verdict,
                    "source_verified_eligible": eligible,
                    "product_status_proposal": proposal,
                    **(
                        {"od003_expected_post_cleanup": od003_expected_by_claim[cid]}
                        if cid in od003_expected_by_claim
                        else {}
                    ),
                    "oracle_status": "DRAFT",
                    "review_required": req,
                    "rationale": rat,
                }
                for cid, sid, verdict, eligible, proposal, req, rat in expected_claims
            ],
        },
        "expected/refetch_oracle_draft.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "artifact_role": "EXPECTED_REFETCH_ORACLE_DRAFT",
            "oracle_status": "DRAFT",
            "verdict_namespace": "DRAFT_PROVISIONAL_NOT_PRODUCT_STATUS",
            "product_status_mapping_policy": "REVIEW_REQUIRED — draft_refetch_verdict values are provisional T-004A evaluation labels, separate from final product states.",
            "frozen_for_golden": False,
            "requires_human_review_task": "T-004B",
            "official_ref_matrix": official_ref_matrix,
            "expected_refetch_fixtures": expected_refetch_fixtures,
        },
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
    print(f"Generated {len(written)} T-004A synthetic dataset files (v{DATASET_VERSION}) under {base_dir}:")
    for p in written:
        print(f"  - {p.relative_to(base_dir)}")


if __name__ == "__main__":
    main()

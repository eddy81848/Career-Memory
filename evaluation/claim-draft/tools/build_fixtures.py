"""Programmatically build all T-004A synthetic dataset fixtures with computed hashes/offsets/locators.

Run from repo/worktree root:
    python3 evaluation/claim-draft/tools/build_fixtures.py

Every version_id, content_sha256, unit_sha256, locator_id, char/utf8/utf16/line offset,
and span_sha256 is computed deterministically from the normalized synthetic texts.
No real user data, real URLs, API keys, or OAuth tokens are used.
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

SCHEMA_VERSION = "0.1.0-draft"
DATASET_VERSION = "0.1.0-draft"

CANARY_TOKEN_REF07 = "CM_SYNTHETIC_CANARY_RAW_TEXT_7F9A2C_DO_NOT_PERSIST"

REVIEW_REQUIRED_SPECS = [
    "REVIEW_REQUIRED: Production text normalization policy (CRLF/NFC/whitespace stripping) — Notion Grounded Claim Pipeline / Option C unavailable",
    "REVIEW_REQUIRED: Production offset unit standard (Unicode codepoint vs UTF-8 byte vs UTF-16 code unit)",
    "REVIEW_REQUIRED: Production canonical entity ID formats and hashing algorithm/salt policy",
    "REVIEW_REQUIRED: Exact REF-01..08 ID-to-scenario label mapping against Notion T-004A table",
    "REVIEW_REQUIRED: Minor user paraphrase edit invalidation tolerance (SCN-12 claim-12-2)",
    "REVIEW_REQUIRED: Exact Option C transient snippet retention/display policy vs zero-raw-text persistence",
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

    # --- Synthetic Source Revisions (all artificial, zero real user text/PII/secrets) ---
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
        scenario_ids=["SCN-02", "REF-01", "REF-02", "REF-03", "REF-04", "REF-05", "REF-06", "REF-08"],
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
    # Dedicated synthetic canary source for REF-07 (Option C raw-source non-persistence check)
    register_source(
        "src_ref07_canary",
        revision_seq=1,
        scenario_ids=["REF-07"],
        source_kind="SYNTHETIC_PR_SUMMARY",
        uri_slug="ref07-canary-non-persistence",
        language="en",
        author_label="Alex Kim (Verified Owner)",
        author_identity_confidence="VERIFIED_OWNER",
        created_at="2026-08-01T10:00:00Z",
        raw_text=(
            "PR #700 — Audit Log Index Maintenance (Synthetic Canary Document)\n"
            f"Transient Raw Body Sentinel: {CANARY_TOKEN_REF07}\n\n"
            "I added automated weekly partition pruning for synthetic audit log tables."
        ),
        contains_canary=True,
        canary_token_id="canary-ref07-raw-text",
    )

    # --- Stage 1: Content Units ---
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
        "REF-07",
        "src_ref07_canary",
        0,
        "PARAGRAPH",
        "I added automated weekly partition pruning for synthetic audit log tables.",
    )

    # --- Stage 2: Canonical Facts & Fact Supports ---
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

    # SCN-01: 0 canonical facts (boilerplate noise)

    # SCN-02: 1 canonical fact
    add_fact(
        "fact-02-1",
        "SCN-02",
        "Implemented an idempotent retry queue with exponential backoff for billing webhooks in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
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

    # SCN-03: 3 canonical facts (N facts, Korean UTF-8 multi-byte spans)
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
        "PRODUCTION",
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
        "PRODUCTION",
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

    # SCN-05: CONTRADICTS across revisions
    add_fact(
        "fact-05-1",
        "SCN-05",
        "Initial incident #77 draft asserted zero checkout requests were dropped during failover (later refuted by log audit).",
        "INCIDENT_RECORD",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
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
        "Postmortem log audit confirmed 412 checkout requests returned HTTP 503 during the 90-second failover window.",
        "CORRECTION_NOTICE",
        "COMPLETED",
        "PRODUCTION",
        "INDIVIDUAL",
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
        "Q4에 user_events 테이블 샤딩 도입 및 다음 달 프로토타입 벤치마크를 계획 중이다(미완료).",
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

    # SCN-07: Negation context
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
        "Did not deploy the canary autoscaler to production and did not achieve the target 30% compute cost savings.",
        "NEGATIVE_OUTCOME",
        "NEGATED",
        "PRODUCTION",
        "INDIVIDUAL",
    )
    add_fact_support(
        "fs-07-2",
        "SCN-07",
        "fact-07-2",
        "src07",
        "cu-07-0",
        "DIRECT",
        "We did not deploy the canary autoscaler to production; I reverted the Terraform config before rollout because cold-start spikes persisted, so we did not achieve the target 30% compute cost savings.",
    )

    # SCN-08: Team vs Individual
    add_fact(
        "fact-08-1",
        "SCN-08",
        "Platform Reliability team (6 engineers) rebuilt the monorepo CI pipeline, reducing median build duration from 22 minutes to 8 minutes.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
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
        "PRODUCTION",
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

    # SCN-12: User edit invalidation
    add_fact(
        "fact-12-1",
        "SCN-12",
        "Tuned PostgreSQL connection pool sizing in the checkout service, reducing p99 checkout API latency by 25% under peak load.",
        "ENGINEERING_DELIVERY",
        "COMPLETED",
        "PRODUCTION",
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

    # --- Stage 3: Claims, Claim Atoms, Claim Atom Supports ---
    claims: list[dict[str, Any]] = []
    claim_atoms: list[dict[str, Any]] = []
    claim_atom_supports: list[dict[str, Any]] = []

    def add_claim(
        claim_id: str,
        scenario_id: str,
        claim_text: str,
        atom_ids: list[str],
        *,
        user_edited: bool = False,
        user_edit_history: list[dict[str, str]] | None = None,
    ) -> None:
        claims.append(
            {
                "claim_id": claim_id,
                "scenario_id": scenario_id,
                "claim_text": claim_text,
                "claim_atom_ids": atom_ids,
                "user_edited": user_edited,
                "user_edit_history": user_edit_history or [],
                "derived_from_claim_ids": [],
            }
        )

    def add_atom(
        atom_id: str,
        scenario_id: str,
        claim_id: str,
        atom_text: str,
        atom_kind: str,
        *,
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
        removed_by_user: bool = False,
    ) -> None:
        claim_atom_supports.append(
            {
                "claim_atom_support_id": cas_id,
                "scenario_id": scenario_id,
                "claim_atom_id": atom_id,
                "canonical_fact_id": fact_id,
                "support_role": support_role,
                "removed_by_user": removed_by_user,
            }
        )

    # SCN-01 (0 facts -> 1 unsupported hallucinated candidate atom)
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

    # SCN-02 (1 fact -> 1 supported atom)
    add_claim(
        "claim-02-1",
        "SCN-02",
        "Implemented an idempotent retry queue with exponential backoff for Project Aurora billing webhooks, preventing duplicate invoice alerts.",
        ["atom-02-1"],
    )
    add_atom(
        "atom-02-1",
        "SCN-02",
        "claim-02-1",
        "Implemented idempotent retry queue with exponential backoff for billing webhooks in Project Aurora",
        "ACTION",
    )
    add_cas("cas-02-1", "SCN-02", "atom-02-1", "fact-02-1", "DIRECT")

    # SCN-03 (3 facts -> 3 atoms in 1 multi-atom claim)
    add_claim(
        "claim-03-1",
        "SCN-03",
        "프로젝트 오로라 검색 클러스터의 p95 지연을 620ms에서 180ms로 단축하고, 2분 내 자동 롤백 스크립트 및 300ms 슬로우 쿼리 알림을 구축했다.",
        ["atom-03-1", "atom-03-2", "atom-03-3"],
    )
    add_atom(
        "atom-03-1",
        "SCN-03",
        "claim-03-1",
        "검색 인덱스 샤드 재분배 스크립트로 운영 클러스터 p95 검색 지연을 620ms에서 180ms로 단축",
        "OUTCOME_METRIC",
    )
    add_atom(
        "atom-03-2",
        "SCN-03",
        "claim-03-1",
        "배포 실패 시 2분 이내 이전 스냅샷으로 복구하는 자동 롤백 스크립트 추가",
        "ACTION",
    )
    add_atom(
        "atom-03-3",
        "SCN-03",
        "claim-03-1",
        "슬로우 쿼리 300ms 초과 시 경보 발송 알림 규칙 구성",
        "ACTION",
    )
    add_cas("cas-03-1", "SCN-03", "atom-03-1", "fact-03-1", "DIRECT")
    add_cas("cas-03-2", "SCN-03", "atom-03-2", "fact-03-2", "DIRECT")
    add_cas("cas-03-3", "SCN-03", "atom-03-3", "fact-03-3", "DIRECT")

    # SCN-04 (DIRECT+CONTEXT vs CONTEXT-only)
    add_claim(
        "claim-04-1",
        "SCN-04",
        "Migrated the nightly billing job queue from legacy cron scripts to WorkerPool v2 in production as part of Q3 reliability work.",
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

    # SCN-05 (CONTRADICTS)
    add_claim(
        "claim-05-1",
        "SCN-05",
        "Executed database primary failover with zero dropped checkout requests.",
        ["atom-05-1"],
    )
    add_atom(
        "atom-05-1",
        "SCN-05",
        "claim-05-1",
        "Zero checkout requests were dropped during database primary failover",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="CONTRADICTED_BY_CORRECTION",
    )
    add_cas("cas-05-1", "SCN-05", "atom-05-1", "fact-05-1", "DIRECT")
    add_cas("cas-05-2", "SCN-05", "atom-05-1", "fact-05-2", "CONTRADICTS")

    add_claim(
        "claim-05-2",
        "SCN-05",
        "Audited failover logs and documented 412 HTTP 503 checkout responses during the 90-second failover window.",
        ["atom-05-2"],
    )
    add_atom(
        "atom-05-2",
        "SCN-05",
        "claim-05-2",
        "Audited failover logs confirming 412 HTTP 503 checkout responses during the 90-second failover window",
        "ACTION",
    )
    add_cas("cas-05-3", "SCN-05", "atom-05-2", "fact-05-2", "DIRECT")

    # SCN-06 (Planned vs Completed)
    add_claim(
        "claim-06-1",
        "SCN-06",
        "읽기 전용 복제본 헬스체크 엔드포인트를 구현하여 운영 모니터링에 연동했다.",
        ["atom-06-1"],
    )
    add_atom(
        "atom-06-1",
        "SCN-06",
        "claim-06-1",
        "읽기 전용 복제본 헬스체크 엔드포인트 구현 및 운영 모니터링 연동 완료",
        "ACTION",
    )
    add_cas("cas-06-1", "SCN-06", "atom-06-1", "fact-06-1", "DIRECT")

    add_claim(
        "claim-06-2",
        "SCN-06",
        "운영 환경의 user_events 테이블 샤딩을 완료하고 벤치마크를 마쳤다.",
        ["atom-06-2"],
    )
    add_atom(
        "atom-06-2",
        "SCN-06",
        "claim-06-2",
        "운영 환경의 user_events 테이블 샤딩 완료",
        "ACTION",
        is_hard_negative=True,
        hard_negative_type="PLANNED_AS_COMPLETED",
    )
    add_cas("cas-06-2", "SCN-06", "atom-06-2", "fact-06-2", "CONTRADICTS")

    # SCN-07 (Negation context)
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
        ["atom-07-2"],
    )
    add_atom(
        "atom-07-2",
        "SCN-07",
        "claim-07-2",
        "Deployed canary autoscaler to production and cut compute costs by 30%",
        "OUTCOME_METRIC",
        is_hard_negative=True,
        hard_negative_type="NEGATION_REVERSAL",
    )
    add_cas("cas-07-2", "SCN-07", "atom-07-2", "fact-07-2", "CONTRADICTS")

    # SCN-08 (Team vs Individual)
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
        ["atom-08-2"],
    )
    add_atom(
        "atom-08-2",
        "SCN-08",
        "claim-08-2",
        "Solo-rebuilt the entire monorepo CI pipeline reducing build duration from 22m to 8m",
        "ATTRIBUTION",
        is_hard_negative=True,
        hard_negative_type="TEAM_OVERCLAIM_AS_INDIVIDUAL",
    )
    add_cas("cas-08-3", "SCN-08", "atom-08-2", "fact-08-1", "CONTEXT")

    # SCN-09 (Staging vs Production)
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
        ["atom-09-2"],
    )
    add_atom(
        "atom-09-2",
        "SCN-09",
        "claim-09-2",
        "운영 데이터베이스에 복합 인덱스 배포 후 조회 p95를 840ms에서 95ms로 개선",
        "SCOPE_ENVIRONMENT",
        is_hard_negative=True,
        hard_negative_type="STAGING_CONFLATED_WITH_PRODUCTION",
    )
    add_cas("cas-09-2", "SCN-09", "atom-09-2", "fact-09-1", "CONTEXT")
    add_cas("cas-09-3", "SCN-09", "atom-09-2", "fact-09-2", "CONTRADICTS")

    # SCN-10 (Unverified causation + fabricated metric -> partially supported 3-atom claim)
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
    # atom-10-3 deliberately has 0 supports

    # SCN-11 (Identity ambiguity vs resolved owner)
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

    # SCN-12 (User edit invalidation: substantive & minor paraphrase)
    add_claim(
        "claim-12-1",
        "SCN-12",
        "Tuned PostgreSQL connection pool sizing globally, cutting p99 API latency by 60%.",
        ["atom-12-1"],
        user_edited=True,
        user_edit_history=[
            {
                "event_id": "evt-edit-12-1",
                "action": "EDIT_CLAIM_TEXT",
                "severity": "SUBSTANTIVE",
                "note": "User changed '25% in checkout service' to '60% globally'",
            }
        ],
    )
    add_atom(
        "atom-12-1",
        "SCN-12",
        "claim-12-1",
        "Reduced global p99 API latency by 60% via PostgreSQL pool tuning",
        "OUTCOME_METRIC",
        user_edited=True,
        edit_severity="SUBSTANTIVE",
        is_hard_negative=True,
        hard_negative_type="USER_EDIT_METRIC_INFLATION",
    )
    add_cas("cas-12-1", "SCN-12", "atom-12-1", "fact-12-1", "DIRECT")

    add_claim(
        "claim-12-2",
        "SCN-12",
        "Optimized Postgres pool configuration in the checkout service, lowering peak p99 API latency by 25%.",
        ["atom-12-2"],
        user_edited=True,
        user_edit_history=[
            {
                "event_id": "evt-edit-12-2",
                "action": "EDIT_CLAIM_TEXT",
                "severity": "MINOR_PARAPHRASE",
                "note": "User paraphrased wording without changing metric; conservative guard clears automatic SOURCE_VERIFIED",
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
    add_cas("cas-12-2", "SCN-12", "atom-12-2", "fact-12-1", "DIRECT")

    # SCN-13 (User deletion invalidation: full & partial cascade)
    add_claim(
        "claim-13-1",
        "SCN-13",
        "Deprecated the legacy Memcached cluster and migrated session keys to Redis Cluster.",
        ["atom-13-1"],
        user_edited=False,
        user_edit_history=[
            {
                "event_id": "evt-del-13-1",
                "action": "DELETE_SOURCE",
                "severity": "CASCADE_DELETE",
                "note": "User deleted underlying source src13a, cascading invalidation to fact-13-1 and claim-13-1",
            }
        ],
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
        user_edited=False,
        user_edit_history=[
            {
                "event_id": "evt-del-13-2",
                "action": "DELETE_SOURCE",
                "severity": "CASCADE_DELETE",
                "note": "Source src13a deleted; atom-13-2 invalidated while atom-13-3 remains supported -> PARTIALLY_SUPPORTED",
            }
        ],
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

    # SCN-14 (Hard negatives bundle: 10x unit distortion + cross-project conflation + partial anchor)
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

    # --- Refetch Stub Snapshots & Stage 4 Checks (REF-01..08) ---
    src02_norm = norm_by_key["src02"]
    src02_span = "I implemented an idempotent retry queue with exponential backoff for billing webhooks in Project Aurora, preventing duplicate invoice notifications during transient gateway timeouts."
    src02_baseline_loc = _make_locator("ref-base-src02", src02_norm, src02_span)

    src02_drifted_body = normalize_text(
        "[EDIT NOTICE 2026-08-05] Added billing compliance header above PR description.\n\n"
        + src02_norm
    )
    src02_partial_body = src02_norm[:95]  # Truncated before baseline locator char_end
    src02_v2_body = normalize_text(
        src02_norm
        + "\n\nFollow-up (2026-08-10): Added dead-letter queue metrics dashboard for webhook retries."
    )

    ref07_norm = norm_by_key["src_ref07_canary"]
    ref07_span = "I added automated weekly partition pruning for synthetic audit log tables."
    ref07_baseline_loc = _make_locator("ref-base-ref07", ref07_norm, ref07_span)

    unsafe_primary_url = "http://169.254.169.254/latest/meta-data/iam/"
    unsafe_extra_urls = [
        "http://127.0.0.1:8080/internal/sources",
        "file:///etc/passwd",
        "ftp://synthetic.example.test/raw-export",
        "https://user:pass@localhost/secret-doc",
        "http://10.0.0.12/admin",
    ]
    # Sanity-check our URL safety helper at build time
    for u in [unsafe_primary_url, *unsafe_extra_urls]:
        ok, _ = check_url_safety(u)
        if ok:
            raise RuntimeError(f"Expected unsafe URL to be blocked: {u}")

    refetch_snapshots: list[dict[str, Any]] = [
        {
            "refetch_case_id": "REF-01",
            "scenario_title": "Same revision, identical content hash and locator",
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
            "refetch_case_id": "REF-02",
            "scenario_title": "Changed content hash and shifted locator offsets (drift)",
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
            "refetch_case_id": "REF-03",
            "scenario_title": "Source unavailable (HTTP 404 Not Found — unverifiable now, NOT historically false)",
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
            "refetch_case_id": "REF-04",
            "scenario_title": "Partial/truncated refetch response (HTTP 206, span beyond returned bytes)",
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
            "refetch_case_id": "REF-05",
            "scenario_title": "Policy failure (HTTP 403 revoked scope / workspace policy denied)",
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
            "refetch_case_id": "REF-06",
            "scenario_title": "Unsafe URL rejected prior to network fetch (SSRF / metadata / loopback / file scheme)",
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
        {
            "refetch_case_id": "REF-07",
            "scenario_title": "Option C raw-source non-persistence canary verification",
            "target_url": "https://synthetic.example.test/sources/ref07-canary-non-persistence",
            "url_safety_precheck": "ALLOWED",
            "fetch_attempted": True,
            "http_status": 200,
            "stub_error_code": None,
            "is_partial_response": False,
            "returned_content_sha256": sha256_prefixed(ref07_norm),
            "returned_body_fixture_only": ref07_norm,
            "canary_tokens": [CANARY_TOKEN_REF07],
        },
        {
            "refetch_case_id": "REF-08",
            "scenario_title": "Successful refetch detects new upstream revision — Post-MVP boundary (no auto-detect / no auto-claim-refresh)",
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
    ]

    refetch_checks: list[dict[str, Any]] = [
        {
            "refetch_case_id": "REF-01",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "REFETCH_MATCHED",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "content_sha256": sha256_prefixed(src02_norm),
                "locator_id": src02_baseline_loc["locator_id"],
                "span_sha256": src02_baseline_loc["span_sha256"],
                "refetch_status": "REFETCH_MATCHED",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
        },
        {
            "refetch_case_id": "REF-02",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "CONTENT_CHANGED_LOCATOR_DRIFT",
            "locator_recheck_matched": False,
            "hash_recheck_matched": False,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "observed_content_sha256": sha256_prefixed(src02_drifted_body),
                "locator_id": src02_baseline_loc["locator_id"],
                "refetch_status": "CONTENT_CHANGED_LOCATOR_DRIFT",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
        },
        {
            "refetch_case_id": "REF-03",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "SOURCE_UNAVAILABLE",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "refetch_status": "SOURCE_UNAVAILABLE",
                "http_status": 404,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "refetch_case_id": "REF-04",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "PARTIAL_RESPONSE",
            "locator_recheck_matched": None,
            "hash_recheck_matched": False,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "refetch_status": "PARTIAL_RESPONSE",
                "http_status": 206,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "refetch_case_id": "REF-05",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "POLICY_DENIED",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "refetch_status": "POLICY_DENIED",
                "error_code": "POLICY_OAUTH_SCOPE_REVOKED",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "refetch_case_id": "REF-06",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "BLOCKED_UNSAFE_URL",
            "locator_recheck_matched": None,
            "hash_recheck_matched": None,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "refetch_status": "BLOCKED_UNSAFE_URL",
                "fetch_attempted": False,
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": [],
        },
        {
            "refetch_case_id": "REF-07",
            "baseline_source_revision_id": rev_id_by_key["src_ref07_canary"],
            "baseline_locator": ref07_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "CANARY_PERSISTENCE_AUDIT_PASS",
            "locator_recheck_matched": True,
            "hash_recheck_matched": True,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src_ref07_canary"],
                "content_sha256": sha256_prefixed(ref07_norm),
                "locator_id": ref07_baseline_loc["locator_id"],
                "span_sha256": ref07_baseline_loc["span_sha256"],
                "char_start": ref07_baseline_loc["char_start"],
                "char_end": ref07_baseline_loc["char_end"],
                "derived_fact_summary": "Added automated weekly partition pruning for synthetic audit log tables.",
                "raw_text_persisted": False,
            },
            "negative_leaky_payload_fixture": {
                "purpose": "Deliberate negative fixture to verify canary non-persistence detector catches raw source leakage",
                "leaked_debug_log_entry": f"ERROR raw_body_dump={CANARY_TOKEN_REF07}",
            },
            "post_mvp_tags": [],
        },
        {
            "refetch_case_id": "REF-08",
            "baseline_source_revision_id": rev_id_by_key["src02"],
            "baseline_locator": src02_baseline_loc,
            "target_claim_id": "claim-02-1",
            "stub_outcome": "NEW_REVISION_DETECTED_NO_AUTO_MUTATION",
            "locator_recheck_matched": True,
            "hash_recheck_matched": False,
            "persisted_payload_snapshot": {
                "source_revision_id": rev_id_by_key["src02"],
                "observed_content_sha256": sha256_prefixed(src02_v2_body),
                "locator_id": src02_baseline_loc["locator_id"],
                "claim_auto_mutated": False,
                "refetch_status": "NEW_REVISION_DETECTED_NO_AUTO_MUTATION",
            },
            "negative_leaky_payload_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
        },
    ]

    # --- Expected Oracle (DRAFT, strictly separated from sources & stages) ---
    expected_scenarios = [
        {
            "scenario_id": "SCN-01",
            "title": "Zero facts from standup boilerplate + ungrounded candidate claim",
            "coverage_tags": ["zero_facts", "hard_negative", "unsupported_claim", "mixed_ko_en"],
            "expected_fact_count": 0,
            "expected_atom_count": 1,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm 0-fact threshold for pure scheduling/boilerplate notes"],
            "rationale": "Meeting logistics chatter contains no career accomplishments; candidate claim has 0 fact supports and must remain UNSUPPORTED.",
        },
        {
            "scenario_id": "SCN-02",
            "title": "Single fact clean individual delivery baseline",
            "coverage_tags": ["single_fact", "direct_support", "individual_attribution"],
            "expected_fact_count": 1,
            "expected_atom_count": 1,
            "oracle_status": "DRAFT",
            "review_required_items": ["T-004B human confirmation of baseline SOURCE_VERIFIED criteria"],
            "rationale": "Single unambiguous PR authored by verified owner directly supports the single claim atom.",
        },
        {
            "scenario_id": "SCN-03",
            "title": "Multiple (N=3) facts from Korean postmortem with multi-byte UTF-8 offsets",
            "coverage_tags": ["n_facts", "multi_atom_claim", "korean_utf8_offsets", "direct_support"],
            "expected_fact_count": 3,
            "expected_atom_count": 3,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm multi-atom aggregation rule when all N atoms have DIRECT support"],
            "rationale": "Three distinct bullet items yield 3 canonical facts and 3 supported claim atoms in one composite claim.",
        },
        {
            "scenario_id": "SCN-04",
            "title": "DIRECT+CONTEXT grounding vs CONTEXT-only insufficient grounding",
            "coverage_tags": ["direct_support", "context_support", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm CONTEXT-only claims map to UNSUPPORTED (not PARTIALLY_SUPPORTED)"],
            "rationale": "Background initiative goal provides CONTEXT only and cannot independently verify a claim of 100% problem elimination.",
        },
        {
            "scenario_id": "SCN-05",
            "title": "CONTRADICTS across initial incident draft and corrected postmortem addendum",
            "coverage_tags": ["contradicts", "multi_revision", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm conflict precedence policy when a newer addendum contradicts an older draft"],
            "rationale": "Unresolved CONTRADICTS support prevents SOURCE_VERIFIED on the zero-drop claim while allowing verification of the corrected log-audit claim.",
        },
        {
            "scenario_id": "SCN-06",
            "title": "Completed delivery vs planned Q4 RFC work (Korean)",
            "coverage_tags": ["planned_vs_completed", "contradicts", "korean_utf8_offsets", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm planned-as-completed maps to CONFLICTING vs UNSUPPORTED"],
            "rationale": "Future plan statements cannot verify a completed-delivery claim, while the completed health-check item is directly supported.",
        },
        {
            "scenario_id": "SCN-07",
            "title": "Negation context (not deployed, reverted, cost savings not achieved)",
            "coverage_tags": ["negation_context", "contradicts", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": ["Verify negation reversal handling in Fact/Claim rubric"],
            "rationale": "Explicit negation refutes positive deployment/savings claims while supporting the accurate rollback claim.",
        },
        {
            "scenario_id": "SCN-08",
            "title": "Team achievement vs individual contribution attribution",
            "coverage_tags": ["team_vs_individual", "context_support", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm attribution rubric for team-level metrics referenced as CONTEXT"],
            "rationale": "Individual cache layer work is DIRECT-supported; solo-rebuilt CI pipeline overclaim lacks DIRECT individual support.",
        },
        {
            "scenario_id": "SCN-09",
            "title": "Staging/test benchmark vs unstarted production deployment (Korean)",
            "coverage_tags": ["test_vs_production", "contradicts", "korean_utf8_offsets", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm environment-scope mismatch triggers CONFLICTING when production non-deployment is explicit"],
            "rationale": "Staging benchmark results cannot be elevated to production impact claims when production rollout has not started.",
        },
        {
            "scenario_id": "SCN-10",
            "title": "Unverified causation and fabricated metric in a 3-atom partial claim",
            "coverage_tags": ["unverified_metric_causation", "partial_support", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm causal attribution bar when confounders exist and no A/B test was run"],
            "rationale": "Only 1 of 3 atoms (launch date) is supported; unverified revenue causation and fabricated 45% conversion lift keep the claim PARTIALLY_SUPPORTED.",
        },
        {
            "scenario_id": "SCN-11",
            "title": "Identity ambiguity (unmapped homonym 'Alex') vs verified account author",
            "coverage_tags": ["identity_ambiguity", "hard_negative"],
            "expected_fact_count": 2,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm identity resolution threshold before allowing SOURCE_VERIFIED"],
            "rationale": "Unmapped display name 'Alex' shared by two team members blocks automatic SOURCE_VERIFIED.",
        },
        {
            "scenario_id": "SCN-12",
            "title": "User edit invalidation (substantive inflation and minor paraphrase)",
            "coverage_tags": ["user_edit_invalidation", "hard_negative"],
            "expected_fact_count": 1,
            "expected_atom_count": 2,
            "oracle_status": "DRAFT",
            "review_required_items": [
                "REVIEW_REQUIRED: Confirm whether minor paraphrase edits (claim-12-2) also invalidate SOURCE_VERIFIED until re-verified"
            ],
            "rationale": "User-edited claims must never remain automatically SOURCE_VERIFIED without re-verification.",
        },
        {
            "scenario_id": "SCN-13",
            "title": "User source deletion cascading to full invalidation and partial-atom invalidation",
            "coverage_tags": ["user_delete_invalidation", "partial_support"],
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm deleted source cascade state naming (INVALIDATED_BY_DELETION vs UNSUPPORTED)"],
            "rationale": "Deleting src13a invalidates fact-13-1, fully invalidating claim-13-1 and downgrading multi-atom claim-13-2 to PARTIALLY_SUPPORTED.",
        },
        {
            "scenario_id": "SCN-14",
            "title": "Hard negatives bundle: 10x numeric distortion and cross-project conflation",
            "coverage_tags": ["hard_negative", "partial_support", "contradicts", "numeric_distortion", "entity_conflation"],
            "expected_fact_count": 2,
            "expected_atom_count": 3,
            "oracle_status": "DRAFT",
            "review_required_items": ["Confirm claim status precedence when a multi-atom claim has both CONTRADICTED and UNSUPPORTED atoms"],
            "rationale": "Despite 1 supported anchor atom, 400ms->40ms distortion and Alpha/Borealis conflation force CONFLICTING (never SOURCE_VERIFIED).",
        },
    ]

    expected_atoms = [
        ("atom-01-1", "SCN-01", "claim-01-1", "UNSUPPORTED", True, "0 canonical facts exist in boilerplate source."),
        ("atom-02-1", "SCN-02", "claim-02-1", "SUPPORTED_DIRECT", True, "Directly supported by fact-02-1 from verified owner PR."),
        ("atom-03-1", "SCN-03", "claim-03-1", "SUPPORTED_DIRECT", True, "Directly supported by Korean bullet 1 (fact-03-1)."),
        ("atom-03-2", "SCN-03", "claim-03-1", "SUPPORTED_DIRECT", True, "Directly supported by Korean bullet 2 (fact-03-2)."),
        ("atom-03-3", "SCN-03", "claim-03-1", "SUPPORTED_DIRECT", True, "Directly supported by Korean bullet 3 (fact-03-3)."),
        ("atom-04-1", "SCN-04", "claim-04-1", "SUPPORTED_DIRECT_WITH_CONTEXT", True, "DIRECT fact-04-1 plus CONTEXT fact-04-2."),
        ("atom-04-2", "SCN-04", "claim-04-2", "CONTEXT_ONLY_UNSUPPORTED", True, "Only initiative goal CONTEXT exists; no direct evidence of 100% elimination."),
        ("atom-05-1", "SCN-05", "claim-05-1", "CONTRADICTED", True, "Refuted by postmortem addendum fact-05-2 (412 HTTP 503s)."),
        ("atom-05-2", "SCN-05", "claim-05-2", "SUPPORTED_DIRECT", True, "Directly supported by audited addendum fact-05-2."),
        ("atom-06-1", "SCN-06", "claim-06-1", "SUPPORTED_DIRECT", True, "Directly supported by completed section fact-06-1."),
        ("atom-06-2", "SCN-06", "claim-06-2", "CONTRADICTED", True, "Source marks sharding as future Q4 plan, contradicting completed claim."),
        ("atom-07-1", "SCN-07", "claim-07-1", "SUPPORTED_DIRECT", True, "Directly supported rollback action fact-07-1."),
        ("atom-07-2", "SCN-07", "claim-07-2", "CONTRADICTED", True, "Directly contradicted by explicit negation fact-07-2."),
        ("atom-08-1", "SCN-08", "claim-08-1", "SUPPORTED_DIRECT_WITH_CONTEXT", True, "Individual cache layer directly supported; team rebuild provides context."),
        ("atom-08-2", "SCN-08", "claim-08-2", "CONTEXT_ONLY_UNSUPPORTED", True, "Team-of-6 accomplishment cannot directly verify solo-rebuilt claim."),
        ("atom-09-1", "SCN-09", "claim-09-1", "SUPPORTED_DIRECT", True, "Accurately scoped to local staging sandbox benchmark."),
        ("atom-09-2", "SCN-09", "claim-09-2", "CONTRADICTED", True, "Source explicitly states production rollout has not started."),
        ("atom-10-1", "SCN-10", "claim-10-1", "SUPPORTED_DIRECT", True, "Launch date May 12 directly supported by fact-10-1."),
        ("atom-10-2", "SCN-10", "claim-10-1", "CONTEXT_ONLY_UNSUPPORTED", True, "Observed revenue correlation with discount confounder and no A/B test cannot verify causation."),
        ("atom-10-3", "SCN-10", "claim-10-1", "UNSUPPORTED", True, "Fabricated 45% conversion lift has zero supporting facts."),
        ("atom-11-1", "SCN-11", "claim-11-1", "IDENTITY_AMBIGUOUS", True, "Source names unmapped 'Alex' across two workspace members."),
        ("atom-11-2", "SCN-11", "claim-11-2", "SUPPORTED_DIRECT", True, "Verified owner account PR directly supports unit test addition."),
        ("atom-12-1", "SCN-12", "claim-12-1", "INVALIDATED_BY_USER_EDIT", True, "User substantively inflated 25% checkout latency reduction to 60% globally."),
        ("atom-12-2", "SCN-12", "claim-12-2", "INVALIDATED_BY_USER_EDIT", True, "Conservative rule: minor user paraphrase still clears automatic SOURCE_VERIFIED until re-verified (REVIEW_REQUIRED)."),
        ("atom-13-1", "SCN-13", "claim-13-1", "INVALIDATED_BY_DELETION", True, "Underlying source src13a and fact-13-1 deleted by user."),
        ("atom-13-2", "SCN-13", "claim-13-2", "INVALIDATED_BY_DELETION", True, "Underlying source src13a deleted by user."),
        ("atom-13-3", "SCN-13", "claim-13-2", "SUPPORTED_DIRECT", True, "Surviving source src13b still directly supports Redis alert dashboard."),
        ("atom-14-1", "SCN-14", "claim-14-1", "SUPPORTED_DIRECT", True, "Directly supported by Project Borealis fact-14-2."),
        ("atom-14-2", "SCN-14", "claim-14-1", "CONTRADICTED", True, "10x numeric distortion: source states 400ms, claim states 40ms."),
        ("atom-14-3", "SCN-14", "claim-14-1", "CONTEXT_ONLY_UNSUPPORTED", True, "False cross-project conflation attaching Project Alpha metric to Project Borealis."),
    ]

    expected_claims = [
        ("claim-01-1", "SCN-01", "UNSUPPORTED", True, "0 supporting facts; cannot be SOURCE_VERIFIED."),
        ("claim-02-1", "SCN-02", "SOURCE_VERIFIED", True, "Draft candidate: 1/1 atom DIRECT-supported, unedited, resolved owner."),
        ("claim-03-1", "SCN-03", "SOURCE_VERIFIED", True, "Draft candidate: 3/3 atoms DIRECT-supported from verified Korean postmortem."),
        ("claim-04-1", "SCN-04", "SOURCE_VERIFIED", True, "Draft candidate: DIRECT + CONTEXT support on unedited atom."),
        ("claim-04-2", "SCN-04", "UNSUPPORTED", True, "CONTEXT-only support cannot verify outcome claim."),
        ("claim-05-1", "SCN-05", "CONFLICTING", True, "Contradicted by postmortem addendum."),
        ("claim-05-2", "SCN-05", "SOURCE_VERIFIED", True, "Draft candidate: directly grounded in corrected addendum."),
        ("claim-06-1", "SCN-06", "SOURCE_VERIFIED", True, "Draft candidate: completed item directly supported."),
        ("claim-06-2", "SCN-06", "CONFLICTING", True, "Planned work misrepresented as completed."),
        ("claim-07-1", "SCN-07", "SOURCE_VERIFIED", True, "Draft candidate: accurately describes reverted rollout."),
        ("claim-07-2", "SCN-07", "CONFLICTING", True, "Explicitly negated in source."),
        ("claim-08-1", "SCN-08", "SOURCE_VERIFIED", True, "Draft candidate: scoped to individual artifact cache contribution."),
        ("claim-08-2", "SCN-08", "UNSUPPORTED", True, "Overclaims team-of-6 rebuild as solo achievement."),
        ("claim-09-1", "SCN-09", "SOURCE_VERIFIED", True, "Draft candidate: accurately scoped to staging sandbox."),
        ("claim-09-2", "SCN-09", "CONFLICTING", True, "Conflates staging benchmark with unstarted production rollout."),
        ("claim-10-1", "SCN-10", "PARTIALLY_SUPPORTED", True, "Only 1/3 atoms supported; causation unverified and 45% metric fabricated."),
        ("claim-11-1", "SCN-11", "IDENTITY_AMBIGUOUS", True, "Ambiguous 'Alex' author prevents automatic SOURCE_VERIFIED."),
        ("claim-11-2", "SCN-11", "SOURCE_VERIFIED", True, "Draft candidate: unambiguous verified account PR."),
        ("claim-12-1", "SCN-12", "INVALIDATED_BY_USER_EDIT", True, "Substantive user edit invalidates SOURCE_VERIFIED."),
        ("claim-12-2", "SCN-12", "INVALIDATED_BY_USER_EDIT", True, "Minor user edit clears automatic SOURCE_VERIFIED pending re-verification (REVIEW_REQUIRED)."),
        ("claim-13-1", "SCN-13", "INVALIDATED_BY_DELETION", True, "All supporting facts deleted by user."),
        ("claim-13-2", "SCN-13", "PARTIALLY_SUPPORTED", True, "1 of 2 atoms invalidated by deletion; partial support cannot be SOURCE_VERIFIED."),
        ("claim-14-1", "SCN-14", "CONFLICTING", True, "Contains contradicted 10x metric distortion and cross-project conflation despite 1 supported atom."),
    ]

    expected_refetch_cases = [
        {
            "refetch_case_id": "REF-01",
            "expected_verdict": "REFETCH_MATCHED",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm REF-01 ID alignment with Notion T-004A"],
            "rationale": "Identical content hash and locator span confirm unchanged source; automatic background change polling and claim auto-refresh remain Post-MVP.",
        },
        {
            "refetch_case_id": "REF-02",
            "expected_verdict": "CONTENT_CHANGED_LOCATOR_DRIFT",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm whether shifted-offset fuzzy re-anchoring is MVP or Post-MVP"],
            "rationale": "Inserted header changes content hash and shifts offsets; marks locator drift/unverifiable-now without claiming the historical fact is false or auto-updating the claim.",
        },
        {
            "refetch_case_id": "REF-03",
            "expected_verdict": "CURRENTLY_UNVERIFIABLE",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm user-facing status badge for HTTP 404/410/timeout"],
            "rationale": "HTTP 404 means the source is currently unavailable/unverifiable; it MUST NOT be conflated with proving the past fact false.",
        },
        {
            "refetch_case_id": "REF-04",
            "expected_verdict": "CURRENTLY_UNVERIFIABLE_PARTIAL",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm byte-limit truncation retry/error policy"],
            "rationale": "Truncated HTTP 206 response omits the baseline locator range -> currently unverifiable, not falsified and not verified.",
        },
        {
            "refetch_case_id": "REF-05",
            "expected_verdict": "REFETCH_POLICY_FAILED",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": True,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm exact permission/retention policy failure codes"],
            "rationale": "HTTP 403 / revoked OAuth scope prevents verification now without falsifying historical facts.",
        },
        {
            "refetch_case_id": "REF-06",
            "expected_verdict": "BLOCKED_UNSAFE_URL",
            "expected_fetch_attempted": False,
            "expected_unverifiable_now": True,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm full production SSRF/DNS-rebinding allowlist policy"],
            "rationale": "Metadata IPs, loopback, private CIDR, embedded credentials, and non-HTTPS schemes must be rejected prior to any outbound request.",
        },
        {
            "refetch_case_id": "REF-07",
            "expected_verdict": "CANARY_NON_PERSISTENCE_VERIFIED",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": True,
            "post_mvp_tags": [],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm Option C zero-raw-text boundary for logs, caches, and traces"],
            "rationale": "Option C forbids persisting raw source text: clean persisted snapshot contains 0 canary tokens, while the negative leak fixture proves detector sensitivity.",
        },
        {
            "refetch_case_id": "REF-08",
            "expected_verdict": "NEW_REVISION_DETECTED_NO_AUTO_MUTATION",
            "expected_fetch_attempted": True,
            "expected_unverifiable_now": False,
            "expected_historical_fact_falsified": False,
            "expected_claim_auto_mutated": False,
            "expected_canary_leaked_in_clean_snapshot": False,
            "expected_canary_detected_in_negative_fixture": None,
            "post_mvp_tags": ["POST_MVP_AUTO_DETECT_SOURCE_CHANGE", "POST_MVP_AUTO_REFRESH_CLAIM"],
            "oracle_status": "DRAFT",
            "review_required_items": ["REVIEW_REQUIRED: Confirm Post-MVP handoff boundary for automatic upstream change detection and Claim auto-refresh"],
            "rationale": "Even when a manual refetch succeeds on a newer revision, MVP must not silently auto-detect changes or overwrite the user's Claim.",
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
            "stage_responsibility": "Assemble Claims and Claim Atoms linked to Canonical Facts via Claim Atom Supports, recording user edits/deletions without self-evidencing claims or prematurely marking oracle approval.",
            "artifact_role": "STAGE_STUB_OUTPUT",
            "claims": claims,
            "claim_atoms": claim_atoms,
            "claim_atom_supports": claim_atom_supports,
        },
        "stages/stage4_refetch_checks.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "stage_name": "STAGE_4_REFETCH_VERIFICATION",
            "stage_responsibility": "Evaluate synthetic refetch stub responses (REF-01..08), locator/hash consistency, URL safety precheck, Option C non-persistence canary audit, and Post-MVP non-mutation boundary.",
            "artifact_role": "STAGE_STUB_OUTPUT",
            "refetch_checks": refetch_checks,
        },
        "expected/oracle_draft.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "artifact_role": "EXPECTED_ORACLE_DRAFT",
            "oracle_status": "DRAFT",
            "frozen_for_golden": False,
            "requires_human_review_task": "T-004B",
            "scenarios": expected_scenarios,
            "expected_atoms": [
                {
                    "claim_atom_id": aid,
                    "scenario_id": sid,
                    "claim_id": cid,
                    "expected_atom_status": status,
                    "oracle_status": "DRAFT",
                    "review_required": req,
                    "rationale": rat,
                }
                for aid, sid, cid, status, req, rat in expected_atoms
            ],
            "expected_claims": [
                {
                    "claim_id": cid,
                    "scenario_id": sid,
                    "expected_claim_status": status,
                    "oracle_status": "DRAFT",
                    "review_required": req,
                    "rationale": rat,
                }
                for cid, sid, status, req, rat in expected_claims
            ],
        },
        "expected/refetch_oracle_draft.json": {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "artifact_role": "EXPECTED_REFETCH_ORACLE_DRAFT",
            "oracle_status": "DRAFT",
            "frozen_for_golden": False,
            "requires_human_review_task": "T-004B",
            "expected_refetch_cases": expected_refetch_cases,
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
    print(f"Generated {len(written)} T-004A synthetic dataset files under {base_dir}:")
    for p in written:
        print(f"  - {p.relative_to(base_dir)}")


if __name__ == "__main__":
    main()

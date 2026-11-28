"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ClaimReviewAdapter,
  ClaimReviewHarnessControls,
  EphemeralResolutionOverrideMode,
} from "@/adapters/claim-review-adapter";
import { MockClaimReviewAdapter } from "@/adapters/mock-claim-review-adapter";
import type {
  ClaimDetailResponse,
  ClaimGenerationResponse,
  ClaimReviewError,
  ClaimReviewScenarioId,
  ClaimReviewWorkspaceGuard,
  ClaimSummaryItem,
  ClaimSupportsResponse,
  ExportDetailResponse,
  ExportFormat,
  ExportPolicy,
  ExportSummaryItem,
  FactSummaryItem,
  FactSupportItem,
} from "@/domain/claim-review-models";
import {
  evaluateWorkspaceGuard,
  formatClaimStatusBadge,
  formatComputedVerificationBadge,
} from "@/domain/claim-review-presenter";
import { ClaimReviewDetailPanel } from "./ClaimReviewDetailPanel";
import { ClaimReviewScenarioBar } from "./ClaimReviewScenarioBar";
import { ExportPanel } from "./ExportPanel";
import { FactReviewPanel } from "./FactReviewPanel";

export interface ClaimReviewAppProps {
  adapter?: ClaimReviewAdapter;
  harness?: ClaimReviewHarnessControls;
  eventId?: string;
  initialScenario?: ClaimReviewScenarioId;
  workspaceGuard?: ClaimReviewWorkspaceGuard;
}

type ActiveSectionTab = "claims" | "facts" | "exports";

const DEFAULT_GUARD: ClaimReviewWorkspaceGuard = {
  import_ready: true,
  import_status: "SUCCESS",
  policy_status: "ACTIVE",
  event_id: "evt_comp_301",
  event_title: "결제 Webhook 서명 검증 및 리플레이 방어 체계 구축",
  event_status: "CONFIRMED",
  repository_label: "seoyeon-lab-demo/ledger-engine",
  notice: null,
};

export function ClaimReviewApp({
  adapter: injectedAdapter,
  harness: injectedHarness,
  eventId = "evt_comp_301",
  initialScenario = "default_grounded",
  workspaceGuard: injectedGuard,
}: ClaimReviewAppProps) {
  const defaultAdapterRef = useRef<MockClaimReviewAdapter | null>(null);
  if (!injectedAdapter && !defaultAdapterRef.current) {
    defaultAdapterRef.current = new MockClaimReviewAdapter({
      latencyMs: 60,
      initialScenario,
      eventId,
    });
  }

  const adapter: ClaimReviewAdapter =
    injectedAdapter ?? defaultAdapterRef.current!;
  const harness: ClaimReviewHarnessControls | undefined =
    injectedHarness ??
    (injectedAdapter ? undefined : defaultAdapterRef.current ?? undefined);

  const [scenarioId, setScenarioId] = useState<ClaimReviewScenarioId>(
    harness?.getScenario() ?? initialScenario
  );
  const [ephemeralOverride, setEphemeralOverride] =
    useState<EphemeralResolutionOverrideMode>(
      harness?.getEphemeralResolutionOverride() ?? "DEFAULT"
    );
  const [activeTab, setActiveTab] = useState<ActiveSectionTab>("claims");

  // Claims state
  const [claims, setClaims] = useState<ClaimSummaryItem[]>([]);
  const [selectedClaimId, setSelectedClaimId] = useState<string | null>(null);
  const [claimDetailsById, setClaimDetailsById] = useState<
    Record<string, ClaimDetailResponse>
  >({});
  const [claimSupportsById, setClaimSupportsById] = useState<
    Record<string, ClaimSupportsResponse>
  >({});

  // Facts state
  const [facts, setFacts] = useState<FactSummaryItem[]>([]);
  const [factSupportsById, setFactSupportsById] = useState<
    Record<string, FactSupportItem[] | undefined>
  >({});
  const [loadingFactSupportsId, setLoadingFactSupportsId] = useState<
    string | null
  >(null);

  // Exports state
  const [exportsList, setExportsList] = useState<ExportSummaryItem[]>([]);
  const [selectedExport, setSelectedExport] =
    useState<ExportDetailResponse | null>(null);

  // UI feedback & non-optimistic mutation state
  const [isLoadingClaims, setIsLoadingClaims] = useState(true);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [isLoadingSupports, setIsLoadingSupports] = useState(false);
  const [inFlightAction, setInFlightAction] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [activeError, setActiveError] = useState<ClaimReviewError | null>(null);
  const [generationBanner, setGenerationBanner] =
    useState<ClaimGenerationResponse | null>(null);

  const isMountedRef = useRef(true);
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const guard: ClaimReviewWorkspaceGuard = useMemo(() => {
    if (injectedGuard) return injectedGuard;
    if (harness) return harness.getWorkspaceGuard(eventId);
    return { ...DEFAULT_GUARD, event_id: eventId };
  }, [injectedGuard, harness, eventId, scenarioId]);

  const workspaceGate = useMemo(() => evaluateWorkspaceGuard(guard), [guard]);

  const loadClaimDetailAndSupports = useCallback(
    async (claimId: string) => {
      setIsLoadingDetail(true);
      setIsLoadingSupports(true);
      const [detailRes, supportsRes] = await Promise.all([
        adapter.getClaimDetail(claimId),
        adapter.getClaimSupports(claimId),
      ]);
      if (!isMountedRef.current) return;
      setIsLoadingDetail(false);
      setIsLoadingSupports(false);

      if (detailRes.ok) {
        setClaimDetailsById((prev) => ({
          ...prev,
          [claimId]: detailRes.data,
        }));
      } else {
        setActiveError(detailRes.error);
      }
      if (supportsRes.ok) {
        setClaimSupportsById((prev) => ({
          ...prev,
          [claimId]: supportsRes.data,
        }));
      }
    },
    [adapter]
  );

  const loadAllWorkspaceData = useCallback(
    async (options?: {
      preserveClaimId?: string | null;
      clearError?: boolean;
    }) => {
      if (!workspaceGate.canEnter) {
        setIsLoadingClaims(false);
        return;
      }
      setIsLoadingClaims(true);
      if (options?.clearError) {
        setActiveError(null);
      }

      const [claimsRes, factsRes, exportsRes] = await Promise.all([
        adapter.listClaims({ event_id: guard.event_id, include_superseded: false }),
        adapter.listFacts({ event_id: guard.event_id, include_superseded: false }),
        adapter.listExports(),
      ]);
      if (!isMountedRef.current) return;
      setIsLoadingClaims(false);

      if (claimsRes.ok) {
        const items = claimsRes.data.items;
        setClaims(items);
        const preferred =
          options?.preserveClaimId !== undefined
            ? options.preserveClaimId
            : selectedClaimId;
        const nextClaimId =
          preferred && items.some((c) => c.claim_id === preferred)
            ? preferred
            : items[0]?.claim_id ?? null;
        setSelectedClaimId(nextClaimId);
        if (nextClaimId) {
          await loadClaimDetailAndSupports(nextClaimId);
        }
      } else {
        setActiveError(claimsRes.error);
      }

      if (factsRes.ok) {
        setFacts(factsRes.data.items);
      }
      if (exportsRes.ok) {
        setExportsList(exportsRes.data.items);
      }
    },
    [
      adapter,
      guard.event_id,
      loadClaimDetailAndSupports,
      selectedClaimId,
      workspaceGate.canEnter,
    ]
  );

  useEffect(() => {
    void loadAllWorkspaceData({ clearError: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adapter, guard.event_id, scenarioId]);

  const selectedClaimDetail = selectedClaimId
    ? claimDetailsById[selectedClaimId] ?? null
    : null;
  const selectedClaimSupports = selectedClaimId
    ? claimSupportsById[selectedClaimId] ?? null
    : null;

  // --- Claim Generation (POST /v1/claims/generations) ---

  const handleGenerateClaims = async () => {
    if (inFlightAction !== null || workspaceGate.mutationsLocked) return;
    setInFlightAction("generate");
    setActiveError(null);
    setActionNotice(null);
    setGenerationBanner(null);

    const res = await adapter.generateClaims({
      confirmed_event_id: guard.event_id,
      input_fact_ids: facts
        .filter((f) => f.status === "SUPPORTED" || f.status === "USER_CONFIRMED")
        .map((f) => f.fact_id),
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setActiveError(res.error);
      return;
    }

    setGenerationBanner(res.data);
    if (res.data.generation_status === "SUCCEEDED" && res.data.claims.length > 0) {
      const firstNew = res.data.claims[0];
      setClaimDetailsById((prev) => ({ ...prev, [firstNew.claim_id]: firstNew }));
      setActionNotice(
        `신규 Grounded Claim(${firstNew.claim_id}) ${res.data.claims.length}건이 생성되었습니다.`
      );
      await loadAllWorkspaceData({ preserveClaimId: firstNew.claim_id });
    }
  };

  // --- Non-Optimistic Claim Mutations ---

  const handleEditClaim = async (params: {
    claim: ClaimDetailResponse;
    claim_text: string;
    edit_reason: string;
  }) => {
    if (inFlightAction !== null || workspaceGate.mutationsLocked) return;
    setInFlightAction("edit_claim");
    setActiveError(null);
    setActionNotice(null);

    const res = await adapter.editClaim({
      claim_id: params.claim.claim_id,
      expected_version: params.claim.version,
      claim_text: params.claim_text,
      edit_reason: params.edit_reason,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setActiveError(res.error);
      return;
    }

    // Non-optimistic: follow up with GET /v1/claims/{new_claim_id} and show Needs Revalidation!
    const newId = res.data.claim_id;
    setActionNotice(
      `새 초안 버전(${newId} · v${res.data.version} · DRAFT)이 생성되었습니다. 승인하려면 'Run Atomization & Support Validation'을 실행하세요.`
    );
    await loadAllWorkspaceData({ preserveClaimId: newId });
  };

  const handleRevalidateClaim = async (claim: ClaimDetailResponse) => {
    if (inFlightAction !== null || workspaceGate.mutationsLocked) return;
    setInFlightAction("revalidate");
    setActiveError(null);
    setActionNotice(null);

    const res = await adapter.revalidateClaim({
      claim_id: claim.claim_id,
      expected_version: claim.version,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setActiveError(res.error);
      return;
    }

    setClaimDetailsById((prev) => ({ ...prev, [res.data.claim_id]: res.data }));
    setActionNotice(
      `Atomization & Support Validation 완료: status=${res.data.status}, computed_verification=${res.data.computed_verification}, approvable=${String(res.data.approvable)}.`
    );
    await loadAllWorkspaceData({ preserveClaimId: res.data.claim_id });
  };

  const handleApproveClaim = async (params: {
    claim: ClaimDetailResponse;
    acknowledge_user_confirmed_atoms: boolean;
  }) => {
    if (inFlightAction !== null || workspaceGate.mutationsLocked) return;
    setInFlightAction("approve");
    setActiveError(null);
    setActionNotice(null);

    const res = await adapter.approveClaim({
      claim_id: params.claim.claim_id,
      expected_version: params.claim.version,
      expected_support_graph_epoch: params.claim.support_graph_epoch,
      acknowledge_user_confirmed_atoms: params.acknowledge_user_confirmed_atoms,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setActiveError(res.error);
      return;
    }

    setClaimDetailsById((prev) => ({ ...prev, [res.data.claim_id]: res.data }));
    setActionNotice(
      `Claim(${res.data.claim_id} · v${res.data.version})이 최종 승인(APPROVED)되었습니다. computed_verification은 서버 계산값(${res.data.computed_verification})으로 유지됩니다.`
    );
    await loadAllWorkspaceData({ preserveClaimId: res.data.claim_id });
  };

  const handleRejectClaim = async (params: {
    claim: ClaimDetailResponse;
    reason: string;
  }) => {
    if (inFlightAction !== null || workspaceGate.mutationsLocked) return;
    setInFlightAction("reject_claim");
    setActiveError(null);
    setActionNotice(null);

    const res = await adapter.rejectClaim({
      claim_id: params.claim.claim_id,
      expected_version: params.claim.version,
      reason: params.reason,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setActiveError(res.error);
      return;
    }

    setClaimDetailsById((prev) => ({ ...prev, [res.data.claim_id]: res.data }));
    setActionNotice(`Claim(${res.data.claim_id})이 제외(REJECTED)되었습니다.`);
    await loadAllWorkspaceData({ preserveClaimId: res.data.claim_id });
  };

  // --- Fact Review Handlers ---

  const handleLoadFactSupports = async (factId: string) => {
    setLoadingFactSupportsId(factId);
    const res = await adapter.getFactSupports(factId);
    if (!isMountedRef.current) return;
    setLoadingFactSupportsId(null);
    if (res.ok) {
      setFactSupportsById((prev) => ({ ...prev, [factId]: res.data.items }));
    } else {
      setActiveError(res.error);
    }
  };

  const handleApproveFact = async (fact: FactSummaryItem) => {
    if (inFlightAction !== null || workspaceGate.mutationsLocked) return;
    setInFlightAction("approve_fact");
    setActiveError(null);
    setActionNotice(null);
    const res = await adapter.approveFact({
      fact_id: fact.fact_id,
      expected_version: fact.version,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);
    if (!res.ok) {
      setActiveError(res.error);
      return;
    }
    setActionNotice(
      `Fact(${res.data.fact.fact_id})가 사용자 확인(${res.data.fact.status}) 상태로 승인되었습니다.`
    );
    await loadAllWorkspaceData({ preserveClaimId: selectedClaimId });
  };

  const handleRejectFact = async (fact: FactSummaryItem, reason: string) => {
    if (inFlightAction !== null || workspaceGate.mutationsLocked) return;
    setInFlightAction("reject_fact");
    setActiveError(null);
    setActionNotice(null);
    const res = await adapter.rejectFact({
      fact_id: fact.fact_id,
      expected_version: fact.version,
      reason,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);
    if (!res.ok) {
      setActiveError(res.error);
      return;
    }
    setActionNotice(`Fact(${res.data.fact.fact_id})가 제외(REJECTED)되었습니다.`);
    await loadAllWorkspaceData({ preserveClaimId: selectedClaimId });
  };

  const handleEditFact = async (params: {
    fact: FactSummaryItem;
    predicate: string;
    object_text: string;
  }) => {
    if (inFlightAction !== null || workspaceGate.mutationsLocked) return;
    setInFlightAction("edit_fact");
    setActiveError(null);
    setActionNotice(null);
    const res = await adapter.editFact({
      fact_id: params.fact.fact_id,
      expected_version: params.fact.version,
      predicate: params.predicate,
      object_text: params.object_text,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);
    if (!res.ok) {
      setActiveError(res.error);
      return;
    }
    setActionNotice(
      `ER-012 비덮어쓰기 편집 완료: 새 CANDIDATE 버전(${res.data.fact.fact_id} · v${res.data.fact.version})이 생성되었습니다.`
    );
    await loadAllWorkspaceData({ preserveClaimId: selectedClaimId });
  };

  // --- Export Handlers ---

  const handleCreateExport = async (params: {
    format: ExportFormat;
    export_policy: ExportPolicy;
    claim_ids: string[];
    expected_claim_versions: Record<string, number>;
  }) => {
    if (inFlightAction !== null || workspaceGate.mutationsLocked) return;
    setInFlightAction("create_export");
    setActiveError(null);
    setActionNotice(null);

    const res = await adapter.createExport({
      format: params.format,
      export_policy: params.export_policy,
      claim_ids: params.claim_ids,
      expected_claim_versions: params.expected_claim_versions,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setActiveError(res.error);
      return;
    }

    setSelectedExport(res.data);
    setActionNotice(
      `비인용 ${res.data.format} Export(${res.data.export_id})가 생성되었습니다(contains_raw_quotes=false).`
    );
    await loadAllWorkspaceData({ preserveClaimId: selectedClaimId });
  };

  const handleSelectExport = async (exportId: string) => {
    const res = await adapter.getExportDetail(exportId);
    if (!isMountedRef.current) return;
    if (res.ok) {
      setSelectedExport(res.data);
    } else {
      setActiveError(res.error);
    }
  };

  const handleDeleteExport = async (exportId: string) => {
    if (inFlightAction !== null || workspaceGate.mutationsLocked) return;
    setInFlightAction("delete_export");
    setActiveError(null);
    setActionNotice(null);

    const res = await adapter.deleteExport({ export_id: exportId });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setActiveError(res.error);
      return;
    }

    if (selectedExport?.export_id === exportId) {
      setSelectedExport(null);
    }
    setActionNotice(`Export(${exportId})가 삭제(DELETED)되었습니다.`);
    await loadAllWorkspaceData({ preserveClaimId: selectedClaimId });
  };

  // --- 409 Non-Destructive Conflict Recovery (Zero Auto-POST!) ---

  const handleReloadLatestClaim = async () => {
    const targetId = activeError?.conflictDetails?.claim_id ?? selectedClaimId;
    setActiveError(null);
    setActionNotice(
      "서버에서 최신 Claim 상태와 근거 그래프를 GET으로 다시 불러왔습니다(자동 재전송 없음). 변경 내용을 확인한 뒤 직접 재적용하세요."
    );
    await loadAllWorkspaceData({
      preserveClaimId: targetId,
      clearError: true,
    });
  };

  // --- Harness Controls ---

  const handleScenarioChange = (nextScenario: ClaimReviewScenarioId) => {
    if (!harness) return;
    harness.setScenario(nextScenario);
    setScenarioId(nextScenario);
    setEphemeralOverride("DEFAULT");
    setClaimDetailsById({});
    setClaimSupportsById({});
    setFactSupportsById({});
    setSelectedExport(null);
    setGenerationBanner(null);
    setActionNotice(null);
    setActiveError(null);
  };

  const handleEphemeralOverrideChange = (
    mode: EphemeralResolutionOverrideMode
  ) => {
    if (!harness) return;
    harness.setEphemeralResolutionOverride(mode);
    setEphemeralOverride(mode);
  };

  const handleSimulateConcurrentClaimUpdate = () => {
    if (!harness || !selectedClaimId) return;
    harness.simulateConcurrentClaimUpdate(selectedClaimId);
    setActionNotice(
      `테스트 주입 완료: 서버 측 ${selectedClaimId} 버전이 증가했습니다. 지금 편집/재검증/승인을 시도하면 409 CLAIM_VERSION_CONFLICT가 발생합니다.`
    );
  };

  const handleReset = () => {
    if (!harness) return;
    harness.reset();
    setEphemeralOverride("DEFAULT");
    setClaimDetailsById({});
    setClaimSupportsById({});
    setFactSupportsById({});
    setSelectedExport(null);
    setGenerationBanner(null);
    setActionNotice(null);
    setActiveError(null);
    void loadAllWorkspaceData({ preserveClaimId: null, clearError: true });
  };

  const is409Conflict =
    activeError !== null &&
    (activeError.kind === "version_conflict" ||
      activeError.kind === "support_incomplete" ||
      activeError.kind === "source_revision_mismatch" ||
      activeError.httpStatus === 409);

  return (
    <div className="page-shell" data-testid="claim-review-root">
      <a href="#claim-review-main" className="skip-link">
        본문으로 건너뛰기
      </a>

      <header className="page-header">
        <div className="page-header__inner page-header__inner--wide">
          <div className="page-header__nav-row">
            <div className="brand-row">
              <span className="brand-mark" aria-hidden="true">
                CM
              </span>
              <div>
                <p className="brand-kicker">
                  Career Memory · M4 Grounded Claim Review &amp; Export
                </p>
                <h1 className="page-title">
                  근거 기반 Claim 검토 · 출처 드릴다운 · 비인용 Export
                </h1>
              </div>
            </div>
            <div className="action-row">
              <a
                href="/event-review"
                className="btn btn--secondary btn--sm"
                data-testid="back-to-event-review-link"
              >
                Event Inbox로 돌아가기 (/event-review)
              </a>
              <a
                href="/"
                className="btn btn--ghost btn--sm"
                data-testid="back-to-import-link"
              >
                가져오기 화면 (/)
              </a>
            </div>
          </div>
          <p className="page-lead">
            확정된 Work Event(<code className="mono">{guard.event_id}</code> ·{" "}
            {guard.event_title})의 Canonical Fact, Atom 근거 체인, Plan C 메모리
            전용 임시 원문, 비인용 Markdown/JSON Export를 검증합니다.
          </p>
        </div>
      </header>

      <main
        id="claim-review-main"
        className="main-content main-content--wide"
        tabIndex={-1}
      >
        {harness && (
          <ClaimReviewScenarioBar
            scenarioId={scenarioId}
            ephemeralOverride={ephemeralOverride}
            selectedClaimId={selectedClaimId}
            disabled={inFlightAction !== null}
            onScenarioChange={handleScenarioChange}
            onEphemeralOverrideChange={handleEphemeralOverrideChange}
            onSimulateConcurrentClaimUpdate={handleSimulateConcurrentClaimUpdate}
            onReset={handleReset}
          />
        )}

        {/* Entry Guard: SUCCESS + import_ready=true */}
        {!workspaceGate.canEnter ? (
          <section
            className="status-banner status-banner--danger"
            role="alert"
            data-testid="claim-review-entry-guard-blocked"
          >
            <div className="status-banner__top">
              <span className="badge badge--danger mono">
                ENTRY GUARD BLOCKED (import_ready=false)
              </span>
            </div>
            <h2 className="status-banner__headline">
              완료된 가져오기(SUCCESS + import_ready=true) 전에는 Claim Review에
              진입할 수 없습니다
            </h2>
            <p className="status-banner__detail">{workspaceGate.lockReason}</p>
            <div className="action-row">
              <a href="/" className="btn btn--primary btn--sm">
                가져오기 상태 확인하기
              </a>
            </div>
          </section>
        ) : (
          <>
            {/* Policy Block / Deletion Fence Banner */}
            {workspaceGate.lockCode === "POLICY_BLOCKED" && (
              <div
                className="status-banner status-banner--danger"
                role="alert"
                data-testid="claim-policy-blocked-banner"
              >
                <div className="status-banner__top">
                  <span className="badge badge--danger">
                    정책 차단 (POLICY_BLOCKED · 삭제 아님)
                  </span>
                </div>
                <p className="status-banner__headline">
                  공개 정책 차단 상태로 모든 Fact/Claim 편집·승인·Export 및 원문
                  열람이 즉시 잠겼습니다
                </p>
                <p className="status-banner__detail">{workspaceGate.lockReason}</p>
              </div>
            )}

            {workspaceGate.lockCode === "DELETION_PENDING" && (
              <div
                className="status-banner status-banner--warning"
                role="alert"
                data-testid="claim-deletion-pending-banner"
              >
                <div className="status-banner__top">
                  <span className="badge badge--warning">
                    삭제 펜스 활성 (DELETION_PENDING · OD-003)
                  </span>
                </div>
                <p className="status-banner__headline">
                  삭제 진행 중 펜스 활성화로 모든 변경·승인·Export 및 원문 열람이
                  즉시 잠겼습니다
                </p>
                <p className="status-banner__detail">{workspaceGate.lockReason}</p>
              </div>
            )}

            {/* Overview + Generate Claims + Navigation Tabs */}
            <section className="card" aria-label="Claim Review 개요 및 탭">
              <div className="event-inbox-topbar">
                <div>
                  <span className="step-kicker">
                    CONFIRMED WORK EVENT · {guard.repository_label}
                  </span>
                  <h2 className="section-title" data-testid="claim-event-heading">
                    {guard.event_title}
                  </h2>
                  <p className="section-subtitle mono">
                    event_id={guard.event_id} · status={guard.event_status} ·
                    import_ready={String(guard.import_ready)}
                  </p>
                </div>

                <div className="action-row">
                  <button
                    type="button"
                    className="btn btn--primary btn--sm"
                    data-testid="generate-claims-button"
                    disabled={
                      workspaceGate.mutationsLocked || inFlightAction !== null
                    }
                    onClick={() => void handleGenerateClaims()}
                  >
                    {inFlightAction === "generate"
                      ? "Claim 생성 요청 중..."
                      : "Claim 생성 요청 (POST /v1/claims/generations)"}
                  </button>
                </div>
              </div>

              {/* Section Tabs */}
              <div
                className="filter-tab-row"
                role="tablist"
                aria-label="M4 검토 영역 전환"
                data-testid="claim-review-tabs"
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={activeTab === "claims"}
                  data-testid="tab-grounded-claims"
                  className={`btn btn--sm ${
                    activeTab === "claims" ? "btn--primary" : "btn--ghost"
                  }`}
                  onClick={() => setActiveTab("claims")}
                >
                  1. Grounded Claims &amp; Provenance ({claims.length})
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={activeTab === "facts"}
                  data-testid="tab-canonical-facts"
                  className={`btn btn--sm ${
                    activeTab === "facts" ? "btn--primary" : "btn--ghost"
                  }`}
                  onClick={() => setActiveTab("facts")}
                >
                  2. Canonical Facts ({facts.length})
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={activeTab === "exports"}
                  data-testid="tab-zero-quote-exports"
                  className={`btn btn--sm ${
                    activeTab === "exports" ? "btn--primary" : "btn--ghost"
                  }`}
                  onClick={() => setActiveTab("exports")}
                >
                  3. Markdown / JSON Export ({exportsList.length})
                </button>
              </div>

              {/* MANUAL_ONLY_BLOCKED / Generation Result Banner */}
              {generationBanner &&
                generationBanner.generation_status === "MANUAL_ONLY_BLOCKED" && (
                  <div
                    className="status-banner status-banner--warning"
                    role="status"
                    data-testid="manual-only-blocked-banner"
                  >
                    <div className="status-banner__top">
                      <span className="badge badge--warning mono">
                        MANUAL_ONLY_BLOCKED
                        {generationBanner.reason_code
                          ? ` · ${generationBanner.reason_code}`
                          : ""}
                      </span>
                    </div>
                    <p className="status-banner__headline">
                      호스팅 LLM 기본 비활성화(OFF) — 외부 AI 호출 없이 안전하게
                      중단되었습니다
                    </p>
                    <p className="status-banner__detail">
                      {generationBanner.reason_message ??
                        "기본 설정에서는 라이브 외부 LLM을 호출하지 않습니다. 아래 무료 대안을 선택하세요."}
                    </p>
                    <ul
                      className="bullet-list"
                      data-testid="manual-only-free-alternatives"
                    >
                      {generationBanner.free_alternatives.map((alt) => (
                        <li key={alt}>{alt}</li>
                      ))}
                    </ul>
                  </div>
                )}

              {generationBanner &&
                generationBanner.generation_status === "DRAFT_INVALID" && (
                  <div
                    className="status-banner status-banner--danger"
                    role="alert"
                    data-testid="generation-draft-invalid-banner"
                  >
                    <p className="status-banner__headline">
                      생성 초안 검증 실패 (DRAFT_INVALID ·{" "}
                      {generationBanner.reason_code ?? "SCHEMA_INVALID"})
                    </p>
                    <p className="status-banner__detail">
                      {generationBanner.reason_message ??
                        "생성된 초안이 스키마 또는 근거 경계를 위반하여 저장되지 않았습니다."}
                    </p>
                  </div>
                )}

              {/* Action Feedback Notice */}
              {actionNotice && !activeError && (
                <div
                  className="inline-alert inline-alert--info"
                  role="status"
                  aria-live="polite"
                  data-testid="claim-action-notice"
                >
                  {actionNotice}
                </div>
              )}

              {/* 409 Non-Destructive Conflict Recovery Banner (Zero Auto-POST!) */}
              {is409Conflict && activeError && (
                <div
                  className="status-banner status-banner--warning"
                  role="alert"
                  aria-live="assertive"
                  data-testid="claim-conflict-recovery-banner"
                  data-error-code={activeError.code}
                >
                  <div className="status-banner__top">
                    <span className="badge badge--warning mono">
                      409 {activeError.code}
                    </span>
                    {activeError.conflictDetails?.current_version !==
                      undefined && (
                      <span
                        className="mono run-id-chip"
                        data-testid="claim-conflict-latest-meta"
                      >
                        최신 버전: v{activeError.conflictDetails.current_version}
                        {activeError.conflictDetails.current_status
                          ? ` · 상태: ${activeError.conflictDetails.current_status}`
                          : ""}
                      </span>
                    )}
                  </div>
                  <p className="status-banner__headline">
                    충돌이 감지되어 변경을 중단했습니다 — 자동 재전송(auto-POST)을
                    수행하지 않았습니다
                  </p>
                  <p className="status-banner__detail">{activeError.message}</p>
                  <div className="action-row">
                    <button
                      type="button"
                      className="btn btn--primary btn--sm"
                      data-testid="reload-latest-claim-button"
                      onClick={() => void handleReloadLatestClaim()}
                    >
                      Reload Latest Claim
                    </button>
                  </div>
                </div>
              )}

              {/* Non-409 Error Banner */}
              {activeError && !is409Conflict && (
                <div
                  className="status-banner status-banner--danger"
                  role="alert"
                  data-testid="claim-error-banner"
                  data-error-code={activeError.code}
                >
                  <div className="status-banner__top">
                    <span className="badge badge--danger mono">
                      {activeError.code}
                    </span>
                  </div>
                  <p className="status-banner__headline">
                    {activeError.message}
                  </p>
                </div>
              )}
            </section>

            {/* Tab 1: Grounded Claims Master-Detail */}
            {activeTab === "claims" && (
              <div className="event-inbox-grid" data-testid="claims-workspace">
                <section
                  className="card event-list-pane"
                  aria-label="Claim 목록"
                  data-testid="claim-list-pane"
                >
                  <div className="subcard__header">
                    <h3 className="subcard__title">
                      Claim 목록 ({claims.length}건)
                    </h3>
                    <span className="field-help">
                      status와 verification 분리 표시
                    </span>
                  </div>
                  {isLoadingClaims ? (
                    <p className="field-help">Claim 목록을 불러오는 중...</p>
                  ) : claims.length === 0 ? (
                    <p className="field-help">
                      생성된 Claim이 없습니다. 상단에서 생성 요청을 실행하세요.
                    </p>
                  ) : (
                    <ul className="event-list" data-testid="claim-list">
                      {claims.map((item) => {
                        const isSelected = item.claim_id === selectedClaimId;
                        const statusB = formatClaimStatusBadge(item.status);
                        const verB = formatComputedVerificationBadge(
                          item.computed_verification
                        );
                        return (
                          <li
                            key={item.claim_id}
                            className={`event-list-item ${
                              isSelected ? "event-list-item--selected" : ""
                            }`}
                            data-testid={`claim-list-item-${item.claim_id}`}
                          >
                            <button
                              type="button"
                              className="event-list-item__body"
                              data-testid={`select-claim-${item.claim_id}`}
                              onClick={() => {
                                setSelectedClaimId(item.claim_id);
                                setActiveError(null);
                                void loadClaimDetailAndSupports(item.claim_id);
                              }}
                            >
                              <div className="status-banner__badges">
                                <span
                                  className={`badge ${statusB.badgeClass}`}
                                >
                                  {item.status}
                                </span>
                                <span className={`badge ${verB.badgeClass}`}>
                                  {item.computed_verification}
                                </span>
                                <span className="badge badge--neutral mono">
                                  v{item.version}
                                </span>
                              </div>
                              <span className="event-list-item__title">
                                {item.claim_text}
                              </span>
                              <span className="event-list-item__meta mono">
                                Atoms {item.atom_count} · SUPPORTED{" "}
                                {item.supported_atom_count} · USER_CONFIRMED{" "}
                                {item.user_confirmed_atom_count} · UNSUPPORTED{" "}
                                {item.unsupported_atom_count} · STALE{" "}
                                {item.stale_atom_count}
                              </span>
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </section>

                <ClaimReviewDetailPanel
                  adapter={adapter}
                  claim={selectedClaimDetail}
                  supports={selectedClaimSupports}
                  isLoadingDetail={isLoadingDetail}
                  isLoadingSupports={isLoadingSupports}
                  mutationsLocked={workspaceGate.mutationsLocked}
                  inFlightAction={inFlightAction}
                  onEditClaim={(params) => void handleEditClaim(params)}
                  onRevalidateClaim={(c) => void handleRevalidateClaim(c)}
                  onApproveClaim={(params) => void handleApproveClaim(params)}
                  onRejectClaim={(params) => void handleRejectClaim(params)}
                />
              </div>
            )}

            {/* Tab 2: Canonical Facts */}
            {activeTab === "facts" && (
              <FactReviewPanel
                adapter={adapter}
                facts={facts}
                supportsByFactId={factSupportsById}
                loadingSupportsForId={loadingFactSupportsId}
                mutationsLocked={workspaceGate.mutationsLocked}
                isMutating={inFlightAction !== null}
                onLoadFactSupports={(fid) => void handleLoadFactSupports(fid)}
                onApproveFact={(f) => void handleApproveFact(f)}
                onRejectFact={(f, reason) => void handleRejectFact(f, reason)}
                onEditFact={(params) => void handleEditFact(params)}
              />
            )}

            {/* Tab 3: Zero-Quote Markdown / JSON Export */}
            {activeTab === "exports" && (
              <ExportPanel
                claims={claims}
                exportsList={exportsList}
                selectedExport={selectedExport}
                mutationsLocked={workspaceGate.mutationsLocked}
                isBusy={inFlightAction !== null}
                onCreateExport={(params) => void handleCreateExport(params)}
                onSelectExport={(eid) => void handleSelectExport(eid)}
                onDeleteExport={(eid) => void handleDeleteExport(eid)}
              />
            )}
          </>
        )}
      </main>
    </div>
  );
}

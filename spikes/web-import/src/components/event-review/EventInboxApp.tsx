"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  EventReviewAdapter,
  EventReviewHarnessControls,
} from "@/adapters/event-review-adapter";
import { MockEventReviewAdapter } from "@/adapters/mock-event-review-adapter";
import type {
  EventInboxAvailabilityState,
  EventReviewError,
  EventReviewScenarioId,
  SplitPartitionInput,
  WorkEventDetailResponse,
  WorkEventStatus,
  WorkEventSummaryItem,
} from "@/domain/event-review-models";
import {
  computeActiveTopLevelCount,
  formatAnchorModeBadge,
  formatEventStatusLabel,
  isEventMutationBlockedByPolicyOrDeletion,
  REVIEW_FLAG_CATALOG,
  validateMergeSelection,
} from "@/domain/event-review-presenter";
import { DissolveConfirmModal } from "./DissolveConfirmModal";
import { EventDetailPanel } from "./EventDetailPanel";
import { EventReviewScenarioBar } from "./EventReviewScenarioBar";
import { MergeConfirmModal } from "./MergeConfirmModal";
import { SplitEventModal } from "./SplitEventModal";

export interface EventInboxAppProps {
  adapter?: EventReviewAdapter;
  harness?: EventReviewHarnessControls;
  sourceResourceId?: string;
  repositoryLabel?: string;
  embedded?: boolean;
  onBackToImport?: () => void;
}

type StatusFilterValue = "ACTIVE" | WorkEventStatus;

export function EventInboxApp({
  adapter: injectedAdapter,
  harness: injectedHarness,
  sourceResourceId,
  repositoryLabel = "seoyeon-lab-demo/ledger-engine",
  embedded = false,
  onBackToImport,
}: EventInboxAppProps) {
  const defaultAdapterRef = useRef<MockEventReviewAdapter | null>(null);
  if (!injectedAdapter && !defaultAdapterRef.current) {
    defaultAdapterRef.current = new MockEventReviewAdapter({
      latencyMs: 100,
      sourceResourceId,
    });
  }

  const adapter: EventReviewAdapter =
    injectedAdapter ?? defaultAdapterRef.current!;
  const harness: EventReviewHarnessControls | undefined =
    injectedHarness ??
    (injectedAdapter ? undefined : defaultAdapterRef.current ?? undefined);

  const [scenarioId, setScenarioId] = useState<EventReviewScenarioId>(
    harness?.getScenario() ?? "default_inbox"
  );
  const [statusFilter, setStatusFilter] = useState<StatusFilterValue>("ACTIVE");
  const [items, setItems] = useState<WorkEventSummaryItem[]>([]);
  const [activeTopLevelTotal, setActiveTopLevelTotal] = useState<number>(0);
  const [countsByStatus, setCountsByStatus] = useState<Record<string, number>>({});
  const [inboxState, setInboxState] =
    useState<EventInboxAvailabilityState>("READY");
  const [inboxNotice, setInboxNotice] = useState<string | null>(null);

  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [detailsById, setDetailsById] = useState<
    Record<string, WorkEventDetailResponse>
  >({});
  const [checkedForMergeIds, setCheckedForMergeIds] = useState<string[]>([]);

  const [isLoadingList, setIsLoadingList] = useState(true);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [inFlightAction, setInFlightAction] = useState<
    null | "confirm" | "edit" | "reject" | "split" | "merge" | "dissolve"
  >(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [activeError, setActiveError] = useState<EventReviewError | null>(null);

  const [isMergeModalOpen, setIsMergeModalOpen] = useState(false);
  const [isSplitModalOpen, setIsSplitModalOpen] = useState(false);
  const [isDissolveModalOpen, setIsDissolveModalOpen] = useState(false);

  const isMountedRef = useRef(true);
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const loadDetail = useCallback(
    async (eventId: string) => {
      setIsLoadingDetail(true);
      const res = await adapter.getWorkEventDetail(eventId);
      if (!isMountedRef.current) return;
      setIsLoadingDetail(false);
      if (res.ok) {
        setDetailsById((prev) => ({ ...prev, [eventId]: res.data }));
      } else {
        setActiveError(res.error);
      }
    },
    [adapter]
  );

  const loadList = useCallback(
    async (options?: {
      filter?: StatusFilterValue;
      preserveSelectedId?: string | null;
      clearError?: boolean;
    }) => {
      const targetFilter = options?.filter ?? statusFilter;
      setIsLoadingList(true);
      if (options?.clearError) {
        setActiveError(null);
      }

      const res = await adapter.listWorkEvents({
        limit: 50,
        status: targetFilter,
        source_resource_id: sourceResourceId,
      });
      if (!isMountedRef.current) return;
      setIsLoadingList(false);

      if (!res.ok) {
        setActiveError(res.error);
        return;
      }

      const listData = res.data;
      setItems(listData.items);
      setActiveTopLevelTotal(
        typeof listData.counts?.active_top_level_total === "number"
          ? listData.counts.active_top_level_total
          : computeActiveTopLevelCount(listData.items)
      );
      setCountsByStatus(listData.counts?.by_status ?? {});
      setInboxState(
        listData.inbox_state ??
          (listData.items.length === 0 ? "EMPTY_NO_ELIGIBLE_EVENTS" : "READY")
      );
      setInboxNotice(listData.inbox_notice ?? null);

      const candidateId =
        options?.preserveSelectedId !== undefined
          ? options.preserveSelectedId
          : selectedEventId;

      const nextSelected =
        candidateId && listData.items.some((i) => i.event_id === candidateId)
          ? candidateId
          : listData.items[0]?.event_id ?? null;

      setSelectedEventId(nextSelected);
      setCheckedForMergeIds((prev) =>
        prev.filter((id) => listData.items.some((i) => i.event_id === id))
      );

      if (nextSelected) {
        await loadDetail(nextSelected);
      }
    },
    [adapter, loadDetail, selectedEventId, sourceResourceId, statusFilter]
  );

  useEffect(() => {
    void loadList({ filter: statusFilter, clearError: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter, sourceResourceId, adapter]);

  // Preload details for merge-checked items so CANNOT_LINK constraints & Fact/Claim/Export impact are exact
  useEffect(() => {
    for (const id of checkedForMergeIds) {
      if (!detailsById[id]) {
        void loadDetail(id);
      }
    }
  }, [checkedForMergeIds, detailsById, loadDetail]);

  const selectedDetail = selectedEventId
    ? detailsById[selectedEventId] ?? null
    : null;

  const selectedForMerge = useMemo(
    () => items.filter((item) => checkedForMergeIds.includes(item.event_id)),
    [items, checkedForMergeIds]
  );

  const mergeValidation = useMemo(
    () => validateMergeSelection(selectedForMerge, detailsById),
    [selectedForMerge, detailsById]
  );

  const handleSelectEvent = (eventId: string) => {
    setSelectedEventId(eventId);
    setActiveError(null);
    void loadDetail(eventId);
  };

  const handleToggleMergeCheck = (eventId: string) => {
    setCheckedForMergeIds((prev) =>
      prev.includes(eventId)
        ? prev.filter((id) => id !== eventId)
        : [...prev, eventId]
    );
  };

  // --- 6 Curation Actions (STRICTLY NON-OPTIMISTIC: UI never updates before server 200!) ---

  const handleConfirmKeep = async (event: WorkEventDetailResponse) => {
    if (inFlightAction !== null) return;
    setInFlightAction("confirm");
    setActiveError(null);
    setActionNotice(null);

    const res = await adapter.confirmWorkEvent({
      event_id: event.event_id,
      expected_version: event.version,
      reason: "사용자가 Event Inbox에서 Keep (확정) 실행",
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setActiveError(res.error);
      return;
    }

    setDetailsById((prev) => ({ ...prev, [res.data.event_id]: res.data }));
    setActionNotice(
      `"${res.data.title}" Event가 확정(CONFIRMED · v${res.data.version})되었습니다.`
    );
    await loadList({ preserveSelectedId: res.data.event_id });
  };

  const handleSubmitEdit = async (params: {
    event: WorkEventDetailResponse;
    title: string;
    problem: string | null;
    action: string | null;
    outcome: string | null;
    reason: string;
  }) => {
    if (inFlightAction !== null) return;
    setInFlightAction("edit");
    setActiveError(null);
    setActionNotice(null);

    const res = await adapter.editWorkEvent({
      event_id: params.event.event_id,
      expected_version: params.event.version,
      title: params.title,
      problem: params.problem,
      action: params.action,
      outcome: params.outcome,
      reason: params.reason,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setActiveError(res.error);
      return;
    }

    setDetailsById((prev) => ({ ...prev, [res.data.event_id]: res.data }));
    setActionNotice(
      `편집이 저장되어 새 CANDIDATE 버전(v${res.data.version})이 생성되었습니다.`
    );
    await loadList({ preserveSelectedId: res.data.event_id });
  };

  const handleConfirmExclude = async (params: {
    event: WorkEventDetailResponse;
    reason: string;
  }) => {
    if (inFlightAction !== null) return;
    setInFlightAction("reject");
    setActiveError(null);
    setActionNotice(null);

    const res = await adapter.rejectWorkEvent({
      event_id: params.event.event_id,
      expected_version: params.event.version,
      reason: params.reason,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setActiveError(res.error);
      return;
    }

    setDetailsById((prev) => ({ ...prev, [res.data.event_id]: res.data }));
    setActionNotice(
      `"${res.data.title}" Event를 제외(REJECTED · v${res.data.version})했습니다. 원본 GitHub 기록 및 출처 메타데이터는 삭제되지 않았습니다.`
    );
    await loadList({ preserveSelectedId: res.data.event_id });
  };

  const handleConfirmSplit = async (params: {
    partitions: SplitPartitionInput[];
    reason: string;
  }) => {
    if (inFlightAction !== null || !selectedDetail) return;
    setInFlightAction("split");
    setActiveError(null);
    setActionNotice(null);

    const res = await adapter.splitWorkEvent({
      event_id: selectedDetail.event_id,
      expected_version: selectedDetail.version,
      partitions: params.partitions,
      reason: params.reason,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setIsSplitModalOpen(false);
      setActiveError(res.error);
      return;
    }

    setIsSplitModalOpen(false);
    const firstSegment = res.data.created_segments[0];
    setDetailsById((prev) => {
      const next = { ...prev };
      next[res.data.superseded_parent.event_id] = res.data.superseded_parent;
      for (const seg of res.data.created_segments) {
        next[seg.event_id] = seg;
      }
      return next;
    });
    setActionNotice(
      `원본 Event(${res.data.superseded_parent.event_id})가 SUPERSEDED 이력으로 이동하고 ${res.data.created_segments.length}개의 PR_SEGMENT 후보가 생성되었습니다.`
    );
    await loadList({ preserveSelectedId: firstSegment?.event_id ?? null });
  };

  const handleConfirmMerge = async (params: {
    title: string;
    reason: string;
  }) => {
    if (inFlightAction !== null || !mergeValidation.valid) return;
    setInFlightAction("merge");
    setActiveError(null);
    setActionNotice(null);

    // Non-optimistic: list & counts stay untouched until server returns 200!
    const res = await adapter.mergeWorkEvents({
      members: selectedForMerge.map((m) => ({
        event_id: m.event_id,
        expected_version: m.version,
      })),
      title: params.title,
      reason: params.reason,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setIsMergeModalOpen(false);
      setActiveError(res.error);
      return;
    }

    setIsMergeModalOpen(false);
    setCheckedForMergeIds([]);
    const composite = res.data.composite_event;
    setDetailsById((prev) => ({
      ...prev,
      [composite.event_id]: composite,
    }));
    setActionNotice(
      `${res.data.superseded_member_ids.length}개 Event가 새 USER_COMPOSITE(${composite.event_id})로 병합되었습니다. 피병합 멤버는 SUPERSEDED 이력으로 접히며 1개 논리 Event로 집계됩니다.`
    );
    await loadList({ preserveSelectedId: composite.event_id });
  };

  const handleConfirmDissolve = async (reason: string) => {
    if (inFlightAction !== null || !selectedDetail) return;
    setInFlightAction("dissolve");
    setActiveError(null);
    setActionNotice(null);

    // Non-optimistic: composite stays active until server 200 arrives!
    const res = await adapter.dissolveWorkEvent({
      event_id: selectedDetail.event_id,
      expected_version: selectedDetail.version,
      reason,
    });
    if (!isMountedRef.current) return;
    setInFlightAction(null);

    if (!res.ok) {
      setIsDissolveModalOpen(false);
      setActiveError(res.error);
      return;
    }

    setIsDissolveModalOpen(false);
    setDetailsById((prev) => {
      const next = { ...prev };
      next[res.data.dissolved_composite.event_id] = res.data.dissolved_composite;
      for (const member of res.data.restored_members) {
        next[member.event_id] = member;
      }
      return next;
    });
    const firstRestored = res.data.restored_members[0];
    setActionNotice(
      `USER_COMPOSITE(${res.data.dissolved_composite.event_id}) 병합이 해제되어 ${res.data.restored_members.length}개 멤버가 재평가·복원되었으며 기존 파생 산출물은 STALE 처리되었습니다.`
    );
    await loadList({ preserveSelectedId: firstRestored?.event_id ?? null });
  };

  // ER-004: Explicit safe reload after 409 EVENT_VERSION_CONFLICT (ZERO auto-POST!)
  const handleReloadAfterConflict = async () => {
    const conflictEventId =
      activeError?.conflictDetails?.event_id ?? selectedEventId;
    setActiveError(null);
    setActionNotice(
      "서버에서 최신 버전과 상태를 다시 불러왔습니다. 변경 사항을 확인한 뒤 직접 재적용해 주세요."
    );
    await loadList({
      preserveSelectedId: conflictEventId,
      clearError: true,
    });
  };

  const handleScenarioChange = (nextScenario: EventReviewScenarioId) => {
    if (!harness) return;
    harness.setScenario(nextScenario);
    setScenarioId(nextScenario);
    setCheckedForMergeIds([]);
    setDetailsById({});
    setActionNotice(null);
    setActiveError(null);
    setStatusFilter("ACTIVE");
    void loadList({ filter: "ACTIVE", preserveSelectedId: null, clearError: true });
  };

  const handleSimulateConcurrentUpdate = () => {
    if (!harness || !selectedEventId) return;
    harness.simulateConcurrentUpdate(selectedEventId);
    setActionNotice(
      `테스트 주입 완료: 서버 측 ${selectedEventId} 버전이 증가했습니다. 지금 Keep/Edit/Exclude/Dissolve를 실행하면 409 EVENT_VERSION_CONFLICT가 발생합니다.`
    );
  };

  const handleReset = () => {
    if (!harness) return;
    harness.reset();
    setCheckedForMergeIds([]);
    setDetailsById({});
    setActionNotice(null);
    setActiveError(null);
    setStatusFilter("ACTIVE");
    void loadList({ filter: "ACTIVE", preserveSelectedId: null, clearError: true });
  };

  const isGlobalPolicyBlocked =
    inboxState === "POLICY_BLOCKED" ||
    (items.length > 0 &&
      items.every((item) => item.policy_state.status === "POLICY_BLOCKED"));
  const isGlobalDeletionPending =
    inboxState === "DELETION_PENDING" ||
    (items.length > 0 &&
      items.every((item) => item.policy_state.status === "DELETION_PENDING"));

  const bodyContent = (
    <div className="event-inbox-shell" data-testid="event-inbox-root">
      {harness && (
        <EventReviewScenarioBar
          scenarioId={scenarioId}
          selectedEventId={selectedEventId}
          disabled={inFlightAction !== null}
          onScenarioChange={handleScenarioChange}
          onSimulateConcurrentUpdate={handleSimulateConcurrentUpdate}
          onReset={handleReset}
        />
      )}

      {/* Top Overview & Active Logical Event Count (EV-010) */}
      <section className="card" aria-labelledby="event-inbox-heading">
        <div className="event-inbox-topbar">
          <div>
            <span className="step-kicker">M3 EVENT INBOX</span>
            <h2 id="event-inbox-heading" className="section-title">
              Event 후보 검토 및 큐레이션 ({repositoryLabel})
            </h2>
            <p className="section-subtitle">
              병합된 <code className="mono">USER_COMPOSITE</code>는 1개의 논리
              Event로만 집계되며, 피병합 <code className="mono">SUPERSEDED</code>{" "}
              멤버는 기본 목록에서 접힙니다.
            </p>
          </div>
          <div className="event-inbox-counters">
            <div className="counter-chip" data-testid="active-event-count">
              <span className="counter-chip__label">활성 논리 Event</span>
              <strong className="counter-chip__value mono">
                {activeTopLevelTotal}건
              </strong>
            </div>
            <div className="counter-chip" data-testid="candidate-event-count">
              <span className="counter-chip__label">검토 대기</span>
              <strong className="counter-chip__value mono">
                {countsByStatus.CANDIDATE ?? 0}건
              </strong>
            </div>
            <div className="counter-chip" data-testid="confirmed-event-count">
              <span className="counter-chip__label">확정됨</span>
              <strong className="counter-chip__value mono">
                {countsByStatus.CONFIRMED ?? 0}건
              </strong>
            </div>
          </div>
        </div>

        {/* Status Filter Tabs */}
        <div
          className="filter-tab-row"
          role="tablist"
          aria-label="Event 상태 필터"
          data-testid="event-status-filters"
        >
          {(
            [
              { value: "ACTIVE", label: `활성 목록 (${activeTopLevelTotal})` },
              {
                value: "CANDIDATE",
                label: `검토 대기 (${countsByStatus.CANDIDATE ?? 0})`,
              },
              {
                value: "CONFIRMED",
                label: `확정됨 (${countsByStatus.CONFIRMED ?? 0})`,
              },
              {
                value: "REJECTED",
                label: `제외됨 (${countsByStatus.REJECTED ?? 0})`,
              },
              {
                value: "STALE",
                label: `재검토 (${countsByStatus.STALE ?? 0})`,
              },
              {
                value: "SUPERSEDED",
                label: `대체 이력 (${countsByStatus.SUPERSEDED ?? 0})`,
              },
            ] as const
          ).map((tab) => (
            <button
              key={tab.value}
              type="button"
              role="tab"
              aria-selected={statusFilter === tab.value}
              data-testid={`filter-tab-${tab.value}`}
              className={`btn btn--sm ${
                statusFilter === tab.value ? "btn--primary" : "btn--ghost"
              }`}
              onClick={() => setStatusFilter(tab.value)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Non-optimistic In-Flight Mutation Banner */}
        {inFlightAction && (
          <div
            className="status-banner status-banner--info"
            role="status"
            aria-live="polite"
            data-testid="event-mutation-pending-banner"
            data-action={inFlightAction}
          >
            <p className="status-banner__headline">
              {inFlightAction === "merge"
                ? "병합 처리 중... (서버 200 커밋 응답 대기 중)"
                : inFlightAction === "dissolve"
                  ? "병합 해제 처리 중... (서버 200 커밋 응답 대기 중)"
                  : inFlightAction === "split"
                    ? "범위 분할 처리 중..."
                    : inFlightAction === "confirm"
                      ? "확정(Keep) 처리 중..."
                      : inFlightAction === "edit"
                        ? "편집 내용 저장 중..."
                        : "제외(Exclude) 처리 중..."}
            </p>
            <p className="status-banner__detail">
              낙관적 업데이트를 수행하지 않으며, 서버 트랜잭션 커밋 응답이 도착한
              후에만 목록과 상태를 갱신합니다.
            </p>
          </div>
        )}

        {/* Action Feedback Banner */}
        {actionNotice && !activeError && (
          <div
            className="inline-alert inline-alert--info"
            role="status"
            aria-live="polite"
            data-testid="event-action-notice"
          >
            {actionNotice}
          </div>
        )}

        {/* ER-004: 409 EVENT_VERSION_CONFLICT Recovery Banner (Zero Auto-POST!) */}
        {activeError && activeError.kind === "version_conflict" && (
          <div
            className="status-banner status-banner--warning"
            role="alert"
            aria-live="assertive"
            data-testid="version-conflict-banner"
          >
            <div className="status-banner__top">
              <span className="badge badge--warning mono">
                409 EVENT_VERSION_CONFLICT
              </span>
              {activeError.conflictDetails && (
                <span className="mono run-id-chip" data-testid="conflict-latest-meta">
                  최신 버전: v{activeError.conflictDetails.current_version} · 상태:{" "}
                  {activeError.conflictDetails.current_status} · 허용 액션:{" "}
                  {activeError.conflictDetails.allowed_actions.join(", ") || "없음"}
                </span>
              )}
            </div>
            <p className="status-banner__headline">
              동시 수정 충돌이 감지되었습니다 — 자동 덮어쓰기·자동 재전송을 수행하지 않았습니다
            </p>
            <p className="status-banner__detail">{activeError.message}</p>
            <div className="action-row">
              <button
                type="button"
                className="btn btn--primary btn--sm"
                data-testid="reload-latest-after-conflict-button"
                onClick={() => void handleReloadAfterConflict()}
              >
                최신 상태 다시 불러오기
              </button>
            </div>
          </div>
        )}

        {/* Generic / CANNOT_LINK / Policy / Validation Error Banner */}
        {activeError && activeError.kind !== "version_conflict" && (
          <div
            className="status-banner status-banner--danger"
            role="alert"
            aria-live="assertive"
            data-testid="event-error-banner"
            data-error-code={activeError.code}
          >
            <div className="status-banner__top">
              <span className="badge badge--danger mono">{activeError.code}</span>
            </div>
            <p className="status-banner__headline">{activeError.message}</p>
          </div>
        )}

        {/* Global Policy Block / Deletion Pending Banner */}
        {isGlobalPolicyBlocked && (
          <div
            className="status-banner status-banner--danger"
            role="alert"
            data-testid="inbox-policy-blocked-banner"
          >
            <div className="status-banner__top">
              <span className="badge badge--danger">
                정책 차단 (POLICY_BLOCKED · 삭제 아님)
              </span>
            </div>
            <p className="status-banner__headline">
              공개→비공개 전환 등 정책 차단 상태로 모든 편집·확정·분할·병합·해제 액션이 차단되었습니다
            </p>
            <p className="status-banner__detail">
              {inboxNotice ??
                "GitHub 원본이 삭제된 것으로 분류하지 않으며, 정책 재확인 전까지 변경 명령을 수행할 수 없습니다."}
            </p>
          </div>
        )}

        {isGlobalDeletionPending && (
          <div
            className="status-banner status-banner--warning"
            role="alert"
            data-testid="inbox-deletion-pending-banner"
          >
            <div className="status-banner__top">
              <span className="badge badge--warning">
                삭제 진행 중 펜스 (DELETION_PENDING · OD-003)
              </span>
            </div>
            <p className="status-banner__headline">
              삭제 작업 진행 중으로 모든 Event 큐레이션 액션이 즉시 차단되었습니다
            </p>
            <p className="status-banner__detail">
              {inboxNotice ??
                "삭제 펜스가 활성화된 동안에는 확정·편집·제외·분할·병합·해제를 실행할 수 없습니다."}
            </p>
          </div>
        )}

        {/* Merge Selection Toolbar */}
        {checkedForMergeIds.length > 0 && (
          <div
            className="merge-toolbar"
            role="region"
            aria-label="USER_COMPOSITE 병합 선택 도구모음"
            data-testid="merge-selection-toolbar"
          >
            <div className="merge-toolbar__info">
              <strong>{checkedForMergeIds.length}개 Event 선택됨</strong>
              {!mergeValidation.valid && mergeValidation.reason && (
                <p
                  className="merge-toolbar__error"
                  role="alert"
                  data-testid="merge-validation-error"
                  data-validation-code={mergeValidation.code}
                >
                  {mergeValidation.reason}
                </p>
              )}
              {mergeValidation.valid && (
                <p className="field-help">
                  동일 저장소 활성 Event {selectedForMerge.length}건을 하나의{" "}
                  <code className="mono">USER_COMPOSITE</code>로 병합할 수
                  있습니다.
                </p>
              )}
            </div>
            <div className="action-row">
              <button
                type="button"
                className="btn btn--primary btn--sm"
                data-testid="open-merge-modal-button"
                disabled={
                  !mergeValidation.valid ||
                  inFlightAction !== null ||
                  isGlobalPolicyBlocked ||
                  isGlobalDeletionPending
                }
                onClick={() => setIsMergeModalOpen(true)}
              >
                선택한 Event 병합 (USER_COMPOSITE)
              </button>
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => setCheckedForMergeIds([])}
              >
                선택 해제
              </button>
            </div>
          </div>
        )}
      </section>

      {/* Distinct Empty State vs UNKNOWN Incomplete Association State */}
      {!isLoadingList &&
      items.length === 0 &&
      inboxState === "ASSOCIATION_UNKNOWN_INCOMPLETE" ? (
        <section
          className="status-banner status-banner--warning"
          role="alert"
          data-testid="inbox-association-unknown-state"
        >
          <div className="status-banner__top">
            <span className="badge badge--warning mono">
              PR_ASSOCIATION_UNKNOWN (연결 확인 미완료)
            </span>
          </div>
          <h3 className="status-banner__headline">
            PR-커밋 연결 확인이 완료되지 않아 Event 후보 승격이 보류되었습니다
          </h3>
          <p className="status-banner__detail">
            {inboxNotice ??
              "일부 기본 브랜치 커밋의 PR 연결 상태가 UNKNOWN으로 남아 있습니다. 이는 적격 PR·커밋이 0건인 정상 빈 상태와 다르며, 연결 확인 완료 후 다시 검토할 수 있습니다."}
          </p>
        </section>
      ) : !isLoadingList && items.length === 0 ? (
        <section className="empty-state" data-testid="inbox-empty-eligible-state">
          <p className="empty-state__title">
            {statusFilter === "ACTIVE"
              ? "검토할 적격 Event 후보가 0건입니다 (정상 빈 상태)"
              : `현재 필터(${statusFilter})에 해당하는 Event가 없습니다`}
          </p>
          <p className="empty-state__body">
            {statusFilter === "ACTIVE"
              ? inboxNotice ??
                "완료된 가져오기(SUCCESS) 기준으로 적격 병합 PR과 적격 기본 브랜치 직접 커밋이 모두 0건이었습니다."
              : "다른 상태 필터 탭을 선택하여 Event를 확인하세요."}
          </p>
        </section>
      ) : (
        /* Master-Detail Event Inbox Split Layout */
        <div className="event-inbox-grid">
          <section
            className="card event-list-pane"
            aria-label="Event 후보 목록"
            data-testid="event-list-pane"
          >
            <div className="subcard__header">
              <h3 className="subcard__title">
                Event 목록 ({items.length}건 표시)
              </h3>
              <span className="field-help">병합할 항목을 체크해 선택</span>
            </div>

            {isLoadingList ? (
              <p className="subcard__body">목록을 불러오는 중입니다...</p>
            ) : (
              <ul className="event-list" data-testid="event-list">
                {items.map((item) => {
                  const isSelected = item.event_id === selectedEventId;
                  const isChecked = checkedForMergeIds.includes(item.event_id);
                  const anchorBadge = formatAnchorModeBadge(item);
                  const statusBadge = formatEventStatusLabel(item.status);
                  const itemBlocked =
                    isEventMutationBlockedByPolicyOrDeletion(item);

                  return (
                    <li
                      key={item.event_id}
                      className={`event-list-item ${
                        isSelected ? "event-list-item--selected" : ""
                      }`}
                      data-testid={`event-list-item-${item.event_id}`}
                      data-anchor-mode={item.anchor_mode}
                      data-status={item.status}
                    >
                      <div className="event-list-item__check">
                        <input
                          type="checkbox"
                          aria-label={`${item.title} 병합 대상 선택`}
                          data-testid={`merge-checkbox-${item.event_id}`}
                          checked={isChecked}
                          disabled={
                            itemBlocked ||
                            inFlightAction !== null ||
                            item.status === "SUPERSEDED"
                          }
                          onChange={() => handleToggleMergeCheck(item.event_id)}
                        />
                      </div>

                      <button
                        type="button"
                        className="event-list-item__body"
                        data-testid={`select-event-${item.event_id}`}
                        onClick={() => handleSelectEvent(item.event_id)}
                      >
                        <div className="status-banner__badges">
                          <span className={`badge ${anchorBadge.badgeClass}`}>
                            {anchorBadge.label}
                          </span>
                          <span className={`badge ${statusBadge.badgeClass}`}>
                            {statusBadge.label}
                          </span>
                          <span className="badge badge--neutral mono">
                            v{item.version}
                          </span>
                        </div>

                        <span className="event-list-item__title">
                          {item.title}
                        </span>

                        {item.direct_commit_badge && (
                          <span
                            className="event-list-item__sub mono"
                            data-testid={`list-direct-commit-meta-${item.event_id}`}
                          >
                            branch: {item.direct_commit_badge.default_branch} ·{" "}
                            {item.direct_commit_badge.abbreviated_sha} · PR 연결
                            미관측(NO_MATCH_OBSERVED)
                          </span>
                        )}

                        <span className="event-list-item__meta mono">
                          직접 근거(MUST_LINK) {item.evidence_count}건 · 참고
                          문맥(CONTEXT_ONLY) {item.context_count}건
                        </span>

                        {item.review_flags.length > 0 && (
                          <div className="event-list-item__flags">
                            {item.review_flags.slice(0, 3).map((flag) => (
                              <span
                                key={flag}
                                className={`badge badge--${REVIEW_FLAG_CATALOG[flag].tone}`}
                              >
                                {REVIEW_FLAG_CATALOG[flag].label}
                              </span>
                            ))}
                            {item.review_flags.length > 3 && (
                              <span className="badge badge--neutral">
                                +{item.review_flags.length - 3}
                              </span>
                            )}
                          </div>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <EventDetailPanel
            event={selectedDetail}
            isLoadingDetail={isLoadingDetail}
            isMutating={inFlightAction !== null}
            inFlightAction={inFlightAction}
            onConfirmKeep={(ev) => void handleConfirmKeep(ev)}
            onSubmitEdit={(params) => void handleSubmitEdit(params)}
            onConfirmExclude={(params) => void handleConfirmExclude(params)}
            onOpenSplitModal={() => setIsSplitModalOpen(true)}
            onOpenDissolveModal={() => setIsDissolveModalOpen(true)}
          />
        </div>
      )}

      {/* T-304B Pre-Submit Merge Confirmation Modal */}
      {isMergeModalOpen && (
        <MergeConfirmModal
          selectedEvents={selectedForMerge}
          affectedImpact={mergeValidation.affectedImpact}
          isSubmitting={inFlightAction === "merge"}
          onCancel={() => setIsMergeModalOpen(false)}
          onConfirmMerge={(params) => void handleConfirmMerge(params)}
        />
      )}

      {/* Split Modal */}
      {isSplitModalOpen && selectedDetail && (
        <SplitEventModal
          event={selectedDetail}
          isSubmitting={inFlightAction === "split"}
          onCancel={() => setIsSplitModalOpen(false)}
          onConfirmSplit={(params) => void handleConfirmSplit(params)}
        />
      )}

      {/* Dissolve Modal */}
      {isDissolveModalOpen && selectedDetail && (
        <DissolveConfirmModal
          event={selectedDetail}
          isSubmitting={inFlightAction === "dissolve"}
          onCancel={() => setIsDissolveModalOpen(false)}
          onConfirmDissolve={(reason) => void handleConfirmDissolve(reason)}
        />
      )}
    </div>
  );

  if (embedded) {
    return bodyContent;
  }

  return (
    <div className="page-shell">
      <a href="#event-inbox-main" className="skip-link">
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
                <p className="brand-kicker">Career Memory · M3 Event Inbox</p>
                <h1 className="page-title">
                  Event 후보 검토 및 큐레이션 (Event Review Inbox)
                </h1>
              </div>
            </div>
            <div className="action-row">
              {onBackToImport ? (
                <button
                  type="button"
                  className="btn btn--secondary btn--sm"
                  onClick={onBackToImport}
                >
                  가져오기 화면으로 돌아가기
                </button>
              ) : (
                <a href="/" className="btn btn--secondary btn--sm">
                  가져오기 화면으로 돌아가기
                </a>
              )}
            </div>
          </div>
          <p className="page-lead">
            완료된 가져오기(SUCCESS · import_ready=true)에서 추출된 PR 및 기본
            브랜치 직접 커밋 Event 후보를 검토하고 확정·편집·제외·분할·병합·해제합니다.
          </p>
        </div>
      </header>

      <main
        id="event-inbox-main"
        className="main-content main-content--wide"
        tabIndex={-1}
      >
        {bodyContent}
      </main>
    </div>
  );
}

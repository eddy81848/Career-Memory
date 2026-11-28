"use client";

import React, { useState } from "react";
import type {
  SplitPartitionInput,
  WorkEventDetailResponse,
} from "@/domain/event-review-models";
import { validateSplitPartitions } from "@/domain/event-review-presenter";

interface SplitEventModalProps {
  event: WorkEventDetailResponse;
  isSubmitting: boolean;
  onCancel: () => void;
  onConfirmSplit: (params: {
    partitions: SplitPartitionInput[];
    reason: string;
  }) => void;
}

export function SplitEventModal({
  event,
  isSubmitting,
  onCancel,
  onConfirmSplit,
}: SplitEventModalProps) {
  const evidenceItems = event.evidence_memberships;
  const midpoint = Math.max(1, Math.floor(evidenceItems.length / 2));

  const [part1Title, setPart1Title] = useState(`${event.title} — 범위 A`);
  const [part2Title, setPart2Title] = useState(`${event.title} — 범위 B`);
  const [part1Ids, setPart1Ids] = useState<string[]>(
    evidenceItems.slice(0, midpoint).map((m) => m.source_object_id)
  );
  const [part2Ids, setPart2Ids] = useState<string[]>(
    evidenceItems.slice(midpoint).map((m) => m.source_object_id)
  );
  const [reason, setReason] = useState("서로 다른 목적의 변경 범위를 독립 Event로 분할");

  const toggleAssignment = (
    partitionIndex: 1 | 2,
    sourceObjectId: string
  ) => {
    if (partitionIndex === 1) {
      setPart1Ids((prev) =>
        prev.includes(sourceObjectId)
          ? prev.filter((id) => id !== sourceObjectId)
          : [...prev, sourceObjectId]
      );
    } else {
      setPart2Ids((prev) =>
        prev.includes(sourceObjectId)
          ? prev.filter((id) => id !== sourceObjectId)
          : [...prev, sourceObjectId]
      );
    }
  };

  const partitions: SplitPartitionInput[] = [
    {
      title: part1Title,
      source_object_ids: part1Ids,
      anchor_scope: {
        file_paths: Array.from(
          new Set(
            evidenceItems
              .filter((m) => part1Ids.includes(m.source_object_id))
              .flatMap((m) => m.file_paths ?? [])
          )
        ),
      },
    },
    {
      title: part2Title,
      source_object_ids: part2Ids,
      anchor_scope: {
        file_paths: Array.from(
          new Set(
            evidenceItems
              .filter((m) => part2Ids.includes(m.source_object_id))
              .flatMap((m) => m.file_paths ?? [])
          )
        ),
      },
    },
  ];

  const validation = validateSplitPartitions(
    partitions,
    evidenceItems.map((m) => m.source_object_id)
  );

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="split-modal-title"
      data-testid="split-event-modal"
    >
      <div className="modal-card">
        <div className="modal-card__header">
          <div>
            <span className="badge badge--info">Split (범위 분할 · PR_SEGMENT)</span>
            <h3 id="split-modal-title" className="section-title">
              소속 근거(MUST_LINK)를 서로 겹치지 않는 2개 이상의 범위로 분할
            </h3>
          </div>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={onCancel}
            disabled={isSubmitting}
          >
            닫기
          </button>
        </div>

        <p className="subcard__body">
          원래 Event(<code className="mono">{event.event_id} · v{event.version}</code>)는{" "}
          <code className="mono">SUPERSEDED</code> 이력으로 보존되고, 각 분할은 독립된{" "}
          <code className="mono">PR_SEGMENT</code> 후보로 생성됩니다.
        </p>

        <div className="split-grid">
          <div className="subcard" data-testid="split-partition-1">
            <label htmlFor="split-part1-title" className="field-label">
              분할 #1 제목
            </label>
            <input
              id="split-part1-title"
              type="text"
              className="text-input"
              data-testid="split-part1-title"
              value={part1Title}
              disabled={isSubmitting}
              onChange={(e) => setPart1Title(e.target.value)}
            />
            <span className="field-label">분할 #1 소속 근거 선택</span>
            <ul className="checkbox-list">
              {evidenceItems.map((item) => (
                <li key={`p1-${item.source_object_id}`}>
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={part1Ids.includes(item.source_object_id)}
                      disabled={isSubmitting}
                      onChange={() => toggleAssignment(1, item.source_object_id)}
                    />
                    <span>
                      <code className="mono">{item.provider_object_id}</code> —{" "}
                      {item.title}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </div>

          <div className="subcard" data-testid="split-partition-2">
            <label htmlFor="split-part2-title" className="field-label">
              분할 #2 제목
            </label>
            <input
              id="split-part2-title"
              type="text"
              className="text-input"
              data-testid="split-part2-title"
              value={part2Title}
              disabled={isSubmitting}
              onChange={(e) => setPart2Title(e.target.value)}
            />
            <span className="field-label">분할 #2 소속 근거 선택</span>
            <ul className="checkbox-list">
              {evidenceItems.map((item) => (
                <li key={`p2-${item.source_object_id}`}>
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={part2Ids.includes(item.source_object_id)}
                      disabled={isSubmitting}
                      onChange={() => toggleAssignment(2, item.source_object_id)}
                    />
                    <span>
                      <code className="mono">{item.provider_object_id}</code> —{" "}
                      {item.title}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </div>
        </div>

        <div className="form-field">
          <label htmlFor="split-reason-input" className="field-label">
            분할 사유
          </label>
          <input
            id="split-reason-input"
            type="text"
            className="text-input"
            data-testid="split-reason-input"
            value={reason}
            disabled={isSubmitting}
            onChange={(e) => setReason(e.target.value)}
          />
        </div>

        {!validation.valid && validation.reason && (
          <div
            className="inline-alert inline-alert--danger"
            role="alert"
            data-testid="split-validation-error"
          >
            {validation.reason}
          </div>
        )}

        <div className="action-row modal-card__footer">
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={onCancel}
            disabled={isSubmitting}
          >
            취소
          </button>
          <button
            type="button"
            className="btn btn--primary btn--sm"
            data-testid="confirm-split-submit-button"
            disabled={!validation.valid || isSubmitting}
            onClick={() =>
              onConfirmSplit({
                partitions,
                reason: reason.trim(),
              })
            }
          >
            {isSubmitting ? "분할 처리 중..." : "범위 분할 확정 실행"}
          </button>
        </div>
      </div>
    </div>
  );
}

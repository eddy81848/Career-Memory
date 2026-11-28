"use client";

import React from "react";
import type { EphemeralResolutionOverrideMode } from "@/adapters/claim-review-adapter";
import type { ClaimReviewScenarioId } from "@/domain/claim-review-models";
import { CLAIM_REVIEW_SCENARIOS } from "@/domain/claim-review-presenter";

interface ClaimReviewScenarioBarProps {
  scenarioId: ClaimReviewScenarioId;
  ephemeralOverride: EphemeralResolutionOverrideMode;
  selectedClaimId: string | null;
  disabled: boolean;
  onScenarioChange: (scenarioId: ClaimReviewScenarioId) => void;
  onEphemeralOverrideChange: (mode: EphemeralResolutionOverrideMode) => void;
  onSimulateConcurrentClaimUpdate: () => void;
  onReset: () => void;
}

const EPHEMERAL_OVERRIDE_OPTIONS: Array<{
  value: EphemeralResolutionOverrideMode;
  label: string;
}> = [
  { value: "DEFAULT", label: "기본 (Unit별 설정: MATCHED / CHANGED / 404)" },
  { value: "MATCHED", label: "MATCHED (200 · 해시 일치 + 임시 원문)" },
  { value: "REVISION_MISMATCH", label: "CHANGED · REVISION_MISMATCH (409 드리프트)" },
  { value: "INVALID_LOCATOR", label: "CHANGED · INVALID_LOCATOR (409 위치 불일치)" },
  {
    value: "UNAVAILABLE_RETRYABLE_503",
    label: "UNAVAILABLE · 503/429 일시 오류 (재시도 가능)",
  },
  {
    value: "UNAVAILABLE_PROVIDER_404",
    label: "UNAVAILABLE · 공급자 404 (사용자 삭제·허위 단정 아님)",
  },
  { value: "POLICY_BLOCKED", label: "POLICY_BLOCKED (403 · 정책 차단)" },
  { value: "PARTIAL_WITH_LIMIT", label: "PARTIAL_WITH_LIMIT (422 · 조회 한도)" },
];

export function ClaimReviewScenarioBar({
  scenarioId,
  ephemeralOverride,
  selectedClaimId,
  disabled,
  onScenarioChange,
  onEphemeralOverrideChange,
  onSimulateConcurrentClaimUpdate,
  onReset,
}: ClaimReviewScenarioBarProps) {
  const activeMeta = CLAIM_REVIEW_SCENARIOS.find(
    (item) => item.id === scenarioId
  );

  return (
    <aside
      aria-label="Claim Review 로컬 프로토타입 시나리오 제어판"
      className="prototype-bar"
      data-testid="claim-review-scenario-bar"
    >
      <div className="prototype-bar__header">
        <span className="badge badge--neutral">
          M4 Claim Review · 로컬 UI Fixture 제어판
        </span>
        <p className="prototype-bar__disclaimer">
          Plan C 임시 원문 뷰어(6상태), 409 충돌 복구, MANUAL_ONLY_BLOCKED, 정책/삭제
          펜스 잠금을 즉시 검증할 수 있습니다.
        </p>
      </div>

      <div className="prototype-bar__controls">
        <div className="prototype-bar__select-wrap">
          <label
            htmlFor="claim-review-scenario-select"
            className="field-label"
          >
            Claim Review 시나리오 (UI Fixture)
          </label>
          <select
            id="claim-review-scenario-select"
            className="select-input"
            data-testid="claim-review-scenario-select"
            value={scenarioId}
            disabled={disabled}
            onChange={(e) =>
              onScenarioChange(e.target.value as ClaimReviewScenarioId)
            }
          >
            {CLAIM_REVIEW_SCENARIOS.map((scenario) => (
              <option key={scenario.id} value={scenario.id}>
                {scenario.label}
              </option>
            ))}
          </select>
          {activeMeta && (
            <p className="field-help">{activeMeta.shortDescription}</p>
          )}
        </div>

        <div className="prototype-bar__select-wrap">
          <label
            htmlFor="ephemeral-resolution-override-select"
            className="field-label"
          >
            Plan C 임시 원문 응답 시뮬레이션
          </label>
          <select
            id="ephemeral-resolution-override-select"
            className="select-input"
            data-testid="ephemeral-resolution-override-select"
            value={ephemeralOverride}
            disabled={disabled}
            onChange={(e) =>
              onEphemeralOverrideChange(
                e.target.value as EphemeralResolutionOverrideMode
              )
            }
          >
            {EPHEMERAL_OVERRIDE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>

        <div className="prototype-bar__actions">
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            data-testid="simulate-concurrent-claim-update-button"
            disabled={disabled || !selectedClaimId}
            onClick={onSimulateConcurrentClaimUpdate}
          >
            선택 Claim 동시 수정 주입 (409 유발)
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            data-testid="reset-claim-review-button"
            disabled={disabled}
            onClick={onReset}
          >
            Claim Review 초기화
          </button>
        </div>
      </div>
    </aside>
  );
}

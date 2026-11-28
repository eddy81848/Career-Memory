"use client";

import React from "react";
import type { EventReviewScenarioId } from "@/domain/event-review-models";
import { EVENT_REVIEW_SCENARIOS } from "@/domain/event-review-presenter";

interface EventReviewScenarioBarProps {
  scenarioId: EventReviewScenarioId;
  selectedEventId: string | null;
  disabled: boolean;
  onScenarioChange: (scenarioId: EventReviewScenarioId) => void;
  onSimulateConcurrentUpdate: () => void;
  onReset: () => void;
}

export function EventReviewScenarioBar({
  scenarioId,
  selectedEventId,
  disabled,
  onScenarioChange,
  onSimulateConcurrentUpdate,
  onReset,
}: EventReviewScenarioBarProps) {
  const activeMeta = EVENT_REVIEW_SCENARIOS.find(
    (item) => item.id === scenarioId
  );

  return (
    <aside
      aria-label="Event Inbox 로컬 프로토타입 시나리오 제어판"
      className="prototype-bar"
      data-testid="event-review-scenario-bar"
    >
      <div className="prototype-bar__header">
        <span className="badge badge--neutral">
          Event Inbox · 로컬 UI Fixture 제어판
        </span>
        <p className="prototype-bar__disclaimer">
          M3 Event 검토·큐레이션 UX 및 409 충돌·정책 차단·병합/해제 시나리오를
          즉시 검증할 수 있습니다.
        </p>
      </div>

      <div className="prototype-bar__controls">
        <div className="prototype-bar__select-wrap">
          <label
            htmlFor="event-review-scenario-select"
            className="field-label"
          >
            Event Inbox 시나리오 (UI Fixture)
          </label>
          <select
            id="event-review-scenario-select"
            className="select-input"
            data-testid="event-review-scenario-select"
            value={scenarioId}
            disabled={disabled}
            onChange={(event) =>
              onScenarioChange(event.target.value as EventReviewScenarioId)
            }
          >
            {EVENT_REVIEW_SCENARIOS.map((scenario) => (
              <option key={scenario.id} value={scenario.id}>
                {scenario.label}
              </option>
            ))}
          </select>
          {activeMeta && (
            <p className="field-help">{activeMeta.shortDescription}</p>
          )}
        </div>

        <div className="prototype-bar__actions">
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            data-testid="simulate-concurrent-update-button"
            disabled={disabled || !selectedEventId}
            onClick={onSimulateConcurrentUpdate}
          >
            선택 Event 동시 수정 주입 (409 유발)
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            data-testid="reset-event-inbox-button"
            disabled={disabled}
            onClick={onReset}
          >
            Event Inbox 초기화
          </button>
        </div>
      </div>
    </aside>
  );
}

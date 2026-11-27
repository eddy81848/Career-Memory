"use client";

import React from "react";
import { PROTOTYPE_SCENARIOS } from "@/adapters/fixtures";
import type { PrototypeScenarioId } from "@/domain/view-models";

interface PrototypeScenarioBarProps {
  scenarioId: PrototypeScenarioId;
  disabled: boolean;
  onScenarioChange: (scenarioId: PrototypeScenarioId) => void;
  onSimulateReloadRecovery: () => void;
  onSimulatePollNetworkError: () => void;
  onResetPrototype: () => void;
}

export function PrototypeScenarioBar({
  scenarioId,
  disabled,
  onScenarioChange,
  onSimulateReloadRecovery,
  onSimulatePollNetworkError,
  onResetPrototype,
}: PrototypeScenarioBarProps) {
  const activeMeta = PROTOTYPE_SCENARIOS.find((item) => item.id === scenarioId);

  return (
    <aside
      aria-label="로컬 프로토타입 UI Fixture 시나리오 제어판"
      className="prototype-bar"
    >
      <div className="prototype-bar__header">
        <span className="badge badge--neutral">로컬 UI Fixture 프로토타입</span>
        <p className="prototype-bar__disclaimer">
          실제 로그인·GitHub API·백엔드 Worker가 연결되지 않은 모의 UI입니다.
          아래 시나리오 선택기는 로컬 검증 전용입니다.
        </p>
      </div>

      <div className="prototype-bar__controls">
        <div className="prototype-bar__select-wrap">
          <label htmlFor="prototype-scenario-select" className="field-label">
            응답 시나리오 (UI Fixture)
          </label>
          <select
            id="prototype-scenario-select"
            className="select-input"
            value={scenarioId}
            disabled={disabled}
            onChange={(event) =>
              onScenarioChange(event.target.value as PrototypeScenarioId)
            }
          >
            {PROTOTYPE_SCENARIOS.map((scenario) => (
              <option key={scenario.id} value={scenario.id}>
                {scenario.label}
              </option>
            ))}
          </select>
          {activeMeta && (
            <p className="field-help" id="prototype-scenario-help">
              {activeMeta.shortDescription}
            </p>
          )}
        </div>

        <div className="prototype-bar__actions">
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            onClick={onSimulateReloadRecovery}
            disabled={disabled}
          >
            새로고침 복구 확인
          </button>
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            onClick={onSimulatePollNetworkError}
            disabled={disabled}
          >
            다음 조회 네트워크 오류 주입
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={onResetPrototype}
            disabled={disabled}
          >
            진행 결과 초기화
          </button>
        </div>
      </div>
    </aside>
  );
}

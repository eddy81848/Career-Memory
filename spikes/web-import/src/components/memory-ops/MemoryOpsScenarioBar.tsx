"use client";

import React from "react";
import type { MemoryOpsScenarioId } from "@/domain/memory-ops-models";
import { MEMORY_OPS_SCENARIOS } from "@/domain/memory-ops-presenter";

interface MemoryOpsScenarioBarProps {
  scenarioId: MemoryOpsScenarioId;
  repoCountMode: "single" | "multi";
  disabled: boolean;
  onScenarioChange: (scenarioId: MemoryOpsScenarioId) => void;
  onRepoCountModeChange: (mode: "single" | "multi") => void;
  onReset: () => void;
}

export function MemoryOpsScenarioBar({
  scenarioId,
  repoCountMode,
  disabled,
  onScenarioChange,
  onRepoCountModeChange,
  onReset,
}: MemoryOpsScenarioBarProps) {
  const activeMeta = MEMORY_OPS_SCENARIOS.find(
    (item) => item.id === scenarioId
  );

  return (
    <aside
      aria-label="M5 Memory Ops 로컬 프로토타입 시나리오 제어판"
      className="prototype-bar"
      data-testid="memory-ops-scenario-bar"
    >
      <div className="prototype-bar__header">
        <span className="badge badge--neutral">
          M5 Memory Ops · 로컬 UI Fixture 제어판
        </span>
        <p className="prototype-bar__disclaimer">
          OD-008 워크스페이스 검색, 커서 버전 드리프트(400), OD-003 삭제 영향 범위
          및 409/PARTIAL/DELETED 영수증, 80%/95% 한도·일시정지 보호를 즉시 검증할
          수 있습니다.
        </p>
      </div>

      <div className="prototype-bar__controls">
        <div className="prototype-bar__select-wrap">
          <label
            htmlFor="memory-ops-scenario-select"
            className="field-label"
          >
            Memory Ops 시나리오 (UI Fixture)
          </label>
          <select
            id="memory-ops-scenario-select"
            className="select-input"
            data-testid="memory-ops-scenario-select"
            value={scenarioId}
            disabled={disabled}
            onChange={(e) =>
              onScenarioChange(e.target.value as MemoryOpsScenarioId)
            }
          >
            {MEMORY_OPS_SCENARIOS.map((scenario) => (
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
          <label htmlFor="memory-ops-repo-mode-select" className="field-label">
            연결 저장소 수 모드 (OD-008 자동 범위 선택 검증)
          </label>
          <select
            id="memory-ops-repo-mode-select"
            className="select-input"
            data-testid="memory-ops-repo-mode-select"
            value={repoCountMode}
            disabled={disabled}
            onChange={(e) =>
              onRepoCountModeChange(e.target.value as "single" | "multi")
            }
          >
            <option value="multi">
              다중 저장소 연결 (2개 · 명시적 선택기 표시)
            </option>
            <option value="single">
              단일 저장소 연결 (1개 · source_resource_id 자동 선택)
            </option>
          </select>
        </div>

        <div className="prototype-bar__actions">
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            data-testid="reset-memory-ops-button"
            disabled={disabled}
            onClick={onReset}
          >
            Memory Ops 초기화
          </button>
        </div>
      </div>
    </aside>
  );
}

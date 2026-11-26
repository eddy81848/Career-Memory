import { describe, expect, it } from "vitest";
import {
  SYNTHETIC_AUTHENTICATED_AUTH,
  SYNTHETIC_CONNECTED_GITHUB,
  SYNTHETIC_PUBLIC_REPOSITORIES,
  SYNTHETIC_REVOKED_GITHUB,
} from "@/adapters/fixtures";
import {
  canExposeRetryAction,
  canSubmitInitialImport,
  createInitialImportJourneyState,
  hasDerivedStageLag,
  importJourneyReducer,
  isFullSuccessRun,
  isImportRequestLocked,
} from "./state-machine";
import type { ImportRunViewModel } from "./view-models";

/**
 * NOTE: These unit tests verify pure UI view-model state transitions and
 * display guards using local UI fixtures — NOT HTTP contracts or backend logic.
 */

function makeRun(overrides: Partial<ImportRunViewModel>): ImportRunViewModel {
  return {
    runId: "run_demo_101",
    repositoryId: SYNTHETIC_PUBLIC_REPOSITORIES[0].repoId,
    repositoryFullName: SYNTHETIC_PUBLIC_REPOSITORIES[0].fullName,
    status: "succeeded",
    attemptNumber: 1,
    startedAt: "2026-11-26T12:00:00Z",
    updatedAt: "2026-11-26T12:00:10Z",
    progressMessage: "완료",
    readiness: {
      importReady: true,
      importReadyState: "ready",
      importReadySummary: "원본 스냅샷 완료",
      derivedAllReady: true,
      derivedStages: [
        {
          key: "event_normalization",
          label: "작업 이벤트 정규화",
          status: "ready",
          detail: "완료",
        },
        {
          key: "timeline_indexing",
          label: "타임라인 준비",
          status: "ready",
          detail: "완료",
        },
      ],
    },
    retryPolicy: {
      allowed: false,
      reason: "이미 성공 완료",
      nextAllowedAt: null,
    },
    summary: {
      commitsCaptured: 142,
      pullRequestsCaptured: 28,
      capturedAt: "2026-11-26T12:00:10Z",
    },
    partialDetail: null,
    limitDetail: null,
    error: null,
    ...overrides,
  };
}

describe("importJourneyReducer & domain guards (UI view-model fixtures)", () => {
  it("does NOT optimistically confirm a run when SUBMIT_IMPORT_START is dispatched", () => {
    const bootstrapped = importJourneyReducer(
      createInitialImportJourneyState(),
      {
        type: "BOOTSTRAP_SUCCESS",
        payload: {
          serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
          githubConnection: SYNTHETIC_CONNECTED_GITHUB,
          repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
          latestRun: null,
          scenarioId: "success_full",
          recoveredFromReload: false,
          checkedAt: "2026-11-26T12:00:00Z",
        },
      }
    );

    expect(canSubmitInitialImport(bootstrapped)).toBe(true);

    const submitting = importJourneyReducer(bootstrapped, {
      type: "SUBMIT_IMPORT_START",
      label: "서버 응답 대기 중",
    });

    expect(submitting.phase).toBe("submitting_import");
    expect(submitting.activeRun).toBeNull();
    expect(isImportRequestLocked(submitting)).toBe(true);
    expect(canSubmitInitialImport(submitting)).toBe(false);
  });

  it("exposes retry action ONLY when server retryPolicy.allowed is true and connection is valid", () => {
    const base = importJourneyReducer(createInitialImportJourneyState(), {
      type: "BOOTSTRAP_SUCCESS",
      payload: {
        serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
        githubConnection: SYNTHETIC_CONNECTED_GITHUB,
        repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
        latestRun: makeRun({
          status: "failed",
          retryPolicy: {
            allowed: true,
            reason: "일시 오류",
            nextAllowedAt: null,
          },
        }),
        scenarioId: "error_retryable",
        recoveredFromReload: false,
        checkedAt: "2026-11-26T12:00:00Z",
      },
    });

    expect(canExposeRetryAction(base)).toBe(true);

    const retryDenied = importJourneyReducer(base, {
      type: "RUN_UPDATED_FROM_SERVER",
      run: makeRun({
        status: "failed",
        retryPolicy: {
          allowed: false,
          reason: "영구 오류",
          nextAllowedAt: null,
        },
      }),
    });
    expect(canExposeRetryAction(retryDenied)).toBe(false);

    const revoked = importJourneyReducer(base, {
      type: "RUN_UPDATED_FROM_SERVER",
      run: makeRun({
        status: "permission_revoked",
        retryPolicy: {
          allowed: true, // Even if misconfigured, permission_revoked must never allow direct retry
          reason: "권한 회수",
          nextAllowedAt: null,
        },
      }),
      githubConnection: SYNTHETIC_REVOKED_GITHUB,
    });
    expect(canExposeRetryAction(revoked)).toBe(false);
  });

  it("never classifies partial, limit_reached, or failed runs as full success", () => {
    expect(isFullSuccessRun(makeRun({ status: "succeeded" }))).toBe(true);
    expect(isFullSuccessRun(makeRun({ status: "partial" }))).toBe(false);
    expect(isFullSuccessRun(makeRun({ status: "limit_reached" }))).toBe(false);
    expect(isFullSuccessRun(makeRun({ status: "failed" }))).toBe(false);
    expect(isFullSuccessRun(makeRun({ status: "permission_revoked" }))).toBe(
      false
    );
  });

  it("detects when import_ready is true while derived stages are incomplete", () => {
    const derivedPendingRun = makeRun({
      status: "succeeded",
      readiness: {
        importReady: true,
        importReadyState: "ready",
        importReadySummary: "원본 스냅샷 준비 완료",
        derivedAllReady: false,
        derivedStages: [
          {
            key: "event_normalization",
            label: "작업 이벤트 정규화",
            status: "ready",
            detail: "완료",
          },
          {
            key: "timeline_indexing",
            label: "타임라인 준비",
            status: "failed",
            detail: "실패",
          },
        ],
      },
    });

    expect(hasDerivedStageLag(derivedPendingRun)).toBe(true);
  });

  it("creates reload recovery notice when restoring an active running job", () => {
    const restored = importJourneyReducer(createInitialImportJourneyState(), {
      type: "BOOTSTRAP_SUCCESS",
      payload: {
        serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
        githubConnection: SYNTHETIC_CONNECTED_GITHUB,
        repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
        latestRun: makeRun({ status: "running" }),
        scenarioId: "running_slow_for_reload",
        recoveredFromReload: true,
        checkedAt: "2026-11-26T12:01:00Z",
      },
    });

    expect(restored.recoveryNotice?.kind).toBe("restored_running");
    expect(restored.recoveryNotice?.message).toContain("중복 요청을 다시 보내지 않고");
    expect(isImportRequestLocked(restored)).toBe(true);
  });
});

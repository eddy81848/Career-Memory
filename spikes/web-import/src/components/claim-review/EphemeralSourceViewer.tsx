"use client";

import React, { useState } from "react";
import type { ClaimReviewAdapter } from "@/adapters/claim-review-adapter";
import type { SpanLocatorV1 } from "@/domain/claim-review-models";
import {
  buildEphemeralViewerStateFromError,
  buildEphemeralViewerStateFromSuccess,
  type EphemeralViewerViewModel,
} from "@/domain/claim-review-presenter";

/**
 * Plan C (ADR-013 / REF-04) Ephemeral Source Viewer.
 *
 * CRITICAL INVARIANTS:
 * - Metadata-only mode renders cleanly BEFORE any remote/ephemeral source fetch.
 * - Clicking "View Ephemeral Source" triggers `GET /v1/content-units/{id}/content`.
 * - Ephemeral source text lives strictly in React component memory (`useState`) ONLY —
 *   NEVER written to localStorage, sessionStorage, IndexedDB, URL query params, or console logs!
 * - Distinctly renders all 6 resolution states:
 *   Loading | Matched | Changed (REVISION_MISMATCH / INVALID_LOCATOR) |
 *   Unavailable (retryable 429/503/timeout vs provider 404) | Policy blocked | Limit.
 */

interface EphemeralSourceViewerProps {
  adapter: ClaimReviewAdapter;
  workspaceId: string;
  contentUnitId: string;
  unitType: string;
  locator: SpanLocatorV1 | null;
  revisionContentHash: string | null;
  revisionObservedAt: string | null;
  lastCheckedAt: string | null;
  disabledByPolicyOrFence?: boolean;
}

export function EphemeralSourceViewer({
  adapter,
  workspaceId,
  contentUnitId,
  unitType,
  locator,
  revisionContentHash,
  revisionObservedAt,
  lastCheckedAt,
  disabledByPolicyOrFence = false,
}: EphemeralSourceViewerProps) {
  // STRICTLY IN-MEMORY REACT STATE ONLY (zero storage / URL / console writes)
  const [viewerState, setViewerState] = useState<EphemeralViewerViewModel | null>(
    null
  );
  const [isLoading, setIsLoading] = useState(false);

  const handleFetchEphemeralSource = async () => {
    setIsLoading(true);
    setViewerState({
      uiState: "LOADING",
      badgeLabel: "원문 재조회 중 (LOADING)",
      badgeClass: "badge--info",
      bannerTone: "info",
      headline: "원본 공급자에서 임시 원문을 실시간 검증 조회 중입니다...",
      explanation:
        "Plan C 정책에 따라 DB에 저장된 원문은 없으며, 리비전 해시와 스팬 위치(cm-locator-v1)를 실시간 대조합니다.",
      canRetry: false,
      resolution: null,
      content: null,
      context: null,
    });

    const result = await adapter.getContentUnitContent({
      content_unit_id: contentUnitId,
      workspace_id: workspaceId,
      include_context: true,
    });

    setIsLoading(false);
    if (result.ok) {
      setViewerState(buildEphemeralViewerStateFromSuccess(result.data));
    } else {
      setViewerState(buildEphemeralViewerStateFromError(result.error));
    }
  };

  const handleClearFromMemory = () => {
    setViewerState(null);
  };

  const uiState = isLoading
    ? "LOADING"
    : viewerState?.uiState ?? "IDLE_METADATA_ONLY";

  return (
    <div
      className="ephemeral-viewer"
      data-testid={`ephemeral-viewer-${contentUnitId}`}
      data-ephemeral-state={uiState}
    >
      {/* Metadata-only summary (always works without fetching raw source) */}
      <div className="ephemeral-viewer__bar">
        <div className="ephemeral-viewer__meta mono">
          <span>
            Unit: <strong>{contentUnitId}</strong> ({unitType})
          </span>
          {locator?.file_path && <span> · {locator.file_path}</span>}
          {locator?.section && !locator?.file_path && (
            <span> · §{locator.section}</span>
          )}
          {locator?.start_line != null && (
            <span>
              {" "}
              · L{locator.start_line}
              {locator.end_line != null ? `-L${locator.end_line}` : ""}
            </span>
          )}
          {locator?.start_char != null && (
            <span>
              {" "}
              (chars {locator.start_char}..{locator.end_char ?? "?"})
            </span>
          )}
        </div>

        <div className="action-row">
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            data-testid={`view-ephemeral-source-${contentUnitId}`}
            disabled={isLoading || disabledByPolicyOrFence}
            onClick={() => void handleFetchEphemeralSource()}
          >
            {isLoading ? "조회 중..." : "View Ephemeral Source"}
          </button>
          {viewerState && !isLoading && (
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              data-testid={`clear-ephemeral-source-${contentUnitId}`}
              onClick={handleClearFromMemory}
            >
              메모리에서 닫기
            </button>
          )}
        </div>
      </div>

      {/* Provenance locator & hash metadata strip */}
      <div className="ephemeral-viewer__hashes mono">
        {locator?.span_sha256 && (
          <span>span_sha256: {locator.span_sha256.slice(0, 26)}...</span>
        )}
        {revisionContentHash && (
          <span>revision_hash: {revisionContentHash.slice(0, 26)}...</span>
        )}
        {revisionObservedAt && <span>observed_at: {revisionObservedAt}</span>}
        {lastCheckedAt && <span>last_checked_at: {lastCheckedAt}</span>}
      </div>

      {/* 6 Distinct Resolution States */}
      {viewerState && (
        <div
          className={`status-banner status-banner--${viewerState.bannerTone} ephemeral-viewer__result`}
          role={
            viewerState.uiState === "LOADING" || viewerState.uiState === "MATCHED"
              ? "status"
              : "alert"
          }
          data-testid={`ephemeral-result-${contentUnitId}`}
          data-resolution-ui-state={viewerState.uiState}
          data-resolution-status={viewerState.resolution?.status ?? viewerState.uiState}
        >
          <div className="status-banner__top">
            <span className={`badge ${viewerState.badgeClass}`}>
              {viewerState.badgeLabel}
            </span>
            {viewerState.resolution && (
              <span className="mono run-id-chip">
                checked_at: {viewerState.resolution.checked_at || "now"}
                {viewerState.resolution.reason_code
                  ? ` · ${viewerState.resolution.reason_code}`
                  : ""}
              </span>
            )}
          </div>

          <p className="status-banner__headline">{viewerState.headline}</p>
          <p className="status-banner__detail">{viewerState.explanation}</p>

          {/* Hash comparison for CHANGED / MATCHED */}
          {viewerState.resolution &&
            (viewerState.resolution.expected_revision_hash ||
              viewerState.resolution.observed_revision_hash) && (
              <p className="status-banner__meta mono">
                expected_hash:{" "}
                {viewerState.resolution.expected_revision_hash ?? "N/A"} |
                observed_hash:{" "}
                {viewerState.resolution.observed_revision_hash ?? "N/A"}
              </p>
            )}

          {/* MATCHED ephemeral text & context callout (strictly memory-only!) */}
          {viewerState.uiState === "MATCHED" && viewerState.content !== null && (
            <div
              className="ephemeral-code-box"
              data-testid={`ephemeral-content-box-${contentUnitId}`}
            >
              <div className="ephemeral-code-box__header">
                <span className="badge badge--success">
                  Plan C 메모리 전용 원문 (영구 저장·캐시·로그 금지)
                </span>
                {viewerState.context?.file_path && (
                  <span className="mono field-help">
                    {viewerState.context.file_path}
                    {viewerState.context.hunk_header
                      ? ` ${viewerState.context.hunk_header}`
                      : ""}
                  </span>
                )}
              </div>
              {viewerState.context?.preceding_text && (
                <pre className="ephemeral-pre ephemeral-pre--context">
                  {viewerState.context.preceding_text}
                </pre>
              )}
              <pre
                className="ephemeral-pre"
                data-testid={`ephemeral-raw-text-${contentUnitId}`}
              >
                {viewerState.content}
              </pre>
              {viewerState.context?.following_text && (
                <pre className="ephemeral-pre ephemeral-pre--context">
                  {viewerState.context.following_text}
                </pre>
              )}
            </div>
          )}

          {/* Retry button ONLY for retryable UNAVAILABLE (429/503/timeout) */}
          {viewerState.uiState === "UNAVAILABLE" && viewerState.canRetry && (
            <div className="action-row">
              <button
                type="button"
                className="btn btn--secondary btn--sm"
                data-testid={`retry-ephemeral-source-${contentUnitId}`}
                disabled={isLoading || disabledByPolicyOrFence}
                onClick={() => void handleFetchEphemeralSource()}
              >
                일시 오류 재조회 시도
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

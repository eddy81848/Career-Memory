"use client";

import React from "react";
import type { QuotaStatusResponse } from "@/domain/memory-ops-models";
import {
  computeMeterBarWidthPercent,
  formatCircuitBreakerBadge,
  formatQuotaAdmissionBadge,
  formatQuotaMeterStatusBadge,
  formatQuotaMetricLabel,
  formatQuotaPauseBadge,
  formatRetryAfterDuration,
  formatUtilizationPercent,
} from "@/domain/memory-ops-presenter";

interface QuotaBudgetBannerProps {
  quotaStatus: QuotaStatusResponse | null;
  isLoading: boolean;
  onRefreshQuota: () => void;
  onTriggerDisconnectNotice?: () => void;
  disconnectNotice?: string | null;
}

export function QuotaBudgetBanner({
  quotaStatus,
  isLoading,
  onRefreshQuota,
  onTriggerDisconnectNotice,
  disconnectNotice,
}: QuotaBudgetBannerProps) {
  if (isLoading && !quotaStatus) {
    return (
      <section
        className="card"
        aria-label="한도 및 예산 상태 불러오는 중"
        data-testid="quota-status-loading"
      >
        <p className="field-help">
          워크스페이스 한도·예산·일시정지 상태를 조회하고 있습니다...
        </p>
      </section>
    );
  }

  if (!quotaStatus) {
    return null;
  }

  const admissionBadge = formatQuotaAdmissionBadge(quotaStatus.admission_state);
  const pauseBadge = formatQuotaPauseBadge(quotaStatus.pause_state);
  const breakerBadge = formatCircuitBreakerBadge(
    quotaStatus.circuit_breaker_state
  );
  const retryAfterText = formatRetryAfterDuration(
    quotaStatus.retry_after_seconds
  );

  const isWarning = quotaStatus.admission_state === "WARNING_80_PERCENT";
  const isCutoffOrPaused =
    quotaStatus.admission_state === "CUTOFF_95_PERCENT" ||
    quotaStatus.admission_state === "PAUSED" ||
    !quotaStatus.high_cost_operations_allowed;

  const bannerToneClass = isCutoffOrPaused
    ? "status-banner--danger"
    : isWarning
      ? "status-banner--warning"
      : "status-banner--success";

  return (
    <section
      className="card"
      aria-labelledby="quota-status-heading"
      data-testid="quota-budget-banner"
      data-admission-state={quotaStatus.admission_state}
      data-pause-state={quotaStatus.pause_state}
    >
      <div className="event-inbox-topbar">
        <div>
          <span className="step-kicker">
            T-503 / INF-06 · 무료 한도 · 예산 보호 · 일시정지 상태
          </span>
          <h2 id="quota-status-heading" className="section-title">
            워크스페이스 쿼터 및 안전 작업 보장 ({quotaStatus.period_key})
          </h2>
          <p className="section-subtitle mono">
            workspace_id={quotaStatus.workspace_id} · checked_at=
            {quotaStatus.checked_at} · paid_fallback_allowed=
            {String(quotaStatus.paid_fallback_allowed)} ·
            safety_operations_allowed=
            {String(quotaStatus.safety_operations_allowed)}
          </p>
        </div>

        <div className="action-row">
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            data-testid="refresh-quota-status-button"
            disabled={isLoading}
            onClick={onRefreshQuota}
          >
            {isLoading ? "한도 조회 중..." : "한도 상태 새로고침"}
          </button>
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            data-testid="safety-account-disconnect-button"
            disabled={!quotaStatus.safety_operations_allowed}
            onClick={onTriggerDisconnectNotice}
          >
            GitHub 계정 연결 해제 (안전 작업 항상 허용)
          </button>
        </div>
      </div>

      {/* Primary Admission / Pause Callout */}
      <div
        className={`status-banner ${bannerToneClass}`}
        role={isCutoffOrPaused ? "alert" : "status"}
        data-testid="quota-admission-callout"
      >
        <div className="status-banner__top">
          <div className="status-banner__badges">
            <span
              className={`badge ${admissionBadge.badgeClass}`}
              data-testid="quota-admission-badge"
            >
              {admissionBadge.label}
            </span>
            <span
              className={`badge ${pauseBadge.badgeClass}`}
              data-testid="quota-pause-badge"
            >
              {pauseBadge.label}
            </span>
            <span className={`badge ${breakerBadge.badgeClass}`}>
              {breakerBadge.label}
            </span>
            {quotaStatus.pause_reason_code && (
              <span className="badge badge--neutral mono">
                {quotaStatus.pause_reason_code}
              </span>
            )}
          </div>
          {retryAfterText && (
            <span
              className="mono run-id-chip"
              data-testid="quota-retry-after-chip"
            >
              {retryAfterText}
            </span>
          )}
        </div>

        <p className="status-banner__headline">
          {isCutoffOrPaused
            ? "95% 한도 도달 또는 일시정지로 고비용 작업(가져오기 · AI 생성 · 서버 Export)이 차단되었습니다"
            : isWarning
              ? "사용량이 80% 소프트 경고 구간에 도달했습니다 — 범위 축소 및 무료 대안을 권장합니다"
              : "모든 무료 구간 한도가 정상 범위(ALLOWED) 내에서 운영 중입니다"}
        </p>

        <p
          className="status-banner__detail"
          data-testid="quota-safety-guarantee-text"
        >
          <strong>비용 안전 보장:</strong> 유료 자동 전환 없음(
          <code className="mono">paid_fallback_allowed=false</code>) · 필수 안전
          작업인 <strong>저장소 영구 삭제(OD-003)</strong> 및{" "}
          <strong>계정 연결 해제</strong>는 한도 차단·일시정지 상태에서도 항상
          활성화됩니다(
          <code className="mono">safety_operations_allowed=true</code>).
        </p>

        {quotaStatus.free_alternatives.length > 0 && (
          <div data-testid="quota-free-alternatives">
            <p className="field-label">무료 대안 안내 (Free Alternatives)</p>
            <ul className="bullet-list">
              {quotaStatus.free_alternatives.map((alt) => (
                <li key={alt}>{alt}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {disconnectNotice && (
        <div
          className="inline-alert inline-alert--info"
          role="status"
          data-testid="safety-disconnect-notice"
        >
          {disconnectNotice}
        </div>
      )}

      {/* Meter Bars */}
      {quotaStatus.meters.length > 0 && (
        <div className="quota-meters-grid" data-testid="quota-meters-grid">
          {quotaStatus.meters.map((meter) => {
            const mBadge = formatQuotaMeterStatusBadge(meter.status);
            const widthPct = computeMeterBarWidthPercent(meter);
            return (
              <div
                key={meter.metric_kind}
                className="subcard quota-meter-card"
                data-testid={`quota-meter-${meter.metric_kind}`}
                data-meter-status={meter.status}
              >
                <div className="subcard__header">
                  <span className="quota-meter-card__title">
                    {formatQuotaMetricLabel(meter.metric_kind)}
                  </span>
                  <span className={`badge ${mBadge.badgeClass}`}>
                    {mBadge.label}
                  </span>
                </div>

                <div
                  className="quota-meter-bar"
                  role="progressbar"
                  aria-label={formatQuotaMetricLabel(meter.metric_kind)}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={Math.round(widthPct)}
                >
                  <div
                    className={`quota-meter-bar__fill quota-meter-bar__fill--${meter.status.toLowerCase()}`}
                    style={{ width: `${widthPct}%` }}
                  />
                </div>

                <p className="field-help mono">
                  사용량 {meter.used_value.toLocaleString()} / 경고{" "}
                  {meter.soft_warning_limit.toLocaleString()} / 차단{" "}
                  {meter.hard_cutoff_limit.toLocaleString()} (
                  <strong>
                    {formatUtilizationPercent(meter.utilization_percent)}
                  </strong>
                  )
                </p>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

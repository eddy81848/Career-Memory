"use client";

import React, { useState } from "react";
import type { ClaimReviewAdapter } from "@/adapters/claim-review-adapter";
import type {
  ClaimDetailResponse,
  ClaimSupportsResponse,
} from "@/domain/claim-review-models";
import {
  extractMetricSixElementPills,
  extractRoleGroundingSummary,
  formatAtomStatusBadge,
  formatAtomTypeLabel,
  formatFactStatusBadge,
  formatSupportTypeBadge,
} from "@/domain/claim-review-presenter";
import { EphemeralSourceViewer } from "./EphemeralSourceViewer";

/**
 * Interactive 5-level Provenance Citation Drill-Down:
 * Claim -> Claim Atom (CLAIM-05 role / CLAIM-06 6-element metric)
 *       -> Canonical Fact (DIRECT | CONTEXT | CONTRADICTS)
 *       -> Content Unit (cm-locator-v1 line/char span & span_sha256)
 *       -> Source Object Revision (revision_content_hash, observed_at vs last_verified_at vs checked_at)
 *       + Plan C Ephemeral Source Viewer (memory-only on demand).
 */

interface ClaimProvenanceDrilldownProps {
  adapter: ClaimReviewAdapter;
  claim: ClaimDetailResponse;
  supports: ClaimSupportsResponse | null;
  isLoadingSupports: boolean;
  mutationsLocked: boolean;
}

export function ClaimProvenanceDrilldown({
  adapter,
  claim,
  supports,
  isLoadingSupports,
  mutationsLocked,
}: ClaimProvenanceDrilldownProps) {
  const [expandedAtomIds, setExpandedAtomIds] = useState<Record<string, boolean>>(
    () => {
      const initial: Record<string, boolean> = {};
      if (claim.atoms[0]) {
        initial[claim.atoms[0].atom_id] = true;
      }
      return initial;
    }
  );

  const toggleAtom = (atomId: string) => {
    setExpandedAtomIds((prev) => ({ ...prev, [atomId]: !prev[atomId] }));
  };

  const supportsByAtomId = new Map(
    (supports?.atoms ?? []).map((item) => [item.atom_id, item])
  );

  return (
    <section
      className="subcard provenance-drilldown"
      aria-labelledby="provenance-drilldown-heading"
      data-testid="claim-provenance-drilldown"
    >
      <div className="subcard__header">
        <div>
          <h4 id="provenance-drilldown-heading" className="subcard__title">
            근거 체인 드릴다운 (Claim → Atom → Fact → Content Unit → Source Revision)
          </h4>
          <p className="field-help">
            메타데이터만으로 전체 근거 구조를 검증할 수 있으며, 필요 시 각 Content
            Unit에서 <code className="mono">View Ephemeral Source</code>를 눌러
            실시간 해시 검증 원문을 메모리 전용으로 열람합니다.
          </p>
        </div>
        <span className="badge badge--neutral mono">
          epoch #{claim.support_graph_epoch} · last_verified_at:{" "}
          {claim.last_verified_at ?? "미검증"}
        </span>
      </div>

      {isLoadingSupports && !supports && (
        <p className="field-help" role="status">
          Claim 근거 그래프(/supports)를 불러오는 중...
        </p>
      )}

      <ul className="stage-list" data-testid="claim-atoms-list">
        {claim.atoms.map((atom) => {
          const atomStatusBadge = formatAtomStatusBadge(atom.status);
          const isOpen = Boolean(expandedAtomIds[atom.atom_id]);
          const atomSupports = supportsByAtomId.get(atom.atom_id)?.supports ?? [];
          const roleSummary = extractRoleGroundingSummary(atom);
          const metricInfo =
            atom.atom_type === "METRIC"
              ? extractMetricSixElementPills(atom.metric_details)
              : null;

          return (
            <li
              key={atom.atom_id}
              className={`atom-card atom-card--${atom.status.toLowerCase()}`}
              data-testid={`claim-atom-${atom.atom_id}`}
              data-atom-type={atom.atom_type}
              data-atom-status={atom.status}
            >
              <div className="atom-card__header">
                <div className="status-banner__badges">
                  <span className="badge badge--neutral mono">
                    Atom #{atom.ordinal} · {formatAtomTypeLabel(atom.atom_type)}
                  </span>
                  <span
                    className={`badge ${atomStatusBadge.badgeClass} mono`}
                    data-testid={`atom-status-${atom.atom_id}`}
                  >
                    {atomStatusBadge.label}
                  </span>
                  <span className="badge badge--neutral mono">
                    연결 Fact {atomSupports.length}건
                  </span>
                </div>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  data-testid={`toggle-atom-${atom.atom_id}`}
                  aria-expanded={isOpen}
                  onClick={() => toggleAtom(atom.atom_id)}
                >
                  {isOpen ? "드릴다운 접기" : "근거 체인 펼치기"}
                </button>
              </div>

              <p className="atom-card__text">{atom.atom_text}</p>

              {/* Per-atom block reason */}
              {atom.block_reason && (
                <p
                  className="inline-alert inline-alert--danger"
                  data-testid={`atom-block-reason-${atom.atom_id}`}
                >
                  {atom.block_reason}
                </p>
              )}

              {/* CLAIM-05 Role Grounding Pill */}
              {roleSummary && (
                <div
                  className="grounding-callout"
                  data-testid={`atom-role-grounding-${atom.atom_id}`}
                >
                  <div className="status-banner__badges">
                    <span
                      className={`badge ${
                        roleSummary.sourceVerifiedEligible
                          ? "badge--success"
                          : "badge--warning"
                      } mono`}
                    >
                      CLAIM-05 역할: {roleSummary.roleLabel} · 신원:{" "}
                      {roleSummary.identityStatus}
                    </span>
                  </div>
                  <p className="field-help">{roleSummary.note}</p>
                </div>
              )}

              {/* CLAIM-06 6-Element Metric Pills */}
              {metricInfo && (
                <div
                  className="metric-pills-box"
                  data-testid={`atom-metric-pills-${atom.atom_id}`}
                >
                  <p className="field-label">
                    CLAIM-06 6요소 지표 검증 ({metricInfo.completeCount}/6)
                  </p>
                  <div className="metric-pills-row">
                    {metricInfo.pills.map((pill) => (
                      <span
                        key={pill.key}
                        className={`badge ${
                          pill.present ? "badge--success" : "badge--danger"
                        } mono`}
                        data-testid={`metric-pill-${atom.atom_id}-${pill.key}`}
                      >
                        {pill.key}: {pill.value ?? "누락"}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Expanded Level 3 -> 4 -> 5: Fact -> Content Unit -> Source Object Revision */}
              {isOpen && (
                <div
                  className="provenance-tree"
                  data-testid={`atom-provenance-tree-${atom.atom_id}`}
                >
                  {atomSupports.length === 0 ? (
                    <div
                      className="inline-alert inline-alert--danger"
                      data-testid={`atom-no-supports-${atom.atom_id}`}
                    >
                      이 Atom을 뒷받침하는 Canonical Fact 링크가 없습니다. 편집 후
                      재검증하거나 과장된 표현을 제거해야 승인할 수 있습니다.
                    </div>
                  ) : (
                    atomSupports.map((link) => {
                      const linkBadge = formatSupportTypeBadge(link.support_type);
                      const factBadge = formatFactStatusBadge(link.fact.status);

                      return (
                        <div
                          key={link.claim_atom_support_id}
                          className="provenance-fact-node"
                          data-testid={`provenance-fact-${link.fact.fact_id}`}
                        >
                          <div className="status-banner__badges">
                            <span className={`badge ${linkBadge.badgeClass}`}>
                              {linkBadge.label}
                            </span>
                            <span className={`badge ${factBadge.badgeClass}`}>
                              Fact: {factBadge.label}
                            </span>
                            <span className="badge badge--neutral mono">
                              {link.fact.fact_id} · v{link.fact.version} ·{" "}
                              {link.fact.fact_type}
                            </span>
                          </div>

                          <p className="provenance-fact-node__text">
                            <strong className="mono">{link.fact.predicate}</strong>:{" "}
                            {link.fact.object_text}
                          </p>

                          {/* Level 4 & 5: Content Unit + Source Object Revision + Ephemeral Viewer */}
                          {link.fact_supports.map((fs) => {
                            const fsBadge = formatSupportTypeBadge(fs.support_type);
                            return (
                              <div
                                key={fs.fact_support_id}
                                className="provenance-unit-node"
                                data-testid={`provenance-unit-${fs.content_unit_id}`}
                              >
                                <div className="status-banner__badges">
                                  <span className={`badge ${fsBadge.badgeClass}`}>
                                    FactSupport: {fsBadge.label}
                                  </span>
                                  <span className="badge badge--neutral mono">
                                    {fs.unit_type} · {fs.locator?.schema ?? "cm-locator-v1"}
                                  </span>
                                  <span className="badge badge--neutral mono">
                                    rev: {fs.source_object_revision_id ?? "N/A"}
                                    {fs.revision_is_tombstone ? " (TOMBSTONE)" : ""}
                                  </span>
                                  {fs.last_resolution_status && (
                                    <span className="badge badge--info mono">
                                      last_resolution: {fs.last_resolution_status}
                                    </span>
                                  )}
                                </div>

                                {/* Timestamps comparison: observed_at vs last_verified_at vs checked_at */}
                                <dl className="plan-meta-grid provenance-meta-grid mono">
                                  <div>
                                    <dt>Source Revision Hash</dt>
                                    <dd>{fs.revision_content_hash ?? "N/A"}</dd>
                                  </div>
                                  <div>
                                    <dt>Observed vs Last Verified</dt>
                                    <dd>
                                      observed: {fs.revision_observed_at ?? "N/A"}
                                      <br />
                                      verified: {claim.last_verified_at ?? "미검증"}
                                    </dd>
                                  </div>
                                  <div>
                                    <dt>Locator & Span SHA-256</dt>
                                    <dd>
                                      {fs.locator?.file_path ??
                                        fs.locator?.section ??
                                        "cm-locator-v1"}
                                      {fs.locator?.start_line != null
                                        ? ` L${fs.locator.start_line}-L${fs.locator.end_line ?? fs.locator.start_line}`
                                        : ""}
                                      <br />
                                      {fs.locator?.span_sha256
                                        ? `${fs.locator.span_sha256.slice(0, 28)}...`
                                        : "N/A"}
                                    </dd>
                                  </div>
                                </dl>

                                <EphemeralSourceViewer
                                  adapter={adapter}
                                  workspaceId={claim.workspace_id}
                                  contentUnitId={fs.content_unit_id}
                                  unitType={fs.unit_type}
                                  locator={fs.locator}
                                  revisionContentHash={fs.revision_content_hash}
                                  revisionObservedAt={fs.revision_observed_at}
                                  lastCheckedAt={fs.last_checked_at}
                                  disabledByPolicyOrFence={mutationsLocked}
                                />
                              </div>
                            );
                          })}
                        </div>
                      );
                    })
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

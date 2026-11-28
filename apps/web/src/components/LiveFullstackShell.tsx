"use client";

import Link from "next/link";
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  buildLiveAuthHeaders,
  createLiveClaimReviewTransport,
  createLiveEventReviewTransport,
  createLiveMemoryOpsTransport,
  fetchLocalDevBootstrap,
  isLiveApiMode,
  LiveImportServiceAdapter,
  mapBootstrapResourcesToMemoryOpsRepos,
  type LocalDevBootstrapResponse,
} from "@/adapters/live-runtime-client";
import { ClaimReviewApp } from "@/components/claim-review/ClaimReviewApp";
import { EventInboxApp } from "@/components/event-review/EventInboxApp";
import { ImportJourneyApp } from "@/components/ImportJourneyApp";
import { MemoryOpsApp } from "@/components/memory-ops/MemoryOpsApp";
import type {
  ClaimReviewScenarioId,
  ClaimReviewWorkspaceGuard,
} from "@/domain/claim-review-models";
import type { MemoryOpsScenarioId } from "@/domain/memory-ops-models";

export type FullstackRouteView =
  | "import"
  | "event-review"
  | "claim-review"
  | "search";

export interface LiveFullstackShellProps {
  route: FullstackRouteView;
  /** Optional explicit override for testing live mode without mutating process.env */
  forceLive?: boolean;
  /** Optional injectable fetch implementation for unit tests */
  fetchImpl?: typeof fetch;
  /** Claim Review query props */
  eventId?: string;
  initialClaimScenario?: ClaimReviewScenarioId;
  /** Search / Memory Ops query props */
  initialMemoryOpsScenario?: MemoryOpsScenarioId;
  initialQuery?: string;
}

const NAV_LINKS: Array<{ route: FullstackRouteView; href: string; label: string }> = [
  { route: "import", href: "/", label: "Import" },
  { route: "event-review", href: "/event-review", label: "Event Review" },
  { route: "claim-review", href: "/claim-review", label: "Claim Review" },
  { route: "search", href: "/search", label: "Search, Deletion & Quotas" },
];

export function LiveFullstackShell({
  route,
  forceLive,
  fetchImpl,
  eventId,
  initialClaimScenario = "default_grounded",
  initialMemoryOpsScenario = "default_active",
  initialQuery = "webhook",
}: LiveFullstackShellProps) {
  const liveEnabled = forceLive ?? isLiveApiMode();

  // When NEXT_PUBLIC_API_MODE !== "live", preserve 100% of existing Mock behavior untouched.
  if (!liveEnabled) {
    if (route === "import") {
      return <ImportJourneyApp />;
    }
    if (route === "event-review") {
      return <EventInboxApp />;
    }
    if (route === "claim-review") {
      return (
        <ClaimReviewApp
          eventId={eventId || "evt_comp_301"}
          initialScenario={initialClaimScenario}
        />
      );
    }
    return (
      <MemoryOpsApp
        initialScenario={initialMemoryOpsScenario}
        initialQuery={initialQuery}
      />
    );
  }

  return (
    <LivePg18Container
      route={route}
      fetchImpl={fetchImpl}
      requestedEventId={eventId}
      initialQuery={initialQuery}
    />
  );
}

interface ResolvedClaimEventMeta {
  eventId: string;
  title: string;
  status: string;
}

function LivePg18Container({
  route,
  fetchImpl,
  requestedEventId,
  initialQuery,
}: {
  route: FullstackRouteView;
  fetchImpl?: typeof fetch;
  requestedEventId?: string;
  initialQuery: string;
}) {
  const effectiveFetch = useMemo(
    () => fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init)),
    [fetchImpl]
  );

  // Strictly in-memory state (ZERO localStorage / sessionStorage / cookie persistence)
  const [bootstrap, setBootstrap] = useState<LocalDevBootstrapResponse | null>(null);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [selectedResourceId, setSelectedResourceId] = useState<string | null>(null);
  const [resolvedClaimEvent, setResolvedClaimEvent] =
    useState<ResolvedClaimEventMeta | null>(null);
  const isMountedRef = useRef(true);

  useEffect(() => {
    isMountedRef.current = true;
    let active = true;

    async function load() {
      try {
        const data = await fetchLocalDevBootstrap(effectiveFetch);
        if (!active || !isMountedRef.current) return;
        setBootstrap(data);
        setSelectedResourceId(
          data.default_source_resource_id ??
            data.resources[0]?.source_resource_id ??
            null
        );

        if (route === "claim-review") {
          const explicitId =
            requestedEventId &&
            requestedEventId.trim() &&
            requestedEventId !== "evt_comp_301"
              ? requestedEventId.trim()
              : null;

          let resolvedId = explicitId ?? data.default_confirmed_event_id;
          let resolvedTitle = "Confirmed WorkEvent (Live PG18)";
          let resolvedStatus = "CONFIRMED";

          try {
            const evRes = await effectiveFetch("/v1/work-events?limit=50", {
              method: "GET",
              cache: "no-store",
              headers: {
                Accept: "application/json",
                ...buildLiveAuthHeaders(data),
              },
            });
            if (evRes.ok) {
              const evJson = (await evRes.json()) as {
                items?: Array<{
                  event_id?: string;
                  id?: string;
                  title?: string;
                  status?: string;
                }>;
              };
              const items = Array.isArray(evJson.items) ? evJson.items : [];
              const matched =
                (resolvedId
                  ? items.find(
                      (i) => (i.event_id || i.id) === resolvedId
                    )
                  : undefined) ??
                items.find((i) => i.status === "CONFIRMED") ??
                items[0];
              if (matched) {
                resolvedId = (matched.event_id || matched.id) ?? resolvedId;
                resolvedTitle = matched.title || resolvedTitle;
                resolvedStatus = matched.status || resolvedStatus;
              }
            }
          } catch {
            // Fall back to bootstrap default_confirmed_event_id
          }

          if (active && isMountedRef.current && resolvedId) {
            setResolvedClaimEvent({
              eventId: resolvedId,
              title: resolvedTitle,
              status: resolvedStatus,
            });
          }
        }
      } catch (err) {
        if (!active || !isMountedRef.current) return;
        setBootstrapError(
          err instanceof Error
            ? err.message
            : "로컬 PG18 런타임 부트스트랩에 실패했습니다."
        );
      }
    }

    void load();
    return () => {
      active = false;
      isMountedRef.current = false;
    };
  }, [effectiveFetch, requestedEventId, route]);

  const adapters = useMemo(() => {
    if (!bootstrap) return null;
    return {
      importAdapter: new LiveImportServiceAdapter(bootstrap, {
        fetchImpl: effectiveFetch,
      }),
      eventReviewAdapter: createLiveEventReviewTransport(
        bootstrap,
        effectiveFetch
      ),
      claimReviewAdapter: createLiveClaimReviewTransport(
        bootstrap,
        effectiveFetch
      ),
      memoryOpsAdapter: createLiveMemoryOpsTransport(bootstrap, effectiveFetch),
    };
  }, [bootstrap, effectiveFetch]);

  const activeResource = useMemo(() => {
    if (!bootstrap) return null;
    return (
      bootstrap.resources.find(
        (r) => r.source_resource_id === selectedResourceId
      ) ??
      bootstrap.resources[0] ??
      null
    );
  }, [bootstrap, selectedResourceId]);

  const claimWorkspaceGuard = useMemo<ClaimReviewWorkspaceGuard | undefined>(() => {
    if (!bootstrap || !resolvedClaimEvent) return undefined;
    return {
      import_ready: true,
      import_status: "SUCCESS",
      policy_status: "ACTIVE",
      event_id: resolvedClaimEvent.eventId,
      event_title: resolvedClaimEvent.title,
      event_status:
        resolvedClaimEvent.status === "CONFIRMED"
          ? "CONFIRMED"
          : resolvedClaimEvent.status === "REJECTED"
            ? "REJECTED"
            : resolvedClaimEvent.status === "STALE"
              ? "STALE"
              : resolvedClaimEvent.status === "SUPERSEDED"
                ? "SUPERSEDED"
                : "CANDIDATE",
      repository_label:
        activeResource?.repository_full_name ?? "seoyeon-dev/ledger-engine",
      notice: null,
    };
  }, [activeResource, bootstrap, resolvedClaimEvent]);

  const memoryOpsRepos = useMemo(
    () =>
      bootstrap ? mapBootstrapResourcesToMemoryOpsRepos(bootstrap.resources) : [],
    [bootstrap]
  );

  if (bootstrapError) {
    return (
      <div
        role="alert"
        data-testid="live-fullstack-error"
        style={{
          maxWidth: 960,
          margin: "2rem auto",
          padding: "1.25rem",
          borderRadius: 12,
          border: "1px solid #ef4444",
          background: "#fef2f2",
          color: "#991b1b",
        }}
      >
        <strong>LIVE PG18 FULLSTACK MODE 연결 실패</strong>
        <p style={{ marginTop: "0.5rem" }}>{bootstrapError}</p>
      </div>
    );
  }

  if (!bootstrap || !adapters) {
    return (
      <div
        data-testid="live-fullstack-loading"
        style={{
          maxWidth: 960,
          margin: "2rem auto",
          padding: "1.25rem",
          borderRadius: 12,
          border: "1px solid #cbd5e1",
          background: "#f8fafc",
          color: "#334155",
        }}
      >
        LIVE PG18 FULLSTACK MODE 세션 및 워크스페이스 정보를 불러오는 중...
      </div>
    );
  }

  return (
    <div>
      <header
        data-testid="live-fullstack-status-bar"
        style={{
          background: "#0f172a",
          color: "#f8fafc",
          padding: "0.65rem 1.25rem",
          borderBottom: "1px solid #1e293b",
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "0.75rem",
          fontSize: "0.85rem",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", flexWrap: "wrap" }}>
          <span
            style={{
              background: "#10b981",
              color: "#022c22",
              fontWeight: 700,
              padding: "0.18rem 0.55rem",
              borderRadius: 999,
              fontSize: "0.72rem",
              letterSpacing: "0.04em",
            }}
          >
            LIVE PG18 FULLSTACK MODE
          </span>
          <span
            data-testid="live-provider-mode-badge"
            style={{
              background: "#1e293b",
              color: "#93c5fd",
              padding: "0.18rem 0.5rem",
              borderRadius: 6,
              fontFamily: "monospace",
              fontSize: "0.75rem",
            }}
          >
            {bootstrap.mode}
          </span>
          <span style={{ color: "#cbd5e1" }}>{bootstrap.workspace_name}</span>
          {bootstrap.resources.length > 0 && (
            <label style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
              <span style={{ color: "#94a3b8" }}>Repo:</span>
              <select
                aria-label="Active workspace repository"
                value={selectedResourceId ?? ""}
                onChange={(e) => setSelectedResourceId(e.target.value || null)}
                style={{
                  background: "#1e293b",
                  color: "#f8fafc",
                  border: "1px solid #334155",
                  borderRadius: 6,
                  padding: "0.2rem 0.45rem",
                  fontSize: "0.8rem",
                }}
              >
                {bootstrap.resources.map((res) => (
                  <option
                    key={res.source_resource_id}
                    value={res.source_resource_id}
                  >
                    {res.repository_full_name}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        <nav
          aria-label="Career Memory Fullstack Navigation"
          style={{ display: "flex", alignItems: "center", gap: "0.45rem", flexWrap: "wrap" }}
        >
          {NAV_LINKS.map((item) => {
            const active = item.route === route;
            return (
              <Link
                key={item.route}
                href={item.href}
                style={{
                  padding: "0.28rem 0.65rem",
                  borderRadius: 6,
                  textDecoration: "none",
                  fontWeight: active ? 700 : 500,
                  background: active ? "#2563eb" : "#1e293b",
                  color: active ? "#ffffff" : "#cbd5e1",
                }}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      </header>

      {route === "import" && (
        <ImportJourneyApp
          adapter={adapters.importAdapter}
          eventReviewAdapter={adapters.eventReviewAdapter}
          pollIntervalMs={500}
        />
      )}

      {route === "event-review" && (
        <EventInboxApp
          key={selectedResourceId ?? "all"}
          adapter={adapters.eventReviewAdapter}
          sourceResourceId={selectedResourceId ?? undefined}
          repositoryLabel={
            activeResource?.repository_full_name ?? "seoyeon-dev/ledger-engine"
          }
        />
      )}

      {route === "claim-review" &&
        (resolvedClaimEvent ? (
          <ClaimReviewApp
            key={resolvedClaimEvent.eventId}
            adapter={adapters.claimReviewAdapter}
            eventId={resolvedClaimEvent.eventId}
            workspaceGuard={claimWorkspaceGuard}
          />
        ) : (
          <div
            data-testid="live-claim-review-empty"
            style={{
              maxWidth: 960,
              margin: "2rem auto",
              padding: "1.25rem",
              borderRadius: 12,
              border: "1px solid #cbd5e1",
              background: "#f8fafc",
            }}
          >
            확정(CONFIRMED)된 WorkEvent가 없습니다. 먼저{" "}
            <Link href="/event-review">/event-review</Link>에서 Event를 확정한 뒤
            Claim Review로 이동하세요.
          </div>
        ))}

      {route === "search" && (
        <MemoryOpsApp
          key={bootstrap.workspace_id}
          adapter={adapters.memoryOpsAdapter}
          initialRepositories={memoryOpsRepos}
          initialQuery={initialQuery}
        />
      )}
    </div>
  );
}

import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveFullstackShell } from "@/components/LiveFullstackShell";
import type { LocalDevBootstrapResponse } from "@/adapters/live-runtime-client";

const SAMPLE_BOOTSTRAP: LocalDevBootstrapResponse = {
  mode: "local_sample",
  workspace_id: "11111111-1111-4111-8111-111111111111",
  workspace_name: "Career Memory Local PG18 Workspace",
  user_id: "22222222-2222-4222-8222-222222222222",
  user_display_name: "Seoyeon Park",
  user_email: "seoyeon@career-memory.local",
  github_login: "seoyeon-dev",
  access_token: "es256-header.es256-payload.es256-signature",
  connected_account_id: "33333333-3333-4333-8333-333333333333",
  resources: [
    {
      source_resource_id: "44444444-4444-4444-8444-444444444444",
      source_account_id: "33333333-3333-4333-8333-333333333333",
      provider_resource_id: "810001",
      repository_full_name: "seoyeon-dev/ledger-engine",
      canonical_url: "https://github.com/seoyeon-dev/ledger-engine",
      visibility: "PUBLIC",
      status: "ACTIVE",
      default_branch: "main",
      description: "Idempotent settlement ledger & webhook replay defense engine",
      primary_language: "Python",
      estimated_commits: 4,
      estimated_pull_requests: 3,
      latest_sync_run_id: "55555555-5555-4555-8555-555555555555",
      latest_sync_run_status: "SUCCESS",
      latest_processing_status: "COMPLETE",
      latest_import_ready: true,
    },
  ],
  default_source_resource_id: "44444444-4444-4444-8444-444444444444",
  default_confirmed_event_id: "66666666-6666-4666-8666-666666666666",
  sample_repositories: [],
};

function jsonResp(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "private, no-store",
    },
  });
}

function createMockLiveFetch(seenUrls: string[]): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    seenUrls.push(url);

    // Every /v1/* call must carry real in-memory Bearer token and X-Workspace-ID
    if (url.startsWith("/v1/")) {
      const headers = new Headers(init?.headers);
      expect(headers.get("authorization")).toBe(
        `Bearer ${SAMPLE_BOOTSTRAP.access_token}`
      );
      expect(headers.get("x-workspace-id")).toBe(SAMPLE_BOOTSTRAP.workspace_id);
    }

    if (url === "/local-dev/bootstrap") {
      return jsonResp(SAMPLE_BOOTSTRAP);
    }
    if (url.startsWith("/v1/source-resources")) {
      return jsonResp({
        items: [
          {
            id: SAMPLE_BOOTSTRAP.resources[0].source_resource_id,
            name: SAMPLE_BOOTSTRAP.resources[0].repository_full_name,
            visibility: "PUBLIC",
            status: "ACTIVE",
          },
        ],
      });
    }
    if (url.startsWith("/v1/sync-runs?")) {
      return jsonResp({
        items: [
          {
            target_id: "55555555-5555-4555-8555-555555555555",
            status_url: "/v1/sync-runs/55555555-5555-4555-8555-555555555555",
            import_status: {
              status: "SUCCESS",
              run_version: 2,
              plan_id: "plan-live-1",
              import_ready: true,
              processing_status: "COMPLETE",
              source_drift: false,
              retry: { allowed: false },
            },
          },
        ],
      });
    }
    if (url.startsWith("/v1/work-events?")) {
      return jsonResp({
        items: [
          {
            event_id: SAMPLE_BOOTSTRAP.default_confirmed_event_id,
            workspace_id: SAMPLE_BOOTSTRAP.workspace_id,
            source_resource_id: SAMPLE_BOOTSTRAP.default_source_resource_id,
            anchor_mode: "PR",
            version: 2,
            title: "PR #101: Webhook HMAC Verification & Replay Latency Guard",
            status: "CONFIRMED",
            created_by: "RULE",
            review_flags: [],
            policy_state: "ACTIVE",
            allowed_actions: ["EDIT", "REJECT", "SPLIT"],
            evidence_count: 1,
            context_relation_count: 0,
            cannot_link_count: 0,
          },
        ],
        counts: {
          active_top_level_total: 1,
          by_status: { CONFIRMED: 1 },
        },
        page: { next_cursor: null, has_more: false },
      });
    }
    if (url.startsWith(`/v1/work-events/${SAMPLE_BOOTSTRAP.default_confirmed_event_id}`)) {
      return jsonResp({
        event_id: SAMPLE_BOOTSTRAP.default_confirmed_event_id,
        workspace_id: SAMPLE_BOOTSTRAP.workspace_id,
        source_resource_id: SAMPLE_BOOTSTRAP.default_source_resource_id,
        anchor_mode: "PR",
        version: 2,
        title: "PR #101: Webhook HMAC Verification & Replay Latency Guard",
        status: "CONFIRMED",
        created_by: "RULE",
        review_flags: [],
        policy_state: "ACTIVE",
        allowed_actions: ["EDIT", "REJECT", "SPLIT"],
        evidence_memberships: [],
        context_relations: [],
        cannot_link_constraints: { total: 0, items: [] },
        version_history: [],
        review_actions: [],
      });
    }
    if (url.startsWith("/v1/claims?")) {
      return jsonResp({
        items: [],
        page: { next_cursor: null, has_more: false },
      });
    }
    if (url.startsWith("/v1/facts?")) {
      return jsonResp({
        items: [],
        page: { next_cursor: null, has_more: false },
      });
    }
    if (url.startsWith("/v1/exports")) {
      return jsonResp({ items: [] });
    }
    if (url.startsWith("/v1/quota-status")) {
      return jsonResp({
        workspace_id: SAMPLE_BOOTSTRAP.workspace_id,
        admission_state: "ALLOWED",
        pause_state: "ACTIVE",
        paid_fallback_allowed: false,
        free_alternatives: [],
        circuit_breaker_state: "CLOSED",
        meters: [],
      });
    }
    if (url.startsWith("/v1/search?")) {
      return jsonResp({
        scope: {
          workspace_id: SAMPLE_BOOTSTRAP.workspace_id,
          source_resource_id: null,
        },
        query_hash: "sha256:abc",
        index_version: "fts-index-v1",
        ranking_version: "rank-bm25-v1",
        items: [],
        next_cursor: null,
      });
    }
    return jsonResp({ error: { code: "NOT_FOUND", message: `Unhandled URL: ${url}` } }, 404);
  }) as typeof fetch;
}

describe("LiveFullstackShell live PG18 wiring & zero-storage invariants", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("wires live mode across /, /event-review, /claim-review, and /search with zero localStorage/sessionStorage access", async () => {
    const localGetSpy = vi.spyOn(Storage.prototype, "getItem");
    const localSetSpy = vi.spyOn(Storage.prototype, "setItem");

    for (const route of [
      "import",
      "event-review",
      "claim-review",
      "search",
    ] as const) {
      const seenUrls: string[] = [];
      const mockFetch = createMockLiveFetch(seenUrls);

      render(<LiveFullstackShell route={route} forceLive fetchImpl={mockFetch} />);

      await waitFor(() => {
        expect(screen.getByTestId("live-fullstack-status-bar")).toBeInTheDocument();
      });
      expect(screen.getByTestId("live-provider-mode-badge")).toHaveTextContent(
        "local_sample"
      );
      expect(seenUrls).toContain("/local-dev/bootstrap");

      if (route === "import") {
        await waitFor(() => {
          expect(seenUrls.some((u) => u.startsWith("/v1/sync-runs?"))).toBe(true);
        });
      } else if (route === "event-review") {
        await waitFor(() => {
          expect(seenUrls.some((u) => u.startsWith("/v1/work-events?"))).toBe(true);
        });
      } else if (route === "claim-review") {
        await waitFor(() => {
          expect(seenUrls.some((u) => u.startsWith("/v1/claims?"))).toBe(true);
        });
      } else if (route === "search") {
        await waitFor(() => {
          expect(seenUrls.some((u) => u.startsWith("/v1/quota-status"))).toBe(true);
        });
      }

      cleanup();
    }

    // Strict zero browser storage persistence invariant
    expect(localGetSpy).not.toHaveBeenCalled();
    expect(localSetSpy).not.toHaveBeenCalled();
  });
});

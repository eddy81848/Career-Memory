import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveFullstackShell } from "@/components/LiveFullstackShell";
import type { HostedAuthSessionResponse } from "@/adapters/hosted-bff-client";

const SAMPLE_CSRF_TOKEN = "csrf-session-bound-token-abc123";

const SAMPLE_HOSTED_AUTH: HostedAuthSessionResponse = {
  authenticated: true,
  csrf_token: SAMPLE_CSRF_TOKEN,
  session: {
    mode: "hosted_supabase",
    workspace_id: "11111111-1111-4111-8111-111111111111",
    workspace_name: "Career Memory Hosted Workspace",
    user_id: "22222222-2222-4222-8222-222222222222",
    user_display_name: "Seoyeon Park",
    user_email: "seoyeon@career-memory.example",
    github_login: "seoyeon-dev",
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
  },
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

function createMockHostedFetch(
  seenRequests: Array<{ url: string; method: string; headers: Headers }>
): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = (init?.method || "GET").toUpperCase();
    const headers = new Headers(init?.headers);
    seenRequests.push({ url, method, headers });

    // Strict OD-010 invariant: browser JS NEVER sends Authorization header
    expect(headers.get("authorization")).toBeNull();

    if (url.startsWith("/bff/v1/")) {
      expect(headers.get("x-workspace-id")).toBe(
        SAMPLE_HOSTED_AUTH.session!.workspace_id
      );
      if (["POST", "PATCH", "PUT", "DELETE"].includes(method)) {
        expect(headers.get("x-csrf-token")).toBe(SAMPLE_CSRF_TOKEN);
      }
    }

    if (url === "/auth/session") {
      return jsonResp(SAMPLE_HOSTED_AUTH);
    }
    if (url === "/auth/logout" && method === "POST") {
      expect(headers.get("x-csrf-token")).toBe(SAMPLE_CSRF_TOKEN);
      return jsonResp({ authenticated: false });
    }
    if (url.startsWith("/bff/v1/source-resources")) {
      return jsonResp({
        items: [
          {
            id: SAMPLE_HOSTED_AUTH.session!.resources[0].source_resource_id,
            name: SAMPLE_HOSTED_AUTH.session!.resources[0].repository_full_name,
            visibility: "PUBLIC",
            status: "ACTIVE",
          },
        ],
      });
    }
    if (url.startsWith("/bff/v1/sync-runs?")) {
      return jsonResp({
        items: [
          {
            target_id: "55555555-5555-4555-8555-555555555555",
            status_url: "/v1/sync-runs/55555555-5555-4555-8555-555555555555",
            import_status: {
              status: "SUCCESS",
              run_version: 2,
              plan_id: "plan-hosted-1",
              import_ready: true,
              processing_status: "COMPLETE",
              source_drift: false,
              retry: { allowed: false },
            },
          },
        ],
      });
    }
    if (url.startsWith("/bff/v1/work-events?")) {
      return jsonResp({
        items: [
          {
            event_id: SAMPLE_HOSTED_AUTH.session!.default_confirmed_event_id,
            workspace_id: SAMPLE_HOSTED_AUTH.session!.workspace_id,
            source_resource_id:
              SAMPLE_HOSTED_AUTH.session!.default_source_resource_id,
            anchor_mode: "PR",
            version: 2,
            title: "PR #101: Hosted BFF PKCE & Origin Guard",
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
    if (
      url.startsWith(
        `/bff/v1/work-events/${SAMPLE_HOSTED_AUTH.session!.default_confirmed_event_id}`
      )
    ) {
      return jsonResp({
        event_id: SAMPLE_HOSTED_AUTH.session!.default_confirmed_event_id,
        workspace_id: SAMPLE_HOSTED_AUTH.session!.workspace_id,
        source_resource_id:
          SAMPLE_HOSTED_AUTH.session!.default_source_resource_id,
        anchor_mode: "PR",
        version: 2,
        title: "PR #101: Hosted BFF PKCE & Origin Guard",
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
    if (url.startsWith("/bff/v1/claims?")) {
      return jsonResp({ items: [], page: { next_cursor: null, has_more: false } });
    }
    if (url.startsWith("/bff/v1/facts?")) {
      return jsonResp({ items: [], page: { next_cursor: null, has_more: false } });
    }
    if (url.startsWith("/bff/v1/exports")) {
      return jsonResp({ items: [] });
    }
    if (url.startsWith("/bff/v1/quota-status")) {
      return jsonResp({
        workspace_id: SAMPLE_HOSTED_AUTH.session!.workspace_id,
        admission_state: "ALLOWED",
        pause_state: "ACTIVE",
        paid_fallback_allowed: false,
        free_alternatives: [],
        circuit_breaker_state: "CLOSED",
        meters: [],
      });
    }
    if (url.startsWith("/bff/v1/search?")) {
      return jsonResp({
        scope: {
          workspace_id: SAMPLE_HOSTED_AUTH.session!.workspace_id,
          source_resource_id: null,
        },
        query_hash: "sha256:hosted",
        index_version: "fts-index-v1",
        ranking_version: "rank-bm25-v1",
        items: [],
        next_cursor: null,
      });
    }

    return jsonResp(
      { error: { code: "NOT_FOUND", message: `Unhandled URL: ${url}` } },
      404
    );
  }) as typeof fetch;
}

describe("HostedBffShell (NEXT_PUBLIC_API_MODE=hosted)", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders hosted PKCE sign-in gate when /auth/session is unauthenticated with zero browser storage access", async () => {
    const localGetSpy = vi.spyOn(Storage.prototype, "getItem");
    const localSetSpy = vi.spyOn(Storage.prototype, "setItem");

    const mockFetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      expect(url).toBe("/auth/session");
      return jsonResp({
        authenticated: false,
        csrf_token: null,
        session: null,
      });
    }) as typeof fetch;

    render(
      <LiveFullstackShell route="event-review" forceHosted fetchImpl={mockFetch} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("hosted-auth-gate")).toBeInTheDocument();
    });

    const loginLink = screen.getByTestId("hosted-login-link");
    expect(loginLink).toHaveTextContent(
      "Supabase Auth로 로그인 (PKCE · HttpOnly Cookie)"
    );
    expect(loginLink).toHaveAttribute(
      "href",
      "/auth/login?next=%2Fevent-review"
    );

    expect(localGetSpy).not.toHaveBeenCalled();
    expect(localSetSpy).not.toHaveBeenCalled();
  });

  it("drives /bff/v1/* across all 4 routes with zero Authorization header, zero storage access, and CSRF-protected logout", async () => {
    const localGetSpy = vi.spyOn(Storage.prototype, "getItem");
    const localSetSpy = vi.spyOn(Storage.prototype, "setItem");

    for (const route of [
      "import",
      "event-review",
      "claim-review",
      "search",
    ] as const) {
      const seenRequests: Array<{
        url: string;
        method: string;
        headers: Headers;
      }> = [];
      const mockFetch = createMockHostedFetch(seenRequests);

      render(
        <LiveFullstackShell route={route} forceHosted fetchImpl={mockFetch} />
      );

      await waitFor(() => {
        expect(screen.getByTestId("hosted-bff-status-bar")).toBeInTheDocument();
      });
      expect(screen.getByTestId("hosted-provider-mode-badge")).toHaveTextContent(
        "hosted_supabase"
      );
      expect(seenRequests.some((r) => r.url === "/auth/session")).toBe(true);
      expect(seenRequests.some((r) => r.url === "/local-dev/bootstrap")).toBe(
        false
      );

      if (route === "import") {
        await waitFor(() => {
          expect(
            seenRequests.some((r) => r.url.startsWith("/bff/v1/sync-runs?"))
          ).toBe(true);
        });
      } else if (route === "event-review") {
        await waitFor(() => {
          expect(
            seenRequests.some((r) => r.url.startsWith("/bff/v1/work-events?"))
          ).toBe(true);
        });
      } else if (route === "claim-review") {
        await waitFor(() => {
          expect(
            seenRequests.some((r) => r.url.startsWith("/bff/v1/claims?"))
          ).toBe(true);
        });
      } else if (route === "search") {
        await waitFor(() => {
          expect(
            seenRequests.some((r) => r.url.startsWith("/bff/v1/quota-status"))
          ).toBe(true);
        });
      }

      // Trigger logout button on the last route and verify POST /auth/logout + CSRF + sign-in gate transition
      if (route === "search") {
        fireEvent.click(screen.getByTestId("hosted-logout-button"));
        await waitFor(() => {
          expect(screen.getByTestId("hosted-auth-gate")).toBeInTheDocument();
        });
        const logoutCall = seenRequests.find(
          (r) => r.url === "/auth/logout" && r.method === "POST"
        );
        expect(logoutCall).toBeDefined();
        expect(logoutCall?.headers.get("x-csrf-token")).toBe(SAMPLE_CSRF_TOKEN);
        expect(logoutCall?.headers.get("authorization")).toBeNull();
      }

      cleanup();
    }

    expect(localGetSpy).not.toHaveBeenCalled();
    expect(localSetSpy).not.toHaveBeenCalled();
  });

  it("preserves session state UI guidance on 503 AUTH_VERIFICATION_UNAVAILABLE", async () => {
    const mockFetch = (async () =>
      jsonResp(
        {
          error: {
            code: "AUTH_VERIFICATION_UNAVAILABLE",
            message: "Temporarily unavailable",
            retryable: true,
          },
        },
        503
      )) as typeof fetch;

    render(
      <LiveFullstackShell route="import" forceHosted fetchImpl={mockFetch} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("hosted-bff-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("hosted-bff-error")).toHaveTextContent(
      "로그인 세션은 유지"
    );
  });
});

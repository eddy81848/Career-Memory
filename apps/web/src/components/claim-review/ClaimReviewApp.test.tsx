import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MockClaimReviewAdapter } from "@/adapters/mock-claim-review-adapter";
import { ClaimReviewApp } from "./ClaimReviewApp";

function createMemoryStorageSpy() {
  const store = new Map<string, string>();
  const setItem = vi.fn((key: string, value: string) => {
    store.set(key, String(value));
  });
  const removeItem = vi.fn((key: string) => {
    store.delete(key);
  });
  const clear = vi.fn(() => {
    store.clear();
  });
  const getItem = vi.fn((key: string) => store.get(key) ?? null);
  const key = vi.fn((index: number) => Array.from(store.keys())[index] ?? null);
  return {
    storage: {
      get length() {
        return store.size;
      },
      clear,
      getItem,
      key,
      removeItem,
      setItem,
    } as Storage,
    setItem,
    store,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ClaimReviewApp — M4 Grounded Claim Review, Provenance, Plan C Viewer & Export UX", () => {
  it("separates workflow status from read-only computed_verification, provides zero verification setter controls, and drills down Claim -> Atom -> Fact -> Unit -> Revision", async () => {
    const user = userEvent.setup();
    const adapter = new MockClaimReviewAdapter({ latencyMs: 0 });

    render(<ClaimReviewApp adapter={adapter} harness={adapter} />);

    // Initial verified claim loads
    await screen.findByTestId("claim-detail-panel");
    expect(screen.getByTestId("claim-workflow-status-badge")).toHaveTextContent(
      "승인됨 (APPROVED)"
    );
    expect(
      screen.getByTestId("claim-computed-verification-badge")
    ).toHaveTextContent("SOURCE_VERIFIED · 읽기 전용");

    // Zero inputs/selects/checkboxes allow setting verification or SOURCE_VERIFIED
    const allInputs = document.querySelectorAll("input, select");
    for (const el of allInputs) {
      const name = el.getAttribute("name") ?? "";
      const id = el.getAttribute("id") ?? "";
      expect(name.toLowerCase()).not.toContain("verification");
      expect(id.toLowerCase()).not.toContain("verification");
    }

    // CLAIM-05 role grounding & CLAIM-06 6-element metric pills
    expect(
      screen.getByTestId("atom-role-grounding-atom_v01_role")
    ).toHaveTextContent("CLAIM-05 역할: IMPLEMENTED");
    expect(
      screen.getByTestId("atom-metric-pills-atom_v01_metric")
    ).toHaveTextContent("6/6");

    // Metadata-only provenance tree visible BEFORE fetching ephemeral source
    expect(
      screen.getByTestId("provenance-fact-fact_hmac_impl_01")
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("provenance-unit-cu_hmac_middleware_01")
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("ephemeral-viewer-cu_hmac_middleware_01")
    ).toHaveAttribute("data-ephemeral-state", "IDLE_METADATA_ONLY");

    // Expand metric atom and confirm its content unit
    await user.click(screen.getByTestId("toggle-atom-atom_v01_metric"));
    expect(
      screen.getByTestId("provenance-unit-cu_replay_metric_02")
    ).toBeInTheDocument();
  });

  it("renders all Plan C Ephemeral Source Viewer states distinctly AND leaks ZERO ephemeral text to localStorage, sessionStorage, URL, or console", async () => {
    const user = userEvent.setup();
    const adapter = new MockClaimReviewAdapter({ latencyMs: 0 });
    const localSpy = createMemoryStorageSpy();
    const sessionSpy = createMemoryStorageSpy();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: localSpy.storage,
    });
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: sessionSpy.storage,
    });
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const infoSpy = vi.spyOn(console, "info").mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    render(<ClaimReviewApp adapter={adapter} harness={adapter} />);
    await screen.findByTestId("ephemeral-viewer-cu_hmac_middleware_01");

    // 1. MATCHED (ephemeral text rendered strictly in memory)
    await user.click(
      screen.getByTestId("view-ephemeral-source-cu_hmac_middleware_01")
    );
    const rawTextBox = await screen.findByTestId(
      "ephemeral-raw-text-cu_hmac_middleware_01"
    );
    expect(rawTextBox).toHaveTextContent("verifyWebhookSignature");
    expect(
      screen.getByTestId("ephemeral-result-cu_hmac_middleware_01")
    ).toHaveAttribute("data-resolution-ui-state", "MATCHED");

    // Strict zero browser-storage / URL / console leak check
    expect(localSpy.setItem).not.toHaveBeenCalled();
    expect(sessionSpy.setItem).not.toHaveBeenCalled();
    expect(localSpy.store.size).toBe(0);
    expect(sessionSpy.store.size).toBe(0);
    expect(window.location.href).not.toContain("verifyWebhookSignature");
    for (const spy of [logSpy, infoSpy, warnSpy, errorSpy]) {
      const serialized = JSON.stringify(spy.mock.calls);
      expect(serialized).not.toContain("verifyWebhookSignature");
    }

    // 2. CHANGED (REVISION_MISMATCH)
    await user.selectOptions(
      screen.getByTestId("ephemeral-resolution-override-select"),
      "REVISION_MISMATCH"
    );
    await user.click(
      screen.getByTestId("view-ephemeral-source-cu_hmac_middleware_01")
    );
    const changedRes = await screen.findByTestId(
      "ephemeral-result-cu_hmac_middleware_01"
    );
    expect(changedRes).toHaveAttribute("data-resolution-ui-state", "CHANGED");
    expect(changedRes).toHaveTextContent("REVISION_MISMATCH");

    // 3. UNAVAILABLE (retryable 503 -> shows retry button)
    await user.selectOptions(
      screen.getByTestId("ephemeral-resolution-override-select"),
      "UNAVAILABLE_RETRYABLE_503"
    );
    await user.click(
      screen.getByTestId("view-ephemeral-source-cu_hmac_middleware_01")
    );
    expect(
      await screen.findByTestId("retry-ephemeral-source-cu_hmac_middleware_01")
    ).toBeInTheDocument();

    // 4. UNAVAILABLE (provider 404 -> NO retry button, never claims user deletion/falsity)
    await user.selectOptions(
      screen.getByTestId("ephemeral-resolution-override-select"),
      "UNAVAILABLE_PROVIDER_404"
    );
    await user.click(
      screen.getByTestId("view-ephemeral-source-cu_hmac_middleware_01")
    );
    await waitFor(() => {
      expect(
        screen.getByTestId("ephemeral-result-cu_hmac_middleware_01")
      ).toHaveTextContent("사용자 삭제");
    });
    expect(
      screen.queryByTestId("retry-ephemeral-source-cu_hmac_middleware_01")
    ).not.toBeInTheDocument();

    // 5. POLICY_BLOCKED
    await user.selectOptions(
      screen.getByTestId("ephemeral-resolution-override-select"),
      "POLICY_BLOCKED"
    );
    await user.click(
      screen.getByTestId("view-ephemeral-source-cu_hmac_middleware_01")
    );
    await waitFor(() => {
      expect(
        screen.getByTestId("ephemeral-result-cu_hmac_middleware_01")
      ).toHaveAttribute("data-resolution-ui-state", "POLICY_BLOCKED");
    });

    // 6. PARTIAL_WITH_LIMIT
    await user.selectOptions(
      screen.getByTestId("ephemeral-resolution-override-select"),
      "PARTIAL_WITH_LIMIT"
    );
    await user.click(
      screen.getByTestId("view-ephemeral-source-cu_hmac_middleware_01")
    );
    await waitFor(() => {
      expect(
        screen.getByTestId("ephemeral-result-cu_hmac_middleware_01")
      ).toHaveAttribute("data-resolution-ui-state", "LIMIT");
    });
  });

  it("enforces non-optimistic Edit -> Needs Revalidation (approvable=false) -> Revalidate -> Approve and blocks UNSUPPORTED/STALE approval", async () => {
    const user = userEvent.setup();
    const adapter = new MockClaimReviewAdapter({ latencyMs: 0 });

    render(<ClaimReviewApp adapter={adapter} harness={adapter} />);
    await screen.findByTestId("claim-detail-panel");

    // 1. Blocked claim (UNSUPPORTED + STALE) disables approve & shows per-atom reasons
    await user.click(screen.getByTestId("select-claim-clm_blocked_03"));
    await waitFor(() => {
      expect(screen.getByTestId("claim-detail-panel")).toHaveAttribute(
        "data-claim-id",
        "clm_blocked_03"
      );
    });
    expect(screen.getByTestId("claim-approve-button")).toBeDisabled();
    expect(screen.getByTestId("claim-approval-blocked-banner")).toBeInTheDocument();
    expect(screen.getByTestId("claim-approval-block-reasons")).toHaveTextContent(
      "UNSUPPORTED_ATOM"
    );

    // 2. MIXED claim requires explicit "Acknowledge user-confirmed limitation" checkbox before Approve enables
    await user.click(screen.getByTestId("select-claim-clm_mixed_02"));
    await waitFor(() => {
      expect(screen.getByTestId("claim-detail-panel")).toHaveAttribute(
        "data-claim-id",
        "clm_mixed_02"
      );
    });
    expect(screen.getByTestId("claim-approve-button")).toBeDisabled();
    await user.click(
      screen.getByTestId("acknowledge-user-confirmed-checkbox")
    );
    expect(screen.getByTestId("claim-approve-button")).toBeEnabled();

    // 3. Edit creates new DRAFT v2 -> Needs Revalidation -> approvable=false -> Revalidate -> Approve
    await user.click(screen.getByTestId("claim-edit-button"));
    const textarea = screen.getByTestId("claim-edit-textarea");
    await user.clear(textarea);
    await user.type(
      textarea,
      "Implemented constant-time HMAC-SHA256 webhook signature middleware in src/webhook/signature.ts."
    );
    await user.click(screen.getByTestId("claim-edit-submit-button"));

    await screen.findByTestId("claim-needs-revalidation-banner");
    expect(screen.getByTestId("claim-workflow-status-badge")).toHaveTextContent(
      "DRAFT"
    );
    expect(screen.getByTestId("claim-approve-button")).toBeDisabled();

    // Run Atomization & Support Validation -> SUPPORT_VALIDATED + SOURCE_VERIFIED -> Approve enabled
    await user.click(screen.getByTestId("run-revalidation-button"));
    await waitFor(() => {
      expect(screen.getByTestId("claim-workflow-status-badge")).toHaveTextContent(
        "SUPPORT_VALIDATED"
      );
    });
    expect(screen.getByTestId("claim-approve-button")).toBeEnabled();

    await user.click(screen.getByTestId("claim-approve-button"));
    await waitFor(() => {
      expect(screen.getByTestId("claim-workflow-status-badge")).toHaveTextContent(
        "APPROVED"
      );
    });
    // Ensure client NEVER sent `verification` or `SOURCE_VERIFIED` in any mutation payload
    for (const call of adapter.mutationPayloads) {
      const json = JSON.stringify(call.body ?? {});
      expect(json).not.toContain('"verification"');
      expect(json).not.toContain("SOURCE_VERIFIED");
    }
  });

  it("displays MANUAL_ONLY_BLOCKED with 4 free alternatives, handles 409 conflict recovery with 0 auto-POST, and enforces entry guard / policy / deletion locks", async () => {
    const user = userEvent.setup();
    const adapter = new MockClaimReviewAdapter({ latencyMs: 0 });

    render(<ClaimReviewApp adapter={adapter} harness={adapter} />);
    await screen.findByTestId("claim-detail-panel");

    // 1. MANUAL_ONLY_BLOCKED on Generate Claims
    await user.click(screen.getByTestId("generate-claims-button"));
    const blockedBanner = await screen.findByTestId("manual-only-blocked-banner");
    expect(blockedBanner).toHaveTextContent("MANUAL_ONLY_BLOCKED");
    const alts = screen.getByTestId("manual-only-free-alternatives");
    expect(alts).toHaveTextContent("Reduce scope");
    expect(alts).toHaveTextContent("Manual retry after quota reset");
    expect(alts).toHaveTextContent("Non-AI curation / manual draft");
    expect(alts).toHaveTextContent("Synthetic fixture preview in prototype mode");

    // 2. 409 CLAIM_VERSION_CONFLICT recovery without auto-POST
    await user.click(screen.getByTestId("select-claim-clm_mixed_02"));
    await waitFor(() => {
      expect(screen.getByTestId("claim-detail-panel")).toHaveAttribute(
        "data-claim-id",
        "clm_mixed_02"
      );
    });
    await user.click(
      screen.getByTestId("acknowledge-user-confirmed-checkbox")
    );
    await user.click(
      screen.getByTestId("simulate-concurrent-claim-update-button")
    );
    const mutationsBefore = adapter.mutationPayloads.length;
    await user.click(screen.getByTestId("claim-approve-button"));

    const conflictBanner = await screen.findByTestId(
      "claim-conflict-recovery-banner"
    );
    expect(conflictBanner).toHaveTextContent("409 CLAIM_VERSION_CONFLICT");
    // Exactly 1 POST attempted (0 automatic retries!)
    expect(adapter.mutationPayloads.length).toBe(mutationsBefore + 1);

    await user.click(screen.getByTestId("reload-latest-claim-button"));
    await waitFor(() => {
      expect(
        screen.queryByTestId("claim-conflict-recovery-banner")
      ).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("claim-version-badge")).toHaveTextContent("v2");

    // 3. Policy blocked & Deletion pending lock mutations immediately
    await user.selectOptions(
      screen.getByTestId("claim-review-scenario-select"),
      "deletion_pending"
    );
    await screen.findByTestId("claim-deletion-pending-banner");
    expect(screen.getByTestId("generate-claims-button")).toBeDisabled();
    expect(screen.getByTestId("claim-edit-button")).toBeDisabled();

    // 4. Entry guard blocks when import_ready=false
    await user.selectOptions(
      screen.getByTestId("claim-review-scenario-select"),
      "import_not_ready"
    );
    expect(
      await screen.findByTestId("claim-review-entry-guard-blocked")
    ).toBeInTheDocument();
  });

  it("supports Fact Review (supports drill-down, approve -> USER_CONFIRMED, non-overwrite edit) and zero-quote Markdown/JSON Export + Delete", async () => {
    const user = userEvent.setup();
    const adapter = new MockClaimReviewAdapter({ latencyMs: 0 });

    render(<ClaimReviewApp adapter={adapter} harness={adapter} />);
    await screen.findByTestId("claim-detail-panel");

    // Switch to Facts tab
    await user.click(screen.getByTestId("tab-canonical-facts"));
    await screen.findByTestId("fact-review-panel");

    // Approve CANDIDATE fact -> USER_CONFIRMED
    await user.click(
      screen.getByTestId("fact-approve-button-fact_replay_window_03")
    );
    await waitFor(() => {
      expect(
        screen.getByTestId("fact-status-badge-fact_replay_window_03")
      ).toHaveTextContent("USER_CONFIRMED");
    });

    // Switch to Exports tab
    await user.click(screen.getByTestId("tab-zero-quote-exports"));
    await screen.findByTestId("claim-export-panel");

    // Verify pre-existing INVALIDATED export is clearly indicated
    expect(
      screen.getByTestId("export-invalidated-reason-exp_invalidated_01")
    ).toHaveTextContent("INVALIDATED");

    // Create zero-quote MARKDOWN export
    await user.click(screen.getByTestId("run-export-button"));
    const mdPreview = await screen.findByTestId("export-rendered-markdown");
    expect(mdPreview).toHaveTextContent("Zero raw source quotes");
    expect(screen.getByTestId("export-citations-manifest")).toHaveTextContent(
      "contains_raw_quotes=false"
    );

    // Create JSON export
    await user.selectOptions(screen.getByTestId("export-format-select"), "JSON");
    await user.click(screen.getByTestId("run-export-button"));
    const jsonPreview = await screen.findByTestId("export-rendered-json");
    expect(jsonPreview).toHaveTextContent('"contains_raw_quotes": false');

    // Delete invalidated export
    await user.click(screen.getByTestId("delete-export-exp_invalidated_01"));
    await waitFor(() => {
      expect(
        screen.queryByTestId("export-list-item-exp_invalidated_01")
      ).not.toBeInTheDocument();
    });
  });
});

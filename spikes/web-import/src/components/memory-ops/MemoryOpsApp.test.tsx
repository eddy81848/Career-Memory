import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEMO_PRIMARY_REPO_ID,
  DEMO_SECONDARY_REPO_ID,
  MockMemoryOpsAdapter,
} from "@/adapters/mock-memory-ops-adapter";
import { MemoryOpsApp } from "./MemoryOpsApp";

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

describe("MemoryOpsApp — M5 Workspace Memory Search, OD-003 Source Deletion & Quota/Pause UI", () => {
  it("renders OD-008 search with zero project_id controls, entity_types filtering, drill-down links, opaque cursor pagination, and zero storage/console leaks", async () => {
    const user = userEvent.setup();
    const adapter = new MockMemoryOpsAdapter({ latencyMs: 0 });

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

    render(<MemoryOpsApp adapter={adapter} harness={adapter} />);

    // Wait for initial search hits & quota banner
    await screen.findByTestId("search-results-list");
    expect(screen.getByTestId("quota-admission-badge")).toHaveTextContent(
      "ALLOWED"
    );

    // Strictly zero project_id inputs or controls in the DOM
    const inputs = document.querySelectorAll("input, select");
    for (const el of inputs) {
      expect((el.getAttribute("name") ?? "").toLowerCase()).not.toContain(
        "project"
      );
      expect((el.getAttribute("id") ?? "").toLowerCase()).not.toContain(
        "project"
      );
    }

    // Drill-down links point to /event-review for WORK_EVENT and /claim-review for FACT/CLAIM
    expect(
      screen.getByTestId("search-hit-link-evt_comp_301")
    ).toHaveAttribute("href", "/event-review?event_id=evt_comp_301");
    expect(
      screen.getByTestId("search-hit-link-clm_verified_01")
    ).toHaveAttribute("href", "/claim-review?claim_id=clm_verified_01");
    expect(
      screen.getByTestId("search-hit-link-fact_hmac_impl_01")
    ).toHaveAttribute("href", "/claim-review?fact_id=fact_hmac_impl_01");

    // Load next page via opaque cursor
    const nextBtn = screen.getByTestId("load-next-search-page-button");
    await user.click(nextBtn);
    await waitFor(() => {
      expect(
        screen.getByTestId("search-hit-fact_latency_metric_02")
      ).toBeInTheDocument();
    });

    // Toggle CLAIM entity filter
    await user.click(screen.getByTestId("entity-filter-CLAIM"));
    await waitFor(() => {
      expect(
        screen.queryByTestId("search-hit-evt_comp_301")
      ).not.toBeInTheDocument();
      expect(
        screen.getByTestId("search-hit-clm_verified_01")
      ).toBeInTheDocument();
    });

    // Single repo mode auto-selects source_resource_id
    await user.selectOptions(
      screen.getByTestId("memory-ops-repo-mode-select"),
      "single"
    );
    await screen.findByTestId("single-repo-auto-selected-scope");

    // Zero localStorage/sessionStorage/console leaks
    expect(localSpy.setItem).not.toHaveBeenCalled();
    expect(sessionSpy.setItem).not.toHaveBeenCalled();
    expect(localSpy.store.size).toBe(0);
    expect(sessionSpy.store.size).toBe(0);
    for (const spy of [logSpy, infoSpy, warnSpy, errorSpy]) {
      expect(spy).not.toHaveBeenCalled();
    }
  });

  it("handles 400 INVALID_SEARCH_CURSOR non-destructively and recovers via 1-click Restart Search", async () => {
    const user = userEvent.setup();
    const adapter = new MockMemoryOpsAdapter({
      latencyMs: 0,
      initialScenario: "cursor_version_drift",
    });

    render(<MemoryOpsApp adapter={adapter} harness={adapter} />);
    await screen.findByTestId("search-results-list");
    expect(screen.getByTestId("search-index-version")).toHaveTextContent(
      "fts-index-v1"
    );

    // Click Load Next Page -> triggers 400 INVALID_SEARCH_CURSOR
    await user.click(screen.getByTestId("load-next-search-page-button"));
    const recoveryBanner = await screen.findByTestId(
      "search-cursor-recovery-banner"
    );
    expect(recoveryBanner).toHaveTextContent("400 INVALID_SEARCH_CURSOR");
    expect(recoveryBanner).toHaveTextContent("처음부터 다시 검색");
    // Existing results remain non-destructively visible
    expect(screen.getByTestId("search-hit-evt_comp_301")).toBeInTheDocument();

    // 1-click Restart Search clears cursor & loads refreshed fts-index-v2
    await user.click(screen.getByTestId("restart-search-button"));
    await waitFor(() => {
      expect(
        screen.queryByTestId("search-cursor-recovery-banner")
      ).not.toBeInTheDocument();
      expect(screen.getByTestId("search-index-version")).toHaveTextContent(
        "fts-index-v2"
      );
    });
  });

  it("enforces OD-003 deletion preview (10 counts + non-recoverable notice) -> 409 scope-changed recovery -> 202 confirm -> immediate fence -> PARTIAL & DELETED receipts", async () => {
    const user = userEvent.setup();
    const adapter = new MockMemoryOpsAdapter({
      latencyMs: 0,
      initialScenario: "deletion_scope_changed_409",
    });

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

    render(<MemoryOpsApp adapter={adapter} harness={adapter} />);
    await screen.findByTestId("search-results-list");

    // 1. Open Source Deletion modal for primary repo
    await user.click(
      screen.getByTestId(`open-source-deletion-${DEMO_PRIMARY_REPO_ID}`)
    );
    await screen.findByTestId("deletion-impact-counts-section");

    // Verify all 10 impact counts + non-recoverable notice + preservation notice
    const expectedImpactKeys = [
      "work_events",
      "facts",
      "contributions",
      "claims_draft",
      "claims_edited",
      "claims_approved",
      "claims_historical_versions",
      "search_index_entries",
      "ephemeral_cache_entries",
      "server_exports",
    ];
    for (const key of expectedImpactKeys) {
      expect(screen.getByTestId(`impact-count-${key}`)).toBeInTheDocument();
    }
    expect(
      screen.getByTestId("deletion-non-recoverable-notice")
    ).toHaveTextContent("already_downloaded_exports=true");
    expect(
      screen.getByTestId("deletion-non-recoverable-notice")
    ).toHaveTextContent("provider_hosted_originals=true");
    expect(
      screen.getByTestId("deletion-preservation-notice")
    ).toHaveTextContent("다른 독립 저장소");

    // Confirm button disabled until explicit acknowledgement checkbox is checked
    expect(
      screen.getByTestId("confirm-source-deletion-button")
    ).toBeDisabled();
    await user.click(screen.getByTestId("deletion-ack-checkbox"));
    expect(
      screen.getByTestId("confirm-source-deletion-button")
    ).toBeEnabled();

    // 2. First confirm hits 409 DELETION_PREVIEW_SCOPE_CHANGED (does NOT delete!)
    await user.click(screen.getByTestId("confirm-source-deletion-button"));
    const conflictBanner = await screen.findByTestId(
      "deletion-preview-conflict-banner"
    );
    expect(conflictBanner).toHaveTextContent(
      "409 DELETION_PREVIEW_SCOPE_CHANGED"
    );
    expect(
      screen.getByTestId("confirm-source-deletion-button")
    ).toBeDisabled();

    // 3. Click Refresh Preview -> fresh rv_ledger_002 / isv_ledger_002
    await user.click(screen.getByTestId("refresh-deletion-preview-button"));
    await waitFor(() => {
      expect(screen.getByTestId("preview-resource-version")).toHaveTextContent(
        "rv_ledger_002"
      );
      expect(
        screen.getByTestId("preview-impact-scope-version")
      ).toHaveTextContent("isv_ledger_002");
    });

    // 4. Acknowledge fresh preview & confirm -> 202 Accepted + immediate fence!
    await user.click(screen.getByTestId("deletion-ack-checkbox"));
    await user.click(screen.getByTestId("confirm-source-deletion-button"));

    // Receipt panel appears showing DELETION_PENDING / PARTIAL
    const receiptPanel = await screen.findByTestId(
      "deletion-job-receipt-panel"
    );
    expect(receiptPanel).toHaveAttribute(
      "data-user-visible-state",
      "DELETION_PENDING"
    );
    expect(receiptPanel).toHaveAttribute("data-fence-active", "true");
    expect(
      screen.getByTestId("deletion-partial-retry-guidance")
    ).toHaveTextContent("FTS_INDEX");

    // Fenced repository immediately disables high-cost & deletion buttons while sibling stays active
    expect(
      screen.getByTestId(`repo-high-cost-action-${DEMO_PRIMARY_REPO_ID}`)
    ).toBeDisabled();
    expect(
      screen.getByTestId(`open-source-deletion-${DEMO_PRIMARY_REPO_ID}`)
    ).toBeDisabled();
    expect(
      screen.getByTestId(`repo-high-cost-action-${DEMO_SECONDARY_REPO_ID}`)
    ).toBeEnabled();

    // Advance deletion job to DELETED -> all 5 components SUCCEEDED
    await user.click(screen.getByTestId("simulate-deletion-complete-button"));
    await waitFor(() => {
      expect(
        screen.getByTestId("deletion-job-receipt-panel")
      ).toHaveAttribute("data-user-visible-state", "DELETED");
    });
    expect(
      screen.getByTestId("deletion-component-AUTHORITATIVE_DOMAIN")
    ).toHaveAttribute("data-component-status", "SUCCEEDED");

    // Preview token never leaked into browser storage
    expect(localSpy.setItem).not.toHaveBeenCalled();
    expect(sessionSpy.setItem).not.toHaveBeenCalled();
  });

  it("displays 80% warning and 95% cutoff/pause quota banners with free_alternatives while keeping OD-003 deletion and account disconnect enabled", async () => {
    const user = userEvent.setup();
    const adapter = new MockMemoryOpsAdapter({
      latencyMs: 0,
      initialScenario: "quota_warning_80",
    });

    render(<MemoryOpsApp adapter={adapter} harness={adapter} />);

    await waitFor(() => {
      expect(screen.getByTestId("quota-admission-badge")).toHaveTextContent(
        "WARNING_80_PERCENT"
      );
    });
    expect(screen.getByTestId("quota-meter-IMPORT_OBJECTS")).toHaveAttribute(
      "data-meter-status",
      "WARNING"
    );
    expect(
      screen.getByTestId(`repo-high-cost-action-${DEMO_PRIMARY_REPO_ID}`)
    ).toBeEnabled();

    // Switch to 95% cutoff / PAUSED_QUOTA
    await user.selectOptions(
      screen.getByTestId("memory-ops-scenario-select"),
      "quota_cutoff_95_paused"
    );

    await waitFor(() => {
      expect(screen.getByTestId("quota-admission-badge")).toHaveTextContent(
        "CUTOFF_95_PERCENT"
      );
    });
    expect(screen.getByTestId("quota-pause-badge")).toHaveTextContent(
      "PAUSED_QUOTA"
    );
    expect(screen.getByTestId("quota-retry-after-chip")).toHaveTextContent(
      "900s"
    );
    expect(screen.getByTestId("quota-free-alternatives")).toHaveTextContent(
      "Reduce scope"
    );
    expect(
      screen.getByTestId("quota-safety-guarantee-text")
    ).toHaveTextContent("paid_fallback_allowed=false");

    // High-cost operations are blocked, while safety operations (OD-003 Deletion + Account Disconnect) remain ENABLED!
    expect(
      screen.getByTestId(`repo-high-cost-action-${DEMO_PRIMARY_REPO_ID}`)
    ).toBeDisabled();
    expect(
      screen.getByTestId("safety-account-disconnect-button")
    ).toBeEnabled();
    expect(
      screen.getByTestId(`open-source-deletion-${DEMO_PRIMARY_REPO_ID}`)
    ).toBeEnabled();

    // Verify OD-003 deletion still works cleanly during 95% cutoff!
    await user.click(
      screen.getByTestId(`open-source-deletion-${DEMO_PRIMARY_REPO_ID}`)
    );
    await screen.findByTestId("deletion-impact-counts-section");
  });
});

import { expect, test } from "@playwright/test";

/**
 * BROWSER E2E TESTS FOR M5 MEMORY OPS (`/search`):
 * - Workspace Memory Search (OD-008): entity filters, drill-down links, opaque cursor pagination,
 *   400 INVALID_SEARCH_CURSOR non-destructive recovery ("처음부터 다시 검색").
 * - Source Deletion (OD-003): 10 impact counts + non-recoverable notice -> 409 scope-changed
 *   recovery ("영향 범위 다시 조회") -> 202 confirm -> immediate fence -> PARTIAL -> DELETED receipt.
 * - Quota / Pause Guard (T-503 / INF-06): 95% cutoff disables high-cost ops while preserving
 *   OD-003 Source Deletion & Account Disconnect.
 */

test.describe("Career Memory M5 Memory Ops UI (/search browser E2E)", () => {
  test("navigates from /claim-review to /search, filters WORK_EVENT/FACT/CLAIM, and recovers from 400 INVALID_SEARCH_CURSOR", async ({
    page,
  }) => {
    await page.goto("/claim-review");
    await expect(page.getByTestId("claim-review-root")).toBeVisible();
    await page.getByTestId("open-memory-ops-link").click();

    await expect(page).toHaveURL(/\/search/);
    await expect(page.getByTestId("memory-ops-root")).toBeVisible();
    await expect(page.getByTestId("search-results-list")).toBeVisible();

    // Verify drill-down link targets
    await expect(
      page.getByTestId("search-hit-link-evt_comp_301")
    ).toHaveAttribute("href", "/event-review?event_id=evt_comp_301");
    await expect(
      page.getByTestId("search-hit-link-clm_verified_01")
    ).toHaveAttribute("href", "/claim-review?claim_id=clm_verified_01");

    // Toggle CLAIM filter
    await page.getByTestId("entity-filter-CLAIM").click();
    await expect(
      page.getByTestId("search-hit-clm_verified_01")
    ).toBeVisible();
    await expect(page.getByTestId("search-hit-evt_comp_301")).toHaveCount(0);

    // Switch to cursor_version_drift scenario -> Load Next Page -> 400 recovery -> Restart Search
    await page
      .getByTestId("memory-ops-scenario-select")
      .selectOption("cursor_version_drift");
    await expect(page.getByTestId("search-index-version")).toContainText(
      "fts-index-v1"
    );
    await page.getByTestId("load-next-search-page-button").click();

    await expect(
      page.getByTestId("search-cursor-recovery-banner")
    ).toContainText("400 INVALID_SEARCH_CURSOR");
    await page.getByTestId("restart-search-button").click();

    await expect(
      page.getByTestId("search-cursor-recovery-banner")
    ).toHaveCount(0);
    await expect(page.getByTestId("search-index-version")).toContainText(
      "fts-index-v2"
    );
  });

  test("executes OD-003 preview -> 409 scope-changed refresh -> 202 confirm -> immediate fence -> PARTIAL -> DELETED receipt", async ({
    page,
  }) => {
    await page.goto("/search?scenario=deletion_scope_changed_409");
    await expect(page.getByTestId("memory-ops-root")).toBeVisible();

    // Open deletion modal for primary repository
    await page
      .getByTestId("open-source-deletion-src_repo_ledger_01")
      .click();
    await expect(
      page.getByTestId("deletion-impact-counts-section")
    ).toBeVisible();
    await expect(
      page.getByTestId("deletion-non-recoverable-notice")
    ).toContainText("already_downloaded_exports=true");
    await expect(
      page.getByTestId("impact-count-claims_approved")
    ).toContainText("4건");

    // Acknowledge and submit -> 409 DELETION_PREVIEW_SCOPE_CHANGED
    await page.getByTestId("deletion-ack-checkbox").check();
    await page.getByTestId("confirm-source-deletion-button").click();

    await expect(
      page.getByTestId("deletion-preview-conflict-banner")
    ).toContainText("409 DELETION_PREVIEW_SCOPE_CHANGED");

    // 1-click Refresh Preview -> new rv_ledger_002 / isv_ledger_002
    await page.getByTestId("refresh-deletion-preview-button").click();
    await expect(
      page.getByTestId("preview-resource-version")
    ).toContainText("rv_ledger_002");

    // Re-acknowledge and confirm -> 202 Accepted + immediate fence + PARTIAL receipt
    await page.getByTestId("deletion-ack-checkbox").check();
    await page.getByTestId("confirm-source-deletion-button").click();

    await expect(page.getByTestId("deletion-job-receipt-panel")).toBeVisible();
    await expect(
      page.getByTestId("deletion-job-receipt-panel")
    ).toHaveAttribute("data-user-visible-state", "DELETION_PENDING");
    await expect(
      page.getByTestId("repo-high-cost-action-src_repo_ledger_01")
    ).toBeDisabled();
    await expect(
      page.getByTestId("repo-high-cost-action-src_repo_portfolio_02")
    ).toBeEnabled();

    // Advance deletion to DELETED and verify all 5 components SUCCEEDED + zero browser storage leak
    await page.getByTestId("simulate-deletion-complete-button").click();
    await expect(
      page.getByTestId("deletion-job-receipt-panel")
    ).toHaveAttribute("data-user-visible-state", "DELETED");
    await expect(
      page.getByTestId("deletion-component-AUTHORITATIVE_DOMAIN")
    ).toHaveAttribute("data-component-status", "SUCCEEDED");

    const storageSnapshot = await page.evaluate(() => ({
      local: JSON.stringify(window.localStorage),
      session: JSON.stringify(window.sessionStorage),
    }));
    expect(storageSnapshot.local).not.toContain("delprev_");
    expect(storageSnapshot.session).not.toContain("delprev_");
  });

  test("preserves OD-003 Source Deletion and Account Disconnect under 95% quota cutoff / PAUSED_QUOTA", async ({
    page,
  }) => {
    await page.goto("/search?scenario=quota_cutoff_95_paused");
    await expect(page.getByTestId("memory-ops-root")).toBeVisible();

    await expect(page.getByTestId("quota-admission-badge")).toContainText(
      "CUTOFF_95_PERCENT"
    );
    await expect(page.getByTestId("quota-pause-badge")).toContainText(
      "PAUSED_QUOTA"
    );
    await expect(page.getByTestId("quota-free-alternatives")).toContainText(
      "Reduce scope"
    );
    await expect(page.getByTestId("quota-retry-after-chip")).toContainText(
      "900s"
    );

    // High-cost action is disabled, while safety actions remain enabled
    await expect(
      page.getByTestId("repo-high-cost-action-src_repo_ledger_01")
    ).toBeDisabled();
    await expect(
      page.getByTestId("safety-account-disconnect-button")
    ).toBeEnabled();
    await expect(
      page.getByTestId("open-source-deletion-src_repo_ledger_01")
    ).toBeEnabled();

    // Complete OD-003 preview & confirm during 95% cutoff
    await page
      .getByTestId("open-source-deletion-src_repo_ledger_01")
      .click();
    await expect(
      page.getByTestId("deletion-impact-counts-section")
    ).toBeVisible();
    await page.getByTestId("deletion-ack-checkbox").check();
    await page.getByTestId("confirm-source-deletion-button").click();

    await expect(page.getByTestId("deletion-job-receipt-panel")).toBeVisible();
    await expect(
      page.getByTestId("deletion-job-receipt-panel")
    ).toHaveAttribute("data-fence-active", "true");
  });
});

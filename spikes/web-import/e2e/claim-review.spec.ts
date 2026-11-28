import { expect, test } from "@playwright/test";

/**
 * BROWSER E2E TESTS FOR M4 GROUNDED CLAIM REVIEW, PROVENANCE DRILL-DOWN,
 * PLAN C EPHEMERAL SOURCE VIEWER, AND ZERO-QUOTE EXPORT (`/claim-review` + `/event-review` handoff).
 */

test.describe("Career Memory M4 Claim Review & Export UI (browser E2E)", () => {
  test("navigates from CONFIRMED Work Event on /event-review to /claim-review and inspects provenance drill-down & Plan C ephemeral viewer states with zero browser storage leaks", async ({
    page,
  }) => {
    await page.goto("/event-review");
    await expect(page.getByTestId("event-inbox-root")).toBeVisible();

    // Select pre-confirmed USER_COMPOSITE event (evt_comp_301) and follow handoff CTA
    await page.getByTestId("select-event-evt_comp_301").click();
    await expect(page.getByTestId("detail-status-badge")).toContainText(
      "CONFIRMED"
    );
    const handoffLink = page.getByTestId("review-grounded-claims-link");
    await expect(handoffLink).toBeVisible();
    await handoffLink.click();

    await expect(page).toHaveURL(/\/claim-review\?event_id=evt_comp_301/);
    await expect(page.getByTestId("claim-review-root")).toBeVisible();

    // 1. Strict separation of workflow status vs read-only computed_verification
    await expect(page.getByTestId("claim-workflow-status-badge")).toContainText(
      "APPROVED"
    );
    await expect(
      page.getByTestId("claim-computed-verification-badge")
    ).toContainText("SOURCE_VERIFIED · 읽기 전용");

    // 2. Provenance drill-down works in metadata-only mode BEFORE fetching source
    await expect(
      page.getByTestId("ephemeral-viewer-cu_hmac_middleware_01")
    ).toHaveAttribute("data-ephemeral-state", "IDLE_METADATA_ONLY");
    await expect(
      page.getByTestId("atom-role-grounding-atom_v01_role")
    ).toContainText("CLAIM-05");
    await expect(
      page.getByTestId("atom-metric-pills-atom_v01_metric")
    ).toContainText("6/6");

    // 3. Click "View Ephemeral Source" -> MATCHED (memory-only) + verify zero localStorage/sessionStorage leak
    await page.getByTestId("view-ephemeral-source-cu_hmac_middleware_01").click();
    await expect(
      page.getByTestId("ephemeral-raw-text-cu_hmac_middleware_01")
    ).toContainText("verifyWebhookSignature");

    const storageSnapshot = await page.evaluate(() => ({
      local: JSON.stringify(window.localStorage),
      session: JSON.stringify(window.sessionStorage),
    }));
    expect(storageSnapshot.local).not.toContain("verifyWebhookSignature");
    expect(storageSnapshot.session).not.toContain("verifyWebhookSignature");

    // 4. Verify CHANGED (REVISION_MISMATCH) & UNAVAILABLE (Provider 404) & POLICY_BLOCKED & LIMIT states
    await page
      .getByTestId("ephemeral-resolution-override-select")
      .selectOption("REVISION_MISMATCH");
    await page.getByTestId("view-ephemeral-source-cu_hmac_middleware_01").click();
    await expect(
      page.getByTestId("ephemeral-result-cu_hmac_middleware_01")
    ).toHaveAttribute("data-resolution-ui-state", "CHANGED");

    await page
      .getByTestId("ephemeral-resolution-override-select")
      .selectOption("UNAVAILABLE_PROVIDER_404");
    await page.getByTestId("view-ephemeral-source-cu_hmac_middleware_01").click();
    await expect(
      page.getByTestId("ephemeral-result-cu_hmac_middleware_01")
    ).toHaveAttribute("data-resolution-ui-state", "UNAVAILABLE");
    await expect(
      page.getByTestId("ephemeral-result-cu_hmac_middleware_01")
    ).toContainText("사용자 삭제");

    await page
      .getByTestId("ephemeral-resolution-override-select")
      .selectOption("POLICY_BLOCKED");
    await page.getByTestId("view-ephemeral-source-cu_hmac_middleware_01").click();
    await expect(
      page.getByTestId("ephemeral-result-cu_hmac_middleware_01")
    ).toHaveAttribute("data-resolution-ui-state", "POLICY_BLOCKED");

    await page
      .getByTestId("ephemeral-resolution-override-select")
      .selectOption("PARTIAL_WITH_LIMIT");
    await page.getByTestId("view-ephemeral-source-cu_hmac_middleware_01").click();
    await expect(
      page.getByTestId("ephemeral-result-cu_hmac_middleware_01")
    ).toHaveAttribute("data-resolution-ui-state", "LIMIT");
  });

  test("enforces blocked unsupported approval, non-optimistic edit -> revalidate -> approve, MANUAL_ONLY_BLOCKED, and zero-quote Markdown/JSON export", async ({
    page,
  }) => {
    await page.goto("/claim-review?event_id=evt_comp_301");
    await expect(page.getByTestId("claim-review-root")).toBeVisible();

    // 1. MANUAL_ONLY_BLOCKED banner with 4 free alternatives
    await page.getByTestId("generate-claims-button").click();
    await expect(page.getByTestId("manual-only-blocked-banner")).toBeVisible();
    await expect(
      page.getByTestId("manual-only-free-alternatives")
    ).toContainText("Reduce scope");

    // 2. Blocked claim (UNSUPPORTED + STALE) keeps Approve disabled with explicit reasons
    await page.getByTestId("select-claim-clm_blocked_03").click();
    await expect(page.getByTestId("claim-approve-button")).toBeDisabled();
    await expect(
      page.getByTestId("claim-approval-blocked-banner")
    ).toBeVisible();

    // 3. Edit MIXED claim -> new DRAFT v2 ("Needs Revalidation") -> Run Revalidation -> Approve
    await page.getByTestId("select-claim-clm_mixed_02").click();
    await expect(page.getByTestId("claim-approve-button")).toBeDisabled();
    await page.getByTestId("claim-edit-button").click();
    await page
      .getByTestId("claim-edit-textarea")
      .fill(
        "Implemented constant-time HMAC-SHA256 webhook signature verification middleware in production."
      );
    await page.getByTestId("claim-edit-submit-button").click();

    await expect(
      page.getByTestId("claim-needs-revalidation-banner")
    ).toBeVisible();
    await expect(page.getByTestId("claim-approve-button")).toBeDisabled();

    await page.getByTestId("run-revalidation-button").click();
    await expect(page.getByTestId("claim-workflow-status-badge")).toContainText(
      "SUPPORT_VALIDATED"
    );
    await expect(page.getByTestId("claim-approve-button")).toBeEnabled();

    await page.getByTestId("claim-approve-button").click();
    await expect(page.getByTestId("claim-workflow-status-badge")).toContainText(
      "APPROVED"
    );

    // 4. Zero-quote Markdown & JSON Export + Delete
    await page.getByTestId("tab-zero-quote-exports").click();
    await expect(page.getByTestId("claim-export-panel")).toBeVisible();

    await page.getByTestId("run-export-button").click();
    await expect(page.getByTestId("export-rendered-markdown")).toContainText(
      "Zero raw source quotes"
    );
    await expect(page.getByTestId("export-citations-manifest")).toContainText(
      "contains_raw_quotes=false"
    );

    await page.getByTestId("export-format-select").selectOption("JSON");
    await page.getByTestId("run-export-button").click();
    await expect(page.getByTestId("export-rendered-json")).toContainText(
      '"contains_raw_quotes": false'
    );

    await page.getByTestId("delete-export-exp_invalidated_01").click();
    await expect(
      page.getByTestId("export-list-item-exp_invalidated_01")
    ).toHaveCount(0);
  });
});

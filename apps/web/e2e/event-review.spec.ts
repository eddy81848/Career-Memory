import { expect, test } from "@playwright/test";

/**
 * BROWSER E2E TESTS FOR M3 EVENT REVIEW & CURATION INBOX (`/event-review` & entry gate on `/`).
 */

test.describe("Career Memory M3 Event Review Inbox (browser E2E)", () => {
  test("enforces entry gate on / (hidden on PARTIAL/BLOCKED_POLICY, enabled on SUCCESS) and enters Event Inbox", async ({
    page,
  }) => {
    await page.goto("/");
    await page.evaluate(() => window.sessionStorage.clear());
    await page.reload();
    await expect(page.getByTestId("service-auth-badge")).toHaveText("로그인 활성");

    // PARTIAL -> Event Inbox CTA MUST NOT be shown
    await page
      .getByLabel(/응답 시나리오/i)
      .selectOption("partial_non_retryable");
    await page.getByTestId("start-import-button").click();
    await expect(page.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "partial"
    );
    await expect(page.getByTestId("enter-event-inbox-cta")).toHaveCount(0);

    // Reset -> SUCCESS -> Event Inbox CTA appears and opens Event Inbox
    await page.getByRole("button", { name: "진행 결과 초기화" }).click();
    await page.getByLabel(/응답 시나리오/i).selectOption("success_full");
    await page.getByTestId("start-import-button").click();
    await expect(page.getByTestId("enter-event-inbox-cta")).toBeVisible();

    await page.getByTestId("enter-event-inbox-cta").click();
    await expect(page.getByTestId("event-inbox-root")).toBeVisible();
    await expect(page.getByTestId("active-event-count")).toContainText("5건");
  });

  test("full curation journey on /event-review: PR & DIRECT_COMMIT inspection -> Merge (USER_COMPOSITE single count) -> 409 conflict recovery -> Dissolve", async ({
    page,
  }) => {
    await page.goto("/event-review");
    await expect(page.getByTestId("event-inbox-root")).toBeVisible();
    await expect(page.getByTestId("active-event-count")).toContainText("5건");

    // 1. Inspect PR #104: MUST_LINK separated from CONTEXT_ONLY
    await expect(page.getByTestId("must-link-section")).toContainText("PR#104");
    await expect(page.getByTestId("context-only-section")).toContainText(
      "참고 문맥 (Context Only — 소속 근거 아님"
    );
    await expect(page.getByTestId("context-only-section")).toContainText(
      "ISSUE#41"
    );

    // 2. Inspect DIRECT_COMMIT on dynamic branch (trunk)
    await page.getByTestId("select-event-evt_dc_215").click();
    await expect(page.getByTestId("direct-commit-detail-box")).toContainText(
      "PR 없음 · default branch 직접 반영"
    );
    await expect(page.getByTestId("direct-commit-meta-chip")).toContainText(
      "default_branch: trunk"
    );
    await expect(page.getByTestId("direct-commit-detail-box")).toContainText(
      "PR 연결 미관측(NO_MATCH_OBSERVED) — 사용자 확인 필요"
    );

    // 3. Merge PR #104 + DIRECT_COMMIT #209 -> USER_COMPOSITE (active count 5 -> 4, members collapsed)
    await page.getByTestId("merge-checkbox-evt_pr_104").check();
    await page.getByTestId("merge-checkbox-evt_dc_209").check();
    await expect(page.getByTestId("open-merge-modal-button")).toBeEnabled();
    await page.getByTestId("open-merge-modal-button").click();

    await expect(page.getByTestId("merge-confirm-modal")).toBeVisible();
    await expect(page.getByTestId("merge-modal-impact-warning")).toContainText(
      "Fact 4건 · Claim 3건 · Export 1건"
    );
    await page.getByTestId("confirm-merge-submit-button").click();

    await expect(page.getByTestId("merge-confirm-modal")).toHaveCount(0);
    await expect(page.getByTestId("active-event-count")).toContainText("4건");
    await expect(page.getByTestId("event-list-item-evt_pr_104")).toHaveCount(0);
    await expect(page.getByTestId("event-list-item-evt_dc_209")).toHaveCount(0);
    await expect(page.getByTestId("detail-anchor-badge")).toContainText(
      "2개 Event를 사용자가 병합"
    );
    await expect(page.getByTestId("composite-member-snapshots")).toContainText(
      "evt_pr_104"
    );
    await expect(page.getByTestId("composite-member-snapshots")).toContainText(
      "evt_dc_209"
    );

    // 4. Trigger 409 EVENT_VERSION_CONFLICT on the new composite and recover safely
    await page.getByTestId("simulate-concurrent-update-button").click();
    await page.getByTestId("action-keep-button").click();

    await expect(page.getByTestId("version-conflict-banner")).toBeVisible();
    await expect(page.getByTestId("conflict-latest-meta")).toContainText(
      "최신 버전: v2"
    );
    await page.getByTestId("reload-latest-after-conflict-button").click();
    await expect(page.getByTestId("version-conflict-banner")).toHaveCount(0);
    await expect(page.getByTestId("detail-version-badge")).toContainText("v2");

    // 5. Dissolve the USER_COMPOSITE -> members restored & active count returns to 5
    await page.getByTestId("action-dissolve-button").click();
    await expect(page.getByTestId("dissolve-confirm-modal")).toBeVisible();
    await page.getByTestId("confirm-dissolve-submit-button").click();

    await expect(page.getByTestId("dissolve-confirm-modal")).toHaveCount(0);
    await expect(page.getByTestId("active-event-count")).toContainText("5건");
    await expect(page.getByTestId("event-list-item-evt_pr_104")).toBeVisible();
    await expect(page.getByTestId("event-list-item-evt_dc_209")).toBeVisible();
  });

  test("disables mutating actions on POLICY_BLOCKED and DELETION_PENDING scenarios", async ({
    page,
  }) => {
    await page.goto("/event-review");
    await page
      .getByTestId("event-review-scenario-select")
      .selectOption("policy_blocked");

    await expect(page.getByTestId("inbox-policy-blocked-banner")).toBeVisible();
    await expect(page.getByTestId("action-keep-button")).toBeDisabled();
    await expect(page.getByTestId("action-edit-button")).toBeDisabled();
    await expect(page.getByTestId("action-exclude-button")).toBeDisabled();

    await page
      .getByTestId("event-review-scenario-select")
      .selectOption("deletion_pending");

    await expect(
      page.getByTestId("inbox-deletion-pending-banner")
    ).toBeVisible();
    await expect(page.getByTestId("action-keep-button")).toBeDisabled();
  });
});

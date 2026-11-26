import { expect, test } from "@playwright/test";
import path from "node:path";

/**
 * BROWSER E2E TESTS AGAINST LOCAL BUILT NEXT.JS APP (UI FIXTURES ONLY).
 *
 * Validates real browser rendering, keyboard-only operation, duplicate click
 * suppression, partial/limit non-success presentation, retry gating,
 * permission revocation recovery, and real `page.reload()` state recovery.
 *
 * NOTE: Uses local UI fixtures only — does NOT validate real auth, GitHub API,
 * or backend Worker execution.
 */

const screenshotDir = process.env.CAPTURE_SCREENSHOTS_DIR;

async function maybeCapture(
  page: import("@playwright/test").Page,
  filename: string
) {
  if (!screenshotDir) {
    return;
  }
  await page.screenshot({
    path: path.join(screenshotDir, filename),
    fullPage: true,
  });
}

test.describe("Career Memory initial import UI prototype (browser E2E)", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.evaluate(() => window.sessionStorage.clear());
    await page.reload();
    await expect(page.getByTestId("service-auth-badge")).toHaveText("로그인 활성");
  });

  test("normal flow with keyboard-only repo selection and full readiness", async ({
    page,
  }) => {
    await expect(page.getByTestId("github-connection-badge")).toHaveText(
      "수집 연결됨"
    );

    const firstRepo = page.getByRole("radio", { name: /ledger-engine/i });
    await firstRepo.focus();
    await page.keyboard.press("ArrowDown");

    const secondRepo = page.getByRole("radio", { name: /design-tokens-kit/i });
    await expect(secondRepo).toBeChecked();

    const submitButton = page.getByTestId("start-import-button");
    await submitButton.focus();
    await page.keyboard.press("Enter");

    await expect(page.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "succeeded"
    );
    await expect(page.getByTestId("import-ready-badge")).toContainText(
      "import_ready: true (준비 완료)"
    );
    await expect(page.getByTestId("derived-ready-badge")).toHaveText(
      "파생 단계 전체 준비 완료"
    );
    await expect(page.getByTestId("retry-import-button")).toHaveCount(0);

    await maybeCapture(page, "01_normal_success.png");
  });

  test("distinguishes import_ready=true when derived stage fails", async ({
    page,
  }) => {
    await page
      .getByLabel(/응답 시나리오/i)
      .selectOption("success_import_ready_derived_pending");

    await page.getByTestId("start-import-button").click();

    await expect(page.getByTestId("derived-lag-notice")).toBeVisible();
    await expect(page.getByTestId("import-ready-badge")).toContainText(
      "import_ready: true (준비 완료)"
    );
    await expect(page.getByTestId("derived-ready-badge")).toHaveText(
      "파생 단계 미완료"
    );
    await expect(
      page.getByTestId("derived-stage-timeline_indexing")
    ).toContainText("실패 (미준비)");
  });

  test("suppresses duplicate click while running and recovers exact run on real browser reload", async ({
    page,
  }) => {
    await page
      .getByLabel(/응답 시나리오/i)
      .selectOption("running_slow_for_reload");

    const startButton = page.getByTestId("start-import-button");
    await startButton.click();

    await expect(page.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "running"
    );

    const runTextBeforeReload = await page
      .getByTestId("run-status-banner")
      .innerText();
    const runIdMatch = runTextBeforeReload.match(/run_demo_\d+/);
    expect(runIdMatch).not.toBeNull();
    const originalRunId = runIdMatch![0];

    // Attempt duplicate trigger while running (browsers deliver Enter/click to
    // aria-disabled buttons; Playwright's default click auto-waits for enabled)
    await startButton.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("duplicate-guard-alert")).toContainText(
      "중복 실행을 방지했으며"
    );

    // Real browser page reload while running
    await page.reload();

    await expect(page.getByTestId("reload-recovery-notice")).toContainText(
      originalRunId
    );
    await expect(page.getByTestId("run-status-banner")).toContainText(
      originalRunId
    );

    await maybeCapture(page, "04_reload_recovery.png");

    // Continues polling restored run until completion
    await expect(page.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "succeeded"
    );
  });

  test("shows partial and cap-limit outcomes distinctly from success and enforces server retry gating", async ({
    page,
  }) => {
    // 1. Partial result with retry denied
    await page
      .getByLabel(/응답 시나리오/i)
      .selectOption("partial_non_retryable");
    await page.getByTestId("start-import-button").click();

    await expect(page.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "partial"
    );
    await expect(page.getByTestId("run-status-badge")).toHaveText(
      "부분 결과 — 전체 성공 아님"
    );
    await expect(page.getByTestId("import-ready-badge")).toContainText(
      "import_ready: false"
    );
    await expect(page.getByTestId("retry-import-button")).toHaveCount(0);

    await maybeCapture(page, "02_partial_non_retryable.png");

    // 2. Partial result with retry allowed -> retry succeeds
    await page.getByRole("button", { name: "진행 결과 초기화" }).click();
    await page.getByLabel(/응답 시나리오/i).selectOption("partial_retryable");
    await page.getByTestId("start-import-button").click();

    await expect(page.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "partial"
    );
    await expect(page.getByTestId("retry-import-button")).toBeVisible();
    await page.getByTestId("retry-import-button").click();

    await expect(page.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "succeeded"
    );

    // 3. Cap exceeded limit (retry denied)
    await page.getByRole("button", { name: "진행 결과 초기화" }).click();
    await page.getByLabel(/응답 시나리오/i).selectOption("limit_cap_blocked");
    await page.getByRole("radio", { name: /telemetry-cli/i }).click();
    await page.getByTestId("start-import-button").click();

    await expect(page.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "limit_reached"
    );
    await expect(page.getByTestId("import-ready-badge")).toContainText(
      "import_ready: false"
    );
    await expect(page.getByTestId("retry-import-button")).toHaveCount(0);
  });

  test("handles permission revocation separately from service login and recovers on reconnect", async ({
    page,
  }) => {
    await page.getByLabel(/응답 시나리오/i).selectOption("permission_revoked");
    await page.getByTestId("start-import-button").click();

    await expect(page.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "permission_revoked"
    );
    await expect(page.getByTestId("service-auth-badge")).toHaveText("로그인 활성");
    await expect(page.getByTestId("github-connection-badge")).toHaveText(
      "권한 상실"
    );
    await expect(page.getByTestId("retry-import-button")).toHaveCount(0);

    await maybeCapture(page, "03_permission_revoked.png");

    await page.getByTestId("reconnect-github-from-run-button").click();
    await expect(page.getByTestId("github-connection-badge")).toHaveText(
      "수집 연결됨"
    );
  });

  test("displays unconfirmed guidance on session expiration and network failure", async ({
    page,
  }) => {
    await page
      .getByLabel(/응답 시나리오/i)
      .selectOption("session_expired_on_submit");
    await page.getByTestId("start-import-button").click();

    await expect(page.getByTestId("unconfirmed-fault-banner")).toContainText(
      "세션 만료 — 요청 미확정"
    );
    await expect(page.getByTestId("import-run-details")).toHaveCount(0);

    await page
      .getByRole("button", { name: "모의 서비스 다시 로그인" })
      .first()
      .click();
    await expect(page.getByTestId("service-auth-badge")).toHaveText("로그인 활성");

    await page
      .getByLabel(/응답 시나리오/i)
      .selectOption("network_error_on_submit");
    await page.getByTestId("start-import-button").click();

    await expect(page.getByTestId("unconfirmed-fault-banner")).toContainText(
      "네트워크 실패 — 서버 미확정"
    );
    await expect(page.getByTestId("import-run-details")).toHaveCount(0);
  });

  test("renders OpenAPI SUCCESS+BLOCKED_POLICY separately, attaches 409 existing_run_id, and handles bodyless 403/409/422 cleanly", async ({
    page,
  }) => {
    // 1. SUCCESS + BLOCKED_POLICY: historical import_ready=true separated from current blocked state
    await page
      .getByLabel(/응답 시나리오/i)
      .selectOption("success_blocked_policy");
    await page.getByTestId("start-import-button").click();

    await expect(page.getByTestId("processing-blocked-notice")).toBeVisible();
    await expect(page.getByTestId("import-ready-badge")).toContainText(
      "import_ready: true"
    );
    await expect(page.getByTestId("processing-status-badge")).toContainText(
      "BLOCKED_POLICY"
    );
    await expect(page.getByTestId("retry-import-button")).toHaveCount(0);

    // 2. 409 SYNC_ALREADY_RUNNING with existing_run_id -> navigates and completes
    await page.getByRole("button", { name: "진행 결과 초기화" }).click();
    await page
      .getByLabel(/응답 시나리오/i)
      .selectOption("conflict_existing_run");
    await page.getByTestId("start-import-button").click();

    await expect(page.getByTestId("reload-recovery-notice")).toContainText(
      "synthetic-run-existing-1"
    );
    await expect(page.getByTestId("run-status-banner")).toHaveAttribute(
      "data-run-status",
      "succeeded"
    );

    // 3. Bodyless 403, 409, 422 -> neutral generic guidance without crash
    await page.getByRole("button", { name: "진행 결과 초기화" }).click();
    await expect(page.getByTestId("import-run-details")).toHaveCount(0);
    for (const [scenario, statusText] of [
      ["empty_403", "HTTP 403"],
      ["empty_409", "HTTP 409"],
      ["empty_422", "HTTP 422"],
    ] as const) {
      await page.getByLabel(/응답 시나리오/i).selectOption(scenario);
      await page.getByTestId("start-import-button").click();
      await expect(page.getByTestId("unconfirmed-fault-banner")).toContainText(
        statusText
      );
      await expect(page.getByTestId("import-run-details")).toHaveCount(0);
    }
  });
});

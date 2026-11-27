import { defineConfig } from "@playwright/test";
import fs from "node:fs";

const CHROME_MAC_PATH =
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const executablePath = fs.existsSync(CHROME_MAC_PATH)
  ? CHROME_MAC_PATH
  : undefined;

const PORT = Number(process.env.E2E_PORT ?? 3105);

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: {
    timeout: 8_000,
  },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    headless: true,
    launchOptions: executablePath ? { executablePath } : undefined,
    trace: "retain-on-failure",
  },
  webServer: {
    command: `npx next start --hostname 127.0.0.1 --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});

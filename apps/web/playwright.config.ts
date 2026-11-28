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
    timeout: 10_000,
  },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    headless: true,
    launchOptions: executablePath ? { executablePath } : undefined,
    trace: "retain-on-failure",
  },
  webServer: {
    command: `sh -c 'mkdir -p .next/standalone/.next && cp -R .next/static .next/standalone/.next/ && ([ -d public ] && cp -R public .next/standalone/ || true) && HOSTNAME=127.0.0.1 PORT=${PORT} node .next/standalone/server.js'`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});

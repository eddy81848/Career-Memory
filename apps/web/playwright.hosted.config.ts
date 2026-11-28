import { defineConfig } from "@playwright/test";
import fs from "node:fs";

const CHROME_MAC_PATH =
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const executablePath = fs.existsSync(CHROME_MAC_PATH)
  ? CHROME_MAC_PATH
  : undefined;

const baseURL = (
  process.env.HOSTED_WEB_URL || "https://career-memory.site"
).replace(/\/+$/, "");

export default defineConfig({
  testDir: "./e2e-hosted",
  timeout: 45_000,
  expect: {
    timeout: 15_000,
  },
  fullyParallel: false,
  retries: 1,
  reporter: [["list"]],
  use: {
    baseURL,
    headless: true,
    launchOptions: executablePath ? { executablePath } : undefined,
    trace: "retain-on-failure",
  },
});

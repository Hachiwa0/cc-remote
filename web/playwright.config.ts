import { defineConfig, devices } from "@playwright/test";

const NEW_CHAT_CONTROL_TESTS = /new-chat controls|256-character profile id/;
const WEBKIT_RENDERING_TESTS =
  /mounted message image|two visible images|HTML preview|artifact-(?:svg|markdown-svg)|Codex settings|Claude settings|history page cache|instant session cache|session cache rejects|canonical image reference|fallback image preview|streaming rerenders|expanded tool batches|Mermaid|chat formulas|real wide Robot|pending composer image/;

export default defineConfig({
  testDir: "./tests",
  testMatch: "history-browser.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 2 : 0,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:4174",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npx vite --host 127.0.0.1 --port 4174",
    url: "http://127.0.0.1:4174/tests/history-browser.html",
    reuseExistingServer: false,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 900, height: 720 },
      },
    },
    {
      name: "webkit",
      grepInvert: [NEW_CHAT_CONTROL_TESTS, WEBKIT_RENDERING_TESTS],
      use: {
        ...devices["iPhone 15"],
      },
    },
    {
      // Keep every serial WebKit browser lifecycle below its macOS context-churn
      // cliff. A 64th+ context can stall before DOMContentLoaded even though the
      // same test passes alone, so independent rendering and control coverage
      // run in fresh browser processes without reducing the test matrix.
      name: "webkit-rendering",
      grep: WEBKIT_RENDERING_TESTS,
      use: {
        ...devices["iPhone 15"],
      },
    },
    {
      name: "webkit-controls",
      grep: NEW_CHAT_CONTROL_TESTS,
      use: {
        ...devices["iPhone 15"],
      },
    },
  ],
});

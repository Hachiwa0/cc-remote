import base from "./playwright.config";

/** Explicitly-run WebKit interaction gate. It stays separate from the default
 * browser suite because it depends on the optional Playwright WebKit runtime. */
export default {
  ...base,
  testMatch: /history-jitter\.spec\.ts/,
  retries: 0,
};

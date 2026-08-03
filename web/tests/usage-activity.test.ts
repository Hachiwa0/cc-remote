import assert from "node:assert/strict";

import {
  buildUsageActivityCalendar,
  compactTokenCount,
  formatUsageDate,
  formatUsageDuration,
  USAGE_ACTIVITY_DAYS,
  USAGE_ACTIVITY_WEEKS,
} from "../src/usage-activity.js";

const calendar = buildUsageActivityCalendar([
  { start_date: "2025-08-02", tokens: 9_999 },
  { start_date: "2026-07-25", tokens: Number.MAX_SAFE_INTEGER + 1 },
  { start_date: "2026-07-26", tokens: 500 },
  { start_date: "2026-07-27", tokens: 501 },
  { start_date: "2026-07-28", tokens: 1_000 },
  { start_date: "2026-07-29", tokens: 1_001 },
  { start_date: "2026-07-30", tokens: 1_501 },
  { start_date: "2026-07-31", tokens: 1 },
  { start_date: "2026-08-01", tokens: 100 },
  { start_date: "2026-08-02", tokens: 1_000 },
  { start_date: "2026-08-02", tokens: 2_000 },
  { start_date: "2026-08-03", tokens: 8_888 },
  { start_date: "2026-02-30", tokens: 7_777 },
], "2026-08-02");

assert.equal(USAGE_ACTIVITY_WEEKS, 53);
assert.equal(USAGE_ACTIVITY_DAYS, 371);
assert.equal(calendar.days.length, 371);
assert.equal(calendar.startDate, "2025-08-03");
assert.equal(calendar.endDate, "2026-08-08");
assert.equal(calendar.days[0]?.weekday, 0);
assert.equal(calendar.days.at(-1)?.weekday, 6);
assert.equal(calendar.days.at(-1)?.future, true);

const byDate = new Map(calendar.days.map((day) => [day.date, day]));
assert.equal(byDate.get("2026-07-25")?.tokens, 0);
assert.equal(byDate.get("2026-07-26")?.level, 1);
assert.equal(byDate.get("2026-07-27")?.level, 2);
assert.equal(byDate.get("2026-07-28")?.level, 2);
assert.equal(byDate.get("2026-07-29")?.level, 3);
assert.equal(byDate.get("2026-07-30")?.level, 4);
assert.equal(byDate.get("2026-07-31")?.level, 1);
assert.equal(byDate.get("2026-08-01")?.level, 1);
assert.deepEqual(byDate.get("2026-08-02"), {
  date: "2026-08-02",
  tokens: 2_000,
  level: 4,
  future: false,
  week: 52,
  weekday: 0,
});
assert.equal(byDate.get("2026-08-03")?.tokens, 0);
assert.ok(calendar.months.length >= 12);
assert.equal(calendar.months[0]?.week, 0);

assert.equal(compactTokenCount(null), "—");
assert.equal(compactTokenCount(999), "999");
assert.equal(compactTokenCount(9_999), "9,999");
assert.equal(compactTokenCount(12_000), "1.2万");
assert.equal(compactTokenCount(12_400_000), "1240万");
assert.equal(compactTokenCount(152_812_481), "1.53亿");
assert.equal(compactTokenCount(2_650_000_000), "26.5亿");
assert.equal(compactTokenCount(1_520_000_000_000), "1.52兆");
assert.equal(formatUsageDuration(null), "—");
assert.equal(formatUsageDuration(42), "42秒");
assert.equal(formatUsageDuration(3_720), "1小时 2分");
assert.equal(formatUsageDuration(93_600), "1天 2小时");
assert.match(formatUsageDate("2026-08-02"), /2026年.*8月.*2日/);
assert.equal(formatUsageDate("not-a-date"), "not-a-date");

console.log("usage activity tests passed");

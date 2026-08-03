import type { StatusDailyUsageBucket } from "./protocol";

export const USAGE_ACTIVITY_WEEKS = 53;
export const USAGE_ACTIVITY_DAYS = USAGE_ACTIVITY_WEEKS * 7;

const DAY_MS = 24 * 60 * 60 * 1000;
const DATE_KEY = /^\d{4}-\d{2}-\d{2}$/;

export type UsageActivityLevel = 0 | 1 | 2 | 3 | 4;

export interface UsageActivityDay {
  date: string;
  tokens: number;
  level: UsageActivityLevel;
  future: boolean;
  week: number;
  weekday: number;
}

export interface UsageActivityMonth {
  label: string;
  week: number;
}

export interface UsageActivityCalendar {
  days: UsageActivityDay[];
  months: UsageActivityMonth[];
  startDate: string;
  endDate: string;
}

function parseDateKey(value: string): number | null {
  if (!DATE_KEY.test(value)) return null;
  const [year, month, day] = value.split("-").map(Number);
  const instant = Date.UTC(year, month - 1, day);
  const parsed = new Date(instant);
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day
    ? instant : null;
}

function dateKey(instant: number): string {
  return new Date(instant).toISOString().slice(0, 10);
}

function monthLabel(instant: number): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    timeZone: "UTC",
  }).format(new Date(instant));
}

function activityLevel(tokens: number, maximum: number): UsageActivityLevel {
  if (tokens <= 0 || maximum <= 0) return 0;
  // Match Codex's own activity chart: every non-zero day is graded against
  // the busiest day in the visible window, not against fixed token bands.
  const ratio = tokens / maximum;
  if (ratio > 0.75) return 4;
  if (ratio > 0.5) return 3;
  if (ratio > 0.25) return 2;
  return 1;
}

export function localDateKey(now = new Date()): string {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function buildUsageActivityCalendar(
  buckets: readonly StatusDailyUsageBucket[] = [],
  today = localDateKey(),
): UsageActivityCalendar {
  const todayInstant = parseDateKey(today) ?? parseDateKey(localDateKey())!;
  const endInstant = todayInstant + (6 - new Date(todayInstant).getUTCDay()) * DAY_MS;
  const startInstant = endInstant - (USAGE_ACTIVITY_DAYS - 1) * DAY_MS;

  const totals = new Map<string, number>();
  for (const bucket of buckets) {
    const instant = parseDateKey(bucket.start_date);
    if (instant === null
        || instant < startInstant
        || instant > todayInstant
        || !Number.isSafeInteger(bucket.tokens)
        || bucket.tokens < 0) continue;
    totals.set(bucket.start_date, Math.max(
      totals.get(bucket.start_date) ?? 0,
      bucket.tokens,
    ));
  }
  const maximum = Math.max(0, ...totals.values());
  const days = Array.from({ length: USAGE_ACTIVITY_DAYS }, (_, index) => {
    const instant = startInstant + index * DAY_MS;
    const current = dateKey(instant);
    const future = instant > todayInstant;
    const tokens = future ? 0 : totals.get(current) ?? 0;
    return {
      date: current,
      tokens,
      level: activityLevel(tokens, maximum),
      future,
      week: Math.floor(index / 7),
      weekday: index % 7,
    } satisfies UsageActivityDay;
  });

  const months: UsageActivityMonth[] = [];
  let lastLabel = "";
  for (let week = 0; week < USAGE_ACTIVITY_WEEKS; week += 1) {
    const weekStart = startInstant + week * 7 * DAY_MS;
    let marker = weekStart;
    if (week > 0) {
      for (let weekday = 0; weekday < 7; weekday += 1) {
        const candidate = weekStart + weekday * DAY_MS;
        if (new Date(candidate).getUTCDate() === 1) {
          marker = candidate;
          break;
        }
      }
      if (new Date(marker).getUTCDate() !== 1) continue;
    }
    const label = monthLabel(marker);
    if (label !== lastLabel) {
      months.push({ label, week });
      lastLabel = label;
    }
  }

  return {
    days,
    months,
    startDate: dateKey(startInstant),
    endDate: dateKey(endInstant),
  };
}

export function compactTokenCount(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const absolute = Math.abs(value);
  const units = [
    { size: 1_000_000_000_000, suffix: "兆" },
    { size: 100_000_000, suffix: "亿" },
    { size: 10_000, suffix: "万" },
  ];
  const unit = units.find((candidate) => absolute >= candidate.size);
  if (!unit) return Math.round(value).toLocaleString("zh-CN");
  const scaled = value / unit.size;
  const digits = Math.abs(scaled) < 10 ? 2
    : Math.abs(scaled) < 100 ? 1 : 0;
  const compact = scaled.toFixed(digits)
    .replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1");
  return `${compact}${unit.suffix}`;
}

export function formatUsageDuration(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const totalMinutes = Math.floor(seconds / 60);
  const days = Math.floor(totalMinutes / (24 * 60));
  const hours = Math.floor(totalMinutes / 60) % 24;
  const minutes = totalMinutes % 60;
  if (days > 0) return `${days}天 ${hours}小时`;
  if (hours > 0) return `${hours}小时 ${minutes}分`;
  if (minutes > 0) return `${minutes}分`;
  return `${Math.floor(seconds)}秒`;
}

export function formatUsageDate(value: string): string {
  const instant = parseDateKey(value);
  if (instant === null) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "short",
    timeZone: "UTC",
  }).format(new Date(instant));
}

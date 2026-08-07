import type {
  StatusRateLimit,
  StatusRateWindow,
  StatusReport,
} from "./protocol";

export interface QuotaWindows {
  fiveHour: StatusRateWindow | null;
  weekly: StatusRateWindow | null;
  overall: StatusRateWindow | null;
  limit: StatusRateLimit | null;
}

export type QuotaTone = "unknown" | "good" | "warn" | "critical";

const clampPercent = (value: number): number =>
  Math.max(0, Math.min(100, value));

export function remainingPercent(
  window?: StatusRateWindow | null,
): number | null {
  if (window?.used_percent == null || !Number.isFinite(window.used_percent)) {
    return null;
  }
  return clampPercent(100 - window.used_percent);
}

export function accountQuotaWindows(
  report?: StatusReport | null,
): QuotaWindows {
  const limits = report?.rate_limits ?? [];
  const hasWindow = (limit: StatusRateLimit, duration: number): boolean =>
    [limit.primary, limit.secondary].some(
      (window) => window?.window_duration_mins === duration,
    );
  const hasUsableWindow = (limit: StatusRateLimit): boolean =>
    [limit.primary, limit.secondary].some((window) =>
      window != null && (
        window.used_percent != null
        || window.resets_at != null
        || window.window_duration_mins != null
      )
    );
  const hasAccountWindow = (limit: StatusRateLimit): boolean =>
    hasWindow(limit, 300) || hasWindow(limit, 10_080);
  // Windows belong to a particular rate-limit bucket.  Never combine a
  // five-hour window from one bucket with a weekly window from another. Codex
  // may also report model-specific buckets beside the account-wide "codex"
  // bucket; those are not substitutes when the account bucket is temporarily
  // absent. An id-less bucket is retained only for older app-server versions.
  const legacyLimits = limits.filter((candidate) =>
    candidate.limit_id == null && hasAccountWindow(candidate)
  );
  const limit = limits.find((candidate) =>
    candidate.limit_id === "codex" && hasUsableWindow(candidate)
  ) ?? legacyLimits.find((candidate) =>
    hasWindow(candidate, 300) && hasWindow(candidate, 10_080),
  ) ?? legacyLimits[0] ?? null;
  const windows = limit == null ? [] : [limit.primary, limit.secondary].filter(
    (window): window is StatusRateWindow => window != null,
  );
  const fiveHour = windows.find(
    (window) => window.window_duration_mins === 300,
  ) ?? null;
  const weekly = windows.find(
    (window) => window.window_duration_mins === 10_080,
  ) ?? null;
  // Free accounts currently expose one authoritative long-lived "codex"
  // bucket instead of the paid-plan 5-hour/weekly pair. Preserve the native
  // shape and present it as a single overall quota; never relabel it as a
  // weekly window.
  const overall = fiveHour == null && weekly == null
    ? windows[0] ?? null
    : null;
  return {
    fiveHour,
    weekly,
    overall,
    limit,
  };
}

export function quotaTone(value: number | null): QuotaTone {
  if (value == null) return "unknown";
  if (value <= 20) return "critical";
  if (value <= 50) return "warn";
  return "good";
}

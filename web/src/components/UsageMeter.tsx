import { useEffect, useRef } from "react";
import type { StatusRateWindow, StatusReport } from "../protocol";
import {
  accountQuotaWindows,
  quotaTone,
  remainingPercent,
} from "../rate-limit-usage";

interface Props {
  open: boolean;
  report: StatusReport | null;
  error?: string | null;
  loading?: boolean;
  onToggle: () => void;
  onRefresh: () => void;
  onOpenStatus?: () => void;
}

function compactPercent(value: number | null): string {
  return value == null ? "—" : `${value.toFixed(0)}%`;
}

function resetTime(value?: number | null): string {
  if (!value) return "重置时间未知";
  return `${new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value * 1000))} 重置`;
}

function MiniQuota({
  label,
  value,
}: {
  label: string;
  value: number | null;
}) {
  const tone = quotaTone(value);
  return <span className="usage-meter-line">
    <b>{label}</b>
    <i><em className={tone} style={{ width: `${value ?? 0}%` }} /></i>
  </span>;
}

function QuotaRow({
  label,
  window,
  showDuration = false,
}: {
  label: string;
  window: StatusRateWindow | null;
  showDuration?: boolean;
}) {
  const remaining = remainingPercent(window);
  const tone = quotaTone(remaining);
  return <div className="usage-pop-window">
    <div>
      <span>{label}</span>
      <b>{remaining == null ? "—" : `剩余 ${remaining.toFixed(0)}%`}</b>
    </div>
    <i><span className={tone} style={{ width: `${remaining ?? 0}%` }} /></i>
    <small>
      {showDuration && window?.window_duration_mins != null
        ? `${window.window_duration_mins} 分钟窗口 · `
        : ""}
      {resetTime(window?.resets_at)}
    </small>
  </div>;
}

export function UsageMeter({
  open,
  report,
  error,
  loading = false,
  onToggle,
  onRefresh,
  onOpenStatus,
}: Props) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onToggle();
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [onToggle, open]);

  // A failed refresh after an account-switch barrier means the retained report
  // may belong to the previous account. Quarantine it until a fresh snapshot
  // succeeds instead of presenting stale values as current.
  const visibleReport = error ? null : report;
  const quotas = accountQuotaWindows(visibleReport);
  const fiveHour = remainingPercent(quotas.fiveHour);
  const weekly = remainingPercent(quotas.weekly);
  const overall = remainingPercent(quotas.overall);
  const minimum = [fiveHour, weekly, overall].filter(
    (value): value is number => value != null,
  ).reduce<number | null>(
    (current, value) => current == null ? value : Math.min(current, value),
    null,
  );
  const summaryTone = quotaTone(minimum);
  const plan = quotas.limit?.plan_type ?? visibleReport?.account?.plan_type;
  const hasOverallQuota = quotas.overall != null;
  const quotaSummary = hasOverallQuota
    ? `Codex 总额度${compactPercent(overall)}`
    : `5小时${compactPercent(fiveHour)}，每周${compactPercent(weekly)}`;

  return <div className="usage-meter-wrap">
    <button
      ref={triggerRef}
      type="button"
      className={`usage-meter ${summaryTone}`}
      aria-label={`账户额度：${quotaSummary}`}
      aria-expanded={open}
      aria-controls="codex-account-usage-popover"
      aria-haspopup="true"
      title="Codex 账户剩余额度"
      onClick={onToggle}
    >
      {hasOverallQuota
        ? <MiniQuota label="总" value={overall} />
        : <>
          <MiniQuota label="5h" value={fiveHour} />
          <MiniQuota label="周" value={weekly} />
        </>}
    </button>
    {open && <div
      id="codex-account-usage-popover"
      className="ctx-pop usage-pop"
      role="region"
      aria-label="Codex 账户额度"
    >
      <div className="usage-pop-head">
        <span>
          <b>账户额度</b>
          <small>
            {hasOverallQuota ? "当前账户的 Codex 总额度" : "5 小时与每周窗口"}
          </small>
        </span>
        {plan && <em>{plan}</em>}
      </div>
      {error ? (
        <div className="ctx-pop-loading" role="alert">{error}</div>
      ) : !visibleReport && loading ? (
        <div className="ctx-pop-loading">正在读取账户额度…</div>
      ) : !quotas.fiveHour && !quotas.weekly && !quotas.overall ? (
        <div className="ctx-pop-loading">
          {visibleReport?.account?.auth_type === "chatgpt"
            ? "账户已登录；本次额度读取失败，请刷新重试。"
            : "当前 Codex app-server 暂未提供账户额度。"}
        </div>
      ) : hasOverallQuota ? (
        <QuotaRow
          label="Codex 总额度"
          window={quotas.overall}
          showDuration
        />
      ) : <>
          <QuotaRow label="5 小时额度" window={quotas.fiveHour} />
          <QuotaRow label="每周额度" window={quotas.weekly} />
        </>}
      <div className="usage-pop-actions">
        <span>{error ? "旧账户数据已隐藏"
          : loading ? "正在更新…" : "来自当前 Codex 账户"}</span>
        <button type="button" onClick={onRefresh} disabled={loading}>刷新</button>
        {onOpenStatus && <button type="button" onClick={onOpenStatus}>完整状态</button>}
      </div>
    </div>}
  </div>;
}

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
}: {
  label: string;
  window: StatusRateWindow | null;
}) {
  const remaining = remainingPercent(window);
  const tone = quotaTone(remaining);
  return <div className="usage-pop-window">
    <div>
      <span>{label}</span>
      <b>{remaining == null ? "—" : `剩余 ${remaining.toFixed(0)}%`}</b>
    </div>
    <i><span className={tone} style={{ width: `${remaining ?? 0}%` }} /></i>
    <small>{resetTime(window?.resets_at)}</small>
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
  const minimum = [fiveHour, weekly].filter(
    (value): value is number => value != null,
  ).reduce<number | null>(
    (current, value) => current == null ? value : Math.min(current, value),
    null,
  );
  const summaryTone = quotaTone(minimum);
  const plan = quotas.limit?.plan_type ?? visibleReport?.account?.plan_type;

  return <div className="usage-meter-wrap">
    <button
      ref={triggerRef}
      type="button"
      className={`usage-meter ${summaryTone}`}
      aria-label={`账户额度：5小时${compactPercent(fiveHour)}，每周${compactPercent(weekly)}`}
      aria-expanded={open}
      aria-controls="codex-account-usage-popover"
      aria-haspopup="true"
      title="Codex 账户剩余额度"
      onClick={onToggle}
    >
      <MiniQuota label="5h" value={fiveHour} />
      <MiniQuota label="周" value={weekly} />
    </button>
    {open && <div
      id="codex-account-usage-popover"
      className="ctx-pop usage-pop"
      role="region"
      aria-label="Codex 账户额度"
    >
      <div className="usage-pop-head">
        <span><b>账户额度</b><small>5 小时与每周窗口</small></span>
        {plan && <em>{plan}</em>}
      </div>
      {error ? (
        <div className="ctx-pop-loading" role="alert">{error}</div>
      ) : !visibleReport && loading ? (
        <div className="ctx-pop-loading">正在读取账户额度…</div>
      ) : !quotas.fiveHour && !quotas.weekly ? (
        <div className="ctx-pop-loading">当前 Codex app-server 暂未提供账户额度。</div>
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

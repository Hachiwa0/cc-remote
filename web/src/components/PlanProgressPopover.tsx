import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";
import type { ProcessBlock } from "../domain/conversation";
import { Icon } from "../icons";
import { PointerTapGuard } from "../pointer-tap";

interface PlanPopoverPosition {
  left: number;
  width: number;
  maxHeight: number;
  top?: number;
  bottom?: number;
}

export function PlanProgressPopover({ block, openOverride, onOpenChange,
  detailLoading = false, onNeedDetail }: {
  block: ProcessBlock;
  openOverride?: boolean;
  onOpenChange?: (open: boolean) => void;
  detailLoading?: boolean;
  onNeedDetail?: () => void;
}) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const [position, setPosition] = useState<PlanPopoverPosition | null>(null);
  const open = openOverride ?? uncontrolledOpen;
  const rootRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLElement>(null);
  const tapGuard = useRef(new PointerTapGuard());
  const labelId = useId();
  const steps = block.plan ?? [];
  const completed = steps.filter((entry) => entry.status === "completed").length;
  const current = steps.find((entry) => entry.status === "inProgress");
  const failed = ["failed", "declined", "cancelled", "interrupted"]
    .includes(block.status);
  // A terminal turn closes any still-open process item as `succeeded`; that
  // says the turn ended cleanly, not that every structured plan step ran.
  // Structured step state is therefore authoritative whenever it exists.
  const complete = !failed && steps.length > 0
    && completed === steps.length;
  const progress = steps.length > 0
    ? Math.min(100, completed / steps.length * 100)
    : 0;
  const progressLabel = steps.length > 0
    ? `${completed} / ${steps.length}`
    : block.done ? "已记录" : "执行中";
  const description = block.explanation || block.summary;
  const fallbackDetail = steps.length === 0
    ? block.detail || block.output || block.progress
    : null;

  const setOpen = useCallback((next: boolean) => {
    setUncontrolledOpen(next);
    onOpenChange?.(next);
  }, [onOpenChange]);

  useLayoutEffect(() => {
    if (!open) {
      setPosition(null);
      return;
    }
    const place = () => {
      const trigger = rootRef.current?.getBoundingClientRect();
      if (!trigger) return;
      // WebKit's fixed-position layout viewport can differ from innerWidth by
      // a few CSS pixels around the safe-area. Use the document viewport and a
      // wider gutter so the card never grazes an iPhone edge.
      const gutter = 20;
      const viewportWidth = Math.min(
        window.innerWidth,
        document.documentElement.clientWidth,
      );
      const width = Math.min(360, viewportWidth - gutter * 2);
      const left = Math.min(
        Math.max(gutter, trigger.right - width),
        viewportWidth - width - gutter,
      );
      const below = window.innerHeight - trigger.bottom - gutter;
      const above = trigger.top - gutter;
      const openUp = below < 240 && above > below;
      const available = Math.max(96, (openUp ? above : below) - 6);
      setPosition({
        left,
        width,
        maxHeight: Math.min(420, available),
        ...(openUp
          ? { bottom: window.innerHeight - trigger.top + 6 }
          : { top: trigger.bottom + 6 }),
      });
    };
    place();
    window.addEventListener("resize", place);
    document.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      document.removeEventListener("scroll", place, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (rootRef.current?.contains(target) || cardRef.current?.contains(target)) return;
      setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside, true);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside, true);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open, setOpen]);

  return <div ref={rootRef} className="plan-progress-control">
    <button type="button"
      className={`plan-progress-trigger${complete ? " complete" : ""}${failed ? " failed" : ""}`}
      aria-expanded={open} aria-controls={labelId}
      aria-label={`查看计划进度，${progressLabel}`}
      title={`计划进度 · ${progressLabel}`}
      onPointerDown={(event) => tapGuard.current.pointerDown(
        event.pointerId, event.clientX, event.clientY)}
      onPointerMove={(event) => tapGuard.current.pointerMove(
        event.pointerId, event.clientX, event.clientY)}
      onPointerUp={(event) => tapGuard.current.pointerUp(event.pointerId)}
      onPointerCancel={(event) => tapGuard.current.pointerCancel(event.pointerId)}
      onClick={(event) => {
        if (!tapGuard.current.consumeClick(event.detail)) {
          event.preventDefault();
          return;
        }
        const next = !open;
        // Cached projection steps paint immediately after refresh, but they do
        // not replace the authoritative turn detail. Opening the popover must
        // still refresh when its owner advertises deferred detail.
        if (next) onNeedDetail?.();
        setOpen(next);
      }}>
      <span className="plan-progress-ring" aria-hidden="true"
        style={{ "--plan-progress": `${progress * 3.6}deg` } as CSSProperties}>
        <Icon name={complete ? "verify" : "plan"} size={12} />
      </span>
    </button>
    {open && position && createPortal(
      <section ref={cardRef} id={labelId} className="plan-progress-popover"
        role="dialog" aria-label="计划进度" style={position}>
        <header>
          <span className={`plan-progress-mark${complete ? " complete" : ""}${failed ? " failed" : ""}`}>
            <Icon name={complete ? "verify" : "plan"} size={15} />
          </span>
          <span>
            <b>计划</b>
            <small>{detailLoading ? "正在同步" : failed ? "执行异常"
              : complete ? "全部完成" : block.done ? "执行已结束"
                : current ? "正在执行" : "等待执行"}</small>
          </span>
          <strong>{progressLabel}</strong>
        </header>
        {description && <p>{description}</p>}
        {steps.length > 0 ? <ol>
          {steps.map((entry, index) => (
            <li key={`${index}-${entry.step}`} className={`plan-step-${entry.status}`}>
              <span aria-hidden="true">{entry.status === "completed"
                ? <Icon name="verify" size={14} />
                : entry.status === "inProgress" ? <i /> : <em>{index + 1}</em>}</span>
              <span>{entry.step}</span>
            </li>
          ))}
        </ol> : fallbackDetail
          ? <pre className="plan-progress-fallback">{fallbackDetail}</pre>
          : <div className="plan-progress-empty">
              {detailLoading ? "正在加载计划…" : "等待计划步骤…"}
            </div>}
      </section>,
      document.body,
    )}
  </div>;
}

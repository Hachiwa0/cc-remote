import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import type { ProcessBlock } from "../domain/conversation";
import { Icon } from "../icons";
import { planProgressPresentation } from "../plan-progress";
import {
  cancelDraggedPointer,
  PointerTapGuard,
  releaseDraggedPointer,
} from "../pointer-tap";

interface PlanPopoverPosition {
  left: number;
  width: number;
  maxHeight: number;
  top?: number;
  bottom?: number;
}

export function PlanProgressContent({ block, detailLoading = false }: {
  block: ProcessBlock;
  detailLoading?: boolean;
}) {
  const presentation = planProgressPresentation(block, detailLoading);
  const steps = block.plan ?? [];
  return <div className="plan-progress-content">
    <header>
      <span className={`plan-progress-mark${presentation.complete ? " complete" : ""}${presentation.failed ? " failed" : ""}`}>
        <Icon name={presentation.complete ? "verify" : "plan"} size={15} />
      </span>
      <span>
        <b>计划</b>
        <small>{presentation.stateLabel}</small>
      </span>
      <strong>{presentation.progressLabel}</strong>
    </header>
    {presentation.description && <p>{presentation.description}</p>}
    {steps.length > 0 ? <ol>
      {steps.map((entry, index) => (
        <li key={`${index}-${entry.step}`} className={`plan-step-${entry.status}`}>
          <span aria-hidden="true">{entry.status === "completed"
            ? <Icon name="verify" size={14} />
            : entry.status === "inProgress" ? <i /> : <em>{index + 1}</em>}</span>
          <span>{entry.step}</span>
        </li>
      ))}
    </ol> : presentation.fallbackDetail
      ? <pre className="plan-progress-fallback">{presentation.fallbackDetail}</pre>
      : <div className="plan-progress-empty">
          {detailLoading ? "正在加载计划…" : "等待计划步骤…"}
        </div>}
  </div>;
}

export function PlanProgressFloatingCard({ anchorRef, block, open,
  onOpenChange, id, detailLoading = false, compact = false }: {
  anchorRef: RefObject<HTMLElement | null>;
  block: ProcessBlock;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  id: string;
  detailLoading?: boolean;
  compact?: boolean;
}) {
  const [position, setPosition] = useState<PlanPopoverPosition | null>(null);
  const cardRef = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    if (!open) {
      setPosition(null);
      return;
    }
    const place = () => {
      const trigger = anchorRef.current?.getBoundingClientRect();
      if (!trigger) return;
      // WebKit's fixed-position layout viewport can differ from innerWidth by
      // a few CSS pixels around the safe-area. Use the document viewport and a
      // wider gutter so the card never grazes an iPhone edge.
      const gutter = 20;
      const viewportWidth = Math.min(
        window.innerWidth,
        document.documentElement.clientWidth,
      );
      const width = Math.min(compact ? 320 : 360, viewportWidth - gutter * 2);
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
        maxHeight: Math.min(compact ? 360 : 420, available),
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
  }, [anchorRef, compact, open]);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (anchorRef.current?.contains(target)
        || cardRef.current?.contains(target)) return;
      onOpenChange(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onOpenChange(false);
    };
    document.addEventListener("pointerdown", closeOutside, true);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside, true);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [anchorRef, onOpenChange, open]);

  if (!open || !position) return null;
  return createPortal(
    <section ref={cardRef} id={id}
      className={`plan-progress-popover${compact ? " compact" : ""}`}
      role="dialog" aria-label="计划进度" style={position}>
      <PlanProgressContent block={block} detailLoading={detailLoading} />
    </section>,
    document.body,
  );
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
  const open = openOverride ?? uncontrolledOpen;
  const rootRef = useRef<HTMLDivElement>(null);
  const tapGuard = useRef(new PointerTapGuard());
  const labelId = useId();
  const presentation = planProgressPresentation(block, detailLoading);

  const setOpen = useCallback((next: boolean) => {
    setUncontrolledOpen(next);
    onOpenChange?.(next);
  }, [onOpenChange]);

  return <div ref={rootRef} className="plan-progress-control">
    <button type="button"
      className={`plan-progress-trigger${presentation.complete ? " complete" : ""}${presentation.failed ? " failed" : ""}`}
      aria-expanded={open} aria-controls={labelId}
      aria-label={`查看计划进度，${presentation.progressLabel}`}
      title={`计划进度 · ${presentation.progressLabel}`}
      onPointerDown={(event) => {
        tapGuard.current.pointerDown(
          event.pointerId, event.clientX, event.clientY);
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (tapGuard.current.pointerMove(
          event.pointerId, event.clientX, event.clientY,
        )) {
          releaseDraggedPointer(
            event.currentTarget, event.pointerId, event.pointerType);
        }
      }}
      onPointerUp={(event) => tapGuard.current.pointerUp(event.pointerId)}
      onPointerCancel={(event) => {
        cancelDraggedPointer(
          tapGuard.current,
          event.currentTarget, event.pointerId, event.pointerType);
      }}
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
        style={{ "--plan-progress": `${presentation.progress * 3.6}deg` } as CSSProperties}>
        <Icon name={presentation.complete ? "verify" : "plan"} size={12} />
      </span>
    </button>
    <PlanProgressFloatingCard anchorRef={rootRef} block={block} open={open}
      onOpenChange={setOpen} id={labelId} detailLoading={detailLoading} />
  </div>;
}

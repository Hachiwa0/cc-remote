import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export function LocalFileLink({ location, children, onOpen, className }: {
  location: string;
  children: ReactNode;
  onOpen: () => void;
  className?: string;
}) {
  const tooltipId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const tooltipRef = useRef<HTMLSpanElement>(null);
  const pathRef = useRef<HTMLSpanElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [shown, setShown] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });

  const cancelClose = () => {
    if (!closeTimer.current) return;
    clearTimeout(closeTimer.current);
    closeTimer.current = null;
  };
  const open = () => {
    cancelClose();
    setShown(true);
  };
  const scheduleClose = () => {
    cancelClose();
    closeTimer.current = setTimeout(() => {
      setShown(false);
      closeTimer.current = null;
    }, 220);
  };

  useEffect(() => () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
  }, []);

  useLayoutEffect(() => {
    if (!shown) return;
    const update = () => {
      const trigger = triggerRef.current;
      const tooltip = tooltipRef.current;
      if (!trigger || !tooltip) return;
      const anchor = trigger.getBoundingClientRect();
      const box = tooltip.getBoundingClientRect();
      const margin = 10;
      const gap = 8;
      const idealLeft = anchor.left + anchor.width / 2 - box.width / 2;
      const left = Math.min(
        Math.max(margin, idealLeft),
        Math.max(margin, window.innerWidth - box.width - margin),
      );
      const above = anchor.top - box.height - gap;
      const top = above >= margin
        ? above
        : Math.min(window.innerHeight - box.height - margin, anchor.bottom + gap);
      setPosition((current) => current.left === left && current.top === top
        ? current : { left, top });
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [shown, location]);

  const selectPath = () => {
    const path = pathRef.current;
    const selection = window.getSelection();
    if (!path || !selection) return;
    const range = document.createRange();
    range.selectNodeContents(path);
    selection.removeAllRanges();
    selection.addRange(range);
  };

  return <>
    <button ref={triggerRef} type="button"
      className={`message-file-link${className ? ` ${className}` : ""}`}
      aria-label={`在 Remote 中打开 ${location}`}
      aria-describedby={shown ? tooltipId : undefined}
      onPointerEnter={open} onPointerLeave={scheduleClose}
      onFocus={open} onBlur={scheduleClose}
      onClick={onOpen}>{children}</button>
    {shown && createPortal(
      <span ref={tooltipRef} id={tooltipId} role="tooltip"
        className="message-file-tooltip"
        style={{ left: position.left, top: position.top }}
        onPointerEnter={open} onPointerLeave={scheduleClose}
        onFocus={open} onBlur={scheduleClose}>
        <span ref={pathRef} className="message-file-tooltip-path"
          onDoubleClick={(event) => {
            event.preventDefault();
            selectPath();
          }}>{location}</span>
      </span>,
      document.body,
    )}
  </>;
}

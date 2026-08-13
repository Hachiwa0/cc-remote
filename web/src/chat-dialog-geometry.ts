import {
  useLayoutEffect,
  useState,
  type RefObject,
} from "react";

export interface ChatDialogGeometry {
  left: number;
  top: number;
  width: number;
  maxHeight: number;
}

interface DialogGeometryOptions {
  open: boolean;
  maxWidth: number;
  maxHeight: number;
  scopeRef?: RefObject<HTMLElement | null>;
  gutter?: number;
  minimumHeight?: number;
}

interface AnchoredPopoverGeometryOptions {
  open: boolean;
  anchorRef: RefObject<HTMLElement | null>;
  maxWidth: number;
  maxHeight: number;
  gap?: number;
  gutter?: number;
}

interface Bounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

function finite(value: number | undefined, fallback: number): number {
  return value !== undefined && Number.isFinite(value) ? value : fallback;
}

function cssPixelProperty(name: string): number | null {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  if (!/^-?(?:\d+|\d*\.\d+)px$/.test(raw)) return null;
  const value = Number.parseFloat(raw);
  return Number.isFinite(value) ? value : null;
}

function visualBounds(): Bounds {
  const viewport = window.visualViewport;
  const layoutWidth = Math.max(
    1,
    window.innerWidth || document.documentElement.clientWidth,
  );
  const layoutHeight = Math.max(
    1,
    window.innerHeight || document.documentElement.clientHeight,
  );
  const left = finite(viewport?.offsetLeft, 0);
  const top = finite(viewport?.offsetTop, 0);
  const width = Math.max(1, finite(viewport?.width, layoutWidth));
  const height = Math.max(1, finite(viewport?.height, layoutHeight));
  const visual = {
    left,
    top,
    right: left + width,
    bottom: top + height,
  };

  // useMobileViewport mirrors the keyboard-sized visual viewport into these
  // variables. Reading the px form also covers the brief Safari interval in
  // which the CSS shell has settled but visualViewport is still catching up.
  const appTop = cssPixelProperty("--app-offset-top");
  const appHeight = cssPixelProperty("--app-height");
  if (appTop === null || appHeight === null || appHeight <= 0) return visual;
  const constrainedTop = Math.max(visual.top, appTop);
  const constrainedBottom = Math.min(visual.bottom, appTop + appHeight);
  return constrainedBottom > constrainedTop
    ? { ...visual, top: constrainedTop, bottom: constrainedBottom }
    : visual;
}

function elementBounds(element: Element): Bounds | null {
  const rect = element.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  return {
    left: rect.left,
    top: rect.top,
    right: rect.right,
    bottom: rect.bottom,
  };
}

function intersection(first: Bounds, second: Bounds): Bounds | null {
  const result = {
    left: Math.max(first.left, second.left),
    top: Math.max(first.top, second.top),
    right: Math.min(first.right, second.right),
    bottom: Math.min(first.bottom, second.bottom),
  };
  return result.right > result.left && result.bottom > result.top
    ? result
    : null;
}

function visibleThreadShell(scope: HTMLElement | null): HTMLElement | null {
  const containingThread = scope?.closest<HTMLElement>(".thread-shell");
  if (containingThread && elementBounds(containingThread)) {
    return containingThread;
  }

  const pane = scope?.closest<HTMLElement>(".pane")
    ?? document.querySelector<HTMLElement>(".pane");
  const paneThread = pane?.querySelector<HTMLElement>(".thread-shell");
  if (paneThread && elementBounds(paneThread)) return paneThread;

  return [...document.querySelectorAll<HTMLElement>(".thread-shell")]
    .find((element) => elementBounds(element) !== null) ?? null;
}

function fallbackChatBounds(scope: HTMLElement | null): Bounds | null {
  const pane = scope?.closest<HTMLElement>(".pane")
    ?? document.querySelector<HTMLElement>(".pane");
  if (!pane) return null;
  const paneBounds = elementBounds(pane);
  if (!paneBounds) return null;
  const header = pane.querySelector<HTMLElement>(".c-head");
  const composer = pane.querySelector<HTMLElement>(".composer");
  const headerBounds = header ? elementBounds(header) : null;
  const composerBounds = composer ? elementBounds(composer) : null;
  return {
    ...paneBounds,
    top: Math.max(paneBounds.top, headerBounds?.bottom ?? paneBounds.top),
    bottom: Math.min(
      paneBounds.bottom,
      composerBounds?.top ?? paneBounds.bottom,
    ),
  };
}

function sameGeometry(
  first: ChatDialogGeometry | null,
  second: ChatDialogGeometry,
): boolean {
  if (!first) return false;
  return Math.abs(first.left - second.left) < 0.25
    && Math.abs(first.top - second.top) < 0.25
    && Math.abs(first.width - second.width) < 0.25
    && Math.abs(first.maxHeight - second.maxHeight) < 0.25;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum);
}

/** Center a floating dialog in the visible conversation, above the composer. */
export function useChatDialogGeometry({
  open,
  maxWidth,
  maxHeight,
  scopeRef,
  gutter = 16,
  minimumHeight = 96,
}: DialogGeometryOptions): ChatDialogGeometry | null {
  const [geometry, setGeometry] = useState<ChatDialogGeometry | null>(null);

  useLayoutEffect(() => {
    if (!open) {
      setGeometry(null);
      return;
    }

    let frame: number | null = null;
    const scope = scopeRef?.current ?? null;
    const threadShell = visibleThreadShell(scope);
    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(() => schedule());

    const place = () => {
      frame = null;
      const visual = visualBounds();
      const chat = (threadShell && elementBounds(threadShell))
        ?? fallbackChatBounds(scope);
      const bounds = chat ? intersection(visual, chat) ?? visual : visual;
      const rawWidth = Math.max(1, bounds.right - bounds.left);
      const availableWidth = rawWidth > gutter * 2
        ? rawWidth - gutter * 2
        : rawWidth;
      const availableHeight = Math.max(1, bounds.bottom - bounds.top - gutter * 2);
      const usableHeight = availableHeight >= minimumHeight
        ? availableHeight
        : Math.max(1, bounds.bottom - bounds.top);
      const next = {
        left: (bounds.left + bounds.right) / 2,
        top: (bounds.top + bounds.bottom) / 2,
        width: Math.min(maxWidth, availableWidth),
        maxHeight: Math.min(maxHeight, usableHeight),
      };
      setGeometry((current) => sameGeometry(current, next) ? current : next);
    };
    function schedule() {
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(place);
    }

    place();
    window.addEventListener("resize", schedule);
    window.visualViewport?.addEventListener("resize", schedule);
    window.visualViewport?.addEventListener("scroll", schedule);
    if (threadShell) resizeObserver?.observe(threadShell);
    const pane = scope?.closest<HTMLElement>(".pane")
      ?? document.querySelector<HTMLElement>(".pane");
    if (pane && pane !== threadShell) resizeObserver?.observe(pane);

    return () => {
      if (frame !== null) window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", schedule);
      window.visualViewport?.removeEventListener("resize", schedule);
      window.visualViewport?.removeEventListener("scroll", schedule);
      resizeObserver?.disconnect();
    };
  }, [gutter, maxHeight, maxWidth, minimumHeight, open, scopeRef]);

  return open ? geometry : null;
}

/** Place a floating card immediately above its trigger without leaving view. */
export function useAnchoredPopoverGeometry({
  open,
  anchorRef,
  maxWidth,
  maxHeight,
  gap = 8,
  gutter = 16,
}: AnchoredPopoverGeometryOptions): ChatDialogGeometry | null {
  const [geometry, setGeometry] = useState<ChatDialogGeometry | null>(null);

  useLayoutEffect(() => {
    if (!open) {
      setGeometry(null);
      return;
    }

    let frame: number | null = null;
    const anchor = anchorRef.current;
    const scope = anchor;
    const threadShell = visibleThreadShell(scope);
    const pane = scope?.closest<HTMLElement>(".pane")
      ?? document.querySelector<HTMLElement>(".pane");
    const composer = pane?.querySelector<HTMLElement>(".composer") ?? null;
    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(() => schedule());

    const place = () => {
      frame = null;
      const anchorBounds = anchor ? elementBounds(anchor) : null;
      if (!anchorBounds) {
        setGeometry(null);
        return;
      }

      const visual = visualBounds();
      const chat = (threadShell && elementBounds(threadShell))
        ?? fallbackChatBounds(scope);
      const bounds = chat ? intersection(visual, chat) ?? visual : visual;
      const rawWidth = Math.max(1, bounds.right - bounds.left);
      const horizontalGutter = Math.min(gutter, Math.max(0, (rawWidth - 1) / 2));
      const availableWidth = Math.max(1, rawWidth - horizontalGutter * 2);
      const width = Math.min(maxWidth, availableWidth);
      const minimumCenter = bounds.left + horizontalGutter + width / 2;
      const maximumCenter = bounds.right - horizontalGutter - width / 2;
      const anchorCenter = (anchorBounds.left + anchorBounds.right) / 2;
      const left = minimumCenter <= maximumCenter
        ? clamp(anchorCenter, minimumCenter, maximumCenter)
        : (bounds.left + bounds.right) / 2;
      const top = anchorBounds.top - gap;
      const safeTop = bounds.top + Math.min(
        gutter,
        Math.max(0, bounds.bottom - bounds.top - 1),
      );
      const next = {
        left,
        top,
        width,
        maxHeight: Math.min(maxHeight, Math.max(1, top - safeTop)),
      };
      setGeometry((current) => sameGeometry(current, next) ? current : next);
    };
    function schedule() {
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(place);
    }

    place();
    window.addEventListener("resize", schedule);
    window.visualViewport?.addEventListener("resize", schedule);
    window.visualViewport?.addEventListener("scroll", schedule);
    document.addEventListener("scroll", schedule, true);
    for (const element of new Set([anchor, threadShell, pane, composer])) {
      if (element) resizeObserver?.observe(element);
    }

    return () => {
      if (frame !== null) window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", schedule);
      window.visualViewport?.removeEventListener("resize", schedule);
      window.visualViewport?.removeEventListener("scroll", schedule);
      document.removeEventListener("scroll", schedule, true);
      resizeObserver?.disconnect();
    };
  }, [anchorRef, gap, gutter, maxHeight, maxWidth, open]);

  return open ? geometry : null;
}

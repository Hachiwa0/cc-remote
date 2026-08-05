/** Separates an intentional tap/click from a pointer sequence used to scroll.
 * Native click remains responsible for keyboard activation and button
 * semantics; this guard only suppresses the synthetic click after a drag,
 * cancellation, or multi-touch gesture. */
export class PointerTapGuard {
  private readonly pointers = new Map<number, { x: number; y: number }>();
  private readonly threshold: number;
  private suppressPointerClick = false;

  constructor(threshold = 8) {
    this.threshold = threshold;
  }

  pointerDown(pointerId: number, x: number, y: number): void {
    if (this.pointers.size === 0) this.suppressPointerClick = false;
    this.pointers.set(pointerId, { x, y });
    if (this.pointers.size > 1) this.suppressPointerClick = true;
  }

  pointerMove(pointerId: number, x: number, y: number): boolean {
    const start = this.pointers.get(pointerId);
    if (!start) return false;
    if (Math.hypot(x - start.x, y - start.y) > this.threshold) {
      this.suppressPointerClick = true;
      // Crossing the drag threshold is a terminal state for this pointer. The
      // caller can release capture/visual focus exactly once while subsequent
      // move events remain ordinary page scrolling.
      this.pointers.delete(pointerId);
      return true;
    }
    return false;
  }

  pointerUp(pointerId: number): void {
    this.pointers.delete(pointerId);
  }

  pointerCancel(pointerId: number): void {
    this.pointers.delete(pointerId);
    this.suppressPointerClick = true;
  }

  consumeClick(detail: number): boolean {
    if (detail === 0) return true;
    const allowed = !this.suppressPointerClick;
    this.suppressPointerClick = false;
    return allowed;
  }
}

/** Release only the touch control on which a scroll gesture began.
 *
 * Do not preventDefault: Safari must retain ownership of native momentum
 * scrolling. Mouse and keyboard focus are deliberately untouched.
 */
export function releaseDraggedPointer(
  target: HTMLElement,
  pointerId: number,
  pointerType: string,
): void {
  try {
    if (target.hasPointerCapture(pointerId)) {
      target.releasePointerCapture(pointerId);
    }
  } catch {
    // WebKit may have released capture as it promoted the gesture to scrolling.
  }
  if (pointerType === "touch") target.blur();
}

export function cancelDraggedPointer(
  guard: PointerTapGuard,
  target: HTMLElement,
  pointerId: number,
  pointerType: string,
): void {
  guard.pointerCancel(pointerId);
  releaseDraggedPointer(target, pointerId, pointerType);
}

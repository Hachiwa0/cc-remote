type Schedule = (callback: () => void, delayMs: number) => number;
type Cancel = (timer: number) => void;

/** A small, bounded repair cycle for a non-authoritative history/detail read.
 *
 * A growing transcript can remain unstable across one 250 ms retry. Three
 * exponentially-spaced attempts cover an ordinary flush without turning a
 * broken source into a polling loop. A later explicit read starts a fresh
 * cycle after this one is exhausted.
 */
export class RecoverableReadCoordinator {
  private readonly state = new Map<string, {
    attempts: number;
    scheduled: boolean;
  }>();
  private readonly timers = new Map<string, number>();
  private readonly schedule: Schedule;
  private readonly cancel: Cancel;
  private readonly delayMs: number;
  private readonly maxAttempts: number;
  private readonly backoff: number;

  constructor(
    schedule: Schedule,
    cancel: Cancel,
    delayMs = 250,
    maxAttempts = 3,
    backoff = 4,
  ) {
    this.schedule = schedule;
    this.cancel = cancel;
    this.delayMs = delayMs;
    this.maxAttempts = Math.max(1, Math.floor(maxAttempts));
    this.backoff = Math.max(1, backoff);
  }

  retry(key: string, read: () => void, delayMs = this.delayMs): boolean {
    const state = this.state.get(key) ?? { attempts: 0, scheduled: false };
    if (state.scheduled) return false;
    // Callers using a long custom watchdog intentionally ask for one probe,
    // not the ordinary short flush-repair sequence.
    const maxAttempts = delayMs === this.delayMs ? this.maxAttempts : 1;
    if (state.attempts >= maxAttempts) {
      this.state.delete(key);
      return false;
    }
    state.scheduled = true;
    this.state.set(key, state);
    const attemptDelay = delayMs === this.delayMs
      ? delayMs * this.backoff ** state.attempts
      : delayMs;
    const timer = this.schedule(() => {
      this.timers.delete(key);
      const current = this.state.get(key);
      if (current !== state || !current.scheduled) return;
      current.scheduled = false;
      current.attempts += 1;
      read();
    }, attemptDelay);
    this.timers.set(key, timer);
    return true;
  }

  complete(key: string): void {
    const timer = this.timers.get(key);
    if (timer !== undefined) this.cancel(timer);
    this.timers.delete(key);
    this.state.delete(key);
  }

  clear(): void {
    for (const timer of this.timers.values()) this.cancel(timer);
    this.timers.clear();
    this.state.clear();
  }
}

export interface DrainableRuntime<Query> {
  pendingSend: Query | null;
  queue: Query[];
}

export const MAX_QUEUED_QUERIES = 32;
export const MAX_QUEUED_QUERY_BYTES = 64 * 1024 * 1024;

interface WireSizedAttachment {
  data?: string;
  media_type?: string;
  filename?: string;
}

export interface WireSizedQuery {
  prompt: string;
  images?: WireSizedAttachment[];
  files?: WireSizedAttachment[];
}

const utf8 = new TextEncoder();
const textBytes = (value: string | undefined): number =>
  value ? utf8.encode(value).byteLength : 0;
const encodedBodyBytes = (value: string | undefined): number =>
  value?.length ?? 0; // attachment bodies are base64 ASCII

/** Approximate the eventual JSON wire size without JSON.stringify-ing large
 * base64 bodies (which would temporarily allocate another huge string). */
export function queuedQueryWireBytes(query: WireSizedQuery): number {
  let bytes = 128 + textBytes(query.prompt);
  for (const image of query.images ?? []) {
    bytes += 64 + textBytes(image.media_type) + encodedBodyBytes(image.data);
  }
  for (const file of query.files ?? []) {
    bytes += 64 + textBytes(file.filename) + encodedBodyBytes(file.data);
  }
  return bytes;
}

/** All memory-resident work counted by the global queue limit. The focused
 * pending query is omitted when a new interrupt-and-send replaces it. */
export function collectWaitingQueries<Query>(
  runtimes: Record<string, Pick<DrainableRuntime<Query>, "queue" | "pendingSend">>,
  replacingSid?: string | null,
): Query[] {
  const waiting: Query[] = [];
  for (const [sid, runtime] of Object.entries(runtimes)) {
    waiting.push(...runtime.queue);
    if (runtime.pendingSend && sid !== replacingSid) waiting.push(runtime.pendingSend);
  }
  return waiting;
}

/** Browser-memory guard for busy-session queue mode. */
export function canEnqueueQuery(
  queue: readonly WireSizedQuery[],
  query: WireSizedQuery,
  maxCount = MAX_QUEUED_QUERIES,
  maxBytes = MAX_QUEUED_QUERY_BYTES,
): boolean {
  if (queue.length >= maxCount) return false;
  const nextBytes = queuedQueryWireBytes(query);
  if (nextBytes > maxBytes) return false;
  let used = 0;
  for (const queued of queue) {
    used += queuedQueryWireBytes(queued);
    if (used > maxBytes - nextBytes) return false;
  }
  return true;
}

export type TargetedRuntimeAction<Turn> =
  | { type: "query_sent"; turn: Turn }
  | { type: "dequeue_at"; i: number }
  | { type: "clear_pending" };

/** Pure sid-targeted runtime update used by the reducer. Keeping this separate
 * makes it difficult for a background drain to accidentally mutate whichever
 * session happens to be focused at dispatch time. */
export function reduceTargetedRuntime<
  Query,
  Turn extends { id: string },
  Runtime extends DrainableRuntime<Query> & { turns: Turn[] },
>(
  runtimes: Record<string, Runtime>,
  sid: string,
  action: TargetedRuntimeAction<Turn>,
): Record<string, Runtime> {
  const current = runtimes[sid];
  if (!current) return runtimes;

  const runtime = { ...current };
  switch (action.type) {
    case "query_sent":
      if (runtime.turns.some((turn) => turn.id === action.turn.id)) return runtimes;
      runtime.turns = [...runtime.turns, action.turn];
      break;
    case "dequeue_at":
      runtime.queue = runtime.queue.filter((_, i) => i !== action.i);
      break;
    case "clear_pending":
      runtime.pendingSend = null;
      break;
  }
  return { ...runtimes, [sid]: runtime };
}

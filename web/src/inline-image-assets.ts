import type {
  PreviewAsset,
  PreviewAuthorizationRequired,
  PreviewAuthorizationResult,
} from "./protocol.ts";
import type { PreviewAuthorizationState } from "./reducer.ts";
import { parseLocalFileTarget } from "./file-link.ts";
import { imageDimensionsFromBase64 } from "./img.ts";

export type MessageImageTarget =
  | { kind: "local"; value: string }
  | { kind: "external"; value: string }
  | { kind: "blocked"; value: "" };

const IMAGE_SUFFIX = /\.(?:png|jpe?g|gif|webp|avif|svg)$/i;

/** Classify an image emitted inside a chat message. Local filesystem paths are
 * never assigned to an HTML src; the caller must materialize them over the
 * authenticated preview-asset channel. */
export function classifyMessageImageTarget(rawTarget: string): MessageImageTarget {
  const target = rawTarget.trim();
  if (!target || target.startsWith("#") || target.startsWith("//")) {
    return { kind: "blocked", value: "" };
  }
  if (/^https?:\/\//i.test(target)) {
    return { kind: "external", value: target };
  }
  const local = parseLocalFileTarget(target);
  if (!local || !IMAGE_SUFFIX.test(local.path)) {
    return { kind: "blocked", value: "" };
  }
  return { kind: "local", value: local.path };
}

export interface InlineImageAsset {
  status: "loading" | "authorization" | "ready" | "error";
  mediaType?: string;
  data?: string;
  error?: string;
  authorization?: InlineImageAuthorization;
  width?: number;
  height?: number;
  /** Wall-clock start of the active request. Present while loading so a
   * virtualized remount can keep the original timeout boundary. */
  startedAt?: number;
  /** Changes for every accepted begin(), including loading -> loading retries. */
  requestGeneration?: number;
}

export interface InlineImageAuthorization extends PreviewAuthorizationState {
  sid: string;
}

interface AssetEntry extends InlineImageAsset {
  sid: string;
  path: string;
  assetKey: string;
  lastUsed: number;
}

export interface InlineImageRequest {
  sid: string;
  path: string;
  assetKey?: string;
  previewId: string;
  requestId: string;
}

interface PendingAsset extends InlineImageRequest {
  key: string;
  startedAt: number;
  requestGeneration: number;
}

export const MAX_INLINE_IMAGE_ASSETS = 24;
export const INLINE_IMAGE_REQUEST_TIMEOUT_MS = 15_000;

interface InlineImageWakeScheduler {
  schedule(callback: () => void, delayMs: number): unknown;
  cancel(handle: unknown): void;
}

const defaultInlineImageWakeScheduler: InlineImageWakeScheduler = {
  schedule: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  cancel: (handle) => globalThis.clearTimeout(
    handle as ReturnType<typeof globalThis.setTimeout>,
  ),
};

let inlineImageAssetCacheVersion = 0;
const inlineImageAssetCacheListeners = new Set<() => void>();

export function subscribeInlineImageAssetCacheChanges(
  listener: () => void,
): () => void {
  inlineImageAssetCacheListeners.add(listener);
  return () => inlineImageAssetCacheListeners.delete(listener);
}

export function inlineImageAssetCacheSnapshot(): number {
  return inlineImageAssetCacheVersion;
}

function publishInlineImageAssetCacheChange(): void {
  inlineImageAssetCacheVersion += 1;
  for (const listener of inlineImageAssetCacheListeners) listener();
}

/** Small in-memory, cross-session LRU for images visible in chat. It validates
 * every response against the exact sid/path/preview/request tuple so a delayed
 * background frame cannot fill another session's image. */
export class InlineImageAssetCache {
  private readonly entries = new Map<string, AssetEntry>();
  private readonly pending = new Map<string, PendingAsset>();
  private readonly limit: number;
  private readonly now: () => number;
  private readonly wakeScheduler: InlineImageWakeScheduler;
  private capacityWakeHandle: unknown = null;
  private capacityWakeAt: number | null = null;
  private tick = 0;
  private requestGeneration = 0;

  constructor(
    limit = MAX_INLINE_IMAGE_ASSETS,
    now: () => number = () => Date.now(),
    wakeScheduler: InlineImageWakeScheduler = defaultInlineImageWakeScheduler,
  ) {
    this.limit = Math.max(0, Math.floor(limit));
    this.now = now;
    this.wakeScheduler = wakeScheduler;
  }

  private key(sid: string, assetKey: string): string {
    return `${sid}\u0000${assetKey}`;
  }

  /** Remove both halves of one request atomically. A pending response without
   * its entry must never be able to resurrect an LRU victim. */
  private removeKey(key: string): boolean {
    let changed = this.entries.delete(key);
    for (const [requestId, pending] of this.pending) {
      if (pending.key !== key) continue;
      this.pending.delete(requestId);
      changed = true;
    }
    return changed;
  }

  private cancelCapacityWake(): void {
    if (this.capacityWakeAt === null) return;
    this.wakeScheduler.cancel(this.capacityWakeHandle);
    this.capacityWakeHandle = null;
    this.capacityWakeAt = null;
  }

  private scheduleCapacityWake(now: number): void {
    let wakeAt = Number.POSITIVE_INFINITY;
    for (const entry of this.entries.values()) {
      if (entry.status !== "loading" || entry.startedAt == null) continue;
      wakeAt = Math.min(
        wakeAt,
        entry.startedAt + INLINE_IMAGE_REQUEST_TIMEOUT_MS,
      );
    }
    if (!Number.isFinite(wakeAt)) return;
    if (this.capacityWakeAt !== null && this.capacityWakeAt <= wakeAt) return;
    this.cancelCapacityWake();
    this.capacityWakeAt = wakeAt;
    this.capacityWakeHandle = this.wakeScheduler.schedule(() => {
      this.capacityWakeHandle = null;
      this.capacityWakeAt = null;
      // Expiry changes admission even though no response mutated the maps.
      publishInlineImageAssetCacheChange();
    }, Math.max(0, wakeAt - now));
  }

  private evictOneSettled(): boolean {
    let oldestKey: string | null = null;
    let oldestTick = Number.POSITIVE_INFINITY;
    const now = this.now();
    for (const [key, entry] of this.entries) {
      const activeLoading = entry.status === "loading"
        && now - (entry.startedAt ?? 0)
          < INLINE_IMAGE_REQUEST_TIMEOUT_MS;
      if (activeLoading || entry.lastUsed >= oldestTick) continue;
      oldestKey = key;
      oldestTick = entry.lastUsed;
    }
    if (!oldestKey) return false;
    return this.removeKey(oldestKey);
  }

  begin(request: InlineImageRequest): boolean {
    if (this.limit === 0 || this.pending.has(request.requestId)) return false;
    const assetKey = request.assetKey ?? request.path;
    const key = this.key(request.sid, assetKey);
    const now = this.now();
    const existing = this.entries.get(key);
    if (existing && (
      existing.status === "ready"
      || (existing.status === "loading"
        && now - (existing.startedAt ?? 0)
          < INLINE_IMAGE_REQUEST_TIMEOUT_MS)
    )) return false;
    let changed = existing ? this.removeKey(key) : false;
    while (this.entries.size >= this.limit) {
      if (this.evictOneSettled()) {
        changed = true;
        continue;
      }
      if (changed) publishInlineImageAssetCacheChange();
      this.scheduleCapacityWake(now);
      return false;
    }
    this.cancelCapacityWake();
    const requestGeneration = ++this.requestGeneration;
    this.entries.set(key, {
      sid: request.sid,
      path: request.path,
      assetKey,
      status: "loading",
      lastUsed: ++this.tick,
      startedAt: now,
      requestGeneration,
    });
    this.pending.set(request.requestId, {
      ...request,
      key,
      startedAt: now,
      requestGeneration,
    });
    publishInlineImageAssetCacheChange();
    return true;
  }

  has(sid: string, path: string, assetKey = path): boolean {
    const entry = this.entries.get(this.key(sid, assetKey));
    if (!entry || entry.status === "error") return false;
    return entry.status === "ready" || entry.status === "authorization"
      || this.now() - (entry.startedAt ?? 0)
        < INLINE_IMAGE_REQUEST_TIMEOUT_MS;
  }

  cancel(requestId: string): void {
    const request = this.pending.get(requestId);
    if (!request) return;
    this.cancelCapacityWake();
    this.pending.delete(requestId);
    const entry = this.entries.get(request.key);
    if (entry?.status === "loading"
        && entry.requestGeneration === request.requestGeneration) {
      this.entries.delete(request.key);
    }
    publishInlineImageAssetCacheChange();
  }

  accept(event: PreviewAsset): boolean {
    const request = this.pending.get(event.request_id);
    if (!request || event.sid !== request.sid || event.path !== request.path
        || event.preview_id !== request.previewId) return false;
    const current = this.entries.get(request.key);
    const ready = !!event.data && !!event.media_type && !event.error;
    const acceptsCurrentState = current?.status === "loading"
      || (
        current?.status === "authorization"
        && current.authorization?.status === "submitting"
        && ready
      );
    if (!acceptsCurrentState
        || current.requestGeneration !== request.requestGeneration) {
      return false;
    }
    this.cancelCapacityWake();
    this.pending.delete(event.request_id);
    const dimensions = ready
      ? imageDimensionsFromBase64(event.data ?? "", event.media_type ?? "")
      : null;
    this.entries.set(request.key, {
      sid: request.sid,
      path: request.path,
      assetKey: request.assetKey ?? request.path,
      status: ready ? "ready" : "error",
      mediaType: ready ? event.media_type ?? undefined : undefined,
      data: ready ? event.data ?? undefined : undefined,
      error: ready ? undefined : event.error ?? "图片加载失败",
      ...(dimensions ? { width: dimensions[0], height: dimensions[1] } : {}),
      lastUsed: ++this.tick,
      requestGeneration: request.requestGeneration,
    });
    publishInlineImageAssetCacheChange();
    return true;
  }

  requireAuthorization(event: PreviewAuthorizationRequired): boolean {
    if (event.operation !== "preview_asset" || !event.sid
        || !event.preview_id) return false;
    const request = this.pending.get(event.request_id);
    if (!request
        || request.sid !== event.sid
        || request.path !== event.path
        || request.previewId !== event.preview_id) return false;
    const current = this.entries.get(request.key);
    if (current?.status !== "loading"
        || current.requestGeneration !== request.requestGeneration) {
      return false;
    }
    this.cancelCapacityWake();
    this.entries.set(request.key, {
      ...current,
      status: "authorization",
      startedAt: undefined,
      authorization: {
        authorizationId: event.authorization_id,
        requestId: event.request_id,
        operation: event.operation,
        path: event.path,
        resolvedPath: event.resolved_path,
        format: event.format,
        previewId: event.preview_id,
        status: "required",
        sid: event.sid,
      },
      lastUsed: ++this.tick,
    });
    publishInlineImageAssetCacheChange();
    return true;
  }

  authorizationRequest(
    authorization: PreviewAuthorizationState,
  ): InlineImageRequest | null {
    const request = this.pending.get(authorization.requestId);
    if (!request || request.path !== authorization.path
        || request.previewId !== authorization.previewId) return null;
    const current = this.entries.get(request.key);
    const active = current?.authorization;
    if (current?.status !== "authorization"
        || !active
        || active.authorizationId !== authorization.authorizationId
        || active.status !== "required") return null;
    return {
      sid: request.sid,
      path: request.path,
      assetKey: request.assetKey,
      previewId: request.previewId,
      requestId: request.requestId,
    };
  }

  markAuthorizationSubmitting(
    authorization: PreviewAuthorizationState,
  ): boolean {
    const request = this.authorizationRequest(authorization);
    if (!request) return false;
    const pending = this.pending.get(request.requestId)!;
    const current = this.entries.get(pending.key)!;
    this.entries.set(pending.key, {
      ...current,
      authorization: {
        ...current.authorization!,
        status: "submitting",
      },
      lastUsed: ++this.tick,
    });
    publishInlineImageAssetCacheChange();
    return true;
  }

  acceptAuthorizationResult(
    event: PreviewAuthorizationResult,
  ): InlineImageRequest | null | false {
    if (!event.sid) return false;
    const request = this.pending.get(event.request_id);
    if (!request || request.sid !== event.sid) return false;
    const current = this.entries.get(request.key);
    const authorization = current?.authorization;
    if (current?.status !== "authorization"
        || !authorization
        || authorization.authorizationId !== event.authorization_id
        || authorization.requestId !== event.request_id
        || (event.operation && event.operation !== "preview_asset")
        || (event.path && event.path !== request.path)
        || (event.preview_id && event.preview_id !== request.previewId)) {
      return false;
    }
    this.cancelCapacityWake();
    if (event.status !== "granted") {
      this.pending.delete(event.request_id);
      this.entries.set(request.key, {
        ...current,
        status: "error",
        authorization: undefined,
        error: event.error ?? (
          event.status === "denied"
            ? "已取消读取外部图片"
            : "图片预览授权已过期"
        ),
        startedAt: undefined,
        lastUsed: ++this.tick,
      });
      publishInlineImageAssetCacheChange();
      return null;
    }
    const requestGeneration = ++this.requestGeneration;
    const startedAt = this.now();
    this.pending.set(event.request_id, {
      ...request,
      requestGeneration,
      startedAt,
    });
    this.entries.set(request.key, {
      ...current,
      status: "loading",
      authorization: undefined,
      error: undefined,
      startedAt,
      requestGeneration,
      lastUsed: ++this.tick,
    });
    this.scheduleCapacityWake(startedAt);
    publishInlineImageAssetCacheChange();
    return {
      sid: request.sid,
      path: request.path,
      assetKey: request.assetKey,
      previewId: request.previewId,
      requestId: request.requestId,
    };
  }

  rekeySession(oldSid: string, newSid: string): boolean {
    if (!oldSid || !newSid || oldSid === newSid) return false;
    const hasOldEntry = Array.from(this.entries.values()).some(
      (entry) => entry.sid === oldSid,
    );
    const hasOldPending = Array.from(this.pending.values()).some(
      (request) => request.sid === oldSid,
    );
    if (!hasOldEntry && !hasOldPending) return false;

    const statusRank = (entry: AssetEntry): number => {
      switch (entry.status) {
        case "ready": return 4;
        case "authorization": return 3;
        case "loading": return 2;
        case "error": return 1;
      }
    };
    const rebuiltEntries = new Map<string, AssetEntry>();
    for (const entry of this.entries.values()) {
      const mapped = entry.sid === oldSid
        ? {
            ...entry,
            sid: newSid,
            ...(entry.authorization
              ? {
                  authorization: {
                    ...entry.authorization,
                    sid: newSid,
                  },
                }
              : {}),
          }
        : entry;
      const mappedKey = this.key(mapped.sid, mapped.assetKey);
      const existing = rebuiltEntries.get(mappedKey);
      if (!existing
          || statusRank(mapped) > statusRank(existing)
          || (
            statusRank(mapped) === statusRank(existing)
            && mapped.lastUsed > existing.lastUsed
          )) {
        rebuiltEntries.set(mappedKey, mapped);
      }
    }

    const rebuiltPending = new Map<string, PendingAsset>();
    for (const [requestId, request] of this.pending) {
      const mapped = request.sid === oldSid
        ? {
            ...request,
            sid: newSid,
            key: this.key(
              newSid,
              request.assetKey ?? request.path,
            ),
          }
        : request;
      const entry = rebuiltEntries.get(mapped.key);
      if (!entry
          || entry.requestGeneration !== mapped.requestGeneration) continue;
      rebuiltPending.set(requestId, mapped);
    }

    this.entries.clear();
    for (const [key, entry] of rebuiltEntries) this.entries.set(key, entry);
    this.pending.clear();
    for (const [requestId, request] of rebuiltPending) {
      this.pending.set(requestId, request);
    }
    this.cancelCapacityWake();
    this.scheduleCapacityWake(this.now());
    publishInlineImageAssetCacheChange();
    return true;
  }

  forSession(sid: string): Record<string, InlineImageAsset> {
    const assets: Record<string, InlineImageAsset> = {};
    for (const entry of this.entries.values()) {
      if (entry.sid !== sid) continue;
      entry.lastUsed = ++this.tick;
      assets[entry.assetKey] = {
        status: entry.status,
        ...(entry.mediaType ? { mediaType: entry.mediaType } : {}),
        ...(entry.data ? { data: entry.data } : {}),
        ...(entry.error ? { error: entry.error } : {}),
        ...(entry.authorization
          ? { authorization: entry.authorization }
          : {}),
        ...(entry.width && entry.height
          ? { width: entry.width, height: entry.height }
          : {}),
        ...(entry.status === "loading"
          ? {
              startedAt: entry.startedAt,
              requestGeneration: entry.requestGeneration,
            }
          : {}),
      };
    }
    return assets;
  }

  dropSession(sid: string): boolean {
    let changed = false;
    for (const [key, entry] of this.entries) {
      if (entry.sid !== sid) continue;
      this.entries.delete(key);
      changed = true;
    }
    for (const [requestId, request] of this.pending) {
      if (request.sid !== sid) continue;
      this.pending.delete(requestId);
      changed = true;
    }
    if (changed) {
      this.cancelCapacityWake();
      publishInlineImageAssetCacheChange();
    }
    return changed;
  }

  clear(): void {
    if (this.entries.size === 0 && this.pending.size === 0) return;
    this.cancelCapacityWake();
    this.entries.clear();
    this.pending.clear();
    publishInlineImageAssetCacheChange();
  }
}

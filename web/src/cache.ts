// IndexedDB cache: per-session turns + lastSeq.
//
// Opening the app restores history instantly from local storage and asks the
// wrapper only for the delta (seq > lastSeq) instead of replaying the whole
// buffer — that's what makes other apps feel instant vs. our "一段段补发".
//
// Writes are coalesced (turns change every frame during streaming).

const DB_NAME = "cc_remote_cache";
const STORE = "sessions";
const SCHEMA = 1;

// Bump when the cached turn shape changes in a way old entries can't be
// trusted (e.g. a past bug left tool-only turns without text). Old entries are
// ignored -> client falls back to a full buffer replay (text + tools restored).
const CACHE_VER = 2;

export interface CachedSession {
  turns: unknown[];
  lastSeq: number;
  savedAt: number;
}

let dbPromise: Promise<IDBDatabase> | null = null;
function db(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, SCHEMA);
    req.onupgradeneeded = () => {
      const d = req.result;
      if (!d.objectStoreNames.contains(STORE)) d.createObjectStore(STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}

export async function loadSession(sessionId: string): Promise<CachedSession | null> {
  if (typeof indexedDB === "undefined") return null;
  try {
    const d = await db();
    return await new Promise((resolve) => {
      const tx = d.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(sessionId);
      req.onsuccess = () => {
        const r = req.result as (CachedSession & { v?: number }) | undefined;
        // Ignore stale caches from before the current shape (v must match).
        resolve(r && r.v === CACHE_VER ? r : null);
      };
      req.onerror = () => resolve(null);
    });
  } catch {
    return null;
  }
}

let pending: { sid: string; turns: unknown[]; lastSeq: number } | null = null;
let saveTimer: ReturnType<typeof setTimeout> | null = null;

/** Coalesced write — call freely on every turns change; actual IDB write is
 *  debounced 400ms so streaming doesn't hammer IndexedDB. */
export function saveSession(sessionId: string, turns: unknown[], lastSeq: number): void {
  if (typeof indexedDB === "undefined" || !sessionId) return;
  pending = { sid: sessionId, turns, lastSeq };
  if (saveTimer) return;
  saveTimer = setTimeout(flush, 400);
}

async function flush(): Promise<void> {
  saveTimer = null;
  const job = pending;
  pending = null;
  if (!job) return;
  try {
    const d = await db();
    await new Promise<void>((resolve) => {
      const tx = d.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(
        { v: CACHE_VER, turns: job.turns, lastSeq: job.lastSeq, savedAt: Date.now() }, job.sid);
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
    });
  } catch { /* ignore — cache is best-effort */ }
}

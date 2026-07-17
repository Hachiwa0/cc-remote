import { useCallback, useEffect, useReducer, useRef, useState, type TouchEvent } from "react";
import { RelayWs } from "./ws";
import { reduce, initialState, createRuntime, type Turn } from "./reducer";
import { uuid } from "./util";
import { Icon } from "./icons";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import { ReconnectBanner } from "./components/ReconnectBanner";
import { NoticeStack } from "./components/NoticeStack";
import { LoginForm } from "./components/LoginForm";
import { SessionsSidebar } from "./components/SessionsSidebar";
import { DirPicker } from "./components/DirPicker";
import { NewChatView } from "./components/NewChatView";
import { ArtifactPanel } from "./components/ArtifactPanel";
import { BtwPanel } from "./components/BtwPanel";
import { QuestionSheet } from "./components/QuestionSheet";
import { GoalPanel } from "./components/GoalPanel";
import { RollbackSheet, type RollbackTarget } from "./components/RollbackSheet";
import { StatusSheet } from "./components/StatusSheet";
import { ForkWorktreeSheet } from "./components/ForkWorktreeSheet";
import { WorkDashboardSheet } from "./components/WorkDashboardSheet";
import { WorkArtifactsSheet } from "./components/WorkArtifactsSheet";
import { CapabilitiesSheet } from "./components/CapabilitiesSheet";
import { TerminalControl } from "./components/TerminalControl";
import { parseGoalCommand } from "./goal-command";
import { shouldOpenCodexStatus } from "./status-capabilities";
import { permsFor } from "./data";
import { shouldAcceptSessionList } from "./session-list";
import { clearLegacyAuthMarkers, probeSession } from "./session-auth";
import { collectWaitingQueries, selectDrainCandidates } from "./runtime-drain";
import { MAX_RUNTIME_SESSIONS } from "./runtime-bounds";
import { isTerminalWorktreeForkError, matchesSessionForkRequest,
  matchesWorktreeForkRequest, type PendingSessionFork,
  type PendingWorktreeFork } from "./session-worktree";
import { classifyBtwOpened, consumeDiscardedBtwSnapshot, matchesBtwRequest,
  normalizeDiffTheme, normalizeEngine, type Snapshot, type QueryImg,
  type QueryFile, type SessionInfo, type CodexPermissionMode,
  type CodexServiceTier, type CollaborationModeName,
  type DiffTheme, type Engine, type Space, type RestoreMode,
  type SessionControl, sessionControlLocksInput } from "./protocol";
import type { EngineCapabilities, WorkArtifactInfo, WorkDashboard } from "./protocol";
import { isMarkdownPath } from "./preview-path";
import { resolveSidebarSwipe } from "./responsive-layout";
import {
  bumpSessionActivity,
  compareSessionsByActivity,
  sessionCommandTarget,
  setSessionPinned,
} from "./session-order";
import { disableRemotePush, enableRemotePush } from "./push";

const THEME_KEY = "cc_remote_theme";
const ENGINE_KEY = "cc_remote_engine";  // which backend the NEXT new session uses
const SPACE_KEY = "cc_remote_space";
const NOTIFY_KEY = "cc_remote_notifications";
const MACHINE_KEY = "cc_remote_machine";
const HISTORY_PAGE = 60;  // turns fetched per GetHistory (initial load + each "load more")

// The sidebar is an overlay on mobile (<980px, matches index.css) but a
// persistent grid column on desktop. So auto-close it after picking a session
// ONLY on mobile; on desktop keep it open.
const isMobile = () => window.matchMedia("(max-width: 979px)").matches;

export default function App() {
  const [theme, setTheme] = useState<DiffTheme>(
    () => normalizeDiffTheme(localStorage.getItem(THEME_KEY)));
  const [engine, setEngine] = useState<Engine>(
    () => normalizeEngine(localStorage.getItem(ENGINE_KEY)));
  const [space, setSpace] = useState<Space>(
    () => localStorage.getItem(SPACE_KEY) === "work" ? "work" : "code");
  const [authed, setAuthed] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [dirPickerOpen, setDirPickerOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [newChatAutoFocus, setNewChatAutoFocus] = useState(true);
  const [editPrompt, setEditPrompt] = useState<string | null>(null);
  // right slot is shared by diff + /btw; rightView picks which shows.
  const [rightView, setRightView] = useState<"diff" | "btw">("diff");
  // true from the moment /btw is clicked until the fork's btw_opened arrives — so
  // the panel appears instantly (spinner) instead of waiting ~1s for the fork.
  const [btwOpening, setBtwOpening] = useState(false);
  // Goal is deliberately opt-in UI: no empty bar and no RPC until /goal runs.
  // Keep reveal/editor state per session so switching sessions never leaks it.
  const [goalUiBySid, setGoalUiBySid] = useState<Record<string, { revealed: boolean; open: boolean }>>({});
  const [statusOpenSid, setStatusOpenSid] = useState<string | null>(null);
  const [forkWorktreeSession, setForkWorktreeSession] = useState<SessionInfo | null>(null);
  const [forkWorktreeCreating, setForkWorktreeCreating] = useState(false);
  const [forkWorktreeError, setForkWorktreeError] = useState<string | null>(null);
  const [forkingPointId, setForkingPointId] = useState<string | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<RollbackTarget | null>(null);
  const [workManagerOpen, setWorkManagerOpen] = useState(false);
  const [workArtifactsOpen, setWorkArtifactsOpen] = useState(false);
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(false);
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(false);
  const [capabilitiesBySurface, setCapabilitiesBySurface] = useState<Record<string, EngineCapabilities>>({});
  const [notificationsEnabled, setNotificationsEnabled] = useState(
    () => localStorage.getItem(NOTIFY_KEY) === "1"
      && typeof Notification !== "undefined" && Notification.permission === "granted");
  const [machineId, setMachineId] = useState(
    () => localStorage.getItem(MACHINE_KEY) || "default");
  const [machines, setMachines] = useState<string[]>([]);
  const [workProjectId, setWorkProjectId] = useState<string | null>(null);
  const [workDashboards, setWorkDashboards] = useState<Partial<Record<Engine, WorkDashboard>>>({});
  const [workArtifactsBySid, setWorkArtifactsBySid] = useState<Record<string, WorkArtifactInfo[]>>({});
  const [state, dispatch] = useReducer(reduce, initialState);
  const remotePushActiveRef = useRef(false);
  const stateRef = useRef(state);
  stateRef.current = state;
  const wsRef = useRef<RelayWs | null>(null);
  const drainingRef = useRef<Set<string>>(new Set());
  const pendingCreateRef = useRef<string | null>(null);
  const pendingBtwRef = useRef<string | null>(null);
  const pendingSessionForkRef = useRef<PendingSessionFork | null>(null);
  const pendingWorktreeForkRef = useRef<PendingWorktreeFork | null>(null);
  const sessionListsBySurfaceRef = useRef<Record<string, SessionInfo[]>>({});
  // Cached lists are paint-only during a surface switch. A surface may choose
  // its remembered/latest focus only after a fresh wrapper list is accepted.
  const authoritativeSurfaceListsRef = useRef<Set<string>>(new Set());
  const sessionActivityPendingRef = useRef<Set<string>>(new Set());
  const prefetchedSurfacesRef = useRef<Set<string>>(new Set());
  const lastFocusBySurfaceRef = useRef<Record<string, string>>({});
  const preferredSurfaceFocusRef = useRef<{ key: string; sid: string } | null>(null);
  const activeBtwRef = useRef<{ requestId: string; sid: string } | null>(null);
  // Retain recently cancelled ids so a late response can be identified and
  // discarded (and a late successful fork can be closed) without disturbing a
  // newer opening spinner. Bounded because a peer may disappear permanently.
  const btwRequestIdsRef = useRef<Set<string>>(new Set());
  const discardedBtwSidsRef = useRef<Set<string>>(new Set());
  // A marker may arrive while its session is in the background and while an
  // IndexedDB read is already in flight. The set blocks new cache use; the
  // epoch rejects reads that started before the destructive mutation.
  // sid -> exact revision required by a rollback marker. null means a replay
  // gap hid the revision, so the next authoritative first page may satisfy it.
  const historyInvalidationsRef = useRef<Map<string, string | null>>(new Map());
  const historyCacheEpochRef = useRef<Map<string, number>>(new Map());
  const previousMachineRef = useRef(machineId);
  const touchStartX = useRef(0);
  const touchStartY = useRef(0);
  const touchSwipeLocked = useRef(false);
  const artifactDirtyRef = useRef(false);
  const setArtifactDirty = useCallback((dirty: boolean) => {
    artifactDirtyRef.current = dirty;
  }, []);
  const confirmArtifactDiscard = useCallback(() => {
    if (!artifactDirtyRef.current) return true;
    if (!window.confirm("Markdown 有未保存的修改，确定放弃吗？")) return false;
    artifactDirtyRef.current = false;
    return true;
  }, []);
  // guards the once-per-connection "land on the latest session" auto-focus below
  const didInitFocusRef = useRef(false);
  const shortcutRef = useRef<{
    artifact: typeof state.artifact;
    btwSid: string | null;
    rightView: "diff" | "btw";
    getDiff: (file: string) => void;
    openBtw: () => void;
    closeBtw: () => void;
  }>({ artifact: null, btwSid: null, rightView: "diff",
    getDiff: () => {}, openBtw: () => {}, closeBtw: () => {} });

  useEffect(() => {
    const previous = previousMachineRef.current;
    if (previous === machineId) return;
    previousMachineRef.current = machineId;
    localStorage.setItem(MACHINE_KEY, machineId);
    pendingCreateRef.current = null;
    pendingBtwRef.current = null;
    activeBtwRef.current = null;
    sessionListsBySurfaceRef.current = {};
    authoritativeSurfaceListsRef.current.clear();
    sessionActivityPendingRef.current.clear();
    prefetchedSurfacesRef.current.clear();
    historyInvalidationsRef.current.clear();
    historyCacheEpochRef.current.clear();
    dispatch({ type: "reset" });
    void import("./cache").then((module) => module.clearCache());
  }, [machineId]);

  // The focused session's runtime (turns/state/model/perm/queue/...). Falls back
  // to an empty runtime before any session is focused.
  const focusedSid = state.focusedSid;
  const rt = state.runtimes[focusedSid ?? ""] ?? createRuntime();
  const focusedEngine = (state.sessions.find(
    (session) => session.session_id === focusedSid)?.engine ?? engine) as "claude" | "codex";
  const currentWorkArtifacts = focusedSid ? (workArtifactsBySid[focusedSid] ?? []) : [];
  const allQueued = collectWaitingQueries(state.runtimes);
  const replaceableQueued = collectWaitingQueries(state.runtimes, focusedSid);

  const goalUi = focusedSid ? goalUiBySid[focusedSid] : undefined;

  // HttpOnly cookies can't be inspected from JS. Ask the relay whether this
  // browser session is still registered before opening a WebSocket; this also
  // makes relay restarts (which intentionally revoke old sessions) fail closed.
  useEffect(() => {
    // Never retain credentials/markers from the pre-HttpOnly implementation.
    clearLegacyAuthMarkers(localStorage);
    let cancelled = false;
    let timer: number | null = null;
    let backoff = 1000;
    const check = async () => {
      const result = await probeSession();
      if (cancelled) return;
      if (result === "unavailable") {
        setAuthReady(false);
        timer = window.setTimeout(check, backoff);
        backoff = Math.min(backoff * 2, 5000);
        return;
      }
      if (result === "unauthorized") {
        clearLegacyAuthMarkers(localStorage);
        // Do not expose the login form until prior-session prompts and
        // attachments are gone. A fast login must not race cache hydration.
        try { await import("./cache").then((module) => module.clearCache()); }
        catch { /* best-effort local cleanup */ }
        if (cancelled) return;
      }
      setAuthed(result === "authenticated");
      setAuthReady(true);
    };
    void check();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    if (!authed) { setMachines([]); return; }
    let cancelled = false;
    void fetch("/api/machines", {
      credentials: "same-origin", cache: "no-store",
    }).then(async (response) => response.ok ? response.json() : null)
      .then((payload) => {
        if (cancelled || !payload || !Array.isArray(payload.machines)) return;
        const available = payload.machines.filter((value: unknown): value is string =>
          typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/.test(value));
        setMachines(available);
        if (available.length && !available.includes(machineId)) {
          setMachineId(available[0]);
        }
      }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [authed, machineId]);

  useEffect(() => {
    let cancelled = false;
    if (!authed || !notificationsEnabled) {
      remotePushActiveRef.current = false;
      return;
    }
    void enableRemotePush(machineId).then((enabled) => {
      if (!cancelled) remotePushActiveRef.current = enabled;
    });
    return () => { cancelled = true; };
  }, [authed, machineId, notificationsEnabled]);

  // Swipe right -> open sidebar, swipe left -> close (mobile). Interactive
  // vertical scrollers opt out so a diagonal scroll never becomes navigation.
  const onTouchStart = (e: TouchEvent) => {
    const touch = e.touches[0];
    touchStartX.current = touch.clientX;
    touchStartY.current = touch.clientY;
    touchSwipeLocked.current = e.target instanceof Element
      && !!e.target.closest("[data-lock-horizontal-swipe]");
  };
  const onTouchEnd = (e: TouchEvent) => {
    const touch = e.changedTouches[0];
    const action = resolveSidebarSwipe(
      touchStartX.current,
      touchStartY.current,
      touch.clientX,
      touch.clientY,
      window.innerWidth,
      touchSwipeLocked.current,
    );
    if (action === "open") setSidebarOpen(true);
    else if (action === "close") setSidebarOpen(false);
  };

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  const toggleTheme = () => setTheme((t) => (t === "dark" ? "light" : "dark"));

  // `engine` selects the backend (Claude Code / Codex): the whole UI re-skins via
  // data-engine, and the sidebar re-lists that engine's own sessions.
  const engineRef = useRef(engine);
  engineRef.current = engine;
  const spaceRef = useRef(space);
  spaceRef.current = space;
  useEffect(() => {
    document.documentElement.setAttribute("data-engine", engine);
    localStorage.setItem(ENGINE_KEY, engine);
    wsRef.current?.setSurface(engine, space);
    wsRef.current?.sendListSessions(engine, space);
    if (space === "work") wsRef.current?.sendGetWorkDashboard(engine);
  }, [engine, space]);
  useEffect(() => {
    document.documentElement.setAttribute("data-space", space);
    localStorage.setItem(SPACE_KEY, space);
  }, [space]);
  const rememberSurfaceFocus = (currentEngine: Engine, currentSpace: Space) => {
    if (focusedSid && !state.newChat) {
      lastFocusBySurfaceRef.current[`${currentSpace}:${currentEngine}`] = focusedSid;
    }
  };

  const prepareSurfaceSwitch = (nextEngine: Engine, nextSpace: Space) => {
    rememberSurfaceFocus(engine, space);
    const surfaceKey = `${nextSpace}:${nextEngine}`;
    authoritativeSurfaceListsRef.current.delete(surfaceKey);
    dispatch({
      type: "restore_session_list",
      sessions: sessionListsBySurfaceRef.current[surfaceKey] ?? [],
    });
    const remembered = lastFocusBySurfaceRef.current[surfaceKey];
    preferredSurfaceFocusRef.current = remembered
      ? { key: surfaceKey, sid: remembered } : null;
    didInitFocusRef.current = false;
    wsRef.current?.setSurface(nextEngine, nextSpace);
    wsRef.current?.setFocusedSid(null);
    // Keep the previous surface's transcript out of view while its accepted
    // list is restored. The focus effect below exits this temporary new page as
    // soon as the remembered (or latest valid) session is available.
    dispatch({ type: "enter_new_chat", cwd: "~" });
    setNewChatAutoFocus(false);
  };

  // Engine and Work/Code switches are navigation. Each surface restores the
  // session that was last open there instead of silently starting a new one.
  const toggleEngine = () => {
    const nextEngine: Engine = engine === "codex" ? "claude" : "codex";
    pendingCreateRef.current = null;
    setCreateError(null);
    setStatusOpenSid(null);
    setWorkArtifactsOpen(false);
    setWorkProjectId(null);
    prepareSurfaceSwitch(nextEngine, space);
    setEngine(nextEngine);
    if (isMobile()) setSidebarOpen(false);
  };

  const switchSpace = (next: Space) => {
    if (next === space || !confirmArtifactDiscard()) return;
    pendingCreateRef.current = null;
    setCreateError(null);
    setStatusOpenSid(null);
    setForkWorktreeSession(null);
    setForkWorktreeError(null);
    setWorkArtifactsOpen(false);
    prepareSurfaceSwitch(engine, next);
    setSpace(next);
  };

  // WebSocket lifecycle
  useEffect(() => {
    if (!authed) return;
    const draining = drainingRef.current;
    didInitFocusRef.current = false;  // re-arm initial-focus for this connection lifecycle
    authoritativeSurfaceListsRef.current.delete(`${spaceRef.current}:${engineRef.current}`);

    let cancelled = false;

    // A snapshot announces a session (cc_session_id/state/cwd). We do NOT reset
    // the cursor here anymore — cursors are seeded from the IndexedDB cache before
    // connecting, so hello asks the wrapper only for the DELTA instead of a full
    // history replay of every resident session (that flood wedged reconnect).
    function handleSnapshot(e: Snapshot) {
      dispatch({ type: "event", event: e });
    }

    (async () => {
      let seeded = { cursors: {} as Record<string, number>,
        generations: {} as Record<string, string>,
        controls: {} as Record<string, SessionControl> };
      try { seeded = await import("./cache").then((m) => m.loadAllReplayState()); } catch { /* best-effort */ }
      if (cancelled) return;
      const ws = new RelayWs({
        onEvent: (msg) => {
          if ((msg.type === "user_msg" || msg.type === "turn_end") && msg.sid) {
            const activityMs = Math.round(msg.ts * 1000);
            let changed = false;
            for (const [key, listed] of Object.entries(
              sessionListsBySurfaceRef.current)) {
              const updated = bumpSessionActivity(listed, msg.sid, activityMs);
              if (updated !== listed) {
                sessionListsBySurfaceRef.current[key] = updated;
                changed = true;
              }
            }
            if (msg.type === "user_msg" && changed) {
              sessionActivityPendingRef.current.add(msg.sid);
            }
          }
          if (msg.type === "history_invalidated") {
            const sid = msg.session_id;
            if (historyInvalidationsRef.current.get(sid) !== msg.revision) {
              historyInvalidationsRef.current.set(sid, msg.revision);
              historyCacheEpochRef.current.set(
                sid, (historyCacheEpochRef.current.get(sid) ?? 0) + 1);
              void import("./cache").then((module) =>
                module.invalidateSessionCache(sid));
            }
            // The marker is deliberately tiny/replayable; the full replacement
            // is one-shot and may have been dropped by a disconnect or frame
            // size bound. Fetch immediately when visible; a background session
            // gets the same authoritative request when it is later focused.
            if (stateRef.current.focusedSid === sid) {
              ws.sendGetHistory(sid, undefined, HISTORY_PAGE);
            }
          } else if (msg.type === "artifact_invalidated") {
            const sid = msg.session_id;
            setWorkArtifactsBySid((current) => {
              if (!(sid in current)) return current;
              const next = { ...current };
              delete next[sid];
              return next;
            });
            const session = stateRef.current.sessions.find(
              (candidate) => candidate.session_id === sid);
            if (session?.space === "work"
                || (spaceRef.current === "work"
                  && stateRef.current.focusedSid === sid)) {
              ws.sendGetWorkArtifacts(
                (session?.engine as Engine | undefined) ?? engineRef.current,
                sid,
              );
            }
          } else if (msg.type === "replay_start" && msg.sid
              && (msg.truncated || msg.rebuild)) {
            const sid = msg.sid;
            // If a marker is still retained inside this replay it will follow
            // ReplayStart and replace null with its exact revision.
            if (!historyInvalidationsRef.current.has(sid)) {
              historyInvalidationsRef.current.set(sid, null);
              historyCacheEpochRef.current.set(
                sid, (historyCacheEpochRef.current.get(sid) ?? 0) + 1);
              void import("./cache").then((module) =>
                module.invalidateSessionCache(sid));
            }
            setWorkArtifactsBySid((current) => {
              if (!(sid in current)) return current;
              const next = { ...current };
              delete next[sid];
              return next;
            });
            if (stateRef.current.focusedSid === sid) {
              ws.sendGetHistory(sid, undefined, HISTORY_PAGE);
            }
          } else if (msg.type === "history" && msg.authoritative !== false && !msg.before
              && historyInvalidationsRef.current.has(msg.session_id)) {
            const expected = historyInvalidationsRef.current.get(msg.session_id);
            if (expected === null || expected === msg.revision) {
              // A late first page from an older revision must not re-enable the
              // cache behind a newer destructive marker.
              historyInvalidationsRef.current.delete(msg.session_id);
              void import("./cache").then((module) =>
                module.allowSessionCache(msg.session_id));
            }
          }
          if (msg.type === "rollback_result" && msg.files === "succeeded"
              && stateRef.current.artifact?.sid === msg.session_id) {
            // Diff/file previews are snapshots of bytes that have just changed.
            // Close them instead of leaving a convincing but stale panel open.
            dispatch({ type: "clear_artifact" });
          }
          if (msg.type === "rollback_result" && msg.prefill_text
              && stateRef.current.focusedSid === msg.session_id) {
            setEditPrompt(msg.prefill_text);
          }
          if (msg.type === "btw_opened") {
            const disposition = classifyBtwOpened(
              pendingBtwRef.current, activeBtwRef.current, msg);
            if (disposition === "duplicate") {
              return; // cached replay after a lost ACK; the fork is already open
            }
            if (disposition === "stale") {
              // The user cancelled, navigated, or started a newer request while
              // this fork was connecting. Never let the stale response open the
              // panel, and tear down the now-unowned ephemeral session.
              const discarded = discardedBtwSidsRef.current;
              discarded.add(msg.btw_sid);
              while (discarded.size > 64) {
                const oldest = discarded.values().next().value as string | undefined;
                if (!oldest) break;
                discarded.delete(oldest);
              }
              ws.sendCloseBtw(msg.btw_sid);
              return;
            }
            pendingBtwRef.current = null;
            activeBtwRef.current = {
              requestId: msg.request_id,
              sid: msg.btw_sid,
            };
            setBtwOpening(false);
          } else if (msg.type === "error" && msg.request_id
              && btwRequestIdsRef.current.has(msg.request_id)) {
            const matches = matchesBtwRequest(
              pendingBtwRef.current, msg.request_id);
            if (!matches) return; // obsolete /btw failure; keep any newer spinner
            pendingBtwRef.current = null;
            setBtwOpening(false);
          }
          if (msg.type === "session_forked") {
            const pendingMessageFork = pendingSessionForkRef.current;
            const matchesMessageFork = msg.target === "same_cwd"
              && matchesSessionForkRequest(
              pendingMessageFork, msg.request_id,
              msg.parent_session_id, msg.last_turn_id);
            const matchesWorktreeFork = msg.target === "worktree"
              && matchesWorktreeForkRequest(
              pendingWorktreeForkRef.current, msg.request_id,
              msg.parent_session_id);
            if (!matchesMessageFork && !matchesWorktreeFork) return;
            const targetEngine = matchesMessageFork
              ? pendingMessageFork!.engine
              : "codex";
            if (matchesMessageFork) {
              pendingSessionForkRef.current = null;
              setForkingPointId(null);
            }
            if (matchesWorktreeFork) {
              pendingWorktreeForkRef.current = null;
              setForkWorktreeCreating(false);
              setForkWorktreeError(null);
              setForkWorktreeSession(null);
            }
            setEngine(targetEngine);
            setSpace("code");
            dispatch({ type: "exit_new_chat" });
            dispatch({ type: "focus_session", sid: msg.session_id });
            ws.setSessionEngines([{ session_id: msg.session_id, engine: targetEngine, space: "code" }]);
            ws.setFocusedSid(msg.session_id, targetEngine, "code");
            ws.sendListSessions(targetEngine, "code");
            ws.sendSwitchSession(msg.session_id, targetEngine, "code");
            if (isMobile()) setSidebarOpen(false);
            return;
          }
          if (msg.type === "error" && isTerminalWorktreeForkError(msg.code)
              && matchesSessionForkRequest(
                pendingSessionForkRef.current, msg.request_id)) {
            pendingSessionForkRef.current = null;
            setForkingPointId(null);
            dispatch({ type: "command_error", detail: `${msg.code}: ${msg.message}` });
            return;
          }
          if (msg.type === "error" && isTerminalWorktreeForkError(msg.code)
              && matchesWorktreeForkRequest(
              pendingWorktreeForkRef.current, msg.request_id)) {
            pendingWorktreeForkRef.current = null;
            setForkWorktreeCreating(false);
            setForkWorktreeError(msg.message);
            return;
          }
          if (msg.type === "session_focus" && msg.request_id
              && msg.request_id === pendingCreateRef.current) {
            pendingCreateRef.current = null;
            setCreateError(null);
            dispatch({ type: "exit_new_chat" });
          } else if (msg.type === "error" && msg.code !== "wrapper_offline" && msg.request_id
              && msg.request_id === pendingCreateRef.current) {
            pendingCreateRef.current = null;
            setCreateError(msg.message);
          }
          if (msg.type === "snapshot") {
            if (consumeDiscardedBtwSnapshot(discardedBtwSidsRef.current, msg)) return;
            handleSnapshot(msg);
            return;
          }
          if (msg.type === "session_rekey") {
            setWorkArtifactsBySid((current) => {
              const prior = current[msg.old_key];
              if (!prior) return current;
              const next = { ...current, [msg.session_id]: prior };
              delete next[msg.old_key];
              return next;
            });
            if (spaceRef.current === "work"
                && stateRef.current.focusedSid === msg.old_key) {
              ws.sendListSessions(engineRef.current, "work");
              ws.sendGetWorkArtifacts(engineRef.current, msg.session_id);
            }
            setGoalUiBySid((current) => {
              const prior = current[msg.old_key];
              if (!prior) return current;
              const next = { ...current, [msg.session_id]: prior };
              delete next[msg.old_key];
              return next;
            });
          }
          if (msg.type === "session_list") {
            ws.setSessionEngines(msg.sessions);
            const listedSpace = msg.space ?? "code";
            const surfaceKey = `${listedSpace}:${msg.engine}`;
            sessionListsBySurfaceRef.current[surfaceKey] = msg.sessions;
            authoritativeSurfaceListsRef.current.add(surfaceKey);
            prefetchedSurfacesRef.current.add(surfaceKey);
            // Warm the sibling Work/Code surface once per page lifetime. Codex
            // reuses the just-read native catalog in the wrapper, so this does
            // not start a second app-server and the user's first toggle is fast.
            const siblingSpace: Space = listedSpace === "work" ? "code" : "work";
            const siblingKey = `${siblingSpace}:${msg.engine}`;
            if (!prefetchedSurfacesRef.current.has(siblingKey)) {
              prefetchedSurfacesRef.current.add(siblingKey);
              ws.sendListSessions(msg.engine, siblingSpace);
            }
          }
          if (msg.type === "work_dashboard") {
            setWorkDashboards((current) => ({ ...current, [msg.engine]: msg }));
            setWorkProjectId((current) => current && msg.projects.some(
              (project) => project.project_id === current) ? current : null);
          }
          if (msg.type === "work_artifacts") {
            setWorkArtifactsBySid((current) => ({
              ...current, [msg.session_id]: msg.artifacts,
            }));
          }
          if (msg.type === "engine_capabilities") {
            setCapabilitiesBySurface((current) => ({
              ...current, [`${msg.space}:${msg.engine}`]: msg,
            }));
            if (msg.space === spaceRef.current && msg.engine === engineRef.current) {
              setCapabilitiesLoading(false);
            }
          }
          if (msg.type === "turn_end" && msg.sid && document.hidden
              && localStorage.getItem(NOTIFY_KEY) === "1"
              && !remotePushActiveRef.current
              && typeof Notification !== "undefined"
              && Notification.permission === "granted") {
            const session = stateRef.current.sessions.find(
              (candidate) => candidate.session_id === msg.sid);
            const label = session?.engine === "codex" ? "Codex" : "Claude";
            const body = msg.result.is_error ? `${label} 会话执行失败` : `${label} 会话已经完成`;
            void navigator.serviceWorker?.ready.then((registration) =>
              registration.showNotification("cc-remote", {
                body, icon: "/favicon.svg", badge: "/favicon.svg",
                tag: `turn-${msg.sid}`, data: { url: "/" },
              })).catch(() => undefined);
          }
          if (msg.type === "session_focus" && spaceRef.current === "work"
              && !msg.session_id.startsWith("tmp-")) {
            ws.sendGetWorkArtifacts(engineRef.current, msg.session_id);
          }
          if (msg.type === "session_list"
              && !shouldAcceptSessionList(engineRef.current, spaceRef.current, msg)) return;
          if (msg.type === "session_list") {
            const currentSid = stateRef.current.focusedSid;
            if (currentSid && !currentSid.startsWith("tmp-")
                && !msg.sessions.some((session) => session.session_id === currentSid)) {
              didInitFocusRef.current = false;
              preferredSurfaceFocusRef.current = null;
            }
          }
          if ((msg.type === "turn_end"
              || (msg.type === "error" && msg.code !== "wrapper_offline"))
              && msg.sid) {
            draining.delete(msg.sid);
          }
          dispatch({ type: "event", event: msg });
          if (msg.type === "wrapper_reconnected") {
            ws.sendListSessions(engineRef.current, spaceRef.current);
            if (spaceRef.current === "work") {
              ws.sendGetWorkDashboard(engineRef.current);
            }
            ws.sendGetModels("codex");
            const currentSid = stateRef.current.focusedSid;
            if (currentSid) ws.sendGetHistory(currentSid, undefined, HISTORY_PAGE);
          }
          // refresh the context ring after each turn (local SDK query, no model tokens)
          if (msg.type === "turn_end" && msg.sid) {
            ws.sendGetContextTo(msg.sid);
            if (sessionActivityPendingRef.current.delete(msg.sid)) {
              const listed = Object.values(sessionListsBySurfaceRef.current)
                .flat().find((session) => session.session_id === msg.sid);
              ws.sendListSessions(
                (listed?.engine as Engine | undefined) ?? engineRef.current,
                listed?.space ?? spaceRef.current,
              );
            }
            const session = stateRef.current.sessions.find(
              (candidate) => candidate.session_id === msg.sid);
            if (session?.space === "work"
                || (spaceRef.current === "work" && stateRef.current.focusedSid === msg.sid)) {
              ws.sendGetWorkArtifacts(
                (session?.engine as Engine | undefined) ?? engineRef.current, msg.sid);
            }
          }
        },
        onConnState: (s, detail) => {
          dispatch({ type: "conn", connState: s, detail });
          if (s === "connected") {
            ws.sendListSessions(engineRef.current, spaceRef.current);
            if (spaceRef.current === "work") {
              ws.sendGetWorkDashboard(engineRef.current);
              const currentSid = stateRef.current.focusedSid;
              if (currentSid) ws.sendGetWorkArtifacts(engineRef.current, currentSid);
            }
            // Always fetch codex's catalog, not just when codex is the active engine:
            // the engine pill switches instantly and must render real models/efforts.
            // The wrapper caches it, so a refresh doesn't respawn an app-server.
            ws.sendGetModels("codex");
          }
        },
        onAuthFail: () => {
          setAuthReady(false);
          clearLegacyAuthMarkers(localStorage);
          pendingCreateRef.current = null;
          pendingBtwRef.current = null;
          pendingSessionForkRef.current = null;
          pendingWorktreeForkRef.current = null;
          activeBtwRef.current = null;
          btwRequestIdsRef.current.clear();
          discardedBtwSidsRef.current.clear();
          historyInvalidationsRef.current.clear();
          historyCacheEpochRef.current.clear();
          setBtwOpening(false);
          setForkingPointId(null);
          setRollbackTarget(null);
          setForkWorktreeSession(null);
          setForkWorktreeCreating(false);
          setForkWorktreeError(null);
          setGoalUiBySid({});
          setStatusOpenSid(null);
          setWorkArtifactsOpen(false);
          setWorkArtifactsBySid({});
          dispatch({ type: "reset" });
          setAuthed(false);
          void (async () => {
            try { await import("./cache").then((module) => module.clearCache()); }
            catch { /* best-effort local cleanup */ }
            setAuthReady(true);
          })();
        },
        onCommandError: (detail) => dispatch({ type: "command_error", detail }),
        onOutboxChanged: (protectedSids) => {
          dispatch({ type: "prune_runtimes", protectedSids });
        },
        onWrapperGenerationChanged: () => {
          discardedBtwSidsRef.current.clear();
          if (stateRef.current.btwSid || pendingBtwRef.current
              || activeBtwRef.current) {
            pendingBtwRef.current = null;
            activeBtwRef.current = null;
            setBtwOpening(false);
            if (stateRef.current.btwSid) dispatch({ type: "clear_btw" });
            dispatch({ type: "command_error",
              detail: "wrapper 已重启，临时 /btw 会话已关闭，请重新打开。" });
          }
        },
      }, machineId);
      ws.setSurface(engineRef.current, spaceRef.current);
      // Seed both transport and reducer watermarks before Hello. This prevents
      // an older replay/snapshot from reviving a lock already superseded in the
      // last authoritative control snapshot.
      ws.seedReplayState(seeded.cursors, seeded.generations, seeded.controls);
      for (const [sid, control] of Object.entries(seeded.controls)) {
        dispatch({
          type: "hydrate_cache", sid, turns: [], revision: null,
          generation: seeded.generations[sid] ?? control.generation, control,
        });
      }
      wsRef.current = ws;
      ws.start();
    })();

    return () => {
      cancelled = true;
      wsRef.current?.stop();
      wsRef.current = null;
      draining.clear();
    };
  }, [authed, machineId]);

  // Land on the preferred/recent session only after an accepted list for the
  // active engine+space arrives. Background snapshots never pick focus.
  useEffect(() => {
    if (didInitFocusRef.current || !wsRef.current) return;
    const surfaceKey = `${spaceRef.current}:${engineRef.current}`;
    if (!authoritativeSurfaceListsRef.current.has(surfaceKey)) return;
    if (state.sessions.length === 0) {
      preferredSurfaceFocusRef.current = null;
      didInitFocusRef.current = true;
      return;
    }
    const preferred = preferredSurfaceFocusRef.current?.key === surfaceKey
      ? state.sessions.find((session) => (
          session.session_id === preferredSurfaceFocusRef.current?.sid
          && (session.space ?? "code") === spaceRef.current
          && (session.engine ?? "claude") === engineRef.current
        ))
      : undefined;
    preferredSurfaceFocusRef.current = null;
    const latest = preferred ?? [...state.sessions]
      .filter((s) => s.tag !== "archived")
      .sort(compareSessionsByActivity)[0]
      ?? state.sessions[0];
    didInitFocusRef.current = true;
    if (latest && latest.session_id !== state.focusedSid) {
      dispatch({ type: "exit_new_chat" });
      dispatch({ type: "focus_session", sid: latest.session_id });
      const latestEngine = (latest.engine as "claude" | "codex") || engineRef.current;
      wsRef.current.setFocusedSid(latest.session_id, latestEngine, spaceRef.current);
      wsRef.current.sendSwitchSession(latest.session_id, latestEngine, spaceRef.current);
    }
  }, [state.sessions, state.focusedSid]);

  // Direct sidebar selection and newly-created sessions both update the
  // per-surface bookmark. A later Work/Code or engine toggle can therefore
  // restore the exact view without relying on whichever list row happens to be
  // newest at that moment.
  useEffect(() => {
    if (!focusedSid || state.newChat) return;
    const selected = state.sessions.find((session) => session.session_id === focusedSid);
    if (!selected) return;
    const selectedEngine = (selected.engine as Engine | undefined) ?? engine;
    const selectedSpace: Space = selected.space === "work" ? "work" : "code";
    lastFocusBySurfaceRef.current[`${selectedSpace}:${selectedEngine}`] = focusedSid;
  }, [focusedSid, state.newChat, state.sessions, engine]);

  // Drain every resident session, not just the one currently visible. A queued
  // background turn must resume when that runtime becomes idle even if the user
  // has switched elsewhere. Never remove work merely because the socket or
  // wrapper is offline; accepted commands are retained by RelayWs's outbox.
  useEffect(() => {
    const draining = drainingRef.current;
    for (const sid of draining) {
      const runtime = state.runtimes[sid];
      if (!runtime || runtime.state !== "idle") draining.delete(sid);
    }

    const ws = wsRef.current;
    if (!ws) return;
    const candidates = selectDrainCandidates(
      state.runtimes,
      draining,
      state.connState === "connected",
      state.wrapperOnline,
    );
    for (const { sid, source, query } of candidates) {
      const msg_id = uuid();
      if (!ws.sendQueryTo(sid, query.prompt, msg_id, query.images, query.files)) continue;
      draining.add(sid);
      dispatch({ type: "query_sent", sid, prompt: query.prompt, msg_id,
        images: query.images, files: query.files, ts: Date.now() });
      if (source === "pending") dispatch({ type: "clear_pending", sid });
      else dispatch({ type: "dequeue_at", sid, i: 0 });
    }
  }, [state.runtimes, state.connState, state.wrapperOnline]);

  // Keep a long-lived tab bounded without evicting anything that can still be
  // acted on. ACK callbacks run the same prune when an outbox target becomes
  // reclaimable; otherwise an idle runtime protected during retry would linger.
  useEffect(() => {
    if (Object.keys(state.runtimes).length <= MAX_RUNTIME_SESSIONS) return;
    dispatch({
      type: "prune_runtimes",
      protectedSids: wsRef.current?.pendingSessionIds() ?? [],
    });
  }, [state.runtimes, focusedSid, state.btwSid, state.artifact?.sid]);

  // Persist the focused session's turns to IndexedDB (Phase-2 will write through
  // background sessions too). Coalesced in cache.ts.
  useEffect(() => {
    const sid = rt.ccSessionId;
    const revision = rt.historyRevision;
    if (!sid || !revision
        || historyInvalidationsRef.current.has(sid)) return;
    import("./cache").then(({ saveSession }) => {
      const live = wsRef.current?.lastSeqFor(sid) || 0;
      saveSession(
        sid, rt.turns, live, revision,
        wsRef.current?.generationFor(sid),
        rt.control,
      );
    });
  }, [focusedSid, rt.turns, rt.ccSessionId, rt.historyRevision, rt.control]);

  // Hydrate the focused session's turns from IndexedDB for an INSTANT render on
  // switch — the wrapper's replay (for non-resident sessions) then reconciles.
  // This completes the previously write-only cache: switching no longer shows an
  // empty view while waiting on a cold wrapper round-trip. A 6s fallback clears
  // the spinner if a session has no cache and the wrapper stays silent.
  useEffect(() => {
    const sid = focusedSid;
    if (!sid) return;
    let cancelled = false;
    const cacheEpoch = historyCacheEpochRef.current.get(sid) ?? 0;
    import("./cache").then(({ loadSession }) => loadSession(sid)).then((cached) => {
      if (!cancelled
          && cacheEpoch === (historyCacheEpochRef.current.get(sid) ?? 0)
          && !historyInvalidationsRef.current.has(sid)
          && cached && Array.isArray(cached.turns)
          && (cached.turns.length || cached.control)) {
        dispatch({
          type: "hydrate_cache", sid, turns: cached.turns as Turn[],
          revision: cached.revision,
          generation: cached.generation ?? cached.control?.generation,
          control: cached.control,
        });
      }
    });
    const t = window.setTimeout(() => dispatch({
      type: "hydrate_cache", sid, turns: [], revision: null,
    }), 6000);
    return () => { cancelled = true; window.clearTimeout(t); };
  }, [focusedSid]);

  // Fetch authoritative history for the focused session — bulk, on-demand, read
  // from the transcript (like a web chat's GET /conversation). Fires on focus
  // change AND on (re)connect, so a reconnect re-syncs any turns that completed
  // while we were away. The `history` event reconciles over the instant cache paint.
  useEffect(() => {
    if (!focusedSid || state.connState !== "connected") return;
    wsRef.current?.sendGetHistory(focusedSid, undefined, HISTORY_PAGE);
  }, [focusedSid, state.connState]);

  // Cmd/Ctrl+B => toggle sidebar; Cmd/Ctrl+Shift+B => open latest turn's diff
  useEffect(() => {
    if (!authed) return;
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const k = e.key.toLowerCase();
      if (k === "b" && e.shiftKey) {           // diff (shared right slot)
        e.preventDefault();
        const latest = shortcutRef.current;
        if (latest.artifact?.kind === "gitdiff" && latest.rightView === "diff") dispatch({ type: "clear_artifact" });
        else latest.getDiff("");
      } else if (k === "b") {                    // toggle sidebar
        e.preventDefault();
        setSidebarOpen((v) => !v);
      } else if (k === "k" && e.shiftKey) {      // /btw side panel (shared right slot)
        e.preventDefault();
        const latest = shortcutRef.current;
        if (latest.btwSid && latest.rightView === "btw") latest.closeBtw();
        else latest.openBtw();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authed]);

  // Shift+Tab follows each engine's real mode control: Claude cycles permission
  // modes; Codex toggles collaboration mode without touching approvalPolicy.
  useEffect(() => {
    if (!authed) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab" && e.shiftKey) {
        e.preventDefault();
        if ((rt.control && sessionControlLocksInput(rt.control))
            || (!rt.control && rt.external)) return;
        if (focusedEngine === "codex") {
          setCollaborationMode(
            rt.collaborationMode === "plan" ? "default" : "plan");
          return;
        }
        const modes = permsFor(focusedEngine).map((p) => p.id);
        const current = modes.indexOf(rt.perm);
        setPerm(modes[current < 0 ? 0 : (current + 1) % modes.length]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authed, focusedSid, rt.perm, rt.collaborationMode, rt.control,
    rt.external, focusedEngine]);

  // ---- /btw effects ----
  // These MUST stay ABOVE the `!authed` early return below. Hooks have to run
  // unconditionally and in the same order on every render; putting them after the
  // return meant logging out (authed -> false) rendered fewer hooks than the
  // previous render, and React blew up with #300 ("rendered fewer hooks than
  // expected"). Logging back in tripped the mirror image. Refreshing "fixed" it
  // only because a fresh mount has no previous render to disagree with.
  //
  // A /btw fork belongs to the session it was forked from. When you switch session
  // or toggle engine, discard it — else a codex btw would linger while you view a
  // cc session ("cc shows codex btw"). Read via ref so opening btw (no focus
  // change) doesn't trip this, and it only fires on actual navigation.
  const btwSidRef = useRef<string | null>(null);
  btwSidRef.current = state.btwSid;
  useEffect(() => {
    pendingBtwRef.current = null;
    activeBtwRef.current = null;
    setBtwOpening(false);
    const s = btwSidRef.current;
    if (s) { wsRef.current?.sendCloseBtw(s); dispatch({ type: "clear_btw" }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusedSid, engine]);

  if (!authReady) {
    return <div className="login" aria-busy="true">正在连接中继…</div>;
  }

  if (!authed) {
    return <LoginForm onLogin={() => { dispatch({ type: "reset" }); setAuthed(true); }} theme={theme} onToggleTheme={toggleTheme} />;
  }

  const sendQuery = (prompt: string, images?: QueryImg[], files?: QueryFile[]): boolean => {
    if (!wsRef.current || !focusedSid) return false;
    const msg_id = uuid();
    if (!wsRef.current.sendQuery(prompt, msg_id, images, files)) return false;
    const activityMs = Date.now();
    const surfaceKey = `${space}:${engine}`;
    const cached = sessionListsBySurfaceRef.current[surfaceKey];
    if (cached) {
      sessionListsBySurfaceRef.current[surfaceKey] = bumpSessionActivity(
        cached, focusedSid, activityMs);
    }
    sessionActivityPendingRef.current.add(focusedSid);
    dispatch({ type: "query_sent", sid: focusedSid, prompt, msg_id, images, files,
      ts: activityMs });
    return true;
  };
  // One command creates the session and starts its first query atomically. The
  // wrapper targets the new temp-keyed ctx directly; no later focus event is used
  // to route or trigger this message.
  const sendFirstMessage = (prompt: string, images?: QueryImg[], files?: QueryFile[],
                            collaborationMode?: CollaborationModeName,
                            permissionMode?: CodexPermissionMode,
                            serviceTier?: CodexServiceTier): boolean => {
    if (!wsRef.current || !state.newChat) return false;
    const { cwd, model, effort } = state.newChat;
    // Null is meaningful: let the local CLI/app-server use its configured defaults.
    // Only explicit user choices cross the wire; otherwise a stale fallback catalog
    // could silently override the machine's real model or reasoning configuration.
    const msg_id = uuid();
    const queued = wsRef.current.sendNewSession(
      space === "work" ? null : cwd, engine, model, effort,
      { prompt, msg_id, images, files },
      engine === "codex" ? collaborationMode : undefined,
      engine === "codex"
        ? (space === "work" ? "on-request" : permissionMode)
        : undefined,
      engine === "codex" ? serviceTier : undefined,
      space, space === "work" ? workProjectId : undefined);
    if (queued) {
      pendingCreateRef.current = msg_id;
      setCreateError(null);
    }
    return queued;
  };
  const interrupt = () => wsRef.current?.sendInterrupt();
  const setModel = (model: string) => {
    wsRef.current?.sendSetModel(model);
  };
  const setEffort = (effort: string) => {
    wsRef.current?.sendSetEffort(effort);
  };
  // Codex Fast mode is persisted by app-server per thread. The runtime's Fast
  // event owns the chip state; here we only forward the requested transition.
  const setServiceTier = (tier: string) => {
    wsRef.current?.sendSetServiceTier(tier);
  };
  const setPerm = (perm: string) => {
    wsRef.current?.sendSetPerm(perm);
  };
  const setCollaborationMode = (mode: CollaborationModeName) => {
    wsRef.current?.sendSetCollaborationMode(mode);
  };
  const setGoalUi = (patch: Partial<{ revealed: boolean; open: boolean }>) => {
    if (!focusedSid) return;
    setGoalUiBySid((current) => {
      const previous = current[focusedSid] ?? { revealed: false, open: false };
      return { ...current, [focusedSid]: { ...previous, ...patch } };
    });
  };
  const runGoal = (args: string) => {
    if (!focusedSid) return;
    const command = parseGoalCommand(args);
    if (command.kind === "clear") {
      wsRef.current?.sendClearGoal();
      setGoalUi({ revealed: false, open: false });
      return;
    }
    setGoalUi({ revealed: true, open: true });
    if (command.kind === "show") wsRef.current?.sendGetGoal();
    else wsRef.current?.sendSetGoal(command.objective, "active", null);
  };
  const openCodexRollback = (numTurns: number, sessionId = focusedSid) => {
    if (!sessionId) return;
    setRollbackTarget({
      sessionId, numTurns,
      label: `最近 ${numTurns} 轮`,
    });
  };
  const confirmRollback = (mode: RestoreMode) => {
    const target = rollbackTarget;
    if (!target) return;
    wsRef.current?.sendRollbackSession(
      target.sessionId, "codex", mode, target.numTurns,
      target.checkpointId);
    setRollbackTarget(null);
  };
  const openStatus = () => {
    if (!focusedSid) return;
    setStatusOpenSid(focusedSid);
    const requestId = wsRef.current?.sendGetStatus();
    if (requestId) {
      dispatch({ type: "begin_status_request", sid: focusedSid, requestId });
    }
  };
  const requestContext = () => {
    if (!focusedSid) return;
    const requestId = wsRef.current?.sendGetContext();
    if (requestId) {
      dispatch({ type: "begin_context_request", sid: focusedSid, requestId });
    }
  };
  const forkFromTurn = (forkPointId: string) => {
    if (!focusedSid
        || pendingSessionForkRef.current || pendingWorktreeForkRef.current) return;
    const requestId = wsRef.current?.sendForkSession(
      focusedSid, forkPointId) ?? null;
    if (!requestId) {
      dispatch({ type: "command_error",
        detail: "派生请求未发送，请等待连接恢复后重试。" });
      return;
    }
    pendingSessionForkRef.current = {
      requestId,
      parentSessionId: focusedSid,
      forkPointId,
      engine: focusedEngine,
    };
    setForkingPointId(forkPointId);
  };
  const openForkWorktree = (session: SessionInfo) => {
    if (pendingSessionForkRef.current || pendingWorktreeForkRef.current) return;
    setForkWorktreeError(null);
    setForkWorktreeSession(session);
  };
  const submitForkWorktree = (name: string) => {
    const source = forkWorktreeSession;
    if (!source || pendingWorktreeForkRef.current) return;
    setForkWorktreeError(null);
    const requestId = wsRef.current?.sendForkSessionWorktree(source.session_id, name) ?? null;
    if (!requestId) {
      setForkWorktreeError("请求未发送，请等待连接恢复后重试。");
      return;
    }
    pendingWorktreeForkRef.current = {
      requestId,
      parentSessionId: source.session_id,
    };
    setForkWorktreeCreating(true);
  };
  const closeForkWorktree = () => {
    if (pendingWorktreeForkRef.current) return;
    setForkWorktreeSession(null);
    setForkWorktreeError(null);
  };
  const getDiff = (file: string) => {
    if (!confirmArtifactDiscard()) return;
    const requestId = wsRef.current?.sendGetDiff(file, theme) ?? null;
    if (!requestId) return;
    setRightView("diff");
    dispatch({ type: "open_artifact_loading", file, sid: focusedSid, requestId });
  };
  const previewFile = (file: string, line?: number) => {
    if (!focusedSid) return;
    if (!confirmArtifactDiscard()) return;
    const requestId = wsRef.current?.sendGetFilePreview(file) ?? null;
    if (!requestId) return;
    setRightView("diff");
    dispatch({
      type: "open_file_loading",
      file,
      sid: focusedSid,
      requestId,
      kind: isMarkdownPath(file) ? "md" : "file",
      line,
    });
  };
  const previewMarkdown = (file: string) => previewFile(file);
  const loadPreviewAsset = (file: string, previewId: string): boolean =>
    !!wsRef.current?.sendGetPreviewAsset(file, previewId);
  const saveMarkdown = (file: string, content: string, expectedSize: number,
                        expectedMtimeNs: string, expectedRevision: string): string | null => {
    const requestId = wsRef.current?.sendSaveMarkdown(
      file, content, expectedSize, expectedMtimeNs, expectedRevision) ?? null;
    if (requestId) dispatch({ type: "start_file_save", requestId, content });
    return requestId;
  };
  // /btw: fork the focused session into an ephemeral side panel (wrapper replies
  // BtwOpened → reducer opens the panel). Send/close target the fork by its sid.
  const openBtw = () => {
    if (!confirmArtifactDiscard()) return;
    setRightView("btw");
    if (!focusedSid || state.btwSid || pendingBtwRef.current) return;
    const requestId = wsRef.current?.sendOpenBtw(focusedSid) ?? null;
    if (!requestId) { setBtwOpening(false); return; }
    pendingBtwRef.current = requestId;
    const requestIds = btwRequestIdsRef.current;
    requestIds.add(requestId);
    while (requestIds.size > 64) {
      const oldest = requestIds.values().next().value as string | undefined;
      if (!oldest) break;
      requestIds.delete(oldest);
    }
    setBtwOpening(true);
  };
  const sendBtw = (prompt: string) => { if (state.btwSid) wsRef.current?.sendQueryTo(state.btwSid, prompt, uuid()); };
  const closeBtw = () => {
    pendingBtwRef.current = null;
    activeBtwRef.current = null;
    setBtwOpening(false);
    if (state.btwSid) {
      wsRef.current?.sendCloseBtw(state.btwSid);
      dispatch({ type: "clear_btw" });
    }
  };
  // Header tab switch between the two right-slot views (opening the target lazily).
  const switchRight = (v: "diff" | "btw") => {
    if (v === "diff") {
      setRightView("diff");
      if (!state.artifact) getDiff("");
    } else openBtw();
  };
  shortcutRef.current = {
    artifact: state.artifact, btwSid: state.btwSid, rightView,
    getDiff, openBtw, closeBtw,
  };
  const logout = async () => {
    try {
      const response = await fetch("/api/logout", {
        method: "POST", credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await import("./cache").then((module) => module.clearCache());
      wsRef.current?.stop();
      pendingCreateRef.current = null;
      pendingBtwRef.current = null;
      pendingSessionForkRef.current = null;
      pendingWorktreeForkRef.current = null;
      sessionActivityPendingRef.current.clear();
      activeBtwRef.current = null;
      btwRequestIdsRef.current.clear();
      discardedBtwSidsRef.current.clear();
      historyInvalidationsRef.current.clear();
      historyCacheEpochRef.current.clear();
      setCreateError(null);
      setForkingPointId(null);
      setRollbackTarget(null);
      setForkWorktreeSession(null);
      setForkWorktreeCreating(false);
      setForkWorktreeError(null);
      dispatch({ type: "reset" });
      setAuthed(false);
    } catch {
      dispatch({ type: "command_error", detail: "退出失败：服务暂不可用，请稍后重试" });
    }
  };

  return (
    <div className={"shell" + (sidebarOpen ? " sidebar-open" : "") + ((state.artifact || state.btwSid || btwOpening) ? " panel-open" : "")} onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
      <SessionsSidebar
        open={sidebarOpen}
        space={space}
        onSpaceChange={switchSpace}
        sessions={state.sessions}
        liveStates={Object.fromEntries(Object.entries(state.runtimes).map(([sid, r]) => [sid, r.state]))}
        activeSessionId={focusedSid}
        onSelect={(id) => { if (!confirmArtifactDiscard()) return; pendingCreateRef.current = null; setCreateError(null); setStatusOpenSid(null); setWorkArtifactsOpen(false); const selected = state.sessions.find((s) => s.session_id === id); const selectedEngine = (selected?.engine as "claude" | "codex") || engine; const selectedSpace = selected?.space === "work" ? "work" : space; dispatch({ type: "exit_new_chat" }); dispatch({ type: "focus_session", sid: id }); wsRef.current?.setFocusedSid(id, selectedEngine, selectedSpace); wsRef.current?.sendSwitchSession(id, selectedEngine, selectedSpace); if (selectedSpace === "work") wsRef.current?.sendGetWorkArtifacts(selectedEngine, id); if (isMobile()) setSidebarOpen(false); }}
        onNew={() => { if (!confirmArtifactDiscard()) return; pendingCreateRef.current = null; setCreateError(null); setStatusOpenSid(null); setNewChatAutoFocus(true); wsRef.current?.setFocusedSid(null); dispatch({ type: "enter_new_chat", cwd: "~" }); if (isMobile()) setSidebarOpen(false); }}
        onNewInDir={(cwd) => { if (!confirmArtifactDiscard()) return; pendingCreateRef.current = null; setCreateError(null); setStatusOpenSid(null); setNewChatAutoFocus(true); wsRef.current?.setFocusedSid(null); dispatch({ type: "enter_new_chat", cwd }); if (isMobile()) setSidebarOpen(false); }}
        onClose={() => setSidebarOpen(false)}
        onRename={(id, title) => wsRef.current?.sendRenameSession(id, title, engine, space)}
        onArchive={(id, archived) => { wsRef.current?.sendArchiveSession(id, archived, engine, space); }}
        onPin={(session, pinned) => {
          const target = sessionCommandTarget(session, engine, space);
          const surfaceKey = `${target.space}:${target.engine}`;
          const cached = sessionListsBySurfaceRef.current[surfaceKey];
          if (cached) {
            sessionListsBySurfaceRef.current[surfaceKey] = setSessionPinned(
              cached, session.session_id, pinned);
          }
          dispatch({ type: "set_session_pinned", sid: session.session_id, pinned });
          wsRef.current?.sendPinSession(
            session.session_id, pinned, target.engine, target.space);
        }}
        onDelete={(id) => {
          const warning = space === "work"
            ? "删除后将永久移除这项工作及其私有文件，确定继续吗？"
            : "删除后将永久移除这条会话历史；代码文件不会被删除，确定继续吗？";
          if (!window.confirm(warning)) return;
          if (focusedSid === id) dispatch({ type: "enter_new_chat", cwd: "~" });
          wsRef.current?.sendDeleteSession(id, engine, space);
        }}
        onRollback={(id) => {
          openCodexRollback(1, id);
        }}
        onForkWorktree={openForkWorktree}
      />
      <DirPicker
        open={dirPickerOpen}
        path={state.dirPicker?.path ?? null}
        parent={state.dirPicker?.parent ?? null}
        dirs={state.dirPicker?.dirs ?? []}
        onBrowse={(p) => wsRef.current?.sendListDir(p)}
        onConfirm={(cwd) => { if (state.newChat) dispatch({ type: "set_new_chat_cwd", cwd }); setDirPickerOpen(false); }}
        onClose={() => setDirPickerOpen(false)}
      />
      <RollbackSheet target={rollbackTarget}
        onClose={() => setRollbackTarget(null)} onConfirm={confirmRollback} />
      <section className={`pane ${space}-pane`}>
        <header className={`c-head ${space}-head`}>
          <div className="titlewrap">
            <div className="ttl">
              <button className="surface-head-title" onClick={() => setSidebarOpen(true)}>
                <span className="surface-head-mark"><Icon name={space === "work" ? "work" : "code"} size={18} /></span>
                <span>{space === "work" ? "Work" : "Code"}</span>
              </button>
            </div>
            <div className="sub">{space === "work" ? "私有工作区 · " : ""}{rt.ccSessionId ? `session ${rt.ccSessionId.slice(0, 8)}` : "connected"}</div>
          </div>
          <span className={`hstat ${rt.state}`}><span className="sd" />
            <span className="hstat-label">{rt.state}</span></span>
          {space === "code" && focusedSid && !state.newChat && (
            <TerminalControl control={rt.control} engine={focusedEngine}
              availability={state.connState !== "connected" || !state.wrapperOnline
                ? "offline" : rt.replaying || !rt.syncReady ? "syncing" : "online"}
              legacyExternal={!rt.control && !!rt.external}
              legacyTakeoverPending={rt.takeoverPending}
              legacyMessage={rt.takeoverMessage}
              onTakeover={() => wsRef.current?.sendTakeover(focusedSid)} />
          )}
          {machines.length > 1 && <select className="machine-select"
            value={machineId} onChange={(event) => setMachineId(event.target.value)}
            aria-label="远程机器" title="切换远程机器">
            {machines.map((machine) => <option key={machine} value={machine}>{machine}</option>)}
          </select>}
          <button className="engine-toggle" onClick={toggleEngine} aria-label="切换新会话引擎"
            title="新建会话使用的引擎">{engine === "codex" ? "◇ Codex" : "✳ Claude"}</button>
          <button className="iconbtn header-secondary" onClick={() => {
            setCapabilitiesOpen(true);
            setCapabilitiesLoading(true);
            wsRef.current?.sendGetEngineCapabilities(
              engine, space, state.newChat?.cwd ?? state.currentCwd);
          }} aria-label="Agent 能力" title="技能、插件、应用与 MCP">
            <Icon name="spark" />
          </button>
          {typeof Notification !== "undefined" && <button
            className={`iconbtn header-secondary${notificationsEnabled ? " notify-on" : ""}`}
            onClick={() => { void (async () => {
              if (notificationsEnabled) {
                localStorage.removeItem(NOTIFY_KEY);
                setNotificationsEnabled(false);
                remotePushActiveRef.current = false;
                await disableRemotePush();
                return;
              }
              const permission = await Notification.requestPermission();
              const enabled = permission === "granted";
              if (enabled) localStorage.setItem(NOTIFY_KEY, "1");
              else localStorage.removeItem(NOTIFY_KEY);
              setNotificationsEnabled(enabled);
              remotePushActiveRef.current = enabled
                ? await enableRemotePush(machineId) : false;
            })(); }} aria-label="完成提醒"
            title={notificationsEnabled ? "后台完成提醒已开启" : "开启后台完成提醒"}>
            <Icon name="notify" />
          </button>}
          <button className="iconbtn header-secondary" onClick={toggleTheme} aria-label="切换主题">
            <Icon name={theme === "dark" ? "sun" : "moon"} />
          </button>
          <button className="iconbtn" onClick={() => void logout()}
            aria-label="退出登录" title="退出登录"><Icon name="logout" /></button>
        </header>

        <ReconnectBanner banner={state.banner} replaying={rt.replaying}
          truncated={rt.truncated}
          busy={state.connState !== "connected" || !state.wrapperOnline || rt.replaying} />
        <NoticeStack notices={rt.notices}
          onDismiss={(noticeId) => {
            if (focusedSid) dispatch({ type: "dismiss_notice", sid: focusedSid, noticeId });
          }} />

        {state.newChat ? (
          <NewChatView cwd={state.newChat.cwd} space={space}
            createError={createError}
            autoFocus={newChatAutoFocus}
            engine={engine}
            workDashboard={workDashboards[engine] ?? null}
            selectedProjectId={workProjectId}
            onSelectProject={setWorkProjectId}
            onManageWork={() => setWorkManagerOpen(true)}
            onPickCwd={() => setDirPickerOpen(true)}
            onSend={sendFirstMessage} />
        ) : (
          <>
            <ChatView sid={focusedSid} turns={rt.turns} loading={!!rt.loading}
              surface={space}
              engine={focusedEngine} forkingPointId={forkingPointId}
              hasMore={!!rt.hasMore}
              onLoadMore={() => { if (focusedSid) wsRef.current?.sendGetHistory(focusedSid, rt.oldestId, HISTORY_PAGE); }}
              onEdit={(prompt) => setEditPrompt(prompt)} onGetDiff={getDiff}
              onPreviewMarkdown={previewMarkdown}
              onOpenFile={previewFile}
              onOpenArtifacts={() => {
                if (focusedSid) {
                  wsRef.current?.sendGetWorkArtifacts(focusedEngine, focusedSid);
                }
                setWorkArtifactsOpen(true);
              }}
              onFork={space === "code" ? forkFromTurn : undefined} />

            <GoalPanel engine={engine} goal={rt.goal}
              revealed={!!goalUi?.revealed} open={!!goalUi?.open}
              onOpen={() => { wsRef.current?.sendGetGoal(); setGoalUi({ revealed: true, open: true }); }}
              onClose={() => setGoalUi({ open: false })}
              onDismiss={() => setGoalUi({ revealed: false, open: false })}
              onSave={(objective, status, budget) => {
                wsRef.current?.sendSetGoal(objective, status, engine === "codex" ? budget : null);
                setGoalUi({ revealed: true, open: false });
              }}
              onClear={() => {
                wsRef.current?.sendClearGoal();
                setGoalUi({ revealed: false, open: false });
              }} />

            <Composer
          surface={space}
          state={rt.state}
          catalog={state.catalog}
          connState={state.connState}
          wrapperOnline={state.wrapperOnline}
          sendMode={state.sendMode}
          setSendMode={(m) => dispatch({ type: "set_send_mode", mode: m })}
          queue={rt.queue}
          allQueued={allQueued}
          replaceableQueued={replaceableQueued}
          model={rt.model}
          effort={rt.effort}
          perm={rt.perm}
          collaborationMode={rt.collaborationMode}
          fast={rt.fast}
          control={rt.control}
          external={rt.external}
          takeoverPending={rt.takeoverPending}
          takeoverMessage={rt.takeoverMessage}
          engine={focusedEngine}
          editPrompt={editPrompt}
          onEditConsumed={() => setEditPrompt(null)}
          onSendQuery={sendQuery}
          onInterrupt={interrupt}
          onEnqueue={(query) => dispatch({ type: "enqueue", query })}
          onSetPending={(query) => dispatch({ type: "set_pending", query })}
          onDequeue={(i) => { if (focusedSid) dispatch({ type: "dequeue_at", sid: focusedSid, i }); }}
          onSetModel={setModel}
          onSetEffort={setEffort}
          onSetServiceTier={setServiceTier}
          onSetPerm={setPerm}
          onSetCollaborationMode={setCollaborationMode}
          onClear={() => dispatch({ type: "enter_new_chat", cwd: space === "work" ? "~" : state.currentCwd })}
          onContext={requestContext}
          onOpenBtw={openBtw}
          onPreview={previewMarkdown}
          onGoal={runGoal}
          onStatus={openStatus}
          onReview={(target, value) => {
            if (focusedSid) wsRef.current?.sendStartReview(focusedSid, target, value);
          }}
          onCompact={() => {
            if (focusedSid) wsRef.current?.sendCompactSession(focusedSid);
          }}
          onRollback={(numTurns) => {
            if (!focusedSid) return;
            openCodexRollback(numTurns);
          }}
          workArtifactCount={space === "work" ? currentWorkArtifacts.length : 0}
          onOpenArtifacts={() => {
            if (focusedSid) {
              wsRef.current?.sendGetWorkArtifacts(focusedEngine, focusedSid);
            }
            setWorkArtifactsOpen(true);
          }}
          contextReport={rt.contextReport}
          contextError={rt.contextError}
        />
          </>
        )}
        {/* context usage now lives in the composer's ring popover (see Composer) */}
      </section>
      {/* Shared right slot: diff and /btw take turns; header tabs switch. */}
      {(() => {
        const btwShowing = !!state.btwSid || btwOpening;
        const view = rightView === "btw" && btwShowing ? "btw"
          : state.artifact ? "diff" : btwShowing ? "btw" : null;
        if (view === "btw")
          return <BtwPanel sid={state.btwSid ?? undefined} rt={state.btwSid ? state.runtimes[state.btwSid] : undefined}
            engine={state.btwEngine} opening={btwOpening && !state.btwSid}
            active="btw" hasArtifact={!!state.artifact} artifactKind={state.artifact?.kind} onTab={switchRight}
            onSend={sendBtw} onOpenFile={previewFile} onClose={closeBtw}
            onDismissNotice={(noticeId) => {
              if (state.btwSid) dispatch({ type: "dismiss_notice", sid: state.btwSid, noticeId });
            }} />;
        if (view === "diff" && state.artifact)
          return <ArtifactPanel artifact={state.artifact} active="diff" hasBtw={!!state.btwSid}
            onTab={switchRight} onRefresh={previewFile}
            onOpenFile={previewFile} onLoadPreviewAsset={loadPreviewAsset}
            onSaveMarkdown={saveMarkdown} onDirtyChange={setArtifactDirty}
            onClose={() => dispatch({ type: "clear_artifact" })} />;
        return null;
      })()}
      {rt.pendingQuestion && (
        <QuestionSheet
          header={rt.pendingQuestion.header}
          question={rt.pendingQuestion.question}
          options={rt.pendingQuestion.options}
          allowText={rt.pendingQuestion.allow_text}
          secret={rt.pendingQuestion.secret}
          onAnswer={(answer) => {
            wsRef.current?.sendAnswerQuestion(rt.pendingQuestion!.ask_id, answer);
            dispatch({ type: "answer_question" });
          }}
        />
      )}
      <StatusSheet open={shouldOpenCodexStatus(statusOpenSid, focusedSid, focusedEngine)} report={rt.statusReport}
        error={rt.statusError}
        onClose={() => setStatusOpenSid(null)}
        onRefresh={openStatus} />
      <ForkWorktreeSheet open={forkWorktreeSession !== null} session={forkWorktreeSession}
        creating={forkWorktreeCreating} error={forkWorktreeError}
        onConfirm={submitForkWorktree} onClose={closeForkWorktree} />
      <WorkDashboardSheet open={workManagerOpen && space === "work"}
        dashboard={workDashboards[engine] ?? null}
        selectedProjectId={workProjectId}
        onSelectProject={setWorkProjectId}
        onClose={() => setWorkManagerOpen(false)}
        onCreateProject={(name, description) => !!wsRef.current?.sendCreateWorkProject(engine, name, description)}
        onDeleteProject={(projectId) => !!wsRef.current?.sendDeleteWorkProject(engine, projectId)}
        onAddSource={(projectId, kind, title, uri, file) => !!wsRef.current?.sendAddWorkSource(engine, projectId, kind, title, uri, file)}
        onDeleteSource={(sourceId) => !!wsRef.current?.sendDeleteWorkSource(engine, sourceId)}
        onCreateSchedule={(title, prompt, nextRunAt, repeatSeconds, projectId) => !!wsRef.current?.sendCreateWorkSchedule(engine, title, prompt, nextRunAt, repeatSeconds, projectId)}
        onDeleteSchedule={(scheduleId) => !!wsRef.current?.sendDeleteWorkSchedule(engine, scheduleId)}
        onCreatePlugin={(name, instructions, projectId) => !!wsRef.current?.sendCreateWorkPlugin(engine, name, instructions, projectId)}
        onDeletePlugin={(pluginId) => !!wsRef.current?.sendDeleteWorkPlugin(engine, pluginId)} />
      <WorkArtifactsSheet open={workArtifactsOpen && space === "work"
          && !state.newChat && currentWorkArtifacts.length > 0}
        artifacts={currentWorkArtifacts}
        onOpen={(path) => { setWorkArtifactsOpen(false); previewFile(path); }}
        onClose={() => setWorkArtifactsOpen(false)} />
      <CapabilitiesSheet open={capabilitiesOpen}
        report={capabilitiesBySurface[`${space}:${engine}`] ?? null}
        loading={capabilitiesLoading}
        onRefresh={() => {
          setCapabilitiesLoading(true);
          wsRef.current?.sendGetEngineCapabilities(
            engine, space, state.newChat?.cwd ?? state.currentCwd);
        }}
        onManagePlugin={(item, action) => {
          const verb = action === "install" ? "安装" : "卸载";
          if (!window.confirm(`${verb}插件「${item.name}」将修改本机 ${engine === "codex" ? "Codex" : "Claude"} 配置，确定继续吗？`)) return;
          setCapabilitiesLoading(true);
          wsRef.current?.sendManageEnginePlugin(
            engine, space, action, item.id,
            state.newChat?.cwd ?? state.currentCwd);
        }}
        onClose={() => setCapabilitiesOpen(false)} />
    </div>
  );
}

import type {
  CodexProfileInfo,
  Engine,
  SessionInfo,
  SessionList,
  Space,
  State,
} from "./protocol";

export interface NormalizedSessionList {
  sessions: SessionInfo[];
  codexProfiles: CodexProfileInfo[];
  defaultCodexProfileId: string | null;
}

/** Apply one authoritative/partial-success catalog response exactly once.
 * Every consumer (React state, surface caches, RelayWs ownership maps) must use
 * this same projection or A→B→A can resurrect a list that dropped the rows of
 * one temporarily unavailable Codex account. */
export function normalizeSessionList(
  previousSessions: readonly SessionInfo[],
  previousProfiles: readonly CodexProfileInfo[],
  previousDefaultProfileId: string | null,
  event: SessionList,
): NormalizedSessionList {
  if (event.engine !== "codex") {
    return {
      sessions: [...event.sessions],
      codexProfiles: [...previousProfiles],
      defaultCodexProfileId: previousDefaultProfileId,
    };
  }
  const codexProfiles = event.codex_profiles?.length
    ? [...event.codex_profiles]
    : [...previousProfiles];
  const configuredProfileIds = new Set(
    codexProfiles.map((profile) => profile.id),
  );
  const unavailableProfileIds = new Set(
    codexProfiles
      .filter((profile) => !!profile.error)
      .map((profile) => profile.id),
  );
  const incomingIds = new Set(
    event.sessions.map((session) => session.session_id),
  );
  const listedSpace = event.space ?? "code";
  const retained = unavailableProfileIds.size === 0
    ? []
    : previousSessions.filter((session) =>
      !session.provisional_fork
      && session.engine === "codex"
      && (session.space ?? "code") === listedSpace
      && !!session.codex_profile_id
      && configuredProfileIds.has(session.codex_profile_id)
      && unavailableProfileIds.has(session.codex_profile_id)
      && !incomingIds.has(session.session_id));
  return {
    sessions: [...event.sessions, ...retained],
    codexProfiles,
    defaultCodexProfileId: event.default_codex_profile_id
      ?? previousDefaultProfileId,
  };
}

export function shouldAcceptSessionList(
  activeEngine: "claude" | "codex",
  activeSpace: Space,
  event: SessionList,
): boolean {
  return event.engine === activeEngine && (event.space ?? "code") === activeSpace;
}

export function updateScopedSessionLifecycle(
  catalog: Readonly<Record<string, SessionInfo[]>>,
  engine: Engine,
  space: Space,
  sid: string,
  state: State,
): Record<string, SessionInfo[]> {
  const key = `${space}:${engine}`;
  const listed = catalog[key];
  if (!listed) return catalog as Record<string, SessionInfo[]>;
  let changed = false;
  const updated = listed.map((session) => {
    if (session.session_id !== sid || session.state === state) return session;
    changed = true;
    return { ...session, state };
  });
  return changed ? { ...catalog, [key]: updated }
    : catalog as Record<string, SessionInfo[]>;
}

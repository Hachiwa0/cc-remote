import type { Engine, SessionInfo, SessionList, Space, State } from "./protocol";

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

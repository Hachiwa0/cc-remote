export interface TextSelectionGuard {
  sid: string;
  revision: string | null;
  viewId: string;
  scopeKey: string | null;
  turnIds: [string, string];
}

export function protectedHistoryTurnIds(
  anchorTurnId: string | null | undefined,
  selection: TextSelectionGuard | null,
  expected: {
    sid: string;
    revision: string;
    viewId: string;
    scopeKey: string;
  },
): string[] | undefined {
  const ids = new Set<string>();
  if (anchorTurnId) ids.add(anchorTurnId);
  if (selection
      && selection.sid === expected.sid
      && selection.revision === expected.revision
      && selection.viewId === expected.viewId
      && selection.scopeKey === expected.scopeKey) {
    ids.add(selection.turnIds[0]);
    ids.add(selection.turnIds[1]);
  }
  return ids.size > 0 ? [...ids] : undefined;
}

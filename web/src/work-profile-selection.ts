import type { CodexProfileInfo, Engine, Space } from "./protocol";

export function newWorkProfileForSidebarFilter(
  engine: Engine,
  space: Space,
  profileFilter: string,
): string | undefined {
  return engine === "codex" && space === "work" && profileFilter !== "all"
    ? profileFilter
    : undefined;
}

export function resolveWorkScheduleProfile(
  profiles: CodexProfileInfo[],
  selectedProfileId: string | null,
  preferredProfileId: string | null,
): { profileId: string | null; missing: boolean } {
  const profileId = selectedProfileId ?? preferredProfileId;
  return {
    profileId,
    missing: !!profileId
      && !profiles.some((profile) => profile.id === profileId),
  };
}

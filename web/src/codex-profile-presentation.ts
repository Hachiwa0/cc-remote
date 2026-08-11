import type { CodexProfileInfo } from "./protocol";

const SECONDARY_NAMES = [
  "nyx",
  "iris",
  "echo",
  "gaia",
  "metis",
  "themis",
  "hestia",
  "athena",
  "hermes",
  "atlas",
  "orpheus",
  "asteria",
] as const;

export interface CodexProfilePresentation {
  name: string;
  label: string;
  fullLabel: string;
  tone: number;
}

export function codexProfilePresentation(
  profiles: readonly CodexProfileInfo[],
  defaultProfileId: string | null | undefined,
  profileId: string | null | undefined,
): CodexProfilePresentation | null {
  if (profiles.length <= 1 || !profileId) return null;
  const profile = profiles.find((candidate) => candidate.id === profileId);
  if (!profile) return null;
  const isDefault = profile.id === defaultProfileId;
  const secondaryIndex = profiles
    .filter((candidate) => candidate.id !== defaultProfileId)
    .findIndex((candidate) => candidate.id === profile.id);
  const name = isDefault
    ? "default"
    : SECONDARY_NAMES[secondaryIndex] ?? "more";
  return {
    name,
    label: profile.label,
    fullLabel: profile.label.toLowerCase() === name
      ? name
      : `${name} · ${profile.label}`,
    tone: isDefault ? 0 : (secondaryIndex % 7) + 1,
  };
}

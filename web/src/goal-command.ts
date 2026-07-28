import type { Engine } from "./protocol";

export type GoalCommand =
  | { kind: "show" }
  | { kind: "clear" }
  | { kind: "resume" }
  | { kind: "set"; objective: string };

/** Parse the shared Claude/Codex /goal argument contract. */
export function parseGoalCommand(args: string, engine: Engine): GoalCommand {
  const value = args.trim();
  if (!value) return { kind: "show" };
  if (value.toLowerCase() === "clear") return { kind: "clear" };
  if (engine === "codex" && value.toLowerCase() === "resume") {
    return { kind: "resume" };
  }
  return { kind: "set", objective: value };
}

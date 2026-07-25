import type { Engine, SessionInfo, Space } from "./protocol";
import type { NotificationRoute } from "./notification-route";

export type NotificationDeviceState = "idle" | "loading" | "ready" | "error";

export interface NotificationOrigin {
  machineId: string;
  engine: Engine;
  space: Space;
  sid: string | null;
}

interface ResolveNotificationNavigationOptions {
  target: NotificationRoute;
  origin: NotificationOrigin | null;
  deviceState: NotificationDeviceState;
  authorizedMachineIds: readonly string[];
  machineId: string;
  engine: Engine;
  space: Space;
  authoritativeSessions: readonly SessionInfo[] | null;
}

type NotificationFailureReason =
  | "devices_unavailable"
  | "device_missing"
  | "session_missing";

export type NotificationNavigationDecision =
  | { kind: "wait" }
  | {
      kind: "fail";
      reason: NotificationFailureReason;
      restore: NotificationOrigin | null;
    }
  | { kind: "switch_machine"; machineId: string }
  | { kind: "request_list"; engine: Engine; space: Space }
  | {
      kind: "switch_surface";
      engine: Engine;
      space: Space;
      session: SessionInfo;
    }
  | { kind: "focus"; session: SessionInfo };

function restoreOrigin(
  origin: NotificationOrigin | null,
  machineId: string,
): NotificationOrigin | null {
  return origin && origin.machineId !== machineId ? origin : null;
}

/**
 * Resolve a notification click without speculatively changing the visible
 * surface. The exact session must exist in a fresh list before any focus or
 * engine/space switch is allowed.
 */
export function resolveNotificationNavigation({
  target,
  origin,
  deviceState,
  authorizedMachineIds,
  machineId,
  engine,
  space,
  authoritativeSessions,
}: ResolveNotificationNavigationOptions): NotificationNavigationDecision {
  if (deviceState === "idle" || deviceState === "loading") {
    return { kind: "wait" };
  }
  if (deviceState === "error") {
    return {
      kind: "fail",
      reason: "devices_unavailable",
      restore: restoreOrigin(origin, machineId),
    };
  }
  if (!authorizedMachineIds.includes(target.machine_id)) {
    return {
      kind: "fail",
      reason: "device_missing",
      restore: restoreOrigin(origin, machineId),
    };
  }
  if (machineId !== target.machine_id) {
    return { kind: "switch_machine", machineId: target.machine_id };
  }
  if (authoritativeSessions === null) {
    return {
      kind: "request_list",
      engine: target.engine,
      space: target.space,
    };
  }
  const listed = authoritativeSessions.find(
    (session) => session.session_id === target.session_id,
  );
  if (!listed) {
    return {
      kind: "fail",
      reason: "session_missing",
      restore: restoreOrigin(origin, machineId),
    };
  }
  const session: SessionInfo = {
    ...listed,
    engine: target.engine,
    space: target.space,
  };
  if (engine !== target.engine || space !== target.space) {
    return {
      kind: "switch_surface",
      engine: target.engine,
      space: target.space,
      session,
    };
  }
  return { kind: "focus", session };
}

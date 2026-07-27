export type NotificationMode = "off" | "generic" | "session";
export type RemoteNotificationMode = Exclude<NotificationMode, "off">;

export const NOTIFICATION_MODE_KEY = "cc_remote_notification_mode";
export const LEGACY_NOTIFICATION_KEY = "cc_remote_notifications";

export interface NotificationStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export function readNotificationMode(
  storage: NotificationStorage,
): NotificationMode {
  const stored = storage.getItem(NOTIFICATION_MODE_KEY);
  if (stored === "off" || stored === "generic" || stored === "session") {
    return stored;
  }
  if (storage.getItem(LEGACY_NOTIFICATION_KEY) === "1") {
    // Preserve the old privacy contract. An upgrade must never start exposing
    // session titles on a lock screen merely because reminders were enabled.
    storage.setItem(NOTIFICATION_MODE_KEY, "generic");
    return "generic";
  }
  return "off";
}

export function writeNotificationMode(
  storage: NotificationStorage,
  mode: NotificationMode,
): void {
  storage.setItem(NOTIFICATION_MODE_KEY, mode);
  if (mode === "off") storage.removeItem(LEGACY_NOTIFICATION_KEY);
  else storage.setItem(LEGACY_NOTIFICATION_KEY, "1");
}

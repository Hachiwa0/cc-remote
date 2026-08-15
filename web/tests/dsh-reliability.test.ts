import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createServer } from "vite";

import { clientSlashesFor, commandsFor } from "../src/data.ts";
import { resolvedEngineOptions } from "../src/engine-picker-options.ts";
import { parseNotificationFragment } from "../src/notification-route.ts";
import type { AppState } from "../src/reducer.ts";
import {
  ENGINE_SPACES_KEY,
  readEngineSpaces,
  siblingSpaceForPrefetch,
} from "../src/surface-preferences.ts";
import { RelayWs } from "../src/ws.ts";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol.ts";

const engineOptions = resolvedEngineOptions([
  { id: "claude", display_name: "Claude Code", available: true,
    spaces: ["code", "work"] },
  { id: "codex", display_name: "Codex", available: true,
    spaces: ["code", "work"] },
]);
assert.deepEqual(engineOptions.map((option) => option.id),
  ["claude", "codex", "dsh"],
  "the picker must expose all three backends instead of hiding DSH discovery");
assert.equal(engineOptions[2]?.available, false);
assert.match(engineOptions[2]?.reason ?? "", /DSH/,
  "an undiscovered DSH backend must remain visible with a safe disabled reason");

const preferenceStorage = {
  getItem: (key: string) => key === ENGINE_SPACES_KEY
    ? JSON.stringify({ claude: "work", codex: "code", dsh: "work" })
    : null,
};
assert.equal(readEngineSpaces(preferenceStorage, "dsh").dsh, "code",
  "DSH must ignore a persisted or poisoned Work surface");
assert.equal(siblingSpaceForPrefetch("dsh", "code"), null,
  "a DSH Code catalog must not prefetch its unsupported Work sibling");
assert.equal(siblingSpaceForPrefetch("dsh", "work"), null,
  "a poisoned DSH Work response must not trigger another surface request");
assert.equal(siblingSpaceForPrefetch("codex", "code"), "work");
assert.equal(siblingSpaceForPrefetch("claude", "work"), "code");

const dshSlashes = commandsFor("dsh", "work")
  .filter((command) => "slash" in command)
  .map((command) => command.slash);
for (const unsupported of ["goal", "btw", "context", "status", "permissions"]) {
  assert.equal(dshSlashes.includes(unsupported), false,
    `DSH must not expose /${unsupported}, even under a poisoned Work surface`);
}
assert.equal(clientSlashesFor("dsh").has("compact"), false,
  "DSH native /compact must pass through instead of becoming a Remote control");
assert.equal(clientSlashesFor("dsh").has("skills"), true,
  "DSH Skills remain a local read-only catalog action");

const notification = {
  machine_id: "nono",
  engine: "dsh",
  space: "code",
  session_id: "dsh@native-session",
};
assert.deepEqual(parseNotificationFragment(
  `#notification=${encodeURIComponent(JSON.stringify(notification))}`),
notification, "DSH notifications may navigate only to Code");
assert.equal(parseNotificationFragment(
  `#notification=${encodeURIComponent(JSON.stringify({
    ...notification,
    space: "work",
  }))}`), null, "DSH notification routes must reject Work");

class FakeWebSocket {
  static readonly OPEN = 1;
  readonly sent: string[] = [];
  readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  send(raw: string): void {
    this.sent.push(raw);
  }

  close(): void {
    this.readyState = 3;
  }
}

Object.assign(globalThis, {
  window: { location: { protocol: "http:", host: "relay.test" } },
  WebSocket: FakeWebSocket,
  sessionStorage: {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  },
});
const relay = new RelayWs({ onEvent: () => {}, onConnState: () => {} });
relay.start();
const socket = (relay as unknown as { ws: FakeWebSocket }).ws;
socket.onopen?.();
relay.sendNewSession(
  "/tmp/project", "dsh", "dsh://deepseek-official/deepseek-v4-flash", "high",
  { prompt: "inspect", msg_id: "dsh-first-message" },
  undefined, undefined, undefined, undefined, undefined,
  "code", null, undefined, "coding-agent",
);
const frame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(frame.type, "new_session");
assert.equal(frame.engine, "dsh");
assert.equal(frame.dsh_agent_preset, "coding-agent",
  "the selected DSH Agent Preset must travel with the atomic create request");
assert.equal("project_id" in frame, false,
  "DSH creation must remain outside the Work API");

relay.setSurface("dsh", "code");
relay.setFocusedSid("dsh@native-session", "dsh", "code");
relay.sendSetEffort("off");
const dshEffort = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(dshEffort.engine, "dsh",
  "opaque DSH effort ids must carry an explicit engine scope");

relay.setSurface("codex", "code");
relay.setFocusedSid("codex-session", "codex", "code");
relay.sendSetEffort("high");
const codexEffort = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal("engine" in codexEffort, false,
  "standard engine effort commands retain the closed legacy envelope");

const dshSid = "dsh@image-session";
const imageRef = {
  image_id: "dsh-img-live",
  media_type: "image/png" as const,
  width: 16,
  height: 9,
  byte_size: 144,
};
const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { createRuntime, initialState, reduce } =
    await reducerHarness.ssrLoadModule("/src/reducer.ts");
  let dshState: AppState = {
    ...initialState,
    focusedSid: dshSid,
    runtimes: {
      [dshSid]: { ...createRuntime(), state: "running" as const },
    },
  };
  dshState = reduce(dshState, {
    type: "steer_sent",
    sid: dshSid,
    prompt: "look at this",
    msg_id: "remote-steer",
    ts: 1_000,
  });
  dshState = reduce(dshState, {
    type: "event",
    event: {
      v: PROTOCOL_VERSION,
      type: "turn_steered",
      sid: dshSid,
      ts: 2,
      msg_id: "dsh-msg-42",
      client_msg_id: "remote-steer",
      turn_id: "dsh-turn-7",
      prompt: "look at this",
      image_refs: [imageRef],
    } as ServerEvent,
  });
  const steeredImageTurn = dshState.runtimes[dshSid].turns[0];
  assert.equal(steeredImageTurn.id, "remote-steer",
    "the live DSH native id must bind into the optimistic steer row");
  assert.equal(steeredImageTurn.historyTurnId, "dsh-msg-42");
  assert.deepEqual(steeredImageTurn.imageRefs, [imageRef],
    "another browser can render DSH images without replaying base64 bodies");
  assert.equal(dshState.runtimes[dshSid].acceptancePending, null);

  const terminalSid = "dsh@terminal-owner";
  let terminalState: AppState = {
    ...initialState,
    focusedSid: terminalSid,
    runtimes: {
      [terminalSid]: {
        ...createRuntime(),
        state: "running" as const,
        turns: [{
          id: "dsh-event-0", prompt: "", blocks: [], done: false,
        }, {
          id: "dsh-msg-7", prompt: "hello", blocks: [], done: false,
        }],
      },
    },
  };
  terminalState = reduce(terminalState, {
    type: "event",
    event: {
      v: PROTOCOL_VERSION,
      type: "turn_end",
      sid: terminalSid,
      ts: 3,
      turn_id: "dsh-seq-436",
      presentation_id: "dsh-msg-7",
      result: { subtype: "success", duration_ms: 5_658, is_error: false },
    } as ServerEvent,
  });
  const terminalTurns = terminalState.runtimes[terminalSid].turns;
  assert.equal(terminalTurns[0].done, false,
    "a DSH terminal must not close an unrelated process-only row");
  assert.equal(terminalTurns[1].done, true,
    "a DSH terminal must close its exact visible conversation row");
  assert.equal(terminalTurns[1].forkPointId, "dsh-seq-436",
    "the visible owner keeps DSH's independent native fork point");

  const previousSid = "codex-previous-surface";
  let bannerState: AppState = {
    ...initialState,
    focusedSid: previousSid,
    runtimes: {
      [previousSid]: createRuntime(),
      [dshSid]: createRuntime(),
    },
  };
  bannerState = reduce(bannerState, {
    type: "event",
    event: {
      v: PROTOCOL_VERSION,
      type: "error",
      sid: previousSid,
      code: "auth",
      message: "stale command from the previous surface",
      ts: 4,
    } as ServerEvent,
  });
  assert.equal(bannerState.banner, "当前操作不适用于这个会话。");
  assert.equal(bannerState.bannerKind, "command");
  bannerState = reduce(bannerState, {
    type: "restore_session_list",
    sessions: [],
  });
  assert.equal(bannerState.banner, undefined,
    "a command warning must not follow an engine switch into DSH");
  bannerState = { ...bannerState, focusedSid: dshSid };
  bannerState = reduce(bannerState, {
    type: "event",
    event: {
      v: PROTOCOL_VERSION,
      type: "error",
      sid: previousSid,
      code: "auth",
      message: "late previous-surface response",
      ts: 5,
    } as ServerEvent,
  });
  assert.equal(bannerState.banner, undefined,
    "a delayed background error must not overwrite the focused DSH view");
  bannerState = reduce(bannerState, {
    type: "conn",
    connState: "reconnecting",
  });
  bannerState = reduce(bannerState, {
    type: "restore_session_list",
    sessions: [],
  });
  assert.equal(bannerState.banner, "正在重新连接…",
    "surface navigation must preserve machine-wide connectivity state");
  assert.equal(bannerState.bannerKind, "connection");
} finally {
  await reducerHarness.close();
}

const appSource = readFileSync(
  resolve(process.cwd(), "src/App.tsx"), "utf8");
const sidebarSource = readFileSync(
  resolve(process.cwd(), "src/components/SessionsSidebar.tsx"), "utf8");
const cssSource = readFileSync(
  resolve(process.cwd(), "src/index.css"), "utf8");
assert.match(sidebarSource,
  /engine === "dsh"[\s\S]*?<button[^>]*disabled[\s\S]*?<Icon name="work"[\s\S]*?<Icon name="lock"[\s\S]*?<Icon name="code"/,
  "DSH must keep Work on the left and Code on the right while visibly locking Work");
assert.match(sidebarSource, /aria-label="Work（DSH 暂不支持）"/,
  "the locked DSH Work tab must explain its unavailable state");
assert.match(cssSource, /:root\[data-engine="dsh"\]\[data-theme="light"\]\s*\{/,
  "DSH must own a light theme instead of inheriting Claude");
assert.match(cssSource, /:root\[data-engine="dsh"\]\[data-theme="dark"\]\s*\{/,
  "DSH must own a dark theme instead of inheriting Claude");
assert.match(cssSource, /\.space-switch\{[^}]*grid-template-columns:1fr 1fr/s,
  "the locked Work tab must retain equal width at narrow sidebar sizes");
assert.doesNotMatch(
  appSource,
  /if \(!current \|\| current\.available\) return;[\s\S]{0,300}switchEngine\(fallback\.id\)/,
  "a temporarily unavailable DSH catalog must not overwrite the saved surface",
);
assert.match(
  appSource,
  /!authed \|\| !focusedSid \|\| state\.newChat[\s\S]{0,100}focusedEngine === "dsh"[\s\S]{0,220}sendGetContext\(\)/,
  "focusing DSH must not request an unsupported context-usage API",
);
relay.stop();

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type TouchEvent,
  type WheelEvent,
} from "react";
import {
  defaultRangeExtractor,
  useVirtualizer,
} from "@tanstack/react-virtual";
import type { Turn } from "../domain/conversation";
import type { Space } from "../protocol";
import { MessageBlock } from "./MessageBlock";
import { Icon, ClaudeMark, ClaudeWorking, ClaudeSpark } from "../icons";
import { canForkTurn } from "../session-worktree";
import { ProcessTimeline } from "./ProcessTimeline";
import { finalTextBlocks, hasActiveProcess } from "../process-blocks";
import { isMarkdownPath } from "../preview-path";
import { collectTurnFileChanges } from "../file-changes";
import type { InlineImageAsset } from "../inline-image-assets";
import {
  historyImageAssetKey,
  type HistoryImageAsset,
  type HistoryImageVariant,
} from "../history-image-assets";
import { ImageLightbox } from "./ImageLightbox";
import { presentHistoricalTurnProblem } from "../problem-presentation";
import { queryImageDimensions } from "../img";
import {
  updateTurnKeySnapshot,
  type TurnKeySnapshot,
} from "../virtual-turn-keys";
import { TurnImagePreviewCache } from "../turn-image-previews";
import type { TextSelectionGuard } from "../history-selection-guard";
import { HistoryUserImage } from "./HistoryUserImage";
import {
  HistoryAnchorController,
  historyPageStatus,
  isAtHistoryEdge,
  isAtLatestEdge,
  measureBottom,
  OlderHistoryLoadGate,
  shouldAutoLoadOlderHistory,
  shouldAutoLoadNewerHistory,
  ScrollFollowController,
  type HistoryAnchorPoint,
  type HistoryPageDirection,
  type ScrollFollowSnapshot,
  type ScrollMetrics,
} from "../scroll-follow";
import {
  ScrollCoordinator,
  type ScrollCommand,
} from "../scroll-coordinator";
import {
  HISTORY_DETAIL_REQUEST_TIMEOUT_MS,
  HISTORY_REQUEST_TIMEOUT_MS,
} from "../history-requests";

const WHEEL_GESTURE_IDLE_MS = 180;
const HISTORY_VIRTUAL_ESTIMATE_PX = 280;
const HISTORY_VIRTUAL_OVERSCAN = 6;
const HISTORY_TURN_GAP_PX = 22;
const HISTORY_LOAD_HEADER_PX = 52;
const USER_SCROLL_INTENT_IDLE_MS = 260;
const HISTORY_ANCHOR_SETTLE_MAX_MS = 2_000;
// HistoryRequestCoordinator allows replacement after 15 seconds. Release the
// local anchor just after that boundary so an unanswered command cannot lock
// pagination forever.
const HISTORY_PAGE_REQUEST_TIMEOUT_MS = HISTORY_REQUEST_TIMEOUT_MS + 1_000;
// Keep the exact process edge until all synchronous and shortly-delayed
// Markdown/image measurements settle, but never pin a huge virtual row
// indefinitely after a successful detail response.
const DETAIL_ANCHOR_QUIET_MS = 300;
const DETAIL_ANCHOR_MAX_SETTLE_MS = 2_000;

type UserScrollDirection = "history" | "latest" | "unknown";

interface CapturedHistoryBoundary extends HistoryAnchorPoint {
  anchorOffset: number;
}

interface TouchAppliedBoundary {
  generation: number;
  appliedEventTimestamp: number;
  baselineY: number;
  movedAfterApply: boolean;
}

interface RetainedMeasurementBoundary {
  sid: string | null;
  revision: string | null;
  viewId: string;
  turnId: string;
  anchorOffset: number;
}

interface TextSelectionRetention {
  scope: string;
  anchorTurnId: string;
  focusTurnId: string;
  pointerId: number;
  interactionToken: number | null;
  dragging: boolean;
  releaseAnchorTurnId: string | null;
  releaseAnchorOffset: number | null;
}

interface TextSelectionCandidate {
  scope: string;
  pointerId: number;
}

const TEXT_SELECTION_EXCLUDED_SELECTOR = [
  "button",
  "a",
  "input",
  "textarea",
  "select",
  "option",
  "summary",
  "[contenteditable='true']",
  "[role='button']",
  ".mermaid-block",
].join(",");

function selectionTurnId(
  root: HTMLElement | null,
  node: Node | null,
): string | null {
  if (!root || !node || !root.contains(node)) return null;
  const element = node instanceof Element ? node : node.parentElement;
  return element?.closest<HTMLElement>("[data-turn-id]")?.dataset.turnId ?? null;
}

type DetailPageDirection = "initial" | "older" | "newer";
type DetailAnchorEdge = "start" | "end";

interface DetailAnchorTransaction {
  scope: string;
  turnId: string;
  edge: DetailAnchorEdge;
  anchorOffset: number;
  token: number;
  initialFingerprint: string;
  sawLoading: boolean;
  responseSettled: boolean;
  requestTimer: number | null;
  quietTimer: number | null;
  maxSettleTimer: number | null;
  firstFrame: number | null;
  secondFrame: number | null;
  observer: ResizeObserver | null;
  observedNode: HTMLElement | null;
}

interface HistoryPageLoadAcceptance {
  accepted: true;
  /** The first runtime page creates its browse view synchronously in App. */
  viewId?: string;
}

type HistoryPageLoadResult = boolean | HistoryPageLoadAcceptance;

function readScrollMetrics(el: HTMLDivElement): ScrollMetrics {
  return {
    scrollHeight: el.scrollHeight,
    scrollTop: el.scrollTop,
    clientHeight: el.clientHeight,
  };
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function detailTurnFingerprint(turn: Turn): string {
  return [
    turn.detailLoaded ? "1" : "0",
    turn.detailOldestCursor ?? "",
    turn.detailNewerCursor ?? "",
    turn.detailHasMore ? "1" : "0",
    turn.detailHasNewer ? "1" : "0",
    turn.detailAutoLoad ? "1" : "0",
    turn.detailProjection?.segments.length ?? 0,
    turn.detailProjection?.blocks.length ?? 0,
    turn.blocks.length,
  ].join("\u0000");
}

export function ChatView({ sid, turns, engine = "claude", loading, hasMore,
  historyRevision = null, historyViewRevision = historyRevision,
  historyViewId = null, historyScopeKey = null,
  historyWindowEpoch = 0, historyCursor = null,
  browseMode = false, hasNewer = false,
  onLoadMore, onLoadNewer, onReturnLatest,
  onLoadDetail, onEdit, onGetDiff, onOpenTurnDiff, onPreviewMarkdown, onOpenFile,
  onOpenArtifacts, onFork, forkingPointId, imageAssets, onLoadImage,
  historyImageAssets, onLoadHistoryImage,
  onTextSelectionGuardChange,
  surface = "code" }: {
  sid: string | null;
  turns: Turn[];
  surface?: Space;
  engine?: "claude" | "codex";
  loading?: boolean;
  hasMore?: boolean;
  historyRevision?: string | null;
  // A non-destructive replay recovery keeps the prior view scope across the
  // atomic History swap so the virtualizer preserves the current reading row.
  // Pagination/detail still use the authoritative historyRevision above.
  historyViewRevision?: string | null;
  historyViewId?: string | null;
  historyScopeKey?: string | null;
  historyWindowEpoch?: number;
  historyCursor?: string | null;
  browseMode?: boolean;
  hasNewer?: boolean;
  onLoadMore?: (anchorTurnId?: string) => HistoryPageLoadResult;
  onLoadNewer?: (anchorTurnId?: string) => HistoryPageLoadResult;
  onReturnLatest?: () => void;
  onLoadDetail?: (turnId: string, before?: string | null) => boolean;
  onEdit?: (prompt: string) => void;
  onGetDiff?: (file: string) => void;
  onOpenTurnDiff?: (files: string[], diff: string) => void;
  onPreviewMarkdown?: (file: string) => void;
  onOpenFile?: (file: string, line?: number) => void;
  onOpenArtifacts?: () => void;
  onFork?: (forkPointId: string) => void;
  forkingPointId?: string | null;
  imageAssets?: Record<string, InlineImageAsset>;
  onLoadImage?: (path: string) => boolean;
  historyImageAssets?: Record<string, HistoryImageAsset>;
  onLoadHistoryImage?: (
    turnId: string, imageId: string, variant: HistoryImageVariant,
  ) => boolean;
  onTextSelectionGuardChange?: (guard: TextSelectionGuard | null) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<ScrollFollowController | null>(null);
  if (!controllerRef.current) controllerRef.current = new ScrollFollowController();
  const scrollCoordinatorRef = useRef(new ScrollCoordinator());
  const [scrollPolicyEpoch, setScrollPolicyEpoch] = useState(0);
  const [scrollState, setScrollState] = useState<ScrollFollowSnapshot>(() =>
    controllerRef.current!.snapshot());
  const [zoom, setZoom] = useState<
    | { kind: "data"; src: string; alt: string }
    | { kind: "history"; turnId: string; imageId: string; alt: string }
    | null
  >(null);
  const historyAnchorRef = useRef(new HistoryAnchorController());
  const historyRequestRef = useRef<{
    sid: string | null;
    revision: string | null;
    viewId: string | null;
    direction: HistoryPageDirection;
    before: string | null;
    windowEpoch: number;
  } | null>(null);
  const turnNodeRefs = useRef(new Map<string, HTMLDivElement>());
  const textSelectionCandidateRef = useRef<TextSelectionCandidate | null>(null);
  const textSelectionRef = useRef<TextSelectionRetention | null>(null);
  const [textSelection, setTextSelection] =
    useState<TextSelectionRetention | null>(null);
  const detailAnchorRef = useRef<DetailAnchorTransaction | null>(null);
  const cancelDetailAnchorFnRef = useRef<
    ((releaseInteraction?: boolean) => void) | null
  >(null);
  const historyReleaseFrameRef = useRef<number | null>(null);
  const historyReleaseStartedAtRef = useRef<{
    generation: number;
    startedAt: number;
  } | null>(null);
  const historyRequestTimeoutRef = useRef<{
    generation: number | null;
    timer: number;
  } | null>(null);
  const historyLoadGateRef = useRef(new OlderHistoryLoadGate());
  const wheelHistoryLoadGateRef = useRef(new OlderHistoryLoadGate());
  const wheelGestureTimerRef = useRef<number | null>(null);
  const wheelGestureActiveRef = useRef(false);
  const autoLoadedBoundaryRef = useRef<string | null>(null);
  const lastScrollTopRef = useRef(0);
  const renderedScrollScopeRef = useRef<string | null>(null);
  const touchYRef = useRef<number | null>(null);
  const touchEventClockOffsetRef = useRef<number | null>(null);
  const touchAppliedBoundaryRef = useRef<TouchAppliedBoundary | null>(null);
  const touchHistoryGenerationRef = useRef<number | null>(null);
  const userScrollIntentRef = useRef(false);
  const userScrollDirectionRef = useRef<UserScrollDirection | null>(null);
  const userScrollIntentTimerRef = useRef<number | null>(null);
  const turnKeySnapshotRef = useRef<TurnKeySnapshot | null>(null);
  const turnImagePreviewCacheRef = useRef(new TurnImagePreviewCache());
  // `historyViewRevision` is kept as a compatibility scope for the
  // non-destructive recovery path. Deep-history browsing supplies an explicit
  // stable view id: revision/view changes reset, window paging does not.
  const resolvedHistoryViewId = historyViewId ?? historyViewRevision ?? "";
  turnImagePreviewCacheRef.current.update(sid, turns);
  const scrollScope = historyViewId == null
    ? `${historyScopeKey ?? ""}\u0000${sid ?? ""}\u0000${resolvedHistoryViewId}`
    : `${historyScopeKey ?? ""}\u0000${sid ?? ""}\u0000${historyRevision ?? ""}\u0000${resolvedHistoryViewId}`;
  const turnKeySnapshot = updateTurnKeySnapshot(
    turnKeySnapshotRef.current,
    turns,
    scrollScope,
  );
  turnKeySnapshotRef.current = turnKeySnapshot;
  const [activeHistoryGeneration, setActiveHistoryGeneration] = useState<number | null>(null);
  const measurementBoundaryRef =
    useRef<RetainedMeasurementBoundary | null>(null);
  const [measurementBoundary, setMeasurementBoundaryState] =
    useState<RetainedMeasurementBoundary | null>(null);
  const setMeasurementBoundary = (
    boundary: RetainedMeasurementBoundary | null,
  ) => {
    measurementBoundaryRef.current = boundary;
    setMeasurementBoundaryState(boundary);
  };
  const [processDisclosureOpen, setProcessDisclosureOpen] = useState<
    Record<string, boolean>
  >({});
  const rememberProcessDisclosure = useCallback((key: string, open: boolean) => {
    setProcessDisclosureOpen((current) => {
      const next = { ...current, [key]: open };
      const keys = Object.keys(next);
      if (keys.length > 512) delete next[keys[0]];
      return next;
    });
  }, []);
  const canLoadOlder = !!hasMore;
  const canLoadNewer = browseMode && !!hasNewer;
  const historyInsetRef = useRef({
    scope: scrollScope,
    enabled: canLoadOlder,
  });
  if (historyInsetRef.current.scope !== scrollScope) {
    historyInsetRef.current = { scope: scrollScope, enabled: canLoadOlder };
  } else if (canLoadOlder) {
    historyInsetRef.current.enabled = true;
  }
  const historyTopInset = historyInsetRef.current.enabled
    ? HISTORY_LOAD_HEADER_PX : 0;
  const historyBottomInsetRef = useRef({
    scope: scrollScope,
    enabled: canLoadNewer,
  });
  if (historyBottomInsetRef.current.scope !== scrollScope) {
    historyBottomInsetRef.current = { scope: scrollScope, enabled: canLoadNewer };
  } else if (canLoadNewer) {
    historyBottomInsetRef.current.enabled = true;
  }
  const historyBottomInset = historyBottomInsetRef.current.enabled
    ? HISTORY_LOAD_HEADER_PX + 8 : 8;
  const activeHistoryAnchor = historyAnchorRef.current.current();
  const keyedPrependActive = activeHistoryGeneration !== null
    && activeHistoryAnchor?.generation === activeHistoryGeneration
    && activeHistoryAnchor.sid === sid
    && activeHistoryAnchor.revision === historyRevision
    && (activeHistoryAnchor.viewId ?? null) === resolvedHistoryViewId;
  const keyedPrependResponseReady = keyedPrependActive
    && historyPageStatus(activeHistoryAnchor, {
      sid, revision: historyRevision, cursor: historyCursor,
      viewId: resolvedHistoryViewId, hasMore: !!hasMore,
      windowEpoch: historyWindowEpoch, hasNewer: canLoadNewer,
    }) === "complete";
  const scopedMeasurementBoundary = measurementBoundary?.sid === sid
      && measurementBoundary.revision === historyRevision
      && measurementBoundary.viewId === resolvedHistoryViewId
    ? measurementBoundary : null;
  const activeTextSelection = textSelection?.scope === scrollScope
    ? textSelection : null;
  const retainedSelectionBoundary =
    activeTextSelection?.releaseAnchorTurnId != null
    && activeTextSelection.releaseAnchorOffset != null
      ? {
        sid,
        revision: historyRevision,
        viewId: resolvedHistoryViewId,
        turnId: activeTextSelection.releaseAnchorTurnId,
        anchorOffset: activeTextSelection.releaseAnchorOffset,
      }
      : null;
  const retainedMeasurementBoundary = retainedSelectionBoundary
    ?? scopedMeasurementBoundary
    ?? (keyedPrependActive && activeHistoryAnchor ? {
      sid,
      revision: historyRevision,
      viewId: resolvedHistoryViewId,
      turnId: activeHistoryAnchor.anchorTurnId,
      anchorOffset: activeHistoryAnchor.anchorOffset,
    } : null);
  const activeDetailAnchor = detailAnchorRef.current?.scope === scrollScope
    ? detailAnchorRef.current : null;
  // Read the epoch so pointer interaction changes synchronously reconfigure
  // the virtualizer even when no other chat state changed.
  void scrollPolicyEpoch;
  const virtualScrollPolicy = scrollCoordinatorRef.current.policy(
    scrollState.followOutput,
  );
  // A history transaction already owns an exact keyed reading boundary. Let
  // that single coordinator restore the row instead of also asking TanStack
  // to capture an end anchor for the same prepend. On iOS the latter is
  // deferred until momentum settles, by which time the local transaction may
  // already have released and the two corrections can replay out of order.
  const virtualAnchorTo = keyedPrependActive
    ? "start" : virtualScrollPolicy.anchorTo;
  const virtualizer = useVirtualizer({
    count: turns.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => HISTORY_VIRTUAL_ESTIMATE_PX,
    getItemKey: turnKeySnapshot.getItemKey,
    // TanStack owns every viewport write. The coordinator only selects policy
    // and serializes explicit bottom requests with interactive pointer locks.
    anchorTo: virtualAnchorTo,
    followOnAppend: virtualScrollPolicy.followOnAppend,
    scrollEndThreshold: 80,
    overscan: HISTORY_VIRTUAL_OVERSCAN,
    rangeExtractor: (range) => {
      const indexes = defaultRangeExtractor(range);
      const retainedIndexes = new Set(indexes);
      const historyBoundaryIndex = retainedMeasurementBoundary
        ? turns.findIndex((turn) => turn.id === retainedMeasurementBoundary.turnId)
        : -1;
      const detailBoundaryIndex = activeDetailAnchor
        ? turns.findIndex((turn) => turn.id === activeDetailAnchor.turnId)
        : -1;
      if (historyBoundaryIndex >= 0) retainedIndexes.add(historyBoundaryIndex);
      if (detailBoundaryIndex >= 0) retainedIndexes.add(detailBoundaryIndex);
      if (activeTextSelection) {
        const anchorIndex = turns.findIndex(
          (turn) => turn.id === activeTextSelection.anchorTurnId,
        );
        const focusIndex = turns.findIndex(
          (turn) => turn.id === activeTextSelection.focusTurnId,
        );
        if (anchorIndex >= 0 && focusIndex >= 0) {
          const first = Math.min(anchorIndex, focusIndex);
          const last = Math.max(anchorIndex, focusIndex);
          for (let index = first; index <= last; index += 1) {
            retainedIndexes.add(index);
          }
        }
      }
      return [...retainedIndexes].sort((left, right) => left - right);
    },
    gap: HISTORY_TURN_GAP_PX,
    paddingStart: historyTopInset,
    paddingEnd: historyBottomInset,
    useAnimationFrameWithResizeObserver: true,
  });
  const measurementBoundaryIndex = retainedMeasurementBoundary
    ? turns.findIndex((turn) => turn.id === retainedMeasurementBoundary.turnId)
    : -1;
  const detailBoundaryIndex = activeDetailAnchor
    ? turns.findIndex((turn) => turn.id === activeDetailAnchor.turnId)
    : -1;
  if (detailBoundaryIndex >= 0 && activeDetailAnchor) {
    // The detail transaction owns the residual exact-edge correction. TanStack
    // still compensates measurements wholly before a start edge, or through
    // the replaced row for an end edge, so unrelated late image/Markdown
    // measurements do not move the retained reading point.
    virtualizer.shouldAdjustScrollPositionOnItemSizeChange = (item) =>
      activeDetailAnchor.edge === "end"
        ? item.index <= detailBoundaryIndex
        : item.index < detailBoundaryIndex;
  } else if (!virtualScrollPolicy.allowResizeAdjustment) {
    virtualizer.shouldAdjustScrollPositionOnItemSizeChange = () => false;
  } else if (!scrollState.followOutput && measurementBoundaryIndex >= 0) {
    // TanStack remains the sole scroll writer. This predicate only tells it
    // which late measurements live completely before the user's reading row.
    virtualizer.shouldAdjustScrollPositionOnItemSizeChange =
      (item) => item.index < measurementBoundaryIndex;
  } else {
    virtualizer.shouldAdjustScrollPositionOnItemSizeChange = undefined;
  }

  const measureTurnOffset = useCallback((turnId: string): number | null => {
    const el = scrollRef.current;
    const node = turnNodeRefs.current.get(turnId);
    if (!el || !node) return null;
    return node.getBoundingClientRect().top - el.getBoundingClientRect().top;
  }, []);

  const measureDetailEdge = useCallback((
    turnId: string,
    edge: DetailAnchorEdge,
  ): number | null => {
    const el = scrollRef.current;
    const turnNode = turnNodeRefs.current.get(turnId);
    if (!el || !turnNode) return null;
    const processNode = turnNode.querySelector<HTMLElement>(
      "[data-process-detail-root]",
    ) ?? turnNode;
    const viewportRect = el.getBoundingClientRect();
    const processRect = processNode.getBoundingClientRect();
    return (edge === "end" ? processRect.bottom : processRect.top)
      - viewportRect.top;
  }, []);

  const firstTurnId = turns[0]?.id ?? null;
  const captureHistoryBoundary = useCallback(
    (): CapturedHistoryBoundary | null => {
      const el = scrollRef.current;
      const viewportTop = el?.getBoundingClientRect().top;
      let anchorTurnId: string | null = null;
      let anchorOffset = 0;
      let bestDistance = Number.POSITIVE_INFINITY;
      if (el && viewportTop != null) {
        for (const [turnId, node] of turnNodeRefs.current) {
          const rect = node.getBoundingClientRect();
          if (rect.bottom <= viewportTop
              || rect.top >= viewportTop + el.clientHeight) continue;
          const distance = Math.abs(rect.top - viewportTop);
          if (distance < bestDistance) {
            anchorTurnId = turnId;
            anchorOffset = rect.top - viewportTop;
            bestDistance = distance;
          }
        }
      }
      anchorTurnId ??= firstTurnId;
      if (!anchorTurnId) return null;
      anchorOffset = measureTurnOffset(anchorTurnId) ?? anchorOffset;
      return {
        anchorTurnId,
        oldestTurnId: firstTurnId,
        anchorOffset,
      };
    },
    [firstTurnId, measureTurnOffset],
  );

  const clearHistoryRequestTimeout = useCallback((generation?: number) => {
    const pending = historyRequestTimeoutRef.current;
    if (!pending || (generation != null
        && pending.generation !== generation)) return;
    window.clearTimeout(pending.timer);
    historyRequestTimeoutRef.current = null;
  }, []);

  const cancelHistoryAnchor = useCallback((generation?: number): boolean => {
    clearHistoryRequestTimeout(generation);
    const activeGeneration =
      historyAnchorRef.current.current()?.generation ?? null;
    const cancelled = historyAnchorRef.current.cancel(generation);
    if (!cancelled) return false;
    if (touchHistoryGenerationRef.current === activeGeneration) {
      touchHistoryGenerationRef.current = null;
      touchAppliedBoundaryRef.current = null;
    }
    if (historyReleaseFrameRef.current !== null) {
      window.cancelAnimationFrame(historyReleaseFrameRef.current);
      historyReleaseFrameRef.current = null;
    }
    if (historyReleaseStartedAtRef.current?.generation === activeGeneration) {
      historyReleaseStartedAtRef.current = null;
    }
    setActiveHistoryGeneration(null);
    return cancelled;
  }, [clearHistoryRequestTimeout]);

  const syncScrollState = useCallback((next: ScrollFollowSnapshot) => {
    setScrollState((previous) =>
      previous.followOutput === next.followOutput && previous.nearBottom === next.nearBottom
        ? previous
        : next);
  }, []);

  const applyScrollCommand = useCallback((command: ScrollCommand | null) => {
    if (!command) return;
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    if (command.kind === "bottom") {
      virtualizer.scrollToEnd({ behavior: command.behavior });
    } else {
      virtualizer.scrollToOffset(command.offset, { behavior: "auto" });
    }
    lastScrollTopRef.current = el.scrollTop;
    syncScrollState(controller.recordProgrammaticScroll(readScrollMetrics(el)));
  }, [syncScrollState, virtualizer]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let previousHeight = el.clientHeight;
    let frame: number | null = null;
    const observer = new ResizeObserver(() => {
      const nextHeight = el.clientHeight;
      if (Math.abs(nextHeight - previousHeight) < 0.5) return;
      previousHeight = nextHeight;
      if (!controllerRef.current?.isFollowing()) return;
      if (frame !== null) window.cancelAnimationFrame(frame);
      // Composer actions and the mobile keyboard resize the thread from
      // outside the virtual list. Let both ResizeObservers commit the new
      // viewport size, then restore the live-tail intent through TanStack's
      // sole scroll writer. Reading-history state never enters this branch.
      frame = window.requestAnimationFrame(() => {
        frame = null;
        if (!controllerRef.current?.isFollowing()
            || userScrollIntentRef.current
            || touchYRef.current !== null) return;
        applyScrollCommand(
          scrollCoordinatorRef.current.requestBottom("auto"),
        );
      });
    });
    observer.observe(el);
    return () => {
      observer.disconnect();
      if (frame !== null) window.cancelAnimationFrame(frame);
    };
  }, [applyScrollCommand, scrollScope]);

  const pauseOutputFollow = useCallback(() => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    syncScrollState(controller.pause(readScrollMetrics(el)));
  }, [syncScrollState]);

  const publishTextSelection = useCallback((
    selection: TextSelectionRetention | null,
  ) => {
    if (!onTextSelectionGuardChange) return;
    if (!selection || selection.scope !== scrollScope || !sid) {
      onTextSelectionGuardChange(null);
      return;
    }
    onTextSelectionGuardChange({
      sid,
      revision: historyRevision,
      viewId: resolvedHistoryViewId,
      scopeKey: historyScopeKey,
      turnIds: [selection.anchorTurnId, selection.focusTurnId],
    });
  }, [
    historyRevision, historyScopeKey, onTextSelectionGuardChange,
    resolvedHistoryViewId, scrollScope, sid,
  ]);

  const commitTextSelection = useCallback((
    selection: TextSelectionRetention | null,
  ) => {
    textSelectionRef.current = selection;
    setTextSelection(selection);
    publishTextSelection(selection);
  }, [publishTextSelection]);

  const releaseTextSelectionInteraction = useCallback((
    pointerId?: number,
  ) => {
    const active = textSelectionRef.current;
    if (!active || !active.dragging
        || (pointerId != null && active.pointerId !== pointerId)) return;
    const releaseBoundary = captureHistoryBoundary();
    if (active.interactionToken !== null) {
      // Finishing a text drag must never replay a bottom command queued while
      // the browser owned native selection auto-scroll.
      scrollCoordinatorRef.current.endInteraction(
        active.interactionToken, false,
      );
      setScrollPolicyEpoch((value) => value + 1);
    }
    commitTextSelection({
      ...active,
      dragging: false,
      interactionToken: null,
      releaseAnchorTurnId: releaseBoundary?.anchorTurnId ?? null,
      releaseAnchorOffset: releaseBoundary?.anchorOffset ?? null,
    });
  }, [captureHistoryBoundary, commitTextSelection]);

  const clearTextSelection = useCallback(() => {
    textSelectionCandidateRef.current = null;
    const active = textSelectionRef.current;
    if (active?.interactionToken != null) {
      scrollCoordinatorRef.current.endInteraction(
        active.interactionToken, false,
      );
      setScrollPolicyEpoch((value) => value + 1);
    }
    if (active) {
      commitTextSelection(null);
    } else {
      publishTextSelection(null);
    }
  }, [commitTextSelection, publishTextSelection]);

  const disposeTextSelection = useCallback(() => {
    const selection = textSelectionRef.current;
    if (selection?.interactionToken != null) {
      scrollCoordinatorRef.current.endInteraction(
        selection.interactionToken, false,
      );
    }
    textSelectionRef.current = null;
    textSelectionCandidateRef.current = null;
    onTextSelectionGuardChange?.(null);
  }, [onTextSelectionGuardChange]);

  const observeNativeTextSelection = useCallback(() => {
    const nativeSelection = window.getSelection();
    const candidate = textSelectionCandidateRef.current;
    const active = textSelectionRef.current;
    if (!nativeSelection || nativeSelection.isCollapsed
        || nativeSelection.rangeCount === 0) {
      if (active) clearTextSelection();
      return;
    }
    if (!candidate && !active) return;
    const expectedScope = active?.scope ?? candidate?.scope;
    if (expectedScope !== scrollScope) {
      clearTextSelection();
      return;
    }
    const root = scrollRef.current;
    const anchorTurnId = selectionTurnId(root, nativeSelection.anchorNode);
    const focusTurnId = selectionTurnId(root, nativeSelection.focusNode);
    if (!anchorTurnId || !focusTurnId) {
      // While the mouse is held just outside the scrollport Chromium may put
      // the focus in the surrounding document for one selectionchange. Keep
      // the last valid in-thread boundary until the next in-thread move.
      if (!active || !active.dragging) clearTextSelection();
      return;
    }
    if (active) {
      if (active.anchorTurnId === anchorTurnId
          && active.focusTurnId === focusTurnId) return;
      commitTextSelection({
        ...active,
        anchorTurnId,
        focusTurnId,
      });
      return;
    }
    if (!candidate || !sid) return;
    cancelDetailAnchorFnRef.current?.();
    setMeasurementBoundary(null);
    pauseOutputFollow();
    const interactionToken =
      scrollCoordinatorRef.current.beginInteraction(false);
    const selection: TextSelectionRetention = {
      scope: scrollScope,
      anchorTurnId,
      focusTurnId,
      pointerId: candidate.pointerId,
      interactionToken,
      dragging: true,
      releaseAnchorTurnId: null,
      releaseAnchorOffset: null,
    };
    commitTextSelection(selection);
    setScrollPolicyEpoch((value) => value + 1);
  }, [
    clearTextSelection, commitTextSelection, pauseOutputFollow, scrollScope,
    sid,
  ]);

  useEffect(() => {
    const handlePointerEnd = (event: globalThis.PointerEvent) => {
      const candidate = textSelectionCandidateRef.current;
      if (candidate?.pointerId === event.pointerId) {
        textSelectionCandidateRef.current = null;
      }
      releaseTextSelectionInteraction(event.pointerId);
    };
    const handleDocumentPointerDown = (event: globalThis.PointerEvent) => {
      const active = textSelectionRef.current;
      if (!active || active.dragging || event.button !== 0) return;
      const target = event.target;
      if (target instanceof Node && !scrollRef.current?.contains(target)) {
        clearTextSelection();
      }
    };
    const handleCopy = () => {
      if (!textSelectionRef.current) return;
      window.requestAnimationFrame(() => clearTextSelection());
    };
    document.addEventListener("selectionchange", observeNativeTextSelection);
    document.addEventListener("pointerup", handlePointerEnd);
    document.addEventListener("pointercancel", handlePointerEnd);
    document.addEventListener("pointerdown", handleDocumentPointerDown, true);
    document.addEventListener("copy", handleCopy);
    return () => {
      document.removeEventListener(
        "selectionchange", observeNativeTextSelection,
      );
      document.removeEventListener("pointerup", handlePointerEnd);
      document.removeEventListener("pointercancel", handlePointerEnd);
      document.removeEventListener(
        "pointerdown", handleDocumentPointerDown, true,
      );
      document.removeEventListener("copy", handleCopy);
    };
  }, [
    clearTextSelection, observeNativeTextSelection,
    releaseTextSelectionInteraction,
  ]);

  const completeHistoryLoadGates = useCallback(() => {
    historyLoadGateRef.current.complete();
    wheelHistoryLoadGateRef.current.complete();
  }, []);

  const scheduleHistoryAnchorRelease = useCallback((generation: number) => {
    if (touchYRef.current !== null
        || scrollCoordinatorRef.current.isInteractionLocked()) return;
    if (historyReleaseFrameRef.current !== null) {
      window.cancelAnimationFrame(historyReleaseFrameRef.current);
    }
    const previousRelease = historyReleaseStartedAtRef.current;
    const startedAt = previousRelease?.generation === generation
      ? previousRelease.startedAt : window.performance.now();
    historyReleaseStartedAtRef.current = { generation, startedAt };
    const releaseWhenAligned = () => {
      historyReleaseFrameRef.current = null;
      const anchor = historyAnchorRef.current.current();
      if (!anchor || anchor.generation !== generation) {
        if (historyReleaseStartedAtRef.current?.generation === generation) {
          historyReleaseStartedAtRef.current = null;
        }
        return;
      }
      if (touchYRef.current !== null
          || scrollCoordinatorRef.current.isInteractionLocked()) {
        // Waiting for a real pointer gesture is not failed settlement time.
        // Its release path will schedule a fresh bounded correction.
        if (historyReleaseStartedAtRef.current?.generation === generation) {
          historyReleaseStartedAtRef.current = null;
        }
        return;
      }
      const currentOffset = measureTurnOffset(anchor.anchorTurnId);
      if (currentOffset == null
          || Math.abs(currentOffset - anchor.anchorOffset) > 0.5) {
        if (window.performance.now() - startedAt
            >= HISTORY_ANCHOR_SETTLE_MAX_MS) {
          cancelHistoryAnchor(generation);
          completeHistoryLoadGates();
          return;
        }
        // The page can commit before WebKit accepts the post-touch scroll
        // correction. Keep the keyed row mounted and run the layout
        // correction again; releasing here would replace it with the newly
        // prepended first row and make the viewport jump.
        setScrollPolicyEpoch((value) => value + 1);
        historyReleaseFrameRef.current =
          window.requestAnimationFrame(releaseWhenAligned);
        return;
      }
      cancelHistoryAnchor(generation);
      completeHistoryLoadGates();
    };
    historyReleaseFrameRef.current =
      window.requestAnimationFrame(releaseWhenAligned);
  }, [
    cancelHistoryAnchor, completeHistoryLoadGates, measureTurnOffset,
  ]);

  // Freeze one retained row before either asynchronous window mutation.
  // Cached-newer paging may append at the tail and evict rows at the head, so
  // it deliberately uses the same keyed measurement transaction as prepend.
  const doLoadPage = (direction: HistoryPageDirection): boolean => {
    if (historyAnchorRef.current.current()) return false;
    const el = scrollRef.current;
    const point = el ? captureHistoryBoundary() : null;
    if (el) pauseOutputFollow();
    const loadResult = direction === "older"
      ? onLoadMore?.(point?.anchorTurnId)
      : onLoadNewer?.(point?.anchorTurnId);
    const accepted = loadResult === true
      || (typeof loadResult === "object" && loadResult.accepted);
    const requestViewId = typeof loadResult === "object"
      ? loadResult.viewId ?? resolvedHistoryViewId
      : resolvedHistoryViewId;
    if (!accepted) {
      clearHistoryRequestTimeout();
      completeHistoryLoadGates();
      return false;
    }
    historyRequestRef.current = {
      sid,
      revision: historyRevision,
      viewId: requestViewId,
      direction,
      before: direction === "older" ? historyCursor : null,
      windowEpoch: historyWindowEpoch,
    };
    let generation: number | null = null;
    if (point) {
      cancelHistoryAnchor();
      setMeasurementBoundary({
        sid,
        revision: historyRevision,
        viewId: requestViewId,
        turnId: point.anchorTurnId,
        anchorOffset: point.anchorOffset,
      });
      generation = historyAnchorRef.current.begin({
        sid, revision: historyRevision, viewId: requestViewId,
        before: direction === "older" ? historyCursor : null,
        windowEpoch: historyWindowEpoch,
        direction,
        source: direction === "older" ? "server" : "local",
        anchorTurnId: point.anchorTurnId,
        oldestTurnId: point.oldestTurnId,
        anchorOffset: point.anchorOffset,
      });
      touchHistoryGenerationRef.current =
        touchYRef.current !== null ? generation : null;
      touchAppliedBoundaryRef.current = null;
      setActiveHistoryGeneration(generation);
    }
    clearHistoryRequestTimeout();
    const timeoutGeneration = generation;
    const timer = window.setTimeout(() => {
      const pending = historyRequestTimeoutRef.current;
      if (!pending || pending.timer !== timer) return;
      historyRequestTimeoutRef.current = null;
      historyRequestRef.current = null;
      if (timeoutGeneration != null) cancelHistoryAnchor(timeoutGeneration);
      setMeasurementBoundary(null);
      completeHistoryLoadGates();
    }, HISTORY_PAGE_REQUEST_TIMEOUT_MS);
    historyRequestTimeoutRef.current = {
      generation: timeoutGeneration,
      timer,
    };
    return true;
  };
  const doLoadMore = (): boolean => doLoadPage("older");
  const doLoadNewer = (): boolean => doLoadPage("newer");

  // Scroll/touch events can repeat many times while a finger or wheel remains
  // pinned at the top. Touch/wheel gates allow one request per gesture; plain
  // scroll/keyboard events additionally use the visible boundary as their gate.
  const maybeAutoLoadOlder = (
    movingTowardHistory: boolean,
    source: "touch" | "wheel" | "other",
  ) => {
    const el = scrollRef.current;
    if (!el || !shouldAutoLoadOlderHistory(
      readScrollMetrics(el), movingTowardHistory, canLoadOlder,
    )) return;
    const boundary = [
      "older", sid ?? "", resolvedHistoryViewId, turns[0]?.id ?? "",
      turns.length, historyRevision ?? "", historyCursor ?? "", hasMore ? 1 : 0,
    ].join("\u0000");
    if (source === "other" && autoLoadedBoundaryRef.current === boundary) return;
    const gestureGate = source === "touch"
      ? historyLoadGateRef.current
      : source === "wheel" ? wheelHistoryLoadGateRef.current : null;
    if (gestureGate && !gestureGate.acquire()) return;
    if (doLoadMore()) {
      autoLoadedBoundaryRef.current = boundary;
    } else {
      gestureGate?.complete();
    }
  };

  const maybeAutoLoadNewer = (
    movingTowardLatest: boolean,
    source: "touch" | "wheel" | "other",
  ) => {
    const el = scrollRef.current;
    if (!el || !shouldAutoLoadNewerHistory(
      readScrollMetrics(el), movingTowardLatest, canLoadNewer,
    )) return;
    const boundary = [
      "newer", sid ?? "", resolvedHistoryViewId,
      turns.at(-1)?.id ?? "", turns.length,
      historyRevision ?? "", historyWindowEpoch, hasNewer ? 1 : 0,
    ].join("\u0000");
    if (source === "other" && autoLoadedBoundaryRef.current === boundary) return;
    const gestureGate = source === "touch"
      ? historyLoadGateRef.current
      : source === "wheel" ? wheelHistoryLoadGateRef.current : null;
    if (gestureGate && !gestureGate.acquire()) return;
    if (doLoadNewer()) {
      autoLoadedBoundaryRef.current = boundary;
    } else {
      gestureGate?.complete();
    }
  };

  useLayoutEffect(() => {
    const request = historyRequestRef.current;
    const requestScopeChanged = request && (
      request.sid !== sid
      || request.revision !== historyRevision
      || request.viewId !== resolvedHistoryViewId
    );
    const requestCompleted = request && (
      request.direction === "older"
        ? request.before !== historyCursor || !hasMore
        : request.windowEpoch !== historyWindowEpoch || !canLoadNewer
    );
    if (requestScopeChanged || requestCompleted) {
      historyRequestRef.current = null;
      clearHistoryRequestTimeout();
    }
    const anchor = historyAnchorRef.current.current();
    if (!anchor) {
      if (requestScopeChanged || requestCompleted) completeHistoryLoadGates();
      return;
    }
    if (anchor.sid !== sid || anchor.revision !== historyRevision
        || (anchor.viewId ?? null) !== resolvedHistoryViewId) {
      cancelHistoryAnchor(anchor.generation);
      completeHistoryLoadGates();
      return;
    }
    if (anchor.phase === "applied") {
      scheduleHistoryAnchorRelease(anchor.generation);
      return;
    }
    if (anchor.phase !== "pending") return;
    const pageStatus = historyPageStatus(anchor, {
      sid, revision: historyRevision, cursor: historyCursor,
      viewId: resolvedHistoryViewId, hasMore: !!hasMore,
      windowEpoch: historyWindowEpoch, hasNewer: canLoadNewer,
    });
    if (pageStatus === "pending") return;
    if (pageStatus === "stale") {
      cancelHistoryAnchor(anchor.generation);
      completeHistoryLoadGates();
      return;
    }

    if (!turns.some((turn) => turn.id === anchor.anchorTurnId)) {
      // The bounded projection no longer contains the reading row. Never
      // manufacture a viewport movement from cursor metadata alone.
      cancelHistoryAnchor(anchor.generation);
      completeHistoryLoadGates();
      return;
    }
    if (historyAnchorRef.current.markRendering(anchor.generation)) {
      if (historyAnchorRef.current.markApplied(anchor.generation)
          && touchHistoryGenerationRef.current === anchor.generation
          && touchYRef.current !== null) {
        const clockOffset = touchEventClockOffsetRef.current;
        touchAppliedBoundaryRef.current = {
          generation: anchor.generation,
          appliedEventTimestamp: clockOffset == null
            ? Number.POSITIVE_INFINITY
            : window.performance.now() - clockOffset,
          baselineY: touchYRef.current,
          movedAfterApply: false,
        };
      }
      scheduleHistoryAnchorRelease(anchor.generation);
    }
  }, [
    cancelHistoryAnchor, canLoadNewer, clearHistoryRequestTimeout,
    completeHistoryLoadGates, hasMore,
    historyCursor, historyRevision, historyWindowEpoch, resolvedHistoryViewId,
    scheduleHistoryAnchorRelease, sid, turns,
  ]);

  // WebKit can clamp the virtualizer's keyed prepend adjustment against the
  // previous sizer height. The coordinator owns the one residual correction:
  // it is scoped to the retained reading turn and never runs during touch or
  // an interactive control press.
  useLayoutEffect(() => {
    const boundary = retainedMeasurementBoundary;
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!boundary || !el || !controller
        || boundary.sid !== sid
        || boundary.revision !== historyRevision
        || boundary.viewId !== resolvedHistoryViewId
        || touchYRef.current !== null
        || (userScrollIntentRef.current && !keyedPrependResponseReady
          && !retainedSelectionBoundary)
        || scrollCoordinatorRef.current.isInteractionLocked()) return;
    const currentOffset = measureTurnOffset(boundary.turnId);
    if (currentOffset == null) return;
    const delta = currentOffset - boundary.anchorOffset;
    if (Math.abs(delta) <= 0.5) return;
    applyScrollCommand(scrollCoordinatorRef.current.requestOffset(
      el.scrollTop + delta,
    ));
  });

  useLayoutEffect(() => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;

    // Initial mount, session switches, and authoritative revision replacements
    // are anchored synchronously before paint. Commit the scope only once the
    // replacement thread exists; an intermediate empty projection has no
    // viewport on which the reset command can be consumed.
    if (renderedScrollScopeRef.current !== scrollScope) {
      renderedScrollScopeRef.current = scrollScope;
      textSelectionCandidateRef.current = null;
      if (textSelectionRef.current) clearTextSelection();
      const request = historyRequestRef.current;
      const anchor = historyAnchorRef.current.current();
      const enteringBrowse = browseMode
        && request?.direction === "older"
        && request.sid === sid
        && request.revision === historyRevision
        && request.viewId === resolvedHistoryViewId
        && anchor?.generation === activeHistoryGeneration
        && anchor.sid === sid
        && anchor.revision === historyRevision
        && anchor.viewId === resolvedHistoryViewId;
      if (enteringBrowse) {
        syncScrollState(controller.pause(readScrollMetrics(el)));
        return;
      }
      cancelHistoryAnchor();
      clearHistoryRequestTimeout();
      touchYRef.current = null;
      touchAppliedBoundaryRef.current = null;
      touchEventClockOffsetRef.current = null;
      userScrollIntentRef.current = false;
      userScrollDirectionRef.current = null;
      if (userScrollIntentTimerRef.current !== null) {
        window.clearTimeout(userScrollIntentTimerRef.current);
        userScrollIntentTimerRef.current = null;
      }
      if (wheelGestureTimerRef.current !== null) {
        window.clearTimeout(wheelGestureTimerRef.current);
        wheelGestureTimerRef.current = null;
      }
      wheelGestureActiveRef.current = false;
      historyRequestRef.current = null;
      autoLoadedBoundaryRef.current = null;
      historyLoadGateRef.current.complete();
      historyLoadGateRef.current.endGesture();
      wheelHistoryLoadGateRef.current.complete();
      wheelHistoryLoadGateRef.current.endGesture();
      setMeasurementBoundary(null);
      cancelDetailAnchorFnRef.current?.(false);
      applyScrollCommand(scrollCoordinatorRef.current.reset());
      syncScrollState(browseMode
        ? controller.pause(readScrollMetrics(el))
        : controller.reset(readScrollMetrics(el)));
      return;
    }

    if (browseMode && controller.isFollowing()) {
      syncScrollState(controller.pause(readScrollMetrics(el)));
      return;
    }
    if (!controller.isFollowing()) {
      syncScrollState(controller.observeLayout(readScrollMetrics(el)));
    }
  }, [
    activeHistoryGeneration, applyScrollCommand, browseMode, cancelHistoryAnchor,
    clearHistoryRequestTimeout, clearTextSelection, historyRevision,
    resolvedHistoryViewId,
    scrollScope, sid, syncScrollState, turns,
  ]);

  useEffect(() => {
    return () => {
      cancelHistoryAnchor();
      clearHistoryRequestTimeout();
      if (wheelGestureTimerRef.current !== null) {
        window.clearTimeout(wheelGestureTimerRef.current);
      }
      if (userScrollIntentTimerRef.current !== null) {
        window.clearTimeout(userScrollIntentTimerRef.current);
      }
      disposeTextSelection();
      cancelDetailAnchorFnRef.current?.(false);
    };
  }, [
    cancelHistoryAnchor, clearHistoryRequestTimeout, disposeTextSelection,
  ]);

  useEffect(() => setZoom(null), [sid]);

  const markUserScrollIntent = (direction: UserScrollDirection) => {
    // A real wheel/touch/key/pointer action transfers ownership back to the
    // reader. Late detail responses and ResizeObserver callbacks must not pull
    // the viewport back to the edge captured before that gesture.
    cancelDetailAnchorFnRef.current?.();
    setMeasurementBoundary(null);
    userScrollIntentRef.current = true;
    userScrollDirectionRef.current = direction;
    if (userScrollIntentTimerRef.current !== null) {
      window.clearTimeout(userScrollIntentTimerRef.current);
    }
    userScrollIntentTimerRef.current = window.setTimeout(() => {
      userScrollIntentTimerRef.current = null;
      userScrollIntentRef.current = false;
      userScrollDirectionRef.current = null;
      const controller = controllerRef.current;
      const point = captureHistoryBoundary();
      const request = historyRequestRef.current;
      if (!measurementBoundaryRef.current
          && point && (!controller?.isFollowing()
          || (request?.sid === sid
            && request.revision === historyRevision
            && request.viewId === resolvedHistoryViewId))) {
        setMeasurementBoundary({
          sid,
          revision: historyRevision,
          viewId: resolvedHistoryViewId,
          turnId: point.anchorTurnId,
          anchorOffset: point.anchorOffset,
        });
      }
    }, USER_SCROLL_INTENT_IDLE_MS);
  };

  const onThreadPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    markUserScrollIntent("unknown");
    if (event.pointerType !== "mouse" || event.button !== 0
        || !event.isPrimary) {
      textSelectionCandidateRef.current = null;
      return;
    }
    if (textSelectionRef.current) clearTextSelection();
    const target = event.target instanceof Element ? event.target : null;
    const turn = target?.closest<HTMLElement>("[data-turn-id]");
    if (!target || !turn || target.closest(TEXT_SELECTION_EXCLUDED_SELECTOR)) {
      textSelectionCandidateRef.current = null;
      return;
    }
    textSelectionCandidateRef.current = {
      scope: scrollScope,
      pointerId: event.pointerId,
    };
  };

  const onScroll = () => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    const metrics = readScrollMetrics(el);
    const movingTowardHistory = metrics.scrollTop < lastScrollTopRef.current - 0.5;
    const movingTowardLatest = metrics.scrollTop > lastScrollTopRef.current + 0.5;
    const movementDirection: UserScrollDirection | null = movingTowardHistory
      ? "history" : movingTowardLatest ? "latest" : null;
    const intendedDirection = userScrollDirectionRef.current;
    const textSelectionDragging =
      textSelectionRef.current?.scope === scrollScope
      && textSelectionRef.current.dragging;
    const currentHistoryAnchor = historyAnchorRef.current.current();
    const touchOwnsHistoryAnchor = currentHistoryAnchor != null
      && touchHistoryGenerationRef.current === currentHistoryAnchor.generation;
    const explicitAppliedMovement = keyedPrependResponseReady
      && currentHistoryAnchor?.phase === "applied"
      && intendedDirection !== null
      && intendedDirection !== "unknown"
      // Mobile WebKit can deliver the scroll that reached the paging edge
      // only after the response has already rendered. For a touch-owned page,
      // only a touchmove observed after that render may replace the original
      // retained row. clearTouch synchronously captures that explicit move
      // before clearing this marker.
      && (!touchOwnsHistoryAnchor
        || (touchAppliedBoundaryRef.current?.generation
            === currentHistoryAnchor.generation
          && touchAppliedBoundaryRef.current.movedAfterApply));
    const userDrivenScroll = movementDirection !== null && (
      textSelectionDragging
      || (userScrollIntentRef.current
        && (intendedDirection === "unknown"
          || intendedDirection === movementDirection)
        && (!keyedPrependResponseReady || explicitAppliedMovement))
    );
    if (userDrivenScroll && intendedDirection) {
      markUserScrollIntent(intendedDirection);
    }
    if (userDrivenScroll && currentHistoryAnchor) {
      if (currentHistoryAnchor.phase === "applied") {
        const point = captureHistoryBoundary();
        if (point && historyAnchorRef.current.rebase(
          currentHistoryAnchor.generation,
          point,
        )) {
          setMeasurementBoundary({
            sid,
            revision: historyRevision,
            viewId: resolvedHistoryViewId,
            turnId: point.anchorTurnId,
            anchorOffset: point.anchorOffset,
          });
        }
      } else {
        const stillAtRequestedEdge = currentHistoryAnchor.direction === "newer"
          ? isAtLatestEdge(metrics) : isAtHistoryEdge(metrics);
        historyAnchorRef.current.observeUserScroll(stillAtRequestedEdge);
        if (!historyAnchorRef.current.current()) {
          setActiveHistoryGeneration(null);
          completeHistoryLoadGates();
        }
      }
    }
    const request = historyRequestRef.current;
    if (userDrivenScroll && request?.sid === sid
        && request.revision === historyRevision
        && request.viewId === resolvedHistoryViewId) {
      const point = captureHistoryBoundary();
      if (point) {
        setMeasurementBoundary({
          sid,
          revision: historyRevision,
          viewId: resolvedHistoryViewId,
          turnId: point.anchorTurnId,
          anchorOffset: point.anchorOffset,
        });
      }
    }
    lastScrollTopRef.current = metrics.scrollTop;
    const nextScrollState = userDrivenScroll
      ? controller.observeScroll(
        metrics, !browseMode && !textSelectionDragging,
      )
      : controller.recordProgrammaticScroll(metrics);
    if (nextScrollState.followOutput && !scrollState.followOutput
        && !historyRequestRef.current) {
      setMeasurementBoundary(null);
    }
    syncScrollState(nextScrollState);
    if (!textSelectionDragging) {
      maybeAutoLoadOlder(
        movingTowardHistory && userDrivenScroll,
        touchYRef.current != null ? "touch"
          : wheelGestureActiveRef.current ? "wheel" : "other",
      );
      maybeAutoLoadNewer(
        movingTowardLatest && userDrivenScroll,
        touchYRef.current != null ? "touch"
          : wheelGestureActiveRef.current ? "wheel" : "other",
      );
    }
  };

  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    if (Math.abs(event.deltaY) <= 0.5) return;
    const direction: HistoryPageDirection =
      event.deltaY < 0 ? "older" : "newer";
    markUserScrollIntent(direction === "older" ? "history" : "latest");
    const anchor = historyAnchorRef.current.current();
    if (anchor?.direction && anchor.direction !== direction
        && (anchor.phase === "pending" || anchor.phase === "rendering")) {
      if (cancelHistoryAnchor(anchor.generation)) {
        setMeasurementBoundary(null);
      }
      completeHistoryLoadGates();
    }
    if (event.deltaY < 0 || browseMode) {
      pauseOutputFollow();
    }
    if (!wheelGestureActiveRef.current) {
      wheelGestureActiveRef.current = true;
      wheelHistoryLoadGateRef.current.beginGesture();
    }
    if (wheelGestureTimerRef.current !== null) {
      window.clearTimeout(wheelGestureTimerRef.current);
    }
    wheelGestureTimerRef.current = window.setTimeout(() => {
      wheelGestureTimerRef.current = null;
      wheelGestureActiveRef.current = false;
      wheelHistoryLoadGateRef.current.endGesture();
    }, WHEEL_GESTURE_IDLE_MS);
    const el = scrollRef.current;
    if (el) {
      const metrics = readScrollMetrics(el);
      if (direction === "older" && isAtHistoryEdge(metrics)) {
        maybeAutoLoadOlder(true, "wheel");
      } else if (direction === "newer" && isAtLatestEdge(metrics)) {
        maybeAutoLoadNewer(true, "wheel");
      }
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (["ArrowUp", "PageUp", "Home"].includes(event.key)) {
      markUserScrollIntent("history");
    } else if (["ArrowDown", "PageDown", "End", " "].includes(event.key)) {
      markUserScrollIntent("latest");
    }
  };

  const onTouchStart = (event: TouchEvent<HTMLDivElement>) => {
    markUserScrollIntent("unknown");
    historyLoadGateRef.current.beginGesture();
    touchAppliedBoundaryRef.current = null;
    touchEventClockOffsetRef.current =
      window.performance.now() - event.timeStamp;
    touchYRef.current = event.touches[0]?.clientY ?? null;
  };

  const onTouchMove = (event: TouchEvent<HTMLDivElement>) => {
    const currentY = event.touches[0]?.clientY;
    const previousY = touchYRef.current;
    if (currentY == null || previousY == null) return;
    // Publish the newest finger position before a paging state update can
    // synchronously commit and capture the applied boundary.
    touchYRef.current = currentY;
    // A finger moving down scrolls the viewport toward earlier messages.
    if (currentY > previousY) {
      markUserScrollIntent("history");
      pauseOutputFollow();
      const anchor = historyAnchorRef.current.current();
      if (anchor?.direction === "newer"
          && (anchor.phase === "pending" || anchor.phase === "rendering")) {
        if (cancelHistoryAnchor(anchor.generation)) {
          setMeasurementBoundary(null);
        }
        completeHistoryLoadGates();
      }
      const el = scrollRef.current;
      if (el && isAtHistoryEdge(readScrollMetrics(el))) {
        maybeAutoLoadOlder(true, "touch");
      }
    } else if (currentY < previousY) {
      markUserScrollIntent("latest");
      const anchor = historyAnchorRef.current.current();
      const appliedBoundary = touchAppliedBoundaryRef.current;
      if (anchor?.phase === "applied"
          && appliedBoundary?.generation === anchor.generation
          && event.timeStamp > appliedBoundary.appliedEventTimestamp
          && currentY < appliedBoundary.baselineY - 0.5) {
        appliedBoundary.movedAfterApply = true;
      }
      if (anchor?.direction !== "newer"
          && (anchor?.phase === "pending" || anchor?.phase === "rendering")) {
        if (cancelHistoryAnchor(anchor.generation)) {
          setMeasurementBoundary(null);
        }
        completeHistoryLoadGates();
      }
      const el = scrollRef.current;
      if (el && isAtLatestEdge(readScrollMetrics(el))) {
        maybeAutoLoadNewer(true, "touch");
      }
    }
  };

  const rebaseAppliedHistoryAnchor = () => {
    const anchor = historyAnchorRef.current.current();
    const point = anchor?.phase === "applied" ? captureHistoryBoundary() : null;
    if (!anchor || !point || !historyAnchorRef.current.rebase(
      anchor.generation,
      point,
    )) return;
    setMeasurementBoundary({
      sid,
      revision: historyRevision,
      viewId: resolvedHistoryViewId,
      turnId: point.anchorTurnId,
      anchorOffset: point.anchorOffset,
    });
  };

  const clearTouch = () => {
    // Mobile WebKit may defer React's scroll event until touchend. Capture the
    // DOM's already-moved reading row before clearing the touch lock, otherwise
    // the residual prepend correction can restore the pre-gesture row first.
    if (touchAppliedBoundaryRef.current?.movedAfterApply) {
      rebaseAppliedHistoryAnchor();
    }
    touchAppliedBoundaryRef.current = null;
    touchEventClockOffsetRef.current = null;
    touchYRef.current = null;
    historyLoadGateRef.current.endGesture();
    // A page can finish while the finger is still down. Re-render after the
    // native touch ends so the retained history transaction can correct and
    // release its exact reading boundary without fighting the gesture.
    setScrollPolicyEpoch((value) => value + 1);
    const anchor = historyAnchorRef.current.current();
    if (anchor?.phase === "applied") {
      scheduleHistoryAnchorRelease(anchor.generation);
    }
  };

  const scrollToBottom = () => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    cancelDetailAnchorFnRef.current?.();
    cancelHistoryAnchor();
    historyRequestRef.current = null;
    setMeasurementBoundary(null);
    syncScrollState(controller.resume(readScrollMetrics(el)));
    applyScrollCommand(
      scrollCoordinatorRef.current.requestBottom("smooth"),
    );
  };

  const returnToLatest = () => {
    if (!browseMode || !onReturnLatest) {
      scrollToBottom();
      return;
    }
    cancelDetailAnchorFnRef.current?.();
    cancelHistoryAnchor();
    historyRequestRef.current = null;
    setMeasurementBoundary(null);
    completeHistoryLoadGates();
    onReturnLatest();
  };

  const beginProcessInteraction = useCallback((): number => {
    const el = scrollRef.current;
    const resumeAtBottom = !!el
      && (controllerRef.current?.isFollowing() ?? false)
      && measureBottom(readScrollMetrics(el)).atBottom;
    const token = scrollCoordinatorRef.current.beginInteraction(resumeAtBottom);
    setScrollPolicyEpoch((value) => value + 1);
    return token;
  }, []);

  const endProcessInteraction = useCallback((token: number): void => {
    const command = scrollCoordinatorRef.current.endInteraction(
      token,
      controllerRef.current?.isFollowing() ?? false,
    );
    setScrollPolicyEpoch((value) => value + 1);
    if (command) {
      window.requestAnimationFrame(() => applyScrollCommand(command));
    }
  }, [applyScrollCommand]);

  const disposeDetailResources = useCallback((
    transaction: DetailAnchorTransaction,
  ): void => {
    if (transaction.requestTimer !== null) {
      window.clearTimeout(transaction.requestTimer);
      transaction.requestTimer = null;
    }
    if (transaction.quietTimer !== null) {
      window.clearTimeout(transaction.quietTimer);
      transaction.quietTimer = null;
    }
    if (transaction.maxSettleTimer !== null) {
      window.clearTimeout(transaction.maxSettleTimer);
      transaction.maxSettleTimer = null;
    }
    if (transaction.firstFrame !== null) {
      window.cancelAnimationFrame(transaction.firstFrame);
      transaction.firstFrame = null;
    }
    if (transaction.secondFrame !== null) {
      window.cancelAnimationFrame(transaction.secondFrame);
      transaction.secondFrame = null;
    }
    transaction.observer?.disconnect();
    transaction.observer = null;
    transaction.observedNode = null;
  }, []);

  const releaseDetailAnchor = useCallback((
    releaseInteraction = true,
  ): void => {
    const transaction = detailAnchorRef.current;
    if (!transaction) return;
    detailAnchorRef.current = null;
    disposeDetailResources(transaction);
    if (releaseInteraction) {
      endProcessInteraction(transaction.token);
    }
  }, [disposeDetailResources, endProcessInteraction]);
  cancelDetailAnchorFnRef.current = releaseDetailAnchor;

  const scheduleDetailCorrection = useCallback((
    transaction: DetailAnchorTransaction,
  ): void => {
    if (detailAnchorRef.current !== transaction) return;
    if (transaction.quietTimer !== null) {
      window.clearTimeout(transaction.quietTimer);
      transaction.quietTimer = null;
    }
    if (transaction.firstFrame !== null
        || transaction.secondFrame !== null) return;
    transaction.firstFrame = window.requestAnimationFrame(() => {
      transaction.firstFrame = null;
      if (detailAnchorRef.current !== transaction) return;
      transaction.secondFrame = window.requestAnimationFrame(() => {
        transaction.secondFrame = null;
        if (detailAnchorRef.current !== transaction) return;
        if (userScrollIntentRef.current || touchYRef.current !== null) {
          releaseDetailAnchor();
          return;
        }
        const el = scrollRef.current;
        const currentOffset = measureDetailEdge(
          transaction.turnId, transaction.edge,
        );
        if (el && currentOffset != null) {
          const delta = currentOffset - transaction.anchorOffset;
          if (Math.abs(delta) > 0.5) {
            applyScrollCommand(
              scrollCoordinatorRef.current.requestInteractionOffset(
                transaction.token,
                el.scrollTop + delta,
              ),
            );
          }
        }
        if (transaction.responseSettled) {
          transaction.quietTimer = window.setTimeout(() => {
            transaction.quietTimer = null;
            if (detailAnchorRef.current === transaction) {
              releaseDetailAnchor();
            }
          }, DETAIL_ANCHOR_QUIET_MS);
        }
      });
    });
  }, [applyScrollCommand, measureDetailEdge, releaseDetailAnchor]);

  const requestProcessDetail = useCallback((
    turnId: string,
    before: string | null | undefined,
    direction: DetailPageDirection,
  ): boolean => {
    if (!onLoadDetail) return false;
    const edge: DetailAnchorEdge = direction === "newer" ? "end" : "start";
    const anchorOffset = measureDetailEdge(turnId, edge);
    const turn = turns.find((candidate) => candidate.id === turnId);
    if (anchorOffset == null || !turn || !onLoadDetail(turnId, before)) {
      return false;
    }

    // The request has been accepted, so this is now an explicit reading
    // action. Freeze the exact process edge only after acceptance; rejected
    // clicks leave the previous follow intent untouched.
    pauseOutputFollow();
    const token = scrollCoordinatorRef.current.beginInteraction(false);
    const previous = detailAnchorRef.current;
    if (previous) {
      detailAnchorRef.current = null;
      disposeDetailResources(previous);
      // The new token already holds the viewport, so releasing the old token
      // cannot replay a pending bottom command between transactions.
      endProcessInteraction(previous.token);
    }
    const transaction: DetailAnchorTransaction = {
      scope: scrollScope,
      turnId,
      edge,
      anchorOffset,
      token,
      initialFingerprint: detailTurnFingerprint(turn),
      sawLoading: false,
      responseSettled: false,
      requestTimer: null,
      quietTimer: null,
      maxSettleTimer: null,
      firstFrame: null,
      secondFrame: null,
      observer: null,
      observedNode: null,
    };
    transaction.requestTimer = window.setTimeout(() => {
      if (detailAnchorRef.current === transaction) releaseDetailAnchor();
    }, HISTORY_DETAIL_REQUEST_TIMEOUT_MS + 1_000);
    detailAnchorRef.current = transaction;
    setScrollPolicyEpoch((value) => value + 1);
    return true;
  }, [
    disposeDetailResources, endProcessInteraction, measureDetailEdge,
    onLoadDetail, pauseOutputFollow, releaseDetailAnchor, scrollScope, turns,
  ]);

  useLayoutEffect(() => {
    const transaction = detailAnchorRef.current;
    if (!transaction || transaction.scope !== scrollScope) return;
    const turn = turns.find((candidate) => candidate.id === transaction.turnId);
    if (!turn) {
      releaseDetailAnchor();
      return;
    }
    if (turn.detailLoading) {
      transaction.sawLoading = true;
      transaction.responseSettled = false;
      if (transaction.quietTimer !== null) {
        window.clearTimeout(transaction.quietTimer);
        transaction.quietTimer = null;
      }
      if (transaction.maxSettleTimer !== null) {
        window.clearTimeout(transaction.maxSettleTimer);
        transaction.maxSettleTimer = null;
      }
      if (transaction.requestTimer !== null) {
        window.clearTimeout(transaction.requestTimer);
      }
      // Automatic "load the whole process" may span many bounded responses.
      // Each accepted next page refreshes this inactivity timeout.
      transaction.requestTimer = window.setTimeout(() => {
        if (detailAnchorRef.current === transaction) releaseDetailAnchor();
      }, HISTORY_DETAIL_REQUEST_TIMEOUT_MS + 1_000);
      return;
    }
    const fingerprint = detailTurnFingerprint(turn);
    if (!transaction.sawLoading
        && fingerprint === transaction.initialFingerprint) return;
    if (fingerprint === transaction.initialFingerprint) {
      // The correlated request ended without replacing the detail page.
      releaseDetailAnchor();
      return;
    }
    const followingOlderPage = !!turn.detailAutoLoad
      && !!turn.detailHasMore && !!turn.detailOldestCursor;
    if (!followingOlderPage && !transaction.responseSettled) {
      transaction.responseSettled = true;
      if (transaction.requestTimer !== null) {
        window.clearTimeout(transaction.requestTimer);
        transaction.requestTimer = null;
      }
      transaction.maxSettleTimer = window.setTimeout(() => {
        transaction.maxSettleTimer = null;
        if (detailAnchorRef.current === transaction) releaseDetailAnchor();
      }, DETAIL_ANCHOR_MAX_SETTLE_MS);
    }
    const turnNode = turnNodeRefs.current.get(transaction.turnId);
    const processNode = turnNode?.querySelector<HTMLElement>(
      "[data-process-detail-root]",
    ) ?? null;
    if (processNode && transaction.observedNode !== processNode) {
      transaction.observer?.disconnect();
      transaction.observedNode = processNode;
      if (typeof ResizeObserver !== "undefined") {
        transaction.observer = new ResizeObserver(() => {
          scheduleDetailCorrection(transaction);
        });
        transaction.observer.observe(processNode);
      }
    }
    // Run after both React layout and TanStack's animation-frame ResizeObserver
    // measurement. ResizeObserver refreshes a short quiet window for delayed
    // image/Markdown growth; a hard deadline guarantees eventual unpin.
    scheduleDetailCorrection(transaction);
  }, [
    releaseDetailAnchor, scheduleDetailCorrection, scrollScope, turns,
  ]);

  const [copiedId, setCopiedId] = useState<string | null>(null);
  const copyText = (id: string, text: string) => {
    navigator.clipboard?.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 1500);
  };
  const aiText = (t: Turn) => finalTextBlocks(t.blocks).map((block) => block.text).join("\n\n");

  // Collect engine-neutral file mutations. The helper also understands old
  // Claude file_path and Codex changes payloads already stored in browser cache.
  const fileChips = (t: Turn) => {
    const changes = collectTurnFileChanges([
      ...t.blocks,
      ...(t.detailProjection?.blocks ?? []),
    ]);
    if (!changes.paths.length) return null;
    const arr = changes.paths;
    const canOpenSummary = surface !== "work"
      ? (!!changes.diff && !!onOpenTurnDiff) || (arr.length === 1 && !!onGetDiff)
      : (arr.length === 1 && !!onOpenFile) || !!onOpenArtifacts;
    const openSummary = () => {
      if (surface !== "work") {
        if (changes.diff && onOpenTurnDiff) {
          onOpenTurnDiff(arr, changes.diff);
        } else if (arr.length === 1 && onGetDiff) {
          // Compatibility fallback for old history without a persisted diff.
          // It remains path-scoped and must never open the whole worktree.
          onGetDiff(arr[0]);
        }
        return;
      }
      if (arr.length === 1 && onOpenFile) {
        onOpenFile(arr[0]);
        return;
      }
      onOpenArtifacts?.();
    };
    return (
      <div className="turn-files">
        <button className="turn-files-summary" onClick={openSummary}
          disabled={!canOpenSummary}
          title={surface === "work" ? "预览 Artifacts" : "查看本轮改动"}>
          <Icon name={surface === "work" ? "folder" : "edit"} size={13} />{
            surface === "work" ? `Artifacts · ${arr.length} 个文件` : `改动 ${arr.length} 个文件`
          }
        </button>
        <div className="turn-files-list">
          {arr.map((f) => {
            const markdown = surface !== "work" && isMarkdownPath(f) && !!onPreviewMarkdown;
            const canOpenFile = markdown || !!onGetDiff;
            return <button key={f} className={"turn-file-chip" + (markdown ? " markdown" : "")}
              disabled={!canOpenFile}
              onClick={() => markdown ? onPreviewMarkdown(f) : onGetDiff?.(f)}
              title={markdown ? `预览 ${f}` : `查看 ${f} 的 diff`}>
              <Icon name={markdown ? "read" : "edit"} size={12} />
              {f.split("/").pop()}
              {markdown && <span className="turn-file-action">预览</span>}
            </button>;
          })}
        </div>
      </div>
    );
  };

  const measuredVirtualItems = virtualizer.getVirtualItems();
  const renderedVirtualItems = measuredVirtualItems.length > 0
    ? measuredVirtualItems
    : turns.slice(-4).map((_, offset) => {
      const index = Math.max(0, turns.length - 4) + offset;
      return {
        index,
        key: turnKeySnapshot.getItemKey(index),
        start: historyTopInset
          + index * (HISTORY_VIRTUAL_ESTIMATE_PX + HISTORY_TURN_GAP_PX),
      };
    });

  if (turns.length === 0) {
    if (loading) {
      return (
        <div className="empty">
          <div className="spinner" aria-label="加载中" />
          <p className="loading-tx">加载会话历史…</p>
        </div>
      );
    }
    return (
      <div className="empty">
        <div className="glyph"><ClaudeMark size={30} /></div>
        <h2>{surface === "work" ? "工作区已就绪" : "已连接"}</h2>
        <p>{surface === "work"
          ? "添加资料并描述成果，我会把生成的文档和文件留在这项工作的私有目录。"
          : <>发一条消息开始，或用 <code>/</code> 唤起命令面板（Plan mode、review、技能…）。</>}</p>
      </div>
    );
  }

  return (
    <div className={surface === "work" ? "thread-shell work-thread-shell" : "thread-shell"}>
      <div className="thread" ref={scrollRef}
        data-detail-anchor-active={activeDetailAnchor ? "true" : "false"}
        data-text-selection-dragging={
          activeTextSelection?.dragging ? "true" : "false"
        }
        data-text-selection-retained={activeTextSelection ? "true" : "false"}
        onScroll={onScroll} onWheel={onWheel}
        onKeyDownCapture={onKeyDown}
        onPointerDown={onThreadPointerDown}
        onTouchStart={onTouchStart} onTouchMove={onTouchMove}
        onTouchEnd={clearTouch} onTouchCancel={clearTouch}>
        <div className="thread-in virtual-thread-in" style={{
          height: `${virtualizer.getTotalSize()}px`,
          position: "relative",
        }}>
          {canLoadOlder && (
            <div className="load-more-wrap virtual-history-loader">
              <button className="load-more-btn" onClick={doLoadMore}>
                加载更早的历史
              </button>
            </div>
          )}
          {canLoadNewer && (
            <div className="load-more-wrap virtual-history-loader"
              style={{ top: "auto", bottom: 0 }}>
              <button className="load-more-btn" onClick={doLoadNewer}>
                加载更新的历史
              </button>
            </div>
          )}
          {renderedVirtualItems.map((virtualItem) => {
            const t = turns[virtualItem.index];
            if (!t) return null;
            const ti = virtualItem.index;
            const timelineBlocks = t.detailProjection?.blocks ?? t.blocks;
            const activeProcess = hasActiveProcess(timelineBlocks);
            const finalBlocks = finalTextBlocks(t.blocks);
            const working = !t.done || activeProcess;
            const showProcessTimeline = timelineBlocks.length > 0
              || (!!t.detailEventCount && !t.detailLoaded);
            const workingLabel = t.progress
              ?? (activeProcess ? "处理中"
                : finalBlocks.length > 0 ? "回答中" : "思考中");
            const processOpenKey = `${scrollScope}\u0000turn:${t.id}`;
            const historyTurnId = t.historyTurnId ?? t.id;
            const historyImagesReady = !!t.imageRefs?.length
              && t.imageRefs.every((image) => (
                historyImageAssets?.[historyImageAssetKey(
                  historyTurnId, image.image_id, "thumbnail")
                ]?.status === "ready"
              ));
            if (historyImagesReady) {
              turnImagePreviewCacheRef.current.release(t.id);
            }
            return (
            <div className="turn" key={virtualItem.key}
              data-index={virtualItem.index} data-turn-id={t.id}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                transform: `translateY(${virtualItem.start}px)`,
              }}
              ref={(node) => {
                virtualizer.measureElement(node);
                if (node) {
                  turnNodeRefs.current.set(t.id, node);
                } else {
                  turnNodeRefs.current.delete(t.id);
                }
              }}>
            {(t.prompt || (t.images && t.images.length) || (t.imageRefs && t.imageRefs.length) || (t.files && t.files.length)) && (
              <div className="ubub-wrap">
                {t.prompt && <div className="ubub">{t.prompt}</div>}
                {t.images && t.images.length > 0 && (
                  <div className="ubub-imgs">
                    {t.images.map((img, i) => {
                      const src = `data:${img.media_type};base64,${img.data}`;
                      const [width, height] = queryImageDimensions(img)
                        ?? [180, 180];
                      return <button key={i} type="button" className="ubub-image-trigger"
                        style={{ aspectRatio: `${width} / ${height}` }}
                        aria-label="预览用户发送的图片"
                        onClick={() => setZoom({ kind: "data", src, alt: "用户发送的图片" })}>
                        <img src={src} className="ubub-img" width={width}
                          height={height} alt="用户发送的图片" />
                      </button>;
                    })}
                  </div>
                )}
                {(!t.images || t.images.length === 0)
                  && t.imageRefs && t.imageRefs.length > 0 && (
                  <div className="ubub-imgs">
                    {t.imageRefs.map((image, imageIndex) => {
                      const thumbnail = historyImageAssets?.[
                        historyImageAssetKey(
                          historyTurnId, image.image_id, "thumbnail")
                      ];
                      const fallback = turnImagePreviewCacheRef.current.get(
                        t.id, imageIndex);
                      return <HistoryUserImage key={image.image_id}
                        turnId={historyTurnId} imageId={image.image_id}
                        width={image.width} height={image.height}
                        asset={thumbnail} fallback={fallback}
                        onLoad={onLoadHistoryImage}
                        onPreview={() => {
                          if (fallback) {
                            setZoom({
                              kind: "data",
                              src: `data:${fallback.media_type};base64,${fallback.data}`,
                              alt: "用户发送的图片",
                            });
                            return;
                          }
                          onLoadHistoryImage?.(
                            historyTurnId, image.image_id, "full");
                          setZoom({
                            kind: "history", turnId: historyTurnId,
                            imageId: image.image_id, alt: "用户发送的图片",
                          });
                        }} />;
                    })}
                  </div>
                )}
                {t.files && t.files.length > 0 && (
                  <div className="ubub-files">
                    {t.files.map((f, i) => (
                      <span key={i} className="ubub-file"><Icon name="read" size={14} />{f.filename}</span>
                    ))}
                  </div>
                )}
                <div className="ubub-meta">
                  {t.ts && <span className="ubub-time">{formatTime(t.ts)}</span>}
                  {t.prompt && onEdit && <button className="ubub-act" onClick={() => onEdit(t.prompt!)} aria-label="编辑"><Icon name="edit" size={13} /></button>}
                  {t.prompt && <button className={"ubub-act" + (copiedId === t.id ? " copied" : "")} onClick={() => copyText(t.id, t.prompt!)} aria-label="复制"><Icon name="check" size={13} /></button>}
                </div>
              </div>
            )}
            {showProcessTimeline && (
              <ProcessTimeline blocks={timelineBlocks} done={t.done} engine={engine}
                durationMs={t.durationMs} startTs={t.ts} doneTs={t.doneTs}
                deferredCount={!t.detailLoaded ? t.detailEventCount : 0}
                detailLoading={t.detailLoading}
                onLoadDetail={onLoadDetail
                  ? () => requestProcessDetail(t.id, undefined, "initial")
                  : undefined}
                onOpenFile={onOpenFile} imageAssets={imageAssets}
                onLoadImage={onLoadImage}
                onInteractionStart={beginProcessInteraction}
                onInteractionEnd={endProcessInteraction}
                openOverride={processDisclosureOpen[`${processOpenKey}\u0000outer`]}
                onOpenChange={(open) => rememberProcessDisclosure(
                  `${processOpenKey}\u0000outer`, open,
                )}
                itemOpen={(key) =>
                  processDisclosureOpen[`${processOpenKey}\u0000${key}`]}
                onItemOpenChange={(key, open) => rememberProcessDisclosure(
                  `${processOpenKey}\u0000${key}`, open,
                )}
                onPreviewImage={(src, alt) => setZoom({ kind: "data", src, alt })} />
            )}
            {t.blocks.length > 0 && (
              <>
                {finalBlocks.map((block) => (
                  <MessageBlock key={block.message_id} text={block.text}
                    done={block.done} onOpenFile={onOpenFile}
                    imageAssets={imageAssets} onLoadImage={onLoadImage}
                    onPreviewImage={(src, alt) => setZoom({ kind: "data", src, alt })} />
                ))}
                {t.done && (
                  <>
                    <div className="ubub-meta ai-meta">
                      {t.doneTs && <span className="ubub-time">{formatTime(t.doneTs)}</span>}
                      {finalBlocks.length > 0 && <button
                        className={"ubub-act" + (copiedId === t.id + "-ai" ? " copied" : "")}
                        onClick={() => copyText(t.id + "-ai", aiText(t))}
                        aria-label="复制">
                        <Icon name="check" size={13} />
                      </button>}
                      {onFork && canForkTurn(engine, t) && (
                        <button className="ubub-act" aria-label="派生"
                          data-tooltip="从此回复派生新会话"
                          aria-busy={forkingPointId === t.forkPointId}
                          disabled={!!forkingPointId}
                          onClick={() => onFork(t.forkPointId)}>
                          <Icon name="branch" size={13} />
                        </button>
                      )}
                    </div>
                    {ti === turns.length - 1 && !working
                      && <div className="turn-done-mark"><ClaudeSpark size={22} /></div>}
                  </>
                )}
              </>
            )}
              {working && (
                <div className="turn-working" role="status" aria-live="polite">
                  <ClaudeWorking size={24} />
                  <span className="turn-working-tx">{workingLabel}</span>
                </div>
              )}
              {fileChips(t)}
              {t.interrupted && <div className="note interrupted">— 已打断 —</div>}
              {t.error && <div className="note interrupted">{
                presentHistoricalTurnProblem(t.error)
              }</div>}
            </div>
            );
          })}
        </div>
      </div>
      {(!scrollState.followOutput || !scrollState.nearBottom) && (
        <div className="scroll-bottom-wrap">
          <button className="scroll-bottom-btn" onClick={returnToLatest}
            style={browseMode ? {
              width: "auto", padding: "0 12px",
              gridAutoFlow: "column", gap: 5,
            } : undefined}
            aria-label={browseMode ? "回到最新" : "滚动到底部"}
            data-tooltip={browseMode ? "回到最新" : undefined}>
            <Icon name="chev" size={20} />
            {browseMode && <span style={{ fontSize: 12 }}>回到最新</span>}
          </button>
        </div>
      )}
      {zoom && (() => {
        const asset = zoom.kind === "history" ? historyImageAssets?.[
          historyImageAssetKey(zoom.turnId, zoom.imageId, "full")
        ] ?? historyImageAssets?.[
          historyImageAssetKey(zoom.turnId, zoom.imageId, "thumbnail")
        ] : null;
        const src = zoom.kind === "data" ? zoom.src
          : asset?.status === "ready" && asset.data && asset.mediaType
            ? `data:${asset.mediaType};base64,${asset.data}` : null;
        return src ? (
        <ImageLightbox key={sid ?? ""} src={src} alt={zoom.alt}
          onClose={() => setZoom(null)} />
        ) : null;
      })()}
    </div>
  );
}

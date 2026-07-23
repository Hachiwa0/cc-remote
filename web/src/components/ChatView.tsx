import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type TouchEvent,
  type WheelEvent,
} from "react";
import type { Turn } from "../reducer";
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
import {
  anchoredElementScrollTop,
  createFrameCoalescer,
  OlderHistoryLoadGate,
  shouldAutoLoadOlderHistory,
  ScrollFollowController,
  type FrameCoalescer,
  type ScrollFollowSnapshot,
  type ScrollMetrics,
} from "../scroll-follow";

interface HistoryAnchor {
  sid: string | null;
  anchorTurnId: string;
  anchorTop: number;
  scrollTop: number;
}

export const INITIAL_RENDER_TURNS = 24;
const RENDER_TURN_BATCH = 24;

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

function HistoryUserImage({ turnId, imageId, width, height, asset, onLoad,
  onPreview }: {
  turnId: string;
  imageId: string;
  width: number;
  height: number;
  asset?: HistoryImageAsset;
  onLoad?: (turnId: string, imageId: string, variant: HistoryImageVariant) => boolean;
  onPreview: () => void;
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (asset || !onLoad) return;
    const node = triggerRef.current;
    if (!node || typeof IntersectionObserver === "undefined") {
      onLoad(turnId, imageId, "thumbnail");
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      onLoad(turnId, imageId, "thumbnail");
      observer.disconnect();
    }, { rootMargin: "500px 0px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [asset, imageId, onLoad, turnId]);

  const src = asset?.status === "ready" && asset.data && asset.mediaType
    ? `data:${asset.mediaType};base64,${asset.data}` : null;
  return (
    <button ref={triggerRef} type="button"
      className="ubub-image-trigger history-image-trigger"
      style={{ aspectRatio: `${width} / ${height}` }}
      aria-label="预览用户发送的图片"
      disabled={!src}
      onClick={onPreview}>
      {src
        ? <img src={src} className="ubub-img" alt="用户发送的图片" />
        : <span className="history-image-placeholder" aria-hidden="true" />}
    </button>
  );
}

export function ChatView({ sid, turns, engine = "claude", loading, hasMore,
  onLoadMore, onLoadDetail, onEdit, onGetDiff, onOpenTurnDiff, onPreviewMarkdown, onOpenFile,
  onOpenArtifacts, onFork, forkingPointId, imageAssets, onLoadImage,
  historyImageAssets, onLoadHistoryImage,
  surface = "code" }: {
  sid: string | null;
  turns: Turn[];
  surface?: Space;
  engine?: "claude" | "codex";
  loading?: boolean;
  hasMore?: boolean;
  onLoadMore?: () => boolean;
  onLoadDetail?: (turnId: string) => void;
  onEdit: (prompt: string) => void;
  onGetDiff: (file: string) => void;
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
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const threadInRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<ScrollFollowController | null>(null);
  if (!controllerRef.current) controllerRef.current = new ScrollFollowController();
  const frameRef = useRef<FrameCoalescer | null>(null);
  if (!frameRef.current) {
    frameRef.current = createFrameCoalescer(
      (callback) => window.requestAnimationFrame(callback),
      (id) => window.cancelAnimationFrame(id),
    );
  }
  const [scrollState, setScrollState] = useState<ScrollFollowSnapshot>(() =>
    controllerRef.current!.snapshot());
  const [zoom, setZoom] = useState<
    | { kind: "data"; src: string; alt: string }
    | { kind: "history"; turnId: string; imageId: string; alt: string }
    | null
  >(null);
  const anchorRef = useRef<HistoryAnchor | null>(null);
  const historyLoadGateRef = useRef(new OlderHistoryLoadGate());
  const requestedOlderRef = useRef<{ sid: string | null; length: number } | null>(null);
  const autoLoadedBoundaryRef = useRef<string | null>(null);
  const lastScrollTopRef = useRef(0);
  const renderedSidRef = useRef<string | null | undefined>(undefined);
  const touchYRef = useRef<number | null>(null);
  const [renderWindow, setRenderWindow] = useState({
    sid, limit: INITIAL_RENDER_TURNS,
  });
  const renderLimit = renderWindow.sid === sid
    ? renderWindow.limit : INITIAL_RENDER_TURNS;
  const visibleTurns = turns.slice(-renderLimit);
  const canLoadOlder = turns.length > visibleTurns.length || !!hasMore;

  const captureHistoryAnchor = (el: HTMLDivElement): HistoryAnchor | null => {
    const viewportTop = el.getBoundingClientRect().top;
    for (const node of el.querySelectorAll<HTMLElement>("[data-turn-id]")) {
      const rect = node.getBoundingClientRect();
      if (rect.bottom < viewportTop) continue;
      const anchorTurnId = node.dataset.turnId;
      if (!anchorTurnId) continue;
      return {
        sid,
        anchorTurnId,
        anchorTop: rect.top,
        scrollTop: el.scrollTop,
      };
    }
    return null;
  };

  const syncScrollState = useCallback((next: ScrollFollowSnapshot) => {
    setScrollState((previous) =>
      previous.followOutput === next.followOutput && previous.nearBottom === next.nearBottom
        ? previous
        : next);
  }, []);

  const requestOutputFollow = useCallback(() => {
    frameRef.current?.schedule(() => {
      const el = scrollRef.current;
      const controller = controllerRef.current;
      if (!el || !controller) return;
      if (!controller.isFollowing()) {
        syncScrollState(controller.observeLayout(readScrollMetrics(el)));
        return;
      }
      // Streaming writes are immediate and coalesced once per frame. Smooth
      // scrolling is reserved for the user's explicit "bottom" button.
      el.scrollTop = el.scrollHeight;
      lastScrollTopRef.current = el.scrollTop;
      syncScrollState(controller.recordProgrammaticScroll(readScrollMetrics(el)));
    });
  }, [syncScrollState]);

  const pauseOutputFollow = useCallback(() => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    syncScrollState(controller.pause(readScrollMetrics(el)));
  }, [syncScrollState]);

  // Capture both dimensions and the first id. A streaming delta can arrive
  // while history is in flight; only an actual prepend should consume this
  // anchor and shift the viewport.
  const doLoadMore = (): boolean => {
    if (requestedOlderRef.current?.sid === sid) return false;
    const el = scrollRef.current;
    if (el) {
      anchorRef.current = captureHistoryAnchor(el);
      pauseOutputFollow();
    }
    if (turns.length > renderLimit) {
      setRenderWindow({
        sid,
        limit: Math.min(turns.length, renderLimit + RENDER_TURN_BATCH),
      });
      return true;
    }
    requestedOlderRef.current = { sid, length: turns.length };
    if (!onLoadMore?.()) {
      requestedOlderRef.current = null;
      anchorRef.current = null;
      return false;
    }
    return true;
  };

  // Scroll/touch events can repeat many times while a finger or wheel remains
  // pinned at the top. Trigger once for each visible oldest boundary; a newly
  // revealed local batch or server page changes the boundary and re-arms it.
  const maybeAutoLoadOlder = (
    movingTowardHistory: boolean,
    source: "touch" | "other",
  ) => {
    const el = scrollRef.current;
    if (!el || !shouldAutoLoadOlderHistory(
      readScrollMetrics(el), movingTowardHistory, canLoadOlder,
    )) return;
    const boundary = [
      sid ?? "", visibleTurns[0]?.id ?? "", renderLimit,
      turns.length, hasMore ? 1 : 0,
    ].join("\u0000");
    if (autoLoadedBoundaryRef.current === boundary) return;
    if (source === "touch" && !historyLoadGateRef.current.acquire()) return;
    if (doLoadMore()) {
      autoLoadedBoundaryRef.current = boundary;
    } else if (source === "touch") {
      historyLoadGateRef.current.complete();
    }
  };

  useLayoutEffect(() => {
    if (renderWindow.sid !== sid) {
      setRenderWindow({ sid, limit: INITIAL_RENDER_TURNS });
    }
    const requested = requestedOlderRef.current;
    if (requested?.sid === sid && turns.length > requested.length) {
      const added = turns.length - requested.length;
      requestedOlderRef.current = null;
      setRenderWindow((current) => ({
        sid,
        limit: Math.min(
          turns.length,
          (current.sid === sid ? current.limit : INITIAL_RENDER_TURNS) + added,
        ),
      }));
    } else if (requested?.sid === sid && !hasMore) {
      requestedOlderRef.current = null;
      anchorRef.current = null;
    }
    historyLoadGateRef.current.complete();
  }, [hasMore, renderWindow.sid, sid, turns.length]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;

    // Initial mount and every session switch are anchored synchronously before
    // paint, so the newly focused session opens at its latest content.
    if (renderedSidRef.current !== sid) {
      renderedSidRef.current = sid;
      anchorRef.current = null;
      touchYRef.current = null;
      frameRef.current?.cancel();
      el.scrollTop = el.scrollHeight;
      lastScrollTopRef.current = el.scrollTop;
      syncScrollState(controller.reset(readScrollMetrics(el)));
      return;
    }

    const anchor = anchorRef.current;
    const prepended = anchor
      && anchor.sid === sid
      && anchor.anchorTurnId !== (visibleTurns[0]?.id ?? null);
    if (prepended) {
      const node = Array.from(
        el.querySelectorAll<HTMLElement>("[data-turn-id]"),
      ).find((candidate) => candidate.dataset.turnId === anchor.anchorTurnId);
      if (node) {
        el.scrollTop = anchoredElementScrollTop(
          anchor.scrollTop, anchor.anchorTop, node.getBoundingClientRect().top,
        );
      }
      lastScrollTopRef.current = el.scrollTop;
      anchorRef.current = null;
      syncScrollState(controller.recordProgrammaticScroll(readScrollMetrics(el)));
    } else if (!controller.isFollowing()) {
      syncScrollState(controller.observeLayout(readScrollMetrics(el)));
    }

    if (controller.isFollowing()) requestOutputFollow();
  }, [requestOutputFollow, sid, syncScrollState, turns, visibleTurns]);

  useLayoutEffect(() => {
    const content = threadInRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const el = scrollRef.current;
      const controller = controllerRef.current;
      if (!el || !controller) return;
      if (controller.isFollowing()) requestOutputFollow();
      else syncScrollState(controller.observeLayout(readScrollMetrics(el)));
    });
    observer.observe(content);
    const viewport = scrollRef.current;
    if (viewport) observer.observe(viewport);
    return () => observer.disconnect();
  }, [requestOutputFollow, syncScrollState]);

  useEffect(() => {
    return () => frameRef.current?.cancel();
  }, []);

  useEffect(() => setZoom(null), [sid]);

  const onScroll = () => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    const metrics = readScrollMetrics(el);
    const movingTowardHistory = metrics.scrollTop < lastScrollTopRef.current - 0.5;
    lastScrollTopRef.current = metrics.scrollTop;
    syncScrollState(controller.observeScroll(metrics));
    maybeAutoLoadOlder(
      movingTowardHistory,
      touchYRef.current == null ? "other" : "touch",
    );
  };

  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    if (event.deltaY < 0) {
      pauseOutputFollow();
      maybeAutoLoadOlder(true, "other");
    }
  };

  const onTouchStart = (event: TouchEvent<HTMLDivElement>) => {
    historyLoadGateRef.current.beginGesture();
    touchYRef.current = event.touches[0]?.clientY ?? null;
  };

  const onTouchMove = (event: TouchEvent<HTMLDivElement>) => {
    const currentY = event.touches[0]?.clientY;
    const previousY = touchYRef.current;
    if (currentY == null || previousY == null) return;
    // A finger moving down scrolls the viewport toward earlier messages.
    if (currentY > previousY) {
      pauseOutputFollow();
      maybeAutoLoadOlder(true, "touch");
    }
    touchYRef.current = currentY;
  };

  const clearTouch = () => {
    touchYRef.current = null;
    historyLoadGateRef.current.endGesture();
  };

  const scrollToBottom = () => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    syncScrollState(controller.resume(readScrollMetrics(el)));
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  };

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
    const changes = collectTurnFileChanges(t.blocks);
    if (!changes.paths.length) return null;
    const arr = changes.paths;
    const openSummary = () => {
      if (surface !== "work") {
        if (changes.diff && onOpenTurnDiff) {
          onOpenTurnDiff(arr, changes.diff);
        } else if (arr.length === 1) {
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
          title={surface === "work" ? "预览 Artifacts" : "查看本轮改动"}>
          <Icon name={surface === "work" ? "folder" : "edit"} size={13} />{
            surface === "work" ? `Artifacts · ${arr.length} 个文件` : `改动 ${arr.length} 个文件`
          }
        </button>
        <div className="turn-files-list">
          {arr.map((f) => {
            const markdown = surface !== "work" && isMarkdownPath(f) && !!onPreviewMarkdown;
            return <button key={f} className={"turn-file-chip" + (markdown ? " markdown" : "")}
              onClick={() => markdown ? onPreviewMarkdown(f) : onGetDiff(f)}
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
      <div className="thread" ref={scrollRef} onScroll={onScroll} onWheel={onWheel}
        onTouchStart={onTouchStart} onTouchMove={onTouchMove}
        onTouchEnd={clearTouch} onTouchCancel={clearTouch}>
        <div className="thread-in" ref={threadInRef}>
          {canLoadOlder && (
            <div className="load-more-wrap">
              <button className="load-more-btn" onClick={doLoadMore}>{
                turns.length > visibleTurns.length
                  ? "显示更早的已加载消息" : "加载更早的历史"
              }</button>
            </div>
          )}
          {visibleTurns.map((t, ti) => {
            const activeProcess = hasActiveProcess(t.blocks);
            const finalBlocks = finalTextBlocks(t.blocks);
            const working = !t.done || activeProcess;
            const showProcessTimeline = t.blocks.length > 0
              || (!!t.detailEventCount && !t.detailLoaded);
            const workingLabel = t.progress
              ?? (activeProcess ? "处理中"
                : finalBlocks.length > 0 ? "回答中" : "思考中");
            return (
            <div className="turn" key={t.id} data-turn-id={t.id}>
            {(t.prompt || (t.images && t.images.length) || (t.imageRefs && t.imageRefs.length) || (t.files && t.files.length)) && (
              <div className="ubub-wrap">
                {t.prompt && <div className="ubub">{t.prompt}</div>}
                {t.images && t.images.length > 0 && (
                  <div className="ubub-imgs">
                    {t.images.map((img, i) => {
                      const src = `data:${img.media_type};base64,${img.data}`;
                      return <button key={i} type="button" className="ubub-image-trigger"
                        aria-label="预览用户发送的图片"
                        onClick={() => setZoom({ kind: "data", src, alt: "用户发送的图片" })}>
                        <img src={src} className="ubub-img" alt="用户发送的图片" />
                      </button>;
                    })}
                  </div>
                )}
                {t.imageRefs && t.imageRefs.length > 0 && (
                  <div className="ubub-imgs">
                    {t.imageRefs.map((image) => {
                      const thumbnail = historyImageAssets?.[
                        historyImageAssetKey(t.id, image.image_id, "thumbnail")
                      ];
                      return <HistoryUserImage key={image.image_id}
                        turnId={t.id} imageId={image.image_id}
                        width={image.width} height={image.height}
                        asset={thumbnail} onLoad={onLoadHistoryImage}
                        onPreview={() => {
                          onLoadHistoryImage?.(t.id, image.image_id, "full");
                          setZoom({
                            kind: "history", turnId: t.id,
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
                  {t.prompt && <button className="ubub-act" onClick={() => onEdit(t.prompt!)} aria-label="编辑"><Icon name="edit" size={13} /></button>}
                  {t.prompt && <button className={"ubub-act" + (copiedId === t.id ? " copied" : "")} onClick={() => copyText(t.id, t.prompt!)} aria-label="复制"><Icon name="check" size={13} /></button>}
                </div>
              </div>
            )}
            {showProcessTimeline && (
              <ProcessTimeline blocks={t.blocks} done={t.done} engine={engine}
                durationMs={t.durationMs} startTs={t.ts}
                deferredCount={!t.detailLoaded ? t.detailEventCount : 0}
                detailLoading={t.detailLoading}
                onLoadDetail={onLoadDetail ? () => onLoadDetail(t.id) : undefined}
                onOpenFile={onOpenFile} imageAssets={imageAssets}
                onLoadImage={onLoadImage}
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
                    {ti === visibleTurns.length - 1 && !working
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
          <button className="scroll-bottom-btn" onClick={scrollToBottom} aria-label="滚动到底部">
            <Icon name="chev" size={20} />
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

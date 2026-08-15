import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";

import { resolvedEngineOptions } from "../engine-picker-options";
import { Icon } from "../icons";
import type { Engine, EngineInfo } from "../protocol";

const ENGINE_MARKS: Record<Engine, string> = {
  claude: "✳",
  codex: "◇",
  dsh: "◆",
};

const ENGINE_SHORT_NAMES: Record<Engine, string> = {
  claude: "Claude",
  codex: "Codex",
  dsh: "DSH",
};

const ENGINE_DESCRIPTIONS: Record<Engine, string> = {
  claude: "Claude Agent SDK · Code / Work",
  codex: "Codex app-server · Code / Work",
  dsh: "本机 DSH Web · Code",
};

interface Props {
  engine: Engine;
  engines: readonly EngineInfo[];
  onSelect: (engine: Engine) => void;
}

interface Position {
  top: number;
  right: number;
}

export function EnginePicker({ engine, engines, onSelect }: Props) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const cardRef = useRef<HTMLElement>(null);
  const optionRefs = useRef<Partial<Record<Engine, HTMLButtonElement>>>({});
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<Position>({ top: 58, right: 8 });
  const options = useMemo(() => resolvedEngineOptions(engines), [engines]);

  const close = () => setOpen(false);
  const show = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      setPosition({
        top: Math.min(rect.bottom + 8, window.innerHeight - 24),
        right: Math.max(8, window.innerWidth - rect.right),
      });
    }
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const focusFrame = window.requestAnimationFrame(() => {
      const target = options.find((candidate) => (
        candidate.id === engine && candidate.available
      )) ?? options.find((candidate) => candidate.available);
      if (target) optionRefs.current[target.id]?.focus();
    });
    const trigger = triggerRef.current;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", close);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", close);
      if (trigger && document.contains(trigger)) trigger.focus();
    };
  }, [engine, open, options]);

  const style = {
    "--engine-picker-top": `${position.top}px`,
    "--engine-picker-right": `${position.right}px`,
  } as CSSProperties;

  return (
    <>
      <button ref={triggerRef} type="button"
        className={`engine-toggle engine-${engine}${open ? " open" : ""}`}
        onClick={() => open ? close() : show()}
        aria-label="选择会话引擎" aria-haspopup="menu" aria-expanded={open}
        title="选择 Claude、Codex 或 DeepSeek Harness">
        <span aria-hidden="true">{ENGINE_MARKS[engine]}</span>
        <span>{ENGINE_SHORT_NAMES[engine]}</span>
        <Icon name="chev" size={13} />
      </button>
      {open && typeof document !== "undefined" && createPortal(
        <div className="engine-picker-scrim" data-lock-horizontal-swipe
          onClick={(event) => {
            if (event.target === event.currentTarget) close();
          }}>
          <section ref={cardRef} className="engine-picker-card" style={style}
            role="menu" aria-label="选择会话引擎"
            onPointerDown={(event) => event.stopPropagation()}>
            <header className="engine-picker-title">
              <b>会话引擎</b>
              <small>每个引擎保留上次打开的 Code / Work 位置</small>
            </header>
            <div className="engine-picker-options">
              {options.map((candidate) => {
                const selected = candidate.id === engine;
                return (
                  <button key={candidate.id} type="button"
                    ref={(node) => {
                      optionRefs.current[candidate.id] = node ?? undefined;
                    }}
                    className={`engine-picker-option engine-${candidate.id}${selected ? " selected" : ""}`}
                    role="menuitemradio" aria-checked={selected}
                    disabled={!candidate.available}
                    onClick={() => {
                      close();
                      if (!selected) onSelect(candidate.id);
                    }}>
                    <span className="engine-picker-mark" aria-hidden="true">
                      {ENGINE_MARKS[candidate.id]}
                    </span>
                    <span className="engine-picker-copy">
                      <b>{candidate.display_name}</b>
                      <small>{candidate.available
                        ? ENGINE_DESCRIPTIONS[candidate.id]
                        : candidate.reason || "当前不可用"}</small>
                    </span>
                    <span className={`engine-picker-state${candidate.available ? " available" : ""}`}>
                      {selected && candidate.available
                        ? <Icon name="check" size={16} />
                        : candidate.available ? <i aria-label="可用" /> : "离线"}
                    </span>
                  </button>
                );
              })}
            </div>
          </section>
        </div>,
        document.body,
      )}
    </>
  );
}

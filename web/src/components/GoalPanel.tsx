import { useEffect, useState } from "react";
import type { GoalStatus, ThreadGoal } from "../protocol";

interface Props {
  goal: ThreadGoal | null;
  onSave: (objective: string | null, status: GoalStatus | null, tokenBudget: number | null) => void;
  onClear: () => void;
}

const statusName: Record<GoalStatus, string> = {
  active: "进行中", paused: "已暂停", blocked: "受阻",
  usageLimited: "用量受限", budgetLimited: "预算已满", complete: "已完成",
};

export function GoalPanel({ goal, onSave, onClear }: Props) {
  const [open, setOpen] = useState(false);
  const [objective, setObjective] = useState("");
  const [status, setStatus] = useState<GoalStatus>("active");
  const [budget, setBudget] = useState("");
  useEffect(() => {
    setObjective(goal?.objective ?? "");
    setStatus(goal?.status ?? "active");
    setBudget(goal?.tokenBudget ? String(goal.tokenBudget) : "");
  }, [goal]);
  const used = goal?.tokensUsed ?? 0;
  const total = goal?.tokenBudget ?? null;
  return <>
    <button className={"goal-bar" + (goal ? " has-goal" : "")} onClick={() => setOpen(true)}>
      <span className="goal-mark">◎</span>
      {goal ? <>
        <span className="goal-objective">{goal.objective}</span>
        <span className="goal-meta">{statusName[goal.status]} · {used.toLocaleString()}{total ? ` / ${total.toLocaleString()} tokens` : " tokens"} · {Math.floor(goal.timeUsedSeconds / 60)} 分钟</span>
      </> : <span className="goal-empty">设置 Codex Goal</span>}
    </button>
    {open && <>
      <div className="scrim show" onClick={() => setOpen(false)} />
      <div className="sheet show goal-sheet" role="dialog" aria-label="Codex Goal">
        <div className="sheet-grip" />
        <div className="sheet-title">Codex Goal</div>
        <label>目标<textarea value={objective} maxLength={16384} onChange={(e) => setObjective(e.target.value)} placeholder="这个任务最终要达成什么？" /></label>
        <label>状态<select value={status} onChange={(e) => setStatus(e.target.value as GoalStatus)}>{Object.entries(statusName).map(([v, n]) => <option key={v} value={v}>{n}</option>)}</select></label>
        <label>Token 预算（可选）<input type="number" min="1" value={budget} onChange={(e) => setBudget(e.target.value)} /></label>
        <div className="goal-actions">
          {goal && <button className="goal-clear" onClick={() => { onClear(); setOpen(false); }}>清除 Goal</button>}
          <button onClick={() => setOpen(false)}>取消</button>
          <button className="goal-save" disabled={!objective.trim()} onClick={() => {
            onSave(objective.trim(), status, budget ? Number(budget) : null); setOpen(false);
          }}>保存</button>
        </div>
      </div>
    </>}
  </>;
}

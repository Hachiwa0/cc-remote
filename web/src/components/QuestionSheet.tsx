import { useState } from "react";
import type { AskOption } from "../protocol";
import { Icon } from "../icons";

interface Props {
  question: string;
  header?: string | null;
  options: AskOption[];
  allowText?: boolean;
  secret?: boolean;
  onAnswer: (answer: string) => void;
}

export function QuestionSheet({ question, header, options, allowText, secret, onAnswer }: Props) {
  const [text, setText] = useState("");
  return (
    <>
      <div className="scrim show" />
      <div className="sheet show" role="dialog" aria-label="操作确认">
        <div className="sheet-grip" />
        <div className="sheet-title">
          <span className="qa-ic"><Icon name="spark" size={15} /></span>
          {header || "助手想确认一下"}
        </div>
        <div className="sheet-scroll">
          <div className="qa-question">{question}</div>
          <div className="qa-options">
            {options.map((o, i) => (
              <button key={i} className="qa-opt" onClick={() => onAnswer(o.label)}>
                <span className="qa-opt-label">{o.label}</span>
                {o.ds && <span className="qa-opt-ds">{o.ds}</span>}
              </button>
            ))}
          </div>
          {allowText && <div className="qa-text-answer">
            <input type={secret ? "password" : "text"} value={text}
              autoFocus={options.length === 0} placeholder={secret ? "输入敏感内容" : "输入回答"}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && text.trim()) onAnswer(text); }} />
            <button disabled={!text.trim()} onClick={() => onAnswer(text)}>确定</button>
          </div>}
        </div>
      </div>
    </>
  );
}

import type { AskOption } from "../protocol";
import { Icon } from "../icons";

interface Props {
  question: string;
  options: AskOption[];
  onAnswer: (answer: string) => void;
}

export function QuestionSheet({ question, options, onAnswer }: Props) {
  return (
    <>
      <div className="scrim show" />
      <div className="sheet show" role="dialog" aria-label="Claude 有个问题">
        <div className="sheet-grip" />
        <div className="sheet-title">
          <span className="qa-ic"><Icon name="spark" size={15} /></span>
          Claude 想确认一下
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
        </div>
      </div>
    </>
  );
}

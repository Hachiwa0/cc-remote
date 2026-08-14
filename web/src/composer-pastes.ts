export const MAX_PROMPT_CHARS = 2 * 1024 * 1024;
export const LONG_PASTE_THRESHOLD = 1000;

export interface ComposerPaste {
  id: string;
  text: string;
  chars: number;
  lines: number;
}

export type PasteText = Pick<ComposerPaste, "text">;

export function makeComposerPaste(text: string, id: string): ComposerPaste {
  return { id, text, chars: text.length, lines: text.split("\n").length };
}

export type ComposedPastePrompt =
  | { ok: true; prompt: string }
  | { ok: false; chars: number; maxChars: number };

/**
 * Long-paste cards are ordered message prefixes. Size the exact joined prompt
 * before allocating it so an oversized private draft stays intact and never
 * reaches the reliable command outbox as a frame the protocol will reject.
 */
export function composePastePrompt(
  pastes: readonly PasteText[],
  input: string,
): ComposedPastePrompt {
  const parts = [...pastes.map((paste) => paste.text), input]
    .filter((part) => part.length > 0);
  const chars = parts.reduce((total, part) => total + part.length, 0)
    + Math.max(0, parts.length - 1) * 2;
  if (chars > MAX_PROMPT_CHARS) {
    return { ok: false, chars, maxChars: MAX_PROMPT_CHARS };
  }
  return { ok: true, prompt: parts.join("\n\n") };
}

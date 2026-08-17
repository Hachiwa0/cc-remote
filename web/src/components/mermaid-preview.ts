import { createContext } from "react";
import type { MermaidTheme } from "../mermaid";

export interface MermaidPreviewPayload {
  svg: string;
  source: string;
  theme: MermaidTheme;
}

export function activeMermaidTheme(): MermaidTheme {
  if (typeof document === "undefined") return "light";
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

export const MermaidPreviewContext = createContext<(
  (preview: MermaidPreviewPayload) => void
) | undefined>(undefined);

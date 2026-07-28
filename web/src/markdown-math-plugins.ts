import "katex/dist/katex.min.css";

import type { Options as ReactMarkdownOptions } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

type RemarkPlugins = NonNullable<ReactMarkdownOptions["remarkPlugins"]>;
type RehypePlugins = NonNullable<ReactMarkdownOptions["rehypePlugins"]>;

export const remarkPlugins: RemarkPlugins = [
  remarkGfm,
  [remarkMath, { singleDollarTextMath: true }],
];

export const rehypePlugins: RehypePlugins = [
  [rehypeKatex, {
    trust: false,
    strict: "warn",
    maxSize: 10,
    maxExpand: 1_000,
    output: "htmlAndMathml",
  }],
];

export interface CompletionReceipt {
  main: boolean;
  btwSids: string[];
}

export type CompletionReceipts = Record<string, CompletionReceipt>;
export type CompletionBadgeKind = "main" | "btw" | "both";

export function markCompletionUnread(
  current: CompletionReceipts,
  parentSid: string,
  sourceSid: string,
  source: "main" | "btw",
): CompletionReceipts {
  const prior = current[parentSid] ?? { main: false, btwSids: [] };
  if (source === "main") {
    if (prior.main) return current;
    return { ...current, [parentSid]: { ...prior, main: true } };
  }
  if (prior.btwSids.includes(sourceSid)) return current;
  return {
    ...current,
    [parentSid]: {
      ...prior,
      btwSids: [...prior.btwSids, sourceSid],
    },
  };
}

export function acknowledgeCompletion(
  current: CompletionReceipts,
  parentSid: string,
  options: { main?: boolean; btwSid?: string },
): CompletionReceipts {
  const prior = current[parentSid];
  if (!prior) return current;
  const main = options.main ? false : prior.main;
  const btwSids = options.btwSid
    ? prior.btwSids.filter((sid) => sid !== options.btwSid)
    : prior.btwSids;
  if (main === prior.main && btwSids.length === prior.btwSids.length) {
    return current;
  }
  const next = { ...current };
  if (!main && btwSids.length === 0) delete next[parentSid];
  else next[parentSid] = { main, btwSids };
  return next;
}

export function completionBadgeKind(
  receipt: CompletionReceipt | undefined,
): CompletionBadgeKind | undefined {
  if (!receipt) return undefined;
  if (receipt.main && receipt.btwSids.length > 0) return "both";
  if (receipt.main) return "main";
  if (receipt.btwSids.length > 0) return "btw";
  return undefined;
}

export function rekeyCompletionReceipts(
  current: CompletionReceipts,
  oldSid: string,
  newSid: string,
): CompletionReceipts {
  if (oldSid === newSid || !current[oldSid]) return current;
  const source = current[oldSid];
  const target = current[newSid];
  const next = { ...current };
  delete next[oldSid];
  next[newSid] = target ? {
    main: target.main || source.main,
    btwSids: Array.from(new Set([...target.btwSids, ...source.btwSids])),
  } : source;
  return next;
}

export function discardBtwCompletionReceipts(
  current: CompletionReceipts,
): CompletionReceipts {
  let changed = false;
  const next: CompletionReceipts = {};
  for (const [parentSid, receipt] of Object.entries(current)) {
    if (receipt.btwSids.length > 0) changed = true;
    if (receipt.main) next[parentSid] = { main: true, btwSids: [] };
  }
  return changed ? next : current;
}

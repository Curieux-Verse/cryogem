// The Layer 2 blocks, in one place, so the Screen table and the asset page
// cannot disagree about which blocks exist, what they are called, or how a
// missing one is described.

import type { BlockKey, Blocks } from "./types";

/** Order and labels. Momentum (D-077) sits after supply, matching the weight
 *  table in config/thresholds.yaml. */
export const BLOCKS: readonly { key: BlockKey; short: string; label: string }[] = [
  { key: "fundamental", short: "Fund", label: "Fundamental" },
  { key: "supply", short: "Supp", label: "Supply" },
  { key: "momentum", short: "Mom", label: "Momentum" },
  { key: "sector", short: "Sect", label: "Sector" },
  { key: "events", short: "Evnt", label: "Events" },
  { key: "attention", short: "Attn", label: "Attention" },
  { key: "drawdown", short: "DD", label: "Drawdown" },
];

/** How one block stands for one asset.
 *
 *  - `scored`: a number.
 *  - `missing`: the block was live this run but this asset had no reading.
 *    Under gem-v2 (D-075) that earns 0 of the block's weight; under the old
 *    method its weight was redistributed. Either way it renders as a dash,
 *    never as a zero bar.
 *  - `dark`: no source for ANY asset this run (not in `live_blocks`). It drops
 *    out for everyone, so no asset gains on another from it.
 *  - `absent`: the block did not exist in the score version that wrote the
 *    file (momentum in a pre-gem-v2 file). */
export type BlockStatus = "scored" | "missing" | "dark" | "absent";

export function blockStatus(
  key: BlockKey,
  blocks: Blocks,
  liveBlocks: readonly string[] | null | undefined,
): BlockStatus {
  const value = blocks[key];
  if (liveBlocks) {
    if (!liveBlocks.includes(key)) return "dark";
    return value === null || value === undefined || Number.isNaN(value) ? "missing" : "scored";
  }
  // A file written before live_blocks existed.
  if (value === undefined) return "absent";
  return value === null || Number.isNaN(value) ? "missing" : "scored";
}

export interface CoverageSummary {
  /** Blocks with a number among those that could have one. */
  measured: number;
  /** Blocks that were live (or, for old files, present) this run. */
  of: number;
  /** Weight measured / weight live, 0-1, or null when the file predates it. */
  weight: number | null;
}

export function coverageSummary(
  blocks: Blocks,
  liveBlocks: readonly string[] | null | undefined,
  coverage: number | null | undefined,
): CoverageSummary {
  let measured = 0;
  let of = 0;
  for (const { key } of BLOCKS) {
    const status = blockStatus(key, blocks, liveBlocks);
    if (status === "dark" || status === "absent") continue;
    of += 1;
    if (status === "scored") measured += 1;
  }
  return {
    measured,
    of,
    weight: typeof coverage === "number" && Number.isFinite(coverage) ? coverage : null,
  };
}

/** "4/6 blocks · 82% of weight measured". */
export function coverageText(summary: CoverageSummary): string {
  const blocks = `${summary.measured}/${summary.of} blocks`;
  return summary.weight === null
    ? blocks
    : `${blocks} · ${Math.round(summary.weight * 100)}% of weight measured`;
}

/** Compact form for a table cell: "4/6 · 82%". */
export function coverageShort(summary: CoverageSummary): string {
  const blocks = `${summary.measured}/${summary.of}`;
  return summary.weight === null ? blocks : `${blocks} · ${Math.round(summary.weight * 100)}%`;
}

export const MISSING_SCORES_NOTHING =
  "Missing data scores nothing (D-075): a live block with no reading for an asset counts as 0, so a coin is never lifted by what was not measured. A block marked dark had no source for any asset this run and drops out for everyone equally. Coverage says how much of the live weight was actually measured.";

export const MISSING_REDISTRIBUTED =
  "A blank block had no data for that asset, and its weight was redistributed across the blocks that did rather than counted as zero.";

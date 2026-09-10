// Formatting helpers.
//
// One rule runs through all of them: a missing value renders as an em dash and
// NEVER as a zero. A screener whose blanks look like zeroes develops a silent
// bias against everything with incomplete data, and the whole point of the L2
// renormalisation is that it does not.

import type { MaybeInfinite } from "./types";

export const DASH = "\u2014";

export function num(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return value.toFixed(digits);
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return `${(value * 100).toFixed(digits)}%`;
}

export function signedPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  const shown = (value * 100).toFixed(digits);
  return value > 0 ? `+${shown}%` : `${shown}%`;
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  const abs = Math.abs(value);
  if (abs >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `$${(value / 1e3).toFixed(0)}K`;
  return `$${value.toFixed(2)}`;
}

export function price(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  if (value >= 1) return `$${value.toFixed(2)}`;
  // Sub-dollar assets need significant figures, not fixed decimals: $0.00 is
  // not a price, and most of this universe trades below a dollar.
  return `$${value.toPrecision(4)}`;
}

/** Infinity arrives as a string, on purpose (DECISIONS D-012). */
export function metric(value: MaybeInfinite, digits = 4): string {
  if (value === null) return DASH;
  if (value === "Infinity") return "\u221e";
  if (value === "-Infinity") return "-\u221e";
  if (Math.abs(value) >= 1e6 || (value !== 0 && Math.abs(value) < 1e-4)) {
    return value.toExponential(2);
  }
  return value.toFixed(digits);
}

export function compactInt(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  return value.toLocaleString("en-US");
}

/** A check id like L1_PERP_SPOT rendered for a heading. */
export function checkLabel(id: string): string {
  return id.replace(/^L[123]_/, "").replace(/_/g, " ").toLowerCase();
}

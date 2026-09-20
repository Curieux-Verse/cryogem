// Chips and sparklines for the Pulse tab. Inline SVG only: no chart library.
//
// Colour is never the only signal. Every chip carries its state in words, and
// every delta carries an arrow, so the page reads the same in greyscale and to
// a screen reader.

import { num } from "../lib/format";

// -- structure state chips (contract.STATES) ------------------------------------

const STATE_STYLE: Record<string, { text: string; className: string; tip: string }> = {
  bull_break: {
    text: "bull break",
    className: "border-pass bg-pass/25 text-pass font-semibold",
    tip: "Closed above a descending trendline within the last few bars: a fresh bullish break.",
  },
  bull_trend: {
    text: "bull trend",
    className: "border-pass/50 text-pass",
    tip: "Fast EMA above slow EMA, both rising, close above the fast EMA.",
  },
  neutral: {
    text: "neutral",
    className: "border-line text-muted",
    tip: "No trend and no recent break.",
  },
  bear_trend: {
    text: "bear trend",
    className: "border-fail/50 text-fail",
    tip: "Fast EMA below slow EMA, both falling, close below the fast EMA.",
  },
  bear_break: {
    text: "bear break",
    className: "border-fail bg-fail/25 text-fail font-semibold",
    tip: "Closed below an ascending support line within the last few bars: a fresh bearish break.",
  },
};

export function StateChip({
  tf,
  state,
  barsSince,
}: {
  tf: "4H" | "1H";
  state: string | null | undefined;
  barsSince?: number | null;
}) {
  if (!state) {
    return (
      <span className="border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted/70">
        {tf} —
      </span>
    );
  }
  const style = STATE_STYLE[state] ?? {
    text: state.replace(/_/g, " "),
    className: "border-line text-muted",
    tip: "Unrecognised state.",
  };
  const since =
    typeof barsSince === "number" && (state === "bull_break" || state === "bear_break")
      ? ` ${barsSince}b`
      : "";
  const tip = `${tf} structure: ${style.text}. ${style.tip}${
    since ? ` ${barsSince} bar${barsSince === 1 ? "" : "s"} since the break.` : ""
  }`;
  return (
    <span
      className={`whitespace-nowrap border px-1.5 py-0.5 font-mono text-[10px] uppercase ${style.className}`}
      title={tip}
      aria-label={tip}
    >
      {tf} {style.text}
      {since}
    </span>
  );
}

// -- OI quadrant chips (contract.OI_QUADRANTS; D-074: a conditioner only) --------

export const OI_EXPLAIN: Record<string, { text: string; className: string; tip: string }> = {
  confirm_long: {
    text: "OI confirms long",
    className: "border-pass/50 text-pass",
    tip: "Price up, open interest up, taker flow buying: new positions agree with the move.",
  },
  short_covering: {
    text: "short covering",
    className: "border-line text-ink",
    tip: "Price up while open interest falls: shorts closing. The rise may not have new buyers behind it.",
  },
  new_shorts: {
    text: "new shorts",
    className: "border-fail/50 text-fail",
    tip: "Price down, open interest up, taker flow selling: new shorts are being opened.",
  },
  long_liquidation: {
    text: "long liquidation",
    className: "border-fail/50 text-fail",
    tip: "Price down while open interest falls: longs exiting. Can mark exhaustion, but is not a bottom signal.",
  },
  leverage_build: {
    text: "leverage build",
    className: "border-amber-400/60 text-amber-300",
    tip: "Price flat while open interest rises a lot: leverage is building without a move, which tends to resolve violently.",
  },
  neutral: {
    text: "OI neutral",
    className: "border-line text-muted",
    tip: "No clear open-interest reading against price and flow.",
  },
};

export const OI_CONDITIONER_NOTE =
  "Open interest is a conditioner, never a buy signal on its own: it can confirm or question a move that price and flow already show.";

export function OiChip({ quadrant }: { quadrant: string | null | undefined }) {
  if (!quadrant) {
    return (
      <span className="border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted/70">
        OI —
      </span>
    );
  }
  const style = OI_EXPLAIN[quadrant] ?? {
    text: quadrant.replace(/_/g, " "),
    className: "border-line text-muted",
    tip: "Unrecognised open-interest reading.",
  };
  const tip = `OI 24H: ${style.text}. ${style.tip} ${OI_CONDITIONER_NOTE}`;
  return (
    <span
      className={`whitespace-nowrap border px-1.5 py-0.5 font-mono text-[10px] uppercase ${style.className}`}
      title={tip}
      aria-label={tip}
    >
      {style.text}
    </span>
  );
}

// -- risk flags (contract.RISK_FLAGS) ----------------------------------------------

export const FLAG_EXPLAIN: Record<string, { text: string; tip: string }> = {
  crowding: {
    text: "crowding",
    tip: "Funding and the 24h OI change are both at this asset's own extremes: a crowded trade. Pulse is multiplied by 0.7.",
  },
  leverage_no_move: {
    text: "leverage, no move",
    tip: "The 24h OI change is extreme while price barely moved: leverage without a move. Pulse is multiplied by 0.8.",
  },
};

export function RiskChip({ flag }: { flag: string }) {
  const style = FLAG_EXPLAIN[flag] ?? { text: flag.replace(/_/g, " "), tip: "Risk flag." };
  const tip = `Risk flag: ${style.text}. ${style.tip}`;
  return (
    <span
      className="whitespace-nowrap border border-fail/60 px-1.5 py-0.5 font-mono text-[10px] uppercase text-fail"
      title={tip}
      aria-label={tip}
    >
      ⚠ {style.text}
    </span>
  );
}

// -- journal trigger chips (D-081) -------------------------------------------------

/** What wrote a Pulse journal row. Three distinct things, and the control is
 *  the one a reader must never mistake for a signal: it is the random
 *  comparison the other two are measured against. */
export const TRIGGER_EXPLAIN: Record<string, { text: string; className: string; tip: string }> = {
  aligned: {
    text: "entered aligned",
    className: "border-pass bg-pass/20 text-pass font-semibold",
    tip: "The asset ENTERED the ALIGNED set this hour: Gem rank ≤ 25, Pulse ≥ 70, a bullish 4H structure and no risk flag. Staying aligned is not an event.",
  },
  top10: {
    text: "entered top 10",
    className: "border-ink/60 text-ink",
    tip: "The asset ENTERED the Pulse top 10 this hour, compared with the previous scored hour. Staying in the top 10 is not an event.",
  },
  control: {
    text: "control · random",
    className: "border-dashed border-muted text-muted",
    tip: "The random comparison: one scored survivor per hour that did NOT trigger, drawn at random. Without it a positive return says nothing, because everything may simply have gone up.",
  },
};

export function TriggerChip({ trigger }: { trigger: string }) {
  const style = TRIGGER_EXPLAIN[trigger] ?? {
    text: trigger.replace(/_/g, " "),
    className: "border-line text-muted",
    tip: "Unrecognised trigger.",
  };
  return (
    <span
      className={`whitespace-nowrap border px-1.5 py-0.5 font-mono text-[10px] uppercase ${style.className}`}
      title={style.tip}
      aria-label={`${style.text}. ${style.tip}`}
    >
      {style.text}
    </span>
  );
}

// -- score delta ----------------------------------------------------------------------

export function Delta({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return (
      <span className="font-mono text-xs text-muted" title="No score at the previous hour">
        —<span className="sr-only">no previous hour</span>
      </span>
    );
  }
  if (Math.abs(value) < 0.05) {
    return (
      <span className="whitespace-nowrap font-mono text-xs text-muted" aria-label="unchanged">
        ±0.0
      </span>
    );
  }
  const up = value > 0;
  return (
    <span
      className={`whitespace-nowrap font-mono text-xs ${up ? "text-pass" : "text-fail"}`}
      aria-label={`${up ? "up" : "down"} ${num(Math.abs(value))} points since the previous hour`}
    >
      {up ? "▲" : "▼"} {num(Math.abs(value))}
    </span>
  );
}

// -- sparklines -----------------------------------------------------------------------

function finite(values: (number | null | undefined)[] | undefined): number[] {
  return (values ?? []).filter(
    (v): v is number => typeof v === "number" && Number.isFinite(v),
  );
}

function pathFor(values: (number | null | undefined)[], width: number, height: number) {
  const nums = finite(values);
  if (nums.length < 2) return null;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const step = width / Math.max(1, values.length - 1);
  let d = "";
  let pen = false;
  values.forEach((v, i) => {
    if (typeof v !== "number" || !Number.isFinite(v)) {
      pen = false; // a gap stays a gap: never interpolate a missing bar
      return;
    }
    const x = i * step;
    const y = height - 1 - ((v - min) / span) * (height - 2);
    d += `${pen ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)} `;
    pen = true;
  });
  return { d: d.trim(), first: nums[0] as number, last: nums[nums.length - 1] as number };
}

function changeText(first: number, last: number): string {
  if (first === 0) return "";
  const change = (last / first - 1) * 100;
  return `${change >= 0 ? "+" : ""}${change.toFixed(1)}%`;
}

/** A close-price sparkline, optionally with per-bar taker flow drawn as bars
 *  beneath it (green = buy-dominant, red = sell-dominant, height = |flow|). */
export function Spark({
  label,
  closes,
  flow,
  width = 96,
}: {
  label: string;
  closes: (number | null)[] | undefined;
  flow?: (number | null)[] | undefined;
  width?: number;
}) {
  const height = 22;
  const line = pathFor(closes ?? [], width, height);
  if (!line) {
    return (
      <span className="font-mono text-[10px] text-muted" title={`${label}: not enough bars`}>
        no {label} bars
      </span>
    );
  }
  const up = line.last >= line.first;
  const flows = flow ?? [];
  const flowNums = finite(flows);
  const buyBars = flowNums.filter((f) => f > 0).length;
  const flowHeight = 10;
  const barW = width / Math.max(1, flows.length);
  // Per-bar flow rarely leaves ±0.2, so bars scale to this series' largest
  // reading (never below 0.1, so a quiet series still looks quiet).
  const flowScale = Math.max(0.1, ...flowNums.map((f) => Math.min(1, Math.abs(f))));
  const change = changeText(line.first, line.last);
  const aria = `${label}: ${finite(closes).length} closes, ${change || "flat"}${
    flowNums.length
      ? `; taker flow buy-dominant in ${buyBars} of ${flowNums.length} bars`
      : ""
  }`;

  return (
    <span className="inline-flex flex-col" role="img" aria-label={aria} title={aria}>
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
        <path
          d={line.d}
          fill="none"
          stroke={up ? "#5FB49C" : "#C2664D"}
          strokeWidth="1.25"
          strokeLinejoin="round"
        />
      </svg>
      {flowNums.length ? (
        <svg
          width={width}
          height={flowHeight}
          viewBox={`0 0 ${width} ${flowHeight}`}
          aria-hidden="true"
          className="mt-px"
        >
          <line x1="0" x2={width} y1={flowHeight / 2} y2={flowHeight / 2} stroke="#2A2F3C" strokeWidth="0.5" />
          {flows.map((f, i) => {
            if (typeof f !== "number" || !Number.isFinite(f) || f === 0) return null;
            const clamped = Math.max(-1, Math.min(1, f));
            const h = Math.max(0.5, (Math.abs(clamped) / flowScale) * (flowHeight / 2));
            return (
              <rect
                key={i}
                x={i * barW + barW * 0.15}
                width={Math.max(0.5, barW * 0.7)}
                y={clamped > 0 ? flowHeight / 2 - h : flowHeight / 2}
                height={h}
                fill={clamped > 0 ? "#5FB49C" : "#C2664D"}
              />
            );
          })}
        </svg>
      ) : null}
      <span className="mt-0.5 font-mono text-[9px] leading-none text-muted">
        {label} {change}
      </span>
    </span>
  );
}

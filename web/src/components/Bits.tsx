import { DASH, num } from "../lib/format";

/** An inline micro-bar for a block score. 0-100, or an explicit blank.
 *
 *  A missing block renders as a dash with a tooltip, NOT as an empty bar. An
 *  empty bar and a zero bar look identical, and the difference between "no
 *  data" and "scored badly" is the whole reason L2 renormalises weights. */
export function MicroBar({
  value,
  label,
  status,
  missing = "redistributed",
}: {
  value: number | null | undefined;
  label: string;
  /** "dark": no source for any asset this run. "absent": the block did not
   *  exist in the score version that wrote the file. Neither is a zero. */
  status?: "dark" | "absent";
  /** How a missing value was scored: the pre-gem-v2 method redistributed its
   *  weight; D-075 (gem-v2) counts it as 0. */
  missing?: "redistributed" | "zero";
}) {
  if (status === "dark") {
    return (
      <span
        className="font-mono text-[10px] uppercase text-muted/70"
        title={`${label}: dark (no source). Nothing was measured for any asset this run, so the block dropped out for everyone rather than scoring zero.`}
      >
        dark
      </span>
    );
  }
  if (status === "absent") {
    return (
      <span
        className="font-mono text-xs text-muted/60"
        title={`${label}: not part of the score version that produced this run.`}
      >
        n/a
      </span>
    );
  }
  if (value === null || value === undefined || Number.isNaN(value)) {
    return (
      <span
        className="font-mono text-xs text-muted"
        title={
          missing === "zero"
            ? `${label}: no data for this asset. Missing data scores nothing: it counts as 0 of this block's weight, and lowers coverage.`
            : `${label}: no data. Its weight was redistributed across the blocks that had data, not counted as zero.`
        }
      >
        {DASH}
      </span>
    );
  }
  return (
    <span className="flex items-center justify-end gap-1.5" title={`${label}: ${num(value, 0)}`}>
      <span className="hidden h-1 w-10 bg-line sm:block" aria-hidden="true">
        <span
          className="block h-full bg-pass/70"
          style={{ width: `${Math.max(2, Math.min(100, value))}%` }}
        />
      </span>
      <span className="num text-xs">{num(value, 0)}</span>
    </span>
  );
}

export function Flag({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "neutral" | "pass" | "fail" }) {
  const tones = {
    neutral: "border-line text-muted",
    pass: "border-pass/50 text-pass",
    fail: "border-fail/50 text-fail",
  };
  return (
    <span className={`border px-1.5 py-0.5 font-mono text-[10px] uppercase ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function Section({
  title,
  note,
  children,
}: {
  title: string;
  note?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-12 first:mt-0">
      <h2 className="text-base font-medium">{title}</h2>
      {note ? <p className="mt-1.5 max-w-3xl text-sm text-muted">{note}</p> : null}
      <div className="mt-5">{children}</div>
    </section>
  );
}

/** Wide content scrolls inside its own container, so the page body never
 *  scrolls horizontally.
 *
 *  `narrow="hide"` additionally hides the table below `sm`, for the one table
 *  the spec requires to become STACKED ROWS at 375px rather than a horizontal
 *  scroll (spec 16.4). A side-scrolling table on a phone hides the columns
 *  that carry the argument -- the reader sees rank and ticker and has to
 *  discover that six block scores exist off-screen. */
export function TableWrap({
  children,
  narrow = "scroll",
}: {
  children: React.ReactNode;
  narrow?: "scroll" | "hide";
}) {
  const base = "-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0";
  return (
    <div className={narrow === "hide" ? `hidden sm:block ${base}` : base}>{children}</div>
  );
}

export function DarkChecksNotice({ checks }: { checks: string[] }) {
  if (!checks.length) return null;
  return (
    <div className="border-l-2 border-fail/60 py-3 pl-4 text-sm">
      <p className="font-medium">
        {checks.length} check{checks.length === 1 ? "" : "s"} dark this run
      </p>
      <p className="mt-1 text-muted">
        <span className="font-mono text-xs">{checks.join(", ")}</span>. The source returned
        nothing for the entire universe, so these did not contribute to any verdict. A dark
        check is not a passing check: every survivor below is <em>unmeasured</em> on them.
      </p>
    </div>
  );
}

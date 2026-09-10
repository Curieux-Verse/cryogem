import { compactInt, pct } from "../lib/format";
import type { Funnel as FunnelData } from "../lib/types";

/** The hero, because the funnel is the thesis (spec 16.3).
 *
 *  The disqualified count is given equal visual weight to the survivor count.
 *  Every crypto dashboard shows what it found; this one shows what it threw
 *  away, and that is the number that makes the rest credible. */
export default function Funnel({ data }: { data: FunnelData }) {
  const stages = [
    { value: data.universe, label: "perps in the universe", tone: "text-ink" },
    { value: data.disqualified, label: "disqualified by L1", tone: "text-fail" },
    { value: data.survivors, label: "survived L1", tone: "text-pass" },
    { value: data.reported, label: "reported", tone: "text-ink" },
  ];

  return (
    <div className="animate-funnel">
      {/* gap-x-12 with the arrow centred in the gutter. At a tighter gap the
          arrow sits against the figure and reads as a MINUS SIGN, so
          "disqualified 321" renders as "-321" -- which is both wrong and
          alarming on the one number this page most wants understood. */}
      <ol className="grid grid-cols-2 gap-x-8 gap-y-8 sm:grid-cols-4 sm:gap-x-12">
        {stages.map((stage, index) => (
          <li key={stage.label} className="relative">
            {index > 0 ? (
              <span
                aria-hidden="true"
                className="absolute -left-7 top-3.5 hidden text-sm text-muted/60 sm:block"
              >
                &rarr;
              </span>
            ) : null}
            <p className={`font-mono text-3xl ${stage.tone}`}>{compactInt(stage.value)}</p>
            <p className="mt-1 text-xs text-muted">{stage.label}</p>
          </li>
        ))}
      </ol>
      <p className="mt-6 max-w-3xl text-sm text-muted">
        Survival rate <span className="font-mono text-ink">{pct(data.survival_rate)}</span>.
        The design target is 20–50%: a rate outside that band means the thresholds need
        review, and the response is a recorded decision, never a quiet loosening of a
        number that disqualified something interesting.
      </p>
    </div>
  );
}

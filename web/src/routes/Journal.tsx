import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Section, TableWrap } from "../components/Bits";
import PulseJournalSection from "../components/PulseJournalSection";
import { Empty, Gate } from "../components/States";
import { getJournal } from "../lib/data";
import { DASH, num, pct, signedPct } from "../lib/format";
import { useData } from "../lib/useData";
import type { GroupStats, Journal as JournalData } from "../lib/types";

// The receipts. The most important page, and the only one that reports outcomes.
//
// Three rules it exists to honour:
//
//   1. MEDIAN AND MEAN SIDE BY SIDE, ALWAYS. They diverge, and the divergence
//      IS the finding: mean positive with median negative is a lottery-ticket
//      distribution, which is legitimate but has to be SIZED as one. That is
//      invisible unless both numbers are on the page together.
//
//   2. IT INCLUDES THE LOSERS. A forward-return distribution with the bad
//      entries removed is not a distribution, it is marketing. The underlying
//      table is append-only precisely so this list cannot be curated.
//
//   3. THE EMPTY STATE DOES NOT FLATTER. Before there is enough data it says so
//      and shows how far short it is, rather than an encouraging partial number.

const MIN_FOR_CONCLUSION = 30;

/** What an entry written before score_version existed belongs to (D-076). */
const LEGACY_VERSION = "gem-v1";

function statisticsFor(data: JournalData, version: string) {
  return data.statistics_by_version?.[version] ?? data.statistics;
}

/** Newest method first, so the current cohort is the default view. */
function versionsOf(data: JournalData): string[] {
  const known = Object.keys(data.statistics_by_version ?? {});
  const seen = new Set([
    ...known,
    ...data.entries.map((e) => e.score_version ?? LEGACY_VERSION),
  ]);
  const current = data.score_version;
  return [...seen].sort((a, b) => {
    if (a === current) return -1;
    if (b === current) return 1;
    return b.localeCompare(a);
  });
}

function Moment({ stats, field }: { stats: GroupStats; field: "median" | "mean" }) {
  const value = stats.vs_btc?.[field] ?? null;
  if (value === null) return <span className="num text-muted">{DASH}</span>;
  return (
    <span className={`num ${value > 0 ? "text-pass" : "text-fail"}`}>
      {signedPct(value)}
    </span>
  );
}

/** A crude but honest histogram: buckets of returns vs BTC, drawn with divs.
 *  No chart library, because a 40 KB dependency to draw twelve rectangles is a
 *  bad trade against the 500 KB bundle budget. */
function Histogram({ values, label }: { values: number[]; label: string }) {
  if (values.length === 0) {
    return <p className="text-sm text-muted">No completed returns for {label} yet.</p>;
  }
  // Open-ended at both ends (D-066). The old edges ran from -100% to +1000%,
  // but a return vs BTC falls below -100% whenever the asset collapses while
  // BTC rises -- and those, the worst outcomes, were silently not drawn.
  const edges = [
    -Infinity, -0.5, -0.3, -0.2, -0.1, -0.05, 0, 0.05, 0.1, 0.2, 0.3, 0.5, 1, Infinity,
  ];
  const buckets = edges.slice(0, -1).map((low, index) => {
    const high = edges[index + 1] ?? Infinity;
    return {
      low,
      high,
      n: values.filter((v) => v >= low && v < high).length,
    };
  });
  const bucketLabel = (low: number, high: number) =>
    low === -Infinity ? `< ${signedPct(high, 0)}` : signedPct(low, 0);
  const peak = Math.max(...buckets.map((b) => b.n), 1);

  return (
    <div>
      <p className="mb-2 text-xs text-muted">
        {label}: {values.length} completed return{values.length === 1 ? "" : "s"} vs BTC
      </p>
      <ul className="space-y-0.5">
        {buckets.map((bucket) => (
          <li key={bucket.low} className="flex items-center gap-2 text-xs">
            <span className="num w-16 text-muted">{bucketLabel(bucket.low, bucket.high)}</span>
            <span className="h-3 flex-1 bg-line/40">
              <span
                className={`block h-full ${bucket.low < 0 ? "bg-fail/70" : "bg-pass/70"}`}
                style={{ width: `${(bucket.n / peak) * 100}%` }}
              />
            </span>
            <span className="num w-8 text-muted">{bucket.n || ""}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function HorizonPanel({
  data,
  horizon,
  version,
}: {
  data: JournalData;
  horizon: string;
  version: string | null;
}) {
  // D-076: a cohort is one scoring method. Blending two methods' returns into
  // one number would make a methodology change look like a result.
  const stats = (version ? statisticsFor(data, version) : data.statistics)[horizon];
  if (!stats) return null;

  const cohort = data.entries.filter(
    (e) => !version || (e.score_version ?? LEGACY_VERSION) === version,
  );
  const signalReturns = cohort
    .filter((e) => !e.is_control && e.returns[horizon]?.return_vs_btc !== undefined)
    .map((e) => e.returns[horizon]?.return_vs_btc)
    .filter((v): v is number => typeof v === "number");
  const controlReturns = cohort
    .filter((e) => e.is_control && e.returns[horizon]?.return_vs_btc !== undefined)
    .map((e) => e.returns[horizon]?.return_vs_btc)
    .filter((v): v is number => typeof v === "number");

  const enough = stats.signal.n >= MIN_FOR_CONCLUSION;
  const median = stats.signal.vs_btc?.median ?? null;
  const mean = stats.signal.vs_btc?.mean ?? null;
  const lottery = median !== null && mean !== null && mean > 0 && median < 0;

  return (
    <div className="border-t border-line pt-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-mono text-sm">{horizon}</h3>
        <p className="text-xs text-muted">
          {stats.entries_with_returns} of {stats.entries_total} entries have a complete{" "}
          {horizon} return
        </p>
      </div>

      {!enough ? (
        <p className="mt-3 max-w-3xl text-sm text-muted">
          <span className="text-ink">Not enough data to draw a conclusion at {horizon}.</span>{" "}
          {stats.signal.n} of {MIN_FOR_CONCLUSION} completed signal returns. A partial
          number here would be misleading rather than encouraging, so none is shown.
        </p>
      ) : (
        <>
          <TableWrap>
            <table className="mt-4 w-full min-w-[34rem] border-collapse text-sm">
              <caption className="sr-only">
                Signal and control statistics at the {horizon} horizon
              </caption>
              <thead>
                <tr className="border-b border-line text-left">
                  <th scope="col" className="py-2 pr-3">Group</th>
                  <th scope="col" className="py-2 pr-3 text-right">n</th>
                  <th scope="col" className="py-2 pr-3 text-right">Median vs BTC</th>
                  <th scope="col" className="py-2 pr-3 text-right">Mean vs BTC</th>
                  <th scope="col" className="py-2 pr-3 text-right">Hit rate</th>
                  <th scope="col" className="py-2 text-right">Worst drawdown</th>
                </tr>
              </thead>
              <tbody>
                {(["signal", "control"] as const).map((key) => {
                  const group = stats[key];
                  return (
                    <tr key={key} className="border-b border-line/50">
                      <td className="py-2 pr-3">{key}</td>
                      <td className="num py-2 pr-3">{group.n}</td>
                      <td className="py-2 pr-3 text-right">
                        {group.n ? <Moment stats={group} field="median" /> : DASH}
                      </td>
                      <td className="py-2 pr-3 text-right">
                        {group.n ? <Moment stats={group} field="mean" /> : DASH}
                      </td>
                      <td className="num py-2 pr-3">
                        {group.hit_rate_vs_btc === null || group.hit_rate_vs_btc === undefined
                          ? DASH
                          : pct(group.hit_rate_vs_btc, 0)}
                      </td>
                      <td className="num py-2">
                        {signedPct(group.max_adverse?.min ?? null)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableWrap>

          {lottery ? (
            <p className="mt-4 max-w-3xl border-l-2 border-fail py-2 pl-4 text-sm">
              <strong className="font-medium">Mean positive, median negative.</strong> This
              is a lottery-ticket distribution: a few large winners carry the average while
              the typical outcome is a loss. Legitimate, but it has to be sized as one.
            </p>
          ) : null}

          {stats.edge_vs_control.available ? (
            <p className="mt-4 text-sm">
              <span className="text-muted">Edge over a random L1 survivor:</span>{" "}
              median{" "}
              <span className="font-mono">
                {signedPct(stats.edge_vs_control.median_difference)}
              </span>
              , mean{" "}
              <span className="font-mono">
                {signedPct(stats.edge_vs_control.mean_difference)}
              </span>{" "}
              <span className="text-muted">
                ({stats.edge_vs_control.signal_n} signals vs{" "}
                {stats.edge_vs_control.control_n} controls)
              </span>
            </p>
          ) : (
            <p className="mt-4 text-sm text-muted">
              Edge vs control: {stats.edge_vs_control.reason}. Without the control group a
              positive return says nothing — everything may simply have gone up.
            </p>
          )}

          <div className="mt-6 grid gap-8 sm:grid-cols-2">
            <Histogram values={signalReturns} label="signals" />
            <Histogram values={controlReturns} label="random controls" />
          </div>
        </>
      )}
    </div>
  );
}

export default function Journal() {
  const journal = useData(getJournal);
  const [group, setGroup] = useState<"all" | "signal" | "control">("all");
  const [horizon, setHorizon] = useState("30d");
  const [version, setVersion] = useState<string | null>(null);

  const versions = useMemo(
    () => (journal.state === "ready" ? versionsOf(journal.data) : []),
    [journal],
  );
  // Default to the method in force now; the reader can switch to an older one.
  const shown = version ?? versions[0] ?? null;

  const entries = useMemo(() => {
    if (journal.state !== "ready") return [];
    return journal.data.entries.filter((entry) => {
      if (shown && (entry.score_version ?? LEGACY_VERSION) !== shown) return false;
      if (group === "signal") return !entry.is_control;
      if (group === "control") return entry.is_control;
      return true;
    });
  }, [journal, group, shown]);

  // TWO records, side by side and never merged. The Gem journal's load state and
  // the Pulse journal's are independent in both directions: the hourly file is
  // absent until its first bake, and the daily one must render exactly as it
  // always has when it is -- and vice versa.
  return (
    <>
      <Gate
        data={journal}
        what="the journal"
        missingTitle="No journal published yet"
        missingBody="The journal records every ranked asset plus a random control drawn from the same survivor pool, then fills in forward returns as each horizon elapses. It appears here after the first journal run."
      >
      {(data) => (
        <>
          <Section
            title="Receipts"
            note="Every signal this system has produced, with its outcome, including the ones that went badly. Returns are measured against BTC: +100% in a market where BTC did +90% is not a good pick. Each ranked signal is journalled alongside a random L1 survivor that did NOT make the list, because without that comparison a positive return says nothing."
          >
            <p className="font-mono text-xs text-muted">{data.coverage_note}</p>
            {data.entries.length === 0 ? (
              <div className="mt-6">
                <Empty
                  title="Nothing recorded yet"
                  body="No journal entries exist. Nothing can be concluded, and nothing is claimed. This page will not show an encouraging partial number in the meantime."
                />
              </div>
            ) : (
              <div className="mt-8 space-y-8">
                {versions.length > 1 ? (
                  <div className="flex flex-wrap items-end gap-4">
                    <label className="text-xs text-muted">
                      <span className="block">Scoring method</span>
                      <select
                        value={shown ?? ""}
                        onChange={(event) => setVersion(event.target.value)}
                        className="mt-1 border border-line bg-transparent px-2 py-1 text-sm"
                      >
                        {versions.map((v) => (
                          <option key={v} value={v}>
                            {v}
                            {v === data.score_version ? " (current)" : ""}
                          </option>
                        ))}
                      </select>
                    </label>
                    <p className="max-w-xl text-xs text-muted">
                      Cohorts are never blended. A change of method starts a new
                      record, so returns earned under the old scoring are shown
                      under the old name rather than added to the new one.
                    </p>
                  </div>
                ) : null}
                {data.horizons.map((h) => (
                  <HorizonPanel key={h} data={data} horizon={h} version={shown} />
                ))}
              </div>
            )}
          </Section>

          {data.entries.length ? (
            <Section
              title={`All entries (${entries.length})`}
              note="Unfiltered and unsorted by outcome. Entries cannot be deleted or edited: the underlying table is append-only, enforced by database triggers, because excluding the one you would have known better about is exactly how every signal channel comes to look profitable."
            >
              <div className="mb-4 flex flex-wrap items-end gap-4">
                <label className="text-xs text-muted">
                  <span className="block">Group</span>
                  <select
                    value={group}
                    onChange={(event) =>
                      setGroup(event.target.value as "all" | "signal" | "control")
                    }
                    className="mt-1 border border-line bg-surface px-2 py-1 text-sm text-ink"
                  >
                    <option value="all">all</option>
                    <option value="signal">signals only</option>
                    <option value="control">controls only</option>
                  </select>
                </label>
                <label className="text-xs text-muted">
                  <span className="block">Horizon shown</span>
                  <select
                    value={horizon}
                    onChange={(event) => setHorizon(event.target.value)}
                    className="mt-1 border border-line bg-surface px-2 py-1 text-sm text-ink"
                  >
                    {data.horizons.map((h) => (
                      <option key={h} value={h}>
                        {h}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              <TableWrap>
                <table className="w-full min-w-[42rem] border-collapse text-sm">
                  <caption className="sr-only">
                    Every journal entry with its {horizon} outcome
                  </caption>
                  <thead>
                    <tr className="border-b border-line text-left">
                      <th scope="col" className="py-2 pr-3">Date</th>
                      <th scope="col" className="py-2 pr-3">Asset</th>
                      <th scope="col" className="py-2 pr-3">Group</th>
                      <th scope="col" className="py-2 pr-3 text-right">Rank</th>
                      <th scope="col" className="py-2 pr-3 text-right">Score</th>
                      <th scope="col" className="py-2 pr-3 text-right">Raw</th>
                      <th scope="col" className="py-2 pr-3 text-right">vs BTC</th>
                      <th scope="col" className="py-2 text-right">Worst</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.slice(0, 500).map((entry) => {
                      const outcome = entry.returns[horizon];
                      return (
                        <tr key={entry.entry_id} className="border-b border-line/40">
                          <td className="py-2 pr-3 font-mono text-xs text-muted">
                            {entry.run_date}
                          </td>
                          <td className="py-2 pr-3">
                            <Link
                              to={`/asset/${encodeURIComponent(entry.base_asset)}`}
                              className="font-mono hover:text-pass"
                            >
                              {entry.base_asset}
                            </Link>
                          </td>
                          <td className="py-2 pr-3 text-xs text-muted">
                            {entry.is_control ? "control" : "signal"}
                          </td>
                          <td className="num py-2 pr-3 text-muted">
                            {entry.rank || DASH}
                          </td>
                          <td className="num py-2 pr-3">
                            {entry.is_control ? DASH : num(entry.total_score)}
                          </td>
                          <td className="num py-2 pr-3">
                            {signedPct(outcome?.return_raw ?? null)}
                          </td>
                          <td
                            className={`num py-2 pr-3 ${
                              outcome?.return_vs_btc == null
                                ? "text-muted"
                                : outcome.return_vs_btc > 0
                                  ? "text-pass"
                                  : "text-fail"
                            }`}
                          >
                            {signedPct(outcome?.return_vs_btc ?? null)}
                          </td>
                          <td className="num py-2 text-muted">
                            {signedPct(outcome?.max_adverse ?? null)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </TableWrap>
              <p className="mt-3 text-xs text-muted">
                A blank outcome means the horizon has not elapsed or no price was recorded
                for the horizon date. It is left blank rather than filled with a zero,
                because a fabricated zero could never be corrected in an append-only table.
              </p>
            </Section>
          ) : null}
        </>
      )}
      </Gate>
      <PulseJournalSection />
    </>
  );
}

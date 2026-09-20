// The Pulse journal (D-081), shown BELOW the Gem journal on the receipts page
// and never merged into it.
//
// Three rules carry over from the daily journal, plus one that is specific to
// this clock:
//
//   1. THE GATE IS ABSOLUTE. Below `min_for_conclusion` completed returns for a
//      trigger and horizon, the numbers are not drawn at all -- only n and how
//      far short it is. A partial median at n=4 is not an early result, it is
//      noise with a sign, and the sign is what a reader remembers.
//
//   2. IT INCLUDES THE LOSERS, and cannot be sorted by outcome. The underlying
//      table is append-only; this view must not become the curated one.
//
//   3. THE CONTROL IS NOT A SIGNAL. One random non-triggering survivor per
//      entry-hour. It is chipped as "control - random" everywhere, because a
//      reader who mistakes it for a pick reads every number on the page wrong.
//
//   4. NOTHING HERE CHANGES A WEIGHT. Pulse's kill criteria were deliberately
//      not implemented (D-081): this is a measurement, not a controller.

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Section, TableWrap } from "./Bits";
import { StateChip, TriggerChip } from "./PulseBits";
import { Failed, Loading } from "./States";
import { PULSE_REFRESH_MS, getPulseJournal } from "../lib/data";
import { DASH, hhmm, num, pct, relative, signedPct } from "../lib/format";
import { useData, useNow } from "../lib/useData";
import type {
  PulseJournal as PulseJournalData,
  PulseJournalBlock,
  PulseJournalEntry,
  PulseMoments,
} from "../lib/types";

/** The order they are always shown in: the two signals, then their control. */
const TRIGGERS = ["aligned", "top10", "control"] as const;
type Trigger = (typeof TRIGGERS)[number];

const TRIGGER_LABEL: Record<Trigger, string> = {
  aligned: "entered aligned",
  top10: "entered top 10",
  control: "control (random)",
};

/** What `pulse_report` keys a row with when it carries no score_version. */
const UNVERSIONED = "unversioned";

const DEFAULT_HORIZONS = ["4h", "24h", "72h"];

const OBSERVATIONAL =
  "Observational only: nothing on this page changes a weight. Pulse's kill criteria were deliberately not implemented, so no number here silently re-tunes the score — it is for reading, not for acting on automatically.";

const WHAT_IT_IS =
  "Pulse scores every Layer 1 survivor each hour. A row is written when an asset ENTERS the Pulse top 10 or ENTERS ALIGNED — entering, not staying — plus one random control per entry-hour drawn from that hour's scored survivors that did not trigger. Entry is the next 1H bar's open, the first price anyone could have traded; returns fill at 4h, 24h and 72h, measured raw and against BTC. It is kept apart from the Gem journal above on purpose: a 4-hour signal and a 90-day signal cannot share a scoreboard without one flattering the other.";

// -- small readers over a schema where almost everything can be absent ----------

function completed(block: PulseJournalBlock | undefined): number {
  if (!block) return 0;
  const n = block.vs_btc?.n;
  return typeof n === "number" ? n : block.n ?? 0;
}

function entryVersion(entry: PulseJournalEntry, fallback: string | null): string {
  return entry.score_version ?? fallback ?? UNVERSIONED;
}

/** Newest method first, so the current cohort is the default view (as D-076
 *  does on the Gem journal). Cohorts are never blended. */
function versionsOf(data: PulseJournalData): string[] {
  const current = data.score_version;
  const seen = new Set([
    ...Object.keys(data.statistics ?? {}),
    ...data.entries.map((e) => entryVersion(e, current)),
  ]);
  if (current) seen.add(current);
  return [...seen].sort((a, b) => {
    if (a === current) return -1;
    if (b === current) return 1;
    return b.localeCompare(a);
  });
}

/** Identical rendering to the Gem journal's Moment, so the two tables' columns
 *  read the same: a signed percentage, coloured, and an em dash when absent. */
function Moment({
  moments,
  field,
}: {
  moments: PulseMoments | null | undefined;
  field: "median" | "mean";
}) {
  const value = moments?.[field] ?? null;
  if (value === null) return <span className="num text-muted">{DASH}</span>;
  return (
    <span className={`num ${value > 0 ? "text-pass" : "text-fail"}`}>{signedPct(value)}</span>
  );
}

function VsBtc({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) {
    return (
      <span className="num text-muted" title="This horizon has not elapsed yet, or its bars are incomplete. Left blank rather than filled with a zero.">
        {DASH}
      </span>
    );
  }
  return (
    <span className={`num ${value > 0 ? "text-pass" : "text-fail"}`}>{signedPct(value)}</span>
  );
}

// -- statistics ------------------------------------------------------------------

function HorizonPanel({
  data,
  horizon,
  version,
}: {
  data: PulseJournalData;
  horizon: string;
  version: string;
}) {
  const byTrigger = data.statistics?.[version] ?? {};
  const min = data.min_for_conclusion;
  const blocks = TRIGGERS.map((trigger) => ({
    trigger,
    block: byTrigger[trigger]?.[horizon],
    n: completed(byTrigger[trigger]?.[horizon]),
  }));
  const total = blocks.reduce((sum, b) => sum + b.n, 0);

  return (
    <div className="border-t border-line pt-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-mono text-sm">{horizon}</h3>
        <p className="text-xs text-muted">
          {total} completed {horizon} return{total === 1 ? "" : "s"} across the three groups
        </p>
      </div>

      <TableWrap>
        {/* table-fixed on purpose: with auto layout a gated row's spanning
            message re-sizes that panel's columns, and the 4h, 24h and 72h
            tables stop lining up down the page. Fixed widths make the three
            scan as one. */}
        <table className="mt-4 w-full min-w-[34rem] table-fixed border-collapse text-sm">
          <caption className="sr-only">
            Pulse journal statistics at the {horizon} horizon, by trigger. A group with
            fewer than {min} completed returns shows its count instead of its numbers.
          </caption>
          <thead>
            <tr className="border-b border-line text-left">
              <th scope="col" className="w-[24%] py-2 pr-3">Trigger</th>
              <th scope="col" className="w-[8%] py-2 pr-3 text-right">n</th>
              <th scope="col" className="w-[18%] py-2 pr-3 text-right">Median vs BTC</th>
              <th scope="col" className="w-[18%] py-2 pr-3 text-right">Mean vs BTC</th>
              <th scope="col" className="w-[14%] py-2 pr-3 text-right">Hit rate</th>
              <th scope="col" className="w-[18%] py-2 text-right">Worst drawdown</th>
            </tr>
          </thead>
          <tbody>
            {blocks.map(({ trigger, block, n }) => {
              const enough = n >= min;
              return (
                <tr key={trigger} className="border-b border-line/50">
                  <td className="py-2 pr-3">
                    <span className="whitespace-nowrap">{TRIGGER_LABEL[trigger]}</span>
                  </td>
                  <td className="num py-2 pr-3">{n}</td>
                  {enough ? (
                    <>
                      <td className="py-2 pr-3 text-right">
                        <Moment moments={block?.vs_btc} field="median" />
                      </td>
                      <td className="py-2 pr-3 text-right">
                        <Moment moments={block?.vs_btc} field="mean" />
                      </td>
                      <td className="num py-2 pr-3">
                        {block?.hit_rate_vs_btc === null || block?.hit_rate_vs_btc === undefined
                          ? DASH
                          : pct(block.hit_rate_vs_btc, 0)}
                      </td>
                      <td className="num py-2">{signedPct(block?.max_adverse?.min ?? null)}</td>
                    </>
                  ) : (
                    // The gate. Not a greyed-out number, no number at all.
                    <td colSpan={4} className="py-2 text-sm text-muted">
                      {n === 0
                        ? `No completed ${horizon} return yet; ${min} are needed`
                        : `${min - n} short of the ${min} completed returns needed`}{" "}
                      — no figure is shown, because a partial one here would be misleading
                      rather than encouraging.
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </TableWrap>

      <div className="mt-4 space-y-1">
        {blocks
          .filter((b) => b.trigger !== "control")
          .map(({ trigger, block, n }) => {
            const label = TRIGGER_LABEL[trigger];
            if (n < min) {
              return (
                <p key={trigger} className="text-sm text-muted">
                  Edge over the random control for {label}: withheld at {n} of {min} completed
                  returns.
                </p>
              );
            }
            const vs = block?.vs_control;
            if (!vs || vs.median_difference === null || vs.median_difference === undefined) {
              return (
                <p key={trigger} className="text-sm text-muted">
                  Edge over the random control for {label}: no control returns have completed at{" "}
                  {horizon}. Without the comparison a positive return says nothing — everything
                  may simply have gone up.
                </p>
              );
            }
            return (
              <p key={trigger} className="text-sm">
                <span className="text-muted">Edge over the random control, {label}:</span>{" "}
                median <span className="font-mono">{signedPct(vs.median_difference)}</span>, mean{" "}
                <span className="font-mono">{signedPct(vs.mean_difference ?? null)}</span>{" "}
                <span className="text-muted">
                  ({n} entries vs {vs.control_n} controls)
                </span>
              </p>
            );
          })}
      </div>
    </div>
  );
}

// -- entries ----------------------------------------------------------------------

function EntriesTable({
  entries,
  horizons,
}: {
  entries: PulseJournalEntry[];
  horizons: string[];
}) {
  return (
    <>
      <TableWrap narrow="hide">
        <table className="w-full min-w-[52rem] border-collapse text-sm">
          <caption className="sr-only">
            Every Pulse journal entry, newest first, with its return against BTC at each
            horizon. Not sorted by outcome.
          </caption>
          <thead>
            <tr className="border-b border-line text-left">
              <th scope="col" className="py-2 pr-3">Signal hour (UTC)</th>
              <th scope="col" className="py-2 pr-3">Asset</th>
              <th scope="col" className="py-2 pr-3">Trigger</th>
              <th scope="col" className="py-2 pr-3 text-right">Pulse</th>
              <th scope="col" className="py-2 pr-3 text-right">Rank</th>
              <th scope="col" className="py-2 pr-3 text-right">Gem</th>
              <th scope="col" className="py-2 pr-3">4H structure</th>
              {horizons.map((h) => (
                <th key={h} scope="col" className="py-2 pr-3 text-right">
                  {h} vs BTC
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.entry_id} className="border-b border-line/40">
                <td
                  className="whitespace-nowrap py-2 pr-3 font-mono text-xs text-muted"
                  title={entry.ts_signal_utc}
                >
                  {entry.ts_signal_utc.slice(0, 10)} {hhmm(entry.ts_signal_utc)}
                </td>
                <td className="py-2 pr-3">
                  <Link
                    to={`/asset/${encodeURIComponent(entry.asset)}`}
                    className="font-mono hover:text-pass"
                  >
                    {entry.asset}
                  </Link>
                </td>
                <td className="py-2 pr-3">
                  <TriggerChip trigger={entry.trigger} />
                </td>
                <td className="num py-2 pr-3">{num(entry.pulse_score, 2)}</td>
                <td className="num py-2 pr-3 text-muted">{entry.pulse_rank ?? DASH}</td>
                <td className="num py-2 pr-3 text-muted">{entry.gem_rank ?? DASH}</td>
                <td className="py-2 pr-3">
                  <StateChip tf="4H" state={entry.state_4h} />
                </td>
                {horizons.map((h) => (
                  <td key={h} className="py-2 pr-3 text-right">
                    <VsBtc value={entry.returns?.[h]?.return_vs_btc ?? null} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </TableWrap>

      {/* Stacked below sm, like the Pulse tab: a side-scrolling table on a phone
          hides the trigger and the outcome, which are the whole row. */}
      <ul className="divide-y divide-line border-t border-line sm:hidden">
        {entries.map((entry) => (
          <li key={entry.entry_id} className="py-4">
            <div className="flex items-baseline justify-between gap-3">
              <p>
                <Link
                  to={`/asset/${encodeURIComponent(entry.asset)}`}
                  className="font-mono text-base hover:text-pass"
                >
                  {entry.asset}
                </Link>
                <span className="ml-2 font-mono text-xs text-muted">
                  {entry.ts_signal_utc.slice(0, 10)} {hhmm(entry.ts_signal_utc)}
                </span>
              </p>
              <p className="num text-base">{num(entry.pulse_score, 2)}</p>
            </div>
            <div className="mt-2 flex flex-wrap gap-1">
              <TriggerChip trigger={entry.trigger} />
              <StateChip tf="4H" state={entry.state_4h} />
            </div>
            <p className="mt-1 text-xs text-muted">
              Pulse rank {entry.pulse_rank ?? DASH} · Gem rank {entry.gem_rank ?? DASH}
            </p>
            <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs">
              {horizons.map((h) => (
                <div key={h} className="flex items-baseline gap-1.5">
                  <dt className="text-muted">{h} vs BTC</dt>
                  <dd>
                    <VsBtc value={entry.returns?.[h]?.return_vs_btc ?? null} />
                  </dd>
                </div>
              ))}
            </dl>
          </li>
        ))}
      </ul>
    </>
  );
}

// -- body --------------------------------------------------------------------------

/** A block that states what is absent and why, in the section's own voice. */
function Note({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-6 max-w-3xl border-l-2 border-line py-6 pl-5">
      <h3 className="text-base font-medium">{title}</h3>
      <p className="mt-2 text-sm leading-relaxed text-muted">{children}</p>
    </div>
  );
}

function Body({ data, now }: { data: PulseJournalData; now: number }) {
  const [version, setVersion] = useState<string | null>(null);
  const [trigger, setTrigger] = useState<"all" | Trigger>("all");

  const versions = useMemo(() => versionsOf(data), [data]);
  const shown = version ?? versions[0] ?? UNVERSIONED;
  const horizons = data.horizons?.length ? data.horizons : DEFAULT_HORIZONS;

  const entries = useMemo(() => {
    const rows = data.entries.filter((entry) => {
      if (versions.length > 1 && entryVersion(entry, data.score_version) !== shown) return false;
      return trigger === "all" || entry.trigger === trigger;
    });
    // Newest first, and that is the ONLY ordering offered: a table the reader
    // can sort by outcome is a table that hides the losers one click away.
    return [...rows].sort((a, b) => b.ts_signal_utc.localeCompare(a.ts_signal_utc));
  }, [data, shown, trigger, versions.length]);

  const counted =
    data.entries_shown < data.entries_total
      ? `${data.entries_total} recorded, ${data.entries_shown} shown`
      : `${data.entries_total} recorded`;

  return (
    <>
      <p className="mt-4 font-mono text-xs text-muted" aria-live="polite">
        {data.score_version ?? UNVERSIONED} · {counted} · {data.entries_with_returns} with at
        least one completed return · first entry{" "}
        {data.first_entry_utc
          ? `${data.first_entry_utc.slice(0, 10)} ${hhmm(data.first_entry_utc)} UTC`
          : DASH}{" "}
        · generated {relative(data.generated_at_utc, now)}
      </p>
      <p className="mt-2 max-w-3xl font-mono text-xs text-muted">{data.coverage_note}</p>
      {data.schema_version !== 1 ? (
        <p className="mt-2 text-xs text-fail">
          pulse_journal.json schema {String(data.schema_version)}; this section was built for
          schema 1. Some fields may not show.
        </p>
      ) : null}

      {data.entries_total === 0 ? (
        <Note title="Nothing recorded yet">
          No Pulse journal entry exists. That is the expected state early on, not a fault: an
          entry is only written when an asset <em>enters</em> the Pulse top 10 or{" "}
          <em>enters</em> ALIGNED compared with the previous scored hour, so the very first
          scored hour writes none, an hour with no change in the top 10 writes none, and an
          hour more than 3 hours after the last scored one writes none rather than counting a
          gap as an entry. Nothing is concluded and nothing is claimed in the meantime.
        </Note>
      ) : data.entries_with_returns === 0 ? (
        <>
          <Note title="No completed returns yet">
            {data.entries_total} {data.entries_total === 1 ? "entry is" : "entries are"} recorded
            and none has a completed return. Each one has to wait out its clock: the {horizons[0]}{" "}
            return fills {horizons[0]} after the signal hour
            {horizons.length > 1 ? `, then ${horizons.slice(1).join(" and ")} later still` : ""}
            , and a return is written only when every 1H bar in the window exists for both the
            asset and BTC. Until then there is no statistic to show, and a partial one would be
            worse than none.
          </Note>
          <Section
            title={`All entries (${entries.length})`}
            note="Newest first, unfiltered and never sorted by outcome. The underlying table is append-only: an entry cannot be edited or deleted."
          >
            <Filters
              versions={versions}
              shown={shown}
              current={data.score_version}
              onVersion={setVersion}
              trigger={trigger}
              onTrigger={setTrigger}
            />
            <EntriesTable entries={entries} horizons={horizons} />
            <BlankNote />
          </Section>
        </>
      ) : (
        <>
          <Section
            title="Statistics by trigger"
            note={`Each horizon compares the two signal triggers against the random control. A group with fewer than ${data.min_for_conclusion} completed returns shows how far short it is instead of a number.`}
          >
            {versions.length > 1 ? (
              <div className="mb-6 flex flex-wrap items-end gap-4">
                <label className="text-xs text-muted">
                  <span className="block">Scoring method</span>
                  <select
                    value={shown}
                    onChange={(event) => setVersion(event.target.value)}
                    className="mt-1 border border-line bg-surface px-2 py-1 text-sm text-ink"
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
                  Cohorts are never blended. Returns earned under an older Pulse scoring stay
                  under that name rather than being added to the current one.
                </p>
              </div>
            ) : null}
            <div className="space-y-8">
              {horizons.map((h) => (
                <HorizonPanel key={h} data={data} horizon={h} version={shown} />
              ))}
            </div>
          </Section>

          <Section
            title={`All entries (${entries.length})`}
            note="Newest first, unfiltered and never sorted by outcome. The underlying table is append-only: an entry cannot be edited or deleted, because excluding the one you would have known better about is exactly how every signal channel comes to look profitable."
          >
            <Filters
              versions={versions}
              shown={shown}
              current={data.score_version}
              onVersion={setVersion}
              trigger={trigger}
              onTrigger={setTrigger}
            />
            <EntriesTable entries={entries} horizons={horizons} />
            <BlankNote />
          </Section>
        </>
      )}
    </>
  );
}

function BlankNote() {
  return (
    <p className="mt-3 max-w-3xl text-xs text-muted">
      A blank return means the horizon has not elapsed, or a 1H bar in the window is missing
      for the asset or for BTC. It is left blank rather than filled with a zero: a fabricated
      zero could never be corrected in an append-only table.
    </p>
  );
}

function Filters({
  versions,
  shown,
  current,
  onVersion,
  trigger,
  onTrigger,
}: {
  versions: string[];
  shown: string;
  current: string | null;
  onVersion: (value: string) => void;
  trigger: "all" | Trigger;
  onTrigger: (value: "all" | Trigger) => void;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-end gap-4">
      <label className="text-xs text-muted">
        <span className="block">Trigger</span>
        <select
          value={trigger}
          onChange={(event) => onTrigger(event.target.value as "all" | Trigger)}
          className="mt-1 border border-line bg-surface px-2 py-1 text-sm text-ink"
        >
          <option value="all">all</option>
          <option value="aligned">entered aligned</option>
          <option value="top10">entered top 10</option>
          <option value="control">control (random)</option>
        </select>
      </label>
      {versions.length > 1 ? (
        <label className="text-xs text-muted">
          <span className="block">Scoring method</span>
          <select
            value={shown}
            onChange={(event) => onVersion(event.target.value)}
            className="mt-1 border border-line bg-surface px-2 py-1 text-sm text-ink"
          >
            {versions.map((v) => (
              <option key={v} value={v}>
                {v}
                {v === current ? " (current)" : ""}
              </option>
            ))}
          </select>
        </label>
      ) : null}
    </div>
  );
}

// -- section -----------------------------------------------------------------------

export default function PulseJournalSection() {
  const journal = useData(getPulseJournal, [], { refreshMs: PULSE_REFRESH_MS });
  const now = useNow(30_000);

  let body: React.ReactNode;
  if (journal.state === "loading") {
    body = <Loading what="the Pulse journal" />;
  } else if (journal.state === "missing") {
    body = (
      <Note title="No Pulse journal published yet">
        The hourly journal is baked alongside the Pulse itself and deployed without a commit,
        so it appears here once the hourly pipeline has written its first file. The daily Gem
        journal above is a separate record and is unaffected.
      </Note>
    );
  } else if (journal.state === "error") {
    body = <Failed what="the Pulse journal" reason={journal.reason} />;
  } else if (journal.data.status === "unavailable") {
    body = (
      <Note title="Pulse journal unavailable">
        The publisher marked the hourly journal unavailable, so nothing is shown in its place:
        an old or partial record displayed as current is worse than none. The daily Gem journal
        above is unaffected.
      </Note>
    );
  } else {
    body = <Body data={journal.data} now={now} />;
  }

  return (
    <section
      aria-labelledby="pulse-journal"
      className="mt-16 border-t-2 border-line pt-10"
    >
      <p className="font-mono text-xs uppercase tracking-wide text-muted">
        a separate record, on a different clock
      </p>
      <h2 id="pulse-journal" className="mt-1 text-base font-medium">
        Pulse journal — the hourly receipts
      </h2>
      <p className="mt-1.5 max-w-3xl text-sm text-muted">{WHAT_IT_IS}</p>
      <p className="mt-3 max-w-3xl border-l-2 border-line py-2 pl-4 text-sm text-muted">
        {OBSERVATIONAL}
      </p>
      {body}
    </section>
  );
}

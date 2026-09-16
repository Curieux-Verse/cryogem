import { Flag, Section, TableWrap } from "../components/Bits";
import { Gate } from "../components/States";
import {
  STALE_AFTER_HOURS,
  SURVIVAL_BAND,
  ageHours,
  getHealth,
  getHistory,
} from "../lib/data";
import { DASH, compactInt, num, pct } from "../lib/format";
import { useData } from "../lib/useData";

// System status. The page that catches silent death.
//
// The last-run-per-collector table is the one that matters. A collector that
// stopped three weeks ago leaves no error anywhere: the screen still runs, the
// report still renders, the dashboard still deploys, and the data quietly goes
// stale. This is where that becomes visible.

function Age({ iso }: { iso: string | null }) {
  if (!iso) return <span className="text-muted">{DASH}</span>;
  const hours = ageHours(iso);
  const stale = hours > STALE_AFTER_HOURS;
  const shown =
    hours >= 48 ? `${Math.floor(hours / 24)}d ago` : `${Math.round(hours)}h ago`;
  return <span className={stale ? "text-fail" : "text-muted"}>{shown}</span>;
}

export default function Health() {
  const health = useData(getHealth);
  // Published on every run since the first release and rendered nowhere
  // until D-069. Loaded separately so a missing history.json costs this
  // section only, never the collector table above it.
  const history = useData(getHistory);

  return (
    <Gate
      data={health}
      what="system status"
      missingTitle="No health data published yet"
      missingBody="Collector outcomes, table row counts and lag percentiles appear here after the first publish."
    >
      {(data) => (
        <>
          <Section
            title="Collectors"
            note="Three statuses, and conflating any two of them misleads. `partial` means the collector RAN and degraded gracefully around an unconfigured optional source: it produced data and is not an outage. Only `failed` is a failure."
          >
            <TableWrap>
              <table className="w-full min-w-[38rem] border-collapse text-sm">
                <caption className="sr-only">Collector outcomes and last run</caption>
                <thead>
                  <tr className="border-b border-line text-left">
                    <th scope="col" className="py-2 pr-3">Collector</th>
                    <th scope="col" className="py-2 pr-3 text-right">Success</th>
                    <th scope="col" className="py-2 pr-3 text-right">Partial</th>
                    <th scope="col" className="py-2 pr-3 text-right">Failed</th>
                    <th scope="col" className="py-2 pr-3 text-right">Completion</th>
                    <th scope="col" className="py-2 pr-3">Last run</th>
                    <th scope="col" className="py-2">Rows</th>
                  </tr>
                </thead>
                <tbody>
                  {data.last_runs.map((run) => {
                    const counts = data.collectors[run.collector_name];
                    return (
                      <tr key={run.collector_name} className="border-b border-line/40">
                        <td className="py-2 pr-3 font-mono text-xs">{run.collector_name}</td>
                        <td className="num py-2 pr-3">{counts?.success ?? DASH}</td>
                        <td className="num py-2 pr-3 text-muted">{counts?.partial ?? DASH}</td>
                        <td
                          className={`num py-2 pr-3 ${counts?.failed ? "text-fail" : "text-muted"}`}
                        >
                          {counts?.failed ?? DASH}
                        </td>
                        <td className="num py-2 pr-3">
                          {counts?.completion_rate === null ||
                          counts?.completion_rate === undefined
                            ? DASH
                            : pct(counts.completion_rate, 0)}
                        </td>
                        <td className="py-2 pr-3 text-xs">
                          <Age iso={run.last_run} />
                        </td>
                        <td className="num py-2 text-xs text-muted">
                          {compactInt(run.rows_written)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </TableWrap>
            {data.last_runs.length === 0 ? (
              <p className="text-sm text-muted">
                No collector has ever run. That is the state before the first pipeline run,
                and also the state if the external trigger was never wired up.
              </p>
            ) : null}
          </Section>

          <Section
            title="Input coverage"
            note="A ROW is not a MEASUREMENT. Some collectors write a row per asset with nothing in it, so `measured` counts assets carrying an actual value and `rows` counts rows written. Where the two differ, the block was scored on far less than the row count suggests. `snapshot` is the date the counts describe — the same latest-at-or-before-today row the screen itself used, so an age above zero means today's ranking was built on older data, not that nothing was collected."
          >
            <TableWrap>
              <table className="w-full min-w-[32rem] border-collapse text-sm">
                <caption className="sr-only">
                  Share of the screened universe measurable in each input table
                </caption>
                <thead>
                  <tr className="border-b border-line text-left">
                    <th scope="col" className="py-2 pr-3">Table</th>
                    <th scope="col" className="py-2 pr-3">Snapshot</th>
                    <th scope="col" className="py-2 pr-3 text-right">Age</th>
                    <th scope="col" className="py-2 pr-3 text-right">Measured</th>
                    <th scope="col" className="py-2 pr-3 text-right">Rows</th>
                    <th scope="col" className="py-2 text-right">Share of universe</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(data.coverage).map(([table, stats]) => (
                    <tr key={table} className="border-b border-line/40">
                      <td className="py-2 pr-3 font-mono text-xs">{table}</td>
                      <td className="py-2 pr-3 font-mono text-xs text-muted">
                        {stats.as_of ?? DASH}
                      </td>
                      <td
                        className={`num py-2 pr-3 ${
                          (stats.age_days ?? 0) > 1 ? "text-fail" : "text-muted"
                        }`}
                      >
                        {stats.age_days === null ? DASH : `${stats.age_days}d`}
                      </td>
                      <td className="num py-2 pr-3">{compactInt(stats.assets)}</td>
                      <td className="num py-2 pr-3 text-muted">
                        {compactInt(stats.rows_present)}
                      </td>
                      <td
                        className={`num py-2 ${
                          (stats.of_universe ?? 0) < 0.5 ? "text-fail" : "text-pass"
                        }`}
                      >
                        {stats.of_universe === null ? DASH : pct(stats.of_universe, 1)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          </Section>

          {/* Coverage answers "was there a row". This answers whether the
              block separated one asset from another -- which is the question
              a reader of the score actually needs answered. A block scoring
              the same value for all 207 survivors changes no ordering and so
              raises no error anywhere, while the total still looks like six
              things were weighed. */}
          <Section
            title="Did each block separate anything?"
            note="A Layer 2 block that scores every asset identically occupies its weight without contributing information. Nothing is wrong with the ranking — it simply rests on fewer inputs than the weights imply. Reported, never silently renormalised away: changing a weight is a recorded decision."
          >
            <TableWrap>
              <table className="w-full min-w-[34rem] border-collapse text-sm">
                <caption className="sr-only">
                  Layer 2 blocks, their weight, and how many distinct values each produced
                </caption>
                <thead>
                  <tr className="border-b border-line text-left">
                    <th scope="col" className="py-2 pr-3">Block</th>
                    <th scope="col" className="py-2 pr-3 text-right">Weight</th>
                    <th scope="col" className="py-2 pr-3 text-right">Scored</th>
                    <th scope="col" className="py-2 pr-3 text-right">Distinct</th>
                    <th scope="col" className="py-2">Separated</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(data.blocks ?? {}).map(([block, stats]) => (
                    <tr key={block} className="border-b border-line/40">
                      <td className="py-2 pr-3 font-mono text-xs">{block}</td>
                      <td className="num py-2 pr-3 text-muted">{stats.weight ?? DASH}</td>
                      <td className="num py-2 pr-3">
                        {compactInt(stats.scored)}/{compactInt(stats.of_ranked)}
                      </td>
                      <td className="num py-2 pr-3">{compactInt(stats.distinct_values)}</td>
                      <td className="py-2">
                        <Flag tone={stats.informative ? "pass" : "fail"}>
                          {stats.informative
                            ? "yes"
                            : stats.distinct_values === 0
                              ? "not scored"
                              : "one value for all"}
                        </Flag>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
            {Object.keys(data.blocks ?? {}).length === 0 ? (
              <p className="mt-3 text-sm text-muted">
                No ranking has been published yet, so there are no block scores to
                describe.
              </p>
            ) : null}
          </Section>

          <Section
            title="Latency"
            note="News lag is publish-to-fetch. Trigger lag is scheduled-to-actual on the external cron, which is the number that shows whether the scheduler is drifting."
          >
            <dl className="grid gap-6 sm:grid-cols-2">
              <div>
                <dt className="text-xs text-muted">News lag (n={data.news_lag_seconds.n})</dt>
                <dd className="mt-1 font-mono text-sm">
                  p50 {num(data.news_lag_seconds.p50, 0)}s · p95{" "}
                  {num(data.news_lag_seconds.p95, 0)}s
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">
                  Trigger lag (n={data.trigger_lag_seconds.n})
                </dt>
                <dd className="mt-1 font-mono text-sm">
                  p50 {num(data.trigger_lag_seconds.p50, 0)}s · p95{" "}
                  {num(data.trigger_lag_seconds.p95, 0)}s
                </dd>
              </div>
            </dl>
          </Section>

          <Section title="Tables">
            <TableWrap>
              <table className="w-full min-w-[28rem] border-collapse text-sm">
                <caption className="sr-only">Writes and last write per table</caption>
                <thead>
                  <tr className="border-b border-line text-left">
                    <th scope="col" className="py-2 pr-3">Table</th>
                    <th scope="col" className="py-2 pr-3 text-right">Writes</th>
                    <th scope="col" className="py-2">Last write</th>
                  </tr>
                </thead>
                <tbody>
                  {data.tables.map((table) => (
                    <tr key={table.table_name} className="border-b border-line/40">
                      <td className="py-2 pr-3 font-mono text-xs">{table.table_name}</td>
                      <td className="num py-2 pr-3">{compactInt(table.row_count)}</td>
                      <td className="py-2 text-xs">
                        <Age iso={table.last_write_utc} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
            <p className="mt-3 text-xs text-muted">
              This counts WRITES, not rows, and only ever grows: an upsert that
              overwrites a row it wrote yesterday increments it again, so a re-run
              inflates the figure without adding a row. Read it as growth and
              liveness. Turso meters row READS, so a `SELECT COUNT(*)` over a
              time-series table to answer a status question would consume the free
              allowance to report on itself (D-069).
            </p>
          </Section>

          {history.state === "ready" && history.data.days.length ? (
            <Section
              title="Funnel history"
              note="The survival rate over time, which is the number the Screen page makes a claim about. One day inside the band says nothing; a drift toward an edge is what calls for a recorded threshold decision instead of a quiet change to a number that disqualified something interesting."
            >
              <TableWrap>
                <table className="w-full min-w-[28rem] border-collapse text-sm">
                  <caption className="sr-only">
                    Universe, survivors and survival rate per run date
                  </caption>
                  <thead>
                    <tr className="border-b border-line text-left">
                      <th scope="col" className="py-2 pr-3">Run date</th>
                      <th scope="col" className="py-2 pr-3 text-right">Universe</th>
                      <th scope="col" className="py-2 pr-3 text-right">Survivors</th>
                      <th scope="col" className="py-2 text-right">Survival rate</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...history.data.days].reverse().map((day) => {
                      const rate = day.survival_rate;
                      const outside =
                        rate !== null && (rate < SURVIVAL_BAND.min || rate > SURVIVAL_BAND.max);
                      return (
                        <tr key={day.run_date} className="border-b border-line/40">
                          <td className="py-2 pr-3 font-mono text-xs">{day.run_date}</td>
                          <td className="num py-2 pr-3">{compactInt(day.universe)}</td>
                          <td className="num py-2 pr-3">{compactInt(day.survivors)}</td>
                          <td className={`num py-2 ${outside ? "text-fail" : "text-muted"}`}>
                            {rate === null ? DASH : pct(rate)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </TableWrap>
              <p className="mt-3 text-xs text-muted">
                {history.data.days.length} run
                {history.data.days.length === 1 ? "" : "s"} recorded over a{" "}
                {history.data.window_days}-day window. A rate in red fell outside the{" "}
                {pct(SURVIVAL_BAND.min, 0)}–{pct(SURVIVAL_BAND.max, 0)} design band.
              </p>
            </Section>
          ) : null}

          {data.recent_failures.length ? (
            <Section title="Recent failures">
              <ul className="space-y-2 text-sm">
                {data.recent_failures.map((failure, index) => (
                  <li key={index} className="border-l-2 border-fail pl-3">
                    <p className="font-mono text-xs">
                      {failure.collector_name} · {failure.started_at_utc}
                    </p>
                    <p className="mt-0.5 text-xs text-muted">
                      {failure.error_message ?? "no message recorded"}
                    </p>
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          <Section title="Hosted database">
            <p className="max-w-3xl text-sm text-muted">{data.turso_usage.reason}</p>
          </Section>
        </>
      )}
    </Gate>
  );
}

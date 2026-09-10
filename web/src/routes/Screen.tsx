import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import Funnel from "../components/Funnel";
import { DarkChecksNotice, Flag, MicroBar, Section, TableWrap } from "../components/Bits";
import { Gate } from "../components/States";
import { getLatest } from "../lib/data";
import { num, pct, signedPct } from "../lib/format";
import { useData } from "../lib/useData";
import type { RankedRow } from "../lib/types";

const BLOCKS = [
  ["fundamental", "Fund"],
  ["supply", "Supp"],
  ["sector", "Sect"],
  ["events", "Evnt"],
  ["attention", "Attn"],
  ["drawdown", "DD"],
] as const;

type SortKey = "rank" | "score" | "asset";

export default function Screen() {
  const latest = useData(getLatest);
  const [sector, setSector] = useState("all");
  const [sort, setSort] = useState<SortKey>("rank");
  const [showAll, setShowAll] = useState(false);

  const rows: RankedRow[] = latest.state === "ready" ? latest.data.ranked : [];
  const sectors = useMemo(
    () => ["all", ...Array.from(new Set(rows.map((r) => r.sector))).sort()],
    [rows],
  );

  const visible = useMemo(() => {
    const limit =
      latest.state === "ready" && !showAll ? latest.data.report_top_n : rows.length;
    const filtered = sector === "all" ? rows : rows.filter((r) => r.sector === sector);
    const sorted = [...filtered].sort((a, b) => {
      if (sort === "asset") return a.asset.localeCompare(b.asset);
      if (sort === "score") return b.score - a.score;
      return a.rank - b.rank;
    });
    return sorted.slice(0, limit);
  }, [rows, sector, sort, showAll, latest]);

  return (
    <Gate
      data={latest}
      what="today's screen"
      missingTitle="No screen published yet"
      missingBody="The pipeline has not published a run. Once collect, screen and publish have each completed once, the funnel and the ranked table appear here."
    >
      {(data) => (
        <>
          <p className="font-mono text-xs text-muted">
            run date {data.run_date} · published {data.generated_at_utc}
            {data.regime?.regime ? ` · regime ${data.regime.regime}` : ""}
            {data.regime?.btc_return_30d !== null &&
            data.regime?.btc_return_30d !== undefined
              ? ` · BTC 30d ${signedPct(data.regime.btc_return_30d)}`
              : ""}
          </p>

          <div className="mt-8">
            <Funnel data={data.funnel} />
          </div>

          {data.dark_checks.length ? (
            <div className="mt-8">
              <DarkChecksNotice checks={data.dark_checks} />
            </div>
          ) : null}

          <Section
            title={`Ranked (${visible.length} of ${rows.length})`}
            note="Cross-sectional percentiles within today's survivors. A blank block had no data for that asset, and its weight was redistributed across the blocks that did rather than counted as zero."
          >
            <div className="mb-4 flex flex-wrap items-end gap-4">
              <label className="text-xs text-muted">
                <span className="block">Sector</span>
                <select
                  value={sector}
                  onChange={(event) => setSector(event.target.value)}
                  className="mt-1 border border-line bg-surface px-2 py-1 text-sm text-ink"
                >
                  {sectors.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-xs text-muted">
                <span className="block">Sort</span>
                <select
                  value={sort}
                  onChange={(event) => setSort(event.target.value as SortKey)}
                  className="mt-1 border border-line bg-surface px-2 py-1 text-sm text-ink"
                >
                  <option value="rank">rank</option>
                  <option value="score">score</option>
                  <option value="asset">ticker</option>
                </select>
              </label>
              <button
                type="button"
                onClick={() => setShowAll((value) => !value)}
                className="border border-line px-2 py-1 text-sm text-muted hover:text-ink"
              >
                {showAll ? `Show top ${data.report_top_n}` : `Show all ${rows.length}`}
              </button>
            </div>

            <TableWrap narrow="hide">
              <table className="w-full min-w-[46rem] border-collapse text-sm">
                <caption className="sr-only">
                  Ranked survivors with their six Layer 2 block scores and Layer 3 flags
                </caption>
                <thead>
                  <tr className="border-b border-line text-left">
                    <th scope="col" className="py-2 pr-3 text-right">
                      #
                    </th>
                    <th scope="col" className="py-2 pr-3">
                      Asset
                    </th>
                    <th scope="col" className="py-2 pr-3 text-right">
                      Score
                    </th>
                    {BLOCKS.map(([key, label]) => (
                      <th key={key} scope="col" className="py-2 pr-3 text-right">
                        {label}
                      </th>
                    ))}
                    <th scope="col" className="py-2 pr-3">
                      Sector
                    </th>
                    <th scope="col" className="py-2">
                      Flags
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((row) => (
                    <tr key={row.asset} className="border-b border-line/50 hover:bg-surface">
                      <td className="num py-2 pr-3 text-muted">{row.rank}</td>
                      <td className="py-2 pr-3">
                        <Link
                          to={`/asset/${encodeURIComponent(row.asset)}`}
                          className="font-mono hover:text-pass"
                        >
                          {row.asset}
                        </Link>
                      </td>
                      <td className="num py-2 pr-3">{num(row.score)}</td>
                      {BLOCKS.map(([key, label]) => (
                        <td key={key} className="py-2 pr-3 text-right">
                          <MicroBar value={row.blocks[key]} label={label} />
                        </td>
                      ))}
                      <td className="py-2 pr-3 text-xs text-muted">{row.sector}</td>
                      <td className="py-2">
                        <span className="flex flex-wrap gap-1">
                          {row.flags.map((flag) => (
                            <Flag key={flag} tone="pass">
                              {flag}
                            </Flag>
                          ))}
                          {row.layer3?.setup_detected ? (
                            <Flag tone="pass">{row.layer3.setup_type ?? "setup"}</Flag>
                          ) : null}
                          {(row.layer3?.risk_flags ?? []).map((flag) => (
                            <Flag key={flag} tone="fail">
                              {flag}
                            </Flag>
                          ))}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>

            {/* Below sm the same rows render stacked. A side-scrolling table on a
                phone hides the six block scores that carry the argument, so the
                reader sees a rank and a ticker and never learns the rest exists. */}
            <ul className="divide-y divide-line border-t border-line sm:hidden">
              {visible.map((row) => (
                <li key={row.asset} className="py-4">
                  <div className="flex items-baseline justify-between gap-3">
                    <p>
                      <span className="num mr-2 text-muted">{row.rank}</span>
                      <Link
                        to={`/asset/${encodeURIComponent(row.asset)}`}
                        className="font-mono text-base hover:text-pass"
                      >
                        {row.asset}
                      </Link>
                    </p>
                    <p className="num text-base">{num(row.score)}</p>
                  </div>
                  <p className="mt-1 text-xs text-muted">{row.sector}</p>
                  <dl className="mt-3 grid grid-cols-3 gap-x-4 gap-y-2">
                    {BLOCKS.map(([key, label]) => (
                      <div key={key} className="flex items-baseline justify-between gap-2">
                        <dt className="text-[11px] text-muted">{label}</dt>
                        <dd>
                          <MicroBar value={row.blocks[key]} label={label} />
                        </dd>
                      </div>
                    ))}
                  </dl>
                  {row.flags.length ||
                  row.layer3?.setup_detected ||
                  (row.layer3?.risk_flags ?? []).length ? (
                    <p className="mt-3 flex flex-wrap gap-1">
                      {row.flags.map((flag) => (
                        <Flag key={flag} tone="pass">
                          {flag}
                        </Flag>
                      ))}
                      {row.layer3?.setup_detected ? (
                        <Flag tone="pass">{row.layer3.setup_type ?? "setup"}</Flag>
                      ) : null}
                      {(row.layer3?.risk_flags ?? []).map((flag) => (
                        <Flag key={flag} tone="fail">
                          {flag}
                        </Flag>
                      ))}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>

            {/* Two different zeroes, and conflating them is the same mistake the
                rest of the system exists to avoid. universe === 0 means nothing
                was SCREENED -- no data. survivors === 0 with a real universe
                means everything was MEASURED and everything failed. The first is
                a pipeline problem, the second is a threshold problem. */}
            {data.funnel.universe === 0 ? (
              <p className="mt-4 max-w-3xl text-sm text-muted">
                Nothing was screened on this run: the universe is empty. That is a
                collection problem rather than a screening result — no asset was
                measured, so no asset passed or failed. Check the Health page for the
                last successful collector run.
              </p>
            ) : rows.length === 0 ? (
              <p className="mt-4 max-w-3xl text-sm text-muted">
                {data.funnel.universe} assets were screened and none survived Layer 1.
                Everything was measured and everything failed, which is a threshold
                problem rather than a data problem — and the response is a recorded
                decision, not a loosened number.
              </p>
            ) : null}
            <p className="mt-4 text-xs text-muted">
              {pct(data.funnel.survival_rate)} of the universe survived. Layer 3 flags are
              advisory: they never raise a score, and no derivatives reading is a buy
              trigger.
            </p>
          </Section>
        </>
      )}
    </Gate>
  );
}

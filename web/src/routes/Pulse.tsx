import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Section, TableWrap } from "../components/Bits";
import { Empty, Failed, Loading } from "../components/States";
import {
  Delta,
  FLAG_EXPLAIN,
  OI_CONDITIONER_NOTE,
  OI_EXPLAIN,
  OiChip,
  RiskChip,
  Spark,
  StateChip,
} from "../components/PulseBits";
import { PULSE_REFRESH_MS, PULSE_STALE_AFTER_HOURS, getPulse } from "../lib/data";
import { DASH, hhmm, num, relative } from "../lib/format";
import { useData, useNow } from "../lib/useData";
import type { Pulse as PulseData, PulseAsset } from "../lib/types";

// The hourly clock (docs/PLAN_ACTIVE_SCREENER.md section 6). The Gem screen
// changes daily; this page changes every hour, and says exactly which hour it
// is showing. An hour it cannot vouch for is shown as unavailable, never as
// current.

const MOVERS_N = 20;

const ALIGNED_RULE =
  "ALIGNED = Gem rank ≤ 25, Pulse ≥ 70, a bullish 4H structure (bull break or bull trend), and no risk flag. It is the only thing that raises an alert.";

const EXCLUSION_TEXT: Record<string, string> = {
  thin_book: "thin book: 24h volume below the floor, where taker flow is noise",
  insufficient_history: "insufficient history: too few closed 1H bars to score",
};

function ageHoursAt(iso: string | null | undefined, now: number): number {
  if (!iso) return Number.POSITIVE_INFINITY;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? Number.POSITIVE_INFINITY : (now - t) / 3_600_000;
}

function assetPath(asset: string) {
  // Routing goes by ticker; the Asset page resolves the file through
  // manifest.json (D-066), exactly as the Screen table's links do.
  return `/asset/${encodeURIComponent(asset)}`;
}

function Unavailable({ why, pulse }: { why: string; pulse: PulseData | null }) {
  return (
    <>
      <PageTitle />
      <div className="mt-6 max-w-2xl border-l-2 border-fail/60 py-8 pl-5" role="status">
        <h2 className="text-base font-medium">Pulse unavailable</h2>
        <p className="mt-2 text-sm leading-relaxed text-muted">{why}</p>
        {pulse?.as_of_utc ? (
          <p className="mt-2 font-mono text-xs text-muted">
            last scored hour {pulse.as_of_utc}
            {pulse.score_version ? ` · ${pulse.score_version}` : ""}
          </p>
        ) : null}
        <p className="mt-3 text-sm leading-relaxed text-muted">
          Nothing is shown in its place: an old hour displayed as current is worse than none.
          The daily Gem screen is unaffected.{" "}
          <Link to="/" className="text-ink hover:text-pass">
            Go to the screen
          </Link>
          .
        </p>
      </div>
    </>
  );
}

function PageTitle({ children }: { children?: React.ReactNode }) {
  return (
    <div>
      <h2 className="text-base font-medium">Pulse{children}</h2>
      <p className="mt-1 max-w-3xl text-sm text-muted">
        An hourly read of order flow, structure and momentum across today&apos;s Layer 1
        survivors. It never overrides the daily screen: an asset absent there cannot appear
        here.
      </p>
    </div>
  );
}

// -- rows --------------------------------------------------------------------------

function Chips({ a }: { a: PulseAsset }) {
  return (
    <span className="flex flex-wrap gap-1">
      <StateChip tf="4H" state={a.state_4h} barsSince={a.bars_since_4h} />
      <StateChip tf="1H" state={a.state_1h} />
      <OiChip quadrant={a.oi_quadrant} />
      {(a.flags ?? []).map((flag) => (
        <RiskChip key={flag} flag={flag} />
      ))}
    </span>
  );
}

function AssetLink({ a }: { a: PulseAsset }) {
  return (
    <Link
      to={assetPath(a.asset)}
      className="font-mono hover:text-pass"
      onClick={(event) => event.stopPropagation()}
    >
      {a.asset}
    </Link>
  );
}

function MoversTable({ rows, caption }: { rows: PulseAsset[]; caption: string }) {
  const navigate = useNavigate();
  return (
    <>
      <TableWrap narrow="hide">
        <table className="w-full min-w-[60rem] border-collapse text-sm">
          <caption className="sr-only">{caption}</caption>
          <thead>
            <tr className="border-b border-line text-left">
              <th scope="col" className="py-2 pr-3 text-right">#</th>
              <th scope="col" className="py-2 pr-3">Asset</th>
              <th scope="col" className="py-2 pr-3 text-right">Pulse</th>
              <th scope="col" className="py-2 pr-3 text-right">Δ 1h</th>
              <th scope="col" className="py-2 pr-3 text-right">Gem</th>
              <th scope="col" className="py-2 pr-3">Structure · OI · risk</th>
              <th scope="col" className="py-2 pr-3">1H + flow</th>
              <th scope="col" className="py-2">4H · 7d</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr
                key={a.asset}
                onClick={() => navigate(assetPath(a.asset))}
                className="cursor-pointer border-b border-line/50 align-middle hover:bg-surface"
              >
                <td className="num py-2 pr-3 text-muted">{a.rank ?? DASH}</td>
                <td className="py-2 pr-3">
                  <AssetLink a={a} />
                  {a.aligned ? (
                    <span className="ml-2 border border-pass px-1 font-mono text-[9px] uppercase text-pass">
                      aligned
                    </span>
                  ) : null}
                </td>
                <td className="num py-2 pr-3">{num(a.score)}</td>
                <td className="py-2 pr-3 text-right">
                  <Delta value={a.delta} />
                </td>
                <td className="num py-2 pr-3 text-muted">{a.gem_rank ?? DASH}</td>
                <td className="max-w-[20rem] py-2 pr-3">
                  <Chips a={a} />
                </td>
                <td className="py-2 pr-3">
                  <Spark label="1H" closes={a.spark_1h} flow={a.flow_1h} />
                </td>
                <td className="py-2">
                  <Spark label="4H" closes={a.spark_4h} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableWrap>

      {/* Stacked below sm, like the Screen table: side-scrolling would hide the
          chips and sparklines that carry the reading. */}
      <ul className="divide-y divide-line border-t border-line sm:hidden">
        {rows.map((a) => (
          <li key={a.asset} className="py-4">
            <div className="flex items-baseline justify-between gap-3">
              <p>
                <span className="num mr-2 text-muted">{a.rank ?? DASH}</span>
                <Link to={assetPath(a.asset)} className="font-mono text-base hover:text-pass">
                  {a.asset}
                </Link>
                {a.aligned ? (
                  <span className="ml-2 border border-pass px-1 font-mono text-[9px] uppercase text-pass">
                    aligned
                  </span>
                ) : null}
              </p>
              <p className="flex items-baseline gap-3">
                <Delta value={a.delta} />
                <span className="num text-base">{num(a.score)}</span>
              </p>
            </div>
            <p className="mt-1 text-xs text-muted">Gem rank {a.gem_rank ?? DASH}</p>
            <div className="mt-2">
              <Chips a={a} />
            </div>
            <div className="mt-3 flex flex-wrap gap-6">
              <Spark label="1H" closes={a.spark_1h} flow={a.flow_1h} width={120} />
              <Spark label="4H" closes={a.spark_4h} width={120} />
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

// -- full sortable table -------------------------------------------------------------

type SortKey = "rank" | "asset" | "score" | "delta" | "gem_rank" | "coverage";
type SortDir = "asc" | "desc";

const COLUMNS: { key: SortKey; label: string; numeric: boolean; defaultDir: SortDir }[] = [
  { key: "rank", label: "#", numeric: true, defaultDir: "asc" },
  { key: "asset", label: "Asset", numeric: false, defaultDir: "asc" },
  { key: "score", label: "Pulse", numeric: true, defaultDir: "desc" },
  { key: "delta", label: "Δ 1h", numeric: true, defaultDir: "desc" },
  { key: "gem_rank", label: "Gem", numeric: true, defaultDir: "asc" },
  { key: "coverage", label: "Coverage", numeric: true, defaultDir: "desc" },
];

function compare(a: PulseAsset, b: PulseAsset, key: SortKey, dir: SortDir): number {
  if (key === "asset") {
    const c = a.asset.localeCompare(b.asset);
    return dir === "asc" ? c : -c;
  }
  const av = a[key];
  const bv = b[key];
  const aMissing = typeof av !== "number" || Number.isNaN(av);
  const bMissing = typeof bv !== "number" || Number.isNaN(bv);
  // Blanks sort LAST in both directions: a missing value is not a small one.
  if (aMissing && bMissing) return a.asset.localeCompare(b.asset);
  if (aMissing) return 1;
  if (bMissing) return -1;
  const c = (av as number) - (bv as number);
  return c === 0 ? a.asset.localeCompare(b.asset) : dir === "asc" ? c : -c;
}

function FullTable({ assets }: { assets: PulseAsset[] }) {
  const navigate = useNavigate();
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: "rank", dir: "asc" });
  const sorted = useMemo(
    () => [...assets].sort((a, b) => compare(a, b, sort.key, sort.dir)),
    [assets, sort],
  );
  const toggle = (key: SortKey, defaultDir: SortDir) =>
    setSort((s) =>
      s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: defaultDir },
    );

  return (
    <TableWrap>
      <table className="w-full min-w-[46rem] border-collapse text-sm">
        <caption className="sr-only">
          Every scored asset this hour. Column headers are buttons that sort the table.
        </caption>
        <thead>
          <tr className="border-b border-line text-left">
            {COLUMNS.map((col) => {
              const active = sort.key === col.key;
              return (
                <th
                  key={col.key}
                  scope="col"
                  aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
                  className={`py-2 pr-3 ${col.numeric ? "text-right" : ""}`}
                >
                  <button
                    type="button"
                    onClick={() => toggle(col.key, col.defaultDir)}
                    className={`uppercase tracking-wide hover:text-ink ${active ? "text-ink" : ""}`}
                  >
                    {col.label}
                    <span aria-hidden="true">
                      {active ? (sort.dir === "asc" ? " ↑" : " ↓") : ""}
                    </span>
                  </button>
                </th>
              );
            })}
            <th scope="col" className="py-2">Structure · OI · risk</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((a) => (
            <tr
              key={a.asset}
              onClick={() => navigate(assetPath(a.asset))}
              className="cursor-pointer border-b border-line/50 hover:bg-surface"
            >
              <td className="num py-2 pr-3 text-muted">{a.rank ?? DASH}</td>
              <td className="py-2 pr-3">
                <AssetLink a={a} />
                {a.aligned ? <span className="ml-1 text-[10px] text-pass">aligned</span> : null}
              </td>
              <td className="num py-2 pr-3">{num(a.score)}</td>
              <td className="py-2 pr-3 text-right">
                <Delta value={a.delta} />
              </td>
              <td className="num py-2 pr-3 text-muted">{a.gem_rank ?? DASH}</td>
              <td className="num py-2 pr-3 text-xs text-muted">
                {typeof a.coverage === "number" ? `${Math.round(a.coverage * 100)}%` : DASH}
              </td>
              <td className="py-2">
                <Chips a={a} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableWrap>
  );
}

// -- page ------------------------------------------------------------------------------

function Legend() {
  return (
    <details className="mt-12 max-w-3xl text-sm">
      <summary className="cursor-pointer text-muted hover:text-ink">How to read the chips</summary>
      <div className="mt-4 space-y-4 text-muted">
        <p>
          <strong className="font-medium text-ink">Structure</strong> (4H drives the score,
          1H confirms): bull break, bull trend, neutral, bear trend, bear break. A break
          chip counts bars since the break.
        </p>
        <div>
          <p>
            <strong className="font-medium text-ink">Open interest</strong> (24H reading).{" "}
            {OI_CONDITIONER_NOTE}
          </p>
          <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-[auto_1fr]">
            {Object.entries(OI_EXPLAIN).map(([key, v]) => (
              <div key={key} className="contents">
                <dt className="font-mono text-xs uppercase text-ink">{v.text}</dt>
                <dd className="text-xs">{v.tip}</dd>
              </div>
            ))}
          </dl>
        </div>
        <div>
          <p>
            <strong className="font-medium text-ink">Risk flags</strong> cut the score and
            block ALIGNED.
          </p>
          <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-[auto_1fr]">
            {Object.entries(FLAG_EXPLAIN).map(([key, v]) => (
              <div key={key} className="contents">
                <dt className="font-mono text-xs uppercase text-ink">{v.text}</dt>
                <dd className="text-xs">{v.tip}</dd>
              </div>
            ))}
          </dl>
        </div>
        <p>
          <strong className="font-medium text-ink">Sparklines</strong>: 48 closed 1H bars with
          per-bar taker flow underneath (green above the line = buyers dominant, red below =
          sellers; bar height is relative to that asset&apos;s largest reading), and 42 closed
          4H bars (7 days).
        </p>
      </div>
    </details>
  );
}

function PulseBody({ pulse, now }: { pulse: PulseData; now: number }) {
  const assets = useMemo(() => pulse.assets ?? [], [pulse]);
  const scored = useMemo(
    () =>
      assets
        .filter((a) => typeof a.score === "number" && !Number.isNaN(a.score))
        .sort((a, b) => compare(a, b, "rank", "asc")),
    [assets],
  );
  const byAsset = useMemo(() => new Map(assets.map((a) => [a.asset, a])), [assets]);
  const aligned = (pulse.aligned ?? [])
    .map((name) => byAsset.get(name))
    .filter((a): a is PulseAsset => Boolean(a));
  const movers = scored.slice(0, MOVERS_N);
  const risers = pulse.risers ?? [];
  const excluded = pulse.excluded ?? [];
  const stamp = pulse.generated_at_utc ?? pulse.as_of_utc;

  return (
    <>
      <PageTitle>
        {" "}
        — last updated <span className="font-mono">{hhmm(stamp)} UTC</span>
      </PageTitle>
      <p className="mt-2 font-mono text-xs text-muted" aria-live="polite">
        {relative(stamp, now)} · hour scored {hhmm(pulse.as_of_utc)} UTC
        {pulse.score_version ? ` · ${pulse.score_version}` : ""}
        {typeof pulse.scored === "number" && typeof pulse.universe_size === "number"
          ? ` · ${pulse.scored} of ${pulse.universe_size} survivors scored`
          : ""}
        {` · refreshes every ${PULSE_REFRESH_MS / 60_000} min`}
      </p>
      {pulse.schema_version !== 1 ? (
        <p className="mt-2 text-xs text-fail">
          pulse.json schema {String(pulse.schema_version)}; this page was built for schema 1.
          Some fields may not show.
        </p>
      ) : null}

      <Section title={`Aligned (${aligned.length})`} note={ALIGNED_RULE}>
        {aligned.length ? (
          <MoversTable rows={aligned} caption="Assets ALIGNED this hour, best Pulse first" />
        ) : (
          <p className="border-l-2 border-line py-2 pl-4 text-sm text-muted">
            No asset is aligned this hour. That is the common case, not a fault: all four
            conditions rarely hold at once.
          </p>
        )}
      </Section>

      <Section
        title={`Movers — top ${Math.min(MOVERS_N, scored.length)} by Pulse`}
        note="Δ is the score change since the previous scored hour. Click a row for the asset's daily detail page."
      >
        {movers.length ? (
          <MoversTable rows={movers} caption={`Top ${movers.length} assets by Pulse this hour`} />
        ) : (
          <p className="text-sm text-muted">No asset was scored this hour.</p>
        )}
      </Section>

      <Section title="Biggest risers since last hour">
        {risers.length ? (
          <TableWrap>
            <table className="w-full min-w-[24rem] max-w-xl border-collapse text-sm">
              <caption className="sr-only">Largest score increases since the previous hour</caption>
              <thead>
                <tr className="border-b border-line text-left">
                  <th scope="col" className="py-2 pr-3">Asset</th>
                  <th scope="col" className="py-2 pr-3 text-right">Δ Pulse</th>
                  <th scope="col" className="py-2 pr-3 text-right">Rank</th>
                  <th scope="col" className="py-2 text-right">Was</th>
                </tr>
              </thead>
              <tbody>
                {risers.map((r) => (
                  <tr key={r.asset} className="border-b border-line/50">
                    <td className="py-2 pr-3">
                      <Link to={assetPath(r.asset)} className="font-mono hover:text-pass">
                        {r.asset}
                      </Link>
                    </td>
                    <td className="py-2 pr-3 text-right">
                      <Delta value={r.delta} />
                    </td>
                    <td className="num py-2 pr-3">{r.rank ?? DASH}</td>
                    <td className="num py-2 text-muted">{r.prev_rank ?? DASH}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        ) : (
          <p className="text-sm text-muted">
            No risers to report: there is no previous scored hour to compare against, or no
            score rose.
          </p>
        )}
      </Section>

      <Section
        title={`All scored (${scored.length})`}
        note="Coverage is the share of Pulse weight actually measured for the asset; an unmeasured component scores nothing."
      >
        {scored.length ? <FullTable assets={scored} /> : <p className="text-sm text-muted">None.</p>}
      </Section>

      {/* Collapsible, but the count is always in the summary: an exclusion is
          never hidden, only folded. */}
      <details className="mt-12" open={excluded.length > 0 && excluded.length <= 5}>
        <summary className="cursor-pointer text-base font-medium">
          Excluded ({excluded.length})
          <span className="ml-2 text-sm font-normal text-muted">
            survivors Pulse could not score honestly
          </span>
        </summary>
        {excluded.length ? (
          <ul className="mt-4 grid gap-x-8 gap-y-1 text-sm sm:grid-cols-2 lg:grid-cols-3">
            {excluded.map((e) => (
              <li key={`${e.asset}-${e.reason}`} className="flex items-baseline gap-3">
                <Link to={assetPath(e.asset)} className="w-20 shrink-0 font-mono hover:text-pass">
                  {e.asset}
                </Link>
                <span className="text-xs text-muted">{EXCLUSION_TEXT[e.reason] ?? e.reason}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-3 text-sm text-muted">Every survivor was scored this hour.</p>
        )}
      </details>

      <Legend />
    </>
  );
}

export default function Pulse() {
  const pulse = useData(getPulse, [], { refreshMs: PULSE_REFRESH_MS });
  const now = useNow(30_000);

  if (pulse.state === "loading") return <Loading what="the Pulse" />;
  if (pulse.state === "error") return <Failed what="the Pulse" reason={pulse.reason} />;
  if (pulse.state === "missing") {
    return (
      <Unavailable
        pulse={null}
        why="No hourly Pulse has been published to this site. It is baked hourly and deployed without a commit, so it appears once the hourly pipeline has run."
      />
    );
  }

  const data = pulse.data;
  if (data.status === "unavailable") {
    return (
      <Unavailable
        pulse={data}
        why="The pipeline marked the Pulse unavailable: no hour was scored in the last 3 hours."
      />
    );
  }
  const age = ageHoursAt(data.as_of_utc ?? data.generated_at_utc, now);
  if (age > PULSE_STALE_AFTER_HOURS) {
    return (
      <Unavailable
        pulse={data}
        why={
          Number.isFinite(age)
            ? `The newest scored hour is ${relative(data.as_of_utc ?? data.generated_at_utc, now)}, older than ${PULSE_STALE_AFTER_HOURS} hours.`
            : "The file carries no readable timestamp, so its hour cannot be vouched for."
        }
      />
    );
  }
  if (!Array.isArray(data.assets)) {
    return <Empty title="Pulse file is malformed" body="pulse.json has no assets list." />;
  }
  return <PulseBody pulse={data} now={now} />;
}

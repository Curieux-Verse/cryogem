import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { Flag, MicroBar, Section, TableWrap } from "../components/Bits";
import { Gate } from "../components/States";
import { MissingData, getAsset, getManifest } from "../lib/data";
import { DASH, metric, num, pct, price, signedPct, thresholdValue, usd } from "../lib/format";
import { useData } from "../lib/useData";
import type { AssetDetail } from "../lib/types";

// The detail page. The argument for a verdict, not just the verdict.
//
// The check grid shows every check's COMPUTED VALUE against its threshold, and
// a failing check shows the number rather than a red mark, because the number
// is the argument (spec 16.3). A dark check is rendered differently again: it
// did not pass, it was not asked.
//
// Written for every asset SCREENED, not only survivors. A disqualified asset
// needs its detail page most, because the rejection wall links here.

const BLOCK_LABELS = {
  fundamental: "Fundamental",
  supply: "Supply",
  sector: "Sector",
  events: "Events",
  attention: "Attention",
  drawdown: "Drawdown",
} as const;

function Sparkline({ rows }: { rows: (string | number | null)[][] }) {
  const points = useMemo(() => {
    const closes = rows
      .map((row) => row[4])
      .filter((value): value is number => typeof value === "number");
    if (closes.length < 2) return null;
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const span = max - min || 1;
    return {
      path: closes
        .map((close, index) => {
          const x = (index / (closes.length - 1)) * 100;
          const y = 30 - ((close - min) / span) * 28;
          return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
        })
        .join(" "),
      first: closes[0] as number,
      last: closes[closes.length - 1] as number,
      min,
      max,
      n: closes.length,
    };
  }, [rows]);

  if (!points) {
    return (
      <p className="text-sm text-muted">
        Not enough recorded price history to draw a chart. Bars accumulate from the klines
        collector; they are not reconstructed on demand.
      </p>
    );
  }

  const up = points.last >= points.first;
  return (
    <div>
      <svg
        viewBox="0 0 100 30"
        preserveAspectRatio="none"
        className="h-24 w-full"
        role="img"
        aria-label={`Closing price over the last ${points.n} recorded days, from ${price(points.first)} to ${price(points.last)}`}
      >
        <path
          d={points.path}
          fill="none"
          stroke={up ? "#5FB49C" : "#C2664D"}
          strokeWidth="0.6"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <p className="mt-1 font-mono text-xs text-muted">
        {points.n} days · low {price(points.min)} · high {price(points.max)} · last{" "}
        {price(points.last)}
      </p>
    </div>
  );
}

function CheckGrid({ detail }: { detail: AssetDetail }) {
  const entries = Object.entries(detail.checks);
  if (!entries.length) {
    return <p className="text-sm text-muted">No check values were recorded for this run.</p>;
  }
  return (
    <ul className="grid gap-px bg-line sm:grid-cols-2 lg:grid-cols-3">
      {entries.map(([id, check]) => {
        const dark = check.source_unavailable;
        const tone = dark ? "text-muted" : check.passed ? "text-pass" : "text-fail";
        const label = dark ? "dark" : check.passed ? "pass" : "fail";
        return (
          <li key={id} className="bg-ground p-4">
            <div className="flex items-baseline justify-between gap-2">
              <p className="font-mono text-xs">{id}</p>
              <p className={`font-mono text-[10px] uppercase ${tone}`}>{label}</p>
            </div>
            <p className={`mt-2 font-mono text-xl ${tone}`}>
              {check.value_display || metric(check.value)}
            </p>
            <p className="mt-1 text-xs text-muted">
              threshold {thresholdValue(check.threshold)}
            </p>
            <p className="mt-2 text-xs leading-relaxed text-muted">
              {dark
                ? "The source returned nothing for the entire universe this run, so this check did not contribute to the verdict. It did not pass; it was not asked."
                : check.reason}
            </p>
            {check.description ? (
              <p className="mt-2 text-[11px] leading-relaxed text-muted/70">
                {check.description}
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export default function Asset() {
  const { ticker = "" } = useParams();
  // The filename comes from manifest.json, which maps EVERY screened ticker.
  // latest.json lists only the ranked head, and the old fallback re-derived a
  // name with a different sanitiser from the publisher's, so a rejected
  // non-Latin ticker linked to a file that never existed (D-066).
  const manifest = useData(getManifest);
  const file = useMemo((): string | null | undefined => {
    if (manifest.state === "loading") return undefined;
    if (manifest.state === "ready") return manifest.data.asset_files[ticker] ?? null;
    // No manifest published: the plain-ticker name is the publisher's for any
    // ticker it did not have to sanitise.
    return `assets/${ticker}.json`;
  }, [manifest, ticker]);

  const detail = useData<AssetDetail>(
    () =>
      file
        ? getAsset(file)
        : file === null
          ? Promise.reject(new MissingData(`${ticker} is not in the screened universe`))
          : // Still waiting for the index: stay "loading" rather than settle.
            new Promise<AssetDetail>(() => {}),
    [file],
  );

  // Until the index resolves there is nothing to load yet -- that is waiting,
  // not an error, and it must not flash "Could not load" (D-066).
  if (file === undefined || detail.state === "loading") {
    return <p className="py-12 text-sm text-muted">Loading…</p>;
  }

  return (
    <Gate
      data={detail}
      what={`${ticker}`}
      missingTitle={`No detail page for ${ticker}`}
      missingBody="This asset was not in the most recent screened universe. Detail files are a rendering of one specific run day and are removed when a ticker leaves the universe, rather than left behind showing a verdict from an unknown date."
    >
      {(data) => (
        <>
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
            <h2 className="font-mono text-2xl">{data.asset}</h2>
            <p className="text-xs text-muted">
              {data.sector} · run {data.run_date}
            </p>
          </div>

          <div
            className={`mt-6 border-l-2 py-4 pl-5 ${
              data.verdict.passed ? "border-pass" : "border-fail"
            }`}
          >
            <p className={`text-base ${data.verdict.passed ? "text-pass" : "text-fail"}`}>
              {data.verdict.passed
                ? "Survived Layer 1"
                : `Disqualified on ${data.verdict.failed_checks.length} check${
                    data.verdict.failed_checks.length === 1 ? "" : "s"
                  }`}
            </p>
            {data.verdict.failed_checks.length ? (
              <p className="mt-2 flex flex-wrap gap-1">
                {data.verdict.failed_checks.map((check) => (
                  <Flag key={check} tone="fail">
                    {check}
                  </Flag>
                ))}
              </p>
            ) : null}
            {data.verdict.dark_checks.length ? (
              <p className="mt-3 max-w-2xl text-xs text-muted">
                Dark this run:{" "}
                <span className="font-mono">{data.verdict.dark_checks.join(", ")}</span>. This
                asset is unmeasured on those, which is not the same as clean.
              </p>
            ) : null}
          </div>

          <Section
            title="Layer 1 — the nine checks"
            note="Each check shows its computed value against the threshold it was measured on. A failing check shows the number, because the number is the argument."
          >
            <CheckGrid detail={data} />
          </Section>

          {data.market ? (
            <Section title="Market">
              <dl className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-4">
                {[
                  ["Price", price(data.market.price_usd)],
                  ["Market cap", usd(data.market.market_cap_usd)],
                  ["FDV", usd(data.market.fdv_usd)],
                  ["Spot volume 24h", usd(data.market.spot_volume_24h_usd)],
                  [
                    // Stored as a negative fraction; the label already says "below".
                    "Below ATH",
                    data.market.pct_below_ath === null
                      ? DASH
                      : pct(Math.abs(data.market.pct_below_ath)),
                  ],
                  [
                    "24h change",
                    data.market.price_change_24h_pct === null
                      ? DASH
                      : signedPct((data.market.price_change_24h_pct ?? 0) / 100),
                  ],
                ].map(([label, value]) => (
                  <div key={label}>
                    <dt className="text-xs text-muted">{label}</dt>
                    <dd className="mt-1 font-mono text-sm">{value}</dd>
                  </div>
                ))}
              </dl>
            </Section>
          ) : null}

          <Section title="Price">
            <Sparkline rows={data.prices.rows} />
          </Section>

          {data.layer2 ? (
            <Section
              title="Layer 2 — block percentiles"
              note={`Rank ${data.layer2.rank ?? DASH} of ${data.layer2.universe_size} survivors, total score ${num(data.layer2.total_score)}. A blank block had no data and its weight was redistributed, not zeroed.`}
            >
              <ul className="max-w-xl space-y-3">
                {Object.entries(BLOCK_LABELS).map(([key, label]) => (
                  <li key={key} className="flex items-center justify-between gap-4">
                    <span className="text-sm">{label}</span>
                    <MicroBar
                      value={data.layer2!.blocks[key as keyof typeof BLOCK_LABELS]}
                      label={label}
                    />
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {data.layer3 ? (
            <Section
              title="Layer 3 — structure and risk (advisory)"
              note="Layer 3 never promotes an asset. It says where a thesis would be wrong and how fragile the positioning is. A derivatives reading is a risk check, never a buy trigger."
            >
              <dl className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-4">
                {[
                  ["Setup", String(data.layer3.setup_type ?? DASH)],
                  [
                    "Live",
                    data.layer3.setup_detected ? "yes" : "no",
                  ],
                  [
                    "Invalidation",
                    data.layer3.invalidation_price === null ||
                    data.layer3.invalidation_price === undefined
                      ? DASH
                      : price(Number(data.layer3.invalidation_price)),
                  ],
                  [
                    "Bid depth ±2%",
                    data.layer3.depth_2pct_usd === null ||
                    data.layer3.depth_2pct_usd === undefined
                      ? DASH
                      : usd(Number(data.layer3.depth_2pct_usd)),
                  ],
                ].map(([label, value]) => (
                  <div key={label}>
                    <dt className="text-xs text-muted">{label}</dt>
                    <dd className="mt-1 font-mono text-sm">{value}</dd>
                  </div>
                ))}
              </dl>
              {Array.isArray(data.layer3.risk_flags) && data.layer3.risk_flags.length ? (
                <p className="mt-4 flex flex-wrap gap-1">
                  {(data.layer3.risk_flags as string[]).map((flag) => (
                    <Flag key={flag} tone="fail">
                      {flag}
                    </Flag>
                  ))}
                </p>
              ) : null}
              {!data.layer3.invalidation_price ? (
                <p className="mt-4 max-w-2xl text-xs text-muted">
                  No invalidation level, so this is not reported as a candidate. A setup
                  without a price at which the thesis is dead is not a setup.
                </p>
              ) : null}
            </Section>
          ) : null}

          {data.events.length ? (
            <Section title="Events, ±90 days">
              <TableWrap>
                <table className="w-full min-w-[30rem] border-collapse text-sm">
                  <caption className="sr-only">Scheduled events around this run date</caption>
                  <thead>
                    <tr className="border-b border-line text-left">
                      <th scope="col" className="py-2 pr-3">Date</th>
                      <th scope="col" className="py-2 pr-3">Type</th>
                      <th scope="col" className="py-2 pr-3">Recipient</th>
                      <th scope="col" className="py-2 pr-3 text-right">% circ</th>
                      <th scope="col" className="py-2">Confidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.events.map((event) => (
                      <tr key={event.event_id ?? event.event_date_utc} className="border-b border-line/40">
                        <td className="py-2 pr-3 font-mono text-xs">{event.event_date_utc}</td>
                        <td className="py-2 pr-3 text-xs">{event.event_type}</td>
                        <td className="py-2 pr-3 text-xs text-muted">
                          {event.recipient_type ?? "unknown"}
                        </td>
                        <td className="num py-2 pr-3">
                          {event.pct_of_circulating === null
                            ? DASH
                            : pct(event.pct_of_circulating)}
                        </td>
                        <td className="py-2 text-xs text-muted">{event.confidence}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            </Section>
          ) : null}

          <Section
            title="News, last 7 days"
            note="CONTEXT, NOT SIGNAL. Nothing here feeds any score: sentiment is rule-based, and a labelling layer that reaches the screening layer would let a headline move a ranking."
          >
            {data.news_context.length === 0 ? (
              <p className="text-sm text-muted">No recorded news mentioning this asset.</p>
            ) : (
              <ul className="max-w-3xl space-y-3">
                {data.news_context.map((item) => (
                  <li key={item.news_id} className="border-l border-line pl-3">
                    <p className="text-sm">
                      {item.url ? (
                        <a href={item.url} className="hover:text-pass" rel="noreferrer noopener" target="_blank">
                          {item.title}
                        </a>
                      ) : (
                        item.title
                      )}
                    </p>
                    <p className="mt-0.5 font-mono text-[11px] text-muted">
                      {item.published_at_utc} · {item.source_name ?? "unknown source"}
                      {item.sentiment_label ? ` · ${item.sentiment_label}` : ""}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <p className="mt-12 text-sm">
            <Link to="/" className="text-muted hover:text-ink">
              ← Back to the screen
            </Link>
          </p>
        </>
      )}
    </Gate>
  );
}

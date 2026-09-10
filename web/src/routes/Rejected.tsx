import { useState } from "react";
import { Link } from "react-router-dom";
import { Section, TableWrap } from "../components/Bits";
import { Gate } from "../components/States";
import { getRejected } from "../lib/data";
import { checkLabel, metric, num, usd } from "../lib/format";
import { useData } from "../lib/useData";

// The disqualification wall.
//
// This is the page that demonstrates the system has a spine. Every crypto
// dashboard shows winners; almost none shows a rejection wall with the computed
// number beside the threshold it failed. A failing check shows its VALUE, not a
// red mark, because the number is the argument (spec 16.3).
//
// Ordering is by market cap, not by a would-be L2 score. See DECISIONS D-011:
// scoring a disqualified asset means either admitting it to the cross-section
// (which changes every survivor's percentile) or publishing a number that looks
// comparable to a survivor's and is not.

export default function Rejected() {
  const rejected = useData(getRejected);
  const [open, setOpen] = useState<string | null>(null);

  return (
    <Gate
      data={rejected}
      what="the disqualification wall"
      missingTitle="No disqualifications published yet"
      missingBody="Once a screen has run and published, everything Layer 1 removed appears here, grouped by the check that removed it."
    >
      {(data) => (
        <Section
          title={`${data.total_disqualified} disqualified on ${data.run_date}`}
          note="Grouped by the check that killed each asset, ordered by market cap within a group. An asset failing several checks appears under each. Layer 1 is a binary kill switch: nothing here can be promoted by a later layer, and no score overrides it."
        >
          <ul className="space-y-8">
            {data.groups.map((group) => {
              const isOpen = open === group.check_id;
              const shown = isOpen ? group.assets : group.assets.slice(0, 10);
              return (
                <li key={group.check_id}>
                  <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line pb-2">
                    <h3 className="font-mono text-sm">
                      {group.check_id}
                      <span className="ml-2 font-sans text-xs text-muted">
                        {checkLabel(group.check_id)}
                      </span>
                    </h3>
                    <p className="font-mono text-sm text-fail">
                      {group.count} asset{group.count === 1 ? "" : "s"}
                    </p>
                  </div>
                  {group.description ? (
                    <p className="mt-2 max-w-3xl text-sm text-muted">{group.description}</p>
                  ) : null}

                  <TableWrap>
                    <table className="mt-3 w-full min-w-[38rem] border-collapse text-sm">
                      <caption className="sr-only">
                        Assets disqualified by {group.check_id}, with the computed value
                        and the threshold it failed
                      </caption>
                      <thead>
                        <tr className="border-b border-line/60 text-left">
                          <th scope="col" className="py-2 pr-3">Asset</th>
                          <th scope="col" className="py-2 pr-3 text-right">Market cap</th>
                          <th scope="col" className="py-2 pr-3 text-right">Value</th>
                          <th scope="col" className="py-2 pr-3 text-right">Threshold</th>
                          <th scope="col" className="py-2">Reason</th>
                        </tr>
                      </thead>
                      <tbody>
                        {shown.map((asset) => {
                          const check = asset.checks[group.check_id] ?? {};
                          return (
                            <tr
                              key={asset.asset}
                              className="border-b border-line/40 hover:bg-surface"
                            >
                              <td className="py-2 pr-3">
                                <Link
                                  to={`/asset/${encodeURIComponent(asset.asset)}`}
                                  className="font-mono hover:text-pass"
                                >
                                  {asset.asset}
                                </Link>
                                {asset.failed_checks.length > 1 ? (
                                  <span className="ml-2 text-xs text-muted">
                                    +{asset.failed_checks.length - 1} more
                                  </span>
                                ) : null}
                              </td>
                              <td className="num py-2 pr-3">{usd(asset.market_cap_usd)}</td>
                              <td className="num py-2 pr-3 text-fail">
                                {check.value_display ?? metric(check.value ?? null)}
                              </td>
                              <td className="num py-2 pr-3 text-muted">
                                {num(check.threshold ?? null, 4)}
                              </td>
                              <td className="py-2 text-xs text-muted">{check.reason ?? ""}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </TableWrap>

                  {group.assets.length > 10 ? (
                    <button
                      type="button"
                      onClick={() => setOpen(isOpen ? null : group.check_id)}
                      className="mt-3 border border-line px-2 py-1 text-xs text-muted hover:text-ink"
                    >
                      {isOpen
                        ? "Show fewer"
                        : `Show all ${group.assets.length} in ${group.check_id}`}
                    </button>
                  ) : null}
                </li>
              );
            })}
          </ul>

          {data.groups.length === 0 ? (
            <p className="text-sm text-muted">
              Nothing was disqualified on this run. With a universe of several hundred
              perpetuals that is not a good sign: verify the checks are running and that
              their inputs are populated.
            </p>
          ) : null}
        </Section>
      )}
    </Gate>
  );
}

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Flag, Section, TableWrap } from "../components/Bits";
import { Gate } from "../components/States";
import { getEvents } from "../lib/data";
import { DASH, pct, usd } from "../lib/format";
import { useData } from "../lib/useData";

// The 90-day calendar.
//
// Coloured by recipient type, because that is the field the unlock research
// identified as most predictive: across 16,000+ events roughly 90% were
// negative, and team allocations were the worst. `null` is carried through
// honestly as "unknown" -- a guessed recipient would be worse than an unknown
// one precisely because the page colours by it.

const RECIPIENT_TONE: Record<string, "pass" | "fail" | "neutral"> = {
  team: "fail",
  investor: "fail",
  private_sale: "fail",
  ecosystem: "neutral",
  community: "neutral",
  airdrop: "neutral",
  staking_rewards: "neutral",
  public: "pass",
};

export default function Events() {
  const events = useData(getEvents);
  const [survivorsOnly, setSurvivorsOnly] = useState(true);

  const rows = useMemo(() => {
    if (events.state !== "ready") return [];
    return events.data.events.filter((e) => (survivorsOnly ? e.is_survivor : true));
  }, [events, survivorsOnly]);

  return (
    <Gate
      data={events}
      what="the event calendar"
      missingTitle="No events published yet"
      missingBody="Scheduled unlocks and catalysts appear here once the events collector has run and published."
    >
      {(data) => (
        <Section
          title={`Next ${data.window_days} days`}
          note="Only events whose first_seen date is on or before the run date appear, so this is what was knowable at the time rather than what is known now. Colour is by recipient type, the field the unlock research found most predictive."
        >
          <div className="mb-4 flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={survivorsOnly}
                onChange={(event) => setSurvivorsOnly(event.target.checked)}
                className="h-3.5 w-3.5 accent-pass"
              />
              Survivors only
            </label>
            <p className="text-xs text-muted">
              {rows.length} of {data.events.length} events shown
            </p>
          </div>

          {rows.length === 0 ? (
            <p className="max-w-3xl text-sm text-muted">
              No recorded events in the window. {data.note}
            </p>
          ) : (
            <TableWrap>
              <table className="w-full min-w-[42rem] border-collapse text-sm">
                <caption className="sr-only">
                  Scheduled events over the next {data.window_days} days
                </caption>
                <thead>
                  <tr className="border-b border-line text-left">
                    <th scope="col" className="py-2 pr-3">Date</th>
                    <th scope="col" className="py-2 pr-3">Asset</th>
                    <th scope="col" className="py-2 pr-3">Type</th>
                    <th scope="col" className="py-2 pr-3">Recipient</th>
                    <th scope="col" className="py-2 pr-3 text-right">% circulating</th>
                    <th scope="col" className="py-2 pr-3 text-right">Value</th>
                    <th scope="col" className="py-2">Confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((event) => (
                    <tr key={event.event_id} className="border-b border-line/40">
                      <td className="py-2 pr-3 font-mono text-xs">{event.event_date_utc}</td>
                      <td className="py-2 pr-3">
                        <Link
                          to={`/asset/${encodeURIComponent(event.base_asset)}`}
                          className="font-mono hover:text-pass"
                        >
                          {event.base_asset}
                        </Link>
                        {!event.is_survivor ? (
                          <span className="ml-2 text-xs text-fail">disqualified</span>
                        ) : null}
                      </td>
                      <td className="py-2 pr-3 text-xs">{event.event_type}</td>
                      <td className="py-2 pr-3">
                        <Flag tone={RECIPIENT_TONE[event.recipient_type ?? ""] ?? "neutral"}>
                          {event.recipient_type ?? "unknown"}
                        </Flag>
                      </td>
                      <td className="num py-2 pr-3">
                        {event.pct_of_circulating === null
                          ? DASH
                          : pct(event.pct_of_circulating)}
                      </td>
                      <td className="num py-2 pr-3">{usd(event.magnitude_usd)}</td>
                      <td className="py-2 text-xs text-muted">{event.confidence}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}

          <p className="mt-4 max-w-3xl text-xs text-muted">{data.note}</p>
        </Section>
      )}
    </Gate>
  );
}

import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useData } from "../lib/useData";
import { STALE_AFTER_HOURS, ageHours, getLatest } from "../lib/data";

const ROUTES = [
  { to: "/", label: "Screen" },
  { to: "/pulse", label: "Pulse" },
  { to: "/rejected", label: "Rejected" },
  { to: "/journal", label: "Journal" },
  { to: "/events", label: "Events" },
  { to: "/health", label: "Health" },
];

function humanAge(hours: number): string {
  const days = Math.floor(hours / 24);
  return days >= 1 ? `${days} day${days === 1 ? "" : "s"}` : `${Math.round(hours)} hours`;
}

/** Spec 16.4.6: never display a stale screen as current.
 *
 *  TWO clocks, and checking only one leaves a gap. `generated_at_utc` is when
 *  the JSON was written; `run_date` is the day the screen actually ran. A
 *  manual re-publish of an old screen produces a FRESH publish time over STALE
 *  data, and a banner keyed on publish time alone would stay silent on exactly
 *  the case a reader most needs warning about.
 *
 *  The banner states the ACTUAL age rather than just saying "stale", because
 *  the age is what distinguishes one missed collection from a dead machine. */
function StaleBanner() {
  const latest = useData(getLatest);
  if (latest.state !== "ready") return null;

  const publishedHours = ageHours(latest.data.generated_at_utc);
  const runHours = ageHours(`${latest.data.run_date}T23:59:59Z`);
  const publishStale = publishedHours > STALE_AFTER_HOURS;
  const runStale = runHours > STALE_AFTER_HOURS;
  if (!publishStale && !runStale) return null;

  return (
    <div
      role="alert"
      className="border-b border-fail/40 bg-fail/10 px-4 py-2 text-sm text-fail sm:px-6"
    >
      {runStale ? (
        <>
          <strong className="font-medium">
            This screen ran {humanAge(runHours)} ago.
          </strong>{" "}
          Run date {latest.data.run_date}
          {publishStale
            ? `, published ${humanAge(publishedHours)} ago.`
            : `, re-published ${humanAge(publishedHours)} ago — the page is new but the data is not.`}{" "}
          Nothing below is current.
        </>
      ) : (
        <>
          <strong className="font-medium">
            Published {humanAge(publishedHours)} ago.
          </strong>{" "}
          Run date {latest.data.run_date}. The screen may have run since without the
          dashboard being updated.
        </>
      )}
    </div>
  );
}

export default function Chrome() {
  const { pathname } = useLocation();
  return (
    <div className="min-h-screen">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:bg-surface focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <StaleBanner />
      <header className="border-b border-line">
        <div className="mx-auto max-w-7xl px-4 py-5 sm:px-6">
          <p className="font-mono text-xs text-muted">gem screener</p>
          <h1 className="mt-1 text-lg font-medium">
            A disqualification-first perpetuals screen
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Layer 1 removes. Layer 2 ranks what is left. Layer 3 says where a thesis would
            be wrong. A ranking is not a buy signal.
          </p>
          <nav aria-label="Sections" className="mt-5 flex flex-wrap gap-x-5 gap-y-2">
            {ROUTES.map((route) => (
              <NavLink
                key={route.to}
                to={route.to}
                end={route.to === "/"}
                className={({ isActive }) =>
                  [
                    "border-b-2 pb-1 text-sm transition-colors",
                    isActive
                      ? "border-pass text-ink"
                      : "border-transparent text-muted hover:text-ink",
                  ].join(" ")
                }
              >
                {route.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      {/* min-h reserves the space the content will occupy. Without it the
          loading paragraph is a few lines tall, the data arrives, and the
          footer travels most of a viewport downwards -- measured at CLS 0.97,
          which is a visible jump on every page load, not a metric quibble.
          A floor costs nothing when the content is taller and removes the
          shift entirely when it is not. */}
      <main
        id="main"
        key={pathname}
        className="mx-auto min-h-[70vh] max-w-7xl px-4 py-8 sm:px-6"
      >
        <Outlet />
      </main>
      <footer className="border-t border-line px-4 py-6 text-xs text-muted sm:px-6">
        <div className="mx-auto max-w-7xl space-y-1">
          <p>
            Every number on this page is read from a pre-baked JSON file under{" "}
            <code className="font-mono">data/public/</code>. Nothing is computed in the
            browser, and the page holds no API key.
          </p>
          <p>
            Not investment advice. The journal page is the only page that reports
            outcomes, and it includes the losers.
          </p>
        </div>
      </footer>
    </div>
  );
}

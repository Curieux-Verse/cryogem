import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useData } from "../lib/useData";
import { STALE_AFTER_HOURS, ageHours, getLatest } from "../lib/data";

const ROUTES = [
  { to: "/", label: "Screen" },
  { to: "/rejected", label: "Rejected" },
  { to: "/journal", label: "Journal" },
  { to: "/events", label: "Events" },
  { to: "/health", label: "Health" },
];

/** Spec 16.4.6: never display a stale screen as current. The banner states the
 *  data's ACTUAL age rather than just saying "stale", because the age is what
 *  tells the reader whether one collection was missed or the machine is dead. */
function StaleBanner() {
  const latest = useData(getLatest);
  if (latest.state !== "ready") return null;
  const hours = ageHours(latest.data.generated_at_utc);
  if (hours <= STALE_AFTER_HOURS) return null;

  const days = Math.floor(hours / 24);
  const age = days >= 1 ? `${days} day${days === 1 ? "" : "s"}` : `${Math.round(hours)} hours`;
  return (
    <div
      role="alert"
      className="border-b border-fail/40 bg-fail/10 px-4 py-2 text-sm text-fail sm:px-6"
    >
      <strong className="font-medium">Data is {age} old.</strong>{" "}
      Published {latest.data.generated_at_utc} for run date {latest.data.run_date}. Nothing
      below is current, and the screen has not been re-run since.
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
      <main id="main" key={pathname} className="mx-auto max-w-7xl px-4 py-8 sm:px-6">
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

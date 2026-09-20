import { useEffect, useState } from "react";
import { MissingData, type Loaded } from "./data";

export interface UseDataOptions {
  /** Re-run the loader on this interval while the tab is VISIBLE, and once
   *  immediately when a hidden tab becomes visible again. A background tab
   *  polling a static host all day is waste; a tab brought back after an
   *  hour must not keep showing the old hour. */
  refreshMs?: number;
}

/** Load one JSON payload into a discriminated state.
 *
 *  `missing` is separated from `error` deliberately: the first deploy happens
 *  before the pipeline has ever published, and "not yet" is a different thing
 *  to say than "something broke".
 *
 *  On a REFRESH the page never flashes back to "loading": the last good data
 *  stays up until new data replaces it. A transient network error on refresh
 *  keeps the last good data (the page's own staleness check still fires on
 *  its timestamp), but a 404 is news and replaces it: a file that has been
 *  withdrawn must not keep being displayed. */
export function useData<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  options: UseDataOptions = {},
): Loaded<T> {
  const [state, setState] = useState<Loaded<T>>({ state: "loading" });
  const { refreshMs } = options;

  useEffect(() => {
    let live = true;
    let hasData = false;
    let lastRun = 0;
    setState({ state: "loading" });

    const run = () => {
      lastRun = Date.now();
      loader()
        .then((data) => {
          if (!live) return;
          hasData = true;
          setState({ state: "ready", data });
        })
        .catch((error: unknown) => {
          if (!live) return;
          if (error instanceof MissingData) {
            hasData = false;
            setState({ state: "missing", reason: error.message });
          } else if (!hasData) {
            setState({
              state: "error",
              reason: error instanceof Error ? error.message : String(error),
            });
          }
        });
    };

    run();

    if (!refreshMs) {
      return () => {
        live = false;
      };
    }

    const visible = () =>
      typeof document === "undefined" || document.visibilityState === "visible";
    const timer = window.setInterval(() => {
      if (visible()) run();
    }, refreshMs);
    const onVisibility = () => {
      if (visible() && Date.now() - lastRun >= refreshMs) run();
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      live = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, refreshMs]);

  return state;
}

/** The current time, re-read every `everyMs`. For relative ages ("12 min
 *  ago") and staleness checks that must move while the page sits open. */
export function useNow(everyMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(timer);
  }, [everyMs]);
  return now;
}

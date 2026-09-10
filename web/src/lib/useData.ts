import { useEffect, useState } from "react";
import { MissingData, type Loaded } from "./data";

/** Load one JSON payload into a discriminated state.
 *
 *  `missing` is separated from `error` deliberately: the first deploy happens
 *  before the pipeline has ever published, and "not yet" is a different thing
 *  to say than "something broke". */
export function useData<T>(loader: () => Promise<T>, deps: unknown[] = []): Loaded<T> {
  const [state, setState] = useState<Loaded<T>>({ state: "loading" });

  useEffect(() => {
    let live = true;
    setState({ state: "loading" });
    loader()
      .then((data) => live && setState({ state: "ready", data }))
      .catch((error: unknown) => {
        if (!live) return;
        if (error instanceof MissingData) {
          setState({ state: "missing", reason: error.message });
        } else {
          setState({
            state: "error",
            reason: error instanceof Error ? error.message : String(error),
          });
        }
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}

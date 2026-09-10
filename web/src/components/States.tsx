import type { Loaded } from "../lib/data";

export function Loading({ what }: { what: string }) {
  return (
    <p className="py-12 text-sm text-muted" role="status">
      Loading {what}…
    </p>
  );
}

/** An empty state is part of the build, not an afterthought (spec 16.4.5). It
 *  says what is absent and why, and never shows an encouraging partial number
 *  in place of the missing one. */
export function Empty({ title, body }: { title: string; body: string }) {
  return (
    <div className="max-w-2xl border-l-2 border-line py-8 pl-5">
      <h2 className="text-base font-medium">{title}</h2>
      <p className="mt-2 text-sm leading-relaxed text-muted">{body}</p>
    </div>
  );
}

export function Failed({ what, reason }: { what: string; reason: string }) {
  return (
    <div role="alert" className="max-w-2xl border-l-2 border-fail py-8 pl-5">
      <h2 className="text-base font-medium text-fail">Could not load {what}</h2>
      <p className="mt-2 font-mono text-xs text-muted">{reason}</p>
    </div>
  );
}

/** Render a Loaded<T> or the right non-ready state. Centralised so no page can
 *  forget one and show a blank screen. */
export function Gate<T>({
  data,
  what,
  missingTitle,
  missingBody,
  children,
}: {
  data: Loaded<T>;
  what: string;
  missingTitle?: string;
  missingBody?: string;
  children: (value: T) => React.ReactNode;
}) {
  if (data.state === "loading") return <Loading what={what} />;
  if (data.state === "missing") {
    return (
      <Empty
        title={missingTitle ?? `No ${what} published yet`}
        body={
          missingBody ??
          `${data.reason}. This page will fill in once the pipeline has run and published.`
        }
      />
    );
  }
  if (data.state === "error") return <Failed what={what} reason={data.reason} />;
  return <>{children(data.data)}</>;
}

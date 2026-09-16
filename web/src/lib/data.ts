// Typed fetch of data/public/*.json, with a cache.
//
// The site is static and holds NO key: everything it displays is pre-baked by
// publish.py inside a job that already has the secrets (spec 16.4.1). If a page
// needs something that is not here, the fix is to emit it from the publisher --
// never to add a client-side API call.

import type {
  AssetDetail,
  Events,
  Health,
  History,
  Journal,
  Latest,
  Manifest,
  Rejected,
} from "./types";

/** Vite rewrites this to the configured `base`, so paths work under /repo/. */
const DATA_ROOT = `${import.meta.env.BASE_URL.replace(/\/$/, "")}/data`;

export type Loaded<T> =
  | { state: "loading" }
  | { state: "ready"; data: T }
  /** `missing` is a first-class state, not an error. The first deploy happens
   *  before any journal entries exist, and an empty site must render honestly
   *  rather than showing a stack trace or a spinner forever (spec 16.4.5). */
  | { state: "missing"; reason: string }
  | { state: "error"; reason: string };

const cache = new Map<string, unknown>();

async function loadJson<T>(name: string): Promise<T> {
  const cached = cache.get(name);
  if (cached !== undefined) return cached as T;

  const response = await fetch(`${DATA_ROOT}/${name}`, { cache: "no-cache" });
  if (response.status === 404) {
    throw new MissingData(`${name} has not been published yet`);
  }
  if (!response.ok) {
    throw new Error(`${name}: HTTP ${response.status}`);
  }
  const data = (await response.json()) as T;
  cache.set(name, data);
  return data;
}

export class MissingData extends Error {}

export const getLatest = () => loadJson<Latest>("latest.json");
export const getManifest = () => loadJson<Manifest>("manifest.json");
export const getRejected = () => loadJson<Rejected>("rejected.json");
export const getJournal = () => loadJson<Journal>("journal.json");
export const getEvents = () => loadJson<Events>("events.json");
export const getHealth = () => loadJson<Health>("health.json");
export const getHistory = () => loadJson<History>("history.json");
export const getAsset = (file: string) =>
  loadJson<AssetDetail>(file.replace(/^assets\//, "assets/"));

/** Hours since an ISO instant. Drives the stale-data banner. */
export function ageHours(iso: string): number {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return Number.POSITIVE_INFINITY;
  return (Date.now() - then) / 3_600_000;
}

/** Spec 16.4.6: never display a stale screen as current. */
export const STALE_AFTER_HOURS = 26;

/** Spec 16.3: the band the survival rate is designed to land in.
 *
 *  Defined once because two pages now depend on it: the Screen page states
 *  the band in prose and the Health page colours a run that fell outside it.
 *  Two copies of a threshold drift, and a threshold that drifts quietly is
 *  the one thing this project refuses to allow. */
export const SURVIVAL_BAND = { min: 0.2, max: 0.5 };

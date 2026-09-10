// Copies data/public/ into web/public/data/ so Vite ships it as a static asset.
//
// The dashboard fetches its JSON from ${BASE_URL}/data/*.json. Those files are
// produced by the Python publisher and committed at the repo root, NOT inside
// web/, because publish.yml commits them and build-site.yml triggers on a push
// to data/public/**. Copying at build time keeps one source of truth.
//
// If the directory is absent the build continues with a warning: the very first
// deploy happens before the pipeline has ever published, and the site is
// required to render honestly with no data (spec 16.4.5).

import { cpSync, existsSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = resolve(here, "../../data/public");
const target = resolve(here, "../public/data");

if (!existsSync(source)) {
  console.warn(
    `[copy-data] ${source} does not exist. Building with no data: every page will ` +
      "render its empty state, which is intended behaviour before the first publish.",
  );
  mkdirSync(target, { recursive: true });
  process.exit(0);
}

mkdirSync(target, { recursive: true });
cpSync(source, target, { recursive: true });

const files = readdirSync(target);
const assets = existsSync(resolve(target, "assets"))
  ? readdirSync(resolve(target, "assets")).length
  : 0;
console.log(`[copy-data] copied ${files.length} top-level files + ${assets} asset files`);

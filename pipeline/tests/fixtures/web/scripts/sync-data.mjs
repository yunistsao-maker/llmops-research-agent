// Copy ../data and ../runs into public/ and write public/data/manifest.json
// (lists digests and test runs so the app can discover them without a server).
import { cpSync, existsSync, rmSync, readdirSync, writeFileSync, mkdirSync } from "node:fs";
const root = new URL("../../", import.meta.url).pathname;
const pub = new URL("../public/", import.meta.url).pathname;
for (const d of ["data", "runs", "config"]) {
  if (existsSync(pub + d)) rmSync(pub + d, { recursive: true });
  if (existsSync(root + d)) cpSync(root + d, pub + d, { recursive: true });
}
const list = (dir) => (existsSync(pub + dir) ? readdirSync(pub + dir).filter((f) => f.endsWith(".json")).sort() : []);
mkdirSync(pub + "data", { recursive: true });
const manifest = {
  generatedAt: new Date().toISOString(),
  digests: list("data/digests").map((f) => f.replace(/\.json$/, "")),
  models: list("data/models").map((f) => f.replace(/\.json$/, "")),
  compatRuns: list("runs/compat"),
  perfRuns: list("runs/perf"),
  hasInventory: existsSync(pub + "data/internal/platform-inventory.json"),
};
writeFileSync(pub + "data/manifest.json", JSON.stringify(manifest, null, 2));
console.log("synced data + runs; manifest:", manifest.digests.length, "digests,", manifest.compatRuns.length + manifest.perfRuns.length, "runs");

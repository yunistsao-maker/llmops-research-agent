/// <reference types="vite/client" />
/**
 * Where the portal's data comes from.
 *
 *   bundled (default)  data/, runs/ and config/ were copied into the site when it was built (npm run sync-data).
 *   remote             a data location that the research pipeline keeps up to date.  <base>manifest.json is a small
 *                      pointer naming the release to read; everything else lives under <base>releases/<id>/ and
 *                      never changes, so a page always sees one consistent version of the data.
 *
 * The data location is set at run time in config.json ({"dataBaseUrl": "https://.../"}) or at build time with
 * VITE_DATA_BASE_URL; config.json wins.  With a data location, updating the data never needs a rebuild or a redeploy
 * of this site.
 *
 * Two rules for everything in this file: a request that gets no answer gives up after a while (a page must not sit on
 * "loading" for ever), and no message ever contains the address of the data location (it can carry the access token,
 * and what the footer shows ends up in screenshots and tickets).
 */

export interface ReleaseEntry {
  releaseId: string;
  publishedAt: string;
  kind: string;
  by?: string;
  summary?: string;
  changed?: number;
  proposalId?: string;
  digestId?: string;
}

/** The index every page uses, plus (remote data only) which release this is and where it came from. */
export interface DataManifest {
  generatedAt: string;
  digests: string[];
  models: string[];
  compatRuns: string[];
  perfRuns: string[];
  hasInventory: boolean;
  schema?: string;
  releaseId?: string;
  basePath?: string;
  publishedAt?: string;
  sequence?: number;
  source?: { kind: string; by?: string; proposalId?: string; approvedBy?: string; note?: string };
  history?: ReleaseEntry[];
}

export interface DataSource {
  remote: boolean;
  /** "" for bundled data, otherwise the data location with a trailing slash */
  base: string;
  manifest: DataManifest;
}

const POINTER_SCHEMA = "portal-manifest/v1";
const RELEASE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const CONFIG_TIMEOUT_MS = 8_000;
const POINTER_TIMEOUT_MS = 15_000;
const FILE_TIMEOUT_MS = 30_000;
const NOT_CONFIGURED = "這個網站沒有打包資料，也沒有設定資料位置（config.json 的 dataBaseUrl，或 build 時的 VITE_DATA_BASE_URL）";

/** Gives up after `ms`.  (A browser too old for AbortSignal.timeout simply waits, as it did before.) */
function deadline(ms: number): AbortSignal | undefined {
  return typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function" ? AbortSignal.timeout(ms) : undefined;
}

const timedOut = (e: unknown): boolean => e instanceof DOMException && (e.name === "TimeoutError" || e.name === "AbortError");

/** An error page is of no use to us: drop it so the connection is free for the next request. */
function cancel(r: Response): void {
  void r.body?.cancel().catch(() => undefined);
}

function failed(r: Response, what: string): Error {
  cancel(r);
  return new Error(`${what}：HTTP ${r.status}`);
}

function withSlash(base: string): string {
  const trimmed = base.trim();
  return trimmed === "" || trimmed.endsWith("/") ? trimmed : trimmed + "/";
}

/** How a file of the data location is named in a message: its host at most, never its path. */
function named(base: string, file: string): string {
  if (!base) return file;
  try {
    const u = new URL(base, window.location.href);
    return u.origin === window.location.origin ? `資料位置的 ${file}` : `${u.host} 的 ${file}`;
  } catch {
    return `資料位置的 ${file}`;
  }
}

const text = (v: unknown, max = 300): string | undefined => (typeof v === "string" ? v.slice(0, max) : undefined);
const names = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
const record = (v: unknown): Record<string, unknown> | null => (v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null);

/**
 * The pointer as the rest of the site may rely on it: every field has the type the interface promises, whatever the
 * file really holds.  (A value of the wrong type that reaches a component blanks the whole page.)
 */
function normalise(raw: Record<string, unknown>): DataManifest {
  const src = record(raw.source);
  const history = Array.isArray(raw.history) ? raw.history.map(record).filter((h): h is Record<string, unknown> => h !== null) : undefined;
  return {
    generatedAt: text(raw.generatedAt) ?? "",
    digests: names(raw.digests),
    models: names(raw.models),
    compatRuns: names(raw.compatRuns),
    perfRuns: names(raw.perfRuns),
    hasInventory: raw.hasInventory === true,
    schema: text(raw.schema),
    releaseId: text(raw.releaseId),
    basePath: text(raw.basePath),
    publishedAt: text(raw.publishedAt),
    sequence: typeof raw.sequence === "number" ? raw.sequence : undefined,
    source: src ? { kind: text(src.kind) ?? "", by: text(src.by), proposalId: text(src.proposalId), approvedBy: text(src.approvedBy), note: text(src.note) } : undefined,
    history: history?.map((h) => ({
      releaseId: text(h.releaseId) ?? "", publishedAt: text(h.publishedAt) ?? "", kind: text(h.kind) ?? "", by: text(h.by),
      summary: text(h.summary, 600), changed: typeof h.changed === "number" ? h.changed : undefined,
      proposalId: text(h.proposalId), digestId: text(h.digestId),
    })),
  };
}

/** {"dataBaseUrl": ...} from the site's own config.json; {} when there is none. */
async function runtimeConfig(): Promise<{ dataBaseUrl?: unknown }> {
  let r: Response;
  try {
    r = await fetch("config.json", { cache: "no-store", signal: deadline(CONFIG_TIMEOUT_MS) });
  } catch (e) {
    throw new Error(`讀不到網站的 config.json（${timedOut(e) ? "沒有回應" : "網路中斷"}）`);
  }
  if (r.status === 404) {
    cancel(r);
    return {}; // no config.json: the build-time setting applies
  }
  // Anything else that is not an answer must not be read as "no data location": the site would quietly show the
  // (older) data it was built with, or none, while the real data sits one request away.
  if (!r.ok) throw failed(r, "讀不到網站的 config.json");
  try {
    return record(await r.json()) ?? {};
  } catch {
    return {}; // not JSON: a host that answers unknown paths with its fallback page
  }
}

async function fetchManifest(base: string): Promise<DataManifest> {
  const what = named(base, "manifest.json");
  let r: Response;
  try {
    // the pointer is the one thing that must never be stale
    r = await fetch(base ? base + "manifest.json" : "data/manifest.json", { cache: "no-store", signal: deadline(POINTER_TIMEOUT_MS) });
  } catch (e) {
    if (timedOut(e)) throw new Error(`${what} 沒有回應`);
    // the browser gives no detail for a blocked or failed cross-origin request, so say what the usual causes are
    throw new Error(`連不到${what}（網路中斷，或資料位置不允許這個網站讀取：CORS／存取限制）`);
  }
  if (!r.ok) {
    if (!base && r.status === 404) {
      cancel(r);
      throw new Error(NOT_CONFIGURED);
    }
    throw failed(r, what);
  }
  let raw: Record<string, unknown> | null = null;
  try {
    raw = record(await r.json());
  } catch {
    /* not JSON: a host that answers unknown paths with its fallback page */
  }
  if (!base) {
    if (!raw || !Array.isArray(raw.digests)) throw new Error(NOT_CONFIGURED);
    return normalise(raw);
  }
  const manifest = raw ? normalise(raw) : null;
  const id = manifest?.releaseId ?? "";
  if (!manifest || manifest.schema !== POINTER_SCHEMA || !RELEASE_ID.test(id) || manifest.basePath !== `releases/${id}/`) {
    throw new Error(`${what} 不是有效的資料版本指標（網址對嗎？拿到的可能是網站的 fallback 頁面）`);
  }
  return manifest;
}

let source: Promise<DataSource> | null = null;

/** Resolved once per page load: a page keeps reading the release it started with.  A failure is not kept: the next call tries again. */
export function dataSource(): Promise<DataSource> {
  if (!source) {
    const loading = (async (): Promise<DataSource> => {
      const cfg = await runtimeConfig();
      const configured = typeof cfg.dataBaseUrl === "string" ? cfg.dataBaseUrl : (import.meta.env.VITE_DATA_BASE_URL as string | undefined) ?? "";
      const base = withSlash(configured);
      return { remote: base !== "", base, manifest: await fetchManifest(base) };
    })();
    source = loading;
    loading.catch(() => {
      if (source === loading) source = null;
    });
  }
  return source;
}

export const loadManifest = async (): Promise<DataManifest> => (await dataSource()).manifest;

/**
 * How a data file is named: a plain relative path such as "data/catalog.json" - segments of letters, digits and
 * . _ @ + = - that do not begin with a dot.  (Those are the only names a release holds.)
 *
 * Part of a path can come from the address bar (#/digest/<id> is read as data/digests/<id>.json), so with a data
 * location nothing else is let through: with "..", a "%" or a "?" in it, a link could make this page - which reads
 * ONE release - show a file of another one, for instance of a release that was rolled back.  (Bundled data has no
 * releases: there a path is a path inside this site, and is asked for as it is.)
 */
const DATA_PATH = /^[A-Za-z0-9][A-Za-z0-9._@+=-]*(?:\/[A-Za-z0-9][A-Za-z0-9._@+=-]*)*$/;

/** URL of a data file such as "data/catalog.json", "runs/perf/x.json" or "config/litellm.example.yaml". */
export async function dataUrl(path: string): Promise<string> {
  const s = await dataSource();
  if (!s.remote) return path; // bundled data: a path inside this site, as it always was
  if (typeof path !== "string" || path.length > 512 || !DATA_PATH.test(path)) throw new Error("沒有這個資料檔（名稱不是資料檔的路徑）");
  return s.base + (s.manifest.basePath ?? "") + path;
}

async function get(path: string): Promise<Response> {
  const url = await dataUrl(path); // (not inside the try below: a name that is refused is not a network failure)
  let r: Response;
  try {
    r = await fetch(url, { signal: deadline(FILE_TIMEOUT_MS) });
  } catch (e) {
    throw new Error(timedOut(e) ? `${path} 沒有回應` : `讀不到 ${path}（網路中斷）`);
  }
  if (!r.ok) throw failed(r, path);
  return r;
}

export async function fetchJson<T>(path: string): Promise<T> {
  return (await get(path)).json() as Promise<T>;
}

export async function fetchText(path: string): Promise<string> {
  return (await get(path)).text();
}

/** The record of a release: what changed, why, and the evidence quoted for it (remote data only). */
export function releaseRecordUrl(s: DataSource): string | null {
  return s.remote && s.manifest.basePath ? s.base + s.manifest.basePath + "release.json" : null;
}

/** The release that is live now, if it is not the one this page loaded (always null for bundled data). */
export async function newerRelease(): Promise<DataManifest | null> {
  const s = await dataSource();
  if (!s.remote) return null;
  const latest = await fetchManifest(s.base);
  return latest.releaseId !== s.manifest.releaseId ? latest : null;
}

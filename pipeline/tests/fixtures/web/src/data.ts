import { createContext, useContext, useEffect, useState } from "react";
import type {
  CatalogFile, CompatRun, DeepDive, DigestFile, LifecycleFile, LiteLLMFile, PerfRun,
  PlatformInventory, RequirementsFile, Source, SourceRef, SourcesFile,
} from "../../schema/types";

export interface Manifest { generatedAt: string; digests: string[]; models: string[]; compatRuns: string[]; perfRuns: string[]; hasInventory: boolean }

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json() as Promise<T>;
}
const cache = new Map<string, Promise<unknown>>();
function cached<T>(path: string): Promise<T> {
  if (!cache.has(path)) cache.set(path, getJson<T>(path).catch((e) => { cache.delete(path); throw e; }));
  return cache.get(path) as Promise<T>;
}

export const loaders = {
  manifest: () => cached<Manifest>("data/manifest.json"),
  catalog: () => cached<CatalogFile>("data/catalog.json"),
  lifecycle: () => cached<LifecycleFile>("data/lifecycle.json"),
  litellm: () => cached<LiteLLMFile>("data/litellm.json"),
  requirements: () => cached<RequirementsFile>("data/requirements.json"),
  sources: () => cached<SourcesFile>("data/sources.json"),
  digest: (id: string) => cached<DigestFile>(`data/digests/${id}.json`),
  deepDive: (id: string) => cached<DeepDive>(`data/models/${id}.json`),
  inventory: async () => {
    const m = await loaders.manifest();
    return cached<PlatformInventory>(m.hasInventory ? "data/internal/platform-inventory.json" : "data/internal/platform-inventory.example.json");
  },
  compatRuns: async () => { const m = await loaders.manifest(); return Promise.all(m.compatRuns.map((f) => cached<CompatRun>(`runs/compat/${f}`))); },
  perfRuns: async () => { const m = await loaders.manifest(); return Promise.all(m.perfRuns.map((f) => cached<PerfRun>(`runs/perf/${f}`))); },
};

export function useData<T>(load: () => Promise<T>, deps: unknown[] = []) {
  const [state, set] = useState<{ data?: T; error?: string }>({});
  useEffect(() => {
    let live = true;
    set({});
    load().then((data) => live && set({ data }), (e) => live && set({ error: String(e) }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

/** Sources are loaded once at the top and shared so any page can render SourceRefs as links. */
export const SourcesCtx = createContext<Map<string, Source>>(new Map());
export const useSources = () => useContext(SourcesCtx);
export type { SourceRef };

export const TODAY = new Date();
export const daysFrom = (iso: string, today = TODAY) =>
  Math.round((new Date(iso + "T00:00:00").getTime() - new Date(today.toDateString()).getTime()) / 86400000);
export const money = (n?: number) => (n === undefined ? "—" : `$${Number.isInteger(n) ? n : n < 0.1 ? n.toFixed(3).replace(/0$/, "") : n.toFixed(2)}`);
export const fmt = (n: number | null | undefined, d = 0) => (n === null || n === undefined ? "—" : n.toLocaleString("en-US", { maximumFractionDigits: d }));

/** Browser-local store (per viewer). Used for gate checklists; wrapped so private windows still work. */
export function useLocal<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try { const s = localStorage.getItem(key); return s ? (JSON.parse(s) as T) : initial; } catch { return initial; }
  });
  const set = (nv: T) => { setV(nv); try { localStorage.setItem(key, JSON.stringify(nv)); } catch { /* ignore */ } };
  return [v, set];
}

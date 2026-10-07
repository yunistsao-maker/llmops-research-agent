import type { DigestItem } from "../../../schema/types";
import { go } from "../App";
import { daysFrom, loaders, money, useData } from "../data";
import { PageHead, Sources, Status } from "../ui";

const SECTIONS: [keyof Pick<import("../../../schema/types").DigestFile, "newModels" | "lifecycleChanges" | "pricingChanges" | "litellm" | "security" | "regulatory">, string][] = [
  ["newModels", "新模型"], ["lifecycleChanges", "生命週期變動"], ["pricingChanges", "價格"], ["litellm", "LiteLLM"], ["security", "資安"], ["regulatory", "法規"],
];

export default function Radar({ digestId }: { digestId?: string }) {
  const manifest = useData(loaders.manifest);
  const ids = manifest.data?.digests ?? [];
  const id = digestId ?? ids[ids.length - 1];
  const digest = useData(() => (id ? loaders.digest(id) : Promise.reject("no digest")), [id]);
  const catalog = useData(loaders.catalog);
  const inv = useData(loaders.inventory);
  const lc = useData(loaders.lifecycle);
  if (!digest.data || !catalog.data) return <Status error={digest.error || catalog.error || manifest.error} />;
  const d = digest.data;
  const urgent = inv.data?.deployments.filter((x) => x.risk === "act-now" || x.risk === "retired").length;
  const next30 = lc.data?.events.filter((e) => { const n = daysFrom(e.date); return n >= 0 && n <= 30 && e.event !== "eol-no-sooner-than"; }) ?? [];
  const items = [...d.actionItems].sort((a, b) => (a.priority + (a.due ?? "9")).localeCompare(b.priority + (b.due ?? "9")));
  return (
    <>
      <PageHead eyebrow={`週報 · ${d.periodStart} 至 ${d.periodEnd}`} title="Release Radar" right={ids.length > 1 && (
        <label className="note">歷史週報{" "}
          <select value={id} onChange={(e) => go(`digest/${e.target.value}`)}>{[...ids].reverse().map((x) => <option key={x}>{x}</option>)}</select>
        </label>)} />
      <p className="lead">{d.headline}</p>
      <section className="stats" aria-label="統計">
        <Stat label="目錄中的新模型" value={catalog.data.models.length} />
        <Stat label="平台路由：30 天內到期或已退役" value={urgent ?? "—"} danger onClick={() => go("eol")} />
        <Stat label="全通路 30 天內生命週期事件" value={next30.length} />
        <Stat label="P0 行動項目" value={d.actionItems.filter((a) => a.priority === "P0").length} />
      </section>
      <div className="row">
        <section className="card grow">
          <h2>行動項目</h2>
          {items.map((a) => (
            <div className="list-item" key={a.action}>
              <span className="mono" style={{ width: 100, flexShrink: 0 }}>{a.due ?? "持續"}
                {a.due && <div className="note">{daysFrom(a.due) >= 0 ? `剩 ${daysFrom(a.due)} 天` : `已過 ${-daysFrom(a.due)} 天`}</div>}</span>
              <span className={`tag ${a.priority}`}>{a.priority}</span>
              <span style={{ fontSize: 15, lineHeight: 1.6 }}>{a.action}
                {a.relatedModelIds?.map((m) => <a key={m} href={`#/models/${m}`} className="note" style={{ marginLeft: 8 }}>{m}</a>)}</span>
            </div>))}
        </section>
        <aside className="side">
          <section className="card">
            <h2>目錄中的模型</h2>
            {catalog.data.models.map((m) => {
              const p = m.offerings.flatMap((o) => o.pricing).find((x) => x.input !== undefined);
              return (
                <a key={m.id} href={`#/models/${m.id}`} className="list-item" style={{ justifyContent: "space-between", color: "inherit" }}>
                  <span style={{ fontWeight: 500 }}>{m.name}</span>
                  <span className="note">{[...new Set(m.offerings.map((o) => o.cloud.toUpperCase()))].join("、")} · {money(p?.input)} / {money(p?.output)}</span>
                </a>);
            })}
            <p className="note">價格：每 1M tokens（USD），Global 部署。</p>
          </section>
          <section className="card">
            <h2>Watchlist</h2>
            {d.watchlist.map((w) => (
              <div key={w.name} className="list-item" style={{ display: "block" }}>
                <div style={{ fontWeight: 500 }}>{w.name} <span className="note">{w.status}</span></div>
                <div className="note">{w.expected} — {w.note}</div>
              </div>))}
          </section>
        </aside>
      </div>
      <div className="grid2">
        {SECTIONS.map(([k, label]) => d[k].length > 0 && (
          <section className="card" key={k}>
            <h2>{label}</h2>
            {(d[k] as DigestItem[]).map((it) => (
              <details key={it.title}>
                <summary><span className={`tag imp-${it.impact}`}>{it.impact === "high" ? "高" : it.impact === "medium" ? "中" : "低"}</span> {it.title} {it.date && <span className="note mono">{it.date}</span>}</summary>
                <p style={{ fontSize: 14, lineHeight: 1.7 }}>{it.detail}<Sources refs={it.sources} /></p>
              </details>))}
          </section>))}
      </div>
      {d.unverified.length > 0 && (
        <section className="card"><h2>本期尚未驗證</h2><ul className="bullets">{d.unverified.map((u) => <li key={u} className="src" style={{ fontSize: 14 }}>{u}</li>)}</ul></section>)}
    </>
  );
}

function Stat({ label, value, danger, onClick }: { label: string; value: string | number; danger?: boolean; onClick?: () => void }) {
  const body = <><div className="eyebrow">{label}</div><div className="v" style={danger ? { color: "var(--danger)" } : undefined}>{value}</div></>;
  return onClick ? <button className="card stat statbtn" onClick={onClick}>{body}</button> : <div className="card stat">{body}</div>;
}

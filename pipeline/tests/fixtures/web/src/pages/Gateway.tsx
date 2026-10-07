import { useState } from "react";
import type { LiteLLMFile } from "../../../schema/types";
import { loaders, useData } from "../data";
import { Bullets, PageHead, Sources, Status, Tabs } from "../ui";

type V = [number, number, number];
const parseV = (s: string): V | null => {
  const m = s.trim().replace(/^v/i, "").match(/^(\d+)\.(\d+)\.(\d+)/);
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
};
const cmp = (a: V, b: V) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];

/** Interprets the human-written version specs in litellm.json → avoid[].versions. */
function matchSpec(spec: string, raw: string, v: V | null): boolean {
  return spec.split(/[、,]/).map((p) => p.trim()).filter(Boolean).some((p) => {
    let m;
    if ((m = p.match(/^v?(\d+\.\d+\.\d+)\s*[–—-]\s*v?(\d+\.\d+\.\d+)$/))) return !!v && cmp(v, parseV(m[1])!) >= 0 && cmp(v, parseV(m[2])!) <= 0;
    if ((m = p.match(/^<\s*v?(\d+\.\d+\.\d+)$/))) return !!v && cmp(v, parseV(m[1])!) < 0;
    if ((m = p.match(/^≤\s*v?(\d+)\.(\d+)\.x$/))) return !!v && (v[0] < Number(m[1]) || (v[0] === Number(m[1]) && v[1] <= Number(m[2])));
    if ((m = p.match(/^v?(\d+\.\d+\.\d+)$/))) return !!v && cmp(v, parseV(m[1])!) === 0;
    const tag = p.replace(/\*/g, "").toLowerCase();
    return tag.length > 1 && raw.toLowerCase().includes(tag);
  });
}

function check(raw: string, f: LiteLLMFile) {
  const v = parseV(raw);
  const avoid = f.avoid.filter((a) => matchSpec(a.versions, raw, v));
  const line = v ? `v${v[0]}.${v[1]}.x` : "";
  const inWindow = !!v && f.supportedLines.includes(line);
  const min = parseV(f.minimumBaseline.version)!;
  const rec = parseV(f.recommended.version)!;
  if (avoid.length) return { level: "fail", title: "避免使用這個版本", lines: avoid.map((a) => `${a.versions}：${a.reason}`) };
  if (!v) return { level: "error", title: "無法解析版本", lines: ["請輸入像 1.102.1 或 v1.101.0 的版本號。"] };
  const lines: string[] = [];
  if (!inWindow) lines.push(`${line} 不在官方支援窗口（${f.supportedLines.join("、")}）。`);
  if (cmp(v, min) < 0) lines.push(`低於最低基準 ${f.minimumBaseline.version}。`);
  if (lines.length) return { level: "error", title: "需要升級", lines };
  return cmp(v, rec) === 0
    ? { level: "pass", title: "建議版本", lines: ["在支援窗口內、沒有已知的避免項目。"] }
    : { level: "pass", title: "可用", lines: [`在支援窗口內；建議版本是 ${f.recommended.version}。`] };
}

const VIEWS = ["版本與相容性", "安全公告", "已知陷阱", "設定範例", "Gateway 強化清單"] as const;

export default function Gateway() {
  const { data, error } = useData(loaders.litellm);
  const cfg = useData(() => fetch("config/litellm.example.yaml").then((r) => (r.ok ? r.text() : Promise.reject(r.status))));
  const [view, setView] = useState<(typeof VIEWS)[number]>("版本與相容性");
  const [ver, setVer] = useState("");
  if (!data) return <Status error={error} />;
  const res = ver.trim() ? check(ver, data) : null;
  return (
    <>
      <PageHead eyebrow={`資料更新 ${data.asOf}`} title="LiteLLM 與 gateway 安全" />
      <div className="stats">
        <div className="card stat"><div className="eyebrow">建議版本</div><div className="v">{data.recommended.version}</div></div>
        <div className="card stat"><div className="eyebrow">最低基準</div><div className="v">{data.minimumBaseline.version}</div></div>
        <div className="card stat"><div className="eyebrow">官方支援的 minor 線</div><div className="v" style={{ fontSize: 20 }}>{data.supportedLines.join(" ")}</div></div>
        <div className="card stat"><div className="eyebrow">重大 / 高風險公告</div><div className="v" style={{ color: "var(--danger)" }}>{data.advisories.filter((a) => a.severity === "critical" || a.severity === "high").length}</div></div>
      </div>
      <section className="card">
        <h2>檢查你們的版本</h2>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <label htmlFor="ver" className="note">目前執行的 LiteLLM 版本</label>
          <input id="ver" type="text" value={ver} onChange={(e) => setVer(e.target.value)} placeholder="例：1.83.2 或 main-latest" />
        </div>
        {res && (
          <div style={{ marginTop: 14 }}>
            <strong className={res.level}>{res.title}</strong>
            <ul className="bullets">{res.lines.map((l) => <li key={l}>{l}</li>)}</ul>
          </div>)}
        <p className="note">依 litellm.json 的避免清單與支援窗口判斷；版本號用 <span className="mono">litellm --version</span> 或 /health/readiness 查。</p>
      </section>
      <Tabs tabs={VIEWS} value={view} onChange={setView} />

      {view === "版本與相容性" && (
        <>
          <div className="grid2">
            <section className="card"><h2>建議：{data.recommended.version}</h2><Bullets items={data.recommended.rationale} /><p className="note">{data.recommended.pinning}</p></section>
            <section className="card"><h2>最低基準：{data.minimumBaseline.version}</h2><Bullets items={data.minimumBaseline.rationale} /><p className="note">{data.supportPolicy}</p></section>
          </div>
          <section className="card"><h2>避免清單</h2>
            <table><thead><tr><th>版本</th><th>原因</th><th>來源</th></tr></thead><tbody>
              {data.avoid.map((a) => <tr key={a.versions}><td className="mono">{a.versions}</td><td>{a.reason}</td><td><Sources refs={a.sources} /></td></tr>)}
            </tbody></table></section>
          <section className="card"><h2>版本紀錄</h2>
            <table><thead><tr><th>版本</th><th>日期</th><th>支援</th><th>重點</th><th>注意</th></tr></thead><tbody>
              {data.releases.map((r) => <tr key={r.version}><td className="mono">{r.version} <span className="note">{r.channel}</span></td><td className="mono">{r.date ?? "—"}</td><td>{r.supported ? <span className="pass">支援中</span> : <span className="note">否</span>}</td><td><Bullets items={r.highlights} /></td><td className="src">{r.caution}</td></tr>)}
            </tbody></table></section>
        </>)}

      {view === "安全公告" && (
        <section className="card">
          <table><thead><tr><th>公告</th><th>嚴重度</th><th>影響版本</th><th>修正</th><th>要做什麼</th></tr></thead><tbody>
            {data.advisories.map((a) => (
              <tr key={a.id}>
                <td><strong>{a.title}</strong><div className="mono note">{a.id}{a.published && ` · ${a.published}`}</div><p style={{ fontSize: 13, lineHeight: 1.7 }}>{a.summary}<Sources refs={a.sources} /></p></td>
                <td><span className={`tag sev-${a.severity}`}>{a.severity}</span></td>
                <td className="mono">{a.affected}</td><td className="mono">{a.fixedIn ?? "—"}</td><td>{a.action}</td>
              </tr>))}
          </tbody></table>
        </section>)}

      {view === "已知陷阱" && (
        <section className="card">
          {data.knownPitfalls.map((p) => (
            <details key={p.title} open>
              <summary>{p.title} <span className="note mono">{p.affects}</span></summary>
              <div className="grid2" style={{ marginTop: 8 }}>
                <div><div className="note">症狀</div><p style={{ fontSize: 14, lineHeight: 1.7 }}>{p.symptom}</p></div>
                <div><div className="note">解法</div><p style={{ fontSize: 14, lineHeight: 1.7 }}>{p.fix}<Sources refs={p.sources} /></p></div>
              </div>
            </details>))}
        </section>)}

      {view === "設定範例" && (
        <section className="card">
          <h2>config/litellm.example.yaml</h2>
          <p className="note">路由命名：<span className="mono">&lt;model&gt;</span> 預設、<span className="mono">&lt;model&gt;@&lt;route&gt;</span> 釘選 CSP / 區域、<span className="mono">tier/*</span> 能力層 alias。上線前請用 compat suite 在 staging 驗證。</p>
          {cfg.data ? <pre className="code">{cfg.data}</pre> : <p className="note">{cfg.error ? "找不到設定檔（請確認 build 時有 config/ 資料夾）。" : "載入中…"}</p>}
        </section>)}

      {view === "Gateway 強化清單" && (
        <section className="card">
          <p className="note" style={{ marginTop: 0 }}>依研究報告整理的平台建議，不是廠商官方清單；請依公司資安規範調整。</p>
          <div className="grid2">
            <div><h2>供應鏈</h2><Bullets items={["Image 以 digest 釘選，不用 main-latest、*-dev、*-rc。", "PyPI 以 --require-hashes 釘選並經私有 mirror。", "CI 快取與 lockfile 排除 1.82.7 / 1.82.8。", "每月至少升一次 minor，升版走 staging → compat 全路由 → perf smoke → canary。"]} /></div>
            <div><h2>網路與驗證</h2><Bullets items={["Proxy 不對外開放；前面放 WAF 與 rate limit。", "Ingress 固定並驗證 Host header。", "每個團隊 / 應用各自的 virtual key、預算與速率上限。", "監控 spend 資料表大小（未知金鑰也會寫入）。"]} /></div>
            <div><h2>資料與紀錄</h2><Bullets items={["限制 observability 後端（traces、spend logs）的存取權。", "檢查歷史紀錄是否含機密 header，必要時清除並輪替金鑰。", "依資料等級限制可用路由（台灣境內處理目前沒有選項）。"]} /></div>
            <div><h2>MCP 與工具</h2><Bullets items={["以 allow-list 管理可註冊的 MCP server，限制 stdio server。", "代理工具的權限最小化，高風險動作需人工確認。"]} /></div>
          </div>
        </section>)}
    </>
  );
}

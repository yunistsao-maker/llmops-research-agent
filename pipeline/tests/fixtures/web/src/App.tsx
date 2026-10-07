import { useEffect, useMemo, useState } from "react";
import { loaders, SourcesCtx, useData } from "./data";
import Radar from "./pages/Radar";
import Catalog from "./pages/Catalog";
import Compare from "./pages/Compare";
import ModelPage from "./pages/ModelPage";
import Eol from "./pages/Eol";
import Tests from "./pages/Tests";
import Release from "./pages/Release";
import Gateway from "./pages/Gateway";
import Onboarding from "./pages/Onboarding";
import Methods from "./pages/Methods";

export const go = (path: string) => { window.location.hash = "/" + path; };
const parse = (h: string) => h.replace(/^#\/?/, "").split("/").map(decodeURIComponent);

const NAV: [string, string][] = [
  ["", "Release Radar"], ["models", "模型目錄"], ["compare", "比較"], ["eol", "平台清單與 EOL"],
  ["tests", "測試結果"], ["release", "發布流程"], ["gateway", "LiteLLM 與安全"], ["onboarding", "上手指南"], ["methods", "方法與來源"],
];

export default function App() {
  const [parts, setParts] = useState(parse(window.location.hash));
  useEffect(() => {
    const on = () => { setParts(parse(window.location.hash)); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const sources = useData(loaders.sources);
  const srcMap = useMemo(() => new Map((sources.data?.sources ?? []).map((s) => [s.id, s])), [sources.data]);
  const [p, a] = parts;
  const section = p === "models" && a ? "models" : p ?? "";
  let page;
  switch (p) {
    case "models": page = a ? <ModelPage id={a} /> : <Catalog />; break;
    case "compare": page = <Compare ids={a ? a.split(",") : []} />; break;
    case "eol": page = <Eol />; break;
    case "tests": page = <Tests />; break;
    case "release": page = <Release model={a} />; break;
    case "gateway": page = <Gateway />; break;
    case "onboarding": page = <Onboarding />; break;
    case "methods": page = <Methods />; break;
    default: page = <Radar digestId={p === "digest" ? a : undefined} />;
  }
  return (
    <SourcesCtx.Provider value={srcMap}>
      <a className="skip" href="#main-content" onClick={(e) => { e.preventDefault(); document.getElementById("main-content")?.focus(); }}>跳到內容</a>
      <header className="topbar">
        <div className="brand">LLM 平台</div>
        <nav aria-label="主選單">
          {NAV.map(([path, label]) => (
            <button key={label} aria-current={(section === path || (path === "" && p === "digest")) ? "page" : undefined} onClick={() => go(path)}>{label}</button>
          ))}
        </nav>
      </header>
      <main id="main-content" tabIndex={-1}>{page}</main>
      <footer className="foot">資料來自 data/*.json（每週排程更新、經平台團隊審核）。社群與未驗證資訊以紅字標示，使用前請核對來源。</footer>
    </SourcesCtx.Provider>
  );
}

import { useEffect, useState } from "react";
import ErrorBoundary from "./ErrorBoundary";
import { dataSource, newerRelease, releaseRecordUrl, type DataManifest, type DataSource } from "./dataSource";

const CHECK_EVERY_MS = 5 * 60 * 1000;
const FIRST_RETRY_MS = 15 * 1000;

const when = (iso?: string) =>
  iso
    ? new Date(iso).toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
    : "—";

/** How this version of the data got here - said plainly, because automatic updates are not reviewed line by line. */
function origin(m: DataManifest): string {
  const src = m.source;
  switch (src?.kind) {
    case "agent":
      return src.approvedBy ? `research agent 提案，${src.approvedBy} 核准後發布` : "research agent 自動更新：通過程式驗證，未經人工逐項審核";
    case "manual":
      return `手動發布${src.by ? `（${src.by}）` : ""}`;
    case "bootstrap":
      return "初始資料";
    case "rollback":
      return `已回復到較早的版本${src.by ? `（${src.by}）` : ""}`;
    default:
      return src?.kind || "來源未註明";
  }
}

function Status() {
  const [src, setSrc] = useState<DataSource | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [recovered, setRecovered] = useState(false);
  const [other, setOther] = useState<DataManifest | null>(null);

  useEffect(() => {
    let live = true;
    let loaded = false;
    let failedBefore = false;
    let retryIn = FIRST_RETRY_MS;
    let retry: number | undefined;

    const load = () => {
      dataSource().then(
        (s) => {
          if (!live) return;
          loaded = true;
          setSrc(s);
          setError(null);
          // Pages that were opened while the data could not be read stay as they are: offer to load them again.
          if (failedBefore) setRecovered(true);
        },
        (e) => {
          if (!live) return;
          failedBefore = true;
          setError(e instanceof Error ? e.message : String(e));
          retry = window.setTimeout(load, retryIn);
          retryIn = Math.min(retryIn * 2, CHECK_EVERY_MS);
        },
      );
    };
    const check = () => {
      if (!loaded || document.visibilityState !== "visible") return; // a tab nobody is looking at does not poll
      // Whatever the answer is, it replaces the last one: after a rollback to the release this page shows, there is
      // nothing to reload any more.  A failed check is not worth a message.
      newerRelease().then((m) => live && setOther(m), () => undefined);
    };

    load();
    const timer = window.setInterval(check, CHECK_EVERY_MS);
    document.addEventListener("visibilitychange", check);
    return () => {
      live = false;
      window.clearInterval(timer);
      window.clearTimeout(retry);
      document.removeEventListener("visibilitychange", check);
    };
  }, []);

  if (!src) {
    return error
      ? <span className="data-status data-status-error" role="alert">無法讀取資料：{error}。會自動再試。</span>
      : <span className="data-status">資料載入中…</span>;
  }
  const m = src.manifest;
  const notice = recovered
    ? "資料已經可以讀取"
    : other
      ? `${other.source?.kind === "rollback" ? "資料已換成另一個版本" : "有新的資料版本"}（${when(other.publishedAt)}）`
      : null;
  const record = releaseRecordUrl(src);
  return (
    <span className="data-status" data-release={src.remote ? m.releaseId : undefined}>
      {src.remote ? (
        <>
          資料版本 <code>{m.releaseId}</code>，{when(m.publishedAt)} 發布（{origin(m)}
          {record && <>；<a href={record} target="_blank" rel="noreferrer">變更紀錄</a></>}）。
        </>
      ) : (
        <>資料隨網站一起打包（{when(m.generatedAt)} build）。</>
      )}
      {/* always in the page, so that a screen reader announces the text when it appears */}
      <span className="data-status-notice" role="status">
        {notice && (
          <>
            {notice}
            <button className="data-status-reload" onClick={() => window.location.reload()}>重新載入</button>
          </>
        )}
      </span>
    </span>
  );
}

/** Footer line: which version of the data this page shows, and a prompt to reload when another one is live. */
export default function DataStatus() {
  return (
    <ErrorBoundary fallback={<span className="data-status data-status-error">資料版本資訊無法顯示。</span>}>
      <Status />
    </ErrorBoundary>
  );
}

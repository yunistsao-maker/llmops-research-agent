#!/usr/bin/env python3
"""把 portal 的 React UI（llmops-portal/web）改成「執行時從資料位置讀資料」，讓 UI 部署與資料更新分開。

    python scripts/patch_ui.py ../llmops-portal/web            # 套用（可重複執行；已套用的部分會略過）
    python scripts/patch_ui.py ../llmops-portal/web --check    # 只檢查，不寫入（全部已套用回 0，還有待套用的回 1）
    python scripts/patch_ui.py ../llmops-portal/web --revert   # 把套用過的修改一項一項撤回

它做的事（都集中在資料載入這一層，頁面程式不動）：

  新增  src/dataSource.ts        資料來源：bundled（隨網站打包）或 remote（資料位置的 manifest → release）
  新增  src/DataStatus.tsx       頁尾顯示目前的資料版本、來源，有新版本時提示重新載入
  新增  src/ErrorBoundary.tsx    某一頁畫不出來時只有那一頁顯示錯誤，導覽列照常（原本是整個網站變空白）
  新增  public/config.json       執行時設定：{"dataBaseUrl": "https://…/"}（已存在就不動）
  修改  src/data.ts              getJson 與 manifest 改走 dataSource
  修改  src/pages/Gateway.tsx    設定範例（config/litellm.example.yaml）改走 dataSource
  修改  scripts/sync-data.mjs    設定了資料位置時不再把 data/ runs/ config/ 打包進網站
  修改  src/App.tsx、src/main.tsx、src/pages/Radar.tsx、src/styles.css、README.md
                                 頁尾改成資料版本列、頁面包一層 ErrorBoundary、首頁等資料索引回來再判斷有沒有週報
                                 （這幾項是選用的：找不到對應位置就略過，並告訴妳怎麼手動加）

每一項修改都只換掉它認得的那一段程式；認不得（妳改過那一段）就不動它，並印出要手動改的內容。
任何「必要」的修改無法套用時，整個動作會停下來、什麼都不寫。

--revert 是把每一項修改「反過來做一次」，不是拿舊檔案蓋回去，所以套用之後妳在同一個檔案別處做的修改都會留著。
被換掉的原文記在 <web>/.patch_ui/state.json：請把 .patch_ui/ 一起 commit（很小），換一台機器才還原得回來。
<web>/.patch_ui/orig/ 另外留了一份套用前的檔案，只是給人對照用的。

Exit code：0 成功（或 --check 全部已套用）· 1 --check 發現還沒套用 · 2 參數錯誤 · 3 環境或輸入問題
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FILES = PROJECT_ROOT / "ui" / "files"
NEW_FILES = ["src/dataSource.ts", "src/DataStatus.tsx", "src/ErrorBoundary.tsx"]
CONFIG = "public/config.json"
BACKUP = ".patch_ui"  # <web>/.patch_ui/state.json = what each edit replaced (needed to undo it); orig/ = the files before, for reference
OK, PENDING, ENV = 0, 1, 3
#: a line that calls fetch() on purpose (something that is not portal data) says so with this comment
ALLOW = "patch_ui: ok"

SYNC_BLOCK = '''// --- remote data (added by llmops-research-agent/scripts/patch_ui.py) -------------------------------------------
// When the site reads its data from a data location nothing is copied into the site, so a data update needs no
// rebuild. The data location is "dataBaseUrl" in public/config.json, or else the environment variable
// VITE_DATA_BASE_URL (the same rule as src/dataSource.ts). BUNDLE_DATA=1 copies the data anyway.
import { existsSync as _exists, readFileSync as _read, readdirSync as _list, rmSync as _rm } from "node:fs";
import { fileURLToPath as _path } from "node:url";
{
  const webDir = _path(new URL("../", import.meta.url)); // a real path: folder names with spaces or non-ASCII letters, Windows
  const publicDir = webDir + "public/";
  let configured = process.env.VITE_DATA_BASE_URL || "";
  let inConfig = false;
  try {
    const cfg = JSON.parse(_read(publicDir + "config.json", "utf8"));
    if (cfg && typeof cfg.dataBaseUrl === "string") { configured = cfg.dataBaseUrl; inConfig = true; }
  } catch { /* no config.json: the build-time setting applies */ }
  if (!inConfig && !configured.trim()) {
    // Vite also takes VITE_* from .env files, which this script does not read: say so instead of quietly disagreeing with the page.
    const envFiles = _list(webDir).filter((f) => /^\\.env(\\..+)?$/.test(f) && /^\\s*VITE_DATA_BASE_URL\\s*=\\s*\\S/m.test(_read(webDir + f, "utf8")));
    if (envFiles.length) console.warn("WARNING: VITE_DATA_BASE_URL is set in " + envFiles.join(", ") + ": the site will read the data location, but the data is still copied into this build. Set it as a real environment variable (Amplify's environment variables are), or put \\"dataBaseUrl\\" in public/config.json.");
  }
  if (configured.trim() && process.env.BUNDLE_DATA !== "1") {
    for (const d of ["data", "runs", "config"]) if (_exists(publicDir + d)) _rm(publicDir + d, { recursive: true });
    console.log("remote data: nothing is bundled into the site (the data location is set)");
    process.exit(0);
  }
}
// --- end remote data ---------------------------------------------------------------------------------------------
'''

CSS_BLOCK = '''
/* data version line in the footer (added by llmops-research-agent/scripts/patch_ui.py) */
.data-status code{font-family:'IBM Plex Mono',monospace;font-size:.95em}
.data-status-error{color:var(--danger)}
.data-status-notice{position:fixed;right:24px;bottom:24px;z-index:20;display:flex;align-items:center;gap:12px;padding:12px 16px;border:1px solid var(--brand);border-radius:4px;background:var(--card);color:var(--ink);font-size:14px;box-shadow:0 4px 16px rgba(0,0,0,.18)}
.data-status-notice:empty{display:none}
.data-status-reload{padding:8px 14px;min-height:36px;border:1px solid var(--brand);border-radius:2px;background:var(--brand);color:#fff;font:inherit;cursor:pointer}
.page-error{border-left:4px solid var(--danger)}
'''

README_BLOCK = '''
## 資料來源：bundled 或 remote

- **bundled（預設）**：`npm run build` 把 `../data`、`../runs`、`../config` 複製進網站。資料更新 = 重新 build、重新部署。
- **remote**：在 `public/config.json` 寫 `{"dataBaseUrl": "https://<資料位置>/"}`，或 build 時設環境變數 `VITE_DATA_BASE_URL`
  （`config.json` 有寫就以它為準；要用環境變數時必須是真正的環境變數，不是 `.env` 檔）。網站啟動時讀 `<資料位置>manifest.json`，
  再從它指到的 release 讀所有資料；頁尾會顯示資料版本，有新版本時右下角會提示重新載入。這時 build 不會打包資料，
  資料更新（research pipeline 發布新的 release）不需要重新 build 或部署這個網站。

資料載入都集中在 `src/dataSource.ts` 與 `src/data.ts`。新增頁面時請用 `loaders` / `fetchJson` / `fetchText`，不要自己呼叫 `fetch()`。
`.patch_ui/` 是這些修改的紀錄（`patch_ui.py --revert` 會用到），請一起 commit。
'''

# the code of the original app that the edits recognise (and put back on --revert)
STOCK_GETJSON = '''async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json() as Promise<T>;
}
'''
NEW_GETJSON = "// Data files are resolved against the current release of the data location (see dataSource.ts).\nconst getJson = fetchJson;\n"
STOCK_MANIFEST = 'manifest: () => cached<Manifest>("data/manifest.json"),'
NEW_MANIFEST = "manifest: () => loadManifest() as Promise<Manifest>,"
STOCK_CONFIG_FETCH = 'fetch("config/litellm.example.yaml").then((r) => (r.ok ? r.text() : Promise.reject(r.status)))'
NEW_CONFIG_FETCH = 'fetchText("config/litellm.example.yaml")'
STOCK_FOOTER = '<footer className="foot">資料來自 data/*.json（每週排程更新、經平台團隊審核）。社群與未驗證資訊以紅字標示，使用前請核對來源。</footer>'
NEW_FOOTER = '<footer className="foot"><DataStatus /> 社群與未驗證資訊以紅字標示，使用前請核對來源。</footer>'
STOCK_DIGEST = 'const digest = useData(() => (id ? loaders.digest(id) : Promise.reject("no digest")), [id]);'
# while the index has not arrived there is no "latest digest" yet - that is "still loading", not "no digest"
NEW_DIGEST = ('const digest = useData(() => (id ? loaders.digest(id) : manifest.data ? Promise.reject("no digest") : new Promise<never>(() => undefined)), '
              "[id, manifest.data]);")
STOCK_CONFIG_HINT = "找不到設定檔（請確認 build 時有 config/ 資料夾）。"
NEW_CONFIG_HINT = "讀不到設定範例（config/litellm.example.yaml）。"

Pairs = list[tuple[str, str]]  # (what the edit put in, what was there): replacing the first by the second undoes it


class ScriptError(Exception):
    """A problem with the environment or the input (exit code 3)."""


@dataclass
class Edit:
    file: str
    name: str
    applied: Callable[[str], bool]  # is this edit (or something a person wrote that does the same) already in the file?
    apply: Callable[[str, str], tuple[str, Pairs] | None]  # (text, newline) -> (new text, how to undo), None = place not found
    manual: str  # what to do by hand in that case
    required: bool = True


# ------------------------------------------------------------------------------------------- the kinds of edit
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_IMPORT_LINE = re.compile(r"^import\b", re.M)


def _nl(block: str, nl: str) -> str:
    return block.replace("\n", nl)


def _once(text: str, piece: str) -> bool:
    return text.count(piece) == 1


def _insert_point(text: str) -> int:
    """In front of the file's first real import (so a header comment stays on top); else after a #! line; else the top."""
    comments = [m.span() for m in _BLOCK_COMMENT.finditer(text)]
    for m in _IMPORT_LINE.finditer(text):
        if not any(a <= m.start() < b for a, b in comments):
            return m.start()
    if text.startswith("#!"):
        end = text.find("\n")
        return len(text) if end < 0 else end + 1
    return 0


def _import(line: str) -> Callable[[str, str], tuple[str, Pairs] | None]:
    """Add an import line (or a block of code that belongs with the imports).

    When the file already imports from the same module, nothing is added: a second import of the same names does not
    compile, and which names the existing one should get is for a person to decide.
    """
    module = re.search(r"from\s+([\"'][^\"']+[\"'])\s*;?\s*$", line.strip()) if line.count("\n") == 1 else None

    def run(text: str, nl: str) -> tuple[str, Pairs] | None:
        block = _nl(line, nl)
        if block in text or (module and re.search(r"from\s+" + re.escape(module.group(1)), text)):
            return None
        at = _insert_point(text)
        return text[:at] + block + text[at:], [(block, "")]

    return run


def _imports(module: str, *names: str) -> Callable[[str], bool]:
    """Does the file import all of ``names`` from ``module`` (in whatever form a person may have written it)?"""
    statement = re.compile(r"import\s+(?P<what>[^;]*?)\s+from\s+[\"']" + re.escape(module) + r"[\"']", re.S)

    def run(text: str) -> bool:
        imported = " ".join(m.group("what") for m in statement.finditer(text))
        return bool(imported) and all(re.search(rf"\b{re.escape(n)}\b", imported) for n in names)

    return run


def _swap(old: str, new: str) -> Callable[[str, str], tuple[str, Pairs] | None]:
    """Replace the one place where ``old`` stands (written exactly like in the original app) by ``new``."""

    def run(text: str, nl: str) -> tuple[str, Pairs] | None:
        old_nl, new_nl = _nl(old, nl), _nl(new, nl)
        if not _once(text, old_nl) or new_nl in text:
            return None
        return text.replace(old_nl, new_nl), [(new_nl, old_nl)]

    return run


def _append(block: str) -> Callable[[str, str], tuple[str, Pairs] | None]:
    def run(text: str, nl: str) -> tuple[str, Pairs] | None:
        added = ("" if text.endswith(nl) or not text else nl) + _nl(block, nl)
        return (text + added, [(added, "")]) if added not in text else None

    return run


def _both(*steps: Callable[[str, str], tuple[str, Pairs] | None]) -> Callable[[str, str], tuple[str, Pairs] | None]:
    def run(text: str, nl: str) -> tuple[str, Pairs] | None:
        pairs: Pairs = []
        for step in steps:
            done = step(text, nl)
            if done is None:
                return None
            text, more = done
            pairs += more
        return text, pairs

    return run


_MAIN = re.compile(r"<main\b([^>]*)>\{page\}</main>")


def _boundary(text: str, nl: str) -> tuple[str, Pairs] | None:
    """Wrap the routed page.  resetKey (the route) clears a shown error on navigation without remounting the page:
    a `key` would rebuild the page - and drop the keyboard focus - on every change of the address."""
    found = _MAIN.findall(text)
    if len(found) != 1 or not re.search(r"\[parts,\s*setParts\]", text):
        return None
    m = _MAIN.search(text)
    assert m is not None
    wrapped = f"<main{m.group(1)}><ErrorBoundary resetKey={{parts.join(\"/\")}}>{{page}}</ErrorBoundary></main>"
    return _both(_swap(m.group(0), wrapped), _import('import ErrorBoundary from "./ErrorBoundary";\n'))(text, nl)


def _has(piece: str) -> Callable[[str], bool]:
    return lambda text: piece in text


EDITS = [
    Edit("src/data.ts", "匯入 dataSource", _imports("./dataSource", "fetchJson", "loadManifest"),
         _import('import { fetchJson, loadManifest } from "./dataSource";\n'),
         '在檔案開頭加上（或併進既有的 import）：import { fetchJson, loadManifest } from "./dataSource";'),
    Edit("src/data.ts", "getJson 改走資料位置", _has("const getJson = fetchJson;"), _swap(STOCK_GETJSON, NEW_GETJSON),
         "把 getJson 函式整個換成：const getJson = fetchJson;（所有資料檔都要經過它）"),
    Edit("src/data.ts", "manifest 改讀資料位置的指標", _has("loadManifest() as Promise<Manifest>"), _swap(STOCK_MANIFEST, NEW_MANIFEST),
         "loaders 裡的 manifest 改成：manifest: () => loadManifest() as Promise<Manifest>,"),
    Edit("src/pages/Gateway.tsx", "匯入 fetchText", _imports("../dataSource", "fetchText"), _import('import { fetchText } from "../dataSource";\n'),
         '在檔案開頭加上（或併進既有的 import）：import { fetchText } from "../dataSource";'),
    Edit("src/pages/Gateway.tsx", "設定範例改走資料位置", _has(NEW_CONFIG_FETCH), _swap(STOCK_CONFIG_FETCH, NEW_CONFIG_FETCH),
         '把 fetch("config/litellm.example.yaml")…（含後面的 .then）換成：fetchText("config/litellm.example.yaml")'),
    Edit("scripts/sync-data.mjs", "設定了資料位置時不打包資料", _has("--- remote data (added by"), _import(SYNC_BLOCK),
         "在第一個 import 之前加入 patch_ui.py 裡的 SYNC_BLOCK"),
    Edit("src/App.tsx", "頁尾改成資料版本列", _has("<DataStatus"), _both(_swap(STOCK_FOOTER, NEW_FOOTER), _import('import DataStatus from "./DataStatus";\n')),
         '在 src/App.tsx 加上 import DataStatus from "./DataStatus"; 並把 <DataStatus /> 放進頁尾（或任何每頁都看得到的位置）',
         required=False),
    Edit("src/App.tsx", "頁面包一層 ErrorBoundary", _has("<ErrorBoundary"), _boundary,
         '在 src/App.tsx 加上 import ErrorBoundary from "./ErrorBoundary"; 並把頁面內容包起來：'
         "<ErrorBoundary resetKey={目前的路由字串}>{page}</ErrorBoundary>",
         required=False),
    Edit("src/main.tsx", "整個 App 外面再包一層 ErrorBoundary", _has("<ErrorBoundary"),
         _both(_swap("<App />", "<ErrorBoundary><App /></ErrorBoundary>"), _import('import ErrorBoundary from "./ErrorBoundary";\n')),
         '在 src/main.tsx 加上 import ErrorBoundary from "./ErrorBoundary"; 並把 <App /> 包成 <ErrorBoundary><App /></ErrorBoundary>'
         "（導覽列本身畫不出來時，顯示錯誤訊息而不是空白頁）",
         required=False),
    Edit("src/pages/Radar.tsx", "首頁等資料索引回來再判斷有沒有週報", _has("new Promise<never>"), _swap(STOCK_DIGEST, NEW_DIGEST),
         "（選用）首頁在資料索引還沒回來時會短暫顯示「載入失敗：no digest」；把 useData 的 loader 改成索引回來之前不要 reject",
         required=False),
    Edit("src/pages/Gateway.tsx", "設定範例讀不到時的提示", _has(NEW_CONFIG_HINT), _swap(STOCK_CONFIG_HINT, NEW_CONFIG_HINT),
         "（選用）把「請確認 build 時有 config/ 資料夾」的提示改成不提 build 的說法", required=False),
    Edit("src/styles.css", "資料版本列的樣式", _has(".data-status"), _append(CSS_BLOCK), "把 patch_ui.py 裡的 CSS_BLOCK 加到 styles.css 最後", required=False),
    Edit("README.md", "README 補上資料來源說明", _has("## 資料來源：bundled 或 remote"), _append(README_BLOCK), "（選用）", required=False),
]
EDITED = sorted({e.file for e in EDITS})

# After the patch the app's own code has no reason to read anything by itself: every data file goes through
# dataSource.ts.  What is left would read from the site instead of the data location - a fetch() whatever its argument
# looks like and however it is spelled, another way to make a request, a data file compiled into the bundle, a data
# path handed to something that is not a loader.  This reads the source as text: it finds the spellings below and no
# others (scripts/ui_smoke.py is the check that watches what the built pages really request).
_GLOBAL = r"(?:window|globalThis|self|top|parent|frames)"
_READS = (
    re.compile(r"(?<![\w.$])fetch\s*\("),                                                 # fetch(...) - the parenthesis may be on the next line
    re.compile(_GLOBAL + r"\s*\??\.\s*fetch(?![\w$])"),                                   # window.fetch
    re.compile(_GLOBAL + r"\s*(?:\?\.)?\[\s*[\"'`]fetch[\"'`]\s*\]"),                      # window["fetch"]
    re.compile(r"\{[^{}]*(?<![\w$])fetch(?![\w$])[^{}]*\}\s*=\s*" + _GLOBAL + r"(?![\w$])"),  # const { fetch: get } = window
    re.compile(r"\bimport\b[^;\"'`]*(?<![\w$])fetch(?![\w$])[^;\"'`]*\bfrom\b"),             # import fetch from "cross-fetch"
    re.compile(r"(?<![\w$])(?:XMLHttpRequest|EventSource)(?![\w$])"),
    re.compile(r"""\b(?:href|src)\s*=\s*\{?\s*[`"'](?:\./|/)?(?:data|runs|config)/"""),
)
# the function under another name: `const get = fetch;`, `load(fetch)`, `{ fetch }`
_AS_A_VALUE = re.compile(r"(?P<lead>[=(,:\[?|&{]|\breturn\b|=>)\s*fetch(?![\w$])(?P<tail>\s*[:(]?)")
# a text that names a data file: "data/catalog.json", `runs/perf/${name}.json`, "../../data/catalog.json"
_DATA_FILE = re.compile(r"""(["'`])[^"'`\s]*?(?<![\w.-])(?:data|runs|config)/[^"'`\s]*\.(?:json|ya?ml|md|csv|txt)\1""")
_CALLED = re.compile(r"([A-Za-z_$][\w$]*)\s*(?:<[^()]{0,200}>)?\s*$")
#: the functions that read a data file from the data location (dataSource.ts, and data.ts on top of it)
LOADERS = frozenset({"cached", "getJson", "fetchJson", "fetchText", "dataUrl"})
_SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs"}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def same_text(a: bytes, b: bytes) -> bool:
    """Equal apart from the line ends (a checkout on Windows may hold the very same file with \\r\\n)."""
    return a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")


def read(path: Path) -> tuple[str, str]:
    """(the file's text exactly as it is, the line end most of its lines use) - nothing about it is normalised."""
    try:
        raw = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as e:
        raise ScriptError(f"{path} 不是 UTF-8 文字檔：{e}") from e
    crlf = raw.count("\r\n")
    return raw, "\r\n" if crlf and crlf * 2 >= raw.count("\n") else "\n"


def write(path: Path, data: bytes) -> None:
    """All of the file or none of it (write next to it, then rename).  A symlink keeps pointing where it points."""
    if path.is_symlink():
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".patch_ui-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def check_web(web: Path) -> None:
    missing = [rel for rel in ("package.json", "src/data.ts", "scripts/sync-data.mjs") if not (web / rel).is_file()]
    if missing:
        raise ScriptError(f"{web} 不像 portal 的 web/ 資料夾：找不到 {', '.join(missing)}")
    for rel in NEW_FILES + [CONFIG]:
        if not (FILES / rel).is_file():
            raise ScriptError(f"找不到要複製的檔案 {FILES / rel}（請在 llmops-research-agent 專案裡執行這支 script）")


@dataclass
class Plan:
    texts: dict[str, str]  # file -> its text after the pending edits
    originals: dict[str, str]  # file -> its text now
    newlines: dict[str, str]
    states: list[tuple[Edit, str]]  # applied | pending | missing | no-file
    pairs: dict[tuple[str, str], Pairs]  # (file, edit name) -> how to undo the edits that are pending


def plan(web: Path) -> Plan:
    p = Plan({}, {}, {}, [], {})
    for edit in EDITS:
        path = web / edit.file
        if edit.file not in p.texts:
            if not path.is_file():
                p.states.append((edit, "no-file"))
                continue
            p.originals[edit.file], p.newlines[edit.file] = read(path)
            p.texts[edit.file] = p.originals[edit.file]
        text = p.texts[edit.file]
        if edit.applied(text):
            p.states.append((edit, "applied"))
            continue
        done = edit.apply(text, p.newlines[edit.file])
        if done is None or not all(_once(done[0], inserted) for inserted, _ in done[1]):
            p.states.append((edit, "missing"))  # the place is not there, or the edit could not be told apart afterwards
            continue
        p.texts[edit.file], p.pairs[(edit.file, edit.name)] = done
        p.states.append((edit, "pending"))
    return p


def kit_files(web: Path, state: dict) -> tuple[list[str], list[str]]:
    """(files to install or update, files left alone because they were changed in the web folder)."""
    install, kept = [], []
    for rel in NEW_FILES:
        path, ours = web / rel, (FILES / rel).read_bytes()
        if not path.is_file():
            install.append(rel)
            continue
        current = path.read_bytes()
        if same_text(current, ours):
            continue
        installed = _entry(state, rel).get("installed")
        (install if isinstance(installed, str) and sha(current.replace(b"\r\n", b"\n")) == installed else kept).append(rel)
    return install, kept


def _called_with(text: str, at: int) -> str:
    """The name of the function in whose argument list position ``at`` stands ("" when it stands in none)."""
    parens = braces = 0  # groups that were closed before ``at``: `(a, b)`, `${name}`, `{ ... }`
    for i in range(at - 1, max(at - 4000, 0) - 1, -1):
        ch = text[i]
        if ch == ")":
            parens += 1
        elif ch == "}":
            braces += 1
        elif ch == "{":
            if not braces:
                return ""  # an object or a block that was opened inside whatever call there is
            braces -= 1
        elif ch == "(":
            if parens:
                parens -= 1
            elif not braces:
                name = _CALLED.search(text, max(0, i - 300), i)
                return name.group(1) if name else ""
        elif ch == ";" and not parens and not braces:
            return ""
    return ""


_ATTRIBUTE_NAME = re.compile(r"(?<![\w$.])[A-Za-z_][\w:-]*\s*=\s*\{?\s*$")
_BRACED = re.compile(r"\{[^{}]*\}")
_QUOTED = re.compile(r"\"[^\"\n]*\"|'[^'\n]*'")
_OPEN_TAG = re.compile(r"<[A-Za-z][\w.]*(?:\s+[\w:.-]+\s*=?)*\s*$")


def _shown_by_a_tag(text: str, at: int) -> bool:
    """Is the text that starts at ``at`` the value of a JSX attribute (`<input placeholder="例：runs/compat/x.json" />`)?

    Such a text is shown, not read (href and src have a pattern of their own).  Looked at from the attribute backwards:
    with the values of the attributes in front of it taken out, what is left ends in an opened tag.
    """
    name = _ATTRIBUTE_NAME.search(text, max(0, at - 120), at)
    if not name:
        return False
    before = text[max(0, name.start() - 3000):name.start()]
    for _ in range(8):  # values such as onChange={(e) => put(id, { evidence: e.target.value })}
        before, n = _BRACED.subn("", before)
        if not n:
            break
    return bool(_OPEN_TAG.search(_QUOTED.sub("", before)))


def _read_positions(text: str) -> list[int]:
    """Where ``text`` (a source file) reads something by itself: see the patterns."""
    found = [m.start() for pattern in _READS for m in pattern.finditer(text)]
    for m in _AS_A_VALUE.finditer(text):
        tail = m.group("tail").strip()
        if tail == "(" or (tail == ":" and m.group("lead") in "{,"):
            continue  # a call (first pattern; `api.fetch(` is not one), or a property that happens to be called fetch
        found.append(m.start())
    found += [m.start() for m in _DATA_FILE.finditer(text) if _called_with(text, m.start()) not in LOADERS and not _shown_by_a_tag(text, m.start())]
    return found


def direct_reads(web: Path, texts: dict[str, str]) -> list[str]:
    found = []
    for path in sorted((web / "src").rglob("*")):
        rel = path.relative_to(web).as_posix()
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES or rel in NEW_FILES:
            continue
        text = texts[rel] if rel in texts else path.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        starts = [0]
        for line in lines:
            starts.append(starts[-1] + len(line) + 1)
        for i in sorted({bisect.bisect_right(starts, at) - 1 for at in _read_positions(text)}):
            if ALLOW not in lines[i]:
                found.append(f"{rel}:{i + 1}: {lines[i].strip()[:120]}")
    return found


# ------------------------------------------------------------------------------------------- what was done, to undo it
def load_state(web: Path) -> dict:
    try:
        state = json.loads((web / BACKUP / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": {}}
    if not isinstance(state, dict) or not isinstance(state.get("files"), dict):
        return {"files": {}}
    return {"files": {k: v for k, v in state["files"].items() if isinstance(k, str) and isinstance(v, dict)}}


def _entry(state: dict, rel: str) -> dict:
    entry = state["files"].get(rel)
    return entry if isinstance(entry, dict) else {}


def _recorded(state: dict, rel: str, name: str) -> Pairs | None:
    """The pairs recorded for one edit - None when there is no usable record."""
    raw = _entry(state, rel).get("edits")
    pairs = raw.get(name) if isinstance(raw, dict) else None
    if not isinstance(pairs, list) or not all(isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p) for p in pairs):
        return None
    return [(p[0], p[1]) for p in pairs]


def save_state(web: Path, state: dict) -> None:
    write(web / BACKUP / "state.json", (json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))


def revert(web: Path) -> int:
    """Undo each edit that is in the files, in reverse order, by putting back what it replaced.

    Nothing is written unless every one of them can be undone: a half-reverted folder would not build.
    """
    state = load_state(web)
    texts: dict[str, str] = {}
    originals: dict[str, str] = {}
    blocked: list[str] = []
    undone = 0
    for edit in reversed(EDITS):
        path = web / edit.file
        if not path.is_file():
            continue
        if edit.file not in texts:
            originals[edit.file], _ = read(path)
            texts[edit.file] = originals[edit.file]
        text = texts[edit.file]
        pairs = _recorded(state, edit.file, edit.name)
        if pairs is None:
            if not edit.applied(text):
                continue
            # no record (the .patch_ui folder was not kept): redo the edit on an empty file to learn what it inserts -
            # which is enough for the edits that only add text
            for nl in ("\n", "\r\n"):
                probe = edit.apply("", nl)
                if probe is not None and all(original == "" for _, original in probe[1]) and all(_once(text, ins) for ins, _ in probe[1]):
                    pairs = probe[1]
                    break
            if pairs is None:
                blocked.append(f"{edit.file}（{edit.name}）：{BACKUP}/state.json 沒有這一項的紀錄，無法確定原本的內容；"
                               f"請用版本控制還原這個檔案（git checkout -- {edit.file}）")
                continue
        elif not all(_once(text, inserted) for inserted, _ in pairs):
            gone = not edit.applied(text) and not any(inserted in text for inserted, _ in pairs)
            if gone and all(original == "" or original in text for _, original in pairs):
                continue  # somebody already put this place back by hand (or with git)
            blocked.append(f"{edit.file}（{edit.name}）：套用之後這一段被改過，無法自動撤回；請手動改回來。原本的內容："
                           + " ⏎ ".join(original.strip()[:200] or "（沒有，是新增的）" for _, original in pairs))
            continue
        for inserted, original in reversed(pairs):
            text = text.replace(inserted, original)
        texts[edit.file] = text
        undone += 1
        say(f"  [撤回] {edit.file}：{edit.name}")
    if blocked:
        say()
        say("✘ 有修改無法自動撤回，所以沒有寫入任何檔案（只撤回一部分的話，網站會 build 不起來）：")
        for line in blocked:
            say(f"  - {line}")
        return ENV

    changed = [rel for rel in texts if texts[rel] != originals[rel]]
    kept = []
    try:
        for rel in changed:
            write(web / rel, texts[rel].encode("utf-8"))
        for rel in NEW_FILES:
            path = web / rel
            if not path.is_file():
                continue
            current = path.read_bytes()
            installed = _entry(state, rel).get("installed")
            if same_text(current, (FILES / rel).read_bytes()) or sha(current.replace(b"\r\n", b"\n")) == installed:
                path.unlink()
                say(f"  [刪除] {rel}")
            else:
                kept.append(rel)
                say(f"  [保留] {rel}：妳修改過這個檔案（已經沒有程式用到它，可以自行刪除）")
        if (web / BACKUP).is_dir():
            shutil.rmtree(web / BACKUP)
    except OSError as e:
        raise ScriptError(f"寫入時發生錯誤（{e}）；已經寫好的檔案是完整的，再執行一次 --revert 即可接著做") from e
    say()
    say(f"已撤回 {undone} 項修改（{len(changed)} 個檔案）。{CONFIG} 保留不動（裡面是妳的設定；原本的程式不會讀它）。")
    return OK


def run(args: argparse.Namespace) -> int:
    web = Path(args.web).resolve()
    check_web(web)
    if args.revert:
        return revert(web)
    p = plan(web)
    blocked = [(e, s) for e, s in p.states if e.required and s in ("missing", "no-file")]
    todo = "待套用" if args.check else "套用"
    labels = {"applied": "已套用", "pending": todo, "missing": "找不到對應位置", "no-file": "沒有這個檔案"}
    for edit, state_ in p.states:
        say(f"  [{labels[state_]}] {edit.file}：{edit.name}")
    state = load_state(web)
    new_files, customised = kit_files(web, state)
    need_config = not (web / CONFIG).is_file()
    for rel in new_files:
        say(f"  [{todo}] {rel}：{'更新' if (web / rel).is_file() else '新增'}")
    for rel in customised:
        say(f"  [保留] {rel}：妳修改過這個檔案，沒有覆蓋（這個專案提供的版本在 ui/files/{rel}）")
    if need_config:
        say(f"  [{todo}] {CONFIG}：新增（內容是 {{}}，表示使用打包的資料）")

    if blocked:
        say()
        say("✘ 有必要的修改無法自動套用，所以沒有寫入任何檔案。請手動修改後再執行一次：")
        for edit, _ in blocked:
            say(f"  - {edit.file}（{edit.name}）：{edit.manual}")
        return ENV
    leftovers = direct_reads(web, p.texts)
    if leftovers:
        say()
        say("✘ 下面這些地方自己讀資料——呼叫 fetch()（不管怎麼寫）、用 XMLHttpRequest、把資料檔 import 進程式，或把資料路徑交給 loaders 以外的東西——")
        say("  會繞過資料位置；請改用 data.ts 的 loaders 或 dataSource.ts 的 fetchJson / fetchText。")
        say(f"  和 portal 資料無關的，在那一行加上註解 // {ALLOW} 就會略過。沒有寫入任何檔案：")
        for line in leftovers:
            say(f"  - {line}")
        return ENV

    changed = [rel for rel in p.texts if p.texts[rel] != p.originals[rel]]
    if args.check:
        pending = bool(changed or new_files or need_config)
        say()
        say("還有待套用的修改（執行時不加 --check 就會套用）。" if pending else "全部已套用。")
        return PENDING if pending else OK

    targets = [web / rel for rel in changed + new_files] + ([web / CONFIG] if need_config else [])
    locked = [str(t.relative_to(web)) for t in targets if t.exists() and not os.access(t, os.W_OK)]
    if locked:
        raise ScriptError(f"這些檔案不能寫入，所以沒有寫入任何檔案：{', '.join(locked)}")
    try:
        for rel in changed:
            untouched = not any(e.applied(p.originals[rel]) for e in EDITS if e.file == rel)
            if untouched:  # the file as it is before any of these edits: kept for people to compare with
                write(web / BACKUP / "orig" / rel, p.originals[rel].encode("utf-8"))
            write(web / rel, p.texts[rel].encode("utf-8"))
            entry = state["files"].setdefault(rel, {})
            edits = entry.get("edits") if isinstance(entry.get("edits"), dict) else {}
            edits.update({name: [list(pair) for pair in pairs] for (file, name), pairs in p.pairs.items() if file == rel})
            entry["edits"] = edits
            save_state(web, state)  # after every file: an interruption leaves a record of exactly what was done
        for rel in new_files:
            data = (FILES / rel).read_bytes()
            write(web / rel, data)
            state["files"][rel] = {"installed": sha(data.replace(b"\r\n", b"\n"))}
        if changed or new_files:
            save_state(web, state)
        if need_config:
            write(web / CONFIG, (FILES / CONFIG).read_bytes())
    except OSError as e:
        raise ScriptError(f"寫入時發生錯誤（{e}）。已經寫好的檔案是完整的，也都有紀錄：排除問題後再執行一次即可接著套用，或用 --revert 撤回") from e

    skipped = [e for e, s in p.states if not e.required and s in ("missing", "no-file")]
    say()
    say(f"✔ 完成：修改 {len(changed)} 個檔案、新增 / 更新 {len(new_files) + int(need_config)} 個檔案。")
    if (web / BACKUP).is_dir():
        say(f"  {BACKUP}/ 記錄了每一項修改換掉的原文（--revert 靠它撤回）：請一起 commit。")
    for edit in skipped:
        say(f"  略過（選用）{edit.file}：{edit.name} —— {edit.manual}")
    say("下一步：")
    say(f"  1. cd {web} && npm run build      # 確認 type check 與 build 通過（這時還是用打包的資料）")
    say(f"  2. 設定資料位置：{CONFIG} 寫 {{\"dataBaseUrl\": \"<data plane stack 的 DataBaseUrl>\"}}，")
    say("     或在 Amplify 的環境變數設 VITE_DATA_BASE_URL；重新 build、部署。之後資料更新就不需要再部署這個網站。")
    return OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="patch_ui.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("web", help="portal repo 的 web/ 資料夾")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只檢查，不寫入")
    mode.add_argument("--revert", action="store_true", help="把套用過的修改一項一項撤回")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # captured streams in tests, exotic consoles
            pass
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ScriptError as e:
        print(f"✘ {e}", file=sys.stderr, flush=True)
        return ENV
    except OSError as e:
        print(f"✘ 讀寫檔案時發生錯誤：{e}", file=sys.stderr, flush=True)
        return ENV


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""在真的瀏覽器裡檢查「UI 讀資料位置」這條路：部署 UI 之前（或改過 UI / schema 之後）跑一次。

    (cd ../llmops-portal/web && npm run build)                    # 先把（套用過 patch_ui.py 的）UI build 出來
    python scripts/ui_smoke.py --dist ../llmops-portal/web/dist --portal ../llmops-portal

全部在本機完成，不碰 AWS，也不改你的任何檔案；瀏覽器只連得到這台機器（其他網站的資源，例如字型，一律不連也不算進結果）：
  1. 用 portal checkout 的資料在暫存資料夾建立一個資料位置（等同 portalctl bootstrap；資料有問題會在這裡被擋下）
  2. 啟動本機的資料位置（serve_data，帶 token、只允許這個 UI 的 origin）與一個靜態網站（你的 dist/，
     config.json 換成指向本機資料位置 —— 同一份 build 產物，只換 config.json）
  3. 用 headless Chromium 逐頁、逐分頁打開 UI：每一頁都要畫得出來、沒有錯誤、資料只從資料位置讀。
     瀏覽途中會發布一個新的 release：已經打開的頁面必須維持它原本的版本；同一個分頁重新打開之後，
     先前讀過的每一個檔案都要改從新的 release 再讀一次（只認檔名、不認版本的快取會在這裡被抓到）
  4. 再發布一個 release，這次改掉頁面上顯示的一段文字 → 開著的頁面提示有新版本 → 重新載入後要看得到新的文字
  5. rollback → 頁面提示版本換了；換回頁面顯示的版本時提示要消失；重新載入後回到舊版，畫面上也要是原本的文字
  6. 資料位置拒絕存取時，頁面要明確顯示讀不到資料（訊息裡不能有資料位置的網址）；恢復後頁尾自己恢復

「資料位置收到什麼」是從資料位置那一端記錄的（瀏覽器不會把 preflight 告訴網頁），所以「只發出單純的 GET」是量出來的。

這支檢查靠下面的 DOM 約定認得頁面。套用 patch_ui.py 之後的 portal UI 都符合；改 UI 時請留著它們：
  header nav button                    導覽列的每一頁；目前所在的那一頁有 aria-current="page"
  main                                 頁面內容。main [role=tab] 是頁內分頁：目前的分頁 aria-selected="true"，
                                       停用的分頁有 disabled（或 aria-disabled="true"），會被略過
  整段文字就是「載入中…」的元素            還在載入（內文裡提到「載入中」不算）
  main 裡以「載入失敗」開頭的元素          這一頁讀不到它要的資料
  .page-error                          這一頁畫不出來（ErrorBoundary 的畫面）
  footer .data-status[data-release]    頁尾的資料版本（讀遠端資料時才有 data-release）
  .data-status-error                   頁尾：讀不到資料
  .data-status-reload                  有另一個版本上線（或資料恢復可讀）時出現的「重新載入」按鈕；
                                       .data-status-notice 是它的說明文字
頁面要在「切回這個分頁」時與「至少每 5 分鐘」檢查有沒有別的版本；讀不到資料時要在一分鐘內自己再試。

需要 Playwright（scripts/requirements-ui.txt）。第一次先下載瀏覽器：
    uv run --with-requirements scripts/requirements-ui.txt playwright install chromium
之後：
    uv run --with-requirements scripts/requirements-ui.txt python scripts/ui_smoke.py --dist … --portal …

Exit code：0 全部通過 · 1 有檢查沒過 · 2 參數錯誤 · 3 環境或輸入問題 · 130 被中斷
"""

from __future__ import annotations

import argparse
import fnmatch
import functools
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for extra in (PROJECT_ROOT / "pipeline", PROJECT_ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import serve_data  # noqa: E402
from portal_pipeline import publish as P  # noqa: E402
from portal_pipeline import releases as R  # noqa: E402
from portal_pipeline.storage import DirStore  # noqa: E402

OK, FAILED, ENV, INTERRUPTED = 0, 1, 3, 130
TOKEN = "ui-smoke-token-0123456789"
WRONG_TOKEN = "wrong-token-0000000000"
SETTLE_TIMEOUT_MS = 20000  # how long one page may take to finish loading
PROMPT_TIMEOUT_MS = 8000  # how long the page may take to react once it has been made to look for another release
CHECK_AFTER_MS = 6 * 60 * 1000  # "at least every 5 minutes": the page's own timer must have fired by then
RETRY_AFTER_MS = 60 * 1000  # a page that could not read the data must have tried again by then
SMOKE_FILE = "config/ui-smoke.md"  # a file no page reads: publishing it makes a new release without changing what is shown
MARK = "UISMOKE"  # put in front of one text that a page shows: a release whose content has to become visible
VISIBLE_TRIES = 6  # how many texts to try before giving up on finding one that is shown and may be changed
OTHER_FILES = 12  # how many more data files get a text of theirs changed, to see every one of them follow a release and a rollback
#: how Chromium words its own reports about a resource it could not fetch (as opposed to what a page logs itself)
BROWSER_REPORT = re.compile(r"Failed to load resource\b|Access to [A-Za-z ]{1,40} at |Refused to (?:load|connect|apply|execute|frame)\b")
#: Chromium that reaches this machine and nothing else.  The check must neither depend on other sites (fonts ...) nor wait for them.
OFFLINE_ARGS = ["--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE localhost , EXCLUDE 127.0.0.1"]
WAYS = {"timer": "定時檢查", "visible": "切回分頁時的檢查"}


class ScriptError(Exception):
    """A problem with the environment or the input (exit code 3)."""


class Halt(Exception):
    """A check failed in a way that leaves nothing for the remaining ones to look at."""


class _SiteHandler(SimpleHTTPRequestHandler):
    config: bytes = b"{}"
    seen: list

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - http.server's name
        pass

    def log_request(self, code="-", size="-") -> None:  # every answer passes through here, errors included
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        self.seen.append((self.command, self.path.split("?")[0], status))

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):  # the browser went away mid-request
            self.close_connection = True

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        if self.path.split("?")[0] == "/config.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.config)))
            self.end_headers()
            self.wfile.write(self.config)
            return
        super().do_GET()


class Site:
    """The built UI as static files. config.json is answered from memory, so this very build can be pointed anywhere
    (which is the point of the run-time setting: one build artefact, any data location)."""

    def __init__(self, dist: Path):
        self.seen: list[tuple[str, str, int]] = []  # (method, path, status) of everything the site was asked
        self.handler = type("SiteHandler", (_SiteHandler,), {"seen": self.seen})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(self.handler, directory=str(dist)))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        host, port = self.server.server_address[:2]
        self.origin = f"http://{host}:{port}"

    def configure(self, config: dict) -> None:
        self.handler.config = json.dumps(config).encode("utf-8")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, passed: bool, what: str, detail: str = "") -> bool:
        print(f"  {'✔' if passed else '✘'} {what}" + (f"：{detail}" if detail and not passed else ""), flush=True)
        self.failed += 0 if passed else 1
        return bool(passed)


#: Decides, inside the page, that it has finished changing - and hands back what it shows at that very moment.
#: "Finished" = the navigation or tab that was clicked is the active one, nothing says it is loading, and the text was the
#: same on two looks a moment apart. Reading the page in the same step leaves no gap in which it could change again.
SNAPSHOT_JS = """
([nav, tab]) => {
  const main = document.querySelector('main');
  if (!main) return false;
  const navButtons = [...document.querySelectorAll('header nav button')];
  const tabs = [...main.querySelectorAll('[role=tab]')];
  if (nav !== null && navButtons.some(b => b.hasAttribute('aria-current')) && navButtons[nav]?.getAttribute('aria-current') !== 'page') return false;
  if (tab !== null && tabs.some(t => t.getAttribute('aria-selected') === 'true') && tabs[tab]?.getAttribute('aria-selected') !== 'true') return false;
  // A placeholder is an element whose whole text says so; a sentence that merely mentions loading is content.
  const whole = (root) => [...root.querySelectorAll('*')].filter(e => e.childElementCount === 0 || e.getAttribute('role') === 'status')
    .map(e => (e.textContent || '').trim());
  const footer = document.querySelector('footer');
  const inMain = whole(main);
  if ([...inMain, ...(footer ? whole(footer) : [])].some(t => /^(資料)?載入中[….。]*$/.test(t))) { window.__uiSmoke = null; return false; }
  const seen = location.hash + '\\n' + document.body.innerText;
  if (window.__uiSmoke !== seen) { window.__uiSmoke = seen; return false; }
  const failed = main.querySelector('.page-error');
  // "載入失敗：…" is said by an element in main - with or without markup inside it ("載入失敗：<code>HTTP 403</code>").  The
  // innermost such elements are the messages; what holds one of them only repeats it.
  const says = (e) => (e.textContent || '').trim().startsWith('載入失敗');
  const failing = [...main.querySelectorAll('*')].filter(says);
  const messages = failing.filter(e => !failing.some(o => o !== e && e.contains(o))).map(e => (e.textContent || '').trim());
  if (!messages.length && says(main)) messages.push((main.textContent || '').trim());
  return {
    text: main.innerText,
    pageError: failed ? failed.innerText : null,
    loadErrors: [...new Set(messages)].map(t => t.split(/[ \\t\\r\\n]+/).join(' ').slice(0, 160)),
    tabs: tabs.map(t => ({ name: t.innerText.trim(), disabled: t.disabled === true || t.getAttribute('aria-disabled') === 'true' })),
  };
}
"""


def _flat(text: str, limit: int = 200) -> str:
    return " ".join(str(text).split())[:limit]


def snapshot(page, *, nav: int | None = None, tab: int | None = None) -> dict:
    """What the page shows once it has settled (see SNAPSHOT_JS); ``{"stuck": ...}`` when it never does."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        return page.wait_for_function(SNAPSHOT_JS, arg=[nav, tab], polling=200, timeout=SETTLE_TIMEOUT_MS).json_value()
    except PlaywrightError:
        main = page.query_selector("main")
        return {"stuck": _flat(main.inner_text() if main else "", 120), "text": "", "pageError": None, "loadErrors": [], "tabs": []}


def release_shown(page) -> str | None:
    el = page.query_selector(".data-status[data-release]")
    return el.get_attribute("data-release") if el else None


def footer_text(page) -> str:
    el = page.query_selector("footer")
    return _flat(el.inner_text() if el else "（沒有 footer）")


def view_problems(snap: dict) -> list[str]:
    """Why the page in this snapshot would not do, if anything."""
    if "stuck" in snap:
        return [f"{SETTLE_TIMEOUT_MS / 1000:g} 秒內沒有載入完成（畫面停在：{snap['stuck'] or '空白'}）"]
    problems = []
    if snap.get("pageError"):
        problems.append("頁面畫不出來：" + _flat(snap["pageError"], 160))
    problems += [_flat(e, 120) for e in snap.get("loadErrors") or []]
    if not problems and not (snap.get("text") or "").strip():
        problems.append("頁面是空的")
    return problems


def visit(page, label: str, report: Report, *, nav: int | None = None) -> tuple[int, str]:
    """Look at the page that is open and at each of its tabs.  Returns (the number of views looked at, the text of the
    page as it opened - before any tab was clicked)."""
    from playwright.sync_api import Error as PlaywrightError

    snap = snapshot(page, nav=nav)
    found = view_problems(snap)
    tabs = snap.get("tabs") or []
    skipped = 0
    for i, tab in enumerate(tabs):
        if tab.get("disabled"):
            skipped += 1
            continue
        try:
            page.locator("main [role=tab]").nth(i).click(timeout=SETTLE_TIMEOUT_MS)
        except PlaywrightError:
            found.append(f"分頁「{tab['name']}」點不到")
            continue
        found += [f"分頁「{tab['name']}」{p}" for p in view_problems(snapshot(page, nav=nav, tab=i))]
    views = 1 + len(tabs) - skipped
    report.check(not found, f"{label}（{views} 個畫面" + (f"，略過 {skipped} 個停用的分頁" if skipped else "") + "）", "；".join(found))
    return views, snap.get("text") or ""


def crawl_nav(page, report: Report, prefix: str = "", texts: dict[str, str] | None = None) -> int:
    """Every page of the navigation and every tab on it, in the document that is open (no reload in between).
    ``texts`` receives what each page shows as it opens, under the address the page then has."""
    count = page.locator("header nav button").count()
    if not count:
        raise ScriptError("找不到導覽列（header nav button）；這不像 portal 的 UI")
    views = 0
    for i in range(count):
        button = page.locator("header nav button").nth(i)
        name = button.inner_text().strip()
        button.click()
        seen, text = visit(page, f"{prefix}頁面「{name}」", report, nav=i)
        views += seen
        if texts is not None:
            at = urlsplit(page.url)
            texts.setdefault((at.path or "/") + (f"?{at.query}" if at.query else "") + (f"#{at.fragment}" if at.fragment else ""), text)
    return views


def releases_in(entries: list[dict]) -> set[str]:
    """The releases the given requests to the data location read files of."""
    return {e["path"].split("/releases/", 1)[1].split("/", 1)[0] for e in entries if "/releases/" in e["path"]}


def files_read(entries: list[dict], release: str) -> set[str]:
    """The files of ``release`` that the data location was asked for and handed out (their paths inside the release)."""
    prefix = f"/releases/{release}/"
    return {e["path"].split(prefix, 1)[1] for e in entries if prefix in e["path"] and e["status"] < 400}


def crawl_routes(page, base: str, routes: list[str], report: Report, seen: list[dict], expect: str,
                 texts: dict[str, str] | None = None) -> tuple[int, list[str]]:
    """Each route opened afresh.  Returns (views, the routes that read anything but the release ``expect``);
    ``texts`` receives what each address shows as it opens."""
    views, strays = 0, []
    for route in routes:
        page.goto("about:blank")
        start = len(seen)
        page.goto(f"{base}/#/{route}")
        count, text = visit(page, f"#/{route}", report)
        views += count
        if texts is not None:
            texts[f"/#/{route}"] = text
        read = releases_in(seen[start:])
        if read - {expect} or release_shown(page) != expect:
            strays.append(f"#/{route} 讀了 {'、'.join(sorted(read)) or '（沒有）'}，頁尾顯示 {release_shown(page)}")
    return views, strays


#: what data is kept in: a site that serves one of these to its own pages is serving them data
DATA_LIKE = (".json", ".yaml", ".yml", ".ndjson", ".jsonl", ".csv")
#: files of that kind that every site may have - unless one of them turns out to be a copy of the data's own pointer
SITE_FILES = ("/manifest.json", "/manifest.webmanifest", "/site.webmanifest", "/asset-manifest.json")


def data_from_site(asked: list[tuple[str, str, int]], dist: Path, allowed: tuple[str, ...] = ()) -> list[str]:
    """The paths the built site was asked for that are (or look like) portal data.

    The UI reads its data from the data location and nothing but ``config.json`` from itself.  A copy of a data file
    bundled into the site - under whatever name and folder - is data that no release can update and no rollback can
    take back, so every request to the site for a JSON / YAML / CSV file counts (``allowed``: patterns for the ones that
    are part of the site itself, e.g. translations).
    """
    found = set()
    for _method, path, _status in asked:
        low = path.lower()
        if path == "/config.json" or any(fnmatch.fnmatch(path, pattern) for pattern in allowed):
            continue
        if path.startswith(("/data/", "/runs/", "/config/")):
            found.add(path)
        elif low.endswith(DATA_LIKE) or low.endswith(".webmanifest"):
            if low in SITE_FILES:
                try:  # a web app manifest is the site's own - a copy of the data pointer under that name is not
                    doc = json.loads((dist / path.lstrip("/")).read_bytes())
                except (OSError, ValueError):
                    continue
                if not (isinstance(doc, dict) and "releaseId" in doc):
                    continue
            found.add(path)
    return sorted(found)


def norm(text: object) -> str:
    return " ".join(str(text).split())


def _leaves(node, where: tuple = ()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaves(value, (*where, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _leaves(value, (*where, i))
    else:
        yield where, node


def _prose(value: object) -> bool:
    """A text a person wrote for a person to read - not an identifier, a link, a date or a number."""
    return (isinstance(value, str) and 12 <= len(value) <= 200 and value == value.strip() and "\n" not in value and "://" not in value
            and (" " in value or any(ord(c) > 0x2E7F for c in value)))


def shown_texts(files: dict[str, bytes], texts: dict[str, str]) -> list[tuple[str, str, tuple, str]]:
    """(address, file, where in the file, text) for the texts of the data that a page shows in full as it opens - the
    longest first.  Only a text that stands in the data exactly once is listed: with a second copy nobody could tell
    which of the two the page shows, and changing one of them would prove nothing."""
    leaves: list[tuple[str, tuple, str]] = []
    for rel in sorted(files):
        if not (rel.startswith("data/") and rel.endswith(".json")):
            continue
        try:
            doc = json.loads(files[rel])
        except ValueError:
            continue
        leaves += [(rel, where, norm(value)) for where, value in _leaves(doc) if isinstance(value, str)]
    corpus = "\n".join(value for _rel, _where, value in leaves)
    pages = {address: norm(text) for address, text in texts.items()}
    found = []
    for rel, where, value in leaves:
        if not _prose(value) or corpus.count(value) != 1:
            continue
        address = next((a for a, text in pages.items() if value in text), None)
        if address is not None:
            found.append((address, rel, where, value))
    return sorted(found, key=lambda item: (-len(item[3]), item[1], [str(k) for k in item[2]]))


def offered(page) -> str | None:
    """What the page says while it offers to reload; None when it does not offer."""
    button = page.query_selector(".data-status-reload")
    if not button:
        return None
    notice = page.query_selector(".data-status-notice")
    return _flat((notice or button).inner_text())


def look_again(page, way: str) -> None:
    """Make the page look for another release, the way it would by itself: its timer fires, or the visitor comes back to the tab."""
    if way == "timer":
        page.clock.fast_forward(CHECK_AFTER_MS)
    else:
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def wait_for_offer(page, ways: tuple[str, ...]) -> tuple[str, str] | None:
    """(the way the page noticed, what it says) once it offers to reload; None when no way made it."""
    from playwright.sync_api import Error as PlaywrightError

    for way in ways:
        look_again(page, way)
        try:
            page.wait_for_selector(".data-status-reload", timeout=PROMPT_TIMEOUT_MS)
        except PlaywrightError:
            continue
        return way, offered(page) or ""
    return None


def offer_gone(page, ways: tuple[str, ...]) -> bool:
    from playwright.sync_api import Error as PlaywrightError

    for way in ways:
        look_again(page, way)
        try:
            page.wait_for_selector(".data-status-reload", state="detached", timeout=PROMPT_TIMEOUT_MS)
        except PlaywrightError:
            continue
        return True
    return False


def accept(page) -> str:
    """The visitor takes the offer: the page reloads.  Returns the text it then shows."""
    with page.expect_navigation():
        page.click(".data-status-reload")
    page.wait_for_selector(".data-status[data-release], .data-status-error", timeout=SETTLE_TIMEOUT_MS)
    return norm(snapshot(page).get("text") or "")


def fresh(page, url: str) -> str:
    """Open ``url`` in a new document of the same tab.  Returns the text it shows."""
    page.goto("about:blank")
    page.goto(url)
    page.wait_for_selector(".data-status[data-release], .data-status-error", timeout=SETTLE_TIMEOUT_MS)
    return norm(snapshot(page).get("text") or "")


def default_routes(index: dict) -> list[str]:
    """Pages that no navigation button leads to directly: one per model and digest, a comparison, the release steps of a model."""
    models = [m for m in index.get("models", []) if isinstance(m, str)]
    routes = [f"models/{m}" for m in models] + [f"digest/{d}" for d in index.get("digests", []) if isinstance(d, str)]
    if len(models) >= 2:
        routes.append("compare/" + ",".join(models[:2]))
    return routes + [f"release/{m}" for m in models]


def escaping_routes(index: dict, other: str) -> list[str]:
    """Links in which the id is a way out of the release.  The portal reads `#/digest/<id>` as data/digests/<id>.json:
    with "../../../<other release>/data/digests/<id>" for an id, that names the same file of another release."""
    out = []
    for route, folder in (("digest", "digests"), ("models", "models")):
        ids = [x for x in index.get(folder, []) if isinstance(x, str)]
        if ids:
            out.append(f"{route}/" + quote(f"../../../{other}/data/{folder}/{ids[0]}", safe=""))
    return out


def launch(p):
    from playwright.sync_api import Error as PlaywrightError

    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    try:
        return p.chromium.launch(env=env, args=OFFLINE_ARGS)
    except PlaywrightError as e:
        raise ScriptError(f"無法啟動 Chromium：{str(e).splitlines()[0]}\n"
                          "  先下載瀏覽器：uv run --with-requirements scripts/requirements-ui.txt playwright install chromium") from e


def run(args: argparse.Namespace) -> int:
    dist = Path(args.dist).resolve()
    portal = Path(args.portal).resolve() if args.portal else dist.parent.parent
    if not (dist / "index.html").is_file():
        raise ScriptError(f"{dist} 沒有 index.html：先在 web/ 執行 npm run build，再用 --dist 指到 dist/")
    if not (portal / "schema" / "llmops.schema.json").is_file():
        raise ScriptError(f"{portal} 不像 portal repo（找不到 schema/llmops.schema.json）：用 --portal 指到 repo 根目錄")
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ScriptError("需要 Playwright：uv run --with-requirements scripts/requirements-ui.txt python scripts/ui_smoke.py …"
                          "（第一次先執行 … playwright install chromium）") from e

    report = Report()
    work = Path(tempfile.mkdtemp(prefix="ui-smoke-"))
    closers: list = []
    halted = ""
    try:
        print("1. 用 portal checkout 的資料建立本機資料位置", flush=True)
        files, skipped = R.collect_tree(portal)
        data = DirStore(work / "portal")
        first = P.publish_tree(data, files, bootstrap=True, actor="ui-smoke")
        if first.status != "published":
            for reason in first.reasons:
                print(f"  ✘ {reason}", flush=True)
            raise ScriptError("portal checkout 的資料無法發布（上面是原因）；先修正資料，UI 的檢查沒有執行")
        report.check(True, f"release {first.release_id}：{len(files)} 個檔案" + (f"（略過 {len(skipped)} 個不發布的檔案）" if skipped else ""))
        markers = 0
        tree = dict(files)  # what the check has published so far

        def publish_marker():
            """A new release that changes nothing a page shows."""
            nonlocal markers
            markers += 1
            tree[SMOKE_FILE] = f"ui_smoke.py 的測試檔（第 {markers} 版），可以忽略。\n".encode("utf-8")
            return P.publish_tree(data, tree, only=["config"], actor="ui-smoke", note=f"ui smoke {markers}")

        def publish_change(rel: str, where: tuple, value: str):
            """A new release in which one text of the data reads differently (refused when the data would not validate)."""
            doc = json.loads(tree[rel])
            node = doc
            for key in where[:-1]:
                node = node[key]
            node[where[-1]] = value
            changed = {**tree, rel: (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")}
            outcome = P.publish_tree(data, changed, base_release=R.read_pointer(data)[0]["releaseId"], actor="ui-smoke", note="ui smoke：改一段顯示的文字")
            if outcome.status == "published":
                tree[rel] = changed[rel]
            return outcome

        site = Site(dist)
        closers.append(site.close)
        seen: list[dict] = []  # what the data location was asked, as its own server saw it
        store = serve_data.make_server(work / "portal", port=0, token=TOKEN, origins=(site.origin,), quiet=True, seen=seen)  # this UI only
        serve_data.serve_in_thread(store)
        closers.append(lambda: (store.shutdown(), store.server_close()))
        data_url = serve_data.base_url(store, TOKEN)
        data_origin = data_url.split(f"/{TOKEN}/")[0]
        site.configure({"dataBaseUrl": data_url})
        ui = site.origin

        def local(url: str) -> bool:
            return url.startswith((ui + "/", data_origin + "/")) or url in (ui, data_origin)

        with sync_playwright() as p:
            browser = launch(p)
            options = {"viewport": {"width": 1400, "height": 900}, "locale": "zh-TW"}
            ctx = browser.new_context(**options)
            page = ctx.new_page()
            noise: list[str] = []
            broken: list[str] = []
            elsewhere: set[str] = set()

            def on_console(message) -> None:
                if message.type != "error":
                    return
                where = (message.location or {}).get("url") or ""
                if where.endswith("/favicon.ico") or (where and not local(where)):
                    return  # raised for (or inside) another site's resource, which this check keeps the browser away from on purpose
                if BROWSER_REPORT.match(message.text):
                    # the browser's own report that something could not be fetched: it names what it tried to fetch first
                    target = re.search(r"https?://[^\s'\"<>)]+", message.text)
                    if target and not local(target.group(0)):
                        return
                # anything else is the page speaking - also when its message happens to carry a link ("Minified React error
                # #310; visit https://react.dev/errors/310 ..." is the page failing to draw, not a note about react.dev)
                noise.append("console.error: " + _flat(message.text) + (f"（{where}）" if where else ""))

            def on_failed(request) -> None:
                if not local(request.url):
                    elsewhere.add(urlsplit(request.url).netloc or request.url[:40])
                elif "ERR_ABORTED" not in (request.failure or ""):  # leaving a page drops what it was still loading
                    broken.append(f"{request.failure} {request.url}")

            page.on("pageerror", lambda e: noise.append(f"pageerror: {_flat(str(e))}"))
            page.on("console", on_console)
            page.on("requestfailed", on_failed)

            print("2. UI 從資料位置載入", flush=True)
            page.goto(ui + "/")
            try:
                page.wait_for_selector(".data-status[data-release], .data-status-error", timeout=SETTLE_TIMEOUT_MS)
            except PlaywrightError:
                raise ScriptError("頁面上沒有出現資料版本列（.data-status）：這份 dist 是套用 patch_ui.py 之後 build 的嗎？") from None
            snapshot(page)
            if not report.check(release_shown(page) == first.release_id, "頁尾顯示資料版本 " + first.release_id, footer_text(page)):
                if any(e["method"] == "OPTIONS" for e in seen):
                    print("    資料位置收到了 preflight（OPTIONS）：UI 的 request 帶了自訂 header；正式環境的資料位置不回應 preflight", flush=True)
                elif not seen:
                    print("    資料位置沒有收到任何 request：UI 沒有讀 config.json 的 dataBaseUrl", flush=True)
                raise Halt("UI 沒有讀到本機的資料位置")

            print("3. 每一頁、每個分頁（瀏覽途中有新的 release 發布）", flush=True)
            second = publish_marker()
            if second.status != "published":
                raise ScriptError("無法發布測試用的 release：" + "；".join(second.reasons))
            published_at = len(seen)
            views = crawl_nav(page, report)
            read, later = releases_in(seen), sum(1 for e in seen[published_at:] if "/releases/" in e["path"])
            report.check(read == {first.release_id} and release_shown(page) == first.release_id,
                         f"途中發布了 {second.release_id}：開著的頁面仍然只讀 {first.release_id}"
                         + (f"（之後又讀了 {later} 個檔案）" if later else "（發布之後它沒有再讀檔案，無從比較）"),
                         f"讀了 {'、'.join(sorted(read))}，頁尾顯示 {release_shown(page)}")
            # The same pages once more, each opened afresh in this very tab.  Now everything has to come from the new release -
            # including what this tab has read before: a page that keeps files by their name alone would go on showing the
            # old ones under the new version label, and would ask the data location for nothing at all.
            earlier = files_read(seen, first.release_id)
            reopened_at = len(seen)
            texts: dict[str, str] = {"/": fresh(page, ui + "/")}
            views += crawl_nav(page, report, prefix="重新打開後的", texts=texts)
            strays = []
            read = releases_in(seen[reopened_at:])
            if read - {second.release_id} or release_shown(page) != second.release_id:
                strays.append(f"導覽列的頁面讀了 {'、'.join(sorted(read)) or '（沒有）'}，頁尾顯示 {release_shown(page)}")
            index = R.read_pointer(data)[0]
            more, off = crawl_routes(page, ui, default_routes(index) + list(args.route), report, seen, second.release_id, texts)
            views += more
            report.check(not (strays + off), f"重新打開的頁面沒有讀別的 release，頁尾都顯示 {second.release_id}", "；".join((strays + off)[:3]))
            missing = sorted(earlier - files_read(seen[reopened_at:], second.release_id))
            report.check(not missing, f"這個分頁先前讀過的 {len(earlier)} 個檔案，重新打開後都改從新的 release 讀",
                         f"沒有從 {second.release_id} 讀：{'、'.join(missing[:4])}{'…' if len(missing) > 4 else ''}"
                         "（頁面顯示的是先前留下來的內容，不是新 release 的）")
            report.check(not noise, f"{views} 個畫面都沒有錯誤訊息", "；".join(noise[:3]))
            from_site = data_from_site(site.seen, dist, tuple(args.allow_site_file))
            report.check(not from_site, "沒有任何資料是從網站本身讀的", "、".join(from_site[:3])
                         + "（網站自己只該提供 config.json；真的是網站本身的檔案——例如翻譯檔——用 --allow-site-file 指明）")
            methods = sorted({e["method"] for e in seen})
            credentials = sorted({h for e in seen for h in e["headers"] if h in ("cookie", "authorization")})
            report.check(methods == ["GET"] and not credentials,
                         f"資料位置收到的 {len(seen)} 個 request 都是單純的 GET（沒有 preflight，不帶 cookie 或授權 header）",
                         "收到 " + "、".join(methods) + ("（OPTIONS 是 preflight：正式環境的資料位置不回應）" if "OPTIONS" in methods else "")
                         + (f"；帶了 {'、'.join(credentials)}" if credentials else ""))
            bad = sorted({f"{e['status']} {e['method']} {e['path']}" for e in seen if e["status"] >= 400})
            bad += sorted({f"{status} {path}" for _, path, status in site.seen if status >= 400 and path != "/favicon.ico"})
            report.check(not (bad or broken), "沒有失敗的 request", "、".join((bad + sorted(set(broken)))[:3]))
            if elsewhere:
                print(f"    （頁面另外向 {'、'.join(sorted(elsewhere)[:4])} 要了資源：這項檢查不讓瀏覽器連到其他網站，也不把它們算進結果）", flush=True)
            # A link can carry anything where an id belongs.  Whatever it carries, a page reads the release it started
            # with - not, say, the digest of one that was rolled back.  (Looked at last: these pages are meant to end in
            # "not found", which is not one of the errors counted above.)
            escapes = escaping_routes(index, first.release_id)
            if escapes:
                asked_at = len(seen)
                for route in escapes:
                    fresh(page, f"{ui}/#/{route}")
                strayed = sorted({e["path"].rsplit("/releases/", 1)[-1] for e in seen[asked_at:] if f"/releases/{first.release_id}/" in e["path"]})
                report.check(not strayed, f"連結裡的 id 帶著「../」，頁面也不會去讀別的 release 的檔案（試了 {len(escapes)} 個連結）",
                             f"讀了 {'、'.join(strayed[:2])}：頁尾寫的是 {second.release_id}，畫面上卻可以是另一個 release（例如已經 rollback 的那一版）的內容。"
                             "ui/files/src/dataSource.ts 只接受單純的相對路徑；這份 UI 用的是舊版，或另外自己組網址")
            ctx.close()

            # From here on the page's clock is in our hands, so that "five minutes later" does not take five minutes.
            timed = browser.new_context(**options)
            timed.clock.install()
            page = timed.new_page()

            print("4. 發布新的 release：頁面不重新部署就讀到新的內容", flush=True)
            # The release changes one text that a page shows, so that "the page reads the new release" can be seen on the page
            # itself and not only in what it asked the data location for.
            home, changed, third, before = ui + "/", None, None, None
            for address, rel, where, value in shown_texts(tree, texts)[:VISIBLE_TRIES]:
                if value not in fresh(page, ui + address):
                    continue
                before = release_shown(page)
                outcome = publish_change(rel, where, f"{MARK} {value}")
                if outcome.status == "published":  # (a text the validators would not let change is simply not the one to use)
                    home, changed, third = ui + address, (address, rel, value), outcome
                    break
            if third is None:
                report.check(False, "找到一段顯示在頁面上的資料文字，用來確認頁面顯示的是新 release 的內容",
                             "重新打開的頁面上沒有完整顯示任何一段資料檔裡的文字（12–200 字、在資料裡只出現一次），「內容有沒有跟著更新」無法檢查")
                fresh(page, home)
                before = release_shown(page)
                third = publish_marker()
            report.check(third.status == "published" and before == second.release_id, "發布成功", "；".join(third.reasons) or f"頁尾顯示 {before}")
            # One text of every OTHER data file that a page shows gets changed too, in the same way: a page that handles
            # one file correctly and keeps another one for itself would otherwise never be looked at again.
            others: list[tuple[str, str, str]] = []  # (address, file, the original text)
            if changed:
                seen_files = {changed[1]}
                for address, rel, where, value in shown_texts(tree, texts):
                    if rel in seen_files or len(others) >= OTHER_FILES:
                        continue
                    if publish_change(rel, where, f"{MARK} {value}").status == "published":
                        seen_files.add(rel)
                        others.append((address, rel, value))
                third_id = R.read_pointer(data)[0]["releaseId"]
            else:
                third_id = third.release_id
            report.check(release_shown(page) == before and offered(page) is None and MARK not in norm(snapshot(page).get("text") or ""),
                         "已經打開的頁面維持原本的版本與內容，也還沒有提示", footer_text(page))
            got = wait_for_offer(page, ("timer", "visible"))
            if report.check(got is not None, "開著的頁面提示有新的資料版本", "頁面沒有提示有新的資料版本（.data-status-reload 沒有出現）"):
                text = accept(page)
                report.check(release_shown(page) == third_id, f"靠{WAYS[got[0]]}提示「{got[1]}」，重新載入後顯示 {third_id}", footer_text(page))
                if changed:
                    report.check(MARK in text, f"重新載入後的畫面是新 release 的內容（{changed[0].lstrip('/') or '首頁'} 上的一段文字改了，畫面跟著改）",
                                 f"{changed[1]} 的「{_flat(changed[2], 40)}」在 {third_id} 改成以「{MARK}」開頭，畫面上仍是原本的文字："
                                 "頁尾顯示新的版本，內容卻是舊的")

            def stale(want_mark: bool) -> list[str]:
                """The other data files whose text on its page is not the one that is live (each page opened afresh)."""
                wrong = []
                for address, rel, value in others:
                    shown = norm(fresh(page, ui + address))
                    if (MARK in shown) != want_mark or value not in shown:
                        wrong.append(f"{rel}（{address.lstrip('/') or '首頁'}）")
                return wrong

            if others:
                wrong = stale(True)
                report.check(not wrong, f"其他 {len(others)} 個資料檔在畫面上的文字也都是新 release 的", "仍是舊的內容：" + "、".join(wrong[:4]))
                fresh(page, home)  # (back on the page the rest of the check keeps open)

            print("5. rollback", flush=True)
            back = P.rollback(data, to=second.release_id, actor="ui-smoke", reason="ui smoke")
            report.check(back.status == "rolled_back" and back.release_id == second.release_id, f"rollback 成功：上線的回到 {second.release_id}", "；".join(back.reasons))
            text = None
            if got is None:  # already reported: the page never prompts, so look at a fresh load instead
                text = fresh(page, home)
            else:
                again = wait_for_offer(page, ("visible", "timer"))
                if report.check(again is not None, "開著的頁面提示資料換了版本", "頁面沒有提示（.data-status-reload 沒有出現）"):
                    print(f"    靠{WAYS[again[0]]}提示「{again[1]}」", flush=True)
                    # the version this page shows goes live again: there is nothing left to reload for
                    forward = P.rollback(data, to=third_id, actor="ui-smoke", reason="ui smoke")
                    report.check(forward.status == "rolled_back" and offer_gone(page, ("visible", "timer")),
                                 "上線的換回頁面顯示的版本後，提示消失", "；".join(forward.reasons) or f"提示還在：{offered(page)}")
                    P.rollback(data, to=second.release_id, actor="ui-smoke", reason="ui smoke")
                    text = accept(page) if wait_for_offer(page, ("visible", "timer")) is not None else fresh(page, home)
            if text is None:  # the page did not prompt for the rollback (reported above): what does a fresh load show?
                text = fresh(page, home)
            report.check(release_shown(page) == second.release_id, f"重新載入後回到 {second.release_id}", footer_text(page))
            if changed:
                report.check(MARK not in text and changed[2] in text, "rollback 之後畫面也回到原本的內容",
                             "畫面上仍是被撤下的那一版的文字" if MARK in text else f"畫面上找不到原本的文字「{_flat(changed[2], 40)}」")
            if others:
                wrong = stale(False)
                report.check(not wrong, f"rollback 之後，其他 {len(others)} 個資料檔在畫面上的文字也都回到原本的內容",
                             "仍是被撤下的那一版的文字（或原本的文字不見了）：" + "、".join(wrong[:4]))

            print("6. 資料位置拒絕存取時", flush=True)
            site.configure({"dataBaseUrl": data_url.replace(TOKEN, WRONG_TOKEN)})
            refused = timed.new_page()
            said_aloud: list[str] = []  # what the page itself writes to the console (not the browser's own report of a failed request)
            refused.on("console", lambda m: None if BROWSER_REPORT.match(m.text) else said_aloud.append(m.text))
            refused.goto(ui + "/")
            try:
                refused.wait_for_selector(".data-status-error", timeout=SETTLE_TIMEOUT_MS)
            except PlaywrightError:
                report.check(False, "頁尾明確顯示讀不到資料", "沒有出現 .data-status-error")
            else:
                text = _flat(refused.inner_text(".data-status-error"))  # the message itself: a "403" elsewhere in the footer says nothing
                # (a real distribution may send its 403 without CORS headers; a browser then only says that it could not connect)
                report.check("403" in text or "連不到" in text, "頁尾明確顯示讀不到資料" + ("（HTTP 403）" if "403" in text else ""), text)
                shown = refused.inner_text("body")
                # ... and nowhere else a visitor (or whoever they send a screenshot or a log to) reads it: the window's
                # title, a tooltip or any other attribute, what the page writes to the console
                places = []
                if WRONG_TOKEN in shown or "/manifest.json" in shown:
                    places.append("頁面文字")
                if WRONG_TOKEN in refused.title():
                    places.append("視窗標題")
                attributes = refused.evaluate("[...document.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.name + '=' + a.value))")
                if any(WRONG_TOKEN in a for a in attributes):
                    places.append("元素屬性（" + next(a.split("=", 1)[0] for a in attributes if WRONG_TOKEN in a) + "）")
                if any(WRONG_TOKEN in m for m in said_aloud):
                    places.append("console 訊息")
                report.check(not places, "畫面上沒有資料位置的網址（存取 token 不會出現在畫面上）", "出現在" + "、".join(places))
                site.configure({"dataBaseUrl": data_url})
                refused.clock.fast_forward(RETRY_AFTER_MS)
                try:
                    refused.wait_for_selector(".data-status[data-release]", timeout=PROMPT_TIMEOUT_MS)
                    recovered = release_shown(refused) == second.release_id
                except PlaywrightError:
                    recovered = False
                if report.check(recovered, "資料位置恢復後，頁尾自己恢復（不必重新整理）", footer_text(refused)):
                    said = offered(refused)
                    if report.check(said is not None, "並提示重新載入（當時打開的頁面內容還停在讀不到資料的樣子）", footer_text(refused)):
                        accept(refused)
                        report.check(not view_problems(snapshot(refused)), f"提示「{said}」，重新載入後頁面正常", "；".join(view_problems(snapshot(refused))))
            browser.close()
    except Halt as e:
        halted = str(e)
    finally:
        for close in closers:
            close()
        if args.keep:
            print(f"（暫存資料保留在 {work}）", flush=True)
        else:
            shutil.rmtree(work, ignore_errors=True)

    print(flush=True)
    if halted:
        print(f"✘ {halted}：後面的檢查沒有執行。", flush=True)
        return FAILED
    if report.failed:
        print(f"✘ 有 {report.failed} 項檢查沒有通過。", flush=True)
        return FAILED
    print("✔ 全部通過：這份 UI 可以從資料位置讀資料，資料更新與 rollback 都不需要重新部署 UI。", flush=True)
    return OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ui_smoke.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dist", required=True, help="build 好的 UI（web/dist）")
    p.add_argument("--portal", help="portal repo 根目錄，資料從這裡來（預設是 dist 的上兩層）")
    p.add_argument("--route", action="append", default=[], metavar="ROUTE", help="額外要檢查的路徑，例如 compare/a,b,c（可重複）")
    p.add_argument("--allow-site-file", action="append", default=[], metavar="PATTERN",
                   help="網站本身的 JSON / YAML 檔（不是 portal 資料，例如翻譯檔 /locales/*.json）；可重複。沒有列出的都算「從網站本身讀資料」")
    p.add_argument("--keep", action="store_true", help="保留暫存的資料位置（除錯用）")
    return p


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # captured streams in tests, exotic consoles
            pass
    args = build_parser().parse_args(argv)
    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, _interrupt)  # a kill must clean up like Ctrl-C: the temp folder holds portal data
    except ValueError:  # not the main thread (somebody imported this): their signals are theirs
        pass
    try:
        return run(args)
    except ScriptError as e:
        print(f"✘ {e}", file=sys.stderr, flush=True)
        return ENV
    except (R.ReleaseError, OSError) as e:
        print(f"✘ {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return ENV
    except KeyboardInterrupt:
        print("✘ 中斷：檢查沒有做完。", file=sys.stderr, flush=True)
        return INTERRUPTED
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())

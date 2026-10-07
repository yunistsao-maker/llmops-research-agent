"""scripts/ui_smoke.py against a tiny stand-in UI (fixtures/fake_ui) with one defect switched on at a time.

The real check of the real UI is running ui_smoke.py itself; these tests make sure the checker notices what it claims to.
They need Playwright and its Chromium, and are skipped without them.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

import ui_smoke  # noqa: E402
from conftest import FIXTURES, REPO, snapshot_tree, write_tree  # noqa: E402

FAKE = (FIXTURES / "fake_ui" / "index.html").read_text(encoding="utf-8")


def browser_available() -> bool:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not browser_available(), reason="Playwright's Chromium is not installed")


def fake_dist(folder: Path, **flaws) -> Path:
    folder.mkdir()
    (folder / "index.html").write_text(FAKE.replace("/*FLAWS*/{}/*END*/", json.dumps(flaws)), encoding="utf-8")
    return folder


@pytest.fixture()
def smoke(tmp_path, capsys, monkeypatch):
    """Run ui_smoke on the stand-in UI with the given flaws: (exit code, output)."""
    portal = write_tree(tmp_path / "llmops-portal", snapshot_tree())
    monkeypatch.setattr(ui_smoke, "SETTLE_TIMEOUT_MS", 3000)  # the stand-in is instant; a stuck page should not cost 20 s here
    monkeypatch.setattr(ui_smoke, "PROMPT_TIMEOUT_MS", 2000)

    def run(**flaws):
        dist = fake_dist(tmp_path / f"dist-{len(list(tmp_path.glob('dist-*')))}", **flaws)
        capsys.readouterr()
        code = ui_smoke.main(["--dist", str(dist), "--portal", str(portal)])
        out = capsys.readouterr()
        return code, out.out + out.err

    return run


def test_a_healthy_ui_passes_every_check(smoke):
    code, out = smoke()
    assert code == 0, out
    assert "✘" not in out and "全部通過" in out
    for expected in (
        "頁面「首頁」（1 個畫面）", "頁面「分頁頁」（3 個畫面，略過 1 個停用的分頁）", "頁面「資料頁」（1 個畫面）",
        "#/digest/2026-09-24",
        "開著的頁面仍然只讀", "（之後又讀了 2 個檔案）",
        "重新打開後的頁面「首頁」（1 個畫面）", "重新打開後的頁面「資料頁」（1 個畫面）", "重新打開的頁面沒有讀別的 release，頁尾都顯示",
        "這個分頁先前讀過的 3 個檔案，重新打開後都改從新的 release 讀",
        "已經打開的頁面維持原本的版本與內容，也還沒有提示",
        "重新載入後的畫面是新 release 的內容（首頁 上的一段文字改了，畫面跟著改）", "rollback 之後畫面也回到原本的內容",
        "其他 2 個資料檔在畫面上的文字也都是新 release 的", "rollback 之後，其他 2 個資料檔在畫面上的文字也都回到原本的內容",
        "都是單純的 GET（沒有 preflight，不帶 cookie 或授權 header）", "沒有任何資料是從網站本身讀的", "沒有失敗的 request",
        "連結裡的 id 帶著「../」，頁面也不會去讀別的 release 的檔案（試了 1 個連結）",
        "fonts.googleapis.com",  # asked for by the page, kept away from, and not held against it
        "靠定時檢查提示「有新的資料版本重新載入」", "靠切回分頁時的檢查提示", "提示消失", "rollback 成功", "重新載入後回到",
        "頁尾明確顯示讀不到資料（HTTP 403）", "畫面上沒有資料位置的網址", "頁尾自己恢復", "提示「資料已經可以讀取重新載入」，重新載入後頁面正常",
    ):
        assert expected in out, f"{expected}\n{out}"


def test_the_links_that_try_to_leave_the_release():
    index = {"models": ["gpt-6-sol", "b"], "digests": ["2026-10-01", "2026-09-24"]}
    assert ui_smoke.escaping_routes(index, "20261002T010000Z-5341fc8c") == [
        "digest/..%2F..%2F..%2F20261002T010000Z-5341fc8c%2Fdata%2Fdigests%2F2026-10-01",
        "models/..%2F..%2F..%2F20261002T010000Z-5341fc8c%2Fdata%2Fmodels%2Fgpt-6-sol"]
    assert ui_smoke.escaping_routes({"models": [], "digests": [5]}, "x") == []


def test_the_pages_no_button_leads_to_are_opened_too():
    index = {"models": ["a", "b", "c"], "digests": ["2026-10-01"]}
    assert ui_smoke.default_routes(index) == ["models/a", "models/b", "models/c", "digest/2026-10-01", "compare/a,b", "release/a", "release/b", "release/c"]
    assert ui_smoke.default_routes({"models": ["a"], "digests": []}) == ["models/a", "release/a"]
    assert ui_smoke.default_routes({}) == []


@pytest.mark.parametrize("flaws, expect", [
    ({"brokenTab": True}, "頁面「分頁頁」（3 個畫面，略過 1 個停用的分頁）：分頁「乙」頁面畫不出來：這個頁面無法顯示。"),
    ({"stuckPage": True}, "頁面「資料頁」（1 個畫面）：3 秒內沒有載入完成"),
    ({"emptyPage": True}, "頁面「資料頁」（1 個畫面）：頁面是空的"),
    ({"readsOwnSite": True}, "✘ 沒有任何資料是從網站本身讀的：/data/catalog.json"),
    ({"consoleError": True}, "個畫面都沒有錯誤訊息：console.error: something broke while drawing the data page"),
    # a message that carries a link is still the page's own message (it used to be taken for a note about the other site)
    ({"consoleErrorWithLink": True}, "個畫面都沒有錯誤訊息：console.error: Error: Minified React error #310; visit https://react.dev/errors/310"),
    # "403" somewhere in the footer is not the page saying that it was refused
    ({"vagueRefusal": True}, "✘ 頁尾明確顯示讀不到資料：目前無法顯示資料。"),
    # the address that was tried holds the access token: nowhere on the page, not only not in the footer
    ({"showsAddress": True}, "✘ 畫面上沒有資料位置的網址（存取 token 不會出現在畫面上）"),
    ({"stalePrompt": True}, "✘ 上線的換回頁面顯示的版本後，提示消失：提示還在"),
    ({"noRetry": True}, "✘ 資料位置恢復後，頁尾自己恢復（不必重新整理）"),
    # --- round 4: four defective UIs that were reported as healthy
    # a copy of the data built into the site, under a name and a folder of its own
    ({"readsBundledCopy": True}, "✘ 沒有任何資料是從網站本身讀的：/assets/sources.json"),
    # every file but the one that is looked at first is replaced only by a LATER release: a rollback never shows on those pages
    ({"newerWinsBut": "data/catalog.json"}, "✘ rollback 之後，其他 2 個資料檔在畫面上的文字也都回到原本的內容：仍是被撤下的那一版的文字（或原本的文字不見了）：data/lifecycle.json（#/tabs）、data/sources.json（#/data）"),
    # 「載入失敗：<code>…</code>」: a load failure whose message holds markup
    ({"markedUpFailure": True}, "頁面「資料頁」（1 個畫面）：載入失敗：sources.json 的格式不對"),
    # the address that was tried (token included) where no text of the page shows it
    ({"addressInTitle": True}, "✘ 畫面上沒有資料位置的網址（存取 token 不會出現在畫面上）：出現在視窗標題"),
    ({"addressInTooltip": True}, "✘ 畫面上沒有資料位置的網址（存取 token 不會出現在畫面上）：出現在元素屬性（title）"),
    ({"addressInConsole": True}, "✘ 畫面上沒有資料位置的網址（存取 token 不會出現在畫面上）：出現在console 訊息"),
    # after a rollback the page shows neither the withdrawn text nor the one that is live again
    ({"blankAfterRollback": True}, "✘ rollback 之後畫面也回到原本的內容：畫面上找不到原本的文字"),
    # ... and the same on the pages of the other data files: "the withdrawn text is gone" is not "the original text is back"
    ({"blankOthersAfterRollback": True}, "✘ rollback 之後，其他 2 個資料檔在畫面上的文字也都回到原本的內容：仍是被撤下的那一版的文字（或原本的文字不見了）：data/lifecycle.json（#/tabs）、data/sources.json（#/data）"),
    # whatever a link carries where an id belongs is asked for: "../" takes the request to another release
    ({"followsLinks": True}, "✘ 連結裡的 id 帶著「../」，頁面也不會去讀別的 release 的檔案（試了 1 個連結）：讀了 "),
])
def test_each_kind_of_defect_is_reported_where_it_is(smoke, flaws, expect):
    code, out = smoke(**flaws)
    assert code == 1, out
    assert expect in out, out
    assert "✔ 頁面「首頁」（1 個畫面）" in out  # the healthy pages are still reported as healthy


# A page can show the new version in its footer and the old data above it.  Nothing on the screen says so, and a page that
# asks the data location for nothing has not read "another release" either - so it is looked for from both ends: what
# the data location was asked, and what the page shows after the data changed.
def test_a_page_that_keeps_files_by_name_shows_old_data_under_the_new_version(smoke):
    code, out = smoke(keepsFiles=True)
    assert code == 1, out
    line = next(l for l in out.splitlines() if "這個分頁先前讀過的 3 個檔案" in l)
    assert line.startswith("  ✘") and "data/catalog.json、data/lifecycle.json、data/sources.json" in line and "不是新 release 的" in line, out
    assert "✘ 重新載入後的畫面是新 release 的內容" in out and "頁尾顯示新的版本，內容卻是舊的" in out, out
    assert "✔ 重新打開後的頁面「首頁」（1 個畫面）" in out  # every page still drew, and no page read another release
    assert "全部通過" not in out


def test_a_page_that_only_takes_later_releases_never_shows_a_rollback(smoke):
    code, out = smoke(newerWins=True)
    assert code == 1, out
    assert "✘ rollback 之後畫面也回到原本的內容：畫面上仍是被撤下的那一版的文字" in out, out
    assert "✘ rollback 之後，其他 2 個資料檔在畫面上的文字也都回到原本的內容：仍是被撤下的那一版的文字" in out, out
    assert "✔ 重新載入後的畫面是新 release 的內容" in out and "✔ 這個分頁先前讀過的 3 個檔案" in out, out  # going forward it is fine
    assert out.count("✘") == 3, out  # those two lines and the summary


def test_when_no_page_shows_a_text_of_the_data_the_check_says_that_it_could_not_look(smoke):
    code, out = smoke(showsNoData=True)
    assert code == 1, out
    assert "✘ 找到一段顯示在頁面上的資料文字，用來確認頁面顯示的是新 release 的內容" in out and "無法檢查" in out, out
    assert "rollback 之後畫面也回到原本的內容" not in out and "重新載入後回到" in out  # the rest still ran


def test_only_a_text_that_is_shown_in_full_and_stands_in_the_data_once_is_used():
    files = {
        "data/a.json": json.dumps({"summary": "這一段文字完整地顯示在頁面上", "id": "claude-sonnet-5-5-long-id", "url": "https://example.com/a page",
                                   "twice": "這段文字在資料裡出現兩次喔", "part": "只有一半顯示在頁面上的文字"}).encode(),
        "data/b.json": json.dumps({"notes": ["這段文字在資料裡出現兩次喔", "短", "比較長的一段文字：這一段文字完整地顯示在頁面上嗎"]}).encode(),
        "runs/x.json": json.dumps({"text": "不是資料檔的文字也顯示在頁面上"}).encode(),
        "data/broken.json": b"{",
    }
    page = "標題 這一段文字完整地顯示在頁面上 claude-sonnet-5-5-long-id https://example.com/a page 這段文字在資料裡出現兩次喔 只有一半顯示 不是資料檔的文字也顯示在頁面上"
    # "這一段文字完整地顯示在頁面上" also stands inside a longer text of b.json: changing a.json would not prove what the page shows
    assert ui_smoke.shown_texts(files, {"/": page}) == []
    files["data/b.json"] = json.dumps({"notes": ["這段文字在資料裡出現兩次喔", "短"]}).encode()
    assert ui_smoke.shown_texts(files, {"/": "沒有", "/#/x": page}) == [("/#/x", "data/a.json", ("summary",), "這一段文字完整地顯示在頁面上")]
    files["data/c.json"] = json.dumps({"list": [{"note": "另一段比較長的文字  也完整地顯示在頁面上了"}]}).encode()
    found = ui_smoke.shown_texts(files, {"/": page + "\n另一段比較長的文字 也完整地顯示在頁面上了"})
    assert [(rel, where) for _a, rel, where, _v in found] == [("data/c.json", ("list", 0, "note")), ("data/a.json", ("summary",))]  # the longest first


# The next two are what a browser does not report to the page; only the data location itself can tell.
def test_a_page_that_mixes_releases_is_told_which_ones_it_read(smoke):
    code, out = smoke(rereadsPointer=True)
    line = next(l for l in out.splitlines() if "途中發布了" in l)
    assert code == 1 and line.startswith("  ✘") and "讀了" in line, out
    assert line.count("Z-") >= 4, line  # the release it should have kept to, and the one it read as well
    assert "✔ 頁面「首頁」（1 個畫面）" in out  # every page still drew: this defect shows nowhere on the screen


def test_requests_that_need_a_preflight_are_found_although_the_browser_hides_them(smoke):
    code, out = smoke(customHeader=True)
    assert code == 1, out
    line = next(l for l in out.splitlines() if "單純的 GET" in l)
    assert line.startswith("  ✘") and "收到 GET、OPTIONS（OPTIONS 是 preflight：正式環境的資料位置不回應）" in line, out
    assert "✘ 沒有失敗的 request：403 OPTIONS /" in out, out


def test_a_ui_that_never_notices_a_new_release_fails_with_an_explanation(smoke):
    code, out = smoke(neverNotices=True)
    assert code == 1 and "沒有提示有新的資料版本" in out, out
    assert "重新載入後回到" in out and "✘ 重新載入後回到" not in out  # the rollback itself is still checked, on a fresh load


@pytest.mark.parametrize("flaw, first, second", [("noTimer", "靠切回分頁時的檢查提示「有新的資料版本", "靠切回分頁時的檢查提示"),
                                                 ("noEvent", "靠定時檢查提示「有新的資料版本", "靠定時檢查提示")])
def test_either_way_of_noticing_is_enough_and_the_output_says_which(smoke, flaw, first, second):
    code, out = smoke(**{flaw: True})
    assert code == 0, out
    assert first in out and out.count(second) == 2, out


def test_a_page_without_the_data_version_line_is_not_the_patched_ui(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ui_smoke, "SETTLE_TIMEOUT_MS", 3000)
    portal = write_tree(tmp_path / "llmops-portal", snapshot_tree())
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>x</title><header><nav><button>a</button></nav></header><main>hello</main>", encoding="utf-8")
    assert ui_smoke.main(["--dist", str(dist), "--portal", str(portal)]) == 3
    assert "patch_ui.py" in capsys.readouterr().err


def test_a_ui_that_cannot_read_the_data_location_stops_with_the_reason(tmp_path, capsys, monkeypatch):
    """Every request with a custom header: the data location only ever sees preflights, and says so."""
    monkeypatch.setattr(ui_smoke, "SETTLE_TIMEOUT_MS", 3000)
    portal = write_tree(tmp_path / "llmops-portal", snapshot_tree())
    page = FAKE.replace('const pointer = () => json(base + "manifest.json", { cache: "no-store" });',
                        'const pointer = () => json(base + "manifest.json", { cache: "no-store", headers: { "X-Portal": "1" } });')
    assert page != FAKE
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(page, encoding="utf-8")
    assert ui_smoke.main(["--dist", str(dist), "--portal", str(portal)]) == 1
    out = capsys.readouterr().out
    assert "✘ 頁尾顯示資料版本" in out and "preflight（OPTIONS）" in out and "後面的檢查沒有執行" in out


def test_data_that_cannot_be_published_stops_before_the_browser(tmp_path, capsys):
    tree = snapshot_tree()
    tree["runs/perf/bad.json"] = b'{"results": []}\n'
    portal = write_tree(tmp_path / "llmops-portal", tree)
    dist = fake_dist(tmp_path / "dist")
    assert ui_smoke.main(["--dist", str(dist), "--portal", str(portal)]) == 3
    out = capsys.readouterr()
    assert "runs/perf/bad.json" in out.out and "無法發布" in out.err


def test_being_killed_leaves_no_portal_data_behind(tmp_path):
    """The temp folder holds a copy of the portal data (the internal inventory included): a kill must remove it like Ctrl-C."""
    portal = write_tree(tmp_path / "llmops-portal", snapshot_tree())
    dist = fake_dist(tmp_path / "dist", stuckPage=True)  # a page that never settles keeps the run busy for a while
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {**os.environ, "TMPDIR": str(temp), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, str(REPO / "scripts" / "ui_smoke.py"), "--dist", str(dist), "--portal", str(portal)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8")
    try:
        deadline = time.time() + 60
        while not list(temp.glob("ui-smoke-*/portal/manifest.json")):
            assert proc.poll() is None and time.time() < deadline, "the run never got as far as publishing its data"
            time.sleep(0.1)
        time.sleep(1.5)  # ... and into the browser
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130, (out, err)
    assert "中斷" in err and not list(temp.glob("ui-smoke-*")), (out, err, list(temp.iterdir()))

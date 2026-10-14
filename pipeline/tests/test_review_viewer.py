"""scripts/review_proposal.html: what a person looks at before approving must show everything, whatever shape it has.

The page is opened from disk in a real browser (Playwright; skipped without it).
"""

from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("playwright.sync_api")

from conftest import FIXTURES, REPO  # noqa: E402

VIEWER = REPO / "scripts" / "review_proposal.html"
RUN = json.loads((FIXTURES / "accepted_run.json").read_text(encoding="utf-8"))
# the fixture run changes one date; give it the other things a proposal can carry
RUN["proposal"]["newSources"] = [{"id": "s-new", "title": "Deprecations", "publisher": "Example AI", "type": "official-doc", "url": "https://docs.example.com/deprecations"}]
RUN["proposal"]["digest"] = {
    "id": "2026-10-02", "periodStart": "2026-09-25", "periodEnd": "2026-10-02", "headline": "一個日期更新",
    "newModels": [{"title": "Model X", "impact": "high", "detail": "發布了。", "vendor": "Example AI", "clouds": ["bedrock"], "sources": [{"id": "s-new"}]}],
    "lifecycleChanges": [], "pricingChanges": [], "litellm": [], "security": [], "regulatory": [],
    "actionItems": [{"priority": "P0", "action": "更新 EOL 日期", "due": "2026-10-09"}],
    "watchlist": [{"name": "Model Y", "status": "rumor", "vendor": "Example AI", "note": "未證實", "sources": []}],
}
RUN["proposal"]["asOfFiles"] = ["data/lifecycle.json"]
RUN["proposal"]["unverified"] = [{"claim": "Model Y 下週發布", "reason": "只有社群消息"}]


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("Playwright's Chromium is not installed")
        yield b
        b.close()


@pytest.fixture()
def view(browser, tmp_path):
    """Open a document in the viewer: (page, things that went wrong in it)."""
    page = browser.new_page()
    wrong: list[str] = []
    page.on("pageerror", lambda e: wrong.append(f"pageerror: {e}"))
    page.on("dialog", lambda d: (wrong.append(f"dialog: {d.message}"), d.dismiss()))
    page.on("request", lambda r: wrong.append(f"request: {r.url}") if not r.url.startswith("file://") else None)
    page.goto(VIEWER.as_uri())

    def show(doc, raw: str | None = None):
        path = tmp_path / f"doc-{len(list(tmp_path.iterdir()))}.json"
        path.write_text(raw if raw is not None else json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        page.set_input_files("#file", str(path))
        page.wait_for_selector("#out > *")
        return page, wrong

    yield show
    page.close()


def headings(page) -> list[str]:
    return [h.inner_text() for h in page.query_selector_all("h2")]


def test_a_run_record_is_shown_in_full(view):
    page, wrong = view(RUN)
    assert not wrong
    assert page.inner_text(".banner") .startswith("status：accepted")
    titles = headings(page)
    for expected in ("摘要", "資料變更（", "新增來源", "標記為「今天重新確認過」", "Weekly digest 2026-10-02", "新模型（1）", "行動項目（1）", "追蹤清單（1）",
                     "未能確認", "抓取的頁面（", "原始 JSON"):
        assert any(t.startswith(expected) for t in titles), (expected, titles)
    assert page.query_selector(".chip.high") and "影響：high" in page.inner_text("#out") and "data/lifecycle.json" in page.inner_text("#out")
    ops = RUN["proposal"]["ops"]
    assert len(page.query_selector_all(".diff")) == len(ops) and len(page.query_selector_all("blockquote")) >= len(ops)
    # every link says where it really leads, in front of whatever text it shows
    link = page.query_selector("blockquote a")
    host = link.evaluate("a => a.previousElementSibling && a.previousElementSibling.tagName === 'B' ? a.previousElementSibling.textContent : ''")
    assert host and host == link.evaluate("a => new URL(a.href).hostname")


def test_a_bare_proposal_is_shown_too(view):
    page, wrong = view(RUN["proposal"])
    assert not wrong and "這是一份提案檔" in page.inner_text(".banner")
    assert any(t.startswith("資料變更（") for t in headings(page))


def test_one_part_that_cannot_be_drawn_does_not_hide_the_others(view):
    doc = copy.deepcopy(RUN)
    p = doc["proposal"]
    first = next(iter(p["evidence"]))
    p["evidence"][first] = None                       # a quote that is not an object
    p["ops"] = [None, "drop everything", *p["ops"]]   # operations that are not objects
    p["newSources"] = [{"id": {"toString": "x"}, "url": {"toString": "x"}, "title": 5}, *p.get("newSources", [])]
    p["digest"]["newModels"] = [{"title": ["a list"], "impact": "banner ok", "detail": {"x": 1}, "sources": "nope", "clouds": 7}]
    p["digest"]["actionItems"] = "none"
    p["unverified"] = [None, 3, {"claim": {"deep": True}}]
    doc["report"] = {"errors": 5, "warnings": {"not": "a list"}}
    doc["stats"]["pages"] = [None, {"url": 12, "chars": "many"}, *doc["stats"]["pages"]]
    doc["usage"] = "a lot"
    page, wrong = view(doc)
    assert not wrong
    titles = headings(page)
    for expected in ("摘要", "資料變更（", "新增來源", "Weekly digest", "新模型（1）", "抓取的頁面（", "原始 JSON"):
        assert any(t.startswith(expected) for t in titles), (expected, titles)
    assert len(page.query_selector_all(".diff")) == len(p["ops"])  # every operation has its card, the odd ones too
    assert "無法解析" not in page.inner_text("#out")
    # a value never becomes a class name
    assert not page.query_selector(".chip.banner") and not page.query_selector(".chip.ok")
    assert len(page.query_selector_all(".banner.ok, .banner.warn, .banner.bad")) == len(page.query_selector_all(".banner"))
    assert page.evaluate("[...document.querySelectorAll('#out [class]')].every(e => /^[a-z ]+$/.test(e.className))")


@pytest.mark.parametrize("url", [
    "https://user:pw@login.example@evil.example/", "https://login.example@evil.example/", "javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
    "http://plain.example/", "//evil.example/x", " https://evil.example/", "https://", "not a url",
])
def test_only_a_plain_https_address_becomes_a_link(view, url):
    doc = copy.deepcopy(RUN)
    for e in doc["proposal"]["evidence"].values():
        e["url"] = url
    doc["proposal"]["newSources"] = [{"id": "s-x", "title": "t", "publisher": "p", "type": "official-doc", "url": url}]
    doc["stats"]["pages"] = [{"url": url, "tier": "official", "chars": 10}]
    page, wrong = view(doc)
    assert not wrong
    hrefs = page.evaluate("[...document.querySelectorAll('#out a')].map(a => a.getAttribute('href'))")
    assert hrefs == [], hrefs
    assert "不是單純的 https 網址" in page.inner_text("#out")


def test_text_from_the_web_stays_text(view):
    doc = copy.deepcopy(RUN)
    nasty = '<img src=x onerror=alert(1)><script>alert(2)</script>"><svg onload=alert(3)>'
    p = doc["proposal"]
    p["summary"] = nasty
    for e in p["evidence"].values():
        e.update(quote=nasty, publisher=nasty, tier=nasty)
    for op in p["ops"]:
        op.update(rationale=nasty, path=nasty, file=nasty)
    p["digest"]["headline"] = nasty
    doc["error"] = nasty
    doc["report"] = {"errors": [nasty], "warnings": [nasty]}
    page, wrong = view(doc)
    assert not wrong
    assert page.evaluate("document.querySelectorAll('#out img, #out script, #out svg, #out [onerror], #out [onload]').length") == 0
    assert nasty in page.inner_text("#out")
    assert all(h.startswith("https://") for h in page.evaluate("[...document.querySelectorAll('#out a')].map(a => a.getAttribute('href'))"))


def test_a_value_that_is_a_key_of_every_object_is_not_a_class_name_either(view):
    """`constructor` and `__proto__` are found in any JavaScript object that is used as a table."""
    doc = copy.deepcopy(RUN)
    items = [{"title": f"Model {word}", "impact": word, "detail": "d", "sources": []} for word in ("constructor", "__proto__", "toString", "hasOwnProperty", "high")]
    doc["proposal"]["digest"]["newModels"] = items
    doc["proposal"]["digest"]["actionItems"] = [{"priority": "constructor", "action": "a"}]
    page, wrong = view(doc)
    assert not wrong and "影響：constructor" in page.inner_text("#out") and "影響：__proto__" in page.inner_text("#out")
    classes = page.evaluate("[...new Set([...document.querySelectorAll('#out [class]')].flatMap(e => e.className.split(/\\s+/)))].sort()")
    assert set(classes) <= {"banner", "ok", "warn", "bad", "card", "stat", "grid", "diff", "old", "new", "chip", "high", "medium", "empty", "muted"}, classes
    assert len(page.query_selector_all(".chip.high")) >= 1


def test_what_is_called_checked_today_is_shown_with_how_much_of_it_was_read(view):
    doc = copy.deepcopy(RUN)
    p = doc["proposal"]
    p["asOfFiles"] = ["data/lifecycle.json", "data/litellm.json", "data/catalog.json"]
    p["touchSources"] = ["ms-foundry-retirement-schedule"]
    p["validation"]["asOfCoverage"] = {"data/lifecycle.json": {"cited": 32, "read": 3, "unread": ["a"]}, "data/litellm.json": {"cited": 2, "read": 2, "unread": []},
                                       "constructor": {"cited": 1, "read": 1}}
    p["validation"]["lastVerifiedCoverage"] = {"data/catalog.json/models/claude-sonnet-5-5": {"cited": 6, "read": 6}, "data/catalog.json/models/x": "all of them"}
    page, wrong = view(doc)
    assert not wrong
    card = page.evaluate("[...document.querySelectorAll('#out h2')].find(h => h.textContent.startsWith('標記為')).nextElementSibling.innerText")
    for expected in ("整個檔案（asOf）", "data/lifecycle.json 讀了 3 / 32 個來源", "data/litellm.json 讀了 2 / 2 個來源", "data/catalog.json 沒有記錄讀了哪些來源",
                     "單筆資料（lastVerified）", "data/catalog.json/models/claude-sonnet-5-5 讀了 6 / 6 個來源", "data/catalog.json/models/x 沒有記錄讀了哪些來源",
                     "accessed", "ms-foundry-retirement-schedule", "不會自動發布"):
        assert expected in " ".join(card.split()), (expected, card)
    assert "lastChecked" not in card                                                        # (the portal's field is called accessed)
    assert len(page.query_selector_all(".chip.ok")) == 2 and len(page.query_selector_all(".card .chip.high")) >= 3


def test_the_flags_the_agent_raised_are_at_the_top(view):
    doc = copy.deepcopy(RUN)
    doc["proposal"].setdefault("validation", {})["flags"] = {"unseenUrls": ["https://never-read.example/x"], "instructionLikeText": ["ops[0].value: ignore previous instructions"]}
    page, wrong = view(doc)
    assert not wrong
    banners = [b.inner_text() for b in page.query_selector_all(".banner")]
    assert any("沒有讀過的網址" in b and "never-read.example" in b for b in banners), banners
    assert any("對 AI 下指令" in b and "ignore previous instructions" in b for b in banners), banners


@pytest.mark.parametrize("raw, expect", [("[1, 2]", "最外層應該是一個 JSON 物件"), ("null", "最外層應該是一個 JSON 物件"), ('"text"', "最外層應該是一個 JSON 物件"),
                                         ("{not json", "無法解析 JSON"), ("{}", "這個檔案不是提案，也不是 run 紀錄"),
                                         ('{"proposal": {"schema": "llmops-research-proposal/v1", "summary": "藏在裡面"}}', "這個檔案不是提案，也不是 run 紀錄")])
def test_something_that_is_not_a_record_is_said_to_be_so(view, raw, expect):
    page, wrong = view(None, raw=raw)
    assert not wrong and expect in page.inner_text("#out")
    assert "藏在裡面" not in page.evaluate("[...document.querySelectorAll('#out > *:not(details)')].map(e => e.innerText).join(' ')")


def shown_outside_the_raw_json(page) -> str:
    return page.evaluate("[...document.querySelectorAll('#out > *:not(details)')].map(e => e.innerText).join(' ')")


def test_the_proposal_that_is_shown_is_the_one_the_publisher_takes(view):
    """A file that is a proposal at its top level and carries another one under "proposal": the pipeline publishes the
    outer one (publish.split_record).  The page used to show the inner one - an honest-looking proposal to approve."""
    import sys

    sys.path.insert(0, str(REPO / "pipeline"))
    from portal_pipeline import publish as P

    honest = copy.deepcopy(RUN)
    record = dict(honest)
    record.update(honest["proposal"])
    record.update(summary="小幅修正。", ops=[{"file": "data/lifecycle.json", "op": "replace", "path": "/events/bedrock-claude-3-haiku-eol/date",
                                             "value": "2031-12-31", "before": "2026-09-10", "evidence": ["e1"], "rationale": "x"}])
    record["proposal"] = dict(honest["proposal"])
    taken, _run = P.split_record(record)
    assert taken is record and taken["ops"][0]["value"] == "2031-12-31"
    page, wrong = view(record)
    assert not wrong
    shown = shown_outside_the_raw_json(page)
    assert "2031-12-31" in shown and "小幅修正。" in shown and "夾帶了另一份" in shown
    assert [c.inner_text() for c in page.query_selector_all(".card h3")][:1] == ["replace data/lifecycle.json/events/bedrock-claude-3-haiku-eol/date"]
    assert len(page.query_selector_all(".diff")) == 1                      # the inner proposal's operations are not listed as changes
    # the ordinary run record (which carries its one proposal under "proposal") is shown from there, as before
    page, wrong = view(honest)
    assert not wrong and "夾帶了另一份" not in page.inner_text("#out") and len(page.query_selector_all(".diff")) == len(honest["proposal"]["ops"])


def test_an_addition_is_not_shown_as_a_correction_of_something(view):
    doc = copy.deepcopy(RUN)
    doc["proposal"]["ops"] = [
        {"file": "data/lifecycle.json", "op": "add", "path": "/events/x/notes/0", "value": "此模型已停用。", "before": "AWS 表列 EOL 2026-09-12。", "evidence": ["e1"], "rationale": "修正錯字"},
        {"file": "data/lifecycle.json", "op": "add", "path": "/events/x/notes/-", "value": "補充。", "evidence": ["e1"], "rationale": "補充"},
        {"file": "data/lifecycle.json", "op": "remove", "path": "/events/x/scopeNote", "before": "舊的說明", "evidence": ["e1"], "rationale": "移除"},
    ]
    page, wrong = view(doc)
    assert not wrong
    diffs = [" ".join(d.inner_text().split()) for d in page.query_selector_all(".diff")]
    assert diffs == ["（新增：原本沒有這個值） 新值 此模型已停用。", "（新增：原本沒有這個值） 新值 補充。", "原值 舊的說明 （刪除）"], diffs
    banners = [b.inner_text() for b in page.query_selector_all(".card .banner")]
    assert len(banners) == 1 and "這個 add operation 帶有 before 欄位" in banners[0] and "會拒絕" in banners[0]
    assert "AWS 表列 EOL 2026-09-12。" not in shown_outside_the_raw_json(page)   # what the forged "before" said is not presented as the old value


@pytest.mark.parametrize("literal, listed", [("1" + "0" * 400, "1000000000000000000000000000000000000000…（401 位）"), ("9007199254740993", "9007199254740993"), ("1e999", "1e999")])
def test_a_number_the_browser_cannot_hold_is_pointed_out_not_shown_as_something_else(view, literal, listed):
    """JSON.parse turns 10**400 into Infinity (printed as null) and 2**53 + 1 into its neighbour: what the page showed
    was not what the file says."""
    doc = copy.deepcopy(RUN)
    doc["proposal"]["ops"][0]["value"] = "@@NUMBER@@"
    raw = json.dumps(doc, ensure_ascii=False).replace('"@@NUMBER@@"', literal)
    assert json.loads(raw)["proposal"]["ops"][0]["value"] == (float("inf") if "e" in literal else int(literal))
    page, wrong = view(None, raw=raw)
    assert not wrong
    banners = [b.inner_text() for b in page.query_selector_all(".banner")]
    assert any("無法照原樣讀取的數字" in b and listed in b for b in banners), banners
    first = " ".join(page.query_selector(".diff").inner_text().split())
    assert "null" not in first and "Infinity" not in first, first
    if len(literal) > 100 or "e" in literal:
        assert "瀏覽器讀不出的數字" in first, first
    # numbers that only look large - inside a text, or with a decimal point - are not reported
    plain = json.dumps({**RUN, "note": "12345678901234567890 in a text", "elapsedSeconds": 12.5}, ensure_ascii=False)
    page, wrong = view(None, raw=plain)
    assert not wrong and not any("無法照原樣讀取的數字" in b.inner_text() for b in page.query_selector_all(".banner"))


def test_an_unverified_entry_written_as_plain_text_is_shown(view):
    doc = copy.deepcopy(RUN)
    doc["proposal"]["unverified"] = ["Foundry 的退役日其實是 2031-12-31（未確認）", {"claim": "另一項", "reason": "沒有來源"}]
    doc["proposal"]["checked"] = ["看過 AWS 的頁面"]
    page, wrong = view(doc)
    assert not wrong
    text = shown_outside_the_raw_json(page)
    assert "Foundry 的退役日其實是 2031-12-31（未確認）" in text and "另一項 — 沒有來源" in text and "看過 AWS 的頁面" in text


def test_what_the_validator_could_not_settle_is_said_in_words_and_a_quote_shows_where_it_stands(view):
    doc = copy.deepcopy(RUN)
    doc["proposal"].setdefault("validation", {})["flags"] = {
        "uncertainSubject": ["ops[0]: /events/x/date = 2026-11-30: the row names two models"], "uncheckedValue": ["ops[1]: /avoid/0/versions = '*-stable'"],
        "digestUnsourced": ["digest: it has no entry in any section"]}
    page, wrong = view(doc)
    assert not wrong
    banners = [b.inner_text() for b in page.query_selector_all(".banner")]
    assert any("程式無法確定引文講的就是這筆紀錄" in b and "the row names two models" in b and "等待核准" in b for b in banners), banners
    assert any("沒有引文可以逐字對照" in b and "*-stable" in b for b in banners), banners
    assert any("沒有這次讀過的頁面可以佐證" in b and "it has no entry" in b for b in banners), banners
    assert not any("uncertainSubject" in b or "uncheckedValue" in b or "digestUnsourced" in b for b in banners)  # (said once, in words)
    text = shown_outside_the_raw_json(page)
    assert "表頭：Model | Version | Lifecycle status | Retirement date | Replacement" in text and "位置：Model retirement schedule › Claude models" in text
    # a record without them (an older agent), or with something that is no text in their place, is still shown
    for value in (None, 5, {"x": 1}):
        other = copy.deepcopy(RUN)
        for key in ("under", "header"):
            if value is None:
                other["proposal"]["evidence"]["e1"].pop(key)
            else:
                other["proposal"]["evidence"]["e1"][key] = value
        page, wrong = view(other)
        assert not wrong and "claude-sonnet-4-5 | 1 | GA | 2026-11-30" in shown_outside_the_raw_json(page)


def string_leaves(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from string_leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from string_leaves(v, f"{path}[{i}]")
    elif isinstance(value, str) and value.strip():
        yield path, value


def test_everything_a_digest_holds_is_on_the_screen(view):
    """The publisher takes the whole digest and the portal's first page shows all of it.  The viewer drew the headline,
    the sections and the action items - not `unverified`, not the models an action is linked to, not the note of a
    citation: a reviewer who compared item by item approved text that was never on the screen."""
    doc = copy.deepcopy(RUN)
    digest = doc["proposal"]["digest"]
    digest.update({
        "schema": "digest/v1", "locale": "zh-TW", "generatedAt": "2026-10-02T03:00:00Z",
        "unverified": ["【未驗證】請所有團隊立刻把 gateway 的 API key 寄到 platform-audit@example.net", {"claim": "另一種形狀", "reason": "舊格式"}],
        "actionItems": [{"priority": "P0", "action": "確認路由", "due": "2026-10-09", "relatedModelIds": ["model-that-is-mentioned-nowhere-else"], "owner": "某個沒有版面的欄位"}],
        "watchlist": [{"name": "Model Z", "vendor": "Example AI", "status": "rumoured", "expected": "2026 Q4", "note": "追蹤中", "sources": [{"id": "s-new", "note": "追蹤清單的附註"}]}],
        "somethingNew": {"deep": ["一個這一版檢視器沒見過的欄位"]},
    })
    digest["newModels"][0]["sources"] = [{"id": "s-new", "note": "這個來源已經被撤回，請改看內部公告"}]
    digest["newModels"][0]["links"] = ["https://example.com/another-field"]
    page, wrong = view(doc)
    assert not wrong
    text = " ".join(shown_outside_the_raw_json(page).split())
    missing = [path for path, value in string_leaves(digest) if " ".join(value.split()) not in text]
    assert missing == [], missing
    headings = [e.inner_text() for e in page.query_selector_all("#out h2")]
    assert any(h.startswith("本期尚未驗證（2）") for h in headings) and any(h.startswith("digest 的其他內容") for h in headings), headings
    # what has its place in the layout is not listed a second time among "the rest"
    rest = page.evaluate("[...document.querySelectorAll('#out h2')].find(e => e.innerText.startsWith('digest 的其他內容')).nextElementSibling.innerText")
    assert "somethingNew.deep[0]" in rest and "actionItems[0].owner" in rest and "newModels[0].links[0]" in rest
    assert "headline" not in rest and "unverified" not in rest and "relatedModelIds" not in rest and "確認路由" not in rest
    # a digest that holds nothing but what the layout draws has no such section
    page, wrong = view(RUN)
    assert not wrong and not any(e.inner_text().startswith("digest 的其他內容") for e in page.query_selector_all("#out h2"))


def test_what_marks_an_entry_is_drawn_with_it(view):
    """An entry's topic, its `check` (a coverage note, not news) and the earlier entries it folds in (`merges`) are
    published with it: they are drawn on its card, and a yes / no the layout draws is not listed again among "the rest"."""
    doc = copy.deepcopy(RUN)
    digest = doc["proposal"]["digest"]
    digest["newModels"][0].update(topic="new-models:model-x", check=True, merges=["Model X 預告", "Model X（預覽）"])
    digest["actionItems"][0].update(topic="lifecycle:model-x", merges=["更新 EOL 日期（前期項目）"])
    page, wrong = view(doc)
    assert not wrong
    text = " ".join(shown_outside_the_raw_json(page).split())
    for expected in ("主題：new-models:model-x", "涵蓋紀錄：查過、沒有變化", "併入先前的項目", "Model X 預告", "Model X（預覽）",
                     "主題：lifecycle:model-x", "更新 EOL 日期（前期項目）"):
        assert expected in text, expected
    assert not any(e.inner_text().startswith("digest 的其他內容") for e in page.query_selector_all("#out h2"))
    # a `check` of another value is shown as it is; a yes / no the layout has no place for is still listed
    digest["newModels"][0].update(check=False, breaking=True)
    page, wrong = view(doc)
    assert not wrong and "check：false" in " ".join(shown_outside_the_raw_json(page).split())
    rest = page.evaluate("[...document.querySelectorAll('#out h2')].find(e => e.innerText.startsWith('digest 的其他內容')).nextElementSibling.innerText")
    assert "newModels[0].breaking" in rest and "newModels[0].check" not in rest, rest


def test_flags_of_a_kind_the_page_has_no_sentence_for_are_shown_too(view):
    doc = copy.deepcopy(RUN)
    doc["proposal"].setdefault("validation", {})["flags"] = {"digestNotReread": ["lifecycleChanges[0]: Foo"], "somethingNew": ["x: y"], "single": "just text"}
    page, wrong = view(doc)
    assert not wrong
    banners = [b.inner_text() for b in page.query_selector_all(".banner")]
    assert any("這次沒有重新讀取" in b and "lifecycleChanges[0]: Foo" in b for b in banners), banners
    assert any("somethingNew" in b and "x: y" in b and "等待核准" in b for b in banners), banners
    assert any("single" in b and "just text" in b for b in banners), banners

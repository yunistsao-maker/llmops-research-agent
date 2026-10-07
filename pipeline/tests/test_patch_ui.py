"""scripts/patch_ui.py against the real files of the portal UI it was written for (fixtures/web = the originals)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import patch_ui
from conftest import FIXTURES, REPO

ORIGINAL = FIXTURES / "web"
KIT = REPO / "ui" / "files"
EDITED = ["README.md", "scripts/sync-data.mjs", "src/App.tsx", "src/data.ts", "src/main.tsx", "src/pages/Gateway.tsx", "src/pages/Radar.tsx", "src/styles.css"]
NODE = shutil.which("node")


def tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def text(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


@pytest.fixture
def web(tmp_path):
    return Path(shutil.copytree(ORIGINAL, tmp_path / "portal" / "web"))


def run(web: Path, *flags: str) -> int:
    return patch_ui.main([str(web), *flags])


def test_the_fixture_is_what_the_patcher_expects():
    assert sorted(set(e.file for e in patch_ui.EDITS)) == EDITED == patch_ui.EDITED
    assert all((ORIGINAL / rel).is_file() for rel in EDITED)
    assert all((KIT / rel).is_file() for rel in patch_ui.NEW_FILES + [patch_ui.CONFIG])


def test_check_reports_what_is_pending_and_writes_nothing(web, capsys):
    before = tree(web)
    assert run(web, "--check") == patch_ui.PENDING
    out = capsys.readouterr().out
    assert tree(web) == before
    assert out.count("[待套用]") == len(patch_ui.EDITS) + len(patch_ui.NEW_FILES) + 1
    assert "還有待套用的修改" in out


def test_apply_changes_only_the_data_layer(web, capsys):
    before = tree(web)
    assert run(web) == patch_ui.OK
    out = capsys.readouterr().out
    assert "✔ 完成：修改 8 個檔案、新增 / 更新 4 個檔案" in out
    after = tree(web)
    changed = sorted(rel for rel in before if after[rel] != before[rel])
    added = sorted(rel for rel in after if rel not in before)
    assert changed == EDITED
    assert added == sorted(["public/config.json", "src/DataStatus.tsx", "src/ErrorBoundary.tsx", "src/dataSource.ts", ".patch_ui/state.json"]
                           + [f".patch_ui/orig/{rel}" for rel in EDITED])
    # the originals are kept byte for byte, outside src/
    for rel in EDITED:
        assert (web / ".patch_ui" / "orig" / rel).read_bytes() == (ORIGINAL / rel).read_bytes()
    assert not list(web.rglob("*.orig"))
    for rel in patch_ui.NEW_FILES:
        assert (web / rel).read_bytes() == (KIT / rel).read_bytes()
    assert json.loads(text(web, "public/config.json")) == {}

    data = text(web, "src/data.ts")
    assert data.startswith('import { fetchJson, loadManifest } from "./dataSource";\nimport { createContext')
    assert "const getJson = fetchJson;" in data and "await fetch(path)" not in data
    assert "manifest: () => loadManifest() as Promise<Manifest>," in data and '"data/manifest.json"' not in data
    assert 'catalog: () => cached<CatalogFile>("data/catalog.json"),' in data  # every other loader is untouched
    assert data.count("\n") == text(ORIGINAL, "src/data.ts").count("\n") - 2  # +1 import, -5 getJson, +2 its replacement

    gateway = text(web, "src/pages/Gateway.tsx")
    assert gateway.startswith('import { fetchText } from "../dataSource";\n')
    assert 'const cfg = useData(() => fetchText("config/litellm.example.yaml"));' in gateway
    assert 'fetch("config/' not in gateway

    sync = text(web, "scripts/sync-data.mjs")
    assert sync.startswith("// Copy ../data and ../runs into public/")  # the file's own header stays on top
    assert sync.index("--- remote data (added by") < sync.index("import { cpSync")
    assert sync.count("process.exit(0)") == 1

    app = text(web, "src/App.tsx")
    assert app.startswith('import ErrorBoundary from "./ErrorBoundary";\nimport DataStatus from "./DataStatus";\n')
    # one page that cannot be drawn must not take the navigation with it; resetKey clears the error on navigation
    # (a `key` would rebuild the page - and drop the keyboard focus - whenever the address changes)
    assert '<main id="main-content" tabIndex={-1}><ErrorBoundary resetKey={parts.join("/")}>{page}</ErrorBoundary></main>' in app
    assert "<ErrorBoundary key=" not in app
    main = text(web, "src/main.tsx")
    assert "render(<StrictMode><ErrorBoundary><App /></ErrorBoundary></StrictMode>)" in main and main.startswith('import ErrorBoundary from "./ErrorBoundary";\n')
    radar = text(web, "src/pages/Radar.tsx")
    assert "manifest.data ? Promise.reject(\"no digest\") : new Promise<never>(() => undefined)" in radar and "[id, manifest.data]" in radar
    assert "請確認 build 時有 config/ 資料夾" not in text(web, "src/pages/Gateway.tsx")
    assert '<footer className="foot"><DataStatus /> 社群與未驗證資訊以紅字標示，使用前請核對來源。</footer>' in app
    assert "經平台團隊審核" not in app  # automatic updates are not reviewed line by line, so the page must not say so

    assert text(web, "src/styles.css").startswith(text(ORIGINAL, "src/styles.css").rstrip("\n"))
    assert ".data-status-reload{" in text(web, "src/styles.css") and ".data-status-notice{position:fixed" in text(web, "src/styles.css")
    assert "## 資料來源：bundled 或 remote" in text(web, "README.md")


def test_applying_twice_changes_nothing_more(web, capsys):
    assert run(web) == patch_ui.OK
    first = tree(web)
    capsys.readouterr()
    assert run(web) == patch_ui.OK
    out = capsys.readouterr().out
    assert tree(web) == first
    assert out.count("[已套用]") == len(patch_ui.EDITS) and "[套用]" not in out
    assert "✔ 完成：修改 0 個檔案、新增 / 更新 0 個檔案" in out
    assert run(web, "--check") == patch_ui.OK
    assert "全部已套用。" in capsys.readouterr().out


def test_revert_restores_the_folder(web, capsys):
    before = tree(web)
    assert run(web) == patch_ui.OK
    assert run(web, "--revert") == patch_ui.OK
    out = capsys.readouterr().out
    after = tree(web)
    assert {rel: h for rel, h in after.items() if rel != "public/config.json"} == before  # byte for byte
    assert "public/config.json" in after  # the user's setting is never removed
    assert out.count("[撤回]") == len(patch_ui.EDITS) and out.count("[刪除]") == 3
    assert run(web, "--check") == patch_ui.PENDING  # and it can be applied again
    assert run(web) == patch_ui.OK


def test_revert_undoes_the_edits_and_keeps_everything_else_that_changed_since(web, capsys):
    """It used to put the old file back whole - and with it every change made to that file after patching was gone."""
    assert run(web) == patch_ui.OK
    edited = text(web, "src/App.tsx").replace("LLM 平台", "LLM Platform").replace('["methods", "方法與來源"],', '["methods", "方法與來源"], ["faq", "常見問題"],')
    (web / "src/App.tsx").write_text(edited, encoding="utf-8")
    styles = text(web, "src/styles.css") + ".faq{margin:0}\n"
    (web / "src/styles.css").write_text(styles, encoding="utf-8")
    assert run(web, "--revert") == patch_ui.OK
    app = text(web, "src/App.tsx")
    assert "LLM Platform" in app and '["faq", "常見問題"]' in app  # the person's changes
    assert "ErrorBoundary" not in app and "DataStatus" not in app and "經平台團隊審核" in app  # ours are gone
    assert app == text(ORIGINAL, "src/App.tsx").replace("LLM 平台", "LLM Platform").replace('["methods", "方法與來源"],', '["methods", "方法與來源"], ["faq", "常見問題"],')
    assert text(web, "src/styles.css") == text(ORIGINAL, "src/styles.css") + ".faq{margin:0}\n"
    assert not (web / ".patch_ui").exists() and not (web / "src/dataSource.ts").exists()


def test_a_backup_from_an_earlier_round_cannot_come_back(web):
    """Apply; restore the files with git (the .patch_ui folder stays); change App.tsx; apply again; revert.
    The revert used to restore the snapshot of the first round, silently dropping what was added in between."""
    assert run(web) == patch_ui.OK
    for rel in EDITED:  # git checkout -- .
        (web / rel).write_bytes((ORIGINAL / rel).read_bytes())
    mine = text(web, "src/App.tsx").replace('["methods", "方法與來源"],', '["methods", "方法與來源"], ["new", "新頁面"],')
    (web / "src/App.tsx").write_text(mine, encoding="utf-8")
    assert run(web) == patch_ui.OK
    assert (web / ".patch_ui/orig/src/App.tsx").read_text(encoding="utf-8") == mine  # the reference copy is of this round
    assert run(web, "--revert") == patch_ui.OK
    assert text(web, "src/App.tsx") == mine


def test_an_edit_applied_in_a_later_round_is_undone_without_touching_what_was_written_in_between(web):
    custom_footer = text(web, "src/App.tsx").replace("（每週排程更新、經平台團隊審核）", "（每週更新）")
    (web / "src/App.tsx").write_text(custom_footer, encoding="utf-8")
    assert run(web) == patch_ui.OK  # the footer is not the original one: left alone
    assert "<DataStatus" not in text(web, "src/App.tsx")
    restored = text(web, "src/App.tsx").replace("（每週更新）", "（每週排程更新、經平台團隊審核）").replace("LLM 平台", "我們的 LLM 平台")
    (web / "src/App.tsx").write_text(restored, encoding="utf-8")
    assert run(web) == patch_ui.OK and "<DataStatus />" in text(web, "src/App.tsx")
    assert run(web, "--revert") == patch_ui.OK
    assert text(web, "src/App.tsx") == text(ORIGINAL, "src/App.tsx").replace("LLM 平台", "我們的 LLM 平台")


def test_revert_writes_nothing_when_one_edit_cannot_be_undone(web, capsys):
    """Half a revert is a folder that does not build: the kit files gone, the imports still there."""
    assert run(web) == patch_ui.OK
    changed = text(web, "src/data.ts").replace("const getJson = fetchJson;", "const getJson = <T,>(path: string) => fetchJson<T>(path);")
    (web / "src/data.ts").write_text(changed, encoding="utf-8")
    before = tree(web)
    capsys.readouterr()
    assert run(web, "--revert") == patch_ui.ENV
    out = capsys.readouterr().out
    assert "沒有寫入任何檔案" in out and "src/data.ts（getJson 改走資料位置）" in out and "async function getJson<T>(path: string)" in out
    assert tree(web) == before and all((web / rel).is_file() for rel in patch_ui.NEW_FILES)


@pytest.mark.parametrize("damage", ["delete", "truncate", "wrong-shape", "no-entry", "folder-gone"])
def test_without_its_record_a_revert_refuses_rather_than_guesses(web, capsys, damage):
    """A fresh clone whose .patch_ui/ was git-ignored, a state file somebody emptied: what stood there before is not known."""
    assert run(web) == patch_ui.OK
    state = web / ".patch_ui/state.json"
    if damage == "delete":
        state.unlink()
    elif damage == "truncate":
        state.write_text("{", encoding="utf-8")
    elif damage == "wrong-shape":
        state.write_text('{"files": ["src/App.tsx"]}', encoding="utf-8")
    elif damage == "no-entry":
        doc = json.loads(state.read_text(encoding="utf-8"))
        doc["files"]["src/App.tsx"] = "oops"
        doc["files"]["src/data.ts"] = {"edits": {"getJson 改走資料位置": "oops"}}
        state.write_text(json.dumps(doc), encoding="utf-8")
    else:
        shutil.rmtree(web / ".patch_ui")
    before = tree(web)
    capsys.readouterr()
    assert run(web, "--revert") == patch_ui.ENV
    out = capsys.readouterr().out
    assert "沒有寫入任何檔案" in out and "git checkout -- src/" in out
    assert tree(web) == before
    assert run(web, "--check") == patch_ui.OK and run(web) == patch_ui.OK  # the folder still is a patched, working one


def test_a_customised_kit_file_survives_a_revert(web, capsys):
    assert run(web) == patch_ui.OK
    (web / "src/DataStatus.tsx").write_text(text(web, "src/DataStatus.tsx").replace("資料版本", "版本"), encoding="utf-8")
    capsys.readouterr()
    assert run(web, "--revert") == patch_ui.OK
    out = capsys.readouterr().out
    assert "[保留] src/DataStatus.tsx" in out and (web / "src/DataStatus.tsx").is_file()
    assert not (web / "src/dataSource.ts").exists() and text(web, "src/App.tsx") == text(ORIGINAL, "src/App.tsx")


def test_a_data_location_that_is_already_configured_is_kept(web):
    (web / "public").mkdir()
    (web / "public/config.json").write_text('{"dataBaseUrl": "https://data.example/t/"}', encoding="utf-8")
    assert run(web) == patch_ui.OK
    assert json.loads(text(web, "public/config.json")) == {"dataBaseUrl": "https://data.example/t/"}
    assert run(web, "--revert") == patch_ui.OK
    assert json.loads(text(web, "public/config.json")) == {"dataBaseUrl": "https://data.example/t/"}


def test_a_customised_kit_file_is_not_overwritten_but_an_untouched_one_is_updated(web, capsys, tmp_path, monkeypatch):
    assert run(web) == patch_ui.OK
    mine = text(web, "src/DataStatus.tsx").replace("資料版本", "資料版次")
    (web / "src/DataStatus.tsx").write_text(mine, encoding="utf-8")
    # a newer kit: both files changed upstream
    kit = Path(shutil.copytree(KIT, tmp_path / "kit"))
    for rel in ("src/dataSource.ts", "src/DataStatus.tsx"):
        (kit / rel).write_text(text(kit, rel) + "// newer\n", encoding="utf-8")
    monkeypatch.setattr(patch_ui, "FILES", kit)
    capsys.readouterr()
    assert run(web, "--check") == patch_ui.PENDING
    assert run(web) == patch_ui.OK
    out = capsys.readouterr().out
    assert "[保留] src/DataStatus.tsx" in out and "[套用] src/dataSource.ts：更新" in out
    assert text(web, "src/DataStatus.tsx") == mine
    assert text(web, "src/dataSource.ts").endswith("// newer\n")
    assert run(web, "--check") == patch_ui.OK


@pytest.mark.parametrize("rel, old, new, expect", [
    ("src/data.ts", "async function getJson<T>(path: string): Promise<T> {", "async function getJson<T>(path: string, init?: RequestInit): Promise<T> {",
     "把 getJson 函式整個換成"),
    ("src/data.ts", 'manifest: () => cached<Manifest>("data/manifest.json"),', 'manifest: () => cached<Manifest>(INDEX),', "loaders 裡的 manifest 改成"),
    ("src/pages/Gateway.tsx", 'fetch("config/litellm.example.yaml")', "fetch(CONFIG_EXAMPLE)", "換成：fetchText"),
])
def test_a_required_change_that_cannot_be_placed_stops_everything(web, capsys, rel, old, new, expect):
    source = text(web, rel)
    assert old in source
    (web / rel).write_text(source.replace(old, new), encoding="utf-8")
    before = tree(web)
    for flags in (["--check"], []):
        assert run(web, *flags) == patch_ui.ENV
        out = capsys.readouterr().out
        assert "沒有寫入任何檔案" in out and expect in out and "[找不到對應位置]" in out
        assert tree(web) == before


def test_a_missing_required_file_stops_everything(web, capsys):
    (web / "src/pages/Gateway.tsx").unlink()
    before = tree(web)
    assert run(web) == patch_ui.ENV
    assert "[沒有這個檔案] src/pages/Gateway.tsx" in capsys.readouterr().out
    assert tree(web) == before


def test_an_optional_change_that_cannot_be_placed_is_skipped_with_instructions(web, capsys):
    app = text(web, "src/App.tsx").replace('<footer className="foot">', '<footer className="site-foot">').replace("{page}</main>", "{page} </main>")
    (web / "src/App.tsx").write_text(app, encoding="utf-8")
    (web / "README.md").unlink()
    assert run(web) == patch_ui.OK
    out = capsys.readouterr().out
    assert "略過（選用）src/App.tsx：頁尾改成資料版本列" in out and 'import DataStatus from "./DataStatus";' in out
    assert "略過（選用）src/App.tsx：頁面包一層 ErrorBoundary" in out and "<ErrorBoundary resetKey=" in out
    assert text(web, "src/App.tsx") == app
    assert "const getJson = fetchJson;" in text(web, "src/data.ts")
    assert not (web / "README.md").exists()


def test_the_two_optional_changes_to_the_page_frame_are_independent(web):
    app = text(web, "src/App.tsx").replace('<footer className="foot">', '<footer className="site-foot">')
    (web / "src/App.tsx").write_text(app, encoding="utf-8")
    assert run(web) == patch_ui.OK
    patched = text(web, "src/App.tsx")
    assert "<ErrorBoundary resetKey=" in patched and "<DataStatus />" not in patched and 'import DataStatus' not in patched


@pytest.mark.parametrize("line", [
    'const extra = fetch("data/extra.json");',
    "const run = await fetch(`runs/perf/${name}`);",
    "fetch('./config/other.yaml')",
    'const a = await fetch("/data/extra.json");',
    "const b = await fetch(BASE + name);",
    "const c = await fetch(`${import.meta.env.BASE_URL}data/x.json`);",
    "const d = await fetch (path);",
    'return <a href="config/litellm.example.yaml">設定範例</a>;',
    "return <img src={`/runs/perf/${id}.png`} />;",
    # the same read, spelled another way: `--check` said "全部已套用。" for each of these
    'const extra = window.fetch("data/catalog.json").then((r) => r.json());',
    "const extra = globalThis.fetch(path);",
    "const extra = self?.fetch(path);",
    'const extra = window["fetch"](path);',
    "const get = fetch; const extra = get(path);",
    "const { fetch: get } = window;",
    "const rows = useData(fetch);",
    "const extra = fetch\n    (path).then((r) => r.json());",
    'const extra = new XMLHttpRequest(); extra.open("GET", path); extra.send();',
    "const feed = new EventSource(path);",
    'import fetch from "cross-fetch";',
    # ... a data file compiled into the bundle, or named where no loader gets it
    'import bundled from "../../../data/catalog.json";',
    'const bundled = await import("../data/catalog.json");',
    'const where = new URL("../data/catalog.json", import.meta.url);',
    'const rows = await client.get("data/catalog.json");',
    'const path = "runs/perf/latest.json";',
    "const path = `data/models/${id}.json`;",
    'const cfg = await load({ path: "config/litellm.example.yaml" });',
])
def test_any_other_way_of_reading_a_data_file_is_reported_and_nothing_is_written(web, capsys, line):
    """After the patch the app's own code has no reason to read anything by itself, whatever the read looks like."""
    (web / "src/pages/Extra.tsx").write_text(f"export default function Extra() {{\n  {line}\n  return null;\n}}\n", encoding="utf-8")
    before = tree(web)
    for flags in ([], ["--check"]):
        assert run(web, *flags) == patch_ui.ENV
        out = capsys.readouterr().out
        assert "src/pages/Extra.tsx:2:" in out and "沒有寫入任何檔案" in out and "patch_ui: ok" in out
        assert tree(web) == before


def test_a_second_helper_that_fetches_is_caught_even_inside_the_data_layer(web, capsys):
    extra = text(web, "src/data.ts") + "\nexport async function raw(path: string) { return (await fetch(path)).text(); }\n"
    (web / "src/data.ts").write_text(extra, encoding="utf-8")
    assert run(web) == patch_ui.ENV and "src/data.ts:" in capsys.readouterr().out


@pytest.mark.parametrize("line", [
    'const status = await fetch("https://status.example.com/api"); // patch_ui: ok',
    "const again = refetch(); const rows = await fetchJson<Row[]>(path);",
    "const text = 'how to fetch data';",
    "api.fetch(query);",
    # a data file that goes through a loader - also when the path is chosen first
    'const rows = await fetchJson<Row[]>("data/extra.json");',
    'const cfg = useData(() => fetchText("config/other.yaml"));',
    'const run = cached<PerfRun>(flag ? `runs/perf/${a}.json` : "runs/perf/default.json");',
    'const href = await dataUrl("config/litellm.example.yaml");',
    # text that only mentions a folder, a property and words that contain the name
    'return <PageHead eyebrow="runs/compat 與 runs/perf（由 tools/ 產生）" title="測試結果" />;',
    # ... or a text a tag shows (the Release page's own example of what to type)
    'return <label style={{ gap: 4 }}>證據<input type="text" value={s.evidence} onChange={(e) => put(g.id, { evidence: e.target.value })} '
    'placeholder="例：runs/compat/2026-10-01-opus55.json" /></label>;',
    'return <Hint text={"data/catalog.json"} />;',
    "const client = { fetch: load, retries: 1 }; const kinds = ['prefetch', 'fetch-policy'];",
    "type Loader = typeof fetchJson; const meta = 'metadata/x.json';",
    'import bundled from "../../../data/catalog.json"; // patch_ui: ok',
])
def test_what_is_not_a_data_read_is_left_alone(web, line):
    (web / "src/pages/Extra.tsx").write_text(f"export default function Extra() {{\n  {line}\n  return null;\n}}\n", encoding="utf-8")
    assert run(web) == patch_ui.OK


def test_backups_and_other_non_source_files_are_not_mistaken_for_direct_reads(web):
    (web / "src/pages/Gateway.tsx.bak").write_bytes((ORIGINAL / "src/pages/Gateway.tsx").read_bytes())
    (web / "src/notes.md").write_text('fetch("data/catalog.json")\n', encoding="utf-8")
    assert run(web) == patch_ui.OK
    assert run(web) == patch_ui.OK


def test_windows_line_endings_are_kept(web):
    for rel in EDITED:
        (web / rel).write_bytes((ORIGINAL / rel).read_bytes().replace(b"\n", b"\r\n"))
    assert run(web) == patch_ui.OK
    for rel in EDITED:
        data = (web / rel).read_bytes()
        assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b""), rel
    assert "const getJson = fetchJson;\r\n" in (web / "src/data.ts").read_bytes().decode("utf-8")
    assert run(web, "--check") == patch_ui.OK
    assert run(web, "--revert") == patch_ui.OK
    for rel in EDITED:
        assert (web / rel).read_bytes() == (ORIGINAL / rel).read_bytes().replace(b"\n", b"\r\n"), rel


def test_lines_the_patch_does_not_touch_keep_their_own_line_ends(web):
    """One stray \\r\\n in a file used to turn every line of it into \\r\\n."""
    original = (ORIGINAL / "src/styles.css").read_bytes()
    first, rest = original.split(b"\n", 1)
    mixed = first + b"\r\n" + rest
    (web / "src/styles.css").write_bytes(mixed)
    assert run(web) == patch_ui.OK
    patched = (web / "src/styles.css").read_bytes()
    assert patched.startswith(mixed) and patched.count(b"\r\n") == 1
    assert run(web, "--revert") == patch_ui.OK and (web / "src/styles.css").read_bytes() == mixed


def test_kit_files_checked_out_with_windows_line_ends_are_still_recognised(web, capsys):
    assert run(web) == patch_ui.OK
    for rel in patch_ui.NEW_FILES:
        (web / rel).write_bytes((web / rel).read_bytes().replace(b"\n", b"\r\n"))
    capsys.readouterr()
    assert run(web, "--check") == patch_ui.OK and "[保留]" not in capsys.readouterr().out
    assert run(web, "--revert") == patch_ui.OK and not any((web / rel).exists() for rel in patch_ui.NEW_FILES)


def test_an_import_someone_wrote_differently_counts_and_one_that_lacks_a_name_is_reported(web, capsys):
    assert run(web) == patch_ui.OK
    data = text(web, "src/data.ts").replace('import { fetchJson, loadManifest } from "./dataSource";', 'import {\n  dataUrl, fetchJson,\n  loadManifest,\n} from "./dataSource";')
    (web / "src/data.ts").write_text(data, encoding="utf-8")
    capsys.readouterr()
    assert run(web) == patch_ui.OK and text(web, "src/data.ts") == data  # nothing added twice
    (web / "src/data.ts").write_text(data.replace("dataUrl, fetchJson,", "dataUrl,"), encoding="utf-8")
    assert run(web) == patch_ui.ENV  # fetchJson is used and no longer imported: say so instead of adding a second import of the module
    assert "併進既有的 import" in capsys.readouterr().out


def test_a_footer_that_is_not_the_original_one_is_left_alone(web, capsys):
    """It used to be replaced whatever it said - custom links and all."""
    app = text(web, "src/App.tsx").replace("使用前請核對來源。</footer>", '使用前請核對來源。<a href="#/methods">方法</a></footer>')
    (web / "src/App.tsx").write_text(app, encoding="utf-8")
    assert run(web) == patch_ui.OK
    out = capsys.readouterr().out
    assert "略過（選用）src/App.tsx：頁尾改成資料版本列" in out and '<a href="#/methods">方法</a></footer>' in text(web, "src/App.tsx")
    assert "<DataStatus" not in text(web, "src/App.tsx")
    # once a person has placed the component themselves (in whatever form), nothing is added on top of it
    placed = text(web, "src/App.tsx").replace("使用前請核對來源。<a", "使用前請核對來源。<DataStatus compact /><a")
    (web / "src/App.tsx").write_text('import DataStatus from "./DataStatus";\n' + placed, encoding="utf-8")
    before = text(web, "src/App.tsx")
    assert run(web) == patch_ui.OK and text(web, "src/App.tsx") == before


def test_two_candidates_for_one_edit_mean_it_is_not_applied(web, capsys):
    app = text(web, "src/App.tsx")
    footer = patch_ui.STOCK_FOOTER
    (web / "src/App.tsx").write_text(app.replace(footer, footer + "\n      " + footer), encoding="utf-8")
    assert run(web) == patch_ui.OK
    assert "略過（選用）src/App.tsx：頁尾改成資料版本列" in capsys.readouterr().out and "<DataStatus" not in text(web, "src/App.tsx")


def test_an_import_is_not_put_into_a_comment_or_above_a_shebang(web):
    sync = text(web, "scripts/sync-data.mjs")
    commented = "#!/usr/bin/env node\n/*\nimport { old } from \"./old.mjs\";\n*/\n" + sync
    (web / "scripts/sync-data.mjs").write_text(commented, encoding="utf-8")
    assert run(web) == patch_ui.OK
    patched = text(web, "scripts/sync-data.mjs")
    assert patched.startswith('#!/usr/bin/env node\n/*\nimport { old } from "./old.mjs";\n*/\n// Copy ../data')
    assert patched.index("--- remote data (added by") > patched.index("*/") and patched.index("--- remote data (added by") < patched.index("import { cpSync")
    only_shebang = web / "scripts/tool.mjs"
    only_shebang.write_text("#!/usr/bin/env node\nconsole.log(1);\n", encoding="utf-8")
    assert patch_ui._insert_point(only_shebang.read_text(encoding="utf-8")) == len("#!/usr/bin/env node\n")


def test_a_file_that_cannot_be_written_stops_everything_before_anything_is_written(web, capsys):
    target = web / "src/styles.css"
    target.chmod(0o444)
    before = tree(web)
    try:
        if os.access(target, os.W_OK):
            pytest.skip("running as a user who can write read-only files")
        assert run(web) == patch_ui.ENV
        assert "不能寫入" in capsys.readouterr().err and tree(web) == before
    finally:
        target.chmod(0o644)


def test_an_interrupted_run_can_be_continued_or_undone(web, monkeypatch, capsys):
    """The write of the fifth file fails: the four before it are complete and recorded, nothing is half-written."""
    real, calls = patch_ui.write, {"n": 0}

    def flaky(path, data):
        if path.suffix != ".json" and BACKUP_DIR not in path.parts:
            calls["n"] += 1
            if calls["n"] == 5:
                raise OSError(28, "No space left on device")
        return real(path, data)

    BACKUP_DIR = ".patch_ui"
    monkeypatch.setattr(patch_ui, "write", flaky)
    assert run(web) == patch_ui.ENV
    assert "再執行一次即可接著套用" in capsys.readouterr().err
    monkeypatch.setattr(patch_ui, "write", real)
    assert not list(web.rglob(".patch_ui-*"))  # no temporary file left behind
    assert run(web) == patch_ui.OK and run(web, "--check") == patch_ui.OK
    assert run(web, "--revert") == patch_ui.OK
    for rel in EDITED:
        assert (web / rel).read_bytes() == (ORIGINAL / rel).read_bytes(), rel


def test_a_symlinked_source_file_stays_a_link(web, tmp_path):
    real = tmp_path / "shared" / "styles.css"
    real.parent.mkdir()
    real.write_bytes((ORIGINAL / "src/styles.css").read_bytes())
    (web / "src/styles.css").unlink()
    (web / "src/styles.css").symlink_to(real)
    assert run(web) == patch_ui.OK
    assert (web / "src/styles.css").is_symlink() and ".data-status" in real.read_text(encoding="utf-8")


def test_a_folder_that_is_not_the_portal_ui_is_refused(tmp_path, capsys):
    assert run(tmp_path) == patch_ui.ENV
    assert "不像 portal 的 web/ 資料夾" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        patch_ui.main([str(tmp_path), "--check", "--revert"])
    assert e.value.code == 2


# ------------------------------------------------------------------------------- the patched build step, run by node
def node(web: Path, **env: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if k not in ("VITE_DATA_BASE_URL", "BUNDLE_DATA")}
    return subprocess.run([NODE, "scripts/sync-data.mjs"], cwd=web, env={**clean, **env}, capture_output=True, text=True, timeout=60)


@pytest.fixture
def checkout(web):
    """web/ inside a portal checkout with a little data, as the build step expects."""
    root = web.parent
    for rel, body in {"data/catalog.json": "{}", "data/digests/2026-10-05.json": "{}", "data/models/m1.json": "{}",
                      "runs/compat/r1.json": "{}", "config/litellm.example.yaml": "model_list: []\n"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    assert run(web) == patch_ui.OK
    return web


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_without_a_data_location_the_build_step_bundles_the_data_as_before(checkout):
    done = node(checkout)
    assert done.returncode == 0, done.stderr
    assert "synced data + runs" in done.stdout
    index = json.loads(text(checkout, "public/data/manifest.json"))
    assert index["digests"] == ["2026-10-05"] and index["models"] == ["m1"] and index["compatRuns"] == ["r1.json"]
    assert (checkout / "public/config/litellm.example.yaml").is_file()
    assert json.loads(text(checkout, "public/config.json")) == {}


@needs_node
@pytest.mark.parametrize("config, env", [
    ('{"dataBaseUrl": "https://data.example/token/"}', {}),
    ("{}", {"VITE_DATA_BASE_URL": "https://data.example/token/"}),
    (None, {"VITE_DATA_BASE_URL": "https://data.example/token/"}),
    ('{"dataBaseUrl": "/portal-data/"}', {"VITE_DATA_BASE_URL": ""}),
])
def test_with_a_data_location_nothing_is_bundled_and_old_copies_are_removed(checkout, config, env):
    assert node(checkout).returncode == 0  # an earlier bundled build left its copies in public/
    assert (checkout / "public/data/catalog.json").is_file()
    if config is None:
        (checkout / "public/config.json").unlink()
    else:
        (checkout / "public/config.json").write_text(config, encoding="utf-8")
    done = node(checkout, **env)
    assert done.returncode == 0, done.stderr
    assert "nothing is bundled" in done.stdout and "synced data" not in done.stdout
    assert "data.example" not in done.stdout  # the data location can contain the access token: keep it out of build logs
    assert sorted(p.name for p in (checkout / "public").iterdir()) == ([] if config is None else ["config.json"])


@needs_node
@pytest.mark.parametrize("config, env", [
    ('{"dataBaseUrl": "https://data.example/token/"}', {"BUNDLE_DATA": "1"}),
    # the same rule as the page: a dataBaseUrl in config.json wins, and an empty one means "bundled"
    ('{"dataBaseUrl": ""}', {"VITE_DATA_BASE_URL": "https://data.example/token/"}),
    ('{"dataBaseUrl": 42}', {}),
    ("not json", {}),
])
def test_cases_where_the_data_is_still_bundled(checkout, config, env):
    (checkout / "public/config.json").write_text(config, encoding="utf-8")
    done = node(checkout, **env)
    assert done.returncode == 0, done.stderr
    assert "synced data + runs" in done.stdout
    assert (checkout / "public/data/manifest.json").is_file()


@needs_node
@pytest.mark.parametrize("folder", ["my portal", "入口網站", "a%20b"])
def test_the_build_step_reads_its_setting_in_folders_with_awkward_names(tmp_path, folder):
    """`new URL(...).pathname` is percent-encoded: in such a folder config.json was silently not found and the data was bundled."""
    web = Path(shutil.copytree(ORIGINAL, tmp_path / folder / "web"))
    assert run(web) == patch_ui.OK
    (web / "public/data").mkdir(parents=True)
    (web / "public/data/catalog.json").write_text("{}", encoding="utf-8")  # left by an earlier bundled build
    (web / "public/config.json").write_text('{"dataBaseUrl": "https://data.example/token/"}', encoding="utf-8")
    done = node(web)
    assert done.returncode == 0, done.stderr
    assert "nothing is bundled" in done.stdout and not (web / "public/data").exists()


@needs_node
def test_a_data_location_set_only_in_an_env_file_is_pointed_out(checkout):
    """Vite reads .env files, this script does not: the page would read the data location while the build still bundles the data."""
    (checkout / ".env.production").write_text("VITE_DATA_BASE_URL=https://data.example/token/\n", encoding="utf-8")
    done = node(checkout)
    assert done.returncode == 0, done.stderr
    assert "WARNING: VITE_DATA_BASE_URL is set in .env.production" in done.stderr and "data.example" not in done.stdout + done.stderr
    assert "synced data + runs" in done.stdout
    (checkout / "public/config.json").write_text('{"dataBaseUrl": ""}', encoding="utf-8")  # config.json says "bundled" on purpose: no warning
    assert "WARNING" not in node(checkout).stderr

"""The data layer the UI kit installs (ui/files/src/dataSource.ts), run as it is - by node, against answers held in memory.

A page reads ONE release: the pointer names it, and every data file is asked for under that release's folder.  Part of a
file's path can come from the address bar (#/digest/<id> becomes data/digests/<id>.json), so the layer itself has to
keep a path inside the release - whatever the pages hand it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[2] / "ui" / "files" / "src" / "dataSource.ts"
NODE = shutil.which("node")
BASE = "https://data.example/tok-1234567890abcdef/"
LIVE, WITHDRAWN = "20261002T010000Z-5341fc8c", "20261002T033000Z-8ab7fc63"

DRIVER = """
const [kit, base, live, paths] = process.argv.slice(2);
const asked: string[] = [];
globalThis.fetch = ((url: any) => {
  const text = String(url);
  if (text === "config.json") return Promise.resolve(new Response(JSON.stringify(base ? { dataBaseUrl: base } : { dataBaseUrl: "" }), { status: 200 }));
  if (!base) {  // bundled data: everything is asked of the site itself
    if (text === "data/manifest.json") return Promise.resolve(new Response(JSON.stringify({ generatedAt: "", digests: [], models: [], compatRuns: [], perfRuns: [], hasInventory: false }), { status: 200 }));
    asked.push(text);
    return Promise.resolve(new Response("{}", { status: 200 }));
  }
  if (text === base + "manifest.json") {
    return Promise.resolve(new Response(JSON.stringify({ schema: "portal-manifest/v1", releaseId: live, basePath: `releases/${live}/`, generatedAt: "",
                                                         digests: [], models: [], compatRuns: [], perfRuns: [], hasInventory: false }), { status: 200 }));
  }
  asked.push(new URL(text).href);  // as the browser would send it: dot segments resolved
  return Promise.resolve(new Response("{}", { status: 200 }));
}) as any;
(globalThis as any).window = { location: { href: "https://portal.example/", origin: "https://portal.example" } };
const { fetchJson, fetchText, dataUrl } = await import(kit);
const out: any[] = [];
for (const path of JSON.parse(paths)) {
  const row: any = { path, asked: [] as string[] };
  for (const [name, read] of [["fetchJson", fetchJson], ["fetchText", fetchText], ["dataUrl", dataUrl]] as const) {
    const before = asked.length;
    try { await read(path); row[name] = "ok"; } catch (e) { row[name] = String((e as Error).message); }
    row.asked.push(...asked.slice(before));
  }
  out.push(row);
}
console.log(JSON.stringify(out));
"""


def kit(tmp_path: Path, paths: list, base: str = BASE) -> list[dict]:
    driver = tmp_path / "driver.ts"
    driver.write_text(DRIVER, encoding="utf-8")
    done = subprocess.run([NODE, str(driver), str(KIT), base, LIVE, json.dumps(paths)], capture_output=True, text=True, timeout=120)
    if done.returncode != 0 and ("ERR_UNKNOWN_FILE_EXTENSION" in done.stderr or "SyntaxError" in done.stderr):
        pytest.skip("this node cannot run TypeScript files as they are (needs node 22.18 or later)")
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

PLAIN = ["data/catalog.json", "data/digests/2026-10-02.json", "data/models/gpt-6.1-sol@azure.json", "runs/perf/2026-10-01_opus55+cache=on.json",
         "config/litellm.example.yaml"]
#: what a link can put where an id belongs (#/digest/<id>), and other things that are not the name of a data file
NOT_A_NAME = [
    f"data/digests/../../../{WITHDRAWN}/data/digests/2026-10-02.json",   # the digest of a release that was rolled back
    "data/digests/../catalog.json", "data/digests/..%2F..%2Fcatalog.json", "data/digests/%2e%2e/x.json", "../manifest.json", "data/./catalog.json",
    "/data/catalog.json", "//evil.example/data/catalog.json", "https://evil.example/data/catalog.json", "data//catalog.json", "data/catalog.json/",
    "data\\\\catalog.json", "data/digests/..\\\\..\\\\x.json", "data/catalog.json?version=1", "data/catalog.json#top", "data/digests/2026 10 02.json",
    "data/digests/\n.json", "data/catalog.json\n", "data/.hidden.json", ".env", "", "data/digests/" + "9" * 600 + ".json", None, 5, ["data/catalog.json"],
]


@needs_node
def test_a_data_file_is_asked_for_inside_the_release_the_page_reads(tmp_path):
    rows = kit(tmp_path, PLAIN)
    for row in rows:
        want = f"{BASE}releases/{LIVE}/{row['path']}"
        assert (row["fetchJson"], row["fetchText"], row["dataUrl"]) == ("ok", "ok", "ok") and row["asked"] == [want, want], row


@needs_node
def test_what_is_not_the_name_of_a_data_file_is_never_asked_for(tmp_path):
    """`#/digest/..%2F..%2F..%2F<release>%2Fdata%2Fdigests%2F<id>` used to be asked for as it stood: the browser resolved
    the dot segments, and the page - whose footer named the live release - showed the digest of the one rolled back."""
    rows = kit(tmp_path, NOT_A_NAME)
    assert len(rows) == len(NOT_A_NAME)
    for row in rows:
        assert row["asked"] == [], row
        for read in ("fetchJson", "fetchText", "dataUrl"):
            assert row[read] == "沒有這個資料檔（名稱不是資料檔的路徑）", row
        assert BASE not in json.dumps(row["fetchJson"]) and "tok-" not in row["fetchJson"]  # (no message carries the address)


@needs_node
def test_bundled_data_is_asked_for_as_it_always_was(tmp_path):
    """Without a data location there are no releases to keep apart: a path is a path inside the site itself, whatever it
    is called (a results file with a blank in its name was readable before the kit knew about releases, and still is)."""
    rows = kit(tmp_path, ["data/catalog.json", "runs/perf/2026-10-01 run (2).json", "data/digests/../catalog.json"], base="")
    for row in rows:
        assert (row["fetchJson"], row["fetchText"], row["dataUrl"]) == ("ok", "ok", "ok") and row["asked"] == [row["path"], row["path"]], row


def test_the_names_the_page_accepts_are_the_names_a_release_can_hold():
    """One rule on both sides: what the data layer lets through is what the storage layer accepts as a key."""
    import re

    from portal_pipeline import storage

    source = KIT.read_text(encoding="utf-8")
    pattern = re.search(r"const DATA_PATH = /\^(.+)\$/;", source).group(1)
    assert pattern.replace("(?:", "(").replace("\\/", "/") == storage._KEY.pattern
    assert "path.length > 512" in source  # (storage.check_key: the same length)

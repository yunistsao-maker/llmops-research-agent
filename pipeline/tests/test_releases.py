"""The release layout: what goes into a release, what the pointer tells the UI, and what a reader can rely on."""

from __future__ import annotations

import json

import pytest
from conftest import T0, T1, snapshot_tree, write_tree

from portal_pipeline import releases as R
from portal_pipeline.storage import Conflict


# ---------------------------------------------------------------------------------------------- what is publishable
def test_only_publishable_files_of_a_checkout_are_collected(tmp_path):
    files = dict(snapshot_tree())
    files.update({
        "data/manifest.json": b'{"legacy": true}',  # the old build-time index: replaced by the pointer
        "data/.DS_Store": b"junk", "data/notes.docx": b"binary", "runs/compat/node_modules/x.json": b"{}",
        "web/src/data.ts": b"export {}", "README.md": b"# portal", "tools/validate_data.py": b"print()",
        "config/has space.yaml": b"a: 1\n",
    })
    write_tree(tmp_path, files)
    (tmp_path / "data" / "link.json").symlink_to(tmp_path / "data" / "catalog.json")
    collected, skipped = R.collect_tree(tmp_path)
    assert collected == snapshot_tree()
    assert sorted(s.split(" ")[0] for s in skipped) == ["config/has", "data/link.json", "data/notes.docx"]


def test_an_invalid_json_file_stops_the_collection(tmp_path):
    write_tree(tmp_path, {**snapshot_tree(), "data/digests/2026-10-05.json": b'{"id": "2026-10-05",'})
    with pytest.raises(R.ReleaseError, match="data/digests/2026-10-05.json is not valid JSON"):
        R.collect_tree(tmp_path)


def test_a_subset_of_folders_can_be_collected(tmp_path):
    write_tree(tmp_path, snapshot_tree())
    collected, _ = R.collect_tree(tmp_path, roots=["runs", "config"])
    assert sorted(collected) == ["config/litellm.example.yaml", "runs/compat/example-mock.json", "runs/perf/example-mock.json"]


@pytest.mark.parametrize("rel, ok", [
    ("data/catalog.json", True), ("data/internal/platform-inventory.json", True), ("config/litellm.example.yaml", True),
    ("schema/llmops.schema.json", True), ("runs/perf/2026-10-01-opus55.json", True),
    ("data/manifest.json", False), ("web/index.html", False), ("data/x.exe", False), ("data/../secrets.json", False),
    ("release.json", False), ("data/a b.json", False),
])
def test_what_may_be_part_of_a_release(rel, ok):
    assert R.allowed(rel) is ok


# ---------------------------------------------------------------------------------------------- the UI's index
def test_the_index_matches_what_the_ui_build_step_used_to_generate():
    """web/scripts/sync-data.mjs listed the *.json files directly inside each folder, sorted, digests/models without extension."""
    files = {**snapshot_tree(), "data/digests/2026-10-05.json": b"{}", "data/digests/notes.txt": b"x", "data/digests/old/2025-01-01.json": b"{}",
             "data/models/gpt-6-sol.json": b"{}", "data/models/claude-opus-5-5.json": b"{}", "runs/perf/b.json": b"{}", "runs/perf/a.json": b"{}",
             "runs/security/20261012T215504-494d13.json": b"{}", "runs/security/20261012T215504-494d13.cases.json": b"{}"}
    assert R.ui_index(files) == {
        "digests": ["2026-09-24", "2026-10-05"], "models": ["claude-opus-5-5", "gpt-6-sol"],
        "compatRuns": ["example-mock.json"], "perfRuns": ["a.json", "b.json", "example-mock.json"],
        "securityRuns": ["20261012T215504-494d13.json"], "securityCases": ["20261012T215504-494d13.cases.json"], "hasInventory": False,
    }
    assert R.ui_index({**files, "data/internal/platform-inventory.json": b"{}"})["hasInventory"] is True


# ---------------------------------------------------------------------------------------------- write / read
def _publish(store, files, now=T0, previous=None, etag=None):
    release_id = R.new_release_id(now, files)
    changes = R.diff({}, files)
    meta = R.release_meta(release_id, files, created_at=R.iso(now), parent=None, source={"kind": "bootstrap", "by": "t"}, changes=changes)
    R.write_release(store, release_id, files, meta)
    entry = R.history_entry(release_id, published_at=R.iso(now), source={"kind": "bootstrap", "by": "t"}, summary="first", changes=changes)
    manifest = R.build_manifest(release_id, files, published_at=R.iso(now), source={"kind": "bootstrap", "by": "t"}, previous=previous, entry=entry)
    R.switch_pointer(store, manifest, etag)
    return release_id, manifest


def test_a_release_round_trips_and_the_pointer_describes_it(plane):
    files = snapshot_tree()
    release_id, manifest = _publish(plane.data, files)
    assert release_id.startswith("20261002T010000Z-") and R.check_release_id(release_id)
    current = R.load_current(plane.data)
    assert current.files == files and current.release_id == release_id and current.manifest == manifest
    assert manifest["schema"] == "portal-manifest/v1" and manifest["basePath"] == f"releases/{release_id}/"
    assert manifest["sequence"] == 1 and manifest["previousReleaseId"] is None
    assert manifest["publishedAt"] == manifest["generatedAt"] == "2026-10-02T01:00:00Z"
    assert manifest["digests"] == ["2026-09-24"] and manifest["compatRuns"] == ["example-mock.json"]
    assert manifest["history"][0]["releaseId"] == release_id and manifest["history"][0]["changed"] == len(files)
    assert current.meta["files"]["data/catalog.json"] == {"sha256": R.sha256_hex(files["data/catalog.json"]), "bytes": len(files["data/catalog.json"])}
    assert R.list_release_ids(plane.data) == [release_id]
    assert plane.data.list("releases/")[0].startswith(f"releases/{release_id}/")


def test_release_files_are_cacheable_forever_and_the_pointer_never(plane):
    if plane.kind != "s3":
        pytest.skip("cache headers are S3 object metadata")
    release_id, _ = _publish(plane.data, snapshot_tree())
    s3, bucket = plane.data.s3, plane.data.bucket
    data = s3.head_object(Bucket=bucket, Key=f"portal/releases/{release_id}/data/catalog.json")
    assert data["CacheControl"] == "public, max-age=31536000, immutable" and data["ContentType"] == "application/json"
    yaml = s3.head_object(Bucket=bucket, Key=f"portal/releases/{release_id}/config/litellm.example.yaml")
    assert yaml["ContentType"] == "text/yaml; charset=utf-8"
    pointer = s3.head_object(Bucket=bucket, Key="portal/manifest.json")
    assert pointer["CacheControl"] == "no-cache" and pointer["ContentType"] == "application/json"


def test_reading_before_the_first_release_says_what_to_do(plane):
    with pytest.raises(R.NotBootstrapped, match="portalctl bootstrap"):
        R.load_current(plane.data)


def test_a_tampered_or_incomplete_release_is_detected(plane):
    release_id, _ = _publish(plane.data, snapshot_tree())
    key = f"releases/{release_id}/data/lifecycle.json"
    original = plane.data.get(key).data
    plane.data.put(key, original.replace(b"2026", b"2036", 1))
    with pytest.raises(R.IntegrityError, match="hash mismatch"):
        R.load_current(plane.data)
    plane.data.delete(key)
    with pytest.raises(R.IntegrityError, match="lifecycle.json is missing"):
        R.load_current(plane.data)


def test_the_first_pointer_is_create_only_and_later_ones_compare_and_swap(plane):
    files = snapshot_tree()
    _, manifest = _publish(plane.data, files)
    with pytest.raises(Conflict):  # a second "first release" must not replace a live pointer
        R.switch_pointer(plane.data, manifest, None)
    _, etag = R.read_pointer(plane.data)
    files2 = {**files, "runs/perf/new.json": b"{}"}
    _publish(plane.data, files2, now=T1, previous=manifest, etag=etag)
    with pytest.raises(Conflict):  # the etag that was read is stale now
        R.switch_pointer(plane.data, manifest, etag)
    assert R.load_current(plane.data).files == files2 and R.load_current(plane.data).manifest["sequence"] == 2


def test_a_release_id_is_never_reused_for_different_content(plane):
    files = snapshot_tree()
    release_id = R.new_release_id(T0, files)
    meta = R.release_meta(release_id, files, created_at=R.iso(T0), parent=None, source={"kind": "manual"}, changes={})
    R.write_release(plane.data, release_id, files, meta)
    R.write_release(plane.data, release_id, files, meta)  # the same release again (a retried upload) is fine
    other = {**files, "runs/perf/new.json": b"{}"}
    with pytest.raises(R.ReleaseError, match="already used by different content"):
        R.write_release(plane.data, release_id, other, R.release_meta(release_id, other, created_at=R.iso(T0), parent=None, source={}, changes={}))


def test_a_published_release_is_never_written_over(plane):
    """Not even by the attempt that is then refused: files used to be replaced before the id was found to be taken."""
    files = snapshot_tree()
    release_id = R.new_release_id(T0, files)
    meta = R.release_meta(release_id, files, created_at=R.iso(T0), parent=None, source={"kind": "manual"}, changes={})
    pinned = R.write_release(plane.data, release_id, files, meta)
    assert pinned == R.sha256_hex(plane.data.get(f"releases/{release_id}/release.json").data) == R.release_sha(plane.data, release_id)
    changed = {**files, "data/catalog.json": b'{"models": []}\n'}
    with pytest.raises(R.ReleaseError, match="already used by different content"):
        R.write_release(plane.data, release_id, changed, R.release_meta(release_id, changed, created_at=R.iso(T0), parent=None, source={}, changes={}))
    assert R.read_release(plane.data, release_id, expect_sha=pinned)[1] == files  # every byte of the first one is still there
    # a retried upload of the very same release reports the record that is stored, whoever wrote it
    again = R.release_meta(release_id, files, created_at=R.iso(T1), parent=None, source={"kind": "manual", "by": "someone else"}, changes={})
    assert R.write_release(plane.data, release_id, files, again) == pinned


def test_the_pointer_pins_the_record_of_the_release_it_names(plane):
    files = snapshot_tree()
    plane.bootstrap()
    current = R.load_current(plane.data)
    assert current.manifest["releaseSha256"] == R.release_sha(plane.data, current.release_id)
    key = f"releases/{current.release_id}/release.json"
    meta = json.loads(plane.data.get(key).data)
    meta["source"] = {"kind": "manual", "by": "somebody who edited the bucket"}
    plane.data.put(key, R.dump(meta))
    with pytest.raises(R.IntegrityError, match="release.json is not the one that was published"):
        R.load_current(plane.data)
    assert R.read_release(plane.data, current.release_id)[1] == files  # (without the pin the edit goes unnoticed)


def test_the_pause_flag_travels_with_the_pointer_unless_it_is_changed_on_purpose():
    files = snapshot_tree()
    entry = R.history_entry("20261002T010000Z-992cd00d", published_at="2026-10-02T01:00:00Z", source={"kind": "manual"}, summary="s", changes={})
    kw = dict(published_at="2026-10-02T01:00:00Z", source={"kind": "manual"}, entry=entry)
    first = R.build_manifest("20261002T010000Z-992cd00d", files, previous=None, **kw)
    assert "autoPublish" not in first and R.hold_of(first) is None
    paused = {**first, "autoPublish": {"paused": True, "by": "eunice", "at": "2026-10-02T02:00:00Z", "reason": "連假"}}
    kept = R.build_manifest("20261002T010000Z-992cd00d", files, previous=paused, **kw)
    assert R.hold_of(kept) == {"by": "eunice", "at": "2026-10-02T02:00:00Z", "reason": "連假"} and kept["sequence"] == 2
    lifted = R.build_manifest("20261002T010000Z-992cd00d", files, previous=paused, auto_publish={"paused": False}, **kw)
    assert R.hold_of(lifted) is None
    for odd in ({"sequence": "7"}, {"sequence": True}, {"sequence": -3}, {"history": "x"}, {"history": [1, "x"]}):
        assert R.build_manifest("20261002T010000Z-992cd00d", files, previous=odd, **kw)["sequence"] == 1


def test_reads_and_writes_of_a_release_survive_a_failing_file(plane):
    """Files are handled concurrently on S3: an error in one of them must surface, not vanish in a worker thread."""

    class OneBadFile:
        concurrent = True

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def put(self, key, data, **kw):
            if key.endswith("data/lifecycle.json"):
                raise RuntimeError("simulated network error")
            return self._inner.put(key, data, **kw)

    files = snapshot_tree()
    release_id = R.new_release_id(T0, files)
    meta = R.release_meta(release_id, files, created_at=R.iso(T0), parent=None, source={"kind": "manual"}, changes={})
    with pytest.raises(RuntimeError, match="simulated network error"):
        R.write_release(OneBadFile(plane.data), release_id, files, meta)
    assert not plane.data.exists(f"releases/{release_id}/release.json")  # an incomplete release never gets its record
    R.write_release(plane.data, release_id, files, meta)  # the retry completes it
    assert R.read_release(plane.data, release_id)[1] == files


def test_a_release_needs_the_schema_and_data(plane):
    for files in ({"data/catalog.json": b"{}"}, {"schema/llmops.schema.json": b"{}"}):
        with pytest.raises(R.ReleaseError, match="needs schema/llmops.schema.json"):
            R.write_release(plane.data, R.new_release_id(T0, files), files, {"files": {}})
    with pytest.raises(R.ReleaseError, match="cannot be part of a release"):
        R.write_release(plane.data, R.new_release_id(T0, {}), {**snapshot_tree(), "web/x.html": b""}, {"files": {}})


@pytest.mark.parametrize("pointer", [
    b"not json", b"[]", b'{"schema": "something-else"}',
    b'{"schema": "portal-manifest/v1", "releaseId": "../../etc", "basePath": "releases/../../etc/"}',
    b'{"schema": "portal-manifest/v1", "releaseId": "20261002T010000Z-992cd00d", "basePath": "releases/20260101T000000Z-00000000/"}',
])
def test_a_malformed_pointer_is_an_error_not_a_guess(plane, pointer):
    plane.data.put("manifest.json", pointer)
    with pytest.raises(R.ReleaseError):
        R.read_pointer(plane.data)


def test_release_ids_sort_by_time_and_depend_on_content():
    files = snapshot_tree()
    a, b = R.new_release_id(T0, files), R.new_release_id(T1, files)
    assert a < b and a.split("-")[1] == b.split("-")[1]
    assert R.new_release_id(T0, {**files, "runs/perf/x.json": b"{}"}) != a
    assert R.new_release_id(T0, files, salt="retry-1") != a
    for bad in ("", "latest", "../x", "20261002T010000Z", "20261002T010000Z-XYZ", None):
        with pytest.raises(R.ReleaseError):
            R.check_release_id(bad)


def test_history_is_capped(plane):
    previous = {"history": [{"releaseId": f"old-{i}"} for i in range(200)], "sequence": 200, "releaseId": "old-0"}
    files = snapshot_tree()
    entry = R.history_entry("20261002T010000Z-992cd00d", published_at="t", source={"kind": "agent", "by": "pipeline", "proposalId": "prop-1"},
                            summary="  多行\n摘要  " * 80, changes={"modified": ["data/lifecycle.json"]})
    manifest = R.build_manifest("20261002T010000Z-992cd00d", files, published_at="t", source={"kind": "agent"}, previous=previous, entry=entry)
    assert len(manifest["history"]) == R.HISTORY_LIMIT and manifest["history"][0] is entry and manifest["sequence"] == 201
    assert len(entry["summary"]) <= 400 and "\n" not in entry["summary"] and entry["proposalId"] == "prop-1" and entry["changed"] == 1
    assert len(json.dumps(manifest)) < 60_000  # the pointer stays small: the UI downloads it on every load


# ---------------------------------------------------------------------------------------------- daily versions
def _publish_at(previous, release_id, at):
    entry = R.history_entry(release_id, published_at=at, source={"kind": "agent"}, summary="s", changes={})
    return R.build_manifest(release_id, snapshot_tree(), published_at=at, source={"kind": "agent"}, previous=previous, entry=entry)


def test_the_pointer_keeps_one_version_per_taipei_day_the_last_release_of_that_day():
    m = _publish_at(None, "20261002T010000Z-00000001", "2026-10-02T01:00:00Z")
    m = _publish_at(m, "20261002T033000Z-00000002", "2026-10-02T03:30:00Z")
    assert m["versions"] == [{"date": "2026-10-02", "releaseId": "20261002T033000Z-00000002", "publishedAt": "2026-10-02T03:30:00Z"}]
    # 16:30 UTC is already the next day in Taipei (00:30)
    m = _publish_at(m, "20261002T163000Z-00000003", "2026-10-02T16:30:00Z")
    assert [(v["date"], v["releaseId"][-8:]) for v in m["versions"]] == [("2026-10-03", "00000003"), ("2026-10-02", "00000002")]


def test_only_the_latest_seven_days_are_kept():
    m = None
    for day in range(1, 13):
        m = _publish_at(m, f"202610{day:02d}T010000Z-{day:08d}", f"2026-10-{day:02d}T01:00:00Z")
    assert [v["date"] for v in m["versions"]] == [f"2026-10-{d:02d}" for d in range(12, 5, -1)]
    assert len(m["versions"]) == R.VERSIONS_LIMIT
    # a rollback on the 12th makes the target the version of the 12th (what was live at the end of the day)
    m = _publish_at(m, "20261011T010000Z-00000011", "2026-10-12T05:00:00Z")
    assert m["versions"][0] == {"date": "2026-10-12", "releaseId": "20261011T010000Z-00000011", "publishedAt": "2026-10-12T05:00:00Z"}


def test_a_pointer_from_before_versions_is_seeded_from_its_history():
    history = [  # newest first, as the pointer keeps it
        {"releaseId": "20261005T020000Z-0000000c", "publishedAt": "2026-10-05T02:00:00Z"},
        {"releaseId": "20261005T010000Z-0000000b", "publishedAt": "2026-10-05T01:00:00Z"},
        {"releaseId": "20261003T010000Z-0000000a", "publishedAt": "2026-10-03T01:00:00Z"},
        {"releaseId": "not-a-release", "publishedAt": "2026-10-02T01:00:00Z"},
        {"releaseId": "20261001T010000Z-00000009", "publishedAt": "t"},
    ]
    m = _publish_at({"history": history, "releaseId": "20261005T020000Z-0000000c"}, "20261006T010000Z-0000000d", "2026-10-06T01:00:00Z")
    assert [(v["date"], v["releaseId"][-1]) for v in m["versions"]] == [("2026-10-06", "d"), ("2026-10-05", "c"), ("2026-10-03", "a")]


def test_damaged_version_entries_are_dropped_and_odd_timestamps_add_none():
    previous = {"versions": [{"date": "2026-10-01", "releaseId": "20261001T010000Z-00000001", "publishedAt": "2026-10-01T01:00:00Z", "x": "<b>"},
                             {"date": "10/02/2026", "releaseId": "20261002T010000Z-00000002", "publishedAt": "2026-10-02T01:00:00Z"},
                             {"date": "2026-10-03", "releaseId": "../../x", "publishedAt": "2026-10-03T01:00:00Z"},
                             {"date": "2026-10-04", "releaseId": "20261004T010000Z-00000004", "publishedAt": "2026-10-04T01:00:00"},
                             "x", None]}
    m = _publish_at(previous, "20261005T010000Z-00000005", "t")  # (a timestamp without a zone has no day)
    assert m["versions"] == [{"date": "2026-10-01", "releaseId": "20261001T010000Z-00000001", "publishedAt": "2026-10-01T01:00:00Z"}]
    assert R.versions_of(m) == m["versions"] and R.versions_of({"versions": "x"}) == []


def test_security_test_summaries_are_published_with_the_other_test_results():
    assert R.publishable("runs/security/20261012T215504-494d13.json")
    assert not R.publishable("runs/security/nested/x.json") and not R.publishable("runs/secsuite-cache/x.json")

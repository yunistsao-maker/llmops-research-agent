"""The storage interface behaves the same on a folder and on S3 (moto), including the conditional writes publishing relies on."""

from __future__ import annotations

import pytest

from portal_pipeline.storage import Conflict, DirStore, NotFound, S3Store, StorageError, check_key, open_store


def test_put_get_list_delete(plane):
    store = plane.data
    assert store.list() == [] and not store.exists("a/b.json")
    etag1 = store.put("a/b.json", b'{"v": 1}', content_type="application/json")
    store.put("a/c.yaml", b"x: 1\n", content_type="text/yaml")
    store.put("z.json", b"{}", content_type="application/json")
    blob = store.get("a/b.json")
    assert blob.data == b'{"v": 1}' and blob.etag == etag1
    assert store.list() == ["a/b.json", "a/c.yaml", "z.json"]
    assert store.list("a/") == ["a/b.json", "a/c.yaml"]
    assert store.exists("z.json")
    etag2 = store.put("a/b.json", b'{"v": 2}', content_type="application/json")
    assert etag2 != etag1 and store.get("a/b.json").data == b'{"v": 2}'
    store.delete("z.json")
    store.delete("z.json")  # deleting what is not there is not an error
    assert store.list() == ["a/b.json", "a/c.yaml"]
    with pytest.raises(NotFound):
        store.get("z.json")


def test_a_create_only_write_never_replaces_an_existing_object(plane):
    store = plane.data
    store.put("releases/r1/release.json", b"one", if_none_match=True)
    with pytest.raises(Conflict):
        store.put("releases/r1/release.json", b"two", if_none_match=True)
    assert store.get("releases/r1/release.json").data == b"one"


def test_a_compare_and_swap_write_needs_the_version_that_was_read(plane):
    store = plane.data
    store.put("manifest.json", b"v1")
    seen = store.get("manifest.json").etag
    store.put("manifest.json", b"v2 by someone else")
    with pytest.raises(Conflict):
        store.put("manifest.json", b"v2 by me", if_match=seen)
    assert store.get("manifest.json").data == b"v2 by someone else"
    current = store.get("manifest.json").etag
    store.put("manifest.json", b"v3", if_match=current)
    store.put("manifest.json", b"v4", if_match='"%s"' % store.get("manifest.json").etag)  # quoted ETags are accepted too
    assert store.get("manifest.json").data == b"v4"


def test_a_compare_and_swap_on_a_missing_object_is_a_conflict_not_a_create(plane):
    with pytest.raises(Conflict):
        plane.data.put("manifest.json", b"x", if_match="abc123")
    assert not plane.data.exists("manifest.json")


@pytest.mark.parametrize("key", ["", "../x", "/abs", "a//b", ".hidden", "a/.hidden", "a/../b", "a b", "a/b/", "a\\b", "a\nb", "é.json"])
def test_unsafe_keys_are_refused_before_touching_storage(plane, key):
    for call in (lambda: plane.data.get(key), lambda: plane.data.put(key, b"x"), lambda: plane.data.exists(key), lambda: plane.data.delete(key)):
        with pytest.raises(StorageError):
            call()
    assert plane.data.list() == []


def test_s3_objects_carry_the_content_type_and_cache_policy(plane):
    if plane.kind != "s3":
        pytest.skip("S3 metadata")
    plane.data.put("manifest.json", b"{}", content_type="application/json", cache_control="no-cache")
    head = plane.data.s3.head_object(Bucket=plane.data.bucket, Key="portal/manifest.json")
    assert head["ContentType"] == "application/json" and head["CacheControl"] == "no-cache"


def test_stores_never_leave_their_prefix(plane):
    plane.data.put("x.json", b"{}")
    plane.control.put("pending/p.json", b"{}")
    assert plane.data.list() == ["x.json"] and plane.control.list() == ["pending/p.json"]
    assert plane.research.list() == []


def test_open_store_understands_s3_uris_and_folders(tmp_path):
    s3 = open_store("s3://my-bucket/portal", client=object())
    assert isinstance(s3, S3Store) and (s3.bucket, s3.prefix) == ("my-bucket", "portal/") and s3.uri("manifest.json") == "s3://my-bucket/portal/manifest.json"
    assert open_store("s3://my-bucket", client=object()).uri("k") == "s3://my-bucket/k"
    folder = open_store(str(tmp_path / "data"))
    assert isinstance(folder, DirStore) and folder.uri("a.json") == str(tmp_path / "data" / "a.json")
    for bad in ("", "s3://", "s3:///prefix"):
        with pytest.raises(StorageError):
            open_store(bad)


def test_a_folder_store_writes_atomically_and_hides_its_temp_files(tmp_path):
    store = DirStore(tmp_path)
    store.put("a/b.json", b"x")
    (tmp_path / "a" / ".tmp-leftover").write_bytes(b"partial")
    assert store.list() == ["a/b.json"]


# ---------------------------------------------------------------------------------------------- S3 answers that moto never gives
class _ClientError(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class DenyingS3:
    """S3 as it answers a caller whose s3:ListBucket is limited to prefixes: a missing key may come back as 403."""

    def __init__(self, objects, *, can_list=True):
        self.objects, self.can_list, self.calls = dict(objects), can_list, []

    def get_object(self, Bucket, Key):
        self.calls.append(("get", Key))
        if Key not in self.objects:
            raise _ClientError("AccessDenied", 403)
        import io
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": '"abc"'}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000):
        self.calls.append(("list", Prefix))
        if not self.can_list:
            raise _ClientError("AccessDenied", 403)
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)][:MaxKeys]}


def test_a_403_for_a_key_that_is_not_there_is_not_found():
    """The waiting step and the pause flag both read objects that normally do not exist yet."""
    s3 = DenyingS3({"pipeline/pending/prop-1.json": b"{}", "pipeline/hold.json.bak": b"x"})
    store = S3Store("bucket", "pipeline/", s3)
    with pytest.raises(NotFound):
        store.get("hold.json")
    assert s3.calls == [("get", "pipeline/hold.json"), ("list", "pipeline/hold.json")]  # hold.json.bak is not hold.json
    assert store.get("pending/prop-1.json").data == b"{}"


def test_a_403_for_a_key_that_exists_or_cannot_be_checked_is_an_access_problem():
    class Unreadable(DenyingS3):
        def get_object(self, Bucket, Key):
            raise _ClientError("AccessDenied", 403)

    for s3 in (Unreadable({"portal/manifest.json": b"{}"}), DenyingS3({}, can_list=False)):
        with pytest.raises(StorageError) as e:
            S3Store("bucket", "portal/", s3).get("manifest.json")
        assert not isinstance(e.value, NotFound) and "AccessDenied" in str(e.value) and "s3://bucket/portal/manifest.json" in str(e.value)


def test_a_bucket_that_does_not_exist_is_not_an_empty_data_location():
    class NoBucket:
        def get_object(self, **kw):
            raise _ClientError("NoSuchBucket", 404)

    with pytest.raises(StorageError) as e:
        S3Store("typo-bucket", "portal/", NoBucket()).get("manifest.json")
    assert not isinstance(e.value, NotFound) and "typo-bucket" in str(e.value)


def test_a_compare_and_swap_without_a_version_is_refused(plane):
    plane.data.put("manifest.json", b"1")
    for empty in ("", '""', " "):
        with pytest.raises(StorageError):
            plane.data.put("manifest.json", b"2", if_match=empty)
    assert plane.data.get("manifest.json").data == b"1"


@pytest.mark.parametrize("key", ["manifest.json\n", "releases/x\n", "a/b.json\r\n"])
def test_a_key_with_a_trailing_line_break_is_unsafe(key):
    with pytest.raises(StorageError):
        check_key(key)


def test_two_writers_racing_on_a_folder_cannot_both_win(tmp_path):
    """Compare-and-swap on a folder is one atomic step too (several portalctl processes on one checkout of the data)."""
    import threading

    store = DirStore(tmp_path / "portal")
    etag = store.put("manifest.json", b"0")
    wins, barrier = [], threading.Barrier(8)

    def attempt(i):
        barrier.wait()
        try:
            store.put("manifest.json", str(i).encode(), if_match=etag)
            wins.append(i)
        except Conflict:
            pass

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(1, 9)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1 and store.get("manifest.json").data == str(wins[0]).encode()

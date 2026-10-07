"""Object storage for the portal data plane: an S3 prefix in AWS, a folder for local runs and tests.

Only what the pipeline needs: get / put / list / delete, plus *conditional* puts.  The conditional put is what makes
publishing safe without locks: the pointer file is replaced only if it is still the version that was read
(``if_match``), and a release is created only if it does not exist yet (``if_none_match``).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# one or more "/"-separated segments of safe characters; no "..", no leading "/" or ".", no empty segments
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+=-]*(/[A-Za-z0-9][A-Za-z0-9._@+=-]*)*")  # used with fullmatch: "$" would let "x\n" through


class StorageError(Exception):
    """A storage operation failed for a reason the caller cannot fix by retrying blindly."""


class NotFound(StorageError):
    """The object does not exist."""


class Conflict(StorageError):
    """A conditional write was refused: somebody else changed (or created) the object first."""


def check_key(key: str) -> str:
    if not isinstance(key, str) or len(key) > 512 or not _KEY.fullmatch(key):
        raise StorageError(f"unsafe object key: {key!r}")
    return key


def check_prefix(prefix: str) -> str:
    if prefix == "":
        return prefix
    check_key(prefix.rstrip("/"))
    return prefix


def check_etag(etag: str) -> str:
    """The version token of a compare-and-swap; an empty one would turn the write into an unconditional one."""
    token = str(etag).strip().strip('"').strip()
    if not token:
        raise StorageError("a conditional write needs the version (ETag) that was read; got an empty one")
    return token


@dataclass(frozen=True)
class Blob:
    data: bytes
    etag: str  # opaque version token: pass it back as ``if_match``


class Store(Protocol):
    def get(self, key: str) -> Blob: ...

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream", cache_control: str | None = None,
            if_match: str | None = None, if_none_match: bool = False) -> str: ...

    def list(self, prefix: str = "") -> list[str]: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def uri(self, key: str = "") -> str: ...


# ---------------------------------------------------------------------------------------------- folder
_DIR_LOCK = threading.Lock()


class DirStore:
    """A folder with the same key layout as the bucket prefix (local development, tests, air-gapped runs)."""

    concurrent = False  # local files: threads would only queue up behind the lock

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    @staticmethod
    def _etag(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def get(self, key: str) -> Blob:
        path = self._path(key)
        if not path.is_file():
            raise NotFound(self.uri(key))
        data = path.read_bytes()
        return Blob(data, self._etag(data))

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream", cache_control: str | None = None,
            if_match: str | None = None, if_none_match: bool = False) -> str:
        path = self._path(key)
        expected = check_etag(if_match) if if_match is not None else None
        with self._locked(conditional=expected is not None or if_none_match):
            if if_none_match and path.exists():
                raise Conflict(f"{self.uri(key)} already exists")
            if expected is not None and (not path.is_file() or self._etag(path.read_bytes()) != expected):
                raise Conflict(f"{self.uri(key)} changed since it was read")
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")  # write + rename: a reader never sees half a file
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        return self._etag(data)

    @contextlib.contextmanager
    def _locked(self, conditional: bool):
        """Compare-and-write as one step: between threads of this process, and (where the OS has flock) between processes."""
        if not conditional:
            yield
            return
        with _DIR_LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            handle = open(self.root / ".lock", "a+b")  # noqa: SIM115 - held for the duration of the block
            try:
                try:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX)
                except ImportError:  # no flock on this platform: the in-process lock still holds
                    pass
                yield
            finally:
                handle.close()

    def list(self, prefix: str = "") -> list[str]:
        check_prefix(prefix)
        if not self.root.is_dir():
            return []
        keys = []
        for path in self.root.rglob("*"):
            if path.is_file() and not path.name.startswith(".tmp-") and path.name != ".lock":
                rel = path.relative_to(self.root).as_posix()
                if rel.startswith(prefix):
                    keys.append(rel)
        return sorted(keys)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()

    def uri(self, key: str = "") -> str:
        return str(self.root / key) if key else str(self.root)


# ---------------------------------------------------------------------------------------------- S3
def _error(e: Exception) -> tuple[str, int]:
    response = getattr(e, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code") or "")
    status = int((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0)
    return code, status


def _missing(e: Exception) -> bool:
    code, status = _error(e)
    return code != "NoSuchBucket" and (code in ("NoSuchKey", "NotFound", "404") or status == 404)


def _denied(e: Exception) -> bool:
    code, status = _error(e)
    return code in ("AccessDenied", "403", "Forbidden") or status == 403


def _precondition(e: Exception) -> bool:
    code, status = _error(e)
    return code in ("PreconditionFailed", "ConditionalRequestConflict") or status in (409, 412)


class S3Store:
    """``s3://bucket/prefix/``.  Conditional puts map to S3 conditional writes (If-Match / If-None-Match)."""

    concurrent = True  # every call is a network round trip; boto3 clients are safe to share between threads

    def __init__(self, bucket: str, prefix: str = "", client: Any = None):
        if not bucket:
            raise StorageError("S3 locations look like s3://bucket/prefix/")
        self.bucket = bucket
        self.prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
        if self.prefix:
            check_key(self.prefix.rstrip("/"))
        self._client = client

    @property
    def s3(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    def _key(self, key: str) -> str:
        return self.prefix + check_key(key)

    def get(self, key: str) -> Blob:
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as e:
            if _missing(e):
                raise NotFound(self.uri(key)) from e
            if _error(e)[0] == "NoSuchBucket":
                raise StorageError(f"bucket {self.bucket} 不存在（或不在這個帳號／region）：{self.uri(key)}") from e
            if _denied(e):
                # S3 answers 403 instead of 404 for a missing key when the caller's s3:ListBucket does not cover the
                # request - which can be the case for roles whose ListBucket is limited to prefixes. Ask the list API.
                if self._listed(key) is False:
                    raise NotFound(self.uri(key)) from e
                raise StorageError(f"AccessDenied：沒有權限讀取 {self.uri(key)}（檢查這個身分的 s3:GetObject／s3:ListBucket）") from e
            raise
        return Blob(resp["Body"].read(), str(resp.get("ETag", "")).strip('"'))

    def _listed(self, key: str) -> bool | None:
        """Does the list API show exactly this key?  None when listing is not allowed either."""
        full = self._key(key)
        try:
            page = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=full, MaxKeys=1)
        except Exception:
            return None
        return any(obj.get("Key") == full for obj in page.get("Contents", []))

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream", cache_control: str | None = None,
            if_match: str | None = None, if_none_match: bool = False) -> str:
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": self._key(key), "Body": data, "ContentType": content_type}
        if cache_control:
            params["CacheControl"] = cache_control
        if if_match is not None:
            params["IfMatch"] = check_etag(if_match)
        if if_none_match:
            params["IfNoneMatch"] = "*"
        try:
            resp = self.s3.put_object(**params)
        except Exception as e:
            # 412: the condition is false.  409: a concurrent write to the same key.  404 with If-Match: the object is gone.
            if _precondition(e) or (if_match is not None and _missing(e)):
                raise Conflict(f"{self.uri(key)} was changed by someone else") from e
            raise
        return str(resp.get("ETag", "")).strip('"')

    def list(self, prefix: str = "") -> list[str]:
        check_prefix(prefix)
        keys: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix + prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"][len(self.prefix):])
        return sorted(keys)

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as e:
            if _missing(e):
                return False
            if _denied(e) and self._listed(key) is False:
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self._key(key))

    def uri(self, key: str = "") -> str:
        return f"s3://{self.bucket}/{self.prefix}{key}"


def open_store(location: str, client: Any = None) -> Store:
    """``s3://bucket/prefix/`` or a folder path."""
    if not location:
        raise StorageError("no storage location given")
    if location.startswith("s3://"):
        bucket, _, prefix = location[len("s3://"):].partition("/")
        return S3Store(bucket, prefix, client)
    return DirStore(location)

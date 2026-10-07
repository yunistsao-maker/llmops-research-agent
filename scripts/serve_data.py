#!/usr/bin/env python3
"""本機的資料位置：用一個資料夾模擬 data plane 的 CloudFront distribution，讓 UI 在本機就能讀「發布出來的資料」。

    python scripts/portalctl.py --data ./_local/portal bootstrap --portal ../llmops-portal --yes   # 先建立第一個 release
    python scripts/serve_data.py ./_local/portal                                             # http://127.0.0.1:8787/
    python scripts/serve_data.py ./_local/portal --token local-demo-token-0001 --origin http://localhost:5173

把印出來的 dataBaseUrl 填進 UI 的 public/config.json，再 `npm run dev`；之後用 portalctl.py（--data 指同一個資料夾）
publish / push / rollback，UI 重新載入就會看到新版本。

它照著 infra/data-plane.yaml 的行為回應，讓本機看到的和 AWS 上一致：
  · 只接受 GET / HEAD（其他 method 一律 403）；設定了 --token 時，路徑必須以 /<token>/ 開頭，否則 403
  · manifest.json 回 Cache-Control: no-cache，releases/ 底下的檔案回一年、immutable
  · 對 --origin 允許的來源加上 CORS header；preflight（OPTIONS）一律 403 —— 正式環境的 S3 origin 不回應 preflight，
    所以 UI 只能發出不帶自訂 header 的 GET，這裡故意一樣嚴格
  · token 不對時的 403 任何網頁都讀得到狀態碼（不含資料），和正式環境的 CloudFront Function 一樣

預設只允許本機的 Vite 開發網址（localhost / 127.0.0.1 的 5173、4173）從瀏覽器讀取：這個資料夾裡有內部清單，不該因為開著
這支工具，就讓瀏覽器裡任何一個網站都讀得到。--origin 是在這份清單之外再加（'*' 表示任何網頁）。
同樣的理由，request 的 Host 必須是 localhost 或 IP 位址（或 --allow-host 列出的名稱）：別的網站可以把自己的網域名稱
指到 127.0.0.1（DNS rebinding），那樣的 request 對瀏覽器來說是「同一個網站」，CORS 管不到，只有 Host 看得出來。

這只是開發與示範用的工具（單機、沒有 TLS、沒有存取紀錄），不是正式環境的元件。
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import ipaddress
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "pipeline") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "pipeline"))

from portal_pipeline import releases as R  # noqa: E402
from portal_pipeline.storage import StorageError, check_key  # noqa: E402

#: who may read without being named: the addresses `npm run dev` and `npm run preview` use on this machine
LOCAL_ORIGINS = tuple(f"http://{host}:{port}" for host in ("localhost", "127.0.0.1") for port in (5173, 4173))
#: the same rule as the data plane stack's DataAccessToken parameter, so a token that works here works there
TOKEN_RULE = re.compile(r"[A-Za-z0-9_-]{16,64}")


def host_of(header: str | None) -> str:
    """The host a ``Host`` header names, without the port ("" when there is none to speak of)."""
    value = (header or "").strip().lower()
    if value.startswith("["):  # [::1]:8787
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def cache_control(key: str) -> str:
    """What the publisher sets on each kind of object (see portal_pipeline.releases)."""
    return R.POINTER_CACHE if key == R.MANIFEST_KEY else R.IMMUTABLE


class DataHandler(BaseHTTPRequestHandler):
    server_version = "portal-data-local"
    protocol_version = "HTTP/1.1"

    # set by make_server
    root: Path
    token: str
    origins: tuple[str, ...]
    hosts: tuple[str, ...] = ()
    quiet: bool
    #: when it is a list, every request that was answered is noted in it: {"method", "path", "status", "headers"}.
    #: (ui_smoke.py reads what a browser really sent from here - a browser does not tell a page about its preflights.)
    seen: list | None = None

    # ------------------------------------------------------------------ helpers
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - http.server's name
        if not self.quiet:
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):  # the browser went away mid-request: not worth a traceback
            self.close_connection = True

    def _cors(self) -> dict[str, str]:
        origin = self.headers.get("Origin")
        if not origin:
            return {}
        if "*" in self.origins:
            return {"Access-Control-Allow-Origin": "*"}
        if origin in self.origins:
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        return {}

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None, *, head: bool = False) -> None:
        merged = {"Content-Length": str(len(body)), "X-Content-Type-Options": "nosniff", **self._cors(), **(headers or {})}
        if status == HTTPStatus.NOT_MODIFIED:
            del merged["Content-Length"]  # a 304 has no body, and "0" would claim the file is empty
        if self.seen is not None:
            self.seen.append({"method": self.command, "path": self._path(), "status": int(status),
                              "headers": sorted(name.lower() for name in self.headers.keys())})
        try:
            self.send_response(status)
            for name, value in merged.items():
                self.send_header(name, value)
            self.end_headers()
            if body and not head and status not in (HTTPStatus.NOT_MODIFIED, HTTPStatus.NO_CONTENT):
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # the browser went away (a reload, a closed tab)
            self.close_connection = True

    def _error(self, status: int, code: str, message: str, *, head: bool = False, readable_by_any_page: bool = False) -> None:
        body = f'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>{code}</Code><Message>{message}</Message></Error>'.encode("utf-8")
        headers = {"Content-Type": "application/xml", "Cache-Control": "no-store"}
        if readable_by_any_page:
            headers["Access-Control-Allow-Origin"] = "*"
        self._send(status, body, headers, head=head)

    def _path(self) -> str:
        """The path exactly as it was sent (http.server tidies self.path, e.g. "//x" -> "/x"), still percent-encoded:
        that is what the distribution's token check sees."""
        parts = self.requestline.split(" ")
        return (parts[1] if len(parts) >= 2 else self.path).split("?", 1)[0].split("#", 1)[0]

    def _key(self) -> str | None:
        """The object key a request path names, or None when the access token is missing or wrong."""
        path = self._path()
        if self.token:
            prefix = f"/{self.token}/"
            if not path.startswith(prefix):
                return None
            path = path[len(prefix) - 1:]
        return unquote(path)[1:]  # S3 decodes the rest; "//x" is the key "/x", which does not exist

    def _named_right(self) -> bool:
        """Was this server asked for by an address, by "localhost", or by a name its operator listed?"""
        host = host_of(self.headers.get("Host"))
        return bool(host) and (host == "localhost" or is_address(host) or host in self.hosts)

    # ------------------------------------------------------------------ methods
    def _get(self, head: bool) -> None:
        if not self._named_right():
            # a page of another site that pointed its own name at this machine: to the browser this is "same origin"
            return self._error(HTTPStatus.FORBIDDEN, "AccessDenied", "This server answers to localhost and to IP addresses only.", head=head)
        key = self._key()
        if key is None:
            # like the distribution's token check: the refusal carries no data, and a page may read that it is a 403
            return self._error(HTTPStatus.FORBIDDEN, "AccessDenied", "Forbidden", head=head, readable_by_any_page=True)
        try:
            check_key(key)
            path = (self.root / key).resolve()
            path.relative_to(self.root)
        except (StorageError, ValueError):
            return self._error(HTTPStatus.NOT_FOUND, "NoSuchKey", "The specified key does not exist.", head=head)
        if not path.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "NoSuchKey", "The specified key does not exist.", head=head)
        body = path.read_bytes()
        etag = '"' + hashlib.md5(body).hexdigest() + '"'  # noqa: S324 - an ETag like S3's, not a security measure
        headers = {
            "Content-Type": R.content_type(key),
            "Cache-Control": cache_control(key),
            "ETag": etag,
            "Last-Modified": email.utils.formatdate(path.stat().st_mtime, usegmt=True),
        }
        if etag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
            return self._send(HTTPStatus.NOT_MODIFIED, b"", {k: v for k, v in headers.items() if k != "Content-Type"}, head=head)
        self._send(HTTPStatus.OK, body, headers, head=head)

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        self._get(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._get(head=True)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # S3 without a CORS configuration refuses preflight requests, and that is the origin behind the real distribution.
        self._error(HTTPStatus.FORBIDDEN, "AccessForbidden", "CORSResponse: This CORS request is not allowed.")

    def _refuse(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 1 << 20))
        self.close_connection = True
        self._error(HTTPStatus.FORBIDDEN, "MethodNotAllowed", "This distribution is not configured to allow the HTTP request that was used.")

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815

    def __getattr__(self, name: str):
        # http.server answers a method it has no do_<METHOD> for with its own 501 page; the distribution answers every
        # method it does not allow the same way, and so does this (PROPFIND, TRACE, CONNECT ...)
        if name.startswith("do_"):
            return self._refuse
        raise AttributeError(name)


def make_server(root: str | Path, *, host: str = "127.0.0.1", port: int = 8787, token: str = "", origins: tuple[str, ...] = LOCAL_ORIGINS,
                quiet: bool = False, seen: list | None = None, hosts: tuple[str, ...] = ()) -> ThreadingHTTPServer:
    """A server for the data location in ``root`` (port 0 = any free port; see server.server_address).

    ``origins``: the pages that may read it from a browser ("*" = any); by default only this machine's dev servers.
    ``hosts``: names, besides "localhost" and IP addresses, that a request may address this server by.
    ``seen``: a list that receives one entry per answered request (see DataHandler.seen).
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"{root} 不是資料夾")
    handler = type("Handler", (DataHandler,), {"root": root, "token": token, "origins": tuple(origins), "quiet": quiet, "seen": seen,
                                               "hosts": tuple(h.strip().lower() for h in hosts if h.strip())})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def base_url(server: ThreadingHTTPServer, token: str = "") -> str:
    host, port = server.server_address[:2]
    if host in ("0.0.0.0", "::", ""):  # listening on every interface: that is not an address a browser can open
        host = "localhost"
    elif ":" in str(host):
        host = f"[{host}]"
    return f"http://{host}:{port}/" + (f"{token}/" if token else "")


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return thread


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="serve_data.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("root", help="資料位置的資料夾（portalctl.py --data 指的同一個；裡面是 manifest.json 與 releases/）")
    p.add_argument("--host", default="127.0.0.1", help="預設只聽本機")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--token", default="", help="模擬 DataAccessToken：路徑必須以 /<token>/ 開頭（16–64 個 A-Z a-z 0-9 _ -，和 stack 的規則相同）")
    p.add_argument("--origin", action="append", default=[], metavar="ORIGIN",
                   help="另外允許讀資料的網頁來源（可重複）；本機的 Vite 開發網址一律允許，'*' 表示任何網頁")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="除了 localhost 與 IP 位址之外，也接受用這個主機名稱連進來的 request（可重複；例如這台機器在區網裡的名稱）")
    p.add_argument("--quiet", action="store_true", help="不印每一個 request")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # captured streams in tests, exotic consoles
            pass
    args = build_parser().parse_args(argv)
    if args.token and not TOKEN_RULE.fullmatch(args.token):
        print("✘ --token 必須是 16–64 個 A-Z a-z 0-9 _ -（data plane stack 的 DataAccessToken 也是這個規則；例如 openssl rand -hex 16）", file=sys.stderr, flush=True)
        return 3
    origins = tuple(dict.fromkeys([*LOCAL_ORIGINS, *args.origin]))
    try:
        server = make_server(args.root, host=args.host, port=args.port, token=args.token, origins=origins, quiet=args.quiet, hosts=tuple(args.allow_host))
    except (ValueError, OSError) as e:
        print(f"✘ {e}", file=sys.stderr, flush=True)
        return 3
    url = base_url(server, args.token)
    root = Path(args.root).resolve()
    print(f"資料位置 {root}\n  dataBaseUrl：{url}\n  UI 的 public/config.json 寫：{{\"dataBaseUrl\": \"{url}\"}}", flush=True)
    allowed = "任何網頁" if "*" in server.RequestHandlerClass.origins else "、".join(server.RequestHandlerClass.origins)
    print(f"  可以從瀏覽器讀取的網頁來源：{allowed}（用 --origin 再加）", flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  ⚠ 正在聽 {args.host}：同一個網路上的其他機器也連得到這個資料夾（沒有 TLS；--token 只是路徑）。"
              "用主機名稱連進來的 request 要先以 --allow-host 列出那個名稱", flush=True)
    if not (root / R.MANIFEST_KEY).is_file():
        print(f"  （還沒有 release：先執行 portalctl.py --data {args.root} bootstrap --portal <portal repo>）", flush=True)
    print("Ctrl-C 結束", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

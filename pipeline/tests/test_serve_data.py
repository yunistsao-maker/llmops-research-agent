"""scripts/serve_data.py: the local stand-in for the data distribution answers the way infra/data-plane.yaml is set up to."""

from __future__ import annotations

import hashlib
import http.client
import json
import os

import pytest

import serve_data
from conftest import T0, T1, accepted_run, snapshot_tree, token_cases
from portal_pipeline import publish as P
from portal_pipeline import releases as R
from portal_pipeline.storage import DirStore

TOKEN = "local-demo-token-0001"


@pytest.fixture
def location(tmp_path):
    """A bootstrapped data location in a folder, plus things next to it that must never be served."""
    root = tmp_path / "bucket" / "portal"
    outcome = P.publish_tree(DirStore(root), snapshot_tree(), bootstrap=True, actor="test", now=T0)
    assert outcome.status == "published"
    (tmp_path / "bucket" / "research").mkdir()
    (tmp_path / "bucket" / "research" / "run.json").write_text('{"secret": true}', encoding="utf-8")
    (root / ".hidden.json").write_text("{}", encoding="utf-8")
    return root, outcome.release_id


def start(root, **options):
    server = serve_data.make_server(root, port=0, quiet=True, **options)
    serve_data.serve_in_thread(server)
    return server


@pytest.fixture
def served(location):
    servers = []

    def make(**options):
        server = start(location[0], **options)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


def ask(server, path, method="GET", headers=None, body=None):
    """One raw request (the path is sent exactly as written): (status, headers, body)."""
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def test_the_pointer_is_never_cached_and_release_files_are_cached_forever(location, served):
    root, release_id = location
    server = served()
    status, headers, body = ask(server, "/manifest.json")
    assert status == 200 and json.loads(body)["releaseId"] == release_id
    assert headers["cache-control"] == "no-cache" == R.POINTER_CACHE
    assert headers["content-type"] == "application/json" and headers["x-content-type-options"] == "nosniff"
    assert headers["etag"] == '"' + hashlib.md5(body).hexdigest() + '"'

    status, headers, body = ask(server, f"/releases/{release_id}/data/catalog.json")
    assert status == 200 and body == (root / "releases" / release_id / "data" / "catalog.json").read_bytes()
    assert headers["cache-control"] == R.IMMUTABLE and "immutable" in headers["cache-control"]
    status, headers, body = ask(server, f"/releases/{release_id}/config/litellm.example.yaml")
    assert status == 200 and headers["content-type"] == "text/yaml; charset=utf-8" and "model_list:" in body.decode("utf-8")
    assert "access-control-allow-origin" not in headers  # not a cross-origin request


def test_head_and_conditional_requests(location, served):
    server = served()
    status, headers, body = ask(server, "/manifest.json", method="HEAD")
    assert status == 200 and body == b"" and int(headers["content-length"]) > 100
    status, headers304, body = ask(server, "/manifest.json", headers={"If-None-Match": headers["etag"]})
    assert status == 304 and body == b"" and headers304["cache-control"] == "no-cache"
    assert "content-length" not in headers304 and "content-type" not in headers304  # "0" would say the file is empty
    assert headers304["etag"] == headers["etag"]
    assert ask(server, "/manifest.json", headers={"If-None-Match": '"something-else"'})[0] == 200


@pytest.mark.parametrize("path", [
    "/nothing.json", "/releases/", "/releases", "/", "", "/releases/20991231T000000Z-00000000/data/catalog.json",
    "/../research/run.json", "/%2e%2e/research/run.json", "/releases/../../research/run.json", "/..%2fresearch%2frun.json",
    "//etc/passwd", "/.hidden.json", "/releases/%00", "/manifest.json/",
])
def test_what_is_not_a_published_object_is_not_found(location, served, path):
    status, headers, body = ask(served(), path or "/")
    assert status == 404 and b"NoSuchKey" in body
    assert headers["cache-control"] == "no-store" and b"secret" not in body


def test_a_link_out_of_the_data_location_is_not_followed(location, served):
    root, _ = location
    os.symlink(root.parent / "research" / "run.json", root / "leak.json")
    assert ask(served(), "/leak.json")[0] == 404


def test_with_a_token_every_path_must_start_with_it(location, served):
    root, release_id = location
    server = served(token=TOKEN)
    assert serve_data.base_url(server, TOKEN).endswith(f"/{TOKEN}/")
    assert ask(server, f"/{TOKEN}/manifest.json")[0] == 200
    assert ask(server, f"/{TOKEN}/releases/{release_id}/release.json")[0] == 200
    for path in ("/manifest.json", f"/{TOKEN}", f"/{TOKEN}x/manifest.json", f"/x{TOKEN}/manifest.json", "/wrong-token-000000000/manifest.json",
                 f"/releases/{release_id}/release.json", f"/{TOKEN.upper()}/manifest.json", f"/{TOKEN}%2Fmanifest.json"):
        status, headers, body = ask(server, path)
        assert status == 403 and headers["cache-control"] == "no-store" and b"releaseId" not in body, path
    assert ask(server, f"/{TOKEN}/../{TOKEN}/nothing.json")[0] == 404
    assert ask(server, f"/{TOKEN}//manifest.json")[0] == 404  # the key "/manifest.json" does not exist
    assert ask(server, f"/{TOKEN}/releases/{release_id}/data%2Fcatalog.json")[0] == 200  # the object key itself may be encoded
    assert ask(server, f"/{TOKEN}/../research/run.json")[0] == 404


def test_a_page_can_read_that_the_token_was_refused_but_nothing_else(location, served):
    """Like the distribution's own check: the 403 has no data in it, so any page may see that it is a 403."""
    server = served(token=TOKEN, origins=("http://localhost:5173",))
    for origin in ("http://localhost:5173", "https://elsewhere.example"):
        status, headers, body = ask(server, "/wrong-token-000000000/manifest.json", headers={"Origin": origin})
        assert status == 403 and headers["access-control-allow-origin"] == "*" and b"releaseId" not in body, origin
    # ... while what the token protects still follows the list
    status, headers, _ = ask(server, f"/{TOKEN}/manifest.json", headers={"Origin": "https://elsewhere.example"})
    assert status == 200 and "access-control-allow-origin" not in headers
    status, headers, _ = ask(server, f"/{TOKEN}/nothing.json", headers={"Origin": "https://elsewhere.example"})
    assert status == 404 and "access-control-allow-origin" not in headers


def test_the_printed_address_is_one_a_browser_can_open(location, served, capsys):
    everywhere = served(host="0.0.0.0", token=TOKEN)
    port = everywhere.server_address[1]
    assert serve_data.base_url(everywhere, TOKEN) == f"http://localhost:{port}/{TOKEN}/"
    assert serve_data.base_url(served()).startswith("http://127.0.0.1:")


def test_the_token_check_is_the_same_as_the_real_distributions(location, served):
    """The same cases the CloudFront Function is run against (test_infra): 403 there is 403 here, and nothing else is."""
    root, release_id = location
    server = served(token=TOKEN)
    for path, expected in token_cases(TOKEN, release_id).items():
        status = ask(server, path)[0]
        if expected == 403:
            assert status == 403, path
        else:  # passed on to the origin, which has the object or does not
            assert status == (200 if (root / str(expected).lstrip("/")).is_file() else 404), path


def test_without_a_list_only_the_dev_servers_of_this_machine_may_read(location, served):
    """The folder holds the internal inventory: an open tool must not hand it to every site in the browser."""
    server = served()
    assert serve_data.LOCAL_ORIGINS == ("http://localhost:5173", "http://localhost:4173", "http://127.0.0.1:5173", "http://127.0.0.1:4173")
    for origin in serve_data.LOCAL_ORIGINS:
        status, headers, _ = ask(server, "/manifest.json", headers={"Origin": origin})
        assert status == 200 and headers["access-control-allow-origin"] == origin and headers["vary"] == "Origin", origin
    for origin in ("https://main.d111.amplifyapp.com", "http://localhost:3000", "http://localhost:5173.evil.example", "null", "http://LOCALHOST:5173"):
        status, headers, _ = ask(server, "/manifest.json", headers={"Origin": origin})
        assert status == 200 and "access-control-allow-origin" not in headers, origin  # the browser keeps it from the page


def test_cors_headers_follow_the_allowed_origins(location, served):
    anyone = served(origins=("*",))
    status, headers, _ = ask(anyone, "/manifest.json", headers={"Origin": "https://main.d111.amplifyapp.com"})
    assert status == 200 and headers["access-control-allow-origin"] == "*" and "vary" not in headers

    listed = served(origins=("https://main.d111.amplifyapp.com", "http://localhost:5173"))
    status, headers, _ = ask(listed, "/manifest.json", headers={"Origin": "http://localhost:5173"})
    assert status == 200 and headers["access-control-allow-origin"] == "http://localhost:5173" and headers["vary"] == "Origin"
    status, headers, _ = ask(listed, "/manifest.json", headers={"Origin": "https://elsewhere.example"})
    assert status == 200 and "access-control-allow-origin" not in headers  # the browser refuses to hand this to the page
    # errors carry the header too, so a page can tell "not found" from "blocked"
    status, headers, _ = ask(listed, "/nothing.json", headers={"Origin": "http://localhost:5173"})
    assert status == 404 and headers["access-control-allow-origin"] == "http://localhost:5173"


def test_preflight_and_writes_are_refused(location, served):
    server = served()
    status, headers, body = ask(server, "/manifest.json", method="OPTIONS",
                                headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-custom"})
    assert status == 403 and b"CORS" in body
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        status, _, body = ask(server, "/manifest.json", method=method, body=b'{"releaseId": "x"}', headers={"Content-Type": "application/json"})
        assert status == 403, method
    assert json.loads(ask(server, "/manifest.json")[2])["schema"] == R.MANIFEST_SCHEMA  # and nothing was changed


def test_what_was_asked_can_be_recorded_as_the_server_saw_it(location, served):
    """ui_smoke.py judges the UI by this: a browser does not report its preflights to the page."""
    seen: list = []
    server = served(token=TOKEN, seen=seen)
    ask(server, f"/{TOKEN}/manifest.json", headers={"Origin": "http://localhost:5173"})
    ask(server, f"/{TOKEN}/manifest.json", method="OPTIONS", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Headers": "x-custom"})
    ask(server, "/nope/manifest.json")
    ask(server, f"/{TOKEN}/nothing.json", method="HEAD")
    ask(server, f"/{TOKEN}/manifest.json", method="POST", body=b"{}")
    assert [(r["method"], r["path"], r["status"]) for r in seen] == [
        ("GET", f"/{TOKEN}/manifest.json", 200), ("OPTIONS", f"/{TOKEN}/manifest.json", 403), ("GET", "/nope/manifest.json", 403),
        ("HEAD", f"/{TOKEN}/nothing.json", 404), ("POST", f"/{TOKEN}/manifest.json", 403)]
    assert "origin" in seen[0]["headers"] and "access-control-request-headers" in seen[1]["headers"]
    assert served().RequestHandlerClass.seen is None  # nothing is kept unless it was asked for


def test_a_release_published_while_serving_is_visible_at_once(location, served):
    root, first = location
    server = served(token=TOKEN)
    store = DirStore(root)
    outcome = P.publish_proposal(store, accepted_run(), policy=P.Policy(), control=DirStore(root.parent / "pipeline"), actor="test", now=T1)
    assert outcome.status == "published" and outcome.release_id != first
    manifest = json.loads(ask(server, f"/{TOKEN}/manifest.json")[2])
    assert manifest["releaseId"] == outcome.release_id and manifest["previousReleaseId"] == first
    for release_id in (first, outcome.release_id):  # the earlier release stays readable for pages that loaded it
        assert ask(server, f"/{TOKEN}/releases/{release_id}/data/catalog.json")[0] == 200


def test_a_request_that_is_cut_off_leaves_no_traceback(location, served, capfd):
    import socket
    import struct
    import time

    server = served()
    host, port = server.server_address[:2]
    for _ in range(5):
        s = socket.create_connection((host, port), timeout=5)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close with a reset, as a reloading tab does
        s.sendall(b"GET /manifest.json HTTP/1.1\r\nHost: x\r\n")  # ... in the middle of the request
        s.close()
    time.sleep(0.3)
    assert ask(server, "/manifest.json")[0] == 200
    assert "Traceback" not in capfd.readouterr().err


def test_main_refuses_something_that_is_not_a_folder(tmp_path, capsys):
    assert serve_data.main([str(tmp_path / "missing")]) == 3
    assert "不是資料夾" in capsys.readouterr().err


def test_main_says_who_may_read_and_warns_when_other_machines_can_connect(location, monkeypatch, capsys):
    root, _ = location
    monkeypatch.setattr(serve_data.ThreadingHTTPServer, "serve_forever", lambda self, *a, **k: None)
    assert serve_data.main([str(root), "--port", "0"]) == 0
    out = capsys.readouterr().out
    assert "http://localhost:5173" in out and "任何網頁" not in out and "⚠" not in out and '{"dataBaseUrl": "http://127.0.0.1:' in out
    assert serve_data.main([str(root), "--port", "0", "--host", "0.0.0.0", "--origin", "*", "--token", TOKEN]) == 0
    out = capsys.readouterr().out
    assert "任何網頁" in out and "⚠ 正在聽 0.0.0.0" in out and f'{{"dataBaseUrl": "http://localhost:' in out and f"/{TOKEN}/" in out


# ---------------------------------------------------------------------------------------------- who is asking, and how
def raw(server, path, method="GET", host=None):
    """A request with exactly the Host header given (None: none at all)."""
    addr, port = server.server_address[:2]
    conn = http.client.HTTPConnection(addr, port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        conn.endheaders()
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def test_a_request_addressed_to_another_sites_name_is_refused(location, served):
    """DNS rebinding: a page on attacker.example re-points its own name at 127.0.0.1.  To the browser its requests are then
    same-origin - no CORS check applies - and the Host header is the only thing that still says where they came from."""
    root, release_id = location
    seen: list = []
    server = served(seen=seen)
    port = server.server_address[1]
    inventory = f"/releases/{release_id}/data/internal/platform-inventory.example.json"
    for host in (f"attacker.example:{port}", "attacker.example", f"localhost.attacker.example:{port}", f"127.0.0.1.attacker.example:{port}", "", None):
        status, headers, body = raw(server, inventory, host=host)
        assert status == 403 and b"deployments" not in body and "access-control-allow-origin" not in headers, host
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", "LOCALHOST", f"[::1]:{port}", f"192.168.1.20:{port}"):
        assert raw(server, inventory, host=host)[0] == 200, host
    assert raw(server, "/manifest.json", method="HEAD", host="attacker.example")[0] == 403
    assert [e["status"] for e in seen].count(403) == 7                                       # refusals are on record too
    # a name the operator listed is this machine's as well
    named = served(hosts=("devbox.corp.example", " Other.Example "))
    assert raw(named, "/manifest.json", host="devbox.corp.example:8787")[0] == 200 and raw(named, "/manifest.json", host="other.example")[0] == 200
    assert raw(named, "/manifest.json", host="attacker.example")[0] == 403


@pytest.mark.parametrize("method", ["PROPFIND", "TRACE", "CONNECT", "MKCOL", "LOCK", "PURGE"])
def test_a_method_nobody_expected_is_refused_like_any_other_write(location, served, method):
    """http.server would answer these itself (501, an HTML page) - and they would be missing from the record of requests."""
    seen: list = []
    server = served(seen=seen)
    status, headers, body = ask(server, "/manifest.json", method=method)
    assert status == 403 and headers["content-type"] == "application/xml" and b"MethodNotAllowed" in body
    assert [(e["method"], e["status"]) for e in seen] == [(method, 403)]


def test_origins_named_on_the_command_line_are_added_to_this_machines(location, monkeypatch, capsys):
    root, _ = location
    made = {}
    real = serve_data.make_server
    monkeypatch.setattr(serve_data, "make_server", lambda *a, **k: made.update(k) or real(*a, **k))
    monkeypatch.setattr(serve_data.ThreadingHTTPServer, "serve_forever", lambda self, *a, **k: None)
    assert serve_data.main([str(root), "--port", "0", "--origin", "https://main.d111.amplifyapp.com", "--origin", "http://localhost:5173",
                            "--allow-host", "devbox.corp.example"]) == 0
    assert made["origins"] == (*serve_data.LOCAL_ORIGINS, "https://main.d111.amplifyapp.com") and made["hosts"] == ("devbox.corp.example",)
    out = capsys.readouterr().out
    assert "http://localhost:5173" in out and "https://main.d111.amplifyapp.com" in out and "用 --origin 再加" in out


@pytest.mark.parametrize("token", ["short", "x" * 15, "x" * 65, "has space in it 0001", "token/with/slash-0001", "tökén-0000000000001"])
def test_a_token_the_real_stack_would_not_take_is_not_taken_here(location, capsys, token):
    assert serve_data.main([str(location[0]), "--port", "0", "--token", token]) == 3
    assert "--token 必須是 16–64 個" in capsys.readouterr().err


def test_the_token_rule_is_the_stacks(location):
    """One rule in two places: the template's AllowedPattern (minus its two special values) and this tool's."""
    import re

    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    template = yaml.load((serve_data.PROJECT_ROOT / "infra" / "data-plane.yaml").read_text(encoding="utf-8"), Loader=Loader)  # noqa: S506 - our own file
    pattern = template["Parameters"]["DataAccessToken"]["AllowedPattern"]
    assert pattern == "^$|^none$|^" + serve_data.TOKEN_RULE.pattern + "$"
    assert re.fullmatch(serve_data.TOKEN_RULE, TOKEN)

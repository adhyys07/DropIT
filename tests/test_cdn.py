import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="dropit-cdn-test-")
os.environ["UPLOAD_DIR"] = _tmp
os.environ["INDEX_DB"] = os.path.join(_tmp, "index.sqlite3")

import backends     # noqa: E402
import index_db     # noqa: E402

PASS = FAIL = 0

D1 = "a" * 64
D2 = "b" * 64


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {extra}")


def write_temp(data: bytes) -> str:
    fd, path = tempfile.mkstemp(dir=_tmp)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def read(self):
        return self._payload


class FakeConn:

    log = []
    script = {}

    def __init__(self):
        self.method = self.path = None
        self.headers = {}
        self.body = b""

    def putrequest(self, method, path, **_kw):
        self.method, self.path = method, path

    def putheader(self, k, v):
        self.headers[k] = v

    def endheaders(self):
        pass

    def send(self, chunk):
        self.body += chunk

    def getresponse(self):
        FakeConn.log.append({"method": self.method, "path": self.path,
                             "headers": dict(self.headers), "body": self.body})
        for prefix, reply in FakeConn.script.items():
            if self.path.startswith(prefix):
                return FakeResponse(*reply)
        return FakeResponse(404, b'{"error":"unscripted"}')

    def close(self):
        pass


def cdn(**kw):
    return backends.HackClubBackend(token="test-key", host="cdn.example",
                                    conn_factory=FakeConn, **kw)


print("\n=== 1. a missing token is refused up front ===")
try:
    backends.HackClubBackend(token="", conn_factory=FakeConn)
    check("empty token rejected", False, "no exception raised")
except RuntimeError as err:
    check("empty token rejected", "CDN_TOKEN" in str(err), str(err))

print("\n=== 2. proxying, not redirecting ===")
b = cdn()
# Downloads are relayed through DropIT rather than handed to the CDN, so
# the response headers are ours again and all three capabilities return.
check("downloads are forced", b.supports_disposition is True)
check("compression is allowed", b.supports_encoding is True)
check("expiry is enforceable", b.enforces_expiry is True)
check("local backend unchanged", backends.LocalBackend.supports_disposition
      and backends.LocalBackend.supports_encoding
      and backends.LocalBackend.enforces_expiry)
check("s3 backend unchanged", backends.S3Backend.supports_disposition
      and backends.S3Backend.supports_encoding
      and backends.S3Backend.enforces_expiry)
check("no local path for remote blobs", b.local_path(D1) is None)
check("no presigned url is handed out",
      b.presigned_url(D1, "x.bin", "application/octet-stream", False) is None)

print("\n=== 3. upload builds a valid multipart envelope ===")
index_db.init()
FakeConn.log.clear()
FakeConn.script = {"/api/v4/upload": (201, json.dumps({
    "id": "11111111-2222-3333-4444-555555555555",
    "filename": "aaaaaaaaaaaaaaaa.bin",
    "size": 7,
    "content_type": "application/octet-stream",
    "url": "https://cdn.example/11111111/aaaaaaaaaaaaaaaa.bin",
    "created_at": "2026-11-17T00:00:00Z",
}).encode())}

tmp = write_temp(b"payload")
check("starts empty", not b.exists(D1))
check("commit reports a fresh store", b.commit(D1, tmp) is True)
check("temp file cleaned up", not os.path.exists(tmp))
check("multipart staging file cleaned up", not os.path.exists(tmp + ".mp"))

req = FakeConn.log[-1]
check("posts to the v4 upload route", req["path"] == "/api/v4/upload", req["path"])
check("sends the bearer token",
      req["headers"].get("Authorization") == "Bearer test-key",
      req["headers"].get("Authorization"))
ctype = req["headers"].get("Content-Type", "")
check("declares multipart with a boundary",
      ctype.startswith("multipart/form-data; boundary=----dropit"), ctype)

boundary = ctype.split("boundary=", 1)[1]
body = req["body"]
check("content-length matches the body exactly",
      req["headers"].get("Content-Length") == str(len(body)),
      f'{req["headers"].get("Content-Length")} vs {len(body)}')
check("body opens with the boundary",
      body.startswith(("--" + boundary + backends.CRLF).encode()), body[:60])
check("names the form field file",
      b'name="file"' in body, body[:200])
check("body carries the payload", b"payload" in body)
check("body closes with the terminator",
      body.endswith((backends.CRLF + "--" + boundary + "--" + backends.CRLF).encode()),
      body[-60:])

print("\n=== 4. the upload id is kept so a delete can find it ===")
rec = index_db.get_remote(D1)
check("remote id stored", rec and rec["id"] == "11111111-2222-3333-4444-555555555555",
      rec)
check("remote url stored",
      rec and rec["url"] == "https://cdn.example/11111111/aaaaaaaaaaaaaaaa.bin", rec)
check("exists now true", b.exists(D1))
check("the url is kept internally, not published",
      b.presigned_url(D1, "report.pdf", "application/pdf", False) is None
      and index_db.get_remote(D1)["url"].startswith("https://"))

print("\n=== 5. a second upload of the same content is skipped ===")
before = len(FakeConn.log)
tmp2 = write_temp(b"payload")
check("dedup returns False", b.commit(D1, tmp2) is False)
check("no request made", len(FakeConn.log) == before, len(FakeConn.log))
check("duplicate temp removed", not os.path.exists(tmp2))

print("\n=== 6. delete calls the CDN and forgets the id ===")
FakeConn.script["/api/v4/upload/"] = (204, b"")
b.delete(D1)
req = FakeConn.log[-1]
check("issues a DELETE", req["method"] == "DELETE", req["method"])
check("targets the upload id",
      req["path"] == "/api/v4/upload/11111111-2222-3333-4444-555555555555",
      req["path"])
check("record cleared", index_db.get_remote(D1) is None)
check("exists back to false", not b.exists(D1))
before = len(FakeConn.log)
b.delete(D1)
check("deleting an unknown blob makes no call", len(FakeConn.log) == before)

# A failed delete must still drop the record -- keeping it would make the
# blob permanently undeletable -- but it must not do so silently.
index_db.set_remote(D2, "doomed-id", "https://cdn.example/doomed.bin")
FakeConn.script = {"/api/v4/upload/": (500, b'{"error":"boom"}')}
import io as _io
import contextlib as _ctx
_log = _io.StringIO()
with _ctx.redirect_stdout(_log):
    b.delete(D2)
noise = _log.getvalue()
check("a failed delete is reported", "failed (500)" in noise, noise.strip())
check("a failed delete still clears the record", index_db.get_remote(D2) is None)

print("\n=== 7. a gzip commit is stored like any other ===")
FakeConn.script = {"/api/v4/upload": (201, json.dumps({
    "id": "99999999-8888-7777-6666-555555555555",
    "url": "https://cdn.example/99999999/bbbbbbbbbbbbbbbb.bin",
}).encode())}
tmp3 = write_temp(b"pretend gzip bytes")
check("gzip commit accepted", b.commit(D2, tmp3, enc="gzip") is True)
check("stored byte for byte",
      b"pretend gzip bytes" in FakeConn.log[-1]["body"])
check("recorded for later retrieval", index_db.get_remote(D2) is not None)
FakeConn.script["/api/v4/upload/"] = (200, b'{"deleted":true}')
b.delete(D2)

print("\n=== 8. a failed upload surfaces instead of recording a ghost ===")
FakeConn.script = {"/api/v4/upload": (413, b'{"error":"file_too_large"}')}
tmp4 = write_temp(b"too big")
try:
    b.commit(D2, tmp4)
    check("error status raises", False, "no exception raised")
except RuntimeError as err:
    check("error status raises", "413" in str(err), str(err))
check("no remote record left behind", index_db.get_remote(D2) is None)
check("temp files cleaned up after failure",
      not os.path.exists(tmp4) and not os.path.exists(tmp4 + ".mp"))

FakeConn.script = {"/api/v4/upload": (201, b'{"filename":"x.bin"}')}
tmp5 = write_temp(b"no id in reply")
try:
    b.commit(D2, tmp5)
    check("missing id raises", False, "no exception raised")
except RuntimeError as err:
    check("missing id raises", "no id or url" in str(err), str(err))

print("\n=== 9. orphan detection is disabled, not wrong ===")
check("list_digests is empty", b.list_digests() == set())
index_db.set_remote(D1, "id-keep", "https://cdn.example/keep.bin")
check("sweep would propose nothing",
      sorted(b.list_digests() - index_db.all_digests()) == [])
index_db.clear_remote(D1)

print("\n=== 10. quota comes from the CDN ===")
FakeConn.script = {"/api/v4/me": (200, json.dumps({
    "quota_tier": "verified", "storage_limit": 5368709120}).encode())}
q = b.quota()
check("tier reported", q and q.get("quota_tier") == "verified", q)
check("limit reported", q and q.get("storage_limit") == 5368709120, q)
FakeConn.script = {"/api/v4/me": (401, b'{"error":"unauthorized"}')}
check("an auth failure is not fatal", b.quota() is None)

print("\n=== 11. the factory selects it by name ===")
real = backends.BACKEND
try:
    backends.BACKEND = "hackclub"
    backends.CDN_TOKEN = "test-key"
    check("hackclub selects the cdn backend",
          isinstance(backends.make(_tmp), backends.HackClubBackend))
    backends.BACKEND = "cdn"
    check("cdn is accepted as an alias",
          isinstance(backends.make(_tmp), backends.HackClubBackend))
    backends.BACKEND = "local"
    check("local still selects local",
          isinstance(backends.make(_tmp), backends.LocalBackend))
finally:
    backends.BACKEND = real


print("\n=== 12. fetch_stream asks for verbatim bytes ===")
# The read path must never let the CDN transform the bytes: a re-encoded
# response would break both Content-Length and the gzip framing.
index_db.set_remote(D1, "id-1", "https://cdn.example/id-1/abc.bin")
calls = []


class FakeUpstream:
    def __init__(self, req):
        calls.append({"url": req.full_url, "headers": dict(req.headers)})

    def read(self, n=-1):
        return b""

    def close(self):
        pass


import urllib.request as _ur  # noqa: E402

_real_urlopen = _ur.urlopen
_ur.urlopen = lambda req, timeout=None: FakeUpstream(req)
try:
    b.fetch_stream(D1)
    check("identity encoding demanded",
          calls[-1]["headers"].get("Accept-encoding") == "identity",
          calls[-1]["headers"])
    check("the cdn url is used",
          calls[-1]["url"] == "https://cdn.example/id-1/abc.bin",
          calls[-1]["url"])
    check("no range header when none was asked for",
          "Range" not in calls[-1]["headers"])

    b.fetch_stream(D1, 100, 199)
    check("closed range passed through",
          calls[-1]["headers"].get("Range") == "bytes=100-199",
          calls[-1]["headers"])

    b.fetch_stream(D1, 100, None)
    check("open-ended range passed through",
          calls[-1]["headers"].get("Range") == "bytes=100-",
          calls[-1]["headers"])

    before = len(calls)
    check("unknown blob yields no stream", b.fetch_stream("f" * 64) is None)
    check("and makes no request", len(calls) == before)
finally:
    _ur.urlopen = _real_urlopen
index_db.clear_remote(D1)
print(f"\n{'=' * 46}\n  {PASS} passed, {FAIL} failed\n{'=' * 46}")
raise SystemExit(1 if FAIL else 0)

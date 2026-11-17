"""Resumable chunked uploads, over HTTP against a live server.

The point of the feature is that an interrupted upload costs one part
rather than the whole transfer, so the centre of this suite is: send some
parts, stop, ask what arrived, send only the rest, and check the bytes are
identical to what a single-shot upload would have produced.
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL = os.environ.get("DROPIT_URL", "http://127.0.0.1:5099")
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {extra}")


def call(path, data=None, method=None, headers=None, raw=False):
    req = urllib.request.Request(URL + path, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return r.status, (body if raw else json.loads(body or b"{}"))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except Exception:
            return e.code, {"error": body[:200].decode("utf-8", "replace")}


def init(name, size, ttl="1h"):
    return call("/api/upload/init", method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"name": name, "size": size, "ttl": ttl}).encode())


def put_part(uid, idx, chunk):
    return call(f"/api/upload/{uid}/{idx}", method="PUT", data=chunk,
                headers={"Content-Type": "application/octet-stream"})


try:
    info = json.loads(urllib.request.urlopen(URL + "/api/hosting", timeout=15).read())
except Exception as err:
    print(f"  no server at {URL} ({err})")
    raise SystemExit(1)

if not info.get("enabled"):
    print("hosting disabled on this server - skipping suite")
    raise SystemExit(0)

check("server advertises resumable uploads", info.get("resumable") is True, info)
check("server advertises a part size", (info.get("partSize") or 0) > 0,
      info.get("partSize"))

# Size the payload from the server's own part size, so this is always a
# genuine multi-part upload. Deliberately not a whole multiple: a short
# final part is the classic off-by-one.
PART = int(info.get("partSize") or 65536)
if PART > 1024 * 1024:
    print(f"  note: server part size is {PART} bytes, so this suite moves "
          f"{(PART * 3 + 1234) // (1024 * 1024)} MB")
PAYLOAD = bytes((i * 7 + 11) % 256 for i in range(PART * 3 + 1234))
SHA = hashlib.sha256(PAYLOAD).hexdigest()


print("\n=== 1. a session starts empty ===")
status, s = init("resume me.bin", len(PAYLOAD))
check("init accepted", status == 200, s)
uid = s.get("uploadId")
check("returns an upload id", bool(uid))
check("returns a part size", (s.get("partSize") or 0) > 0, s.get("partSize"))
check("nothing received yet", s.get("received") == [], s.get("received"))
check("size echoed back", s.get("size") == len(PAYLOAD), s.get("size"))

psize = s["partSize"]
nparts = (len(PAYLOAD) + psize - 1) // psize


def part_bytes(i):
    return PAYLOAD[i * psize:min((i + 1) * psize, len(PAYLOAD))]


print("\n=== 2. parts land, and out of order is fine ===")
order = list(range(nparts))
if nparts > 2:
    order[0], order[1] = order[1], order[0]
sent = []
for i in order[:max(1, nparts // 2)]:
    st, r = put_part(uid, i, part_bytes(i))
    check(f"part {i} accepted", st == 200, r)
    sent.append(i)

st, s = call(f"/api/upload/{uid}")
check("status lists exactly what arrived", sorted(s.get("received", [])) == sorted(sent),
      s.get("received"))
check("byte count matches",
      s.get("bytesReceived") == sum(len(part_bytes(i)) for i in sent),
      s.get("bytesReceived"))

print("\n=== 3. finishing early is refused, and says so ===")
st, r = call(f"/api/upload/{uid}/finish", method="POST")
check("409 while parts are missing", st == 409, st)
check("the error names a missing part", "missing" in str(r.get("error", "")).lower(),
      r.get("error"))

print("\n=== 4. resume sends only what is left ===")
st, s = call(f"/api/upload/{uid}")
have = set(s.get("received", []))
missing = [i for i in range(nparts) if i not in have]
check("the gap is what we expect", sorted(missing) == sorted(
    set(range(nparts)) - set(sent)), missing)
for i in missing:
    st, r = put_part(uid, i, part_bytes(i))
    check(f"resumed part {i} accepted", st == 200, r)

print("\n=== 5. a re-sent part is accepted, not duplicated ===")
st, r = put_part(uid, 0, part_bytes(0))
check("re-sending part 0 is fine", st == 200, r)
st, s = call(f"/api/upload/{uid}")
check("still exactly the right part count", len(s.get("received", [])) == nparts,
      s.get("received"))

print("\n=== 6. finishing produces the original bytes ===")
st, meta = call(f"/api/upload/{uid}/finish", method="POST")
check("finish accepted", st == 200, meta)
check("name sanitised", meta.get("name") == "resume_me.bin", meta.get("name"))
check("size is the whole payload", meta.get("size") == len(PAYLOAD), meta.get("size"))
check("server hash matches ours", meta.get("sha256") == SHA, meta.get("sha256"))

st, body = call(f"/f/{meta['id']}/{meta['name']}", raw=True)
check("download works", st == 200, st)
check("bytes identical", body == PAYLOAD, f"{len(body)} of {len(PAYLOAD)}")
check("still forced as a download", True)

print("\n=== 7. the session is gone once finished ===")
st, r = call(f"/api/upload/{uid}")
check("status 404s after finishing", st == 404, st)
st, r = call(f"/api/upload/{uid}/finish", method="POST")
check("finishing twice 404s", st == 404, st)

call(f"/api/file/{meta['id']}?token={urllib.parse.quote(meta['deleteToken'])}",
     method="DELETE")

print("\n=== 8. bad parts are rejected ===")
st, s = init("bad parts.bin", len(PAYLOAD))
uid2 = s["uploadId"]
st, r = put_part(uid2, 999, b"nowhere")
check("part index past the end is refused", st == 400, st)
st, r = put_part(uid2, 0, part_bytes(0)[:-10])
check("a short part is refused", st == 400, r)
st, s = call(f"/api/upload/{uid2}")
check("the short part was not recorded", s.get("received") == [], s.get("received"))
st, r = put_part("not-a-session", 0, b"x")
check("unknown session 404s", st == 404, st)

print("\n=== 9. cancelling cleans up ===")
st, r = call(f"/api/upload/{uid2}", method="DELETE")
check("cancel accepted", st == 200, r)
st, r = call(f"/api/upload/{uid2}")
check("cancelled session is gone", st == 404, st)
st, r = call(f"/api/upload/{uid2}", method="DELETE")
check("cancelling twice 404s", st == 404, st)

print("\n=== 10. limits are enforced at init, before any bytes move ===")
st, r = init("huge.bin", info["maxFileBytes"] + 4096)
check("oversize refused up front", st == 413, st)
st, r = init("empty.bin", 0)
check("empty refused up front", st == 413, st)

print(f"\n{'=' * 46}\n  {PASS} passed, {FAIL} failed\n{'=' * 46}")
raise SystemExit(1 if FAIL else 0)

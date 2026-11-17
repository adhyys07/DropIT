import http.client
import os
import time
from urllib.parse import quote, urlsplit

import requests

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


def upload(data, name, ttl="1h"):
    return requests.post(
        f"{URL}/api/upload?ttl={ttl}",
        data=data,
        headers={"X-File-Name": quote(name), "Content-Type": "application/octet-stream"},
        timeout=60,
    )


def drop(meta):
    try:
        requests.delete(f"{URL}/api/file/{meta['id']}?token={meta['deleteToken']}",
                        timeout=15)
    except Exception:
        pass


info = requests.get(f"{URL}/api/hosting", timeout=15).json()
if not info.get("enabled"):
    print("hosting disabled on this server — skipping suite")
    raise SystemExit(0)

def oversize_status():
    """Offer more than the cap without allocating it.

    The server rejects on Content-Length before reading the body, so the
    declared size is what matters. Building the bytes would mean a
    multi-gigabyte allocation once the cap is raised for large files.
    """
    parts = urlsplit(URL)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=30)
    try:
        conn.putrequest("POST", "/api/upload?ttl=1h", skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("X-File-Name", "big.bin")
        conn.putheader("Content-Length", str(info["maxFileBytes"] + 4096))
        conn.endheaders()
        conn.send(b"x" * 1024)          # the body is never completed
        return conn.getresponse().status
    except Exception as err:
        print(f"  (oversize probe: {err})")
        return None
    finally:
        conn.close()


print("\n=== 1. upload and download round trip ===")
payload = bytes(range(256)) * 400                      # 102,400 bytes
r = upload(payload, "round trip.bin")
check("upload accepted", r.status_code == 200, r.text[:200])
meta = r.json() if r.ok else {}
check("returns an id", bool(meta.get("id")))
check("name sanitised", meta.get("name") == "round_trip.bin", meta.get("name"))
check("size recorded", meta.get("size") == len(payload), meta.get("size"))
check("checksum is sha256", len(meta.get("sha256", "")) == 64)

got = requests.get(meta["url"], timeout=60)
check("download works", got.status_code == 200, got.status_code)
check("bytes identical", got.content == payload,
      f"{len(got.content)} vs {len(payload)}")

print("\n=== 2. hosted files cannot execute on our origin ===")
cd = got.headers.get("Content-Disposition", "")
check("forced as attachment", "attachment" in cd, cd)
check("neutral content type",
      got.headers.get("Content-Type", "").startswith("application/octet-stream"),
      got.headers.get("Content-Type"))
check("nosniff set", got.headers.get("X-Content-Type-Options") == "nosniff")
csp = got.headers.get("Content-Security-Policy", "")
check("locked-down CSP, not the site CSP", "default-src 'none'" in csp, csp[:80])
check("sandboxed", "sandbox" in csp, csp[:80])
check("not indexable", "noindex" in got.headers.get("X-Robots-Tag", ""),
      got.headers.get("X-Robots-Tag"))

print("\n=== 3. an uploaded HTML file is never served as HTML ===")
evil = b"<script>alert(document.domain)</script>"
r2 = upload(evil, "evil.html")
check("html upload accepted", r2.status_code == 200, r2.text[:120])
d2 = r2.json()
g2 = requests.get(d2["url"], timeout=30)
check("served as a download",
      "attachment" in g2.headers.get("Content-Disposition", ""),
      g2.headers.get("Content-Disposition"))
check("content type is not text/html",
      not g2.headers.get("Content-Type", "").startswith("text/html"),
      g2.headers.get("Content-Type"))
g2i = requests.get(d2["url"] + "?inline=1", timeout=30)
check("inline refused for scriptable types",
      "attachment" in g2i.headers.get("Content-Disposition", ""),
      g2i.headers.get("Content-Disposition"))

r3 = upload(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            "evil.svg")
d3 = r3.json()
g3 = requests.get(d3["url"] + "?inline=1", timeout=30)
check("svg never inlined",
      "attachment" in g3.headers.get("Content-Disposition", ""),
      g3.headers.get("Content-Disposition"))
drop(d2)
drop(d3)

print("\n=== 4. safe media may be previewed inline ===")
png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
r4 = upload(png, "pic.png")
d4 = r4.json()
g4 = requests.get(d4["url"] + "?inline=1", timeout=30)
check("png allowed inline",
      "inline" in g4.headers.get("Content-Disposition", ""),
      g4.headers.get("Content-Disposition"))
check("png keeps its real type",
      g4.headers.get("Content-Type", "").startswith("image/png"),
      g4.headers.get("Content-Type"))
g4d = requests.get(d4["url"], timeout=30)
check("still a download without ?inline=1",
      "attachment" in g4d.headers.get("Content-Disposition", ""),
      g4d.headers.get("Content-Disposition"))
drop(d4)

print("\n=== 5. range requests work (resume and video seek) ===")
rr = requests.get(meta["url"], headers={"Range": "bytes=10-19"}, timeout=30)
check("206 returned", rr.status_code == 206, rr.status_code)
check("correct 10 bytes", rr.content == payload[10:20], rr.content[:20])

print("\n=== 6. deletion ===")
bad = requests.delete(f"{URL}/api/file/{meta['id']}?token=wrong-token", timeout=15)
check("wrong token refused", bad.status_code == 403, bad.status_code)
check("file still there", requests.get(meta["url"], timeout=30).status_code == 200)
ok = requests.delete(f"{URL}/api/file/{meta['id']}?token={meta['deleteToken']}", timeout=15)
check("right token deletes", ok.status_code == 200, ok.text[:120])
check("404 afterwards", requests.get(meta["url"], timeout=30).status_code == 404)

print("\n=== 7. deduplication shares one blob safely ===")
a = upload(b"identical bytes here", "a.txt").json()
b = upload(b"identical bytes here", "b.txt").json()
check("same checksum", a["sha256"] == b["sha256"])
check("different ids", a["id"] != b["id"])
requests.delete(f"{URL}/api/file/{a['id']}?token={a['deleteToken']}", timeout=15)
check("deleting one leaves the other readable",
      requests.get(b["url"], timeout=30).content == b"identical bytes here")
drop(b)

print("\n=== 8. limits and bad input ===")
check("limits advertised", info.get("maxFileBytes", 0) > 0, info)
check("oversize refused", oversize_status() == 413, oversize_status())
empty = upload(b"", "empty.bin")
check("empty upload refused", empty.status_code in (400, 413), empty.status_code)

print("\n=== 9. unknown and hostile ids ===")
check("404 for unknown id",
      requests.get(f"{URL}/f/doesnotexist", timeout=15).status_code == 404)
check("no path traversal",
      requests.get(f"{URL}/f/..%2F..%2Fserver.py", timeout=15).status_code in (400, 404))
check("metadata 404s too",
      requests.get(f"{URL}/api/file/nope", timeout=15).status_code == 404)

print("\n=== 10. robots.txt hides hosted files ===")
rob = requests.get(f"{URL}/robots.txt", timeout=15)
check("robots.txt served", rob.status_code == 200, rob.status_code)
check("disallows /f/", "/f/" in rob.text, rob.text[:80])

print("\n=== 11. expiry is honoured ===")
# TTL choices start at an hour, so verify the contract rather than wait.
short = upload(b"expires later", "ttl.txt", ttl="1h").json()
check("expiry roughly an hour out",
      3_000_000 < short["expiresAt"] - time.time() * 1000 < 4_000_000,
      short["expiresAt"] - time.time() * 1000)
fallback = upload(b"x", "x.txt", ttl="nonsense").json()
check("unknown ttl falls back to the default",
      fallback["ttl"] == info["defaultTtl"], fallback.get("ttl"))
drop(short)
drop(fallback)

print(f"\n{'=' * 46}\n  {PASS} passed, {FAIL} failed\n{'=' * 46}")
raise SystemExit(1 if FAIL else 0)

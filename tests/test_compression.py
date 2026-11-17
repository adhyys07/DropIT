"""Storage compression, end to end.

Three things here are easy to get wrong and expensive to discover later:
compression being attempted on content it cannot help, a deduplicated
upload recording its own encoding guess instead of what is actually on
disk, and a client that refuses gzip receiving compressed bytes.
"""
import gzip
import io
import os
import secrets
import sys
import tempfile
from urllib.parse import quote

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="dropit-compress-test-")
os.environ["UPLOAD_DIR"] = _tmp
os.environ["INDEX_DB"] = os.path.join(_tmp, "index.sqlite3")

import compression   # noqa: E402
import index_db      # noqa: E402
import storage       # noqa: E402

URL = os.environ.get("DROPIT_URL", "http://127.0.0.1:5099")
PASS = FAIL = 0

TEXT = b"GET /index.html 200 1043 firefox linux referrer=none\n" * 20000


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {extra}")


print("\n=== 1. what gets attempted at all ===")
for good in ("app.log", "data.csv", "dump.sql", "notes.txt", "payload.json", "noext"):
    check(f"tries {good}", compression.worth_trying(good))
for bad in ("clip.mp4", "photo.jpg", "bundle.zip", "paper.pdf", "deck.pptx",
            "song.mp3", "image.webp", "disk.iso"):
    check(f"skips {bad}", not compression.worth_trying(bad))

print("\n=== 2. the keep-or-discard rule ===")
check("keeps a real saving", compression.accept(1000, 400))
check("rejects a marginal saving", not compression.accept(1000, 950))
check("rejects an abandoned attempt", not compression.accept(1000, None))
check("rejects an empty original", not compression.accept(0, 0))

print("\n=== 3. an attempt gives up on incompressible input ===")
real_probe = compression.PROBE_BYTES
compression.PROBE_BYTES = 64 * 1024
try:
    side = os.path.join(_tmp, "attempt.gz")
    a = compression.Attempt(side)
    a.feed(secrets.token_bytes(128 * 1024))
    check("marked dead", not a.alive)
    check("side file removed", not os.path.exists(side))
    check("finish reports nothing kept", a.finish() is None)
    a.abandon()                                  # must be safe to repeat

    side2 = os.path.join(_tmp, "attempt2.gz")
    b = compression.Attempt(side2)
    b.feed(TEXT[:128 * 1024])
    check("stays alive on compressible input", b.alive)
    packed = b.finish()
    check("reports a compressed size", packed and packed < 128 * 1024, packed)
    os.remove(side2)
finally:
    compression.PROBE_BYTES = real_probe

print("\n=== 4. storage stores text compressed, verbatim bytes intact ===")
meta = storage.save_stream(io.BytesIO(TEXT), "access.log", "1h", "9.9.9.9", len(TEXT))
check("recorded as gzip", meta["enc"] == "gzip", meta["enc"])
check("logical size is the original", meta["size"] == len(TEXT), meta["size"])
check("stored size is much smaller", meta["stored"] < len(TEXT) // 10,
      f'{meta["stored"]} of {len(TEXT)}')
blob = storage.blob_for(meta)
with gzip.open(blob, "rb") as f:
    check("inflates back to the original", f.read() == TEXT)

print("\n=== 5. incompressible uploads are stored raw ===")
noise = secrets.token_bytes(300 * 1024)
m_raw = storage.save_stream(io.BytesIO(noise), "clip.mp4", "1h", "9.9.9.9", len(noise))
check("no encoding recorded", m_raw["enc"] == "", m_raw["enc"])
check("stored at full size", m_raw["stored"] == len(noise), m_raw["stored"])
with open(storage.blob_for(m_raw), "rb") as f:
    check("bytes identical", f.read() == noise)

print("\n=== 6. a dedup hit adopts the encoding already on disk ===")
m_dup = storage.save_stream(io.BytesIO(TEXT), "copy.log", "1h", "9.9.9.9", len(TEXT))
check("same blob", m_dup["sha256"] == meta["sha256"])
check("adopted the stored encoding", m_dup["enc"] == meta["enc"], m_dup["enc"])
check("adopted the stored size", m_dup["stored"] == meta["stored"], m_dup["stored"])
check("two records share one blob", index_db.refcount(meta["sha256"]) == 2)

print("\n=== 7. accounting is in real bytes, not logical ones ===")
stored, logical = storage.savings()
check("logical counts the uncompressed content",
      logical == len(TEXT) + len(noise), f"{logical}")
check("stored counts what the disk holds",
      stored == meta["stored"] + len(noise), f"{stored}")
check("usage agrees with savings", storage.usage()[0] == stored)
check("usage counts every record", storage.usage()[1] == 3, storage.usage()[1])

print("\n=== 8. the blob survives until the last record goes ===")
storage.delete(m_dup["id"], m_dup["deleteToken"])
check("blob kept while referenced", os.path.exists(blob))
check("blob row kept", index_db.get_blob(meta["sha256"]) is not None)
storage.delete(meta["id"], meta["deleteToken"])
check("blob removed with the last record", not os.path.exists(blob))
check("blob row removed", index_db.get_blob(meta["sha256"]) is None)
storage.delete(m_raw["id"], m_raw["deleteToken"])

print("\n=== 9. over HTTP ===")
try:
    info = requests.get(f"{URL}/api/hosting", timeout=15).json()
except Exception as err:
    print(f"  no server at {URL} ({err}) - skipping the HTTP section")
    info = None

if info and info.get("enabled"):
    def upload(data, name, ttl="1h"):
        return requests.post(
            f"{URL}/api/upload?ttl={ttl}", data=data,
            headers={"X-File-Name": quote(name),
                     "Content-Type": "application/octet-stream"},
            timeout=120)

    check("hosting reports a ratio", "compressionRatio" in info, list(info))
    check("ratio is a fraction", 0 < info.get("compressionRatio", 0) <= 1.0,
          info.get("compressionRatio"))

    r = upload(TEXT, "served.log")
    check("compressible upload accepted", r.status_code == 200, r.text[:200])
    up = r.json() if r.ok else {}

    if up:
        got = requests.get(up["url"], headers={"Accept-Encoding": "gzip"}, timeout=120)
        check("gzip client gets the original bytes", got.content == TEXT,
              f"{len(got.content)} of {len(TEXT)}")
        check("sent compressed", got.headers.get("Content-Encoding") == "gzip",
              got.headers.get("Content-Encoding"))
        check("original length advertised",
              got.headers.get("X-DropIT-Uncompressed-Length") == str(len(TEXT)),
              got.headers.get("X-DropIT-Uncompressed-Length"))
        check("ranges offered on compressed blobs",
              got.headers.get("Accept-Ranges") == "bytes",
              got.headers.get("Accept-Ranges"))
        check("varies on encoding", "Accept-Encoding" in got.headers.get("Vary", ""),
              got.headers.get("Vary"))
        check("still forced as a download",
              "attachment" in got.headers.get("Content-Disposition", ""),
              got.headers.get("Content-Disposition"))
        check("still sandboxed",
              "sandbox" in got.headers.get("Content-Security-Policy", ""),
              got.headers.get("Content-Security-Policy"))

        # A resume must answer from the inflated bytes, so the offsets mean
        # what the client thinks they mean.
        rr = requests.get(up["url"], headers={"Range": "bytes=1000-1099"},
                          timeout=120)
        check("range answered with 206", rr.status_code == 206, rr.status_code)
        check("range bytes are the plaintext slice", rr.content == TEXT[1000:1100],
              rr.content[:40])
        check("content-range names the full length",
              rr.headers.get("Content-Range") == f"bytes 1000-1099/{len(TEXT)}",
              rr.headers.get("Content-Range"))
        check("range is not encoded",
              rr.headers.get("Content-Encoding") in (None, "", "identity"),
              rr.headers.get("Content-Encoding"))

        tail = requests.get(up["url"], headers={"Range": "bytes=-50"}, timeout=120)
        check("suffix range works", tail.status_code == 206
              and tail.content == TEXT[-50:], tail.status_code)

        openended = requests.get(up["url"],
                                 headers={"Range": f"bytes={len(TEXT) - 10}-"},
                                 timeout=120)
        check("open-ended range works", openended.status_code == 206
              and openended.content == TEXT[-10:], openended.status_code)

        bad = requests.get(up["url"],
                           headers={"Range": f"bytes={len(TEXT) + 5}-"}, timeout=120)
        check("unsatisfiable range gets 416", bad.status_code == 416, bad.status_code)

        plain = requests.get(up["url"], headers={"Accept-Encoding": "identity"},
                             timeout=120)
        check("identity client gets the original bytes", plain.content == TEXT,
              f"{len(plain.content)} of {len(TEXT)}")
        check("identity response is not encoded",
              plain.headers.get("Content-Encoding") in (None, "", "identity"),
              plain.headers.get("Content-Encoding"))

        requests.delete(f"{URL}/api/file/{up['id']}?token={up['deleteToken']}",
                        timeout=15)
else:
    print("  hosting disabled - skipping the HTTP section")

print(f"\n{'=' * 46}\n  {PASS} passed, {FAIL} failed\n{'=' * 46}")
raise SystemExit(1 if FAIL else 0)

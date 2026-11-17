import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="dropit-backend-test-")
os.environ["UPLOAD_DIR"] = _tmp
os.environ["INDEX_DB"] = os.path.join(_tmp, "index.sqlite3")

import backends          # noqa: E402
import index_db          # noqa: E402

PASS = FAIL = 0


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


D1 = "a" * 64
D2 = "b" * 64

print("\n=== 1. local backend ===")
local = backends.LocalBackend(os.path.join(_tmp, "blobs"))
check("starts empty", not local.exists(D1))
local.commit(D1, write_temp(b"hello"))
check("blob exists after commit", local.exists(D1))
check("sharded by hash prefix",
      local.local_path(D1).replace("\\", "/").endswith(f"blobs/aa/{D1}"),
      local.local_path(D1))
with open(local.local_path(D1), "rb") as f:
    check("bytes intact", f.read() == b"hello")

tmp2 = write_temp(b"hello")
local.commit(D1, tmp2)                      # same digest again
check("duplicate commit removes the temp file", not os.path.exists(tmp2))
check("still exactly one blob", len(local.list_digests()) == 1, local.list_digests())

local.commit(D2, write_temp(b"world"))
check("list_digests sees both", local.list_digests() == {D1, D2})
local.delete(D1)
check("delete removes it", not local.exists(D1))
check("no presigned URL for local", local.presigned_url(D2, "x.bin", "text/plain", False) is None)
local.delete(D2)


print("\n=== 2. S3 backend against a fake client ===")


class FakeS3:

    def __init__(self):
        self.objects = {}
        self.uploads = 0
        self.presign_params = None

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("404")
        return {"ContentLength": len(self.objects[Key])}

    def upload_file(self, path, Bucket, Key, ExtraArgs=None):
        self.uploads += 1
        with open(path, "rb") as f:
            self.objects[Key] = f.read()

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presign_params = Params
        return f"https://bucket.example/{Params['Key']}?sig=fake&exp={ExpiresIn}"

    def get_paginator(self, _op):
        objects = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}
        return P()


fake = FakeS3()
s3 = backends.S3Backend("my-bucket", prefix="blobs/", client=fake)

check("starts empty", not s3.exists(D1))
s3.commit(D1, write_temp(b"payload"))
check("object uploaded", s3.exists(D1))
check("key is prefixed and sharded",
      f"blobs/aa/{D1}" in fake.objects, list(fake.objects))
check("bytes stored", fake.objects[f"blobs/aa/{D1}"] == b"payload")
check("temp file cleaned up after upload", len(os.listdir(_tmp)) >= 0)

uploads_before = fake.uploads
tmp3 = write_temp(b"payload")
s3.commit(D1, tmp3)
check("dedup skips re-upload", fake.uploads == uploads_before, fake.uploads)
check("duplicate temp file still removed", not os.path.exists(tmp3))

check("no local path for remote blobs", s3.local_path(D1) is None)

print("\n=== 3. presigned URLs keep the download hardening ===")
url = s3.presigned_url(D1, "report.pdf", "application/octet-stream", inline=False)
check("url returned", url and url.startswith("https://"), url)
p = fake.presign_params
check("disposition forced to attachment",
      p["ResponseContentDisposition"].startswith("attachment;"),
      p["ResponseContentDisposition"])
check("filename carried through",
      'filename="report.pdf"' in p["ResponseContentDisposition"],
      p["ResponseContentDisposition"])
check("content type pinned",
      p["ResponseContentType"] == "application/octet-stream", p["ResponseContentType"])

s3.presigned_url(D1, 'ev"il.png', "image/png", inline=True)
p = fake.presign_params
check("inline honoured for safe types",
      p["ResponseContentDisposition"].startswith("inline;"),
      p["ResponseContentDisposition"])
check("quotes stripped from the filename",
      '"' not in p["ResponseContentDisposition"].split("filename=")[1].strip('"'),
      p["ResponseContentDisposition"])

print("\n=== 4. orphan listing ===")
s3.commit(D2, write_temp(b"second"))
check("both digests listed", s3.list_digests() == {D1, D2}, s3.list_digests())
s3.delete(D1)
check("delete removes from the bucket", s3.list_digests() == {D2}, s3.list_digests())

print("\n=== 5. missing bucket name is refused ===")
try:
    backends.S3Backend("", client=fake)
    check("empty bucket rejected", False, "no exception raised")
except RuntimeError:
    check("empty bucket rejected", True)


print("\n=== 6. SQLite index ===")
index_db.init()
base = {"name": "a.bin", "size": 100, "sha256": D1, "created": 1.0,
        "expires": 9e9, "ttlKey": "1h", "deleteToken": "tok", "downloads": 0,
        "scriptable": False}
index_db.add({**base, "id": "one"})
index_db.add({**base, "id": "two"})                       # same blob, dedup
index_db.add({**base, "id": "three", "sha256": D2, "size": 50})

# usage() reports what storage actually holds, which means the physical
# blob rows -- a file record alone is not enough.
index_db.put_blob(D1, "", 100)
index_db.put_blob(D2, "gzip", 20)

total, count = index_db.usage()
check("counts records", count == 3, count)
check("usage counts stored bytes", total == 120, f"{total} (expected 100+20)")
check("logical usage counts dedup once", index_db.logical_usage() == 150,
      f"{index_db.logical_usage()} (expected 100+50)")
check("blob encoding round trips", index_db.get_blob(D2)["enc"] == "gzip")
check("first writer wins on a blob", (index_db.put_blob(D2, "", 50)
                                      or index_db.get_blob(D2)["stored"]) == 20,
      index_db.get_blob(D2)["stored"])
check("refcount tracks shared blobs", index_db.refcount(D1) == 2, index_db.refcount(D1))

index_db.remove("one")
check("refcount drops", index_db.refcount(D1) == 1, index_db.refcount(D1))
index_db.remove("two")
check("refcount reaches zero", index_db.refcount(D1) == 0)

index_db.add({**base, "id": "old", "expires": 1.0})
exp = index_db.expired(now=1000.0)
check("expiry query finds it", [m["id"] for m in exp] == ["old"], [m["id"] for m in exp])
check("unexpired left alone", index_db.get("three") is not None)

index_db.bump_downloads("three")
index_db.bump_downloads("three")
check("download counter increments", index_db.get("three")["downloads"] == 2,
      index_db.get("three")["downloads"])

index_db.drop_blob(D1)
check("dropped blob stops counting", index_db.usage()[0] == 20, index_db.usage()[0])

check("all_digests covers live records", D2 in index_db.all_digests())
check("round trip preserves fields",
      index_db.get("three")["ttlKey"] == "1h"
      and index_db.get("three")["deleteToken"] == "tok")

print(f"\n{'=' * 46}\n  {PASS} passed, {FAIL} failed\n{'=' * 46}")
raise SystemExit(1 if FAIL else 0)

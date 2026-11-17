import io, os, secrets, sys, urllib.request

if os.environ.get("STORAGE_BACKEND", "").lower() not in ("hackclub", "cdn"):
    sys.exit("Set STORAGE_BACKEND=hackclub and CDN_TOKEN first.")

import storage

storage.init()
print("backend:", storage.backend_name())
q = storage.remote_quota()
print("quota  :", q and (q.get("quota_tier"), q.get("storage_limit")))

payload = secrets.token_bytes(32 * 1024)
meta = storage.save_stream(io.BytesIO(payload), "probe.bin", "1h",
                           "127.0.0.1", len(payload))
print("uploaded:", meta["id"], "enc=", repr(meta["enc"]))

url = storage.download_url(meta, "application/octet-stream", False)
print("cdn url :", url)
with urllib.request.urlopen(url, timeout=120) as r:
    body = r.read()
    print("GET", r.status, len(body), "bytes")
    print("  disposition:", r.headers.get("Content-Disposition"))
assert body == payload, "bytes differ"

storage.delete(meta["id"], meta["deleteToken"])
try:
    urllib.request.urlopen(url, timeout=60)
    print("WARNING: url still resolves after delete")
except Exception as err:
    print("deleted, url now fails:", err)
import hashlib
import os
import re
import secrets
import threading
import time
import compression

import backends
import index_db

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(os.getcwd(), "uploads"))
BLOB_DIR = os.path.join(UPLOAD_DIR, "blobs")
TEMP_DIR = os.environ.get("UPLOAD_TEMP_DIR", os.path.join(UPLOAD_DIR, "tmp"))

MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", 10 * 1024 * 1024 * 1024))   # 10 GB
MAX_TOTAL_BYTES = int(os.environ.get("MAX_TOTAL_BYTES", 10 * 1024 * 1024 * 1024))  # 10 GB on disk
MAX_FILES = int(os.environ.get("MAX_FILES", 5000))

TTL_CHOICES = {"1h": 3600, "12h": 43200, "24h": 86400, "72h": 259200, "7d": 604800}
DEFAULT_TTL = "24h"

UPLOAD_WINDOW = 3600
UPLOAD_MAX_FILES = int(os.environ.get("UPLOAD_MAX_FILES", 30))
UPLOAD_MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", 10 * 1024 * 1024 * 1024))

ID_LEN = 10
_lock = threading.RLock()
_upload_log: dict[str, list[tuple[float, int]]] = {}

SCRIPTABLE = {
    ".html", ".htm", ".xhtml", ".svg", ".xml", ".js", ".mjs",
    ".pdf", ".swf", ".xht", ".xslt",
}

_backend: backends.Backend | None = None


def backend() -> backends.Backend:
    global _backend
    if _backend is None:
        _backend = backends.make(BLOB_DIR)
    return _backend


def init() -> None:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    index_db.init()
    backend()


def backend_name() -> str:
    return backend().name


def new_id() -> str:
    while True:
        fid = secrets.token_urlsafe(8)[:ID_LEN]
        if not index_db.exists(fid):
            return fid


SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def clean_name(name: str) -> str:
    name = os.path.basename(name or "").strip()
    name = SAFE_NAME.sub("_", name)
    name = name.lstrip(".") or "file"
    if len(name) > 100:
        root, ext = os.path.splitext(name)
        name = root[: 100 - len(ext)] + ext
    return name


def usage() -> tuple[int, int]:
    return index_db.usage()


def rate_limited(ip: str, incoming: int) -> str | None:
    now = time.time()
    with _lock:
        hits = [(t, n) for (t, n) in _upload_log.get(ip, []) if now - t < UPLOAD_WINDOW]
        _upload_log[ip] = hits
        if len(hits) >= UPLOAD_MAX_FILES:
            return "Too many uploads from this address in the last hour."
        if sum(n for _t, n in hits) + incoming > UPLOAD_MAX_BYTES:
            return "Hourly upload size limit reached for this address."
    return None


def note_upload(ip: str, nbytes: int) -> None:
    with _lock:
        _upload_log.setdefault(ip, []).append((time.time(), nbytes))


def prune_rate_log() -> None:
    now = time.time()
    with _lock:
        for ip in [k for k, v in _upload_log.items()
                   if not v or now - max(t for t, _n in v) > UPLOAD_WINDOW]:
            _upload_log.pop(ip, None)


class TooLarge(Exception):
    pass


class OutOfSpace(Exception):
    pass


def save_stream(stream, filename: str, ttl_key: str, ip: str,
                declared_len: int | None = None) -> dict:
    init()
    ttl = TTL_CHOICES.get(ttl_key, TTL_CHOICES[DEFAULT_TTL])

    total, count = usage()
    if count >= MAX_FILES:
        raise OutOfSpace("The server is holding its maximum number of files.")
    if declared_len and total + declared_len > MAX_TOTAL_BYTES:
        raise OutOfSpace("The server is out of storage.")

    name = clean_name(filename)
    token = secrets.token_hex(8)
    tmp = os.path.join(TEMP_DIR, f"incoming-{token}")
    attempt = (compression.Attempt(os.path.join(TEMP_DIR, f"incoming-{token}.gz"))
               if compression.worth_trying(name) and backend().supports_encoding
               else None)

    sha = hashlib.sha256()
    written = 0
    try:
        with open(tmp, "wb") as out:
            while True:
                chunk = stream.read(1024 * 256)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_FILE_BYTES:
                    gb = MAX_FILE_BYTES / (1024 ** 3)
                    cap = (f"{gb:.0f} GB" if gb >= 1
                           else f"{MAX_FILE_BYTES // (1024 * 1024)} MB")
                    raise TooLarge(f"Files are capped at {cap}.")
                if total + written > MAX_TOTAL_BYTES:
                    raise OutOfSpace("The server is out of storage.")
                sha.update(chunk)       
                out.write(chunk)
                if attempt:
                    attempt.feed(chunk)

        if written == 0:
            raise TooLarge("Empty upload.")

        digest = sha.hexdigest()
        packed = attempt.finish() if attempt else None

        if compression.accept(written, packed):
            enc, stored_bytes = "gzip", packed
            fresh = backend().commit(digest, attempt.path, enc)
            os.remove(tmp)                      
            tmp = None
        else:
            if attempt:
                attempt.abandon()               
            enc, stored_bytes = "", written
            fresh = backend().commit(digest, tmp)
            tmp = None
        attempt = None

        index_db.put_blob(digest, enc, stored_bytes)
        if not fresh:
            existing = index_db.get_blob(digest)
            if existing:
                enc, stored_bytes = existing["enc"], existing["stored"]
    finally:
        if attempt:
            attempt.abandon()
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    meta = {
        "id": new_id(),
        "name": name,
        "size": written,                 # logical size, what the user sent
        "sha256": digest,
        "created": time.time(),
        "expires": time.time() + ttl,
        "ttlKey": ttl_key if ttl_key in TTL_CHOICES else DEFAULT_TTL,
        "deleteToken": secrets.token_urlsafe(24),
        "downloads": 0,
        "scriptable": os.path.splitext(name)[1].lower() in SCRIPTABLE,
    }
    index_db.add(meta)
    note_upload(ip, written)
    meta["enc"] = enc
    meta["stored"] = stored_bytes
    return meta


def load(fid: str) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", fid or ""):
        return None
    meta = index_db.get(fid)
    if not meta:
        return None
    if time.time() > meta["expires"]:
        _purge(meta)
        return None
    blob = index_db.get_blob(meta["sha256"]) or {"enc": "", "stored": meta["size"]}
    meta["enc"] = blob["enc"]
    meta["stored"] = blob["stored"]
    return meta


def blob_for(meta: dict) -> str | None:
    return backend().local_path(meta["sha256"])

def remote_quota() -> dict | None:
    b = backend()
    return b.quota() if hasattr(b, "quota") else None


def enforces_expiry() -> bool:
    return backend().enforces_expiry


def supports_disposition() -> bool:
    return backend().supports_disposition


def savings() -> tuple[int, int]:
    stored, _count = index_db.usage()
    return stored, index_db.logical_usage()

def open_blob(meta: dict, first: int | None = None, last: int | None = None):
    return backend().fetch_stream(meta["sha256"], first, last)


def download_url(meta: dict, content_type: str, inline: bool) -> str | None:
    return backend().presigned_url(meta["sha256"], meta["name"], content_type, inline)


def note_download(fid: str) -> None:
    try:
        index_db.bump_downloads(fid)
    except Exception:
        pass


def _purge(meta: dict) -> None:
    index_db.remove(meta["id"])
    if index_db.refcount(meta["sha256"]) == 0:
        backend().delete(meta["sha256"])
        index_db.drop_blob(meta["sha256"])


def delete(fid: str, token: str) -> bool:
    meta = index_db.get(fid)
    if not meta:
        return False
    if not secrets.compare_digest(meta.get("deleteToken", ""), token or ""):
        return False
    _purge(meta)
    return True


def reap() -> int:
    init()
    removed = 0
    for meta in index_db.expired():
        try:
            _purge(meta)
            removed += 1
        except Exception:
            pass

    now = time.time()
    try:
        for f in os.listdir(TEMP_DIR):
            p = os.path.join(TEMP_DIR, f)
            try:
                if now - os.path.getmtime(p) > 3600:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass

    prune_rate_log()
    return removed


def sweep_orphans(dry_run: bool = True) -> list[str]:
    init()
    known = index_db.all_digests()
    stored = backend().list_digests()
    orphans = sorted(stored - known)
    if not dry_run:
        for d in orphans:
            backend().delete(d)
    return orphans

def _finalise(digest: str, enc: str, stored_bytes: int, fresh: bool,
              name: str, written: int, ttl_key: str, ip: str) -> dict:
    index_db.put_blob(digest, enc, stored_bytes)
    if not fresh:
        existing = index_db.get_blob(digest)
        if existing:
            enc, stored_bytes = existing["enc"], existing["stored"]

    meta = {
        "id": new_id(),
        "name": name,
        "size": written,
        "sha256": digest,
        "created": time.time(),
        "expires": time.time() + TTL_CHOICES.get(ttl_key, TTL_CHOICES[DEFAULT_TTL]),
        "ttlKey": ttl_key if ttl_key in TTL_CHOICES else DEFAULT_TTL,
        "deleteToken": secrets.token_urlsafe(24),
        "downloads": 0,
        "scriptable": os.path.splitext(name)[1].lower() in SCRIPTABLE,
    }
    index_db.add(meta)
    note_upload(ip, written)
    meta["enc"] = enc
    meta["stored"] = stored_bytes
    return meta


def save_file(path: str, filename: str, ttl_key: str, ip: str) -> dict:
    init()
    name = clean_name(filename)
    written = os.path.getsize(path)
    if written == 0:
        raise TooLarge("Empty upload.")
    if written > MAX_FILE_BYTES:
        gb = MAX_FILE_BYTES / (1024 ** 3)
        raise TooLarge(f"Files are capped at {gb:.0f} GB.")

    total, count = usage()
    if count >= MAX_FILES:
        raise OutOfSpace("The server is holding its maximum number of files.")
    if total + written > MAX_TOTAL_BYTES:
        raise OutOfSpace("The server is out of storage.")

    attempt = (compression.Attempt(path + ".gz")
               if compression.worth_trying(name) and backend().supports_encoding
               else None)
    sha = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 256)
                if not chunk:
                    break
                sha.update(chunk)
                if attempt:
                    attempt.feed(chunk)
        digest = sha.hexdigest()
        packed = attempt.finish() if attempt else None

        if compression.accept(written, packed):
            enc, stored_bytes = "gzip", packed
            fresh = backend().commit(digest, attempt.path, enc)
            os.remove(path)
        else:
            if attempt:
                attempt.abandon()
            enc, stored_bytes = "", written
            fresh = backend().commit(digest, path)
        attempt = None
    finally:
        if attempt:
            attempt.abandon()

    return _finalise(digest, enc, stored_bytes, fresh, name, written, ttl_key, ip)

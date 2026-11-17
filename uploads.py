import os
import secrets
import time

import index_db
import storage

PART_SIZE = int(os.environ.get("UPLOAD_PART_SIZE", 8 * 1024 * 1024))
SESSION_TTL = int(os.environ.get("UPLOAD_SESSION_TTL", 24 * 3600))
MAX_PARTS = 10000

def _session_dir() -> str:
    d = os.path.join(storage.TEMP_DIR, "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def path_for(uid: str) -> str:
    return os.path.join(_session_dir(), uid + ".part")

def begin(filename: str, size: int, ttl_key: str, ip: str) -> dict:
    storage.init()
    if size <= 0:
        raise storage.TooLarge("Empty upload.")
    if size > storage.MAX_FILE_BYTES:
        gb = storage.MAX_FILE_BYTES / (1024 ** 3)
        raise storage.TooLarge(f"Files are capped at {gb:.0f} GB.")

    total, count = storage.usage()
    if count >= storage.MAX_FILES:
        raise storage.OutOfSpace("The server is holding its maximum number of files.")
    if total + size > storage.MAX_TOTAL_BYTES:
        raise storage.OutOfSpace("The server is out of storage.")

    part_size = PART_SIZE
    while (size + part_size - 1) // part_size > MAX_PARTS:
        part_size *= 2        
    uid = secrets.token_urlsafe(24)
    dest = path_for(uid)
    with open(dest, "wb") as f:
        f.truncate(size)

    index_db.upload_new(uid, storage.clean_name(filename), size, part_size,
                        ttl_key, ip)
    return {"uploadId": uid, "partSize": part_size, "size": size,
            "received": [], "bytesReceived": 0}


def status(uid: str) -> dict | None:
    sess = index_db.upload_get(uid)
    if not sess:
        return None
    return {"uploadId": uid, "partSize": sess["part_size"],
            "size": sess["size"], "name": sess["name"],
            "received": index_db.upload_received(uid),
            "bytesReceived": index_db.upload_bytes(uid)}

def write_part(uid: str, idx: int, stream, declared: int | None = None) -> dict:
    sess = index_db.upload_get(uid)
    if not sess:
        raise KeyError("No such upload session.")

    part_size = sess["part_size"]
    size = sess["size"]
    nparts = (size + part_size - 1) // part_size
    if idx < 0 or idx >= nparts:
        raise ValueError(f"Part {idx} is outside this upload (0..{nparts - 1}).")

    offset = idx * part_size
    expect = min(part_size, size - offset)
    if declared is not None and declared != expect:
        raise ValueError(f"Part {idx} should be {expect} bytes, not {declared}.")

    dest = path_for(uid)
    if not os.path.exists(dest):
        raise KeyError("This upload's scratch file is gone.")

    # No lock here, deliberately. Parts occupy disjoint byte ranges, so
    # concurrent writers never overlap -- and holding a threading.Lock
    # across stream.read() deadlocks under eventlet, because a greenlet
    # blocked on a real lock cannot be rescheduled to release it.
    written = 0
    with open(dest, "r+b") as f:
        f.seek(offset)
        while written < expect:
            chunk = stream.read(min(256 * 1024, expect - written))
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    if written != expect:
        # Leave it unrecorded so the client retries this part.
        raise ValueError(f"Part {idx} arrived short: {written} of {expect}.")
    index_db.upload_part_done(uid, idx, written)
    index_db.upload_touch(uid)

    return {"index": idx, "bytes": written,
            "received": len(index_db.upload_received(uid)), "parts": nparts}

def finish(uid: str, ip: str) -> dict:
    sess = index_db.upload_get(uid)
    if not sess:
        raise KeyError("No such upload session.")

    part_size = sess["part_size"]
    nparts = (sess["size"] + part_size - 1) // part_size
    got = set(index_db.upload_received(uid))
    missing = [i for i in range(nparts) if i not in got]
    if missing:
        raise ValueError(f"{len(missing)} part(s) still missing, "
                         f"first is {missing[0]}.")

    dest = path_for(uid)
    if os.path.getsize(dest) != sess["size"]:
        raise ValueError("The assembled file is not the declared size.")

    try:
        # save_file consumes dest: no copy of the payload is made.
        meta = storage.save_file(dest, sess["name"], sess["ttl_key"], ip)
    finally:
        index_db.upload_drop(uid)
        return meta

def abort(uid: str) -> bool:
    sess = index_db.upload_get(uid)
    if not sess:
        return False
    try:
        os.remove(path_for(uid))
    except OSError:
        pass
    index_db.upload_drop(uid)
    return True

def sweep() -> int:
    dropped = 0
    for uid in index_db.uploads_stale(time.time() - SESSION_TTL):
        if abort(uid):
            dropped += 1
    return dropped
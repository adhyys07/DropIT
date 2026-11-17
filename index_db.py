import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get(
    "INDEX_DB",
    os.path.join(os.environ.get("UPLOAD_DIR", os.path.join(os.getcwd(), "uploads")),
                 "index.sqlite3"),
)

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id           TEXT PRIMARY KEY,
    name         TEXT    NOT NULL,
    size         INTEGER NOT NULL,
    sha256       TEXT    NOT NULL,
    created      REAL    NOT NULL,
    expires      REAL    NOT NULL,
    ttl_key      TEXT    NOT NULL,
    delete_token TEXT    NOT NULL,
    downloads    INTEGER NOT NULL DEFAULT 0,
    scriptable   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_expires ON files(expires);
CREATE INDEX IF NOT EXISTS idx_files_sha     ON files(sha256);

CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    enc    TEXT    NOT NULL DEFAULT '',
    stored INTEGER NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(SCHEMA)
        c.execute(
            "INSERT OR IGNORE INTO blobs (sha256, enc, stored) "
            "SELECT DISTINCT sha256, '', size FROM files"
        )
        _local.conn = c
    return c


def init() -> None:
    _conn()


def _row_to_meta(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "size": r["size"],
        "sha256": r["sha256"],
        "created": r["created"],
        "expires": r["expires"],
        "ttlKey": r["ttl_key"],
        "deleteToken": r["delete_token"],
        "downloads": r["downloads"],
        "scriptable": bool(r["scriptable"]),
    }


def add(meta: dict) -> None:
    _conn().execute(
        "INSERT INTO files (id, name, size, sha256, created, expires, ttl_key,"
        " delete_token, downloads, scriptable) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (meta["id"], meta["name"], meta["size"], meta["sha256"], meta["created"],
         meta["expires"], meta["ttlKey"], meta["deleteToken"],
         meta.get("downloads", 0), 1 if meta.get("scriptable") else 0),
    )


def get(fid: str) -> dict | None:
    r = _conn().execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
    return _row_to_meta(r) if r else None


def exists(fid: str) -> bool:
    return _conn().execute("SELECT 1 FROM files WHERE id = ?", (fid,)).fetchone() is not None


def remove(fid: str) -> None:
    _conn().execute("DELETE FROM files WHERE id = ?", (fid,))


def bump_downloads(fid: str) -> None:
    _conn().execute("UPDATE files SET downloads = downloads + 1 WHERE id = ?", (fid,))


def refcount(digest: str) -> int:
    return _conn().execute(
        "SELECT COUNT(*) AS n FROM files WHERE sha256 = ?", (digest,)).fetchone()["n"]

def put_blob(digest: str, enc: str, stored: int) -> None:
    _conn().execute(
        "INSERT OR IGNORE INTO blobs (sha256, enc, stored) VALUES (?,?,?)",
        (digest, enc or "", stored),
    )

def get_blob(digest: str) -> dict | None:
    r = _conn().execute(
        "SELECT enc, stored FROM blobs WHERE sha256 = ?", (digest,)).fetchone()
    return {"enc": r["enc"], "stored": r["stored"]} if r else None


def drop_blob(digest: str) -> None:
    _conn().execute("DELETE FROM blobs WHERE sha256 = ?", (digest,))


def usage() -> tuple[int, int]:
    row = _conn().execute(
       "SELECT COALESCE(SUM(b.stored), 0) AS b FROM blobs b "
        "WHERE EXISTS (SELECT 1 FROM files f WHERE f.sha256 = b.sha256)"
    ).fetchone()
    n = _conn().execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    return int(row["b"]), int(n)

def logical_usage() -> int:
    row = _conn().execute(
        "SELECT COALESCE(SUM(size), 0) AS b FROM "
        "(SELECT DISTINCT sha256, size FROM files)").fetchone()
    return int(row["b"])


def expired(now: float | None = None) -> list[dict]:
    now = now if now is not None else time.time()
    rows = _conn().execute("SELECT * FROM files WHERE expires < ?", (now,)).fetchall()
    return [_row_to_meta(r) for r in rows]


def all_digests() -> set[str]:
    return {r["sha256"] for r in _conn().execute("SELECT DISTINCT sha256 FROM files")}


def count() -> int:
    return _conn().execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]

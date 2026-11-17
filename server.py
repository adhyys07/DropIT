
import eventlet

eventlet.monkey_patch(socket=True, select=True)

# Load .env before importing any project module. storage, backends and
# uploads all read their configuration at import time, so loading it even
# one import later leaves STORAGE_BACKEND and CDN_TOKEN unseen.
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import gzip
import hashlib
import hmac
import os
import secrets
import threading
import time
import base64
from urllib.parse import unquote
import mimetypes

import storage   # noqa: E402
import uploads   # noqa: E402

from flask import Flask, redirect, request, send_from_directory, stream_with_context, send_file, jsonify, Response, abort
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__, static_folder="static")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  
CODE_LEN = 8                                        
DEFAULT_TTL = 30 * 60
MAX_TTL = 6 * 3600
SENDER_GRACE = 120          
MAX_RECEIVERS = 8
PBKDF2_ROUNDS = 240_000
JOIN_WINDOW = 60            
JOIN_MAX = 12               
REAP_EVERY = 15

TURN_SECRET = os.environ.get("TURN_SECRET", "")
TURN_URLS = [u.strip() for u in os.environ.get("TURN_URLS", "").split(",") if u.strip()]
STUN_URLS = [u.strip() for u in os.environ.get(
    "STUN_URLS",
    "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302"
).split(",") if u.strip()]
ICE_TTL = int(os.environ.get("ICE_TTL", 3600))

FORCE_HTTPS = os.environ.get("FORCE_HTTPS", "").lower() in ("1", "true", "yes")

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.socket.io https://cdn.jsdelivr.net "
    "https://unpkg.com https://cdnjs.cloudflare.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "object-src 'self' blob:",          
    "connect-src 'self' ws: wss:",
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])

INLINE_OK = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "video/mp4", "video/webm", "audio/mpeg", "audio/ogg", "audio/wav",
    "text/plain",
}

storage.init()

HOSTING_ENABLED = os.environ.get("HOSTING_ENABLED", "1").lower() in ("1", "true", "yes")


rooms: dict[str, dict] = {}
_lock = threading.RLock()
_join_log: dict[str, list[float]] = {}


def new_code() -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if code not in rooms:
            return code


def hash_pw(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt, dk


def verify_pw(password: str, salt: bytes, expected: bytes) -> bool:
    _, dk = hash_pw(password, salt)
    return hmac.compare_digest(dk, expected)


def rate_limited(ip: str) -> bool:
    now = time.time()
    with _lock:
        hits = [t for t in _join_log.get(ip, []) if now - t < JOIN_WINDOW]
        _join_log[ip] = hits
        return len(hits) >= JOIN_MAX


def note_failure(ip: str) -> None:
    now = time.time()
    with _lock:
        hits = [t for t in _join_log.get(ip, []) if now - t < JOIN_WINDOW]
        hits.append(now)
        _join_log[ip] = hits


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "?"


def find_room_by_sender(sid: str):
    return next((r for r in rooms.values() if r["sender_sid"] == sid), None)


def find_room_by_receiver(sid: str):
    for room in rooms.values():
        for rid, rec in room["receivers"].items():
            if rec["sid"] == sid:
                return room, rid
    return None, None


def public_room(room: dict) -> dict:
    return {
        "code": room["code"],
        "expiresAt": int(room["expires"] * 1000),
        "hasPassword": room["pw"] is not None,
        "senderName": room["sender_name"],
        "maxReceivers": room["max_receivers"],
        "maxUses": room["max_uses"],
        "uses": room["uses"],
        "requireApproval": room["require_approval"],
        "receivers": [
            {"receiverId": rid, "name": rec["name"], "approved": rec["approved"]}
            for rid, rec in room["receivers"].items()
        ],
    }


def reaper():
    while True:
        socketio.sleep(REAP_EVERY)
        now = time.time()
        with _lock:
            dead = []
            for code, room in rooms.items():
                if now > room["expires"]:
                    dead.append((code, "expired"))
                elif room["sender_gone_at"] and now - room["sender_gone_at"] > SENDER_GRACE:
                    dead.append((code, "sender-gone"))
            for code, why in dead:
                room = rooms.pop(code, None)
                if room:
                    socketio.emit("room-closed", {"code": code, "reason": why}, room=code)
                    print(f"[reap] {code} ({why})", flush=True)

            for ip in [k for k, v in _join_log.items()
                       if not v or now - max(v) > JOIN_WINDOW]:
                _join_log.pop(ip, None)
            pass
        if HOSTING_ENABLED:
            try:
                n = storage.reap()
                uploads.sweep()
                if n:
                    print(f"[reap] {n} hosted file(s) expired", flush=True)
            except Exception as err:                     
                print(f"[reap] storage error: {err}", flush=True)

def turn_credentials(label: str = "dropit") -> tuple[str, str]:
    expiry = int(time.time()) + ICE_TTL
    username = f"{expiry}:{label}"
    digest = hmac.new(TURN_SECRET.encode("utf-8"),
                      username.encode("utf-8"), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode("ascii")


@app.before_request
def require_https():
    if not FORCE_HTTPS:
        return None
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    if proto != "https":
        return redirect(request.url.replace("http://", "https://", 1), code=301)
    return None


@app.after_request
def security_headers(resp):
    if resp.headers.pop("X-DropIT-Hosted", None):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; sandbox; frame-ancestors 'none'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-site"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Permissions-Policy",
        "camera=(self), microphone=(), geolocation=(), interest-cohort=()",
    )
    if request.path == "/sw.js":
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
    if FORCE_HTTPS:
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/ice")
def ice():
    servers: list[dict] = [{"urls": STUN_URLS}] if STUN_URLS else []
    has_turn = bool(TURN_SECRET and TURN_URLS)
    if has_turn:
        username, credential = turn_credentials()
        servers.append({"urls": TURN_URLS,
                        "username": username,
                        "credential": credential})
    return {
        "iceServers": servers,
        "ttlSeconds": ICE_TTL,
        "hasTurn": has_turn,
        "note": None if has_turn else
                "No TURN relay configured — peers behind symmetric NAT "
                "(most mobile networks) will fail to connect.",
    }

@app.route("/<path:path>")
def static_files(path):
    resp = send_from_directory("static", path)
    if path.endswith(".js"):
        resp.headers["Content-Type"] = "text/javascript; charset=utf-8"
    return resp

@app.route("/api/upload/init", methods=["POST"])
def api_upload_init():
    if not HOSTING_ENABLED:
        return jsonify({"error": "File hosting is disabled on this server."}), 403

    body = request.get_json(silent=True) or {}
    try:
        size = int(body.get("size", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "A numeric size is required."}), 400

    filename = body.get("name") or "file"
    ip = client_ip()

    if (not storage.supports_disposition()
            and os.path.splitext(storage.clean_name(filename))[1].lower()
            in storage.SCRIPTABLE):
        return jsonify({"error": "This server cannot host scriptable file types "
                                 "safely. Use direct send instead."}), 415

    # Size before quota, so an oversize file is told it is too large rather
    # than being handed a misleading hourly-limit message.
    if size > storage.MAX_FILE_BYTES:
        return jsonify({"error": "File too large.",
                        "maxBytes": storage.MAX_FILE_BYTES}), 413

    reason = storage.rate_limited(ip, size)
    if reason:
        return jsonify({"error": reason}), 429

    ttl_key = (body.get("ttl") or storage.DEFAULT_TTL).lower()
    try:
        return jsonify(uploads.begin(filename, size, ttl_key, ip))
    except storage.TooLarge as err:
        return jsonify({"error": str(err)}), 413
    except storage.OutOfSpace as err:
        return jsonify({"error": str(err)}), 507


@app.route("/api/upload/<uid>", methods=["GET"])
def api_upload_status(uid):
    st = uploads.status(uid)
    return jsonify(st) if st else (jsonify({"error": "No such upload."}), 404)


@app.route("/api/upload/<uid>/<int:idx>", methods=["PUT"])
def api_upload_part(uid, idx):
    try:
        return jsonify(uploads.write_part(uid, idx, request.stream,
                                          request.content_length))
    except KeyError as err:
        # str() on a KeyError quotes the message; args[0] is the text.
        return jsonify({"error": err.args[0] if err.args else "Not found."}), 404
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    except (RuntimeError, OSError) as err:
        print(f"[upload] part {idx} of {uid} failed: {err}", flush=True)
        return jsonify({"error": "Could not store that part."}), 502


@app.route("/api/upload/<uid>/finish", methods=["POST"])
def api_upload_finish(uid):
    try:
        meta = uploads.finish(uid, client_ip())
    except KeyError as err:
        # str() on a KeyError quotes the message; args[0] is the text.
        return jsonify({"error": err.args[0] if err.args else "Not found."}), 404
    except ValueError as err:
        return jsonify({"error": str(err)}), 409      # resumable: send the rest
    except storage.TooLarge as err:
        return jsonify({"error": str(err)}), 413
    except storage.OutOfSpace as err:
        return jsonify({"error": str(err)}), 507
    except (RuntimeError, OSError) as err:
        print(f"[upload] finishing {uid} failed: {err}", flush=True)
        return jsonify({"error": "Storage is unavailable right now."}), 502

    base = request.url_root.rstrip("/")
    return jsonify({
        "id": meta["id"], "name": meta["name"], "size": meta["size"],
        "sha256": meta["sha256"],
        "url": f"{base}/f/{meta['id']}/{meta['name']}",
        "deleteUrl": f"{base}/api/file/{meta['id']}?token={meta['deleteToken']}",
        "deleteToken": meta["deleteToken"],
        "expiresAt": int(meta["expires"] * 1000),
        "ttl": meta["ttlKey"],
    })


@app.route("/api/upload/<uid>", methods=["DELETE"])
def api_upload_cancel(uid):
    return (jsonify({"cancelled": True}) if uploads.abort(uid)
            else (jsonify({"error": "No such upload."}), 404))



@app.route("/healthz")
def healthz():
    return {"ok": True, "rooms": len(rooms)}

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if not HOSTING_ENABLED:
        return jsonify({"error": "File hosting is disabled on this server."}), 403

    ip = client_ip()
    declared = request.content_length or 0
    if declared and declared > storage.MAX_FILE_BYTES:
        return jsonify({"error": "File too large.",
                        "maxBytes": storage.MAX_FILE_BYTES}), 413

    reason = storage.rate_limited(ip, declared)
    if reason:
        return jsonify({"error": reason}), 429

    ttl_key = (request.args.get("ttl") or storage.DEFAULT_TTL).lower()
    raw_name = request.headers.get("X-File-Name", "")
    try:
        filename = unquote(raw_name)
    except Exception:
        filename = raw_name
    if request.mimetype in ("multipart/form-data", "application/x-www-form-urlencoded"):
        fs = request.files.get("file")
        if fs is None:
            return jsonify({"error": "Send the file as the raw request body, or as "
                                     "a multipart field named 'file'."}), 400
        stream, filename = fs.stream, filename or fs.filename
    else:
        stream, filename = request.stream, filename or "file"

    if (not storage.supports_disposition()
            and os.path.splitext(storage.clean_name(filename))[1].lower()
            in storage.SCRIPTABLE):
        return jsonify({"error": "This server cannot host scriptable file types "
                                 "safely. Use direct send instead."}), 415

    try:
        meta = storage.save_stream(stream, filename, ttl_key, ip, declared or None)
    except storage.TooLarge as err:
        return jsonify({"error": str(err)}), 413
    except storage.OutOfSpace as err:
        return jsonify({"error": str(err)}), 507
    except (RuntimeError, OSError) as err:
        # Remote storage refused or could not be reached. That is not the
        # client's fault and not a bug here, so say so rather than 500.
        print(f"[upload] storage backend failed: {err}", flush=True)
        return jsonify({"error": "Storage is unavailable right now. "
                                 "Try again, or use direct send."}), 502
    except Exception as err:
        print(f"[upload] failed: {err}", flush=True)
        return jsonify({"error": "Upload failed."}), 500

    base = request.url_root.rstrip("/")
    return jsonify({
        "id": meta["id"],
        "name": meta["name"],
        "size": meta["size"],
        "sha256": meta["sha256"],
        "url": f"{base}/f/{meta['id']}/{meta['name']}",
        "deleteUrl": f"{base}/api/file/{meta['id']}?token={meta['deleteToken']}",
        "deleteToken": meta["deleteToken"],
        "expiresAt": int(meta["expires"] * 1000),
        "ttl": meta["ttlKey"],
    })


def parse_byte_range(header: str | None, size: int):
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].split(",")[0].strip()
    try:
        if spec.startswith("-"):
            tail = int(spec[1:])
            if tail <= 0:
                return False
            first, last = max(0, size - tail), size - 1
        else:
            head, _sep, rest = spec.partition("-")
            first = int(head)
            last = int(rest) if rest else size - 1
    except ValueError:
        return False
    if first > last or first >= size or first < 0:
        return False
    return first, min(last, size - 1)

def proxy_open(meta, first=None, last=None):
    try:
        up = storage.open_blob(meta, first, last)
    except Exception as err:
        print(f"[serve] remote storage failed for {meta['id']}: {err}", flush=True)
        abort(502)
    if up is None:
        abort(404)
    return up

def relay(up, limit=None):
    def gen():
        left = limit
        try:
            while left is None or left > 0:
                want = 256 * 1024 if left is None else min(256 * 1024, left)
                chunk = up.read(want)
                if not chunk:
                    break
                if left is not None:
                    left -= len(chunk)
                yield chunk
        finally:
            try:
                up.close()
            except Exception:
                pass
    return gen

def relay_inflated(up, first=0, limit=None):
    def gen():
        try:
            with gzip.GzipFile(fileobj=up, mode="rb") as gz:
                if first:
                    gz.seek(first)     
                left = limit
                while left is None or left > 0:
                    want = 256 * 1024 if left is None else min(256 * 1024, left)
                    chunk = gz.read(want)
                    if not chunk:
                        break
                    if left is not None:
                        left -= len(chunk)
                    yield chunk
        finally:
            try:
                up.close()
            except Exception:
                pass
    return gen


@app.route("/f/<fid>", defaults={"name": None})
@app.route("/f/<fid>/<path:name>")
def hosted_file(fid, name):
    meta = storage.load(fid)
    if not meta:
        abort(404)

    guessed = mimetypes.guess_type(meta["name"])[0] or "application/octet-stream"
    wants_inline = request.args.get("inline") == "1"
    inline = wants_inline and guessed in INLINE_OK and not meta["scriptable"]
    serve_type = guessed if inline else "application/octet-stream"

    signed = storage.download_url(meta, serve_type, inline)
    if signed:
        if meta["scriptable"] and not storage.supports_disposition():
            abort(409)
        storage.note_download(fid)
        resp = redirect(signed, code=302)
        resp.headers["X-DropIT-Hosted"] = "1"
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    path = storage.blob_for(meta)
    disposition = f'{"inline" if inline else "attachment"}; filename="{meta["name"]}"'
    serve_mime = guessed if inline else "application/octet-stream"

    if path is None:
        storage.note_download(fid)
        size = meta["size"]
        rng = parse_byte_range(request.headers.get("Range"), size)

        if rng is False:
            resp = Response(status=416)
            resp.headers["Content-Range"] = f"bytes */{size}"

        elif meta.get("enc") == "gzip":
            if rng:
                first, last = rng
                up = proxy_open(meta)
                resp = Response(
                    stream_with_context(
                        relay_inflated(up, first, last - first + 1)()),
                    status=206, mimetype=serve_mime)
                resp.headers["Content-Range"] = f"bytes {first}-{last}/{size}"
                resp.headers["Content-Length"] = str(last - first + 1)
            elif "gzip" in request.headers.get("Accept-Encoding", "").lower():
                # Relay the compressed bytes untouched: our egress is the
                # stored size and the browser does the inflating.
                up = proxy_open(meta)
                resp = Response(stream_with_context(relay(up)()),
                                mimetype=serve_mime)
                resp.headers["Content-Encoding"] = "gzip"
                resp.headers["Content-Length"] = str(meta["stored"])
                resp.headers["X-DropIT-Uncompressed-Length"] = str(size)
            else:
                up = proxy_open(meta)
                resp = Response(stream_with_context(relay_inflated(up)()),
                                mimetype=serve_mime)
                resp.headers["Content-Length"] = str(size)
            resp.headers["Vary"] = "Accept-Encoding"

        else:
            # Stored verbatim, so stored offsets are plaintext offsets and
            # the range can go straight to the backend.
            first, last = rng if rng else (None, None)
            up = proxy_open(meta, first, last)
            limit = (last - first + 1) if rng else None
            resp = Response(stream_with_context(relay(up, limit)()),
                            status=206 if rng else 200, mimetype=serve_mime)
            if rng:
                resp.headers["Content-Range"] = f"bytes {first}-{last}/{size}"
                resp.headers["Content-Length"] = str(limit)
            else:
                resp.headers["Content-Length"] = str(size)

        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["X-DropIT-Hosted"] = "1"
        resp.headers["Content-Disposition"] = disposition
        return resp

    if not os.path.exists(path):
        abort(404)

    storage.note_download(fid)

    if meta.get("enc") == "gzip":
        size = meta["size"]
        rng = parse_byte_range(request.headers.get("Range"), size)

        if rng is False:
            resp = Response(status=416)
            resp.headers["Content-Range"] = f"bytes */{size}"

        elif rng:
            first, last = rng

            def pump_range(blob_path=path, first=first, last=last):
                with gzip.open(blob_path, "rb") as f:
                    f.seek(first)
                    left = last - first + 1
                    while left > 0:
                        chunk = f.read(min(256 * 1024, left))
                        if not chunk:
                            break
                        left -= len(chunk)
                        yield chunk

            resp = Response(stream_with_context(pump_range()), status=206,
                            mimetype=serve_mime)
            resp.headers["Content-Range"] = f"bytes {first}-{last}/{size}"
            resp.headers["Content-Length"] = str(last - first + 1)

        elif "gzip" in request.headers.get("Accept-Encoding", "").lower():
            # Hand over the stored bytes untouched and let the browser inflate
            # them, so a whole-file download costs us no CPU.
            resp = send_file(path, mimetype=serve_mime,
                             conditional=False, max_age=0)
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Content-Length"] = str(meta["stored"])
            resp.headers["X-DropIT-Uncompressed-Length"] = str(size)

        else:
            # A client that will not take gzip gets it inflated on the way
            # out, streamed so a huge file never becomes a huge buffer.
            def pump(blob_path=path):
                with gzip.open(blob_path, "rb") as f:
                    while True:
                        chunk = f.read(256 * 1024)
                        if not chunk:
                            break
                        yield chunk

            resp = Response(stream_with_context(pump()), mimetype=serve_mime)
            resp.headers["Content-Length"] = str(size)

        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Vary"] = "Accept-Encoding"
    else:
        resp = send_file(path, mimetype=serve_mime, as_attachment=not inline,
                         download_name=meta["name"], conditional=True, max_age=0)

    resp.headers["X-DropIT-Hosted"] = "1"
    resp.headers["Content-Disposition"] = disposition
    return resp


@app.route("/api/file/<fid>", methods=["GET"])
def api_file_meta(fid):
    meta = storage.load(fid)
    if not meta:
        return jsonify({"error": "Not found or expired."}), 404
    return jsonify({
        "id": meta["id"], "name": meta["name"], "size": meta["size"],
        "sha256": meta["sha256"], "downloads": meta.get("downloads", 0),
        "expiresAt": int(meta["expires"] * 1000),
    })


@app.route("/api/file/<fid>", methods=["DELETE"])
def api_file_delete(fid):
    token = request.args.get("token") or request.headers.get("X-Delete-Token", "")
    if storage.delete(fid, token):
        return jsonify({"deleted": True})
    return jsonify({"error": "Wrong delete token, or the file is already gone."}), 403


@app.route("/api/hosting")
def api_hosting():
    total, count = storage.usage()
    stored, logical = storage.savings()
    out = {
        "enabled": HOSTING_ENABLED,
        "maxFileBytes": storage.MAX_FILE_BYTES,
        "ttlChoices": list(storage.TTL_CHOICES.keys()),
        "defaultTtl": storage.DEFAULT_TTL,
        "usedBytes": total,
        "totalBytes": storage.MAX_TOTAL_BYTES,
        "logicalBytes": logical,
        "compressionRatio": round(stored / logical, 3) if logical else 1.0,
        "files": count,
        "backend": storage.backend_name(),
        "resumable": True,
        "partSize" : uploads.PART_SIZE,
        "forcesDownload": storage.supports_disposition(),
        "enforcesExpiry": storage.enforces_expiry(),
    }
    q = storage.remote_quota()
    if q:
        out["remoteQuota"] = {
            "tier": q.get("quota_tier"),
            "limitBytes": q.get("storage_limit"),
        }
    return jsonify(out)



@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /f/\n", mimetype="text/plain")


@socketio.on("connect")
def on_connect():
    emit("client-id", {"clientId": request.sid})


@socketio.on("create-room")
def on_create_room(data):
    data = data or {}
    try:
        ttl = min(int(data.get("ttlSeconds") or DEFAULT_TTL), MAX_TTL)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    try:
        max_recv = min(max(1, int(data.get("maxReceivers") or 1)), MAX_RECEIVERS)
    except (TypeError, ValueError):
        max_recv = 1

    try:
        max_uses = max(0, int(data.get("maxUses") or 0))    
    except (TypeError, ValueError):
        max_uses = 0

    password = (data.get("password") or "").strip()
    salt, pw = (None, None)
    if password:
        salt, pw = hash_pw(password)                      

    code = new_code()
    resume_token = secrets.token_urlsafe(32)              
    now = time.time()

    with _lock:
        rooms[code] = {
            "code": code,
            "sender_sid": request.sid,
            "sender_name": (data.get("deviceName") or "Sender")[:40],
            "resume_token": resume_token,
            "salt": salt,
            "pw": pw,
            "created": now,
            "expires": now + ttl,
            "sender_gone_at": None,
            "max_receivers": max_recv,
            "max_uses": max_uses,
            "uses": 0,
            "require_approval": bool(data.get("requireApproval", True)),
            "receivers": {},
        }
    join_room(code)
    emit("room-created", {**public_room(rooms[code]), "resumeToken": resume_token})
    print(f"[room] created {code} ttl={ttl}s pw={bool(pw)} max={max_recv}", flush=True)


@socketio.on("touch-room")
def on_touch_room(data):
    data = data or {}
    room = rooms.get(data.get("code"))
    if not room or room["sender_sid"] != request.sid:
        return
    try:
        ttl = min(int(data.get("ttlSeconds") or DEFAULT_TTL), MAX_TTL)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    with _lock:
        room["expires"] = time.time() + ttl
    emit("room-touched", {"expiresAt": int(room["expires"] * 1000)})


@socketio.on("resume-room")
def on_resume_room(data):
    data = data or {}
    room = rooms.get(data.get("code"))
    supplied = data.get("resumeToken") or ""
    if not room or not hmac.compare_digest(room["resume_token"], supplied):
        return emit("error", {"code": "resume-failed", "message": "Cannot resume this room"})
    with _lock:
        room["sender_sid"] = request.sid
        room["sender_gone_at"] = None
    join_room(room["code"])
    emit("room-resumed", public_room(room))
    socketio.emit("sender-back", {"code": room["code"]},
                  room=room["code"], skip_sid=request.sid)


@socketio.on("join-room")
def on_join_room(data):
    data = data or {}
    ip = client_ip()
    if rate_limited(ip):
        return emit("error", {"code": "rate-limited",
                              "message": "Too many attempts. Wait a minute."})

    code = (data.get("code") or "").strip().upper()
    room = rooms.get(code)
    if not room:
        note_failure(ip)
        return emit("error", {"code": "no-room", "message": "Invalid or expired code"})
    if time.time() > room["expires"]:
        note_failure(ip)
        return emit("error", {"code": "expired", "message": "This room has expired"})

    if room["pw"] is not None:                           
        supplied = data.get("password")
        if not supplied:
            return emit("need-password", {"code": code})
        if not verify_pw(supplied, room["salt"], room["pw"]):
            note_failure(ip)
            return emit("error", {"code": "bad-password", "message": "Wrong password"})

    if room["max_uses"] and room["uses"] >= room["max_uses"]:
        return emit("error", {"code": "used-up",
                              "message": "This key has already been used."})

    with _lock:
        rejoin = False
        for old_rid, rec in list(room["receivers"].items()):
            if rec["sid"] == request.sid:
                room["receivers"].pop(old_rid, None)
                rejoin = True

        if len(room["receivers"]) >= room["max_receivers"]:
            return emit("error", {"code": "room-full", "message": "This room is full"})

        rid = secrets.token_hex(8)
        room["receivers"][rid] = {
            "sid": request.sid,
            "name": (data.get("deviceName") or "Receiver")[:40],
            "approved": not room["require_approval"],
        }
        if not rejoin:
            room["uses"] += 1
        exhausted = bool(room["max_uses"]) and room["uses"] >= room["max_uses"]
    join_room(code)

    if exhausted:
        socketio.emit("room-exhausted",
                      {"code": code, "uses": room["uses"]}, room=room["sender_sid"])

    emit("room-joined", {
        "code": code,
        "receiverId": rid,
        "senderName": room["sender_name"],
        "needsApproval": room["require_approval"],
        "expiresAt": int(room["expires"] * 1000),
    })
    socketio.emit("receiver-joined", {
        "receiverId": rid,
        "name": room["receivers"][rid]["name"],
        "approved": room["receivers"][rid]["approved"],
    }, room=room["sender_sid"])
    print(f"[room] {code} + receiver {rid}", flush=True)


@socketio.on("approve-receiver")
def on_approve_receiver(data):
    data = data or {}
    room = rooms.get(data.get("code"))
    if not room or room["sender_sid"] != request.sid:
        return
    rid = data.get("receiverId")
    rec = room["receivers"].get(rid)
    if not rec:
        return
    allow = bool(data.get("approved"))
    with _lock:
        rec["approved"] = allow
    socketio.emit("approval-result", {"approved": allow}, room=rec["sid"])
    if not allow:
        with _lock:
            room["receivers"].pop(rid, None)


@socketio.on("signal")
def on_signal(data):
    data = data or {}
    room = rooms.get(data.get("code"))
    if not room:
        return emit("error", {"code": "no-room", "message": "Room gone"})
    rid = data.get("receiverId")
    rec = room["receivers"].get(rid)
    if not rec or not rec["approved"]:
        return emit("error", {"code": "not-approved", "message": "Not approved"})

    if request.sid == room["sender_sid"]:
        target = rec["sid"]                    
    elif request.sid == rec["sid"]:
        target = room["sender_sid"]            
    else:
        return

    socketio.emit("signal", {
        "receiverId": rid,
        "signal": data.get("signal"),
        "code": room["code"],
    }, room=target)


@socketio.on("leave-room")
def on_leave_room(data):
    code = (data or {}).get("code") or ""
    _cleanup(request.sid, hard=True)
    if code:
        leave_room(code)


@socketio.on("disconnect")
def on_disconnect():
    _cleanup(request.sid, hard=False)


def _cleanup(sid: str, hard: bool):
    with _lock:
        room = find_room_by_sender(sid)
        if room:
            if hard:
                rooms.pop(room["code"], None)
                socketio.emit("room-closed",
                              {"code": room["code"], "reason": "closed"},
                              room=room["code"])
            else:
                room["sender_gone_at"] = time.time()     
                socketio.emit("sender-dropped", {
                    "code": room["code"], "graceSeconds": SENDER_GRACE,
                }, room=room["code"])
            return

        room, rid = find_room_by_receiver(sid)
        if room and rid:
            room["receivers"].pop(rid, None)
            socketio.emit("receiver-left", {"receiverId": rid}, room=room["sender_sid"])


socketio.start_background_task(reaper)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)


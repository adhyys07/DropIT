
import hashlib
import hmac
import os
import secrets
import threading
import time
import base64

from flask import Flask, redirect, request, send_from_directory
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
    "object-src 'self' blob:",          # the PDF <embed> preview
    "connect-src 'self' ws: wss:",
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])

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
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    # camera must stay allowed or the QR scanner breaks.
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


@app.route("/healthz")
def healthz():
    return {"ok": True, "rooms": len(rooms)}


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
        max_uses = max(0, int(data.get("maxUses") or 0))     # 0 = unlimited
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
        # A reconnect from the same socket is not a new use, or a
        # "burn after one use" key would die on the first dropped connection.
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
        target = rec["sid"]                    # sender -> that one receiver
    elif request.sid == rec["sid"]:
        target = room["sender_sid"]            # receiver -> sender
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


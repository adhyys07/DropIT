"""End-to-end test of the DropIT signalling protocol."""
import os
import time

import socketio

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


def mk(name):
    c = socketio.Client(logger=False, engineio_logger=False)
    c.events = []
    for ev in ["room-created", "room-joined", "receiver-joined", "need-password",
               "error", "signal", "approval-result", "room-touched",
               "room-resumed", "receiver-left", "sender-dropped", "room-closed",
               "room-exhausted"]:
        def handler(data=None, _ev=ev, _c=c):
            _c.events.append((_ev, data))
        c.on(ev, handler)
    c.connect(URL, wait_timeout=10)
    return c


def wait_for(c, ev, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        for e, d in c.events:
            if e == ev:
                return d
        time.sleep(0.05)
    return None


print("\n=== 1. open room, no password ===")
s = mk("sender")
s.emit("create-room", {"deviceName": "TestSender", "requireApproval": False,
                       "maxReceivers": 2, "ttlSeconds": 600})
room = wait_for(s, "room-created")
check("room-created received", room is not None)
code = room["code"] if room else None
check("code is 8 chars", code and len(code) == 8, f"got {code!r}")
check("code uses safe alphabet",
      code and all(ch in "ABCDEFGHJKMNPQRSTUVWXYZ23456789" for ch in code), code)
check("resumeToken present", room and len(room.get("resumeToken", "")) > 20)
check("expiresAt in future", room and room["expiresAt"] > time.time() * 1000)
check("hasPassword False", room and room["hasPassword"] is False)

print("\n=== 2. receiver joins, sender is told ===")
r1 = mk("r1")
r1.emit("join-room", {"code": code, "deviceName": "TestRecv1"})
joined = wait_for(r1, "room-joined")
check("room-joined received", joined is not None)
check("senderName relayed", joined and joined["senderName"] == "TestSender",
      joined and joined.get("senderName"))
check("needsApproval False", joined and joined["needsApproval"] is False)
rid = joined["receiverId"] if joined else None
notice = wait_for(s, "receiver-joined")
check("sender got receiver-joined", notice is not None)
check("receiver name relayed", notice and notice["name"] == "TestRecv1")

print("\n=== 3. signal relay both directions ===")
s.events.clear(); r1.events.clear()
s.emit("signal", {"code": code, "receiverId": rid, "signal": {"type": "offer", "x": 1}})
got = wait_for(r1, "signal")
check("sender -> receiver relay", got and got["signal"]["x"] == 1, got)
r1.emit("signal", {"code": code, "receiverId": rid, "signal": {"type": "answer", "y": 2}})
got = wait_for(s, "signal")
check("receiver -> sender relay", got and got["signal"]["y"] == 2, got)

print("\n=== 4. bad code rejected ===")
bad = mk("bad")
bad.emit("join-room", {"code": "ZZZZZZZZ", "deviceName": "nope"})
err = wait_for(bad, "error")
check("unknown code -> error", err and err["code"] == "no-room", err)
bad.disconnect()

print("\n=== 5. room capacity ===")
r2 = mk("r2")
r2.emit("join-room", {"code": code, "deviceName": "TestRecv2"})
check("2nd receiver allowed (max 2)", wait_for(r2, "room-joined") is not None)
r3 = mk("r3")
r3.emit("join-room", {"code": code, "deviceName": "TestRecv3"})
err = wait_for(r3, "error")
check("3rd receiver -> room-full", err and err["code"] == "room-full", err)
r3.disconnect()

print("\n=== 6. touch-room extends TTL ===")
s.events.clear()
old_exp = room["expiresAt"]
s.emit("touch-room", {"code": code, "ttlSeconds": 3600})
t = wait_for(s, "room-touched")
check("room-touched received", t is not None)
check("expiry extended", t and t["expiresAt"] > old_exp, f"{t} vs {old_exp}")

print("\n=== 7. receiver-left on disconnect ===")
s.events.clear()
r2.disconnect()
check("sender got receiver-left", wait_for(s, "receiver-left") is not None)

print("\n=== 8. password-protected room ===")
s2 = mk("sender2")
s2.emit("create-room", {"deviceName": "PwSender", "password": "hunter2",
                        "requireApproval": False})
pwroom = wait_for(s2, "room-created")
check("pw room created", pwroom is not None)
check("hasPassword True", pwroom and pwroom["hasPassword"] is True)
pcode = pwroom["code"]

pr = mk("pwrecv")
pr.emit("join-room", {"code": pcode, "deviceName": "PwRecv"})
check("no password -> need-password", wait_for(pr, "need-password") is not None)

pr.events.clear()
pr.emit("join-room", {"code": pcode, "deviceName": "PwRecv", "password": "wrong"})
err = wait_for(pr, "error")
check("wrong password -> error", err and err["code"] == "bad-password", err)

pr.events.clear()
pr.emit("join-room", {"code": pcode, "deviceName": "PwRecv", "password": "hunter2"})
check("right password -> joined", wait_for(pr, "room-joined") is not None)
pr.disconnect(); s2.disconnect()

print("\n=== 9. approval gate ===")
s3 = mk("sender3")
s3.emit("create-room", {"deviceName": "ApSender", "requireApproval": True,
                        "maxReceivers": 3})
aroom = wait_for(s3, "room-created")
acode = aroom["code"]
ar = mk("aprecv")
ar.emit("join-room", {"code": acode, "deviceName": "ApRecv"})
aj = wait_for(ar, "room-joined")
check("needsApproval True", aj and aj["needsApproval"] is True)
arid = aj["receiverId"]
note = wait_for(s3, "receiver-joined")
check("joined notice approved False", note and note["approved"] is False, note)

# relay must be refused before approval
ar.events.clear()
ar.emit("signal", {"code": acode, "receiverId": arid, "signal": {"a": 1}})
err = wait_for(ar, "error")
check("signal before approval refused", err and err["code"] == "not-approved", err)

s3.emit("approve-receiver", {"code": acode, "receiverId": arid, "approved": True})
res = wait_for(ar, "approval-result")
check("approval-result approved", res and res["approved"] is True, res)

ar.events.clear(); s3.events.clear()
ar.emit("signal", {"code": acode, "receiverId": arid, "signal": {"b": 2}})
got = wait_for(s3, "signal")
check("signal works after approval", got and got["signal"]["b"] == 2, got)

print("\n=== 10. denial removes the receiver ===")
ar2 = mk("aprecv2")
ar2.emit("join-room", {"code": acode, "deviceName": "ApRecv2"})
aj2 = wait_for(ar2, "room-joined")
check("2nd receiver joined approval room", aj2 is not None,
      f"events={ar2.events}")
arid2 = aj2["receiverId"] if aj2 else None
wait_for(s3, "receiver-joined")
s3.emit("approve-receiver", {"code": acode, "receiverId": arid2, "approved": False})
res = wait_for(ar2, "approval-result")
check("denial relayed", res and res["approved"] is False, res)
ar2.events.clear()
ar2.emit("signal", {"code": acode, "receiverId": arid2, "signal": {"c": 3}})
err = wait_for(ar2, "error")
check("denied receiver cannot signal", err is not None, err)
ar2.disconnect(); ar.disconnect(); s3.disconnect()

print("\n=== 11. resume-room with token ===")
s4 = mk("sender4")
s4.emit("create-room", {"deviceName": "ResSender", "requireApproval": False})
rroom = wait_for(s4, "room-created")
rcode, rtok = rroom["code"], rroom["resumeToken"]
s4.disconnect()
time.sleep(0.6)
s4b = mk("sender4b")
s4b.emit("resume-room", {"code": rcode, "resumeToken": rtok})
res = wait_for(s4b, "room-resumed")
check("resume with good token works", res and res["code"] == rcode, res)

s4c = mk("sender4c")
s4c.emit("resume-room", {"code": rcode, "resumeToken": "bogus-token-value"})
err = wait_for(s4c, "error")
check("resume with bad token refused", err and err["code"] == "resume-failed", err)
s4c.disconnect(); s4b.disconnect()

print("\n=== 12. burn after N uses ===")
s5 = mk("sender5")
s5.emit("create-room", {"deviceName": "BurnSender", "requireApproval": False,
                        "maxReceivers": 4, "maxUses": 2})
broom = wait_for(s5, "room-created")
check("maxUses echoed", broom and broom.get("maxUses") == 2, broom and broom.get("maxUses"))
bcode = broom["code"]

b1 = mk("burn1")
b1.emit("join-room", {"code": bcode, "deviceName": "B1"})
check("1st use allowed", wait_for(b1, "room-joined") is not None)

s5.events.clear()
b2 = mk("burn2")
b2.emit("join-room", {"code": bcode, "deviceName": "B2"})
check("2nd use allowed", wait_for(b2, "room-joined") is not None)
check("sender told the key is spent", wait_for(s5, "room-exhausted") is not None)

b3 = mk("burn3")
b3.emit("join-room", {"code": bcode, "deviceName": "B3"})
err = wait_for(b3, "error")
check("3rd use refused", err and err["code"] == "used-up", err)
for c in (b1, b2, b3, s5):
    try: c.disconnect()
    except Exception: pass

print("\n=== 13. unlimited uses is the default ===")
s6 = mk("sender6")
s6.emit("create-room", {"deviceName": "FreeSender", "requireApproval": False,
                        "maxReceivers": 3})
froom = wait_for(s6, "room-created")
check("maxUses defaults to 0", froom and froom.get("maxUses") == 0, froom and froom.get("maxUses"))
fcode = froom["code"]
ok = True
clients = []
for i in range(3):
    c = mk(f"free{i}")
    clients.append(c)
    c.emit("join-room", {"code": fcode, "deviceName": f"F{i}"})
    if wait_for(c, "room-joined") is None:
        ok = False
check("three joins all succeed with no use cap", ok)
for c in clients + [s6]:
    try: c.disconnect()
    except Exception: pass

# Runs last on purpose: it deliberately trips the rate limiter, which
# would otherwise reject every join in the tests that followed it.
print("\n=== 14. join rate limit (12 per 60s per IP) ===")
rl = mk("rl")
hits = 0
for i in range(16):
    rl.events.clear()
    rl.emit("join-room", {"code": "ZZZZZZZZ"})
    e = wait_for(rl, "error", timeout=3)
    if e and e["code"] == "rate-limited":
        hits += 1
check("rate limiting kicks in", hits > 0, f"{hits} rate-limited of 16")
rl.disconnect()



for c in (s, r1):
    try: c.disconnect()
    except Exception: pass

print(f"\n{'='*46}\n  {PASS} passed, {FAIL} failed\n{'='*46}")
raise SystemExit(1 if FAIL else 0)

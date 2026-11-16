<h1>DropIT ! - A fast and safe peer-to-peer file sharing platform</h1>

Files move directly between two browsers over WebRTC. The server only brokers
the handshake — it never sees a byte of your data.

## Features

| Area | What you get |
|---|---|
| Transfer | Streaming chunked send, pause/resume, cancel, per-file skip, chunk acknowledgements, gzip on the wire |
| Scale | No file-size limit, whole folders, up to 8 receivers at once |
| Security | CSPRNG room codes, optional password, per-device approval, room expiry, burn-after-N-uses, SHA-256 integrity, safety-word MITM check |
| Reliability | Auto-reconnect with mid-transfer resume, resume across a page reload, ICE failure diagnosis |
| Extras | Chat, clipboard sharing, device names, file previews, transfer history, installable PWA with OS share target |

## Running locally

```bash
pip install -r requirements.txt
python server.py            # http://localhost:5000
```

## Deploying

**HTTPS is required.** Browsers disable WebRTC, clipboard access, notifications
and the QR camera outside a secure context. The pages detect this and refuse to
run rather than failing in confusing ways.

```bash
export FORCE_HTTPS=1
export PORT=8080
```

### TURN relay (strongly recommended)

Without a relay roughly one connection in ten fails. Symmetric NAT — which
covers most mobile carrier networks and many corporate firewalls — cannot be
traversed by STUN alone.

```bash
export TURN_SECRET='<coturn static-auth-secret>'
export TURN_URLS='turn:turn.example.com:3478?transport=udp,turns:turn.example.com:5349?transport=tcp'
```

The server mints time-limited credentials at `/ice`, so no static password ever
reaches the browser. In `turnserver.conf` set `use-auth-secret`, a matching
`static-auth-secret`, and `denied-peer-ip` ranges covering RFC1918 space —
without those, anyone holding a credential can relay into your private network.

When no TURN is configured the UI says so plainly instead of leaving users on a
spinner.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5000` | Listen port |
| `FORCE_HTTPS` | off | Redirect HTTP to HTTPS and send HSTS |
| `TURN_SECRET` | — | coturn shared secret |
| `TURN_URLS` | — | Comma-separated TURN URLs |
| `STUN_URLS` | Google STUN | Comma-separated STUN URLs |
| `ICE_TTL` | `3600` | Lifetime of minted TURN credentials |

## Tests

```bash
pip install -r requirements-dev.txt
bash tests/run.sh
```

Three suites: static checks (DOM ids, import paths, asset refs), the signalling
protocol against a live server, and the data path moving real bytes through both
engines — including corruption, reordering, duplicate frames and resume.

## Desktop build

```bash
cargo install tauri-cli --version "^2"
cargo tauri icon static/icons/icon-512.png     # generates src-tauri/icons/
cargo tauri build
```

The desktop app launches the Python server as a child process, so Python must be
present on the machine. To ship without that dependency, compile the server with
PyInstaller and declare it under `bundle.externalBin` instead.

## Architecture

```
server.py          Flask + Socket.IO. Rooms in memory, TTL-reaped. Never sees file data.
static/js/
  protocol.js      Message types, 9-byte chunk framing, chunk-size negotiation
  send-engine.js   Streaming slice reader, ack window, pause/cancel, hashing, gzip
  recv-engine.js   Ordered reassembly, four writer tiers, integrity verification
  ice.js           TURN/STUN fetch, ICE diagnosis, connection quality
  integrity.js     Streaming SHA-256 (Merkle root over per-chunk digests)
  safety.js        Safety words derived from both DTLS fingerprints
  compress.js      Per-chunk gzip via CompressionStream
  idb-store.js     IndexedDB chunk persistence for resume-after-reload
  reconnect.js     Socket and datachannel supervisors with backoff
  preview.js       Image / video / audio / PDF / text previews
  history.js       Local transfer log
  extras.js        Device names, clipboard, notifications
```

### Wire protocol

Control messages are JSON. Payload frames are binary with a 9-byte header:
`u32 fileId`, `u32 chunkIndex`, `u8 flags` (bit 0 = gzip), big-endian. The index
lets the receiver reorder frames, discard duplicates, and tell the sender exactly
which chunk to resume from.

Chunks are hashed **before** compression on the sender and **after**
decompression on the receiver, so the integrity root is over plaintext either
way. A resumed file skips chunks and is reported as `partial` rather than
claiming a verification it cannot make.

### Writer tiers

The receiver picks the best sink available: File System Access (streams straight
to disk, unlimited size), StreamSaver, IndexedDB (survives a page reload), then
an in-memory Blob for small files.

### Safety words

Both peers hash the two DTLS certificate fingerprints — sorted, so each side
computes the same value — into four words. Matching words on both screens mean
no one is sitting between you. WebRTC is always encrypted, but the signalling
server relays the SDP, so without this check a compromised server could
substitute its own keys undetected.

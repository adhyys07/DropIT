<div align="center">
  <img src="static/icons/icon-192.png" width="88" alt="DropIT logo">
  <h1>DropIT</h1>
  <p><strong>Send a file to another device. That's it.</strong></p>
  <p>No account. No upload. No 2 GB limit. The file goes straight from your browser to theirs.</p>
</div>

---

## What You Do

1. Open DropIT and drop in your files — or a whole folder.
2. You get an 8-character key and a QR code.
3. The other person types the key, or scans it.
4. The file transfers directly between the two devices.

**Your files never touch our server.** It introduces the two browsers to each
other, then gets out of the way.

---
## Is It Actually Private?

Yes, and you can check rather than take our word for it.

WebRTC encrypts everything in transit. But our server passes the two browsers'
connection details to each other — so in principle a bad server could swap in
its own keys and read the lot.

So DropIT shows you **four words** once you're connected:

```
harbor · quartz · lantern · fern
```

Both screens must show the same four words. If they do, nobody is in the middle.
If they don't, stop. Same idea as Signal's safety numbers.

Each file also carries a SHA-256 checksum. If anything arrives damaged, you get
told — not a corrupted file that *looks* fine.

---

## What It Does

**Transfer** — Pause, resume, cancel, or skip one file mid-flight. Send to up to
8 devices at once. Folders keep their structure. Text and code get gzipped on the
way, often 50%+ smaller.

**Survives bad networks** — Drops out? It reconnects and carries on from the
exact chunk it stopped at. Reload the page mid-download and it offers to resume.

**Locks** — Optional password. Approve each device before it connects. Keys
expire, and can be set to burn after one use.

**Extras** — Live chat, clipboard sharing, previews for images, video, audio,
PDFs and text, device names, and a local transfer history. Installs as an app
and shows up in your phone's share sheet.

---

## 🛠 Tech Stack

**Transport** — WebRTC data channels via [simple-peer](https://github.com/feross/simple-peer)
**Signalling** — Python, Flask, Flask-SocketIO on eventlet
**Frontend** — Vanilla JS ES modules. No framework, no build step.
**Storage** — File System Access API → StreamSaver → IndexedDB → Blob, whichever the browser supports
**Crypto** — WebCrypto for SHA-256 and safety words; PBKDF2 for room passwords
**Desktop** — Tauri 2

---

## Running Locally

```bash
git clone https://github.com/adhyys07/DropIT.git
cd DropIT
pip install -r requirements.txt
python server.py
```

Open http://localhost:5000 in two tabs — one sender, one receiver.

### Tests

```bash
pip install -r requirements-dev.txt
bash tests/run.sh
```

Three suites: static checks, the signalling protocol against a live server, and
the data path pushing real bytes through both engines — corruption, reordering,
duplicate frames, pause, cancel and resume.

---

## Deploying

**You need HTTPS.** Browsers switch off WebRTC, clipboard, notifications and the
camera on plain HTTP. DropIT detects this and says so instead of half-working.

```bash
export FORCE_HTTPS=1
export PORT=8080
python server.py
```

### Set Up a TURN Relay

Without one, **roughly 1 in 10 transfers will never connect.** Symmetric NAT —
most mobile networks, plenty of office Wi-Fi — can't be punched through with
STUN alone.

```bash
export TURN_SECRET='<your coturn static-auth-secret>'
export TURN_URLS='turn:turn.example.com:3478?transport=udp'
```

The server mints credentials that expire in an hour, so no permanent password
is ever handed to a browser. In `turnserver.conf` set `use-auth-secret`, the
matching `static-auth-secret`, and `denied-peer-ip` ranges covering private
address space — otherwise anyone with a credential can relay into your network.

With no TURN configured the UI tells users plainly, rather than spinning forever.

## Desktop App

```bash
cargo install tauri-cli --version "^2"
cargo tauri icon static/icons/icon-512.png
cargo tauri build
```

The desktop build gives you real streaming-to-disk on every OS, not just
Chromium. It launches the Python server as a child process, so Python has to be
installed. To avoid that, compile the server with PyInstaller and declare it
under `bundle.externalBin`.

---

## For Developers

The codebase is small and has no build step — open a file and read it.

```
server.py            Flask + Socket.IO. Rooms in memory, TTL-reaped. Never sees file data.
static/js/
  protocol.js        Message types, 9-byte chunk framing, chunk-size negotiation
  send-engine.js     Streaming slice reader, ack window, pause/cancel, hashing, gzip
  recv-engine.js     Ordered reassembly, four writer tiers, integrity verification
  ice.js             TURN/STUN fetch, ICE diagnosis, connection quality
  integrity.js       Streaming SHA-256 (Merkle root over per-chunk digests)
  safety.js          Safety words from both DTLS fingerprints
  compress.js        Per-chunk gzip via CompressionStream
  idb-store.js       IndexedDB chunk persistence for resume-after-reload
  reconnect.js       Socket and datachannel supervisors with backoff
  preview.js         Image / video / audio / PDF / text previews
  history.js         Local transfer log
  extras.js          Device names, clipboard, notifications
```

### Good First Issues

- A TURN relay is the single biggest reliability win — see the deploy section.
- No browser-level E2E tests yet; Playwright would catch what the headless suites can't.
- `eventlet` is ageing. Moving to `async_mode="gevent"` is a one-line change plus a dependency swap.
- The chunk size is negotiated but never adapts to measured throughput.
- No i18n.

---
<div align="center">
  <sub>Two browsers. One pipe. No middleman.</sub>
</div>

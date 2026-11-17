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

**In direct send, your files never touch our server.** It introduces the two
browsers to each other, then gets out of the way.

---

## Two Modes

| | Direct send | Upload a link |
|---|---|---|
| Where the file goes | Browser → browser | Stored on the server |
| Both online at once | Required | No |
| Size limit | None | 200 MB (configurable) |
| Who can read it | Only you two | Anyone with the link |
| Lifetime | The session | 1 hour to 7 days, then deleted |

**Direct send** is the default and the private one. **Upload a link** is there
for when the other person is asleep — it works like catbox: you get a URL, it
expires, and you hold the only delete key.

Pick direct send for anything you'd rather we couldn't read.

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

1)**Transport** : WebRTC data channels via [simple-peer](https://github.com/feross/simple-peer)<br>
2)**Signalling**: Python, Flask, Flask-SocketIO on eventlet<br>
3)**Frontend** : Vanilla JS ES modules. No framework, no build step.<br>
4)**Storage** : File System Access API → StreamSaver → IndexedDB → Blob, whichever the browser supports<br>
5)**Crypto** : WebCrypto for SHA-256 and safety words; PBKDF2 for room passwords<br>
6)**Desktop** : Tauri 2<br>

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

Seven suites: static checks, the signalling protocol against a live server, the
data path pushing real bytes through both engines (corruption, reordering,
duplicate frames, pause, cancel, resume), hosted mode (upload, download, XSS
hardening, quotas, dedup, deletion), the storage backends plus the SQLite index
(key layout, presigned-URL hardening, orphan sweeps, reference counting), and
storage compression (skip rules, abandoned attempts, dedup encoding, and the
gzip and identity download paths), and the Hack Club CDN backend (multipart
envelope, upload-id tracking, refused gzip commits, capability flags).

The CDN suite drives a fake HTTPS connection, so it needs no API key and
spends no quota.

The backend suite drives a fake S3 client, so it runs without `boto3` and
without a bucket. It proves our logic, not Amazon's.

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

### Hosted Mode Settings

Upload-a-link stores real files, so give it a disk with room and keep the caps
tight. It is on by default.

```bash
export UPLOAD_DIR='/var/lib/dropit/uploads'   # NOT inside static/
export MAX_FILE_BYTES=10737418240             # 10 GB per file
export MAX_TOTAL_BYTES=10737418240            # 10 GB held on disk
export UPLOAD_MAX_FILES=30                    # per IP, per hour
export UPLOAD_MAX_BYTES=10737418240           # per IP, per hour
export HOSTING_ENABLED=0                      # turn the mode off entirely
```

`MAX_TOTAL_BYTES` is measured in bytes **actually on disk**, after
deduplication and compression — so a 10 GB pool holds far more than 10 GB of
text. `MAX_FILE_BYTES` is measured on the upload itself, because the raw bytes
have to fit in `UPLOAD_TEMP_DIR` while being hashed.

Note that `UPLOAD_MAX_BYTES` is checked against `Content-Length` *before* a
byte is read, so leaving it below `MAX_FILE_BYTES` rejects every large upload
with an hourly-limit message.

#### Compression

Uploads are stored gzipped when that genuinely saves space, and served with
`Content-Encoding: gzip` so the **browser** inflates them. Downloads therefore
cost no server CPU, and the user still receives the original file.

```bash
export COMPRESS_UPLOADS=1             # 0 disables it entirely
export COMPRESS_LEVEL=6               # 1 is ~4x faster for ~15% less saving
export COMPRESS_MIN_GAIN=0.10         # keep the gzip copy only if it saves 10%+
export COMPRESS_PROBE_BYTES=4194304   # give up after 4 MB of no progress
export COMPRESS_PROBE_RATIO=0.90
```

Most uploads are never compressed, by design. Video, photos, MP3s, PDFs and
`.zip`-family archives are already compressed — gzip would spend minutes of CPU
to make them marginally *larger* — so those extensions are skipped outright,
and anything else is abandoned mid-stream if its first few megabytes are not
shrinking. Text, logs, CSV, JSON and SQL dumps are where the 5-10x lives.

### Hack Club CDN

An alternative to a bucket, if you have a Hack Club account:

```bash
export STORAGE_BACKEND=hackclub
export CDN_TOKEN='<api key from https://cdn.hackclub.com>'
```

Sign in at `cdn.hackclub.com` with Hack Club OAuth and create an API key.
`POST /api/v4/revoke` revokes the key in use, which is your kill switch if it
leaks.

It is free and needs no bucket, but it gives up four things a bucket has, and
the first is a security property rather than a convenience:

* **Downloads are not forced.** The CDN serves files with its own headers, so
  `Content-Disposition: attachment` cannot be applied. An uploaded `.html` or
  `.svg` would render on `cdn.hackclub.com`. DropIT therefore **refuses
  scriptable uploads with a 415** on this backend rather than hosting
  something it cannot serve safely.
* **No compression.** There is no way to set `Content-Encoding`, so blobs are
  stored verbatim and `COMPRESS_UPLOADS` is ignored.
* **No orphan sweep.** api/v4 has no listing endpoint, so `sweep_orphans()`
  always reports nothing. A blob leaked by a crash between upload and index
  write has to be removed through the CDN dashboard.
* **Expiry cannot be enforced.** This one is measured, not theoretical.
  Files are served with `Cache-Control: public, max-age=2592000`, and after
  a successful `DELETE` the Cloudflare edge keeps answering
  `CF-Cache-Status: HIT`. **A file that was downloaded even once stays
  publicly readable for up to 30 days past its expiry**, and there is no
  purge API available to us. `/api/hosting` reports this as
  `enforcesExpiry: false`.

  So on this backend a TTL means "DropIT stops advertising the link", not
  "the file becomes unreachable". If you need expiry to actually expire,
  use a bucket: an R2 presigned URL stops working when the signature does.

The ceiling is your API key's quota tier (`unverified`, `verified`,
`functionally_unlimited`), not `MAX_TOTAL_BYTES`; `/api/hosting` reports it as
`remoteQuota`. And it is a shared nonprofit resource under *your* key — fine
for your own project's files, worth thinking hard about for a public uploader
that anyone can post to.

#### Expiry, in two layers

The reaper handles exact per-file TTLs. Add a lifecycle rule as a backstop so
nothing leaks forever if the server is down when something expires:

```bash
aws s3api put-bucket-lifecycle-configuration --bucket dropit-files \
  --lifecycle-configuration '{"Rules":[{
    "ID":"backstop-delete","Status":"Enabled",
    "Filter":{"Prefix":"blobs/"},
    "Expiration":{"Days":8},
    "AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}'
```

Eight days sits just past the longest TTL DropIT offers. The multipart rule
matters more than it looks: abandoned multipart uploads do not appear in
listings but you are still billed for them.

For blobs orphaned by a process dying mid-upload:

```bash
python -c "import storage; print(storage.sweep_orphans(dry_run=True))"
```

Run it rarely. On a bucket it is a paid `LIST` over everything.

> **Untested against a real bucket.** The S3 path has only ever run against a
> fake client in the test suite. Expect to shake out a wrinkle on your first
> live upload.

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

It does not require any setup, you can just run the python server and you are good to go

---
<div align="center">
  <sub>Two browsers. One pipe. No middleman.</sub>
</div>

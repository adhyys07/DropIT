import {
  MSG, frame, send, maxChunkFor, WINDOW_CHUNKS, HIGH_WATER, DEFAULT_CHUNK,
  FLAG_GZIP,
} from './protocol.js';
import { RollingDigest } from './integrity.js';
import { gzip, worthCompressing } from './compress.js';

const tick = (ms) => new Promise((r) => setTimeout(r, ms));

export class SendEngine {
  constructor(peer, hooks = {}) {
    this.peer   = peer;
    this.hooks  = hooks;              
    this.chunk  = DEFAULT_CHUNK;
    this.files  = [];
    this.paused = false;
    this.cancelledAll = false;
    this.cancelled = new Set();
    this._waiters = [];
    this.unacked = new Map();         
    this.sent = 0;
    this.total = 0;
    this.startedAt = 0;
    this.resumeFrom = new Map();      
    this.interrupted = false;
    this.compress = true;     // page may switch this off
    this.wireBytes = 0;       // actual bytes on the wire (post-gzip)
  }

  pause() {                                            
    if (this.paused) return;
    this.paused = true;
    send(this.peer, { type: MSG.PAUSE });
    this.hooks.onState?.('paused');
  }

  resume() {                                           
    if (!this.paused) return;
    this.paused = false;
    send(this.peer, { type: MSG.RESUME });
    this._waiters.splice(0).forEach((r) => r());
    this.hooks.onState?.('sending');
  }

  cancelFile(fileId) {                                 
    this.cancelled.add(fileId);
    send(this.peer, { type: MSG.CANCEL, fileId });
    this.resume();                                     
  }

  cancelAll() {                                        
    this.cancelledAll = true;
    send(this.peer, { type: MSG.CANCEL, all: true });
    this.resume();
    this.hooks.onState?.('cancelled');
  }

  /** Point the engine at a replacement datachannel after a reconnect. */
  rebind(peer) {
    this.peer = peer;
    this.unacked.clear();        // the old send window is meaningless now
    this.interrupted = false;
  }

  handleControl(msg) {
    if (msg.type === MSG.ACK) {                        
      for (const key of [...this.unacked.keys()]) {
        const [fid, idx] = key.split(':').map(Number);
        if (fid === msg.fileId && idx <= msg.upTo) this.unacked.delete(key);
      }
    } else if (msg.type === MSG.RESUME_STATE) {        
      this.resumeFrom.set(msg.fileId, msg.nextIndex | 0);
    } else if (msg.type === MSG.PAUSE) {
      this.paused = true; this.hooks.onState?.('paused-by-peer');
    } else if (msg.type === MSG.RESUME) {
      this.paused = false;
      this._waiters.splice(0).forEach((r) => r());
      this.hooks.onState?.('sending');
    } else if (msg.type === MSG.CANCEL) {
      if (msg.all) this.cancelledAll = true; else this.cancelled.add(msg.fileId);
      this.resume();
    } else if (msg.type === MSG.APPROVE) {             
      this._approval?.(msg);
    }
  }

  async _pauseGate() {
    while (this.paused && !this.cancelledAll) {
      await new Promise((r) => this._waiters.push(r));
    }
  }

  async _windowGate() {
    while (
      !this.cancelledAll &&
      (this.unacked.size >= WINDOW_CHUNKS || this.peer.bufferedAmount > HIGH_WATER)
    ) {
      await tick(15);
    }
  }

  async sendAll(files, { requireApproval = true } = {}) {
    this.chunk = maxChunkFor(this.peer);                     
    this.files = files.map((f, i) => {
      try { f._fileId = i + 1; } catch { /* frozen File */ }
      return {
      fileId: i + 1,
      file: f,
      name: f.name,
      size: f.size,
      type: f.type || 'application/octet-stream',
      relativePath: f.relativePath || f.webkitRelativePath || f.name,
      };
    });
    this.total = this.files.reduce((a, f) => a + f.size, 0);
    this.sent = 0;
    this.startedAt = Date.now();

    const manifest = {
      type: MSG.MANIFEST,
      chunkSize: this.chunk,
      totalBytes: this.total,
      files: this.files.map(({ fileId, name, size, type, relativePath }) =>
        ({ fileId, name, size, type, relativePath })),
    };
    send(this.peer, manifest);

    if (requireApproval) {                                    
      const ok = await new Promise((resolve) => {
        this._approval = (m) => resolve(!!m.accepted);
        send(this.peer, { type: MSG.REQ_APPROVE, totalBytes: this.total,
                          count: this.files.length });
        setTimeout(() => resolve(false), 120000);
      });
      this._approval = null;
      if (!ok) { this.hooks.onState?.('declined'); return { declined: true }; }
    }

    this.hooks.onState?.('sending');

    for (const entry of this.files) {
      if (this.cancelledAll) break;
      if (this.cancelled.has(entry.fileId)) { this.sent += entry.size; continue; }
      await this._sendOne(entry);
    }

    const cancelled = this.cancelledAll;
    send(this.peer, { type: MSG.DONE, cancelled });
    this.hooks.onState?.(cancelled ? 'cancelled' : 'done');
    this.hooks.onAllDone?.({ cancelled, bytes: this.sent, ms: Date.now() - this.startedAt });
    return { cancelled, bytes: this.sent };
  }

  async _sendOne(entry) {
    const { fileId, file, size } = entry;
    const chunkCount = Math.max(1, Math.ceil(size / this.chunk));
    const start = this.resumeFrom.get(fileId) || 0;           

    send(this.peer, {
      type: MSG.FILE_START, fileId, name: entry.name, size,
      mime: entry.type, relativePath: entry.relativePath,
      chunkSize: this.chunk, chunkCount, startIndex: start,
    });
    if (start) this.sent += Math.min(size, start * this.chunk);

    // Feature: integrity. Hash each chunk as it goes out; the root is sent
    // with FILE_END. A resume skips chunks, so the root is then unusable.
    const digest = new RollingDigest();
    if (start > 0) digest.markGap();

    // Compression is decided per file: already-compressed formats would only
    // grow. Text and code folders typically shrink by 60-80%.
    const tryCompress = this.compress && worthCompressing(entry.type, size);

    for (let index = start; index < chunkCount; index++) {
      if (this.cancelledAll || this.cancelled.has(fileId)) return;
      await this._pauseGate();
      if (this.cancelledAll || this.cancelled.has(fileId)) return;
      await this._windowGate();

      const offset = index * this.chunk;
      const blob = file.slice(offset, Math.min(offset + this.chunk, size));
      let buf;
      try {
        buf = await blob.arrayBuffer();
      } catch (err) {
        this.hooks.onError?.(new Error(`Read failed at ${entry.name} +${offset}: ${err.message}`));
        return;
      }

      // Hash the plaintext first: the receiver verifies decompressed bytes.
      await digest.update(new Uint8Array(buf));

      let body = new Uint8Array(buf);
      let flags = 0;
      if (tryCompress) {
        try {
          const z = await gzip(body);
          // Only pay the framing cost when it actually saves something.
          if (z.byteLength < body.byteLength * 0.92) { body = z; flags = FLAG_GZIP; }
        } catch { /* compression unavailable - send as-is */ }
      }

      try {
        this.peer.send(frame(fileId, index, body, flags));
      } catch (err) {
        // Channel went away mid-file: remember the spot, let the
        // supervisor rebuild, and surrender this loop.
        this.resumeFrom.set(fileId, index);
        this.interrupted = true;
        this.hooks.onError?.(err);
        return;
      }

      this.unacked.set(`${fileId}:${index}`, body.byteLength);
      this.wireBytes += body.byteLength;
      this.sent += buf.byteLength;
      this.hooks.onProgress?.(this._stats());
    }

    const deadline = Date.now() + 30000;
    while (this.unacked.size && Date.now() < deadline && !this.cancelledAll) await tick(25);

    const sha256 = await digest.hex();
    send(this.peer, { type: MSG.FILE_END, fileId, sha256, bytes: size });
    this.hooks.onFileDone?.({ ...entry, sha256 });
  }

  _stats() {
    const elapsed = Math.max(0.001, (Date.now() - this.startedAt) / 1000);
    const bps = this.sent / elapsed;
    return {
      sent: this.sent,
      total: this.total,
      percent: this.total ? Math.min(100, (this.sent / this.total) * 100) : 0,
      bps,
      etaSeconds: bps > 0 ? Math.max(0, (this.total - this.sent) / bps) : Infinity,
      inflight: this.unacked.size,
      paused: this.paused,
    };
  }
}

import { MSG, unframe, readControl, send, fmtBytes, FLAG_GZIP } from './protocol.js';
import { gunzip } from './compress.js';
import * as idb from './idb-store.js';
import { RollingDigest } from './integrity.js';


async function makeWriter(meta) {
  if (window.showSaveFilePicker && meta.size > 256 * 1024 * 1024) {
    try {
      const handle = await window.showSaveFilePicker({ suggestedName: meta.name });
      const ws = await handle.createWritable();
      return {
        kind: 'fsa',
        write: (u8, _i) => ws.write(u8),          // sequential sink
        close: async () => { await ws.close(); return { handle }; },
        abort: async () => { try { await ws.abort(); } catch {} },
      };
    } catch { }
  }

  if (window.streamSaver && meta.size > 512 * 1024 * 1024) {
    const fileStream = window.streamSaver.createWriteStream(meta.name, { size: meta.size });
    const w = fileStream.getWriter();
    return {
      kind: 'streamsaver',
      write: (u8, _i) => w.write(u8),             // sequential sink
      close: async () => { await w.close(); return {}; },
      abort: async () => { try { await w.abort(); } catch {} },
    };
  }

  // Tier 3: IndexedDB — survives a page reload, so a resume can continue.
  if (meta.sessionId && meta.size > 32 * 1024 * 1024 && globalThis.indexedDB) {
    try {
      await idb.putMeta(meta.sessionId, meta.fileId, {
        name: meta.name, size: meta.size, mime: meta.mime,
        relativePath: meta.relativePath, chunkSize: meta.chunkSize,
        chunkCount: meta.chunkCount, at: Date.now(),
      });
      return {
        kind: 'idb',
        // Key by the absolute chunk index — a local counter desynchronises
        // from the real indices as soon as a resume skips chunks.
        write: async (u8, absIndex) => {
          await idb.putChunk(meta.sessionId, meta.fileId, absIndex, u8.slice());
        },
        close: async () => {
          const blob = await idb.assemble(
            meta.sessionId, meta.fileId, meta.chunkCount, meta.mime);
          await idb.dropFile(meta.sessionId, meta.fileId);
          return { blob, url: URL.createObjectURL(blob) };
        },
        abort: async () => { try { await idb.dropFile(meta.sessionId, meta.fileId); } catch {} },
      };
    } catch { /* IDB unavailable (private mode, quota) -> fall through */ }
  }

  // Tier 4: in memory. Fine for small files.
  const parts = [];
  return {
    kind: 'blob',
    write: (u8, absIndex) => { parts[absIndex] = u8.slice(); },
    close: async () => {
      const blob = new Blob(parts.filter(Boolean),
                            { type: meta.mime || 'application/octet-stream' });
      parts.length = 0;
      return { blob, url: URL.createObjectURL(blob) };
    },
    abort: async () => { parts.length = 0; },
  };
}

export class RecvEngine {
  constructor(peer, hooks = {}) {
    this.peer = peer;
    this.hooks = hooks;   
    this.manifest = null;
    this.active = null;
    this.have = new Map();
    this.files = [];              // completed files — must exist before _finishFile
    this.paused = false;
    this.startedAt = 0;
    this.totalReceived = 0;
    this.wireBytes = 0;
    this._ackTimer = null;
    this._queue = Promise.resolve();   // serialises onData; writes are async
    this.sessionId = null;             // set by the page; enables IDB resume
  }

  pause()  { this.paused = true;  send(this.peer, { type: MSG.PAUSE }); }
  resume() { this.paused = false; send(this.peer, { type: MSG.RESUME }); }
  cancelAll() { send(this.peer, { type: MSG.CANCEL, all: true }); this._abort(); }
  cancelFile(fileId) { send(this.peer, { type: MSG.CANCEL, fileId }); }

  approve(accepted) { send(this.peer, { type: MSG.APPROVE, accepted }); }   // Feature 13

  /**
   * Rebuild `have` from chunks persisted by a previous page load, so
   * announceResumeState() asks only for the gap. Without this a reload
   * silently restarts every file from zero.
   */
  async restoreFromStorage() {
    if (!this.sessionId || !globalThis.indexedDB) return 0;
    let restored = 0;
    try {
      for (const s of await idb.listSessions()) {
        const [sid, fid] = String(s.key).split('/');
        if (sid !== this.sessionId) continue;
        const next = await idb.firstGap(sid, Number(fid), s.chunkCount || 0);
        this.have.set(Number(fid), next);
        restored++;
      }
    } catch { /* storage unavailable */ }
    return restored;
  }

  /** Feature 8: after a reconnect, tell the sender what we already hold. */
  announceResumeState() {
    for (const [fileId, nextIndex] of this.have) {
      send(this.peer, { type: MSG.RESUME_STATE, fileId, nextIndex });
    }
  }

  onData(data) {
    const msg = readControl(data);
    const job = msg
      ? () => this._onControl(msg)
      : () => this._onChunk(unframe(data));
    this._queue = this._queue.then(job).catch((err) => {
      this.hooks.onError?.(err);
    });
    return this._queue;
  }

  async _onControl(msg) {
    switch (msg.type) {
      case MSG.MANIFEST:
        this.manifest = msg;
        this.startedAt = Date.now();
        this.hooks.onManifest?.(msg);
        break;

      case MSG.REQ_APPROVE:                                 
        this.hooks.onApprovalNeeded?.({
          count: msg.count, totalBytes: msg.totalBytes,
          accept: () => this.approve(true),
          decline: () => this.approve(false),
        });
        break;

      case MSG.FILE_START: {
        if (this.active) await this.active.writer.abort();
        const writer = await makeWriter({
          name: msg.name, size: msg.size, mime: msg.mime, fileId: msg.fileId,
          relativePath: msg.relativePath, chunkSize: msg.chunkSize,
          chunkCount: msg.chunkCount, startIndex: msg.startIndex || 0,
          sessionId: this.sessionId,
        });
        const digest = new RollingDigest();
        if (msg.startIndex) digest.markGap();
        this.active = {
          meta: msg, writer, digest,
          received: (msg.startIndex || 0) * (msg.chunkSize || 0),
          nextIndex: msg.startIndex || 0,
          pending: new Map(),
        };
        this.hooks.onState?.({ phase: 'receiving', file: msg.name, writer: writer.kind });
        break;
      }

      case MSG.FILE_END:
        await this._finishFile(msg.sha256 || null);
        break;

      case MSG.PAUSE:  this.paused = true;  this.hooks.onState?.({ phase: 'paused' }); break;
      case MSG.RESUME: this.paused = false; this.hooks.onState?.({ phase: 'receiving' }); break;

      case MSG.CANCEL:
        await this._abort(msg.all ? null : msg.fileId);
        this.hooks.onState?.({ phase: 'cancelled' });
        break;

      case MSG.DONE:
        this.hooks.onState?.({ phase: msg.cancelled ? 'cancelled' : 'done', files: this.files });
        break;

      case MSG.CLIPBOARD: this.hooks.onClipboard?.(msg); break; 
      case MSG.CHAT:      this.hooks.onChat?.(msg);      break;
      case MSG.TYPING:    this.hooks.onTyping?.(msg);    break;
    }
  }

  async _onChunk({ fileId, chunkIndex, flags, payload }) {
    const a = this.active;
    if (!a || a.meta.fileId !== fileId) return;   // stray chunk from a cancelled file

    // Inflate before writing or hashing: the sender hashed the plaintext.
    if (flags & FLAG_GZIP) {
      try {
        payload = await gunzip(payload);
      } catch (err) {
        this.hooks.onError?.(new Error(`Could not decompress chunk ${chunkIndex}: ${err.message}`));
        return;
      }
    }
    this.wireBytes += payload.byteLength;

    if (chunkIndex === a.nextIndex) {
      await a.writer.write(payload, a.nextIndex);
      await a.digest.update(payload);
      a.received += payload.byteLength;
      a.nextIndex++;
      while (a.pending.has(a.nextIndex)) {
        const p = a.pending.get(a.nextIndex);
        a.pending.delete(a.nextIndex);
        await a.writer.write(p, a.nextIndex);
        await a.digest.update(p);
        a.received += p.byteLength;
        a.nextIndex++;
      }
    } else if (chunkIndex > a.nextIndex) {
      a.pending.set(chunkIndex, payload.slice());   
    } else {
      return;                                        
    }

    this.have.set(fileId, a.nextIndex);
    this.totalReceived += payload.byteLength;
    this._scheduleAck(fileId, a.nextIndex - 1);
    this.hooks.onProgress?.(this._stats());
  }

  _scheduleAck(fileId, upTo) {
    this._pendingAck = { fileId, upTo };
    if ((upTo + 1) % 8 === 0) return this._flushAck();
    if (!this._ackTimer) this._ackTimer = setTimeout(() => this._flushAck(), 100);
  }

  _flushAck() {
    clearTimeout(this._ackTimer);
    this._ackTimer = null;
    if (!this._pendingAck) return;
    send(this.peer, { type: MSG.ACK, ...this._pendingAck });
    this._pendingAck = null;
  }

  async _finishFile(expected = null) {
    const a = this.active;
    if (!a) return;
    this._flushAck();
    const out = await a.writer.close();
    const actual = await a.digest.hex();

    let integrity = 'skipped';
    if (expected && actual) integrity = (expected === actual) ? 'verified' : 'MISMATCH';
    else if (expected || actual) integrity = 'partial';

    const rec = {
      fileId: a.meta.fileId,
      name: a.meta.name,
      size: a.meta.size,
      mime: a.meta.mime,
      relativePath: a.meta.relativePath || a.meta.name,
      bytes: a.received,
      url: out.url || null,
      blob: out.blob || null,
      savedToDisk: a.writer.kind !== 'blob',
      complete: a.received >= a.meta.size,
      sha256: actual,
      integrity,
    };
    this.files.push(rec);
    this.active = null;

    try { send(this.peer, { type: MSG.HASH_RESULT, fileId: rec.fileId, integrity }); } catch {}
    this.hooks.onFile?.(rec);
    if (integrity === 'MISMATCH') this.hooks.onIntegrityFailure?.(rec);
  }

  async _abort(fileId = null) {
    if (this.active && (fileId === null || this.active.meta.fileId === fileId)) {
      await this.active.writer.abort();
      this.active = null;
    }
    if (fileId === null) this.have.clear();
  }

  _stats() {
    const a = this.active;
    const elapsed = Math.max(0.001, (Date.now() - this.startedAt) / 1000);
    const total = this.manifest?.totalBytes || a?.meta.size || 0;
    const bps = this.totalReceived / elapsed;
    return {
      received: this.totalReceived,
      total,
      percent: total ? Math.min(100, (this.totalReceived / total) * 100) : 0,
      bps,
      etaSeconds: bps > 0 ? Math.max(0, (total - this.totalReceived) / bps) : Infinity,
      file: a?.meta.name || null,
      label: `${fmtBytes(this.totalReceived)} / ${fmtBytes(total)}`,
      paused: this.paused,
    };
  }
}

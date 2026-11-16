import { MSG, unframe, readControl, send, fmtBytes } from './protocol.js';

async function makeWriter(meta) {
  // Tier 1: File System Access API — streams to disk, unlimited size.
  if (window.showSaveFilePicker && meta.size > 256 * 1024 * 1024) {
    try {
      const handle = await window.showSaveFilePicker({ suggestedName: meta.name });
      const ws = await handle.createWritable();
      return {
        kind: 'fsa',
        write: (u8) => ws.write(u8),
        close: async () => { await ws.close(); return { handle }; },
        abort: async () => { try { await ws.abort(); } catch {} },
      };
    } catch { }
  }

  // Tier 2: StreamSaver (service-worker download stream).
  if (window.streamSaver && meta.size > 512 * 1024 * 1024) {
    const fileStream = window.streamSaver.createWriteStream(meta.name, { size: meta.size });
    const w = fileStream.getWriter();
    return {
      kind: 'streamsaver',
      write: (u8) => w.write(u8),
      close: async () => { await w.close(); return {}; },
      abort: async () => { try { await w.abort(); } catch {} },
    };
  }

  const parts = [];
  return {
    kind: 'blob',
    write: (u8) => { parts.push(u8.slice()); },
    close: async () => {
      const blob = new Blob(parts, { type: meta.mime || 'application/octet-stream' });
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
    this._ackTimer = null;
    this._queue = Promise.resolve();   // serialises onData; writes are async
  }

  pause()  { this.paused = true;  send(this.peer, { type: MSG.PAUSE }); }
  resume() { this.paused = false; send(this.peer, { type: MSG.RESUME }); }
  cancelAll() { send(this.peer, { type: MSG.CANCEL, all: true }); this._abort(); }
  cancelFile(fileId) { send(this.peer, { type: MSG.CANCEL, fileId }); }

  approve(accepted) { send(this.peer, { type: MSG.APPROVE, accepted }); }   // Feature 13

  /** Feature 8: after a reconnect, tell the sender what we already hold. */
  announceResumeState() {
    for (const [fileId, nextIndex] of this.have) {
      send(this.peer, { type: MSG.RESUME_STATE, fileId, nextIndex });
    }
  }

  /**
   * Entry point for peer.on('data').
   *
   * Chunk handling awaits writer.write(), so two overlapping invocations could
   * interleave and corrupt nextIndex. Everything is therefore chained onto a
   * single promise so frames are processed strictly in arrival order.
   */
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
        const writer = await makeWriter({ name: msg.name, size: msg.size, mime: msg.mime });
        this.active = {
          meta: msg, writer,
          received: (msg.startIndex || 0) * (msg.chunkSize || 0),
          nextIndex: msg.startIndex || 0,
          pending: new Map(),
        };
        this.hooks.onState?.({ phase: 'receiving', file: msg.name, writer: writer.kind });
        break;
      }

      case MSG.FILE_END:
        await this._finishFile();
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

  async _onChunk({ fileId, chunkIndex, payload }) {
    const a = this.active;
    if (!a || a.meta.fileId !== fileId) return;   

    if (chunkIndex === a.nextIndex) {
      await a.writer.write(payload);
      a.received += payload.byteLength;
      a.nextIndex++;
      while (a.pending.has(a.nextIndex)) {
        const p = a.pending.get(a.nextIndex);
        a.pending.delete(a.nextIndex);
        await a.writer.write(p);
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

  async _finishFile() {
    const a = this.active;
    if (!a) return;
    this._flushAck();
    const out = await a.writer.close();
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
    };
    this.files.push(rec);
    this.active = null;
    this.hooks.onFile?.(rec);
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

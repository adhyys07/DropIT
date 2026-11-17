const KEY = 'dropit.upload.';
const fingerprint = (f) => `${KEY}${f.name}:${f.size}:${f.lastModified}`;

export class ResumableUpload {
  constructor(file, { ttl = '24h', parallel = 3, retries = 5 } = {}) {
    this.file = file;
    this.ttl = ttl;
    this.parallel = parallel;
    this.retries = retries;
    this.sent = 0;
    this.aborted = false;
    this.controllers = new Set();
    this.onProgress = () => {};
  }

  async start() {
    const resumed = await this._resume();
    const s = resumed || await this._init();
    this.id = s.uploadId;
    this.partSize = s.partSize;
    localStorage.setItem(fingerprint(this.file), this.id);

    const total = Math.ceil(this.file.size / this.partSize);
    const done = new Set(s.received || []);
    this.sent = (s.bytesReceived || 0);
    this.onProgress(this.sent, this.file.size, done.size, total);

    const queue = [];
    for (let i = 0; i < total; i++) if (!done.has(i)) queue.push(i);

    const worker = async () => {
      while (queue.length && !this.aborted) {
        await this._sendPart(queue.shift(), total);
      }
    };
    await Promise.all(Array.from({ length: Math.min(this.parallel, queue.length || 1) },
                                 worker));
    if (this.aborted) throw new Error('Upload cancelled.');

    const res = await fetch(`/api/upload/${this.id}/finish`, { method: 'POST' });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error((data && data.error) || `Finish failed (${res.status}).`);
    localStorage.removeItem(fingerprint(this.file));
    return data;
  }

  async _init() {
    const res = await fetch('/api/upload/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: this.file.name, size: this.file.size, ttl: this.ttl }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error((data && data.error) || `Could not start (${res.status}).`);
    return data;
  }

  async _resume() {
    const id = localStorage.getItem(fingerprint(this.file));
    if (!id) return null;
    try {
      const res = await fetch(`/api/upload/${id}`);
      if (!res.ok) { localStorage.removeItem(fingerprint(this.file)); return null; }
      const s = await res.json();
      return s.size === this.file.size ? s : null;
    } catch { return null; }
  }

  async _sendPart(index, total) {
    const start = index * this.partSize;
    const blob = this.file.slice(start, Math.min(start + this.partSize, this.file.size));

    for (let attempt = 0; attempt <= this.retries; attempt++) {
      if (this.aborted) return;
      const ctl = new AbortController();
      this.controllers.add(ctl);
      try {
        const res = await fetch(`/api/upload/${this.id}/${index}`, {
          method: 'PUT',
          body: blob,
          headers: { 'Content-Type': 'application/octet-stream' },
          signal: ctl.signal,
        });
        if (res.ok) {
          const info = await res.json();
          this.sent += blob.size;
          this.onProgress(this.sent, this.file.size, info.received, total);
          return;
        }
        if (res.status < 500 && res.status !== 408 && res.status !== 429) {
          const err = await res.json().catch(() => null);
          throw new Error((err && err.error) || `Part ${index} rejected (${res.status}).`);
        }
      } catch (err) {
        if (this.aborted) return;
        if (attempt === this.retries) throw err;
      } finally {
        this.controllers.delete(ctl);
      }
      await new Promise((r) => setTimeout(r, Math.min(16000, 500 * 2 ** attempt)
                                               + Math.random() * 400));
    }
    throw new Error(`Part ${index} failed after ${this.retries} retries.`);
  }

  abort() {
    this.aborted = true;
    this.controllers.forEach((c) => c.abort());
    if (this.id) navigator.sendBeacon?.(`/api/upload/${this.id}`) ??
      fetch(`/api/upload/${this.id}`, { method: 'DELETE' }).catch(() => {});
  }
}

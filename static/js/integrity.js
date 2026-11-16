export class RollingDigest {
  constructor() { this.parts = []; this.bytes = 0; this.gap = false; }

  async update(u8) {
    const d = await crypto.subtle.digest('SHA-256', u8);
    this.parts.push(new Uint8Array(d));
    this.bytes += u8.byteLength;
  }

  markGap() { this.gap = true; }

  async hex() {
    if (this.gap) return null;
    const flat = new Uint8Array(this.parts.length * 32);
    this.parts.forEach((p, i) => flat.set(p, i * 32));
    const root = new Uint8Array(await crypto.subtle.digest('SHA-256', flat));
    return [...root].map((b) => b.toString(16).padStart(2, '0')).join('');
  }
}

export const shortHash = (hex) => (hex ? hex.slice(0, 8) + '…' + hex.slice(-4) : 'n/a');

/* Data-path test: SendEngine -> RecvEngine over a fake peer pair.
   Verifies byte-exact delivery, framing, acks, pause/resume, cancel, resume-state. */

import { webcrypto } from 'node:crypto';

// ---- browser shims the engines touch -------------------------------------
const objectUrls = new Map();
globalThis.window = {};                       // recv-engine probes window.showSaveFilePicker
globalThis.URL.createObjectURL = (blob) => {
  const id = 'blob:' + Math.random().toString(36).slice(2);
  objectUrls.set(id, blob);
  return id;
};
globalThis.URL.revokeObjectURL = (id) => objectUrls.delete(id);
if (!globalThis.crypto) globalThis.crypto = webcrypto;

const JS = new URL('../static/js/', import.meta.url);
const { SendEngine } = await import(new URL('send-engine.js', JS));
const { RecvEngine } = await import(new URL('recv-engine.js', JS));

let PASS = 0, FAIL = 0;
const check = (name, cond, extra = '') => {
  if (cond) { PASS++; console.log(`  PASS  ${name}`); }
  else { FAIL++; console.log(`  FAIL  ${name} ${extra}`); }
};

/** Minimal in-process peer pair mimicking simple-peer's API. */
function peerPair({ dropFirst = null, latencyMs = 0, drainBytesPerTick = 0,
                    corruptNthPayload = null } = {}) {
  const mk = () => ({
    _h: {}, bufferedAmount: 0, _pc: null, sent: 0,
    on(ev, fn) { (this._h[ev] ||= []).push(fn); },
    _emit(ev, d) { (this._h[ev] || []).forEach((f) => f(d)); },
  });
  const a = mk(), b = mk();
  let dropped = false;
  const deliver = (to, u8) => {
    if (latencyMs) setTimeout(() => to._emit('data', u8), latencyMs);
    else setImmediate(() => to._emit('data', u8));
  };
  const norm = (data) => (typeof data === 'string'
    ? new TextEncoder().encode(data)
    : new Uint8Array(data.buffer ? data : new Uint8Array(data)));

  let payloads = 0;
  a.send = (data) => {
    a.sent++;
    const u8 = norm(data);
    // Flip a bit in the Nth binary frame to simulate on-wire corruption.
    if (corruptNthPayload !== null && u8.byteLength > 64) {
      payloads++;
      if (payloads === corruptNthPayload) u8[u8.byteLength - 1] ^= 0xff;
    }
    // Mimic a real datachannel: the local send buffer fills and drains.
    if (drainBytesPerTick) a.bufferedAmount += u8.byteLength;
    if (dropFirst && !dropped && a.sent === dropFirst) { dropped = true; return; }
    deliver(b, u8.slice());
  };
  b.send = (data) => deliver(a, norm(data).slice());

  if (drainBytesPerTick) {
    const t = setInterval(() => {
      a.bufferedAmount = Math.max(0, a.bufferedAmount - drainBytesPerTick);
    }, 10);
    t.unref?.();
  }
  return [a, b];
}

function makeFile(name, bytes, mime = 'application/octet-stream') {
  const f = new File([bytes], name, { type: mime });
  f.relativePath = name;
  return f;
}

const randomBytes = (n) => {
  const u = new Uint8Array(n);
  for (let i = 0; i < n; i++) u[i] = (i * 37 + (i >> 8) * 11) & 0xff;  // deterministic
  return u;
};

async function readBlob(url) {
  const blob = objectUrls.get(url);
  return new Uint8Array(await blob.arrayBuffer());
}

const eq = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

// =========================================================================
console.log('\n=== 1. single file, byte-exact round trip ===');
{
  const [sp, rp] = peerPair();
  const data = randomBytes(300 * 1024);          // ~4.6 chunks at 64 KB
  const file = makeFile('alpha.bin', data);

  const got = [];
  const recv = new RecvEngine(rp, {
    onApprovalNeeded: ({ accept }) => accept(),
    onFile: (rec) => got.push(rec),
  });
  rp.on('data', (d) => recv.onData(d));

  const sendEngine = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let msg = null;
    try { msg = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (msg && msg.type) sendEngine.handleControl(msg);
  });

  const res = await sendEngine.sendAll([file]);
  await new Promise((r) => setTimeout(r, 120));

  check('not cancelled', res.cancelled === false, JSON.stringify(res));
  check('one file completed', got.length === 1, `got ${got.length}`);
  check('reported size matches', got[0] && got[0].size === data.length);
  check('marked complete', got[0] && got[0].complete === true);
  const out = got[0] ? await readBlob(got[0].url) : new Uint8Array();
  check('bytes identical', eq(out, data), `${out.length} vs ${data.length}`);
}

// =========================================================================
console.log('\n=== 2. three files incl. empty + folder path ===');
{
  const [sp, rp] = peerPair();
  const files = [
    makeFile('one.bin', randomBytes(70 * 1024)),
    makeFile('nested/two.txt', new TextEncoder().encode('hello dropit'), 'text/plain'),
    makeFile('three.bin', randomBytes(1)),
  ];
  const got = [];
  const recv = new RecvEngine(rp, {
    onApprovalNeeded: ({ accept }) => accept(),
    onFile: (rec) => got.push(rec),
  });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });

  await se.sendAll(files);
  await new Promise((r) => setTimeout(r, 150));

  check('all three arrived', got.length === 3, `got ${got.length}`);
  check('folder path preserved',
        got.some((g) => g.relativePath === 'nested/two.txt'),
        JSON.stringify(got.map((g) => g.relativePath)));
  const two = got.find((g) => g.name === 'nested/two.txt' || g.relativePath === 'nested/two.txt');
  const txt = two ? new TextDecoder().decode(await readBlob(two.url)) : '';
  check('text content intact', txt === 'hello dropit', JSON.stringify(txt));
  const three = got.find((g) => g.relativePath === 'three.bin');
  check('1-byte file intact', three && three.bytes === 1, three && three.bytes);
}

// =========================================================================
console.log('\n=== 3. acks drain the window ===');
{
  const [sp, rp] = peerPair();
  const data = randomBytes(2 * 1024 * 1024);     // 32 chunks = exactly WINDOW_CHUNKS
  const recv = new RecvEngine(rp, { onApprovalNeeded: ({ accept }) => accept() });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });

  const t0 = Date.now();
  const res = await Promise.race([
    se.sendAll([makeFile('big.bin', data)]),
    new Promise((r) => setTimeout(() => r({ timeout: true }), 15000)),
  ]);
  check('completed without deadlock', !res.timeout, `took ${Date.now() - t0}ms`);
  check('unacked drained to zero', se.unacked.size === 0, `${se.unacked.size} left`);
  check('all bytes accounted', se.sent === data.length, `${se.sent} vs ${data.length}`);
}

// =========================================================================
console.log('\n=== 4. pause really stops, resume continues ===');
{
  const [sp, rp] = peerPair();
  const data = randomBytes(1024 * 1024);
  const recv = new RecvEngine(rp, { onApprovalNeeded: ({ accept }) => accept() });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });

  const done = se.sendAll([makeFile('p.bin', data)]);
  await new Promise((r) => setTimeout(r, 25));
  se.pause();
  // The chunk already past the gate finishes (read + hash + gzip + send are
  // awaited), so allow exactly one more, then require a hard stop.
  await new Promise((r) => setTimeout(r, 120));
  const settled = se.sent;
  await new Promise((r) => setTimeout(r, 250));
  const afterWait = se.sent;
  check('paused: stays stopped', afterWait === settled, `${settled} -> ${afterWait}`);
  check('paused flag set', se.paused === true);
  se.resume();
  const res = await done;
  check('resumed to completion', se.sent === data.length, `${se.sent} vs ${data.length}`);
  check('not marked cancelled', res.cancelled === false);
}

// =========================================================================
console.log('\n=== 5. cancel stops mid-flight ===');
{
  // Throttled link so cancellation genuinely lands mid-flight, as on a real
  // datachannel. With a zero-latency loopback the whole file flushes instantly.
  const [sp, rp] = peerPair({ latencyMs: 1, drainBytesPerTick: 256 * 1024 });
  const data = randomBytes(16 * 1024 * 1024);
  let phase = null;
  const recv = new RecvEngine(rp, {
    onApprovalNeeded: ({ accept }) => accept(),
    onState: (s) => { phase = s.phase; },
  });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });

  const done = se.sendAll([makeFile('c.bin', data)]);
  await new Promise((r) => setTimeout(r, 40));
  se.cancelAll();
  const res = await done;
  await new Promise((r) => setTimeout(r, 120));
  check('result flags cancelled', res.cancelled === true, JSON.stringify(res));
  check('stopped before the end', se.sent < data.length, `${se.sent} of ${data.length}`);
  check('receiver saw cancellation', phase === 'cancelled', `phase=${phase}`);
}

// =========================================================================
console.log('\n=== 6. decline is honoured ===');
{
  const [sp, rp] = peerPair();
  const recv = new RecvEngine(rp, { onApprovalNeeded: ({ decline }) => decline() });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });
  const res = await se.sendAll([makeFile('d.bin', randomBytes(70 * 1024))]);
  check('declined reported', res.declined === true, JSON.stringify(res));
  check('nothing was sent', se.sent === 0, `${se.sent} bytes`);
}

// =========================================================================
console.log('\n=== 7. resume-state restarts from the right chunk ===');
{
  const [sp, rp] = peerPair();
  const recv = new RecvEngine(rp, { onApprovalNeeded: ({ accept }) => accept() });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });

  // Pretend the receiver already holds chunks 0-3 of file 1.
  se.handleControl({ type: 'resume-state', fileId: 1, nextIndex: 4 });
  check('resumeFrom recorded', se.resumeFrom.get(1) === 4, se.resumeFrom.get(1));

  const data = randomBytes(640 * 1024);     // 10 chunks
  await se.sendAll([makeFile('r.bin', data)]);
  await new Promise((r) => setTimeout(r, 120));
  // 4 chunks credited up front + 6 actually sent = full size accounted.
  check('accounting includes skipped chunks', se.sent === data.length,
        `${se.sent} vs ${data.length}`);
}

// =========================================================================
console.log('\n=== 8. out-of-order frame is reordered, not corrupted ===');
{
  const { frame, unframe } = await import(new URL('protocol.js', JS));
  const recvOnly = new RecvEngine({ send() {}, bufferedAmount: 0 }, {});
  const a = new TextEncoder().encode('AAAA');
  const b = new TextEncoder().encode('BBBB');
  const c = new TextEncoder().encode('CCCC');

  await recvOnly.onData(new TextEncoder().encode(JSON.stringify({
    type: 'file-start', fileId: 1, name: 'o.txt', size: 12,
    mime: 'text/plain', chunkSize: 4, chunkCount: 3, startIndex: 0,
  })));
  await recvOnly.onData(frame(1, 0, a));
  await recvOnly.onData(frame(1, 2, c));   // early
  await recvOnly.onData(frame(1, 1, b));   // fills the gap
  await recvOnly.onData(new TextEncoder().encode(JSON.stringify({ type: 'file-end', fileId: 1 })));

  const rec = recvOnly.files[0];
  const text = rec ? new TextDecoder().decode(await readBlob(rec.url)) : '';
  check('reassembled in order', text === 'AAAABBBBCCCC', JSON.stringify(text));
  check('frame header round-trips', (() => {
    const f = unframe(frame(7, 1234, a));
    return f.fileId === 7 && f.chunkIndex === 1234;
  })());
}

// =========================================================================
console.log('\n=== 9. duplicate frame after resume is ignored ===');
{
  const { frame } = await import(new URL('protocol.js', JS));
  const r = new RecvEngine({ send() {}, bufferedAmount: 0 }, {});
  const a = new TextEncoder().encode('XXXX');
  await r.onData(new TextEncoder().encode(JSON.stringify({
    type: 'file-start', fileId: 1, name: 'dup.txt', size: 8,
    mime: 'text/plain', chunkSize: 4, chunkCount: 2, startIndex: 0,
  })));
  await r.onData(frame(1, 0, a));
  await r.onData(frame(1, 0, a));          // duplicate
  await r.onData(frame(1, 1, a));
  await r.onData(new TextEncoder().encode(JSON.stringify({ type: 'file-end', fileId: 1 })));
  const rec = r.files[0];
  const out = rec ? await readBlob(rec.url) : new Uint8Array();
  check('duplicate dropped', out.length === 8, `${out.length} bytes (expected 8)`);
}

// =========================================================================
console.log('\n=== 10. integrity: clean transfer verifies ===');
{
  const [sp, rp] = peerPair();
  const data = randomBytes(300 * 1024);
  let rec = null;
  const recv = new RecvEngine(rp, {
    onApprovalNeeded: ({ accept }) => accept(),
    onFile: (r) => { rec = r; },
  });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });
  await se.sendAll([makeFile('ok.bin', data)]);
  await new Promise((r) => setTimeout(r, 150));
  check('integrity = verified', rec && rec.integrity === 'verified', rec && rec.integrity);
  check('hash is 64 hex chars', rec && /^[0-9a-f]{64}$/.test(rec.sha256 || ''),
        rec && rec.sha256);
}

// =========================================================================
console.log('\n=== 11. integrity: corruption is caught ===');
{
  const [sp, rp] = peerPair({ corruptNthPayload: 3 });
  let rec = null, failed = null;
  const recv = new RecvEngine(rp, {
    onApprovalNeeded: ({ accept }) => accept(),
    onFile: (r) => { rec = r; },
    onIntegrityFailure: (r) => { failed = r; },
  });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  se.compress = false;          // isolate the hash check from gzip's own CRC
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });
  await se.sendAll([makeFile('bad.bin', randomBytes(400 * 1024))]);
  await new Promise((r) => setTimeout(r, 200));
  check('integrity = MISMATCH', rec && rec.integrity === 'MISMATCH', rec && rec.integrity);
  check('onIntegrityFailure fired', failed !== null);
  // The byte count still adds up — only the hash reveals the damage.
  check('length check alone would have passed', rec && rec.complete === true);
}

// =========================================================================
console.log('\n=== 12. integrity: resumed file is not falsely verified ===');
{
  const [sp, rp] = peerPair();
  let rec = null;
  const recv = new RecvEngine(rp, {
    onApprovalNeeded: ({ accept }) => accept(),
    onFile: (r) => { rec = r; },
  });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });
  se.handleControl({ type: 'resume-state', fileId: 1, nextIndex: 2 });
  await se.sendAll([makeFile('res.bin', randomBytes(640 * 1024))]);
  await new Promise((r) => setTimeout(r, 150));
  check('not falsely verified', rec && rec.integrity !== 'verified', rec && rec.integrity);
  check('reported skipped or partial',
        rec && ['skipped', 'partial'].includes(rec.integrity), rec && rec.integrity);
}

// =========================================================================
console.log('\n=== 13. rebind() clears the stale send window ===');
{
  const [sp] = peerPair();
  const [sp2] = peerPair();
  const se = new SendEngine(sp, {});
  se.unacked.set('1:0', 1024);
  se.interrupted = true;
  se.rebind(sp2);
  check('window cleared on rebind', se.unacked.size === 0, se.unacked.size);
  check('interrupted flag reset', se.interrupted === false);
  check('peer swapped', se.peer === sp2);
}

// =========================================================================
console.log('\n=== 14. compression: text shrinks on the wire, arrives intact ===');
{
  const [sp, rp] = peerPair();
  // Highly compressible payload.
  const text = new TextEncoder().encode('the quick brown fox '.repeat(20000));
  let rec = null;
  const recv = new RecvEngine(rp, {
    onApprovalNeeded: ({ accept }) => accept(),
    onFile: (r) => { rec = r; },
  });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });

  await se.sendAll([makeFile('big.txt', text, 'text/plain')]);
  await new Promise((r) => setTimeout(r, 250));

  check('wire bytes < plaintext bytes', se.wireBytes < text.length,
        `${se.wireBytes} wire vs ${text.length} plain`);
  check('compression ratio better than 50%', se.wireBytes < text.length * 0.5,
        `${((se.wireBytes / text.length) * 100).toFixed(1)}%`);
  const out = rec ? await readBlob(rec.url) : new Uint8Array();
  check('decompressed bytes identical', eq(out, text), `${out.length} vs ${text.length}`);
  check('integrity still verified', rec && rec.integrity === 'verified',
        rec && rec.integrity);
}

// =========================================================================
console.log('\n=== 15. compression: already-compressed data is left alone ===');
{
  const [sp, rp] = peerPair();
  const jpeg = randomBytes(200 * 1024);
  const recv = new RecvEngine(rp, { onApprovalNeeded: ({ accept }) => accept() });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });
  await se.sendAll([makeFile('photo.jpg', jpeg, 'image/jpeg')]);
  await new Promise((r) => setTimeout(r, 200));
  // JPEG is in the skip list, so the wire carries the plaintext unchanged.
  check('jpeg not inflated by gzip framing', se.wireBytes === jpeg.length,
        `${se.wireBytes} vs ${jpeg.length}`);
}

// =========================================================================
console.log('\n=== 16. compression: a corrupted gzip chunk is rejected ===');
{
  const [sp, rp] = peerPair({ corruptNthPayload: 2 });
  let errored = false, rec = null;
  const recv = new RecvEngine(rp, {
    onApprovalNeeded: ({ accept }) => accept(),
    onFile: (r) => { rec = r; },
    onError: () => { errored = true; },
  });
  rp.on('data', (d) => recv.onData(d));
  const se = new SendEngine(sp, {});
  sp.on('data', (d) => {
    let m = null;
    try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(d)); } catch {}
    if (m && m.type) se.handleControl(m);
  });
  const text = new TextEncoder().encode('compressible payload '.repeat(20000));
  await se.sendAll([makeFile('corrupt.txt', text, 'text/plain')]);
  await new Promise((r) => setTimeout(r, 300));
  // gzip carries its own CRC, so damage is caught before the hash stage.
  check('corruption surfaced', errored || (rec && rec.integrity !== 'verified'),
        `errored=${errored} integrity=${rec && rec.integrity}`);
  check('no silently-good file produced', !(rec && rec.integrity === 'verified'));
}

// =========================================================================
console.log('\n=== 17. frame flags round-trip ===');
{
  const { frame, unframe, FLAG_GZIP, HEADER_BYTES } = await import(new URL('protocol.js', JS));
  check('header is 9 bytes', HEADER_BYTES === 9, HEADER_BYTES);
  const body = new TextEncoder().encode('payload');
  const f = unframe(frame(42, 7, body, FLAG_GZIP));
  check('fileId preserved', f.fileId === 42, f.fileId);
  check('chunkIndex preserved', f.chunkIndex === 7, f.chunkIndex);
  check('gzip flag preserved', (f.flags & FLAG_GZIP) === FLAG_GZIP, f.flags);
  check('payload preserved', new TextDecoder().decode(f.payload) === 'payload');
  const plain = unframe(frame(1, 1, body));
  check('no flag when unset', plain.flags === 0, plain.flags);
}

console.log(`\n${'='.repeat(46)}\n  ${PASS} passed, ${FAIL} failed\n${'='.repeat(46)}`);
process.exit(FAIL ? 1 : 0);

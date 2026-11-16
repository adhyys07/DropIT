/* DropIT datachannel protocol.
   Control messages: JSON strings.  Payload: binary frames with an 8-byte header.
   Header: u32 fileId (big-endian), u32 chunkIndex (big-endian).  */

/* Frame header: u32 fileId, u32 chunkIndex, u8 flags (big-endian). */
export const HEADER_BYTES = 9;
export const FLAG_GZIP = 1;
export const DEFAULT_CHUNK = 64 * 1024;  
export const WINDOW_CHUNKS = 32;          
export const HIGH_WATER   = 4 * 1024 * 1024;

export const MSG = {
  HELLO:        'hello',          
  MANIFEST:     'manifest',       
  REQ_APPROVE:  'request-approve',
  APPROVE:      'approve',        
  FILE_START:   'file-start',
  FILE_END:     'file-end',
  ACK:          'ack',            
  PAUSE:        'pause',          
  RESUME:       'resume',         
  CANCEL:       'cancel',         
  RESUME_STATE: 'resume-state',   
  CLIPBOARD:    'clipboard',      
  CHAT:         'chat',
  TYPING:       'typing',
  DONE:         'done',
  FILE_HASH:    'file-hash',     
  HASH_RESULT:  'hash-result', 
};

export function frame(fileId, chunkIndex, payload, flags = 0) {
  const body = payload instanceof ArrayBuffer ? new Uint8Array(payload) : payload;
  const out = new Uint8Array(HEADER_BYTES + body.byteLength);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, fileId);
  dv.setUint32(4, chunkIndex);
  dv.setUint8(8, flags);
  out.set(body, HEADER_BYTES);
  return out;
}

export function unframe(data) {
  const u8 = data instanceof Uint8Array ? data : new Uint8Array(data);
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  return {
    fileId: dv.getUint32(0),
    chunkIndex: dv.getUint32(4),
    flags: dv.getUint8(8),
    payload: u8.subarray(HEADER_BYTES),
  };
}

export function readControl(data) {
  if (typeof data === 'string') { try { return JSON.parse(data); } catch { return null; } }
  if (data.byteLength > 0 && data.byteLength < 65536) {
    try {
      const txt = new TextDecoder('utf-8', { fatal: true }).decode(data);
      const v = JSON.parse(txt);
      return (v && typeof v === 'object' && v.type) ? v : null;
    } catch { return null; }
  }
  return null;
}

export const send = (peer, obj) => peer.send(JSON.stringify(obj));

export function maxChunkFor(peer) {
  try {
    const sctp = peer._pc && peer._pc.sctp;
    if (sctp && sctp.maxMessageSize) {
      return Math.max(16 * 1024, Math.min(256 * 1024, sctp.maxMessageSize - HEADER_BYTES - 64));
    }
  } catch {}
  return DEFAULT_CHUNK;
}

export const fmtBytes = (n) => {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: i ? 1 : 0 })
           .format(n / 1024 ** i) + ' ' + u[i];
};

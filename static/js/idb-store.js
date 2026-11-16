const DB = 'dropit';
const STORE = 'chunks';
const META = 'meta';
const VERSION = 1;

function open() {
    return new Promise((res, rej) => {
    const r = indexedDB.open(DB, VERSION);
    r.onupgradeneeded = () => {
      const db = r.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
      if (!db.objectStoreNames.contains(META)) db.createObjectStore(META);
    };
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}

const tx = async (store, mode, fn) => {
  const db = await open();
  return new Promise((res, rej) => {
    const t = db.transaction(store, mode);
    const out = fn(t.objectStore(store));
    t.oncomplete = () => res(out && out.result !== undefined ? out.result : out);
    t.onerror = () => rej(t.error);
  });
};

const key = (sessionId, fileId, index) => `${sessionId}/${fileId}/${index}`;

export const putChunk = (sessionId, fileId, index, u8) =>
  tx(STORE, 'readwrite', (s) => s.put(u8, key(sessionId, fileId, index)));

export const putMeta = (sessionId, fileId, meta) =>
  tx(META, 'readwrite', (s) => s.put(meta, `${sessionId}/${fileId}`));

export const getMeta = (sessionId, fileId) =>
  tx(META, 'readonly', (s) => s.get(`${sessionId}/${fileId}`));

export async function listSessions() {
  const db = await open();
  return new Promise((res, rej) => {
    const out = [];
    const t = db.transaction(META, 'readonly');
    const req = t.objectStore(META).openCursor();
    req.onsuccess = () => {
      const c = req.result;
      if (!c) return res(out);
      out.push({ key: c.key, ...c.value });
      c.continue();
    };
    req.onerror = () => rej(req.error);
    });
}

export async function assemble(sessionId, fileId, chunkCount, mime) {
  const db = await open();
  const parts = [];
  let missing = 0;
  for (let i = 0; i < chunkCount; i++) {
    const part = await new Promise((res, rej) => {
      const r = db.transaction(STORE, 'readonly').objectStore(STORE)
                  .get(key(sessionId, fileId, i));
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
    if (part) parts.push(part); else missing++;
  }
  if (missing) {
    console.warn(`[idb] ${missing}/${chunkCount} chunks missing for file ${fileId}`);
  }
  return new Blob(parts, { type: mime || 'application/octet-stream' });
}

export async function dropSession(sessionId) {
    const db = await open();
    for (const store of [STORE, META]){
    await new Promise((res) => {
      const t = db.transaction(store, 'readwrite');
      const os = t.objectStore(store);
      const req = os.openKeyCursor();
      req.onsuccess = () => {
        const c = req.result;
        if (!c) return;
        if (String(c.key).startsWith(sessionId + '/')) os.delete(c.key);
        c.continue();
      };
      t.oncomplete = res;
      t.onerror = res;
    });
  }
}

/** Lowest chunk index not yet stored — exactly where a resume must restart. */
export async function firstGap(sessionId, fileId, chunkCount) {
  const db = await open();
  for (let i = 0; i < chunkCount; i++) {
    // eslint-disable-next-line no-await-in-loop
    const has = await new Promise((res) => {
      const r = db.transaction(STORE, 'readonly').objectStore(STORE)
                  .getKey(key(sessionId, fileId, i));
      r.onsuccess = () => res(r.result !== undefined);
      r.onerror = () => res(false);
    });
    if (!has) return i;
  }
  return chunkCount;
}

export async function dropFile(sessionId, fileId) {
  const db = await open();
  const prefix = `${sessionId}/${fileId}/`;
  const metaKey = `${sessionId}/${fileId}`;
  await new Promise((res) => {
    const t = db.transaction([STORE, META], 'readwrite');
    const os = t.objectStore(STORE);
    const req = os.openKeyCursor();
    req.onsuccess = () => {
      const c = req.result;
      if (!c) return;
      if (String(c.key).startsWith(prefix)) os.delete(c.key);
      c.continue();
    };
    t.objectStore(META).delete(metaKey);
    t.oncomplete = res;
    t.onerror = res;
  });
}

export async function prune(maxAgeMs = 7 * 24 * 3600 * 1000) {
  const now = Date.now();
  const sessions = await listSessions();
  const stale = new Set(sessions.filter((s) => now - (s.at || 0) > maxAgeMs)
                                .map((s) => String(s.key).split('/')[0]));
  for (const id of stale) await dropSession(id);   // eslint-disable-line no-await-in-loop
  return stale.size;
}

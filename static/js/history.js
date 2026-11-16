const KEY = 'dropit.history.v1';
const MAX = 200;

const read = () => {
  try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch { return []; }
};

const write = (rows) => {
  try { localStorage.setItem(KEY, JSON.stringify(rows.slice(0, MAX))); }
  catch { /* quota: drop oldest half and retry once */
    try { localStorage.setItem(KEY, JSON.stringify(rows.slice(0, Math.floor(MAX / 2)))); } catch {}
  }
};

export const list = () => read();

export function record(entry) {
    const rows = read();
    rows.unshift({
    id: crypto.randomUUID(),
    at: Date.now(),
    direction: entry.direction,          
    peerName: entry.peerName || 'Unknown device',
    roomCode: entry.roomCode || null,
    files: (entry.files || []).map((f) => ({
      name: f.name, size: f.size, mime: f.mime || f.type || '',
    })),
    totalBytes: entry.totalBytes || 0,
    durationMs: entry.durationMs || 0,
    status: entry.status || 'complete', 
  });
  write(rows);
  return rows[0];
}


export function clear() { localStorage.removeItem(KEY); }
export function remove(id) { write(read().filter((r) => r.id !== id)); }

export function render(container) {
  const rows = read();
  container.textContent = '';
  if (!rows.length) {
    container.innerHTML = '<p class="muted">No transfers yet.</p>';
    return;
  }
  const fmtDate = new Intl.DateTimeFormat(undefined,
    { dateStyle: 'medium', timeStyle: 'short' });
  const table = document.createElement('table');
  table.innerHTML =
    '<thead><tr><th scope="col">When</th><th scope="col">Dir</th>' +
    '<th scope="col">Device</th><th scope="col">Files</th>' +
    '<th scope="col">Size</th><th scope="col">Status</th></tr></thead>';
  const tb = document.createElement('tbody');
  for (const r of rows) {
    const tr = document.createElement('tr');
    const names = r.files.map((f) => f.name).join(', ');
    tr.innerHTML =
      `<td style="white-space:nowrap">${fmtDate.format(r.at)}</td>` +
      `<td>${r.direction === 'sent' ? '&uarr; Sent' : '&darr; Received'}</td>` +
      `<td>${escapeHtml(r.peerName)}</td>` +
      `<td title="${escapeHtml(names)}">${r.files.length} &mdash; ${escapeHtml(names)}</td>` +
      `<td class="tnum">${fmtBytes(r.totalBytes)}</td>` +
      `<td>${escapeHtml(r.status)}</td>`;
    tb.append(tr);
  }
  table.append(tb);
  container.append(table);
}

const escapeHtml = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmtBytes = (n) => {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return (n / 1024 ** i).toFixed(i ? 1 : 0) + ' ' + u[i];
};
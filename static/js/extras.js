import { MSG, send } from './protocol.js';

const NAME_KEY = 'dropit.deviceName';
const ADJ = ['Swift','Quiet','Bright','Clever','Calm','Bold','Keen','Warm'];
const NOUN = ['Falcon','Otter','Cedar','Comet','Harbor','Lantern','Meadow','Quartz'];


export function deviceName() {
    let n = localStorage.getItem(NAME_KEY);
    if (!n) { n = guessPlatform() + ' ' + pick(ADJ) + '-' + pick(NOUN); setDeviceName(n); }
  return n;
}
export function setDeviceName(n) {
    localStorage.setItem(NAME_KEY, String(n).slice(0, 40));
    return localStorage.getItem(NAME_KEY);
}
const pick = (a) => a[Math.floor(Math.random() * a.length)];

function guessPlatform() {
  const d = navigator.userAgentData;
  if (d?.platform) return d.platform;
  const ua = navigator.userAgent;
  if (/iPhone|iPad/.test(ua)) return 'iOS';
  if (/Android/.test(ua)) return 'Android';
  if (/Mac/.test(ua)) return 'Mac';
  if (/Win/.test(ua)) return 'Windows';
  if (/Linux/.test(ua)) return 'Linux';
  return 'Device';
}

export function sendHello(peer, extra = {}) {
  send(peer, { type: MSG.HELLO, name: deviceName(), ...extra });
}

export async function shareClipboard(peer, text) {
    const payload = (text ?? '').toString();
    if (!payload.trim()) throw new Error('Nothing to share');
    if (payload.length > 1_000_000) throw new Error('Clipboard text too large (1 MB cap)');
    const isUrl = /^https?:\/\/\S+$/i.test(payload.trim());
    send(peer, { type: MSG.CLIPBOARD, text: payload, isUrl, at: Date.now() });
}

export async function readLocalClipboard() {
  if (!navigator.clipboard?.readText) throw new Error('Clipboard read unsupported');
  return navigator.clipboard.readText();
}

export async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }
}

export function renderClipboardCard(msg, mount) {
  const card = document.createElement('div');
  card.className = 'clipcard';

  const body = document.createElement(msg.isUrl ? 'a' : 'pre');
  if (msg.isUrl) {
    body.href = msg.text.trim();
    body.textContent = msg.text.trim();
    body.rel = 'noopener noreferrer';
    body.target = '_blank';
  } else {
    body.textContent = msg.text;
  }

  const meta = document.createElement('div');
  meta.className = 'cmeta';

  const when = document.createElement('span');
  when.textContent = new Intl.DateTimeFormat(undefined, { timeStyle: 'short' })
    .format(msg.at ? new Date(msg.at) : new Date());

  const kind = document.createElement('span');
  kind.textContent = msg.isUrl ? 'link' : `${msg.text.length} chars`;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-sm';
  btn.textContent = 'Copy';
  btn.onclick = async () => {
    btn.textContent = (await copyToClipboard(msg.text)) ? 'Copied' : 'Copy failed';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  };

  meta.append(when, kind, btn);
  card.append(body, meta);
  mount.prepend(card);
}

let originalTitle = null;

export async function enableNotifications() {
  if (!('Notification' in window)) return 'unsupported';
  if (Notification.permission === 'granted') return 'granted';
  if (Notification.permission === 'denied') return 'denied';
  return Notification.requestPermission();        // must follow a user gesture
}

export function notify(title, body, { tag = 'dropit', silent = false } = {}) {
  flashTitle(title);
  if (!('Notification' in window) || Notification.permission !== 'granted') return null;
  try {
    const n = new Notification(title, { body, tag, silent, icon: '/icons/icon-192.png' });
    n.onclick = () => { window.focus(); n.close(); };
    return n;
  } catch { return null; }
}


export function flashTitle(text) {
  if (!document.hidden) return;
  originalTitle ??= document.title;
  document.title = `• ${text}`;
  const restore = () => {
    if (originalTitle) { document.title = originalTitle; originalTitle = null; }
    document.removeEventListener('visibilitychange', restore);
  };
  document.addEventListener('visibilitychange', restore, { once: true });
}

export const notifyTransferStart = (n, who) =>
  notify('Transfer started', `${n} file${n === 1 ? '' : 's'} from ${who}`);
export const notifyTransferDone = (n, bytes, who) =>
  notify('Transfer complete', `${n} file${n === 1 ? '' : 's'} (${(bytes / 1048576).toFixed(1)} MB) from ${who}`);
export const notifyCancelled = (who) =>
  notify('Transfer cancelled', `${who} stopped the transfer`);